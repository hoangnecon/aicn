---
name: aicn-script
description: Use when separating audio stems, running Whisper ASR transcription, auditing source keyframes for homophones/names, locking character pronouns and speaker voices in series_bible.json, or translating and editing Vietnamese dubbing scripts ("tách thoại", "chạy ASR", "viết kịch bản", "dịch lời thoại", "chốt xưng hô").
---

# Transcribing & Adapting Dual-Layer Vietnamese Dub Scripts (`/aicn-script`)

## Overview
Drive **Stage 1** (Demucs stems + multi-pass Whisper `large-v3` ASR + `wf-4.0` acoustic snap + visual keyframe audit) and **Stage 2** (`series_bible.json` character/voice lock + dual-layer `02_bilingual_script.json` with `sub_text` and `<en>...</en>` `tts_text` passing `100/100` calibrated pacing validation).

- `<aicn>`: `$AICN_HOME` if set, else `~/.gemini/config/plugins/aicn` (`../..` from this skill).
- `$AICN_PY`: `C:\Work\Tools\VieNeu-TTS\.venv\Scripts\python.exe` (Windows) or `/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python` (macOS).
- Deep references: `<aicn>/references/visual_audit_handbook.md`, `<aicn>/references/syntax_and_register_guide.md`, `<aicn>/references/zh_vi_ai_tech_glossary.md`, `<aicn>/references/genre_playbooks.md`.

## Mandatory Recipe

### Part A — Stage 1: Warm Stems, Multi-Pass ASR & Visual Homophone Audit
1. **Run warm batch prep across pending units (loads Demucs once, Whisper `large-v3` once, then parallel CPU waveform snap & audit frame extraction):**
   ```bash
   $AICN_PY <aicn>/scripts/aicn.py run-series --hub <hub> --phase prep
   ```
   *(Or pass `--units <u1>,<u2>` to stream selected batches immediately.)*

2. **Visually inspect extracted `frames/audit/*.jpg` via `read_file` & sign Gate 1:**
   Open every frame listed in `<unit>/data/01_extracted_audit_manifest.json` using `read_file` to resolve CJK homophones, character names, titles, signs, and speaker gender/age. Record findings in `<unit>/01_visual_audit_notes.md` and `<unit>/data/01_asr_clean.json`, then verify `100/100`:
   ```bash
   $AICN_PY <aicn>/scripts/verify_stage_1.py --workspace <unit>
   ```

### Part B — Stage 2: Living `series_bible.json` & Dual-Layer `02_bilingual_script.json`
1. **Lock character pronouns, register & valid `VieNeu v3 Turbo` voices in `<hub>/config/series_bible.json`** (and sync `<unit>/config/series_bible.json`):
   Assign real `voices_v3_turbo.json` presets per character (`"Trúc Ly"`, `"Ngọc Huyền"`, `"Hải Đăng"`, `"Thanh Bình"`, `"Phạm Tuyên"`, etc.) plus exact directional pronouns (`anh/em`, `trẫm/ái khanh`, `sư phụ/đồ nhi`, `mày/tao` when furious).

2. **Author `<unit>/data/02_bilingual_script.json` from `01_asr_clean.json`:**
   Preserve exact monotonic `start`/`end` slots (`0 <= start[i] < end[i] <= start[i+1] - 0.02`, `dur >= 0.15s`) and populate for every cue:
   - `speaker`: canonical character key matching `series_bible.json`
   - `zh_text`: verified source transcript
   - `sub_text`: clean viewer `.srt` Vietnamese (`<= 84` chars, natural spoken phrasing, zero `<en>` tags)
   - `tts_text`: `VieNeu-TTS` performance line — wrap ALL Latin acronyms/English words in `<en>...</en>` (`<en>AI</en>`, `<en>CEO</en>`, `<en>VIP</en>`), expand numbers/units into spoken Vietnamese (`3000 năm` -> `ba nghìn năm`), split mixed alphanumerics (`F1` -> `<en>F</en> một`), and keep syllable density concise (`mean <= 5.6`, `max <= 6.8 syl/s` over elastic window) so Stage 3 never exceeds `max_tempo`.

3. **Validate & sign Gate 2 (`100/100` required):**
   ```bash
   $AICN_PY <aicn>/scripts/validate_bilingual_script.py --workspace <unit>
   ```

## Common Mistakes
- **Editing `sub_text` while leaving `tts_text` bloated:** Stage 3 synthesizes `tts_text`. Trim filler particles in `tts_text` whenever `validate_bilingual_script.py` flags duration budget overflow.
- **Leaving raw English/acronyms unwrapped in `tts_text`:** Causes Vietnamese letter-by-letter mispronunciation; always wrap in `<en>...</en>`.

**Next step:** Stream Stage 2-verified units immediately into `aicn-produce`.
