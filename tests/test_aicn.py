"""Unit tests for the pure parts of scripts/aicn.py (v2.0.0: dub + watermark + translate only).

Run:  python3 -m unittest discover -s tests -v
"""
import importlib.util
import json
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


class HashStability(unittest.TestCase):
    def test_hash_is_order_independent_and_stable(self):
        self.assertEqual(aicn.h_obj({"a": 1, "b": [1, 2]}), aicn.h_obj({"b": [1, 2], "a": 1}))
        self.assertNotEqual(aicn.h_obj({"a": 1}), aicn.h_obj({"a": 2}))


class TTSKey(unittest.TestCase):
    def test_key_depends_on_text_and_voice_only(self):
        a = {"id": 1, "tts_text": "Xin chào", "slot_start": 1.0}
        b = {"id": 99, "tts_text": "Xin chào", "slot_start": 7.0}
        self.assertEqual(aicn.tts_key(a, "Trúc Ly"), aicn.tts_key(b, "Trúc Ly"))
        self.assertNotEqual(aicn.tts_key(a, "Trúc Ly"), aicn.tts_key(a, "Hải Đăng"))


class EncodeAndKey(unittest.TestCase):
    def test_resolve_encode_presets_and_quality_override(self):
        h264 = aicn.resolve_encode({})
        self.assertEqual(h264["vcodec"], "h264_videotoolbox")
        self.assertEqual(h264["q"], 58)
        self.assertTrue(h264["hwdec"])

        hevc = aicn.resolve_encode({}, codec="hevc")
        self.assertEqual(hevc["vcodec"], "hevc_videotoolbox")
        self.assertEqual(hevc["tag"], "hvc1")
        self.assertEqual(hevc["q"], 60)

        hi = aicn.resolve_encode({}, codec="h264_high")
        self.assertEqual(hi["bitrate"], "5000k")
        self.assertNotIn("q", hi)

        over = aicn.resolve_encode({}, codec="h264_high", quality=55)
        self.assertEqual(over["q"], 55)
        self.assertNotIn("bitrate", over)

    def test_ff_render_key_reacts_to_codec_and_watermark(self):
        with tempfile.TemporaryDirectory() as d:
            fh = aicn.FastHash(d)
            tl = os.path.join(d, "tl.png")
            tr = os.path.join(d, "tr.png")
            for p in (tl, tr):
                with open(p, "wb") as f:
                    f.write(b"fake-png")
            wm = {"tl": tl, "tr": tr, "scale": 1.0, "tl_x": 42, "tl_y": 22, "tr_mx": 42, "tr_y": 58}
            _, k_copy = aicn.ff_render_key(d, fh, aicn.resolve_encode({}, "h264"), None)
            _, k_h264_wm = aicn.ff_render_key(d, fh, aicn.resolve_encode({}, "h264"), wm)
            _, k_hevc_wm = aicn.ff_render_key(d, fh, aicn.resolve_encode({}, "hevc"), wm)
            self.assertNotEqual(k_copy, k_h264_wm)
            self.assertNotEqual(k_h264_wm, k_hevc_wm)


