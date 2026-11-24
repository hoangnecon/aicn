# Stage 3 audio: dub, mix, master

Engine: `$AICN_PY <skill>/scripts/aicn_audio.py <step> --unit U` where `<step>` is
`separate | dub | analyze | mix | master | verify | all`. Parameters live in `config/audio_mix.json`;
anything missing falls back to `DEFAULTS` in the script.

## Goal

The dub sits on top of the original film. The original voice, music and effects stay in
the mix all the time. Before mixing, `analyze` measures the 9-band spectral balance and
RMS/crest profile of every stem (`data/03_spectral_analysis.json`) and sets clean
High-Pass / Low-Pass conditioning so sub-rumble, chest thump, and harsh vocal sibilants
never collide while foley/SFX (`accomp.wav`) stays full-bodied and natural. During mixing,
the original voice stem is tucked smoothly (`base_vocal_db` `-6.5` dB, `12` dB SNR) with
hangover hold (`0.35` s) and slower release (`0.42` s) to prevent inter-clause volume pumping.

## Steps

| Step | Does | Output | Cache |
|---|---|---|---|
| separate | Demucs `htdemucs` (`demucs-mlx` on macOS Metal, PyTorch `demucs` CUDA/CPU on Windows/Linux) splits the source into vocals and accompaniment (drums + bass + other) | `media/stems/{vocals,accomp}.wav`, `stems.json` | source sha256 + model |
| dub | groups comma-chained clauses of one speaker into sentences, auto-scaled CPU `fp32` VieNeu TTS (`tts_workers=0` auto), cleans the hiss floor, time-stretches in parallel with rubberband R3 (formant preserved), places each sentence at its first clause, cuts it back into clauses | `media/audio/dub_voice_48k.wav`, `media/tts_segments/seg_NNNN.wav`, `data/03_aligned_subtitles.json`, `data/03_dub_sentences.json`, SRT | hub `.cache/tts_sent/` + per-clause `.cache/rb_clause/` + step `.cache/audio_state.json` (`--force` bypasses) |
| analyze | active-frame 9-band spectral energy, centroid, crest factor & adaptive HP/LP cutoffs across `vocals.wav`, `accomp.wav`, and `dub_voice_48k.wav` (auto-runs inside `mix`; respects user overrides in `config/audio_mix.json`) | `data/03_spectral_analysis.json` | none |
| mix | HP/LP conditioning + RBJ biquad voice chain on dub, piped zero-disk LUFS match, attack-hold-release (`follow_hold`) 12-band STFT/iSTFT ducking + dynamic `>5 kHz` vocal sibilance softening | `media/audio/premaster_48k.wav`, `data/03_mix_stats.json`, `data/03_spectral_analysis.json` | step `.cache/audio_state.json` (stems + dub + mix config hash) |
| master | glue compressor 2:1 + native 48 kHz EBU R128 linear gain + lookahead limiter (`master_mode="fast"`, ≈ 4.1 s vs 115 s legacy 192 kHz `loudnorm`) | `media/master_audio_48k.wav` (48 kHz, 24-bit) | step `.cache/audio_state.json` (premaster + master config hash) |
| verify | audio gate + writes `03_pacing_and_audio_report.md`, then `verify_stage_3.py` signs Stage 3 | `data/03_mix_report.json`, `03_pacing_and_audio_report.md`, `.cicd/stage_3_gate.json` | n/a |

## Why sentences, not clauses

Stage 2 cuts lines at commas to follow the Chinese hardsub. When VieNeu reads a 2–4
syllable fragment alone, it often gets the tone wrong. For example, "Viện trưởng," alone
was heard as "Viện trường"; inside the full sentence it was read correctly. The dub
therefore reads each sentence once. The display still follows the clauses, because
subtitles take their timing from the hardsub, not from the audio.

## Why rubberband, not atempo

`atempo` (ffmpeg WSOLA) roughly doubled the Whisper word error rate on the lines it
stretched (0.137 → 0.304), and it adds a rough, phasey sound. Rubberband R3 with formant
preservation (`rubberband -3 -F`) keeps the timbre.

VieNeu v3 Turbo reads at about 5.0–5.5 syllables per second and has no speed parameter.
`base_tempo` 1.10 brings it to about 5.7. Each sentence is sped up further only when it
does not fit before the next sentence (`max_tempo` 1.25). A sentence that still does not
fit is reported as an `OVERFLOW`, and the gate fails. Fix it by shortening `tts_text`
in Stage 2.

## Hiss

