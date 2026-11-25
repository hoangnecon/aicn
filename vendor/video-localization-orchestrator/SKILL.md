---
name: video-localization-orchestrator
description: Use when localizing, dubbing, re-upping, or translating foreign videos (especially Chinese Hongguo/Douyin/Bilibili or English short dramas) into Vietnamese, or when coordinating multi-stage subtitle, translation, voice synthesis, and watermark rendering workflows.
---

# Video Localization Orchestrator & CI/CD Gatekeeper (`v3.0.0`)

## Overview

Master supervisor and relocation-safe CI/CD gatekeeper for end-to-end Vietnamese video localization (`voice_dub`-only). Coordinates the 5-stage unit pipeline (`Stage 0` to `Stage 5`) plus Series Hub orchestration (`Stage 6` via `<aicn>/scripts/aicn.py`), where every stage passes a deterministic Python validator (`100/100` score) and atomically writes a relocation-safe SHA-256 signed gate token (`<unit>/.cicd/stage_N_gate.json`, `schema_version: "3.0"`).

**Violating the letter of the rules is violating the spirit of the rules. No stage can produce output unless its upstream CI/CD gate token is verified.**

---

## Core Capabilities (`v3.0.0`)

### 1. Multi-Format Video Profiling & CFR/PTS Normalization
Probes source media via `ffprobe` to classify resolution/aspect ratio (`16:9` landscape or `9:16` portrait such as `1080×1920` / `720×1280`), frame rate stability (`r_frame_rate` vs `avg_frame_rate`), stream indices, and timescales, normalizing mixed-FPS/stream-order batches with `setpts=PTS-STARTPTS` / `-avoid_negative_ts make_zero` into `source/master_source.mp4` (`data/00_video_profile.json`, `data/00_episodes_manifest.json`).

### 2. Mandatory `voice_dub` Contract (Full Dubbing + Dual-Corner Watermark + External `.srt`)
Every published series MUST be localized in **`voice_dub`** mode (48 kHz Vietnamese VieNeu-TTS dubbing mixed over Demucs stems + dual-corner channel watermark + external `.srt`).
- `init_workspace.py` enforces `--mode voice_dub` across all genres (`short-drama`, `tech-review`, `documentary`, `tutorial`) and rejects any non-dubbing mode so every series ships with full Vietnamese dubbing.
- Stage 4 produces clean video with optional dual-corner branding (`--brand-dir`) and `-sn` (zero burned-in subtitle overlays).

### 3. Relocation-Safe Workspace Initialization (`init_workspace.py`)
Provisions clean zero-clutter workspaces across topologies (`series_episodic` [default AICN hub + internal working batches], `series_compilation`, `single`):
- **Series Hub (`workspace/<series-slug>/`):** Creates `config/{rights.json,series_bible.json,series_config.json}`, `posters/`, `source_inbox/`, `units/`, `deliverables/subtitles/`, `.cache/`, `CHANGELOG.md`, `PROGRESS.md`, `00_series_manifest.md`.
- **Internal Working Batch (`units/<unit-slug>/`):** Creates `.cicd/`, `config/schemas/`, `source/episodes/`, `data/`, `media/{stems,tts_segments,audio}/`, `frames/{audit,qa}/`, `scripts/`. Never creates per-unit `deliverables/` or stale intermediate folders.
- **Relative POSIX Paths Everywhere:** `00_video_profile.json` writes `"source_video": "source/master_source.mp4"`; `00_episodes_manifest.json` uses POSIX `/` separators; `project_config.json` stores `"series_hub_rel": "../.."` resolved dynamically via `aicn.resolve_unit_hub()`.
- **`series_bible.json` Decoupled:** Stage 0 signs only source media and core unit specs, so Stage 2 glossary/character updates never invalidate Stage 0.

### 4. Relocation-Safe Cryptographic CI/CD Gates (`cicd_gate.py`, `schema_version: "3.0"`)
- Computes file hashes via `aicn_platform.content_sha256(path)` backed by SQLite-WAL `<global_cache>/content_hash.db` (zero path dependence).
- Signed payload uses `"workspace_slug"` (no absolute `"workspace"` path) and workspace-relative POSIX artifact keys, written atomically via `ap.atomic_write_json`.
- Moving or renaming a unit or series hub keeps 100% of upstream gate tokens and global caches valid!

### 5. VRAM-Aware GPU Pool & Warm Multi-Unit Orchestration (`aicn_platform.py`, `aicn.py`)
- Single-lock serialization is eliminated: `ap.gpu_slot(task, vram_mb)` allocates OS-advisory-locked 512 MB VRAM slots (`whisper=2560 MB`, `demucs=1024 MB`, `vieneu=1024 MB`) so compatible GPU stages run concurrently while crashes auto-release slots immediately.
- `aicn.py run-series --hub <hub> --phase prep|produce [--units u1,u2,...]` keeps Demucs, Whisper, and VieNeu CUDA warm across multiple units and runs NVENC/VideoToolbox Stage 4 renders + QA extraction lock-free in parallel (`AICN_MAX_RENDER_JOBS=2`).

