---
name: aicn-audit
description: Use when the user asks to review, inspect, check quality, verify gates, or diagnose issues in an existing series hub or unit without modifying files ("kiểm tra chất lượng", "audit bộ này", "soi xem có lỗi tràn câu hay lệch tiếng không", "review trước khi đăng").
---

# Forensic Read-Only Quality Audit (`/aicn-audit`)

## Overview
Perform a **100% read-only** multi-layer audit across chained CI/CD gate signatures (`0..5`), bilingual script integrity, Stage 3 utterance tempo/overflow telemetry, `-16 LUFS` mix compliance, visual QA frames, and full-series merge freshness. **Never edit scripts, re-render media, or write temp files into `workspace/` during an audit.**

- `<aicn>`: `$AICN_HOME` if set, else `~/.gemini/config/plugins/aicn` (`../..` from this skill).
- `$AICN_PY`: `C:\Work\Tools\VieNeu-TTS\.venv\Scripts\python.exe` (Windows) or `/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python` (macOS).
- Deep reference: `<aicn>/references/qa_defect_triage_manual.md`.

## Mandatory Read-Only Audit Recipe

1. **Run the deterministic non-mutating `audit` inspector:**
   ```bash
   $AICN_PY <aicn>/scripts/aicn.py audit --hub <hub> --strict --json
   ```
   *(Or `--unit <unit>` for a single batch. Note: exit code `2` signals defects or incomplete gates — inspect the JSON payload rather than treating exit `2` as a crash.)*

2. **Verify all 5 technical layers from the JSON report:**
   - **Chained Gates (`gates`):** Confirm every finished batch is `PPPPPP` (or `PPPxxP` after `clean --deep`). Any `F` or `x` before Stage 5 means an upstream file was edited after signing without rebuilding downstream stages.
   - **Script Integrity:** `0` CJK/Hangul residue, `0` empty `sub_text`/`tts_text`, `0` monotonic timestamp overlaps.
   - **Voice Pacing (`pacing`):** `overflows == 0`, `tempo_violations == 0`, `runts == 0`. Flag any utterance near the ceiling (`tempo > 1.24`) if the user reported rushed speech.
   - **Mix & Master (`mix`):** `passed == true` (`-16 ± 1 LUFS` / Gate 3 checks).
   - **Full-Series Release (`release`):** `stale_merge == false` (ensuring `final_<slug>_full_vi.mp4` is newer than every unit MP4), `av_sync_drift_s <= 0.50`, and branded posters present.

3. **Mandatory Visual Frame Proof (`read_file`):**
   Open `sample_qa_frames` from each unit (plus `posters/*_branded.png` if present) via `read_file`. Visually confirm:
   - Dual-corner channel logos are crisp, unclipped, and properly positioned.
   - Video frame has no black bars/stretching and zero burned-in Vietsub plates.

4. **Report structured findings (`Critical` / `Important` / `Minor`):**
   Cite exact `unit`, `cue_index`, and `full_span` / `local_span` (use `$AICN_PY <aicn>/scripts/aicn.py locate --hub <hub> ...` if pinpointing specific cues) and end by asking whether to run `/aicn-fix` on any reported defect.

## Rationalizations & Red Flags

| Excuse | Reality |
|---|---|
| "`stage_5_gate.json` says `PASS`, so everything is fine" | Post-gate edits or unmerged unit re-renders break downstream validity; `aicn.py audit` checks `upstream_signature`, SHA-256 hashes, and `stale_merge`. |
| "CLI JSON passed, no need to call `read_file` on `.jpg` frames" | CLI metrics cannot see a misplaced logo, stretched aspect ratio, or visual artifact. Always open sample QA frames via `read_file`. |
| "I found a typo while auditing, let me quickly edit and re-render" | `/aicn-audit` is strictly read-only. Report the defect first; repair belongs in `/aicn-fix`. |
