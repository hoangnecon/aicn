# Changelog

## 1.0.0 (2026-11-19)
- Renamed from `drama-reup-pipeline` to `aicn`; engine `scripts/aicn.py`.
- Stage skills vendored under `vendor/`; `cicd_gate.py` is found there first.
- Renderer styleB-1.4: per-Chinese-interval blur, on-frame width re-measure, mask grown by 1.5 × feather.
- Display windows: hold (overlap ≥ 0.25 s), snap (≤ 1.0 s late, previous cue ≤ +0.3 s), frame-quantized.
- Stage 4 gate checks blank coverage, line count, chunks, duration.
- `discover` marks gates whose media were deleted with `x`.
- Tested end to end on unit_07_ep61_70 (10 episodes, 609.5 s): full render 207–243 s, one-line fix 38 s.
- Unit tests for display windows, chunking, cache keys and the CLI.
