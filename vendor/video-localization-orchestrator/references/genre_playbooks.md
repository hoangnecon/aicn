# Generalized Video Localization Playbooks by Genre & Format

This reference manual defines the exact parameter matrices, voice personas, subtitle geometries, and audio mixing curves across different video genres and aspect ratios.

---

## 1. Genre Configuration Matrix

| Parameter | `tech-review` (Bilibili / YouTube Tech, AI, Hardware) | `short-drama` (Multi-Character AI Sci-Fi / Thriller / Drama) | `documentary` (Finance, History, Deep Essays) | `tutorial` (Coding, Software UI Walkthroughs) |
| :--- | :--- | :--- | :--- | :--- |
| **Default Localization Mode (`--mode`)** | `voice_dub` (Vietnamese TTS narration + BGM ducking) | `sub_only` (Keep original voice acting/SFX + full ViSub; `voice_dub` optional) | `voice_dub` (or `sub_only` for archival interviews) | `voice_dub` |
| **Default Overlay Style (`--overlay-style`)** | `frosted_glass` or `solid_pill` | `frosted_glass` (sleek compact translucent glass blur) | `frosted_glass` | `frosted_glass` |
| **Target Clause Duration** | `1.5s – 3.5s` | `0.8s – 2.5s` (fast dialogue turns & phone calls) | `2.0s – 4.0s` (measured cadence) | `1.2s – 3.0s` (locked to mouse clicks) |
| **Target Syllable Rate** | `4.5 – 5.2 syl/s` (max `6.2`) | `4.0 – 6.2 syl/s` (max `7.8` in `sub_only` fast arguments) | `4.0 – 4.8 syl/s` (max `5.6`) | `4.5 – 5.4 syl/s` (max `6.2`) |
| **Max Tempo Ratio (`atempo`)** | `<= 1.24x` (median `~1.05x–1.14x`) | `<= 1.18x` (emotional pitch sensitive) | `<= 1.16x` (preserve gravitas) | `<= 1.24x` |
| **Inter-Clause Breathing Pause** | `0.18s – 0.28s` (`mean ~0.22s`) | `0.10s – 0.25s` (tight turn-taking) | `0.25s – 0.45s` (dramatic pauses) | `0.15s – 0.25s` |
| **Default `VieNeu-TTS` Voice** | `Hải Đăng` (clear, analytical male) | Multi-voice mapping (`speaker_id` -> `Hải Đăng`, `Ngọc`, `Bình`, `Tuyên`) | `Hải Đăng` or `Bình` | `Hải Đăng` |
| **BGM Gain (Active Speech)** | `0.28` (`-11 dB` ducking) | `0.35` (preserve dramatic tension & SFX) | `0.25` | `0.18` (ultra-clear instruction) |
| **BGM Gain (Pauses / Swells)** | `0.65` | `0.80` (full dramatic stingers) | `0.70` | `0.40` |
| **Max Subtitle Lines** | `2` balanced lines (`<= 84 chars`) | `2` short lines (`<= 44 chars` total for 9:16) | `2` balanced lines (`<= 84 chars`) | `1–2` compact lines (`<= 76 chars`) |

---

## 2. Aspect Ratio & Resolution Geometry

### A. Landscape `16:9` (`1920x1080` or `3840x2160`)
- **Hardsub Scan Band:** `y = [880..1080]` (`strip_y0 = 880`, `strip_h = 200` on 1080p; scale by `2x` for 4K).
- **Horizontal Search Window:** `x = [250..1670]` (centered at `X = 960`).
- **Typography:** `Arial Bold` `36pt`, `line_h = 42px`, `line_gap = 8px`, `max_px = 1150px` per line.
- **Pill Padding:** `+44px` horizontal padding around Vietnamese text, `+28px` horizontal and `+14px` vertical padding beyond the widest detected original hardsub pixel in that time slot.

### B. Vertical Short-Form `9:16` (`1080x1920` Douyin / TikTok / Reels / Shorts)
- **Why 9:16 is different:** The bottom `280px` (`y = [1640..1920]`) is covered by platform UI (caption, audio marquee, progress bar), and the right `140px` (`x = [940..1080]`) is covered by Like/Comment/Share buttons.
- **Hardsub Scan Band:** Typically `y = [1280..1640]` (`strip_y0 = 1280`, `strip_h = 360`), though some creators place hardsubs at `y = [1150..1500]`. Always run `scan_hardsub_bbox.py` with `--auto-band` on vertical videos first to locate the vertical peak of white/yellow stroke text.
- **Horizontal Safe Zone:** Center at `X = 540`, maximum pill width `<= 920px` (`x = [80..1000]`), maximum line width `max_px = 780px` (`<= 22–26 characters per line`).
- **Typography:** `Arial Bold` `42pt`, `line_h = 50px`, `line_gap = 10px`.

---

## 3. Handling Edge-Case Source Videos

### Case 1: Variable Frame Rate (VFR) Videos (`is_vfr == True`)
Screen recordings and mobile uploads often use Variable Frame Rate (`r_frame_rate = 60/1` while `avg_frame_rate = 28.4/1`). Overlaying a Constant Frame Rate (CFR) `10fps` `.mov` onto a VFR video can drift by `0.5s–2.0s` over 10 minutes.
- **Mandatory Fix in Stage 0/1:** Normalize the source video to `30fps` CFR before running Stage 1:
  ```bash
  ffmpeg -y -i raw_vfr.mp4 -vf "fps=30" -c:v h264_videotoolbox -b:v 6000k -c:a copy normalized_cfr.mp4
  ```

### Case 2: Colored or Outlined Hardsubs (Yellow, Cyan, Black-on-White)
Not all original hardsubs are pure white (`RGB > 238`).
- **White with dark stroke (Bilibili standard):** `(R > 238) & (G > 238) & (B > 238)`.
- **Bright Yellow hardsubs (Douyin dramas / variety shows):** `(R > 220) & (G > 190) & (B < 90)`.
- **High-Contrast Dark Stroke Detection:** When background paper/slides are also white (e.g., white document slides at `t=18s` or `t=655s`), pure white thresholding can pick up the white paper background. Use **stroke-pair detection**: a valid subtitle character has bright interior pixels (`L > 230`) immediately adjacent (within `3px`) to dark outline/shadow pixels (`L < 55`). `run_asr_and_scan.py` includes this stroke-aware filter so white paper backgrounds never trigger false full-width boxes.

### Case 3: Multi-Speaker Videos (Interviews, Dialogues, Short Dramas)
When a video has 2+ speakers:
1. Add `"speaker": "M1" | "F1" | "M2"` to each segment in `data/02_bilingual_script.json`.
2. Map each speaker ID to a distinct `VieNeu-TTS` preset voice in `synthesize_and_audit_pacing.py` (e.g., `M1 -> "Hải Đăng"`, `F1 -> "Ngọc"`, `M2 -> "Bình"`).
3. At speaker changes (`seg[i].speaker != seg[i-1].speaker`), enforce a minimum `0.12s` visual/audio separation so voices never overlap unnaturally unless intentional.

### Case 4: Videos Without Burned-In Hardsubs (Clean Feed or Soft-Sub Only)
If `01_zh_bbox_scan.json` detects `< 5%` of frames with burned-in bottom text:
- The pill width no longer needs to mask underlying Chinese characters (`half_span_zh = 0`).
- Size the pill purely to `half_span_vi = max_text_w // 2 + 44` with a slightly softer background opacity (`RGBA = (14, 16, 22, 215)`) since no anti-ghosting mask is required.
