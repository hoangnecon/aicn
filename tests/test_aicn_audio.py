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


if __name__ == "__main__":
    unittest.main()
