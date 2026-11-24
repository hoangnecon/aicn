---
name: aicn
description: Use when starting, resuming, or editing a Chinese short-drama / AI-drama Vietsub or Vietnamese dub re-up (any series, any unit of episodes), when entering a workspace with series hubs and units/, when a subtitle or dubbed line must change after render, when a re-render or a re-run of Whisper/TTS feels too slow or would start from scratch, or when estimating how long a finished video takes to produce.
---

# AICN: drama re-up pipeline (full-series release, incremental)

## Overview

One entry point for every series. It drives five stage skills (vendored in `vendor/`:
orchestrator, ASR + visual audit, zh/en→vi script adapter, VieNeu voice + pacing mixer,
QA review) and adds root discovery, a fixed folder and log contract, content-hash
caching, hardware-overlapped full-series execution, and one-pass dub + dual-corner watermark rendering (v2.0.0 — no burned-in subtitle overlay or hardsub blurring).

> **Release rule (mandatory): 1 published video = 1 FULL series.**
> Every publishable release is ALWAYS the complete series with **full Vietnamese dubbing +
> dual-corner branding watermark + external `.srt` subtitles** at `deliverables/final_<series>_full_vi.mp4` (and
> `deliverables/subtitles/final_<series>_full_vi.srt`). Working batches under `units/`
> exist **strictly as internal work slices** (for caching, incremental edits, QA, and
> GPU/CPU/Media-Engine pipelining) — **never** as separate uploads.

**Core principle: every expensive output is keyed by the hash of exactly the inputs
that produce it.** An edit dirties only the cues, TTS sentences and working batch whose
inputs changed. Everything else comes from cache. Nothing is recomputed because it is "safer".

| Variable | Value |
|---|---|
| `<skill>` | the folder holding this file |
| Engine | `python <skill>/scripts/aicn.py <cmd>` (`--help` lists commands) |
| Hongguo Sourcing CLI | `node <skill>/scripts/hg_tool.cjs <cmd>` (backed by `tools/hongguo-drama`; replaces retired `ohg`) |
| Douyin Sourcing CLI | `<dy_py> <skill>/scripts/dy_tool.py <cmd>` (`tools/.venv-dy/bin/python` or `Scripts/python.exe`) |
| English DramaDunyam CLI | `python <skill>/scripts/dd_tool.py top\|list\|search\|detail\|download` (clean `lang=en`, non-adult `dramadunyam.com` API) |
| Unified Radar & Index | `python <skill>/scripts/aicn_radar.py sync-all\|search\|top` (`data/drama_index.db`, Hongguo + Douyin + Bilibili + DramaDunyam) |
| Stage scripts | `<skill>/vendor/<stage-skill>/scripts/*.py`, run with `$AICN_PY` (macOS `/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python`, Windows `C:\Work\Tools\VieNeu-TTS\.venv\Scripts\python.exe`) |
| Gate tokens | `<unit>/.cicd/stage_N_gate.json`, signed, 100/100 required |
| RSI Ledger | `<skill>/LESSONS.md` + `references/recursive_self_improvement.md` |

## 0. Once per machine: doctor

```bash
python <skill>/scripts/aicn.py doctor --root <project-root>
```

It writes `config/machine.json` and prints the stack to use (`scripts/aicn_platform.py`).
- **macOS Apple Silicon**: Whisper on MLX (Metal), Demucs MLX (`uvx`), ffmpeg one-pass render with `h264_videotoolbox` / `hevc_videotoolbox` (hardware decode + quality VBR), `rubberband`.
- **Windows 10/11 + NVIDIA CUDA**: `faster-whisper` on CUDA (`float16`), PyTorch `demucs` on CUDA, ffmpeg one-pass render with `h264_nvenc` / `hevc_nvenc` (`-rc vbr -cq`), `rubberband` (or `libx264` CPU fallback). Python never touches video frames on the default path.

## 1. On entry: read the root

```bash
python3 <skill>/scripts/aicn.py discover --root <project-root>
```