At the default sampling settings the TTS output has a continuous noise floor around
-80 dBFS between 6 and 12 kHz. After the dub is raised by about 8 dB to match the film,
that floor becomes audible. An expander lowers it by 15 dB wherever the 10 ms envelope
is below -50 dBFS.

## Mix algorithm

- Spectral pre-pass (`analyze`): measures active-frame 9 frequency bands (`0–80`, `80–150`, `150–300`, `300–500`, `500–2k`, `2k–5k`, `5k–8k`, `8k–16k`, `16k–24k` Hz), spectral centroid, and crest factor; computes adaptive High-Pass / Low-Pass cutoffs while strictly preserving any explicit overrides in `config/audio_mix.json`:
  - Dub (`dub_hp_hz = 90` Hz, `dub_lp_hz = 16000` Hz),
  - Original vocal stem (`vocal_hp_hz = 95` Hz, `vocal_lp_hz = 0` off so non-dubbed singing/laughter/air stays natural; dynamic `vocal_high_dip_db = -3.5` dB on `>5 kHz` applies ONLY while the dub speaks),
  - Original SFX/Music stem (`bgm_hp_hz = 35` Hz, `bgm_lp_hz = 18500` Hz — subsonic DC safety only, keeps impacts & ambience intact).
- STFT: 2048 points, hop 512 (about 10.7 ms), 12 bands with edges at 150, 300, 500, 800, 1.2k, 1.8k, 2.6k, 3.6k, 5k, 7k and 10 kHz.
- Dub chain: HP/LP + true RBJ biquad EQ (zero comb-filtering: optional `380 Hz` `-1.5 dB` mud trim if elevated, `+2.5 dB` peaking presence at `3.1 kHz`, `-2.0 dB` high-shelf at `8 kHz`) + 3:1 compression above -20 dBFS, then LUFS matched to original vocal stem + `dub_over_vocal_lu` (`+1.5` LU).
- For each band and frame where the dub is active, each original stem gets the gain that puts the dub `snr_*` dB above it (`snr_vocal_db = 12`, `snr_bgm_db = 6`), clipped to `floor_*` (`floor_vocal_db = -20`, `floor_bgm_db = -9`), so original sound effects are never muted.
- The original voice also gets a broadband dip of `base_vocal_db` (`-6.5` dB) plus dynamic `>5 kHz` sibilance softening (`vocal_high_dip_db = -3.5` dB) while the dub talks.
- Speech bands (500 Hz–5 kHz) get full treatment; low and very high bands keep weight `0.3–0.8`.
- **Anti-pumping Attack-Hold-Release (`follow_hold`):** applies `hold_s = 0.35` s flat hold directly on the gain envelope (`attack 20 ms`, `hold 350 ms`, `release 420 ms`) — never forcing `active=True` where `PT=0` — so levels hold smoothly across inter-clause pauses with zero floor-slam and zero pumping.

## Gate (`verify`)

| Check | Rule |
|---|---|
| loudness | integrated -14 ± 1 LUFS, peak ≤ -0.7 dBFS |
| stems_match_source | stems were built from the current `master_source.mp4` |
| dub_intelligible | median dub-to-original ratio at 500 Hz–5 kHz while dubbing ≥ 8 dB |
| original_never_muted | deepest cut is within 6 dB of the configured floors |
| original_present | where nobody dubs, master energy ≥ original energy - 6 dB (catches a silent-BGM fallback) |
| dub_fits | every sentence fits at ≤ `max_tempo`, no overflow |

After that, `verify_stage_3.py` checks per-line WAVs, slot drift, tempo ≤ 1.265 and the
master format, then signs Stage 3.

## Tuning

| Want | Change |
|---|---|
| Original voice louder under the dub | `base_vocal_db` -4 → -2, `floor_vocal_db` -18 → -14 |
| Dub clearer | `snr_vocal_db` 10 → 12, or `dub_over_vocal_lu` 1 → 2 |
| Music louder under the dub | `floor_bgm_db` -9 → -6, `snr_bgm_db` 6 → 4 |
| Faster dub | `base_tempo` 1.10 → 1.15 (keep ≤ 1.2 for natural speech) |
| Faster CPU synthesis | `tts_workers=2, tts_threads=4` (default, measured +23% faster on 8-core M1); `tts_workers=4, tts_threads=2` (+26%, 4× RAM) |

Changing any mix or master parameter re-runs `mix` and `master` only (about 25–30 s total),
then Stage 4 renders the new audio. Changing `voice`,
`temperature` or `top_k` re-synthesizes every sentence.
