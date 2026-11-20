---
name: aicn
description: Use when starting, resuming, or editing a Chinese short-drama / AI-drama Vietsub or Vietnamese dub re-up (any series, any unit of episodes), when entering a workspace with series hubs and units/, when a subtitle or dubbed line must change after render, when a re-render or a re-run of Whisper/TTS feels too slow or would start from scratch, or when estimating how long a finished video takes to produce.
---

# AICN: drama re-up pipeline (full-series release, incremental)

## Overview

One entry point for every series. It drives five stage skills (vendored in `vendor/`:
orchestrator, ASR + visual audit, zh→vi script adapter, VieNeu voice + pacing mixer,
QA review) and adds root discovery, a fixed folder and log contract, content-hash
caching, hardware-overlapped full-series execution, and one-pass Style-B rendering.

> **Release rule (mandatory): 1 published video = 1 FULL series.**
> Every publishable release is ALWAYS the complete series with **full Vietnamese dubbing +
> full Vietnamese subtitles + branding** at `deliverables/final_<series>_full_vi.mp4` (and
> `deliverables/subtitles/final_<series>_full_vi.srt`). Working batches under `units/`
> exist **strictly as internal work slices** (for caching, incremental edits, QA, and
> GPU/CPU/Media-Engine pipelining) — **never** as separate uploads.

**Core principle: every expensive output is keyed by the hash of exactly the inputs
that produce it.** An edit dirties only the cues, TTS sentences and working batch whose
inputs changed. Everything else comes from cache. Nothing is recomputed because it is "safer".

| Variable | Value |
|---|---|
| `<skill>` | the folder holding this file |
| Engine | `python3 <skill>/scripts/aicn.py <cmd>` (`--help` lists commands) |
| Hongguo Sourcing CLI | `node <skill>/scripts/hg_tool.cjs <cmd>` (backed by `tools/hongguo-drama`; replaces retired `ohg`) |
| Stage scripts | `<skill>/vendor/<stage-skill>/scripts/*.py`, run with `$AICN_PY` (default `/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python`) |
| Gate tokens | `<unit>/.cicd/stage_N_gate.json`, signed, 100/100 required |
| RSI Ledger | `<skill>/LESSONS.md` + `references/recursive_self_improvement.md` |

## 0. Once per machine: doctor

```bash
python3 <skill>/scripts/aicn.py doctor --root <project-root>
```

It writes `config/machine.json` and prints the stack to use. On Apple Silicon that is:
Whisper on MLX (Metal), Demucs MLX, ffmpeg one-pass render with `h264_videotoolbox` /
`hevc_videotoolbox` (hardware decode + quality VBR), rubberband. Python never touches video
frames on the default path.

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

## 1b. Sourcing, Discovery Analytics & Candidate Lists (`tools/hongguo-drama`)

Full sourcing & analytics guide: `references/sourcing_and_analytics.md`.
Primary engine: **`jackotom/hongguo-drama`** at `<project-root>/tools/hongguo-drama` via `node <skill>/scripts/hg_tool.cjs` (`openhongguo`/`ohg` is retired and deleted).

- **Freshness-first discovery:** Always combine `hg_tool.cjs new --genre ai|real|all` (sorted by launch date descending, `红果首发`) with `hg_tool.cjs rank --category hot-ai-drama|hot-real-drama|hot-comic-drama` and `hg_tool.cjs search "<kw>"`. Do not pitch stale/old dramas as "trending".
- **Analytics filter:** Prefer `AI仿真人剧` (`hot-ai-drama`), `真人喜剧` (`hot-real-drama`), or `3D漫剧` (`hot-comic-drama`) in the **50–120 ep (~60–150 min)** sweet spot; verify playable episodes via `hg_tool.cjs episodes <series_id>` before pitching.
- **Mandatory Candidate List Contract:** Every shortlist presented to the user MUST be written to an artifact and include for **every** candidate:
  1. **Vietnamese translated title** + original Chinese title (`ZH`) + valid Vietnamese `--series-slug`.
  2. `series_id`, format/genre, episode count, estimated total duration, and recency/trend signal.
  3. **Vietnamese synopsis** (2–4 sentences covering hook, comedy/drama beat, and main cast).
  4. **Local `.jpg` cover thumbnail** fetched & converted via `hg_tool.cjs fetch-covers` and embedded directly in the artifact.
