# Stage 3 Audio: Indivisible Dubbing, Single-STFT Mix & Master (`aicn-audio-3.0`)

Engine: `$AICN_PY <skill>/scripts/aicn_audio.py <step> --unit U` (single unit: `separate | dub | analyze | mix | master | verify | all`) or warm multi-unit batch CLIs (`batch-separate | batch-dub | batch-all --units u1,u2,... [--force] [--allow-overflow]`). Parameters live in `config/audio_mix.json`; anything missing falls back to `DEFAULTS` in `scripts/aicn_audio.py`. Only **`voice_dub`** mode is supported.

## 1. Goal

The Vietnamese dub sits cleanly above the original film while keeping original sound effects, music (`accomp.wav`), and muted background ambience alive. Chinese speech (`vocals.wav`) is actively carved in speech bands (`300 Hz–5 kHz`) whenever the Vietnamese dub speaks so two voices never clash, while non-dubbed crying, laughter, and SFX pass through naturally.

## 2. Pipeline Steps (`aicn-audio-3.0`)

| Step | Does | Output | Pure Content Cache |
|---|---|---|---|
| `separate` / `batch-separate` | Demucs `htdemucs` (`torch-cuda` under `gpu_slot("demucs", 1024 MB)` or `demucs-mlx` on macOS) splits source into 48 kHz `PCM_16` stems; `batch-separate` keeps `htdemucs` warm in VRAM across units | `media/stems/{vocals,accomp}.wav`, `stems.json` | `<global_cache>/stems/<stems_key>/` (`STEMS_VERSION="aicn-audio-1.0"`) |
| `dub` / `batch-dub` | Groups continuous same-speaker comma clauses (`gap <= 0.12s`, `<= 3` clauses, `span <= 4.5s`), synthesizes intact utterances via single-process CUDA `infer_batch(16)` (`gpu_slot("vieneu", 1024 MB)`) or ONNX CPU, trims (`5ms` guard) + expander + `10ms` cosine edge fade, stretches ONCE intact via parallel `rubberband` R3 into elastic slack (`next_start - 0.05s`, `+1.20s` cap), places ONCE intact on timeline; **fails fast (exit 2) before `mix` on overflow** | `media/audio/dub_voice_48k.wav`, `media/tts_segments/utt_NNNN.wav`, `data/03_aligned_subtitles.json`, `data/03_dub_sentences.json`, `data/subtitles_full.srt` | `<global_cache>/tts_sent/<tts_sent_key>.wav` + `rb_stretch/<rb_stretch_key>.wav` (`PCM_16`) |
| `analyze` | Strided active-block Welch `spectral_profile_fast()` (`< 0.15s`) + adaptive HP/LP cutoffs (respecting `config/audio_mix.json` overrides) | `data/03_spectral_analysis.json` | n/a |
| `mix` | Single 48 kHz HP/LP + anti-denormal `sosfilt` RBJ biquad voice chain, in-RAM BS.1770-4 `lufs()` gain matching, **1 `torch.stft` (`scipy.fft` fallback) STFT per stem (`T`, `V`, `B`)**, C-speed chunked `follow_hold()`, non-stacked vocal carving (`-24 dB` floor), **1 combined linear `torch.istft`** `istft(ZV*GV + ZB*GB)` (measured `11.56s` / 10-min unit) | `media/audio/premaster_48k.wav`, `data/03_mix_stats.json`, `data/03_spectral_analysis.json` | Unit `.cache/audio_state.json` |
| `master` | Glue compressor 2:1 + native 48 kHz `ebur128` linear gain + lookahead `alimiter` (`master_mode="fast"`, ≈ 4 s) | `media/master_audio_48k.wav` (48 kHz, 24-bit stereo) | Unit `.cache/audio_state.json` |
| `verify` | Evaluates 7 Stage 3 checks (`data/03_mix_report.json`), writes `03_pacing_and_audio_report.md`, invokes `verify_stage_3.py` (100/100) | `data/03_mix_report.json`, `03_pacing_and_audio_report.md`, `.cicd/stage_3_gate.json` | n/a |

