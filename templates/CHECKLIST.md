# CHECKLIST — {{unit}}

A box is ticked only after the command in its row prints PASS. The gate tokens in `.cicd/` are the real record; this file is for humans.

## Stage 0 — init
- [ ] `config/rights.json` on the hub has a basis other than `unknown`
- [ ] `init_workspace.py` → `.cicd/stage_0_gate.json` PASS
- [ ] `aicn.py adopt` → CHECKLIST, CHANGELOG, `config/overlay_style.json`, `.cache/`

## Stage 1 — ASR + hardsub scan (runs ONCE per source)
- [ ] `run_asr_and_scan.py` (mlx/faster-whisper 2-pass + 5 fps bbox)
- [ ] `analyze_voice_waveform.py --snap-and-split`
- [ ] `extract_audit_frames.py` and every frame logged in `01_visual_audit_notes.md`
- [ ] `verify_stage_1.py` PASS

## Stage 2 — script
- [ ] `02_bilingual_script.json` uses names and xưng hô from `series_bible.json`
- [ ] `validate_bilingual_script.py` PASS

## Stage 3 — voice / audio
- [ ] `aicn.py tts-sync` → `synthesize_and_audit_pacing.py --only-missing` → `aicn.py tts-sync --store`
- [ ] `assemble_master_audio.py` → `verify_stage_3.py` PASS

## Stage 4 — overlay (style B, incremental)
- [ ] `aicn.py plan` reviewed (expected dirty chunks only)
- [ ] `aicn.py render`
- [ ] `aicn.py verify-overlay` PASS (drift ≤ 0.30 s, ≤ 2 lines, all chunks present)

## Stage 5 — QA + release
- [ ] `composite_and_verify.py --import-existing-mp4 media/.staging_render.mp4 --qa-timestamps ...`
- [ ] every `frames/qa/*.jpg` viewed and logged PASS in `04_qa_review_report.md`
- [ ] `verify_stage_5.py` PASS → `media/final_<unit>_vi.mp4`
- [ ] `aicn.py log --kind release` + hub PROGRESS refreshed
