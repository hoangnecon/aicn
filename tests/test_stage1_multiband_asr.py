import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import numpy as np
import soundfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
SCRIPTS = os.path.join(ROOT, "scripts")
V1_SCRIPTS = SCRIPTS
V2_SCRIPTS = SCRIPTS
ORCH_SCRIPTS = SCRIPTS
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import analyze_voice_waveform as avw
import cicd_gate
import extract_audit_frames as eaf
import run_asr_and_scan as ras
import validate_bilingual_script as vbs


class _FakeWord:
    def __init__(self, start, end, word, probability=0.90):
        self.start = start
        self.end = end
        self.word = word
        self.probability = probability


class _FakeSeg:
    def __init__(self, start, end, text, avg_logprob=-0.25, no_speech_prob=0.05, compression_ratio=1.1, words=None):
        self.start = start
        self.end = end
        self.text = text
        self.avg_logprob = avg_logprob
        self.no_speech_prob = no_speech_prob
        self.compression_ratio = compression_ratio
        self.words = words


class TestStage1MultiBandASR(unittest.TestCase):
    sr = 16000

    def test_gap_first_catches_deadzone_normal_speech_and_splits_long_gaps(self):
        sr = self.sr
        total_s = 35
        t = np.arange(sr * total_s) / sr
        syl = (0.5 + 0.5 * np.sin(2 * np.pi * 4.5 * t)).astype(np.float32) ** 2
        speech = (syl * (0.065 * np.sin(2 * np.pi * 650 * t) + 0.035 * np.sin(2 * np.pi * 1500 * t))).astype(np.float32)
        voc = speech.copy()
        raw = speech.copy()

        existing = [{"id": 0, "start": 0.0, "end": 12.0, "text": "前面十二秒已经识别了"}]
        gaps = avw.detect_uncovered_multiband_gaps(existing, voc, raw, sr=sr, max_window_s=10.0)

        self.assertGreaterEqual(len(gaps), 2, f"Expected 23s uncovered region split into >=2 windows, got {gaps}")
        for g in gaps:
            self.assertGreaterEqual(g["start"], 12.0, f"Protective collar leaked covered region into gap: {g}")
            self.assertLessEqual(g["duration"], 10.2, f"Gap exceeded max_window_s=10.0: {g}")
        covered_by_gaps = sum(g["duration"] for g in gaps)
        self.assertGreaterEqual(covered_by_gaps, 18.0, f"Dead-zone speech was missed: {gaps}")

    def test_multiband_high_mid_low_and_reverse_raw_recovery(self):
        sr = self.sr
        t2 = np.arange(sr * 2) / sr
        sil2 = np.zeros(sr * 2, dtype=np.float32)
        sil1 = np.zeros(sr * 1, dtype=np.float32)
        syl = (0.5 + 0.5 * np.sin(2 * np.pi * 5.0 * t2)).astype(np.float32) ** 2

        child_hi = (syl * (0.055 * np.sin(2 * np.pi * 2400 * t2) + 0.045 * np.sin(2 * np.pi * 3800 * t2))).astype(np.float32)
        bass = (0.14 * np.sin(2 * np.pi * 90 * t2)).astype(np.float32)
        phone_mid = (syl * (0.060 * np.sin(2 * np.pi * 700 * t2) + 0.045 * np.sin(2 * np.pi * 1500 * t2))).astype(np.float32)
        hi_hiss = (0.14 * np.sin(2 * np.pi * 5200 * t2)).astype(np.float32)
        male_lo = (syl * (0.055 * np.sin(2 * np.pi * 130 * t2) + 0.045 * np.sin(2 * np.pi * 220 * t2))).astype(np.float32)
        hi_sfx = (0.16 * np.sin(2 * np.pi * 5500 * t2)).astype(np.float32)
        steady = (0.045 * np.sin(2 * np.pi * 1500 * t2)).astype(np.float32)

        voc = np.concatenate([sil1, 0.05 * child_hi, sil2, sil2, sil2, male_lo, sil2, sil2, sil1])
        raw = np.concatenate([sil1, child_hi + bass, sil2, phone_mid + bass + hi_hiss, sil2, male_lo + hi_sfx, sil2, steady, sil1])

        class _SpectralFakeModel:
            def transcribe(self, clip, **kwargs):
                spec = np.abs(np.fft.rfft(clip)) ** 2
                freqs = np.fft.rfftfreq(len(clip), 1.0 / sr)
                tot = float(spec.sum() + 1e-12)
                e_bass = float(spec[freqs < 105].sum()) / tot
                e_lo = float(spec[(freqs >= 110) & (freqs < 280)].sum()) / tot
                e_mid = float(spec[(freqs >= 550) & (freqs < 1800)].sum()) / tot
                e_hi = float(spec[(freqs >= 2100) & (freqs < 4500)].sum()) / tot
                e_sfx = float(spec[freqs >= 4600].sum()) / tot

                if e_hi > 0.45 and e_bass < 0.05 and e_sfx < 0.10:
                    return [_FakeSeg(0.25, 1.95, "小舅我来啦", avg_logprob=-0.22, no_speech_prob=0.05)], {}
                if e_mid > 0.55 and e_bass < 0.05 and e_sfx < 0.10 and e_hi < 0.25:
                    return [_FakeSeg(0.25, 1.95, "喂听得见吗", avg_logprob=-0.28, no_speech_prob=0.08)], {}
                if e_lo > 0.55 and e_sfx < 0.05:
                    return [_FakeSeg(0.25, 1.95, "把行李拿过来", avg_logprob=-0.26, no_speech_prob=0.06)], {}
                return [], {}

        with tempfile.TemporaryDirectory() as td:
            rep_p = os.path.join(td, "01_pass2b_recovery_report.json")
            out, report = ras.run_multiband_pass2b_recovery(
                model=_SpectralFakeModel(),
                results=[],
                voc_audio=voc,
                raw_audio=raw,
                whisper_lang="zh",
                source_lang="zh",
                sr=sr,
                report_path=rep_p,
            )
            texts = [s["text"] for s in out]
            self.assertIn("小舅我来啦", texts, f"High-band child voice missed: {out}")
            self.assertIn("喂听得见吗", texts, f"Mid-band phone voice missed: {out}")
            self.assertIn("把行李拿过来", texts, f"Low-band deep voice missed: {out}")
            self.assertFalse(any(12.8 <= s["start"] <= 15.2 for s in out), f"Steady instrument falsely transcribed: {out}")
            self.assertTrue(os.path.exists(rep_p))
            self.assertEqual(report["recovered_total"], 3)
            self.assertGreaterEqual(report["recovered_by_band"]["high"], 1)
            self.assertGreaterEqual(report["recovered_by_band"]["mid"], 1)
            self.assertGreaterEqual(report["recovered_by_band"]["low"], 1)

    def test_single_char_exclamation_and_hallucination_guard(self):
        sr = self.sr
        wave = (0.10 * np.sin(2 * np.pi * 800 * np.arange(sr * 2) / sr)).astype(np.float32)

        class _MixedModel:
            def transcribe(self, clip, **kwargs):
                return [
                    _FakeSeg(0.1, 0.5, "滚！", avg_logprob=-0.25, no_speech_prob=0.05),
                    _FakeSeg(0.6, 1.0, "啊", avg_logprob=-0.42, no_speech_prob=0.28),
                    _FakeSeg(1.1, 1.7, "谢谢观看", avg_logprob=-0.10, no_speech_prob=0.01),
                ], {}

        cands = ras._probe_band_clip(
            model=_MixedModel(), wave=wave, pad_s=0.0, pad_e=2.0, gst=0.0, get=2.0,
            band_name="full_vocal", whisper_lang="zh", source_lang="zh", peak_voc_rms=0.08, sr=sr,
        )
        texts = [c["text"] for c in cands]
        self.assertIn("滚！", texts, "High-confidence loud 1-char exclamation must be kept")
        self.assertNotIn("啊", texts, "Borderline 1-char utterance must be dropped under v3 thresholds")
        self.assertNotIn("谢谢观看", texts, "Hallucination substring must be rejected")

    def test_smart_acoustic_gate_flags_unprobed_vocal_gap_and_allows_verified_bgm(self):
        sr = self.sr
        sil2 = np.zeros(sr * 2, dtype=np.float32)
        t2 = np.arange(sr * 2) / sr
        syl = (0.5 + 0.5 * np.sin(2 * np.pi * 5.0 * t2)).astype(np.float32) ** 2
        loud_voice = (syl * (0.12 * np.sin(2 * np.pi * 700 * t2) + 0.08 * np.sin(2 * np.pi * 1600 * t2))).astype(np.float32)
        voc = np.concatenate([sil2, loud_voice, sil2])

        with tempfile.TemporaryDirectory() as td:
            for sub in ("data", "media/stems"):
                os.makedirs(os.path.join(td, sub), exist_ok=True)
            soundfile.write(os.path.join(td, "media/stems/vocals.wav"), voc, sr)
            soundfile.write(os.path.join(td, "media/source_audio.wav"), voc, sr)
            with open(os.path.join(td, "data/00_video_profile.json"), "w", encoding="utf-8") as f:
                json.dump({"duration": 6.0, "source_video": os.path.join(td, "media/source_audio.wav")}, f)
            with open(os.path.join(td, "data/01_asr_raw_segments.json"), "w", encoding="utf-8") as f:
                json.dump([], f)

            rep1 = avw.analyze_waveform_and_clauses(td)
            self.assertGreaterEqual(rep1["uncovered_acoustic_bursts_count"], 1, f"Blind gate failed to flag unprobed vocal gap: {rep1}")
            self.assertLess(rep1["acoustic_speech_coverage_ratio"], 1.0)
            self.assertGreaterEqual(rep1["longest_uncovered_vocal_gap_s"], 1.0)

            with open(os.path.join(td, "data/01_pass2b_recovery_report.json"), "w", encoding="utf-8") as f:
                json.dump({
                    "gaps_probed": 1,
                    "recovered_total": 0,
                    "recovered_by_band": {},
                    "verified_non_speech_gaps": [{"start": 1.9, "end": 4.1, "duration": 2.2}],
                }, f)

            rep2 = avw.analyze_waveform_and_clauses(td)
            self.assertEqual(rep2["uncovered_acoustic_bursts_count"], 0, f"Verified non-speech gap deadlocked gate: {rep2}")
            self.assertEqual(rep2["acoustic_speech_coverage_ratio"], 1.0)
            self.assertEqual(rep2["longest_uncovered_vocal_gap_s"], 0.0)

    def test_v3_versions_and_dead_v1_code_purged(self):
        self.assertEqual(ras.ASR_PIPELINE_VERSION, "asr-4.1")
        self.assertEqual(avw.WAVEFORM_CACHE_VERSION, "wf-4.1")
        self.assertEqual(ras._initial_prompt_for("zh"), "以下是普通话的句子，使用简体中文。")
        self.assertIsNone(ras._initial_prompt_for("en"))
        self.assertFalse(hasattr(ras, "scan_hardsub_and_scenes"), "Dead v1.x scan_hardsub_and_scenes must be purged")
        self.assertFalse(hasattr(ras, "find_hardsub_intervals"), "Dead v1.x find_hardsub_intervals must be purged")
        self.assertFalse(hasattr(avw, "compute_audio_spectral_profile"), "Redundant 16kHz spectral toy must be purged")

    def test_stitched_pass2b_blocks_prechop_hallucination_and_bloated_pass1_hiding(self):
        sr = self.sr
        t2 = np.arange(sr * 2) / sr
        sil1 = np.zeros(sr * 1, dtype=np.float32)
        syl = (0.5 + 0.5 * np.sin(2 * np.pi * 5.0 * t2)).astype(np.float32) ** 2
        sp_a = (syl * (0.10 * np.sin(2 * np.pi * 650 * t2))).astype(np.float32)
        sp_b = (syl * (0.09 * np.sin(2 * np.pi * 750 * t2))).astype(np.float32)
        voc = np.concatenate([sp_a, sil1, sil1, sp_b, sil1])
        bloated_pass1 = [{"id": 0, "start": 0.0, "end": 6.2, "text": "好吧"}]
        gaps = avw.detect_uncovered_multiband_gaps(bloated_pass1, voc, voc, sr=sr)
        self.assertTrue(any(3.7 <= g["start"] <= 4.3 and g["end"] >= 5.5 for g in gaps), f"Bloated Pass 1 hid speech B: {gaps}")

        seen_kwargs = []

        class _HallucinatingStitchModel:
            def transcribe(self, buf, **kwargs):
                seen_kwargs.append(kwargs)
                bad = _FakeSeg(
                    0.1, 4.2, "请不吝点赞 订阅 转发 打赏支持明镜与点点栏目",
                    avg_logprob=-0.20, no_speech_prob=0.05,
                    words=[_FakeWord(0.2, 0.9, "请不"), _FakeWord(1.0, 1.4, "吝"), _FakeWord(2.5, 4.0, "镜与点点栏目")],
                )
                return [bad], {}

        out = ras._probe_band_gaps_stitched(
            model=_HallucinatingStitchModel(),
            wave=voc,
            gap_entries=[(0, {"start": 0.5, "end": 2.0, "peak_voc_rms": 0.08}), (1, {"start": 4.0, "end": 5.8, "peak_voc_rms": 0.08})],
            band_name="full_vocal",
            whisper_lang="zh",
            source_lang="zh",
            total_dur=7.0,
            sr=sr,
            force_stitch=True,
        )
        self.assertNotIn("initial_prompt", seen_kwargs[0], "Pass 2B vad_filter=False must never pass initial_prompt")
        self.assertEqual(out[0], [], f"Chopped hallucination leaked into slot 0: {out}")
        self.assertEqual(out[1], [], f"Chopped hallucination leaked into slot 1: {out}")

    def test_stitched_multigap_batching_packs_gaps_into_one_call(self):
        sr = self.sr
        t = np.arange(sr * 20) / sr
        wave = (0.08 * np.sin(2 * np.pi * 900 * t)).astype(np.float32)
        gaps = [
            (0, {"start": 1.0, "end": 2.2, "peak_voc_rms": 0.05}),
            (1, {"start": 5.0, "end": 6.4, "peak_voc_rms": 0.05}),
            (2, {"start": 10.0, "end": 11.3, "peak_voc_rms": 0.05}),
            (3, {"start": 15.0, "end": 16.2, "peak_voc_rms": 0.05}),
        ]
        calls = []

        class _BatchSpyModel:
            def transcribe(self, buf, **kwargs):
                calls.append(len(buf) / sr)
                s0 = _FakeSeg(0.25, 1.35, "第一句", avg_logprob=-0.20, no_speech_prob=0.05,
                              words=[_FakeWord(0.30, 0.80, "第一"), _FakeWord(0.80, 1.30, "句")])
                s1 = _FakeSeg(2.65, 3.85, "第二句", avg_logprob=-0.22, no_speech_prob=0.06,
                              words=[_FakeWord(2.70, 3.30, "第二"), _FakeWord(3.30, 3.80, "句")])
                return [s0, s1], {}

        out = ras._probe_band_gaps_stitched(
            model=_BatchSpyModel(),
            wave=wave,
            gap_entries=gaps,
            band_name="full_vocal",
            whisper_lang="zh",
            source_lang="zh",
            total_dur=20.0,
            sr=sr,
            force_stitch=True,
        )
        self.assertEqual(len(calls), 1, f"4 short gaps must be packed into 1 single Whisper call, got {calls}")
        self.assertEqual(out[0][0]["text"], "第一句")
        self.assertGreaterEqual(out[0][0]["start"], 1.0)
        self.assertLessEqual(out[0][0]["end"], 2.2)
        self.assertEqual(out[1][0]["text"], "第二句")
        self.assertGreaterEqual(out[1][0]["start"], 5.0)
        self.assertLessEqual(out[1][0]["end"], 6.4)
        self.assertEqual(out[2], [])
        self.assertEqual(out[3], [])

    def test_pass1_dtw_orphan_guard_merges_single_char_splits(self):
        sr = self.sr
        voc = np.zeros(sr * 10, dtype=np.float32)
        # Voice actually occurs at [5.0..6.5s]; '卫' drifted early onto silence at 1.0..1.4s
        t_v = np.arange(int(1.5 * sr)) / sr
        voc[int(5.0 * sr):int(6.5 * sr)] = (0.08 * np.sin(2 * np.pi * 650 * t_v)).astype(np.float32)

        seg = _FakeSeg(
            1.0, 6.5, "卫生跟胖瘦没关系",
            words=[
                _FakeWord(1.0, 1.4, "卫"),
                _FakeWord(5.0, 5.4, "生"),
                _FakeWord(5.4, 5.8, "跟"),
                _FakeWord(5.8, 6.5, "胖瘦没关系"),
            ],
        )
        out = ras.split_pass1_words_with_orphan_guard(seg, voc, avg_prob=-0.15, sr=sr)
        self.assertEqual(len(out), 1, f"1-char DTW orphan '卫' should merge instead of splitting: {out}")
        self.assertEqual(out[0]["text"], "卫生跟胖瘦没关系")
        self.assertGreaterEqual(out[0]["start"], 4.8)

        # Trailing 1-char late silence drift ('统' at 5.5..5.9 after '系' or '我的系' at 1.0..1.8) must anchor end without bloating
        voc_trail = np.zeros(sr * 8, dtype=np.float32)
        voc_trail[int(1.0 * sr):int(1.8 * sr)] = (0.08 * np.sin(2 * np.pi * 650 * np.arange(int(0.8 * sr)) / sr)).astype(np.float32)
        seg_trail_pair = _FakeSeg(
            1.0, 5.9, "系统",
            words=[_FakeWord(1.0, 1.8, "系"), _FakeWord(5.5, 5.9, "统")],
        )
        out_trail_pair = ras.split_pass1_words_with_orphan_guard(seg_trail_pair, voc_trail, avg_prob=-0.15, sr=sr)
        self.assertEqual(len(out_trail_pair), 1)
        self.assertEqual(out_trail_pair[0]["text"], "系统")
        self.assertLessEqual(out_trail_pair[0]["end"], 2.0, f"Trailing silence orphan bloated end timestamp: {out_trail_pair}")

        seg_trail_multi = _FakeSeg(
            1.0, 5.9, "我的系统",
            words=[_FakeWord(1.0, 1.8, "我的系"), _FakeWord(5.5, 5.9, "统")],
        )
        out_trail_multi = ras.split_pass1_words_with_orphan_guard(seg_trail_multi, voc_trail, avg_prob=-0.15, sr=sr)
        self.assertEqual(len(out_trail_multi), 1)
        self.assertEqual(out_trail_multi[0]["text"], "我的系统")
        self.assertLessEqual(out_trail_multi[0]["end"], 2.0, f"Trailing merged orphan bloated end timestamp: {out_trail_multi}")

    def test_pass2b_protective_collar_word_exclusion_and_boundary_dedup(self):
        sr = self.sr
        t1 = np.arange(sr * 2) / sr
        syl = (0.5 + 0.5 * np.sin(2 * np.pi * 5.0 * t1)).astype(np.float32) ** 2
        speech = (syl * (0.09 * np.sin(2 * np.pi * 700 * t1))).astype(np.float32)
        sil = np.zeros(sr * 2, dtype=np.float32)
        # [0..2s] silence, [2..4s] Pass 1 ("系统"), [4..6s] uncovered, [6..8s] Pass 1 ("还有超长性感大长腿")
        voc = np.concatenate([sil, speech, speech, speech])
        pass1 = [
            {"id": 1, "start": 2.0, "end": 3.85, "text": "系统", "source": "pass1_asr"},
            {"id": 2, "start": 6.0, "end": 7.9, "text": "还有超长性感大长腿", "source": "pass1_asr"},
        ]

        class _BleedAndDupModel:
            def transcribe(self, buf, **kwargs):
                # Simulates Whisper returning:
                # 1) A 1-char fragment ("统") right next to Pass 1 ("系统" ending at 3.85s) -> rejected!
                # 2) A genuine recovered phrase inside [4.0..5.8] ("这人好胖") -> kept!
                # 3) A boundary duplicate echo ("还有") right next to Pass 1 ("还有超长性感大长腿") -> rejected!
                return [
                    _FakeSeg(0.05, 0.40, "统", avg_logprob=-0.20, no_speech_prob=0.05),
                    _FakeSeg(0.50, 1.70, "这人好胖", avg_logprob=-0.22, no_speech_prob=0.05),
                    _FakeSeg(1.75, 2.05, "还有", avg_logprob=-0.25, no_speech_prob=0.06,
                             words=[_FakeWord(1.80, 2.05, "还有")]),
                ], {}

        out, rep = ras.run_multiband_pass2b_recovery(
            model=_BleedAndDupModel(),
            results=list(pass1),
            voc_audio=voc,
            raw_audio=voc,
            whisper_lang="zh",
            source_lang="zh",
            sr=sr,
        )
        texts = [s["text"] for s in out]
        self.assertIn("这人好胖", texts, f"Genuine Pass 2B speech was wrongly dropped: {out}")
        self.assertNotIn("统", texts, f"1-char fragment '统' leaked past dedup: {out}")
        self.assertNotIn("还有", texts, f"Boundary duplicate '还有' leaked past dedup: {out}")
        self.assertGreaterEqual(rep["dedup_rejected"], 2)
        self.assertEqual(avw.find_timestamp_violations(out), [])
        self.assertEqual(avw.find_boundary_duplicate_pairs(out), [])

    def test_snap_and_split_fixes_line842_overlaps_inversions_and_micro_fragments(self):
        sr = self.sr
        voice_mask = np.ones(int(20.0 / 0.05), dtype=bool)
        # Reproduce exact forensic bugs:
        # - unit_02 id 98 inverted negative span (162.82..162.48 -> shifted here to 2.82..2.48)
        # - tight segments (<0.35s apart) that old line 842 forced into -0.25s overlaps
        # - <0.25s stutter/orphan fragments & '你知道'+'知道' boundary echo
        bad_segs = [
            {"id": 1, "start": 1.00, "end": 1.35, "text": "你知道", "source": "pass1_asr"},
            {"id": 2, "start": 1.10, "end": 1.70, "text": "知道", "source": "pass1_asr"},
            {"id": 3, "start": 2.25, "end": 2.80, "text": "庞仔", "source": "pass2b_multiband_full_vocal"},
            {"id": 4, "start": 2.82, "end": 2.48, "text": "仔", "source": "pass1_asr"},
            {"id": 5, "start": 2.50, "end": 3.40, "text": "够爷们", "source": "pass1_asr"},
            {"id": 6, "start": 5.00, "end": 5.18, "text": "统", "source": "pass1_asr"},
            {"id": 7, "start": 5.20, "end": 6.40, "text": "有面板没", "source": "pass1_asr"},
            {"id": 8, "start": 7.00, "end": 7.12, "text": "呃", "source": "pass1_asr"},
            {"id": 9, "start": 7.15, "end": 8.40, "text": "我来啦", "source": "pass1_asr"},
        ]
        fixed = avw.snap_and_split_segments(bad_segs, voice_mask)
        self.assertEqual(avw.find_timestamp_violations(fixed, min_dur_s=0.15, min_gap_s=0.02), [], fixed)
        self.assertEqual(avw.find_boundary_duplicate_pairs(fixed), [], fixed)
        texts = [s["text"] for s in fixed]
        self.assertNotIn("仔", texts)
        self.assertNotIn("知道", texts)
        for i in range(len(fixed)):
            self.assertGreaterEqual(fixed[i]["start"], 0.0)
            self.assertGreater(fixed[i]["end"], fixed[i]["start"])
            self.assertGreaterEqual(fixed[i]["end"] - fixed[i]["start"], 0.15 - 1e-6)
            if i + 1 < len(fixed):
                self.assertLessEqual(fixed[i]["end"], fixed[i + 1]["start"] - 0.02 + 1e-6)

        # Also verify post-snap dedup when acoustic padding expands end and shrinks a >0.35s gap (0.45s) to <=0.35s (0.30s)
        vm_snap = np.zeros(int(12.0 / 0.05), dtype=bool)
        vm_snap[int(1.30 / 0.05):int(1.75 / 0.05)] = True
        vm_snap[int(2.15 / 0.05):int(3.20 / 0.05)] = True
        snap_dup = [
            {"id": 1, "start": 1.30, "end": 1.70, "text": "还有", "source": "pass2b_multiband_mid"},
            {"id": 2, "start": 2.15, "end": 3.20, "text": "还有超长性感大长腿", "source": "pass1_asr"},
        ]
        fixed_snap_dup = avw.snap_and_split_segments(snap_dup, vm_snap)
        self.assertEqual(avw.find_boundary_duplicate_pairs(fixed_snap_dup), [], fixed_snap_dup)
        self.assertEqual(len(fixed_snap_dup), 1, f"Post-snap boundary duplicate was not merged: {fixed_snap_dup}")

    def test_warm_multi_unit_loads_whisper_once_and_hits_relocated_cache(self):
        sr = self.sr
        t2 = np.arange(sr * 3) / sr
        wave = (0.08 * np.sin(2 * np.pi * 700 * t2)).astype(np.float32)

        with tempfile.TemporaryDirectory() as td:
            old_cache = os.environ.get("AICN_CACHE_DIR")
            os.environ["AICN_CACHE_DIR"] = os.path.join(td, "gcache")
            load_calls = []

            class _WarmFakeModel:
                def transcribe(self, audio, **kwargs):
                    if kwargs.get("vad_filter", True):
                        return [_FakeSeg(0.2, 1.8, "大家好我叫李航", avg_logprob=-0.2, no_speech_prob=0.05)], {}
                    return [], {}

            try:
                def _make_unit(name, payload_bytes):
                    u = os.path.join(td, "hub", "units", name)
                    for sub in ("data", "config", "source", "media/stems", ".cicd"):
                        os.makedirs(os.path.join(u, sub), exist_ok=True)
                    src = os.path.join(u, "source", "master_source.mp4")
                    with open(src, "wb") as f:
                        f.write(payload_bytes)
                    soundfile.write(os.path.join(u, "media/stems/vocals.wav"), wave, sr)
                    soundfile.write(os.path.join(u, "media/stems/accomp.wav"), wave, sr)
                    with open(os.path.join(u, "data/00_video_profile.json"), "w", encoding="utf-8") as f:
                        json.dump({"duration": 3.0, "source_video": "source/master_source.mp4", "localization_mode": "voice_dub"}, f)
                    with open(os.path.join(u, "config/project_config.json"), "w", encoding="utf-8") as f:
                        json.dump({"source_language": "zh", "localization_mode": "voice_dub"}, f)
                    cicd_gate.write_gate_token(
                        u, 0, "Stage 0", [{"id": "ok", "weight": 100, "passed": True, "detail": "ok"}], {}, ["data/00_video_profile.json"]
                    )
                    return u

                u1 = _make_unit("unit_01", b"video-payload-unit-1")
                u2 = _make_unit("unit_02", b"video-payload-unit-2")

                def _fake_load(sz):
                    load_calls.append(sz)
                    return _WarmFakeModel()

                with mock.patch.object(ras, "load_whisper", side_effect=_fake_load), \
                     mock.patch.object(ras, "decode_audio_16k", return_value=wave):
                    ras.run_units_asr_batch([(u1, ""), (u2, "")], skip_waveform=True)
                self.assertEqual(len(load_calls), 1, f"Whisper should load ONCE across 2 units, loaded {len(load_calls)} times")

                # Move/rename unit_01 to a brand-new folder -> must hit global pure-content SHA-256 cache (0 Whisper loads!)
                u1_moved = os.path.join(td, "relocated_hub", "units", "renamed_unit_01")
                shutil.move(u1, u1_moved)
                os.remove(os.path.join(u1_moved, "data", "01_asr_raw_segments.json"))
                os.remove(os.path.join(u1_moved, ".cache", "stage1_asr_cache.json"))

                load_calls.clear()
                with mock.patch.object(ras, "load_whisper", side_effect=_fake_load), \
                     mock.patch.object(ras, "decode_audio_16k", return_value=wave):
                    ras.run_units_asr_batch([(u1_moved, "")], skip_waveform=True)
                self.assertEqual(len(load_calls), 0, "Relocated unit must hit global content-SHA256 cache with 0 Whisper loads")
                self.assertTrue(os.path.exists(os.path.join(u1_moved, "data", "01_asr_raw_segments.json")))

                # Verify configurable parallel worker scaling when VRAM budget allows (>= 5120 MB)
                with mock.patch.dict(os.environ, {"AICN_GPU_BUDGET_MB": "8192"}, clear=False):
                    self.assertEqual(ras._resolve_asr_workers(4, cli_workers=2), 2)
                with mock.patch.dict(os.environ, {"AICN_GPU_BUDGET_MB": "4096"}, clear=False):
                    self.assertEqual(ras._resolve_asr_workers(4, cli_workers=2), 1)
            finally:
                if old_cache is None:
                    os.environ.pop("AICN_CACHE_DIR", None)
                else:
                    os.environ["AICN_CACHE_DIR"] = old_cache

    def test_verify_stage_1_and_stage_2_gates_reject_overlaps_dups_and_short_tts_overflow(self):
        sr = self.sr
        t2 = np.arange(sr * 4) / sr
        syl = (0.5 + 0.5 * np.sin(2 * np.pi * 5.0 * t2)).astype(np.float32) ** 2
        voc = (syl * (0.08 * np.sin(2 * np.pi * 700 * t2))).astype(np.float32)

        with tempfile.TemporaryDirectory() as td:
            for sub in ("data", "config", "media/stems", "frames/audit", ".cicd"):
                os.makedirs(os.path.join(td, sub), exist_ok=True)
            soundfile.write(os.path.join(td, "media/stems/vocals.wav"), voc, sr)
            soundfile.write(os.path.join(td, "media/stems/accomp.wav"), voc, sr)
            soundfile.write(os.path.join(td, "media/source_audio.wav"), voc, sr)
            with open(os.path.join(td, "data/00_video_profile.json"), "w", encoding="utf-8") as f:
                json.dump({"duration": 4.0, "source_video": "media/source_audio.wav", "localization_mode": "voice_dub"}, f)
            cicd_gate.write_gate_token(
                td, 0, "Stage 0", [{"id": "ok", "weight": 100, "passed": True, "detail": "ok"}], {}, ["data/00_video_profile.json"]
            )
            # 10 dummy audit frames + notes
            frames = []
            notes_lines = ["# Audit Notes\n"]
            for k in range(10):
                fn = f"frame_{k:04d}s.jpg"
                fp = os.path.join(td, "frames", "audit", fn)
                with open(fp, "wb") as f:
                    f.write(b"jpg-bytes")
                frames.append({"timestamp": float(k) * 0.4, "filename": fn, "rel_path": f"frames/audit/{fn}", "sha256": cicd_gate.sha256_file(fp)})
                notes_lines.append(f"- {fn}: clear")
            with open(os.path.join(td, "data/01_extracted_audit_manifest.json"), "w", encoding="utf-8") as f:
                json.dump({"num_frames": 10, "frames": frames}, f)
            with open(os.path.join(td, "01_visual_audit_notes.md"), "w", encoding="utf-8") as f:
                f.write("\n".join(notes_lines))

            # Overlapping ASR segments must FAIL verify_stage_1.py
            with open(os.path.join(td, "data/01_asr_raw_segments.json"), "w", encoding="utf-8") as f:
                json.dump([
                    {"id": 1, "start": 0.2, "end": 1.6, "text": "大家好", "source": "pass1_asr"},
                    {"id": 2, "start": 1.4, "end": 3.5, "text": "我叫李航", "source": "pass1_asr"},
                ], f)
            r_fail = subprocess.run(
                [sys.executable, os.path.join(V1_SCRIPTS, "verify_stage_1.py"), "--workspace", td],
                capture_output=True, text=True,
            )
            self.assertEqual(r_fail.returncode, 2, r_fail.stdout + r_fail.stderr)
            self.assertIn("overlap_or_gap_violation", r_fail.stdout)

            # Pass 2B boundary duplicate fragment must FAIL verify_stage_1.py
            with open(os.path.join(td, "data/01_asr_raw_segments.json"), "w", encoding="utf-8") as f:
                json.dump([
                    {"id": 1, "start": 0.1, "end": 1.5, "text": "还有超长性感大长腿", "source": "pass1_asr"},
                    {"id": 2, "start": 1.55, "end": 3.8, "text": "还有", "source": "pass2b_multiband_mid"},
                ], f)
            r_dup = subprocess.run(
                [sys.executable, os.path.join(V1_SCRIPTS, "verify_stage_1.py"), "--workspace", td],
                capture_output=True, text=True,
            )
            self.assertEqual(r_dup.returncode, 2, r_dup.stdout + r_dup.stderr)
            self.assertIn("boundary_dups=1", r_dup.stdout)

            # Fix overlap & dup -> PASS verify_stage_1.py
            with open(os.path.join(td, "data/01_asr_raw_segments.json"), "w", encoding="utf-8") as f:
                json.dump([
                    {"id": 1, "start": 0.1, "end": 1.5, "text": "大家好", "source": "pass1_asr"},
                    {"id": 2, "start": 1.6, "end": 3.8, "text": "我叫李航", "source": "pass1_asr"},
                ], f)
            r_ok = subprocess.run(
                [sys.executable, os.path.join(V1_SCRIPTS, "verify_stage_1.py"), "--workspace", td],
                capture_output=True, text=True,
            )
            self.assertEqual(r_ok.returncode, 0, r_ok.stdout + r_ok.stderr)

            # Stage 2 calibrated budget check:
            # - short 2-syl line ("Thế thôi.") in a 0.42s tight slot MUST be flagged!
            # - comma-chained group [2, 3] ("Đào," + "Giám đốc hành chính đây.") overflowing tight slack MUST be flagged!
            tight_script = [
                {"id": 1, "start": 0.10, "end": 0.40, "sub_text": "Thế thôi.", "tts_text": "Thế thôi."},
                {"id": 2, "start": 0.47, "end": 0.70, "sub_text": "Đào,", "tts_text": "Đào,"},
                {"id": 3, "start": 0.78, "end": 1.15, "sub_text": "Giám đốc hành chính đây.", "tts_text": "Giám đốc hành chính đây."},
                {"id": 4, "start": 1.25, "end": 3.80, "sub_text": "Rất vui được gặp anh.", "tts_text": "Rất vui được gặp anh."},
            ]
            errs = vbs.check_tts_duration_budgets(tight_script, video_dur=4.0)
            self.assertTrue(any("Seg #1" in e for e in errs), f"Tight 2-syl line 'Thế thôi.' missed: {errs}")
            self.assertTrue(any("Group ids=[2, 3]" in e for e in errs), f"Tight v3 comma group [2, 3] missed: {errs}")
            # Continuous 2-clause comma-chained group MUST NOT false-positive on its interior short clause
            valid_group_script = [
                {"id": 1, "start": 0.05, "end": 0.40, "sub_text": "Này anh,", "tts_text": "Này anh,"},
                {"id": 2, "start": 0.45, "end": 2.00, "sub_text": "đừng đi vội.", "tts_text": "đừng đi vội."},
                {"id": 3, "start": 3.00, "end": 3.90, "sub_text": "Chờ tôi với.", "tts_text": "Chờ tôi với."},
            ]
            self.assertEqual(vbs.check_tts_duration_budgets(valid_group_script, video_dur=4.0), [])
            # Verify overlap check in Stage 2 script validator logic
            ov_script = [
                {"id": 1, "start": 0.10, "end": 1.50, "sub_text": "Chào bạn.", "tts_text": "Chào bạn."},
                {"id": 2, "start": 1.45, "end": 3.80, "sub_text": "Mình là Nam.", "tts_text": "Mình là Nam."},
            ]
            with open(os.path.join(td, "data/02_bilingual_script.json"), "w", encoding="utf-8") as f:
                json.dump(ov_script, f, ensure_ascii=False)
            with open(os.path.join(td, "02_glossary_and_pronunciation.md"), "w", encoding="utf-8") as f:
                f.write("# Glossary\n")
            r_s2 = subprocess.run(
                [sys.executable, os.path.join(V2_SCRIPTS, "validate_bilingual_script.py"), "--workspace", td],
                capture_output=True, text=True,
            )
            self.assertEqual(r_s2.returncode, 2, r_s2.stdout + r_s2.stderr)
            self.assertIn("overlap/gap violation", r_s2.stdout)

    def test_extract_audit_frames_prioritizes_pass2b_without_bbox_scan(self):
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "data"), exist_ok=True)
            with open(os.path.join(td, "data/00_video_profile.json"), "w", encoding="utf-8") as f:
                json.dump({"duration": 120.0}, f)
            with open(os.path.join(td, "data/01_asr_raw_segments.json"), "w", encoding="utf-8") as f:
                json.dump([
                    {"id": 1, "start": 14.0, "end": 16.0, "text": "喂你好", "source": "pass2b_multiband_mid", "cue_type": "phone_call"},
                    {"id": 2, "start": 45.0, "end": 46.6, "text": "小声说", "source": "pass2b_multiband_high", "cue_type": "high_voice"},
                ], f)
            ts = eaf.select_smart_timestamps(td)
            self.assertGreaterEqual(len(ts), 12)
            self.assertIn(15.0, ts)
            self.assertIn(45.8, ts)

    def test_reentrant_gpu_slot_and_content_sha256_survives_move(self):
        import aicn_platform as ap
        with tempfile.TemporaryDirectory() as td:
            old_lock_dir = os.environ.get("AICN_LOCK_DIR")
            old_cache_dir = os.environ.get("AICN_CACHE_DIR")
            os.environ["AICN_LOCK_DIR"] = os.path.join(td, "locks")
            os.environ["AICN_CACHE_DIR"] = os.path.join(td, "gcache")
            try:
                if hasattr(ap, "gpu_slot"):
                    with ap.gpu_slot("whisper", vram_mb=2560, label="TEST"):
                        with ap.gpu_slot("whisper", vram_mb=1024, label="TEST-REENTER"):
                            pass
                elif hasattr(ap, "gpu_lock"):
                    with ap.gpu_lock():
                        with ap.gpu_lock():
                            pass
                p1 = os.path.join(td, "a", "sample.bin")
                p2 = os.path.join(td, "b", "renamed.bin")
                os.makedirs(os.path.dirname(p1), exist_ok=True)
                os.makedirs(os.path.dirname(p2), exist_ok=True)
                with open(p1, "wb") as f:
                    f.write(b"identical-video-payload-123456")
                h1 = avw._fast_file_sig(p1)
                shutil.copyfile(p1, p2)
                os.utime(p2, (1700000000, 1700000000))
                h2 = avw._fast_file_sig(p2)
                self.assertEqual(h1, h2, "Moving/recreating identical media must preserve content SHA-256")
            finally:
                if old_lock_dir is None:
                    os.environ.pop("AICN_LOCK_DIR", None)
                else:
                    os.environ["AICN_LOCK_DIR"] = old_lock_dir
                if old_cache_dir is None:
                    os.environ.pop("AICN_CACHE_DIR", None)
                else:
                    os.environ["AICN_CACHE_DIR"] = old_cache_dir

    def test_stage1_cache_keys_and_global_cache_path_survive_relocation(self):
        import aicn_platform as ap
        k1 = ap.stage1_asr_key("src_abc", "voc_def", "large-v3", "zh")
        k2 = ap.stage1_asr_key("src_abc", "voc_def", "large-v3", "zh")
        k3 = ap.stage1_asr_key("src_xyz", "voc_def", "large-v3", "zh")
        self.assertEqual(k1, k2)
        self.assertNotEqual(k1, k3)
        with tempfile.TemporaryDirectory() as td:
            old_cache = os.environ.get("AICN_CACHE_DIR")
            os.environ["AICN_CACHE_DIR"] = os.path.join(td, "gcache")
            try:
                p_a = ap.global_cache_path("asr", k1, ".json", hint_path=os.path.join(td, "hub_a", "unit_01"))
                p_b = ap.global_cache_path("asr", k1, ".json", hint_path=os.path.join(td, "hub_b", "unit_99"))
                self.assertEqual(p_a, p_b)
            finally:
                if old_cache is None:
                    os.environ.pop("AICN_CACHE_DIR", None)
                else:
                    os.environ["AICN_CACHE_DIR"] = old_cache

    def test_two_clause_pause_bridged_and_bark_batch_never_poisons_short_speech_or_ci_gate(self):
        """Regression test for Ep1 0-6s: 0.30s pause inside '说吧，哪疼' stays 1 intact gap, window no_sp=0.658 is rescued, and ambiguous loud vocals are NOT rubber-stamped as verified_non_speech."""
        sr = self.sr
        voc = np.zeros(sr * 10, dtype=np.float32)
        # [0.2..3.3s] impulsive high-ZCR dog bark; [3.80..4.46s] '说吧' + 0.30s pause + [4.76..5.30s] '哪疼'
        rng = np.random.default_rng(42)
        bark_t = np.arange(int(3.1 * sr)) / sr
        bark_env = (np.sin(2 * np.pi * 3.2 * bark_t) > 0.2).astype(np.float32)
        voc[int(0.2 * sr):int(3.3 * sr)] = (0.16 * bark_env * rng.standard_normal(len(bark_t))).astype(np.float32)
        t_c1 = np.arange(int(0.66 * sr)) / sr
        t_c2 = np.arange(int(0.54 * sr)) / sr
        voc[int(3.80 * sr):int(4.46 * sr)] = (0.14 * np.sin(2 * np.pi * 520 * t_c1) * (0.6 + 0.4 * np.sin(2 * np.pi * 5 * t_c1))).astype(np.float32)
        voc[int(4.76 * sr):int(5.30 * sr)] = (0.14 * np.sin(2 * np.pi * 560 * t_c2) * (0.6 + 0.4 * np.sin(2 * np.pi * 5 * t_c2))).astype(np.float32)
        # [7.5..8.5s] ambiguous mumbled vocal burst (high voc_rms, borderline word prob 0.38 -> must NOT enter verified_non_speech_gaps!)
        t_m = np.arange(int(1.0 * sr)) / sr
        voc[int(7.5 * sr):int(8.5 * sr)] = (0.10 * np.sin(2 * np.pi * 480 * t_m) * (0.6 + 0.4 * np.sin(2 * np.pi * 4 * t_m))).astype(np.float32)

        gaps = avw.detect_uncovered_multiband_gaps([], voc, voc, sr=sr)
        speech_gaps = [g for g in gaps if 3.5 <= g["start"] <= 4.3 and 5.2 <= g["end"] <= 5.8]
        self.assertEqual(len(speech_gaps), 1, f"'说吧，哪疼' (0.30s pause) was chopped or onset-clipped: {gaps}")
        self.assertLessEqual(speech_gaps[0]["start"], 3.85)

        class _WindowInflatedNoSpeechModel:
            feature_extractor = True

            def __init__(self):
                self.n_calls = 0

            def transcribe(self, buf, **kwargs):
                self.n_calls += 1
                # Simulate Whisper's 30s-window inflated no_speech_prob=0.658 on full_vocal batch (call #1):
                # Slot 0 ([3.80..5.30s]) has word probs 0.99 / 0.82 -> rescued!
                # Slot 1 ([7.50..8.50s]) has borderline word prob 0.36 -> rejected AND flagged as unrecovered_vocal!
                if self.n_calls == 1:
                    return [
                        _FakeSeg(
                            0.35, 1.95, "说吧哪疼", avg_logprob=-0.223, no_speech_prob=0.658,
                            words=[_FakeWord(0.45, 1.10, "说吧", 0.99), _FakeWord(1.35, 1.90, "哪疼", 0.82)],
                        ),
                        _FakeSeg(
                            3.40, 4.00, "唔唔", avg_logprob=-0.85, no_speech_prob=0.72,
                            words=[_FakeWord(3.45, 3.95, "唔唔", 0.36)],
                        ),
                    ], {}
                return [], {}

        out, rep = ras.run_multiband_pass2b_recovery(
            model=_WindowInflatedNoSpeechModel(),
            results=[],
            voc_audio=voc,
            raw_audio=voc,
            whisper_lang="zh",
            source_lang="zh",
            sr=sr,
        )
        self.assertTrue(any("说吧哪疼" in s["text"] for s in out), f"'说吧哪疼' dropped despite high word prob & voc_rms: {out}")
        self.assertLessEqual(rep["encoder_calls"], 6, f"Multiband turned into an excessive call loop: {rep['encoder_calls']}")
        self.assertGreaterEqual(len(rep["unrecovered_vocal_gaps"]), 1, f"Ambiguous vocal burst was rubber-stamped as non-speech: {rep}")


if __name__ == "__main__":
    unittest.main()