### 6. Dual-Corner Channel Branding (`--brand-dir` + `aicn.py brand-poster`)
- Standard dual-corner assets live in `workspace/branding-<channel>/`: `13_top_left_logo_large.png` (horizontal mascot + 3D wordmark) and `15_top_right_vietsub_floating.png` (`"Vietsub by <Channel>"` + red seal).
- Stage 4 (`aicn.py render --brand-dir ...`) pre-composites both PNGs ONCE into `<global_cache>/wm_overlay/<wm_key>.png` and overlays via `overlay_cuda` (NVENC) or single static `overlay` (VideoToolbox/CPU), scaling proportionally on narrow/portrait widths (`< 1200`).
- Series posters (`9:16`, `16:9`, `3:4`) are standardized and branded via `aicn.py brand-poster`. See [channel_branding_and_watermark_guide.md](./references/channel_branding_and_watermark_guide.md).

---

## Master Pipeline Architecture

```text
Stage 0 (init-series + init_workspace.py + adopt) -> .cicd/stage_0_gate.json
   -> Stage 1 (video-asr-visual-audit: stems + asr-4.0 + wf-4.0 + verify_stage_1.py) -> stage_1_gate.json
   -> Stage 2 (zh-vi-script-adapter: 02_bilingual_script.json + validate_bilingual_script.py) -> stage_2_gate.json
   -> Stage 3 (vieneu-voice-pacing-mixer: aicn_audio.py all/batch-all + verify_stage_3.py) -> stage_3_gate.json
   -> Stage 4 (aicn.py render + verify-render) -> stage_4_gate.json
   -> Stage 5 (video-localization-qa-review: composite_and_verify.py + visual QA + verify_stage_5.py) -> media/final_<unit>_vi.mp4
   -> Stage 6 (aicn.py merge-series --hub <hub>) -> deliverables/final_<series>_full_vi.mp4 + .srt
```

1. **Stage 0 (Init & Profile):** `aicn.py init-series` + `init_workspace.py` + `aicn.py adopt`.
2. **Stage 1 (Stems, Multi-Pass ASR & Visual Audit):** **REQUIRED SUB-SKILL:** `video-asr-visual-audit`
3. **Stage 2 (Dual-Layer Script & Budget Validation):** **REQUIRED SUB-SKILL:** `zh-vi-script-adapter`
4. **Stage 3 (Indivisible Voice Synthesis & Single-STFT Mix):** **REQUIRED SUB-SKILL:** `vieneu-voice-pacing-mixer`
5. **Stage 4 (Hardware Dub + Watermark Render):** `aicn.py render` + `aicn.py verify-render`
6. **Stage 5 (Staging Hardlink, QA Frame Inspection & Unit Promotion):** **REQUIRED SUB-SKILL:** `video-localization-qa-review`
7. **Stage 6 (Full-Series Merge & Poster Packaging):** `aicn.py merge-series` + `aicn.py brand-poster`

---

## Commands Reference

Always execute Python scripts with `$AICN_PY` (`C:\Work\Tools\VieNeu-TTS\.venv\Scripts\python.exe` on Windows, `/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python` on macOS).

### 1A. Initialize a Series Hub + Internal Working Batch (`series_episodic`)
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

$AICN_PY <aicn>/scripts/aicn.py adopt --unit "workspace/nam-18-tuoi-toi-che-tao-than-khi/units/unit_01_ep01_10"
```

### 1B. Retrofit / Sync an Existing Workspace (`--sync-existing`)
```bash
$AICN_PY <aicn>/vendor/video-localization-orchestrator/scripts/init_workspace.py \
  --sync-existing \
  --workspace "workspace/<series>/units/<unit>"
```

### 2. Inspect or Enforce CI/CD Pipeline Gate Status
```bash
$AICN_PY <aicn>/vendor/video-localization-orchestrator/scripts/cicd_gate.py \
  --workspace "workspace/<series>/units/<unit>" [--require-stage 2]
