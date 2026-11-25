# Audio DSP, Indivisible Pacing & Single-STFT Mixing Manual (`v3.0.0`)

This manual specifies the acoustic signal-processing rules, indivisible utterance alignment, single-STFT spectral ducking math, and quality gates for Stage 3 (`scripts/aicn_audio.py`, `AUDIO_VERSION = "aicn-audio-3.0"`).

---

## 1. `VieNeu-TTS v3 Turbo` Runtime & Voice Persona Guide

### Runtime Environment
- **Interpreter (`$AICN_PY`):** Windows `C:\Work\Tools\VieNeu-TTS\.venv\Scripts\python.exe` | macOS `/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python`.
- **NVIDIA CUDA (Primary on Windows/Linux):** Single in-process PyTorch `Vieneu` instance under `ap.gpu_slot("vieneu", vram_mb=1024)` using `infer_batch(batch_size=16)` grouped by `(voice, temperature, top_k)`. Measured on RTX 2060 6 GB: `4.23s` for 16 sentences (`RTF 0.127`, `3.78 sent/s`, peak VRAM `498 MB`).
- **macOS / CPU Fallback:** ONNX `fp32` `ProcessPoolExecutor` (`AICN_TTS_DEVICE=cpu` forces CPU path).
- **Warm Multi-Unit Batching:** `aicn_audio.py batch-dub` and `batch-all` keep one `Vieneu` model resident in VRAM across all missing utterances of multiple units.
- **Native Sample Rate & Cache Format:** `48000 Hz` mono `PCM_16` cached globally at `<global_cache>/tts_sent/<tts_sent_key>.wav` (`TTS_ENGINE_TAG = "vieneu-v3-turbo-48k"`).

### Preset Voice Selection
| Voice Name | Gender / Timbre | Best Used For |
| :--- | :--- | :--- |
| `Trúc Ly` | Balanced default (`DEFAULTS["voice"]`) | General short-drama narration & default fallback |
| `Hải Đăng` | Male, crisp, analytical, modern Northern cadence | Tech reviews, AI essays, tutorials, male leads |
| `Bình` | Male, warm, steady narrative gravitas | Documentaries, male authority / narrator roles |
| `Ngọc` | Female, clear, expressive natural warmth | Female drama leads, conversational explainers |
| `Tuyên` | Male, authoritative news/commentary projection | News briefings, formal announcements |

---

## 2. Indivisible Utterances & Plosive-Safe Cleaning (`cmd_dub`)

In `v3.0.0`, synthesized multi-clause WAVs are **never sliced back into clause pieces**:
- **Continuous-Speech Grouping (`group_lines`):** Adjacent segments merge ONLY when ALL hold: same `speaker` & `voice`, previous segment ends with comma/continuation punctuation (not `[.!?…]\s*$`), inter-segment silence `gap = s[i].start - s[i-1].end <= 0.12s`, `<= 3` clauses, and total group span `<= 4.5s`.
- **Clean Silence Trimming (`clean()`):**
  - Trim leading/trailing silence where `|w| > 0.006` with a **`5ms` guard** (zero excess silence pad).
  - Apply noise-floor expander (`expander_thresh_db = -50.0`, `expander_depth_db = -15.0`) to eliminate `-80 dBFS` TTS hiss between phrases.
  - Apply **`10ms` cosine fade-in / fade-out** at utterance edges to prevent DC clicks while preserving crisp initial plosives and final stops.

---

## 3. Elastic Window, `rubberband` R3 & Fail-Fast Overflow Check

- **Never Shift Timeline Causing Cumulative Drift:** Utterance `gi` starts at:
  $$\text{start}_u = \max\left(\text{segs}[g[0]].\text{start},\; \text{prev\_end} + 0.03\right)$$
- **Elastic Silent-Slack Borrowing:**
  $$\text{avail} = \min\left(\text{next\_utterance\_start} - 0.05,\; \text{segs}[g[-1]].\text{end} + 1.20\right) - \text{start}_u$$
  (`lead_gap_s = 0.05`, `max_overhang_s = 1.20`).
- **Formant-Preserved Time-Stretch (`rubberband -3 -F -q`):**
  - `base_tempo = 1.08`; if `raw_dur / base_tempo > avail`, compute `tempo = raw_dur / max(0.15, avail)`.
  - Caps: `max_tempo = 1.28` (`max_tempo_short = 1.32` for `<= 2`-syllable interjections).
  - Stretched intact WAVs are cached globally at `<global_cache>/rb_stretch/<rb_stretch_key>.wav` (`RB_STRETCH_VERSION = "rb3-f-3.0"`) via parallel `ThreadPoolExecutor`, placed **once intact** onto `dub_voice_48k.wav`, and written intact to `media/tts_segments/utt_{gi:04d}.wav`.
