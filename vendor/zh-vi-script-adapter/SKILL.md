---
name: zh-vi-script-adapter
description: Use when translating Chinese or English video transcripts into Vietnamese subtitles and VieNeu-TTS dub scripts, requiring visual cue alignment, natural breathing phrase segmentation, calibrated duration budgeting, and VieNeu-TTS <en>...</en> IPA phonemization.
---

# Bilingual Dual-Layer Script Adapter, Budget Validator & IPA Gate (`v3.0.0`)

## Overview

Stage 2 of the video localization pipeline (`voice_dub`-only). Transforms strictly monotonic Stage 1 ASR clauses (`data/01_asr_raw_segments.json`) and visual audit corrections (`01_visual_audit_notes.md`) into a synchronized dual-layer script (`data/02_bilingual_script.json`) containing both clean display subtitles (`sub_text`) and IPA-verified speech input (`tts_text`). Enforces a **6-check, 100-point CI/CD gate (`validate_bilingual_script.py` → `.cicd/stage_2_gate.json`)** including a calibrated VieNeu-TTS duration predictor validated across every `v3` utterance group (`group_script_utterances_v3` — both singletons and `<= 0.12s` continuous comma chains) so Stage 2 catches overflow risks before Stage 3 without falsely penalizing interior grouped clauses.

**REQUIRED BACKGROUND:** Complete Stage 1 (`video-asr-visual-audit`) and verify `.cicd/stage_1_gate.json` == `PASS` first.

---

## Core Capabilities (`v3.0.0`)

### 1. Visual Anchor Locking (Head-Final to Head-Initial Syntax Mapping)
Chinese syntax is head-final (`在 [A] 的 [B] 中，通过 [C] 来 [D]`) while Vietnamese is head-initial (`[D] bằng [C] trong [B] của [A]`). Merging 5–8 seconds of clauses and inverting them reverses chronological visual cues.
- Preserve `1.5s–3.5s` clause boundaries and use **clause-locked progressive connectors** so every entity is spoken during the exact `[start, end]` window when it appears visually. See [syntax_and_register_guide.md](./references/syntax_and_register_guide.md).

