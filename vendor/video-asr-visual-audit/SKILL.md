---
name: video-asr-visual-audit
description: Use when starting a video localization job to separate vocals/accompaniment stems with Demucs, transcribe fine-grained clauses with Whisper multi-pass recovery, align strictly non-overlapping timestamps via acoustic waveform analysis, and extract keyframes to visually audit ASR homophones.
---

# Video ASR, Acoustic Alignment & Visual Keyframe Audit (`v3.0.0`)

## Overview

Stage 1 of the video localization pipeline (`asr-4.0`, `wf-4.0`, `voice_dub`-only). Separates clean 48 kHz `PCM_16` vocal and accompaniment stems (`aicn_audio.py separate` / `batch-separate`), runs two-pass Whisper `large-v3` ASR with anti-orphan Pass 1 and collar-masked / deduplicated multi-band Pass 2B recovery (`run_asr_and_scan.py`), snaps and enforces strictly non-overlapping monotonic timestamps against the acoustic waveform (`analyze_voice_waveform.py --snap-and-split`), extracts 12+ stratified/Pass-2B audit keyframes (`extract_audit_frames.py`), and enforces the **100/100 CI/CD gate (`verify_stage_1.py` → `.cicd/stage_1_gate.json`)** after every extracted keyframe is inspected via `read_file` and logged in `01_visual_audit_notes.md`.

**REQUIRED BACKGROUND:** Initialize the unit workspace and `.cicd/stage_0_gate.json` via `video-localization-orchestrator` first.

---

## Core Capabilities (`v3.0.0`)

### 1. Global Content-Addressed Demucs Stems (`media/stems/{vocals,accomp}.wav`)
Separates the source audio using `htdemucs` (`torch-cuda` under `ap.gpu_slot("demucs", vram_mb=1024)` on NVIDIA CUDA or `demucs-mlx` on macOS Metal) into `48000 Hz` `PCM_16` stems at `media/stems/vocals.wav` and `media/stems/accomp.wav`, cached globally in `<global_cache>/stems/<stems_key>/` (`STEMS_VERSION = "aicn-audio-1.0"`). Always run via `aicn_audio.py separate` or `batch-separate` — never raw `python -m demucs.separate`.

### 2. Multi-Pass Whisper `large-v3` ASR (`run_asr_and_scan.py`, `ASR_PIPELINE_VERSION = "asr-4.0"`)
- **Warm Multi-Unit Batch Mode (`--units u1,u2,... [--workers N]`):** Loads Whisper (`large-v3`, CUDA `int8_float16` or MLX Metal) **ONCE** per worker stream (`0=auto`) under `ap.gpu_slot("whisper", vram_mb=2560)` across all cache-miss units while pre-decoding unit `k+1` audio in a background CPU thread. Single-unit `--video <mp4> --workspace <ws>` remains fully supported.
- **Pure Content-SHA-256 Caching:** Reads/writes `<global_cache>/asr/<stage1_asr_key>.json` using `ap.stage1_asr_key(vid_sha, voc_sha, model, source_lang, "asr-4.0")` and syncs `build_state.json["stage1_key"]` (zero path dependence).
- **Pass 1 (Anti-Stall & Anti-DTW-Orphan):**
  - Uses `70%` `vocals.wav` + `30%` raw mix, `condition_on_previous_text=False`, language-aware `initial_prompt`, tightened VAD (`threshold=0.20`, `speech_pad_ms=80`, `min_speech_duration_ms=80`), and hallucination/repetition filtering.
  - Guards `>= 1.0s` word-gap splitting with local acoustic RMS verification so single CJK characters are never stranded as isolated DTW orphans across silent noise drift.
- **Pass 2B (Gap-First Multi-Band + Protective Collar + Strict Boundary Deduplication):**
  1. **Outward `+0.08s` Protective Collar:** Builds Pass 1 `covered` mask expanded by `+0.08s` outward around Pass 1 spans so boundary tails never leak back in.
  2. **Gap-First Multi-Band Streams:** Decomposes uncovered gaps (`> 10s` split at energy valleys; skips `< 1.0s` non-vocal BGM ticks) into `full_vocal` (`120–7500 Hz`), `high` (`900–7500 Hz`), `mid` (`300–3400 Hz`), `low` (`85–1400 Hz`), and `raw_clean` (`100–7500 Hz`), packing consecutive gaps into `<= 22s` stitched buffers with `0.65s` digital-zero barriers and **no `initial_prompt`** when `vad_filter=False`.
  3. **Strict Slice Clamping & Boundary Dedup:** Clamps slice padding strictly outside neighbors (`pad_s = max(prev_end + 0.02, gst - 0.15)`, `pad_e = min(next_start - 0.02, get + 0.15)`), drops collar-bleed words outside `[gst - 0.02, get + 0.02]`, verifies full unchopped segment `_is_hallucination()` before slot splitting, and rejects boundary fragments/duplicates within `0.35s` of an adjacent Pass 1 or Pass 2B segment if:
     - `ov_existing / dur >= 0.20`, OR
     - candidate has `<= 2` CJK chars sharing any CJK char / prefix / suffix with the adjacent segment, OR
     - CJK character containment `|S_cand & S_nb| / min(|S_cand|, |S_nb|) >= 0.55`.
     - 1-char CJK exclamations require `gap >= 0.30s` on both sides, `avg_logprob > -0.35`, `no_speech_prob < 0.20`, and `peak_voc_rms >= 0.045`.
  4. Logs all recoveries and `verified_non_speech_gaps` to `data/01_pass2b_recovery_report.json`.