- **Fail-Fast Exit `2` Before `cmd_mix`:** If ANY utterance has `tempo > cap + 0.005`, `overflow_s > 0.05`, or `end_u > next_start - 0.03`, `cmd_dub` writes `data/03_dub_sentences.json` with exact offending IDs and trim advice and exits with code `2` immediately (unless `--allow-overflow` is explicitly passed).

---

## 4. Single-STFT DSP, C-Speed `follow_hold()` & Clean Vocal Carving (`cmd_mix`)

1. **Fast Conditioning & Active-Block Analysis:**
   - Applies 48 kHz HP/LP filters (`dub_hp_hz = 90`, `dub_lp_hz = 16000`, `vocal_hp_hz = 95`, `vocal_lp_hz = 0`, `bgm_hp_hz = 35`, `bgm_lp_hz = 18500`), anti-denormal `sosfilt` RBJ biquad EQ + 3:1 dub compression, and PyTorch Welch `spectral_profile_fast()`.
   - Matches dub loudness to `vocals + dub_over_vocal_lu` (`+1.5 LU`) using fast in-RAM BS.1770-4 `lufs()` (`~0.6s`).
2. **Single STFT Per Stem + C-Speed `follow_hold()` (`< 0.1s`):**
   - Computes `complex64` STFT (`n_fft = 2048`, `hop = 512`) **once** per stem (`T`, `V`, `B`) via PyTorch CPU `torch.stft` (`scipy.fft` fallback).
   - Runs C-speed chunked `follow_hold()` (causal `scipy.ndimage.minimum_filter1d` flat hold + causal exponential smoothing with `attack_s = 0.015`, `hold_s = 0.30`, `release_s = 0.38`).
3. **Clean Vocal Carving (Non-Stacked Total Floor Clamp):**
   - Broadband vocal dip while dub talks (`talk > 0.05`): `base_vocal_db = -10.0`.
   - Active speech-band vocal dip (`300 Hz–5 kHz`, `talk > 0.05`): `speech_vocal_dip_db = -16.0` (prevents Chinese speech pop-back between Vietnamese syllables).
   - High-frequency sibilance softening (`> 5 kHz`): `vocal_high_dip_db = -4.5`.
   - Per-band SNR targets: `snr_vocal_db = 14.0`, `snr_bgm_db = 6.0`.
   - **Direct Floor Clamp:** Clamps `gV_raw` directly against `floor_vocal_db = -24.0` (never stacking `-10 - 16 - 24 = -50 dB`!), and `gB` against `floor_bgm_db = -9.0`.
4. **Single Combined Linear `torch.istft` & Active-Frame SNR:**
   - Computes `Y_orig_ducked = istft(ZV * GV + ZB * GB)` once (measured `11.56s` total `cmd_mix` on 10-min `600s` 48 kHz stereo — `9.8×` faster than `113.8s` in `v2.7.0`).
   - Vocal carving engages early down to `-30 dB` (`p95 * 1e-3`), while `dub_snr_speech_db_median` (`>= 10.0 dB`; `17.4 dB` 10-min synthetic, `22.03 dB` live VieNeu CUDA) and `dub_snr_speech_db_p10` (`>= 4.0 dB`; `16.48 dB` synthetic, `12.72 dB` live VieNeu CUDA) are evaluated strictly on `-15 dB` active voiced-syllable frames (`snr_on = pt_speech > max(p95_sp * 10**(-15/10), 1e-10)`).

---

## 5. Fast Native 48 kHz Mastering & Verification (`cmd_master`, `cmd_verify`)

- `cmd_master` (`master_mode = "fast"`): `acompressor` (2:1 glue) + `ebur128` linear gain + lookahead `alimiter` → `media/master_audio_48k.wav` (48 kHz, 24-bit stereo, integrated `-14.0 ± 1.0 LUFS`, peak `<= -0.8 dBFS`).
- `cmd_verify` + `verify_stage_3.py` enforce all 7 audio continuity, SNR, loudness, and sync checks (`0` overlaps, `0` `< 0.12s` runts, utterance `speech_start - script_start` in `[-0.10s, +0.50s]`).
