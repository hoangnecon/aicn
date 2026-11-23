# Folder, Naming & Markdown Contract

## 1. Series Naming Rule (Mandatory — Enforced by `aicn.py init-series`)

- **Rule:** Every series folder (`--series-slug`) MUST be the **Vietnamese translated title**, **no accents (không dấu)**, **lowercase (viết thường)**, **hyphen-separated (gạch nối `-`)**, matching `^[a-z0-9]+(-[a-z0-9]+)*$`.
- **Examples:**
  - `《那年十八我造神器》` ("Năm 18 Tuổi Tôi Chế Tạo Thần Khí") → **`nam-18-tuoi-toi-che-tao-than-khi`**
  - `《重回1990》` ("Trở Lại Năm 1990") → **`tro-lai-nam-1990`**
- **Forbidden:** Pinyin (`na_nian_shi_ba...`), underscores (`_`), uppercase letters, Vietnamese diacritics, or arbitrary nicknames.
- **Release rule:** 1 published video = **1 FULL series** (`deliverables/final_<series-slug>_full_vi.mp4`). Working batches under `units/` are internal processing slices only — never standalone uploads.

---

## 2. Parent Folder: Series Hub (`workspace/<series-slug>/`)

Created by `aicn.py init-series --series-slug <slug> --title "<Tên Tiếng Việt>" --total-episodes <N> --rights <basis>`:

```text
workspace/<series-slug>/
├── CHANGELOG.md                          # Append-only hub event log (template: templates/CHANGELOG.md)
├── PROGRESS.md                           # Hub progress & full-release status (template: templates/PROGRESS.md)
├── 00_series_manifest.md                 # Episode-to-batch registry (created by init_workspace.py)
├── config/
│   ├── rights.json                       # License basis & proof (required != "unknown")
│   ├── series_bible.json                 # Shared character roster, pronoun matrix (xưng hô), glossary, voices
│   └── series_config.json                # Default genre, mode (voice_dub), export_formats
├── source_inbox/
│   ├── raw/                              # Raw downloads (ep01.mp4..epNN.mp4), immutable
│   └── batch_01_ep01_10/ .. batch_08_*/  # 10-ep hardlinked input batches (ep01.mp4..ep10.mp4)
├── units/
│   └── unit_01_ep01_10/ .. unit_08_*/    # Internal working batches (NOT standalone uploads)
├── deliverables/
│   ├── final_<series-slug>_full_vi.mp4   # THE publishable full-series master (created by `aicn.py merge-series`)
│   ├── subtitles/
│   │   └── final_<series-slug>_full_vi.srt
│   ├── 16x9/                             # Hardlinked verified batch MP4s (input to merge-series)
│   ├── 9x16/                             # Optional vertical exports
│   └── episodes/                         # Optional per-episode splits
├── posters/                              # Thumbnails & series artwork
└── .cache/                               # Hub-level concat list & shared cache (safe to delete)
```

---

## 3. Child Folder: Internal Working Batch (`units/<batch>/`)

Created by `init_workspace.py --topology series_episodic ...` + `aicn.py adopt --unit <batch>`:

```text
units/<batch>/
├── CHECKLIST.md                          # Stage 0..6 human checklist (template: templates/CHECKLIST.md)
├── CHANGELOG.md                          # Append-only batch log via `aicn.py log` (template: templates/UNIT_CHANGELOG.md)
├── 00_project_manifest.md                # Stage 0: batch video spec & episode table
├── 01_visual_audit_notes.md              # Stage 1: visual keyframe OCR & homophone audit log
├── 02_glossary_and_pronunciation.md      # Stage 2: batch terms, xưng hô & <en>...</en> IPA notes
├── 03_pacing_and_audio_report.md         # Stage 3: auto-written by `aicn_audio.py verify` (LUFS, SNR, tempo)
├── 04_qa_review_report.md                # Stage 5: mandatory visual inspection table (1 PASS/FAIL row per QA frame)
├── config/
│   ├── project_config.json               # Batch settings, series_hub_dir, optional watermark paths/coords
│   ├── audio_mix.json                    # Optional overrides for aicn_audio.py DEFAULTS
│   ├── series_bible.json                 # Linked/synced copy of hub series_bible.json
│   └── schemas/*.template.json           # Canonical JSON schemas for Stages 1..3
├── source/
│   ├── episodes/ep01.mp4..ep10.mp4       # Hardlinked batch episodes (immutable)
│   ├── concat_list.txt
│   └── master_source.mp4                 # Concatenated batch source (immutable; sha256 freezes Stage 1)
├── data/
│   ├── 00_video_profile.json             # Width, height, fps, duration, localization_mode
│   ├── 00_episodes_manifest.json         # Per-episode sha256 & timeline offsets
│   ├── 01_asr_raw_segments.json          # Multi-pass MLX Whisper segments
│   ├── 01_waveform_voice_audit.json      # 50 ms acoustic voice mask & onset/coverage audit
│   ├── 01_extracted_audit_manifest.json  # Stage 1 audit frames + sha256
│   ├── 02_bilingual_script.json          # Translated clauses (`zh_text`, `sub_text`, `tts_text`, `speaker`)
│   ├── 03_aligned_subtitles.json         # Hard-locked `slot_start`/`slot_end` + tempo
│   ├── 03_dub_sentences.json             # Sentence-level VieNeu TTS log & overflow check
│   ├── 03_mix_stats.json                 # Measured dub gain, SNR, vocal/BGM attenuation
│   ├── 03_mix_report.json                # Audio gate checks & params
│   ├── subtitles_full.srt                # Batch Vietnamese SRT
│   └── 04_qa_frames_manifest.json        # Stage 5 QA frames + sha256
├── media/
│   ├── stems/{vocals,accomp}.wav         # Demucs MLX stems + stems.json (pruned by `clean`)
│   ├── tts_segments/seg_NNNN.wav         # Per-clause 48 kHz dub clips
│   ├── audio/{dub_voice,premaster}_48k.wav # Intermediate mixes (pruned by `clean`)
│   ├── master_audio_48k.wav              # Final -14 LUFS 24-bit stereo master (hashed by Stage 3 gate)
│   ├── .staging_render.mp4               # Stage 4 one-pass output (hashed by Stage 4 gate)
│   ├── .staging_final.mp4                # Hardlink to .staging_render.mp4 for Stage 5 QA
│   └── final_<batch>_vi.mp4              # Hardlink promoted by verify_stage_5.py (hashed by Stage 5 gate)
├── frames/
│   ├── audit/frame_NNNNs.jpg             # Stage 1 visual audit keyframes
│   └── qa/frame_NNNs.jpg                 # Stage 5 QA keyframes (every frame must be viewed)
├── .cicd/
│   └── stage_0_gate.json .. stage_5_gate.json # Cryptographically signed 100/100 gate tokens
└── .cache/
    ├── stat_hash.json                    # (size, mtime_ns) → sha256 fast cache
    ├── build_state.json                  # Render keys, per-part hashes & last render stats
    └── tts_sent/<sha>.wav                # Content-addressed sentence TTS cache
```

