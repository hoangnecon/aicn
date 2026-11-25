---
name: aicn
description: Use when starting, resuming, or editing a Chinese short-drama / AI-drama or English short-drama Vietnamese dub + Vietsub re-up (any series, any unit of episodes), when entering a workspace with series hubs and units/, when a subtitle or dubbed line must change after render, when a re-render or Whisper/TTS run is needed, or when estimating how long a finished video takes to produce.
---

# AICN: Full-Series Vietnamese Dubbing & Localization Pipeline (`v3.0.0`)

## Overview

One unified entry point for every series. It drives five stage skills (canonical under `vendor/`: orchestrator, ASR + acoustic/visual audit, zh/en→vi script adapter, VieNeu voice + single-STFT mixer, QA review) and adds root discovery, a relocation-safe folder and log contract, SQLite-WAL pure content-SHA-256 caching, VRAM-aware concurrent GPU slot pooling (`gpu_slot`), warm multi-unit model batching, and hardware-accelerated one-pass dub + pre-composited dual-corner watermark rendering (`FF_RENDERER_VERSION = v3-dub-wm-3.0`, no burned-in subtitle overlay).

> **Release & Mode Contract (mandatory):**
> 1. **1 published video = 1 FULL series** at `deliverables/final_<series>_full_vi.mp4` and `deliverables/subtitles/final_<series>_full_vi.srt`.
> 2. **`voice_dub` is mandatory** on every series (full Vietnamese dubbing + dual-corner watermark + external `.srt`). Never downgrade to subtitle-only video.
> 3. Working batches under `units/` exist **strictly as internal work slices** (for caching, incremental edits, QA, and parallel batching) — **never** as standalone uploads.

**Core principle: every expensive output is keyed strictly by the content SHA-256 of its inputs and parameters (`content_hash.db`, zero path dependence).** Moving or renaming a unit or series hub keeps 100% cache and CI/CD gate validity.

| Variable | Value |
|---|---|
| `<skill>` | Folder holding this `SKILL.md` |
| Engine | `$AICN_PY <skill>/scripts/aicn.py <cmd>` (`--help` lists commands) |
| Hongguo Sourcing CLI | `node <skill>/scripts/hg_tool.cjs <cmd>` (thin dynamic wrapper over `tools/hongguo-drama`; always run `check-upstream` first) |
| Douyin Sourcing CLI | `<dy_py> <skill>/scripts/dy_tool.py <cmd>` (Windows `tools\.venv-dy\Scripts\python.exe`, macOS `tools/.venv-dy/bin/python`) |
| English DramaDunyam CLI | `$AICN_PY <skill>/scripts/dd_tool.py top\|list\|search\|detail\|download` (clean `lang=en`, non-adult `dramadunyam.com` API) |
| Unified Radar & Index | `$AICN_PY <skill>/scripts/aicn_radar.py sync-all\|search\|top` (`data/drama_index.db`, Hongguo + Douyin + Bilibili + DramaDunyam) |
| Stage scripts | `<skill>/vendor/<stage-skill>/scripts/*.py`, run with `$AICN_PY` (Windows `C:\Work\Tools\VieNeu-TTS\.venv\Scripts\python.exe`, macOS `/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python`) |
| Gate tokens | `<unit>/.cicd/stage_N_gate.json` (`schema_version: "3.0"`, relocation-safe relative POSIX artifacts, 100/100 required) |
| RSI Ledger | `<skill>/LESSONS.md` + `references/recursive_self_improvement.md` |

## 0. Once per machine: doctor

```bash
$AICN_PY <skill>/scripts/aicn.py doctor --root <project-root>
```

Writes `config/machine.json`, probes GPU VRAM, `overlay_cuda`/`scale_cuda`/`hwupload_cuda` filters, encoders, and tools (`scripts/aicn_platform.py`):
- **Windows 10/11 + NVIDIA CUDA**: `faster-whisper` (`large-v3`, CUDA `int8_float16`), PyTorch `demucs` (`htdemucs` CUDA), single-process `VieNeu-TTS v3 Turbo` CUDA `infer_batch(16)`, FFmpeg hardware render (`h264_nvenc` / `hevc_nvenc` with `-hwaccel cuda` + `overlay_cuda`, or single static CPU `overlay` fallback), `rubberband` R3.
- **macOS Apple Silicon**: `mlx-whisper` (`large-v3` on Metal), `demucs-mlx` (`uvx`), `VieNeu-TTS v3 Turbo` ONNX `fp32`, FFmpeg `h264_videotoolbox` / `hevc_videotoolbox` with single static `overlay` strip, `rubberband` R3.
- **CPU Fallback**: `faster-whisper` `int8`, PyTorch `demucs`, ONNX `fp32`, `libx264` / `libx265`.

