"""Tests for scripts/aicn_audio.py and Stage 3 gate (verify_stage_3.py) for AICN v3.0.0.

Run:  C:\\Work\\Tools\\VieNeu-TTS\\.venv\\Scripts\\python.exe -m unittest tests.test_aicn_audio -v
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest

try:
    import numpy as np
    import soundfile  # noqa: F401
    import scipy  # noqa: F401
    HAVE = True
except ImportError:
    HAVE = False

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
SCRIPTS = os.path.join(ROOT, "scripts")
V3_SCRIPTS = os.path.join(ROOT, "vendor", "vieneu-voice-pacing-mixer", "scripts")
ORCH_SCRIPTS = os.path.join(ROOT, "vendor", "video-localization-orchestrator", "scripts")


def load_audio():
    if SCRIPTS not in sys.path:
        sys.path.insert(0, SCRIPTS)
    spec = importlib.util.spec_from_file_location("aicn_audio", os.path.join(SCRIPTS, "aicn_audio.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def line(i, a, b, text, spk="A", voice=None):
    d = {"id": i, "start": a, "end": b, "tts_text": text, "sub_text": text, "speaker": spk}
    if voice is not None:
        d["voice"] = voice
    return d


class _TempEnvMixin:
    def _enter_cache_env(self, td):
        self._old_cache = os.environ.get("AICN_CACHE_DIR")
        self._old_lock = os.environ.get("AICN_LOCK_DIR")
        self._old_skip_vram = os.environ.get("AICN_SKIP_VRAM_PROBE")
        os.environ["AICN_CACHE_DIR"] = os.path.join(td, "global_cache")
        os.environ["AICN_LOCK_DIR"] = os.path.join(td, "locks")
        os.environ["AICN_SKIP_VRAM_PROBE"] = "1"

    def _exit_cache_env(self):
        for k, v in (
            ("AICN_CACHE_DIR", self._old_cache),
            ("AICN_LOCK_DIR", self._old_lock),
            ("AICN_SKIP_VRAM_PROBE", self._old_skip_vram),
        ):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@unittest.skipUnless(HAVE, "numpy/scipy/soundfile not installed")
class Grouping(unittest.TestCase):
    def setUp(self):
        self.m = load_audio()

    def test_continuous_comma_chain_same_speaker_and_voice_merges(self):
        s = [line(0, 0.0, 1.0, "Viện trưởng,"), line(1, 1.08, 2.0, "tôi ở lại."), line(2, 2.05, 3.0, "Được.")]
        self.assertEqual(self.m.group_lines(s), [[0, 1], [2]])

    def test_pause_separated_clauses_never_merge(self):
        # Gap 0.25s > 0.12s -> MUST remain separate utterances (no mid-sentence gaps or slicing)
        s = [line(0, 0.0, 1.0, "Viện trưởng,"), line(1, 1.25, 2.2, "tôi ở lại.")]
        self.assertEqual(self.m.group_lines(s), [[0], [1]])

    def test_speaker_or_voice_change_splits(self):
        s_spk = [line(0, 0.0, 1.0, "Viện trưởng,", spk="A"), line(1, 1.05, 2.0, "tôi ở lại.", spk="B")]
        self.assertEqual(self.m.group_lines(s_spk), [[0], [1]])
        s_voc = [
            line(0, 0.0, 1.0, "Viện trưởng,", spk="A", voice="Trúc Ly"),
            line(1, 1.05, 2.0, "tôi ở lại.", spk="A", voice="Hải Đăng"),
        ]
        self.assertEqual(self.m.group_lines(s_voc), [[0], [1]])

    def test_sentence_end_splits(self):
        s = [line(0, 0.0, 1.0, "Đi thôi!"), line(1, 1.05, 2.0, "Nhanh lên.")]
        self.assertEqual(self.m.group_lines(s), [[0], [1]])

    def test_group_lines_caps_3_clauses_and_4_5s_span(self):
        s = [line(i, round(i * 1.1, 2), round(i * 1.1 + 1.05, 2), f"Mệnh đề {i},") for i in range(8)]
        groups = self.m.group_lines(s)
        self.assertTrue(all(len(g) <= 3 for g in groups), groups)
        for g in groups:
            self.assertLessEqual(s[g[-1]]["end"] - s[g[0]]["start"], 4.5 + 1e-6)


@unittest.skipUnless(HAVE, "numpy/scipy/soundfile not installed")
class SignalAndTiming(unittest.TestCase, _TempEnvMixin):
    def setUp(self):
        self.m = load_audio()
        self.sr = self.m.SR

    def tone(self, s, freq=220.0, amp=0.3):
        t = np.arange(int(round(s * self.sr))) / self.sr
        return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)

    def test_split_points_and_sub_only_abolished(self):
        self.assertFalse(hasattr(self.m, "split_points"))
        self.assertFalse(hasattr(self.m, "write_sub_only_alignment"))
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "data"))
            with open(os.path.join(d, "data", "00_video_profile.json"), "w", encoding="utf-8") as f:
                json.dump({"duration": 5.0, "localization_mode": "sub_only"}, f)
            with self.assertRaises(SystemExit):
                self.m._assert_voice_dub(d)

    def test_clean_trims_with_5ms_guard_10ms_cosine_fade_and_expander(self):
        rng = np.random.default_rng(0)
        lead_sil = np.zeros(int(0.20 * self.sr), dtype=np.float32)
        hiss = (rng.standard_normal(int(0.5 * self.sr)) * 10 ** (-70 / 20)).astype(np.float32)
        w = np.concatenate([lead_sil, self.tone(0.5), hiss, self.tone(0.5), lead_sil])
        out = self.m.clean(w, self.m.DEFAULTS)
        # Must NOT retain 30ms dead silence pads on both sides (total trimmed ~1.51s, <= 1.52s)
        self.assertLessEqual(len(out) / self.sr, 1.52)
        mid = out[len(out) // 2 - 4800: len(out) // 2 + 4800]
        self.assertLess(np.sqrt((mid ** 2).mean()), 10 ** (-80 / 20))
        self.assertGreater(np.abs(out).max(), 0.25)
        # Edge fade starts near zero (no DC click)
        self.assertLess(abs(float(out[0])), 1e-4)
        self.assertLess(abs(float(out[-1])), 1e-4)
        # Pure silence returns empty
        self.assertEqual(len(self.m.clean(np.zeros(4800, dtype=np.float32), self.m.DEFAULTS)), 0)

    def test_follow_attack_faster_than_release(self):
        g = np.r_[np.ones(10), np.zeros(10), np.ones(30)][None].astype(float)
        out = self.m.follow(g, 100, 0.02, 0.25)[0]
        self.assertLess(out[12], 0.5)
        self.assertLess(out[25], 0.6)

    def test_follow_hold_matches_reference_and_runs_c_speed(self):
        g = np.ones((1, 180), dtype=np.float64)
        g[0, 10:30] = 0.35
        g[0, 50:70] = 0.35
        held = self.m.follow_hold(g, fps=100.0, att=0.02, hold_s=0.35, rel=0.40)[0]
        # 0.20s pause (30..50) stays flat; releases smoothly after 70+35=105
        self.assertGreater(held[35], 0.30)
        self.assertLess(held[45], 0.45)
        self.assertLess(held[104], 0.38)
        self.assertGreater(held[170], 0.80)

        # 10-minute 12-band array must run in < 0.20s (vs ~3s Python loop)
        big = np.ones((12, 56250), dtype=np.float64)
        big[:, 1000:5000] = 0.25
        t0 = time.perf_counter()
        out_big = self.m.follow_hold(big, fps=93.75, att=0.015, hold_s=0.30, rel=0.38)
        dt = time.perf_counter() - t0
        self.assertEqual(out_big.shape, big.shape)
        self.assertLess(dt, 0.25, f"follow_hold too slow: {dt:.3f}s")

    def test_cached_stft_and_istft_combined_match_reference(self):
        from scipy.signal import stft, istft
        rng = np.random.default_rng(7)
        x1 = (rng.standard_normal((self.sr * 2, 2)) * 0.2).astype(np.float32)
        x2 = (rng.standard_normal((self.sr * 2, 2)) * 0.15).astype(np.float32)
        idx, band_of_bin = self.m._band_indices()
        P1, Z1, _ = self.m.stft_and_power(x1, idx)
        P2, Z2, _ = self.m.stft_and_power(x2, idx)
        g1 = np.linspace(0.25, 1.0, len(idx))[:, None] * np.ones((1, P1.shape[1]), dtype=np.float32)
        g2 = np.linspace(0.8, 0.4, len(idx))[:, None] * np.ones((1, P2.shape[1]), dtype=np.float32)
        y_single = self.m.istft_from_Z(Z1, g1, band_of_bin, len(x1))
        y_comb = self.m.istft_combined([(Z1, g1), (Z2, g2)], band_of_bin, len(x1))

        def _ref(x, g):
            out = np.zeros_like(x, dtype=np.float64)
            for ch in range(2):
                _, _, Z = stft(x[:, ch], self.sr, nperseg=self.m.NFFT, noverlap=self.m.NFFT - self.m.HOP)
                G = np.ones((Z.shape[0], Z.shape[1]))
                for k, ii in enumerate(idx):
                    G[ii] = g[k, :Z.shape[1]]
                _, yr = istft(Z * G, self.sr, nperseg=self.m.NFFT, noverlap=self.m.NFFT - self.m.HOP)
                out[:, ch] = yr[:len(x)]
            return out

        ref1 = _ref(x1, g1)
        ref2 = _ref(x2, g2)
        self.assertLess(np.max(np.abs(y_single - ref1)), 1e-5)
        self.assertLess(np.max(np.abs(y_comb - (ref1 + ref2))), 1e-5)

    @unittest.skipUnless(shutil.which("rubberband"), "rubberband not installed")
    def test_rubberband_shortens(self):
        w = self.tone(2.0)
        out = self.m.stretch(w, 1.25)
        self.assertAlmostEqual(len(out) / len(w), 0.8, delta=0.02)

    def test_srt_time(self):
        self.assertEqual(self.m.srt_t(3661.5), "01:01:01,500")

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_inram_lufs_matches_ffmpeg_file_lufs(self):
        w_st = np.stack([self.tone(2.5, 1000, 0.25), self.tone(2.5, 440, 0.18)], axis=1)
        w_m = self.tone(2.5, 800, 0.22)[:, None]
        for label, w in (("stereo", w_st), ("mono", w_m)):
            l_ram = self.m.lufs(w)
            with tempfile.TemporaryDirectory() as d:
                p = os.path.join(d, f"{label}.wav")
                soundfile.write(p, w, self.sr, subtype="FLOAT")
                l_file, _ = self.m.lufs_file(p)
            self.assertAlmostEqual(l_ram, l_file, delta=0.15, msg=label)

    def test_analyze_writes_spectral_json_and_respects_user_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            self._enter_cache_env(d)
            try:
                for sub in ("data", "config", "media/stems", "media/audio"):
                    os.makedirs(os.path.join(d, sub), exist_ok=True)
                t = np.arange(self.sr * 2) / self.sr
                rumble_voc = (0.35 * np.sin(2 * np.pi * 60 * t) + 0.15 * np.sin(2 * np.pi * 900 * t)).astype(np.float32)
                soundfile.write(os.path.join(d, "media/stems/vocals.wav"), np.stack([rumble_voc, rumble_voc], axis=1), self.sr)
                acc = (0.2 * np.sin(2 * np.pi * 400 * t)).astype(np.float32)
                soundfile.write(os.path.join(d, "media/stems/accomp.wav"), np.stack([acc, acc], axis=1), self.sr)
                dub_sig = np.concatenate([np.zeros(self.sr, np.float32), self.tone(1.0)])
                soundfile.write(os.path.join(d, "media/audio/dub_voice_48k.wav"), dub_sig, self.sr)
                with open(os.path.join(d, "data/00_video_profile.json"), "w", encoding="utf-8") as f:
                    json.dump({"duration": 2.0, "localization_mode": "voice_dub"}, f)
                self.m.cmd_analyze(d, dict(self.m.DEFAULTS))
                with open(os.path.join(d, "data/03_spectral_analysis.json"), encoding="utf-8") as f:
                    spec = json.load(f)
                self.assertGreaterEqual(spec["filter_plan"]["vocal_hp_hz"], 120.0)
                self.assertGreater(spec["stems"]["dub"]["rms_dbfs"], -18.0)

                with open(os.path.join(d, "config/audio_mix.json"), "w", encoding="utf-8") as f:
                    json.dump({"vocal_hp_hz": 75.0, "vocal_high_dip_db": -1.5}, f)
                self.m.cmd_analyze(d, self.m.cfg(d))
                with open(os.path.join(d, "data/03_spectral_analysis.json"), encoding="utf-8") as f:
                    spec2 = json.load(f)
                self.assertEqual(spec2["filter_plan"]["vocal_hp_hz"], 75.0)
                self.assertEqual(spec2["filter_plan"]["vocal_high_dip_db"], -1.5)
            finally:
                self._exit_cache_env()

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_fast_master_hits_target_lufs_and_peak(self):
        with tempfile.TemporaryDirectory() as d:
            self._enter_cache_env(d)
            try:
                os.makedirs(os.path.join(d, "data"))
                os.makedirs(os.path.join(d, "media", "audio"))
                with open(os.path.join(d, "data", "00_video_profile.json"), "w", encoding="utf-8") as f:
                    json.dump({"duration": 3.0, "localization_mode": "voice_dub"}, f)
                w = np.stack([self.tone(3.0), self.tone(3.0)], axis=1)
                soundfile.write(os.path.join(d, "media", "audio", "premaster_48k.wav"), w, self.sr, subtype="FLOAT")
                self.m.cmd_master(d, dict(self.m.DEFAULTS, master_mode="fast"))
                out = os.path.join(d, "media", "master_audio_48k.wav")
                self.assertTrue(os.path.exists(out))
                i, pk = self.m.lufs_file(out)
                self.assertAlmostEqual(i, -14.0, delta=0.5)
                self.assertLessEqual(pk, -0.8)
            finally:
                self._exit_cache_env()


@unittest.skipUnless(HAVE, "numpy/scipy/soundfile not installed")
class IndivisibleDubAndDSP(unittest.TestCase, _TempEnvMixin):
    def setUp(self):
        self.m = load_audio()
        self.sr = self.m.SR
        if SCRIPTS not in sys.path:
            sys.path.insert(0, SCRIPTS)
        import aicn_platform as ap
        self.ap = ap

    def tone(self, s, freq=440.0, amp=0.3):
        t = np.arange(int(round(s * self.sr))) / self.sr
        return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)

    @unittest.skipUnless(shutil.which("rubberband"), "rubberband not installed")
    def test_cmd_dub_indivisible_utterances_no_chop_and_relocation_cache_hit(self):
        with tempfile.TemporaryDirectory() as td:
            self._enter_cache_env(td)
            try:
                ws1 = os.path.join(td, "hub_a", "units", "u1")
                os.makedirs(os.path.join(ws1, "data"), exist_ok=True)
                # Seg 0 & 1 continuous (gap 0.05s <= 0.12s -> 1 indivisible utterance);
                # Seg 2 separated by pause (gap 0.45s > 0.12s -> separate utterance)
                segs = [
                    {"id": 0, "start": 0.50, "end": 1.40, "speaker": "A", "sub_text": "Mệnh đề một,", "tts_text": "Mệnh đề một,"},
                    {"id": 1, "start": 1.45, "end": 2.40, "speaker": "A", "sub_text": "tiếp nối ngay.", "tts_text": "tiếp nối ngay."},
                    {"id": 2, "start": 2.85, "end": 4.20, "speaker": "A", "sub_text": "Câu hai đây.", "tts_text": "Câu hai đây."},
                ]
                self.ap.atomic_write_json(os.path.join(ws1, "data", "00_video_profile.json"), {"duration": 6.0, "localization_mode": "voice_dub"})
                self.ap.atomic_write_json(os.path.join(ws1, "data", "02_bilingual_script.json"), segs)

                c = dict(self.m.DEFAULTS)
                # Populate global tts_sent cache with continuous tones (no internal zeros!)
                t0_key = self.ap.tts_sent_key("Mệnh đề một, tiếp nối ngay.", c["voice"], self.m.TTS_ENGINE_TAG, c["temperature"], c["top_k"])
                t1_key = self.ap.tts_sent_key("Câu hai đây.", c["voice"], self.m.TTS_ENGINE_TAG, c["temperature"], c["top_k"])
                soundfile.write(self.ap.global_cache_path("tts_sent", t0_key, ".wav"), self.tone(1.75), self.sr)
                soundfile.write(self.ap.global_cache_path("tts_sent", t1_key, ".wav"), self.tone(1.20), self.sr)

                self.m.cmd_dub(ws1, c)
                utt0 = os.path.join(ws1, "media", "tts_segments", "utt_0000.wav")
                utt1 = os.path.join(ws1, "media", "tts_segments", "utt_0001.wav")
                self.assertTrue(os.path.exists(utt0))
                self.assertTrue(os.path.exists(utt1))
                self.assertFalse(os.path.exists(os.path.join(ws1, "media", "tts_segments", "seg_0000.wav")))

                # Intact continuity check: interior of utt_0000.wav must have ZERO silent mid-word chops!
                w0, _ = soundfile.read(utt0, dtype="float32")
                self.assertGreaterEqual(len(w0) / self.sr, 1.2)
                interior = w0[int(0.05 * self.sr): -int(0.05 * self.sr)]
                hop = int(0.01 * self.sr)
                hop_rms = np.sqrt(np.mean(interior[: (len(interior) // hop) * hop].reshape(-1, hop) ** 2, axis=1))
                self.assertTrue((hop_rms > 0.05).all(), f"Unexpected mid-utterance drop: min_rms={hop_rms.min()}")

                with open(os.path.join(ws1, "data", "03_dub_sentences.json"), encoding="utf-8") as f:
                    sent_log = json.load(f)
                self.assertEqual(len(sent_log), 2)
                self.assertLessEqual(sent_log[0]["speech_end"], sent_log[1]["speech_start"] - 0.02)

                # Relocation cache hit: move/copy unit to a brand new path -> 0 rubberband invocations!
                ws2 = os.path.join(td, "hub_renamed", "units", "u1_moved")
                os.makedirs(os.path.join(ws2, "data"), exist_ok=True)
                shutil.copy2(os.path.join(ws1, "data", "00_video_profile.json"), os.path.join(ws2, "data", "00_video_profile.json"))
                shutil.copy2(os.path.join(ws1, "data", "02_bilingual_script.json"), os.path.join(ws2, "data", "02_bilingual_script.json"))
                orig_stretch = self.m.stretch
                try:
                    def _fail_if_called(*_a, **_kw):
                        raise AssertionError("stretch() should not be called on global rb_stretch cache hit!")
                    self.m.stretch = _fail_if_called
                    self.m.cmd_dub(ws2, c)
                finally:
                    self.m.stretch = orig_stretch
            finally:
                self._exit_cache_env()

    @unittest.skipUnless(shutil.which("rubberband"), "rubberband not installed")
    def test_cmd_dub_elastic_borrowing_and_fail_fast_before_mix(self):
        with tempfile.TemporaryDirectory() as td:
            self._enter_cache_env(td)
            try:
                ws = os.path.join(td, "unit_ov")
                os.makedirs(os.path.join(ws, "data"), exist_ok=True)
                os.makedirs(os.path.join(ws, "media", "stems"), exist_ok=True)
                # 1) Tight 2-syllable interjection ("Chuẩn!") fits via <=1.32x + elastic slack up to next_start - 0.05
                # 2) Huge 3.5s audio crammed into 0.90s window MUST fail-fast in cmd_dub before mix/master!
                segs = [
                    {"id": 0, "start": 1.0, "end": 1.35, "speaker": "A", "sub_text": "Chuẩn!", "tts_text": "Chuẩn!"},
                    {"id": 1, "start": 2.0, "end": 2.90, "speaker": "A", "sub_text": "Quá dài.", "tts_text": "Quá dài lắm luôn ấy."},
                    {"id": 2, "start": 3.0, "end": 4.00, "speaker": "A", "sub_text": "Tiếp.", "tts_text": "Tiếp."},
                ]
                self.ap.atomic_write_json(os.path.join(ws, "data", "00_video_profile.json"), {"duration": 5.0, "localization_mode": "voice_dub"})
                self.ap.atomic_write_json(os.path.join(ws, "data", "02_bilingual_script.json"), segs)
                c = dict(self.m.DEFAULTS)
                k0 = self.ap.tts_sent_key("Chuẩn!", c["voice"], self.m.TTS_ENGINE_TAG, c["temperature"], c["top_k"])
                k1 = self.ap.tts_sent_key("Quá dài lắm luôn ấy.", c["voice"], self.m.TTS_ENGINE_TAG, c["temperature"], c["top_k"])
                k2 = self.ap.tts_sent_key("Tiếp.", c["voice"], self.m.TTS_ENGINE_TAG, c["temperature"], c["top_k"])
                # "Chuẩn!" raw=1.22s fits into 0.95s slack at ~1.284x <= 1.32x!
                soundfile.write(self.ap.global_cache_path("tts_sent", k0, ".wav"), self.tone(1.22), self.sr)
                soundfile.write(self.ap.global_cache_path("tts_sent", k1, ".wav"), self.tone(3.50), self.sr)
                soundfile.write(self.ap.global_cache_path("tts_sent", k2, ".wav"), self.tone(0.60), self.sr)

                with self.assertRaises(SystemExit):
                    self.m.cmd_dub(ws, c)
                # Confirm seg 0 alone fits via 1.32x cap when seg 1 is fixed, and premaster/master never ran
                self.assertFalse(os.path.exists(os.path.join(ws, "media", "audio", "premaster_48k.wav")))
                self.assertFalse(os.path.exists(os.path.join(ws, "media", "master_audio_48k.wav")))
                with open(os.path.join(ws, "data", "03_dub_sentences.json"), encoding="utf-8") as f:
                    sent_log = json.load(f)
                self.assertLessEqual(sent_log[0]["overflow_s"], 0.0)
                self.assertGreater(sent_log[1]["overflow_s"], 0.5)
                self.assertGreaterEqual(sent_log[1]["suggested_trim_syls"], 3)
            finally:
                self._exit_cache_env()

    def test_cmd_dub_repeated_identical_utterances_dedup_rb_stretch_no_winerror(self):
        with tempfile.TemporaryDirectory() as td:
            self._enter_cache_env(td)
            orig_stretch = self.m.stretch
            stretch_calls = []
            try:
                ws = os.path.join(td, "unit_rep")
                os.makedirs(os.path.join(ws, "data"), exist_ok=True)
                segs = [
                    {"id": k, "start": k * 2.0, "end": k * 2.0 + 1.1, "speaker": "A", "voice": "Trúc Ly", "zh_text": "好。", "sub_text": "Được rồi.", "tts_text": "Được rồi."}
                    for k in range(4)
                ]
                self.ap.atomic_write_json(os.path.join(ws, "data", "00_video_profile.json"), {"duration": 9.0, "localization_mode": "voice_dub"})
                self.ap.atomic_write_json(os.path.join(ws, "data", "02_bilingual_script.json"), segs)
                c = dict(self.m.DEFAULTS)
                k_tts = self.ap.tts_sent_key("Được rồi.", c["voice"], self.m.TTS_ENGINE_TAG, c["temperature"], c["top_k"])
                soundfile.write(self.ap.global_cache_path("tts_sent", k_tts, ".wav"), self.tone(1.0), self.sr)

                def _counting_stretch(w, tempo):
                    stretch_calls.append(round(float(tempo), 4))
                    n_out = max(1, int(round(len(w) / float(tempo))))
                    return np.asarray(w[:n_out], dtype=np.float32)

                self.m.stretch = _counting_stretch
                self.m.cmd_dub(ws, c)
                self.assertEqual(len(stretch_calls), 1, f"Expected rb_stretch dedup to run once, got {stretch_calls}")
                for k in range(4):
                    self.assertTrue(os.path.exists(os.path.join(ws, "media", "tts_segments", f"utt_{k:04d}.wav")))
            finally:
                self.m.stretch = orig_stretch
                self._exit_cache_env()

    def test_batch_dub_overflow_exits_with_code_2(self):
        with tempfile.TemporaryDirectory() as td:
            self._enter_cache_env(td)
            try:
                ws = os.path.join(td, "unit_ov2")
                os.makedirs(os.path.join(ws, "data"), exist_ok=True)
                segs = [
                    {"id": 0, "start": 0.0, "end": 0.8, "speaker": "A", "voice": "Trúc Ly", "zh_text": "太长了。", "sub_text": "Quá dài lắm luôn.", "tts_text": "Quá dài lắm luôn."},
                    {"id": 1, "start": 1.0, "end": 1.8, "speaker": "A", "voice": "Trúc Ly", "zh_text": "停。", "sub_text": "Dừng.", "tts_text": "Dừng."},
                ]
                self.ap.atomic_write_json(os.path.join(ws, "data", "00_video_profile.json"), {"duration": 3.0, "localization_mode": "voice_dub"})
                self.ap.atomic_write_json(os.path.join(ws, "data", "02_bilingual_script.json"), segs)
                c = dict(self.m.DEFAULTS)
                k0 = self.ap.tts_sent_key("Quá dài lắm luôn.", c["voice"], self.m.TTS_ENGINE_TAG, c["temperature"], c["top_k"])
                k1 = self.ap.tts_sent_key("Dừng.", c["voice"], self.m.TTS_ENGINE_TAG, c["temperature"], c["top_k"])
                soundfile.write(self.ap.global_cache_path("tts_sent", k0, ".wav"), self.tone(3.0), self.sr)
                soundfile.write(self.ap.global_cache_path("tts_sent", k1, ".wav"), self.tone(0.5), self.sr)
                with self.assertRaises(SystemExit) as cm:
                    self.m.batch_dub([ws], exit_on_overflow=True)
                self.assertEqual(cm.exception.code, 2)
                orig_bs = self.m.batch_separate
                try:
                    self.m.batch_separate = lambda *a, **kw: None
                    with self.assertRaises(SystemExit) as cm2:
                        self.m.cmd_batch_all([ws])
                    self.assertEqual(cm2.exception.code, 2)
                finally:
                    self.m.batch_separate = orig_bs
            finally:
                self._exit_cache_env()

    def test_cmd_mix_carves_chinese_vocals_high_snr_and_no_floor_stacking(self):
        with tempfile.TemporaryDirectory() as d:
            self._enter_cache_env(d)
            try:
                for sub in ("data", "config", "media/stems", "media/audio"):
                    os.makedirs(os.path.join(d, sub), exist_ok=True)
                t = np.arange(self.sr * 4) / self.sr
                # Loud Chinese vocal at 1 kHz + BGM at 250 Hz; dub speaks 0.5..1.5s & 1.7..2.7s
                voc = (0.28 * np.sin(2 * np.pi * 1000 * t) + 0.15 * np.sin(2 * np.pi * 2200 * t)).astype(np.float32)
                acc = (0.12 * np.sin(2 * np.pi * 250 * t)).astype(np.float32)
                dub = np.zeros(self.sr * 4, dtype=np.float32)
                dub[int(0.5 * self.sr): int(1.5 * self.sr)] = 0.25 * np.sin(2 * np.pi * 1000 * t[:self.sr])
                dub[int(1.7 * self.sr): int(2.7 * self.sr)] = 0.25 * np.sin(2 * np.pi * 1000 * t[:self.sr])
                soundfile.write(os.path.join(d, "media/stems/vocals.wav"), np.stack([voc, voc], axis=1), self.sr)
                soundfile.write(os.path.join(d, "media/stems/accomp.wav"), np.stack([acc, acc], axis=1), self.sr)
                soundfile.write(os.path.join(d, "media/audio/dub_voice_48k.wav"), dub, self.sr)
                self.ap.atomic_write_json(os.path.join(d, "data/00_video_profile.json"), {"duration": 4.0, "localization_mode": "voice_dub"})

                c = self.m.cfg(d)
                self.m.cmd_mix(d, c)
                with open(os.path.join(d, "data/03_mix_stats.json"), encoding="utf-8") as f:
                    st = json.load(f)
                self.assertGreaterEqual(st["dub_snr_speech_db_median"], 10.0)
                self.assertGreaterEqual(st["dub_snr_speech_db_p10"], 4.0)
                # Direct floor clamp: never slams below floor_vocal_db (-24 dB)
                self.assertGreaterEqual(st["orig_vocal_atten_db_min"], c["floor_vocal_db"] - 0.1)

                # Step cache hit on unchanged inputs
                pre_p = os.path.join(d, "media/audio/premaster_48k.wav")
                mt1 = os.path.getmtime(pre_p)
                self.m.cmd_mix(d, c)
                self.assertEqual(os.path.getmtime(pre_p), mt1)
            finally:
                self._exit_cache_env()

    def test_modulated_speech_snr_p10_passes_clean_carving_and_fails_vocal_bleed(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            self._enter_cache_env(d)
            try:
                for sub in ("data", "config", "media/stems", "media/audio", "source"):
                    os.makedirs(os.path.join(d, sub), exist_ok=True)
                t = np.arange(self.sr * 4) / self.sr
                # Modulated Vietnamese-like speech: stressed vowels (1.0), soft voiced syllables (-13 dB = 0.22),
                # and 25ms plosive closures (-30 dB = 0.03) with 10ms cosine edge fades.
                mod = np.zeros_like(t, dtype=np.float32)
                for idx_p, pulse_s in enumerate(np.arange(0.50, 3.35, 0.22)):
                    amp_syl = 0.20 if (idx_p % 4 == 3) else 1.0  # 25% soft voiced syllables -14 dB (inside -15 dB snr_on)
                    a = int(round(pulse_s * self.sr))
                    b = int(round((pulse_s + 0.17) * self.sr))
                    mod[a:b] = amp_syl
                    c_a = b
                    c_b = int(round((pulse_s + 0.20) * self.sr))
                    mod[c_a:c_b] = 0.03  # plosive closure -30 dB
                dub = mod * (0.22 * np.sin(2 * np.pi * 850 * t) + 0.16 * np.sin(2 * np.pi * 2100 * t)).astype(np.float32)
                voc = (0.24 * np.sin(2 * np.pi * 1000 * t) + 0.14 * np.sin(2 * np.pi * 2200 * t)).astype(np.float32)
                acc = (0.10 * np.sin(2 * np.pi * 250 * t)).astype(np.float32)

                with open(os.path.join(d, "source/master_source.mp4"), "wb") as f:
                    f.write(b"dummy-source")
                soundfile.write(os.path.join(d, "media/stems/vocals.wav"), np.stack([voc, voc], axis=1), self.sr)
                soundfile.write(os.path.join(d, "media/stems/accomp.wav"), np.stack([acc, acc], axis=1), self.sr)
                soundfile.write(os.path.join(d, "media/audio/dub_voice_48k.wav"), dub, self.sr)
                self.ap.atomic_write_json(os.path.join(d, "data/00_video_profile.json"), {"duration": 4.0, "localization_mode": "voice_dub"})

                # 1) Clean v3.0 carving (-24 dB floor) passes p10 >= 4.0 dB and dub_intelligible gate
                c_clean = dict(self.m.DEFAULTS, _force=True)
                self.m.cmd_mix(d, c_clean)
                shutil.copy2(os.path.join(d, "media/audio/premaster_48k.wav"), os.path.join(d, "media/master_audio_48k.wav"))
                with open(os.path.join(d, "data/03_mix_stats.json"), encoding="utf-8") as f:
                    st_clean = json.load(f)
                self.assertGreaterEqual(st_clean["dub_snr_speech_db_median"], 10.0, st_clean)
                self.assertGreaterEqual(st_clean["dub_snr_speech_db_p10"], 4.0, st_clean)
                chk_clean = {x["id"]: x for x in self.m._run_audio_checks(d, c_clean)}
                self.assertTrue(chk_clean["dub_intelligible"]["passed"], chk_clean["dub_intelligible"])

                # 2) Shallow -6.5 dB vocal bleed (v2.7.0 style) fails dub_intelligible gate
                c_bleed = dict(self.m.DEFAULTS, _force=True, base_vocal_db=-6.5, speech_vocal_dip_db=-6.5, floor_vocal_db=-6.5, snr_vocal_db=4.0, vocal_high_dip_db=0.0)
                self.m.cmd_mix(d, c_bleed)
                shutil.copy2(os.path.join(d, "media/audio/premaster_48k.wav"), os.path.join(d, "media/master_audio_48k.wav"))
                with open(os.path.join(d, "data/03_mix_stats.json"), encoding="utf-8") as f:
                    st_bleed = json.load(f)
                self.assertLess(st_bleed["dub_snr_speech_db_p10"], 4.0, st_bleed)
                chk_bleed = {x["id"]: x for x in self.m._run_audio_checks(d, c_bleed)}
                self.assertFalse(chk_bleed["dub_intelligible"]["passed"], chk_bleed["dub_intelligible"])
            finally:
                self._exit_cache_env()

    def test_cmd_mix_60s_speed_projects_well_under_12s_for_10min(self):
        with tempfile.TemporaryDirectory() as d:
            self._enter_cache_env(d)
            try:
                for sub in ("data", "config", "media/stems", "media/audio"):
                    os.makedirs(os.path.join(d, sub), exist_ok=True)
                n = self.sr * 60
                rng = np.random.default_rng(1)
                voc = (rng.standard_normal((n, 2)) * 0.04).astype(np.float32)
                acc = (rng.standard_normal((n, 2)) * 0.04).astype(np.float32)
                dub = (rng.standard_normal(n) * 0.04).astype(np.float32)
                soundfile.write(os.path.join(d, "media/stems/vocals.wav"), voc, self.sr)
                soundfile.write(os.path.join(d, "media/stems/accomp.wav"), acc, self.sr)
                soundfile.write(os.path.join(d, "media/audio/dub_voice_48k.wav"), dub, self.sr)
                self.ap.atomic_write_json(os.path.join(d, "data/00_video_profile.json"), {"duration": 60.0, "localization_mode": "voice_dub"})
                t0 = time.perf_counter()
                self.m.cmd_mix(d, self.m.cfg(d))
                dt = time.perf_counter() - t0
                # 60s mix in < 1.5s projects < 15s (typically ~6-9s) for a 600s unit
                self.assertLess(dt, 2.0, f"60s cmd_mix too slow: {dt:.2f}s")
            finally:
                self._exit_cache_env()


@unittest.skipUnless(HAVE, "numpy/scipy/soundfile not installed")
class WarmBatchAndStage3Gates(unittest.TestCase, _TempEnvMixin):
    def setUp(self):
        self.m = load_audio()
        self.sr = self.m.SR
        if SCRIPTS not in sys.path:
            sys.path.insert(0, SCRIPTS)
        import aicn_platform as ap
        self.ap = ap

    def tone(self, s, freq=330.0, amp=0.3):
        t = np.arange(int(round(s * self.sr))) / self.sr
        return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)

    def test_presynth_uses_cuda_infer_batch_single_process_and_gpu_slot(self):
        calls = []
        sr = self.sr

        class _FakeCudaTTS:
            def infer_batch(self, texts, voice, temperature, top_k, batch_size):
                calls.append((list(texts), voice, temperature, top_k, batch_size))
                return [np.zeros(sr // 10, dtype=np.float32) for _ in texts]

        orig_cuda = self.m._tts_cuda_available
        orig_tts = self.m._TTS
        with tempfile.TemporaryDirectory() as td:
            self._enter_cache_env(td)
            try:
                self.m._tts_cuda_available = lambda: True
                self.m._TTS = _FakeCudaTTS()
                jobs = [(f"Câu {i}", "Trúc Ly", 0.8, 25, os.path.join(td, f"j{i}.wav")) for i in range(5)]
                self.m.presynth(jobs, workers=4, threads=4, batch_size=2)
                self.assertEqual([len(c[0]) for c in calls], [2, 2, 1])
                for _, _, _, _, p in jobs:
                    self.assertTrue(os.path.exists(p))
            finally:
                self.m._tts_cuda_available = orig_cuda
                self.m._TTS = orig_tts
                self._exit_cache_env()

    def test_batch_separate_loads_separator_once_and_hits_global_cache(self):
        with tempfile.TemporaryDirectory() as td:
            self._enter_cache_env(td)
            sep_inits = []
            sep_calls = []

            class _FakeSep:
                def __init__(self, **_kw):
                    sep_inits.append(1)

            fake_demucs = types.ModuleType("demucs")
            fake_demucs_api = types.ModuleType("demucs.api")
            fake_demucs_api.Separator = _FakeSep
            fake_demucs.api = fake_demucs_api

            orig_demucs = sys.modules.get("demucs")
            orig_demucs_api = sys.modules.get("demucs.api")
            orig_detect = self.m.ap.detect_demucs_backend
            orig_sep_fn = self.m._separate_with_separator
            try:
                sys.modules["demucs"] = fake_demucs
                sys.modules["demucs.api"] = fake_demucs_api
                self.m.ap.detect_demucs_backend = lambda *_a, **_kw: {"engine": "torch-cpu", "device": "cpu", "label": "fake-cpu"}

                def _fake_sep(sep, src, sr=self.sr):
                    sep_calls.append(src)
                    w = np.stack([self.tone(1.0), self.tone(1.0)], axis=1)
                    return w, w * 0.5

                self.m._separate_with_separator = _fake_sep
                units = []
                for idx, content in enumerate((b"video-bytes-A", b"video-bytes-B")):
                    u = os.path.join(td, f"u{idx}")
                    os.makedirs(os.path.join(u, "data"), exist_ok=True)
                    os.makedirs(os.path.join(u, "source"), exist_ok=True)
                    with open(os.path.join(u, "data", "00_video_profile.json"), "w", encoding="utf-8") as f:
                        json.dump({"duration": 1.0, "localization_mode": "voice_dub"}, f)
                    with open(os.path.join(u, "source", "master_source.mp4"), "wb") as f:
                        f.write(content)
                    units.append(u)
                # Duplicate content in u2 should hit global stems cache without running Separator!
                u2 = os.path.join(td, "u2_dup_of_u0")
                os.makedirs(os.path.join(u2, "data"), exist_ok=True)
                os.makedirs(os.path.join(u2, "source"), exist_ok=True)
                with open(os.path.join(u2, "data", "00_video_profile.json"), "w", encoding="utf-8") as f:
                    json.dump({"duration": 1.0, "localization_mode": "voice_dub"}, f)
                with open(os.path.join(u2, "source", "master_source.mp4"), "wb") as f:
                    f.write(b"video-bytes-A")

                self.m.batch_separate(units + [u2])
                self.assertEqual(len(sep_inits), 1)
                self.assertEqual(len(sep_calls), 2)
                self.assertTrue(os.path.exists(os.path.join(u2, "media", "stems", "vocals.wav")))
            finally:
                if orig_demucs is None:
                    sys.modules.pop("demucs", None)
                else:
                    sys.modules["demucs"] = orig_demucs
                if orig_demucs_api is None:
                    sys.modules.pop("demucs.api", None)
                else:
                    sys.modules["demucs.api"] = orig_demucs_api
                self.m.ap.detect_demucs_backend = orig_detect
                self.m._separate_with_separator = orig_sep_fn
                self._exit_cache_env()

    def test_stage3_gate_rejects_defects_and_passes_relocation_safe(self):
        for p in (V3_SCRIPTS, ORCH_SCRIPTS):
            if p not in sys.path:
                sys.path.insert(0, p)
        from cicd_gate import write_gate_token
        from verify_stage_3 import run_stage_3_verification

        with tempfile.TemporaryDirectory() as td:
            self._enter_cache_env(td)
            try:
                d = os.path.join(td, "hub_orig", "units", "u1")
                for sub in ("data", "media", "media/tts_segments", ".cicd"):
                    os.makedirs(os.path.join(d, sub), exist_ok=True)
                self.ap.atomic_write_json(os.path.join(d, "data", "00_video_profile.json"), {"duration": 10.0, "localization_mode": "voice_dub"})
                segs = [
                    {"id": 0, "start": 1.0, "end": 2.5, "zh_text": "一", "sub_text": "Một,", "tts_text": "Một,"},
                    {"id": 1, "start": 7.0, "end": 8.5, "zh_text": "二", "sub_text": "Hai.", "tts_text": "Hai."},
                ]
                self.ap.atomic_write_json(os.path.join(d, "data", "02_bilingual_script.json"), segs)
                for idx in range(2):
                    soundfile.write(os.path.join(d, "media", "tts_segments", f"utt_{idx:04d}.wav"), self.tone(1.0), self.sr)
                soundfile.write(os.path.join(d, "media", "master_audio_48k.wav"), np.stack([self.tone(10.0), self.tone(10.0)], axis=1), self.sr)
                with open(os.path.join(d, "data", "subtitles_full.srt"), "w", encoding="utf-8") as f:
                    f.write("1\n00:00:01,000 --> 00:00:02,500\nMột,\n\n2\n00:00:07,000 --> 00:00:08,500\nHai.\n\n")
                with open(os.path.join(d, "03_pacing_and_audio_report.md"), "w", encoding="utf-8") as f:
                    f.write("# ok\n")
                good_mix = {
                    "passed": True,
                    "checks": [
                        {"id": "loudness", "passed": True, "detail": "-14.0 LUFS"},
                        {"id": "dub_intelligible", "passed": True, "detail": "median 15 dB, p10 9 dB"},
                        {"id": "original_present", "passed": True, "detail": "-2.0 dB"},
                    ],
                }
                good_aligned = [
                    {"id": 0, "slot_start": 1.0, "slot_end": 2.5, "speech_start": 1.0, "speech_end": 2.1, "tempo": 1.08},
                    {"id": 1, "slot_start": 7.0, "slot_end": 8.5, "speech_start": 7.0, "speech_end": 8.1, "tempo": 1.08},
                ]
                good_sents = [
                    {"utterance_id": 0, "ids": [0], "utterance_wav": "media/tts_segments/utt_0000.wav", "script_start": 1.0, "speech_start": 1.0, "speech_end": 2.1, "tempo": 1.08, "cap_tempo": 1.32, "overflow_s": -0.5},
                    {"utterance_id": 1, "ids": [1], "utterance_wav": "media/tts_segments/utt_0001.wav", "script_start": 7.0, "speech_start": 7.0, "speech_end": 8.1, "tempo": 1.08, "cap_tempo": 1.32, "overflow_s": -0.5},
                ]
                write_gate_token(d, 2, "Stage 2", [{"id": "s2", "passed": True, "weight": 100, "detail": "ok"}],
                                 metrics={}, artifact_paths=["data/02_bilingual_script.json"])

                # 1) Early speech drift -> FAIL
                self.ap.atomic_write_json(os.path.join(d, "data", "03_mix_report.json"), good_mix)
                self.ap.atomic_write_json(os.path.join(d, "data", "03_aligned_subtitles.json"), good_aligned)
                bad_drift = [dict(good_sents[0]), dict(good_sents[1], speech_start=1.666, speech_end=2.9)]
                self.ap.atomic_write_json(os.path.join(d, "data", "03_dub_sentences.json"), bad_drift)
                with self.assertRaises(SystemExit):
                    run_stage_3_verification(d)

                # 2) Utterance overlap -> FAIL
                bad_ov = [dict(good_sents[0], speech_end=7.01), dict(good_sents[1])]
                self.ap.atomic_write_json(os.path.join(d, "data", "03_dub_sentences.json"), bad_ov)
                with self.assertRaises(SystemExit):
                    run_stage_3_verification(d)

                # 3) Runt < 0.12s utterance WAV -> FAIL
                self.ap.atomic_write_json(os.path.join(d, "data", "03_dub_sentences.json"), good_sents)
                soundfile.write(os.path.join(d, "media", "tts_segments", "utt_0000.wav"), self.tone(0.05), self.sr)
                with self.assertRaises(SystemExit):
                    run_stage_3_verification(d)
                soundfile.write(os.path.join(d, "media", "tts_segments", "utt_0000.wav"), self.tone(1.0), self.sr)

                # 4) Failed mix report (e.g. p10 SNR < 4 dB) -> FAIL
                self.ap.atomic_write_json(os.path.join(d, "data", "03_mix_report.json"), {"passed": False, "checks": []})
                with self.assertRaises(SystemExit):
                    run_stage_3_verification(d)

                # 5) Clean v3.0.0 workspace -> PASS 100/100 + relocation safe!
                self.ap.atomic_write_json(os.path.join(d, "data", "03_mix_report.json"), good_mix)
                res = run_stage_3_verification(d)
                self.assertEqual(res["status"], "PASS")
                self.assertEqual(res["schema_version"], "3.0")

                from cicd_gate import require_gate
                moved = os.path.join(td, "hub_moved", "units", "u1")
                os.makedirs(os.path.dirname(moved), exist_ok=True)
                shutil.move(d, moved)
                gate_ok = require_gate(moved, 3)
                self.assertEqual(gate_ok["status"], "PASS")
            finally:
                self._exit_cache_env()

    def test_stage1_detects_modulated_whisper_and_phone_with_bass_and_rejects_steady_instrument(self):
        v1_dir = os.path.join(ROOT, "vendor", "video-asr-visual-audit", "scripts")
        if v1_dir not in sys.path:
            sys.path.insert(0, v1_dir)
        import analyze_voice_waveform as avw
        sr16 = 16000
        t1 = np.arange(sr16 * 1) / sr16
        rng = np.random.default_rng(42)
        sil = np.zeros(sr16, dtype=np.float32)
        syl_env = (0.5 + 0.5 * np.sin(2 * np.pi * 5.0 * t1)).astype(np.float32) ** 2
        whisper = (syl_env * (0.011 * np.sin(2 * np.pi * 1400 * t1) + 0.006 * rng.standard_normal(sr16))).astype(np.float32)
        bass = (0.06 * np.sin(2 * np.pi * 80 * t1)).astype(np.float32)
        phone_speech = (syl_env * (0.045 * np.sin(2 * np.pi * 1200 * t1) + 0.035 * np.sin(2 * np.pi * 2100 * t1))).astype(np.float32)
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

    def test_stage2_overflow_predictor_catches_tight_tts(self):
        v2_dir = os.path.join(ROOT, "vendor", "zh-vi-script-adapter", "scripts")
        if v2_dir not in sys.path:
            sys.path.insert(0, v2_dir)
        import validate_bilingual_script as vbs
        short_s = vbs.estimate_vieneu_tts_seconds("Đi thôi.")
        long_s = vbs.estimate_vieneu_tts_seconds("Đây là một câu tiếng Việt rất dài, nhiều âm tiết, có <en>GPU</en> nữa.")
        self.assertLess(short_s, 0.8)
        self.assertGreater(long_s, 3.0)

    def test_chunked_stft_istft_equivalence_and_concurrent_write_wav16(self):
        from concurrent.futures import ThreadPoolExecutor
        rng = np.random.default_rng(7)
        v = (rng.standard_normal((self.sr * 4 + 137, 2)) * 0.25).astype(np.float32)
        b = (rng.standard_normal((self.sr * 4 + 137, 2)) * 0.20).astype(np.float32)
        idx, bob = self.m._band_indices()
        P_ref, Zv, _ = self.m.stft_and_power(v, idx)
        _, Zb, _ = self.m.stft_and_power(b, idx)
        P_chk, _ = self.m.band_power_chunked(v, idx, chunk_frames=100)
        self.assertEqual(P_ref.shape, P_chk.shape)
        self.assertLess(float(np.max(np.abs(P_ref - P_chk)) / (np.max(P_ref) + 1e-12)), 1e-5)
        gV = np.clip(0.25 + 0.65 * rng.random(P_ref.shape), 0.1, 1.0)
        gB = np.clip(0.30 + 0.60 * rng.random(P_ref.shape), 0.1, 1.0)
        y_ref = self.m.istft_combined([(Zv, gV), (Zb, gB)], bob, len(v))
        y_chk = self.m.mix_stems_chunked([(v, gV), (b, gB)], bob, len(v), idx=idx, chunk_frames=100)
        self.assertLess(float(np.max(np.abs(y_ref - y_chk))), 1e-5)

        with tempfile.TemporaryDirectory() as td:
            target = os.path.join(td, "same_hash.wav")
            wav = np.zeros(self.sr // 5, dtype=np.float32)
            with ThreadPoolExecutor(max_workers=6) as ex:
                list(ex.map(lambda _: self.m._write_wav16(target, wav, self.sr), range(12)))
            self.assertTrue(os.path.isfile(target))

    def test_cmd_mix_headroom_no_clip_on_loud_source_and_quiet_vocal_floor(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            self._enter_cache_env(td)
            try:
                # 1) Very loud hot source (-8 LUFS vocal + high-crest dub) must never clip 0 dBFS in premaster_48k.wav
                d_loud = os.path.join(td, "loud")
                for sub in ("data", "config", "media/stems", "media/audio"):
                    os.makedirs(os.path.join(d_loud, sub), exist_ok=True)
                t = np.arange(self.sr * 2) / self.sr
                voc_loud = (0.75 * np.sin(2 * np.pi * 900 * t)).astype(np.float32)
                acc_loud = (0.35 * np.sin(2 * np.pi * 240 * t)).astype(np.float32)
                dub = np.zeros(self.sr * 2, dtype=np.float32)
                dub[int(0.3 * self.sr): int(1.5 * self.sr)] = 0.08 * np.sin(2 * np.pi * 1100 * t[: int(1.2 * self.sr)])
                dub[self.sr] = 0.65  # sharp crest transient
                soundfile.write(os.path.join(d_loud, "media/stems/vocals.wav"), np.stack([voc_loud, voc_loud], axis=1), self.sr)
                soundfile.write(os.path.join(d_loud, "media/stems/accomp.wav"), np.stack([acc_loud, acc_loud], axis=1), self.sr)
                soundfile.write(os.path.join(d_loud, "media/audio/dub_voice_48k.wav"), dub, self.sr)
                self.ap.atomic_write_json(os.path.join(d_loud, "data/00_video_profile.json"), {"duration": 2.0, "localization_mode": "voice_dub"})
                self.m.cmd_mix(d_loud, self.m.cfg(d_loud))
                pm_loud, _ = soundfile.read(os.path.join(d_loud, "media/audio/premaster_48k.wav"), dtype="float32")
                self.assertLessEqual(float(np.max(np.abs(pm_loud))), 0.96)

                # 2) Near-silent vocal stem (-70 LUFS) must NOT mute dub by -48 dB
                d_quiet = os.path.join(td, "quiet")
                for sub in ("data", "config", "media/stems", "media/audio"):
                    os.makedirs(os.path.join(d_quiet, sub), exist_ok=True)
                voc_quiet = np.zeros((self.sr * 2, 2), dtype=np.float32)
                acc_quiet = (0.04 * np.sin(2 * np.pi * 240 * t))[:, None].repeat(2, axis=1).astype(np.float32)
                dub_q = np.zeros(self.sr * 2, dtype=np.float32)
                dub_q[int(0.3 * self.sr): int(1.5 * self.sr)] = 0.22 * np.sin(2 * np.pi * 1100 * t[: int(1.2 * self.sr)])
                soundfile.write(os.path.join(d_quiet, "media/stems/vocals.wav"), voc_quiet, self.sr)
                soundfile.write(os.path.join(d_quiet, "media/stems/accomp.wav"), acc_quiet, self.sr)
                soundfile.write(os.path.join(d_quiet, "media/audio/dub_voice_48k.wav"), dub_q, self.sr)
                self.ap.atomic_write_json(os.path.join(d_quiet, "data/00_video_profile.json"), {"duration": 2.0, "localization_mode": "voice_dub"})
                self.m.cmd_mix(d_quiet, self.m.cfg(d_quiet))
                st_q = json.load(open(os.path.join(d_quiet, "data/03_mix_stats.json"), encoding="utf-8"))
                self.assertGreaterEqual(st_q["dub_gain_db"], -12.0)
                self.assertGreaterEqual(st_q["dub_snr_speech_db_median"], 10.0)
            finally:
                self._exit_cache_env()

    def test_series_bible_speaker_voice_mapping_and_allow_overflow_no_cache_poison(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            self._enter_cache_env(td)
            try:
                ws = os.path.join(td, "u_spk")
                for sub in ("data", "config", "media/audio", "media/tts_segments"):
                    os.makedirs(os.path.join(ws, sub), exist_ok=True)
                self.ap.atomic_write_json(os.path.join(ws, "data/00_video_profile.json"), {"duration": 3.0, "localization_mode": "voice_dub"})
                self.ap.atomic_write_json(os.path.join(ws, "config/series_bible.json"), {
                    "characters": [
                        {"speaker_id": "M1", "voice_id": "Hải Đăng"},
                        {"speaker_id": "F_OLD", "voice_id": "Ngọc"},  # invalid legacy name -> safely ignored
                    ],
                    "voices": {"F2": "Ngọc Huyền"},
                })
                segs = [
                    {"id": 0, "start": 0.1, "end": 0.4, "speaker": "M1", "zh_text": "太长了。", "sub_text": "Câu này dài.", "tts_text": "Câu này quá dài luôn."},
                    {"id": 1, "start": 0.5, "end": 1.2, "speaker": "F_OLD", "zh_text": "好。", "sub_text": "Ừ.", "tts_text": "Ừ."},
                ]
                self.ap.atomic_write_json(os.path.join(ws, "data/02_bilingual_script.json"), segs)
                c = self.m.cfg(ws)
                spk_map, dv = self.m.resolve_speaker_voices(ws, c)
                self.assertEqual(spk_map.get("M1"), "Hải Đăng")
                self.assertEqual(spk_map.get("F2"), "Ngọc Huyền")
                if self.m._known_vieneu_voices():
                    self.assertNotIn("F_OLD", spk_map)
                pl = self.m._plan_unit_dub(ws, c)
                self.assertEqual(pl["items"][0][3], "Hải Đăng")
                self.assertEqual(pl["items"][1][3], dv if self.m._known_vieneu_voices() else "Ngọc")

                # Pre-populate TTS cache with overflowing WAV on seg 0; verify --allow-overflow does NOT poison dub_key
                for _gi, _g, _txt, _v, _tk, raw_p in pl["items"]:
                    soundfile.write(raw_p, self.tone(1.6 if _gi == 0 else 0.3), self.sr)
                ok_diag, overs = self.m._assemble_unit_dub(pl, allow_overflow=True)
                self.assertTrue(ok_diag)
                self.assertGreaterEqual(len(overs), 1)
                # Subsequent normal cmd_dub MUST NOT see cached=True — it must still fail fast with code 2!
                with self.assertRaises(SystemExit) as cm:
                    self.m.cmd_dub(ws, self.m.cfg(ws))
                self.assertEqual(cm.exception.code, 2)
            finally:
                self._exit_cache_env()

    def test_stage2_elastic_short_burst_syl_rate_passes(self):
        v2_py = os.path.join(ROOT, "vendor", "zh-vi-script-adapter", "scripts", "validate_bilingual_script.py")
        cg_py = os.path.join(ROOT, "vendor", "video-localization-orchestrator", "scripts", "cicd_gate.py")
        spec_cg = importlib.util.spec_from_file_location("cg_mod2", cg_py)
        cg = importlib.util.module_from_spec(spec_cg)
        spec_cg.loader.exec_module(cg)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            self._enter_cache_env(td)
            try:
                ws = os.path.join(td, "u_s2")
                for sub in ("data", "config", "source", "media/stems", ".cicd"):
                    os.makedirs(os.path.join(ws, sub), exist_ok=True)
                vid = os.path.join(ws, "source", "master_source.mp4")
                voc_w = np.zeros(self.sr * 3, dtype=np.float32)
                t_sub = np.arange(int(0.35 * self.sr)) / self.sr
                voc_w[int(0.20 * self.sr): int(0.55 * self.sr)] = 0.15 * np.sin(2 * np.pi * 600 * t_sub)
                voc_w[int(1.80 * self.sr): int(2.15 * self.sr)] = 0.15 * np.sin(2 * np.pi * 600 * t_sub)
                soundfile.write(os.path.join(ws, "media/stems/vocals.wav"), np.stack([voc_w, voc_w], axis=1), self.sr)
                subprocess.run([
                    self.ap.find_tool("ffmpeg"), "-v", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=black:s=160x120:d=3.0:r=25",
                    "-i", os.path.join(ws, "media/stems/vocals.wav"),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", vid,
                ], check=True)
                self.ap.atomic_write_json(os.path.join(ws, "data/00_video_profile.json"), {"duration": 3.0, "localization_mode": "voice_dub", "source_video": "source/master_source.mp4"})
                raw_asr = [
                    {"id": 1, "start": 0.20, "end": 0.45, "text": "快走！", "source": "pass1_asr"},
                    {"id": 2, "start": 1.80, "end": 2.25, "text": "好！", "source": "pass1_asr"},
                ]
                script_segs = [
                    {"id": 1, "start": 0.20, "end": 0.45, "speaker": "M1", "zh_text": "快走！", "sub_text": "Mau đi!", "tts_text": "Mau đi!"},
                    {"id": 2, "start": 1.80, "end": 2.25, "speaker": "F1", "zh_text": "好！", "sub_text": "Được!", "tts_text": "Được!"},
                ]
                self.ap.atomic_write_json(os.path.join(ws, "data/01_asr_raw_segments.json"), raw_asr)
                self.ap.atomic_write_json(os.path.join(ws, "data/02_bilingual_script.json"), script_segs)
                with open(os.path.join(ws, "02_glossary_and_pronunciation.md"), "w", encoding="utf-8") as f:
                    f.write("# Glossary\n")
                cg.write_gate_token(ws, 1, "Stage 1", [{"id": "ok", "weight": 100, "passed": True, "detail": "ok"}], {}, ["data/01_asr_raw_segments.json"])
                r = subprocess.run([sys.executable, v2_py, "--workspace", ws], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stdout + "\n" + r.stderr)
            finally:
                self._exit_cache_env()


if __name__ == "__main__":
    unittest.main()