It lists each Series Hub (`workspace/<series>/` with `units/` and
`config/series_bible.json`), hub files, inbox, and each unit's gates as six letters:
`P` pass, `F` fail, `x` passed but its media was deleted, `-` missing.
**The next stage for a unit is the first position that is not `P`.**
Then read, in order: hub `PROGRESS.md`, the last 20 lines of the unit `CHANGELOG.md`,
the unit `CHECKLIST.md`. Do not open media to find out where things stand.

## 1b. Sourcing, Discovery Analytics & Candidate Lists (Chinese-First, Zero YouTube)

Full sourcing & analytics guide: `references/sourcing_and_analytics.md`.
Original-platform engines: **`jackotom/hongguo-drama`** (`scripts/hg_tool.cjs`), **`Evil0ctal/Douyin_TikTok_Download_API`** (`scripts/dy_tool.py`), **DramaDunyam EN** (`scripts/dd_tool.py`), **`yt-dlp`** (Bilibili `--user-agent`), indexed together by **`scripts/aicn_radar.py`** -> `data/drama_index.db`.

- **Chinese-original & verified clean English-distribution sources (NO YouTube):** Never search or download Chinese dramas from YouTube (YouTube hosts recaps/re-ups). For Chinese dramas, always start in **Chinese keywords (`中文`)** on **Hongguo `红果`**, **Douyin `抖音`**, **Bilibili `B站`**, **Hema `河马`**, **Kuaishou `快手`**. For English AI/short dramas (DramaBox/ReelShort/NetShort/FreeReels), use **`dd_tool.py`** (`dramadunyam.com`, `--lang en`, excluding `yetiskin: true`) — set `--source-lang en` at `init-series` so Stage 1 Whisper & 9:16 portrait typography adapt cleanly (`720x1280` supported end-to-end).
- **Search local analytics index first:** Run `aicn_radar.py sync-all` periodically, then query `aicn_radar.py search "<中文关键词>"` or `aicn_radar.py top --format ai --limit 15` (scored by recency-weighted engagement velocity from `first_visible_time`/`create_time` + `likes` + `favorites`).
- **Freshness-first live queries:** Combine `hg_tool.cjs new --genre ai|real|all`, `hg_tool.cjs rank --category hot-ai-drama|hot-real-drama|hot-comic-drama`, `dy_tool.py detail` + `related_posts`, and `yt-dlp bilisearch`. Do not pitch stale/old dramas as "trending".
- **Mandatory Candidate List Contract:** Every shortlist presented to the user MUST be written to an artifact and include for **every** candidate:
  1. **Vietnamese translated title** + original Chinese title (`ZH`) + valid Vietnamese `--series-slug`.
  2. Platform (`hongguo`/`douyin`/`bilibili`), `series_id` / `aweme_id`, format/genre, episode count, publish date (`age`), likes/favorites/heat score.
  3. **Vietnamese synopsis** (2–4 sentences covering hook, comedy/drama beat, and main cast).
  4. **Local `.jpg` cover thumbnail** fetched via `hg_tool.cjs fetch-covers` or `dy_tool.py detail --cover-out` and embedded directly in the artifact.
- **No-Watermark / 1080p Downloads:**
  - Hongguo: `node <skill>/scripts/hg_tool.cjs download <series_id> --episodes 1-10 --out <inbox_batch_dir> --quality 1080p`
  - Douyin: `tools/.venv-dy/bin/python <skill>/scripts/dy_tool.py download <aweme_id_or_mix_id> --out <inbox_batch_dir>` (clean watermark-free MP4)
- **Facebook Caption Standard (when user asks for upload metadata):** 2-line minimal format — Line 1: `[TRỌN BỘ THUYẾT MINH] <Hook Title> | Full N Tập`; Line 2: all-lowercase broad hashtags (`#aishortfilmdrama #aishortdrama #aifilm #phimai #phimngan #phimthuyetminh #phimtrungquoc ...`, never awkward genre calque tags).

## 2. Folder contract

Full tree: `references/folder_contract.md`. Short form:

