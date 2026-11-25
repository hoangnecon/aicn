# AICN Drama Localization Plugin (`aicn` · `v3.1.0`)

Standard multi-provider plugin (**Google Antigravity**, **Anthropic Claude Code**, **Google Gemini CLI**, **Cursor**, and **OpenAI Codex**) for full-series Vietnamese dubbing + Vietsub localization of Chinese (`红果`/`抖音`/`B站`) and English (`DramaDunyam`) short dramas.

**Every published release is 1 full series** (`deliverables/final_<series>_full_vi.mp4` + `deliverables/subtitles/final_<series>_full_vi.srt`) in mandatory **`voice_dub`** mode (full Vietnamese dubbing + dual-corner channel watermark + external `.srt`). Internally, series are split into working batches (`units/*`) backed by pure content-addressed SHA-256 caches (`<project_root>/.cache/content_hash.db` across `asr/`, `stems/`, `tts_sent/`, `rb_stretch/`, `wm_overlay/`), VRAM-aware `gpu_slot` concurrency, single-STFT C-speed vocal-carved mixing, and hardware `overlay_cuda` / VideoToolbox rendering.

---

## User-Invocable Slash Commands (`skills/aicn-*`)

Type `/aicn` in the chat input to open the intent-driven command menu:

| Command | When to Invoke | Core Action |
| :--- | :--- | :--- |
| **`/aicn-scout`** | Finding or pitching new dramas | Checks upstream `hongguo-drama`, queries original Chinese/English feeds (zero YouTube), fetches `.jpg` covers, and writes a Candidate Shortlist artifact. |
| **`/aicn-create`** | Starting a new series project | Validates unaccented Vietnamese `--series-slug` & `--rights`, downloads raw episodes into `source_inbox/`, scaffolds ~10-ep `units/*`, and signs Stage 0. |
| **`/aicn-script`** | Transcribing & writing VI dub scripts | Runs warm Demucs + Whisper `large-v3` (`asr-4.0`/`wf-4.0`), inspects audit frames, locks `series_bible.json` voices/pronouns, and validates `02_bilingual_script.json` (`100/100`). |
| **`/aicn-produce`** | Dubbing, rendering & releasing | Streams Stage 3→6: warm CUDA `VieNeu v3 Turbo` `infer_batch(16)`, intact `rubberband` R3, `-16 LUFS` mix, NVENC watermark render, visual QA, `merge-series`, and `brand-poster`. |
| **`/aicn-audit`** | Read-only quality inspection | Runs `aicn.py audit --strict` across chained gate signatures, script, TTS tempo/overflows, LUFS, `stale_merge`, and visual QA frames without mutating files. |
| **`/aicn-fix`** | Repairing a reported defect | Runs `aicn.py locate` (maps full-series `HH:MM:SS`, episode time, or phrase to exact unit/cue/utterance), patches only the broken layer, and rebuilds with `100%` cache reuse on unchanged lines. |
| **`/aicn-clean`** | Progress, time estimate & cleanup | Runs `discover`, `doctor`, turnaround estimation, `clean [--deep]` after release, or raw-preserving unit reset. |

---

## Multi-Provider Plugin Layout

```text
aicn/
├── plugin.json · .claude-plugin/plugin.json · .cursor-plugin/plugin.json · .codex-plugin/plugin.json · gemini-extension.json
├── assets/logo.svg            High-contrast plugin icon
├── rules/AGENTS.md            Lean workspace-scoped routing & production guardrails (linked via AGENTS.md & GEMINI.md)
├── skills/                    7 intent-driven user-invocable skills (aicn-scout .. aicn-clean)
├── scripts/                   Unified Python/Node engine (aicn.py, aicn_audio.py, aicn_platform.py, stage gates & sourcing CLIs)
├── references/                16 deep technical manuals, glossaries & playbooks
├── templates/                 Series Hub & working batch Markdown templates
└── tests/                     105+ deterministic unit, DSP, concurrency, cache & plugin contract tests
```

---

## Install

**Windows 10/11 (NVIDIA CUDA / CPU):**
```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -Force
# Add -InstallTools on a fresh machine to install portable ffmpeg, rubberband, and node
```

**macOS / Linux (Apple Silicon / CPU):**
```bash
bash ./install.sh --sync --force
```

- Installs the full plugin bundle into `~/.gemini/config/plugins/aicn` (`AICN_HOME`), syncs the 7 `aicn-*` skills into `~/.claude/skills/`, and automatically retires the legacy 6 loose skill folders.

## Test

```powershell
& $env:AICN_PY -m unittest discover -s tests -v
```
