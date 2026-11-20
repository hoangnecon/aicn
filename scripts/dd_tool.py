#!/usr/bin/env python3
"""
dd_tool.py — DramaDunyam (dramadunyam.com) English AI/Short-Drama Discovery & Downloader.

Uses public DramaDunyam JSON endpoints with the native `dd_bilet` guest session cookie
from `/api/config` (no login, no Turnstile/CAPTCHA bypass, clean stop on HTTP 402 locks).
Supports clean English (`lang=en`) and non-adult filtering across charts, lists, search,
series metadata, cover fetching, and episode direct-MP4/HLS download + ffprobe audit.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

BASE_URL = "https://dramadunyam.com"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
FFPROBE = "/opt/homebrew/bin/ffprobe" if Path("/opt/homebrew/bin/ffprobe").exists() else "ffprobe"
FFMPEG = "/opt/homebrew/bin/ffmpeg" if Path("/opt/homebrew/bin/ffmpeg").exists() else "ffmpeg"


class PaywallLockedError(RuntimeError):
    """Raised when an episode endpoint returns HTTP 402 VIP/coins paywall."""


def extract_tag_names(tags: Any) -> list[str]:
    """Normalize API tag lists of strings or `{slug, name}` objects into plain names."""
    out: list[str] = []
    for t in tags or []:
        if isinstance(t, dict):
            name = str(t.get("name") or t.get("slug") or "").strip()
            if name:
                out.append(name)
        elif str(t).strip():
            out.append(str(t).strip())
    return out


def filter_series_items(
    items: list[dict[str, Any]],
    lang: str = "en",
    exclude_adult: bool = True,
) -> list[dict[str, Any]]:
    """Filter items by adult flag (`yetiskin`) and optional explicit `language` match."""
    kept: list[dict[str, Any]] = []
    for it in items:
        if exclude_adult and bool(it.get("yetiskin")):
            continue
        item_lang = it.get("language")
        if lang and item_lang and str(item_lang).lower() != lang.lower():
            continue
        kept.append(it)
    return kept


def parse_episode_spec(spec: str, max_ep: int) -> list[int]:
    """Parse `'all'`, `'1-10'`, or `'1,3,5-8'` into sorted 1-based episode numbers."""
    if max_ep <= 0:
        raise ValueError(f"Invalid max_ep={max_ep}")
    raw = (spec or "all").strip().lower()
    if raw == "all":
        return list(range(1, max_ep + 1))
    eps: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a_s, b_s = part.split("-", 1)
            a, b = int(a_s), int(b_s)
            if a < 1 or b > max_ep or a > b:
                raise ValueError(f"Episode range {part!r} outside 1..{max_ep}")
            eps.update(range(a, b + 1))
        else:
            n = int(part)
            if n < 1 or n > max_ep:
                raise ValueError(f"Episode {n} outside 1..{max_ep}")
            eps.add(n)
    return sorted(eps)


def ffprobe_summary(path: Path) -> dict[str, Any]:
    """Return stream codecs, dimensions, fps, duration, and size for a downloaded media file."""
    proc = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(f"ffprobe failed on {path}: {proc.stderr[:240]}")
    data = json.loads(proc.stdout)
    fmt = data.get("format") or {}
    v_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    a_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
    dur = float(fmt.get("duration") or 0.0)
    size = int(fmt.get("size") or path.stat().st_size)
    return {
        "duration_sec": round(dur, 2),
        "size_mb": round(size / (1024 * 1024), 2),
        "v_codec": v_stream.get("codec_name", ""),
        "width": int(v_stream.get("width") or 0),
        "height": int(v_stream.get("height") or 0),
        "fps": v_stream.get("r_frame_rate", ""),
        "a_codec": a_stream.get("codec_name", ""),
        "sample_rate": int(a_stream.get("sample_rate") or 0),
    }


class DramaDunyamClient:
    """Stateless-cookie HTTP client for dramadunyam.com JSON & playback endpoints."""

    def __init__(self, base_url: str = BASE_URL, timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self.opener.addheaders = [
            ("User-Agent", UA),
            ("Accept", "application/json, text/plain, */*"),
            ("Referer", f"{self.base_url}/en"),
        ]
        self._initialized = False

    def init_session(self, force: bool = False) -> dict[str, Any]:
        """Call `/api/config` so the server natively issues the `dd_bilet` guest cookie."""
        if self._initialized and not force:
            return {}
        req = urllib.request.Request(f"{self.base_url}/api/config")
        with self.opener.open(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        self._initialized = True
        return payload

    def _get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        _cookie_retried: bool = False,
        _attempts: int = 4,
    ) -> Any:
        self.init_session(force=False)
        url = f"{self.base_url}{path}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None and v != ""}
            if clean:
                url = f"{url}?{urllib.parse.urlencode(clean)}"
        for attempt in range(_attempts):
            req = urllib.request.Request(url)
            try:
                with self.opener.open(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 412 and not _cookie_retried:
                    self.init_session(force=True)
                    return self._get_json(path, params=params, _cookie_retried=True, _attempts=_attempts)
                if exc.code == 402:
                    raise PaywallLockedError(f"Episode locked behind HTTP 402 paywall: {path}") from exc
                body = exc.read().decode("utf-8", errors="ignore")[:240]
                if exc.code in (429, 502, 503, 504) and attempt + 1 < _attempts:
                    wait_s = 1.5 * (2 ** attempt)
                    print(f"[dd] transient HTTP {exc.code} on {path}, retrying in {wait_s:.1f}s...", flush=True)
                    time.sleep(wait_s)
                    continue
                raise RuntimeError(f"HTTP {exc.code} on {url}: {body}") from exc

    def home(self, lang: str = "en") -> dict[str, Any]:
        return self._get_json("/api/home", {"lang": lang})

    def top(self, period: str = "hafta", lang: str = "en", exclude_adult: bool = True) -> dict[str, Any]:
        data = self._get_json("/api/siralama", {"donem": period, "lang": lang})
        items = filter_series_items(data.get("items") or [], lang=lang, exclude_adult=exclude_adult)
        return {"donem": data.get("donem", period), "count": len(items), "items": items}

    def list_series(
        self,
        sort_type: str = "populer",
        tag: str | None = None,
        page: int = 1,
        limit: int = 40,
        lang: str = "en",
        exclude_adult: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page, "limit": limit, "lang": lang}
        if tag:
            params["tag"] = tag
        else:
            params["type"] = sort_type
        data = self._get_json("/api/series", params)
        items = filter_series_items(data.get("data") or [], lang=lang, exclude_adult=exclude_adult)
        return {
            "count": len(items),
            "total": data.get("total", len(items)),
            "page": data.get("page", page),
            "pages": data.get("pages", 1),
            "items": items,
        }

    def search(
        self,
        query: str,
        page: int = 1,
        limit: int = 48,
        lang: str = "en",
        exclude_adult: bool = True,
    ) -> dict[str, Any]:
        data = self._get_json("/api/search", {"q": query, "page": page, "limit": limit, "lang": lang})
        items = filter_series_items(data.get("data") or [], lang=lang, exclude_adult=exclude_adult)
        return {"q": query, "count": len(items), "total": data.get("total", len(items)), "items": items}

    def detail(self, slug_or_id: str, lang: str = "en") -> dict[str, Any]:
        return self._get_json(f"/api/series/{urllib.parse.quote(slug_or_id)}", {"lang": lang})

    def play_info(self, series_id: str, ep: int) -> dict[str, Any]:
        return self._get_json(f"/play/{urllib.parse.quote(str(series_id))}/{ep}")

    def download_cover(self, detail: dict[str, Any], dest: Path) -> Path | None:
        variants = detail.get("cover_variants") or {}
        url = variants.get("1080") or variants.get("720") or variants.get("480") or detail.get("cover")
        if not url:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp_cover")
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": f"{self.base_url}/en"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp, open(tmp, "wb") as fh:
            shutil.copyfileobj(resp, fh)
        if dest.suffix.lower() in (".jpg", ".jpeg", ".png") and not url.lower().endswith(dest.suffix.lower()):
            conv = subprocess.run(
                [FFMPEG, "-y", "-v", "error", "-i", str(tmp), str(dest)],
                capture_output=True,
                text=True,
                check=False,
            )
            tmp.unlink(missing_ok=True)
            if conv.returncode != 0:
                raise RuntimeError(f"Failed converting cover to {dest}: {conv.stderr[:200]}")
        else:
            tmp.replace(dest)
        return dest

    def download_stream(self, url: str, dest: Path, delivery: str = "direct") -> dict[str, Any]:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part.mp4")
        if delivery == "hls" or ".m3u8" in url:
            cmd = [
                FFMPEG,
                "-y",
                "-v",
                "error",
                "-headers",
                f"Referer: {self.base_url}/\r\nUser-Agent: {UA}\r\n",
                "-i",
                url,
                "-c",
                "copy",
                "-bsf:a",
                "aac_adtstoasc",
                str(tmp),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if proc.returncode != 0:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(f"HLS download failed for {dest.name}: {proc.stderr[:240]}")
        else:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": f"{self.base_url}/en"})
            with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as fh:
                shutil.copyfileobj(resp, fh, length=256 * 1024)
        tmp.replace(dest)
        return ffprobe_summary(dest)


def cmd_top(client: DramaDunyamClient, args: argparse.Namespace) -> None:
    res = client.top(period=args.period, lang=args.lang, exclude_adult=not args.include_adult)
    res["items"] = res["items"][: args.limit]
    res["count"] = len(res["items"])
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return
    print(f"=== DramaDunyam Top ({args.period}, lang={args.lang}, {res['count']} items) ===")
    for i, it in enumerate(res["items"], 1):
        print(
            f"{i:02d}. [id:{it.get('id')}] ({it.get('platform','?')}, {it.get('available_episodes',0)}ep) "
            f"{it.get('title')} — slug: {it.get('slug')}"
        )


def cmd_list(client: DramaDunyamClient, args: argparse.Namespace) -> None:
    res = client.list_series(
        sort_type=args.type,
        tag=args.tag,
        page=args.page,
        limit=args.limit,
        lang=args.lang,
        exclude_adult=not args.include_adult,
    )
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return
    label = f"tag={args.tag}" if args.tag else f"type={args.type}"
    print(f"=== DramaDunyam List ({label}, page={res['page']}/{res['pages']}, {res['count']} items) ===")
    for i, it in enumerate(res["items"], 1):
        print(
            f"{i:02d}. [id:{it.get('id')}] ({it.get('platform','?')}, {it.get('available_episodes',0)}/"
            f"{it.get('total_episodes',0)}ep, lang={it.get('language','?')}) {it.get('title')} — {it.get('slug')}"
        )


def cmd_search(client: DramaDunyamClient, args: argparse.Namespace) -> None:
    res = client.search(args.query, page=args.page, limit=args.limit, lang=args.lang, exclude_adult=not args.include_adult)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return
    print(f"=== DramaDunyam Search '{args.query}' ({res['count']} items) ===")
    for i, it in enumerate(res["items"], 1):
        print(
            f"{i:02d}. [id:{it.get('id')}] ({it.get('platform','?')}, {it.get('available_episodes',0)}ep, "
            f"lang={it.get('language','?')}) {it.get('title')} — {it.get('slug')}"
        )


def cmd_detail(client: DramaDunyamClient, args: argparse.Namespace) -> None:
    det = client.detail(args.slug, lang=args.lang)
    if args.json:
        print(json.dumps(det, ensure_ascii=False, indent=2))
        return
    tags = ", ".join(extract_tag_names(det.get("tags")))
    print(f"Title    : {det.get('title')} ({det.get('language')})")
    print(f"ID / Slug: {det.get('id')} / {det.get('slug')}")
    print(f"Platform : {det.get('platform')} | Genre: {det.get('genre')}")
    print(f"Episodes : {det.get('available_episodes')}/{det.get('total_episodes')}")
    print(f"Stats    : views={det.get('view_count', det.get('views', 0))}, likes={det.get('like_count', 0)}")
    print(f"Tags     : {tags}")
    print(f"Synopsis : {det.get('description', '')}")


def cmd_download(client: DramaDunyamClient, args: argparse.Namespace) -> None:
    det = client.detail(args.slug, lang=args.lang)
    series_id = str(det.get("id") or "")
    max_ep = int(det.get("available_episodes") or det.get("total_episodes") or 0)
    if not series_id or max_ep <= 0:
        raise RuntimeError(f"Could not resolve playable episodes for {args.slug}: id={series_id}, max_ep={max_ep}")

    episodes = parse_episode_spec(args.episodes, max_ep)
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    cover_saved: Path | None = None
    if args.cover_out:
        cover_saved = client.download_cover(det, Path(args.cover_out).resolve())
        print(f"[dd] Saved cover -> {cover_saved}", flush=True)

    manifest: list[dict[str, Any]] = []
    skipped_existing = 0
    locked_eps: list[int] = []
    t0 = time.time()

    print(
        f"[dd] Downloading {det.get('title')} (id={series_id}, {len(episodes)}/{max_ep} eps) -> {out_dir}",
        flush=True,
    )
    for idx, ep in enumerate(episodes, 1):
        dest = out_dir / f"ep{ep:02d}.mp4"
        if dest.exists() and dest.stat().st_size > 10_000 and not args.force:
            try:
                probe = ffprobe_summary(dest)
                if probe["duration_sec"] > 0 and probe["width"] > 0:
                    skipped_existing += 1
                    manifest.append({"ep": ep, "file": str(dest), "status": "existing", **probe})
                    print(
                        f"[dd] [{idx}/{len(episodes)}] ep{ep:02d}.mp4 already valid "
                        f"({probe['duration_sec']}s, {probe['width']}x{probe['height']}, {probe['size_mb']} MB)",
                        flush=True,
                    )
                    continue
            except Exception:
                pass

        try:
            play = client.play_info(series_id, ep)
        except PaywallLockedError as exc:
            locked_eps.append(ep)
            print(f"[dd] [{idx}/{len(episodes)}] ep{ep:02d} LOCKED (HTTP 402): {exc}", flush=True)
            if not args.skip_locked:
                raise
            continue

        stream_url = str(play.get("url") or "").strip()
        delivery = str(play.get("delivery") or "direct").strip()
        if not stream_url:
            stream_url = f"{client.base_url}/hls/{series_id}/{ep}/playlist.m3u8"
            delivery = "hls"

        probe = client.download_stream(stream_url, dest, delivery=delivery)
        manifest.append({"ep": ep, "file": str(dest), "status": "downloaded", "delivery": delivery, **probe})
        print(
            f"[dd] [{idx}/{len(episodes)}] ep{ep:02d}.mp4 ok "
            f"({probe['duration_sec']}s, {probe['width']}x{probe['height']} {probe['v_codec']}/{probe['a_codec']}, "
            f"{probe['size_mb']} MB)",
            flush=True,
        )
        if args.delay > 0 and idx < len(episodes):
            time.sleep(args.delay)

    elapsed = round(time.time() - t0, 1)
    summary = {
        "series_id": series_id,
        "slug": det.get("slug"),
        "title": det.get("title"),
        "language": det.get("language"),
        "platform": det.get("platform"),
        "requested": len(episodes),
        "completed": len(manifest),
        "skipped_existing": skipped_existing,
        "locked_episodes": locked_eps,
        "cover": str(cover_saved) if cover_saved else None,
        "elapsed_sec": elapsed,
        "episodes": manifest,
    }
    manifest_path = out_dir / "_dd_manifest.json"
    manifest_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"[dd] Complete: {len(manifest)}/{len(episodes)} episodes ready in {elapsed}s -> {manifest_path}",
        flush=True,
    )
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="DramaDunyam discovery, metadata & direct MP4 downloader")
    sub = p.add_subparsers(dest="cmd", required=True)

    s_top = sub.add_parser("top", help="Fetch ranked rankings (/api/siralama)")
    s_top.add_argument("--period", default="hafta", choices=["gun", "hafta", "ay"])
    s_top.add_argument("--lang", default="en")
    s_top.add_argument("--limit", type=int, default=30)
    s_top.add_argument("--include-adult", action="store_true")
    s_top.add_argument("--json", action="store_true")

    s_list = sub.add_parser("list", help="Browse /api/series by sort type or tag")
    s_list.add_argument("--type", default="populer", choices=["populer", "yeni", "all"])
    s_list.add_argument("--tag", default="", help="Optional tag slug, e.g. ai-short-dramas")
    s_list.add_argument("--page", type=int, default=1)
    s_list.add_argument("--limit", type=int, default=40)
    s_list.add_argument("--lang", default="en")
    s_list.add_argument("--include-adult", action="store_true")
    s_list.add_argument("--json", action="store_true")

    s_search = sub.add_parser("search", help="Search /api/search")
    s_search.add_argument("query", help="Search keywords")
    s_search.add_argument("--page", type=int, default=1)
    s_search.add_argument("--limit", type=int, default=48)
    s_search.add_argument("--lang", default="en")
    s_search.add_argument("--include-adult", action="store_true")
    s_search.add_argument("--json", action="store_true")

    s_det = sub.add_parser("detail", help="Fetch full series metadata (/api/series/<slug>)")
    s_det.add_argument("slug", help="Series slug (e.g. deadliest-prison-the-female-warden-is-my-plaything)")
    s_det.add_argument("--lang", default="en")
    s_det.add_argument("--json", action="store_true")

    s_dl = sub.add_parser("download", help="Download series episodes + optional cover")
    s_dl.add_argument("slug", help="Series slug")
    s_dl.add_argument("--out", required=True, help="Output directory for ep01.mp4..epNN.mp4")
    s_dl.add_argument("--episodes", default="all", help="'all', '1-10', or '1,3,5-8'")
    s_dl.add_argument("--cover-out", default="", help="Optional destination path for series cover image")
    s_dl.add_argument("--lang", default="en")
    s_dl.add_argument("--delay", type=float, default=0.15, help="Polite delay (seconds) between episodes")
    s_dl.add_argument("--force", action="store_true", help="Re-download existing valid files")
    s_dl.add_argument("--skip-locked", action="store_true", help="Skip HTTP 402 episodes instead of failing")
    s_dl.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    client = DramaDunyamClient()
    if args.cmd == "top":
        cmd_top(client, args)
    elif args.cmd == "list":
        cmd_list(client, args)
    elif args.cmd == "search":
        cmd_search(client, args)
    elif args.cmd == "detail":
        cmd_detail(client, args)
    elif args.cmd == "download":
        cmd_download(client, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
