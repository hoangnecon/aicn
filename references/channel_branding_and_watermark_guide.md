# Channel Branding, Logo Pack Extraction & Dual-Corner Watermark Guide (`v3.0.0`)

This reference manual defines the canonical storage hierarchy, channel logo assets, and fast pre-composited **in-frame dual-corner watermark & attribution (`Vietsub by <Channel>`)** rendering used in `aicn v3.0.0` across portrait (`9:16` — `1080×1920`, `720×1280`) and landscape (`16:9` — `1920×1080`) videos.

---

## 1. Canonical 3-Tier Storage Hierarchy (Branding vs. Series vs. Working Units)

Never mix shared channel assets, series posters, and working-unit intermediates:

1. **Tier 1 — Project / Channel Branding (`workspace/branding-<channel>/`):**
   - Shared across all series on the channel:
     - `logos/`: Master `2x2` grid (`00`), extracted transparent RGBA logos (`01`, `02`, `04`, `06`), and dark-background verification sheet (`99`).
     - `overlays/`: Seals (`03`, `03b`), horizontal lockup (`05`), Top-Left Large Logo (`13_top_left_logo_large.png`), and Top-Right Attribution Overlays (`14` Pill, `15` Floating, `18` 3D Wordmark).
     - `covers/`: `16:9` (`2560x1440` 2K) channel banners (`07..09`).
2. **Tier 2 — Series Hub (`workspace/<series-slug>/posters/` & `deliverables/`):**
   - **Posters (`posters/`):**
     - `posters/cover.jpg` (Catalog thumbnail)
     - `posters/official_poster_<series-slug>_zh.jpg` (HD unshrunk `1440px+` official source poster)
     - `posters/poster_<series-slug>_vi_<ratio>_raw.<ext>`, `_clean.png`, and `_branded.png` (standardized via `aicn.py brand-poster`)
   - **Published Full-Series Release (`deliverables/`):**
     - `deliverables/final_<series-slug>_full_vi.mp4` (built ONLY by `aicn.py merge-series` once all working batches pass Stage 5)
     - `deliverables/subtitles/final_<series-slug>_full_vi.srt`
3. **Tier 3 — Internal Working Units (`workspace/<series-slug>/units/<unit-slug>/`):**
   - Internal processing slices (`media/final_<unit-slug>_vi.mp4` + `data/subtitles_full.srt`). Never uploaded standalone.

---

## 2. Channel Logo Pack Structure (`2x2` Master Grid & Zero-Halo Extraction)

When creating a new channel identity (e.g., **"Đậu Đỏ"** / `红豆`), keep all core elements on a clean `2x2` grid over `#FFFFFF`:
- **Q1 (`01_logo_icon_transparent.png`):** Pure mascot icon (no text).
- **Q2 (`02_logo_with_text_transparent.png`):** Stacked mascot + 3D display wordmark (yields standalone wordmark `06_typography_only_transparent.png` and horizontal lockup `05_watermark_horizontal_lockup_transparent.png`).
- **Q3 (`03_watermark_seal_transparent.png` & `03b_watermark_seal_solid_transparent.png`):** Square traditional red ink seal (`印章`), hollow and solid-cream variants.
- **Q4 (`04_avatar_emblem_transparent.png`):** Circular social avatar emblem.

Use border-seeded connected-component flood-fill (`scipy.ndimage.label`) + closed-counter hole knockout (`Đ, Ậ, Ỏ`) + `1px` erosion + `3x3` minimum-filter RGB edge color decontamination, verified on a dark slate sheet (`99_dark_bg_verification_sheet.png`) via `read_file`.

---

## 3. Dual-Corner In-Frame Watermark Architecture (`v3.0.0` Dub + Watermark Only)

`aicn v3.0.0` renders **no burned-in subtitle overlays**. The frame carries only clean source video plus balanced top dual-corner branding (`top_left_x=42`, `top_left_y=22`, `top_right_margin_x=42`, `top_right_y=58` at `1920px` reference width; proportionally scaled on narrower/portrait frames `width < 1200`):

