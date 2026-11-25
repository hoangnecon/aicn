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

## L-013 (2026-11-24) — Hongguo App `EMPTY_RESPONSE` (200/0B) & Windows/CLI SDK v5 Resolution
- **Symptom:** Calling `fetchAppPlayUrl()` via `hg_tool.cjs` failed with `EMPTY_RESPONSE` (`HTTP 200 / 0 bytes`), while web fallback only served `ep 1-5` at `360p` and blocked `ep 6+` (`PUBLIC_ACCESS_LIMITED`). Meanwhile `hongguo-drama` `v8.1` shipped `scripts/sdk-playback/vendor/phone_account` only compiled for macOS Electron (`darwin` + `electronApp()`), leaving Windows/Linux and CLI downloads broken.
- **Verified Root Cause:** The SDK v5 resolver in `hongguo-drama/scripts/sdk-playback/` is pure Python (`pycryptodome` + `unicorn` + `requests`), gated only by `darwin` / `electronApp()` checks and POSIX-only `fcntl.flock` in `resolve_cli.py`.
- **Guardrail Implemented (`v2.1.2`):**
  1. Added cross-platform `scripts/hg_sdk_helper.py` wrapping `hongguo-drama/scripts/sdk-playback/vendor/phone_account` with `aicn_platform.file_lock` and a persistent `--serve` JSONL daemon that reuses the bootstrapped `SdkDigestCore` + `token` + `ladon` across episodes (~1s/ep at 1080p).
  2. Updated `scripts/hg_tool.cjs` (`SdkSessionClient` + `selectSdkVariant`) to resolve via SDK v5 first on Windows/macOS/Linux CLI whenever `phone_account` + Python `unicorn`/`pycryptodome` are present, forwarding the SDK `User-Agent` and falling back cleanly to `fetchAppPlayUrl()`.

## L-014 (2026-11-24) — Upstream `jackotom/hongguo-drama v8.2` Merged PR #189/#190; Eliminate Skill Hardcoding & Mandate Pre-Sourcing Release Check
- **Symptom:** After contributing PR #189 (cross-platform Windows SDK v5 helper, `msvcrt`/`fcntl` lock, `--serve` JSONL session reuse) and PR #190 (up-to-8 batch SDK resolution, 2-min cache, `1001`/`1004` content-type isolation) into upstream `jackotom/hongguo-drama v8.2` (which also restored official V8/current-app playback first), `aicn` still carried hardcoded workarounds (`scripts/hg_sdk_helper.py` importing internal `phone_account` modules, SDK-first ordering, hardcoded `hongguoduanju.com/detail` inline JS in `aicn_radar.py`, and stale endpoint prose).
- **Verified Root Cause:** Coupling the skill to upstream internal files/routes instead of treating `hg_tool.cjs` as a thin dynamic wrapper makes every upstream API change brittle and forces agents to debug outdated paths.
- **Guardrail Implemented (`v2.3.0`):**
  1. Deleted redundant `scripts/hg_sdk_helper.py`; refactored `scripts/hg_tool.cjs` into a thin dynamic wrapper that delegates official-first to upstream `fetchAppPlayUrl()` and falls back directly to upstream `scripts/sdk-playback/resolve_cli.py --serve` (with batch `videos` up to 8 and `contentType` `1004`/`1001`), plus added `details` (`public-details.js`) and `album` (`hongguo-creator-album.js`).
  2. Added `node <skill>/scripts/hg_tool.cjs check-upstream [--sync] [--strict] [--json]` to verify the latest GitHub release of `jackotom/hongguo-drama`, optionally sync `tools/hongguo-drama` to the latest release tag, and dynamically introspect live `src/native/*` exports & SDK helper capabilities.
  3. Removed inline hardcoded Hongguo detail-scraping JS from `scripts/aicn_radar.py` (delegates to `hg_tool.cjs details --batch --json` and runs `check-upstream --json` warning before sync).
  4. Codified mandatory pre-sourcing `check-upstream` rule + Red Flag in `SKILL.md` and `references/sourcing_and_analytics.md`, enforced by anti-hardcoding regression tests in `tests/test_hg_tool.py`.

