# Channel Branding, Logo Pack Extraction & 16:9 Dual-Corner Watermark Guide

This reference manual defines the complete workflow for generating channel brand assets, extracting zero-halo transparent RGBA logos from a `2x2` grid, generating `16:9` texture-gradient cover banners, and burning **in-frame dual-corner watermarks & attribution text (`Vietsub by <Channel>`)** into `16:9` (`1920x1080`) localized videos.

---

## 1. Canonical 3-Tier Storage Hierarchy (Project Branding vs. Series vs. Episode Deliverables)

Never mix project-wide channel logos/overlays, series-wide posters, and per-episode video deliverables in the same folder:

1. **Tier 1 — Project / Channel Branding (`workspace/branding-<channel>/`):**
   - Belongs to the entire channel/project and is shared across all series:
     - `logos/`: Master `2x2` grid (`00`), extracted transparent RGBA logos (`01`, `02`, `04`, `06`), and dark-background verification sheet (`99`).
     - `overlays/`: Seals (`03`, `03b`), horizontal lockup (`05`), Top-Left Large Logo (`13_top_left_logo_large.png`), and Top-Right Attribution Overlays (`14` Pill, `15` Floating, `18` 3D Wordmark).
     - `covers/`: `16:9` (`2560x1440` 2K) channel banners (`07..09`).
2. **Tier 2 — Series Folder (`workspace/<series_slug>/posters/`):**
   - Belongs to a specific **Series (`Bộ phim`)** as a whole (NOT an individual episode):
     - `posters/official_poster_<series_slug>_zh.png` (Original Chinese poster)
     - `posters/poster_<series_slug>_vi_9x16_clean.png` & `poster_<series_slug>_vi_9x16_branded.png` (`1080x1920` vertical series posters with Gemini `✦` star removed)
     - `posters/poster_<series_slug>_vi_16x9_clean.png` & `poster_<series_slug>_vi_16x9_branded.png` (`1920x1080` horizontal series posters/thumbnails with Gemini `✦` star removed)
     - `posters/poster_<series_slug>_vi_3x4_clean.png` (`3:4` native poster ratio)
3. **Tier 3 — Episode / Unit Deliverables (`workspace/<series_slug>/deliverables/`):**
   - Contains ONLY rendered video/subtitle deliverables (`deliverables/16x9/final_<slug>_vi.mp4`, `deliverables/subtitles/<slug>_vi.srt`, `deliverables/episodes/`).
   - **Standard Deliverable Format:** Always deliver widescreen `16:9` (`1920x1080`, `SAR 1:1`, `DAR 16:9`) at `deliverables/16x9/final_<slug>_vi.mp4`. Do NOT default to `9:16` video cropping unless explicitly requested.

---

## 2. Generating & Slicing the Channel Logo Pack (`2x2` Master Grid Strategy)

When creating a new channel identity (e.g., **"Đậu Đỏ"** / `红豆`), generate all core brand elements in a single **2x2 Master Brand Grid (`1:1`, 2K resolution)** using **Nano Banana 2.1** (`image_model: "nano_banana_2"`) on Google Flow so color palette, character proportions, and gold/crimson material shaders stay 100% consistent across all assets.

### 2.1. The 4-Panel `2x2` Grid Layout
Prompt the image generator for a clean `2x2` layout on a **pure solid flat white background (`#FFFFFF`)** with generous white margins around each element and no drop shadows touching the panel edges:

| Quadrant | Asset Role | Output Filename (`workspace/branding-<channel>/`) | Description |
| :--- | :--- | :--- | :--- |
| **Top-Left (`Q1`)** | **Pure Mascot Icon (No Text)** | `01_logo_icon_transparent.png` | Standalone mascot/icon (e.g., glossy ruby-red bean holding a mini clapperboard with a golden Chinese cloud sprout). |
| **Top-Right (`Q2`)** | **Stacked Logo + 3D Wordmark** | `02_logo_with_text_transparent.png` | Mascot icon stacked above bold 3D display typography (`"ĐẬU ĐỎ"`). Used to automatically separate the standalone wordmark (`06`) and horizontal lockup (`05`). |
| **Bottom-Left (`Q3`)** | **Traditional Seal / Stamp (`印章`)** | `03_watermark_seal_transparent.png` & `03b_watermark_seal_solid_transparent.png` | Square Chinese red ink seal combining the channel motif, cinema reel, and auspicious clouds. Produce both a **hollow negative-space version (`03`)** and a **solid cream-interior version (`03b`)**. |
| **Bottom-Right (`Q4`)** | **Circular Channel Avatar** | `04_avatar_emblem_transparent.png` | Circular emblem framed in metallic gold (e.g., mascot wearing round gold glasses holding popcorn) for TikTok / YouTube / Facebook circular profile pictures. |