class CLI(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True)

    def test_help_lists_all_commands(self):
        out = self.run_cli("--help").stdout
        for c in ("doctor", "discover", "init-series", "adopt", "plan", "tts-sync", "render",
                  "verify-overlay", "verify-render", "clean", "merge-series", "run-series", "log", "progress"):
            self.assertIn(c, out)

    def test_clean_requires_final_and_keeps_staging_by_default(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "media", "stems"))
            with open(os.path.join(d, "media", "stems", "vocals.wav"), "wb") as f:
                f.write(b"0" * 1024)
            with open(os.path.join(d, "media", ".staging_render.mp4"), "wb") as f:
                f.write(b"0" * 1024)
            r_refuse = self.run_cli("clean", "--unit", d, "--dry-run")
            self.assertNotEqual(r_refuse.returncode, 0)
            with open(os.path.join(d, "media", "final_u_vi.mp4"), "wb") as f:
                f.write(b"0" * 1024)
            r_dry = self.run_cli("clean", "--unit", d, "--dry-run")
            self.assertEqual(r_dry.returncode, 0, r_dry.stderr)
            self.assertIn("media/stems", r_dry.stdout)
            self.assertNotIn(".staging_render.mp4", r_dry.stdout)
            r_deep = self.run_cli("clean", "--unit", d, "--dry-run", "--deep")
            self.assertEqual(r_deep.returncode, 0, r_deep.stderr)
            self.assertIn(".staging_render.mp4", r_deep.stdout)

    def test_init_series_requires_rights(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.run_cli("init-series", "--workspace-root", os.path.join(d, "workspace"), "--series-slug", "s", "--title", "T",
                             "--total-episodes", "10")
            self.assertNotEqual(r.returncode, 0)

    def test_init_series_enforces_vietnamese_hyphen_slug(self):
        with tempfile.TemporaryDirectory() as d:
            for bad in ("na_nian_shi_ba", "Nam-18-Tuoi", "năm-18-tuổi", "-bad", "bad--slug"):
                r = self.run_cli("init-series", "--workspace-root", os.path.join(d, "workspace"),
                                 "--series-slug", bad, "--title", "T", "--total-episodes", "10", "--rights", "own")
                self.assertNotEqual(r.returncode, 0, f"should reject {bad}")

    def test_init_series_then_discover(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.run_cli("init-series", "--workspace-root", os.path.join(d, "workspace"), "--series-slug", "demo", "--title", "Demo",
                             "--total-episodes", "20", "--rights", "own")
            self.assertEqual(r.returncode, 0, r.stderr)
            hub = os.path.join(d, "workspace", "demo")
            for f in ("CHANGELOG.md", "PROGRESS.md", "config/series_bible.json", "config/rights.json"):
                self.assertTrue(os.path.exists(os.path.join(hub, f)), f)
            disc = self.run_cli("discover", "--root", d).stdout
            self.assertIn("demo", disc)
            self.assertIn("FULL RELEASE: PENDING", disc)
            m = self.run_cli("merge-series", "--hub", hub)
            self.assertNotEqual(m.returncode, 0)
            b1 = os.path.join(hub, "units", "batch_01")
            os.makedirs(os.path.join(b1, "config"))
            with open(os.path.join(b1, "config", "project_config.json"), "w", encoding="utf-8") as f:
                f.write("{}")
            rs = self.run_cli("run-series", "--hub", hub, "--phase", "produce", "--dry-run")
            self.assertEqual(rs.returncode, 0, rs.stderr)
            self.assertIn("phase=produce", rs.stdout)

    def test_portrait_9x16_watermark_scaling(self):
        with tempfile.TemporaryDirectory() as td:
            bd = os.path.join(td, "brand")
            os.makedirs(bd)
            for name in ("13_top_left_logo_large.png", "15_top_right_vietsub_floating.png"):
                with open(os.path.join(bd, name), "wb") as f:
                    f.write(b"png")
            wm_land = aicn.resolve_watermark(td, brand_dir=bd, width=1920)
            self.assertEqual(wm_land["scale"], 1.0)
            self.assertEqual(wm_land["tl_x"], 42)
            wm_port = aicn.resolve_watermark(td, brand_dir=bd, width=720)
            self.assertIsNotNone(wm_port)
            self.assertEqual(wm_port["scale"], 0.5)
            self.assertEqual(wm_port["tl_x"], 21)

    def test_init_workspace_decoupled_series_bible(self):
        init_path = os.path.join(
            os.path.dirname(HERE), "vendor", "video-localization-orchestrator", "scripts", "init_workspace.py"
        )
        gate_path = os.path.join(
            os.path.dirname(HERE), "vendor", "video-localization-orchestrator", "scripts", "cicd_gate.py"
        )
        gp_mod = importlib.util.spec_from_file_location("cicd_gate", gate_path)
        cg = importlib.util.module_from_spec(gp_mod)
        gp_mod.loader.exec_module(cg)

        with tempfile.TemporaryDirectory() as td:
            vid = os.path.join(td, "ep01.mp4")
            subprocess.run([
                "ffmpeg", "-v", "error", "-y",
                "-f", "lavfi", "-i", "color=c=black:s=720x1280:d=2.0:r=25",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=2.0",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", vid
            ], check=True)
            ws_root = os.path.join(td, "ws")
            subprocess.run([
                sys.executable, init_path,
                "--workspace-root", ws_root,
                "--slug", "unit_01",
                "--video", vid,
            ], check=True, stdout=subprocess.DEVNULL)
            unit_dir = os.path.join(ws_root, "unit_01")
            cg.require_gate(unit_dir, 0)
            with open(os.path.join(unit_dir, "data", "00_video_profile.json"), "r", encoding="utf-8") as f:
                prof = json.load(f)
            self.assertNotIn("strip_y0", prof)
            self.assertNotIn("overlay_style", prof)
            bible_p = os.path.join(unit_dir, "config", "series_bible.json")
            with open(bible_p, "r", encoding="utf-8") as f:
                bible = json.load(f)
            bible["glossary"] = {"主角": "Nhân vật chính"}
            with open(bible_p, "w", encoding="utf-8") as f:
                json.dump(bible, f)
            cg.require_gate(unit_dir, 0)

    def test_verify_overlay_rejects_av_duration_drift(self):
        with tempfile.TemporaryDirectory() as td:
            u = os.path.join(td, "unit_01")
            for d in ("data", "config", "media", "source", ".cache"):
                os.makedirs(os.path.join(u, d), exist_ok=True)
            aicn.jdump(os.path.join(u, "data", "00_video_profile.json"), {"width": 1080, "height": 1920, "duration": 2.0, "r_fps": 25.0, "is_vfr": False})
            aicn.jdump(os.path.join(u, "data", "00_episodes_manifest.json"), {"episodes": []})
            aicn.jdump(os.path.join(u, "data", "03_aligned_subtitles.json"), [])
            aicn.jdump(os.path.join(u, ".cache", "build_state.json"), {"render": {"engine": "ffmpeg"}})
            stg = os.path.join(u, "media", ".staging_render.mp4")
            subprocess.run([
                "ffmpeg", "-v", "error", "-y",
                "-f", "lavfi", "-i", "color=c=black:s=320x240:d=2.0:r=25",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=3.2",
                "-c:v", "libx264", "-c:a", "aac", stg
            ], check=True)
            with self.assertRaises(SystemExit) as ctx:
                aicn.cmd_verify_overlay(type("A", (), {"unit": u, "no_gate": True})())
            self.assertEqual(ctx.exception.code, 2)

    def test_render_stream_copy_without_watermark_and_verify_passes(self):
        with tempfile.TemporaryDirectory() as td:
            u = os.path.join(td, "unit_01")
            for d in ("data", "config", "media", "source", ".cache"):
                os.makedirs(os.path.join(u, d), exist_ok=True)
            src = os.path.join(u, "source", "master_source.mp4")
            audio = os.path.join(u, "media", "master_audio_48k.wav")
            subprocess.run([
                "ffmpeg", "-v", "error", "-y",
                "-f", "lavfi", "-i", "color=c=black:s=320x240:d=1.5:r=25",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=1.5",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", src
            ], check=True)
            subprocess.run([
                "ffmpeg", "-v", "error", "-y",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=1.5",
                "-c:a", "pcm_s16le", audio
            ], check=True)
            aicn.jdump(os.path.join(u, "data", "00_video_profile.json"), {"width": 320, "height": 240, "duration": 1.5, "r_fps": 25.0, "is_vfr": False})
            aicn.jdump(os.path.join(u, "data", "00_episodes_manifest.json"), {"episodes": []})
            aicn.jdump(os.path.join(u, "config", "project_config.json"), {"slug": "unit_01"})
            aicn.cmd_render(type("A", (), {"unit": u, "no_gate": True, "codec": None, "quality": None, "brand_dir": None})())
            stg = os.path.join(u, "media", ".staging_render.mp4")
            self.assertTrue(os.path.exists(stg))
            aicn.cmd_verify_overlay(type("A", (), {"unit": u, "no_gate": True})())
            gate = aicn.jload(os.path.join(u, ".cicd", "stage_4_gate.json"))
            self.assertEqual(gate["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
