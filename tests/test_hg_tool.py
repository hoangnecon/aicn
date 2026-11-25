import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
HG_TOOL = SKILL_DIR / "scripts" / "hg_tool.cjs"
DY_TOOL = SKILL_DIR / "scripts" / "dy_tool.py"
RADAR_TOOL = SKILL_DIR / "scripts" / "aicn_radar.py"

sys.path.insert(0, str(SKILL_DIR / "scripts"))
import aicn_platform as ap  # noqa: E402
import aicn_radar  # noqa: E402

NODE_BIN = ap.find_tool("node")
DY_PY = Path(ap.find_dy_python(SKILL_DIR.parents[1]))


class TestHgToolAndRsi(unittest.TestCase):
    def test_hg_tool_exists_and_help_runs(self):
        self.assertTrue(HG_TOOL.exists(), f"Missing {HG_TOOL}")
        res = subprocess.run(
            [NODE_BIN, str(HG_TOOL), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        for cmd in ("check-upstream", "rank", "new", "search", "details", "album", "episodes", "download", "fetch-covers"):
            self.assertIn(cmd, res.stdout)

    def test_hg_tool_unknown_command_fails_nonzero(self):
        res = subprocess.run(
            [NODE_BIN, str(HG_TOOL), "nonexistent-subcmd"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Unknown command", res.stderr)

    def test_hg_tool_missing_args_fail_cleanly(self):
        for argv in (["search"], ["details"], ["album"], ["episodes"], ["download"], ["fetch-covers"]):
            res = subprocess.run(
                [NODE_BIN, str(HG_TOOL), *argv],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(res.returncode, 0, f"Expected failure for {argv}")
            self.assertIn("Usage:", res.stderr)

    def test_rsi_docs_and_lessons_present(self):
        for rel in (
            "LESSONS.md",
            "references/sourcing_and_analytics.md",
            "references/recursive_self_improvement.md",
        ):
            p = SKILL_DIR / rel
            self.assertTrue(p.exists(), f"Missing RSI/sourcing doc: {p}")
            self.assertGreater(p.stat().st_size, 200)

    def test_no_legacy_ohg_binary_in_path(self):
        found = shutil.which("ohg")
        self.assertIsNone(found, f"Retired ohg binary still on PATH: {found}")

    def test_dy_tool_help_and_radar_index_roundtrip(self):
        self.assertTrue(DY_TOOL.exists(), f"Missing {DY_TOOL}")
        self.assertTrue(RADAR_TOOL.exists(), f"Missing {RADAR_TOOL}")
        if DY_PY.exists():
            res = subprocess.run([str(DY_PY), str(DY_TOOL), "--help"], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr)
            for cmd in ("detail", "download", "mix", "hot"):
                self.assertIn(cmd, res.stdout)

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "idx.db"
            conn = aicn_radar.connect_db(db)
            aicn_radar.upsert_series(
                conn,
                {
                    "platform": "douyin",
                    "series_id": "7707844707132476726",
                    "title_zh": "某天醒来，我家两只猫变成美少女",
                    "format": "ai",
                    "author": "小律",
                    "episodes": 5,
                    "published_ts": 1794622450,
                    "likes": 985,
                    "favorites": 214,
                    "source_url": "https://www.douyin.com/video/7707844707132476726",
                    "rank_note": "seed",
                },
            )
            conn.commit()
            rows = conn.execute("SELECT title_zh, heat_score FROM series WHERE platform='douyin'").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertIn("两只猫", rows[0]["title_zh"])
            self.assertGreater(rows[0]["heat_score"], 0)
            conn.close()

            q = subprocess.run(
                [sys.executable, str(RADAR_TOOL), "--db", str(db), "search", "两只猫", "--json"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(q.returncode, 0, q.stderr)
            self.assertIn("7707844707132476726", q.stdout)

    def test_init_workspace_normalizes_timebase_and_stream_order(self):
        init_ws = SKILL_DIR / "scripts" / "init_workspace.py"
        src = init_ws.read_text(encoding="utf-8")
        self.assertIn('vs.get("time_base"', src)
        self.assertIn('vs.get("index"', src)
        self.assertIn('"-map", "0:v:0", "-map", "0:a:0"', src)
        self.assertIn('"-video_track_timescale"', src)

    def test_dd_tool_filtering_and_episode_spec(self):
        import dd_tool

        self.assertEqual(dd_tool.parse_episode_spec("all", 4), [1, 2, 3, 4])
        self.assertEqual(dd_tool.parse_episode_spec("1-3,5", 6), [1, 2, 3, 5])
        with self.assertRaises(ValueError):
            dd_tool.parse_episode_spec("1-10", 5)

        items = [
            {"id": "1", "title": "Clean EN", "language": "en", "yetiskin": False},
            {"id": "2", "title": "Adult", "language": "en", "yetiskin": True},
            {"id": "3", "title": "Turkish", "language": "tr", "yetiskin": False},
        ]
        clean = dd_tool.filter_series_items(items, lang="en", exclude_adult=True)
        self.assertEqual([x["id"] for x in clean], ["1"])

        tags = dd_tool.extract_tag_names([{"slug": "a", "name": "Rules Horror"}, "Survival"])
        self.assertEqual(tags, ["Rules Horror", "Survival"])

    def test_radar_supports_dramadunyam_platform(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "idx.db"
            conn = aicn_radar.connect_db(db)
            aicn_radar.upsert_series(
                conn,
                {
                    "platform": "dramadunyam",
                    "series_id": "332020",
                    "title_zh": "Deadliest Prison: The Female Warden Is My Plaything",
                    "format": "ai",
                    "author": "DramaBox",
                    "episodes": 50,
                    "likes": 126,
                    "views": 511,
                    "source_url": "https://dramadunyam.com/en/series/deadliest-prison-the-female-warden-is-my-plaything",
                    "rank_note": "siralama:hafta #12",
                },
            )
            conn.commit()
            q = subprocess.run(
                [sys.executable, str(RADAR_TOOL), "--db", str(db), "search", "--platform", "dramadunyam", "Prison", "--json"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(q.returncode, 0, q.stderr)
            self.assertIn("332020", q.stdout)
            conn.close()

    def test_dd_tool_retries_transient_503_source_preparing(self):
        import io
        import json
        import urllib.error
        from unittest.mock import patch
        import dd_tool

        client = dd_tool.DramaDunyamClient()
        client._initialized = True
        calls = {"n": 0}

        class _Resp:
            def __init__(self, body: bytes):
                self._body = body

            def read(self) -> bytes:
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_open(req, timeout=20.0):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError(
                    req.full_url,
                    503,
                    "Service Unavailable",
                    None,
                    io.BytesIO(b'{"hata":"source_preparing","retryable":true}'),
                )
            return _Resp(json.dumps({"delivery": "direct", "url": "https://cdn.example/ep47.mp4"}).encode())

        with patch.object(client.opener, "open", side_effect=fake_open), patch("time.sleep"):
            res = client.play_info("332020", 47)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(res["url"], "https://cdn.example/ep47.mp4")

    def test_dd_tool_shortmax_relay_hls_referer_cookie_and_prefix(self):
        import http.cookiejar
        import json
        from unittest.mock import patch
        import dd_tool

        client = dd_tool.DramaDunyamClient()
        client._initialized = True
        client.cookies.set_cookie(
            http.cookiejar.Cookie(
                version=0,
                name="dd_bilet",
                value="guest_tok_123",
                port=None,
                port_specified=False,
                domain="dramadunyam.com",
                domain_specified=False,
                domain_initial_dot=False,
                path="/",
                path_specified=True,
                secure=True,
                expires=None,
                discard=True,
                comment=None,
                comment_url=None,
                rest={},
            )
        )

        seen_reqs = []

        class _Resp:
            def __init__(self, body: bytes):
                self._body = body

            def read(self) -> bytes:
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_open(req, timeout=20.0):
            seen_reqs.append(req)
            return _Resp(
                json.dumps(
                    {"delivery": "hls", "url": "/vr/dr/hls/123/main.m3u8?auth_key=xyz"}
                ).encode()
            )

        with patch.object(client.opener, "open", side_effect=fake_open):
            play = client.play_info("123", 1, slug="my-test-series")

        self.assertEqual(len(seen_reqs), 1)
        self.assertEqual(
            seen_reqs[0].get_header("Referer"),
            "https://dramadunyam.com/en/series/my-test-series",
        )

        ffmpeg_cmds = []

        class _Proc:
            returncode = 0
            stderr = ""

        def fake_run(cmd, capture_output=True, text=True, check=False):
            ffmpeg_cmds.append(cmd)
            Path(cmd[-1]).write_bytes(b"fake-mp4")
            return _Proc()

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "ep01.mp4"
            with (
                patch("subprocess.run", side_effect=fake_run),
                patch.object(
                    dd_tool,
                    "ffprobe_summary",
                    return_value={"duration_sec": 60.0, "width": 720, "height": 1280, "size_mb": 5.0},
                ),
            ):
                client.download_stream(play["url"], dest, delivery="hls", slug="my-test-series")

        self.assertEqual(len(ffmpeg_cmds), 1)
        cmd = ffmpeg_cmds[0]
        self.assertIn("https://dramadunyam.com/vr/dr/hls/123/main.m3u8?auth_key=xyz", cmd)
        hdr_idx = cmd.index("-headers") + 1
        headers_val = cmd[hdr_idx]
        self.assertIn("Referer: https://dramadunyam.com/en/series/my-test-series", headers_val)
        self.assertIn("Cookie: dd_bilet=guest_tok_123", headers_val)

    def test_dy_tool_overwrite_and_mix_pagination(self):
        import types
        from unittest.mock import patch
        import dy_tool

        with tempfile.TemporaryDirectory() as td:
            out_mp4 = Path(td) / "ep01.mp4"
            out_mp4.write_bytes(b"old-content")

            class _Resp:
                content = b"new-mp4-bytes"
                def raise_for_status(self):
                    return None

            fake_req = types.SimpleNamespace(get=lambda *a, **kw: _Resp())
            fake_cf = types.SimpleNamespace(requests=fake_req)
            with patch.dict(sys.modules, {"curl_cffi": fake_cf, "curl_cffi.requests": fake_req}):
                dy_tool.download_media("https://example.com/v.mp4", out_mp4)
            self.assertEqual(out_mp4.read_bytes(), b"new-mp4-bytes")

        pages_seen = []

        class _FakeClient:
            def mix_posts(self, mix_id, cursor=0, count=20):
                pages_seen.append(cursor)
                if cursor == 0:
                    return {
                        "has_more": True,
                        "cursor": 20,
                        "aweme_list": [{"aweme_id": "101", "mix_info": {"statis": {"current_episode": 1}}, "video": {"play_addr": {"url_list": ["u1"]}}}],
                    }
                return {
                    "has_more": False,
                    "cursor": 21,
                    "aweme_list": [{"aweme_id": "102", "mix_info": {"statis": {"current_episode": 2}}, "video": {"play_addr": {"url_list": ["u2"]}}}],
                }

        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "mix_out"
            with patch.object(dy_tool, "DouyinClient", return_value=_FakeClient()), \
                 patch.object(dy_tool, "download_media", side_effect=lambda u, p, **kw: Path(p).write_bytes(b"mp4")), \
                 patch.object(dy_tool.subprocess, "run", return_value=types.SimpleNamespace(stdout='{"format":{}}', returncode=0)), \
                 patch("time.sleep"), \
                 patch.object(sys, "argv", ["dy_tool.py", "download", "777", "--mix", "--out", str(out_dir)]):
                rc = dy_tool.main()
            self.assertEqual(rc, 0)
            self.assertEqual(pages_seen, [0, 20])
            self.assertTrue((out_dir / "ep_001_101.mp4").exists())
            self.assertTrue((out_dir / "ep_002_102.mp4").exists())

    def test_aicn_radar_db_busy_timeout_and_wal(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "idx.db"
            conn = aicn_radar.connect_db(db)
            try:
                bt = conn.execute("PRAGMA busy_timeout").fetchone()[0]
                jm = conn.execute("PRAGMA journal_mode").fetchone()[0]
                self.assertGreaterEqual(int(bt), 15000)
                self.assertEqual(str(jm).lower(), "wal")
            finally:
                conn.close()

    def test_hg_tool_is_thin_wrapper_no_hardcoded_upstream_internals(self):
        # Redundant hg_sdk_helper.py must stay retired (upstreamed into hongguo-drama v8.2 resolve_cli.py)
        self.assertFalse(
            (SKILL_DIR / "scripts" / "hg_sdk_helper.py").exists(),
            "Retire scripts/hg_sdk_helper.py — delegate to upstream hongguo-drama/scripts/sdk-playback/resolve_cli.py",
        )
        hg_src = HG_TOOL.read_text(encoding="utf-8")
        for forbidden in (
            "/novel/player/",
            "/reading/bookapi/",
            "phone_sdk_core_v5",
            "phone_playback_request_v5",
            "hg_sdk_helper.py",
        ):
            self.assertNotIn(forbidden, hg_src, f"hg_tool.cjs must not hardcode upstream internal: {forbidden}")
        self.assertIn("check-upstream", hg_src)
        self.assertIn("resolve_cli.py", hg_src)

        radar_src = RADAR_TOOL.read_text(encoding="utf-8")
        self.assertNotIn("require('./src/native/catalog')", radar_src)
        self.assertIn('"check-upstream"', radar_src)
        self.assertIn('"details"', radar_src)

    def test_hg_tool_check_upstream_offline_introspects_mock_repo(self):
        import json

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "hongguo-drama"
            native = repo / "src" / "native"
            sdk_dir = repo / "scripts" / "sdk-playback"
            native.mkdir(parents=True)
            sdk_dir.mkdir(parents=True)
            (repo / "node_modules").mkdir()
            (repo / "package.json").write_text('{"name":"hongguo-downloader","version":"8.2.0"}', encoding="utf-8")
            (native / "hongguo-app-catalog.js").write_text(
                "module.exports = { browseList() {}, catalogCover(u) { return u; } };", encoding="utf-8"
            )
            (native / "hongguo-app-new.js").write_text(
                "module.exports = { listNewReleases() {} };", encoding="utf-8"
            )
            (native / "hongguo-app-search.js").write_text(
                "module.exports = { searchApp() {} };", encoding="utf-8"
            )
            (native / "hongguo.js").write_text(
                "module.exports = { fetchEpisodeList() {}, deriveKey() {}, UA: 'test-ua' };", encoding="utf-8"
            )
            (native / "app-source.js").write_text(
                "module.exports = { fetchAppPlayUrl() {} };", encoding="utf-8"
            )
            (native / "public-details.js").write_text(
                "module.exports = { fetchPublicDetails() {} };", encoding="utf-8"
            )
            (sdk_dir / "resolve_cli.py").write_text(
                "# flags: --serve --videos --content-type\n", encoding="utf-8"
            )

            res = subprocess.run(
                [NODE_BIN, str(HG_TOOL), "check-upstream", "--offline", "--json", "--repo", str(repo)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
            report = json.loads(res.stdout)
            self.assertTrue(report["installed"])
            self.assertEqual(report["local_version"], "8.2.0")
            self.assertTrue(report["wrapper_contract_ok"])
            self.assertEqual(report["missing_hooks"], [])
            self.assertEqual(report["sdk_cli"]["capabilities"], ["--serve", "--videos", "--content-type"])
            self.assertIn("fetchAppPlayUrl", report["api_surface"]["app-source.js"])


if __name__ == "__main__":
    unittest.main()