## 3. Why Indivisible Utterances (Zero Mid-Word Chopping)

Splitting a synthesized multi-clause WAV back into individual clauses caused mid-word vowel chops, tiny `<150ms` glitch pieces, and artificial mid-sentence gaps whenever speakers paused differently in Vietnamese vs Chinese. In `v3.0.0`:
- `group_lines()` merges adjacent segments ONLY when ALL hold:
  1. Same `speaker` and `voice`,
  2. Previous segment ends with continuation punctuation (not `[.!?…]\s*$`),
  3. Inter-segment silence `gap = s[i].start - s[i-1].end <= 0.12s` (`group_max_gap_s = 0.12`),
  4. Group has `< 3` clauses (`max_group_clauses = 3`) and total span `<= 4.5s` (`max_group_span_s = 4.5`).
- Each group/utterance `gi` is synthesized **once**, cleaned (`|w| > 0.006` with `5ms` guard + expander + `10ms` cosine edge fade), stretched **once** intact with `rubberband -3 -F -q` (`RB_STRETCH_VERSION = "rb3-f-3.0"`), and placed **once** intact onto `dub_voice_48k.wav` and saved as `media/tts_segments/utt_{gi:04d}.wav`.
- `03_aligned_subtitles.json` retains all `N` rows for `subtitles_full.srt` with strictly monotonic `slot_start`/`slot_end` AND links each row to `utterance_id`, `utterance_wav`, `utt_speech_start`, `utt_speech_end`, and `tempo_ratio`.

## 4. Elastic Pacing, Rubberband R3 & Fail-Fast Overflow

- Always use **`rubberband -3 -F -q`** (formant-preserved R3). Never use FFmpeg audio tempo filters.
- `base_tempo = 1.08`, `max_tempo = 1.28`, `max_tempo_short = 1.32` (only for `<= 2`-syllable interjections).
- Elastic window for utterance `gi`:
  - `start_u = max(segs[g[0]]["start"], prev_end + 0.03)` (first utterance: `segs[g[0]]["start"]`)
  - `avail = min(next_start - 0.05, segs[g[-1]]["end"] + 1.20) - start_u` (`lead_gap_s = 0.05`, `max_overhang_s = 1.20`)
- **Fail-Fast Before `mix`:** If any utterance requires `tempo > cap + 0.005`, `overflow_s > 0.05`, or `end_u > next_start - 0.03`, `cmd_dub` writes `data/03_dub_sentences.json` with exact offending IDs and syllable-trim advice and **exits with code `2` immediately** (unless `--allow-overflow` is explicitly passed for diagnostics).

## 5. Single-STFT DSP & Clean Vocal Carving (`v3.0.0`)

- **TTS Noise Expander:** Removes the ~`-80 dBFS` VieNeu silence floor (`expander_thresh_db = -50.0`, `expander_depth_db = -15.0`).
- **Dub Chain:** HP/LP (`dub_hp_hz = 90`, `dub_lp_hz = 16000`) + vectorized RBJ biquad EQ (`380 Hz` `-1.5 dB` optional mud trim, `+2.5 dB` presence at `3.1 kHz`, `-2.0 dB` shelf at `8 kHz`) + 3:1 compression above `-20 dBFS`, LUFS matched in-RAM to `vocals` + `dub_over_vocal_lu = +1.5 LU`.
- **STFT Parameters:** `n_fft = 2048`, `hop = 512` (~10.7 ms at 48 kHz), 12 bands (`150, 300, 500, 800, 1.2k, 1.8k, 2.6k, 3.6k, 5k, 7k, 10k` Hz). Computed **once** per stem (`T`, `V`, `B`) via PyTorch CPU `torch.stft` (`scipy.fft` fallback), alongside anti-denormal `sosfilt` (preventing x86_64 subnormal slowdowns on silence).
- **C-Speed `follow_hold()` (`< 0.1s`):** Vectorized chunked causal `scipy.ndimage.minimum_filter1d` flat hold + causal exponential attack/release smoothing (`attack_s = 0.015`, `hold_s = 0.30`, `release_s = 0.38`).
- **Clean Vocal Carving (Non-Stacked Floor):**
  - `base_vocal_db = -10.0` broadband attenuation while dub talks (`talk > 0.05`).
  - `speech_vocal_dip_db = -16.0` across `300 Hz–5 kHz` whenever broadband `talk > 0.05` (prevents Chinese vocal pop-back between syllables).
  - `vocal_high_dip_db = -4.5` on `> 5 kHz` sibilance while dub speaks.
  - `gV_raw` is clamped directly at **`floor_vocal_db = -24.0`** total floor (never stacked to `-50 dB`!), with `snr_vocal_db = 14.0`, `snr_bgm_db = 6.0`, `floor_bgm_db = -9.0`.
