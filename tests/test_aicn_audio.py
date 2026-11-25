"""Tests for scripts/aicn_audio.py pure parts. Needs numpy, scipy, soundfile (run with $AICN_PY).

Run:  /Users/Shared/Tools/VieNeu-TTS/.venv/bin/python -m unittest discover -s tests -v
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest

try:
    import numpy as np
    import soundfile  # noqa: F401
    import scipy  # noqa: F401
    HAVE = True
except ImportError:
    HAVE = False

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")


def load():
    sys.path.insert(0, SCRIPTS)
    spec = importlib.util.spec_from_file_location("aicn_audio", os.path.join(SCRIPTS, "aicn_audio.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def line(i, a, b, text, spk="A"):
    return {"id": i, "start": a, "end": b, "tts_text": text, "sub_text": text, "speaker": spk}


@unittest.skipUnless(HAVE, "numpy/scipy/soundfile not installed")
class Grouping(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def test_comma_chain_same_speaker_merges(self):
        s = [line(0, 0, 1, "Viện trưởng,"), line(1, 1.1, 2, "tôi ở lại."), line(2, 2.2, 3, "Được.")]
        self.assertEqual(self.m.group_lines(s, 0.6), [[0, 1], [2]])

    def test_speaker_change_splits(self):
        s = [line(0, 0, 1, "Viện trưởng,", "A"), line(1, 1.1, 2, "tôi ở lại.", "B")]
        self.assertEqual(self.m.group_lines(s, 0.6), [[0], [1]])

    def test_long_gap_splits(self):
        s = [line(0, 0, 1, "Viện trưởng,"), line(1, 2.0, 3, "tôi ở lại.")]
        self.assertEqual(self.m.group_lines(s, 0.6), [[0], [1]])

    def test_sentence_end_splits(self):
        s = [line(0, 0, 1, "Đi thôi!"), line(1, 1.1, 2, "Nhanh lên.")]
        self.assertEqual(self.m.group_lines(s, 0.6), [[0], [1]])

    def test_group_lines_caps_count_and_span(self):
        # 10 comma-chained lines over 17s must not merge into one mega-group
        s = [line(i, i * 1.7, i * 1.7 + 1.5, f"Mệnh đề số {i},") for i in range(10)]
        groups = self.m.group_lines(s, 0.6)
        self.assertTrue(all(len(g) <= 4 for g in groups), groups)
        for g in groups:
            self.assertLessEqual(s[g[-1]]["end"] - s[g[0]]["start"], 8.0)


@unittest.skipUnless(HAVE, "numpy/scipy/soundfile not installed")
class Signal(unittest.TestCase):
    def setUp(self):
        self.m = load()
        self.sr = self.m.SR

    def tone(self, s):
        t = np.arange(int(s * self.sr)) / self.sr
        return (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)

    def test_split_snaps_to_silence(self):
        w = np.concatenate([self.tone(1.0), np.zeros(int(0.2 * self.sr), np.float32), self.tone(1.0)])
        cuts = self.m.split_points(w, [5, 5])
        self.assertTrue(1.0 * self.sr <= cuts[1] <= 1.2 * self.sr, cuts)
        self.assertEqual(cuts[0], 0)
        self.assertEqual(cuts[-1], len(w))

    def test_expander_lowers_hiss_only(self):
        rng = np.random.default_rng(0)
        hiss = (rng.standard_normal(int(0.5 * self.sr)) * 10 ** (-70 / 20)).astype(np.float32)
        w = np.concatenate([self.tone(0.5), hiss, self.tone(0.5)])
        out = self.m.clean(w, self.m.DEFAULTS)
        mid = out[len(out) // 2 - 4800: len(out) // 2 + 4800]
        self.assertLess(np.sqrt((mid ** 2).mean()), 10 ** (-80 / 20))
        self.assertGreater(np.abs(out).max(), 0.25)

    def test_follow_attack_faster_than_release(self):
        g = np.r_[np.ones(10), np.zeros(10), np.ones(30)][None].astype(float)
        out = self.m.follow(g, 100, 0.02, 0.25)[0]
        self.assertLess(out[12], 0.5)
        self.assertLess(out[25], 0.6)

    @unittest.skipUnless(shutil.which("rubberband"), "rubberband not installed")
    def test_rubberband_shortens(self):
        w = self.tone(2.0)
        out = self.m.stretch(w, 1.25)
        self.assertAlmostEqual(len(out) / len(w), 0.8, delta=0.02)

    def test_srt_time(self):
        self.assertEqual(self.m.srt_t(3661.5), "01:01:01,500")

    def test_cached_stft_and_istft_match_reference(self):
        from scipy.signal import stft, istft
        rng = np.random.default_rng(7)
        x = (rng.standard_normal((self.sr * 2, 2)) * 0.2).astype(np.float32)
        idx, band_of_bin = self.m._band_indices()
        P, Zs, _ = self.m.stft_and_power(x, idx)
        # Build non-trivial per-band gain matrix
        g = np.linspace(0.25, 1.0, len(idx))[:, None] * np.ones((1, P.shape[1]), dtype=np.float32)
        y_fast = self.m.istft_from_Z(Zs, g, band_of_bin, len(x))
        # Reference float64 SciPy path
        y_ref = np.zeros_like(x, dtype=np.float64)
        for ch in range(2):
            _, _, Z = stft(x[:, ch], self.sr, nperseg=self.m.NFFT, noverlap=self.m.NFFT - self.m.HOP)
            G = np.ones((Z.shape[0], Z.shape[1]))
            for k, ii in enumerate(idx):
                G[ii] = g[k, :Z.shape[1]]
            _, yr = istft(Z * G, self.sr, nperseg=self.m.NFFT, noverlap=self.m.NFFT - self.m.HOP)
            y_ref[:, ch] = yr[:len(x)]
        self.assertLess(np.max(np.abs(y_fast - y_ref)), 1e-5)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_piped_lufs_matches_file_lufs(self):
        w = np.stack([self.tone(2.0), self.tone(2.0)], axis=1)
        l_pipe = self.m.lufs(w)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.wav")
            soundfile.write(p, w, self.sr, subtype="FLOAT")
            l_file, _ = self.m.lufs_file(p)
        self.assertAlmostEqual(l_pipe, l_file, delta=0.05)

    def test_sub_only_alignment_and_report(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "data"))
            with open(os.path.join(d, "data", "00_video_profile.json"), "w", encoding="utf-8") as f:
                json.dump({"duration": 10.0, "localization_mode": "sub_only"}, f)
            with open(os.path.join(d, "data", "02_bilingual_script.json"), "w", encoding="utf-8") as f:
                json.dump([
                    {"id": 0, "start": 1.0, "end": 3.0, "zh_text": "你好", "sub_text": "Xin chào."},
                    {"id": 1, "start": 3.5, "end": 5.5, "zh_text": "再见", "sub_text": "Tạm biệt."},
                ], f)
            n = self.m.write_sub_only_alignment(d)
            self.assertEqual(n, 2)
            self.assertTrue(os.path.exists(os.path.join(d, "data", "03_aligned_subtitles.json")))
            self.assertTrue(os.path.exists(os.path.join(d, "data", "subtitles_full.srt")))
            self.m.write_stage3_report(d, "sub_only", [{"id": "loudness", "passed": True, "detail": "-14.0 LUFS"}], self.m.DEFAULTS)
            self.assertTrue(os.path.exists(os.path.join(d, "03_pacing_and_audio_report.md")))

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_fast_master_hits_target_lufs_and_peak(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "data"))
            os.makedirs(os.path.join(d, "media", "audio"))
            with open(os.path.join(d, "data", "00_video_profile.json"), "w", encoding="utf-8") as f:
                json.dump({"duration": 3.0, "localization_mode": "voice_dub"}, f)
            w = np.stack([self.tone(3.0), self.tone(3.0)], axis=1)
            soundfile.write(os.path.join(d, "media", "audio", "premaster_48k.wav"), w, self.sr, subtype="FLOAT")
            c = dict(self.m.DEFAULTS, master_mode="fast")
            self.m.cmd_master(d, c, None)
            out = os.path.join(d, "media", "master_audio_48k.wav")
            self.assertTrue(os.path.exists(out))
            i, pk = self.m.lufs_file(out)
            self.assertAlmostEqual(i, -14.0, delta=0.5)
            self.assertLessEqual(pk, -0.7)

    @unittest.skipUnless(shutil.which("rubberband"), "rubberband not installed")
    def test_cmd_dub_places_each_clause_at_own_start_no_early_drift(self):
        sys.path.insert(0, SCRIPTS)
        import aicn
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "data"))
            os.makedirs(os.path.join(d, ".cache", "tts_sent"))
            segs = [
                {"id": i, "start": round(i * 2.0, 2), "end": round(i * 2.0 + 1.7, 2),
                 "speaker": "A", "tts_text": f"Mệnh đề {i} rất hay,", "sub_text": f"Mệnh đề {i} rất hay,"}
                for i in range(4)
            ]
            segs[-1]["tts_text"] = "Mệnh đề 3 kết thúc."
            segs[-1]["sub_text"] = "Mệnh đề 3 kết thúc."
            aicn.jdump(os.path.join(d, "data", "00_video_profile.json"), {"duration": 10.0, "localization_mode": "voice_dub"})
            aicn.jdump(os.path.join(d, "data", "02_bilingual_script.json"), segs)
            # Pre-populate cached group TTS WAV shorter (4.4s) than the 8.0s group span -> previously caused ~3s early drift
            c = dict(self.m.DEFAULTS)
            text = " ".join(s["tts_text"] for s in segs)
            key = aicn.h_obj({"t": text, "v": c["voice"], "e": aicn.TTS_ENGINE_TAG, "temp": c["temperature"], "k": c["top_k"]})
            clause_wav = np.concatenate([self.tone(0.95), np.zeros(int(0.15 * self.sr), np.float32)])
            soundfile.write(os.path.join(d, ".cache", "tts_sent", key + ".wav"), np.tile(clause_wav, 4), self.sr, subtype="FLOAT")
            self.m.cmd_dub(d, c, aicn.FastHash(d))
            aligned = aicn.jload(os.path.join(d, "data", "03_aligned_subtitles.json"))
            self.assertEqual(len(aligned), 4)
            for s0, al in zip(segs, aligned):
                drift = al["speech_start"] - s0["start"]
                self.assertGreaterEqual(drift, -0.05, f"Early drift on seg {al['id']}: {drift}")
                self.assertLessEqual(drift, 0.25, f"Late drift on seg {al['id']}: {drift}")

    def test_stage3_gate_rejects_early_speech_drift_and_accepts_synced(self):
        sys.path.insert(0, SCRIPTS)
        import aicn
        v3_dir = os.path.join(HERE, "..", "vendor", "vieneu-voice-pacing-mixer", "scripts")
        orch_dir = os.path.join(HERE, "..", "vendor", "video-localization-orchestrator", "scripts")
        sys.path.insert(0, v3_dir)
        sys.path.insert(0, orch_dir)
        from cicd_gate import write_gate_token
        from verify_stage_3 import run_stage_3_verification
        with tempfile.TemporaryDirectory() as d:
            for sub in ("data", "media", "media/tts_segments", ".cicd"):
                os.makedirs(os.path.join(d, sub), exist_ok=True)
            aicn.jdump(os.path.join(d, "data", "00_video_profile.json"), {"duration": 10.0, "localization_mode": "voice_dub"})
            segs = [
                {"id": 0, "start": 1.0, "end": 2.5, "zh_text": "一", "sub_text": "Một,", "tts_text": "Một,"},
                {"id": 1, "start": 7.0, "end": 8.5, "zh_text": "二", "sub_text": "Hai.", "tts_text": "Hai."},
            ]
            aicn.jdump(os.path.join(d, "data", "02_bilingual_script.json"), segs)
            for idx in range(2):
                soundfile.write(os.path.join(d, "media", "tts_segments", f"seg_{idx:04d}.wav"), self.tone(1.0), self.sr)
            soundfile.write(os.path.join(d, "media", "master_audio_48k.wav"), np.stack([self.tone(10.0), self.tone(10.0)], axis=1), self.sr)
            with open(os.path.join(d, "data", "subtitles_full.srt"), "w", encoding="utf-8") as f:
                f.write("1\n00:00:01,000 --> 00:00:02,500\nMột,\n\n2\n00:00:07,000 --> 00:00:08,500\nHai.\n\n")
            with open(os.path.join(d, "03_pacing_and_audio_report.md"), "w", encoding="utf-8") as f:
                f.write("# ok\n")
            write_gate_token(d, 2, "Stage 2", [{"id": "s2", "passed": True, "weight": 100, "detail": "ok"}],
                             metrics={}, artifact_paths=["data/02_bilingual_script.json"])
            # 1) Reproduce -5.33s early speech_start drift -> Stage 3 gate MUST fail (SystemExit 2)
            bad_aligned = [
                {"id": 0, "slot_start": 1.0, "slot_end": 2.5, "speech_start": 1.0, "speech_end": 1.6, "pause_after": 0.4, "tempo": 1.1},
                {"id": 1, "slot_start": 7.0, "slot_end": 8.5, "speech_start": 1.666, "speech_end": 2.9, "pause_after": 0.4, "tempo": 1.1},
            ]
            aicn.jdump(os.path.join(d, "data", "03_aligned_subtitles.json"), bad_aligned)
            with self.assertRaises(SystemExit):
                run_stage_3_verification(d)
            # 2) Synced speech_start -> Stage 3 gate MUST pass
            good_aligned = [
                {"id": 0, "slot_start": 1.0, "slot_end": 2.5, "speech_start": 1.0, "speech_end": 2.1, "pause_after": 0.4, "tempo": 1.1},
                {"id": 1, "slot_start": 7.0, "slot_end": 8.5, "speech_start": 7.0, "speech_end": 8.1, "pause_after": 0.4, "tempo": 1.1},
            ]
            aicn.jdump(os.path.join(d, "data", "03_aligned_subtitles.json"), good_aligned)
            res = run_stage_3_verification(d)
            self.assertEqual(res["status"], "PASS")

    def test_apply_hp_lp_and_hangover_hold(self):
        t = np.arange(self.sr) / self.sr
        sub = (0.4 * np.sin(2 * np.pi * 40 * t)).astype(np.float32)
        mid = (0.4 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
        hi = (0.4 * np.sin(2 * np.pi * 17000 * t)).astype(np.float32)
        y_sub = self.m.apply_hp_lp(sub, hp_hz=120.0, lp_hz=12000.0)
        y_mid = self.m.apply_hp_lp(mid, hp_hz=120.0, lp_hz=12000.0)
        y_hi = self.m.apply_hp_lp(hi, hp_hz=120.0, lp_hz=12000.0)
        self.assertLess(np.sqrt(np.mean(y_sub[4000:] ** 2)), 0.08)
        self.assertGreater(np.sqrt(np.mean(y_mid[4000:] ** 2)), 0.25)
        self.assertLess(np.sqrt(np.mean(y_hi[4000:] ** 2)), 0.12)

        # Hangover hold bridges short 20-frame (0.2s) gap, leaves 80-frame (0.8s) gap open
        m = np.zeros(160, dtype=bool)
        m[10:30] = True
        m[50:70] = True
        m[145:155] = True
        held = self.m.hangover_hold(m, fps=100.0, hold_s=0.35)
        self.assertTrue(held[30:50].all())
        self.assertFalse(held[70:145].any())

    def test_analyze_writes_spectral_json_and_respects_user_overrides(self):
        sys.path.insert(0, SCRIPTS)
        import aicn
        with tempfile.TemporaryDirectory() as d:
            for sub in ("data", "config", "media/stems", "media/audio"):
                os.makedirs(os.path.join(d, sub), exist_ok=True)
            t = np.arange(self.sr * 2) / self.sr
            rumble_voc = (0.35 * np.sin(2 * np.pi * 60 * t) + 0.15 * np.sin(2 * np.pi * 900 * t)).astype(np.float32)
            soundfile.write(os.path.join(d, "media/stems/vocals.wav"), np.stack([rumble_voc, rumble_voc], axis=1), self.sr)
            acc = (0.2 * np.sin(2 * np.pi * 400 * t)).astype(np.float32)
            soundfile.write(os.path.join(d, "media/stems/accomp.wav"), np.stack([acc, acc], axis=1), self.sr)
            # Dub with 50% silence padding: active-frame RMS must stay near speech level, not dropped by silence
            dub_sig = np.concatenate([np.zeros(self.sr, np.float32), self.tone(1.0)])
            soundfile.write(os.path.join(d, "media/audio/dub_voice_48k.wav"), dub_sig, self.sr)
            aicn.jdump(os.path.join(d, "data/00_video_profile.json"), {"duration": 2.0, "localization_mode": "voice_dub"})
            # Default: adaptive vocal_hp_hz raises to >=120 Hz on heavy 60Hz rumble
            self.m.cmd_analyze(d, dict(self.m.DEFAULTS), aicn.FastHash(d))
            spec = aicn.jload(os.path.join(d, "data/03_spectral_analysis.json"))
            self.assertGreaterEqual(spec["filter_plan"]["vocal_hp_hz"], 120.0)
            self.assertGreater(spec["stems"]["dub"]["rms_dbfs"], -18.0)
            # Explicit user override in config/audio_mix.json MUST be respected
            aicn.jdump(os.path.join(d, "config/audio_mix.json"), {"vocal_hp_hz": 75.0, "vocal_high_dip_db": -1.5})
            self.m.cmd_analyze(d, self.m.cfg(d), aicn.FastHash(d))
            spec2 = aicn.jload(os.path.join(d, "data/03_spectral_analysis.json"))
            self.assertEqual(spec2["filter_plan"]["vocal_hp_hz"], 75.0)
            self.assertEqual(spec2["filter_plan"]["vocal_high_dip_db"], -1.5)

    def test_follow_hold_and_end_to_end_mix_no_floor_slam_and_step_cache(self):
        sys.path.insert(0, SCRIPTS)
        import aicn
        # 1) Pure follow_hold check: 0.2s pause stays held flat (no release jump, no floor slam), then releases smoothly
        g = np.ones((1, 180), dtype=np.float64)
        g[0, 10:30] = 0.35
        g[0, 50:70] = 0.35
        held = self.m.follow_hold(g, fps=100.0, att=0.02, hold_s=0.35, rel=0.40)[0]
        self.assertGreater(held[35], 0.30)
        self.assertLess(held[45], 0.45)
        self.assertGreater(held[170], 0.80)

        # 2) End-to-end cmd_mix -> cmd_master + cache hit verification
        with tempfile.TemporaryDirectory() as d:
            for sub in ("data", "config", "media/stems", "media/audio"):
                os.makedirs(os.path.join(d, sub), exist_ok=True)
            t = np.arange(self.sr * 4) / self.sr
            voc = (0.18 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
            acc = (0.12 * np.sin(2 * np.pi * 250 * t)).astype(np.float32)
            # Dub speaks 0.5..1.5s, 0.20s breath pause 1.5..1.7s, speaks 1.7..2.7s, silence 2.7..4.0s
            dub = np.zeros(self.sr * 4, dtype=np.float32)
            dub[int(0.5 * self.sr):int(1.5 * self.sr)] = 0.25 * np.sin(2 * np.pi * 1000 * t[:self.sr])
            dub[int(1.7 * self.sr):int(2.7 * self.sr)] = 0.25 * np.sin(2 * np.pi * 1000 * t[:self.sr])
            soundfile.write(os.path.join(d, "media/stems/vocals.wav"), np.stack([voc, voc], axis=1), self.sr)
            soundfile.write(os.path.join(d, "media/stems/accomp.wav"), np.stack([acc, acc], axis=1), self.sr)
            soundfile.write(os.path.join(d, "media/audio/dub_voice_48k.wav"), dub, self.sr)
            aicn.jdump(os.path.join(d, "data/00_video_profile.json"), {"duration": 4.0, "localization_mode": "voice_dub"})
            fh = aicn.FastHash(d)
            c = self.m.cfg(d)
            self.m.cmd_mix(d, c, fh)
            st = aicn.jload(os.path.join(d, "data/03_mix_stats.json"))
            self.assertGreaterEqual(st["dub_snr_speech_db_median"], 8.0)
            # Second call with unchanged inputs must hit step cache without modifying premaster mtime
            pre_p = os.path.join(d, "media/audio/premaster_48k.wav")
            mt1 = os.path.getmtime(pre_p)
            self.m.cmd_mix(d, c, fh)
            self.assertEqual(os.path.getmtime(pre_p), mt1)

    def test_stage1_detects_modulated_whisper_and_phone_with_bass_and_rejects_steady_instrument(self):
        v1_dir = os.path.join(HERE, "..", "vendor", "video-asr-visual-audit", "scripts")
        sys.path.insert(0, v1_dir)
        import analyze_voice_waveform as avw
        sr16 = 16000
        t1 = np.arange(sr16 * 1) / sr16
        rng = np.random.default_rng(42)
        sil = np.zeros(sr16, dtype=np.float32)
        # Syllable modulation envelope (~5 Hz bursts like human speech)
        syl_env = (0.5 + 0.5 * np.sin(2 * np.pi * 5.0 * t1)).astype(np.float32) ** 2
        whisper = (syl_env * (0.011 * np.sin(2 * np.pi * 1400 * t1) + 0.006 * rng.standard_normal(sr16))).astype(np.float32)
        # Modulated phone call + heavy 80 Hz bass BGM (vocals stem silent because Demucs pushed phone to accomp)
        bass = (0.06 * np.sin(2 * np.pi * 80 * t1)).astype(np.float32)
        phone_speech = (syl_env * (0.045 * np.sin(2 * np.pi * 1200 * t1) + 0.035 * np.sin(2 * np.pi * 2100 * t1))).astype(np.float32)
        # Steady sustained 1.5 kHz solo instrument (zero syllable modulation) -> MUST NOT trigger phone_call
        steady_inst = (0.04 * np.sin(2 * np.pi * 1500 * t1)).astype(np.float32)

        voc = np.concatenate([sil, whisper, sil, sil, sil, sil])
        raw = np.concatenate([sil, whisper, sil, phone_speech + bass, sil, steady_inst])
        raw_tel = avw._bandpass_in_ram(raw, 300.0, 3400.0, sr=sr16)
        tel_norm = np.clip(raw_tel * 6.0, -0.95, 0.95).astype(np.float32)
        _, bursts = avw.compute_voice_mask_and_bursts(voc, tel_norm, raw_tel_arr=raw_tel, raw_arr=raw, sr=sr16)
        kinds = [(b["kind"], b["start"]) for b in bursts]
        self.assertTrue(any(k == "whisper" and 0.8 <= s <= 1.4 for k, s in kinds), kinds)
        self.assertTrue(any(k == "phone_call" and 2.8 <= s <= 3.4 for k, s in kinds), kinds)
        self.assertFalse(any(4.8 <= s <= 6.0 for _, s in kinds), f"Steady solo instrument falsely detected: {kinds}")
        prof = avw.compute_audio_spectral_profile(raw, sr=sr16)
        self.assertEqual(prof["nyquist_hz"], 8000)
        self.assertNotIn("recommended_lp_hz", prof)

    def test_stage2_overflow_predictor_catches_tight_tts(self):
        v2_dir = os.path.join(HERE, "..", "vendor", "zh-vi-script-adapter", "scripts")
        sys.path.insert(0, v2_dir)
        import validate_bilingual_script as vbs
        short_s = vbs.estimate_vieneu_tts_seconds("Đi thôi.")
        long_s = vbs.estimate_vieneu_tts_seconds("Đây là một câu tiếng Việt rất dài, nhiều âm tiết, có <en>GPU</en> nữa.")
        self.assertLess(short_s, 0.8)
        self.assertGreater(long_s, 3.0)

    def test_presynth_uses_cuda_infer_batch_single_process(self):
        calls = []
        sr = self.sr
        class _FakeCudaTTS:
            def infer_batch(self, texts, voice, temperature, top_k, batch_size):
                calls.append((list(texts), voice, temperature, top_k, batch_size))
                return [np.zeros(sr // 10, dtype=np.float32) for _ in texts]
        orig_cuda = self.m._tts_cuda_available
        orig_tts = self.m._TTS
        old_lock = os.environ.get("AICN_LOCK_DIR")
        try:
            self.m._tts_cuda_available = lambda: True
            self.m._TTS = _FakeCudaTTS()
            with tempfile.TemporaryDirectory() as td:
                os.environ["AICN_LOCK_DIR"] = os.path.join(td, "locks")
                jobs = [(f"Câu {i}", "Trúc Ly", 0.8, 25, os.path.join(td, f"j{i}.wav")) for i in range(5)]
                self.m.presynth(jobs, workers=4, threads=4, batch_size=2)
                self.assertEqual([len(c[0]) for c in calls], [2, 2, 1])
                for _, _, _, _, p in jobs:
                    self.assertTrue(os.path.exists(p))
        finally:
            if old_lock is None:
                os.environ.pop("AICN_LOCK_DIR", None)
            else:
                os.environ["AICN_LOCK_DIR"] = old_lock
            self.m._tts_cuda_available = orig_cuda
            self.m._TTS = orig_tts


if __name__ == "__main__":
    unittest.main()
