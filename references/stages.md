# Stage Commands (`v3.0.0`, Copy-Paste Reference)

Set once per shell:

```powershell
# Windows PowerShell
$SK = "<skill>"                               # folder holding SKILL.md
$V  = "$SK\vendor"
$PY = $env:AICN_PY                            # C:\Work\Tools\VieNeu-TTS\.venv\Scripts\python.exe
$U  = "workspace\<series>\units\<unit>"       # internal working batch
```

```bash
# macOS / Linux
SK=<skill>                                    # folder holding SKILL.md
V=$SK/vendor
PY=${AICN_PY:-/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python}
U=workspace/<series>/units/<unit>             # internal working batch
```

Every validator writes a relocation-safe `.cicd/stage_N_gate.json` (`schema_version: "3.0"`) on `100/100`. Downstream scripts verify upstream gate hashes automatically via `cicd_gate.py`. Tick the unit `CHECKLIST.md` box only after the gate prints `PASS`, then run `aicn.py log`.

---

## Stage 0: Series Hub & Internal Working Batch

```bash
$PY $SK/scripts/aicn.py init-series \
  --series-slug <series> \
  --title-vi "<Tên Tiếng Việt>" \
  --title-zh "<Tên Gốc ZH>" \
  --total-episodes <N> \
  --rights <basis>

# Update slug / title_vi / title_zh anytime without invalidating translations or unit gates:
$PY $SK/scripts/aicn.py update-series --hub workspace/<series> \
  [--series-slug <new-slug>] [--title-vi "<Tên VI>"] [--title-zh "<Tên ZH>"]

# Place episode batches in workspace/<series>/source_inbox/<batch>/, then initialize each unit:
$PY $V/video-localization-orchestrator/scripts/init_workspace.py \
  --topology series_episodic \
  --series-slug <series> \
  --unit-slug <unit> \
  --episodes-dir workspace/<series>/source_inbox/<batch> \
  --genre short-drama \
  --mode voice_dub

$PY $SK/scripts/aicn.py adopt --unit $U
```

Check: `data/00_episodes_manifest.json` lists each episode with `sha256` and timeline offsets (relative POSIX paths).

---

## Stage 1: Stems + Multi-Pass ASR + Acoustic Alignment (`asr-4.0`, `wf-4.0`)

### Recommended: Warm Multi-Unit Batch Prep
```bash
$PY $SK/scripts/aicn.py run-series --hub workspace/<series> --phase prep [--units unit_01,unit_02]
```
`run-series --phase prep` executes:
1. `aicn_audio.py batch-separate --units ...` (loads `htdemucs` once under `gpu_slot("demucs", vram_mb=1024)`)
2. `run_asr_and_scan.py --units ... [--workers N]` (loads Whisper `large-v3` once under `gpu_slot("whisper", vram_mb=2560)` with background CPU audio pre-decoding)
3. Parallel CPU `analyze_voice_waveform.py --snap-and-split` + `extract_audit_frames.py` across units.

### Single-Unit Commands
```bash
$PY $SK/scripts/aicn_audio.py separate --unit $U
$PY $V/video-asr-visual-audit/scripts/run_asr_and_scan.py --video $U/source/master_source.mp4 --workspace $U [--source-lang zh|en|auto] [--model large-v3] [--force]
$PY $V/video-asr-visual-audit/scripts/analyze_voice_waveform.py --workspace $U --snap-and-split
$PY $V/video-asr-visual-audit/scripts/extract_audit_frames.py --video $U/source/master_source.mp4 --workspace $U
# Mandatory: view every JPG in data/01_extracted_audit_manifest.json via read_file and log in 01_visual_audit_notes.md
$PY $V/video-asr-visual-audit/scripts/verify_stage_1.py --workspace $U
```

Outputs: `media/stems/{vocals,accomp}.wav` (`PCM_16`, globally cached in `<global_cache>/stems/`), `data/01_asr_raw_segments.json` (globally cached in `<global_cache>/asr/`), `data/01_pass2b_recovery_report.json`, `data/01_waveform_voice_audit.json`, `data/01_extracted_audit_manifest.json`.

---

## Stage 2: Dual-Layer Bilingual Script (`voice_dub`)

Write `data/02_bilingual_script.json`: one clause object per item with `id`, `ep_id`, `speaker`, `raw_ids`, `start`, `end`, `zh_text`, `sub_text`, and `tts_text` (`<en>...</en>` tags for foreign acronyms/names). Maintain `config/series_bible.json` + `02_glossary_and_pronunciation.md`.

```bash
$PY $V/zh-vi-script-adapter/scripts/validate_bilingual_script.py --workspace $U
```

Checks strict monotonic non-overlapping timing (`0.0 <= start[i] < end[i] <= start[i+1] - 0.02`, `dur >= 0.15s`), `100%` raw coverage, `0` CJK/Korean chars, `0` unwrapped alphanumeric tokens, `sea-g2p` compilation, and calibrated VieNeu duration budgets (`<= 1.26x` / `<= 1.30x` for `<= 2`-syl interjections).

---

## Stage 3: Indivisible Audio Dub, Single-STFT Mix & Master (`aicn-audio-3.0`)

