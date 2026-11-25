---
name: video-asr-visual-audit
description: Use when starting a video localization or subtitling job to separate vocals/BGM with Demucs, transcribe fine-grained clauses with Whisper, scan burned-in subtitle bounding boxes, and extract keyframes to visually correct ASR homophones.
---

# Video ASR, Hardsub Scanner & Visual Keyframe Audit

## Overview

Stage 1 of the video localization pipeline. Isolates clean vocal and BGM stems (`htdemucs`), transcribes fine-grained clauses (`faster-whisper`), performs a 5fps stroke-aware pixel scan of the bottom subtitle strip across the entire video (`data/01_zh_bbox_scan.json`), extracts stratified visual keyframes (`frames/audit/*.jpg`), and enforces a **hard CI/CD verification gate (`verify_stage_1.py` -> `.cicd/stage_1_gate.json`)** that blocks Stage 2 translation until every extracted keyframe has been visually inspected (`read_file`) and logged in `01_visual_audit_notes.md`.

**REQUIRED BACKGROUND:** Initialize `workspace/<slug>/` and `.cicd/stage_0_gate.json` via `video-localization-orchestrator` first.

---

## Internal Sub-Skills Catalog (8 Core Capabilities)

### Sub-Skill 2.1: Stem Separation & Vocal Isolation (`htdemucs`)
Separates the source audio into `media/demucs/vocals.wav` (clean speech for Whisper ASR without BGM interference) and `media/demucs/no_vocals.wav` (clean music/SFX bed for Stage 3 auto-ducking).

### Sub-Skill 2.2: Multi-Pass Acoustic ASR & Telephone/Whisper/PA Recovery (`faster-whisper`)
Standard single-pass ASR on Demucs `vocals.wav` fails on **telephone calls, radio comms, PA broadcasts, whispers, and distant screams** because:
1. `htdemucs` strips bandpass-filtered telephone/radio frequencies (`300–3400 Hz`) and heavy-SFX screams into `no_vocals.wav`.
2. Default Silero VAD (`threshold=0.50`) drops low-energy whispers and muffled speech.
- **Mandatory Multi-Pass Solution (`run_asr_and_scan.py`, `asr-3.0`, default `large-v3`):**
  - **Pass 1 (Anti-Stall Global ASR — `large-v3` default):** Blends `70%` clean `vocals.wav` + `30%` raw full-mix audio and runs `faster-whisper` (`int8_float16` first on CUDA for 6 GB VRAM headroom, or `mlx-whisper` on Metal; override via `--model` / `AICN_WHISPER_MODEL`) with `condition_on_previous_text=False`, language-specific `initial_prompt`, sensitive VAD (`threshold=0.20, min_speech_duration_ms=80, min_silence_duration_ms=200, speech_pad_ms=200`), and Pass 1 hallucination filtering so music/SFX never stalls 30–90s context windows.
  - **Pass 2A (Optional Hardsub-Guided Gap Recovery — only when `--with-bbox-scan` is passed):** Cross-references visual hardsub intervals from `01_zh_bbox_scan.json` when opted in (disabled by default in v2.0.0+ because many source videos have no burned-in subtitles).
  - **Pass 2B (Gap-First Multi-Band Frequency ASR + Reverse Cross-Check & Refine — Independent of Hardsub):**
    1. **Gap-First Uncovered Masking (`detect_uncovered_multiband_gaps`):** Masks out already-transcribed Pass 1 spans FIRST (eliminating giant-blob swallowing and the old `0.024–0.080` RMS dead zone) and splits any long uncovered region (`> 10s`) at internal energy valleys.
    2. **4-Band Spectral Decomposition (`build_multiband_audio_streams`):** Decomposes `vocals.wav` + `raw_audio` in RAM into `full_vocal` (`120–7500 Hz`), `high` (`900–7500 Hz`, child/cartoon pitch & breathy whispers), `mid` (`300–3400 Hz`, phone/radio/PA on raw-heavy mix), `low` (`85–1400 Hz`, deep/muffled male voices under high-freq SFX), and `raw_clean` (`100–7500 Hz` unfiltered raw).
    3. **Forward Sweep + Reverse Cross-Check & Refine (`fuse_and_refine_multiband_candidates`):** Probes `full_vocal` (`vad_filter=False`) first; whenever uncovered `< 65%` or borderline confidence (`avg_logprob <= -0.42` / `no_speech_prob >= 0.30`), sweeps active `high` / `mid` / `low` bands in measured dominance order, scores multi-band consensus, runs reverse cross-check on `raw_clean` when Demucs attenuated `vocals`, preserves high-confidence 1-char CJK exclamations, trims spans to active band energy onset/offset, and writes `data/01_pass2b_recovery_report.json` (`verified_non_speech_gaps` + `recovered_by_band`).
  - **Smart Acoustic Gate (`analyze_voice_waveform.py` + `verify_stage_1.py`):** Flags any unprobed strong vocal burst (`peak_voc_rms >= 0.035`) and tracks `longest_uncovered_vocal_gap_s`, while allowing pure BGM/SFX gaps logged in `verified_non_speech_gaps` to pass cleanly without deadlocking Stage 1.
