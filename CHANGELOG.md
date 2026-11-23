# Changelog

## 2.1.0 (2026-11-24)
- **Windows 10/11 + NVIDIA CUDA Support & Cross-Platform Architecture (`scripts/aicn_platform.py`)**:
  - Added stdlib-only `scripts/aicn_platform.py` centralizing OS/CPU/RAM/GPU detection (Apple Silicon Metal + NVIDIA CUDA), cross-platform file locking (`fcntl.flock` on POSIX, `msvcrt.locking` + `threading.Lock` on Windows), tool/Python discovery (`AICN_PY`, `~/.local/bin`, `/opt/homebrew/bin`, `imageio_ffmpeg` fallback), POSIX path normalization & concat escaping, video encoder backend selection (`videotoolbox` | `nvenc` | `cpu` with functional 1-frame NVENC check and no `-hwaccel cuda` before CPU overlay), and ASR/Demucs backend dispatch (`mlx` | `faster-cuda` | `faster-cpu`; `mlx` | `torch-cuda` | `torch-cpu`).
  - Updated `scripts/aicn.py`, `scripts/aicn_audio.py`, `scripts/dd_tool.py`, `scripts/dy_tool.py`, `scripts/hg_tool.cjs`, `scripts/aicn_radar.py`, and all `vendor/` scripts (`run_asr_and_scan.py`, `extract_audit_frames.py`, `analyze_voice_waveform.py`, `init_workspace.py`, `cicd_gate.py`, `composite_and_verify.py`, `apply_channel_branding.py`, `render_vertical_9x16.py`, `split_compiled_episodes.py`, `verify_stage_5.py`, `assemble_master_audio.py`) to route hardware/tool/path/lock operations through `aicn_platform.py` while preserving 100% macOS Apple Silicon compatibility.
  - Added `install.ps1` for Windows directory-junction linking into `%USERPROFILE%\.claude\skills` and `%USERPROFILE%\.gemini\config\skills` plus optional `-InstallTools` portable `ffmpeg`/`ffprobe`, `rubberband`, and `node` installation.
  - Synced documentation (`README.md`, `SKILL.md`, `references/*`, `vendor/*/SKILL.md`).

## 2.0.0 (2026-11-24)
- **Dub + Watermark + Translate Only (`v2.0.0`, `L-012`)**:
  - Removed all burned-in subtitle overlay, Style-B pill plate rendering, GaussianBlur hardsub masking, and default 5fps hardsub bbox scanning across `scripts/aicn.py` and `vendor/` (`init_workspace.py`, `run_asr_and_scan.py`, `analyze_voice_waveform.py`, `verify_stage_1.py`, `validate_bilingual_script.py`, `composite_and_verify.py`, `verify_stage_5.py`).
  - `aicn.py render` now cleanly muxes `source/master_source.mp4` + `media/master_audio_48k.wav` + optional dual-corner watermarks (`--brand-dir`, scaled cleanly for portrait/landscape) via hardware `h264_videotoolbox` / `hevc_videotoolbox`, or stream-copies video (`-c:v copy -sn`) when no watermark is set.
  - `aicn.py verify-render` (with backward-compatible alias `verify-overlay`) checks staging MP4 existence, codec/audio compliance (`h264`/`hevc`, `aac 48kHz stereo`, `0` subtitle streams), and `av_sync_and_frame_integrity`.
- **ShortMax Relay HLS Fix (`scripts/dd_tool.py`, `tests/test_hg_tool.py`)**:
  - `play_info(series_id, ep, slug=...)` and `cmd_download` send per-series `Referer: https://dramadunyam.com/en/series/<slug>`.
  - Relative `/...` stream URLs (e.g. `/vr/dr/hls/.../main.m3u8?auth_key=...`) are prefixed with `BASE_URL`.
  - `download_stream` includes `Cookie: <jar cookies>` and per-series `Referer` in `ffmpeg -headers` for HLS relay downloads.
- **Docs & Tests**: Updated `SKILL.md`, `references/*`, `templates/*`, `LESSONS.md`, `tests/test_aicn.py`, and `tests/test_hg_tool.py`.

