# Folder and file contract

## Root discovery order
1. `<root>/workspace/*/` and `<root>/*/` that contain `units/` and `config/series_bible.json` are Series Hubs.
2. Inside a hub, `units/*/config/project_config.json` marks a unit.
3. A unit is "adopted" when `CHECKLIST.md` or `.cache/build_state.json` exists. Adopt units that predate this skill before editing them.

## Series Hub
| Path | Created by | Purpose |
|---|---|---|
| `CHANGELOG.md` | init-series | append-only event log for the whole series |
| `PROGRESS.md` | init-series | scope, auto-table of units (`<!-- AUTO:UNITS -->`), open issues, next action |
| `00_series_manifest.md` | init_workspace.py | episode-to-unit map |
| `config/series_bible.json` | init-series | characters, pronoun matrix (xưng hô), glossary, voices, `overlay_style` |
| `config/rights.json` | init-series | licence basis and proof; Stage 0 is not done while basis is `unknown` |
| `source_inbox/<batch>/` | you | raw downloads, never modified |
| `units/<batch>/` | init_workspace.py + adopt | internal working batch (~10 episodes) for caching/QA/pipelining — **never** a standalone upload |
| `deliverables/final_<series>_full_vi.mp4` | `merge-series` | **THE publishable full-series master** (all episodes, full dub + full sub + watermark) |
| `deliverables/subtitles/final_<series>_full_vi.srt` | `merge-series` | consolidated full-series SRT with cumulative offsets |

## Unit (adds to the init_workspace.py v2.0 tree)
| Path | Purpose |
|---|---|
| `CHECKLIST.md` | per-stage boxes, ticked only after the gate prints PASS |
| `CHANGELOG.md` | `- time [kind] what (why) -> rerun` lines, via `aicn.py log` |
| `config/overlay_style.json` | style B parameters; part of every chunk key |
| `.cache/stat_hash.json` | (size, mtime_ns) → sha256, so big media are hashed once |
| `.cache/build_state.json` | chunk id → key and per-part hashes, last render stats |
| `.cache/chunks/<key>.mp4` | rendered video-only chunks, content-addressed |
| `.cache/tts/<key>.wav`, `.cache/tts_index.json` | TTS lines, content-addressed; index maps seg number → key |
| `media/chunks/concat.txt` | concat list of the current chunk keys |
| `media/.staging_render.mp4` | concatenated chunks + master audio; imported by Stage 5 |

## Rules
- `.json`/`.srt` never go in `scripts/`; data lives in `data/`.
- `source/` and `source_inbox/` are immutable. Re-concatenating `master_source.mp4` invalidates Stage 1 and every chunk.
- `.cache/` may be deleted at any time; the cost is a full re-render, never a wrong result.
- Log kinds: `init`, `stage`, `edit`, `fix`, `render`, `release`, `note`.
