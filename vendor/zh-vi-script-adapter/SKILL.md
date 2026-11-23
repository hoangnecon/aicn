---
name: zh-vi-script-adapter
description: Use when translating Chinese video transcripts into Vietnamese subtitles and TTS scripts, requiring visual cue alignment, natural breathing phrase segmentation, and VieNeu-TTS <en>...</en> IPA phonemization.
---

# Chinese-to-Vietnamese Dual-Layer Script Adapter & IPA Validator

## Overview

Stage 2 of the video localization pipeline. Transforms fine-grained ASR clauses (`data/01_asr_raw_segments.json`) and visual frame audit corrections (`01_visual_audit_notes.md`) into a synchronized dual-layer script (`data/02_bilingual_script.json`) containing both clean display subtitles (`sub_text`) and IPA-verified speech input (`tts_text`). Supports both **`voice_dub` mode** (tight spoken syllable rate `<= 6.8 syl/s` and `<en>...</en>` IPA tags) and **`sub_only` mode** (subtitle-only reading budget `<= 7.8 syl/s` covering 100% of dialogue, phone calls, radio comms, PA broadcasts, and whispers). Enforces a **6-check, 100-point CI/CD gate (`validate_bilingual_script.py` -> `.cicd/stage_2_gate.json`)**.

**REQUIRED BACKGROUND:** Complete Stage 1 (`video-asr-visual-audit`) and verify `.cicd/stage_1_gate.json` == `PASS` first.

---

## Internal Sub-Skills Catalog (8 Core Capabilities)

### Sub-Skill 3.1: Visual Anchor Locking (Head-Final to Head-Initial Syntax Mapping)
Chinese syntax is head-final (`在 [A] 的 [B] 中，通过 [C] 来 [D]`) while Vietnamese is head-initial (`[D] bằng [C] trong [B] của [A]`). Merging 5–8 seconds of Chinese clauses and inverting them into textbook Vietnamese reverses the chronological order of concepts on screen (e.g., speaking `"thẻ User"` while the red pencil on screen points at `<|end|>`).
- **Mandatory Technique:** Preserve `1.5s–3.5s` clause boundaries and use **clause-locked progressive connectors** so every concept is named in Vietnamese during the exact `[start, end]` window when it appears visually on screen. See [syntax_and_register_guide.md](./references/syntax_and_register_guide.md) for concrete before/after transformations.