- **Single Combined Linear iSTFT:** Mixes `Y_orig_ducked = istft(ZV * GV + ZB * GB)` once and adds the conditioned dub track.
- **Active-Speech SNR Telemetry:** Vocal carving engages down to `-30 dB` (`PT[SPEECH].sum(0) > p95 * 1e-3`), while `dub_snr_speech_db_median` and `dub_snr_speech_db_p10` are measured strictly on active voiced syllable frames within `-15 dB` of `p95` (`snr_on = pt_speech > max(p95_sp * 10**(-15/10), 1e-10)`, excluding `-25..-30 dB` plosive closures, `10ms` fade edges, and hold/release silent tails).

## 6. Gate Checks (`cmd_verify` + `verify_stage_3.py`)

| Check (`03_mix_report.json`) | Pass Rule |
|---|---|
| `loudness` | Integrated `-14.0 ± 1.0 LUFS`, true peak `<= -0.8 dBFS` |
| `stems_match_source` | `media/stems/stems.json` key matches current `ap.stems_key(src_sha, demucs_model, STEMS_VERSION)` |
| `dub_intelligible` | Active-speech frames `dub_snr_speech_db_median >= 10.0 dB` AND `dub_snr_speech_db_p10 >= 4.0 dB` |
| `original_never_muted` | Deepest attenuation stays within `6.0 dB` of `floor_vocal_db` (`-24.0`) and `floor_bgm_db` (`-9.0`) |
| `original_present` | Non-dubbed passages preserve original energy (`>= -6.0 dB` vs source) |
| `dub_fits_and_intact` | `0` overflows, `0` tempo violations (`<= 1.28` / `<= 1.32`), `0` runt (`< 0.12s`) or silent utterances, `0` utterance overlaps (`speech_end[g] <= speech_start[g+1] - 0.02`), 1:1 intact waveform placement |
| `dub_sync` | Every utterance `speech_start - script_start` in `[-0.10s, +0.50s]`, `0` subtitle slot overlaps |

`verify_stage_3.py` validates Stage 2 gate, `03_mix_report.json` all PASS, intact `media/tts_segments/utt_NNNN.wav` files (`>= 0.12s`, non-silent), zero utterance overlaps, sync drift bounds, `media/master_audio_48k.wav` (48 kHz stereo), `data/subtitles_full.srt` (`N` blocks), and `03_pacing_and_audio_report.md`.

## 7. Tuning (`config/audio_mix.json`)

| Goal | Parameter Change |
|---|---|
| Original voice slightly louder under dub | `base_vocal_db`: `-10.0` → `-7.0`, `floor_vocal_db`: `-24.0` → `-18.0` |
| Stronger dub clarity over loud original voice | `snr_vocal_db`: `14.0` → `16.0` or `dub_over_vocal_lu`: `1.5` → `2.5` |
| Music/SFX slightly louder under dub | `floor_bgm_db`: `-9.0` → `-6.0`, `snr_bgm_db`: `6.0` → `4.0` |
| Slightly brisker baseline cadence | `base_tempo`: `1.08` → `1.12` (keep natural) |
| Adjust CUDA static TTS batch size | `tts_batch_size`: `16` (default; lower to `8` on `< 4 GB` VRAM) |

Changing `mix` or `master` parameters only re-runs `mix` (`< 12s`) + `master` (`~4s`) + Stage 4 `render`. Changing `voice`, `temperature`, or `top_k` re-synthesizes utterances (keyed by `tts_sent_key`).
