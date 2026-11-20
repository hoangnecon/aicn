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


if __name__ == "__main__":
    unittest.main()