## 1.6.0 (2026-11-21)
- **Full-Series RSI Audit & ~45m Streaming Pipeline Optimization (`L-011`)**:
  - **Audio/Scene Sync & Frame Integrity (`init_workspace.py`, `aicn.py`)**: Added `r_fps`/`avg_fps` to `init_workspace.py` `specs` with mode-fps + `setpts=PTS-STARTPTS`/`-avoid_negative_ts make_zero` normalization; added `av_sync_and_frame_integrity` hard gate in `aicn.py verify-overlay` and uniform stream-spec + post-concat A/V sync checks in `merge-series`.
  - **Auto `strip_y0` Calibration & `1080×1920` Portrait Fix (`init_workspace.py`, `run_asr_and_scan.py`, `aicn.py`)**: Added `calibrate_strip_y0()` pre-flight keyframe sampling in `init_workspace.py`; fixed portrait detection from `width < 1000` to `height > width` in `run_asr_and_scan.py` and `aicn.py resolve_style_for_profile()`.
  - **Decoupled `series_bible.json` & Hub-Shared TTS Cache (`init_workspace.py`, `aicn_audio.py`)**: Removed `config/series_bible.json` from `stage_0_gate.json` `artifact_paths` so Stage 2 glossary edits never invalidate Stage 0; shared `.cache/tts_sent` at `<hub>/.cache/tts_sent` across all units.
  - **Streaming & Lock-Safe Orchestration (`aicn.py`, `SKILL.md`, `production_time.md`)**: Added `--units` streaming, overlapped Metal/CPU `prep`, and cross-process `fcntl.flock` (`gpu/cpu/media`) in `cmd_run_series`, plus lean 2–3 worker cap contract.
  - **Regression Tests (`tests/test_aicn.py`)**: Added tests for `1080×1920` portrait detection/style, `calibrate_strip_y0` + decoupled `series_bible.json`, and Stage 4 `av_sync_and_frame_integrity` rejection (50/50 tests passing).

## 1.5.1 (2026-11-20)
- **Per-Clause Dub Sync Fix & Stage 3 Sync Gate (`aicn_audio.py`, `verify_stage_3.py`, `L-010`)**:
  - Fixed multi-clause early voice desync (measured up to `-5.334s` in `nam-18-tuoi-toi-che-tao-than-khi` `unit_01` `grp=157`): `group_lines()` now caps groups (`max_group_segs=4`, `max_group_dur=8.0`), and `cmd_dub()` splits each synthesized group WAV into per-clause pieces, time-stretches each piece to its own clause window, and places every piece at `max(s0, prev_sp_e + 0.02)` instead of placing the whole group contiguously.
  - Added `dub_sync` hard check in `aicn_audio.py` `cmd_verify` and `speech_start` drift enforcement (`[-0.15s, +0.75s]` vs `02_bilingual_script.json` `start`) in `verify_stage_3.py` so Stage 3 blocks any early/late audio desync.
  - Added 3 unit/regression tests in `tests/test_aicn_audio.py` (47/47 tests passing).

## 1.5.0 (2026-11-20)
- **DramaDunyam Open API Engine (`scripts/dd_tool.py` + `aicn_radar.py sync-dramadunyam`, `L-009`)**:
  - Added pure-Python stdlib CLI `scripts/dd_tool.py` (`top`, `list`, `search`, `detail`, `download`) using native `/api/config` `dd_bilet` guest session cookies (HTTP 412 auto-refresh, clean fail/skip on HTTP 402 paywalls, zero CAPTCHA/paywall bypassing).
  - Enforces clean English (`lang=en`) and non-adult (`yetiskin != true`) filtering across `tag=ai-short-dramas`, `populer`, and `/api/siralama`.
  - Integrated `sync-dramadunyam` into `scripts/aicn_radar.py` (`--platform dramadunyam` + `sync-all`).
- **9:16 Portrait (`720×1280`) & English Source Compatibility (`run_asr_and_scan.py`, `aicn.py`)**:
  - Generalized `scan_hardsub_and_scenes()` and `find_hardsub_intervals()` in `run_asr_and_scan.py` for narrow/portrait (`width < 1000`, `xc = width // 2`, extended `y_bot`) and added `resolve_source_language()` reading `--source-lang` / `project_config.json` / `series_bible.json` (`--source-lang en|zh|auto`).
  - Added `resolve_style_for_profile()` (`720w` auto-scales `font_size=28`, padding, and `max_line_px` inside `W - 120`) and proportional corner watermark scaling in `aicn.py`, plus `init-series --source-lang` and `merge-series` `deliverables/9x16/` fallback.

