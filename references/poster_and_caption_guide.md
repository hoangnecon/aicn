# Poster Localization, Thumbnail Packaging & Upload Caption Guide (`v2.7.0`)

This guide standardizes post-production release packaging for every AICN series: pulling the HD official source poster, localizing Vietnamese `9:16` / `16:9` posters (user-generated externally via Nano Banana 2 / Google Flow / Gemini or agent-assisted), auto-renaming & compositing dual-corner channel branding via `aicn.py brand-poster`, and writing ready-to-paste upload captions.

---

## 1. Canonical `posters/` File Contract (Tier 2 — Series Folder)

Every Series Hub (`workspace/<series-slug>/posters/` and its Google Drive mirror) uses this exact naming contract:

| File | Role |
|---|---|
| `cover.jpg` | Compact catalog thumbnail fetched at discovery (`~640px`). |
| `official_poster_<slug>_zh.jpg` | **HD unshrunk official source poster** (`1440px+` wide) used as the image-edit reference. |
| `poster_<slug>_vi_<ratio>_raw.<ext>` | Unmodified Vietnamese poster generated externally (or via `image-generator`) before resize/branding (`<ratio>` = `9x16`, `16x9`, or `3x4`). |
| `poster_<slug>_vi_<ratio>_clean.png` | Normalized full-res PNG (`1080x1920` for `9x16`, `1920x1080` for `16x9`, `1080x1440` for `3x4`) with zero text distortion and optional corner star `✦` removed. |
| `poster_<slug>_vi_<ratio>_branded.png` | Final publish-ready poster with dual-corner channel branding (`13_top_left_logo_large.png` + `15_top_right_vietsub_floating.png`). |

---

## 2. Step 1 — Pull the HD Official Source Poster (`official_poster_<slug>_zh.jpg`)

Hongguo catalog URLs often end with `~tplv-shrink:640:0.image`. Before handing a reference image to the poster generator:
1. Run `node <skill>/scripts/hg_tool.cjs details <series_id> --json` and inspect `"cover"`.
2. Replace `~tplv-shrink:640:0.image` with `~tplv-resize:1440:0.image` (fallback: `~tplv-noop.image`) and save as `posters/official_poster_<slug>_zh.jpg` (typically `1440×2057` or `1039×1484` crisp JPEG).

---

## 3. Step 2 — Vietnamese Poster & Thumbnail Prompt Templates

The user may generate the Vietnamese poster externally (e.g., **Nano Banana 2 on Google Flow** or **Gemini Image Edit**) using `official_poster_<slug>_zh.jpg` as Reference Image and drop the output into `posters/` — or ask the agent to generate it directly.

### 3.1. Vertical Poster `9:16` (`1080×1920` — Reels / TikTok / Shorts / Facebook Watch)

```text
Edit the attached official Chinese 3D/AI drama poster into a vertical 9:16 Vietnamese localized series poster (1080x1920), keeping 100% of the exact characters, faces, expressions, poses, outfits, lighting, and rendering quality intact.

Extend the upper background cleanly upward to fit 9:16 vertical aspect ratio, leaving generous unobstructed headroom in the top-left and top-right corners (top 140px safe margin) for channel logos.

Completely remove ALL original Chinese characters and replace them with perfectly spelled Vietnamese 3D display typography matching the exact original material, color palette, outlines, and playful/cinematic motifs:
- Top main title (1–2 balanced lines, matching primary color/texture): "<LINE 1>" / "<LINE 2>"
- Secondary punchline right below (matching accent color/outline): "<PUNCHLINE>"
- Optional compact badge below: "TRỌN BỘ <N> TẬP"

Ensure crisp, accurate Vietnamese diacritics with zero typos, zero leftover Chinese text, zero watermarks, zero extra logos, ultra-clean commercial poster finish.
```

### 3.2. Horizontal Thumbnail `16:9` (`1920×1080` — YouTube / Facebook Widescreen)

```text
Transform and outpaint the attached official Chinese 3D/AI drama poster into a high-impact horizontal 16:9 Vietnamese YouTube/Facebook thumbnail (1920x1080).

Keep the main characters 100% identical to the reference image in face, hair, clothing, lighting, and pose — positioned prominently on the left-to-center-left half of the frame. Seamlessly expand the background scenery across the right half of the 16:9 canvas. Leave the top-left and top-right corners (top 120px) clean and uncluttered for channel watermarks.

Remove ALL original Chinese text completely. On the upper-right and middle-right open area, render large, ultra-legible, high-contrast 3D Vietnamese title typography matching the reference poster's color palette and texture:
- Line 1: "<MAIN HOOK LINE 1>"
- Line 2: "<PUNCHLINE LINE 2>"
- Compact badge below: "FULL <N> TẬP · THUYẾT MINH"

Exact Vietnamese spelling and diacritics, zero Chinese characters, zero watermarks, crisp 2K finish, maximum thumbnail click-through contrast.
```

---

## 4. Step 3 — Standardize, Clean & Brand via `aicn.py brand-poster`

Once the user drops the generated poster (e.g., `Editing_Vietnamese_anime_poster_xxxx.jpg`) into `posters/`, run:

```bash
python <skill>/scripts/aicn.py brand-poster \
  --hub workspace/<series-slug> \
  --input workspace/<series-slug>/posters/<raw_file> \
  --brand-dir <branding_dir> \
  --delete-source
```

Options:
- `--aspect auto|9x16|16x9|3x4` (default `auto`: detects portrait `9x16` → `1080×1920`, widescreen `16x9` → `1920×1080`, or `3x4` → `1080×1440` using aspect-preserving center-crop + Lanczos resize).
- `--remove-star` (optional: removes the Flow/Imagen bottom-right corner star `✦` via smooth boundary inpainting before resizing; omit when the raw image has no star).
- `--delete-source` (removes the messy-named input file after archiving it as `poster_<slug>_vi_<ratio>_raw.<ext>`).
- **Mandatory visual verification:** Always inspect `poster_<slug>_vi_<ratio>_branded.png` via `read_file` to verify Vietnamese diacritics, zero Chinese leakage, and clean dual-corner logo placement without overlapping the title.

---

## 5. Step 4 — Upload Caption Standard (Facebook / YouTube / TikTok)

Strict **2-line minimal format**:
- **Line 1:** `<Catchy Vietnamese Hook Title in Title Case> <2 fitting emojis> | Full <N> Tập`
- **Line 2:** Channel tag first (`#daudonho`), followed by the 7 core all-lowercase broad tags, plus 2–3 lowercase non-accented Vietnamese topic tags:
  `#daudonho #aishortfilmdrama #aishortdrama #aifilm #phimai #phimngan #phimthuyetminh #phimtrungquoc #<topic1> #<topic2>`

Example:
```text
Sống Cô Đơn 3000 Năm, Rồng Băng Lạnh Lùng Cuối Cùng Cũng Có Con Gái Cưng, Lần Đầu Làm Cha Vụng Về Mà Ngọt Xỉu! 🐉❄️ | Full 135 Tập
#daudonho #aishortfilmdrama #aishortdrama #aifilm #phimai #phimngan #phimthuyetminh #phimtrungquoc #rongcon #chualanh
```
