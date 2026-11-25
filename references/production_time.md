# Production Time (`v3.0.0`, Full-Series Release — Full Dubbing + Watermark + Subtitles)

> **Release rule:** 1 published video = **1 FULL series** (`deliverables/final_<series>_full_vi.mp4`, e.g. 80–134 episodes ≈ 90–145 min).
> Working batches (`units/*`, ~10 episodes each) are internal processing slices only.

## 1. Forensic Baseline (`v2.7.0`) vs `v3.0.0` Redesign

Measured bottlenecks on a real 13-unit / 134-episode `1080×1920` production run on Windows 11 + NVIDIA RTX 2060 (6 GB VRAM):

| Bottleneck | Measured in `v2.7.0` | Fixed in `v3.0.0` (`SPEC_V3.md`) | Measured / Target Impact |
|---|---|---|---|
| **Serial GPU lock** | 33 lock waits = **343.6 min** (max **78 min** single wait); 26+ cold model loads | Replaced single-lock serialization with 512 MB-unit `ap.gpu_slot` (`10` slots on RTX 2060: `whisper(5)+demucs(2)+vieneu(2)=9` run concurrently) + warm multi-unit `batch-separate`, `run_asr_and_scan --units`, `batch-all` | Zero serial GPU lock pileups; models load **ONCE** per batch |
| **Stage 3 VieNeu TTS** | Cold-started 13+ times across units; post-synthesis clause slicing chopped words | Single in-process CUDA `infer_batch(16)` (`4.23s` for 16 sents, `RTF 0.127`, `498 MB` VRAM on RTX 2060) + indivisible utterance placement + global `tts_sent/` & `rb_stretch/` cache | Zero mid-word chops; `cmd_dub` fails fast (`exit 2`) before `cmd_mix` (saving ~28 min across 15 overflowed runs) |
| **Stage 3 DSP Mix** | **113.8s/unit** avg (8 STFT/iSTFT passes + Python `follow_hold()` loop + `ffmpeg` pipe `lufs()`) | PyTorch Welch `spectral_profile_fast()` + anti-denormal `sosfilt` + in-RAM BS.1770-4 `lufs()` + C-speed chunked `follow_hold()` + PyTorch CPU `torch.stft`/`torch.istft` (`scipy.fft` fallback) combined synthesis | **`11.56s`** measured on 10-min (`600s`) 48 kHz stereo (**9.8× faster**; `dub_snr_speech_db_median = 17.4 dB`, `p10 = 16.48 dB`) |
| **Stage 3 Master** | Legacy 192 kHz 2-pass `loudnorm` (`115.4s`) | Native 48 kHz `acompressor` + `ebur128` + `alimiter` (`master_mode="fast"`) | **`~4.1s`** (**28× faster**) |
| **Stage 4 Hardware Render** | **154.7s/unit** (`8.75s` on 30s 1080p30 slice due to dual `-framerate 30 -loop 1` CPU `overlay`) | Pre-composited top-band RGBA PNG (`wm_overlay/` cache) + `hwupload_cuda` + `overlay_cuda` (or single static `overlay` fallback), lock-free parallel (`AICN_MAX_RENDER_JOBS=2`) | **`2.87s` / 30s** (`10.5×` RT, **3.05× faster** with `overlay_cuda`); **`3.90s` / 30s** (`7.7×` RT single CPU `overlay` fallback) |
| **Stage 5 QA Extract** | Sequential under `media.lock` | Hardlink `.staging_render.mp4 → .staging_final.mp4` (`0 B`) + 4-worker parallel extract, lock-free | **~3–4s** per unit |

## 2. Measured Hardware Benchmark Reference Points

- **VieNeu-TTS v3 Turbo CUDA `infer_batch(16)` (RTX 2060 6 GB):** `4.23s` for 16 sentences (`RTF 0.127`, `3.78 sent/s`, peak VRAM `498 MB`).
- **Stage 3 Single-STFT DSP Mix (10-min `600s` 48 kHz stereo benchmark):** **`11.56s`** wall time (**9.8× faster** than `113.8s` in `v2.7.0`, `dub_snr_speech_db_median = 17.4 dB`, `p10 = 16.48 dB`, `orig_vocal_atten_db_median_during_dub = -15.36 dB`, `min = -24.0 dB`).
- **Stage 4 Render Speed (RTX 2060 1080p30, 30s real slice):** `2.87s` (`10.5×` realtime) with `overlay_cuda`, `3.90s` (`7.7×` realtime) with single static CPU `overlay` vs `8.75s` (`3.4×` realtime) in `v2.7.0`.
- **Stage 4 Output File Size & Visual Quality (Apple Silicon VideoToolbox 60s 1080p30 slice, `1.01 Mbps` HEVC source):**
  - `h264` (`-q:v 58`, default + dual watermark): `19.00 MB / 60s` (`≈ 1.86 GB / 100 min`, SSIM `0.988`).
  - `hevc` (`-q:v 60 -tag:v hvc1` + dual watermark): `14.40 MB / 60s` (`≈ 1.41 GB / 100 min`, SSIM `0.987`).
  - `h264_high` (`5000k` ABR, legacy): `37.40 MB / 60s` (`≈ 3.65 GB / 100 min`, SSIM `0.991`).
  - No watermark (`-c:v copy -sn`): stream-copy completes in seconds (preserves source bitrate).

## 3. Incremental Edits After Release (Per Affected Batch + ~3s `merge-series`)

| Change | Typical Time | Work Performed |
|---|---|---|
| Reword external `.srt` (`sub_text` only) | ≈ `0.1 min` | `validate_bilingual_script.py` → `aicn_audio.py dub` (100% cache hit, rewrites `subtitles_full.srt`) → `verify` → `merge-series` (0 TTS, 0 mix, 0 render) |
| Change one dubbed line (`tts_text`) | ≈ `1–1.5 min` | 1 VieNeu utterance + 1 `rubberband` (all other lines hit `tts_sent`/`rb_stretch` cache) → `< 12s` `mix` → `~4s` `master` → 1 fast hardware `render` → QA → `merge-series` |
| Tune `config/audio_mix.json` only | ≈ `0.5 min` + render | `< 12s` `mix` → `~4s` `master` → `verify` → 1 fast hardware `render` → `merge-series` |
| Change codec or `--brand-dir` | `render` + QA | Global `wm_overlay` composited once → 1 fast hardware `render` (or instant `-c:v copy` if unbranded) |
| Rename/move unit or series hub | **Seconds** (100% cache hit) | `content_hash.db` hashes content once at new path; `stage_N_gate.json` (`schema_version: "3.0"`) stays valid |
