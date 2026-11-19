# Changelog

## 1.3.1 (2026-11-20)
- **Series naming & folder/markdown contract**:
  - `aicn.py init-series` now enforces Vietnamese translated, no-accent, lowercase, hyphen-separated `--series-slug` (`^[a-z0-9]+(-[a-z0-9]+)*$`, e.g. `nam-18-tuoi-toi-che-tao-than-khi`).
  - Expanded `references/folder_contract.md` and `SKILL.md` with the complete parent/child directory tree, per-folder contents, and per-stage Markdown checklist/report templates & fill formats.

## 1.3.0 (2026-11-20)
- **Full-series release contract ("1 published video = 1 FULL series")**:
  - Working batches under `units/*` are strictly internal work slices (caching, QA, pipelining) — never standalone uploads.
  - `aicn.py discover` and `PROGRESS.md` track `FULL RELEASE: READY / PENDING (X/Y batches PASS)`.
  - `aicn.py merge-series --hub H`: verifies all batches passed Stage 5, stream-copies all batch MP4s + time-shifted SRTs into `deliverables/final_<series>_full_vi.mp4` + `final_<series>_full_vi.srt`, logs `[release]` to hub `CHANGELOG.md`, and refreshes `PROGRESS.md`.
  - `aicn.py run-series --hub H --phase prep|produce`: hardware-overlapped full-series runner (`Metal GPU: separate` ∥ `CPU: dub+mix+master+verify` ∥ `Media Engine: render+verify-overlay+qa-extract`), preserving every human/agent visual QA gate.
- **Stage 1 ASR & 5 fps scan (`vendor/video-asr-visual-audit`)**:
  - Disabled unused `word_timestamps` in Whisper Pass 1 (`run_asr_and_scan.py`): **3.33× faster** on warm MLX Metal (`72.03 s → 21.63 s` on 120 s real slice).
  - Fast-math 5 fps hardsub scan (skip frames with zero candidate pixels + `int16` subsampled scene-cut): **38.86 s → 29.73 s** (-23%) on 609.5 s unit 07, **211/211 bit-exact** `hardsub_intervals`.
  - Fixed stems path mismatch (`media/stems/` vs legacy `media/demucs/`) in `run_asr_and_scan.py`, `analyze_voice_waveform.py`, and `verify_stage_1.py`.
  - Parallelized `extract_audit_frames.py` (4 workers).
- **Stage 3 audio (`aicn-audio-1.1`, full CPU VieNeu dubbing)**:
  - **Fast native 48 kHz master** (`master_mode="fast"`, default): `acompressor` + `ebur128` + linear `volume` + `alimiter` cuts `cmd_master` from **115.38 s** (legacy 192 kHz 2-pass `loudnorm`) to **4.11 s** (**28× faster**) at `-14.00 LUFS`, `-0.80 dBFS` true peak.
  - **Cached-Z `complex64` STFT/iSTFT + piped `lufs()` + 100 Hz RMS** in `cmd_mix`: **56.54 s compute (78.8 s total) → 20.49 s compute** (**2.8–3.7× faster**), identical LUFS/floors (`-14.0 LUFS`, `-21.93 dB`, `-9.0 dB`), median SNR `14.90 dB`.
  - **CPU VieNeu `tts_workers=2, tts_threads=4`** (`fp32` ONNX, active for `>= 8` missing jobs): **+23% faster** (`gen_RTF 0.317 → 0.258` on 12 real sentences) + parallel `rubberband` (`ThreadPoolExecutor`).
  - Kept `STEMS_VERSION = "aicn-audio-1.0"` so `v1.2.x` Demucs stems stay cache-valid; `cmd_verify` now writes `03_pacing_and_audio_report.md`; `sub_only` alignment fix included.
- **Stage 4 render & Stage 5 QA (`ff-1.1`, `vendor/video-localization-qa-review`)**:
  - Enabled `-hwaccel videotoolbox` + quality VBR presets (`--codec h264|hevc|h264_high`, `--quality N`):
    - `h264` (default, `-q:v 58`): **-49% smaller** (~1.86 GB / 100 min), **5.75×** real-time (vs 4.21× baseline, SSIM 0.988).
    - `hevc` (`-q:v 60 -tag:v hvc1`): **-63% smaller** (~1.35 GB / 100 min), **6.06×** real-time (SSIM 0.987).
  - Single-pass dual-corner watermark compositing via `--brand-dir` or `config/project_config.json["watermark"]` (5.59–5.75×).
  - Fixed `verify_stage_5.py` to accept both `h264` and `hevc` (previously blocked `hevc`).
  - Replaced duplicate `shutil.copy2` in `composite_and_verify.py` and `verify_stage_5.py` with hardlinks (`os.link`, saving ~6.3 GB/series), and parallelized QA frame extraction (4 workers).
  - `ff_render_key` hashes font, encoder, and watermark; `band_y0` aligns to multiple of 8; `verify-overlay` records actual renderer (`ff-1.1`).
  - `aicn.py clean --unit U [--dry-run] [--deep]`: frees **~1.2 GB/batch** keeping `PPPPPP` gates intact (`~1.8 GB/batch` with `--deep`).
- **Doctor & Tests**: `doctor` detects `h264_videotoolbox + hevc_videotoolbox`; unit tests expanded and verified with 0 warnings.

## 1.2.1 (2026-11-19)
- MLX Whisper: forced Simplified Chinese (it returned 146 Traditional characters on unit 07) and drops segments Whisper marks as silence or repetition loops (no VAD in mlx-whisper). Full Stage 1 on unit 07: 105 s, sub-clause coverage 210/211 as before, punctuation now kept.
- `plan` understands the ffmpeg engine: prints `render=current` or `stale: <parts>`.
- The ffmpeg band is computed from the scan and the plates (`band_y0`) instead of a fixed y = 760, so 720p, vertical sources and high subtitles are masked in the right place.
- `tts_workers` option for VieNeu. Default 1: VieNeu ONNX already uses every core (3 workers measured 9% faster, 4 slower).

## 1.2.0 (2026-11-19)
- `aicn.py doctor`: probes chip, cores, RAM, ffmpeg filters/encoders, MLX, models and tools; writes `config/machine.json` and names the stack to use.
- Stage 4 default engine is now one ffmpeg pass (`ff-1.0`): Python only draws mask and plate PNGs, ffmpeg decodes, masks, overlays, encodes and muxes. Unit 07: 116 s instead of 207–400 s. Every source frame keeps its own timestamp (VFR-safe): 18,277 frames out of 18,277.
- Mask style `mask_mode`: `blur` (default) or `solid` (black box, about 12% faster); `render --mask` overrides.
- Unchanged inputs skip the render entirely. The old per-frame renderer stays as `render --engine python`.
- Stage 1 Whisper runs on MLX (Metal) when `mlx_whisper` is installed: 48 s instead of 194 s for one pass on unit 07. `AICN_ASR=faster` forces the CPU path.

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
