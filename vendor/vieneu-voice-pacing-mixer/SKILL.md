---
name: vieneu-voice-pacing-mixer
description: Use when synthesizing Vietnamese narration with VieNeu-TTS, enforcing indivisible utterance pacing with rubberband R3, locking visual cue start timestamps, and mixing 48kHz voice with single-STFT vocal-carved original audio.
---

# VieNeu-TTS 48 kHz Indivisible Dubbing, Elastic Pacing & Single-STFT Mixer (`v3.0.0`)

## Overview

Stage 3 of the video localization pipeline (`voice_dub`-only, `AUDIO_VERSION = "aicn-audio-3.0"`), driven by `<aicn>/scripts/aicn_audio.py` and gated by `verify_stage_3.py`. Synthesizes continuous same-speaker Vietnamese utterances at `48000 Hz` (`PCM_16`) using `VieNeu-TTS v3 Turbo` (single-process CUDA `infer_batch(16)` under `ap.gpu_slot("vieneu", vram_mb=1024)` or ONNX `fp32` on macOS/CPU), stretches each intact utterance once via parallel `rubberband -3 -F -q` (`base_tempo = 1.08`, `max_tempo = 1.28`, `1.32` for `<= 2`-syl interjections) into elastic slack, fails fast before mixing on any overflow, and mixes with `media/stems/{vocals,accomp}.wav` through a single-STFT, C-speed DSP pipeline (`< 12s`/10-min unit) that carves Chinese speech bands cleanly without muting sound effects or music.

**REQUIRED BACKGROUND:** Complete Stage 2 (`zh-vi-script-adapter`) and verify `.cicd/stage_2_gate.json` == `PASS` first.

---

## Core Capabilities (`v3.0.0`)

### 1. Single-Process CUDA `infer_batch` & Global `tts_sent` Cache
- Always run via `$AICN_PY` (`C:\Work\Tools\VieNeu-TTS\.venv\Scripts\python.exe` on Windows, `/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python` on macOS).
- On NVIDIA CUDA, loads **1 in-process PyTorch `Vieneu` instance** inside `ap.gpu_slot("vieneu", vram_mb=1024)` with `infer_batch(batch_size=16)` grouped by `(voice, temperature, top_k)` (ONNX `fp32` on macOS/CPU).
- In warm batch mode (`aicn_audio.py batch-dub` / `batch-all --units ...`), loads VieNeu **ONCE** across all cache-miss utterances of all units in the batch.
- Caches raw 48 kHz `PCM_16` WAVs globally at `<global_cache>/tts_sent/<tts_sent_key>.wav` (`TTS_ENGINE_TAG = "vieneu-v3-turbo-48k"`, pure content hash — zero path dependence).

### 2. Indivisible Utterances (Zero Mid-Word Slicing)
Never merge pause-separated clauses and chop a synthesized WAV back into pieces!
- `group_lines()` merges adjacent clauses ONLY when ALL hold: same `speaker` & `voice`, previous clause is comma-chained (not `[.!?…]\s*$`), `gap <= 0.12s` (`group_max_gap_s = 0.12`), `< 3` clauses (`max_group_clauses = 3`), and total span `<= 4.5s` (`max_group_span_s = 4.5`).
- `clean()` trims `|w| > 0.006` with a **`5ms` guard**, runs the `-50 dBFS / -15 dB` noise-floor expander, and applies **`10ms` cosine edge fades** (zero excess silence pad).
- Each utterance is stretched ONCE intact via `rubberband -3 -F -q` (cached globally at `<global_cache>/rb_stretch/<rb_stretch_key>.wav`), placed ONCE intact onto `media/audio/dub_voice_48k.wav`, and saved intact at `media/tts_segments/utt_{gi:04d}.wav`.
- `data/03_aligned_subtitles.json` preserves all `N` rows for `data/subtitles_full.srt` with strictly monotonic `slot_start`/`slot_end` and links each row to `utterance_id`, `utterance_wav`, `utt_speech_start`, `utt_speech_end`, and `tempo_ratio`.

### 3. Elastic Pause Borrowing & Fail-Fast Overflow Guard
- Utterance `gi` starts at `start_u = max(segs[g[0]]["start"], prev_end + 0.03)` and borrows silent slack up to `avail = min(next_start - 0.05, segs[g[-1]]["end"] + 1.20) - start_u` (`lead_gap_s = 0.05`, `max_overhang_s = 1.20`).
- **Fail-Fast Before Mix:** If ANY utterance has `tempo > cap + 0.005` (`1.28` standard, `1.32` for `<= 2`-syl), `overflow_s > 0.05`, or `end_u > next_start - 0.03`, `cmd_dub` writes `data/03_dub_sentences.json` with offending IDs + trim guidance and exits with code **`2`** immediately before `mix` / `master` (unless `--allow-overflow` is passed for diagnostics).

