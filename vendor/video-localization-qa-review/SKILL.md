---
name: video-localization-qa-review
description: Use when compositing the final localized MP4 video and verifying audio/video streams, visual subtitle layout, hardsub masking, and frame-level alignment before delivering to the user.
---

# Video Localization Staging Composite, Computer-Vision QA & Release Gate

## Overview

Stage 5 (final release gate) of the video localization pipeline. Enforces a **Staging-First Output Lock**: `composite_and_verify.py` encodes the composited video into a hidden staging file (`media/.staging_final.mp4`), extracts `12+` stratified QA keyframes (`frames/qa/frame_*s.jpg`), and runs an automated Computer-Vision pill & glyph scan (`data/04_qa_frames_manifest.json`). The final deliverable (`media/final_<slug>_vi.mp4`) **cannot exist on disk** until the agent inspects every extracted QA frame via `read_file`, documents each frame in `04_qa_review_report.md`, and passes the **5-check, 100-point CI/CD Release Validator (`verify_stage_5.py` -> `.cicd/stage_5_gate.json`)**, which then promotes `.staging_final.mp4` to `final_<slug>_vi.mp4`.

**REQUIRED BACKGROUND:** Complete Stages 0–4 via `video-localization-orchestrator` and verify `.cicd/stage_4_gate.json` == `PASS` first.

---

## Internal Sub-Skills Catalog (9 Core Capabilities)

### Sub-Skill 6.1: Staging-First Output Lock (`media/.staging_final.mp4`)
Prevents premature completion claims. `composite_and_verify.py` deletes any stale `media/final_<slug>_vi.mp4` before encoding and writes exclusively to `media/.staging_final.mp4`. Only `verify_stage_5.py` promotes the staging file to `media/final_<slug>_vi.mp4` upon scoring `100/100`.

### Sub-Skill 6.2: Stream Sanitization & Zero-Duplicate-Subtitle Enforcement (`-sn`)
**STRICT PROHIBITION:** Never embed a soft-subtitle stream (`mov_text` or `ass`) inside the final MP4. Passing `-sn` guarantees clean video + audio streams (with optional dual-corner watermarks). Encodes video with `h264_videotoolbox` (or stream-copy when no watermark is set) and audio with `aac -b:a 192k -ar 48000 -movflags +faststart`.

### Sub-Skill 6.3: Flushed Real-Time Encoding Telemetry
Parses `ffmpeg -progress pipe:1` and prints flushed progress every 10 seconds (`Encoded Xs / Ys (Z%) | speed=... | elapsed=...`).

### Sub-Skill 6.4: Stratified 5-Category Keyframe Extraction (`frames/qa/`)
Extracts `12–16` keyframes (`frames/qa/frame_*s.jpg`) spanning 5 risk strata: (1) widest Chinese hardsubs, (2) longest 2-line Vietnamese cues, (3) visual diagram/tag pointers (`<|user|>`, `<|assistant|>`, `<|end|>`), (4) speech-to-pause boundaries, and (5) early/mid/late timeline checkpoints. See [qa_defect_triage_manual.md](./references/qa_defect_triage_manual.md).

### Sub-Skill 6.5: Automated Computer-Vision Pill & Glyph Inspector
On every extracted QA frame at an active cue, inspects the pixel region `[box_x0+16..box_x1-16, strip_y0+box_top+8..strip_y0+box_bottom-8]` to verify that the opaque dark pill is rendered (`dark_pill_ratio >= 0.45`) and crisp white Vietnamese text glyphs are present (`white_glyph_pixels >= 60`), recording SHA-256 hashes and `cv_status` in `data/04_qa_frames_manifest.json`.

### Sub-Skill 6.6: Mandatory Agent Multimodal Visual Audit (`read_file`)
Automated pixel checks must be paired with semantic visual verification. You MUST call `read_file` on the extracted `frames/qa/frame_*s.jpg` images to verify with your own eyes:
- Zero white/yellow Chinese characters peeking out from the left, right, top, or bottom of the dark pill.
- Vietnamese text is 1–2 balanced lines, centered, with zero diacritic clipping.
- On-screen visual pointers/diagrams match the Vietnamese subtitle at that exact timestamp.

### Sub-Skill 6.7: Surgical Defect Triage & Rollback
If any frame fails either the automated CV scan or your `read_file` visual inspection, use the Surgical Rollback Matrix in `video-localization-orchestrator` to fix the exact offending segment in Stage 2, 3, or 4 without wiping the workspace.

