# CHECKLIST — {{unit}} (internal working batch)

A box is ticked only after the command in its row prints `PASS`. The relocation-safe SHA-256 tokens in `.cicd/` are the real record; this file is for humans.
**Reminder:** a working batch is NOT a standalone upload. Publish only the full series built by `aicn.py merge-series --hub <hub>` once all batches pass Stage 5.

## Stage 0 — init
- [ ] `config/rights.json` on the hub has a basis other than `unknown`
- [ ] `init_workspace.py` (`--mode voice_dub`) → `.cicd/stage_0_gate.json` PASS
- [ ] `aicn.py adopt` → CHECKLIST, CHANGELOG, `build_state.json`

## Stage 1 — Stems + Multi-Pass ASR + Acoustic Alignment (runs ONCE per source)
- [ ] `aicn_audio.py separate` / `batch-separate` (Demucs `htdemucs` → `media/stems/{vocals,accomp}.wav`, cached globally)
- [ ] `run_asr_and_scan.py` (`asr-4.0`: `large-v3` Pass 1 + collar-masked Pass 2B, or `--units` warm batch)
- [ ] `analyze_voice_waveform.py --snap-and-split` (`wf-4.0`: strict monotonic `end[i] <= start[i+1] - 0.02`, `dur >= 0.15s`)
- [ ] `extract_audit_frames.py` and every frame logged in `01_visual_audit_notes.md`
- [ ] `verify_stage_1.py` PASS (`100/100`)

## Stage 2 — bilingual script (`voice_dub`)
- [ ] `02_bilingual_script.json` uses names, `xưng hô`, and `<en>...</en>` tokens matching `series_bible.json`
- [ ] `validate_bilingual_script.py` PASS (`100/100`: monotonic timing + calibrated VieNeu duration budget)

## Stage 3 — full Vietnamese dub + single-STFT mix + master (`aicn_audio.py`)
- [ ] `aicn_audio.py all --unit .` (or `run-series --phase produce`: CUDA `infer_batch` / ONNX VieNeu dub → indivisible `utt_NNNN.wav` + parallel `rubberband` R3 → single-STFT C-speed mix → fast 48 kHz master → `03_mix_report.json` + `verify_stage_3.py` PASS)
- [ ] Spot-listened to at least one dubbed passage and one undubbed passage

## Stage 4 — dub + watermark render (one-pass hardware FFmpeg)
- [ ] `aicn.py plan` reviewed (expected dirty parts only)
- [ ] `aicn.py render [--codec h264|hevc] [--brand-dir <dir>]` (cached top-band watermark strip + `overlay_cuda` / single static `overlay`, or stream-copy if no watermark)
- [ ] `aicn.py verify-render` PASS (`h264`/`hevc`, `48 kHz` stereo AAC, `0` subtitle streams, `±0.30s` sync, exact frame count)

## Stage 5 — batch QA (hardlinked, 0 duplicate disk copy)
- [ ] `composite_and_verify.py --video source/master_source.mp4 --workspace . --slug {{unit}}` (hardlinks `media/.staging_render.mp4` → `.staging_final.mp4`, parallel 4-worker QA extract)
- [ ] Every `frames/qa/*.jpg` viewed via `read_file` and logged `PASS` in `04_qa_review_report.md`
- [ ] `verify_stage_5.py` PASS → `media/final_<batch>_vi.mp4`
- [ ] `aicn.py log --kind stage --message "Batch Stage 5 PASS"` (optional: `aicn.py clean --unit .` to free intermediates while keeping `PPPPPP`)

## Stage 6 — full-series release (on Series Hub, after ALL batches PASS)
- [ ] `aicn.py merge-series --hub <series-hub>` → `deliverables/final_<series>_full_vi.mp4` + `deliverables/subtitles/final_<series>_full_vi.srt`
