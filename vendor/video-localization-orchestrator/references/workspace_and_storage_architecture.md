# Workspace Initialization, Storage Topology & File Contract Manual

This manual defines the **mandatory directory architecture, storage rules, initialization workflows, and JSON/Markdown templates** for every video localization project. A strict, predictable file layout eliminates 80%+ of agent confusion, duplicate files, path mismatches, and cross-episode inconsistency.

---

## 1. Three Standardized Project Topologies (`--topology`)

Before downloading or processing any media, classify the project into one of **3 topologies** and initialize it with `init_workspace.py`:

| Topology | Flag | When to Use | Storage Root | Output Deliverables |
| :--- | :--- | :--- | :--- | :--- |
| **1. Single Video (`single`)** | `--topology single` | 1 standalone video (e.g., Bilibili tech essay, single short film, documentary, tutorial). | `workspace/<slug>/` | `deliverables/16x9/final_<slug>_vi.mp4`<br>`deliverables/9x16/final_<slug>_vi_9x16.mp4`<br>`deliverables/subtitles/<slug>_vi.srt` |
| **2. Multi-Episode Compilation (`series_compilation`)** | `--topology series_compilation` | 2–15 short episodes (`1.5–8 min` each) merged into a single `15–30 min` movie (e.g., AI short drama Season/Arc compilation). | `workspace/<slug>/` (with raw episodes in `source/episodes/` and merged timeline in `source/master_source.mp4`) | Full compiled movie (`16x9` + `9x16` + `.srt`) **PLUS** auto-split individual episodes in `deliverables/episodes/epXX_*` |
| **3. Multi-Episode Series Hub (`series_episodic`)** | `--topology series_episodic` | Long series (`20–100+` episodes) processed episode-by-episode or batch-by-batch over time while sharing character names, pronouns (`xưng hô`), voice IDs, and glossary. | Series Hub: `workspace/<series-slug>/`<br>Working Units: `workspace/<series-slug>/units/<unit-slug>/` | Consolidated in Series Hub `workspace/<series-slug>/deliverables/{16x9,9x16,subtitles,episodes}/` |

---

## 2. Canonical Directory Tree (`workspace/<slug>/`)

Every workspace (whether a standalone project in `workspace/<slug>/` or a unit inside `workspace/<series-slug>/units/<unit-slug>/`) MUST follow this exact directory contract:

