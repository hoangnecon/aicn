import os
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
DY_PY = SKILL_DIR.parents[1] / "tools" / ".venv-dy" / "bin" / "python"

sys.path.insert(0, str(SKILL_DIR / "scripts"))
import aicn_radar  # noqa: E402


class TestHgToolAndRsi(unittest.TestCase):
    def test_hg_tool_exists_and_help_runs(self):
        self.assertTrue(HG_TOOL.exists(), f"Missing {HG_TOOL}")
        res = subprocess.run(
            ["node", str(HG_TOOL), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        for cmd in ("rank", "new", "search", "episodes", "download", "fetch-covers"):
            self.assertIn(cmd, res.stdout)

    def test_hg_tool_unknown_command_fails_nonzero(self):
        res = subprocess.run(
            ["node", str(HG_TOOL), "nonexistent-subcmd"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Unknown command", res.stderr)

    def test_hg_tool_missing_args_fail_cleanly(self):
        for argv in (["search"], ["episodes"], ["download"], ["fetch-covers"]):
            res = subprocess.run(
                ["node", str(HG_TOOL), *argv],
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
        res = subprocess.run(["which", "ohg"], capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0, f"Retired ohg binary still on PATH: {res.stdout}")

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
        init_ws = (
            SKILL_DIR
            / "vendor"
            / "video-localization-orchestrator"
            / "scripts"
            / "init_workspace.py"
        )
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


if __name__ == "__main__":
    unittest.main()