- **1080p Download + CENC Decrypt:** `node <skill>/scripts/hg_tool.cjs download <series_id> --episodes 1-10 --out <inbox_batch_dir> --quality 1080p` (uses V5 `video_tab` GET signing + local `spade_a` key derivation + `ffmpeg -decryption_key`).

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

New series: `aicn.py init-series --series-slug <slug> --title "<Tên Tiếng Việt>" --total-episodes N --rights <basis>`
- **Naming rule (mandatory, enforced by CLI):** `--series-slug` MUST be the **Vietnamese translated title**, **no accents**, **lowercase**, **hyphen-separated** (`[a-z0-9]+(-[a-z0-9]+)*`), e.g. `《那年十八我造神器》` → `nam-18-tuoi-toi-che-tao-than-khi`. Never use pinyin, underscores, uppercase, or arbitrary names.
- `--rights` is required: `own`, `licensed:<contract>`, `cps:<platform>`, `mcn:<name>`.
- New internal working batch: `init_workspace.py --topology series_episodic ...`, then `aicn.py adopt --unit <batch>`.
- Every folder, subfolder, Markdown checklist, and per-stage report template/format is specified in `references/folder_contract.md`.

## 3. Stages and gates

Exact commands per stage: `references/stages.md`.

| Stage | Does | Gate | Cached by |
|---|---|---|---|
| 0 Init | series hub + internal working batches (`units/*`), profile, episode manifest | stage_0 | n/a |
| 1 Prep (Stems + ASR + 5 fps scan + audit) | `aicn_audio.py separate` (Metal), MLX Whisper, 5 fps Chinese hardsub intervals, audit frames (`run-series --phase prep`) | `verify_stage_1.py` | stems & Stage 1 frozen by source sha256 |
| 2 Script | `02_bilingual_script.json` (translation, `sub_text`, `tts_text`) | `validate_bilingual_script.py` | none needed (seconds) |
| 3 Audio (full dub) | `aicn_audio.py all`: stems (cached) → CPU VieNeu TTS (2×4 threads) + parallel rubberband → cached-Z mix → fast 48 kHz master → verify | audio gate + `verify_stage_3.py` | stems: source sha; dub: per sentence sha256(text, voice, engine, sampling) |
| 4 Overlay (style B) | `render` (one ffmpeg pass: hwdec + VBR `--codec h264\|hevc\|h264_high`, optional `--brand-dir` dual-corner watermark) | `aicn.py verify-overlay` | whole batch: sha256(source, display cues, hardsub intervals, style, font, watermark, audio, encoder, renderer version); unchanged → skipped |
| 5 Batch QA | hardlink staging MP4, 4-worker QA frame extraction, **view every frame**, verify batch (`run-series --phase produce` automates S3→S5-extract with 3-engine overlap) | `verify_stage_5.py` | hardlink (0 extra bytes) |
| 6 Full-Series Release | **`aicn.py merge-series --hub <hub>`** once **ALL** working batches PASS Stage 5 → single full-series MP4 + SRT; optional `aicn.py clean` per batch | all batches `P` at Stage 5 | stream copy (seconds) |

The project requires **full Vietnamese dubbing (`voice_dub`) + full Vietnamese subtitles** on
every published series. Never downgrade to `sub_only` as a shortcut.
The old `assemble_master_audio.py` path is retired: it replaced a missing Demucs stem with
silence and dropped the original voice entirely. Do not use it.
(`aicn.py tts-sync` is legacy for the old per-chunk flow; Stage 3 uses `aicn_audio.py`.)

## 4. Edit loop (≈ 2–3 min per edit)

1. Edit the smallest thing: one item in `02_bilingual_script.json` (and the same id in `03_aligned_subtitles.json` for sub_only), or `config/overlay_style.json`.
2. Re-run the validator of the stage you edited.
3. `voice_dub`, if `tts_text` changed: `aicn_audio.py dub`, `mix`, `master`, `verify` (only the edited sentence is re-synthesized). For a mix-only change (`config/audio_mix.json`): `mix`, `master`, `verify`.
4. `aicn.py plan --unit U` (pass the same `--codec`/`--quality`/`--mask`/`--brand-dir` flags as `render`) says `render=current` or `stale: <parts>`; anything other than the part you edited means an unintended change. Then `aicn.py render --unit U`: one ffmpeg pass over the unit (≈ 1.7–1.8 min per 10 min of video). It is skipped when nothing it reads changed.
5. `aicn.py verify-overlay`, Stage 5 import with `--qa-timestamps` at cue midpoints (always including edited cues), view the QA frames of changed chunks, `verify_stage_5.py`.
6. `aicn.py log --unit U --kind fix --message "<what> (<why>) -> rerun: <stages/chunks>"`.