```text
workspace/<slug>/
├── .cicd/                                   # [LOCKED] Cryptographic SHA-256 CI/CD Gate Tokens
│   ├── stage_0_gate.json                    # Stage 0: Topology, source media, config & templates gate
│   ├── stage_1_gate.json                    # Stage 1: Demucs/audio, 5fps bbox, waveform onset & audit gate
│   ├── stage_2_gate.json                    # Stage 2: Bilingual script, CJK=0, onset lock & IPA gate
│   ├── stage_3_gate.json                    # Stage 3: 48kHz audio master, pacing tempo & drift=0 gate
│   ├── stage_4_gate.json                    # Stage 4: Frosted-glass/pill geometry, leak=0 & ProRes 4444 gate
│   └── stage_5_gate.json                    # Stage 5: Stream check, CV perimeter scan & release promotion gate
├── config/                                  # [CONFIG & TEMPLATES] Setup parameters, Series Bible & JSON schemas
│   ├── project_config.json                  # Canonical topology, mode, style, export formats & file path map
│   ├── series_bible.json                    # Character roster, Pronoun Matrix (xưng hô), Voice IDs & Glossary
│   └── schemas/                             # Read-only JSON schema templates for agent reference
│       ├── 01_asr_segment.template.json     # Stage 1 fine-grained clause schema
│       ├── 02_bilingual_cue.template.json   # Stage 2 dual-layer translation schema
│       └── 03_aligned_cue.template.json     # Stage 3 locked timing schema
├── source/                                  # [IMMUTABLE INPUTS] Raw source videos & concatenation manifests
│   ├── episodes/                            # Raw individual episodes (ep01.mp4, ep02.mp4, ...)
│   ├── concat_list.txt                      # FFmpeg concat demuxer list (auto-generated for compilations)
│   └── master_source.mp4                    # Canonical input video read by Stages 1–5
├── data/                                    # [SINGLE SOURCE OF TRUTH] Structured pipeline JSONs & canonical SRT
│   ├── 00_video_profile.json                # Probed resolution, FPS, VFR flag, aspect_mode, strip_y0, strip_h
│   ├── 00_episodes_manifest.json            # Episode timeline map (ep_id, start_s, end_s, timecode_range, sha256)
│   ├── 01_asr_raw_segments.json             # Onset-clamped, blank-gap-split Chinese clauses (with ep_id)
│   ├── 01_zh_bbox_scan.json                 # 5fps stroke-validated hardsub bounding boxes + glyph IoU changes
│   ├── 01_waveform_voice_audit.json         # Acoustic waveform & visual sub-clause coverage report
│   ├── 01_extracted_audit_manifest.json     # SHA-256 manifest of Stage 1 visual audit frames
│   ├── 02_bilingual_script.json             # Dual-layer Vietnamese script (sub_text + tts_text + speaker + ep_id)
│   ├── 03_aligned_subtitles.json            # Final locked slot_start/slot_end & tempo ratios
│   ├── 04_overlay_geometry_log.json         # Per-cue frosted-glass/pill bounding boxes [x0, y0, x1, y1]
│   ├── 04_qa_frames_manifest.json           # Stratified QA frames + automated CV inspection results
│   └── subtitles_full.srt                   # Canonical Vietnamese SRT subtitle file
├── frames/                                  # [VISUAL EVIDENCE] Extracted keyframes for OCR & QA
│   ├── audit/                               # Stage 1 keyframes for homophone & visual context verification
│   ├── qa/                                  # Stage 5 16:9 composite verification frames
│   └── qa_9x16/                             # Stage 5B 9:16 vertical composite verification frames
├── media/                                   # [INTERMEDIATE & MASTER MEDIA] Audio stems, alpha overlay & renders
│   ├── source_audio.wav                     # 16kHz mono PCM for acoustic waveform & high-recall ASR
│   ├── demucs/                              # htdemucs stems (vocals.wav, no_vocals.wav)
│   ├── tts_segments/                        # 48kHz VieNeu-TTS segment WAVs (seg_0000.wav .. seg_NNNN.wav)
│   ├── master_audio_48k.wav                 # 48kHz stereo master audio track (TTS+BGM or native stereo)
│   ├── subtitles_overlay_alpha.mov          # ProRes 4444 (yuva444p10le) dynamic subtitle overlay strip
│   ├── .staging_final.mp4                   # Locked staging composite prior to Stage 5 gate approval
│   ├── final_<slug>_vi.mp4                  # Verified 16:9 master MP4
│   └── final_<slug>_vi_9x16.mp4             # Verified 9:16 vertical master MP4
├── deliverables/                            # [RELEASE VAULT] Clean user-facing outputs organized by format
│   ├── 16x9/
│   │   └── final_<slug>_vi.mp4              # Ready-to-publish 16:9 Landscape MP4
│   ├── 9x16/
│   │   └── final_<slug>_vi_9x16.mp4         # Ready-to-publish 9:16 Vertical MP4 (TikTok / Reels / Shorts)
│   ├── subtitles/
│   │   └── <slug>_vi.srt                    # Ready-to-publish Vietnamese SRT file
│   └── episodes/                            # Individual localized episodes (auto-split from compilation)
│       ├── ep01_<slug>_vi_16x9.mp4
│       ├── ep01_<slug>_vi_9x16.mp4
│       └── ep01_<slug>_vi.srt
├── scripts/                                 # [CODE ONLY] Project-specific Python helper scripts (.py/.sh ONLY)
├── 00_project_manifest.md                   # Project overview, episode timeline map & CI/CD checklist
├── 01_visual_audit_notes.md                 # Visual frame OCR table & acoustic phone/whisper audit log
├── 02_glossary_and_pronunciation.md         # Character roster, pronoun matrix (xưng hô), glossary & IPA rules
├── 03_pacing_and_audio_report.md            # Audio fidelity & subtitle pacing metrics
└── 04_qa_review_report.md                   # Final stream verification & visual QA sign-off table
```

---

## 3. Series Hub Architecture for Multi-Episode Projects (`series_episodic`)

When localizing a multi-episode series across multiple sessions or batches (e.g., 30 episodes of an AI drama, or Season 1 + Season 2), store the project under a **Series Hub (`workspace/<series-slug>/`)**:

```text
workspace/<series-slug>/                         # SERIES HUB ROOT (e.g., workspace/drama-quy-khu/)
├── 00_series_manifest.md                        # Series-wide episode status table & release tracker
├── config/
│   ├── series_config.json                       # Default genre, localization_mode, overlay_style, export_formats
│   └── series_bible.json                        # MASTER Character Roster, Pronoun Matrix & World Glossary
├── source_inbox/                                # Raw downloaded episodes waiting to be assigned to a unit
│   ├── ep01.mp4
│   ├── ep02.mp4
│   └── ...
├── units/                                       # Independent 6-stage workspaces per Episode or Compilation Batch
│   ├── s01-ep01-05/                             # Batch compilation of Episodes 1–5 (follows Section 2 tree)
│   ├── s01-ep06-10/                             # Batch compilation of Episodes 6–10
│   └── ep11/                                    # Single-episode workspace for Episode 11
└── deliverables/                                # Consolidated Series Release Vault (all completed units)
    ├── 16x9/
    ├── 9x16/
    ├── subtitles/
    └── episodes/
```

### Why the Shared `series_bible.json` Is Critical for Multi-Episode Series
Without `series_bible.json`, an agent working on Episode 2 might translate `陈默` as *"Trần Mặc"* with pronouns *"tôi - Đội trưởng Lâm"*, while in Episode 6 it translates the same character or title differently or changes pronouns to *"em - anh"*.
- In `series_episodic` mode, every unit's `config/series_bible.json` is linked to the Series Hub's `workspace/<series-slug>/config/series_bible.json`.
- Before Stage 2 translation begins on any new episode or batch, the agent MUST read `config/series_bible.json` and append any newly introduced characters/terms back into the shared bible.

---

## 4. Initialization Commands (`init_workspace.py`)

### Case A: Single Video (`--topology single`)
```bash
/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python \
  .agents/skills/video-localization-orchestrator/scripts/init_workspace.py \
  --topology single \
  --video "bilibili/input.mp4" \
  --slug "bilibili-anthropic-distill" \
  --genre "tech-review" \
  --mode "voice_dub" \
  --overlay-style "solid_pill" \
  --export-formats "16x9"
```

### Case B: Multi-Episode Compilation (`--topology series_compilation`)
Pass `--episodes-dir` (a folder containing `ep01.mp4`, `ep02.mp4`, ...) OR `--episodes ep01.mp4 ep02.mp4 ...`.
`init_workspace.py` will automatically copy/link the episodes into `source/episodes/`, probe and verify stream compatibility, generate `source/concat_list.txt`, concatenate them into `source/master_source.mp4`, and write `data/00_episodes_manifest.json` with exact timeline boundaries:
```bash
/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python \
  .agents/skills/video-localization-orchestrator/scripts/init_workspace.py \
  --topology series_compilation \
  --episodes-dir "downloads/quy_khu_s01" \
  --slug "drama-quy-khu-s01" \
  --genre "short-drama" \
  --mode "sub_only" \
  --overlay-style "frosted_glass" \
  --export-formats "16x9,9x16"
```

### Case C: Multi-Episode Series Hub + Working Unit (`--topology series_episodic`)
Creates/updates the Series Hub at `workspace/<series-slug>/` and initializes a working unit at `workspace/<series-slug>/units/<unit-slug>/`:
```bash
/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python \
  .agents/skills/video-localization-orchestrator/scripts/init_workspace.py \
  --topology series_episodic \
  --series-slug "drama-quy-khu" \
  --unit-slug "batch-01-ep01-05" \
  --episodes-dir "workspace/drama-quy-khu/source_inbox/batch01" \
  --genre "short-drama" \
  --mode "sub_only" \
  --overlay-style "frosted_glass" \
  --export-formats "16x9,9x16"
```

### Case D: Retrofit / Upgrade an Existing Workspace (`--sync-existing`)
If a workspace was created with an older layout, run `init_workspace.py --sync-existing --workspace workspace/<slug>` to provision `config/`, `source/`, `deliverables/`, `data/00_episodes_manifest.json`, and `config/schemas/` without overwriting completed translations or renders.

---

## 5. JSON Schema Templates (`config/schemas/`)

`init_workspace.py` writes 3 canonical JSON templates into `config/schemas/` so the agent never guesses field names:

### 5.1 `data/00_episodes_manifest.json`
```json
{
  "slug": "drama-quy-khu-s01",
  "topology": "series_compilation",
  "total_episodes": 2,
  "total_duration_s": 1009.15,
  "master_source": "source/master_source.mp4",
  "episodes": [
    {
      "ep_index": 1,
      "ep_id": "ep01",
      "title": "Part 1 (Tập 1–3)",
      "source_file": "source/episodes/ep01.mp4",
      "duration_s": 491.77,
      "timeline_start_s": 0.0,
      "timeline_end_s": 491.77,
      "timecode_start": "00:00:00.000",
      "timecode_end": "00:08:11.770",
      "width": 1920,
      "height": 1080,
      "fps": 30.0
    },
    {
      "ep_index": 2,
      "ep_id": "ep02",
      "title": "Part 2 (Tập 4–5)",
      "source_file": "source/episodes/ep02.mp4",
      "duration_s": 517.38,
      "timeline_start_s": 491.77,
      "timeline_end_s": 1009.15,
      "timecode_start": "00:08:11.770",
      "timecode_end": "00:16:49.150",
      "width": 1920,
      "height": 1080,
      "fps": 30.0
    }
  ]
}
```

### 5.2 `config/series_bible.json`
```json
{
  "series_slug": "drama-quy-khu",
  "title_zh": "诡区",
  "title_vi": "Quỷ Khu",
  "genre": "short-drama",
  "localization_mode": "sub_only",
  "overlay_style": "frosted_glass",
  "characters": [
    {
      "speaker_id": "M1",
      "name_zh": "外卖员 / 男主",
      "name_vi": "Chàng shipper (Nam chính)",
      "role": "Protagonist / Survivor",
      "voice_id": "Hải Đăng",
      "default_self_pronoun": "tôi",
      "notes": "Calm, observant under life-or-death rules"
    }
  ],
  "pronoun_matrix": [
    {
      "from_speaker": "M1",
      "to_speaker": "COMMANDER",
      "self_pronoun": "tôi",
      "address_pronoun": "Đội trưởng / Ngài",
      "register": "Formal military/investigation"
    }
  ],
  "glossary": [
    {
      "zh": "诡区",
      "vi": "Quỷ Khu",
      "tts_text": "Quỷ Khu",
      "category": "World-building"
    }
  ]
}
```

### 5.3 `data/02_bilingual_script.json` Item Schema (`config/schemas/02_bilingual_cue.template.json`)
```json
{
  "id": 0,
  "ep_id": "ep01",
  "speaker": "M1",
  "audio_type": "normal_speech",
  "raw_ids": [0],
  "start": 7.2,
  "end": 8.6,
  "zh_text": "你好 外卖到了",
  "sub_text": "Xin chào, đồ ăn giao tới rồi,",
  "tts_text": "Xin chào, đồ ăn giao tới rồi,"
}
```

---

## 6. Five Golden Storage Rules (Enforced by CI/CD Validators)

1. **Zero Stray Data in `scripts/`:** `scripts/` is strictly for `.py` and `.sh` code. Never save `.json` or `.srt` files inside `scripts/`. All JSON state files belong in `data/`.
2. **Never Write Temp Files to `/tmp` or Project Root:** All intermediate files belong inside `workspace/<slug>/media/` or `workspace/<slug>/data/`.
3. **Immutable `source/` Directory:** Once `source/master_source.mp4` and `source/episodes/*.mp4` are ingested and hashed into `.cicd/stage_0_gate.json`, never overwrite them with rendered outputs.
4. **Staging before Release:** Composite outputs always write to `media/.staging_final.mp4` first. Only `verify_stage_5.py` promotes to `media/final_<slug>_vi.mp4` and publishes hardlinks into `deliverables/16x9/`, `deliverables/9x16/`, and `deliverables/subtitles/`.
5. **Multi-Episode Post-Split (`split_compiled_episodes.py`):** When `topology == "series_compilation"` and `total_episodes >= 2`, run `split_compiled_episodes.py --workspace workspace/<slug>` after Stage 5 to populate `deliverables/episodes/epXX_<slug>_vi_16x9.mp4`, `epXX_<slug>_vi_9x16.mp4`, and re-zeroed `epXX_<slug>_vi.srt`.
