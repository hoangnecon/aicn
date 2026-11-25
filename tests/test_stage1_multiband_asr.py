import json
import os
import sys
import tempfile
import unittest
import numpy as np
import soundfile

HERE = os.path.dirname(os.path.abspath(__file__))
V1_SCRIPTS = os.path.abspath(os.path.join(HERE, "..", "vendor", "video-asr-visual-audit", "scripts"))
if V1_SCRIPTS not in sys.path:
    sys.path.insert(0, V1_SCRIPTS)

import analyze_voice_waveform as avw
import run_asr_and_scan as ras


class _FakeSeg:
    def __init__(self, start, end, text, avg_logprob=-0.25, no_speech_prob=0.05, compression_ratio=1.1):
        self.start = start
        self.end = end
        self.text = text
        self.avg_logprob = avg_logprob
        self.no_speech_prob = no_speech_prob
        self.compression_ratio = compression_ratio


class TestStage1MultiBandASR(unittest.TestCase):
    sr = 16000

    def test_gap_first_catches_deadzone_normal_speech_and_splits_long_gaps(self):
        sr = self.sr
        total_s = 35
        t = np.arange(sr * total_s) / sr
        syl = (0.5 + 0.5 * np.sin(2 * np.pi * 4.5 * t)).astype(np.float32) ** 2
        # Clear speech sitting in the old 0.024-0.080 RMS dead zone (~0.045 RMS) across 0..35s
        speech = (syl * (0.065 * np.sin(2 * np.pi * 650 * t) + 0.035 * np.sin(2 * np.pi * 1500 * t))).astype(np.float32)
        voc = speech.copy()
        raw = speech.copy()

        # Pass 1 only covered 0..12s; 12..35s (23s!) was missed by Pass 1
        existing = [{"id": 0, "start": 0.0, "end": 12.0, "text": "前面十二秒已经识别了"}]
        gaps = avw.detect_uncovered_multiband_gaps(existing, voc, raw, sr=sr, max_window_s=10.0)

        self.assertGreaterEqual(len(gaps), 2, f"Expected 23s uncovered region split into >=2 windows, got {gaps}")
        for g in gaps:
            self.assertGreaterEqual(g["start"], 11.8, f"Covered region leaked into gap: {g}")
            self.assertLessEqual(g["duration"], 10.2, f"Gap exceeded max_window_s=10.0: {g}")
        covered_by_gaps = sum(g["duration"] for g in gaps)
        self.assertGreaterEqual(covered_by_gaps, 18.0, f"Dead-zone speech was missed: {gaps}")

    def test_multiband_high_mid_low_and_reverse_raw_recovery(self):
        sr = self.sr
        t2 = np.arange(sr * 2) / sr
        sil2 = np.zeros(sr * 2, dtype=np.float32)
        sil1 = np.zeros(sr * 1, dtype=np.float32)
        syl = (0.5 + 0.5 * np.sin(2 * np.pi * 5.0 * t2)).astype(np.float32) ** 2

        # [1..3s] High-pitched child/cartoon voice (2400+3800 Hz) attenuated in vocals stem + heavy 90 Hz bass BGM
        child_hi = (syl * (0.055 * np.sin(2 * np.pi * 2400 * t2) + 0.045 * np.sin(2 * np.pi * 3800 * t2))).astype(np.float32)
        bass = (0.14 * np.sin(2 * np.pi * 90 * t2)).astype(np.float32)
        # [5..7s] Mid-band phone call (700+1500 Hz) sandwiched between 90 Hz bass AND 5200 Hz high-freq hiss -> ONLY mid isolates it!
        phone_mid = (syl * (0.060 * np.sin(2 * np.pi * 700 * t2) + 0.045 * np.sin(2 * np.pi * 1500 * t2))).astype(np.float32)
        hi_hiss = (0.14 * np.sin(2 * np.pi * 5200 * t2)).astype(np.float32)
        # [9..11s] Deep male voice (240+420 Hz) in vocals, drowned by loud 5500 Hz high-freq SFX in raw
        male_lo = (syl * (0.050 * np.sin(2 * np.pi * 240 * t2) + 0.040 * np.sin(2 * np.pi * 420 * t2))).astype(np.float32)
        hi_sfx = (0.16 * np.sin(2 * np.pi * 5500 * t2)).astype(np.float32)
        # [13..15s] Steady unmodulated 1500 Hz solo instrument (must NOT trigger)
        steady = (0.045 * np.sin(2 * np.pi * 1500 * t2)).astype(np.float32)

        voc = np.concatenate([sil1, 0.05 * child_hi, sil2, sil2, sil2, male_lo, sil2, sil2, sil1])
        raw = np.concatenate([sil1, child_hi + bass, sil2, phone_mid + bass + hi_hiss, sil2, male_lo + hi_sfx, sil2, steady, sil1])

        class _SpectralFakeModel:
            def transcribe(self, clip, **kwargs):
                # Inspect dominant spectral energy of clip to simulate band-specific Whisper accuracy
                spec = np.abs(np.fft.rfft(clip)) ** 2
                freqs = np.fft.rfftfreq(len(clip), 1.0 / sr)
                tot = float(spec.sum() + 1e-12)
                e_bass = float(spec[freqs < 160].sum()) / tot
                e_lo = float(spec[(freqs >= 160) & (freqs < 480)].sum()) / tot
                e_mid = float(spec[(freqs >= 550) & (freqs < 1800)].sum()) / tot
                e_hi = float(spec[(freqs >= 2100) & (freqs < 4500)].sum()) / tot
                e_sfx = float(spec[freqs >= 4600].sum()) / tot

                # High-band (bass removed) isolates child voice cleanly
                if e_hi > 0.45 and e_bass < 0.05 and e_sfx < 0.10:
                    return [_FakeSeg(0.25, 1.95, "小舅我来啦", avg_logprob=-0.22, no_speech_prob=0.05)], {}
                # Mid-band (both bass AND high hiss removed!) isolates phone call
                if e_mid > 0.55 and e_bass < 0.05 and e_sfx < 0.10 and e_hi < 0.25:
                    return [_FakeSeg(0.25, 1.95, "喂听得见吗", avg_logprob=-0.28, no_speech_prob=0.08)], {}
                # Low-band (high SFX removed) isolates deep male voice
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
                    _FakeSeg(0.6, 1.0, "啊", avg_logprob=-0.80, no_speech_prob=0.40),
                    _FakeSeg(1.1, 1.7, "谢谢观看", avg_logprob=-0.10, no_speech_prob=0.01),
                ], {}

        cands = ras._probe_band_clip(
            model=_MixedModel(), wave=wave, pad_s=0.0, pad_e=2.0, gst=0.0, get=2.0,
            band_name="full_vocal", whisper_lang="zh", source_lang="zh", peak_voc_rms=0.08, sr=sr,
        )
        texts = [c["text"] for c in cands]
        self.assertIn("滚！", texts, "High-confidence loud 1-char exclamation must be kept")
        self.assertNotIn("啊", texts, "Low-confidence 1-char utterance must be dropped")
        self.assertNotIn("谢谢观看", texts, "Hallucination substring must be rejected")

    def test_smart_acoustic_gate_flags_unprobed_vocal_gap_and_allows_verified_bgm(self):
        sr = self.sr
        t = np.arange(sr * 6) / sr
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
            # Empty ASR segments -> loud vocal burst at 2..4s is UNCOVERED and UNPROBED (no bbox scan)
            with open(os.path.join(td, "data/01_asr_raw_segments.json"), "w", encoding="utf-8") as f:
                json.dump([], f)

            rep1 = avw.analyze_waveform_and_clauses(td)
            self.assertGreaterEqual(rep1["uncovered_acoustic_bursts_count"], 1, f"Blind gate failed to flag unprobed vocal gap: {rep1}")
            self.assertLess(rep1["acoustic_speech_coverage_ratio"], 1.0)
            self.assertGreaterEqual(rep1["longest_uncovered_vocal_gap_s"], 1.0)

            # Now simulate Pass 2B proving 2..4s was pure non-speech BGM (L-015 safe)
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

    def test_pass1_pipeline_version_and_defaults(self):
        self.assertEqual(ras.ASR_PIPELINE_VERSION, "asr-3.0")
        self.assertEqual(ras._initial_prompt_for("zh"), "以下是普通话的句子，使用简体中文。")
        self.assertIsNone(ras._initial_prompt_for("en"))


if __name__ == "__main__":
    unittest.main()