### 2. Natural Breathing-Phrase Segmentation (`>= 55%` Clause Ratio)
Never collapse raw ASR clauses into long multi-sentence paragraphs.
- Combine two adjacent raw ASR fragments (`"raw_ids": [i, i+1]`) **only** when they share speaker/scene AND combined `sub_text <= 96 characters` (aim for `<= 84 chars`, `<= 2` balanced lines).
- Maintain `>= 55%` of raw clause count (`validate_bilingual_script.py` Check #2).

### 3. Syllable-Budget Condensation & Calibrated VieNeu Fit
Vietnamese expands dense Chinese idioms by 25–40% if translated literally.
- Individual clause density is guarded by `max_syl <= 6.8 syl/s` (mean `<= 5.6 syl/s`, ideal target `4.5–5.2 syl/s`).
- Utterance duration fit is validated per `v3` utterance group (`group_script_utterances_v3`: both standalone singletons and `<= 0.12s` same-speaker/voice continuous comma chains up to `3` clauses / `4.5s` span) against Stage 3's exact elastic window `avail = min(next_group_start - 0.05, group_end + 1.20) - group_start` at `est_dur / avail <= 1.26` (`<= 1.30` for `<= 2`-syl interjections) using the 753-WAV calibrated predictor:
  `est_dur(text) = 0.185 * vi_syls + 0.22 + 0.32 * en_words + 0.08 * soft_pauses + 0.14 * hard_pauses`
  (without falsely charging startup overhead or `50ms` gaps to interior grouped clauses).

### 4. Domain Terminology, Series Bible & Register Calibration
Always read and sync `config/series_bible.json` (character roster, `speaker` IDs, `xưng hô` pronoun matrix, world glossary) and log unit updates in `02_glossary_and_pronunciation.md`. Never flip pronoun pairs mid-series. See [zh_vi_ai_tech_glossary.md](./references/zh_vi_ai_tech_glossary.md).

### 5. Dual-Layer Script Architecture (`sub_text` vs. `tts_text`)
Every item in `data/02_bilingual_script.json` MUST include:
- `id`, `ep_id`, `speaker`, `raw_ids`, `start`, `end`, `zh_text`
- `sub_text`: Clean visual orthography for `.srt` (`DeepSeek-R1`, `SFT`, `3FS`, `KYC`).
- `tts_text`: Prosody-tuned text with `<en>...</en>` tags for `VieNeu-TTS` (`<en>Deep Seek R one</en>`, `<en>S F T</en>`, `<en>Three F S</en>`, `<en>K Y C</en>`).

```json
{
  "id": 46,
  "ep_id": "ep01",
  "speaker": "NARRATOR",
  "raw_ids": [65, 66],
  "start": 108.16,
  "end": 111.78,
  "zh_text": "模型就会学到在user标签和问题后面是assistant标签",
  "sub_text": "mô hình sẽ học được rằng: sau thẻ User và câu hỏi là thẻ Assistant,",
  "tts_text": "mô hình sẽ học được rằng, sau thẻ <en>User</en> và câu hỏi là thẻ <en>Assistant</en>,"
}
```

### 6. `VieNeu-TTS` `<en>...</en>` Phonetics & `sea-g2p` Engineering
Never write Vietnamese phonetic approximations (`đíp xích`, `ét ép tê`, `kây oai xi`) in `tts_text`. `VieNeu-TTS v3 Turbo` uses `sea-g2p`, switching to native English IPA inside `<en>...</en>`:
- **Letter-by-Letter Acronyms:** Separate letters with spaces: `SFT` → `<en>S F T</en>`, `RL` → `<en>R L</en>`, `KYC` → `<en>K Y C</en>`, `API` → `<en>A P I</en>`.
- **Alphanumeric Tokens:** Spell out digits as English words inside `<en>...</en>`: `3FS` → `<en>Three F S</en>`, `DeepSeek-R1` → `<en>Deep Seek R one</en>`, `GPT-4o` → `<en>G P T four O</en>`.
- **Brand & Proper Names:** Syllabify cleanly inside `<en>...</en>` when needed (`<en>An thro pic</en>`, `<en>Claude</en>`, `<en>Quen</en>`, `<en>Jen sen Hoang</en>`).

### 7. Punctuation Prosody Sculpting
In `tts_text`, replace colons `:` or parentheses `()` with natural commas `,` so `VieNeu-TTS` renders smooth continuation pitch without abrupt stops. Never leave double commas `,,` or unmatched quotes.

### 8. Stage 2 Cryptographic CI/CD Gate Validator (`validate_bilingual_script.py`, `100/100`, `voice_dub`)
Enforces 6 weighted deterministic checks:
1. `schema_and_monotonic_timing` (`15 pts`): Valid schema, non-empty `sub_text`/`tts_text`, strict non-overlapping monotonic `0.0 <= start[i] < end[i] <= start[i+1] - 0.02`, and `dur >= 0.15s`.
2. `timeline_and_clause_granularity` (`15 pts`): `100%` coverage of raw ASR timeline (`±1.5s`) and `>= 55%` of raw clause count.
3. `zero_foreign_char_leakage` (`20 pts`): `0` Chinese/Japanese/Korean characters (`\u3040-\u9fff`, `\uac00-\ud7af`) and `0` `,,` typos in `sub_text` or `tts_text`.
4. `alphanumeric_en_tag_compliance` (`15 pts`): `0` unwrapped mixed alphanumeric tokens (`3FS`, `R1`) outside `<en>...</en>` in `tts_text`.
5. `syllable_and_tts_duration_budget` (`15 pts`): `0` segments with `sub_text > 96 chars`, `mean_syllable_rate <= 5.6 syl/s`, `max_syllable_rate <= 6.8 syl/s` (individual clause density guard), AND calibrated VieNeu duration ratio `est_dur / avail <= 1.26` (`<= 1.30` for `<= 2`-syl interjections) across every `v3` utterance group (`group_script_utterances_v3` — both singletons and `<= 0.12s` continuous chains).
6. `live_seag2p_ipa_compilation` (`20 pts`): Compiles `100%` of `tts_text` entries through `vieneu_utils.phonemize_text.phonemize_text_with_emotions` with zero errors.

---

## Mandatory Validation Command

Run using `$AICN_PY` (`C:\Work\Tools\VieNeu-TTS\.venv\Scripts\python.exe` on Windows, `/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python` on macOS) after creating or editing `data/02_bilingual_script.json`:

```bash
$AICN_PY <aicn>/vendor/zh-vi-script-adapter/scripts/validate_bilingual_script.py \
  --workspace "workspace/<series>/units/<unit>"
```

Do NOT proceed to `vieneu-voice-pacing-mixer` until this command prints `Status: PASS | Score: 100.0/100` and writes `<unit>/.cicd/stage_2_gate.json`.
