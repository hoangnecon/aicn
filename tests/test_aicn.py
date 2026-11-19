"""Unit tests for the pure parts of scripts/aicn.py (no ffmpeg, no models).

Run:  python3 -m unittest discover -s tests -v
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "aicn.py")
spec = importlib.util.spec_from_file_location("aicn", SCRIPT)
aicn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aicn)

STYLE = {"max_snap_s": 1.0, "display_fps": 30.0}


def cue(i, a, b, text="x"):
    return {"id": i, "slot_start": a, "slot_end": b, "sub_text": text}


class DisplayWindows(unittest.TestCase):
    def test_audio_slots_never_change(self):
        cues = [cue(0, 1.2, 2.0), cue(1, 3.0, 4.0)]
        aicn.apply_display_windows(cues, [{"start": 1.0, "end": 2.6}], STYLE)
        self.assertEqual((cues[0]["slot_start"], cues[0]["slot_end"]), (1.2, 2.0))

    def test_snap_to_line_start_when_late(self):
        cues = [cue(0, 1.5, 2.0)]
        aicn.apply_display_windows(cues, [{"start": 1.0, "end": 2.5}], STYLE)
        self.assertEqual(cues[0]["show_a"], 1.0)

    def test_no_snap_when_too_late(self):
        cues = [cue(0, 2.5, 3.0)]
        aicn.apply_display_windows(cues, [{"start": 1.0, "end": 3.5}], STYLE)
        self.assertEqual(cues[0]["show_a"], 2.5)

    def test_hold_until_line_ends(self):
        cues = [cue(0, 1.0, 2.0), cue(1, 5.0, 6.0)]
        aicn.apply_display_windows(cues, [{"start": 1.0, "end": 3.0}], STYLE)
        self.assertEqual(cues[0]["show_b"], 3.0)

    def test_hold_stops_at_next_cue(self):
        cues = [cue(0, 1.0, 2.0), cue(1, 2.4, 3.5)]
        aicn.apply_display_windows(cues, [{"start": 1.0, "end": 3.0}], STYLE)
        self.assertLessEqual(cues[0]["show_b"], cues[1]["show_a"])

    def test_no_hold_when_only_brushing_next_line(self):
        # cue ends 0.1 s into a new line: it must not be held across that line
        cues = [cue(0, 1.0, 2.1)]
        aicn.apply_display_windows(cues, [{"start": 2.0, "end": 4.0}], STYLE)
        self.assertEqual(cues[0]["show_b"], 2.1)

    def test_frame_quantized(self):
        cues = [cue(0, 1.0004, 2.0013)]
        aicn.apply_display_windows(cues, [], STYLE)
        for k in ("show_a", "show_b"):
            self.assertAlmostEqual(cues[0][k] * 30, round(cues[0][k] * 30), places=2)


class Chunks(unittest.TestCase):
    def test_episode_boundaries(self):
        eps = [{"ep_id": "ep01", "timeline_start_s": 0, "timeline_end_s": 60},
               {"ep_id": "ep02", "timeline_start_s": 60, "timeline_end_s": 250}]
        w = aicn.chunk_windows({"duration": 250}, eps, 90)
        self.assertEqual(w[0], ("ep01", 0.0, 60.0))
        self.assertEqual([c[0] for c in w[1:]], ["ep02_00", "ep02_01", "ep02_02"])
        self.assertAlmostEqual(w[-1][2], 250.0)

    def test_hash_is_order_independent_and_stable(self):
        self.assertEqual(aicn.h_obj({"a": 1, "b": [1, 2]}), aicn.h_obj({"b": [1, 2], "a": 1}))
        self.assertNotEqual(aicn.h_obj({"a": 1}), aicn.h_obj({"a": 2}))


class TTSKey(unittest.TestCase):
    def test_key_depends_on_text_and_voice_only(self):
        a = {"id": 1, "tts_text": "Xin chào", "slot_start": 1.0}
        b = {"id": 99, "tts_text": "Xin chào", "slot_start": 7.0}
        self.assertEqual(aicn.tts_key(a, "Trúc Ly"), aicn.tts_key(b, "Trúc Ly"))
        self.assertNotEqual(aicn.tts_key(a, "Trúc Ly"), aicn.tts_key(a, "Hải Đăng"))


class CLI(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True)

    def test_help_lists_all_commands(self):
        out = self.run_cli("--help").stdout
        for c in ("discover", "init-series", "adopt", "plan", "tts-sync", "render",
                  "verify-overlay", "log", "progress"):
            self.assertIn(c, out)

    def test_init_series_requires_rights(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.run_cli("init-series", "--workspace-root", os.path.join(d, "workspace"), "--series-slug", "s", "--title", "T",
                             "--total-episodes", "10")
            self.assertNotEqual(r.returncode, 0)

    def test_init_series_then_discover(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.run_cli("init-series", "--workspace-root", os.path.join(d, "workspace"), "--series-slug", "demo", "--title", "Demo",
                             "--total-episodes", "20", "--rights", "own")
            self.assertEqual(r.returncode, 0, r.stderr)
            hub = os.path.join(d, "workspace", "demo")
            for f in ("CHANGELOG.md", "PROGRESS.md", "config/series_bible.json", "config/rights.json"):
                self.assertTrue(os.path.exists(os.path.join(hub, f)), f)
            self.assertIn("demo", self.run_cli("discover", "--root", d).stdout)


if __name__ == "__main__":
    unittest.main()