### 2.2. Derived Lockup Assets (Auto-Assembled from `Q1` + `Q2`)
Using connected-component labeling (`scipy.ndimage.label`) on the alpha channel of `02_logo_with_text_transparent.png`, isolate the bottom text components (`centroid_y > threshold`) to produce:
- **`06_typography_only_transparent.png`:** Standalone 3D `"ĐẬU ĐỎ"` wordmark with all internal counter-holes (`Đ, Ậ, Ỏ, A, O, P, R, B`) cleanly knocked out.
- **`05_watermark_horizontal_lockup_transparent.png`:** Side-by-side horizontal lockup (`01_logo_icon` on the left + `06_typography_only` vertically centered on the right). This is the **primary top-left corner video watermark**.

### 2.3. Zero-Halo RGBA Extraction & RGB Edge Color Decontamination
Never use naive global white thresholding (`RGB > 240`), which leaves a jagged white halo over dark movie scenes and punches holes inside intentional white highlights (eyes, clapperboard stripes, popcorn). Always apply the **4-Step Decontamination Pipeline**:
1. **Border-Seeded Connected-Component Flood Fill (`scipy.ndimage.label`):** Identify white pixels (`luma > 235`, `chroma < 22`) connected to the 4 outer edges of each quadrant.
2. **Closed-Counter Hole Knockout:** For typography (`Q2`) and hollow seals (`Q3`), also flood-fill interior white islands (`area >= 25px`) inside letter counters (`Đ, Ậ, Ỏ`).
3. **1px Foreground Erosion + Soft Alpha Feathering:** Erode the foreground binary mask by `1px` and apply a `sigma=0.8` Gaussian ramp on the boundary so anti-aliased edge transitions are smooth.
4. **3x3 Minimum-Filter RGB Color Decontamination:** On boundary pixels (`1..4px` inside the alpha edge), replace RGB values with a `3x3` spatial minimum filter (`ndimage.minimum_filter(rgb, size=(3,3,1))`) so semi-transparent edge pixels inherit the dark crimson/gold interior color rather than white background bleed.
5. **Mandatory Dark-Background Verification (`99_dark_bg_verification_sheet.png`):** Composite all extracted RGBA assets over a dark slate canvas (`#10141C`) and inspect via `read_file` to confirm zero white fringe.

---

## 3. 16:9 Texture Gradient Channel Cover Banner (`2560x1440` 2K QHD)

For channel banners (`16:9`), use a **Tactile Multi-Layered Texture Gradient**:
- **Color & Material Stack:** Blend deep midnight ink (`#0D111A`), imperial velvet crimson (`#3B0610`), warm cinnabar red (`#B81D2A`), and luminous sunset amber-gold (`#F59E0B`), layered with embossed Chinese silk brocade cloud patterns (`祥云`), fine Kintsugi liquid gold veins, frosted silk waves, and 35mm film grain.
- **Generator Corner Star Removal (`✦`):** Flow/Imagen `16:9` outputs (`1376x768`) embed a neutral gray 4-point star (`✦`) centered at `(x=1285, y=662)`. Always remove this star before delivery using **harmonic Laplace inpainting (`r=38px`) + high-frequency grain transfer** from the surrounding ring, then export both native (`1376x768`) and **2K QHD (`2560x1440`)** PNGs (`07_cover_16x9_silk_kintsugi_gradient_2560x1440.png`).

---

## 4. 16:9 Dual-Corner In-Frame Watermark & Overlay Text Architecture

To brand `16:9` (`1920x1080`) videos cleanly without obstructing actors' faces or bottom subtitles, use a **balanced Dual-Corner Header inside the safe frame (`margin_x = 42px`, `top_y = 22px`, vertical axis `center_y = 93px`)**:

```text
+-----------------------------------------------------------------------------------+
|  [42px]                                                                   [42px]  |
|  +------------------------+                     +------------------------------+  |
|  | (Icon) ĐẬU ĐỎ          |                     |  Vietsub by Đậu Đỏ  [Ấn Đỏ]  |  |
|  | Top-Left Brand Lockup  |                     |  Top-Right Credit Overlay    |  |
|  | (x=42, y=22, h=114px)  |                     |  (x=W-w-42, y=58, center=93) |  |
|  +------------------------+                     +------------------------------+  |
|                                                                                   |
|                                                                                   |
|                           ACTIVE 16:9 CINEMA VIEWPORT                             |
|                                  (1920 x 1080)                                    |
|                                                                                   |
|                                                                                   |
|                   +-------------------------------------------+                   |
|                   |   Frosted-Glass / Pill Vietnamese Sub     |                   |
|                   |            (y = 880 .. 1080)              |                   |
|                   +-------------------------------------------+                   |
+-----------------------------------------------------------------------------------+
```