### 4. Single-STFT, C-Speed DSP Mixer & Clean Vocal Carving (`cmd_mix`, `11.56s` / 10-min unit)
- Uses PyTorch Welch `spectral_profile_fast()`, anti-denormal `sosfilt` RBJ biquad EQ + 3:1 dub chain, in-RAM BS.1770-4 `lufs()` (`~0.6s`), C-speed chunked `follow_hold()` (`< 0.1s`), **ONE `torch.stft` (`scipy.fft` fallback) STFT per stem** (`T`, `V`, `B`), and **ONE combined linear `torch.istft`** `istft(ZV*GV + ZB*GB)` (`9.8×` faster than `v2.7.0`'s `113.8s`, measuring `dub_snr_speech_db_median = 17.4 dB`, `p10 = 16.48 dB` on 10-min benchmark).
- Carves original Chinese vocals cleanly (`base_vocal_db = -10.0`, `speech_vocal_dip_db = -16.0` on `300 Hz–5 kHz` when broadband `talk > 0.05`, `vocal_high_dip_db = -4.5` on `> 5 kHz`, clamped directly at **`floor_vocal_db = -24.0`** total floor without stacking; `snr_vocal_db = 14.0`, `snr_bgm_db = 6.0`, `floor_bgm_db = -9.0`, `attack_s = 0.015`, `hold_s = 0.30`, `release_s = 0.38`).
- Vocal carving engages early down to `-30 dB` (`p95 * 1e-3`), while `dub_snr_speech_db_median` and `p10` are measured strictly on `-15 dB` active voiced-syllable frames (`snr_on = pt_speech > max(p95_sp * 10**(-15/10), 1e-10)` — live VieNeu CUDA E2E verified at `median = 22.03 dB`, `p10 = 12.72 dB`, `orig_vocal_atten = -17.35 dB`).

### 5. Native 48 kHz Fast Master & Gate Checks (`cmd_master`, `cmd_verify`, `verify_stage_3.py`)
- `cmd_master` (`master_mode="fast"`): 2:1 glue compressor + native 48 kHz `ebur128` linear gain + `alimiter` → `media/master_audio_48k.wav` (48 kHz, 24-bit stereo).
- `cmd_verify` validates 7 checks in `data/03_mix_report.json` (`loudness` `-14.0 ± 1.0 LUFS` & peak `<= -0.8 dBFS`, `stems_match_source`, `dub_intelligible` active `median >= 10.0 dB` & `p10 >= 4.0 dB`, `original_never_muted`, `original_present >= -6.0 dB`, `dub_fits_and_intact` zero overflow/overlap/runt `< 0.12s`, `dub_sync` `speech_start - script_start` in `[-0.10, +0.50]s`), writes `03_pacing_and_audio_report.md`, and runs `verify_stage_3.py` (`100/100`: `audio_gate_report_passed` 20 pts, `utterance_wavs_intact_and_non_runt` 20 pts, `utterance_sync_and_zero_overlaps` 30 pts, `master_audio_48k_stereo_and_srt_valid` 30 pts).

See [audio_dsp_and_pacing_manual.md](./references/audio_dsp_and_pacing_manual.md) and `<aicn>/references/audio_mix.md`.

---

## Execution Commands

### Single Unit (All Steps)
```bash
$AICN_PY <aicn>/scripts/aicn_audio.py all --unit "workspace/<series>/units/<unit>" [--force]
# Or individual steps: separate | dub | analyze | mix | master | verify
```

### Warm Multi-Unit Batch (Recommended for Series)
```bash
$AICN_PY <aicn>/scripts/aicn_audio.py batch-all --units "workspace/<series>/units/unit_01,workspace/<series>/units/unit_02"
# Or via orchestrator: aicn.py run-series --hub workspace/<series> --phase produce --units unit_01,unit_02
```

### Standalone Gate Check
```bash
$AICN_PY <aicn>/vendor/vieneu-voice-pacing-mixer/scripts/verify_stage_3.py \
  --workspace "workspace/<series>/units/<unit>"
```
Do NOT proceed to Stage 4 render until `.cicd/stage_3_gate.json` prints `Status: PASS | Score: 100.0/100`.
