# Folder, Naming & Markdown Contract (`v3.0.0`)

## 1. Series Naming Rule (Mandatory — Enforced by `aicn.py init-series`)

- **Rule:** Every series folder (`--series-slug`) MUST be the **Vietnamese translated title**, **no accents (không dấu)**, **lowercase (viết thường)**, **hyphen-separated (gạch nối `-`)**, matching `^[a-z0-9]+(-[a-z0-9]+)*$`.
- **Examples:**
  - `《那年十八我造神器》` ("Năm 18 Tuổi Tôi Chế Tạo Thần Khí") → **`nam-18-tuoi-toi-che-tao-than-khi`**
  - `《重回1990》` ("Trở Lại Năm 1990") → **`tro-lai-nam-1990`**
- **Forbidden:** Pinyin (`na_nian_shi_ba...`), underscores (`_`), uppercase letters, Vietnamese diacritics, or arbitrary nicknames.
- **Release & Mode rule:** 1 published video = **1 FULL series** (`deliverables/final_<series-slug>_full_vi.mp4` + `deliverables/subtitles/final_<series-slug>_full_vi.srt`) in mandatory **`voice_dub`** mode. Working batches under `units/` are internal processing slices only — never standalone uploads.

---

## 2. Global Content-Addressed Cache (`<project_root>/.cache/`)

Pure content SHA-256 stores shared across all series and surviving unit/hub renames or `aicn.py clean`:

```text
<project_root>/.cache/                    # (fallback: ~/.cache/aicn/)
├── content_hash.db                       # SQLite-WAL stat->sha256 index (PRAGMA journal_mode=WAL; busy_timeout=15000)
├── asr/<stage1_asr_key>.json             # Stage 1 Pass 1 + Pass 2B cache (asr-4.0)
├── stems/<stems_key>/                    # Demucs 48 kHz PCM_16 vocals.wav & accomp.wav (aicn-audio-1.0)
├── tts_sent/<tts_sent_key>.wav           # Raw VieNeu-TTS utterance WAVs (PCM_16, vieneu-v3-turbo-48k)
├── rb_stretch/<rb_stretch_key>.wav       # Stretched rubberband R3 WAVs (PCM_16, rb3-f-3.0)
└── wm_overlay/<wm_overlay_key>.png       # Pre-composited tight top-band dual-corner RGBA watermark strips
```

Machine-wide GPU slot locks live in `~/.cache/aicn/locks/`: `gpu_pool_alloc.lock` + `gpu_vram_slot_0..M-1.lock` (OS-advisory locked, auto-released on exit/crash).

---

## 3. Parent Folder: Series Hub (`workspace/<series-slug>/`)

Created by `aicn.py init-series --series-slug <slug> --title-vi "<Tên Tiếng Việt>" --title-zh "<Tên Gốc ZH>" --total-episodes <N> --rights <basis>` (`--title` is accepted as an alias for `--title-vi`; pass `--source-lang en` for English sources):

```text
workspace/<series-slug>/
├── CHANGELOG.md                          # Append-only hub event log (templates/CHANGELOG.md)
├── PROGRESS.md                           # Hub progress & full-release status (templates/PROGRESS.md)
├── 00_series_manifest.md                 # Episode-to-batch registry (updated by init_workspace.py)
├── config/
│   ├── rights.json                       # License basis & proof (required != "unknown")
│   ├── series_bible.json                 # Shared character roster, pronoun matrix (xưng hô), glossary, voices
│   └── series_config.json                # mode: "voice_dub", genre, source_lang
├── source_inbox/
│   ├── raw/                              # Raw downloads (ep01.mp4..epNN.mp4), immutable
│   └── batch_01_ep01_10/ ...             # 10-ep hardlinked input batches (ep01.mp4..ep10.mp4)
├── units/
│   └── unit_01_ep01_10/ ...              # Internal working batches (NOT standalone uploads)
├── posters/                              # Series artwork (see references/poster_and_caption_guide.md)
│   ├── cover.jpg                         # Catalog thumbnail (~640px)
│   ├── official_poster_<slug>_zh.jpg     # HD unshrunk official source poster (1440px+)
│   ├── poster_<slug>_vi_<ratio>_raw.*    # Archived raw localized poster (9x16 | 16x9 | 3x4)
│   ├── poster_<slug>_vi_<ratio>_clean.png    # Resized clean PNG (optional star removal)
│   └── poster_<slug>_vi_<ratio>_branded.png  # Final publish-ready dual-corner branded poster
├── deliverables/
│   ├── final_<series-slug>_full_vi.mp4   # THE publishable full-series master (via `aicn.py merge-series`)
│   └── subtitles/
│       └── final_<series-slug>_full_vi.srt
└── .cache/                               # Hub-level concat/temp directory (safe to delete)
```

