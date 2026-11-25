"""Unit tests for scripts/aicn_platform.py cross-platform, SQLite-WAL cache, and VRAM pool abstractions."""

import hashlib
import json
import os
import shutil
import subprocess
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


class PlatformLocksAndGpuPool(unittest.TestCase):
    def test_serial_gpu_lock_removed(self):
        self.assertFalse(hasattr(ap, "gpu_lock"), "Serial gpu_lock() must stay deleted in v3.0.0")

    def test_file_lock_serializes_threads(self):
        with tempfile.TemporaryDirectory() as td:
            lp = os.path.join(td, "locks", "test.lock")
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

    def test_vram_budget_and_concurrent_gpu_slots_threads(self):
        self.assertEqual(ap.gpu_vram_budget_mb({"backend": "cuda", "vram_gb": 6.0}), 5222)
        self.assertEqual(ap.gpu_vram_budget_mb({"backend": "metal", "vram_gb": 16.0}), 9830)
        # On RTX 2060 (5222 MB = 10x512MB slots), whisper(2560=5) + demucs(1024=2) + vieneu(1024=2) = 9 <= 10!
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"AICN_LOCK_DIR": td, "AICN_GPU_SLOTS": "4", "AICN_SKIP_VRAM_PROBE": "1"}):
                active = 0
                max_active = 0
                guard = threading.Lock()
                errors = []

                def worker(idx):
                    nonlocal active, max_active
                    try:
                        # 1024 MB = 2 slots out of 4 -> up to 2 workers run concurrently!
                        with ap.gpu_slot(f"w{idx}", vram_mb=1024) as lease:
                            self.assertEqual(len(lease["slots"]), 2)
                            with guard:
                                active += 1
                                max_active = max(max_active, active)
                            time.sleep(0.08)
                            with guard:
                                active -= 1
                    except Exception as exc:
                        errors.append(exc)

                threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=10)
                self.assertFalse(errors, errors)
                self.assertEqual(max_active, 2, f"Expected 2 concurrent 1GB jobs on 4-slot pool, got {max_active}")

    def test_vram_gpu_slots_multiprocess_concurrency(self):
        with tempfile.TemporaryDirectory() as td:
            env = dict(os.environ)
            env["AICN_LOCK_DIR"] = td
            env["AICN_GPU_SLOTS"] = "4"
            env["AICN_SKIP_VRAM_PROBE"] = "1"
            code = (
                f"import sys, time\nsys.path.insert(0, r'{SCRIPTS}')\n"
                "import aicn_platform as ap\n"
                "with ap.gpu_slot('mp', vram_mb=1024) as lease:\n"
                "    print('ENTER', time.time(), lease['slots'], flush=True)\n"
                "    time.sleep(0.25)\n"
                "    print('EXIT', time.time(), flush=True)\n"
            )
            procs = [
                subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for _ in range(3)
            ]
            events = []
            for p in procs:
                out, err = p.communicate(timeout=15)
                self.assertEqual(p.returncode, 0, err)
                for ln in out.splitlines():
                    parts = ln.split()
                    if parts and parts[0] in ("ENTER", "EXIT"):
                        events.append((float(parts[1]), 1 if parts[0] == "ENTER" else -1))
            events.sort(key=lambda x: (x[0], x[1]))
            cur = 0
            peak = 0
            for _, delta in events:
                cur += delta
                peak = max(peak, cur)
            self.assertEqual(peak, 2, f"Expected peak 2 concurrent processes on 4 slots, got {peak}")

    def test_gpu_slot_parent_child_inheritance_no_deadlock(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"AICN_LOCK_DIR": td, "AICN_GPU_SLOTS": "6", "AICN_SKIP_VRAM_PROBE": "1"}):
                with ap.gpu_slot("whisper", vram_mb=2560) as parent_lease:
                    self.assertFalse(parent_lease["inherited"])
                    self.assertEqual(len(parent_lease["slots"]), 5)
                    res = subprocess.run(
                        [
                            sys.executable, "-c",
                            f"import sys\nsys.path.insert(0, r'{SCRIPTS}')\n"
                            "import aicn_platform as ap\n"
                            "with ap.gpu_slot('whisper', vram_mb=2560) as lease:\n"
                            "    assert lease['inherited'] is True\n"
                            "    print('CHILD_GPU_SLOT_OK', len(lease['slots']))\n",
                        ],
                        env=ap.child_env_with_held_locks(),
                        capture_output=True, text=True, timeout=10,
                    )
                    self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
                    self.assertIn("CHILD_GPU_SLOT_OK 5", res.stdout)

    def test_gpu_slot_child_upgrade_low_vram_no_deadlock(self):
        """Child inheriting 2 real parent slots upgrading to 5 under low live VRAM must not self-deadlock."""
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"AICN_LOCK_DIR": td, "AICN_GPU_SLOTS": "6", "AICN_SKIP_VRAM_PROBE": "1"}):
                with ap.gpu_slot("parent", vram_mb=1024) as parent_lease:
                    self.assertEqual(parent_lease["slots"], [0, 1])
                    child_env = ap.child_env_with_held_locks()
                    child_env.pop("AICN_GPU_SLOTS", None)
                    child_env.pop("AICN_GPU_BUDGET_MB", None)
                    child_env.pop("AICN_SKIP_VRAM_PROBE", None)
                    code = (
                        f"import sys\nsys.path.insert(0, r'{SCRIPTS}')\n"
                        "from unittest import mock\n"
                        "import aicn_platform as ap\n"
                        "probed = []\n"
                        "def fake_free():\n"
                        "    probed.append(900)\n"
                        "    return 900\n"
                        "with mock.patch.object(ap, '_cached_gpu_info', return_value={'backend': 'cuda', 'vram_gb': 4.0}), \\\n"
                        "     mock.patch.object(ap, '_probe_nvidia_smi_free_mb', side_effect=fake_free):\n"
                        "    with ap.gpu_slot('tts', vram_mb=2560) as lease:\n"
                        "        assert probed, 'expected live VRAM guard probe to run'\n"
                        "        assert lease['inherited'] is True\n"
                        "        assert lease['slots'] == [0, 1, 2, 3, 4], lease\n"
                        "        print('UPGRADE_OK', lease['slots'])\n"
                    )
                    res = subprocess.run(
                        [sys.executable, "-c", code],
                        env=child_env,
                        capture_output=True, text=True, timeout=6,
                    )
                    self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
                    self.assertIn("UPGRADE_OK [0, 1, 2, 3, 4]", res.stdout)