## 1. On entry: read the root

```bash
$AICN_PY <skill>/scripts/aicn.py discover --root <project-root>
```

Lists each Series Hub (`workspace/<series>/` with `units/` and `config/series_bible.json`), hub files, inbox, and each unit's gates as six letters:
`P` pass, `F` fail, `x` passed but its media was deleted, `-` missing.
**The next stage for a unit is the first position that is not `P`.**
Then read, in order: hub `PROGRESS.md`, the last 20 lines of the unit `CHANGELOG.md`, and the unit `CHECKLIST.md`. Do not open media to find out where things stand.

## 1b. Sourcing, Discovery Analytics & Candidate Lists (Chinese-First, Zero YouTube)

Full sourcing & analytics guide: `references/sourcing_and_analytics.md`.
Original-platform engines: **`jackotom/hongguo-drama`** (thin wrapper `scripts/hg_tool.cjs`), **`Evil0ctal/Douyin_TikTok_Download_API`** (`scripts/dy_tool.py`), **DramaDunyam EN** (`scripts/dd_tool.py`), **`yt-dlp`** (Bilibili `--user-agent`), indexed together by **`scripts/aicn_radar.py`** -> `data/drama_index.db`.

- **Mandatory Upstream Open-Source Check FIRST (zero hardcoding):** `scripts/hg_tool.cjs` is strictly a **thin dynamic wrapper** around `jackotom/hongguo-drama` — never hardcode upstream Hongguo private API routes, signing headers, or SDK internals in the skill or ad-hoc scripts. **Before ANY Hongguo search, ranking, episode inspection, or download**, run:
  ```bash
  node <skill>/scripts/hg_tool.cjs check-upstream --json
  ```
  Verify `up_to_date: true` and `wrapper_contract_ok: true`. If `up_to_date` is `false`, run `node <skill>/scripts/hg_tool.cjs check-upstream --sync` first and inspect `tools/hongguo-drama/src/native/` exports.
- **Original Chinese & verified clean English sources ONLY (NO YouTube):** Never search or download Chinese dramas from YouTube (YouTube hosts recaps/re-ups). Search in **Chinese keywords (`中文`)** on **Hongguo `红果`**, **Douyin `抖音`**, **Bilibili `B站`**, **Hema `河马`**, **Kuaishou `快手`**. For English AI/short dramas, use **`dd_tool.py`** (`dramadunyam.com`, `--lang en`, non-adult) and pass `--source-lang en` to `init-series`.
- **Search local analytics index first:** Run `aicn_radar.py sync-all` periodically, then query `aicn_radar.py search "<中文关键词>"` or `aicn_radar.py top --format ai --limit 15`.
- **Freshness-first live queries:** Combine `hg_tool.cjs new --genre ai|real|all`, `hg_tool.cjs rank --category hot-ai-drama|hot-real-drama|hot-comic-drama`, `dy_tool.py detail` + `related_posts`, and `yt-dlp bilisearch`. Do not pitch old series as "trending".
- **Mandatory Candidate List Contract:** Every shortlist presented to the user MUST be written to an artifact and include for **every** candidate:
  1. **Vietnamese translated title** + original Chinese title (`ZH`) + valid Vietnamese `--series-slug`.
  2. Platform (`hongguo`/`douyin`/`bilibili`/`dramadunyam`), `series_id` / `aweme_id`, format/genre, episode count, publish date (`age`), likes/favorites/heat score.
  3. **Vietnamese synopsis** (2–4 sentences covering hook, comedy/drama beat, and main cast).
  4. **Local `.jpg` cover thumbnail** fetched via `hg_tool.cjs fetch-covers`, `dy_tool.py detail --cover-out`, or `dd_tool.py download --cover-out` and embedded directly in the artifact.
- **Upload Caption & Poster Packaging Standard:** Full guide in `references/poster_and_caption_guide.md`.
  - **Caption (2-line minimal format):** Line 1: `<Catchy Vietnamese Hook Title> <2 emojis> | Full N Tập`; Line 2: `#daudonho #aishortfilmdrama #aishortdrama #aifilm #phimai #phimngan #phimthuyetminh #phimtrungquoc #<topic1> #<topic2>` (all-lowercase, no awkward genre calque tags).
  - **Poster / Thumbnail workflow:** Pull HD official source poster (`~tplv-resize:1440:0.image`) into `posters/official_poster_<slug>_zh.jpg`, localize `9:16` / `16:9` poster, then standardize & composite dual-corner branding via `aicn.py brand-poster --hub <hub> --input <raw_file> --brand-dir <brand_dir> --delete-source` and visually inspect `_branded.png` via `read_file`.