```
workspace/<series>/                      Series Hub
  CHANGELOG.md  PROGRESS.md  config/series_bible.json  config/rights.json
  source_inbox/<batch>/epNN.mp4          raw downloads, never edited
  units/<batch>/                         internal working batches (~10 eps each; NOT standalone uploads)
     CHECKLIST.md CHANGELOG.md  config/ data/ source/ media/ frames/ .cicd/
     .cache/{timeline,tts_sent}/  media/.staging_render.mp4  media/final_<batch>_vi.mp4
  deliverables/
     final_<series>_full_vi.mp4          THE publishable full-series master (via `merge-series`)
     subtitles/final_<series>_full_vi.srt
```

New series: `aicn.py init-series --series-slug <slug> --title-vi "<Tên Tiếng Việt>" --title-zh "<Tên Gốc ZH>" --total-episodes N --rights <basis>`
- Update metadata anytime (including renaming slug across hub, config, bible, manifest & deliverables): `aicn.py update-series --hub workspace/<slug> [--series-slug <new-slug>] [--title-vi "<Tên Tiếng Việt>"] [--title-zh "<Tên Gốc ZH>"]`
- **Naming rule (mandatory, enforced by CLI):** `--series-slug` MUST be the **Vietnamese translated title**, **no accents**, **lowercase**, **hyphen-separated** (`[a-z0-9]+(-[a-z0-9]+)*`), e.g. `《那年十八我造神器》` → `nam-18-tuoi-toi-che-tao-than-khi`. Never use pinyin, underscores, uppercase, or arbitrary names.
- `--rights` is required: `own`, `licensed:<contract>`, `cps:<platform>`, `mcn:<name>`.
- New internal working batch: `init_workspace.py --topology series_episodic ...`, then `aicn.py adopt --unit <batch>`.
- Every folder, subfolder, Markdown checklist, and per-stage report template/format is specified in `references/folder_contract.md`.

## 3. Stages and gates

Exact commands per stage: `references/stages.md`.

| Stage | Does | Gate | Cached by |
|---|---|---|---|
| 0 Init | series hub + internal working batches (`units/*`), fps/PTS-normalized `master_source.mp4` | stage_0 (decoupled from `series_bible.json`) | `source/master_source.mp4` |
| 1 Prep (Stems + ASR + audit) | `run-series --phase prep` (GPU `separate+ASR` overlapped with CPU `waveform+audit`) | `verify_stage_1.py` | stems & Stage 1 frozen by source sha256 |
| 2 Script | `02_bilingual_script.json` (translation, `sub_text`, `tts_text`) + living `series_bible.json` | `validate_bilingual_script.py` | stream each unit immediately on Stage 1 PASS |
| 3 Audio (full dub) | `aicn_audio.py all`: stems (cached) → CPU VieNeu TTS (2×4 threads) + parallel rubberband → cached-Z mix → fast 48 kHz master → verify | audio gate + `verify_stage_3.py` | stems: source sha; dub: hub-shared `<hub>/.cache/tts_sent/<sha>.wav` |
| 4 Render (Dub + Watermark) | `render` (one ffmpeg pass: hardware `videotoolbox`/`nvenc` or `libx264` with `--brand-dir` dual-corner watermark, or instant video stream-copy when no watermark is set) | `aicn.py verify-render` / `verify-overlay` (codec/audio + A/V sync + frame count) | whole batch: sha256(source, audio, watermark, encoder, renderer version); unchanged → skipped |
| 5 Batch QA | hardlink staging MP4, 4-worker QA frame extraction, **view every frame**, verify batch (`run-series --phase produce --units ...` streams S3→S5 with 3-engine locks) | `verify_stage_5.py` | hardlink (0 extra bytes) |
| 6 Full-Series Release | **`aicn.py merge-series --hub <hub>`** once **ALL** working batches PASS Stage 5 (verifies uniform stream specs & A/V sync) → single full-series MP4 + SRT | all batches `P` at Stage 5 | stream copy (seconds) |

**Streaming & Lean Worker Cap (mandatory for ~45m target):**
- **Never wait for all batches at Stage 2:** stream units independently (`Stage 1 → 2 → 3 → 4 → 5`) via `aicn.py run-series --hub <hub> --phase produce --units <unit>` as soon as each unit passes Stage 2.
- **Keep at most 2–3 workers active** (1–2 Stage 2 translators + 1 `run-series` runner; auto-kill/pause idle workers). Never spawn >1 GPU (Metal/CUDA) job or >1 VieNeu TTS job concurrently; `run-series` enforces cross-process `gpu.lock` / `cpu.lock` / `media.lock` via `aicn_platform.file_lock`.