### Sub-Skill 3.2: Natural Breathing-Phrase Segmentation (`>= 55%` Clause Ratio)
Never collapse 440 short ASR clauses into 150–190 long paragraphs.
- Combine two adjacent raw ASR fragments (`"raw_ids": [i, i+1]`) **only** when they belong to the **exact same static visual shot** AND the combined `sub_text` is `<= 84 characters` (`<= 2` balanced lines).
- For a 12-minute (~440 raw clause) video, the adapted script MUST contain **260–340 natural breathing phrases** (`>= 55%` of raw clause count, enforced by Check #2 of `validate_bilingual_script.py`).

### Sub-Skill 3.3: Syllable-Budget & Condensation Engineering
Spoken Vietnamese translations of dense Chinese technical commentary naturally expand by 25–40% if translated word-for-word.
- Target syllable density: **`4.5 – 5.2 syllables/second`** (`mean <= 5.6 syl/s`, hard per-segment ceiling `<= 6.8 syl/s`).
- Strip bureaucratic nominalizations (*"tiến hành thực hiện việc"* -> omit) and condense 4-character Chinese idioms (`四字成语`) into sharp 2–4 syllable Vietnamese equivalents.

### Sub-Skill 3.4: Domain Terminology & Register Calibration
Adapt tone and pronoun pairs (`mình/các bạn` for tech review; locked `tôi/cậu`, `anh/em`, `Ngài/tôi` pairs for multi-character short dramas) and log all recurring terms in `workspace/<slug>/02_glossary_and_pronunciation.md`. See [zh_vi_ai_tech_glossary.md](./references/zh_vi_ai_tech_glossary.md) for the canonical Chinese-to-Vietnamese AI/LLM and business glossary.

### Sub-Skill 3.5: Dual-Layer Script Architecture (`sub_text` vs. `tts_text`)
Every item in `data/02_bilingual_script.json` MUST contain both layers:
- `sub_text`: Clean visual orthography for on-screen reading (`DeepSeek-R1`, `SFT`, `3FS`, `KYC`, `64 quỹ đạo`).
- `tts_text`: Prosody-tuned text with `<en>...</en>` tags for `VieNeu-TTS` (`<en>Deep Seek R one</en>`, `<en>S F T</en>`, `<en>Three F S</en>`, `<en>K Y C</en>`).

```json
{
  "id": 46,
  "raw_ids": [65, 66],
  "start": 108.16,
  "end": 111.78,
  "zh_text": "模型就会学到在user标签和问题后面是assistant标签",
  "sub_text": "mô hình sẽ học được rằng: sau thẻ User và câu hỏi là thẻ Assistant,",
  "tts_text": "mô hình sẽ học được rằng, sau thẻ <en>User</en> và câu hỏi là thẻ <en>Assistant</en>,"
}
```

### Sub-Skill 3.6: `VieNeu-TTS` `<en>...</en>` Phonetics & `sea-g2p` Engineering
**STRICT PROHIBITION:** Never write crude Vietnamese phonetic approximations (`đíp xích`, `ét ép tê`, `kây oai xi`, `cờ lau đờ`) in `tts_text`. `VieNeu-TTS v3 Turbo` uses `sea-g2p`, which switches to native English IPA inside `<en>...</en>` tags:
- **Letter-by-Letter Acronyms:** Separate letters with spaces inside `<en>...</en>`:
  - `SFT` → `<en>S F T</en>`
  - `RL` → `<en>R L</en>`
  - `KYC` → `<en>K Y C</en>`
  - `API` → `<en>A P I</en>`
  - `DSH` → `<en>D S H</en>`
- **Alphanumeric Tokens (`3FS`, `R1`, `V3`, `GPT-4o`):** Expand the digit to an English word inside `<en>...</en>`:
  - `3FS` → `<en>Three F S</en>`
  - `DeepSeek-R1` → `<en>Deep Seek R one</en>`
  - `GPT-4o` → `<en>G P T four O</en>`
- **Brand & Proper Names:** Syllabify cleanly inside `<en>...</en>` when needed (`<en>An thro pic</en>`, `<en>Claude</en>`, `<en>Deep In fer</en>`, `<en>Quen</en>`, `<en>Closed A I</en>`, `<en>Jen sen Hoang</en>`).

### Sub-Skill 3.7: Punctuation Prosody Sculpting
In `tts_text`, replace colons `:` or parentheses `()` with natural commas `,` so `VieNeu-TTS` inserts smooth `120ms–160ms` continuation breaths without abrupt pitch resets. Never leave double commas `,,` or trailing unmatched quotes.

### Sub-Skill 3.8: Stage 2 Cryptographic CI/CD Gate Validator (`validate_bilingual_script.py`)
Enforces 6 weighted deterministic checks (`100/100` required to sign `.cicd/stage_2_gate.json`):
1. `schema_and_monotonic_timing` (`15 pts`): Valid schema, non-empty `sub_text`/`tts_text`, strictly monotonic `start` timestamps.
2. `timeline_and_clause_granularity` (`15 pts`): Covers 100% of the ASR timeline (`±1.5s`) and keeps `>= 55%` of raw clause count (blocks paragraph over-merging).
3. `zero_foreign_char_leakage` (`20 pts`): `0` Chinese/Japanese/Korean characters (`\u3040-\u9fff`, `\uac00-\ud7af`) and `0` `,,` typos in `sub_text` or `tts_text`.
4. `alphanumeric_en_tag_compliance` (`15 pts`): `0` unwrapped mixed alphanumeric tokens (`3FS`, `R1`) outside `<en>...</en>` in `tts_text`.
5. `syllable_and_line_length_budget` (`15 pts`): `0` segments with `sub_text > 96 chars`, `mean_syllable_rate <= 5.6 syl/s`, `max_syllable_rate <= 6.8 syl/s`.
6. `live_seag2p_ipa_compilation` (`20 pts`): Compiles 100% of `tts_text` entries through `vieneu_utils.phonemize_text.phonemize_text_with_emotions` with zero errors.

---

## Mandatory Validation Command

Run `validate_bilingual_script.py` using `$AICN_PY` (macOS `/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python`, Windows `C:\Work\Tools\VieNeu-TTS\.venv\Scripts\python.exe`) after generating or editing `data/02_bilingual_script.json`:

```bash
$AICN_PY .agents/skills/zh-vi-script-adapter/scripts/validate_bilingual_script.py \
  --workspace "workspace/bilibili-anthropic-distill"
```

Do NOT proceed to `vieneu-voice-pacing-mixer` until this command prints `Status: PASS | Score: 100.0/100` and writes `workspace/<slug>/.cicd/stage_2_gate.json`.
