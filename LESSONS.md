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

## L-006 (2026-11-20) — Sourcing Chinese Dramas Must Never Touch YouTube; Search & Index Original CN Sources Only
- **Symptom:** Initial searches on `Săn bắn thập niên 70` and `2 Con Mèo Biến Thành Mỹ Nữ` surfaced YouTube review/re-up channels (`Mễ Cốc Review`, etc.), and even Bilibili had multiple MCN mirrors (`小律AIGC` vs `小映漫剧`) of a video first published on Douyin.
- **Verified Root Cause:** YouTube uploads are downstream recaps/re-ups; Bilibili AIGC channels sometimes cross-post Douyin originals days later. Without Chinese-first discovery across Hongguo + Douyin + Bilibili + a unified analytics index, discovery wastes time and risks picking stale/re-uploaded sources.
- **Guardrail Implemented (`v1.4.1`):**
  1. Added hard rule banning YouTube as a Chinese drama source; all Chinese drama searches start with Chinese keywords across Hongguo, Douyin, Bilibili, Hema, and Kuaishou.
  2. Installed `Evil0ctal/Douyin_TikTok_Download_API` at `tools/Douyin_TikTok_Download_API` + lightweight `tools/.venv-dy` (`wreq`, `curl_cffi`, `playwright`).
  3. Built `scripts/dy_tool.py` using pure-Python `ABogus` + `websign` (`x-secsdk-web-signature`) + Chrome guest `UIFID_TEMP` minting — verified live fetching no-watermark 1080p MP4 from Douyin (`7707844707132476726`, 17.71 MB) and 2-hop `related_posts` AI-drama graph discovery.
  4. Built unified SQLite FTS5 analytics & search index `scripts/aicn_radar.py` (`data/drama_index.db`) indexing Hongguo (`new` + `rank` + `first_visible_time` + `likes/favorites`), Douyin (`detail` + `related_posts` + `hot`), and Bilibili (`bilisearch`).

## L-007 (2026-11-20) — Facebook Upload Metadata Requires Single 2-Line Format & Lowercase Broad Hashtags
- **Symptom:** User rejected separate YouTube-style Title/Description/Tags blocks and niche calque hashtags (`#phimhaikhoahuyen`) when posting to Facebook.
- **Verified Root Cause:** Facebook only has one Description box; user prefers a punchy `[TRỌN BỘ THUYẾT MINH] ... | Full N Tập` hook line followed immediately by all-lowercase broad tags (`#aishortfilmdrama #aishortdrama #aifilm #phimai ...`).
- **Guardrail Implemented (`v1.4.1`):** Codified the 2-line Facebook caption format and lowercase hashtag policy in `SKILL.md` and `references/sourcing_and_analytics.md`.

## L-008 (2026-11-20) — Mixed Bilibili + Douyin Episode Stream Concat (`Non-monotonic DTS` & Ghost Duration)
- **Symptom:** Concatenating Bilibili `ep01..ep04` with Douyin `ep05` in `init_workspace.py` emitted `Non-monotonic DTS` and produced `master_source.mp4` reporting `duration=1843.97s` (when total episode runtime was `669.01s`).
- **Verified Root Cause:** `init_workspace.py` checked only `(width, height, codec_name, sample_rate, channels)`, missing three fatal stream mismatches between platforms: Douyin puts `audio` at stream index `0` and `video` at `1` (reversed from Bilibili), uses `HE-AACv2` instead of `AAC-LC`, and uses `time_base=1/15360` instead of `1/16000`.
- **Guardrail Implemented (`v1.4.2`):** Updated `init_workspace.py` `specs` tuple to include `(v_profile, v_index, v_time_base, a_codec, a_profile)` plus post-concat duration verification (`abs(master_dur - sum(ep_durs))`), normalizing mixed batches with `-map 0:v:0 -map 0:a:0 -video_track_timescale 15360`.

## L-009 (2026-11-20) — English `720×1280` 9:16 Portrait Pipeline Adaptation & DramaDunyam Integration
- **Symptom:** Overseas English short-drama sources (e.g. DramaDunyam `332020`, `720×1280` 9:16 portrait with English audio and white bottom hardsubs at `y=919..958`) hit four latent landscape-Chinese assumptions in `aicn`:
  1. `run_asr_and_scan.py` hardcoded `xc = 960` and `y_bot = 86` (`rel_y <= 86`), dropping/clipping all centered subtitles on `720w` frames, plus hardcoded `language="zh"` in both Whisper passes.
  2. `aicn.py` Style-B defaults (`font_size=48`, `max_line_px=1500`) and dual-corner PNGs (`284×142` + `295×70`) overflowed or bloated `720w` portrait frames and triggered `verify-overlay` `line_px > W - 120` failures.
