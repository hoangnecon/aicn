# Stage 3 audio: dub, mix, master

Engine: `$AICN_PY <skill>/scripts/aicn_audio.py <step> --unit U` where `<step>` is
`separate | dub | mix | master | verify | all`. Parameters live in `config/audio_mix.json`;
anything missing falls back to `DEFAULTS` in the script.

## Goal

The dub sits on top of the original film. The original voice, music and effects stay in
the mix all the time. They are turned down only in the frequency bands the dub needs, only
while the dub is speaking, and only as far as needed for the dub to be clearly heard.

## Steps

| Step | Does | Output | Cache |
|---|---|---|---|
| separate | Demucs htdemucs (MLX) splits the source into vocals and accompaniment (drums + bass + other) | `media/stems/{vocals,accomp}.wav`, `stems.json` | source sha256 + model |
| dub | groups comma-chained clauses of one speaker into sentences, TTS per sentence, cleans the hiss floor, time-stretches with rubberband R3 (formant preserved), places each sentence at its first clause, cuts it back into clauses | `media/audio/dub_voice_48k.wav`, `media/tts_segments/seg_NNNN.wav`, `data/03_aligned_subtitles.json`, `data/03_dub_sentences.json`, SRT | `.cache/tts_sent/<sha(text, voice, engine, temperature, top_k)>.wav` |
| mix | voice chain on the dub, level match, per-band per-frame ducking of the original stems | `media/audio/premaster_48k.wav`, `data/03_mix_stats.json` | none (≈ 1 min per unit) |
| master | glue compressor 2:1, 2-pass loudnorm | `media/master_audio_48k.wav` (48 kHz, 24-bit) | none |
| verify | audio gate, then `verify_stage_3.py` signs Stage 3 | `data/03_mix_report.json`, `.cicd/stage_3_gate.json` | n/a |

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

- STFT: 2048 points, hop 512 (about 10.7 ms), 12 bands with edges at 150, 300, 500, 800, 1.2k, 1.8k, 2.6k, 3.6k, 5k, 7k and 10 kHz.
- Dub chain: high-pass at 90 Hz, +2.5 dB presence at 2.2–4.5 kHz, -2 dB above 8 kHz, 3:1 compression above -20 dBFS. The dub level is then matched to the original vocal stem LUFS + `dub_over_vocal_lu`.
- For each band and frame where the dub is active, each original stem gets the gain that puts the dub `snr_*` dB above it. The gain is clipped to `floor_*`, so the original is never muted.
- The original voice also gets a broadband dip of `base_vocal_db` while the dub talks.
- Speech bands (500 Hz–5 kHz) get the full gain. Low and very high bands are relaxed (weight 0.3–0.8), which keeps the body of the original and the air in the room.
- Gains follow an envelope: 20 ms down, 250 ms back up, so the original returns smoothly between sentences.

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

Changing any mix or master parameter re-runs `mix` and `master` only (about 2 minutes),
then Stage 4 muxes the new audio without re-rendering any chunk. Changing `voice`,
`temperature` or `top_k` re-synthesizes every sentence.
