# Stage commands (copy-paste)

Set once per shell:

```bash
SK=<skill>                                   # folder of SKILL.md
V=$SK/vendor
PY=${AICN_PY:-/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python}
# Windows PowerShell: $PY = $env:AICN_PY (e.g. C:\Work\Tools\VieNeu-TTS\.venv\Scripts\python.exe)
A="python $SK/scripts/aicn.py"
U=workspace/<series>/units/<unit>            # unit workspace
```

Every validator writes `.cicd/stage_N_gate.json` on 100/100. A stage's scripts refuse
to run while the previous gate is missing or its artifacts changed. Tick the unit
`CHECKLIST.md` box only after the gate passes, then `$A log`.

## Stage 0: series hub and unit

```bash
$A init-series --series-slug <series> --title "<title>" --total-episodes <N> --rights <basis>
# put the downloaded episodes in workspace/<series>/source_inbox/<batch>/
$PY $V/video-localization-orchestrator/scripts/init_workspace.py --topology series_episodic \
    --series-slug <series> --unit-slug <unit> --episodes-dir workspace/<series>/source_inbox/<batch> --genre short-drama
$A adopt --unit $U                       # CHECKLIST, CHANGELOG, .cache/
```

Check: `data/00_episodes_manifest.json` lists each episode with sha256 and timeline offsets.

## Stage 1: Stems + ASR + acoustic waveform audit (once per source)

```bash
# Separate stems first so Stage 1 ASR gets clean vocals AND Stage 3 stems are already cached
$PY $SK/scripts/aicn_audio.py separate --unit $U
$PY $V/video-asr-visual-audit/scripts/run_asr_and_scan.py --video $U/source/master_source.mp4 --workspace $U
$PY $V/video-asr-visual-audit/scripts/analyze_voice_waveform.py --workspace $U --snap-and-split
$PY $V/video-asr-visual-audit/scripts/extract_audit_frames.py --video $U/source/master_source.mp4 --workspace $U
# view the audit frames and record findings in 01_visual_audit_notes.md
$PY $V/video-asr-visual-audit/scripts/verify_stage_1.py --workspace $U
```

Or run the non-interactive prep across all batches of a series at once:
```bash
$A run-series --hub workspace/<series> --phase prep
```

Outputs: `media/stems/{vocals,accomp}.wav`, `01_asr_raw_segments.json` (MLX Whisper on Metal on macOS,
`faster-whisper` CUDA `float16` on NVIDIA, or CPU `int8`; `AICN_ASR=mlx|faster-cuda|faster-cpu` overrides), and
`01_waveform_voice_audit.json`. These are frozen; `$A plan` reports when the source hash changes.

## Stage 2: bilingual script

Write `data/02_bilingual_script.json`: one item per clause with `zh`, `vi`, `sub_text`
(screen) and `tts_text` (voice). Follow `vendor/zh-vi-script-adapter/SKILL.md` for
register, pronouns and glossary.

```bash
$PY $V/zh-vi-script-adapter/scripts/validate_bilingual_script.py --workspace $U
```

## Stage 3: audio

```bash
$PY $SK/scripts/aicn_audio.py all --unit $U
# or step by step: separate | dub | mix | master | verify
```

`dub` prints `OVERFLOW` for any sentence that does not fit at 1.25×. Shorten its `tts_text`
in Stage 2, re-validate, and run `dub` again: only that sentence is re-synthesized.
`verify` writes `data/03_mix_report.json` and signs Stage 3 only when every audio check passes.
Then listen to one dubbed passage and one passage without dub.
Details and tuning: `references/audio_mix.md`.

Requirements: `rubberband` (`brew install rubberband` on macOS, `install.ps1 -InstallTools` on Windows) and `uvx` (for `demucs-mlx` on macOS) or `demucs` in `$AICN_PY` (PyTorch CUDA/CPU on Windows/Linux).

## Stage 4: dub + watermark render (v2.0.0)

```bash
$A plan --unit $U [--codec h264|hevc|h264_high] [--quality N] [--brand-dir <dir>]
$A render --unit $U [--codec h264|hevc|h264_high] [--quality N] [--brand-dir <dir>]
$A verify-render --unit $U  # Stage 4 gate (alias: verify-overlay)
```

- `--codec h264` (default): `h264_videotoolbox -q:v 58` (macOS) / `h264_nvenc -rc vbr -cq 22` (NVIDIA) / `libx264 -crf 20` (CPU).
- `--codec hevc`: `hevc_videotoolbox -q:v 60 -tag:v hvc1` (macOS) / `hevc_nvenc -rc vbr -cq 24 -tag:v hvc1` (NVIDIA) / `libx265 -crf 22` (CPU).
- `--codec h264_high`: legacy `5000k` ABR across `h264_videotoolbox` / `h264_nvenc` / `libx264`.
- `--brand-dir <dir>` (or `config/project_config.json["watermark"]`): composites dual-corner PNGs (`13_top_left_logo_large.png`, `15_top_right_vietsub_floating.png`) inside a single FFmpeg pass. When no watermark is configured, `render` stream-copies video (`-c:v copy -sn`).

## Stage 5: QA and release

```bash
$PY $V/video-localization-qa-review/scripts/composite_and_verify.py --video $U/source/master_source.mp4 \
    --workspace $U --slug <unit> --import-existing-mp4 $U/media/.staging_render.mp4
# open every frame in data/04_qa_frames_manifest.json; append one PASS/FAIL row each to 04_qa_review_report.md
$PY $V/video-localization-qa-review/scripts/verify_stage_5.py --workspace $U
$A log --unit $U --kind release --message "Stage 5 PASS -> media/final_<unit>_vi.mp4"
```

This extracts 12 stratified QA frames across the batch for watermark/visual sanity inspection.

## Stage 6: Full-Series Pipeline & Release (mandatory — 1 published video = 1 FULL series)

```bash
# Hardware-overlapped full-series production after Stage 2 passes on batches (GPU ∥ CPU ∥ Hardware Encoder)
$A run-series --hub workspace/<series> --phase produce [--codec h264|hevc] [--brand-dir <dir>]

# After visually verifying Stage 5 QA on EVERY working batch (all batches Stage 5 = P):
$A merge-series --hub workspace/<series>
# -> deliverables/final_<series>_full_vi.mp4 + deliverables/subtitles/final_<series>_full_vi.srt

# Optional per-batch housekeeping (~1.2 GB freed/batch, keeps PPPPPP gates intact; --deep frees ~1.8 GB/batch)
$A clean --unit $U --dry-run
$A clean --unit $U
```

Other optional scripts: `split_compiled_episodes.py --workspace $U`, `render_vertical_9x16.py --workspace $U`.
