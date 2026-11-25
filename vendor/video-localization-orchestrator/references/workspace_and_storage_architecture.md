# Workspace Initialization, Storage Topology & File Contract Manual (`v3.0.0`)

This manual defines the **mandatory directory architecture, relocation-safe POSIX storage rules, initialization workflows, and JSON/Markdown schemas** for `aicn v3.0.0`.

---

## 1. Standardized Project Topologies (`--topology`)

| Topology | Flag | When to Use | Storage Root | Output Deliverables |
| :--- | :--- | :--- | :--- | :--- |
| **Series Hub + Working Units (Canonical)** | `--topology series_episodic` | Standard AICN full-series pipeline (`20–150+` episodes split into ~10-ep working slices sharing `series_bible.json`). | Series Hub: `workspace/<series-slug>/`<br>Working Units: `workspace/<series-slug>/units/<unit-slug>/` | `workspace/<series-slug>/deliverables/final_<series-slug>_full_vi.mp4` + `deliverables/subtitles/final_<series-slug>_full_vi.srt` (via `aicn.py merge-series`) |
| **Multi-Episode Compilation** | `--topology series_compilation` | Standalone compilation of 2–15 short episodes into one workspace. | `workspace/<slug>/` | Unit master `media/final_<slug>_vi.mp4` + `data/subtitles_full.srt` |
| **Single Video** | `--topology single` | 1 standalone video. | `workspace/<slug>/` | Unit master `media/final_<slug>_vi.mp4` + `data/subtitles_full.srt` |

> **Mandatory Mode & Release Contract:** Every workspace uses **`--mode voice_dub`** (full Vietnamese dubbing + dual-corner watermark + external `.srt`). `init_workspace.py` enforces `voice_dub` across all genres and rejects non-dubbing modes. Working batches under `units/` are internal slices only — never standalone uploads.

---

## 2. Canonical Directory Trees (`v3.0.0`)

### 2.1 Global Content-Addressed Cache (`<project_root>/.cache/`)
Shared across all hubs/units and immune to folder moves or `aicn.py clean`:
```text
<project_root>/.cache/
├── content_hash.db              # SQLite-WAL stat->sha256 index (PRAGMA journal_mode=WAL; busy_timeout=15000)
├── asr/<stage1_asr_key>.json    # Pure content-SHA256 Stage 1 ASR cache (asr-4.0)
├── stems/<stems_key>/           # Demucs 48 kHz PCM_16 vocals.wav + accomp.wav (aicn-audio-1.0)
├── tts_sent/<tts_sent_key>.wav  # Raw VieNeu-TTS utterance WAVs (PCM_16, vieneu-v3-turbo-48k)
├── rb_stretch/<rb_key>.wav      # Stretched rubberband R3 WAVs (PCM_16, rb3-f-3.0)
└── wm_overlay/<wm_key>.png      # Pre-composited tight top-band dual-corner watermark strips
```

### 2.2 Series Hub (`workspace/<series-slug>/`)
```text
workspace/<series-slug>/
├── CHANGELOG.md
├── PROGRESS.md
├── 00_series_manifest.md
├── config/
│   ├── rights.json
│   ├── series_bible.json        # MASTER Character Roster, Pronoun Matrix & Glossary
│   └── series_config.json       # mode: "voice_dub", genre, source_lang
├── posters/
│   ├── cover.jpg
│   ├── official_poster_<slug>_zh.jpg
│   └── poster_<slug>_vi_<ratio>_{raw,clean,branded}.png
├── source_inbox/
│   ├── raw/                     # Immutable raw downloads
│   └── batch_01_ep01_10/ ...    # Hardlinked 10-ep input batches
├── units/
│   └── unit_01_ep01_10/ ...     # Internal working batches (see Section 2.3)
├── deliverables/
│   ├── final_<series-slug>_full_vi.mp4
│   └── subtitles/final_<series-slug>_full_vi.srt
└── .cache/
```

