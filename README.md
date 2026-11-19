# aicn

Agent skill for full-series Vietnamese dubbing + Vietsub production of Chinese short dramas / AI dramas.
**Every published release is 1 full series** (`deliverables/final_<series>_full_vi.mp4` +
`final_<series>_full_vi.srt`). Internally, a series is split into working batches (`units/*`)
so Whisper/scan and Demucs stems stay cached per source, VieNeu CPU TTS is cached per sentence,
Stage 4 renders each batch in one hardware-accelerated FFmpeg pass (≈ 1.7 min per 10 min of
1080p video, with optional dual-corner watermark compositing), `run-series` overlaps Apple
Silicon's Metal GPU, CPU, and VideoToolbox Media Engine across batches, and `merge-series`
stream-copies all verified batches into the final full-series master.

- `SKILL.md`: what the agent reads (full-series contract, stages, edit loop, rules).
- `scripts/aicn.py`: the engine (`doctor`, `discover`, `init-series`, `adopt`, `plan`, `render`, `verify-overlay`, `run-series`, `merge-series`, `clean`, `log`, `progress`; legacy `tts-sync`).
- `scripts/aicn_audio.py`: Stage 3 audio (Demucs MLX stems, sentence-level CPU VieNeu dub with 2×4 threads + parallel rubberband, cached-Z frequency-aware mix, fast 48 kHz -14 LUFS master, audio gate).
- `vendor/`: the five stage skills it drives, with their gate scripts. See `vendor/README.md`.
- `references/`: folder contract, stage commands, caching design, production time.
- `templates/`: hub and unit CHANGELOG / PROGRESS / CHECKLIST.
- `tests/`: unit tests for the pure logic and CLI.

## Install

```bash
git clone <this repo> ~/Work/AICN/skills/aicn
bash ~/Work/AICN/skills/aicn/install.sh
```

`install.sh` links the skill (and any vendored stage skill not already installed) into
`~/.claude/skills/` and `~/.gemini/config/skills/`. Existing folders are never overwritten.

## Requirements

| Tool | Used for |
|---|---|
| macOS on Apple Silicon | `h264_videotoolbox` / `hevc_videotoolbox` encoders, Avenir Next font |
| `ffmpeg`, `ffprobe` (needs `gblur`, `alphamerge`, `overlay`) | one-pass render: decode, mask, overlay, encode, mux |
| `mlx-whisper` in `$AICN_PY` | Stage 1 ASR on Metal (falls back to faster-whisper) |
| Python 3.10+ with numpy, Pillow, scipy | `aicn.py` renderer |
| VieNeu-TTS venv (`/Users/Shared/Tools/VieNeu-TTS/.venv`, or `$AICN_PY`) | faster-whisper, TTS, stage scripts, `aicn_audio.py` |
| `rubberband` (`brew install rubberband`) | time-stretch of the dub, formants preserved |
| `uvx` (uv) | runs `demucs-mlx` for stem separation |

## Quick start

```bash
python3 scripts/aicn.py doctor --root ~/Work/AICN
python3 scripts/aicn.py discover --root ~/Work/AICN
python3 scripts/aicn.py plan --unit workspace/<series>/units/<unit>
python3 scripts/aicn.py render --unit workspace/<series>/units/<unit>
```

Full stage-by-stage commands: `references/stages.md`.
How long a finished unit takes: `references/production_time.md`.

## Test

```bash
/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python -m unittest discover -s tests -v   # audio tests need numpy/scipy/soundfile
```

## Rights

`init-series` requires a rights basis (`own`, `licensed:<contract>`, `cps:<platform>`,
`mcn:<name>`). Re-uploading a series you have no licence for is not supported.
