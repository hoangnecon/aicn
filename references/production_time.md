# Production time

Reference unit: unit_07_ep61_70. It has 10 episodes, 609.5 s, 1080p30, 186 dubbed lines, and was run in voice_dub mode on Apple Silicon.
**M** marks a time measured on 2026-11-19. **E** marks an estimate: work done by the agent or a person, so the time varies with the series.

## One new unit (10 episodes, ≈ 10 min of video), from download to release

| Step | Time | | Notes |
|---|---|---|---|
| Download 10 episodes (`ohg download`) | 1–3 min | E | network bound |
| Stage 0 init + adopt | < 30 s | M | |
| Stage 1 Whisper (2 passes) + 5 fps scan + waveform audit | 3.3 min | M | scan 24 s, ASR 165 s, audit 3 s |
| Stage 1 audit-frame review (agent views keyframes, fixes homophones) | 5–10 min | E | |
| Stage 2 translate + adapt 180–240 lines, validate | 20–40 min | E | the largest variable; glossary reuse makes later units faster |
| Stage 3 TTS for all lines | ≈ 5.5 min | M | 1.7 s per line (30 lines in 51 s) |
| Stage 3 rewrite over-tempo lines + assemble + verify | 1–3 min | M/E | usually 0–5 lines |
| Stage 4 style-B render, all chunks | 3.5–4 min | M | 207–243 s, 2.5–2.9× real time |
| Stage 5 import + 15 QA frames + viewing + gate | 5–8 min | E | viewing every frame is mandatory |
| **Total** | **≈ 50–80 min** | | about 18 min of machine time; the rest is translation and review |

Per episode (≈ 1 min) that is **about 5–8 min**. A full 80-episode series (8 units) takes
**about 7–10 hours** of work. Units can run in an overlapped pipeline: one unit renders or
synthesises while the next is being translated, so the wall time is closer to the
translation time × 8 plus one unit of machine time.

`sub_only` mode skips Stage 3: about 8–10 min less per unit.

## Edits after release (incremental)

| Change | Time | | Work |
|---|---|---|---|
| Reword one subtitle (`sub_text`) | 38 s | M | validate, assemble, render 1/10 chunks, concat, mux |
| Change one dubbed line (`tts_text`) | ≈ 45 s | M | + 1 TTS line |
| Shorten 4 TTS lines | 14 s | M | TTS + assemble only |
| Timing fix touching 2 lines | ≈ 2 min | M | 5/10 chunks |
| Style or renderer change | ≈ 3.5–4 min | M | every chunk |
| Undo any edit | < 5 s | M | chunk comes back from cache |
| Add Stage 5 QA after an edit | 2–4 min | E | frames of the changed chunks only |

## Formula

```
machine_min ≈ 0.33·D (Stage 1) + 0.03·L (TTS, voice_dub) + 0.38·D (render) + 1.5
human_min   ≈ 0.12·L (translate + adapt) + 10 (audit + QA viewing)
```

`D` is the unit duration in minutes. `L` is the number of lines.
For D = 10 and L = 186: about 15 min of machine time plus about 32 min of human/agent time.
