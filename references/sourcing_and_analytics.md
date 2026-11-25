# Sourcing, Discovery Analytics & Candidate Presentation Contract (`v3.0.0`)

Primary Chinese short-drama sourcing engine: **`jackotom/hongguo-drama`** (installed at `<project-root>/tools/hongguo-drama`, wrapped dynamically by `scripts/hg_tool.cjs`).

---

## 1. Thin-Wrapper Contract & Mandatory Upstream Preflight (`tools/hongguo-drama`)

> **Mandatory Rule (Zero Hardcoding — Always Check Upstream Latest Release First):**
> `scripts/hg_tool.cjs` is strictly a **thin dynamic wrapper** around `jackotom/hongguo-drama`. Never hardcode Hongguo private API paths, request signing headers, or `phone_account` SDK internals inside `aicn` or ad-hoc scripts.
>
> **Before EVERY Hongguo discovery, metadata lookup, or download session**, run:
> ```bash
> node <skill>/scripts/hg_tool.cjs check-upstream --json
> ```
> 1. **Check release currency:** Verify `up_to_date: true` against the latest GitHub release of `jackotom/hongguo-drama`. If `up_to_date` is `false` (or `installed: false`), run:
>    ```bash
>    node <skill>/scripts/hg_tool.cjs check-upstream --sync
>    ```
>    This fetches and checks out the latest release tag (`--depth 1`) and installs runtime dependencies (`npm install --omit=dev --ignore-scripts`).
> 2. **Inspect live API surface:** Review `release_highlights`, `api_surface`, `sdk_cli` (`script`/`binary`/`capabilities`), and `wrapper_contract_ok`.
> 3. **Adapt wrapper if upstream changed:** If `wrapper_contract_ok` is `false`, inspect `tools/hongguo-drama/src/native/` and update `scripts/hg_tool.cjs` to match upstream — never bypass upstream by hardcoding private endpoints.

All Hongguo discovery, analytics, metadata, cover fetching, and 1080p CENC-decrypted downloads run through:
```bash
node <skill>/scripts/hg_tool.cjs <command> [options]
```

### Core CLI Commands

| Goal | Command |
|---|---|
| **Mandatory upstream release & API check** | `node <skill>/scripts/hg_tool.cjs check-upstream [--sync] [--strict] [--json]` |
| **Trending charts (by format)** | `node <skill>/scripts/hg_tool.cjs rank --category hot-ai-drama\|hot-real-drama\|hot-comic-drama\|hot-drama [--page 1] [--json]` |
| **Newest releases (freshness-first)** | `node <skill>/scripts/hg_tool.cjs new [--genre all\|ai\|real] [--pages 2] [--limit 20] [--json]` |
| **Keyword search** | `node <skill>/scripts/hg_tool.cjs search "<keyword>" [--page 1] [--json]` |
| **Public details & engagement metrics** | `node <skill>/scripts/hg_tool.cjs details <series_id...> [--vid <vid>] [--json]` |
| **Creator album (`content_type=1001`)** | `node <skill>/scripts/hg_tool.cjs album <album_id> [--json]` |
| **Inspect episodes & IDs** | `node <skill>/scripts/hg_tool.cjs episodes <series_id> [--json]` |
| **Download 1080p + CENC decrypt** | `node <skill>/scripts/hg_tool.cjs download <series_id> --out <dir> [--episodes 1-10\|all] [--quality 1080p\|auto] [--content-type 1004\|1001] [--delay-ms 1500]` |
| **Download & convert JPEG covers** | `node <skill>/scripts/hg_tool.cjs fetch-covers --input <catalog.json> --out <covers_dir>` |

### How Playback & CENC Decryption Delegates to Upstream
1. **Primary (Upstream Official Entry First):** Calls upstream `app-source.js` `fetchAppPlayUrl(vid, { quality, allowLowerQuality: true, contentType })`.
2. **Fallback (Upstream SDK CLI):** Only if `fetchAppPlayUrl()` fails (or `--prefer-sdk` is passed), `hg_tool.cjs` lazily delegates to upstream's compiled binary or `scripts/sdk-playback/resolve_cli.py --serve` (cross-platform locks, persistent JSONL reuse, up to 8-episode batching, `contentType` `1004`/`1001` separation).
3. **Optional Web Fallback:** Disabled by default so 1080p jobs never silently downgrade to 360p; opt-in only via `--allow-web-fallback`.
4. **CENC Decryption:** Derives the 16-byte AES-128-CTR content key via upstream `deriveKey()` (or uses upstream `contentKey`), fetches with 30s timeout and `try/finally` temp cleanup, decrypts cleanly with `ffmpeg -decryption_key <hex> -i raw.mp4 -c copy out.mp4`, and validates stream duration (`> 1s`) via `ffprobe`.

---

## 2. Discovery & Analytics Workflow (How to Pick High-Performing Series)

Never pick candidates from a single static list or old viral hits. Combine **freshness**, **chart momentum**, and **pipeline fit**:

1. **Pull Fresh Releases FIRST (`hg_tool.cjs new`):**
   - Run `hg_tool.cjs new --genre ai --pages 2 --limit 25 --json` and `hg_tool.cjs new --genre real --pages 2 --limit 25 --json`.
   - Prioritize titles tagged `红果首发` or with recent launch dates (`上线日期` within the last 1–30 days).
2. **Cross-Check Live Charts (`hg_tool.cjs rank`):**
   - `hot-ai-drama` (`AI仿真人剧` — photorealistic AI human dramas).
   - `hot-real-drama` (`真人剧` — live-action comedy/revenge).
   - `hot-comic-drama` (`3D漫剧` — 3D AI animation; avoid static 2D / `沙雕漫`).
