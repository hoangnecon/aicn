# Incremental build model

## Units of work and their keys
| Output | Granularity | Key = sha256 of | Store |
|---|---|---|---|
| Stage 1 ASR + scan | unit | master_source sha256, ASR engine, scan fps | `data/01_*` (frozen) |
| TTS line | script item | `tts_text`, voice, engine tag | `.cache/tts/<key>.wav` |
| Overlay chunk | episode, split every `--chunk-s` (90 s) | source sha, frame window, cues in window (id, start, end, text), hardsub intervals in window, style, font sha, encoder, renderer version | `.cache/chunks/<key>.mp4` |
| Staging MP4 | unit | concat of chunk keys + master audio | `media/.staging_render.mp4` (seconds to rebuild) |

A cue that spans a chunk boundary belongs to both chunks; both re-render when it changes.

## Why chunks are episodes
Episode files are the natural cut points: no cue crosses them in practice, so one text edit dirties one chunk. Long episodes are split into equal sub-chunks. Boundaries are snapped to the frame grid and each chunk is decoded with `-frames:v N`, so the concatenation has exactly the source frame count and the audio (muxed once from `master_audio_48k.wav`) cannot drift.

## Why video-only chunks
AAC encoder priming adds a few ms per chunk; concatenating audio chunks clicks and drifts. Video chunks are concatenated with `-c copy`, then the single master audio is muxed. Cost of the final step: seconds.

## TTS reindexing
`synthesize_and_audit_pacing.py` names files by position (`seg_0042.wav`). Inserting or deleting a line shifts every later position. `tts-sync` maps each position to its content key: keeps files whose key matches, restores moved lines from cache, deletes stale ones. Then `--only-missing` synthesises only new text. `tts-sync --store` caches the new files.

## Measured on a 2×10 s synthetic unit (1080p30, Apple Silicon)
| Action | Chunks rendered | Wall time |
|---|---|---|
| first render | 2 | 8.8 s |
| edit one cue | 1 | 4.7 s |
| undo the edit | 0 | 0.4 s |
Throughput ≈ 68 fps (2.3× real time). A 10-minute unit renders fully in ≈ 4.5 min; a one-line fix re-renders one episode (≈ 30 s).

## Measured on a real unit (unit_07_ep61_70: 10 eps, 609.5 s, 1080p30, 186 cues, voice_dub)
| Action | Work done | Wall time |
|---|---|---|
| full style-B render | 10/10 chunks, 18,286 frames | 207–243 s (≈ 2.5–2.9× real time) |
| reword one subtitle (sub_text only) | Stage 2 validate, 0 TTS, assemble, 1/10 chunks | 38 s end to end |
| shorten 4 TTS lines | 4 TTS synth, assemble | 14 s |
| fix display timing of 2 lines | 5/10 chunks (lines + chunks whose windows moved) | 118 s |
| renderer change (version bump) | 10/10 chunks | ≈ 215 s |

Lessons from that run:
- A full re-synthesis of all TTS changes `slot_end` by a few ms on many lines (TTS is not deterministic). Display times are frame-quantized so this jitter does not dirty chunks; with the TTS cache, unchanged lines are never re-synthesized anyway.
- The 5 fps scan can under-measure line width (clipped outer glyphs). The renderer re-measures each Chinese line on the first real frame it covers and grows the box.
- The mask feather (σ 10) weakens blur at the box edge. The box is grown by 1.5 × feather on every side so the glyphs themselves get full blur; otherwise black-outlined glyphs on white backgrounds leave ghosts.
- The automated CV check in Stage 5 passed frames that still had visible Chinese strokes. Only the visual frame review caught them. Never skip it.
