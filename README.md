# aicn

Agent skill for Vietsub and Vietnamese dub re-ups of Chinese short dramas / AI dramas.
It works for any series, cut into units of 10 episodes, and makes edits incremental:
changing one subtitle line re-renders one episode chunk in about 40 seconds instead of
the whole unit.

- `SKILL.md`: what the agent reads (entry point, stages, edit loop, rules).
- `scripts/aicn.py`: the engine (doctor, discover, init-series, adopt, plan, tts-sync, render, verify-overlay, log, progress).
- `scripts/aicn_audio.py`: Stage 3 audio (Demucs stems, sentence-level dub, frequency-aware mix, -14 LUFS master, audio gate).
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
| macOS on Apple Silicon | `h264_videotoolbox` encoder, Avenir Next font |
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