### 2.3 Internal Working Batch (`units/<unit-slug>/`)
```text
units/<unit-slug>/
├── .cicd/                                   # Relocation-safe signed gates (`schema_version: "3.0"`)
│   └── stage_0_gate.json .. stage_5_gate.json
├── config/
│   ├── project_config.json                  # Canonical settings (`series_hub_rel: "../.."`, optional `watermark`)
│   ├── audio_mix.json                       # Optional Stage 3 overrides
│   ├── series_bible.json                    # Synced copy of hub series_bible.json
│   └── schemas/                             # 01_asr_segment, 02_bilingual_cue, 03_aligned_cue templates
├── source/
│   ├── episodes/                            # ep01.mp4 .. epNN.mp4 (hardlinked, immutable)
│   ├── concat_list.txt
│   └── master_source.mp4                    # Canonical input video read by Stages 1–5
├── data/
│   ├── 00_video_profile.json                # Resolution, FPS, duration, `source_video: "source/master_source.mp4"`, `localization_mode: "voice_dub"`
│   ├── 00_episodes_manifest.json            # Episode map (`source_file` in POSIX `/`, sha256, offsets)
│   ├── 01_asr_raw_segments.json             # Anti-orphan & collar-deduped ASR clauses (`asr-4.0`)
│   ├── 01_pass2b_recovery_report.json       # Multi-band Pass 2B recovery + `verified_non_speech_gaps`
│   ├── 01_waveform_voice_audit.json         # Acoustic alignment & strict monotonic audit (`wf-4.0`)
│   ├── 01_extracted_audit_manifest.json     # Audit frames + content SHA-256
│   ├── 02_bilingual_script.json             # Dual-layer script (`sub_text` + `tts_text` + `speaker` + `ep_id`)
│   ├── 03_aligned_subtitles.json            # Per-clause SRT timing + `utterance_id`, `utterance_wav`, `tempo_ratio`
│   ├── 03_dub_sentences.json                # Indivisible utterance synthesis log & overflow trim advice
│   ├── 03_mix_stats.json                    # Dub gain, active-frame `dub_snr_speech_db`, vocal/BGM attenuation
│   ├── 03_mix_report.json                   # 7 Stage 3 gate checks
│   ├── subtitles_full.srt                   # Unit Vietnamese SRT
│   └── 04_qa_frames_manifest.json           # Stratified QA frames + content SHA-256
├── frames/
│   ├── audit/                               # Stage 1 visual audit frames
│   └── qa/                                  # Stage 5 QA frames
├── media/
│   ├── stems/{vocals,accomp}.wav            # 48 kHz PCM_16 Demucs stems + stems.json
│   ├── tts_segments/utt_NNNN.wav            # Intact stretched 48 kHz PCM_16 utterance WAVs
│   ├── audio/{dub_voice,premaster}_48k.wav  # Intermediate Stage 3 WAVs (pruned by `clean`)
│   ├── master_audio_48k.wav                 # Final -14 LUFS 24-bit stereo master
│   ├── .staging_render.mp4                  # Stage 4 one-pass hardware render
│   ├── .staging_final.mp4                   # Hardlink of .staging_render.mp4 for Stage 5 QA
│   └── final_<unit-slug>_vi.mp4             # Hardlink promoted by verify_stage_5.py
├── scripts/                                 # Unit-local helper code only (0 .json/.srt allowed)
├── CHECKLIST.md
├── CHANGELOG.md
├── 00_project_manifest.md
├── 01_visual_audit_notes.md
├── 02_glossary_and_pronunciation.md
├── 03_pacing_and_audio_report.md
└── 04_qa_review_report.md
```

---

## 3. Initialization Commands (`init_workspace.py`)

### Case A: Series Hub + Working Unit (`--topology series_episodic`, Canonical)
```bash
$AICN_PY <aicn>/scripts/aicn.py init-series \
  --series-slug "nam-18-tuoi-toi-che-tao-than-khi" \
  --title-vi "Năm 18 Tuổi Tôi Chế Tạo Thần Khí" \
  --title-zh "那年十八我造神器" \
  --total-episodes 80 \
  --rights "own"

$AICN_PY <aicn>/vendor/video-localization-orchestrator/scripts/init_workspace.py \
  --topology series_episodic \
  --series-slug "nam-18-tuoi-toi-che-tao-than-khi" \
  --unit-slug "unit_01_ep01_10" \
  --episodes-dir "workspace/nam-18-tuoi-toi-che-tao-than-khi/source_inbox/batch_01_ep01_10" \
  --genre "short-drama" \
  --mode "voice_dub"

$AICN_PY <aicn>/scripts/aicn.py adopt \
  --unit "workspace/nam-18-tuoi-toi-che-tao-than-khi/units/unit_01_ep01_10"
```

### Case B: Single Video or Compilation
```bash
$AICN_PY <aicn>/vendor/video-localization-orchestrator/scripts/init_workspace.py \
  --topology single \
  --video "downloads/input.mp4" \
  --slug "my-video" \
  --genre "tech-review" \
  --mode "voice_dub"
```

### Case C: Upgrade / Retrofit an Existing Workspace (`--sync-existing`)
```bash
$AICN_PY <aicn>/vendor/video-localization-orchestrator/scripts/init_workspace.py \
  --sync-existing \
  --workspace "workspace/<series>/units/<unit>"
```

---

## 4. Relocation-Safe Relative Paths & Golden Rules

1. **Zero Absolute Workspace Paths in Metadata or Gates:**
   - `00_video_profile.json` writes `"source_video": "source/master_source.mp4"`.
   - `00_episodes_manifest.json` uses POSIX `/` paths.
   - `project_config.json` uses `"series_hub_rel": "../.."`.
   - `.cicd/stage_N_gate.json` (`schema_version: "3.0"`) records `"workspace_slug"` and relative POSIX artifact keys hashed with `ap.content_sha256`.
2. **Zero Stray Data in `scripts/`:** `scripts/` holds code only; structured JSONs and SRTs live in `data/`.
3. **Immutable `source/` & `source_inbox/`:** Rebuilding `master_source.mp4` invalidates Stage 1 and downstream outputs.
4. **Staging Before Promotion:** Stage 4 renders to `media/.staging_render.mp4`; Stage 5 hardlinks to `media/.staging_final.mp4` and promotes `media/final_<unit>_vi.mp4` only after `verify_stage_5.py` scores `100/100`.
5. **Single Full-Series Release (`merge-series`):** Once ALL batches read `PPPPPP`, `aicn.py merge-series --hub <hub>` reads `units/<u_slug>/media/final_*_vi.mp4` and `units/<u_slug>/data/subtitles_full.srt` to produce `deliverables/final_<series>_full_vi.mp4` and `deliverables/subtitles/final_<series>_full_vi.srt`.