## 1.4.2 (2026-11-20)
- **Mixed-Source Concatenation Fix (`init_workspace.py`, `L-008`)**:
  - Expanded episode stream uniformity checks to verify `profile`, stream `index`, `time_base`, `audio.codec_name`, and `audio.profile` (catching Douyin vs Bilibili stream order reversal `0=audio/1=video`, `HE-AACv2` vs `AAC-LC`, and `1/15360` vs `1/16000` timescale mismatches).
  - Normalized mixed episodes with explicit `-map 0:v:0 -map 0:a:0 -video_track_timescale 15360` and added post-concat duration verification against summed source episodes.

## 1.4.1 (2026-11-20)
- **Douyin Native Engine (`Evil0ctal/Douyin_TikTok_Download_API` via `scripts/dy_tool.py`)**:
  - Installed `tools/Douyin_TikTok_Download_API` + lightweight `tools/.venv-dy` (`wreq`, `curl_cffi`, `playwright`).
  - Added `scripts/dy_tool.py` (`detail`, `download`, `mix`, `hot`) using pure-Python `ABogus` + `websign` (`x-secsdk-web-signature`) + Chrome guest `UIFID_TEMP` cookie minting (zero login required) — live verified fetching watermark-free 1080p MP4 from Douyin (`7707844707132476726`, `17.71 MB`).
- **Unified Multi-Platform Analytics & Search Index (`scripts/aicn_radar.py`)**:
  - Indexes **Hongguo** (`rank`, `new`, `first_visible_time`, `likes`, `favorites`), **Douyin** (`detail` + 2-hop `related_posts` AI-drama BFS + `hot`), and **Bilibili** (`bilisearch`) into SQLite + FTS5 (`data/drama_index.db`) with recency-weighted velocity `heat_score`.
- **Chinese-First Sourcing & Facebook Caption RSI (`L-006`, `L-007`)**:
  - Enforced strict **no-YouTube sourcing** rule for Chinese dramas (`SKILL.md`, `references/sourcing_and_analytics.md`): search in Chinese keywords first and acquire only from original Chinese platforms (Hongguo, Douyin, Bilibili, Hema, Kuaishou).
  - Documented 2-line Facebook upload metadata format (`[TRỌN BỘ THUYẾT MINH] ...` + lowercase broad hashtags `#aishortfilmdrama #aishortdrama #aifilm #phimai ...`).

## 1.4.0 (2026-11-20)
- **Primary Hongguo Engine Switch (`jackotom/hongguo-drama` via `scripts/hg_tool.cjs`)**:
  - Retired and deleted `openhongguo` (`ohg v0.4.0`) after live HTTP debugging confirmed ByteDance returns HTTP 200 / 0 bytes on legacy POST playback routes.
  - Added unified Node.js CLI `scripts/hg_tool.cjs` backed by `tools/hongguo-drama` (`rank`, `new`, `search`, `episodes`, `download`, `fetch-covers`), using the working `GET /reading/bookapi/video_tab/multi_video_model/v1/` V5 bodyless signature + local `spade_a` CENC-AES-CTR key derivation + `ffmpeg -decryption_key` decryption.
  - Live verified end-to-end on Ep 1 of 《来自未来的女儿》 (`7696412960813485081`, 1080p HEVC `1920x1080`, 8.65 MB, 141.7s, 9.1s total).
- **Sourcing, Analytics & Candidate Presentation Contract (`references/sourcing_and_analytics.md`)**:
  - Added freshness-first discovery (`hg_tool.cjs new` sorted by launch date descending + `rank` hot charts) so candidate picks never drift to stale/old titles.
  - Codified Mandatory Candidate List Presentation Contract in `SKILL.md`: every shortlist presented to the user must include Vietnamese translated title, Chinese title, Vietnamese `--series-slug`, metadata/recency signal, Vietnamese synopsis, and embedded local `.jpg` cover thumbnails (`hg_tool.cjs fetch-covers`).
- **Recursive Self-Improvement (RSI) Protocol (`references/recursive_self_improvement.md` + `LESSONS.md`)**:
  - Added formal 4-step RSI loop (`Detect & Measure -> Root-Cause Audit -> Codify Guardrail -> Verify & Bump`) and pre-populated `LESSONS.md` (`L-001..L-005`).
  - Added unit test suite `tests/test_hg_tool.py` covering `hg_tool.cjs` CLI, episode spec parsing, and repo resolution.