## 2. Folder contract

Full tree: `references/folder_contract.md`. Short form:

```text
workspace/<series>/                      Series Hub
  CHANGELOG.md  PROGRESS.md  00_series_manifest.md
  config/{rights.json,series_bible.json,series_config.json}
  source_inbox/<batch>/epNN.mp4          raw downloads, immutable
  units/<batch>/                         internal working batches (~10 eps each; NOT standalone uploads)
     CHECKLIST.md CHANGELOG.md 00..04.md
     config/ data/ source/ media/{stems,tts_segments,audio}/ frames/{audit,qa}/ .cicd/
     media/.staging_render.mp4  media/final_<batch>_vi.mp4
  posters/
     cover.jpg  official_poster_<slug>_zh.jpg  poster_<slug>_vi_<ratio>_{raw,clean,branded}.png
  deliverables/
     final_<series>_full_vi.mp4          THE publishable full-series master (via `merge-series`)
     subtitles/final_<series>_full_vi.srt
<project_root>/.cache/                   Global pure content-addressed cache
  content_hash.db  asr/  stems/  tts_sent/  rb_stretch/  wm_overlay/
```

New series: `$AICN_PY <skill>/scripts/aicn.py init-series --series-slug <slug> --title-vi "<Tên Tiếng Việt>" --title-zh "<Tên Gốc ZH>" --total-episodes N --rights <basis>`
- Update metadata anytime (including renaming slug across hub, config, bible, manifest & deliverables): `aicn.py update-series --hub workspace/<slug> [--series-slug <new-slug>] [--title-vi "<Tên VI>"] [--title-zh "<Tên ZH>"]`
- **Naming rule (mandatory, enforced by CLI):** `--series-slug` MUST be the **Vietnamese translated title**, **no accents**, **lowercase**, **hyphen-separated** (`^[a-z0-9]+(-[a-z0-9]+)*$`), e.g. `《那年十八我造神器》` → `nam-18-tuoi-toi-che-tao-than-khi`.
- `--rights` is required: `own`, `licensed:<contract>`, `cps:<platform>`, `mcn:<name>`.
- New internal working batch: `init_workspace.py --topology series_episodic ...`, then `aicn.py adopt --unit <batch>`.

## 3. Stages and gates

Exact commands per stage: `references/stages.md`.

| Stage | Does | Gate | Cached by (Pure Content SHA-256) |
|---|---|---|---|
| 0 Init | Series hub + internal working batches (`units/*`), fps/PTS-normalized `master_source.mp4` | `stage_0_gate.json` (`series_bible.json` decoupled) | `source/master_source.mp4` |
| 1 Prep (Stems + ASR + Audit) | Warm `batch-separate` (`htdemucs`, `vram_mb=1024`) + warm `run_asr_and_scan.py --units` (`large-v3`, `vram_mb=2560`, anti-orphan Pass 1 + collar-masked Pass 2B) + parallel CPU `analyze_voice_waveform.py --snap-and-split` (`wf-4.0`) + `extract_audit_frames.py` | `verify_stage_1.py` (100 pts) | `<global_cache>/stems/<stems_key>/` + `asr/<stage1_asr_key>.json` |
| 2 Script | `02_bilingual_script.json` (`zh_text`, `sub_text`, `tts_text`, `speaker`) + living `series_bible.json` | `validate_bilingual_script.py` (100 pts) | Stream each unit immediately on Stage 1 PASS |
| 3 Audio (Full Dub) | Warm `batch-all --units` (or `aicn_audio.py all --unit`): single CUDA VieNeu `infer_batch(16)` (`vram_mb=1024`) -> indivisible utterance grouping (`gap<=0.12s`, `<=3` clauses, `span<=4.5s`, **never sliced back into clause pieces**) -> parallel rubberband -> **fail-fast on overflow before `mix`** -> single-STFT + C-speed `follow_hold` mix (`<12s`/unit) -> fast 48 kHz 24-bit master -> verify | Audio gate (`03_mix_report.json`) + `verify_stage_3.py` (100 pts) | `<global_cache>/tts_sent/<tts_sent_key>.wav` + `rb_stretch/<rb_stretch_key>.wav` |
| 4 Render (Dub + Watermark) | `aicn.py render` (lock-free parallel: cached tight top-band RGBA PNG + `-hwaccel cuda`/`overlay_cuda` or single static `overlay`, instant `-c:v copy` if no watermark) | `aicn.py verify-render` (codec/audio, `-sn`, A/V sync `±0.30s`, exact frame count) | `render_key(src_sha, audio_sha, wm_key, enc, v3-dub-wm-3.0)` |
| 5 Batch QA | `composite_and_verify.py` (hardlink `.staging_render.mp4 -> .staging_final.mp4`, 4-worker QA frame extraction), **view every frame via `read_file`**, verify batch | `verify_stage_5.py` (100 pts) → `media/final_<batch>_vi.mp4` | Hardlink (0 extra bytes) |
| 6 Full-Series Release | `aicn.py merge-series --hub <hub>` once **ALL** working batches PASS Stage 5 → single full-series MP4 + SRT | All batches `P` at Stage 5 + stream/sync check | Stream copy (seconds) |