```text
+---------------------------------------------------+
| [margin]                                 [margin] |
| +------------------+       +--------------------+ |
| | (Icon) ĐẬU ĐỎ    |       | Vietsub by Đậu Đỏ  | |
| | Top-Left Lockup  |       | + [Red Seal]       | |
| +------------------+       +--------------------+ |
|                                                   |
|              CLEAN VIDEO VIEWPORT                 |
|      (1080x1920 / 720x1280 / 1920x1080)           |
|     Full Vietnamese Dub + External .srt           |
|                                                   |
+---------------------------------------------------+
```

- **Top-Left Corner:** `13_top_left_logo_large.png` (horizontal mascot + 3D wordmark with soft drop shadow).
- **Top-Right Corner:** `15_top_right_vietsub_floating.png` (default `floating` attribution text `"Vietsub by Đậu Đỏ"` + `38×38` red seal `03b`, soft shadow) — or `14_top_right_vietsub_pill.png` / `18_top_right_vietsub_3d_wordmark.png`.

---

## 4. Fast Pre-Composited Top-Band Hardware Overlay (`aicn.py render`, `v3-dub-wm-3.0`)

Never pass two `-framerate <fps> -loop 1` PNG inputs into dual CPU `overlay` filters (which choked RTX 2060 NVENC to `154.7s/unit` in `v2.7.0`). In `v3.0.0`:
1. **Cached Top-Band Strip (`build_cached_watermark_strip`):**
   - `aicn.py render --unit $U --brand-dir <branding_dir>` hashes the source branding PNGs via `ap.content_sha256`, computes `wm_key = ap.wm_overlay_key(width, height, tl_sha, tr_sha, scale, pos)`, and composites both corners **ONCE** via Pillow onto a tight transparent RGBA strip `(width, band_h)` saved atomically at `<global_cache>/wm_overlay/<wm_key>.png`.
2. **GPU NVENC Path (`hwupload_cuda` + `overlay_cuda`, measured `7.0×–10.5×` RT on RTX 2060):**
   - Decodes at exact source dimensions (avoiding NVDEC `-hwaccel_output_format cuda` `1088`/`256` macroblock surface padding on `1920x1080` and `1080x1920`), uploads `format=yuv420p,hwupload_cuda` + the single static top-band PNG via `format=yuva420p,hwupload_cuda`, blends with `overlay_cuda=0:0:eof_action=repeat`, and enforces exact `dims_ok` in `verify-render`. Falls back automatically to single-static CPU `overlay` if needed.
3. **Fast Single-Static Overlay Fallback / VideoToolbox / CPU (`3.90s / 30s` = `7.7×` RT):**
   - Single static `-i <overlay_strip.png>` with `overlay=0:0:eof_action=repeat:format=auto,format=<pix_fmt>` (zero `-loop 1` PNG decoding!).
4. **No Watermark:** Stream-copies video (`-c:v copy -sn -shortest -movflags +faststart`) in seconds.

### Configuration in `config/project_config.json` (Optional)
Passing `--brand-dir workspace/branding-dau-do` to `render` / `plan` / `run-series` resolves `13_top_left_logo_large.png` and `15_top_right_vietsub_floating.png` automatically. If configured in `config/project_config.json`, use portable paths relative to project root or pass `--brand-dir` explicitly:

```json
"watermark": {
  "enabled": true,
  "brand_dir": "workspace/branding-dau-do",
  "channel_name": "Đậu Đỏ",
  "layout": "dual_corner",
  "style": "floating",
  "top_left_logo": "workspace/branding-dau-do/13_top_left_logo_large.png",
  "top_left_x": 42,
  "top_left_y": 22,
  "top_right_badge": "workspace/branding-dau-do/15_top_right_vietsub_floating.png",
  "top_right_margin_x": 42,
  "top_right_y": 58
}
```

---

## 5. Poster Standardization & Branding (`aicn.py brand-poster`)

Use `aicn.py brand-poster` to standardize any raw `9:16` (`1080×1920`), `16:9` (`1920×1080`), or `3:4` (`1080×1440`) poster, optionally inpaint the Flow/Imagen bottom-right corner star `✦` (`--remove-star`), and composite the dual-corner channel header:

```bash
$AICN_PY <aicn>/scripts/aicn.py brand-poster \
  --hub workspace/<series-slug> \
  --input workspace/<series-slug>/posters/<raw_file> \
  --brand-dir workspace/branding-dau-do \
  [--remove-star] --delete-source
```
See `<aicn>/references/poster_and_caption_guide.md` for full prompt templates and caption formatting.
