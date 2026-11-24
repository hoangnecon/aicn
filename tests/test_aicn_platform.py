"""Unit tests for scripts/aicn_platform.py cross-platform abstractions."""

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import aicn_platform as ap  # noqa: E402


class PlatformLocks(unittest.TestCase):
    def test_file_lock_serializes_threads(self):
        with tempfile.TemporaryDirectory() as td:
            lp = os.path.join(td, "locks", "gpu.lock")
            active = []
            max_active = 0
            errors = []

            def worker(idx):
                nonlocal max_active
                try:
                    with ap.file_lock(lp):
                        active.append(idx)
                        max_active = max(max_active, len(active))
                        time.sleep(0.03)
                        active.remove(idx)
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
            self.assertFalse(errors, errors)
            self.assertEqual(max_active, 1)


class PlatformPaths(unittest.TestCase):
    def test_ffmpeg_concat_line_normalizes_slashes_and_quotes(self):
        line = ap.ffmpeg_concat_line(r"C:\tmp\it's a test\ep 01.mp4")
        self.assertNotIn("\\", line.replace(r"'\''", ""))
        self.assertIn("file '", line)
        self.assertIn(r"'\''", line)
        self.assertTrue(line.endswith("\n"))

    def test_normalize_rel_path_and_same_path(self):
        with tempfile.TemporaryDirectory() as td:
            sub = os.path.join(td, "media", "stems")
            os.makedirs(sub)
            rel = ap.normalize_rel_path(sub, td)
            self.assertEqual(rel, "media/stems")
            self.assertTrue(ap.same_path(sub, Path(td) / "media" / "stems"))
            if ap.is_windows():
                self.assertTrue(ap.same_path(sub.upper(), sub.lower()))


class PlatformEncoders(unittest.TestCase):
    def test_resolve_build_and_vendor_args_all_backends(self):
        for bk, exp_vc, exp_hw in (
            ("videotoolbox", "h264_videotoolbox", ["-hwaccel", "videotoolbox"]),
            ("nvenc", "h264_nvenc", []),
            ("cpu", "libx264", []),
        ):
            enc = ap.resolve_encode_preset({}, codec="h264", backend=bk)
            self.assertEqual(enc["vcodec"], exp_vc)
            self.assertEqual(ap.build_hwaccel_input_args(enc), exp_hw)
            vargs = ap.build_vcodec_args(enc, include_pix_fmt=True)
            self.assertIn(exp_vc, vargs)
            self.assertIn("-pix_fmt", vargs)

            vendor = ap.vendor_h264_encode_args("5200k", backend=bk)
            self.assertEqual(vendor[:2], ["-c:v", exp_vc])
            self.assertIn("5200k", vendor)

        nv_hevc = ap.resolve_encode_preset({}, codec="hevc", backend="nvenc")
        nv_args = ap.build_vcodec_args(nv_hevc)
        self.assertIn("hevc_nvenc", nv_args)
        self.assertIn("-rc", nv_args)
        self.assertIn("vbr", nv_args)
        self.assertIn("-cq", nv_args)
        self.assertIn("hvc1", nv_args)


class PlatformBackends(unittest.TestCase):
    def test_detect_asr_backend_env_overrides(self):
        for env_val, exp_eng, exp_dev in (
            ("mlx", "mlx", "mps"),
            ("faster-cuda", "faster-cuda", "cuda"),
            ("faster-cpu", "faster-cpu", "cpu"),
        ):
            with mock.patch.dict(os.environ, {"AICN_ASR": env_val}, clear=False):
                res = ap.detect_asr_backend(sys.executable, gpu_info={"cuda": False, "ct2_cuda": False})
                self.assertEqual(res["engine"], exp_eng)
                self.assertEqual(res["device"], exp_dev)

    def test_detect_demucs_backend_env_overrides(self):
        for env_val, exp_eng, exp_dev in (
            ("mlx", "mlx", "mps"),
            ("torch-cuda", "torch-cuda", "cuda"),
            ("torch-cpu", "torch-cpu", "cpu"),
        ):
            with mock.patch.dict(os.environ, {"AICN_DEMUCS": env_val}, clear=False):
                res = ap.detect_demucs_backend(sys.executable, gpu_info={"cuda": False})
                self.assertEqual(res["engine"], exp_eng)
                self.assertEqual(res["device"], exp_dev)

    def test_vendor_scripts_support_sibling_aicn_import(self):
        """Copied standalone vendor skills (e.g. ~/.gemini/config/skills/<stage>/) must still import aicn_platform."""
        import shutil
        import subprocess
        import tempfile

        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        vendor_root = os.path.join(root, "vendor")
        checked = 0
        for skill in os.listdir(vendor_root):
            sdir = os.path.join(vendor_root, skill, "scripts")
            if not os.path.isdir(sdir):
                continue
            for fn in os.listdir(sdir):
                if not fn.endswith(".py"):
                    continue
                p = os.path.join(sdir, fn)
                with open(p, encoding="utf-8") as fh:
                    src = fh.read()
                if "import aicn_platform" in src:
                    checked += 1
                    self.assertIn('"aicn", "scripts"', src, f"{p} missing sibling aicn/scripts fallback")
        self.assertGreaterEqual(checked, 10)

        with tempfile.TemporaryDirectory() as td:
            shutil.copytree(os.path.join(root, "scripts"), os.path.join(td, "aicn", "scripts"))
            shutil.copytree(
                os.path.join(vendor_root, "video-localization-orchestrator"),
                os.path.join(td, "video-localization-orchestrator"),
            )
            script = os.path.join(td, "video-localization-orchestrator", "scripts", "init_workspace.py")
            res = subprocess.run(
                [
                    sys.executable, "-c",
                    f"import importlib.util; spec = importlib.util.spec_from_file_location('iw', r'{script}'); "
                    "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); "
                    "assert m.ap is not None, 'ap was None'; print('OK', m.ap.__file__)",
                ],
                capture_output=True, text=True,
            )
            self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
            self.assertIn("OK", res.stdout)


if __name__ == "__main__":
    unittest.main()
