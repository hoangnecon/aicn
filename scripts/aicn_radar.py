#!/usr/bin/env python3
"""
aicn_radar.py — Unified Chinese Short-Drama / AI-Drama Analytics, Trending & Search Index CLI.

Indexes Hongguo (via hg_tool.cjs + public-details), Douyin (via Evil0ctal ABogus/websign/curl_cffi),
and Bilibili (via yt-dlp) into a fast local SQLite + FTS5 analytics database.
Strictly enforces Chinese-first, original-platform sourcing (no YouTube re-ups).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import aicn_platform as ap  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SKILL_DIR.parents[1]
HG_TOOL = SKILL_DIR / "scripts" / "hg_tool.cjs"
DY_TOOL = SKILL_DIR / "scripts" / "dy_tool.py"
DD_TOOL = SKILL_DIR / "scripts" / "dd_tool.py"
DY_PY = Path(ap.find_dy_python(PROJECT_ROOT))
NODE_BIN = ap.find_tool("node")
HONGGUO_REPO = PROJECT_ROOT / "tools" / "hongguo-drama"
DOUYIN_REPO = PROJECT_ROOT / "tools" / "Douyin_TikTok_Download_API"
DEFAULT_DB = PROJECT_ROOT / "data" / "drama_index.db"

BILI_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def connect_db(db_path: Path) -> sqlite3.Connection:
    """Open SQLite DB and ensure schema + FTS5 virtual table exist."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS series (
            uid TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            series_id TEXT NOT NULL,
            title_zh TEXT NOT NULL,
            title_vi TEXT DEFAULT '',
            format TEXT DEFAULT 'unknown',
            author TEXT DEFAULT '',
            episodes INTEGER DEFAULT 0,
            duration_sec INTEGER DEFAULT 0,
            published_ts INTEGER DEFAULT 0,
            likes INTEGER DEFAULT 0,
            favorites INTEGER DEFAULT 0,
            comments INTEGER DEFAULT 0,
            shares INTEGER DEFAULT 0,
            views INTEGER DEFAULT 0,
            heat_score REAL DEFAULT 0.0,
            tags TEXT DEFAULT '',
            synopsis TEXT DEFAULT '',
            cover_url TEXT DEFAULT '',
            source_url TEXT DEFAULT '',
            rank_note TEXT DEFAULT '',
            updated_ts INTEGER NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS series_fts USING fts5(
            uid UNINDEXED,
            title_zh,
            title_vi,
            tags,
            synopsis,
            author,
            series_id,
            content='series',
            content_rowid='rowid'
        );
        CREATE TRIGGER IF NOT EXISTS series_ai AFTER INSERT ON series BEGIN
            INSERT INTO series_fts(rowid, uid, title_zh, title_vi, tags, synopsis, author, series_id)
            VALUES (new.rowid, new.uid, new.title_zh, new.title_vi, new.tags, new.synopsis, new.author, new.series_id);
        END;
        CREATE TRIGGER IF NOT EXISTS series_ad AFTER DELETE ON series BEGIN
            INSERT INTO series_fts(series_fts, rowid, uid, title_zh, title_vi, tags, synopsis, author, series_id)
            VALUES ('delete', old.rowid, old.uid, old.title_zh, old.title_vi, old.tags, old.synopsis, old.author, old.series_id);
        END;
        CREATE TRIGGER IF NOT EXISTS series_au AFTER UPDATE ON series BEGIN
            INSERT INTO series_fts(series_fts, rowid, uid, title_zh, title_vi, tags, synopsis, author, series_id)
            VALUES ('delete', old.rowid, old.uid, old.title_zh, old.title_vi, old.tags, old.synopsis, old.author, old.series_id);
            INSERT INTO series_fts(rowid, uid, title_zh, title_vi, tags, synopsis, author, series_id)
            VALUES (new.rowid, new.uid, new.title_zh, new.title_vi, new.tags, new.synopsis, new.author, new.series_id);
        END;
        CREATE TABLE IF NOT EXISTS douyin_hot (
            board TEXT NOT NULL,
            position INTEGER NOT NULL,
            word TEXT NOT NULL,
            hot_value INTEGER DEFAULT 0,
            fetched_ts INTEGER NOT NULL,
            PRIMARY KEY (board, word)
        );
        """
    )
    return conn


def compute_heat_score(published_ts: int, likes: int, favorites: int, views: int, now_ts: int) -> float:
    """Compute recency-weighted engagement velocity score."""
    engagement = likes + favorites * 1.5 + views * 0.02
    if published_ts <= 0:
        return round(engagement, 2)
    age_days = max(0.25, (now_ts - published_ts) / 86400.0)
    recency_boost = 1.0 / (1.0 + age_days / 7.0)
    return round((engagement / age_days) * (1.0 + recency_boost), 2)


def upsert_series(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Insert or update a series record with computed heat score."""
    now_ts = int(time.time())
    pub_ts = int(row.get("published_ts") or 0)
    likes = int(row.get("likes") or 0)
    favs = int(row.get("favorites") or 0)
    views = int(row.get("views") or 0)
    heat = row.get("heat_score")
    if not heat:
        heat = compute_heat_score(pub_ts, likes, favs, views, now_ts)
    uid = f"{row['platform']}:{row['series_id']}"
    conn.execute(
        """
        INSERT INTO series (
            uid, platform, series_id, title_zh, title_vi, format, author,
            episodes, duration_sec, published_ts, likes, favorites, comments,
            shares, views, heat_score, tags, synopsis, cover_url, source_url,
            rank_note, updated_ts
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(uid) DO UPDATE SET
            title_zh = CASE WHEN excluded.title_zh != '' THEN excluded.title_zh ELSE series.title_zh END,
            title_vi = CASE WHEN excluded.title_vi != '' THEN excluded.title_vi ELSE series.title_vi END,
            format = CASE WHEN excluded.format != 'unknown' THEN excluded.format ELSE series.format END,
            author = CASE WHEN excluded.author != '' THEN excluded.author ELSE series.author END,
            episodes = MAX(series.episodes, excluded.episodes),
            duration_sec = MAX(series.duration_sec, excluded.duration_sec),
            published_ts = CASE WHEN excluded.published_ts > 0 THEN excluded.published_ts ELSE series.published_ts END,
            likes = MAX(series.likes, excluded.likes),
            favorites = MAX(series.favorites, excluded.favorites),
            comments = MAX(series.comments, excluded.comments),
            shares = MAX(series.shares, excluded.shares),
            views = MAX(series.views, excluded.views),
            heat_score = MAX(series.heat_score, excluded.heat_score),
            tags = CASE WHEN excluded.tags != '' THEN excluded.tags ELSE series.tags END,
            synopsis = CASE WHEN excluded.synopsis != '' THEN excluded.synopsis ELSE series.synopsis END,
            cover_url = CASE WHEN excluded.cover_url != '' THEN excluded.cover_url ELSE series.cover_url END,
            source_url = CASE WHEN excluded.source_url != '' THEN excluded.source_url ELSE series.source_url END,
            rank_note = CASE WHEN excluded.rank_note != '' THEN excluded.rank_note ELSE series.rank_note END,
            updated_ts = excluded.updated_ts
        """,
        (
            uid,
            row["platform"],
            str(row["series_id"]),
            row.get("title_zh", ""),
            row.get("title_vi", ""),
            row.get("format", "unknown"),
            row.get("author", ""),
            int(row.get("episodes") or 0),
            int(row.get("duration_sec") or 0),
            pub_ts,
            likes,
            favs,
            int(row.get("comments") or 0),
            int(row.get("shares") or 0),
            views,
            float(heat),
            row.get("tags", ""),
            row.get("synopsis", ""),
            row.get("cover_url", ""),
            row.get("source_url", ""),
            row.get("rank_note", ""),
            now_ts,
        ),
    )


