---
name: aicn-scout
description: Use when the user asks to find, browse, rank, search, or pitch new or trending Chinese short dramas, AI dramas, or English short dramas ("tìm phim mới", "săn trend Hồng Quả", "đề cử bộ mới", "check bảng xếp hạng"), or needs a candidate shortlist with covers before downloading.
---

# Scouting & Pitching Original Short Dramas (`/aicn-scout`)

## Overview
Source fresh, high-retention Chinese (`红果` Hongguo, `抖音` Douyin, `B站` Bilibili) and clean English (`DramaDunyam`) short dramas directly from original platforms — never from YouTube re-ups — and present a verified Vietnamese shortlist with local cover thumbnails.

- `<aicn>`: `$AICN_HOME` if set, else `~/.gemini/config/plugins/aicn` (`../..` from this skill).
- `$AICN_PY`: `C:\Work\Tools\VieNeu-TTS\.venv\Scripts\python.exe` (Windows) or `/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python` (macOS).
- Deep reference: `<aicn>/references/sourcing_and_analytics.md`.

## Mandatory Recipe

1. **Verify upstream Hongguo SDK first (never hardcode private routes):**
   ```bash
   node <aicn>/scripts/hg_tool.cjs check-upstream --json
   ```
   Require `up_to_date: true` and `wrapper_contract_ok: true`. If `up_to_date` is `false`, run `node <aicn>/scripts/hg_tool.cjs check-upstream --sync` first.

2. **Query local index + live freshness-first feeds in Chinese (`中文`) or English (`en`):**
   ```bash
   $AICN_PY <aicn>/scripts/aicn_radar.py top --format ai --limit 15
   node <aicn>/scripts/hg_tool.cjs new --genre ai
   node <aicn>/scripts/hg_tool.cjs rank --category hot-ai-drama
   $AICN_PY <aicn>/scripts/dd_tool.py top --lang en
   ```
   For Douyin, use `<dy_py> <aicn>/scripts/dy_tool.py detail`; for Bilibili, use `yt-dlp "bilisearch10:<中文关键词>"`. Filter out stale releases when the user asks for new/trending titles.

3. **Download local `.jpg` covers and write a Candidate Shortlist Artifact:**
   Fetch covers via `node <aicn>/scripts/hg_tool.cjs fetch-covers`, `dy_tool.py detail --cover-out`, or `dd_tool.py download --cover-out` into the session artifact `covers/` folder. Every candidate entry MUST contain:
   - **Vietnamese translated title** + original source title (`ZH`/`EN`) + valid unaccented `--series-slug` (`^[a-z0-9]+(-[a-z0-9]+)*$`)
   - **Platform & ID** (`hongguo`/`douyin`/`bilibili`/`dramadunyam`, `series_id`), genre/format, episode count, publish date/age, heat/likes
   - **Vietnamese synopsis** (2–4 sentences: hook, conflict/comedy beat, main cast)
   - **Embedded local `.jpg` cover** (`![Title](/absolute/artifact/path/covers/<slug>.jpg)`)

## Rationalizations & Red Flags

| Excuse | Reality |
|---|---|
| "Search YouTube quickly for Chinese AI drama compilations" | Forbidden. YouTube hosts cropped recaps/re-ups with burned logos and degraded audio. |
| "Skip `check-upstream` since `hg_tool.cjs` worked yesterday" | Upstream Hongguo signing headers rotate; always verify `check-upstream --json` first. |
| "List titles without downloading cover thumbnails" | Every shortlist requires local `.jpg` covers embedded in an artifact so the user can visually pick. |

**Next step once user picks a candidate:** Invoke `aicn-create`.
