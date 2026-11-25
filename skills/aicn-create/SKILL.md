---
name: aicn-create
description: Use when starting or scaffolding a new drama series project, downloading raw episodes into source_inbox, splitting episodes into working units, renaming a series slug or title ("tạo dự án mới", "tải bộ này về làm", "chia unit", "đổi tên bộ phim"), or setting up Stage 0.
---

# Creating & Scaffolding a Series Hub (`/aicn-create`)

## Overview
Initialize a relocation-safe Series Hub (`workspace/<slug>/`), ingest immutable raw episodes into `source_inbox/`, normalize working batches (`units/*`, ~10 episodes per batch), and sign Stage 0 (`stage_0_gate.json` `100/100`).

- `<aicn>`: `$AICN_HOME` if set, else `~/.gemini/config/plugins/aicn` (`../..` from this skill).
- `$AICN_PY`: `C:\Work\Tools\VieNeu-TTS\.venv\Scripts\python.exe` (Windows) or `/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python` (macOS).
- Deep references: `<aicn>/references/folder_contract.md`, `<aicn>/references/workspace_and_storage_architecture.md`.

## Mandatory Recipe

1. **Ensure machine profile exists (once per machine):**
   ```bash
   $AICN_PY <aicn>/scripts/aicn.py doctor --root <project-root>
   ```

2. **Scaffold the Series Hub with an unaccented Vietnamese slug & rights basis:**
   `--series-slug` MUST be the Vietnamese translated title, lowercase, ASCII, hyphen-separated (`nam-18-tuoi-toi-che-tao-than-khi` — never Pinyin or numeric IDs). `--rights` is mandatory (`own` | `licensed:<id>` | `cps:<platform>` | `mcn:<name>`).
   ```bash
   $AICN_PY <aicn>/scripts/aicn.py init-series \
     --workspace-root <project-root>/workspace \
     --series-slug <slug> \
     --title-vi "<Tên Tiếng Việt>" \
     --title-zh "<Tên Gốc>" \
     --total-episodes <N> \
     --unit-size 10 \
     --source-lang zh \
     --rights cps:hongguo
   ```
   *(For English DramaDunyam series, pass `--source-lang en`.)*

3. **Download raw episodes & official HD poster into the Hub:**
   Save raw episodes strictly into `workspace/<slug>/source_inbox/<batch>/epNN.mp4` (immutable; never edit raw files) and HD source poster into `workspace/<slug>/posters/official_poster_<slug>_zh.jpg` via `hg_tool.cjs`, `dy_tool.py`, or `dd_tool.py`.

4. **Initialize each ~10-episode internal working batch (`units/<batch>`) & adopt:**
   ```bash
   $AICN_PY <aicn>/scripts/init_workspace.py \
     --episodes-dir <hub>/source_inbox/<batch> \
     --workspace-root <hub>/units \
     --slug <batch> \
     --topology series_episodic \
     --localization-mode voice_dub \
     --series-hub <hub> \
     --brand-dir <project-root>/workspace/branding-dau-do
   $AICN_PY <aicn>/scripts/aicn.py adopt --unit <hub>/units/<batch>
   ```
   Verify `discover` shows `P-----` (`stage_0_gate.json` = `100/100`) on every created batch.

## Updating Series Slug or Titles Later
Never rename hub folders by hand; run:
```bash
$AICN_PY <aicn>/scripts/aicn.py update-series --hub <hub> --series-slug <new-slug> --title-vi "<Tên Mới>"
```

## Common Mistakes
- **Treating `units/*` as standalone uploads:** `units/*` are internal processing slices only; the sole publishable video is `deliverables/final_<slug>_full_vi.mp4`.
- **Setting `sub_only`:** Retired. Every AICN series requires full `voice_dub`.

**Next step:** Invoke `aicn-script` to run Stage 1 ASR/visual audit and author Stage 2 scripts.