- **Prohibition:** Never pre-merge ASR clauses into 5–8s paragraphs during Stage 1. Fine-grained clause timestamps (`median duration <= 2.8s`) are mandatory.

### Sub-Skill 2.3: Stroke-Aware Multi-Color Hardsub Bounding-Box Scanner (`5 fps`)
Streams the bottom subtitle band (`y=[strip_y0..strip_y0+strip_h]`) at `5 fps` across 100% of the video to measure the exact `[x0, x1, y0, y1]` pixel span of burned-in subtitles in every `0.2s` frame:
- Supports `white` (`RGB > 238`), `yellow` (`R>220, G>190, B<90`), and `auto` dual-color detection.
- **White-Slide Immunity:** When a scene shows a bright white document or web page (`bright_ratio > 0.35`), automatically switches to **gradient/stroke-edge filtering** (`grad_x > 65 | grad_y > 65`) so white slide backgrounds never trigger false full-width subtitle boxes.

### Sub-Skill 2.4: Automated Scene-Cut & Visual Transition Detection
Computes frame-to-frame luminance deltas (`mean_abs_diff > 28.0`) during the 5fps scan to record exact visual cut timestamps (`scene_cuts_sample`) and the top 8 widest subtitle timestamps (`top_wide_timestamps`) in `data/01_zh_bbox_scan.json`.

### Sub-Skill 2.5: Smart Stratified Keyframe Sampling (`extract_audit_frames.py`)
Automatically selects 12+ high-value timestamps combining: (a) the widest burned-in subtitles, (b) major visual scene cuts, and (c) evenly stratified checkpoints (`3%, 15%, 30%, 50%, 72%, 90%` of duration)—or custom `--timestamps`—extracting `frames/audit/frame_XXXXs.jpg` and writing a SHA-256 manifest (`data/01_extracted_audit_manifest.json`).

### Sub-Skill 2.6: Five-Zone Visual OCR & Homophone Disambiguation
Requires the agent to call `read_file` on each extracted `.jpg` in `frames/audit/` to inspect 5 visual zones (bottom hardsub, center slide/UI diagram, floating impact callouts `花字`, speaker chyrons, and 1-line vs. 2-line hardsub height). See [visual_audit_handbook.md](./references/visual_audit_handbook.md) for the complete homophone trap catalog.

### Sub-Skill 2.7: Non-Speech & Montage Interval Identification
Identifies BGM-only intros, transitions, and outro screens where `spans[fi] == None` and no speech occurs, ensuring no phantom subtitles or TTS hallucinations are placed over clean visual graphics.

### Sub-Skill 2.8: Multi-Band Acoustic Waveform & Glyph-IoU Sub-Clause Analysis (`analyze_voice_waveform.py`)
- **Stroke-Validated Center-Clustered Hardsub & Glyph-IoU Clause Transition Detection (`scan_hardsub_and_scenes`):**
  - Restricts subtitle glyph detection to the calibrated subtitle band (`y=[32..86]` in the 200px bottom strip) and requires each white/yellow glyph pixel to have a dark outline/stroke neighbor (`minimum_filter(luma, size=(7, 7)) < 118`), recovering 100% of subtitles over bright backgrounds (white tables, bright walls, glowing screens) while ignoring isolated side reflections.
  - Tracks frame-to-frame pixel mask Intersection-over-Union (`glyph_iou < 0.48`) alongside bounding-box edge jumps (`|dx0| > 22px` or `|dx1| > 22px`), splitting back-to-back subtitle clauses even when two consecutive lines have identical character counts/widths or zero blank gap between them (e.g., rapid phone call clauses).