---

## 4. Child Folder: Internal Working Batch (`units/<batch>/`)

Created by `init_workspace.py --topology series_episodic ... --mode voice_dub` + `aicn.py adopt --unit <batch>`:

```text
units/<batch>/
├── CHECKLIST.md                          # Stage 0..6 human checklist (templates/CHECKLIST.md)
├── CHANGELOG.md                          # Append-only batch log via `aicn.py log` (templates/UNIT_CHANGELOG.md)
├── 00_project_manifest.md                # Stage 0: batch video spec & episode table
├── 01_visual_audit_notes.md              # Stage 1: visual keyframe OCR & homophone audit log
├── 02_glossary_and_pronunciation.md      # Stage 2: batch terms, xưng hô & <en>...</en> IPA notes
├── 03_pacing_and_audio_report.md         # Stage 3: auto-written by `aicn_audio.py verify` (LUFS, SNR, tempo)
├── 04_qa_review_report.md                # Stage 5: mandatory visual inspection table (1 PASS/FAIL row per QA frame)
├── config/
│   ├── project_config.json               # Batch settings, `series_hub_rel: "../.."`, optional watermark block
│   ├── audio_mix.json                    # Optional overrides for aicn_audio.py DEFAULTS
│   ├── series_bible.json                 # Synced copy of hub series_bible.json
│   └── schemas/*.template.json           # Canonical JSON schemas for Stages 1..3
├── source/
│   ├── episodes/ep01.mp4..ep10.mp4       # Hardlinked batch episodes (immutable)
│   ├── concat_list.txt
│   └── master_source.mp4                 # Concatenated batch source (immutable; sha256 freezes Stage 1)
├── data/
│   ├── 00_video_profile.json             # Width, height, fps, duration, `source_video: "source/master_source.mp4"`, `localization_mode: "voice_dub"`
│   ├── 00_episodes_manifest.json         # Per-episode sha256 & timeline offsets (POSIX relative paths)
│   ├── 01_asr_raw_segments.json          # Multi-pass Whisper segments (asr-4.0, anti-orphan + collar dedup)
│   ├── 01_pass2b_recovery_report.json    # Multi-band Pass 2B recovery & verified_non_speech_gaps log
│   ├── 01_waveform_voice_audit.json      # 50 ms acoustic voice mask & strict monotonic alignment report (wf-4.0)
│   ├── 01_extracted_audit_manifest.json  # Stage 1 audit frames + content SHA-256
│   ├── 02_bilingual_script.json          # Dual-layer script (`zh_text`, `sub_text`, `tts_text`, `speaker`)
│   ├── 03_aligned_subtitles.json         # Per-clause SRT timing + `utterance_id` / `utterance_wav` / `tempo_ratio`
│   ├── 03_dub_sentences.json             # Indivisible utterance synthesis log & overflow trim guidance
│   ├── 03_mix_stats.json                 # Measured dub gain, active-frame `dub_snr_speech_db`, vocal/BGM attenuation
│   ├── 03_mix_report.json                # 7 Stage 3 audio gate checks & params
│   ├── subtitles_full.srt                # Batch Vietnamese SRT
│   └── 04_qa_frames_manifest.json        # Stage 5 QA frames + content SHA-256
├── media/
│   ├── stems/{vocals,accomp}.wav         # Demucs 48 kHz PCM_16 stems + stems.json (pruned by `clean`)
│   ├── tts_segments/utt_NNNN.wav         # Intact stretched 48 kHz PCM_16 utterance WAVs
│   ├── audio/{dub_voice,premaster}_48k.wav # Intermediate mixes (pruned by `clean`)
│   ├── master_audio_48k.wav              # Final -14 LUFS 24-bit stereo master (hashed by Stage 3 gate)
│   ├── .staging_render.mp4               # Stage 4 one-pass output (hashed by Stage 4 gate)
│   ├── .staging_final.mp4                # Hardlink to .staging_render.mp4 for Stage 5 QA
│   └── final_<batch>_vi.mp4              # Hardlink promoted by verify_stage_5.py (hashed by Stage 5 gate)
├── frames/
│   ├── audit/frame_NNNNs.jpg             # Stage 1 visual audit keyframes
│   └── qa/frame_NNNs.jpg                 # Stage 5 QA keyframes (every frame must be viewed via read_file)
├── scripts/                              # Unit-local helper code only (no .json/.srt files)
├── .cicd/
│   └── stage_0_gate.json .. stage_5_gate.json # Relocation-safe signed 100/100 tokens (`schema_version: "3.0"`)
└── .cache/
    ├── audio_state.json                  # Stage 3 content-hash build state
    └── build_state.json                  # Unified `stage1_key` + `render_key` state
```