Undo is free: the old text restores the old key, and the chunk comes back from cache.

## 5. Style B (default overlay)

Translucent black plate hugging each line: Avenir Next Demi Bold 48 px, white text,
plate `rgba(0,0,0,135)`, radius 10, padding 20/10/12, line gap 12, ≤ 2 balanced lines,
≤ 1500 px per line, block centred on the unit's median hardsub centre (no jumping).
The Chinese hardsub is hidden **per Chinese interval from `01_zh_bbox_scan.json`, not per
Vietnamese cue**, with a feathered mask grown by 1.5 × feather. `mask_mode` in
`config/overlay_style.json`: `blur` (default, σ 22, ×0.82) or `solid` (black box).
The ffmpeg engine does not re-measure lines on the real frame; if a QA frame shows a
clipped stroke, raise `box_pad_x` or use `render --engine python` (re-measures, ≈ 3× slower).

## 6. Audio: dub over the film, never instead of it

Full design, parameters and tuning: `references/audio_mix.md`.

- The original voice, music and effects stay in the master. Demucs stems are mandatory: `mix` stops if they are missing.
- TTS reads whole sentences (comma-chained clauses of one speaker are merged), because short fragments come out with wrong tones.
- Speed: rubberband R3 with formants preserved, `base_tempo` 1.10, up to 1.25 to fit. Never `atempo`: it doubled the word error rate.
- Mix: 12 bands, about 10 ms frames. The original is turned down only in bands where the dub is active, only enough for the dub to be 10 dB above the voice and 6 dB above the music, never below its floor (voice -18 dB, music -9 dB).
- Master: -14 LUFS, -1 dBTP, 48 kHz, 24-bit. Delivered as AAC 192k.
- Listen to at least one dubbed passage and one passage without dub before release. Numbers do not catch a voice that sounds wrong.

## 7. Correctness rules (never traded for speed)

- Timeline truth is Whisper + the 5 fps scan. Never retime by eye; use `analyze_voice_waveform.py --snap-and-split`, then re-validate.
- Display follows the Chinese line; audio does not move. `show_a/show_b` hold a cue to its line end (overlap ≥ 0.25 s) and snap a late first cue (≤ 1.0 s) to the line start. `slot_start/slot_end` never change.
- `verify-overlay` fails on **blank**: a Chinese line blurred with no Vietnamese for > 0.30 s. Fix the script or Stage 1 data; do not raise the tolerance.
- The render keeps every source frame with its own timestamp (`-fps_mode passthrough`), so the output has the source frame count and audio cannot drift. Check the frame count after a render.
- Changed renderer code → bump `FF_RENDERER_VERSION` (or `RENDERER_VERSION` for `--engine python`) in `aicn.py`.
- The automated Stage 5 CV check has passed frames with visible Chinese strokes. Viewing each QA frame is mandatory.

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
| Stage 5 CV check fails on style B | QA timestamp in a gap between cues | timestamps inside cues (`04_overlay_geometry_log.json`) |
| Chinese strokes or ghosts visible though CV passed | old renderer (< styleB-1.4) | re-render; re-view the frame |
| `frames/qa/` has frames in another font | leftovers from an older render | only `data/04_qa_frames_manifest.json` counts |
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
- "Use `ohg` / `openhongguo` to download" → retired and deleted; always use `node <skill>/scripts/hg_tool.cjs` (`tools/hongguo-drama`).
- "Present a candidate list with only Chinese titles or without thumbnails" → never. Always include Vietnamese title, Vietnamese synopsis, `--series-slug`, recency/trend signal, and embedded local `.jpg` covers.
- "I'll remember not to make that mistake next time" → never prose-only; log `L-NNN` in `LESSONS.md` and codify into code, tests, or a hard gate.

References: `references/sourcing_and_analytics.md`, `references/recursive_self_improvement.md`, `references/stages.md`, `references/audio_mix.md`, `references/folder_contract.md`,
`references/incremental_build.md`, `references/production_time.md`, `LESSONS.md`.
