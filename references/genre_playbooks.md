# Generalized Video Localization Playbooks by Genre & Format (`v3.0.0`)

This manual defines the `v3.0.0` parameter matrices, voice personas, pacing limits, and mixing defaults across supported video genres and aspect ratios. Every project runs in mandatory **`voice_dub`** mode (full Vietnamese dubbing + dual-corner watermark + external `.srt`).

---

## 1. Genre Configuration Matrix (`voice_dub` Everywhere)

| Parameter | `short-drama` (Chinese / English AI & Live-Action Dramas) | `tech-review` (Bilibili / Tech, AI, Hardware) | `documentary` (Finance, History, Deep Essays) | `tutorial` (Coding, Software UI Walkthroughs) |
| :--- | :--- | :--- | :--- | :--- |
| **Localization Mode (`--mode`)** | **`voice_dub`** (mandatory) | **`voice_dub`** (mandatory) | **`voice_dub`** (mandatory) | **`voice_dub`** (mandatory) |
| **Target Clause Duration** | `0.8s – 2.5s` (fast dialogue turns & phone calls) | `1.5s – 3.5s` | `2.0s – 4.0s` (measured cadence) | `1.2s – 3.0s` (locked to UI cues) |
| **Target Syllable Rate (`Stage 2`)** | `4.5 – 5.6 syl/s` (hard ceiling `6.8 syl/s`) | `4.5 – 5.2 syl/s` (hard ceiling `6.8`) | `4.0 – 5.0 syl/s` (hard ceiling `6.8`) | `4.5 – 5.4 syl/s` (hard ceiling `6.8`) |
| **Rubberband R3 Tempo Range (`Stage 3`)** | `base = 1.00x` (pure raw TTS when fits; `rubberband` only if `> 1.005x`), `max = 1.28x` (`1.32x` for `<= 2`-syl) | `base = 1.00x`, `max = 1.28x` | `base = 1.00x`, `max = 1.28x` | `base = 1.00x`, `max = 1.28x` |
| **Continuous Utterance Grouping** | Same speaker & voice, `gap <= 0.12s`, `<= 3` clauses, `span <= 4.5s` | Same speaker & voice, `gap <= 0.12s`, `<= 3` clauses, `span <= 4.5s` | Same speaker & voice, `gap <= 0.12s`, `<= 3` clauses, `span <= 4.5s` | Same speaker & voice, `gap <= 0.12s`, `<= 3` clauses, `span <= 4.5s` |
| **Default `VieNeu-TTS` Voice** | **`Trúc Ly`** (100% single house voice across all characters/cues) | **`Trúc Ly`** (100% single house voice) | **`Trúc Ly`** (100% single house voice) | **`Trúc Ly`** (100% single house voice) |
| **Vocal Carving (`config/audio_mix.json`)** | `base_vocal_db = -10.0`, `speech_vocal_dip_db = -16.0`, `floor_vocal_db = -24.0`, `snr_vocal_db = 14.0` | `base_vocal_db = -10.0`, `speech_vocal_dip_db = -16.0`, `floor_vocal_db = -24.0` | `base_vocal_db = -10.0`, `speech_vocal_dip_db = -16.0`, `floor_vocal_db = -24.0` | `base_vocal_db = -10.0`, `speech_vocal_dip_db = -16.0`, `floor_vocal_db = -24.0` |
| **BGM / SFX Ducking (`accomp.wav`)** | `snr_bgm_db = 6.0`, `floor_bgm_db = -9.0` | `snr_bgm_db = 6.0`, `floor_bgm_db = -9.0` | `snr_bgm_db = 6.0`, `floor_bgm_db = -9.0` | `snr_bgm_db = 8.0`, `floor_bgm_db = -12.0` |
| **External `.srt` Line Cap** | `<= 96` chars (prefer `<= 64` chars on portrait) | `<= 96` chars (`<= 2` balanced lines) | `<= 96` chars (`<= 2` balanced lines) | `<= 96` chars (`<= 2` compact lines) |

---

## 2. Aspect Ratio & Watermark Scaling (`Stage 4`)

Because `aicn v3.0.0` does not burn in subtitle pills, Stage 4 preserves the native source resolution (`-fps_mode passthrough -sn`) and adapts only the optional dual-corner watermark strip (`aicn.py render --brand-dir <dir>`):

### A. Landscape `16:9` (`1920×1080` or `1280×720`)
- At `width >= 1200`: scale `= 1.0` (`top_left_x = 42`, `top_left_y = 22`, `top_right_margin_x = 42`, `top_right_y = 58`).

### B. Portrait `9:16` (`1080×1920` or `720×1280`)
- Automatically scales watermark PNGs and margins proportionally for portrait widths `< 1200` so the top-left brand lockup and top-right credit badge stay crisp and non-overlapping inside the top safe band.

---

## 3. Handling Edge-Case Source Videos

### Case 1: Mixed Frame Rates, Stream Order Reversal, or VFR (`init_workspace.py`)
Mixed batches (e.g., Bilibili `video=0/audio=1` + Douyin `audio=0/video=1`, `25fps` + `30fps`, `HE-AACv2` + `AAC-LC`, `1/15360` + `1/16000` timescales) cause `Non-monotonic DTS` and ghost duration if concatenated raw.
- `init_workspace.py` checks `(width, height, codec_name, v_profile, v_index, v_time_base, r_fps, avg_fps, sample_rate, channels, a_codec, a_profile)` and normalizes mismatched episodes automatically with `-map 0:v:0 -map 0:a:0`, mode-fps CFR, `setpts=PTS-STARTPTS`, `-avoid_negative_ts make_zero`, and `-video_track_timescale 15360`, verifying summed episode duration against `source/master_source.mp4`.

### Case 2: Multi-Character Dramas with Single House Voice (`series_bible.json`)
When a drama has 2+ characters:
1. Assign distinct `speaker` IDs (`M1`, `F1`, `M2`, `SYSTEM`, `NARRATOR`) for pronoun consistency, and map **every** character's `voice_id` and **every** key in `voices` to **`"Trúc Ly"`** (`"default": "Trúc Ly"`) in `config/series_bible.json` (single-narrator thuyết minh house rule — never assign multiple TTS voices unless the user explicitly sets `"allow_multi_voice": true` in `series_config.json`).
2. Tag every segment in `data/02_bilingual_script.json` with `"speaker"` (do not set non-`"Trúc Ly"` per-cue `"voice"` overrides).
3. Stage 3 `group_lines()` never merges segments across `speaker` changes, preserving clean character turn boundaries even when all characters share voice `"Trúc Ly"`.

### Case 3: English Source Audio (`--source-lang en`)
When localizing English short dramas (e.g., from `dd_tool.py`), pass `--source-lang en` to `aicn.py init-series` (or `--source-lang en` to `run_asr_and_scan.py`) so Stage 1 Whisper transcribes English cleanly instead of defaulting to Simplified Chinese.