- **Prohibition:** Never pre-merge ASR clauses into 5–8s paragraphs in Stage 1 (`median duration <= 2.8s` required).

### 3. Strictly Monotonic Acoustic Alignment (`analyze_voice_waveform.py --snap-and-split`, `wf-4.0`)
- Pure acoustic alignment on `vocals.wav` + `300–3400 Hz` bandpass energy (zero visual bbox or spectral-toy clutter).
- Splits compound segments containing `>= 0.40s` internal silent gaps and clamps `[start, end]` to active acoustic voice onset/offset (`premature_lead_segments == 0`, `bloated_segments == 0`).
- Cleanly absorbs `< 0.25s` micro-fragments/stutter echoes within `0.20s` and drops `< 0.15s` stubs.
- **Strict Post-Alignment Monotonic Invariant:** Guarantees across ALL segments:
  `0.0 <= start[i] < end[i] <= start[i+1] - 0.02` AND `(end[i] - start[i]) >= 0.15`.

### 4. Smart Stratified & Pass-2B Keyframe Sampling (`extract_audit_frames.py`)
Selects `>= 12` high-value keyframes prioritizing **Pass 2B recovered timestamps** (`source` starting with `pass2b`) plus evenly stratified timeline checkpoints (`frames/audit/frame_XXXXs.jpg`), hashing each via `ap.content_sha256` into `data/01_extracted_audit_manifest.json`.

### 5. Visual OCR & Homophone Disambiguation (`read_file`)
Inspect every extracted `.jpg` in `frames/audit/` via `read_file` across on-screen hardsubs (if present), center slides/diagrams, floating callouts (`花字`), and speaker nameplates to fix phonetic Whisper homophones before Stage 2 translation. See [visual_audit_handbook.md](./references/visual_audit_handbook.md).

### 6. Stage 1 Cryptographic CI/CD Gate Validator (`verify_stage_1.py`, `100/100`)
Enforces 4 deterministic checks (`100` pts total):
1. `stems_ready` (`20 pts`): `media/stems/vocals.wav` and `media/stems/accomp.wav` exist and match source duration.
2. `asr_monotonic_and_clean` (`25 pts`): `median_dur <= 2.8s`, `0` overlaps/inversions (`end[i] <= start[i+1] - 0.02`), `0` `< 0.15s` runts, `0` boundary duplicate fragments, `0` empty/placeholder texts.
3. `acoustic_waveform_voice_coverage` (`30 pts`): `acoustic_speech_coverage_ratio >= 0.90`, `bloated_segments == 0`, `premature_lead_segments == 0`, `longest_uncovered_vocal_gap_s <= 6.0s` (with `verified_non_speech_gaps` honored).
4. `visual_audit_frames_inspected_and_logged` (`25 pts`): `>= 10` frames in `01_extracted_audit_manifest.json` exist with matching SHA-256 AND every frame filename is documented in `01_visual_audit_notes.md`.

---

## Execution Workflow

Use `$AICN_PY` (`C:\Work\Tools\VieNeu-TTS\.venv\Scripts\python.exe` on Windows, `/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python` on macOS).

### Option A: Warm Multi-Unit Prep Across a Series (Recommended)
```bash
$AICN_PY <aicn>/scripts/aicn.py run-series --hub workspace/<series> --phase prep [--units unit_01,unit_02]
# View every frames/audit/*.jpg via read_file -> fill 01_visual_audit_notes.md -> run verify_stage_1.py per unit
```

### Option B: Single-Unit Step-by-Step
```bash
# 1A. Separate 48 kHz PCM_16 stems (cached globally):
$AICN_PY <aicn>/scripts/aicn_audio.py separate --unit "workspace/<series>/units/<unit>"

# 1B. Multi-pass Whisper ASR (or batch across units with --units u1,u2):
$AICN_PY <aicn>/vendor/video-asr-visual-audit/scripts/run_asr_and_scan.py \
  --video "workspace/<series>/units/<unit>/source/master_source.mp4" \
  --workspace "workspace/<series>/units/<unit>" \
  [--source-lang zh|en|auto] [--model large-v3] [--force]

# 1C. Strictly monotonic acoustic snap-and-split alignment:
$AICN_PY <aicn>/vendor/video-asr-visual-audit/scripts/analyze_voice_waveform.py \
  --workspace "workspace/<series>/units/<unit>" \
  --snap-and-split

# 1D. Extract 12+ Pass-2B & stratified audit frames:
$AICN_PY <aicn>/vendor/video-asr-visual-audit/scripts/extract_audit_frames.py \
  --video "workspace/<series>/units/<unit>/source/master_source.mp4" \
  --workspace "workspace/<series>/units/<unit>"

# 1E. Call read_file on EVERY JPG in data/01_extracted_audit_manifest.json -> populate 01_visual_audit_notes.md

# 1F. Sign Stage 1 CI/CD gate (100/100 required):
$AICN_PY <aicn>/vendor/video-asr-visual-audit/scripts/verify_stage_1.py \
  --workspace "workspace/<series>/units/<unit>"
```
Do NOT proceed to `zh-vi-script-adapter` until `verify_stage_1.py` prints `Status: PASS | Score: 100.0/100`.
