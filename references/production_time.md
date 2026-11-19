# Production time

Reference unit: unit_07_ep61_70. It has 10 episodes, 609.5 s, 1080p30, 186 dubbed lines, and was run in voice_dub mode on Apple Silicon.
**M** marks a time measured on 2026-11-19. **E** marks an estimate: work done by the agent or a person, so the time varies with the series.

## One new unit (10 episodes, ≈ 10 min of video), from download to release

| Step | Time | | Notes |
|---|---|---|---|
| Download 10 episodes (`ohg download`) | 1–3 min | E | network bound |
| Stage 0 init + adopt | < 30 s | M | |
| Stage 1 Whisper (2 passes) + 5 fps scan + waveform audit | ≈ 1.5 min | M/E | scan 24 s; one Whisper pass 48 s on MLX (194 s on CPU) |
| Stage 1 audit-frame review (agent views keyframes, fixes homophones) | 5–10 min | E | |
| Stage 2 translate + adapt 180–240 lines, validate | 20–40 min | E | the largest variable; glossary reuse makes later units faster |
| Stage 3 Demucs stems | 1.2 min | M | 74 s for 609 s |
| Stage 3 sentence-level TTS (186 lines → 107 sentences) + rubberband | 4 min | M | 240 s |
| Stage 3 frequency-aware mix + master + gate | 1.5–2 min | M | mix 79 s |
| Stage 4 render, one ffmpeg pass | 2 min | M | 116 s blur, 106 s solid (5.3× real time); old Python renderer 207–400 s |
| Stage 5 import + 15 QA frames + viewing + gate | 5–8 min | E | viewing every frame is mandatory |
| **Total** | **≈ 45–75 min** | | about 10–12 min of machine time; the rest is translation and review |

Per episode (≈ 1 min) that is **about 5–8 min**. A full 80-episode series (8 units) takes
**about 7–10 hours** of work. Units can run in an overlapped pipeline: one unit renders or
synthesises while the next is being translated, so the wall time is closer to the
translation time × 8 plus one unit of machine time.

`sub_only` mode skips Stage 3: about 8–10 min less per unit.

## Edits after release (incremental)

| Change | Time | | Work |
|---|---|---|---|
| Reword one subtitle (`sub_text`) | ≈ 2 min | M | validate, one ffmpeg pass |
| Change one dubbed line (`tts_text`) | ≈ 3 min | E | 1 sentence TTS + mix + master + 1 chunk |
| Change mix parameters only | ≈ 2.5 min | M | mix + master + gate; no chunk re-render |
| Shorten 4 TTS lines | 14 s | M | TTS + assemble only |
| Timing fix touching 2 lines | ≈ 2 min | M | 5/10 chunks |
| Style or renderer change | ≈ 2 min | M | one ffmpeg pass |
| Undo any edit | ≈ 2 min | M | one ffmpeg pass (`--engine python` restores from chunk cache in seconds) |
| Add Stage 5 QA after an edit | 2–4 min | E | frames of the changed chunks only |

## Formula

```
machine_min ≈ 0.15·D (Stage 1) + 0.12·D (stems) + 0.022·L (TTS) + 0.15·D (mix+master) + 0.20·D (render) + 1.5
human_min   ≈ 0.12·L (translate + adapt) + 10 (audit + QA viewing)
```

`D` is the unit duration in minutes. `L` is the number of lines.
For D = 10 and L = 186: about 11 min of machine time plus about 32 min of human/agent time.