The project requires **full Vietnamese dubbing (`voice_dub`) + full Vietnamese subtitles** on
every published series. Never downgrade to `sub_only` as a shortcut.
The old `assemble_master_audio.py` path is retired: it replaced a missing Demucs stem with
silence and dropped the original voice entirely. Do not use it.
(`aicn.py tts-sync` is legacy for the old per-chunk flow; Stage 3 uses `aicn_audio.py`.)

## 4. Edit loop (≈ 1–2 min per edit)

1. Edit the smallest thing: one item in `02_bilingual_script.json`.
2. Re-run `validate_bilingual_script.py`.
3. `voice_dub`, if `tts_text` changed: `aicn_audio.py dub`, `mix`, `master`, `verify` (only the edited sentence is re-synthesized). For a mix-only change (`config/audio_mix.json`): `mix`, `master`, `verify`.
4. `aicn.py plan --unit U` (pass the same `--codec`/`--quality`/`--brand-dir` flags as `render`) says `render=current` or `stale: <parts>`; anything other than the part you edited means an unintended change. Then `aicn.py render --unit U`: one ffmpeg pass over the unit (watermarked encode or instant stream-copy when no watermark is set). It is skipped when nothing it reads changed.
5. `aicn.py verify-render --unit U`, Stage 5 import with `--qa-timestamps` at cue midpoints, view the QA frames for watermark/visual sanity, `verify_stage_5.py`.
6. `aicn.py log --unit U --kind fix --message "<what> (<why>) -> rerun: <stages>"`.

Undo is free: the old text restores the old key, and the cached TTS segment is restored.

## 5. Dub + Watermark + Translate Only (v2.0.0)

`aicn v2.0.0` produces clean dubbed + watermarked video with an external `.srt` track — **no burned-in subtitle overlay, no Style-B pill plates, and no GaussianBlur hardsub masking**.
- When `--brand-dir` (or `project_config.json["watermark"]`) is set, `render` composites the dual-corner PNGs (`13_top_left_logo_large.png` + `15_top_right_vietsub_floating.png`, scaled proportionally on portrait widths `< 1200`) via hardware `videotoolbox` (macOS), `nvenc` (NVIDIA GPU), or `libx264` (CPU fallback) with `-fps_mode passthrough -sn`.
- When no watermark is configured, `render` stream-copies video (`-c:v copy -sn`) and muxes `master_audio_48k.wav` in seconds.

## 6. Audio: dub over the film, never instead of it

Full design, parameters and tuning: `references/audio_mix.md`.

- The original voice, music and effects stay in the master. Demucs stems are mandatory: `mix` stops if they are missing.
- TTS reads whole sentences (comma-chained clauses of one speaker are merged), because short fragments come out with wrong tones.
- Speed: rubberband R3 with formants preserved, `base_tempo` 1.10, up to 1.25 to fit. Never `atempo`: it doubled the word error rate.
- Mix: 12 bands, about 10 ms frames. The original is turned down only in bands where the dub is active, only enough for the dub to be 10 dB above the voice and 6 dB above the music, never below its floor (voice -18 dB, music -9 dB).
- Master: -14 LUFS, -1 dBTP, 48 kHz, 24-bit. Delivered as AAC 192k.
- Listen to at least one dubbed passage and one passage without dub before release. Numbers do not catch a voice that sounds wrong.

## 7. Correctness rules (never traded for speed)

- Timeline truth is Whisper + acoustic waveform (`analyze_voice_waveform.py --snap-and-split`), then re-validate.
- `verify-render` / `verify-overlay` checks staging MP4 presence, `h264`/`hevc` + `aac 48000Hz stereo` + `0` subtitle streams (`-sn`), and `av_sync_and_frame_integrity` (duration & frame count match source).
- The render keeps every source frame with its own timestamp (`-fps_mode passthrough`), so the output has the source frame count and audio cannot drift. Check the frame count after a render.
- Changed renderer code → bump `FF_RENDERER_VERSION` in `aicn.py`.
- Viewing each extracted QA frame (`frames/qa/*.jpg`) for watermark placement and visual sanity is mandatory.

