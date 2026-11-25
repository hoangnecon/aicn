# Stage 5 Staging QA & Defect Triage Manual (`v3.0.0`)

This manual defines the staging-first release lock, parallel stratified keyframe inspection protocol, stream compliance checks, and surgical defect triage for Stage 5 (`video-localization-qa-review`).

---

## 1. Staging-First Release Architecture (`media/.staging_render.mp4` → `media/.staging_final.mp4`)

To prevent premature completion claims or stale outputs from slipping past QA:
1. Stage 4 (`aicn.py render` + `aicn.py verify-render`) encodes the unit into `media/.staging_render.mp4` and signs `.cicd/stage_4_gate.json`.
2. `composite_and_verify.py` removes any stale `media/final_<unit>_vi.mp4`, hardlinks `media/.staging_render.mp4` (or `--import-existing-mp4`) to `media/.staging_final.mp4` (0 extra disk bytes), and extracts `12` stratified QA frames across 4 parallel FFmpeg workers.
3. Only `verify_stage_5.py` promotes `media/.staging_final.mp4` → `media/final_<unit>_vi.mp4` inside the unit after all 5 CI/CD checks score `100/100`.
4. Full-series deliverables are built exclusively at the Series Hub via `aicn.py merge-series --hub <hub>` once every working batch passes Stage 5.

---

## 2. Stratified Keyframe Sampling (`12` Frames in `frames/qa/`)

`composite_and_verify.py` extracts `12` evenly stratified frames (`frames/qa/frame_XXXXs.jpg`, or custom `--qa-timestamps`) spanning early, middle, and late checkpoints across the working unit:
- **Early checkpoints (`~2%–20%`):** Catch wrong aspect scaling, cropped headers, or missing top-corner watermark.
- **Middle checkpoints (`~25%–70%`):** Catch scene contrast clashes (e.g., white sky behind watermark shadow) or bitrate blocking.
- **Late checkpoints (`~75%–98%`):** Catch late-timeline corruption or A/V drift alongside `ffprobe` duration validation.

---

## 3. Mandatory Visual Inspection & Triage (`read_file` → `04_qa_review_report.md`)

Automated `ffprobe` checks verify stream technical compliance, while your visual audit via `read_file` verifies visual cleanliness:

### What to Verify on Every QA Frame (`frames/qa/frame_XXXXs.jpg`)
1. **Frame Crispness & Aspect Integrity:** Exact source resolution (`1080×1920`, `720×1280`, or `1920×1080`), sharp encoding, zero macroblocking or frame corruption.
2. **Dual-Corner Watermark Placement (when `--brand-dir` is enabled):**
   - Top-left logo (`13_top_left_logo_large.png`) and top-right badge (`15_top_right_vietsub_floating.png`) sit cleanly inside safe margins without clipping, halo artifacts, or collision.
3. **Zero Unwanted Artifacts:** Confirm no stray/duplicate overlays or corrupt alpha borders appear anywhere in the frame.

### Required Format in `04_qa_review_report.md`
Append one row per frame listed in `data/04_qa_frames_manifest.json` (no `_TBD_` and no `FAIL` entries):

```markdown
| Frame | Timestamp | Visual Sanity & Dual-Corner Watermark Check | Status |
| :--- | :--- | :--- | :--- |
| `frame_0015s.jpg` | `15.0s` | Crisp 1080x1920 portrait, top-left ĐẬU ĐỎ & top-right Vietsub badge clean | PASS |
```

### Surgical Rollback on Failure
- **Missing or unlogged frame in `04_qa_review_report.md`:** Inspect via `read_file`, log its row, re-run `verify_stage_5.py`.
- **Watermark placement / quality issue:** Adjust watermark PNG or `--codec` / `--quality`, re-run `aicn.py render`, `aicn.py verify-render`, `composite_and_verify.py`, re-inspect frames, and run `verify_stage_5.py`.
- **Audio sync or Chinese vocal bleed noticed during spot-listening:** Fix `02_bilingual_script.json` or `config/audio_mix.json`, re-run Stage 2/3, re-run Stage 4 `render` + `verify-render`, and repeat Stage 5 QA.
- **Stream duration drift (`> ±0.30s`):** Re-verify `source/master_source.mp4` FPS/PTS normalization (`Stage 0`) and re-run `aicn.py render`.
