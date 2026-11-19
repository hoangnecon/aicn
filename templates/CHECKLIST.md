# CHECKLIST — {{unit}} (internal working batch)

A box is ticked only after the command in its row prints PASS. The gate tokens in `.cicd/` are the real record; this file is for humans.
**Reminder:** a working batch is NOT a standalone upload. Publish only the full series built by `aicn.py merge-series --hub <hub>` once all batches pass Stage 5.

## Stage 0 — init
- [ ] `config/rights.json` on the hub has a basis other than `unknown`
- [ ] `init_workspace.py` → `.cicd/stage_0_gate.json` PASS
- [ ] `aicn.py adopt` → CHECKLIST, CHANGELOG, `config/overlay_style.json`, `.cache/`

## Stage 1 — Stems + ASR + hardsub scan (runs ONCE per source)
- [ ] `aicn_audio.py separate` (Demucs MLX stems → `media/stems/`)
- [ ] `run_asr_and_scan.py` (MLX Whisper + fast 5 fps bbox)
- [ ] `analyze_voice_waveform.py --snap-and-split`
- [ ] `extract_audit_frames.py` and every frame logged in `01_visual_audit_notes.md`
- [ ] `verify_stage_1.py` PASS

## Stage 2 — script
- [ ] `02_bilingual_script.json` uses names and xưng hô from `series_bible.json`
- [ ] `validate_bilingual_script.py` PASS

## Stage 3 — full Vietnamese dub + mix + master (`aicn_audio.py`)
- [ ] `aicn_audio.py all --unit .` (stems cached → CPU VieNeu dub + parallel rubberband → cached-Z mix → fast 48 kHz master → audio gate + `verify_stage_3.py` PASS)
- [ ] Spot-listened to at least one dubbed passage and one undubbed passage

## Stage 4 — overlay (style B, one-pass FFmpeg)
- [ ] `aicn.py plan` reviewed (expected dirty parts only)
- [ ] `aicn.py render [--codec h264|hevc] [--brand-dir <dir>]`
- [ ] `aicn.py verify-overlay` PASS (blank ≤ 0.30 s, ≤ 2 lines, duration exact)

## Stage 5 — batch QA (hardlinked, 0 duplicate disk copy)
- [ ] `composite_and_verify.py --import-existing-mp4 media/.staging_render.mp4 --qa-timestamps ...`
- [ ] every `frames/qa/*.jpg` viewed and logged PASS in `04_qa_review_report.md`
- [ ] `verify_stage_5.py` PASS → `media/final_<batch>_vi.mp4`
- [ ] `aicn.py log --kind stage --message "Batch Stage 5 PASS"` (optional: `aicn.py clean --unit .` to free ~1.2 GB intermediates while keeping `PPPPPP`)

## Stage 6 — full-series release (on Series Hub, after ALL batches PASS)
- [ ] `aicn.py merge-series --hub <series-hub>` → `deliverables/final_<series>_full_vi.mp4` + `final_<series>_full_vi.srt`
