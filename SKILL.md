---
name: aicn
description: Use when starting, resuming, or editing a Chinese short-drama / AI-drama Vietsub or Vietnamese dub re-up (any series, any unit of episodes), when entering a workspace with series hubs and units/, when a subtitle or dubbed line must change after render, when a re-render or a re-run of Whisper/TTS feels too slow or would start from scratch, or when estimating how long a finished video takes to produce.
---

# AICN: drama re-up pipeline (series-general, incremental)

## Overview

One entry point for every series. It drives five stage skills (vendored in `vendor/`:
orchestrator, ASR + visual audit, zh→vi script adapter, VieNeu voice + pacing mixer,
QA review) and adds root discovery, a fixed folder and log contract, content-hash
caching, and the style-B subtitle overlay rendered per chunk.

**Core principle: every expensive output is keyed by the hash of exactly the inputs
that produce it.** An edit dirties only the cues, TTS lines and chunks whose inputs
changed. Everything else comes from cache. Nothing is recomputed because it is "safer".

| Variable | Value |
|---|---|
| `<skill>` | the folder holding this file |
| Engine | `python3 <skill>/scripts/aicn.py <cmd>` (`--help` lists commands) |
| Stage scripts | `<skill>/vendor/<stage-skill>/scripts/*.py`, run with `$AICN_PY` (default `/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python`) |
| Gate tokens | `<unit>/.cicd/stage_N_gate.json`, signed, 100/100 required |

## 0. Once per machine: doctor

```bash
python3 <skill>/scripts/aicn.py doctor --root <project-root>
```

It writes `config/machine.json` and prints the stack to use. On Apple Silicon that is:
Whisper on MLX (Metal), Demucs MLX, ffmpeg one-pass render with `h264_videotoolbox`,
rubberband. Python never touches video frames on the default path.

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

## 2. Folder contract

Full tree: `references/folder_contract.md`. Short form:

```
workspace/<series>/                      Series Hub
  CHANGELOG.md  PROGRESS.md  config/series_bible.json  config/rights.json
  source_inbox/<batch>/epNN.mp4          raw downloads, never edited
  units/<unit>/                          10 episodes each
     CHECKLIST.md CHANGELOG.md  config/ data/ source/ media/ frames/ .cicd/
     .cache/{chunks,tts}/  media/.staging_render.mp4  media/final_<unit>_vi.mp4
  deliverables/{16x9,9x16,subtitles,episodes}/
```

New series: `aicn.py init-series --series-slug S --title T --total-episodes N --rights <basis>`
(`--rights` is required: `own`, `licensed:<contract>`, `cps:<platform>`, `mcn:<name>`).
New unit: `init_workspace.py --topology series_episodic ...`, then `aicn.py adopt --unit <unit>`.
Units made before this skill are adopted the same way.

## 3. Stages and gates

Exact commands per stage: `references/stages.md`.

| Stage | Does | Gate | Cached by |
|---|---|---|---|
| 0 Init | workspace, profile, episode manifest | stage_0 | n/a |
| 1 ASR + 5 fps hardsub scan + audit | Whisper timeline, Chinese line intervals, audit frames | `verify_stage_1.py` | source sha256. **Frozen** unless `plan` says the source changed |
| 2 Script | `02_bilingual_script.json` (translation, `sub_text`, `tts_text`) | `validate_bilingual_script.py` | none needed (seconds) |
| 3 Audio | `aicn_audio.py all`: separate → dub → mix → master → verify | audio gate + `verify_stage_3.py` | stems: source sha; dub: per sentence sha256(text, voice, engine, sampling) |
| 4 Overlay (style B) | `render` (one ffmpeg pass) | `aicn.py verify-overlay` | whole unit: sha256(source, display cues, hardsub intervals, style, audio, encoder, renderer version); unchanged → skipped |
| 5 QA + release | import staging MP4, QA frames, **view every frame**, log rows | `verify_stage_5.py` | n/a (stream copy) |

`sub_only` mode: Stage 3 masters the original stereo (`aicn_audio.py all` skips separate/dub/mix).
The old `assemble_master_audio.py` path is retired: it replaced a missing Demucs stem with
silence and dropped the original voice entirely. Do not use it.

## 4. Edit loop (≈ 2–3 min per edit)

1. Edit the smallest thing: one item in `02_bilingual_script.json` (and the same id in `03_aligned_subtitles.json` for sub_only), or `config/overlay_style.json`.
2. Re-run the validator of the stage you edited.
3. `voice_dub`, if `tts_text` changed: `aicn_audio.py dub`, `mix`, `master`, `verify` (only the edited sentence is re-synthesized). For a mix-only change (`config/audio_mix.json`): `mix`, `master`, `verify`.
4. `aicn.py plan --unit U` says `render=current` or `stale: <parts>`; anything other than the part you edited means an unintended change. Then `aicn.py render --unit U`: one ffmpeg pass over the unit (≈ 2 min per 10 min of video). It is skipped when nothing it reads changed.
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

References: `references/stages.md`, `references/audio_mix.md`, `references/folder_contract.md`,
`references/incremental_build.md`, `references/production_time.md`.