## 1.3.1 (2026-11-20)
- **Series naming & folder/markdown contract**:
  - `aicn.py init-series` now enforces Vietnamese translated, no-accent, lowercase, hyphen-separated `--series-slug` (`^[a-z0-9]+(-[a-z0-9]+)*$`, e.g. `nam-18-tuoi-toi-che-tao-than-khi`).
  - Expanded `references/folder_contract.md` and `SKILL.md` with the complete parent/child directory tree, per-folder contents, and per-stage Markdown checklist/report templates & fill formats.

## 1.3.0 (2026-11-20)
- **Full-series release contract ("1 published video = 1 FULL series")**:
  - Working batches under `units/*` are strictly internal work slices (caching, QA, pipelining) — never standalone uploads.
  - `aicn.py discover` and `PROGRESS.md` track `FULL RELEASE: READY / PENDING (X/Y batches PASS)`.
  - `aicn.py merge-series --hub H`: verifies all batches passed Stage 5, stream-copies all batch MP4s + time-shifted SRTs into `deliverables/final_<series>_full_vi.mp4` + `final_<series>_full_vi.srt`, logs `[release]` to hub `CHANGELOG.md`, and refreshes `PROGRESS.md`.
  - `aicn.py run-series --hub H --phase prep|produce`: hardware-overlapped full-series runner (`Metal GPU: separate` ∥ `CPU: dub+mix+master+verify` ∥ `Media Engine: render+verify-overlay+qa-extract`), preserving every human/agent visual QA gate.
- **Stage 1 ASR & 5 fps scan (`vendor/video-asr-visual-audit`)**:
  - Disabled unused `word_timestamps` in Whisper Pass 1 (`run_asr_and_scan.py`): **3.33× faster** on warm MLX Metal (`72.03 s → 21.63 s` on 120 s real slice).
  - Fast-math 5 fps hardsub scan (skip frames with zero candidate pixels + `int16` subsampled scene-cut): **38.86 s → 29.73 s** (-23%) on 609.5 s unit 07, **211/211 bit-exact** `hardsub_intervals`.
  - Fixed stems path mismatch (`media/stems/` vs legacy `media/demucs/`) in `run_asr_and_scan.py`, `analyze_voice_waveform.py`, and `verify_stage_1.py`.
  - Parallelized `extract_audit_frames.py` (4 workers).
- **Stage 3 audio (`aicn-audio-1.1`, full CPU VieNeu dubbing)**:
  - **Fast native 48 kHz master** (`master_mode="fast"`, default): `acompressor` + `ebur128` + linear `volume` + `alimiter` cuts `cmd_master` from **115.38 s** (legacy 192 kHz 2-pass `loudnorm`) to **4.11 s** (**28× faster**) at `-14.00 LUFS`, `-0.80 dBFS` true peak.
  - **Cached-Z `complex64` STFT/iSTFT + piped `lufs()` + 100 Hz RMS** in `cmd_mix`: **56.54 s compute (78.8 s total) → 20.49 s compute** (**2.8–3.7× faster**), identical LUFS/floors (`-14.0 LUFS`, `-21.93 dB`, `-9.0 dB`), median SNR `14.90 dB`.
  - **CPU VieNeu `tts_workers=2, tts_threads=4`** (`fp32` ONNX, active for `>= 8` missing jobs): **+23% faster** (`gen_RTF 0.317 → 0.258` on 12 real sentences) + parallel `rubberband` (`ThreadPoolExecutor`).
  - Kept `STEMS_VERSION = "aicn-audio-1.0"` so `v1.2.x` Demucs stems stay cache-valid; `cmd_verify` now writes `03_pacing_and_audio_report.md`; `sub_only` alignment fix included.
- **Stage 4 render & Stage 5 QA (`ff-1.1`, `vendor/video-localization-qa-review`)**:
  - Enabled `-hwaccel videotoolbox` + quality VBR presets (`--codec h264|hevc|h264_high`, `--quality N`):
    - `h264` (default, `-q:v 58`): **-49% smaller** (~1.86 GB / 100 min), **5.75×** real-time (vs 4.21× baseline, SSIM 0.988).
    - `hevc` (`-q:v 60 -tag:v hvc1`): **-63% smaller** (~1.35 GB / 100 min), **6.06×** real-time (SSIM 0.987).
  - Single-pass dual-corner watermark compositing via `--brand-dir` or `config/project_config.json["watermark"]` (5.59–5.75×).
  - Fixed `verify_stage_5.py` to accept both `h264` and `hevc` (previously blocked `hevc`).
  - Replaced duplicate `shutil.copy2` in `composite_and_verify.py` and `verify_stage_5.py` with hardlinks (`os.link`, saving ~6.3 GB/series), and parallelized QA frame extraction (4 workers).
  - `ff_render_key` hashes font, encoder, and watermark; `band_y0` aligns to multiple of 8; `verify-overlay` records actual renderer (`ff-1.1`).
  - `aicn.py clean --unit U [--dry-run] [--deep]`: frees **~1.2 GB/batch** keeping `PPPPPP` gates intact (`~1.8 GB/batch` with `--deep`).
