---
name: aicn-fix
description: Use when the user reports any defect, error, mistranslation, wrong pronoun, mispronunciation, rushed or cut-off voice, missing dialogue line, bad BGM/vocal balance, watermark glitch, or poster issue after listening or watching ("sửa lỗi câu này", "dịch sai xưng hô", "giọng đọc bị gấp/chập/lệch nhịp", "thiếu câu thoại", "nhạc nền to quá").
---

# Surgical Defect Repair (`/aicn-fix`)

## Overview
Fix user-reported localization defects with **minimum blast radius and maximum SHA-256 cache reuse**. Pinpoint the exact unit, episode, cue, and utterance first; patch only the root-cause layer; regenerate only what changed (`100%` `content_hash.db` cache hit on unchanged sentences); and re-merge `final_<slug>_full_vi.mp4` via fast stream-copy.

- `<aicn>`: `$AICN_HOME` if set, else `~/.gemini/config/plugins/aicn` (`../..` from this skill).
- `$AICN_PY`: `C:\Work\Tools\VieNeu-TTS\.venv\Scripts\python.exe` (Windows) or `/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python` (macOS).
- Deep references: `<aicn>/references/incremental_build.md`, `<aicn>/references/qa_defect_triage_manual.md`, `<aicn>/references/audio_mix.md`.

## Mandatory 4-Step Surgical Protocol

### Step 1 — Pinpoint Exact Unit, Cue & Utterance (`aicn.py locate`)
Never guess batch offsets or grep blindly. Run `locate` using whatever clue the user gave (full-series timestamp `--full-at`, unit/episode timestamp `--ep` + `--at`, or spoken words `--query`):
```bash
$AICN_PY <aicn>/scripts/aicn.py locate \
  --hub <hub> \
  [--full-at 14:25] [--ep 14 --at 01:10] [--query "Sư phụ đi đâu"] \
  --window 2
```
Inspect the returned `unit`, `target_cue_index`, `sub_text`, `tts_text`, `speaker`, and Stage 3 `utterance` telemetry (`utt_id`, `seg_indices`, `voice`, `tempo`, `required_tempo`, `overflow_s`).

### Step 2 — Classify Root Layer & Apply Minimal Patch
- **Layer A — Wrong translation / pronoun / `<en>` pronunciation / rushed tempo (`tempo > 1.20` or `overflow_s > 0`):**
  Edit ONLY the target cue(s) in `<unit>/data/02_bilingual_script.json` — update **BOTH** `sub_text` and `tts_text` (shorten `tts_text` phrasing if rushed; wrap English/acronyms in `<en>...</en>`; update `series_bible.json` too if fixing a recurring character name/pronoun/voice).
- **Layer B — Missing or clipped source speech cue:**
  Inspect surrounding audio/frames (`frames/audit/` or narrow clip check), insert/adjust the monotonic non-overlapping cue in `01_asr_clean.json` + `02_bilingual_script.json` (`start[i] >= prev_end + 0.02`, `dur >= 0.15s`), and re-verify Stage 1 (`verify_stage_1.py`) before Stage 2.
- **Layer C — Wrong speaker voice or BGM/vocal balance:**
  House rule locks **100% single voice `"Trúc Ly"`** across every character and cue (never switch to other voices unless the user explicitly requested `"allow_multi_voice": true`). Update `<hub>/config/series_bible.json` + `<unit>/config/series_bible.json` or tune `<unit>/config/audio_mix.json` (`dub_over_vocal_lu`, `floor_vocal_db`, `floor_bgm_db` — see `<aicn>/references/audio_mix.md`). If rebuilding multiple units via `run-series --phase produce`, remove stale `.cicd/stage_{3,4,5}_gate.json` on affected units first so `run-series` does not skip them.
- **Layer D — Watermark / aspect / poster defect:**
  Fix `--brand-dir` / `project_config.json["watermark"]` (only Stage 4–6 needed; Stage 3 audio stays untouched!) or re-run `aicn.py brand-poster`.

### Step 3 — Cache-Warm Incremental Rebuild (Affected Unit Only)
Run ONLY from the patched stage forward on the **single affected `<unit>`** (unchanged utterances hit `<project_root>/.cache/tts_sent/` & `rb_stretch/` in `<0.1s`!):
```bash
# If Stage 2 script changed:
$AICN_PY <aicn>/scripts/validate_bilingual_script.py --workspace <unit>
# If Stage 2 or Stage 3 audio config/voices changed:
$AICN_PY <aicn>/scripts/aicn_audio.py all --unit <unit>
# Re-render & promote ONLY the affected unit:
$AICN_PY <aicn>/scripts/aicn.py render --unit <unit>
$AICN_PY <aicn>/scripts/aicn.py verify-render --unit <unit>
$AICN_PY <aicn>/scripts/composite_and_verify.py --video <unit>/source/master_source.mp4 --workspace <unit> --slug <unit_name> --import-existing-mp4 <unit>/media/.staging_render.mp4
# View updated QA frame via read_file, update 04_qa_review_report.md, then promote:
$AICN_PY <aicn>/scripts/verify_stage_5.py --workspace <unit>
# Stream-copy re-merge full series (~2-5s) & log fix:
$AICN_PY <aicn>/scripts/aicn.py merge-series --hub <hub>
$AICN_PY <aicn>/scripts/aicn.py log --unit <unit> --kind fix --message "<concise fix summary>"
```

### Step 4 — Verify Complete Chain
Run `$AICN_PY <aicn>/scripts/aicn.py audit --hub <hub> --strict` and confirm `Status=PASS` and `stale_merge=False` before reporting completion.

## Rationalizations & Red Flags

| Excuse | Reality |
|---|---|
| "Delete `.cache` or re-run all units to be safe" | Destroys pure content-SHA-256 cache hits and wastes 20+ minutes. Never wipe `.cache/content_hash.db` or touch unaffected units. |
| "Just edit `subtitles_full.srt` or `sub_text` directly" | Leaves spoken dub audio (`tts_text` -> `master_audio_48k.wav`) unpatched and breaks gate SHA-256 signatures. Always patch `02_bilingual_script.json` (`sub_text` + `tts_text`). |
| "Re-rendered the unit MP4, so we're done" | `deliverables/final_<slug>_full_vi.mp4` stays stale until `aicn.py merge-series --hub <hub>` runs (~3 seconds stream-copy). |
