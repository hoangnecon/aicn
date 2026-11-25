# aicn (`v3.0.0`)

Agent skill for full-series Vietnamese dubbing + Vietsub production of Chinese and English short dramas / AI dramas.
**Every published release is 1 full series** (`deliverables/final_<series>_full_vi.mp4` + `deliverables/subtitles/final_<series>_full_vi.srt`) in mandatory **`voice_dub`** mode (full Vietnamese dubbing + dual-corner branding watermark + external `.srt`). Internally, a series is split into working batches (`units/*`) so pure content-addressed SHA-256 caches (`<project_root>/.cache/content_hash.db` across `asr/`, `stems/`, `tts_sent/`, `rb_stretch/`, `wm_overlay/`) survive unit/hub renames and cleanups. Stage 1 & 3 warm batch CLIs load Whisper, Demucs, and VieNeu-TTS CUDA once per multi-unit batch, VRAM-aware `gpu_slot` pooling runs compatible GPU jobs concurrently (replacing single-lock GPU serialization), Stage 4 composites a cached top-band watermark PNG via hardware `overlay_cuda` / VideoToolbox in parallel lock-free passes, and `merge-series` stream-copies all verified batches into the final full-series master.

- `SKILL.md`: agent entry point (full-series contract, sourcing/analytics, stages, edit loop, RSI protocol, rules).
- `scripts/aicn_platform.py`: cross-platform runtime, SQLite-WAL `content_hash.db`, unified cache key builders, crash-safe 512 MB-unit `gpu_slot` VRAM pool, encoder & `overlay_cuda` filter probing, atomic JSON writes.
- `scripts/aicn.py`: core engine (`doctor`, `discover`, `init-series`, `update-series`, `adopt`, `plan`, `render`, `verify-render`, `run-series`, `merge-series`, `brand-poster`, `clean`, `log`, `progress`).
- `scripts/aicn_audio.py`: Stage 3 audio (`separate`, `dub`, `analyze`, `mix`, `master`, `verify`, `all`, plus warm multi-unit `batch-separate`, `batch-dub`, `batch-all`) — Demucs `htdemucs` stems (`PCM_16`), indivisible utterance grouping (zero mid-word slicing), CUDA `infer_batch(16)` VieNeu-TTS + parallel `rubberband` R3 with fail-fast overflow detection before `mix`, single-STFT + C-speed vectorized `follow_hold()` mixer (`<12s`/unit), native 48 kHz `-14 LUFS` master, and active-speech SNR/continuity gate.
- `scripts/hg_tool.cjs`: thin dynamic wrapper around upstream `jackotom/hongguo-drama` (`check-upstream [--sync]`, `rank`, `new`, `search`, `details`, `album`, `episodes`, `fetch-covers`, `download`).
- `scripts/dy_tool.py`: Douyin detail, mix/collection, hot boards, and watermark-free MP4 downloader (`Evil0ctal/Douyin_TikTok_Download_API` + `tools/.venv-dy`).
- `scripts/dd_tool.py`: DramaDunyam clean `lang=en`, non-adult AI/short-drama API client and downloader (`dramadunyam.com`).
- `scripts/aicn_radar.py`: Unified Hongguo + Douyin + Bilibili + DramaDunyam SQLite FTS5 analytics & search index (`data/drama_index.db`).
- `vendor/`: the five stage skills (`video-localization-orchestrator`, `video-asr-visual-audit`, `zh-vi-script-adapter`, `vieneu-voice-pacing-mixer`, `video-localization-qa-review`). See `vendor/README.md`.
- `references/`: `stages.md`, `audio_mix.md`, `folder_contract.md`, `incremental_build.md`, `production_time.md`, `sourcing_and_analytics.md`, `poster_and_caption_guide.md`, `recursive_self_improvement.md`.
- `LESSONS.md`: live RSI ledger of measured incidents and codified guardrails.
- `templates/`: hub and unit `CHANGELOG.md`, `PROGRESS.md`, `CHECKLIST.md`, `UNIT_CHANGELOG.md`.
- `tests/`: unit, concurrency, hash-portability, ASR, audio DSP, and sourcing regression tests.

## Install

**Windows 10/11 (NVIDIA CUDA / CPU):**
```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -InstallTools
```

**macOS / Linux (Apple Silicon / CPU):**
```bash
git clone <this repo> ~/Work/AICN/skills/aicn
bash ~/Work/AICN/skills/aicn/install.sh
```

- `install.ps1` installs **real directory copies** (not NTFS Junctions) into `%USERPROFILE%\.gemini\config\skills\` and `%USERPROFILE%\.claude\skills\` for all 6 skills (`aicn` + 5 vendored stage skills), cleans deleted legacy files on re-sync via `.aicn-managed`, excludes dev files (`SPEC_V3.md`, `.git`, `__pycache__`, `.cache`), and auto-configures `AICN_PY`.
- `install.sh` symlinks (or refreshes with `--sync`) all 6 skills into `$HOME/.claude/skills` and `$HOME/.gemini/config/skills`.

## Requirements

| Tool | Used for |
|---|---|
| Windows/Linux + NVIDIA CUDA **or** macOS Apple Silicon | `nvenc` (`overlay_cuda` when available) / `videotoolbox` / `libx264` fallback |
| `ffmpeg`, `ffprobe` | one-pass render, mux, `ebur128` loudness, stream probing (`install.ps1 -InstallTools` on Windows) |
| `faster-whisper` CUDA/CPU **or** `mlx-whisper` (macOS) in `$AICN_PY` | Stage 1 `large-v3` ASR (`int8_float16` on CUDA, Metal on Apple Silicon) |
| Python 3.10+ with `numpy`, `Pillow`, `scipy`, `soundfile` | `aicn.py`, `aicn_audio.py`, stage validators |
| VieNeu-TTS venv (`$AICN_PY`, Windows `C:\Work\Tools\VieNeu-TTS\.venv\Scripts\python.exe`, macOS `/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python`) | CUDA `infer_batch` / ONNX VieNeu-TTS, Demucs, Whisper, stage scripts |
| `rubberband` (`install.ps1 -InstallTools` / `brew install rubberband`) | Formant-preserved R3 time-stretching of intact utterances |
| `demucs` in `$AICN_PY` **or** `uvx` (`demucs-mlx` on macOS) | Warm `htdemucs` vocal/accompaniment separation |
| `node` (v20+) | `hg_tool.cjs` Hongguo sourcing CLI |

## Quick start

```bash
$AICN_PY scripts/aicn.py doctor --root <project-root>
$AICN_PY scripts/aicn.py discover --root <project-root>
$AICN_PY scripts/aicn.py run-series --hub workspace/<series> --phase prep
$AICN_PY scripts/aicn.py run-series --hub workspace/<series> --phase produce --brand-dir <brand_dir>
$AICN_PY scripts/aicn.py merge-series --hub workspace/<series>
```

Full stage-by-stage commands: `references/stages.md`.
How long a finished unit takes: `references/production_time.md`.

## Test

```powershell
# Windows:
& $env:AICN_PY -m unittest discover -s tests -v
```
```bash
# macOS / Linux:
/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python -m unittest discover -s tests -v
```

## Rights

`init-series` requires a valid rights basis (`own`, `licensed:<contract>`, `cps:<platform>`, `mcn:<name>`). Re-uploading a series without a rights basis is rejected.
