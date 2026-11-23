# aicn

Agent skill for full-series Vietnamese dubbing + Vietsub production of Chinese short dramas / AI dramas.
**Every published release is 1 full series** (`deliverables/final_<series>_full_vi.mp4` +
`final_<series>_full_vi.srt`). Internally, a series is split into working batches (`units/*`)
so Whisper/scan and Demucs stems stay cached per source, VieNeu CPU TTS is cached per sentence,
Stage 4 renders each batch in one hardware-accelerated FFmpeg pass (≈ 1.7 min per 10 min of
1080p video, with optional dual-corner watermark compositing), `run-series` overlaps GPU
(Metal/CUDA), CPU, and Hardware Video Encoder (VideoToolbox/NVENC) across batches, and `merge-series`
stream-copies all verified batches into the final full-series master.

- `SKILL.md`: what the agent reads (full-series contract, sourcing/analytics, stages, edit loop, RSI protocol, rules).
- `scripts/aicn_platform.py`: cross-platform runtime, hardware/GPU/encoder detection (`VideoToolbox` / `NVENC` / `libx264`), tool/Python discovery, and locking (`fcntl`/`msvcrt`).
- `scripts/aicn.py`: the engine (`doctor`, `discover`, `init-series`, `adopt`, `plan`, `render`, `verify-overlay`, `run-series`, `merge-series`, `clean`, `log`, `progress`; legacy `tts-sync`).
- `scripts/aicn_audio.py`: Stage 3 audio (Demucs MLX/PyTorch-CUDA stems, sentence-level CPU VieNeu dub with 2×4 threads + parallel rubberband, cached-Z frequency-aware mix, fast 48 kHz -14 LUFS master, audio gate).
- `scripts/hg_tool.cjs`: Hongguo discovery, freshness/trend analytics, cover JPEG fetcher, and 1080p CENC-decrypted downloader (backed by `tools/hongguo-drama`).
- `scripts/dy_tool.py`: Douyin detail, mix/collection, hot boards, and watermark-free MP4 downloader (`Evil0ctal/Douyin_TikTok_Download_API` + `tools/.venv-dy`).
- `scripts/aicn_radar.py`: Unified Hongguo + Douyin + Bilibili SQLite FTS5 analytics & search index (`data/drama_index.db`).
- `vendor/`: the five stage skills it drives, with their gate scripts. See `vendor/README.md`.
- `references/`: sourcing & analytics, recursive self-improvement (RSI), folder contract, stage commands, caching design, production time.
- `LESSONS.md`: live RSI ledger of measured incidents and codified guardrails.
- `templates/`: hub and unit CHANGELOG / PROGRESS / CHECKLIST.
- `tests/`: unit tests for the pure logic, audio engine, and `hg_tool.cjs`.

## Install

**macOS (Apple Silicon):**
```bash
git clone <this repo> ~/Work/AICN/skills/aicn
bash ~/Work/AICN/skills/aicn/install.sh
```

**Windows 10/11 (NVIDIA CUDA / CPU):**
```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -InstallTools
```

`install.sh` / `install.ps1` link the skill (and any vendored stage skill not already installed) into
`~/.claude/skills/` and `~/.gemini/config/skills/` (using symlinks on POSIX and directory junctions on Windows). Existing folders are never overwritten.

## Requirements

| Tool | Used for |
|---|---|
| macOS Apple Silicon **or** Windows/Linux + NVIDIA CUDA | `videotoolbox` (macOS) / `nvenc` (NVIDIA) / `libx264` (CPU fallback) |
| `ffmpeg`, `ffprobe` | one-pass render, mux, loudness, probing (`install.ps1 -InstallTools` on Windows) |
| `mlx-whisper` (macOS) or `faster-whisper` CUDA/CPU in `$AICN_PY` | Stage 1 ASR (`mlx` on Metal, `faster-whisper` `float16` on CUDA) |
| Python 3.10+ with numpy, Pillow, scipy | `aicn.py` renderer |
| VieNeu-TTS venv (`$AICN_PY`, default `/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python` or `C:\Work\Tools\VieNeu-TTS\.venv\Scripts\python.exe`) | faster-whisper, Demucs, TTS, stage scripts, `aicn_audio.py` |
| `rubberband` (`brew install rubberband` / `install.ps1 -InstallTools`) | time-stretch of the dub, formants preserved |
| `uvx` (`uv`) or `demucs` in `$AICN_PY` | `demucs-mlx` on macOS, PyTorch `demucs` (CUDA/CPU) on Windows/Linux |
| `node` (v20+) | `hg_tool.cjs` Hongguo sourcing CLI |

## Quick start

```bash
python3 scripts/aicn.py doctor --root <project-root>
python3 scripts/aicn.py discover --root <project-root>
python3 scripts/aicn.py plan --unit workspace/<series>/units/<unit>
python3 scripts/aicn.py render --unit workspace/<series>/units/<unit>
```

Full stage-by-stage commands: `references/stages.md`.
How long a finished unit takes: `references/production_time.md`.

## Test

```bash
# macOS / Linux:
/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python -m unittest discover -s tests -v
# Windows:
& $env:AICN_PY -m unittest discover -s tests -v
```

## Rights

`init-series` requires a rights basis (`own`, `licensed:<contract>`, `cps:<platform>`,
`mcn:<name>`). Re-uploading a series you have no licence for is not supported.
