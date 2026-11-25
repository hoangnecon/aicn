# AICN Drama Localization Plugin (`aicn` · v3.1.0) — Workspace Routing & Non-Bypass Rules

Applies whenever working on Chinese (`红果`/`抖音`/`B站`) or English (`DramaDunyam`) short-drama Vietnamese dubbing, or inside any workspace containing `series_bible.json`, `source_inbox/`, `units/`, `02_bilingual_script.json`, or `deliverables/final_*_full_vi.mp4`.

Plugin root (`<aicn>`): directory containing `plugin.json` and `scripts/aicn.py` (or `$AICN_HOME` if set).
Python runtime (`$AICN_PY`): `C:\Work\Tools\VieNeu-TTS\.venv\Scripts\python.exe` on Windows, `/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python` on macOS.

## 1. Intent-Driven Skill Routing (Load Before Acting)
Match the user's explicit `/aicn-*` command or natural-language goal and read its `SKILL.md` first:
- **Find / pitch new dramas (`/aicn-scout`):** `skills/aicn-scout/SKILL.md`
- **Download raw & scaffold Series Hub / batches (`/aicn-create`):** `skills/aicn-create/SKILL.md`
- **Stems + Whisper ASR + visual audit + VI dub script (`/aicn-script`):** `skills/aicn-script/SKILL.md`
- **VieNeu CUDA dub + STFT mix + NVENC watermark + full merge + poster (`/aicn-produce`):** `skills/aicn-produce/SKILL.md`
- **Read-only QA across gates, pacing, LUFS, frames & merge (`/aicn-audit`):** `skills/aicn-audit/SKILL.md`
- **Surgical repair of a reported line, voice, timing, mix, or logo defect (`/aicn-fix`):** `skills/aicn-fix/SKILL.md`
- **Workspace status, time estimate, intermediate cleanup, or raw-preserving reset (`/aicn-clean`):** `skills/aicn-clean/SKILL.md`

## 2. Mandatory Production Contracts
1. **1 Full Series = 1 Published Video:** Deliver `deliverables/final_<series>_full_vi.mp4` + `deliverables/subtitles/final_<series>_full_vi.srt` via `aicn.py merge-series`. Working folders under `units/` are strictly internal batches.
2. **Full `voice_dub` Mandatory:** Vietnamese dubbing + dual-corner channel watermark + external `.srt` (zero burned-in subtitle overlay, zero `sub_only` downgrade).
3. **Pure Content-SHA-256 Cache & Chained Gates:** Never wipe `<project_root>/.cache/content_hash.db` or re-run unaffected units during a fix. Use `aicn.py locate` for surgical fixes and `aicn.py audit` + `read_file` on JPEG frames for visual QA.
4. **Original Sources Only:** Never source Chinese dramas from YouTube; run `node <aicn>/scripts/hg_tool.cjs check-upstream --json` before any Hongguo query.