### Sub-Skill 6.8: Stage 5 Cryptographic Release Validator (`verify_stage_5.py`)
Enforces 5 weighted checks (`100/100` required to sign `.cicd/stage_5_gate.json` and promote `media/final_<slug>_vi.mp4`):
1. `all_upstream_cicd_gates_signed` (`15 pts`): `.cicd/stage_0..4_gate.json` all verified `PASS` (`100/100`) with intact artifact SHA-256 hashes.
2. `staging_mp4_stream_and_codec_compliance` (`25 pts`): `ffprobe` confirms `h264`, `48000 Hz` stereo `aac`, `0` subtitle streams, and duration within `±0.25s` of source.
3. `automated_cv_frame_inspection` (`25 pts`): `>= 10` QA frames in `04_qa_frames_manifest.json` exist on disk with matching SHA-256 hashes and `100%` `cv_status == "PASS"`.
4. `agent_visual_audit_report_completeness` (`20 pts`): Every frame filename in `04_qa_frames_manifest.json` is explicitly logged with `PASS` in `04_qa_review_report.md` (zero `_TBD_` or `FAIL` entries).
5. `release_promotion_and_srt_sync` (`15 pts`): Promotes `media/.staging_final.mp4` -> `media/final_<slug>_vi.mp4` (and `--release-path` if specified).

### Sub-Skill 6.9: Channel Branding, 2x2 Grid Slicing & 16:9 Dual-Corner Watermark Engine (`apply_channel_branding.py`)
Automates channel logo extraction (`01..06_transparent.png` from a `2x2` Master Brand Grid with border flood-fill, counter-hole knockout, and `3x3` minimum-filter RGB edge color decontamination) and renders the **16:9 In-Frame Dual-Corner Header** (`Top-Left Large Logo 13_top_left_logo_large.png` at `x=42, y=22` + `Top-Right "Vietsub by <Channel>" + Red Seal` at `x=W-w-42, y=58` in `floating` [default], `pill`, or `3d_wordmark` style).
- Automatically burned during `composite_and_verify.py` when `watermark.enabled == true` in `config/project_config.json`, or applied standalone via `apply_channel_branding.py --burn-video`.

---

## Execution Commands

### Step 5A: Composite to Locked Staging File & Run CV Scan
```bash
/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python .agents/skills/video-localization-qa-review/scripts/composite_and_verify.py \
  --video "bilibili/input.mp4" \
  --workspace "workspace/bilibili-anthropic-distill" \
  --slug "bilibili-anthropic-distill" \
  --qa-timestamps "18,37,76,113,136,196,310,533,600,655,681,683,738,745"
```

### Step 5B: Inspect Extracted QA Frames via `read_file` & Update `04_qa_review_report.md`
Call `read_file` on `workspace/<slug>/frames/qa/frame_*s.jpg` and record the verification row for every frame in `workspace/<slug>/04_qa_review_report.md`.

### Step 5C: Run Stage 5 CI/CD Release Validator to Promote Final Output
```bash
/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python .agents/skills/video-localization-qa-review/scripts/verify_stage_5.py \
  --workspace "workspace/bilibili-anthropic-distill" \
  --release-path "bilibili/final_anthropic_distillation_vietnamese.mp4"
```
Never claim completion until `verify_stage_5.py` prints `Status: PASS | Score: 100.0/100` and promotes `media/final_<slug>_vi.mp4`.

### Step 5D: Slice 2x2 Brand Grid, Build Dual-Corner Badges, or Burn Watermark (`apply_channel_branding.py`)
To slice a 2x2 brand grid, rebuild the Top-Left Logo (`13`) + Top-Right `"Vietsub by <Channel>"` badges (`14=pill`, `15=floating` [default], `18=3d_wordmark`), or burn the Dual-Corner Watermark onto a `16:9` MP4:
```bash
uv run --with pillow --with numpy --with scipy python3 .agents/skills/video-localization-qa-review/scripts/apply_channel_branding.py \
  --brand-dir "workspace/branding-dau-do" \
  --channel-name "Đậu Đỏ" \
  --credit-prefix "Vietsub by " \
  --style "floating" \
  --burn-video "workspace/drama-quy-khu-s01/deliverables/16x9/final_drama-quy-khu-s01_vi.mp4"
```

### Step 5E (Multi-Episode Compilation Splitter): Export Individual Localized Episodes (`deliverables/episodes/`)
When `topology == "series_compilation"` and `data/00_episodes_manifest.json` contains `>= 2` episodes, slice the verified `16:9` MP4, `9:16` MP4, and `.srt` back into individual localized episodes (with timestamps re-zeroed to `00:00:00.000` per episode):
```bash
/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python .agents/skills/video-localization-qa-review/scripts/split_compiled_episodes.py \
  --workspace "workspace/drama-quy-khu-s01"
```
Outputs `deliverables/episodes/<ep_id>_<slug>_vi_16x9.mp4`, `<ep_id>_<slug>_vi_9x16.mp4`, `<ep_id>_<slug>_vi.srt`, and `episodes_export_manifest.json`.