## 8. How long a video takes

Measured numbers and the formula: `references/production_time.md`.
Rule of thumb for one 10-episode unit (≈ 10 min, 1080p30, Apple Silicon) is in its first table.

## 9. Recursive Self-Improvement (RSI)

Full protocol: `references/recursive_self_improvement.md`. Live ledger: `LESSONS.md`.
Whenever an upstream API breaks, a QA gate fails, the user corrects an output/workflow, or a full series finishes:
1. **Detect & Measure:** Log exact error/metric (`Measured` vs `Assumed` vs `Unknown`) as `L-NNN` in `LESSONS.md`.
2. **Codify into Guardrails:** Turn every lesson into code (`scripts/`), a regression test (`tests/`), or a hard contract/Red Flag in `SKILL.md` — never prose-only promises.
3. **Verify & Bump:** Run the full `unittest` suite + live smoke test, update `CHANGELOG.md`, commit, and tag `skills/aicn`.

## Common mistakes

| Symptom | Cause | Fix |
|---|---|---|
| `plan` says the source changed | `master_source.mp4` was rebuilt | re-run Stage 1 for this unit, then downstream |
| Final has only the dub voice, no film sound | old `assemble_master_audio.py` with no Demucs stem | use `aicn_audio.py all`; its gate checks `original_present` |
| Dub sounds slurred or robotic on some lines | `atempo` speed-up, or a 2–4 syllable clause read alone | `aicn_audio.py dub` (sentences + rubberband) |
| `OVERFLOW` in `dub` | sentence too long even at 1.25× | shorten `tts_text` of those ids in Stage 2 |
| Hiss between dubbed words | TTS noise floor raised by the level match | keep the expander (`expander_*` in `audio_mix.json`) |
| `frames/qa/` has stale frames | leftovers from an older render | only `data/04_qa_frames_manifest.json` counts |
| `discover` shows `x` | gate signed, media deleted | restore media (verify sha256 against `00_episodes_manifest.json`), re-run that stage |
| A gate blocks after editing a JSON | gates hash their artifacts | re-run that stage's validator; never edit `.cicd/` |

## Red flags (stop)

- "Re-run the whole pipeline to be safe" → run `plan`; render only what it lists.
- "Whisper again, the text changed" → text edits never touch Stage 1.
- "Tick the CHECKLIST box now, run the gate later" → tick after PASS.
- "CV check passed, skip viewing frames" → view them.
- "Rights later" → `init-series` will not run without `--rights`.
- "No stems, just mix the dub alone" → that is the bug that removed the film sound. Run `separate`.
- "Speed it up with atempo" → rubberband only.
- "Render frames in Python, it is more flexible" → ffmpeg one pass; Python draws PNGs only.
- "faster-whisper is what the script used" → run `doctor`; on Apple Silicon use MLX.
- "Upload one unit/batch as a standalone episode" → never. `units/` are internal working batches; every published video must be the full series assembled by `aicn.py merge-series`.
- "Skip dubbing (`sub_only`) to hit a time target" → never. Every full-series release requires complete Vietnamese dubbing + subtitles.
- "Use `ohg` / `openhongguo` to download" → retired and deleted; always use `hg_tool.cjs` (Hongguo) or `dy_tool.py` (Douyin).
- "Grab Chinese dramas from YouTube or search in Vietnamese/English first" → forbidden. YouTube is recap/re-up. Search in Chinese and acquire only from Hongguo, Douyin, Bilibili, Hema, or Kuaishou.
- "Present a candidate list with only Chinese titles or without thumbnails" → never. Always include Vietnamese title, Vietnamese synopsis, `--series-slug`, recency/trend signal, and embedded local `.jpg` covers.
- "I'll remember not to make that mistake next time" → never prose-only; log `L-NNN` in `LESSONS.md` and codify into code, tests, or a hard gate.

References: `references/sourcing_and_analytics.md`, `references/recursive_self_improvement.md`, `references/stages.md`, `references/audio_mix.md`, `references/folder_contract.md`,
`references/incremental_build.md`, `references/production_time.md`, `LESSONS.md`.