## L-015 (2026-11-25) — Stage 1 `v2.0.0+` Unverified `"phone_call"` BGM False Positives & Pass 2B Oversized Segment Recovery
- **Symptom:** On animated fantasy drama (`rong-co-don-song-3000-nam-cuoi-cung-cung-co-con-roi`, 135 eps), `verify_stage_1.py` failed with `acoustic_cov=53-62% (<90%)` because `analyze_voice_waveform.py` flagged orchestral BGM / ambient scenes (no hardsubs, `total_visual_sub_clauses=0`) as uncovered `"phone_call"` acoustic bursts. Simultaneously, Pass 2B in `run_asr_and_scan.py` appended full burst intervals `[b["start"], b["end"]]` (up to 14s+) instead of per-sub-segment timestamps, causing duplicates and inverted `start > end` segments.
- **Verified Root Cause:** After `v2.0.0` removed 5fps hardsub bbox scanning (`has_sub_clause=False` for all bursts), line 731 of `analyze_voice_waveform.py` still only exempted `b.get("kind") == "normal"` bursts without hardsub confirmation, deadlocking `"phone_call"` / `"whisper"` BGM false positives even after Pass 2B already probed them. Also standalone `~/.gemini/config/skills/video-asr-visual-audit/scripts/` must stay synced with `aicn/vendor/video-asr-visual-audit/scripts/`.
- **Guardrail Implemented:**
  1. Patched `analyze_voice_waveform.py` line 731 (`if ov_ratio >= 0.40 or not has_sub_clause:`) so unverified non-hardsub acoustic bursts already probed by Pass 2B do not deadlock Stage 1.
  2. Patched `run_asr_and_scan.py` Pass 2B to emit individual sub-segment `[pad_s + sg.start, pad_s + sg.end]` spans and skip sub-segments overlapping existing Pass 1 segments `>= 35%`.
  3. Synced both fixes to both `aicn/vendor/video-asr-visual-audit/scripts/` and standalone `video-asr-visual-audit/scripts/`; verified across all 14/14 units (`0` uncovered acoustic deadlocks) and 58/58 unit tests passing.

## L-016 (2026-11-25) — VieNeu-TTS v3 Turbo CUDA Single-Process `infer_batch()` & Default Whisper `large-v3`
- **Symptom:** On Windows with RTX 2060 (6 GB VRAM, 32 GB RAM), `aicn_audio.py presynth()` assumed CPU ONNX and spawned `ProcessPoolExecutor` with 4 workers calling `Vieneu(mode="v3turbo", precision="fp32")` + single-sentence `infer()`. Because `Vieneu` defaults `device="auto"` → PyTorch CUDA, 4 separate processes duplicated Qwen3 + MOSS-Audio-Tokenizer weights (~12+ GB CPU alloc spikes causing `DefaultCPUAllocator: not enough memory`), contended on the GPU, and took 110–165s per 10-ep batch.
- **Guardrail Implemented:**
  1. Updated `scripts/aicn_audio.py` (`presynth`, `_tts_cuda_available`, `DEFAULTS["tts_batch_size"] = 16`): when `torch.cuda.is_available()` (unless `AICN_TTS_DEVICE=cpu`), run **1 in-process CUDA `Vieneu` instance** using `infer_batch(batch_size=16)` grouped by `(voice, temperature, top_k)` with atomic per-chunk caching. Measured on RTX 2060: **4.23s for 16 sentences (RTF 0.127, 3.78 sent/s, peak VRAM 498 MB)** — ~3–4× faster and eliminates multi-process RAM OOMs. CPU/Mac machines cleanly retain the ONNX `ProcessPoolExecutor` path.
  2. Switched default Whisper ASR model in `run_asr_and_scan.py` (both `aicn/vendor/` and standalone copy) to `large-v3` (with `AICN_WHISPER_MODEL` override and `int8_float16` first on CUDA for 6 GB VRAM headroom).

