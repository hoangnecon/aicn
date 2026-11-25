#!/usr/bin/env python3
"""
dy_tool.py — Douyin (抖音) Discovery, Collection/Mix Analytics & No-Watermark Downloader.
Uses Evil0ctal/Douyin_TikTok_Download_API native signing (ABogus + websign) + curl_cffi TLS
impersonation + headless Chrome guest cookie minter (no login required for detail/mix/author/hot).
"""

from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import aicn_platform as ap  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ap.find_project_root(SKILL_DIR) or SKILL_DIR.parents[1]
DOUYIN_REPO = PROJECT_ROOT / "tools" / "Douyin_TikTok_Download_API"
COOKIE_CACHE = PROJECT_ROOT / "tools" / ".dy_cookies.json"
FFPROBE = ap.find_tool("ffprobe")
FFMPEG = ap.find_tool("ffmpeg")

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
MS_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-"


def _load_evil0ctal():
    """Load pure-Python ABogus, SM3, and websign from Evil0ctal repo without DB/Redis deps."""
    import importlib.util
    import types

    src = str(DOUYIN_REPO / "src")
    for pkg in ("dtk", "dtk.signing", "dtk.signing.native"):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            m.__path__ = [f"{src}/" + pkg.replace(".", "/")]
            sys.modules[pkg] = m

    def load_mod(name: str, rel: str):
        if name in sys.modules:
            return sys.modules[name]
        spec = importlib.util.spec_from_file_location(name, f"{src}/{rel}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    load_mod("dtk.signing.native.sm3", "dtk/signing/native/sm3.py")
    abogus = load_mod("dtk.signing.native.abogus", "dtk/signing/native/abogus.py")
    websign = load_mod("dtk.signing.native.websign", "dtk/signing/native/websign.py")
    return abogus, websign


def mint_guest_cookies(seed_url: str = "https://www.douyin.com/video/7707844707132476726", max_age_sec: int = 3600) -> dict[str, str]:
    """Reuse cached UIFID/ttwid guest cookies or mint fresh ones via headless Chrome."""
    if COOKIE_CACHE.exists():
        try:
            cached = json.loads(COOKIE_CACHE.read_text(encoding="utf-8"))
            if time.time() - cached.get("minted_at", 0) < max_age_sec and cached.get("cookies", {}).get("UIFID_TEMP"):
                return cached["cookies"]
        except Exception:
            pass

    from playwright.sync_api import sync_playwright

    chrome_path = ap.find_chrome()
    with sync_playwright() as p:
        launch_kw: dict[str, Any] = {
            "headless": True,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if chrome_path:
            launch_kw["executable_path"] = chrome_path
        browser = p.chromium.launch(**launch_kw)
        ctx = browser.new_context(locale="zh-CN", user_agent=UA, viewport={"width": 1920, "height": 1080})
        page = ctx.new_page()
        page.goto(seed_url, wait_until="domcontentloaded", timeout=25000)
        page.wait_for_timeout(5500)
        cookies = {c["name"]: c["value"] for c in ctx.cookies() if c["name"]}
        browser.close()

    if not any(k in cookies for k in ("UIFID", "UIFID_TEMP", "uifid")):
        raise RuntimeError("Failed to mint Douyin UIFID guest cookie")

    COOKIE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    ap.atomic_write_json(COOKIE_CACHE, {"minted_at": time.time(), "cookies": cookies})
    return cookies


class DouyinClient:
    """Thin signed client backed by Evil0ctal ABogus + websign + curl_cffi Chrome TLS."""

    def __init__(self, force_mint: bool = False) -> None:
        from curl_cffi import requests
        self.abogus, self.websign = _load_evil0ctal()
        if force_mint and COOKIE_CACHE.exists():
            COOKIE_CACHE.unlink()
        self.cookies = mint_guest_cookies()
        self.session = requests.Session(impersonate="chrome131")
        self.session.cookies.update(self.cookies)
        self.signer = self.abogus.ABogus(user_agent=UA)
        self.uifid = self.websign.pick_uifid(self.cookies) or ""

    def _base_params(self) -> dict[str, str]:
        ms = self.cookies.get("msToken") or ("".join(random.choice(MS_ALPHABET) for _ in range(126)) + "==")
        return {
            "device_platform": "webapp",
            "aid": "6383",
            "channel": "channel_pc_web",
            "pc_client_type": "1",
            "version_code": "170400",
            "version_name": "17.4.0",
            "cookie_enabled": "true",
            "screen_width": "1920",
            "screen_height": "1080",
            "browser_language": "zh-CN",
            "browser_platform": "MacIntel",
            "browser_name": "Chrome",
            "browser_version": "131.0.0.0",
            "browser_online": "true",
            "engine_name": "Blink",
            "engine_version": "131.0.0.0",
            "os_name": "Mac OS",
            "os_version": "10.15.7",
            "cpu_core_num": "8",
            "device_memory": "8",
            "platform": "PC",
            "msToken": ms,
        }

    def get_signed(self, path: str, extra: dict[str, str], referer: str, sign_uifid: bool = False) -> dict[str, Any]:
        params = {**self._base_params(), **extra}
        if self.cookies.get("s_v_web_id"):
            params["verifyFp"] = self.cookies["s_v_web_id"]
            params["fp"] = self.cookies["s_v_web_id"]
        pairs = list(params.items())
        q = urllib.parse.urlencode(pairs)
        ab = self.signer.get_value(q)
        pairs.append(("a_bogus", ab))
        headers = {
            "User-Agent": UA,
            "Referer": referer,
            "Accept": "application/json, text/plain, */*",
        }
        if sign_uifid and self.uifid:
            qs, _, sec_headers = self.websign.sign(pairs, self.uifid)
            headers.update(sec_headers)
        else:
            qs = urllib.parse.urlencode(pairs)
        url = f"https://www.douyin.com{path}?{qs}"
        resp = self.session.get(url, headers=headers, timeout=15)
        if resp.status_code != 200 or not resp.text:
            raise RuntimeError(f"Douyin {path} -> HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def detail(self, aweme_id: str) -> dict[str, Any]:
        return self.get_signed(
            "/aweme/v1/web/aweme/detail/",
            {"aweme_id": aweme_id},
            referer=f"https://www.douyin.com/video/{aweme_id}",
            sign_uifid=True,
        )

    def mix_posts(self, mix_id: str, cursor: int = 0, count: int = 20) -> dict[str, Any]:
        return self.get_signed(
            "/aweme/v1/web/mix/aweme/",
            {"mix_id": mix_id, "cursor": str(cursor), "count": str(count)},
            referer=f"https://www.douyin.com/collection/{mix_id}",
            sign_uifid=True,
        )

    def author_posts(self, sec_user_id: str, max_cursor: int = 0, count: int = 18) -> dict[str, Any]:
        return self.get_signed(
            "/aweme/v1/web/aweme/post/",
            {
                "sec_user_id": sec_user_id,
                "max_cursor": str(max_cursor),
                "count": str(count),
                "publish_video_strategy_type": "2",
                "from_user_page": "1",
                "locate_query": "false",
                "need_time_list": "1",
                "show_live_replay_strategy": "1",
                "time_list_query": "0",
                "pc_libra_divert": "Mac OS",
                "whale_cut_token": "",
                "cut_version": "1",
            },
            referer=f"https://www.douyin.com/user/{sec_user_id}",
            sign_uifid=True,
        )

    def related_posts(self, aweme_id: str, count: int = 10) -> dict[str, Any]:
        return self.get_signed(
            "/aweme/v1/web/aweme/related/",
            {"aweme_id": aweme_id, "count": str(count), "filterGids": "", "refresh_index": "1"},
            referer=f"https://www.douyin.com/video/{aweme_id}",
            sign_uifid=False,
        )

    def author_mixes(self, sec_user_id: str, cursor: int = 0, count: int = 20) -> dict[str, Any]:
        return self.get_signed(
            "/aweme/v1/web/mix/list/",
            {"sec_user_id": sec_user_id, "cursor": str(cursor), "count": str(count)},
            referer=f"https://www.douyin.com/user/{sec_user_id}",
            sign_uifid=True,
        )

    def hot(self, board: str = "hot") -> list[dict[str, Any]]:
        boards = {
            "hot": ("0", ""),
            "entertainment": ("2", "2"),
            "society": ("2", "4"),
            "challenge": ("2", "hotspot_challenge"),
        }
        bt, bst = boards[board]
        d = self.get_signed(
            "/aweme/v1/web/hot/search/list/",
            {"detail_list": "1", "source": "6", "board_type": bt, "board_sub_type": bst},
            referer="https://www.douyin.com/hot",
            sign_uifid=False,
        )
        return d.get("data", {}).get("word_list", [])


def extract_id(value: str) -> str:
    m = re.search(r"(\d{15,25})", value)
    return m.group(1) if m else value.strip()


def summarize_aweme(ad: dict[str, Any]) -> dict[str, Any]:
    stats = ad.get("statistics") or {}
    author = ad.get("author") or {}
    mix = ad.get("mix_info") or {}
    video = ad.get("video") or {}
    cover_urls = (video.get("cover") or {}).get("url_list") or []
    bit_rates = video.get("bit_rate") or []
    best_url = ""
    best_res = ""
    if bit_rates:
        top = sorted(
            bit_rates,
            key=lambda b: (b.get("play_addr", {}).get("height", 0) * b.get("play_addr", {}).get("width", 0), b.get("bit_rate", 0)),
            reverse=True,
        )[0]
        pa = top.get("play_addr") or {}
        best_url = (pa.get("url_list") or [""])[0]
        best_res = f"{pa.get('width', 0)}x{pa.get('height', 0)}"
    if not best_url:
        pa = video.get("play_addr") or {}
        best_url = (pa.get("url_list") or [""])[0]
        best_res = f"{pa.get('width', 0)}x{pa.get('height', 0)}"
    ts = int(ad.get("create_time") or 0)
    dt_vn = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(timezone(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
    return {
        "aweme_id": str(ad.get("aweme_id") or ""),
        "desc": ad.get("desc") or "",
        "create_time": ts,
        "published_vn": dt_vn,
        "duration_sec": round((video.get("duration") or 0) / 1000.0, 2),
        "resolution": best_res,
        "author": author.get("nickname") or "",
        "sec_user_id": author.get("sec_uid") or "",
        "unique_id": author.get("unique_id") or author.get("short_id") or "",
        "mix_id": str(mix.get("mix_id") or ""),
        "mix_name": mix.get("mix_name") or "",
        "episode": ((mix.get("statis") or {}).get("current_episode")) or 0,
        "mix_total_eps": ((mix.get("statis") or {}).get("updated_to_episode")) or 0,
        "likes": int(stats.get("digg_count") or 0),
        "comments": int(stats.get("comment_count") or 0),
        "favorites": int(stats.get("collect_count") or 0),
        "shares": int(stats.get("share_count") or 0),
        "cover_url": cover_urls[0] if cover_urls else "",
        "video_url": best_url,
    }


def download_media(url: str, out_path: Path, is_image: bool = False) -> None:
    from curl_cffi import requests
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    try:
        r = requests.get(
            url,
            impersonate="chrome131",
            headers={"User-Agent": UA, "Referer": "https://www.douyin.com/"},
            timeout=30,
        )
        r.raise_for_status()
        tmp.write_bytes(r.content)
        if is_image and out_path.suffix.lower() in (".jpg", ".jpeg"):
            subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(tmp), "-q:v", "2", str(out_path)], check=True)
        else:
            tmp.replace(out_path)
    finally:
        tmp.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Douyin API & No-Watermark Downloader")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_det = sub.add_parser("detail", help="Inspect aweme detail & mix metadata")
    p_det.add_argument("target")
    p_det.add_argument("--cover-out", type=Path)
    p_det.add_argument("--json", action="store_true")

    p_dl = sub.add_parser("download", help="Download single aweme or full mix (no watermark)")
    p_dl.add_argument("target")
    p_dl.add_argument("--out", type=Path, required=True)
    p_dl.add_argument("--mix", action="store_true", help="Treat target as mix_id (or auto-download mix of video)")
    p_dl.add_argument("--limit", type=int, default=0)

    p_mix = sub.add_parser("mix", help="List episodes in a Douyin mix/collection")
    p_mix.add_argument("mix_id")
    p_mix.add_argument("--limit", type=int, default=50)
    p_mix.add_argument("--json", action="store_true")

    p_hot = sub.add_parser("hot", help="Fetch Douyin hot board")
    p_hot.add_argument("--board", default="hot", choices=["hot", "entertainment", "society", "challenge"])
    p_hot.add_argument("--json", action="store_true")

    args = ap.parse_args()
    client = DouyinClient()

    if args.cmd == "hot":
        words = client.hot(args.board)
        if args.json:
            print(json.dumps(words, ensure_ascii=False, indent=2))
        else:
            for w in words[:25]:
                print(f"{str(w.get('position') or 0):>2}. [{w.get('hot_value')}] {w.get('word')}")
        return 0

    if args.cmd == "detail":
        aid = extract_id(args.target)
        raw = client.detail(aid)
        ad = raw.get("aweme_detail")
        if not ad:
            print(json.dumps(raw, ensure_ascii=False)[:400], file=sys.stderr)
            return 1
        info = summarize_aweme(ad)
        if args.cover_out and info["cover_url"]:
            download_media(info["cover_url"], args.cover_out, is_image=True)
            info["cover_saved"] = str(args.cover_out)
        if args.json:
            print(json.dumps(info, ensure_ascii=False, indent=2))
        else:
            print(f"[{info['aweme_id']}] {info['desc']}")
            print(f"Author : {info['author']} ({info['sec_user_id']})")
            print(f"Time   : {info['published_vn']} | {info['duration_sec']}s | {info['resolution']}")
            print(f"Mix    : [{info['mix_id']}] {info['mix_name']} (ep {info['episode']}/{info['mix_total_eps']})")
            print(f"Stats  : likes={info['likes']} fav={info['favorites']} comments={info['comments']} shares={info['shares']}")
            print(f"Cover  : {info['cover_url'][:80]}...")
        return 0

    if args.cmd == "mix":
        mid = extract_id(args.mix_id)
        all_eps = []
        cursor = 0
        while True:
            res = client.mix_posts(mid, cursor=cursor, count=20)
            items = [summarize_aweme(a) for a in (res.get("aweme_list") or [])]
            all_eps.extend(items)
            if not res.get("has_more") or (args.limit and len(all_eps) >= args.limit):
                break
            cursor = int(res.get("cursor") or 0)
            time.sleep(0.8)
        if args.limit:
            all_eps = all_eps[: args.limit]
        if args.json:
            print(json.dumps({"mix_id": mid, "count": len(all_eps), "items": all_eps}, ensure_ascii=False, indent=2))
        else:
            print(f"=== Douyin Mix {mid}: {len(all_eps)} episodes ===")
            for it in all_eps:
                print(f"ep_{it['episode']:03d} [{it['aweme_id']}] {it['published_vn']} {it['duration_sec']}s likes={it['likes']} | {it['desc'][:50]}")
        return 0

    if args.cmd == "download":
        tid = extract_id(args.target)
        args.out.mkdir(parents=True, exist_ok=True)
        items = []
        if args.mix:
            cursor = 0
            while True:
                res = client.mix_posts(tid, cursor=cursor, count=20)
                items.extend(summarize_aweme(a) for a in (res.get("aweme_list") or []))
                if not res.get("has_more") or (args.limit and len(items) >= args.limit):
                    break
                cursor = int(res.get("cursor") or 0)
                time.sleep(0.8)
        else:
            ad = client.detail(tid).get("aweme_detail")
            if ad:
                items = [summarize_aweme(ad)]
        if args.limit:
            items = items[: args.limit]
        for idx, it in enumerate(items, start=1):
            fname = args.out / f"ep_{it['episode'] or idx:03d}_{it['aweme_id']}.mp4"
            download_media(it["video_url"], fname)
            probe = subprocess.run(
                [FFPROBE, "-v", "error", "-show_entries", "format=duration,size:stream=codec_name,width,height", "-of", "json", str(fname)],
                capture_output=True, text=True, check=False
            )
            print(f"OK {fname} ({fname.stat().st_size / 1024 / 1024:.2f} MB) | {probe.stdout.strip().replace(chr(10), ' ')}")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
