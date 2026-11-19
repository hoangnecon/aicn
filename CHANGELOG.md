# Changelog

## 1.1.0 (2026-11-19)
- New Stage 3 engine `scripts/aicn_audio.py` replaces `assemble_master_audio.py`, which replaced a missing Demucs stem with silence and shipped dub-only audio.
- Demucs (MLX) stems are mandatory. The original voice, music and effects stay in the master.
- Frequency-aware mix: 12 bands, about 10 ms frames, the original is ducked only where and as much as the dub needs, with floors (voice -18 dB, music -9 dB).
- Dub read per sentence (comma-chained clauses merged): fewer mis-toned short fragments.
- Speed: rubberband R3 with formants preserved (base 1.10×, up to 1.25× to fit). `atempo` removed: it doubled the word error rate.
- Expander removes the TTS hiss floor. Dub chain: high-pass, presence, 3:1 compression.
- Master: 2-pass loudnorm to -14 LUFS, -1 dBTP, 24-bit.
- Audio gate (`data/03_mix_report.json`): loudness, stems match source, dub intelligibility, original never muted, original present, dub fits.
- `references/audio_mix.md`; audio unit tests.

## 1.0.0 (2026-11-19)
- Renamed from `drama-reup-pipeline` to `aicn`; engine `scripts/aicn.py`.
- Stage skills vendored under `vendor/`; `cicd_gate.py` is found there first.
- Renderer styleB-1.4: per-Chinese-interval blur, on-frame width re-measure, mask grown by 1.5 × feather.
- Display windows: hold (overlap ≥ 0.25 s), snap (≤ 1.0 s late, previous cue ≤ +0.3 s), frame-quantized.
- Stage 4 gate checks blank coverage, line count, chunks, duration.
- `discover` marks gates whose media were deleted with `x`.
- Tested end to end on unit_07_ep61_70 (10 episodes, 609.5 s): full render 207–243 s, one-line fix 38 s.
- Unit tests for display windows, chunking, cache keys and the CLI.
