---
name: video-localization-qa-review
description: Use when staging a rendered localized MP4 video, extracting stratified QA keyframes, inspecting dual-corner channel branding and visual sanity, and verifying audio/video stream integrity before releasing a unit.
---

# Video Localization Staging QA & Release Gate (`v3.0.0`)

## Overview

Stage 5 (unit release gate) of the video localization pipeline (`v3.0.0` dub + dual-corner watermark + external `.srt`, no burned-in subtitle overlays). Works immediately after Stage 4 (`aicn.py render` + `aicn.py verify-render` → `media/.staging_render.mp4`):
1. `composite_and_verify.py` hardlinks `media/.staging_render.mp4` (or `--import-existing-mp4`) to `media/.staging_final.mp4` (0 extra disk bytes), extracts `12` stratified QA keyframes (`frames/qa/frame_*s.jpg`) using 4 parallel workers, and hashes every frame via `ap.content_sha256` into `data/04_qa_frames_manifest.json`.
2. You MUST call `read_file` on **every** extracted QA frame in `data/04_qa_frames_manifest.json` and log 1 `PASS` row per frame in `04_qa_review_report.md`.
3. `verify_stage_5.py` runs the 5-check `100/100` gate validator (duration within `±0.30s` of source, codec/audio compliance, intact QA manifest & complete visual log) and promotes `media/.staging_final.mp4` → `media/final_<unit>_vi.mp4`.

**REQUIRED BACKGROUND:** Complete Stage 4 (`aicn.py render` + `aicn.py verify-render`) and verify `.cicd/stage_4_gate.json` == `PASS` first.

---

## Core Capabilities (`v3.0.0`)

### 1. Staging-First Output Lock (`media/.staging_final.mp4`)
`composite_and_verify.py` deletes any stale `media/final_<unit>_vi.mp4` before staging and hardlinks `.staging_render.mp4` → `media/.staging_final.mp4`. Only `verify_stage_5.py` promotes `.staging_final.mp4` → `media/final_<unit>_vi.mp4` upon scoring `100/100`.

### 2. Stream Sanitization (`-sn`) & Codec Compliance
Both `h264` and `hevc` (`hvc1`) video streams are accepted alongside `48000 Hz` stereo `aac` audio and **`0` subtitle streams** (`-sn`, external `.srt` only).

### 3. Parallel Stratified Keyframe Extraction (`frames/qa/`)
Extracts `12` stratified keyframes spanning early, mid, and late timeline checkpoints (`frames/qa/frame_*s.jpg`) across 4 parallel FFmpeg workers (`composite_and_verify.py`) and computes content SHA-256 hashes in `data/04_qa_frames_manifest.json`. See [qa_defect_triage_manual.md](./references/qa_defect_triage_manual.md).

### 4. Mandatory Agent Visual Inspection (`read_file`)
Call `read_file` on **every** `.jpg` listed in `data/04_qa_frames_manifest.json` to inspect:
- Clean, sharp video frames matching source resolution (`1080×1920`, `720×1280`, or `1920×1080`) with zero encoder blocking, corruption, or unwanted artifacts.
- Proper **dual-corner watermark placement** (`13_top_left_logo_large.png` top-left + `15_top_right_vietsub_floating.png` top-right): proportional scaling on portrait frames, crisp edges, zero white halo, legible shadow contrast.
- Zero unexpected stale or stray overlays.

### 5. Stage 5 Cryptographic Release Validator (`verify_stage_5.py`, `100/100`)
Enforces 5 weighted checks (`100/100` required to sign `.cicd/stage_5_gate.json` and promote `media/final_<unit>_vi.mp4`):
1. `all_upstream_cicd_gates_signed` (`15 pts`): `.cicd/stage_0..4_gate.json` all verified `PASS` (`100/100`) with intact artifact SHA-256 hashes.
2. `staging_mp4_stream_and_codec_compliance` (`25 pts`): `ffprobe` confirms `h264` or `hevc`, `48000 Hz` stereo `aac`, `0` subtitle streams, and duration within `±0.30s` of source.
3. `qa_frames_extracted_and_verified` (`25 pts`): `>= 10` QA frames in `04_qa_frames_manifest.json` exist on disk with matching SHA-256 hashes.
4. `agent_visual_audit_report_completeness` (`20 pts`): Every frame filename in `04_qa_frames_manifest.json` is explicitly logged with `PASS` in `04_qa_review_report.md` (zero `_TBD_` or `FAIL` entries).
5. `release_promotion_and_srt_sync` (`15 pts`): Hardlinks/promotes `media/.staging_final.mp4` → `media/final_<unit>_vi.mp4` inside the unit (and `--release-path` only if explicitly specified). Once ALL batches pass Stage 5, `aicn.py merge-series --hub <hub>` builds the publishable full-series deliverable at `deliverables/final_<series>_full_vi.mp4`.

---

## Execution Commands

Use `$AICN_PY` (`C:\Work\Tools\VieNeu-TTS\.venv\Scripts\python.exe` on Windows, `/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python` on macOS).

### Step 5A: Stage `.staging_render.mp4` & Extract 12 Stratified QA Frames
```bash
$AICN_PY <aicn>/vendor/video-localization-qa-review/scripts/composite_and_verify.py \
  --video "workspace/<series>/units/<unit>/source/master_source.mp4" \
  --workspace "workspace/<series>/units/<unit>" \
  --slug "<unit>"
# (--import-existing-mp4 defaults to media/.staging_render.mp4)
```

### Step 5B: Visually Inspect Every QA Frame via `read_file` & Update `04_qa_review_report.md`
Open every `.jpg` listed in `data/04_qa_frames_manifest.json` with `read_file` and log 1 `PASS` row per frame in `workspace/<series>/units/<unit>/04_qa_review_report.md`.

### Step 5C: Run Stage 5 Gate Validator to Promote `media/final_<unit>_vi.mp4`
```bash
$AICN_PY <aicn>/vendor/video-localization-qa-review/scripts/verify_stage_5.py \
  --workspace "workspace/<series>/units/<unit>"
```
Never claim unit completion until `verify_stage_5.py` prints `Status: PASS | Score: 100.0/100`, and never upload individual units — run `aicn.py merge-series --hub workspace/<series>` once all working batches pass Stage 5.
