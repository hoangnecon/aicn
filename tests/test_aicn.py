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
        for c in ("doctor", "discover", "init-series", "update-series", "adopt", "plan", "tts-sync", "render",
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
        with tempfile.TemporaryDirectory() as td:
            u = os.path.join(td, "unit_wm")
            bd = os.path.join(td, "brand")
            for d in ("data", "config", "media", "source", ".cache"):
                os.makedirs(os.path.join(u, d), exist_ok=True)
            os.makedirs(bd, exist_ok=True)
            ff = aicn.ap.find_tool("ffmpeg")
            for name in ("13_top_left_logo_large.png", "15_top_right_vietsub_floating.png"):
                subprocess.run([
                    ff, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red@0.5:s=64x32:d=0.04",
                    "-frames:v", "1", os.path.join(bd, name),
                ], check=True)
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
            aicn.cmd_render(type("A", (), {"unit": u, "no_gate": True, "codec": "h264", "quality": None, "brand_dir": bd})())
            stg = os.path.join(u, "media", ".staging_render.mp4")
            self.assertTrue(os.path.exists(stg))
            aicn.cmd_verify_overlay(type("A", (), {"unit": u, "no_gate": True})())
            gate = aicn.jload(os.path.join(u, ".cicd", "stage_4_gate.json"))
            self.assertEqual(gate["status"], "PASS")

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


if __name__ == "__main__":
    unittest.main()