- **Guardrail Implemented (`v1.5.0`):**
  1. Added pure-Python `scripts/dd_tool.py` + `aicn_radar.py sync-dramadunyam` filtering clean `lang=en` and non-adult (`yetiskin != true`), auto-handling `/api/config` `dd_bilet` cookies and HTTP 412 refresh while stopping cleanly on HTTP 402 paywalls.
  2. Adapted `run_asr_and_scan.py` (`find_hardsub_intervals`, `scan_hardsub_and_scenes`) dynamically for `width < 1000` portrait strips (`xc = width // 2`, `y_bot = 128`, scaled gap/center tolerances) and added `resolve_source_language()` reading `series_bible.json` / `--source-lang`.
  3. Added `resolve_style_for_profile()` and proportional `resolve_watermark(..., width=W)` scaling in `aicn.py`, plus `init-series --source-lang` and `merge-series` `deliverables/9x16/` fallback.

## L-010 (2026-11-20) — Multi-Clause `cmd_dub` Grouping Caused 2–5s+ Early Voice Desync & Stage 3 Gate Blind Spot
- **Symptom:** User reported that in the published *Viện Thiên Công / Năm 18 Tuổi Tôi Chế Tạo Thần Khí* (`nam-18-tuoi-toi-che-tao-than-khi`) video, Vietnamese voice dubbing played 5s+ early relative to video/mouth/hardsub in some sections while snapping back into sync in others, and Stage 1–5 gates all reported `PASS`.
- **Verified Root Cause:**
  1. `aicn_audio.py` `group_lines()` merged uncapped chains of comma-separated clauses (up to 10 segments / `16.88s` in `unit_01` `grp=157`), and `cmd_dub()` placed the entire synthesized group WAV contiguously on `dub_voice_48k.wav` starting at `segs[g[0]]["start"]` at `>=1.10x` tempo without respecting per-clause `segs[i]["start"]` timestamps. Because continuous TTS lacked the original Chinese pauses/pacing (`10.86s` vs `16.80s` window), every subsequent clause in the group rushed ahead (`unit_01` seg 262 `speech_start=674.566s` vs `start=679.900s`, `-5.334s` early; `unit_02` `-4.859s`; `unit_03` `-3.655s`; `unit_06` `-3.424s`), snapping back to `0.000s` at the start of the next group.
  2. `verify_stage_3.py` only checked `abs(slot_start - script_start) <= 0.025` (where `slot_start` was copied verbatim from `script_start`), never checking `speech_start` (actual audio placement), and `cmd_verify` only checked `overflow_s > 0.05` (too late, never too early).
- **Guardrail Implemented (`v1.5.1`):**
  1. Capped `group_lines()` (`max_group_segs=4`, `max_group_dur=8.0`) and updated `cmd_dub()` to split each group's synthesized WAV into per-clause pieces (`split_points` on raw audio), time-stretch each piece to its own clause window, and place each piece on `dub_voice_48k.wav` anchored to `max(s0, prev_sp_e + 0.02)` with 5ms edge fades.
  2. Added `dub_sync` check in `aicn_audio.py` `cmd_verify` and `speech_start` drift enforcement (`[-0.15s, +0.75s]` of `script_start`) in `verify_stage_3.py` `hard_locked_slot_start_zero_drift`.
  3. Added regression tests in `tests/test_aicn_audio.py` (`test_group_lines_caps_count_and_span`, `test_cmd_dub_places_each_clause_at_own_start_no_early_drift`, `test_stage3_gate_rejects_early_speech_drift_and_accepts_synced`).