---

## 5. Per-Stage Markdown Files, Templates & Fill Format

| File | Scope | Created by | When & How to Fill | Required Format |
|---|---|---|---|---|
| `PROGRESS.md` | Hub | `init-series` (`templates/PROGRESS.md`) | Auto-updated by `aicn.py progress` / `log` / `merge-series`. Never edit inside `<!-- AUTO:UNITS -->`. | Scope header + `voice_dub` contract + auto table + Open issues / Next action |
| `CHANGELOG.md` | Hub & Batch | `init-series` / `adopt` (`templates/CHANGELOG.md`, `UNIT_CHANGELOG.md`) | Append via `aicn.py log --unit U --kind <kind> --message "..."`. Never rewrite past lines. | `- YYYY-MM-DD HH:MM [kind] what (why) -> rerun` (`kind`: `init\|stage\|edit\|fix\|render\|release\|note`) |
| `CHECKLIST.md` | Batch | `adopt` (`templates/CHECKLIST.md`) | Tick `[x]` **only after** the stage gate prints `PASS`. | Markdown checkboxes grouped by Stage 0..6 |
| `00_project_manifest.md` | Batch | `init_workspace.py` | Filled automatically at Stage 0. | Video profile + episode table |
| `01_visual_audit_notes.md` | Batch | `init_workspace.py` | **Stage 1 (mandatory before `verify_stage_1.py`)**: inspect every JPG in `01_extracted_audit_manifest.json` via `read_file` and log filename/timestamp + character/proper-noun/homophone notes. | One table row per `frame_NNNNs.jpg` listing filename, timestamp, and visual notes |
| `02_glossary_and_pronunciation.md` | Batch | `init_workspace.py` | **Stage 2**: list character names, `xưng hô` pairs, domain terms, and `<en>...</en>` tokens matching `series_bible.json`. | Markdown table (`ZH \| VI \| TTS / Pronunciation \| Notes`) |
| `03_pacing_and_audio_report.md` | Batch | `aicn_audio.py verify` | Auto-generated before `verify_stage_3.py` hashes it. | Engine version, target LUFS/TP, `[PASS]/[FAIL]` list of all audio gate checks |
| `04_qa_review_report.md` | Batch | `init_workspace.py` | **Stage 5 (mandatory before `verify_stage_5.py`)**: inspect every JPG in `04_qa_frames_manifest.json` via `read_file` and append 1 row per frame. **No `_TBD_` and no `\| FAIL` allowed.** | Table: `\| frame_NNNs.jpg \| <t>s \| <visual sanity / dual watermark> \| PASS \|` |

---

## 6. Housekeeping Rules

- `.json`/`.srt` never go in `scripts/`; all structured data lives in `data/`.
- `source/` and `source_inbox/` are immutable. Rebuilding `master_source.mp4` invalidates Stage 1 and downstream caches.
- Global `<project_root>/.cache/` caches may be pruned only if disk space requires; unit `.cache/` contains lightweight build state JSONs only.
- Run `aicn.py clean --unit <batch>` after Stage 5 `PASS` to free intermediate stems/audio (`media/stems`, `media/tts_segments`, `media/audio`, `media/source_audio.wav`) while keeping all 6 gates (`PPPPPP`) and global `content_hash.db` / `asr` / `tts_sent` / `rb_stretch` / `wm_overlay` caches intact. Pass `--deep` after `merge-series` to also remove `media/.staging_render.mp4`, `media/master_audio_48k.wav`, and `source/*`.