### Single-Unit or Warm Multi-Unit Batch
```bash
# Single unit:
$PY $SK/scripts/aicn_audio.py all --unit $U [--force] [--allow-overflow]
# Or individual steps: separate | dub | analyze | mix | master | verify

# Warm multi-unit batch (loads Demucs / VieNeu CUDA infer_batch ONCE across units):
$PY $SK/scripts/aicn_audio.py batch-separate --units $U1,$U2 [--force]
$PY $SK/scripts/aicn_audio.py batch-dub --units $U1,$U2 [--force] [--allow-overflow]
$PY $SK/scripts/aicn_audio.py batch-all --units $U1,$U2 [--force] [--allow-overflow]
```

- `dub` groups continuous same-speaker comma clauses (`gap <= 0.12s`, `<= 3` clauses, `span <= 4.5s`), synthesizes each utterance ONCE, trims cleanly (`5ms` guard + `10ms` cosine fade), stretches ONCE intact with `rubberband -3 -F -q` (`base_tempo=1.08`, `max_tempo=1.28`, `1.32` for `<= 2`-syl) into elastic slack (`next_start - 0.05s`, `+1.20s` cap), and writes `media/tts_segments/utt_NNNN.wav` + `media/audio/dub_voice_48k.wav`.
- **Fail-fast:** Any `OVERFLOW` exits with code `2` immediately in `dub` **before `mix`**, listing the exact segment IDs and suggested syllable trims for Stage 2.
- `verify` evaluates 7 checks (`03_mix_report.json`), writes `03_pacing_and_audio_report.md`, and calls `verify_stage_3.py` (100/100). Spot-listen to at least one dubbed passage and one undubbed passage. Details: `references/audio_mix.md`.

---

## Stage 4: Hardware Dub + Watermark Render (`v3-dub-wm-3.0`)

```bash
$PY $SK/scripts/aicn.py plan --unit $U [--codec h264|hevc|h264_high] [--quality N] [--brand-dir <dir>]
$PY $SK/scripts/aicn.py render --unit $U [--codec h264|hevc|h264_high] [--quality N] [--brand-dir <dir>]
$PY $SK/scripts/aicn.py verify-render --unit $U
```

- `--codec h264` (default): `h264_nvenc -rc vbr -cq 22` (NVIDIA) / `h264_videotoolbox -q:v 58` (macOS) / `libx264 -crf 20` (CPU).
- `--codec hevc`: `hevc_nvenc -rc vbr -cq 24 -tag:v hvc1` (NVIDIA) / `hevc_videotoolbox -q:v 60 -tag:v hvc1` (macOS) / `libx265 -crf 22` (CPU).
- `--codec h264_high`: `5000k` ABR across `h264_nvenc` / `h264_videotoolbox` / `libx264`.
- `--brand-dir <dir>` (or `config/project_config.json["watermark"]`): pre-composites `13_top_left_logo_large.png` + `15_top_right_vietsub_floating.png` into a cached tight top-band RGBA PNG (`<global_cache>/wm_overlay/<wm_key>.png`) and overlays in one pass via `overlay_cuda` (NVENC CUDA) or single static `overlay` (VideoToolbox/CPU fallback). Without a watermark, `render` stream-copies video (`-c:v copy -sn`).

---

## Stage 5: Staging Hardlink, Parallel QA Extraction & Visual Verification

```bash
$PY $V/video-localization-qa-review/scripts/composite_and_verify.py \
  --video $U/source/master_source.mp4 \
  --workspace $U \
  --slug <unit>
# (--import-existing-mp4 defaults to $U/media/.staging_render.mp4; extracts 12 stratified frames with 4 workers)

# Mandatory: view EVERY frame in data/04_qa_frames_manifest.json via read_file; log PASS rows in 04_qa_review_report.md
$PY $V/video-localization-qa-review/scripts/verify_stage_5.py --workspace $U
$PY $SK/scripts/aicn.py log --unit $U --kind release --message "Stage 5 PASS -> media/final_<unit>_vi.mp4"
```

---

## Stage 6: Streaming Production, Full-Series Merge, Posters & Housekeeping

```bash
# Stream Stage-2-ready units through warm Stage 3 + lock-free parallel Stage 4/5 QA:
$PY $SK/scripts/aicn.py run-series --hub workspace/<series> --phase produce [--units unit_01,unit_02] [--codec h264|hevc] [--brand-dir <dir>]

# After ALL working batches read PPPPPP (Stage 5 PASS):
$PY $SK/scripts/aicn.py merge-series --hub workspace/<series>
# -> deliverables/final_<series>_full_vi.mp4 + deliverables/subtitles/final_<series>_full_vi.srt

# Standardize, optionally de-star, and dual-corner brand localized 9:16 / 16:9 / 3:4 posters:
$PY $SK/scripts/aicn.py brand-poster --hub workspace/<series> --input workspace/<series>/posters/<raw_file> --brand-dir <dir> [--remove-star] --delete-source

# Optional per-batch disk cleanup (keeps PPPPPP gates & global caches intact; --deep removes .staging_render.mp4 & master_audio_48k.wav):
$PY $SK/scripts/aicn.py clean --unit $U --dry-run
$PY $SK/scripts/aicn.py clean --unit $U [--deep]
```