### 4.1. Top-Left Corner: Primary Brand Lockup (`13_top_left_logo_large.png`)
- **Asset:** Horizontal Lockup (`05_watermark_horizontal_lockup_transparent.png` = Mascot Icon + 3D `"ĐẬU ĐỎ"` wordmark).
- **Scale & Position (`1920x1080`):**
  - `target_height = 114px` (padded with shadow to `255x142px`).
  - Placed inside the top-left safe frame at `x = 42px`, `y = 22px` (vertical center `center_y = 93px`).
  - `opacity = 0.95` with a soft Gaussian drop shadow (`radius = 5px`, `offset = (+2px, +4px)`, `shadow_alpha = 50%`) so it remains legible over bright sky/wall backgrounds.

### 4.2. Top-Right Corner: Attribution Text Overlay (`"Vietsub by <Channel>"`)
Aligned along the same horizontal centerline (`center_y = 93px`, right safe margin `42px`, so `x = 1920 - width - 42`). The skill suite supports **3 selectable overlay text styles** (generated via `apply_channel_branding.py`):

1. **Style B (`floating` — Default Production Standard):**
   - **File:** `15_top_right_vietsub_floating.png` (`319x70px`, placed at `x = W - w - 42`, `y = 58`).
   - **Visual Design:** Clean floating typography with **no background box**, consisting of:
     - `"Vietsub by "` in `24pt` bold sans-serif, warm champagne ivory (`#F8F2E6`, `alpha=92%`),
     - `"Đậu Đỏ"` in `26pt` bold display sans-serif, warm metallic gold (`#FFD46C`) with a `2px` deep crimson stroke (`#781018`) matching the left logo's color palette,
     - Followed by a `12px` gap and the solid square Chinese red seal stamp (`03b_watermark_seal_solid_transparent.png` at `38x38px`),
     - Backed by a soft Gaussian drop shadow (`radius = 6px`, `offset = (+3px, +5px)`, `alpha = 60%`) so the floating text never blends into bright backgrounds.
2. **Style A (`pill` — Frosted Dark Capsule with Gold Border):**
   - **File:** `14_top_right_vietsub_pill.png`
   - **Visual Design:** Translucent dark crimson-obsidian rounded capsule (`rgba(22, 12, 16, 175)`) with a `1.5px` warm gold border (`rgba(238, 192, 105, 170)`), containing `[Red Seal 34x34] + "VIETSUB BY " (ivory) + "ĐẬU ĐỎ" (gold)`. Best when videos have extreme white flashing backgrounds in the top-right corner.
3. **Style C (`3d_wordmark` — Seal + Italic Prefix + Miniature 3D Logo Text):**
   - **File:** `18_top_right_vietsub_3d_wordmark.png`
   - **Visual Design:** Combines `[Red Seal 36x36] + "Vietsub by" (21pt bold italic serif) + [Miniature 3D "ĐẬU ĐỎ" graphic wordmark 34px tall]`.

---

## 5. Fast Dual-ROI Hardware Compositing (`apply_channel_branding.py` & `composite_and_verify.py`)

### 5.1. Why Dual-ROI Overlay Beats Full-Frame `1920x1080` PNG Overlay
Never feed a mostly-empty `1920x1080` RGBA PNG to FFmpeg `overlay=0:0` when burning corner watermarks—that forces `swscaler` and `overlay` to alpha-blend `2,073,600` pixels per frame. Instead, pass the **two tight cropped PNGs** (`255x142` and `319x70`, totaling `< 2.8%` of the frame area):

```bash
ffmpeg -y -nostats -progress pipe:1 \
  -i input_16x9.mp4 \
  -i workspace/branding-dau-do/13_top_left_logo_large.png \
  -i workspace/branding-dau-do/15_top_right_vietsub_floating.png \
  -filter_complex "[0:v][1:v]overlay=42:22[v1];[v1][2:v]overlay=W-w-42:58,setsar=1:1[vout]" \
  -map "[vout]" -map 0:a:0 -sn \
  -c:v h264_videotoolbox -b:v 5000k -pix_fmt yuv420p \
  -c:a copy -movflags +faststart \
  output_16x9.mp4
```

### 5.2. Configuration in `config/project_config.json`
`init_workspace.py` automatically provisions the `watermark` configuration block in `config/project_config.json`:

```json
"watermark": {
  "enabled": true,
  "brand_dir": "/Users/hoangnguyen142/Work/PROJ1/workspace/branding-dau-do",
  "channel_name": "Đậu Đỏ",
  "layout": "dual_corner",
  "style": "floating",
  "top_left_logo": "/Users/hoangnguyen142/Work/PROJ1/workspace/branding-dau-do/13_top_left_logo_large.png",
  "top_left_x": 42,
  "top_left_y": 22,
  "top_right_badge": "/Users/hoangnguyen142/Work/PROJ1/workspace/branding-dau-do/15_top_right_vietsub_floating.png",
  "top_right_margin_x": 42,
  "top_right_y": 58
}
```
When `watermark.enabled == true`, both `composite_and_verify.py` (during initial Stage 5 encoding) and `apply_channel_branding.py` (for post-composite branding updates) automatically burn the dual-corner header into the `16:9` deliverable.
