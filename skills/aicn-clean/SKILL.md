---
name: aicn-clean
description: Use when checking series progress or machine status ("đến đâu rồi", "kiểm tra tiến độ", "check môi trường CUDA"), estimating production time ("bộ này làm mất bao lâu"), freeing disk space after release ("dọn file tạm cho nhẹ máy"), or resetting broken unit build artifacts while keeping raw videos ("xóa dữ liệu tập lỗi, chỉ giữ raw").
---

# Workspace Status, Time Estimation & Safe Cleanup (`/aicn-clean`)

## Overview
Inspect Series Hub progress (`discover`), verify GPU/tool readiness (`doctor`), estimate turnaround time from empirical benchmarks, prune heavy intermediates safely after full-series release (`clean` / `clean --deep`), or reset broken unit outputs while strictly protecting `source_inbox/` raw episodes.

- `<aicn>`: `$AICN_HOME` if set, else `~/.gemini/config/plugins/aicn` (`../..` from this skill).
- `$AICN_PY`: `C:\Work\Tools\VieNeu-TTS\.venv\Scripts\python.exe` (Windows) or `/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python` (macOS).
- Deep references: `<aicn>/references/production_time.md`, `<aicn>/references/folder_contract.md`.

## Operations

### 1. Check Series Progress & Next Action (`discover`)
```bash
$AICN_PY <aicn>/scripts/aicn.py discover --root <project-root>
```
Reads hub manifests, `source_inbox` count, per-unit 6-stage gate letters (`P` pass, `F` fail, `x` stale/pruned intermediate, `-` missing), and whether `deliverables/final_<slug>_full_vi.mp4` is `READY`. Never decode video files just to check progress.

### 2. Probe Hardware & Toolchain (`doctor`)
```bash
$AICN_PY <aicn>/scripts/aicn.py doctor --root <project-root>
```
Verifies CUDA/Metal VRAM slots (`512 MB`/slot), `overlay_cuda`, `h264_nvenc`/`hevc_nvenc`/`videotoolbox`, `faster-whisper`, `demucs`, `vieneu`, `ffmpeg`, and `rubberband`.

### 3. Estimate Production Time
Read `<aicn>/references/production_time.md` and calculate using hardware tier (`CUDA RTX` vs `Apple Silicon` vs `CPU`), total source minutes, batch count, and warm-cache hit ratio.

### 4. Reclaim Disk Space After Release (`clean` / `clean --deep`)
Only after `media/final_<unit>_vi.mp4` and `deliverables/final_<slug>_full_vi.mp4` are verified:
```bash
# Dry-run first:
$AICN_PY <aicn>/scripts/aicn.py clean --unit <unit> --dry-run
# Prune stems & TTS clause intermediates (keeps .staging_render.mp4 & master_audio_48k.wav):
$AICN_PY <aicn>/scripts/aicn.py clean --unit <unit>
# Deep archive prune (also removes .staging_render.mp4 & master_audio_48k.wav; Stage 5 stays 'P'):
$AICN_PY <aicn>/scripts/aicn.py clean --unit <unit> --deep
```

### 5. Raw-Preserving Reset (Only When User Explicitly Requests Deleting Broken Data)
- **Confirm scope first** if ambiguous (single unit vs single series hub vs `.cache/`).
- **Never delete `<hub>/source_inbox/` or `posters/official_poster_*`** unless the user explicitly orders deleting the entire series off disk.
- **Never touch other series hubs** under `workspace/*` or `<project-root>/.cache/content_hash.db` unless the user explicitly names them.
