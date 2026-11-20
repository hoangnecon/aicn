# Sourcing, Discovery Analytics & Candidate Presentation Contract

Primary Chinese short-drama sourcing engine: **`jackotom/hongguo-drama`** (installed at `<project-root>/tools/hongguo-drama`, driven by `scripts/hg_tool.cjs`).
> **Retired tool:** `openhongguo` (`ohg v0.4.0`) is permanently retired and deleted because its playback POST endpoints (`/novel/player/multi_video_model/player/v1` and `/novel/player/video_model/v1/`) return HTTP 200 / 0 bytes (`ErrAnonymousLimited`). Do **not** reinstall or reference `ohg`.

---

## 1. Setup & Primary Engine (`tools/hongguo-drama`)

```bash
# One-time setup (runtime deps only — skips 300 MB Electron devDependencies)
git clone --depth 1 https://github.com/jackotom/hongguo-drama tools/hongguo-drama
cd tools/hongguo-drama && npm install --omit=dev --ignore-scripts
```

All discovery, analytics, cover fetching, and 1080p CENC-decrypted downloads run through:
```bash
node <skill>/scripts/hg_tool.cjs <command> [options]
```

### Core CLI Commands

| Goal | Command |
|---|---|
| **Trending charts (by format)** | `node <skill>/scripts/hg_tool.cjs rank --category hot-ai-drama\|hot-real-drama\|hot-comic-drama\|hot-drama [--page 1] [--json]` |
| **Newest releases (freshness-first)** | `node <skill>/scripts/hg_tool.cjs new [--genre all\|ai\|real] [--pages 2] [--limit 20] [--json]` |
| **Keyword search** | `node <skill>/scripts/hg_tool.cjs search "<keyword>" [--page 1] [--json]` |
| **Inspect episodes & IDs** | `node <skill>/scripts/hg_tool.cjs episodes <series_id> [--json]` |
| **Download 1080p + CENC decrypt** | `node <skill>/scripts/hg_tool.cjs download <series_id> --out <dir> [--episodes 1-10\|all] [--quality 1080p\|auto] [--delay-ms 1500]` |
| **Download & convert JPEG covers** | `node <skill>/scripts/hg_tool.cjs fetch-covers --input <catalog.json> --out <covers_dir>` |

### How Playback & CENC Decryption Works Under the Hood
1. `fetchAppPlayUrl()` registers an anonymous device and queries `POST /novel/player/multi_video_model/player/v1`; on `EMPTY_RESPONSE` (HTTP 200 / 0 bytes), it automatically falls back to **`GET /reading/bookapi/video_tab/multi_video_model/v1/`** signed with V5 bodyless headers (`x-khronos`, `x-helios`, `x-medusa`, no `x-ss-stub`).
2. Selects the highest compatible stream (`1080p` HEVC/`bytevc1` or H.264 — avoiding unsupported `bytevc2` unless unavoidable).
3. Derives the 16-byte AES-128-CTR content key locally from `encrypt_info.spade_a` via `deriveKey()`, downloads the CDN MP4, and decrypts cleanly with `ffmpeg -decryption_key <hex> -i raw.mp4 -c copy out.mp4` (validated via `ffprobe`).

---

## 2. Discovery & Analytics Workflow (How to Pick High-Performing Series)

Never pick candidates from a single static list or old viral hits. Combine **freshness**, **chart momentum**, and **pipeline fit**:

1. **Pull Fresh Releases FIRST (`hg_tool.cjs new`):**
   - Run `hg_tool.cjs new --genre ai --pages 2 --limit 25 --json` and `hg_tool.cjs new --genre real --pages 2 --limit 25 --json`.
   - Prioritize titles tagged `红果首发` or with recent launch dates (`上线日期` within the last 1–30 days) so the user never gets stale/old series.
2. **Cross-Check Live Charts (`hg_tool.cjs rank`):**
   - `hot-ai-drama` (`AI仿真人剧` — photorealistic AI human dramas; ~42k avg YouTube views/series vs ~5.8k for 2D motion comics).
   - `hot-real-drama` (`真人剧` — live-action comedy/revenge; ~96k avg YouTube views/series).
   - `hot-comic-drama` (`3D漫剧` — 3D AI animation; avoid static 2D / sand-sculpture `沙雕漫`).
3. **External Industry Chart Cross-Check (Optional):**
   - Verify monthly heat & breakout velocity on Chinese portals: `tahou.com`, `duanjugongcheng.com`, `thepaper.cn`, `baijing.cn`.
4. **Filter by Pipeline Fit & Retention Mechanics:**
   - **Episode count & runtime sweet spot:** 50–120 episodes (~60–150 min total) fits a clean 1-video full-series master without bloated 500-ep marathons.
   - **High-retention hooks:** Strong opening 20% hook (transmigration, time-travel family comedy, hidden identity reversal, prodigy kid, system gag) where 50% of YouTube replay peaks cluster.
   - **Verify playable episodes:** Run `hg_tool.cjs episodes <series_id>` to confirm episode count and valid `video_id`s before pitching.

### Secondary Chinese & International Open-Source Sources (Backup Stack)
- **Bundled inside `tools/hongguo-drama/src/native/`:** `hema.js` (`河马剧场`), `xifan.js` (`喜番`), `dramabox.js` (DramaBox), `reelshort.js` (ReelShort).
- **Bilibili (`B站`):** `yutto` (`yutto-dev/yutto`) or `yt-dlp --user-agent`.
- **Douyin (`抖音`):** `Evil0ctal/Douyin_TikTok_Download_API` or `JoeanAmier/TikTokDownloader` (requires fresh browser cookies).
- **Kuaishou (`快手`):** `JoeanAmier/KS-Downloader`.

---

## 3. Mandatory Candidate List Presentation Contract

Whenever the user asks to browse, find, or recommend dramas (e.g., "liệt kê 5 bộ trending..."), **EVERY** candidate entry presented MUST include all 5 items below — no exceptions:

1. **Tên tiếng Việt (Vietnamese Translated Title)** + **Tên gốc tiếng Trung (`ZH`)** + **`--series-slug`** chuẩn (tiếng Việt không dấu, viết thường, nối bằng gạch ngang).
2. **Metadata đo thực tế:** `series_id`, định dạng (`AI仿真人剧` / `3D漫剧` / `真人喜剧`), số tập (`N tập`), thời lượng dự kiến, và **tín hiệu xu hướng / độ mới** (Top mấy bảng nào hoặc ngày lên sóng `红果首发`).
3. **Nội dung cơ bản (Vietnamese Synopsis):** 2–4 câu tóm tắt tiếng Việt rõ ràng về bối cảnh, nhân vật chính, tuyến hài/drama và điểm hút mở đầu.
4. **Ảnh bìa Thumbnail `.jpg` hiển thị trực tiếp:**
   - Tải bằng `hg_tool.cjs fetch-covers --input <catalog.json> --out <artifact_dir>/covers/` (tự động chuyển `.heic`/`.webp` sang `.jpg` chuẩn).
   - Nhúng trực tiếp ảnh vào artifact danh sách ứng viên (`![Tên phim](/abs/path/to/covers/1_slug.jpg)`) và gửi link artifact cho người dùng xem ngay.
5. **Trạng thái kiểm chứng tải (khi cần):** Đánh dấu rõ đã verify `episodes` hay chưa, không tự suy đoán.
