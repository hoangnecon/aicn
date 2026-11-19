# Vendored stage skills

Copies of the five stage skills `aicn` drives, kept here so the repo runs on its own.
They are unchanged from their source (`~/.gemini/config/skills/`), so their own SKILL.md
files still show paths like `.agents/skills/<name>/scripts/...`. With `aicn`, use
`<skill>/vendor/<name>/scripts/...` as in `references/stages.md`.

| Skill | Stage | Gate script |
|---|---|---|
| video-localization-orchestrator | 0, gate library (`cicd_gate.py`) | `init_workspace.py` |
| video-asr-visual-audit | 1 | `verify_stage_1.py` |
| zh-vi-script-adapter | 2 | `validate_bilingual_script.py` |
| vieneu-voice-pacing-mixer | 3 | `verify_stage_3.py` |
| video-localization-qa-review | 5 | `verify_stage_5.py` |

Stage 4 (style-B overlay) is `aicn.py verify-overlay`.
To update a vendored skill: `rsync -aL --exclude __pycache__ ~/.gemini/config/skills/<name> vendor/`, then run the tests.

**Retired in aicn:** `vieneu-voice-pacing-mixer/scripts/assemble_master_audio.py` and the
`synthesize_and_audit_pacing.py` → `atempo` path. With no Demucs stem, the assembler replaced
the film audio with silence. It also used `atempo`, which degrades the voice. Use
`scripts/aicn_audio.py` instead. `verify_stage_3.py` is still used, and `aicn_audio.py verify`
calls it.