3. **External Industry Chart Cross-Check (Optional):**
   - Check monthly heat & breakout velocity on Chinese portals (`tahou.com`, `duanjugongcheng.com`, `thepaper.cn`, `baijing.cn`).
4. **Filter by Pipeline Fit & Retention Mechanics:**
   - **Episode count & runtime sweet spot:** 50–135 episodes (~60–150 min total) fits a clean 1-video full-series master.
   - **High-retention hooks:** Strong opening 20% hook (transmigration, time-travel family comedy, hidden identity reversal, prodigy kid, system gag).
   - **Verify playable episodes:** Run `hg_tool.cjs episodes <series_id>` to confirm episode count and valid `video_id`s before pitching.

### Secondary & Multi-Platform Original Sources (`dy_tool.py`, `dd_tool.py`, `aicn_radar.py`)

> **Mandatory Origin Rule (NO YouTube):** Never search, pitch, or download Chinese dramas from YouTube (`YouTube` is downstream recap/re-up). Always start with **Chinese keywords (`中文`)** and extract directly from original Chinese platforms.

#### Douyin (`抖音`) — `scripts/dy_tool.py`
Powered by `Evil0ctal/Douyin_TikTok_Download_API` (`tools\.venv-dy\Scripts\python.exe` on Windows, `tools/.venv-dy/bin/python` on macOS/Linux; no Douyin login needed for `detail`/`mix`/`hot`/`related_posts`):
- **Inspect video + author + stats + `.jpg` cover:** `<dy_py> <skill>/scripts/dy_tool.py detail <aweme_id_or_url> --cover-out <path.jpg>`
- **Download watermark-free MP4 (supports paginated `--mix`, Windows-safe atomic `replace`):** `<dy_py> <skill>/scripts/dy_tool.py download <aweme_id_or_mix_id> [--mix] --out <dir>`
- **Inspect collection (`合集`):** `<dy_py> <skill>/scripts/dy_tool.py mix <mix_id>`
- **Douyin Hot Boards:** `<dy_py> <skill>/scripts/dy_tool.py hot --board hot|entertainment|challenge`

#### DramaDunyam English AI/Short-Drama Source (`dramadunyam.com`) — `scripts/dd_tool.py`
Pure-Python stdlib client using `/api/config` guest cookie (`dd_bilet`, HTTP 412 auto-refresh, clean stop on HTTP 402 paywalls — zero CAPTCHA/paywall bypassing). Strictly filters `language="en"` and `yetiskin != true`:
- **Rankings / Browse / Search / Detail:** `$AICN_PY <skill>/scripts/dd_tool.py top|list|search|detail ... --lang en`
- **Download MP4s + `.jpg` cover:** `$AICN_PY <skill>/scripts/dd_tool.py download <slug> --episodes all --out <inbox_raw_dir> --cover-out <posters/cover.jpg>`
- *Note:* When creating a series hub from an English source, pass `--source-lang en` to `aicn.py init-series` so Stage 1 Whisper transcribes English cleanly.

#### Unified Analytics & Search Index — `scripts/aicn_radar.py`
Stores Hongguo, Douyin, Bilibili (`ap.find_tool("yt-dlp")`), and DramaDunyam into `data/drama_index.db` (SQLite FTS5 with `busy_timeout=15000`) scored by recency-weighted velocity (`heat_score`):
```bash
$AICN_PY <skill>/scripts/aicn_radar.py sync-all
$AICN_PY <skill>/scripts/aicn_radar.py top --platform dramadunyam --limit 15
$AICN_PY <skill>/scripts/aicn_radar.py search "<keywords>" --platform all|hongguo|douyin|bilibili|dramadunyam --sort newest|score|likes
```

#### Other Original Chinese Platforms
- **Bilibili (`B站`):** `yutto` or `yt-dlp --user-agent ...` (direct 1080p MP4).
- **Bundled in `tools/hongguo-drama/src/native/`:** `hema.js` (`河马剧场`), `xifan.js` (`喜番`).
- **Kuaishou (`快手`):** `JoeanAmier/KS-Downloader`.

---

## 3. Mandatory Candidate List Presentation Contract

Whenever the user asks to browse, find, or recommend dramas, **EVERY** candidate entry presented MUST include all 5 items below — no exceptions:

1. **Tên tiếng Việt (Vietnamese Translated Title)** + **Tên gốc tiếng Trung (`ZH`)** + **`--series-slug`** chuẩn (tiếng Việt không dấu, viết thường, nối bằng gạch ngang).
2. **Metadata đo thực tế:** `series_id`, định dạng (`AI仿真人剧` / `3D漫剧` / `真人喜剧`), số tập (`N tập`), thời lượng dự kiến, và **tín hiệu xu hướng / độ mới** (Top mấy bảng nào hoặc ngày lên sóng `红果首发`).
3. **Nội dung cơ bản (Vietnamese Synopsis):** 2–4 câu tóm tắt tiếng Việt rõ ràng về bối cảnh, nhân vật chính, tuyến hài/drama và điểm hút mở đầu.
4. **Ảnh bìa Thumbnail `.jpg` hiển thị trực tiếp:**
   - Tải bằng `hg_tool.cjs fetch-covers --input <catalog.json> --out <artifact_dir>/covers/` (tự động chuyển `.heic`/`.webp` sang `.jpg` chuẩn).
   - Nhúng trực tiếp ảnh vào artifact danh sách ứng viên (`![Tên phim](/abs/path/to/covers/1_slug.jpg)`) và gửi link artifact cho người dùng xem ngay.
5. **Trạng thái kiểm chứng tải (khi cần):** Đánh dấu rõ đã verify `episodes` hay chưa, không tự suy đoán.