**VRAM-Aware Concurrency & Warm Batching (No Serial GPU Lock):**
- `aicn_platform.gpu_slot(task, vram_mb)` allocates 512 MB OS-locked VRAM slots (`RTX 2060 6 GB` = 10 slots: `whisper(5) + demucs(2) + vieneu(2) = 9 <= 10` run concurrently without OOM!).
- `run-series --phase prep|produce [--units ...]` batches units into ONE warm Demucs/Whisper/VieNeu CUDA session and runs NVENC/VideoToolbox Stage 4 renders + QA extraction lock-free in parallel (`AICN_MAX_RENDER_JOBS=2`).
- Stream units independently (`Stage 1 → 2 → 3 → 4 → 5`) via `--units` as soon as each unit passes Stage 2 — never wait for an all-batch barrier.

## 4. Edit loop (≈ 1 min per edit)

1. Edit the smallest thing: one item in `data/02_bilingual_script.json`.
2. Re-run `validate_bilingual_script.py --workspace $U`.
3. If `tts_text` changed: `$AICN_PY <skill>/scripts/aicn_audio.py dub --unit $U`, then `mix`, `master`, `verify` (only changed utterances are re-synthesized/re-stretched; everything else hits `tts_sent`/`rb_stretch` cache). For mix-only changes (`config/audio_mix.json`): `mix`, `master`, `verify`. If only `sub_text` changed: re-export SRT (`master`/`verify`) and re-run `merge-series` (no video re-render needed).
4. `$AICN_PY <skill>/scripts/aicn.py plan --unit $U` (pass matching `--codec`/`--quality`/`--brand-dir` flags) shows `render=current` or `stale: <parts>`. Then `$AICN_PY <skill>/scripts/aicn.py render --unit $U`.
5. `$AICN_PY <skill>/scripts/aicn.py verify-render --unit $U`, run `composite_and_verify.py` (defaults to `.staging_render.mp4`), inspect every QA frame in `read_file`, log PASS in `04_qa_review_report.md`, and run `verify_stage_5.py`.
6. `$AICN_PY <skill>/scripts/aicn.py log --unit $U --kind fix --message "<what> (<why>) -> rerun: <stages>"`.

## 5. Audio: dub over the film, never instead of it (`v3.0.0`)

Full design, parameters, and tuning: `references/audio_mix.md`.

- **Indivisible Utterances (Zero Mid-Word Chopping):** Adjacent same-speaker comma-chained clauses are merged ONLY when truly continuous (`gap <= 0.12s`, `<= 3` clauses, `span <= 4.5s`). Each utterance is synthesized once, trimmed (`5ms` guard + `10ms` cosine fade), stretched ONCE intact via `rubberband -3 -F -q` (`base_tempo = 1.08`, up to `1.28`, or `1.32` for `<= 2`-syl interjections) into elastic slack (`next_start - 0.05s`, max overhang `+1.20s`), and placed ONCE intact onto `dub_voice_48k.wav` (`media/tts_segments/utt_NNNN.wav`).
- **Fail-Fast Before Mix:** Any genuine overflow aborts `cmd_dub` immediately (exit code `2`) with exact syllable-trim advice — zero wasted `mix`/`master` runs.
- **Single-STFT + Clean Vocal Carving:** Demucs stems (`vocals.wav`, `accomp.wav`) are mandatory. One `torch.stft` (`scipy.fft` fallback) STFT per stem, C-speed chunked `follow_hold()`, and one combined `torch.istft` (`11.56s` / 10-min unit) duck `V` (`base_vocal_db = -10.0`, `speech_vocal_dip_db = -16.0` on `300 Hz–5 kHz` while active, clamped directly at `floor_vocal_db = -24.0` — never stacked) and `B` (`snr_bgm_db = 6.0`, `floor_bgm_db = -9.0`).
- **Master & Listen:** `-14.0 ± 1.0 LUFS`, true peak `<= -0.8 dBFS`, 48 kHz 24-bit (`AAC 192k` in MP4). Spot-listen to at least one dubbed passage and one undubbed passage before release.