---

## 4. Per-Stage Markdown Files, Templates & Fill Format

| File | Scope | Created by | When & How to Fill | Required Format |
|---|---|---|---|---|
| `PROGRESS.md` | Hub | `init-series` (`templates/PROGRESS.md`) | Auto-updated by `aicn.py progress` / `log` / `merge-series`. Never edit inside `<!-- AUTO:UNITS -->`. | Scope header + `FULL RELEASE` status + auto table + Open issues / Next action |
| `CHANGELOG.md` | Hub & Batch | `init-series` / `adopt` (`templates/CHANGELOG.md`, `UNIT_CHANGELOG.md`) | Append via `aicn.py log --unit U --kind <kind> --message "..."`. Never rewrite past lines. | `- YYYY-MM-DD HH:MM [kind] what (why) -> rerun` (`kind`: `init\|stage\|edit\|fix\|render\|release\|note`) |
| `CHECKLIST.md` | Batch | `adopt` (`templates/CHECKLIST.md`) | Tick `[x]` **only after** the stage gate prints `PASS`. | Markdown checkboxes grouped by Stage 0..6 |
| `00_project_manifest.md` | Batch | `init_workspace.py` | Filled automatically at Stage 0. | Video profile + episode table |
| `01_visual_audit_notes.md` | Batch | `init_workspace.py` | **Stage 1 (mandatory before `verify_stage_1.py`)**: inspect every JPG in `01_extracted_audit_manifest.json` via `read_file` and log each filename/timestamp + character/proper-noun/homophone notes. | One bullet/table row per `frame_NNNNs.jpg` listing exact filename or `<t>s` and visual notes |
| `02_glossary_and_pronunciation.md` | Batch | `init_workspace.py` | **Stage 2**: list character names, `xưng hô` pairs, domain terms, and `<en>...</en>` tokens matching `series_bible.json`. | Markdown table (`ZH \| VI \| TTS / Pronunciation \| Notes`) |
| `03_pacing_and_audio_report.md` | Batch | `aicn_audio.py verify` | Auto-generated before `verify_stage_3.py` hashes it. | Engine version, target LUFS/TP, `[PASS]/[FAIL]` list of all audio gate checks |
| `04_qa_review_report.md` | Batch | `init_workspace.py` | **Stage 5 (mandatory before `verify_stage_5.py`)**: inspect every JPG in `04_qa_frames_manifest.json` via `read_file` and append 1 row per frame. **No `_TBD_` and no `\| FAIL` allowed.** | Table: `\| frame_NNNs.jpg \| <t>s \| <visual sanity / dual watermark> \| PASS \|` |

---

## 5. Housekeeping Rules

- `.json`/`.srt` never go in `scripts/`; all structured data lives in `data/`.
- `source/` and `source_inbox/` are immutable. Rebuilding `master_source.mp4` invalidates Stage 1 and downstream caches.
- `.cache/` may be deleted at any time; the cost is recomputation, never a wrong result.
- Run `aicn.py clean --unit <batch>` after Stage 5 PASS to free ~1.2 GB/batch while keeping all 6 gates (`PPPPPP`) intact.