- **Multi-Band Acoustic Waveform, Premature Lead-In & Silent Gap Analyzer (`analyze_voice_waveform.py`):**
  - Computes 50ms-hop RMS energy and Zero-Crossing Rate (`ZCR`) across both `vocals.wav` and `300–3400 Hz` telephone/speech formant bandpass-boosted audio (`dynaudnorm`).
  - Cross-checks all fine-grained visual sub-clauses (`hardsub_intervals`) and acoustic voice bursts against ASR/script segments, flagging into `data/01_waveform_voice_audit.json`:
    1. Uncovered visual sub-clauses (`< 60%` overlap),
    2. Bloated segment durations (`dur > zh_chars * 0.35 + 1.4s`),
    3. **Premature Subtitle Lead-In (`premature_lead_segments_count`)**: segments whose `start` appears `> 0.22s` before the first visual hardsub frame (`hs_lead_s > 0.22s`) or `> 0.30s` before acoustic voice onset when no hardsub is present (caused by Whisper attaching silent establishing-shot ambient noise before the actor speaks),
    4. **Unsplit Internal Silent Gaps (`internal_blank_gap_segments_count`)**: compound segments spanning 2+ distinct visual sub-clauses separated by a `>= 0.40s` silent/non-hardsub gap (causing the second clause to appear prematurely on screen during the silent reaction shot).
  - Supports `--snap-and-split` mode (`analyze_voice_waveform.py --workspace <ws> --snap-and-split`), which automatically splits compound multi-interval segments and clamps every segment's `[start, end]` to the exact active onset/offset of its visual hardsub and voice waveform.

### Sub-Skill 2.9: Stage 1 Cryptographic CI/CD Gate Validator (`verify_stage_1.py`)
Programmatically verifies 5 weighted checks (`100/100` required):
1. `audio_stem_or_native_source_ready` (`15 pts`): `media/demucs/no_vocals.wav` exists (`voice_dub` mode) or native stereo source audio is ready (`sub_only` mode).
2. `asr_fine_grained_clauses_and_no_placeholders` (`25 pts`): `data/01_asr_raw_segments.json` has fine-grained clauses (`median duration <= 2.8s`) and `0` unresolved `[GAP_VISUAL_SUB]` placeholders.
3. `hardsub_bbox_scan_complete` (`15 pts`): `data/01_zh_bbox_scan.json` covers `>= 97%` of source video duration.
4. `hardsub_gap_coverage_phone_whisper_complete` (`20 pts`): `>= 96%` of fine-grained visual sub-clauses (`hardsub_intervals` split by BBox-Jump + Glyph-IoU) are covered with `0` bloated segments (`bloated_segments_count == 0`), `0` premature lead-in segments (`premature_lead_segments_count == 0`), and `0` unsplit internal blank gaps (`internal_blank_gap_segments_count == 0`).
5. `visual_audit_frames_inspected_and_logged` (`25 pts`): Every frame in `data/01_extracted_audit_manifest.json` (`>= 10` frames) exists on disk with matching SHA-256 hash AND is explicitly documented in `01_visual_audit_notes.md`.
6. Writes `.cicd/stage_1_gate.json` (`100/100 PASS`). Without this token, Stage 2 (`validate_bilingual_script.py`) refuses to run.

---

## Step-by-Step Execution Workflow

### Step 1A: Separate Vocals/BGM (if not already separated)
Run using `$AICN_PY` (macOS `/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python` or Windows `C:\Work\Tools\VieNeu-TTS\.venv\Scripts\python.exe`):
```bash
$AICN_PY -m demucs.separate \
  -n htdemucs --two-stems=vocals \
  -o "workspace/bilibili-anthropic-distill/media/demucs" \
  "bilibili/input.mp4"
```
*(Ensure `vocals.wav` and `no_vocals.wav` are placed in `workspace/<slug>/media/demucs/`).*

### Step 1B: Run 5fps Hardsub Scan, Scene-Cut Detection & Fine-Grained ASR
```bash
$AICN_PY .agents/skills/video-asr-visual-audit/scripts/run_asr_and_scan.py \
  --video "bilibili/input.mp4" \
  --workspace "workspace/bilibili-anthropic-distill" \
  --color-mode "auto"
```

### Step 1C: Extract Audit Keyframes & Generate SHA-256 Manifest
```bash
$AICN_PY .agents/skills/video-asr-visual-audit/scripts/extract_audit_frames.py \
  --video "bilibili/input.mp4" \
  --workspace "workspace/bilibili-anthropic-distill" \
  --timestamps "18,37,76,113,136,196,310,533,600,655,681,683,738"
```
*(Omit `--timestamps` to use automatic smart selection from `01_zh_bbox_scan.json`).*

### Step 1D: Inspect Frames via `read_file` & Populate `01_visual_audit_notes.md`
Call `read_file` on every `.jpg` listed in `data/01_extracted_audit_manifest.json` and document the exact on-screen Chinese/English terms and visual cues in `workspace/<slug>/01_visual_audit_notes.md`.

### Step 1E: Run Stage 1 CI/CD Gate Validator (MANDATORY)
```bash
$AICN_PY .agents/skills/video-asr-visual-audit/scripts/verify_stage_1.py \
  --workspace "workspace/bilibili-anthropic-distill"
```
Do NOT proceed to `zh-vi-script-adapter` until `verify_stage_1.py` prints `Status: PASS | Score: 100.0/100`.
