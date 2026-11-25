---
name: aicn-produce
description: Use when synthesizing Vietnamese dub audio with VieNeu CUDA, mixing vocals and BGM, rendering dual-corner watermarked video, verifying Stage 5 QA frames, merging units into the full-series master MP4/SRT, or packaging branded posters and upload captions ("chạy lồng tiếng", "render xuất bản", "ghép full bộ", "làm poster và caption").
---

# Producing Audio, Watermark Render & Full-Series Release (`/aicn-produce`)

## Overview
Execute **Stage 3 → Stage 6** using VRAM-pooled warm CUDA batching (`VieNeu v3 Turbo` `infer_batch(16)`), indivisible utterance pacing (`rubberband` R3), single-STFT `follow_hold` vocal-carved mixing (`-16 LUFS`), lock-free hardware rendering (`h264_nvenc`/`hevc_nvenc` `overlay_cuda` or `videotoolbox`), visual Stage 5 QA, and full-series stream-copy release (`merge-series` + `brand-poster`).

- `<aicn>`: `$AICN_HOME` if set, else `~/.gemini/config/plugins/aicn` (`../..` from this skill).
- `$AICN_PY`: `C:\Work\Tools\VieNeu-TTS\.venv\Scripts\python.exe` (Windows) or `/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python` (macOS).
- Deep references: `<aicn>/references/audio_dsp_and_pacing_manual.md`, `<aicn>/references/channel_branding_and_watermark_guide.md`, `<aicn>/references/poster_and_caption_guide.md`.

## Mandatory Recipe

1. **Run warm batch audio + hardware watermark render + QA frame extraction (Stages 3–5A):**
   Stream units as soon as they pass Stage 2 (`PPP---`) without waiting for all batches:
   ```bash
   $AICN_PY <aicn>/scripts/aicn.py run-series \
     --hub <hub> \
     --phase produce \
     --brand-dir <project-root>/workspace/branding-dau-do
   ```
   *(Pass `--units <u1>,<u2>` to produce specific batches.)*
   - **If Stage 3 fails fast on utterance overflow:** NEVER pass `--allow-overflow` for production. Shorten the overflowing `tts_text` cues listed in console/`03_dub_sentences.json`, re-run `validate_bilingual_script.py`, and re-run `produce` (`100%` cache hit on unchanged utterances).

2. **Visually inspect EVERY batch's `frames/qa/*.jpg` via `read_file` & sign Gate 5:**
   Open the extracted QA frames listed in `<unit>/data/04_qa_frames_manifest.json` with `read_file`. Confirm dual-corner channel branding (`13_top_left_logo_large.png` + `15_top_right_vietsub_floating.png`), aspect integrity, and zero visual corruption. Log each frame `PASS` in `<unit>/04_qa_review_report.md`, then promote `.staging_final.mp4 -> media/final_<batch>_vi.mp4`:
   ```bash
   $AICN_PY <aicn>/scripts/verify_stage_5.py --workspace <unit>
   ```

3. **Merge ALL verified batches into THE single full-series master (Stage 6):**
   Once every batch under `<hub>/units/` shows `PPPPPP`:
   ```bash
   $AICN_PY <aicn>/scripts/aicn.py merge-series --hub <hub>
   ```
   Verify `deliverables/final_<slug>_full_vi.mp4` and `deliverables/subtitles/final_<slug>_full_vi.srt` exist with A/V drift `<= 0.50s`.

4. **Package branded `9:16` / `16:9` poster & 2-line upload caption:**
   ```bash
   $AICN_PY <aicn>/scripts/aicn.py brand-poster \
     --hub <hub> \
     --input <raw_localized_poster> \
     --brand-dir <project-root>/workspace/branding-dau-do \
     --remove-star --delete-source
   ```
   Open `posters/poster_<slug>_vi_*_branded.png` via `read_file` to verify corner logos and Vietnamese typography, and output the 2-line caption per `<aicn>/references/poster_and_caption_guide.md`.

## Common Mistakes
- **Uploading individual `units/*` MP4s:** Strictly forbidden. Always run `merge-series` for the single full-series master.
- **Signing Stage 5 without opening `frames/qa/*.jpg` via `read_file`:** Blind QA misses clipped logos or wrong aspect overlays.