```

---

## CI/CD Stage Gatekeeping Matrix (`100/100` Required at Every Gate)

| Gate | Validator Script | Mandatory Programmatic Checks (`100/100`) |
| :--- | :--- | :--- |
| **Stage 0** | `init_workspace.py` | Source video readable (`duration > 1s`), audio stream present, clean tree + `00..04.md` initialized, `data/00_video_profile.json` (`localization_mode: "voice_dub"`), relative POSIX paths. |
| **Stage 1** | `verify_stage_1.py` | `stems_ready` (20 pts: `media/stems/{vocals,accomp}.wav`), `asr_monotonic_and_clean` (25 pts: `median_dur <= 2.8s`, `0` overlaps/inversions, `0` `<0.15s` runts, `0` boundary duplicates, `0` empty texts), `acoustic_waveform_voice_coverage` (30 pts: `>= 90%`, `bloated == 0`, `premature_lead == 0`, `longest_uncovered_vocal_gap_s <= 6.0s`), `visual_audit_frames_inspected_and_logged` (25 pts: `>= 10` frames logged in `01_visual_audit_notes.md`). |
| **Stage 2** | `validate_bilingual_script.py` | Strict `0 <= start[i] < end[i] <= start[i+1] - 0.02` & `dur >= 0.15s`, `100%` raw coverage & `>= 55%` clause granularity, `0` CJK/Korean chars & `0` `,,`, `0` unwrapped mixed tokens outside `<en>`, `sub_text <= 96`, `mean_syl <= 5.6`, `max_syl <= 6.8`, calibrated VieNeu duration fit (`<= 1.26x` / `<= 1.30x`), `100%` `sea-g2p` compilation. |
| **Stage 3** | `verify_stage_3.py` | Stage 2 gate PASS, `03_mix_report.json` all 7 checks PASS (`-14 ± 1 LUFS`, peak `<= -0.8 dBFS`, active `dub_snr_speech_db_median >= 10 dB` & `p10 >= 4 dB`, intact `media/tts_segments/utt_NNNN.wav >= 0.12s`, `0` overlaps, sync `[-0.10, +0.50]s`), 48 kHz stereo `master_audio_48k.wav`, `N`-block `subtitles_full.srt`, populated `03_pacing_and_audio_report.md`. |
| **Stage 4** | `aicn.py verify-render` | Stage 3 gate PASS, `.staging_render.mp4` valid (`h264`/`hevc`, `48000 Hz` stereo `aac`, `0` subtitle streams `-sn`), `av_sync_and_frame_integrity` (`±0.30s` duration tol & exact source frame count). |
| **Stage 5** | `verify_stage_5.py` | Stages 0–4 gates PASS, `media/.staging_final.mp4` (`h264`/`hevc`, `48000 Hz` stereo `aac`, `0` subtitle streams, `±0.30s` dur), `>= 10` QA frames intact in `04_qa_frames_manifest.json`, `100%` logged `PASS` in `04_qa_review_report.md` → promotes `.staging_final.mp4` to `media/final_<unit>_vi.mp4`. |

---

## Surgical Rollback Matrix (When a Gate Fails)

| Gate Failure Symptom | Root Stage | Surgical Fix Action |
| :--- | :--- | :--- |
| `Stage 1 FAIL: Unlogged audit frames` | Stage 1 | Call `read_file` on missing `frames/audit/*.jpg`, add rows in `01_visual_audit_notes.md`, re-run `verify_stage_1.py`. |
| `Stage 1 FAIL: Overlap or boundary duplicate` | Stage 1 | Re-run `run_asr_and_scan.py --force` (`asr-4.0`) + `analyze_voice_waveform.py --snap-and-split` (`wf-4.0`), inspect, re-run `verify_stage_1.py`. |
| `Stage 2 FAIL: Mixed token or duration budget` | Stage 2 | Edit only the flagged IDs in `02_bilingual_script.json` (`<en>...</en>` or shorten phrasing), re-run `validate_bilingual_script.py`. |
| `Stage 3 FAIL: OVERFLOW in cmd_dub` | Stage 2 → 3 | `cmd_dub` exits `2` before `mix`. Trim `tts_text` of the listed IDs in `02_bilingual_script.json`, re-run `validate_bilingual_script.py`, re-run `aicn_audio.py all --unit $U`. |
| `Stage 5 FAIL: Unlogged QA frame` | Stage 5 | Call `read_file` on `frames/qa/frame_*s.jpg`, append `PASS` row to `04_qa_review_report.md`, re-run `verify_stage_5.py`. |

---

## Rationalization Table (Anti-Laziness Controls)

| Agent Excuse | Reality & CI/CD Countermeasure |
| :--- | :--- |
| "I'll mark Markdown checkboxes `[x]` myself" | Checkboxes do nothing. Scripts verify cryptographic SHA-256 tokens in `.cicd/stage_N_gate.json`. |
| "I inspected 2 QA frames; good enough" | `verify_stage_5.py` checks `04_qa_frames_manifest.json` and blocks promotion unless all frames are logged `PASS`. |
| "I edited `02_bilingual_script.json` after Stage 2 passed; no need to re-validate" | `cicd_gate.py` hashes `02_bilingual_script.json` inside `stage_2_gate.json`; unvalidated edits immediately block Stage 3. |
| "I'll skip Demucs and mix dub over raw audio, or ship subtitle-only" | Both are forbidden. `verify_stage_1.py` and `verify_stage_3.py` require `media/stems/{vocals,accomp}.wav` and `voice_dub` master compliance. |
