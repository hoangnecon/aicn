# Final Composite, Computer-Vision QA & Defect Triage Manual

This manual defines the staging-first release lock, stratified keyframe sampling protocol, computer-vision pill inspection metrics, and surgical defect rollback procedures for Stage 5 (`video-localization-qa-review`).

---

## 1. Staging-First Release Architecture (Why Unverified Output Cannot Exist)

A common failure mode of autonomous agents is generating a flawed `.mp4` file, skipping visual frame verification, and immediately reporting "Done!" to the user. To make this physically impossible:
1. `composite_and_verify.py` encodes the composited video to a hidden staging path: `workspace/<slug>/media/.staging_final.mp4`.
2. If an older `media/final_<slug>_vi.mp4` exists in the workspace, `composite_and_verify.py` removes it at the start of encoding so stale outputs can never be mistaken for verified outputs.
3. Only `verify_stage_5.py` has permission to promote `media/.staging_final.mp4` to `media/final_<slug>_vi.mp4` (and `--release-path`), and it executes that promotion **only after all 5 CI/CD release checks score `100/100`**.

---

## 2. Stratified Keyframe Sampling Protocol (`>= 12` Frames Across 5 Risk Categories)

Randomly picking 3 frames misses 90% of layout bugs. `composite_and_verify.py` extracts `12–16` frames (`frames/qa/frame_XXXXs.jpg`) covering 5 mandatory risk strata:

| Risk Stratum | How Timestamps Are Selected | What Defect It Catches |
| :--- | :--- | :--- |
| **1. Widest Original Hardsubs** | Top widest `[zh_x0..zh_x1]` frames from `01_zh_bbox_scan.json` | Left/right Chinese character edges peeking outside the dark pill. |
| **2. Tallest / 2-Line Vietnamese Cues** | Longest 2-line `sub_text` cues from `04_overlay_geometry_log.json` | Vietnamese stacked diacritics (`ắ, ể, ợ`) clipping at top/bottom of pill, or 3-line overflow. |
| **3. Visual Diagram & UI Pointer Scenes** | Keyframes with on-screen tags (`<|user|>`, `<|assistant|>`, `<|end|>`, charts, code) | Audio-visual desynchronization where the subtitle talks about item A while the screen highlights item B. |
| **4. Speech-to-Pause Transitions** | Silent gap frames (e.g., `t=745s` outro/transition) | Phantom pill boxes stuck on screen when no one is speaking. |
| **5. Timeline Boundary Checkpoints** | Early (`~15–40s`), Mid (`~300–350s`), Late (`~650–740s`) | Cumulative audio/video drift (`is_vfr` or sample-rate mismatch) toward the end of the video. |

---

## 3. Dual-Layer Frame Verification (Automated Computer Vision + Agent `read_file` Audit)

Every frame in `frames/qa/frame_XXXXs.jpg` undergoes **two independent verification layers**:

### Layer A: Automated Computer-Vision Pill & Perimeter Scan (`composite_and_verify.py`)
For each extracted frame at timestamp $t$:
- If $t$ falls inside an active subtitle segment $s$ with strip coordinates `[box_x0, box_top, box_x1, box_bottom]` (offset by `strip_y0` in full-frame coordinates):
  1. **Pill Interior Presence Check:** Measures the dark background pixels inside `[box_x0+16 .. box_x1-16, y_full_top+8 .. y_full_bot-8]`. Verifies that `> 55%` of interior pixels are dark pill background (`RGB < 35`) and `> 80` pixels are crisp white Vietnamese glyphs (`RGB > 215`).
  2. **Chinese Hardsub Perimeter Leak Check:** Scans the `16px` flanking bands immediately outside the left and right edges of the pill (`[box_x0-18 .. box_x0-3]` and `[box_x1+3 .. box_x1+18]`) inside the original hardsub vertical band. If bright stroke pixels appear over a non-white background, flags a perimeter leak.

### Layer B: Mandatory Agent Visual Inspection (`read_file` -> `04_qa_review_report.md`)
Automated pixel checks verify geometry and contrast, while the agent's multimodal vision (`read_file`) verifies semantic alignment (e.g., confirming that `"và gặp thẻ End thì phải dừng xuất văn bản"` is displayed at the exact frame `frame_113s.jpg` where the red pencil points at `<|end|>`).
- The agent MUST call `read_file` on the extracted `frames/qa/*.jpg` files and log each frame's filename, hardsub coverage status (`PASS`), line balance (`PASS`), and visual cue alignment (`PASS`) in `04_qa_review_report.md`.
- `verify_stage_5.py` cross-checks `04_qa_review_report.md` against `data/04_qa_frames_manifest.json`. If any extracted frame is missing from `04_qa_review_report.md`, `verify_stage_5.py` blocks release!
