---
name: vieneu-voice-pacing-mixer
description: Use when synthesizing Vietnamese narration with VieNeu-TTS, enforcing natural speech pacing without robotic speedups, locking visual cue start timestamps, and mixing 48kHz voice with auto-ducked background music.
---

# VieNeu-TTS 48kHz Voice Synthesis, Elastic Pacing & BGM Mixer

## Overview

Stage 3 of the video localization pipeline. Supports two configurable audio architectures (`localization_mode` in `data/00_video_profile.json` or `--mode voice_dub | sub_only`):
- **`voice_dub` Mode:** Synthesizes all `tts_text` entries at `48000 Hz` using `VieNeu-TTS v3 Turbo` (ONNX `fp32`), enforces a strict natural tempo ceiling (`<= 1.24x`), hard-locks every segment's `slot_start` to prevent cumulative visual drift, preserves natural breathing pauses (`~0.22s`), and mixes the voice track with `no_vocals.wav` using smooth cosine auto-ducking.
- **`sub_only` Mode (Native Audio Passthrough):** Skips TTS synthesis, extracts the original stereo audio track losslessly at `48000 Hz` (`media/master_audio_48k.wav`) to preserve 100% of the native voice acting, telephone call acoustics, emotional screams, and cinema sound design, and maps `02_bilingual_script.json` directly to `data/03_aligned_subtitles.json` and `data/subtitles_full.srt` with `tempo = 1.0x`.
Enforces a **4-check, 100-point CI/CD gate (`verify_stage_3.py` -> `.cicd/stage_3_gate.json`)** before Stage 4 overlay rendering can run.

**REQUIRED BACKGROUND:** Complete Stage 2 (`zh-vi-script-adapter`) and verify `.cicd/stage_2_gate.json` == `PASS` first.

---

## Internal Sub-Skills Catalog (8 Core Capabilities)

### Sub-Skill 4.1: `VieNeu-TTS v3 Turbo` Engine & Multi-Persona Dispatch
- Always run with `/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python` (never system `python3`).
- Instantiate `Vieneu()` with default cached `fp32` ONNX weights (`RTF ≈ 0.32` on Apple Silicon). Never pass `precision="int8"` unless already cached locally.
- Supports single-narrator (`--voice "Hải Đăng"`) and multi-speaker drama scripts (where each item in `02_bilingual_script.json` can specify `"voice": "Hải Đăng" | "Ngọc" | "Bình" | "Tuyên"`). See [audio_dsp_and_pacing_manual.md](./references/audio_dsp_and_pacing_manual.md) for voice persona selection.

### Sub-Skill 4.2: Plosive-Safe Silence Trimming (`trim_silence`)
Trims leading/trailing dead air from each synthesized `seg_XXXX.wav` at amplitude threshold `0.008` while preserving a mandatory `30ms` (`1440` samples at `48kHz`) attack/release margin and applying a `15ms` linear boundary fade so initial plosives (`p, t, c, k, s`) and final stops (`-p, -t, -c`) are never clipped.

### Sub-Skill 4.3: Hard-Locked `slot_start` & Zero-Drift Timeline Mathematics
**STRICT PROHIBITION:** Never delay or push `seg[i+1].start` forward when `seg[i]` runs long. Pushing subsequent segments creates cumulative drift across a 12-minute video and breaks visual synchronization.
- Every segment `i` is **hard-locked** to its original `start` timestamp (`|slot_start - script_start| <= 25ms`).
- Bounded elastic ceiling: `ceiling = min(seg[i].end + 0.12, seg[i+1].start - 0.14)`.

### Sub-Skill 4.4: Closed-Loop Text Tightening for Over-Budget Segments (`> 1.24x`)
`synthesize_and_audit_pacing.py` computes `raw_ratio = trimmed_wav_dur / avail_dur` for every segment.
- **If any segment has `raw_ratio > 1.24x`:** The script exits with code `2` and lists the exact segment IDs.
- **Mandatory Recovery Loop:** Never increase `atempo` above `1.25x`. Instead, shorten `sub_text` and `tts_text` for those specific segment IDs in `data/02_bilingual_script.json` by 15–25%, re-run `validate_bilingual_script.py` (to re-sign Stage 2 gate), delete only the affected `seg_XXXX.wav` files, and re-run `synthesize_and_audit_pacing.py --only-missing` until `100%` of segments pass `<= 1.24x`.

### Sub-Skill 4.5: Natural Micro-Pauses & Prosodic Breathing Room
Reserves at least `0.14s–0.28s` of inter-clause breathing silence (`mean_breathing_pause >= 0.14s`, typically `~0.22s`) so the narrator sounds calm and human rather than a breathless machine gun.

### Sub-Skill 4.6: Multi-Rate Stem Resampling (`44.1kHz` -> `48kHz` Stereo)
`htdemucs` outputs `no_vocals.wav` at `44100 Hz`. `assemble_master_audio.py` resamples `no_vocals.wav` on the fly via `ffmpeg -f f32le -ar 48000 -ac 2` so the stereo BGM bed aligns sample-for-sample with the `48000 Hz` voice track.

### Sub-Skill 4.7: Cosine Envelope Auto-Ducking & Headroom Normalization
Convolves the binary speech activity mask with a `400ms` (`19,200` samples) Hanning window to smoothly duck the BGM bed to `0.28` during active narration and swell it to `0.65` during pauses/transitions, normalizing peak amplitude to `<= 0.92` (`-0.7 dBFS`).

### Sub-Skill 4.8: Stage 3 Cryptographic CI/CD Gate Validator (`verify_stage_3.py`)
Enforces 4 weighted deterministic checks (`100/100` required to sign `.cicd/stage_3_gate.json`):
1. `tts_segments_complete_48k` (`20 pts`): All `N` WAV files (`seg_0000.wav .. seg_{N-1:04d}.wav`) exist in `media/tts_segments/` and are non-silent.
2. `hard_locked_slot_start_zero_drift` (`25 pts`): Every segment in `data/03_aligned_subtitles.json` has `|slot_start - script_start| <= 25ms` and `0` overlaps (`slot_end <= next_slot_start`).
3. `natural_tempo_and_breathing_pauses` (`25 pts`): `max_tempo <= 1.26x`, `mean_tempo <= 1.18x`, and `mean_breathing_pause >= 0.14s`.
4. `master_audio_48k_stereo_and_srt_valid` (`30 pts`): `media/master_audio_48k.wav` is `48000 Hz` stereo matching source duration (`±0.20s`), and `data/subtitles_full.srt` contains all `N` subtitle blocks.

---

## Execution Commands

### Step 3A: Synthesize 48kHz Voice Segments & Audit Pacing
```bash
/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python .agents/skills/vieneu-voice-pacing-mixer/scripts/synthesize_and_audit_pacing.py \
  --workspace "workspace/bilibili-anthropic-distill" \
  --voice "Hải Đăng" \
  --only-missing
```

### Step 3B: Assemble 48kHz Stereo Master Audio & Sign Stage 3 Gate
```bash
/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python .agents/skills/vieneu-voice-pacing-mixer/scripts/assemble_master_audio.py \
  --workspace "workspace/bilibili-anthropic-distill"
```

### Step 3C: Standalone Stage 3 CI/CD Gate Check
```bash
/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python .agents/skills/vieneu-voice-pacing-mixer/scripts/verify_stage_3.py \
  --workspace "workspace/bilibili-anthropic-distill"
```
Do NOT proceed to Stage 4 render until `.cicd/stage_3_gate.json` reports `Status: PASS | Score: 100.0/100`.
