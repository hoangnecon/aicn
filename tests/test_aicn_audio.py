"""Tests for scripts/aicn_audio.py pure parts. Needs numpy, scipy, soundfile (run with $AICN_PY).

Run:  /Users/Shared/Tools/VieNeu-TTS/.venv/bin/python -m unittest discover -s tests -v
"""
import importlib.util
import os
import shutil
import sys
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


if __name__ == "__main__":
    unittest.main()
