# Stage commands (copy-paste)

Set once per shell:

```bash
SK=<skill>                                   # folder of SKILL.md
V=$SK/vendor
PY=${AICN_PY:-/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python}
A="python3 $SK/scripts/aicn.py"
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
$A adopt --unit $U                       # CHECKLIST, CHANGELOG, overlay_style.json, .cache/
```

Check: `data/00_episodes_manifest.json` lists each episode with sha256 and timeline offsets.

## Stage 1: ASR + hardsub scan + audit (once per source)

```bash
$PY $V/video-asr-visual-audit/scripts/run_asr_and_scan.py --video $U/source/master_source.mp4 --workspace $U --color-mode auto
$PY $V/video-asr-visual-audit/scripts/analyze_voice_waveform.py --workspace $U --snap-and-split
$PY $V/video-asr-visual-audit/scripts/extract_audit_frames.py --video $U/source/master_source.mp4 --workspace $U
# view the audit frames and record findings in 01_*.md
$PY $V/video-asr-visual-audit/scripts/verify_stage_1.py --workspace $U
```

Outputs: `01_asr_raw_segments.json` (faster-whisper small, int8, multi-pass) and
`01_zh_bbox_scan.json` (5 fps, Chinese line intervals and bboxes). These are frozen;
`$A plan` reports when the source hash no longer matches them.

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

Requirements: `rubberband` (`brew install rubberband`) and `uvx` (for `demucs-mlx`).

## Stage 4: style-B overlay

```bash
$A plan --unit $U            # cached / changed:<parts> per chunk
$A render --unit $U          # dirty chunks only, then concat + mux -> media/.staging_render.mp4
$A verify-overlay --unit $U  # Stage 4 gate
```

## Stage 5: QA and release

```bash
TS=$(python3 -c "import json;g=json.load(open('$U/data/04_overlay_geometry_log.json'));print(','.join(str(round((c['slot_start']+c['slot_end'])/2,2)) for c in g['cues'][::12]))")
$PY $V/video-localization-qa-review/scripts/composite_and_verify.py --video $U/source/master_source.mp4 \
    --workspace $U --slug <unit> --import-existing-mp4 $U/media/.staging_render.mp4 --qa-timestamps "$TS"
# open every frame in data/04_qa_frames_manifest.json; append one PASS/FAIL row each to 04_qa_review_report.md
$PY $V/video-localization-qa-review/scripts/verify_stage_5.py --workspace $U
$A log --unit $U --kind release --message "Stage 5 PASS -> media/final_<unit>_vi.mp4"
```

This takes every 12th cue midpoint (about 15 frames per unit). After an edit, append the
midpoints of the edited cues to `TS`.

## Optional deliverables

`split_compiled_episodes.py --workspace $U` (per-episode files),
`render_vertical_9x16.py --workspace $U` (9:16), `apply_channel_branding.py` (logo and
credit; its `--brand-dir` default points at a local folder, so pass yours).
