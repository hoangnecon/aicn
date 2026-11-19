# Production time (Full-Series Release — Full Dubbing + Subtitles + Watermark)

Reference measurement batch: `unit_07_ep61_70` (10 episodes, 609.54 s, 1080p30, 186 lines / 107 sentences, full `voice_dub` mode on Apple M1 8-core / 16 GB).
**M** = measured directly on this machine (2026-11-19/20). **P** = projected from measured per-batch numbers. **E** = agent/human estimate.

> **Release rule:** 1 published video = **1 FULL series** (`final_<series>_full_vi.mp4`, e.g., 80 episodes ≈ 100 min).
> Working batches (~10 episodes each) are internal processing slices only.

## Per internal working batch (10 episodes, ≈ 10 min of video)

| Step | `v1.2.1` (M) | `v1.3.0` (M) | Notes |
|---|---|---|---|
| Stage 0 init + adopt | < 30 s | < 30 s | |
| Stage 1 Demucs stems (`demucs-mlx`, Metal) | 74 s | 74 s | cached for Stage 3 |
| Stage 1 5 fps hardsub scan | 38.9 s | **29.7 s** (-23%) | bit-exact 211/211 intervals |
| Stage 1 MLX Whisper ASR (Metal) | ~80 s | **~22–28 s** (3.3×) | `word_timestamps=False` (72.0s → 21.6s on 120s warm slice) |
| Stage 3 VieNeu CPU (`fp32`) + parallel rubberband | ~240 s | **~185–195 s** (+23%) | `tts_workers=2, tts_threads=4` (`gen_RTF` 0.317 → 0.258) |
| Stage 3 frequency-aware `mix` | 78.8 s (56.5 s compute) | **~21.5 s** (20.5 s compute) | cached-Z `complex64` STFT + piped `lufs()` (2.8–3.7×) |
| Stage 3 `master` (-14 LUFS) | **115.4 s** | **4.1 s** (**28×**) | native 48 kHz `ebur128 + alimiter` (-14.00 LUFS, -0.80 dBFS) |
| Stage 4 one-pass render (+ 2-corner watermark) | ~145 s (2 passes) | **~100–108 s** (1 pass) | `hwaccel videotoolbox` + VBR (5.6–6.06×), -49% to -63% smaller |
| Stage 5 hardlink + parallel QA extract | ~12 s (+790 MB copy) | **~4 s** (0 B extra) | hardlink + 4-worker ffmpeg |
| **Machine time / 10-min batch** | **≈ 13.0 min** | **≈ 7.5 min** (**-42%**) | full dub + full sub + watermark |

## Full 80-episode series (8 batches, ≈ 100 min, full dub + sub + watermark)

| Execution mode | Machine wall time | Notes |
|---|---|---|
| `v1.2.1` sequential (8 × 13.0 min) | ≈ **104 min** (P) | single-engine sequential |
| `v1.3.0` sequential (8 × 7.5 min) | ≈ **60 min** (P) | -42% faster per stage |
| **`v1.3.0` overlapped (`aicn.py run-series`)** | **≈ 32–38 min** (P) | Metal GPU (`separate`) ∥ CPU (`dub+mix+master`) ∥ Media Engine (`render`) overlap across batches (`prep` ≈ 8–10 min + `produce` ≈ 26–28 min + `merge-series` ≈ 3 s) |

*Human/agent work (audit frames, translation, mandatory Stage 5 visual QA viewing) runs in parallel or before `--phase produce` and depends on glossary reuse.*

## Output file size for 1 full 100-minute series (1080p30)

Measured on a 60 s real slice of `unit_07_ep61_70` (original HEVC source `1.01 Mbps` / `7.54 MB`):

| Preset | Speed (M) | 60 s size (M) | Full 100 min (P) | SSIM vs source (M) |
|---|---|---|---|---|
| `h264_high` (`5000k` ABR, legacy) | 4.21× (no hwdec) / 5.30× (hwdec) | 37.40 MB | ≈ 3.65 GB | 0.9908 |
| **`h264` (`-q:v 58`, default, + 2-corner wm)** | **5.75×** | **19.00 MB** | **≈ 1.86 GB (-49%)** | 0.9879 (at q60) |
| **`hevc` (`-q:v 60 -tag:v hvc1`)** | **6.06×** | **13.86 MB** | **≈ 1.35 GB (-63%)** | 0.9870 |
| **`hevc` + 2-corner watermark** | **5.59×** | **14.40 MB** | **≈ 1.41 GB (-61%)** | — |

## Edits after release (incremental, per affected batch + 3 s `merge-series`)

| Change | Time | | Work |
|---|---|---|---|
| Reword one subtitle (`sub_text`) | ≈ 1.8 min | M | validate, one ffmpeg pass on affected batch + `merge-series` |
| Change one dubbed line (`tts_text`) | ≈ 2.2 min | M/E | 1 sentence TTS + `mix` (21 s) + `master` (4 s) + one ffmpeg pass + `merge-series` |
| Change mix parameters only | ≈ 30 s + render | M | `mix` (21 s) + `master` (4 s) + gate |
| Style, codec, watermark, or renderer change | ≈ 1.8 min/batch | M | one ffmpeg pass |

## Formula (per internal working batch, `v1.3.0`)

```
machine_min ≈ 0.09·D (Stage 1 MLX+scan) + 0.12·D (stems) + 0.017·L (CPU TTS 2x4) + 0.042·D (mix+master) + 0.17·D (render) + 0.3
human_min   ≈ 0.10·L (translate + adapt) + 10 (audit + QA viewing)
```

`D` = batch duration in minutes, `L` = number of lines. For `D = 10`, `L = 186`: ≈ 7.7 min sequential per batch (≈ 32–38 min overlapped machine wall time for an entire 80-episode series).