def infer_format(tags: list[str], fallback: str = "unknown") -> str:
    joined = " ".join(tags)
    if any(k in joined for k in ("仿真", "AI", "ai_series", "ai-short-dramas")):
        return "ai"
    if any(k in joined for k in ("漫剧", "动漫", "3D", "comic")):
        return "comic"
    if any(k in joined for k in ("真人", "short_play")):
        return "real"
    return fallback


def enrich_hongguo_details(series_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch exact first_visible_time, like_count, favorite_count & synopsis from Hongguo web details."""
    if not series_ids or not HONGGUO_REPO.exists():
        return {}
    js = """
const axios = require('axios');
const { readRouterData } = require('./src/native/catalog');
const ids = JSON.parse(process.argv[1]);
(async () => {
  const out = {};
  for (const sid of ids) {
    try {
      const res = await axios.get(`https://hongguoduanju.com/detail?series_id=${sid}`, {
        timeout: 6000,
        headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36' }
      });
      const router = readRouterData(res.data);
      const d = Object.values(router.loaderData || {}).find(v => v && v.seriesDetail);
      if (d && d.seriesDetail) {
        const sd = d.seriesDetail;
        const soc = d.seriesSocialInfo || {};
        out[sid] = {
          first_visible_time: Number(sd.first_visible_time || 0),
          like_count: Number(soc.series_like_count || 0),
          favorite_count: Number(soc.series_favorite_count || 0),
          episodes: Number(sd.episode_cnt || 0),
          synopsis: String(sd.series_intro || ''),
          cover: String(sd.series_cover || '')
        };
      }
    } catch (_) {}
  }
  console.log(JSON.stringify(out));
})();
"""
    res = subprocess.run(
        [NODE_BIN, "-e", js, json.dumps(series_ids)],
        cwd=str(HONGGUO_REPO),
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0 or not res.stdout.strip():
        return {}
    try:
        return json.loads(res.stdout.strip())
    except json.JSONDecodeError:
        return {}


def sync_hongguo(conn: sqlite3.Connection, pages: int = 2, enrich_top: int = 15) -> int:
    """Sync Hongguo live charts and new releases into SQLite index."""
    if not HG_TOOL.exists():
        raise FileNotFoundError(f"Missing {HG_TOOL}")

    collected: dict[str, dict[str, Any]] = {}

    # 1. Rank charts
    charts = [
        ("hot-ai-drama", "ai"),
        ("hot-real-drama", "real"),
        ("hot-comic-drama", "comic"),
        ("hot-drama", "unknown"),
    ]
    for cat, fmt in charts:
        proc = subprocess.run(
            [NODE_BIN, str(HG_TOOL), "rank", "--category", cat, "--json"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            continue
        data = json.loads(proc.stdout)
        for idx, it in enumerate(data.get("items", []), start=1):
            sid = str(it.get("series_id", ""))
            if not sid:
                continue
            tags = it.get("tags") or []
            entry = collected.setdefault(
                sid,
                {
                    "platform": "hongguo",
                    "series_id": sid,
                    "title_zh": it.get("series_title", ""),
                    "format": infer_format(tags, fmt),
                    "episodes": it.get("episode_count") or 0,
                    "tags": ",".join(tags),
                    "synopsis": it.get("description", ""),
                    "cover_url": it.get("cover", ""),
                    "source_url": it.get("url", f"https://hongguoduanju.com/detail?series_id={sid}"),
                    "rank_note": f"{cat} #{idx}",
                },
            )
            if f"{cat}" not in entry["rank_note"]:
                entry["rank_note"] = f"{entry['rank_note']}; {cat} #{idx}".strip("; ")

    # 2. Fresh releases
    proc_new = subprocess.run(
        [NODE_BIN, str(HG_TOOL), "new", "--pages", str(pages), "--limit", str(pages * 30), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc_new.returncode == 0:
        data_new = json.loads(proc_new.stdout)
        for it in data_new.get("items", []):
            sid = str(it.get("series_id", ""))
            if not sid:
                continue
            tags = it.get("tags") or it.get("labels") or []
            fmt = "ai" if it.get("genre") == "ai_series" else ("real" if it.get("genre") == "short_play" else infer_format(tags))
            if sid not in collected:
                collected[sid] = {
                    "platform": "hongguo",
                    "series_id": sid,
                    "title_zh": it.get("series_title", ""),
                    "format": fmt,
                    "episodes": it.get("episode_count") or 0,
                    "tags": ",".join(tags),
                    "synopsis": it.get("description", ""),
                    "cover_url": it.get("cover", ""),
                    "source_url": it.get("url", f"https://hongguoduanju.com/detail?series_id={sid}"),
                    "rank_note": "new_release",
                }
            elif "new_release" not in collected[sid]["rank_note"]:
                collected[sid]["rank_note"] += "; new_release"

    # 3. Enrich top N candidates with exact publish timestamps & engagement stats
    target_ids = list(collected.keys())[:enrich_top]
    details = enrich_hongguo_details(target_ids)
    for sid, row in collected.items():
        if sid in details:
            det = details[sid]
            row["published_ts"] = det["first_visible_time"]
            row["likes"] = det["like_count"]
            row["favorites"] = det["favorite_count"]
            if det["episodes"]:
                row["episodes"] = det["episodes"]
            if det["synopsis"] and not row["synopsis"]:
                row["synopsis"] = det["synopsis"]
            if det["cover"] and not row["cover_url"]:
                row["cover_url"] = det["cover"]
        upsert_series(conn, row)
    conn.commit()
    return len(collected)


def sync_bilibili(conn: sqlite3.Connection, queries: list[str], count_per_query: int = 8) -> int:
    """Sync Bilibili AI/short-drama videos via yt-dlp into SQLite index."""
    total = 0
    for q in queries:
        proc = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--dump-single-json", f"bilisearch{count_per_query}:{q}", "--user-agent", BILI_UA],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            continue
        urls = [e["url"] for e in json.loads(proc.stdout).get("entries", []) if e.get("url")]
        if not urls:
            continue
        meta_proc = subprocess.run(
            ["yt-dlp", "--skip-download", "--dump-json", *urls, "--user-agent", BILI_UA],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in meta_proc.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            d = json.loads(line)
            bvid = d.get("id") or ""
            if not bvid:
                continue
            up = d.get("upload_date") or ""
            pub_ts = 0
            if len(up) == 8 and up.isdigit():
                pub_ts = int(datetime.strptime(up, "%Y%m%d").replace(tzinfo=timezone.utc).timestamp())
            tags = d.get("tags") or []
            upsert_series(
                conn,
                {
                    "platform": "bilibili",
                    "series_id": bvid,
                    "title_zh": d.get("title") or "",
                    "format": infer_format(tags + [d.get("title") or ""], "ai"),
                    "author": d.get("uploader") or "",
                    "episodes": 1,
                    "duration_sec": int(d.get("duration") or 0),
                    "published_ts": pub_ts,
                    "likes": int(d.get("like_count") or 0),
                    "views": int(d.get("view_count") or 0),
                    "tags": ",".join(tags[:8]),
                    "synopsis": d.get("description") or "",
                    "cover_url": d.get("thumbnail") or "",
                    "source_url": f"https://www.bilibili.com/video/{bvid}",
                    "rank_note": f"bilisearch:{q}",
                },
            )
            total += 1
    conn.commit()
    return total


def sync_douyin(conn: sqlite3.Connection, seeds: list[str], hops: int = 2, max_nodes: int = 60) -> int:
    """Sync Douyin AI-drama seed details + related_posts BFS graph + hot boards via dy_tool.py."""
    if not DY_TOOL.exists() or not DY_PY.exists():
        raise FileNotFoundError(f"Missing Douyin tooling: {DY_TOOL} / {DY_PY}")

    helper = """
import json, sys, time
sys.path.insert(0, sys.argv[1])
from dy_tool import DouyinClient, summarize_aweme

seeds = json.loads(sys.argv[2])
hops = int(sys.argv[3])
max_nodes = int(sys.argv[4])

c = DouyinClient()
seen = {}
frontier = list(dict.fromkeys(seeds))

for aid in list(frontier):
    if len(seen) >= max_nodes:
        break
    try:
        ad = c.detail(aid).get("aweme_detail")
        if ad:
            info = summarize_aweme(ad)
            seen[info["aweme_id"]] = (info, "seed")
    except Exception:
        pass
    time.sleep(0.4)

for depth in range(hops):
    next_frontier = []
    for aid in frontier:
        if len(seen) >= max_nodes:
            break
        try:
            rel = c.related_posts(aid, count=10).get("aweme_list") or []
            for ad in rel:
                info = summarize_aweme(ad)
                wid = info["aweme_id"]
                if wid and wid not in seen:
                    seen[wid] = (info, f"related_hop_{depth+1}")
                    next_frontier.append(wid)
                    if len(seen) >= max_nodes:
                        break
        except Exception:
            pass
        time.sleep(0.4)
    frontier = next_frontier
    if not frontier:
        break

hot_rows = {}
for b in ("hot", "entertainment", "challenge"):
    try:
        hot_rows[b] = c.hot(b)
    except Exception:
        hot_rows[b] = []

print(json.dumps({"items": [{"info": v[0], "note": v[1]} for v in seen.values()], "hot": hot_rows}, ensure_ascii=False))
"""
    proc = subprocess.run(
        [str(DY_PY), "-c", helper, str(SKILL_DIR / "scripts"), json.dumps(seeds), str(hops), str(max_nodes)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(f"Douyin sync failed: {proc.stderr[:400]}")

    payload = json.loads(proc.stdout.strip())
    items = payload.get("items", [])
    for wrap in items:
        info = wrap["info"]
        desc = info.get("desc") or ""
        title = desc.splitlines()[0][:120].strip() if desc else info["aweme_id"]
        fmt = infer_format([desc], "ai")
        upsert_series(
            conn,
            {
                "platform": "douyin",
                "series_id": info["aweme_id"],
                "title_zh": title,
                "format": fmt,
                "author": info.get("author") or "",
                "episodes": max(1, int(info.get("mix_total_eps") or 1)),
                "duration_sec": int(round(info.get("duration_sec") or 0)),
                "published_ts": int(info.get("create_time") or 0),
                "likes": int(info.get("likes") or 0),
                "favorites": int(info.get("favorites") or 0),
                "comments": int(info.get("comments") or 0),
                "shares": int(info.get("shares") or 0),
                "tags": info.get("mix_name") or "",
                "synopsis": desc,
                "cover_url": info.get("cover_url") or "",
                "source_url": f"https://www.douyin.com/video/{info['aweme_id']}",
                "rank_note": wrap.get("note", "douyin"),
            },
        )

    now_ts = int(time.time())
    for board, words in (payload.get("hot") or {}).items():
        for w in words:
            word = str(w.get("word") or "").strip()
            if not word:
                continue
            conn.execute(
                """
                INSERT INTO douyin_hot (board, position, word, hot_value, fetched_ts)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(board, word) DO UPDATE SET
                    position = excluded.position,
                    hot_value = excluded.hot_value,
                    fetched_ts = excluded.fetched_ts
                """,
                (board, int(w.get("position") or 0), word, int(w.get("hot_value") or 0), now_ts),
            )
    conn.commit()
    return len(items)


def sync_dramadunyam(
    conn: sqlite3.Connection,
    pages: int = 2,
    enrich_top: int = 12,
    lang: str = "en",
) -> int:
    """Sync clean English, non-adult DramaDunyam AI/short-drama charts & lists into SQLite index."""
    if str(SKILL_DIR / "scripts") not in sys.path:
        sys.path.insert(0, str(SKILL_DIR / "scripts"))
    from dd_tool import DramaDunyamClient, extract_tag_names

    client = DramaDunyamClient()
    collected: dict[str, dict[str, Any]] = {}

    def _merge(it: dict[str, Any], fmt_hint: str, note: str) -> None:
        sid = str(it.get("id") or "")
        slug = str(it.get("slug") or "")
        if not sid or not slug:
            return
        tag_list = extract_tag_names(it.get("tags"))
        if fmt_hint == "ai":
            tag_list = list(dict.fromkeys([*tag_list, "ai-short-dramas"]))
        entry = collected.get(sid)
        if not entry:
            collected[sid] = {
                "platform": "dramadunyam",
                "series_id": sid,
                "slug": slug,
                "title_zh": str(it.get("title") or ""),
                "format": infer_format(tag_list, fmt_hint),
                "author": str(it.get("platform") or ""),
                "episodes": int(it.get("available_episodes") or it.get("total_episodes") or 0),
                "tags": ",".join(tag_list),
                "synopsis": str(it.get("description") or ""),
                "cover_url": str(it.get("cover") or ""),
                "source_url": f"https://dramadunyam.com/en/series/{slug}",
                "rank_note": note,
            }
        else:
            if note and note not in entry["rank_note"]:
                entry["rank_note"] = f"{entry['rank_note']}; {note}".strip("; ")
            if fmt_hint == "ai":
                entry["format"] = "ai"

    for p in range(1, max(1, pages) + 1):
        res_ai = client.list_series(tag="ai-short-dramas", page=p, limit=40, lang=lang, exclude_adult=True)
        for it in res_ai.get("items", []):
            _merge(it, "ai", "tag:ai-short-dramas")

    for idx, it in enumerate(client.top(period="hafta", lang=lang, exclude_adult=True).get("items", []), 1):
        _merge(it, "unknown", f"siralama:hafta #{idx}")

    for p in range(1, max(1, pages) + 1):
        res_pop = client.list_series(sort_type="populer", page=p, limit=40, lang=lang, exclude_adult=True)
        for it in res_pop.get("items", []):
            _merge(it, "unknown", f"populer:p{p}")

    for sid in list(collected.keys())[: max(0, enrich_top)]:
        row = collected[sid]
        try:
            det = client.detail(row["slug"], lang=lang)
        except Exception:
            continue
        if det.get("yetiskin"):
            collected.pop(sid, None)
            continue
        tag_list = extract_tag_names(det.get("tags"))
        if row["format"] == "ai" and "ai-short-dramas" not in tag_list:
            tag_list.append("ai-short-dramas")
        if det.get("genre"):
            tag_list.append(str(det["genre"]))
        row["tags"] = ",".join(dict.fromkeys([t for t in tag_list if t]))
        row["synopsis"] = str(det.get("description") or row["synopsis"])
        row["likes"] = int(det.get("like_count") or 0)
        row["views"] = int(det.get("view_count") or det.get("views") or 0)
        row["episodes"] = int(det.get("available_episodes") or det.get("total_episodes") or row["episodes"])
        variants = det.get("cover_variants") or {}
        row["cover_url"] = str(variants.get("1080") or variants.get("720") or det.get("cover") or row["cover_url"])

    for row in collected.values():
        upsert_series(conn, row)
    conn.commit()
    return len(collected)


def format_age(published_ts: int, now_ts: int) -> str:
    if published_ts <= 0:
        return "?"
    days = (now_ts - published_ts) / 86400.0
    if days < 1:
        return f"{max(1, int(days * 24))}h"
    return f"{days:.1f}d"


def cmd_search(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    now_ts = int(time.time())
    where = []
    params: list[Any] = []
    if args.platform and args.platform != "all":
        where.append("s.platform = ?")
        params.append(args.platform)
    if args.format and args.format != "all":
        where.append("s.format = ?")
        params.append(args.format)
    if args.max_age_days and args.max_age_days > 0:
        cutoff = now_ts - args.max_age_days * 86400
        where.append("s.published_ts >= ?")
        params.append(cutoff)

    if args.query:
        tokens = [t.strip() for t in args.query.split() if t.strip()]
        like_clauses = []
        for tok in tokens:
            pat = f"%{tok}%"
            like_clauses.append("(s.title_zh LIKE ? OR s.title_vi LIKE ? OR s.tags LIKE ? OR s.synopsis LIKE ? OR s.series_id LIKE ? OR s.author LIKE ?)")
            params.extend([pat, pat, pat, pat, pat, pat])
        where.append("(" + " AND ".join(like_clauses) + ")")

    order_map = {
        "score": "s.heat_score DESC, s.likes DESC, s.published_ts DESC",
        "newest": "s.published_ts DESC, s.heat_score DESC",
        "likes": "s.likes DESC, s.views DESC",
    }
    order_sql = order_map.get(args.sort, order_map["score"])
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"SELECT s.* FROM series s {where_sql} ORDER BY {order_sql} LIMIT ?"
    params.append(args.limit)

    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    for r in rows:
        r["age"] = format_age(r["published_ts"], now_ts)

    if args.json:
        print(json.dumps({"count": len(rows), "items": rows}, ensure_ascii=False, indent=2))
        return

    print(f"=== AICN Drama Index ({len(rows)} results) ===")
    for i, r in enumerate(rows, start=1):
        print(
            f"{i:02d}. [{r['platform']}:{r['series_id']}] ({r['format']}, {r['episodes']}ep, age={r['age']}, "
            f"likes={r['likes']}, fav={r['favorites']}, score={r['heat_score']}) {r['title_zh']}"
        )
        if r["rank_note"]:
            print(f"    signal: {r['rank_note']} | url: {r['source_url']}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Unified Hongguo/Douyin/Bilibili Analytics & Search Index")
    p.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite DB path")
    sub = p.add_subparsers(dest="cmd", required=True)

    s_hg = sub.add_parser("sync-hongguo", help="Sync Hongguo charts & fresh releases")
    s_hg.add_argument("--pages", type=int, default=2)
    s_hg.add_argument("--enrich-top", type=int, default=20)

    s_bi = sub.add_parser("sync-bilibili", help="Sync Bilibili AI/short-drama series")
    s_bi.add_argument("--query", action="append", help="Chinese search query (repeatable)")
    s_bi.add_argument("--limit", type=int, default=8)

    s_dy = sub.add_parser("sync-douyin", help="Sync Douyin AI-drama seed + related graph + hot boards")
    s_dy.add_argument("--seed", action="append", help="Seed aweme_id (repeatable)")
    s_dy.add_argument("--hops", type=int, default=2)
    s_dy.add_argument("--max-nodes", type=int, default=50)

    s_dd = sub.add_parser("sync-dramadunyam", help="Sync DramaDunyam English non-adult AI/short dramas")
    s_dd.add_argument("--pages", type=int, default=2)
    s_dd.add_argument("--enrich-top", type=int, default=12)
    s_dd.add_argument("--lang", default="en")

    s_all = sub.add_parser("sync-all", help="Sync Hongguo + Douyin + Bilibili + DramaDunyam into unified index")
    s_all.add_argument("--pages", type=int, default=2)
    s_all.add_argument("--enrich-top", type=int, default=15)

    for cname in ("search", "top"):
        sc = sub.add_parser(cname, help="Query local analytics index")
        sc.add_argument("query", nargs="?", default="", help="Chinese/VI search keywords")
        sc.add_argument("--platform", default="all", choices=["all", "hongguo", "douyin", "bilibili", "dramadunyam"])
        sc.add_argument("--format", default="all", choices=["all", "ai", "real", "comic"])
        sc.add_argument("--max-age-days", type=int, default=0)
        sc.add_argument("--sort", default="score", choices=["score", "newest", "likes"])
        sc.add_argument("--limit", type=int, default=20)
        sc.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    conn = connect_db(args.db)
    try:
        if args.cmd == "sync-hongguo":
            n = sync_hongguo(conn, pages=args.pages, enrich_top=args.enrich_top)
            print(f"[radar] Indexed {n} Hongguo series -> {args.db}")
        elif args.cmd == "sync-bilibili":
            queries = args.query or ["AI漫剧 仿真人", "某天醒来 美少女", "重生七零 打猎"]
            n = sync_bilibili(conn, queries=queries, count_per_query=args.limit)
            print(f"[radar] Indexed {n} Bilibili series -> {args.db}")
        elif args.cmd == "sync-douyin":
            seeds = args.seed or ["7707844707132476726", "7694594942861167914", "7702783920940518656"]
            n = sync_douyin(conn, seeds=seeds, hops=args.hops, max_nodes=args.max_nodes)
            print(f"[radar] Indexed {n} Douyin AI series + hot boards -> {args.db}")
        elif args.cmd == "sync-dramadunyam":
            n = sync_dramadunyam(conn, pages=args.pages, enrich_top=args.enrich_top, lang=args.lang)
            print(f"[radar] Indexed {n} DramaDunyam English series -> {args.db}")
        elif args.cmd == "sync-all":
            n_hg = sync_hongguo(conn, pages=args.pages, enrich_top=args.enrich_top)
            n_dy = sync_douyin(conn, seeds=["7707844707132476726", "7694594942861167914", "7702783920940518656"], hops=2, max_nodes=40)
            n_bi = sync_bilibili(conn, queries=["AI漫剧 仿真人", "某天醒来 美少女", "重生七零 打猎"], count_per_query=6)
            n_dd = sync_dramadunyam(conn, pages=args.pages, enrich_top=min(12, args.enrich_top))
            print(f"[radar] Synced total: hongguo={n_hg}, douyin={n_dy}, bilibili={n_bi}, dramadunyam={n_dd} -> {args.db}")
        elif args.cmd in ("search", "top"):
            cmd_search(conn, args)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