## L-011 (2026-11-21) — Full-Series Audit (`do-dai-hoc-la-len-dinh`, 81 eps / 143.8m / 1080×1920): Mixed-FPS Concat, Portrait Calibration, `series_bible` Gate Storm & Stage-2 Barrier
- **Symptom:** The 81-episode `1080×1920` series took ~6.5 hours active wall time and shipped with cumulative video/audio drift (`v_st=0.061s` vs `a_st=0.000s`, `avg_frame_rate=25.0426`, `v_dur - a_dur = -0.396s`), required full Stage 1 rescans across all 8 units, broke Stage 0 gates whenever `series_bible.json` was edited, and stalled 2h10m at an all-batch Stage-2 barrier.
- **Verified Root Causes:**
  1. **Mixed-FPS & PTS Drift (`init_workspace.py`, `aicn.py`):** 80/81 episodes were `25fps` (`1/12800`) while `unit_03/ep05.mp4` was `30fps` (`1/15360`). `init_workspace.py` `specs` omitted `r_frame_rate`/`avg_frame_rate` and hardcoded `fps=30` without `PTS-STARTPTS`/`make_zero`, leaving `unit_03` at `avg_frame_rate=25.430fps` and all 8 `master_source.mp4` with `v_st=0.046s`. Neither `verify-overlay` nor `merge-series` checked A/V stream duration/frame integrity.
  2. **Input Calibration & Portrait Detection (`init_workspace.py`, `run_asr_and_scan.py`, `aicn.py`):** `init_workspace.py` hardcoded `strip_y0=round(height*0.68)=1306` (missing hardsubs at `y=1257..1327`), and `run_asr_and_scan.py` / `aicn.py` checked `width < 1000` (missing `1080×1920` portrait!).
  3. **Gate Invalidation Storm (`init_workspace.py`):** `config/series_bible.json` was hashed inside `stage_0_gate.json`, so Stage 2 glossary edits broke Stage 0 on all 8 units.
  4. **Stage-2 Barrier & Contention (`aicn.py`):** `cmd_run_series --phase prep` ran sequentially, had no `--units` streaming filter, and lacked cross-process locks (`gpu/cpu/media`), causing 6-agent contention and a 2h10m Stage-2 stall.
- **Guardrail Implemented (`v1.6.0`):**
  1. `init_workspace.py`: added `calibrate_strip_y0()` pre-flight keyframe sampling, added `r_fps`/`avg_fps` to `specs` with mode-fps + `setpts=PTS-STARTPTS`/`-avoid_negative_ts make_zero` normalization, and removed `config/series_bible.json` from `stage_0_gate.json` `artifact_paths`.
  2. `run_asr_and_scan.py` & `aicn.py`: fixed portrait detection to `height > width` (supporting both `720×1280` and `1080×1920`).
  3. `aicn.py`: added `av_sync_and_frame_integrity` gate in `verify-overlay`, stream-spec + A/V sync verification in `merge-series`, `--units` streaming + overlapped `prep` + cross-process `fcntl.flock` (`gpu/cpu/media`) in `run-series`, and hub-shared `.cache/tts_sent` in `aicn_audio.py`.
  4. Added regression tests in `tests/test_aicn.py` (50/50 tests passing).

## L-012 (2026-11-24) — `v2.0.0` Pipeline Simplification (Dub + Watermark + Translate Only) & ShortMax Relay HLS Fix
- **Symptom:**
  1. Style-B subtitle overlay + 5fps hardsub bbox scanning + GaussianBlur masking added heavy per-frame CPU/GPU cost and brittle bbox/geometry failure modes even when full Vietnamese dubbing (`voice_dub`) + external `.srt` already carried the translation.
  2. `scripts/dd_tool.py` failed on DramaDunyam ShortMax relay HLS streams returning relative `/vr/dr/hls/.../main.m3u8?auth_key=...` URLs because `play_info` lacked the per-series `Referer: https://dramadunyam.com/en/series/<slug>` header, relative URLs were not prefixed with `BASE_URL`, and `ffmpeg -headers` omitted the `dd_bilet` `Cookie` header.
- **Guardrail Implemented (`v2.0.0`):**
  1. Removed all burned-in subtitle overlay, Style-B plate rendering, GaussianBlur hardsub masking, and default 5fps hardsub bbox scanning across `scripts/aicn.py` and `vendor/` (`init_workspace.py`, `run_asr_and_scan.py`, `analyze_voice_waveform.py`, `verify_stage_1.py`, `validate_bilingual_script.py`, `composite_and_verify.py`, `verify_stage_5.py`). `cmd_render` now cleanly muxes `source/master_source.mp4` + `media/master_audio_48k.wav` + optional dual-corner watermarks (`--brand-dir`), or stream-copies video (`-c:v copy`) when no watermark is set.
  2. Updated `scripts/dd_tool.py` (`play_info`, `download_stream`, `cmd_download`) to send per-series `Referer`, prefix relative `/...` URLs with `BASE_URL`, and pass `Cookie: <jar cookies>` in `ffmpeg -headers`.
  3. Added regression tests in `tests/test_hg_tool.py` and updated `tests/test_aicn.py` for `v2.0.0`.