- **Doctor & Tests**: `doctor` detects `h264_videotoolbox + hevc_videotoolbox`; unit tests expanded and verified with 0 warnings.

## 1.2.1 (2026-11-19)
- MLX Whisper: forced Simplified Chinese (it returned 146 Traditional characters on unit 07) and drops segments Whisper marks as silence or repetition loops (no VAD in mlx-whisper). Full Stage 1 on unit 07: 105 s, sub-clause coverage 210/211 as before, punctuation now kept.
- `plan` understands the ffmpeg engine: prints `render=current` or `stale: <parts>`.
- The ffmpeg band is computed from the scan and the plates (`band_y0`) instead of a fixed y = 760, so 720p, vertical sources and high subtitles are masked in the right place.
- `tts_workers` option for VieNeu. Default 1: VieNeu ONNX already uses every core (3 workers measured 9% faster, 4 slower).

## 1.2.0 (2026-11-19)
- `aicn.py doctor`: probes chip, cores, RAM, ffmpeg filters/encoders, MLX, models and tools; writes `config/machine.json` and names the stack to use.
- Stage 4 default engine is now one ffmpeg pass (`ff-1.0`): Python only draws mask and plate PNGs, ffmpeg decodes, masks, overlays, encodes and muxes. Unit 07: 116 s instead of 207–400 s. Every source frame keeps its own timestamp (VFR-safe): 18,277 frames out of 18,277.
- Mask style `mask_mode`: `blur` (default) or `solid` (black box, about 12% faster); `render --mask` overrides.
- Unchanged inputs skip the render entirely. The old per-frame renderer stays as `render --engine python`.
- Stage 1 Whisper runs on MLX (Metal) when `mlx_whisper` is installed: 48 s instead of 194 s for one pass on unit 07. `AICN_ASR=faster` forces the CPU path.

## 1.1.0 (2026-11-19)
- New Stage 3 engine `scripts/aicn_audio.py` replaces `assemble_master_audio.py`, which replaced a missing Demucs stem with silence and shipped dub-only audio.
- Demucs (MLX) stems are mandatory. The original voice, music and effects stay in the master.
- Frequency-aware mix: 12 bands, about 10 ms frames, the original is ducked only where and as much as the dub needs, with floors (voice -18 dB, music -9 dB).
- Dub read per sentence (comma-chained clauses merged): fewer mis-toned short fragments.
- Speed: rubberband R3 with formants preserved (base 1.10×, up to 1.25× to fit). `atempo` removed: it doubled the word error rate.
- Expander removes the TTS hiss floor. Dub chain: high-pass, presence, 3:1 compression.
- Master: 2-pass loudnorm to -14 LUFS, -1 dBTP, 24-bit.
- Audio gate (`data/03_mix_report.json`): loudness, stems match source, dub intelligibility, original never muted, original present, dub fits.
- `references/audio_mix.md`; audio unit tests.

## 1.0.0 (2026-11-19)
- Renamed from `drama-reup-pipeline` to `aicn`; engine `scripts/aicn.py`.
- Stage skills vendored under `vendor/`; `cicd_gate.py` is found there first.
- Renderer styleB-1.4: per-Chinese-interval blur, on-frame width re-measure, mask grown by 1.5 × feather.
- Display windows: hold (overlap ≥ 0.25 s), snap (≤ 1.0 s late, previous cue ≤ +0.3 s), frame-quantized.
- Stage 4 gate checks blank coverage, line count, chunks, duration.
- `discover` marks gates whose media were deleted with `x`.
- Tested end to end on unit_07_ep61_70 (10 episodes, 609.5 s): full render 207–243 s, one-line fix 38 s.
- Unit tests for display windows, chunking, cache keys and the CLI.
