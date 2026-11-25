# Vendored Stage Skills (`v3.0.0`)

`aicn` is the canonical source of truth for all five stage skills in `vendor/`. Both `install.ps1` (Windows real directory sync via `.aicn-managed`) and `install.sh` (macOS/Linux symlinks or `--sync`) publish `aicn` and all five stage skills from here into `~/.gemini/config/skills/` and `~/.claude/skills/`.

All stage skills and scripts share the unified `v3.0.0` architecture (`voice_dub`-only, pure content-SHA-256 `content_hash.db` caching, VRAM-aware `gpu_slot` pooling, warm multi-unit batching, and hardware-accelerated dub + watermark rendering). Always invoke stage scripts with `$AICN_PY` via `<aicn>/vendor/<name>/scripts/...` (or the installed `<skills>/<name>/scripts/...` sibling copy, both of which resolve `aicn_platform.py` automatically).

| Skill | Stage | Gate / Validator Script |
|---|---|---|
| `video-localization-orchestrator` | Stage 0 Init & CI/CD gate engine (`cicd_gate.py`) | `init_workspace.py` |
| `video-asr-visual-audit` | Stage 1 Multi-pass Whisper ASR (`asr-4.0`) + Acoustic Alignment (`wf-4.0`) + Audit Frames | `verify_stage_1.py` |
| `zh-vi-script-adapter` | Stage 2 Dual-layer VI script (`sub_text` + `<en>` `tts_text`) & calibrated TTS duration budget | `validate_bilingual_script.py` |
| `vieneu-voice-pacing-mixer` | Stage 3 Indivisible VieNeu-TTS dub + single-STFT DSP mix (`scripts/aicn_audio.py`) | `verify_stage_3.py` |
| `video-localization-qa-review` | Stage 5 Staging hardlink + parallel QA frame extraction & visual verification | `verify_stage_5.py` |

- **Stage 4 (Dub + Watermark Render):** handled directly by `<aicn>/scripts/aicn.py render` and gated by `<aicn>/scripts/aicn.py verify-render`.
- **Stage 6 (Full-Series Release):** handled by `<aicn>/scripts/aicn.py run-series`, `merge-series`, and `brand-poster`.
- **Legacy Cleanup (`v3.0.0`):** All retired `v1.x`/`v2.x` stage scripts have been permanently removed from `vendor/` (see `<aicn>/CHANGELOG.md`). Use `scripts/aicn_audio.py` for Stage 3 and `scripts/aicn.py` for Stage 4 & poster branding.
