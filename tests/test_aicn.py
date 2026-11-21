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



class FfmpegTimeline(unittest.TestCase):
    """build_timeline must cover the whole video exactly and show the right PNG at each time."""
    def test_segments_cover_duration_and_switch_at_events(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow missing")
        prof = {"width": 320, "height": 1080, "duration": 10.0}
        style = dict(aicn.DEFAULT_STYLE, temporal_pad_s=0.0)
        scan = {"strip_y0": 880, "hardsub_intervals": [{"start": 1.0, "end": 3.0, "bbox": [50, 250, 10, 60]}]}
        cues = [{"id": 0, "show_a": 2.0, "show_b": 4.0}]
        plates = {0: (Image.new("RGBA", (320, 80), (0, 0, 0, 135)), 900)}
        with tempfile.TemporaryDirectory() as d:
            aicn.build_timeline(d, prof, cues, scan, style, plates, d, 760)
            for kind, want in (("m", [1.0, 2.0, 7.0]), ("p", [2.0, 2.0, 6.0])):
                with open(os.path.join(d, kind + ".ffconcat"), encoding="utf-8") as f:
                    lines = f.read().split("\n")
                durs = [float(x.split()[1]) for x in lines if x.startswith("duration")]
                self.assertAlmostEqual(sum(durs), 10.0, places=4)
                self.assertEqual([round(x, 3) for x in durs], want)

    def test_band_starts_above_masks_and_plates(self):
        from PIL import Image
        style = dict(aicn.DEFAULT_STYLE)
        scan = {"strip_y0": 600, "hardsub_intervals": [{"start": 0, "end": 1, "bbox": [0, 10, 20, 60]}]}
        y = aicn.band_y0({"height": 1080}, scan, style, {0: (Image.new("RGBA", (8, 8)), 700)})
        self.assertLessEqual(y, 600 + 20 - style["box_pad_y"] - 15 - 20)
        self.assertEqual(y % 8, 0)


class EncodeAndKey(unittest.TestCase):
    def test_resolve_encode_presets_and_quality_override(self):
        h264 = aicn.resolve_encode(aicn.DEFAULT_STYLE)
        self.assertEqual(h264["vcodec"], "h264_videotoolbox")
        self.assertEqual(h264["q"], 58)
        self.assertTrue(h264["hwdec"])

        hevc = aicn.resolve_encode(aicn.DEFAULT_STYLE, codec="hevc")
        self.assertEqual(hevc["vcodec"], "hevc_videotoolbox")
        self.assertEqual(hevc["tag"], "hvc1")
        self.assertEqual(hevc["q"], 60)

        hi = aicn.resolve_encode(aicn.DEFAULT_STYLE, codec="h264_high")
        self.assertEqual(hi["bitrate"], "5000k")
        self.assertNotIn("q", hi)

        over = aicn.resolve_encode(aicn.DEFAULT_STYLE, codec="h264_high", quality=55)
        self.assertEqual(over["q"], 55)
        self.assertNotIn("bitrate", over)

    def test_ff_render_key_reacts_to_codec_and_watermark(self):
        with tempfile.TemporaryDirectory() as d:
            fh = aicn.FastHash(d)
            cues = [{"id": 0, "sub_text": "Xin chào", "show_a": 1.0, "show_b": 2.0}]
            scan = {"hardsub_intervals": []}
            style = dict(aicn.DEFAULT_STYLE, font_path="")
            _, k_h264 = aicn.ff_render_key(d, fh, cues, scan, style, 760, aicn.resolve_encode(style, "h264"), None)
            _, k_hevc = aicn.ff_render_key(d, fh, cues, scan, style, 760, aicn.resolve_encode(style, "hevc"), None)
            self.assertNotEqual(k_h264, k_hevc)
            tl = os.path.join(d, "tl.png")
            tr = os.path.join(d, "tr.png")
            for p in (tl, tr):
                with open(p, "wb") as f:
                    f.write(b"fake-png")
            wm = {"tl": tl, "tr": tr, "tl_x": 42, "tl_y": 22, "tr_mx": 42, "tr_y": 58}
            _, k_wm = aicn.ff_render_key(d, fh, cues, scan, style, 760, aicn.resolve_encode(style, "h264"), wm)
            self.assertNotEqual(k_h264, k_wm)


class CLI(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True)

    def test_help_lists_all_commands(self):
        out = self.run_cli("--help").stdout
        for c in ("doctor", "discover", "init-series", "adopt", "plan", "tts-sync", "render",
                  "verify-overlay", "clean", "merge-series", "run-series", "log", "progress"):
            self.assertIn(c, out)

    def test_clean_requires_final_and_keeps_staging_by_default(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "media", "stems"))
            with open(os.path.join(d, "media", "stems", "vocals.wav"), "wb") as f:
                f.write(b"0" * 1024)
            with open(os.path.join(d, "media", ".staging_render.mp4"), "wb") as f:
                f.write(b"0" * 1024)
            # Without final MP4, clean refuses
            r_refuse = self.run_cli("clean", "--unit", d, "--dry-run")
            self.assertNotEqual(r_refuse.returncode, 0)
            # With final MP4, default clean removes stems but keeps .staging_render.mp4
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
            # merge-series must refuse when no working batches exist
            m = self.run_cli("merge-series", "--hub", hub)
            self.assertNotEqual(m.returncode, 0)
            # run-series dry-run on hub with 1 working batch
            b1 = os.path.join(hub, "units", "batch_01")
            os.makedirs(os.path.join(b1, "config"))
            with open(os.path.join(b1, "config", "project_config.json"), "w", encoding="utf-8") as f:
                f.write("{}")
            rs = self.run_cli("run-series", "--hub", hub, "--phase", "produce", "--dry-run")
            self.assertEqual(rs.returncode, 0, rs.stderr)
            self.assertIn("phase=produce", rs.stdout)

    def test_portrait_9x16_style_and_watermark_scaling(self):
        # Landscape keeps 48px / 1500px defaults untouched
        st_land = aicn.resolve_style_for_profile({}, {"width": 1920, "height": 1080})
        self.assertEqual(st_land["font_size"], 48)
        self.assertEqual(st_land["max_line_px"], 1500)

        # Portrait 720x1280 scales font_size down and clamps max_line_px inside W - 120 - 2*pad_x - 12
        st_port = aicn.resolve_style_for_profile(aicn.DEFAULT_STYLE, {"width": 720, "height": 1280})
        self.assertEqual(st_port["font_size"], 28)
        self.assertLessEqual(st_port["max_line_px"] + 2 * st_port["pad_x"], 720 - 120)

        with tempfile.TemporaryDirectory() as td:
            bd = os.path.join(td, "brand")
            os.makedirs(bd)
            for name in ("13_top_left_logo_large.png", "15_top_right_vietsub_floating.png"):
                with open(os.path.join(bd, name), "wb") as f:
                    f.write(b"png")
            wm_port = aicn.resolve_watermark(td, brand_dir=bd, width=720)
            self.assertIsNotNone(wm_port)
            self.assertEqual(wm_port["scale"], 0.5)
            self.assertEqual(wm_port["tl_x"], 21)

    def test_scan_find_hardsub_intervals_portrait_720(self):
        scan_path = os.path.join(
            os.path.dirname(HERE),
            "vendor",
            "video-asr-visual-audit",
            "scripts",
            "run_asr_and_scan.py",
        )
        sp_mod = importlib.util.spec_from_file_location("run_asr_and_scan", scan_path)
        ras = importlib.util.module_from_spec(sp_mod)
        sp_mod.loader.exec_module(ras)

        # Simulate English 720x1280 subtitle box centered around x=360, rel_y=[50..88]
        spans = [[210, 510, 50, 88]] * 10 + [None] * 2
        ivs = ras.find_hardsub_intervals(spans, fps_scan=5, width=720, strip_h=230)
        self.assertEqual(len(ivs), 1)
        self.assertEqual(ivs[0]["bbox"], [210, 510, 50, 88])

    def test_portrait_1080x1920_style_and_scan_detection(self):
        # 1080x1920 portrait must NOT be treated as 16:9 landscape
        st = aicn.resolve_style_for_profile(aicn.DEFAULT_STYLE, {"width": 1080, "height": 1920})
        self.assertLess(st["font_size"], 48)
        self.assertLessEqual(st["max_line_px"] + 2 * st["pad_x"], 1080 - 120)

        scan_path = os.path.join(
            os.path.dirname(HERE), "vendor", "video-asr-visual-audit", "scripts", "run_asr_and_scan.py"
        )
        sp_mod = importlib.util.spec_from_file_location("run_asr_and_scan", scan_path)
        ras = importlib.util.module_from_spec(sp_mod)
        sp_mod.loader.exec_module(ras)

        # 1080x1920 hardsub box centered at x=540, rel_y=[60..145] (h=85px, y1=145 > 98)
        spans = [[240, 840, 60, 145]] * 8
        ivs = ras.find_hardsub_intervals(spans, fps_scan=5, width=1080, height=1920, strip_h=240)
        self.assertEqual(len(ivs), 1)
        self.assertEqual(ivs[0]["bbox"], [240, 840, 60, 145])

    def test_calibrate_strip_y0_and_decoupled_series_bible(self):
        init_path = os.path.join(
            os.path.dirname(HERE), "vendor", "video-localization-orchestrator", "scripts", "init_workspace.py"
        )
        sp_mod = importlib.util.spec_from_file_location("init_workspace", init_path)
        iw = importlib.util.module_from_spec(sp_mod)
        sp_mod.loader.exec_module(iw)

        gate_path = os.path.join(
            os.path.dirname(HERE), "vendor", "video-localization-orchestrator", "scripts", "cicd_gate.py"
        )
        gp_mod = importlib.util.spec_from_file_location("cicd_gate", gate_path)
        cg = importlib.util.module_from_spec(gp_mod)
        gp_mod.loader.exec_module(cg)

        import json
        from PIL import Image, ImageDraw
        with tempfile.TemporaryDirectory() as td:
            # Build a 1080x1920 portrait test video with bright outlined bar at y=1257..1327
            png = os.path.join(td, "frame.png")
            img = Image.new("RGB", (1080, 1920), (40, 40, 50))
            dr = ImageDraw.Draw(img)
            dr.rectangle([260, 1254, 820, 1330], fill=(10, 10, 10))
            for x in range(280, 800, 28):
                dr.rectangle([x, 1260, x + 16, 1324], fill=(250, 250, 250))
            img.save(png)
            vid = os.path.join(td, "ep01.mp4")
            subprocess.run([
                "ffmpeg", "-v", "error", "-y", "-loop", "1", "-i", png,
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-t", "3.0", "-r", "25", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest", vid
            ], check=True)

            y0, sh, cal = iw.calibrate_strip_y0(vid, 1080, 1920, 3.0)
            self.assertTrue(cal)
            self.assertLess(y0, 1257)
            self.assertGreater(y0 + sh, 1327)

            # Run init_workspace.py and verify editing series_bible.json does NOT break Stage 0 gate
            ws_root = os.path.join(td, "ws")
            subprocess.run([
                sys.executable, init_path,
                "--workspace-root", ws_root,
                "--slug", "unit_01",
                "--video", vid,
            ], check=True, stdout=subprocess.DEVNULL)
            unit_dir = os.path.join(ws_root, "unit_01")
            cg.require_gate(unit_dir, 0)
            bible_p = os.path.join(unit_dir, "config", "series_bible.json")
            with open(bible_p, "r", encoding="utf-8") as f:
                bible = json.load(f)
            bible["glossary"] = {"主角": "Nhân vật chính"}
            with open(bible_p, "w", encoding="utf-8") as f:
                json.dump(bible, f)
            # Stage 0 gate must STILL pass after Stage 2 series_bible.json edit
            cg.require_gate(unit_dir, 0)

    def test_verify_overlay_rejects_av_duration_drift(self):
        with tempfile.TemporaryDirectory() as td:
            u = os.path.join(td, "unit_01")
            for d in ("data", "config", "media", "source", ".cache"):
                os.makedirs(os.path.join(u, d), exist_ok=True)
            aicn.jdump(os.path.join(u, "data", "00_video_profile.json"), {"width": 1080, "height": 1920, "duration": 2.0, "r_fps": 25.0, "is_vfr": False})
            aicn.jdump(os.path.join(u, "data", "00_episodes_manifest.json"), {"episodes": []})
            aicn.jdump(os.path.join(u, "data", "01_zh_bbox_scan.json"), {"strip_y0": 1200, "hardsub_intervals": []})
            aicn.jdump(os.path.join(u, "data", "03_aligned_subtitles.json"), [])
            aicn.jdump(os.path.join(u, "data", "04_overlay_geometry_log.json"), {"cues": []})
            aicn.jdump(os.path.join(u, "config", "overlay_style.json"), {})
            aicn.jdump(os.path.join(u, ".cache", "build_state.json"), {"render": {"engine": "ffmpeg"}})
            stg = os.path.join(u, "media", ".staging_render.mp4")
            # Video 2.0s vs Audio 3.2s -> 1.2s A/V drift must FAIL Stage 4 gate
            subprocess.run([
                "ffmpeg", "-v", "error", "-y",
                "-f", "lavfi", "-i", "color=c=black:s=320x240:d=2.0:r=25",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=3.2",
                "-c:v", "libx264", "-c:a", "aac", stg
            ], check=True)
            with self.assertRaises(SystemExit) as ctx:
                aicn.cmd_verify_overlay(type("A", (), {"unit": u, "no_gate": True})())
            self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
