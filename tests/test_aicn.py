"""Unit, concurrency, and relocation tests for scripts/aicn.py & orchestrator/QA v3.0.0.

Run:  python -m unittest discover -s tests -v
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "scripts", "aicn.py")
spec = importlib.util.spec_from_file_location("aicn", SCRIPT)
aicn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aicn)


class HashStability(unittest.TestCase):
    def test_hash_is_order_independent_and_stable(self):
        self.assertEqual(aicn.h_obj({"a": 1, "b": [1, 2]}), aicn.h_obj({"b": [1, 2], "a": 1}))
        self.assertNotEqual(aicn.h_obj({"a": 1}), aicn.h_obj({"a": 2}))


class ContentKeys(unittest.TestCase):
    def test_tts_key_depends_on_text_and_voice_only(self):
        self.assertEqual(
            aicn.ap.tts_sent_key("Xin chào", "Trúc Ly"),
            aicn.ap.tts_sent_key("Xin chào", "Trúc Ly"),
        )
        self.assertNotEqual(
            aicn.ap.tts_sent_key("Xin chào", "Trúc Ly"),
            aicn.ap.tts_sent_key("Xin chào", "Hải Đăng"),
        )


class EncodeAndKey(unittest.TestCase):
    def test_resolve_encode_presets_and_quality_override(self):
        # macOS VideoToolbox
        vt_h264 = aicn.resolve_encode({}, backend="videotoolbox")
        self.assertEqual(vt_h264["vcodec"], "h264_videotoolbox")
        self.assertEqual(vt_h264["q"], 58)
        self.assertTrue(vt_h264["hwdec"])

        vt_hevc = aicn.resolve_encode({}, codec="hevc", backend="videotoolbox")
        self.assertEqual(vt_hevc["vcodec"], "hevc_videotoolbox")
        self.assertEqual(vt_hevc["tag"], "hvc1")
        self.assertEqual(vt_hevc["q"], 60)

        vt_hi = aicn.resolve_encode({}, codec="h264_high", backend="videotoolbox")
        self.assertEqual(vt_hi["bitrate"], "5000k")
        self.assertNotIn("q", vt_hi)

        vt_over = aicn.resolve_encode({}, codec="h264_high", quality=55, backend="videotoolbox")
        self.assertEqual(vt_over["q"], 55)
        self.assertNotIn("bitrate", vt_over)

        # NVIDIA NVENC
        nv_h264 = aicn.resolve_encode({}, backend="nvenc")
        self.assertEqual(nv_h264["vcodec"], "h264_nvenc")
        self.assertEqual(nv_h264["cq"], 22)
        self.assertFalse(nv_h264["hwdec"])

        nv_hevc = aicn.resolve_encode({}, codec="hevc", backend="nvenc")
        self.assertEqual(nv_hevc["vcodec"], "hevc_nvenc")
        self.assertEqual(nv_hevc["tag"], "hvc1")
        self.assertEqual(nv_hevc["cq"], 24)

        nv_over = aicn.resolve_encode({}, codec="h264_high", quality=19, backend="nvenc")
        self.assertEqual(nv_over["cq"], 19)
        self.assertNotIn("bitrate", nv_over)

        # CPU libx264 / libx265
        cpu_h264 = aicn.resolve_encode({}, backend="cpu")
        self.assertEqual(cpu_h264["vcodec"], "libx264")
        self.assertEqual(cpu_h264["crf"], 20)
        self.assertFalse(cpu_h264["hwdec"])

        cpu_over = aicn.resolve_encode({}, codec="hevc", quality=21, backend="cpu")
        self.assertEqual(cpu_over["vcodec"], "libx265")
        self.assertEqual(cpu_over["crf"], 21)
        self.assertFalse(cpu_over["hwdec"])

    def test_ff_render_key_and_cached_watermark_strip(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"AICN_CACHE_DIR": os.path.join(d, "gcache")}):
            tl = os.path.join(d, "tl.png")
            tr = os.path.join(d, "tr.png")
            Image.new("RGBA", (120, 48), (220, 20, 40, 255)).save(tl)
            Image.new("RGBA", (140, 32), (240, 200, 60, 255)).save(tr)
            wm = {"tl": tl, "tr": tr, "scale": 1.0, "tl_x": 42, "tl_y": 22, "tr_mx": 42, "tr_y": 58}
            _, k_copy = aicn.ff_render_key(d, aicn.resolve_encode({}, "h264"), None, width=1080, height=1920)
            _, k_h264_wm = aicn.ff_render_key(d, aicn.resolve_encode({}, "h264"), wm, width=1080, height=1920)
            _, k_hevc_wm = aicn.ff_render_key(d, aicn.resolve_encode({}, "hevc"), wm, width=1080, height=1920)
            self.assertNotEqual(k_copy, k_h264_wm)
            self.assertNotEqual(k_h264_wm, k_hevc_wm)

            strip_p, strip_k = aicn.build_cached_watermark_strip(wm, 1080, 1920)
            self.assertTrue(os.path.isfile(strip_p))
            with Image.open(strip_p) as sim:
                self.assertEqual(sim.mode, "RGBA")
                self.assertEqual(sim.size[0], 1080)
                self.assertLess(sim.size[1], 200, "Watermark strip should stay tight top-band, not full 1920px height")
            strip_p2, strip_k2 = aicn.build_cached_watermark_strip(wm, 1080, 1920)
            self.assertEqual((strip_p, strip_k), (strip_p2, strip_k2))


class CLI(unittest.TestCase):
    def run_cli(self, *args, env=None):
        return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True, env=env)

    def test_help_lists_v3_commands_and_removes_tts_sync(self):
        out = self.run_cli("--help").stdout
        self.assertNotIn("tts-sync", out)
        for c in ("doctor", "discover", "init-series", "update-series", "adopt", "plan", "render",
                  "verify-render", "clean", "merge-series", "run-series", "log", "progress", "brand-poster"):
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
            for f in ("CHANGELOG.md", "PROGRESS.md", "config/series_bible.json", "config/rights.json", "deliverables/subtitles"):
                self.assertTrue(os.path.exists(os.path.join(hub, f)), f)
            for dead_d in ("deliverables/16x9", "deliverables/9x16", "deliverables/episodes", ".cache/tts"):
                self.assertFalse(os.path.exists(os.path.join(hub, dead_d)), f"Legacy dir should not exist: {dead_d}")
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

    def test_init_and_update_series_slug_and_titles(self):
        with tempfile.TemporaryDirectory() as d:
            ws_root = os.path.join(d, "workspace")
            r = self.run_cli(
                "init-series", "--workspace-root", ws_root,
                "--series-slug", "bo-cu", "--title-vi", "Bộ Cũ", "--title-zh", "旧片",
                "--total-episodes", "10", "--rights", "own",
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            old_hub = os.path.join(ws_root, "bo-cu")
            bible = aicn.jload(os.path.join(old_hub, "config", "series_bible.json"))
            bible["characters"] = [{"zh": "猫", "vi": "Mèo"}]
            aicn.jdump(os.path.join(old_hub, "config", "series_bible.json"), bible)
            with open(os.path.join(old_hub, "deliverables", "final_bo-cu_full_vi.mp4"), "wb") as f:
                f.write(b"mp4")
            with open(os.path.join(old_hub, "deliverables", "subtitles", "final_bo-cu_full_vi.srt"), "w", encoding="utf-8") as f:
                f.write("1\n00:00:00,000 --> 00:00:01,000\nHi\n")
            with open(os.path.join(old_hub, "posters", "official_poster_bo-cu_zh.jpg"), "wb") as f:
                f.write(b"jpg")

            bad = self.run_cli("update-series", "--hub", old_hub, "--series-slug", "Bad_Slug")
            self.assertNotEqual(bad.returncode, 0)

            u = self.run_cli(
                "update-series", "--hub", old_hub,
                "--series-slug", "bo-moi", "--title-vi", "Bộ Mới VIP", "--title-zh", "新剧名",
            )
            self.assertEqual(u.returncode, 0, u.stderr)
            new_hub = os.path.join(ws_root, "bo-moi")
            self.assertFalse(os.path.exists(old_hub))
            self.assertTrue(os.path.exists(new_hub))
            self.assertTrue(os.path.exists(os.path.join(new_hub, "deliverables", "final_bo-moi_full_vi.mp4")))
            self.assertTrue(os.path.exists(os.path.join(new_hub, "deliverables", "subtitles", "final_bo-moi_full_vi.srt")))
            self.assertTrue(os.path.exists(os.path.join(new_hub, "posters", "official_poster_bo-moi_zh.jpg")))
            nb = aicn.jload(os.path.join(new_hub, "config", "series_bible.json"))
            self.assertEqual(nb["series_slug"], "bo-moi")
            self.assertEqual(nb["title_vi"], "Bộ Mới VIP")
            self.assertEqual(nb["title_zh"], "新剧名")
            self.assertEqual(nb["characters"], [{"zh": "猫", "vi": "Mèo"}])
            nc = aicn.jload(os.path.join(new_hub, "config", "series_config.json"))
            self.assertEqual(nc["title_vi"], "Bộ Mới VIP")
            self.assertEqual(nc["title_zh"], "新剧名")
            self.assertEqual(aicn.jload(os.path.join(new_hub, "config", "rights.json"))["series"], "bo-moi")
            with open(os.path.join(new_hub, "00_series_manifest.md"), "r", encoding="utf-8") as f:
                man = f.read()
            self.assertIn("Bộ Mới VIP", man)
            self.assertIn("新剧名", man)
            self.assertIn("bo-moi", man)
            disc = self.run_cli("discover", "--root", d).stdout
            self.assertIn("title_vi: Bộ Mới VIP", disc)
            self.assertIn("title_zh: 新剧名", disc)
            # Renamed hub fallback when unit project_config still points at old hub dir
            u1 = os.path.join(new_hub, "units", "unit_01")
            os.makedirs(os.path.join(u1, "config"), exist_ok=True)
            aicn.jdump(os.path.join(u1, "config", "project_config.json"), {"slug": "unit_01", "series_hub_dir": old_hub})
            self.assertEqual(os.path.normcase(aicn.resolve_unit_hub(u1)), os.path.normcase(new_hub))

    def test_ffprobe_streams_includes_codec_tag_string(self):
        with tempfile.TemporaryDirectory() as td:
            vid = os.path.join(td, "sample.mp4")
            subprocess.run([
                aicn.ap.find_tool("ffmpeg"), "-v", "error", "-y",
                "-f", "lavfi", "-i", "color=c=black:s=160x120:d=0.5:r=24",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", vid,
            ], check=True)
            streams = aicn.ffprobe_streams(vid)
            vs = next(s for s in streams if s.get("codec_type") == "video")
            self.assertTrue(vs.get("codec_tag_string"), f"codec_tag_string missing from ffprobe_streams: {vs}")

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

    def test_watermarked_render_active_backend_and_verify_passes(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"AICN_CACHE_DIR": os.path.join(td, "gcache")}):
            u = os.path.join(td, "unit_wm")
            bd = os.path.join(td, "brand")
            for d in ("data", "config", "media", "source", ".cache"):
                os.makedirs(os.path.join(u, d), exist_ok=True)
            os.makedirs(bd, exist_ok=True)
            ff = aicn.ap.find_tool("ffmpeg")
            Image.new("RGBA", (64, 32), (220, 20, 40, 220)).save(os.path.join(bd, "13_top_left_logo_large.png"))
            Image.new("RGBA", (64, 32), (240, 200, 60, 220)).save(os.path.join(bd, "15_top_right_vietsub_floating.png"))
            src = os.path.join(u, "source", "master_source.mp4")
            audio = os.path.join(u, "media", "master_audio_48k.wav")
            subprocess.run([
                ff, "-v", "error", "-y",
                "-f", "lavfi", "-i", "color=c=black:s=320x240:d=1.5:r=25",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=1.5",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", src
            ], check=True)
            subprocess.run([
                ff, "-v", "error", "-y",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=1.5",
                "-c:a", "pcm_s16le", audio
            ], check=True)
            aicn.jdump(os.path.join(u, "data", "00_video_profile.json"), {"width": 320, "height": 240, "duration": 1.5, "r_fps": 25.0, "is_vfr": False})
            aicn.jdump(os.path.join(u, "data", "00_episodes_manifest.json"), {"episodes": []})
            aicn.jdump(os.path.join(u, "config", "project_config.json"), {"slug": "unit_wm"})

            # Active hardware backend (including NVENC overlay_cuda when available)
            aicn.cmd_render(type("A", (), {"unit": u, "no_gate": True, "codec": "h264", "quality": None, "brand_dir": bd})())
            stg = os.path.join(u, "media", ".staging_render.mp4")
            self.assertTrue(os.path.exists(stg))
            aicn.cmd_verify_overlay(type("A", (), {"unit": u, "no_gate": True})())
            gate = aicn.jload(os.path.join(u, ".cicd", "stage_4_gate.json"))
            self.assertEqual(gate["status"], "PASS")
            self.assertEqual(gate.get("schema_version"), "3.0")
            self.assertNotIn("workspace", gate)

            # Second plan check sees current cache
            self.assertEqual(aicn.cmd_plan(type("A", (), {"unit": u, "codec": "h264", "quality": None, "brand_dir": bd})(), quiet=True), [])

            # Fallback static CPU overlay path (AICN_NO_CUDA_OVERLAY=1)
            os.remove(stg)
            with mock.patch.dict(os.environ, {"AICN_NO_CUDA_OVERLAY": "1"}):
                aicn.cmd_render(type("A", (), {"unit": u, "no_gate": True, "codec": "h264", "quality": None, "brand_dir": bd})())
            self.assertTrue(os.path.exists(stg))

    def test_non_mod16_watermarked_render_preserves_exact_dimensions(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"AICN_CACHE_DIR": os.path.join(td, "gcache")}):
            u = os.path.join(td, "unit_non16")
            bd = os.path.join(td, "brand")
            for d in ("data", "config", "media", "source", ".cache"):
                os.makedirs(os.path.join(u, d), exist_ok=True)
            os.makedirs(bd, exist_ok=True)
            ff = aicn.ap.find_tool("ffmpeg")
            Image.new("RGBA", (64, 32), (220, 20, 40, 220)).save(os.path.join(bd, "13_top_left_logo_large.png"))
            Image.new("RGBA", (64, 32), (240, 200, 60, 220)).save(os.path.join(bd, "15_top_right_vietsub_floating.png"))
            src = os.path.join(u, "source", "master_source.mp4")
            audio = os.path.join(u, "media", "master_audio_48k.wav")
            # 320x216 matches 1080p mod-16 remainder (216 % 16 == 8)
            subprocess.run([
                ff, "-v", "error", "-y",
                "-f", "lavfi", "-i", "color=c=black:s=320x216:d=1.2:r=25",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=1.2",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", src
            ], check=True)
            subprocess.run([
                ff, "-v", "error", "-y",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=1.2",
                "-c:a", "pcm_s16le", audio
            ], check=True)
            aicn.jdump(os.path.join(u, "data", "00_video_profile.json"), {"width": 320, "height": 216, "duration": 1.2, "r_fps": 25.0, "is_vfr": False})
            aicn.jdump(os.path.join(u, "data", "00_episodes_manifest.json"), {"episodes": []})
            aicn.jdump(os.path.join(u, "config", "project_config.json"), {"slug": "unit_non16"})

            # Active hardware backend (hwupload_cuda + overlay_cuda without -hwaccel cuda padding)
            aicn.cmd_render(type("A", (), {"unit": u, "no_gate": True, "codec": "h264", "quality": None, "brand_dir": bd})())
            stg = os.path.join(u, "media", ".staging_render.mp4")
            sv = next(s for s in aicn.ffprobe_streams(stg) if s.get("codec_type") == "video")
            self.assertEqual((int(sv["width"]), int(sv["height"])), (320, 216))
            aicn.cmd_verify_overlay(type("A", (), {"unit": u, "no_gate": True})())
            gate = aicn.jload(os.path.join(u, ".cicd", "stage_4_gate.json"))
            self.assertEqual(gate["status"], "PASS")

            # Fallback static CPU overlay preserves exact 320x216 too
            os.remove(stg)
            with mock.patch.dict(os.environ, {"AICN_NO_CUDA_OVERLAY": "1"}):
                aicn.cmd_render(type("A", (), {"unit": u, "no_gate": True, "codec": "h264", "quality": None, "brand_dir": bd})())
            sv2 = next(s for s in aicn.ffprobe_streams(stg) if s.get("codec_type") == "video")
            self.assertEqual((int(sv2["width"]), int(sv2["height"])), (320, 216))

            # verify-render must fail if staging dimensions diverge from profile
            aicn.jdump(os.path.join(u, "data", "00_video_profile.json"), {"width": 320, "height": 200, "duration": 1.2, "r_fps": 25.0, "is_vfr": False})
            with self.assertRaises(SystemExit):
                aicn.cmd_verify_overlay(type("A", (), {"unit": u, "no_gate": True})())

    def test_moving_unit_and_hub_preserves_caches_and_gates(self):
        init_path = os.path.join(ROOT, "vendor", "video-localization-orchestrator", "scripts", "init_workspace.py")
        cg = aicn.cicd()
        ff = aicn.ap.find_tool("ffmpeg")
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"AICN_CACHE_DIR": os.path.join(td, "gcache")}):
            old_ws = os.path.join(td, "ws_old")
            vid = os.path.join(td, "ep01.mp4")
            subprocess.run([
                ff, "-v", "error", "-y",
                "-f", "lavfi", "-i", "color=c=black:s=320x240:d=1.5:r=25",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=1.5",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", vid
            ], check=True)
            subprocess.run([
                sys.executable, init_path,
                "--topology", "series_episodic",
                "--workspace-root", old_ws,
                "--series-slug", "bo-goc",
                "--unit-slug", "unit_01",
                "--genre", "short-drama",
                "--video", vid,
            ], check=True, stdout=subprocess.DEVNULL)
            old_hub = os.path.join(old_ws, "bo-goc")
            old_unit = os.path.join(old_hub, "units", "unit_01")
            subprocess.run([
                ff, "-v", "error", "-y",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=1.5",
                "-c:a", "pcm_s16le", os.path.join(old_unit, "media", "master_audio_48k.wav")
            ], check=True)
            aicn.cmd_adopt(type("A", (), {"unit": old_unit})())
            aicn.cmd_render(type("A", (), {"unit": old_unit, "no_gate": True, "codec": None, "quality": None, "brand_dir": None})())
            aicn.cmd_verify_overlay(type("A", (), {"unit": old_unit, "no_gate": True})())

            old_s1 = aicn.stage1_key(old_unit)
            _, old_rk = aicn.ff_render_key(old_unit, aicn.resolve_encode({}), None, width=320, height=240)
            st = aicn.jload(os.path.join(old_unit, ".cache", "build_state.json"))
            st["stage1_key"] = old_s1
            aicn.jdump(os.path.join(old_unit, ".cache", "build_state.json"), st)

            # Relocate and rename the entire hub directory!
            new_hub = os.path.join(td, "ws_new", "bo-da-doi-ten")
            os.makedirs(os.path.dirname(new_hub), exist_ok=True)
            shutil.move(old_hub, new_hub)
            new_unit = os.path.join(new_hub, "units", "unit_01")

            cg.require_gate(new_unit, 0)
            cg.require_gate(new_unit, 4)
            self.assertEqual(aicn.stage1_key(new_unit), old_s1)
            _, new_rk = aicn.ff_render_key(new_unit, aicn.resolve_encode({}), None, width=320, height=240)
            self.assertEqual(new_rk, old_rk)
            self.assertEqual(aicn.cmd_plan(type("A", (), {"unit": new_unit, "codec": None, "quality": None, "brand_dir": None})(), quiet=True), [])
            self.assertEqual(os.path.normcase(aicn.resolve_unit_hub(new_unit)), os.path.normcase(new_hub))

    def test_init_workspace_rejects_sub_only_and_uses_relative_posix_paths(self):
        init_path = os.path.join(ROOT, "vendor", "video-localization-orchestrator", "scripts", "init_workspace.py")
        ff = aicn.ap.find_tool("ffmpeg")
        with tempfile.TemporaryDirectory() as td:
            vid = os.path.join(td, "ep01.mp4")
            subprocess.run([
                ff, "-v", "error", "-y",
                "-f", "lavfi", "-i", "color=c=black:s=720x1280:d=1.5:r=25",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=1.5",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", vid
            ], check=True)
            bad = subprocess.run([
                sys.executable, init_path, "--workspace-root", os.path.join(td, "ws"),
                "--slug", "u_sub", "--video", vid, "--mode", "sub_only",
            ], capture_output=True, text=True)
            self.assertNotEqual(bad.returncode, 0)
            self.assertIn("sub_only", bad.stderr)

            ws_root = os.path.join(td, "ws")
            subprocess.run([
                sys.executable, init_path, "--topology", "series_episodic",
                "--workspace-root", ws_root, "--series-slug", "phim-hay", "--unit-slug", "unit_01",
                "--genre", "short-drama", "--mode", "auto", "--video", vid,
            ], check=True, stdout=subprocess.DEVNULL)
            u = os.path.join(ws_root, "phim-hay", "units", "unit_01")
            prof = aicn.jload(os.path.join(u, "data", "00_video_profile.json"))
            self.assertEqual(prof["localization_mode"], "voice_dub")
            self.assertEqual(prof["source_video"], "source/master_source.mp4")
            ep_man = aicn.jload(os.path.join(u, "data", "00_episodes_manifest.json"))
            self.assertEqual(ep_man["episodes"][0]["source_file"], "source/episodes/ep01.mp4")
            cfg = aicn.jload(os.path.join(u, "config", "project_config.json"))
            self.assertEqual(cfg.get("series_hub_rel"), "../..")
            self.assertNotIn("bbox_scan", cfg["paths"])
            self.assertNotIn("deliverable_16x9", cfg["paths"])
            wm_cfg = cfg["watermark"]
            for wk in ("brand_dir", "top_left_logo", "top_right_badge"):
                self.assertFalse(os.path.isabs(wm_cfg[wk]), f"{wk} must be relative POSIX path, got {wm_cfg[wk]}")
                self.assertNotIn("\\", wm_cfg[wk])
            for dead in ("deliverables", "frames/qa_9x16", "media/demucs"):
                self.assertFalse(os.path.exists(os.path.join(u, dead)), f"Unexpected legacy dir: {dead}")

    def test_cached_watermark_strip_clips_oversized_logos(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"AICN_CACHE_DIR": os.path.join(td, "gcache")}):
            bd = os.path.join(td, "brand")
            os.makedirs(bd, exist_ok=True)
            Image.new("RGBA", (600, 180), (220, 20, 40, 255)).save(os.path.join(bd, "13_top_left_logo_large.png"))
            Image.new("RGBA", (500, 140), (240, 200, 60, 255)).save(os.path.join(bd, "15_top_right_vietsub_floating.png"))
            wm = {
                "tl": os.path.join(bd, "13_top_left_logo_large.png"),
                "tr": os.path.join(bd, "15_top_right_vietsub_floating.png"),
                "tl_x": 42, "tl_y": 22, "tr_mx": 42, "tr_y": 58, "scale": 1.0,
            }
            strip_path, _ = aicn.build_cached_watermark_strip(wm, 320, 180)
            self.assertTrue(os.path.isfile(strip_path))
            with Image.open(strip_path) as out_im:
                self.assertEqual(out_im.width, 320)
                self.assertLessEqual(out_im.height, 180)

    def test_run_series_dry_run_invokes_warm_batch_clis(self):
        cg = aicn.cicd()
        with tempfile.TemporaryDirectory() as td:
            hub = os.path.join(td, "workspace", "warm-series")
            subprocess.run([
                sys.executable, SCRIPT, "init-series",
                "--workspace-root", os.path.join(td, "workspace"),
                "--series-slug", "warm-series", "--rights", "own",
            ], check=True, stdout=subprocess.DEVNULL)
            for name in ("unit_01", "unit_02"):
                u = os.path.join(hub, "units", name)
                for d in ("config", "data", ".cicd", "source", "media"):
                    os.makedirs(os.path.join(u, d), exist_ok=True)
                aicn.jdump(os.path.join(u, "config", "project_config.json"), {"slug": name})
                aicn.jdump(os.path.join(u, "data", "00_video_profile.json"), {"duration": 10.0})
                cg.write_gate_token(u, 0, "s0", [{"id": "ok", "weight": 100, "passed": True}], {}, [])

            r_prep = self.run_cli("run-series", "--hub", hub, "--phase", "prep", "--dry-run")
            self.assertEqual(r_prep.returncode, 0, r_prep.stderr)
            self.assertIn("batch-separate(2)", r_prep.stdout)
            self.assertIn("batch-asr(2)", r_prep.stdout)

            for name in ("unit_01", "unit_02"):
                u = os.path.join(hub, "units", name)
                cg.write_gate_token(u, 1, "s1", [{"id": "ok", "weight": 100, "passed": True}], {}, [])
                cg.write_gate_token(u, 2, "s2", [{"id": "ok", "weight": 100, "passed": True}], {}, [])
            r_prod = self.run_cli("run-series", "--hub", hub, "--phase", "produce", "--dry-run")
            self.assertEqual(r_prod.returncode, 0, r_prod.stderr)
            self.assertIn("batch-all(2)", r_prod.stdout)
            self.assertIn("unit_01:render", r_prod.stdout)

    def test_retired_qa_scripts_deleted_and_stage5_promotes_unit_only_and_merges(self):
        qa_dir = os.path.join(ROOT, "vendor", "video-localization-qa-review", "scripts")
        for dead_py in ("apply_channel_branding.py", "render_vertical_9x16.py", "split_compiled_episodes.py"):
            self.assertFalse(os.path.exists(os.path.join(qa_dir, dead_py)), f"Retired script still present: {dead_py}")

        cg = aicn.cicd()
        ff = aicn.ap.find_tool("ffmpeg")
        comp_py = os.path.join(qa_dir, "composite_and_verify.py")
        ver5_py = os.path.join(qa_dir, "verify_stage_5.py")
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"AICN_CACHE_DIR": os.path.join(td, "gcache")}):
            hub = os.path.join(td, "workspace", "merge-demo")
            subprocess.run([
                sys.executable, SCRIPT, "init-series",
                "--workspace-root", os.path.join(td, "workspace"),
                "--series-slug", "merge-demo", "--rights", "own",
            ], check=True, stdout=subprocess.DEVNULL)

            for idx, uname in enumerate(("unit_01", "unit_02"), start=1):
                u = os.path.join(hub, "units", uname)
                for d in ("config", "data", ".cicd", "source", "media"):
                    os.makedirs(os.path.join(u, d), exist_ok=True)
                src = os.path.join(u, "source", "master_source.mp4")
                aud = os.path.join(u, "media", "master_audio_48k.wav")
                subprocess.run([
                    ff, "-v", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=black:s=320x240:d=2.2:r=25",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=2.2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", src
                ], check=True)
                subprocess.run([
                    ff, "-v", "error", "-y",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=2.2",
                    "-c:a", "pcm_s16le", aud
                ], check=True)
                aicn.jdump(os.path.join(u, "config", "project_config.json"), {"slug": uname, "series_hub_rel": "../.."})
                aicn.jdump(os.path.join(u, "data", "00_video_profile.json"), {"slug": uname, "width": 320, "height": 240, "duration": 2.2, "r_fps": 25.0, "is_vfr": False})
                aicn.jdump(os.path.join(u, "data", "00_episodes_manifest.json"), {"episodes": [{"ep_id": f"ep0{idx}"}]})
                with open(os.path.join(u, "data", "subtitles_full.srt"), "w", encoding="utf-8") as sf:
                    sf.write(f"1\n00:00:00,200 --> 00:00:01,500\nCâu số {idx}\n\n")
                for st_i in range(4):
                    cg.write_gate_token(u, st_i, f"s{st_i}", [{"id": "ok", "weight": 100, "passed": True}], {}, [])
                aicn.cmd_render(type("A", (), {"unit": u, "no_gate": False, "codec": None, "quality": None, "brand_dir": None})())
                aicn.cmd_verify_overlay(type("A", (), {"unit": u, "no_gate": False})())

                ts = ",".join(str(round(0.2 + i * 0.18, 2)) for i in range(10))
                subprocess.run([
                    sys.executable, comp_py, "--workspace", u, "--slug", uname, "--qa-timestamps", ts
                ], check=True, stdout=subprocess.DEVNULL)
                qaman = aicn.jload(os.path.join(u, "data", "04_qa_frames_manifest.json"))
                with open(os.path.join(u, "04_qa_review_report.md"), "w", encoding="utf-8") as rf:
                    for fr in qaman["frames"]:
                        rf.write(f"| {fr['timestamp']} | {fr['filename']} | PASS |\n")
                subprocess.run([sys.executable, ver5_py, "--workspace", u], check=True, stdout=subprocess.DEVNULL)

                self.assertTrue(os.path.isfile(os.path.join(u, "media", f"final_{uname}_vi.mp4")))
                self.assertFalse(os.path.exists(os.path.join(u, "deliverables", "16x9")))
                self.assertFalse(os.path.exists(os.path.join(hub, "deliverables", "16x9")))

            rm = self.run_cli("merge-series", "--hub", hub)
            self.assertEqual(rm.returncode, 0, rm.stderr)
            self.assertTrue(os.path.isfile(os.path.join(hub, "deliverables", "final_merge-demo_full_vi.mp4")))
            full_srt = os.path.join(hub, "deliverables", "subtitles", "final_merge-demo_full_vi.srt")
            self.assertTrue(os.path.isfile(full_srt))
            with open(full_srt, "r", encoding="utf-8") as sf:
                srt_txt = sf.read()
            self.assertIn("Câu số 1", srt_txt)
            self.assertIn("Câu số 2", srt_txt)

    def test_installers_exclude_spec_and_prune_retired_scripts(self):
        with open(os.path.join(ROOT, "install.ps1"), encoding="utf-8") as fh:
            ps1 = fh.read()
        with open(os.path.join(ROOT, "install.sh"), encoding="utf-8") as fh:
            sh = fh.read()
        for txt in (ps1, sh):
            self.assertIn("SPEC_V3.md", txt)
            for retired in (
                "apply_channel_branding.py",
                "render_vertical_9x16.py",
                "split_compiled_episodes.py",
                "assemble_master_audio.py",
                "synthesize_and_audit_pacing.py",
            ):
                self.assertIn(retired, txt)
        self.assertIn("retired legacy file still present", ps1)

    def test_doctor_writes_machine_json(self):
        with tempfile.TemporaryDirectory() as td:
            r = self.run_cli("doctor", "--root", td)
            self.assertEqual(r.returncode, 0, r.stderr)
            m = aicn.jload(os.path.join(td, "config", "machine.json"))
            self.assertIsInstance(m, dict)
            for key in ("gpu", "ffmpeg", "tools", "choose"):
                self.assertIn(key, m)
            self.assertIn("backend", m["gpu"])
            self.assertIn("backend", m["ffmpeg"])
            self.assertIn("asr", m["choose"])


    def test_brand_poster_renames_resizes_and_brands_9x16_and_16x9(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("PIL not installed in current test python")
        with tempfile.TemporaryDirectory() as td:
            hub = os.path.join(td, "my-test-series")
            os.makedirs(os.path.join(hub, "config"))
            os.makedirs(os.path.join(hub, "posters"))
            aicn.jdump(os.path.join(hub, "config", "series_config.json"), {"series_slug": "my-test-series"})
            bd = os.path.join(td, "brand")
            os.makedirs(bd)
            Image.new("RGBA", (284, 142), (220, 20, 40, 255)).save(os.path.join(bd, "13_top_left_logo_large.png"))
            Image.new("RGBA", (295, 70), (240, 200, 60, 255)).save(os.path.join(bd, "15_top_right_vietsub_floating.png"))

            # 9:16 raw poster with messy Flow filename + --delete-source + --remove-star
            raw_in = os.path.join(hub, "posters", "Editing_Vietnamese_anime_poster_abc123.jpg")
            Image.new("RGB", (768, 1376), (30, 110, 210)).save(raw_in, "JPEG")
            res = aicn.cmd_brand_poster(type("A", (), {
                "hub": hub, "out_dir": None, "slug": None, "input": raw_in,
                "brand_dir": bd, "aspect": "auto", "remove_star": True, "delete_source": True,
            })())
            self.assertFalse(os.path.exists(raw_in))
            self.assertEqual(res["aspect"], "9x16")
            self.assertTrue(os.path.isfile(os.path.join(hub, "posters", "poster_my-test-series_vi_9x16_raw.jpg")))
            with Image.open(os.path.join(hub, "posters", "poster_my-test-series_vi_9x16_clean.png")) as clean_im:
                self.assertEqual(clean_im.size, (1080, 1920))
            with Image.open(os.path.join(hub, "posters", "poster_my-test-series_vi_9x16_branded.png")) as branded_im:
                self.assertEqual(branded_im.size, (1080, 1920))
                # Verify top-left red logo and top-right gold badge were composited
                self.assertGreater(branded_im.getpixel((100, 100))[0], 180)
                self.assertGreater(branded_im.getpixel((900, 110))[1], 160)

            # 16:9 widescreen thumbnail
            raw_16 = os.path.join(td, "thumb_wide.png")
            Image.new("RGB", (1376, 768), (20, 40, 80)).save(raw_16, "PNG")
            res16 = aicn.cmd_brand_poster(type("A", (), {
                "hub": hub, "out_dir": None, "slug": None, "input": raw_16,
                "brand_dir": bd, "aspect": "auto", "remove_star": False, "delete_source": False,
            })())
            self.assertEqual(res16["aspect"], "16x9")
            with Image.open(res16["branded"]) as b16:
                self.assertEqual(b16.size, (1920, 1080))

    def test_chained_cicd_gates_invalidate_downstream_on_edit_and_preserve_deep_clean(self):
        cg = aicn.cicd()
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"AICN_CACHE_DIR": os.path.join(td, "gcache")}):
            ws = os.path.join(td, "unit_chain")
            for sub in ("data", "media", ".cicd"):
                os.makedirs(os.path.join(ws, sub), exist_ok=True)
            arts = [
                "data/a0.json",
                "data/a1.json",
                "data/02_bilingual_script.json",
                "media/master_audio_48k.wav",
                "media/.staging_render.mp4",
                "media/final_unit_chain_vi.mp4",
            ]
            for idx, rel in enumerate(arts):
                with open(os.path.join(ws, rel), "wb") as f:
                    f.write(f"stage-{idx}-v1".encode("utf-8"))
                cg.write_gate_token(
                    ws, idx, f"Stage {idx}",
                    [{"id": "ok", "weight": 100, "passed": True, "detail": "ok"}],
                    {}, [rel],
                )

            self.assertEqual(aicn.gate_status(ws), "PPPPPP")
            cg.require_gate(ws, 5)

            # 1) Editing Stage 2 script in-place invalidates Stages 2..5 in gate_status AND blocks require_gate(4)
            with open(os.path.join(ws, "data/02_bilingual_script.json"), "wb") as f:
                f.write(b"stage-2-v2-edited")
            self.assertEqual(aicn.gate_status(ws), "PPxxxx")
            with self.assertRaises(SystemExit) as cm1:
                cg.require_gate(ws, 4)
            self.assertEqual(cm1.exception.code, 2)

            # 2) Re-signing Stage 2 alone leaves Stages 3..5 stale via upstream_signature mismatch
            cg.write_gate_token(
                ws, 2, "Stage 2",
                [{"id": "ok", "weight": 100, "passed": True, "detail": "ok"}],
                {}, ["data/02_bilingual_script.json"],
            )
            self.assertEqual(aicn.gate_status(ws), "PPPxxx")
            with self.assertRaises(SystemExit) as cm2:
                cg.require_gate(ws, 4)
            self.assertEqual(cm2.exception.code, 2)

            # 3) Re-signing Stages 3, 4, 5 restores PPPPPP; deep-clean deleting Stage 3/4 media keeps Stage 5 P
            for idx in (3, 4, 5):
                cg.write_gate_token(
                    ws, idx, f"Stage {idx}",
                    [{"id": "ok", "weight": 100, "passed": True, "detail": "ok"}],
                    {}, [arts[idx]],
                )
            self.assertEqual(aicn.gate_status(ws), "PPPPPP")
            os.remove(os.path.join(ws, "media/master_audio_48k.wav"))
            os.remove(os.path.join(ws, "media/.staging_render.mp4"))
            self.assertEqual(aicn.gate_status(ws), "PPPxxP")

    def test_install_sh_self_guard_and_extract_audit_frames_collision_safety(self):
        sh_txt = open(os.path.join(ROOT, "install.sh"), encoding="utf-8").read()
        self.assertIn('if [ "$src_real" = "$dst_real" ]; then', sh_txt)
        self.assertLess(sh_txt.index('if [ "$src_real" = "$dst_real" ]; then'), sh_txt.index('copy_clean_tree "$src" "$dst"'))

        cg = aicn.cicd()
        ext_py = os.path.join(ROOT, "vendor", "video-asr-visual-audit", "scripts", "extract_audit_frames.py")
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"AICN_CACHE_DIR": os.path.join(td, "gcache")}):
            ws = os.path.join(td, "u_frames")
            for sub in ("source", "data", ".cicd"):
                os.makedirs(os.path.join(ws, sub), exist_ok=True)
            vid = os.path.join(ws, "source", "master_source.mp4")
            subprocess.run([
                aicn.ap.find_tool("ffmpeg"), "-v", "error", "-y",
                "-f", "lavfi", "-i", "testsrc=s=160x120:d=3.0:r=25",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", vid,
            ], check=True)
            aicn.jdump(os.path.join(ws, "data", "00_video_profile.json"), {"duration": 3.0, "source_video": "source/master_source.mp4"})
            cg.write_gate_token(ws, 0, "Stage 0", [{"id": "ok", "weight": 100, "passed": True, "detail": "ok"}], {}, ["data/00_video_profile.json"])
            subprocess.run([sys.executable, ext_py, "--workspace", ws, "--timestamps", "0.6,1.1,1.4"], check=True)
            man = aicn.jload(os.path.join(ws, "data", "01_extracted_audit_manifest.json"))
            self.assertEqual(man["num_frames"], 3)
            fnames = [it["filename"] for it in man["frames"]]
            self.assertEqual(len(set(fnames)), 3, f"Colliding filenames generated: {fnames}")
            for it in man["frames"]:
                fp = os.path.join(ws, it["rel_path"])
                self.assertEqual(aicn.ap.content_sha256(fp), it["sha256"])


if __name__ == "__main__":
    unittest.main()