class SqliteWalContentHashAndKeys(unittest.TestCase):
    def test_sqlite_wal_content_sha256_survives_move_and_concurrent_processes(self):
        with tempfile.TemporaryDirectory() as td:
            cache_dir = os.path.join(td, "gcache")
            f1 = os.path.join(td, "dir_a", "media.bin")
            f2 = os.path.join(td, "dir_b", "renamed_media.bin")
            os.makedirs(os.path.dirname(f1), exist_ok=True)
            os.makedirs(os.path.dirname(f2), exist_ok=True)
            payload = b"aicn-v3-content-addressed-payload-" * 1024
            with open(f1, "wb") as fh:
                fh.write(payload)
            shutil.copyfile(f1, f2)
            os.utime(f2, (1700000123, 1700000123))

            expected = hashlib.sha256(payload).hexdigest()
            env = dict(os.environ)
            env["AICN_CACHE_DIR"] = cache_dir
            code = (
                f"import sys\nsys.path.insert(0, r'{SCRIPTS}')\n"
                "import aicn_platform as ap\n"
                f"print(ap.content_sha256(r'{f1}'))\n"
                f"print(ap.content_sha256(r'{f2}'))\n"
            )
            procs = [
                subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for _ in range(8)
            ]
            for p in procs:
                out, err = p.communicate(timeout=15)
                self.assertEqual(p.returncode, 0, err)
                lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
                self.assertEqual(lines, [expected, expected])

            self.assertTrue(os.path.isfile(os.path.join(cache_dir, "content_hash.db")))
            self.assertFalse(os.path.exists(os.path.join(cache_dir, "global_stat_hash.json")))
            self.assertEqual(ap.content_sha256(os.path.join(td, "does_not_exist.bin"), cache_dir=cache_dir), "missing")

    def test_open_hash_db_closes_conn_on_error_and_threads_cleanup(self):
        import sqlite3
        conn_closed = []
        cursor_closed = []

        class _FakeCursor:
            def fetchall(self):
                return []
            def close(self):
                cursor_closed.append(True)

        class _FakeConn:
            def execute(self, sql):
                if "journal_mode=WAL" in sql:
                    raise sqlite3.OperationalError("database is locked")
                return _FakeCursor()
            def close(self):
                conn_closed.append(True)

        with mock.patch.object(ap.sqlite3, "connect", return_value=_FakeConn()):
            with self.assertRaises(sqlite3.OperationalError):
                ap._open_hash_db("ignored.db")
        self.assertEqual(len(cursor_closed), 1)
        self.assertEqual(len(conn_closed), 1)

        with tempfile.TemporaryDirectory() as td:
            files = []
            for i in range(6):
                fp = os.path.join(td, f"f{i}.bin")
                with open(fp, "wb") as fh:
                    fh.write(f"payload-{i}".encode("utf-8") * 512)
                files.append(fp)
            errs = []

            def worker(idx):
                try:
                    for j in range(12):
                        ap.content_sha256(files[(idx + j) % len(files)], cache_dir=td)
                except Exception as exc:
                    errs.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)
            self.assertFalse(errs, errs)

    def test_single_source_key_builders_and_atomic_write(self):
        # tts_sent_key must stay byte-identical with v2.7.0 .cache/tts_sent WAV filenames
        legacy_tts = hashlib.sha256(
            json.dumps({"t": "Xin chào", "v": "Trúc Ly", "e": "vieneu-v3-turbo-48k", "temp": 0.8, "k": 25},
                       sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        self.assertEqual(ap.tts_sent_key("Xin chào", "Trúc Ly"), legacy_tts)
        self.assertNotEqual(ap.tts_sent_key("Xin chào", "Trúc Ly"), ap.tts_sent_key("Xin chào", "Hải Đăng"))

        k1 = ap.stage1_asr_key("aaa", "bbb", "large-v3", "zh")
        self.assertEqual(len(k1), 24)
        self.assertNotEqual(k1, ap.stage1_asr_key("aaa", "bbb", "medium", "zh"))
        self.assertNotEqual(ap.stems_key("aaa"), ap.stems_key("aab"))
        self.assertEqual(ap.rb_stretch_key("wav1", 1.1234), ap.rb_stretch_key("wav1", 1.1231))
        self.assertNotEqual(
            ap.wm_overlay_key(1080, 1920, "tl", "tr", 0.5, [21, 11, 21, 29]),
            ap.wm_overlay_key(1920, 1080, "tl", "tr", 1.0, [42, 22, 42, 58]),
        )
        self.assertNotEqual(
            ap.render_key("s", "a", None, {"vcodec": "copy"}),
            ap.render_key("s", "a", "wm1", {"vcodec": "h264_nvenc"}),
        )

        with tempfile.TemporaryDirectory() as td:
            target = os.path.join(td, "state.json")
            errors = []

            def writer(idx):
                try:
                    for j in range(15):
                        ap.atomic_write_json(target, {"worker": idx, "iter": j})
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
            self.assertFalse(errors, errors)
            with open(target, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertIn("worker", data)
            self.assertEqual([fn for fn in os.listdir(td) if fn.endswith(".tmp")], [])


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

        filters = ap.probe_ffmpeg_filters()
        for fname in ("overlay", "scale_vt", "overlay_cuda", "hwupload_cuda", "scale_cuda"):
            self.assertIn(fname, filters)


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

    def test_plugin_manifests_unified_scripts_and_skills_sdo_contract(self):
        """Verify multi-provider plugin manifests, unified scripts/ imports, and all 7 aicn-* skills SDO rules."""
        import json
        import py_compile
        import re
        import subprocess
        import tempfile

        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        self.assertFalse(os.path.exists(os.path.join(root, "vendor")), "Legacy vendor/ directory must not exist")

        # 1. Antigravity plugin.json strict top-level schema check
        allowed_agy_keys = {
            "name", "description", "logo", "suggestedPrompts", "displayName",
            "version", "author", "homepage", "repository", "disabled",
        }
        with open(os.path.join(root, "plugin.json"), encoding="utf-8") as fh:
            agy_manifest = json.load(fh)
        self.assertEqual(agy_manifest["name"], "aicn")
        self.assertTrue(set(agy_manifest.keys()).issubset(allowed_agy_keys), set(agy_manifest.keys()) - allowed_agy_keys)
        self.assertTrue(os.path.isfile(os.path.join(root, agy_manifest["logo"])))
        self.assertEqual(len(agy_manifest.get("suggestedPrompts", [])), 3)

        # 2. Multi-provider manifests (Claude Code, Cursor, Codex, Gemini CLI)
        for rel in (
            ".claude-plugin/plugin.json",
            ".cursor-plugin/plugin.json",
            ".codex-plugin/plugin.json",
            "gemini-extension.json",
        ):
            mp = os.path.join(root, rel)
            self.assertTrue(os.path.isfile(mp), f"Missing provider manifest: {rel}")
            with open(mp, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual(data["name"], "aicn")
            self.assertEqual(data["version"], agy_manifest["version"])
        self.assertTrue(os.path.isfile(os.path.join(root, "rules", "AGENTS.md")))
        self.assertTrue(os.path.isfile(os.path.join(root, "GEMINI.md")))

        # 3. All 7 user-invocable aicn-* skills SDO & path reference checks
        expected_skills = {
            "aicn-scout", "aicn-create", "aicn-script", "aicn-produce",
            "aicn-audit", "aicn-fix", "aicn-clean",
        }
        skills_root = os.path.join(root, "skills")
        actual_skills = {d for d in os.listdir(skills_root) if os.path.isdir(os.path.join(skills_root, d))}
        self.assertEqual(actual_skills, expected_skills)

        ref_pat = re.compile(r"<aicn>/(scripts|references)/([A-Za-z0-9_.-]+)")
        for sname in sorted(expected_skills):
            smd = os.path.join(skills_root, sname, "SKILL.md")
            self.assertTrue(os.path.isfile(smd))
            txt = open(smd, encoding="utf-8").read()
            self.assertTrue(txt.startswith("---\n"), f"{sname} missing YAML frontmatter")
            fm = txt.split("---\n", 2)[1]
            self.assertLessEqual(len(fm), 1024, f"{sname} frontmatter > 1024 chars")
            m_name = re.search(r"^name:\s*(.+)$", fm, re.M)
            m_desc = re.search(r"^description:\s*(.+)$", fm, re.M)
            self.assertIsNotNone(m_name)
            self.assertIsNotNone(m_desc)
            self.assertEqual(m_name.group(1).strip(), sname)
            desc = m_desc.group(1).strip()
            self.assertTrue(desc.startswith("Use when "), f"{sname} description must start with 'Use when ': {desc}")
            self.assertLessEqual(len(desc), 500, f"{sname} description > 500 chars ({len(desc)})")
            self.assertNotIn("vendor/", txt, f"{sname} still references retired vendor/")
            for folder, fn in ref_pat.findall(txt):
                target = os.path.join(root, folder, fn)
                self.assertTrue(os.path.isfile(target), f"{sname} references missing {folder}/{fn}")

        # 4. Unified scripts/ compile & standalone import check
        scripts_dir = os.path.join(root, "scripts")
        py_files = [fn for fn in sorted(os.listdir(scripts_dir)) if fn.endswith(".py")]
        self.assertGreaterEqual(len(py_files), 15)
        for fn in py_files:
            py_compile.compile(os.path.join(scripts_dir, fn), doraise=True)

        with tempfile.TemporaryDirectory() as td:
            copied_scripts = os.path.join(td, "plugin", "scripts")
            shutil.copytree(scripts_dir, copied_scripts)
            script = os.path.join(copied_scripts, "init_workspace.py")
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

    def test_file_lock_child_process_reentrant_no_deadlock(self):
        """Parent holding file_lock() spawning child with child_env_with_held_locks() must not self-deadlock."""
        import subprocess
        import tempfile

        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        with tempfile.TemporaryDirectory() as td:
            lp = os.path.join(td, "nested.lock")
            with ap.file_lock(lp, reentrant=True):
                res = subprocess.run(
                    [
                        sys.executable, "-c",
                        f"import sys\nsys.path.insert(0, r'{os.path.join(root, 'scripts')}')\n"
                        f"import aicn_platform as ap\n"
                        f"with ap.file_lock(r'{lp}', reentrant=True):\n    print('CHILD_LOCK_OK')\n",
                    ],
                    env=ap.child_env_with_held_locks(),
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
                self.assertIn("CHILD_LOCK_OK", res.stdout)


if __name__ == "__main__":
    unittest.main()
