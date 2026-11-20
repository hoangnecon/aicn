# AICN Recursive Self-Improvement Ledger (`LESSONS.md`)

Structured log of verified production failures, user corrections, and API changes — each paired with the durable code/test/contract guardrail that prevents recurrence. See `references/recursive_self_improvement.md`.

---

## L-001 (2026-11-20) — `openhongguo` (`ohg v0.4.0`) Playback Returns HTTP 200 / 0 Bytes
- **Symptom:** `ohg play` / `ohg download` failed across all 5 tested series IDs with `ohg: anonymous response was empty` (`ErrAnonymousLimited`), even after `ohg device reset`.
- **Verified Root Cause:** Direct HTTP inspection (`debug_playback`) and `TestLiveContract` proved metadata endpoints worked, while ByteDance's origin returned `HTTP 200 OK` + `Content-Length: 0` on both legacy POST playback routes (`POST /novel/player/multi_video_model/player/v1` and `POST /novel/player/video_model/v1/`). Not a Vietnam IP block (`Via: oversea-VN-HCM-VIETTEL2` succeeded on the new route).
- **Guardrail Implemented (`v1.4.0`):**
  1. Permanently retired and deleted `openhongguo` (`~/go/bin/ohg`, config, Go cache).
  2. Adopted `jackotom/hongguo-drama` at `tools/hongguo-drama` (runtime deps only via `npm install --omit=dev --ignore-scripts`).
  3. Built `scripts/hg_tool.cjs` using `GET /reading/bookapi/video_tab/multi_video_model/v1/` (V5 bodyless GET signing) + local `spade_a` CENC-AES-CTR key derivation + `ffmpeg -decryption_key` decryption.
  4. Verified live on Ep 1 of `7696412960813485081` (1080p HEVC, 8.65 MB, 141.7s, clean `ffprobe`).

## L-002 (2026-11-20) — Candidate Lists Missing Vietnamese Translation, Synopsis & Thumbnails
- **Symptom:** User had to ask a follow-up for Vietnamese translated titles, basic synopses, and cover thumbnails when browsing candidate dramas.
- **Verified Root Cause:** No mandatory candidate presentation schema or automated cover fetcher existed in `aicn`.
- **Guardrail Implemented (`v1.4.0`):**
  1. Added Mandatory Candidate List Presentation Contract in `SKILL.md` and `references/sourcing_and_analytics.md` (requires VI title, ZH title, VI slug, `series_id`, genre/eps/recency, VI synopsis, and embedded local `.jpg` cover).
  2. Added `hg_tool.cjs fetch-covers` to download and auto-convert HEIC/WebP covers into standard JPEG thumbnails.

## L-003 (2026-11-20) — Trending Picks Skewed Toward Older / Stale Series
- **Symptom:** User noted that generic "trending" queries surfaced older series instead of fresh, newly trending releases.
- **Verified Root Cause:** Relying only on overall hot charts without querying launch-date-sorted new releases.
- **Guardrail Implemented (`v1.4.0`):**
  1. Added `hg_tool.cjs new --genre ai|real|all` backed by `hongguo-app-new.js` (`online_time` descending, preserving `红果首发` and launch date labels).
  2. Updated `references/sourcing_and_analytics.md` to require combining `new` (freshness) + `rank` (momentum) + duration/episode fit.

## L-004 (2026-11-19..20) — `cmd_dub` Boundary Broadcast Crash & Stage 5 H.264 Hardcoding
- **Symptom:** `ValueError: operands could not be broadcast together` at video end boundary in `cmd_dub`; `verify_stage_5.py` rejected valid HEVC renders.
- **Verified Root Cause:** Unclamped end index in audio placement array; hardcoded `codec_name == "h264"` check in Stage 5 verifier.
- **Guardrail Implemented (`v1.3.0`–`v1.3.2`):** Clamped `a = min(n, max(0, ...))` with `if b > a:` guard; updated `verify_stage_5.py` to accept both `h264` and `hevc`; verified via 32/32 unit tests.

## L-005 (2026-11-19) — Disk Exhaustion (`LibsndfileError`) During Multi-Batch Production
- **Symptom:** `soundfile.LibsndfileError: System error` during `cmd_mix` / `cmd_dub` when `/System/Volumes/Data` dropped below 2 GiB free.
- **Verified Root Cause:** Intermediate `premaster_48k.wav` and raw staging files accumulated across batches.
- **Guardrail Implemented (`v1.3.x`):** Run `aicn.py clean` on verified (`PPPPPP`) batches and keep >= 5–10 GiB free space; offload finished full-series deliverables to Drive when requested.