## 6. Correctness rules (never traded for speed)

- Timeline truth is Whisper (`asr-4.0`) + acoustic waveform (`analyze_voice_waveform.py --snap-and-split`, `wf-4.0`), enforcing strict monotonic `0.0 <= start[i] < end[i] <= start[i+1] - 0.02` and `dur >= 0.15s`.
- `verify-render` checks `.staging_render.mp4`, `h264`/`hevc` + `aac 48000 Hz stereo` + `0` subtitle streams (`-sn`), and `av_sync_and_frame_integrity` (`±0.30s` duration tol + exact source frame count via `-fps_mode passthrough`).
- Changed renderer code → bump `FF_RENDERER_VERSION` in `aicn_platform.py`.
- Viewing every extracted QA frame (`frames/qa/*.jpg`) via `read_file` for dual-corner watermark placement and visual sanity is mandatory.

## 7. Production Time & Recursive Self-Improvement (RSI)

- Measured `v3.0.0` speeds and full-series timing: `references/production_time.md`.
- RSI protocol: `references/recursive_self_improvement.md`. Live ledger: `LESSONS.md`.
  1. **Detect & Measure** (`Measured` vs `Assumed` vs `Unknown`) → log `L-NNN` in `LESSONS.md`.
  2. **Codify into Guardrails** (code + `unittest` + hard gate/contract — never prose-only).
  3. **Verify & Bump** (`unittest` + smoke test + `CHANGELOG.md`).

## Common mistakes

| Symptom | Cause | Fix |
|---|---|---|
| `plan` says the source changed | `master_source.mp4` was rebuilt | Re-run Stage 1 for this unit, then downstream |
| `cmd_dub` exits 2 with `OVERFLOW` | Utterance exceeds elastic window even at `1.28x`/`1.32x` | Shorten `tts_text` of offending IDs in `02_bilingual_script.json`, re-run `validate_bilingual_script.py`, re-run `dub` |
| `dub_intelligible` FAIL in `cmd_verify` | Active-frame `dub_snr_speech_db_p10 < 4.0 dB` or `median < 10.0 dB` | Check `03_mix_stats.json`; ensure `base_vocal_db <= -10.0`, `speech_vocal_dip_db <= -16.0`, `snr_vocal_db >= 14.0` |
| `frames/qa/` has stale frames | Leftovers from an older render | Only `data/04_qa_frames_manifest.json` counts |
| `discover` shows `x` | Gate signed, media deleted | Restore media (verify sha256 against `00_episodes_manifest.json`), re-run that stage |
| A gate blocks after editing a JSON | Gates hash their artifacts | Re-run that stage's validator; never edit `.cicd/` manually |

## Red flags (stop)

- "Re-run the whole pipeline to be safe" → run `plan`; render only what it lists.
- "Whisper again, the text changed" → text edits never touch Stage 1.
- "Tick the CHECKLIST box now, run the gate later" → tick only after `PASS`.
- "Automated checks passed, skip viewing frames" → inspect every frame via `read_file`.
- "Rights later" → `init-series` will not run without `--rights`.
- "No stems, just mix the dub alone" → Demucs stems are mandatory; run `aicn_audio.py separate`.
- "Speed it up with FFmpeg audio tempo filters" → `rubberband` R3 (`-3 -F -q`) only.
- "Wait for all 13 units to finish Stage 2 before starting Stage 3" → stream ready units immediately via `--units`.
- "Upload one unit/batch as a standalone episode or ship subtitle-only video" → never. Every published release is 1 full series with full Vietnamese dubbing (`merge-series`).
- "Skip `check-upstream` or hardcode Hongguo private API routes / SDK internals" → forbidden; always run `hg_tool.cjs check-upstream [--sync]` first.
- "Grab Chinese dramas from YouTube or search in Vietnamese/English first" → forbidden; use original CN platforms (Hongguo, Douyin, Bilibili, Hema, Kuaishou) or `dd_tool.py` for English sources (`--source-lang en`).
- "Present a candidate list without Vietnamese title, synopsis, slug, or thumbnails" → never.
- "I'll remember not to make that mistake next time" → never prose-only; log `L-NNN` in `LESSONS.md` and codify into code, tests, or gates.

References: `references/stages.md`, `references/audio_mix.md`, `references/folder_contract.md`, `references/incremental_build.md`, `references/production_time.md`, `references/sourcing_and_analytics.md`, `references/poster_and_caption_guide.md`, `references/recursive_self_improvement.md`, `LESSONS.md`.