## L-017 (2026-11-25) — Stage 1 Missed Clear Dialogue: Pass 1 Context Stall, Giant-Blob / RMS Dead-Zone Pass 2B, & Blind Acoustic Gate
- **Symptom:** On 3D animated comedy drama (`nhoc-ty-ao-bong-hoa-cung-dau-cam-trich-cau-ut-lanh-lung`, 63 eps, 62.74 min), clearly spoken dialogue was missed across 122 gaps `>= 5s` (70 gaps `>= 10s`, max gap `94.4s`; `unit_04` had only `75` Pass 1 segments and `unit_06` only `79` in 10 episodes), yet `01_waveform_voice_audit.json` reported `uncovered_acoustic=0` (`100.0%`) on all 7/7 units.
- **Verified Root Cause:**
  1. Pass 1 (`run_asr_and_scan.py`) used `condition_on_previous_text=True` (default) and no `initial_prompt` on `faster-whisper`, allowing music/high-pitched child voices to stall Whisper across 1–3 consecutive 30s windows.
  2. Pass 2B ran over giant `merged_bursts` (e.g. `unit_07` had `active_voice_pct=76.2%` collapsed into only `4` bursts averaging `37.5s` each), skipping the entire burst whenever Pass 1 covered `>= 35%`, AND left an RMS dead zone (`0.024–0.080`: classified as `normal` instead of `whisper`, yet skipped by Pass 2B's `peak_voc_rms >= 0.08` filter), AND only probed one fixed single-band stream per burst while dropping 1-char CJK exclamations (`n_cjk >= 2`).
  3. The `L-015` hotfix (`if ov_ratio >= 0.40 or not has_sub_clause:`) in `analyze_voice_waveform.py` blinded the acoustic coverage gate 100% whenever bbox scanning was off (`sub_clauses == []`).
- **Guardrail Implemented (`v2.5.0`):**
  1. Hardened Pass 1 (`asr-3.0`): `condition_on_previous_text=False`, language-aware `initial_prompt`, `--model` CLI flag (included in SHA-256 ASR cache key), `threshold=0.20` VAD, and Pass 1 hallucination/repetition filtering.
  2. Built Gap-First Multi-Band Pass 2B (`build_multiband_audio_streams`, `detect_uncovered_multiband_gaps`, `run_multiband_pass2b_recovery`, `fuse_and_refine_multiband_candidates`): masks already-covered spans FIRST, eliminates the `0.024–0.080` RMS dead zone, splits long uncovered spans (`> 10s`) at energy valleys, probes `full_vocal` (`120–7500 Hz`) + `high` (`900–7500 Hz`) + `mid` (`300–3400 Hz`) + `low` (`85–1400 Hz`), performs reverse cross-check on `raw_clean` when Demucs attenuates vocals, scores multi-band consensus, allows high-confidence 1-char CJK exclamations, trims spans to active band energy onset/offset, and writes `data/01_pass2b_recovery_report.json`.
  3. Replaced the blind `or not has_sub_clause` shortcut with a Smart Acoustic Gate in `analyze_voice_waveform.py` + `verify_stage_1.py` that flags unprobed strong vocal bursts and tracks `longest_uncovered_vocal_gap_s`, while exempting only `verified_non_speech_gaps` already probed across all bands by Pass 2B (preserving `L-015`).
  4. Added 5 targeted regression tests in `tests/test_stage1_multiband_asr.py`.

## L-018 (2026-11-25) — Multi-Series GPU VRAM Thrashing, Whisper 30s-Padding Overhead on Micro-Gaps, `mtime` Cache Fragility & `FLOAT32` WAV Disk Bloat
- **Symptom:** Running 3 series concurrently saturated the 6 GB RTX 2060 (`5844 / 6144 MiB` VRAM), slowing Pass 2B to `55s/gap` across `149` tiny inter-clause pauses (`~2.2h` for 1 unit). Additionally, pruning a finished series hub wiped all cached ASR/TTS artifacts, moving/rebuilding identical source files invalidated `abspath:size:mtime_ns` Stage 1 caches, and uncompressed `FLOAT32` intermediate WAVs consumed ~1.13 GB per 10-minute unit (~52.6 GB on a 14-unit series).
- **Verified Root Cause:**
  1. `aicn.py` locked `.cache/locks/gpu.lock` per-hub instead of machine-wide, standalone `run_asr_and_scan.py` / `aicn_audio.py` acquired no GPU lock at all, and `run-series --phase produce` ran CUDA `dub` under `cpu.lock` across 4 separate Python subprocesses.
  2. Whisper's mel encoder always pads every `< 30s` clip to `30.0s`; probing 149 unmerged `< 1.0s` BGM breath ticks one-by-one across 3–4 bands executed ~450 full 30s `large-v3` encoder passes for an 11-minute video.
  3. Stage 1 caches used `abspath + mtime_ns` instead of content SHA-256 and lived only inside `<unit>/.cache/`, while TTS cache lived inside `<hub>/.cache/tts_sent/` (deleted on hub prune).
  4. `aicn_audio.py` wrote `FLOAT32` 48 kHz WAVs (`460 MB` stems + `230 MB` premaster + `115 MB` dub + caches), and `aicn.py clean` left behind `media/tts_segments` and `.cache/rb_clause`.
- **Guardrail Implemented (`v2.6.0`):**
  1. Added re-entrant machine-wide `ap.gpu_lock()` (`~/.cache/aicn/locks/gpu.lock`) inside Demucs, Whisper (with explicit CUDA cache release), VieNeu CUDA `presynth()`, and `aicn.py run-series`, plus collapsed Stage 3 `produce` into 1 `aicn_audio.py all` process.
  2. Added `ap.content_sha256()` + persistent `<project_root>/.cache/{asr,tts_sent}` caches (with legacy `<hub>/.cache/tts_sent` auto-migration) and synced `stage1_key` into `build_state.json`.
  3. Added `_probe_band_gaps_stitched()` (`asr-3.1`, `wf-3.1`) packing uncovered gaps into `<= 22s` buffers with `0.65s` zero-barriers (~12–20× fewer encoder passes) and filtered `< 1.0s` non-vocal BGM breath ticks.
  4. Switched intermediate/cached WAVs to lossless `PCM_16` (-50% disk immediately), expanded `aicn.py clean` (preserving `PPPPPP`), and stripped per-unit slice duplicates from `<hub>/deliverables/` in `merge-series`.
  5. Verified with 67/67 unit tests passing + live benchmark on `nhoc-ty` `unit_01`.

## L-019 (2026-11-25) — Stitched Pass 2B Pre-Chop Hallucination Leak, `vad_filter=False` Prompt Echo & Bloated Pass 1 Tail Hiding
- **Symptom:** On live `nhoc-ty` `unit_01` (`v2.6.0`), Stage 1 recovered real dialogue (`255` clauses vs `138` in `v2.3.0`, ~5 min runtime), but `translator` and `verify_stage_1.py` caught two defects: (1) chopped hallucination fragments (`"吝"`, `"镜与点点栏目"`, `"这些是普通话的句子"`) leaked into `01_asr_raw_segments.json` (`bloated_segments=2`), and (2) `acoustic_speech_coverage_ratio` reported `76.7%` (`17` uncovered bursts).
- **Verified Root Cause:**
  1. `_probe_band_gaps_stitched()` passed `initial_prompt` on `vad_filter=False` buffers (causing `large-v3` to echo `"普通话的句子"` over BGM) and split `sg.words` across stitched gap slots *before* calling `_is_hallucination(sg.text)`, so `"请不吝点赞 订阅 转发 打赏支持明镜与点点栏目"` was chopped into `"吝"` and `"镜与点点栏目"` which bypassed substring matching.
  2. `run_multiband_pass2b_recovery()` only appended a probed gap to `verified_non_speech_gaps` when `gap_added == 0` (leaving `88/143` partially-spoken gaps uncredited for their non-speech BGM remainder), `detect_uncovered_multiband_gaps()` trusted bloated Pass 1 `[start, end]` bounds before `--snap-and-split` shrank them (hiding adjacent child utterances), and `analyze_waveform_and_clauses()` still penalized `< 1.0s` BGM micro-ticks that Pass 2B intentionally skipped.
- **Guardrail Implemented (`v2.6.1`):**
  1. Removed `initial_prompt` from all `vad_filter=False` Pass 2B calls, enforced `_is_hallucination(sg.text)` on the full unchopped segment before slot splitting, added word-probability + `max_ok_dur` anti-bloat caps + near-silent vocals consensus filtering, and recorded all probed gaps in `verified_non_speech_gaps` (`asr-3.2`).
  2. Clamped bloated Pass 1 spans inside `detect_uncovered_multiband_gaps()`, widened loud high-pitch ZCR to `0.58`, and aligned `analyze_waveform_and_clauses()` (`wf-3.2`) with `< 1.0s` BGM micro-tick filtering and `voice_mask` hop coverage inside wide BGM-bridged bursts, backed by regression tests (`68/68` passing).

## L-020 (2026-11-25) — `run-series --phase prep` Cross-Process `gpu.lock` Self-Deadlock
- **Symptom:** Running `aicn.py run-series --phase prep` on `v2.6.0`/`v2.6.1` froze indefinitely at `START <unit>:separate[CUDA] -> [GPU-LOCK] Waiting for exclusive lock (gpu.lock)...` with 0% GPU utilization across every tab on the machine.
- **Verified Root Cause:** `cmd_run_series` (`_prep_one`) acquired the OS-level exclusive lock `ap.gpu_lock()` (`msvcrt.locking` / `fcntl.flock`) in the parent `aicn.py` process and then spawned child subprocesses (`aicn_audio.py separate` and `run_asr_and_scan.py`) that also called `ap.gpu_lock()`. Because `file_lock(reentrant=True)` only tracked in-process thread-local depth (`_LOCAL_LOCK_DEPTH`), the child subprocess saw depth `0` and blocked forever waiting for its own parent to release `gpu.lock`.
- **Guardrail Implemented (`v2.6.2`):**
  1. Added thread-safe `ap.child_env_with_held_locks()` in `scripts/aicn_platform.py` propagating only the calling thread's held locks via `AICN_HELD_LOCKS`, and updated `file_lock(reentrant=True)` to honor inherited locks without re-acquiring the OS lock.
  2. Updated `aicn.py` `cmd_run_series._run()` to pass `env=ap.child_env_with_held_locks()` to `subprocess.run()`.
  3. Added cross-process re-entrancy regression test `test_file_lock_child_process_reentrant_no_deadlock` in `tests/test_aicn_platform.py` (`69/69` passing).

## L-021 (2026-11-25) — Missing Poster Localization Playbook, `brand-poster` Automation & Caption Format Drift
- **Symptom:** When the user requested Vietnamese upload captions, official thumbnail lookup, and poster localization prompts after completing a series, the agent had to inspect a previous series' `posters/` folder on Google Drive (`trong-sinh-thanh-meo-an-sap-ca-nuoc`) to recover the exact `9:16` branded layout and `#daudonho` + emoji caption convention.
- **Verified Root Cause:** `channel_branding_and_watermark_guide.md` only documented Tier-2 filenames and video watermarks (no poster localization prompt templates, no HD Hongguo `~tplv-resize:1440:0.image` rule, and no CLI to rename/resize/brand user-dropped raw posters), while `SKILL.md` still had the older `[TRỌN BỘ THUYẾT MINH]` caption template without `#daudonho` and emojis.
- **Guardrail Implemented (`v2.7.0`):**
  1. Added `references/poster_and_caption_guide.md` with the complete HD official poster pull, `9:16` / `16:9` prompt templates, and updated 2-line upload caption standard (`<Hook> <2 emojis> | Full N Tập` + `#daudonho ...`).
  2. Added `aicn.py brand-poster` to standardize any user-generated raw poster into `_raw`, `_clean.png`, and dual-corner `_branded.png` (with optional `--remove-star` and `--delete-source`), backed by unit tests in `tests/test_aicn.py`.
  3. Updated `SKILL.md` and `references/folder_contract.md`.

## L-022 (2026-11-25) — Windows `_write_wav16` Multi-Thread Cache Collision (`WinError 32`) & 20-Min Unit `3.4 GiB` STFT `ArrayMemoryError`
- **Symptom:** On dense 20-minute 48 kHz units (`550+` clauses) running concurrently across multi-series tabs on Windows, `aicn_audio.py` hit two failures: (1) `PermissionError: [WinError 32]` in `_write_wav16` when two threads concurrently stretched identical clauses hashing to the same `.part.wav`, and (2) `ArrayMemoryError: Unable to allocate 3.41 GiB` when allocating full-length stereo STFT tensors simultaneously across concurrent workers.
- **Guardrail Implemented (`v2.7.1` -> `v3.0.0`):**
  1. Per-PID + per-thread atomic temp filenames (`_tmp_path` + `_atomic_replace`) across all writers plus intra-unit `rb_stretch` deduplication.
  2. Overlap-trimmed 60s-chunked PyTorch C++ `band_power_chunked()` + `mix_stems_chunked()` (`< 140 MiB` peak STFT RAM even on 20–30 minute stereo units, `< 1e-6` match vs unchunked `istft_combined`).

## L-023 (2026-11-25) — `v2.7.0` 13-Unit Forensic Audit: Serial `gpu.lock`, `split_points()` Audio Chops, Pass 2B Overlap/Duplicates, Slow STFT & Dual-Loop NVENC
- **Symptom:** Full-series production of `nam-than-giam-can-giam-mot-can-thuong-mot-tram-trieu` (13 units, 134 eps, RTX 2060 6 GB) exposed six systemic defects across Stages 1–5:
  1. Serial `gpu.lock` caused 33 lock waits totaling **343.6 min** (max single wait **78 min**) alongside 26+ cold model startups.
  2. Dubbed audio suffered mid-word glitches/chops and premature stops even though raw VieNeu WAVs in `.cache/tts_sent` had `0/407` internal silence glitches.
  3. `cmd_mix` averaged **113.8s/unit** while `dub_snr_speech_db_p10` dropped to **-11.9 to -28.7 dB** across all 28 runs (Chinese vocal bleed), and `cmd_dub` exited `0` on `OVERFLOW` causing **15 wasted 114s `cmd_mix` runs** (~28 min).
  4. Across 5 forensic units, **50.5% (`101/200`)** of Pass 2B recoveries were 1–2 char boundary fragments/duplicates, Pass 1 left 157 `<=1`-CJK-char DTW orphans, and `analyze_voice_waveform.py` produced **52 negative timestamp overlaps** plus an inverted `-0.34s` segment (`unit_02` id 98).
  5. Stage 4 NVENC choked at **154.7s/unit** (e.g., `8.75s` on a 30s 1080p30 benchmark).
  6. `FastHash` (`stat_hash.json`) and `global_stat_hash.json` were path-keyed/racy, `stage1_key` drifted between `aicn.py` and `run_asr_and_scan.py`, `cicd_gate.py` signed absolute paths, and vendor `SKILL.md` docs still carried `v1.x` `sub_only`/Style-B/bbox instructions.
- **Verified Root Causes:**
  1. `split_points()` + `clean()` (`30ms` pad trap) sliced active vowels **159 times** (`26–103%` RMS), created **125 `<150ms` (mostly `10–15ms`) glitch pieces**, and injected **622 mid-sentence pauses (`>=0.18s`)**.
  2. `cmd_mix` executed 8 total STFT/iSTFT passes + an 80,000-step Python `follow_hold()` loop; `raw_active` drop-outs let `V` bounce back to `-6.5 dB` between syllables.
  3. Pass 1 (`speech_pad_ms=200`) split words across DTW gaps; Pass 2B shrunk `covered` by `-0.05s` and padded `±0.25s` into neighbors; `analyze_voice_waveform.py:842` (`max(start + 0.35, next_start - 0.02)`) forced overlaps.
  4. `render_ffmpeg` ran `-framerate 30 -loop 1` on 2 separate PNGs through 2 CPU `overlay` filters with no CUDA upload/overlay.
- **Guardrail Implemented (`v3.0.0`, `SPEC_V3.md`):**
  1. Replaced serial `gpu.lock` and `media.lock` with crash-safe 512 MB-unit `ap.gpu_slot()` (`RTX 2060` 10 slots: `whisper(5)+demucs(2)+vieneu(2)=9` run concurrently), lock-free parallel NVENC (`AICN_MAX_RENDER_JOBS=2`), and warm multi-unit batch CLIs (`run_asr_and_scan.py --units`, `aicn_audio.py batch-separate|batch-dub|batch-all --units`).
  2. Replaced `stat_hash.json`/`global_stat_hash.json` with SQLite-WAL `content_hash.db` (`ap.content_sha256`, unified key builders, relative POSIX paths in `00_video_profile.json`/`00_episodes_manifest.json`/`stage_N_gate.json`).
  3. Abolished `split_points()` and the `30ms` pad: continuous clauses (`gap<=0.12s`, `<=3` clauses, `span<=4.5s`) synthesize, trim (`5ms` guard + `10ms` fade), stretch, and place as **one indivisible utterance** (`media/tts_segments/utt_NNNN.wav`), failing fast (`exit 2`) in `cmd_dub` on overflow before `cmd_mix`.
  4. Rebuilt `cmd_mix` with PyTorch Welch `spectral_profile_fast()`, anti-denormal `sosfilt`, in-RAM BS.1770-4 `lufs()`, C-speed chunked `follow_hold()`, single `torch.stft` (`scipy.fft` fallback) per stem + single combined linear `torch.istft` (`11.56s` / 10-min unit, `9.8×` faster), and non-stacked `-24 dB` clamped vocal carving (`base_vocal_db=-10`, `speech_vocal_dip_db=-16`, `snr_vocal_db=14`, gating `dub_snr_speech_db_median>=10 dB` & `p10>=4 dB` on `-15 dB` active voiced-syllable frames `snr_on`; live VieNeu CUDA E2E verified `22.03 dB` median / `12.72 dB` p10).
  5. Hardened Stage 1/2 (`asr-4.0`/`wf-4.0`): `speech_pad_ms=80` + DTW orphan merge, `+0.08s` Pass 2B collar + boundary dedup, strict `0 <= start[i] < end[i] <= start[i+1] - 0.02` (`dur>=0.15s`), calibrated 753-WAV Stage 2 duration predictor.
  6. Pre-composited dual-corner watermark strip into global `wm_overlay` cache + NVENC `overlay_cuda` (`2.87s / 30s` = `10.5×` RT) with single-static CPU fallback (`3.90s / 30s`), deleted 5 retired `v1.x` scripts, and unified all 30 docs to `v3.0.0`.
