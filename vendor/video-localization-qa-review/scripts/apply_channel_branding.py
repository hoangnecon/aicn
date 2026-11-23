#!/usr/bin/env python3
"""
Channel Branding, 2x2 Grid Slicing, Dual-Corner Watermark & Attribution Badge Engine (apply_channel_branding.py).
Capabilities:
  1. --slice-grid <grid.jpg>: Slices a 2x2 brand grid into 6 zero-halo RGBA assets (01..06) + 99_dark_bg_verification_sheet.png
  2. --build-badges: Generates Top-Left Large Logo (13) and 3 Top-Right Attribution Styles (14=pill, 15=floating, 18=3d_wordmark)
  3. --burn-video: Burns Dual-Corner In-Frame Watermark (Top-Left Logo + Top-Right "Vietsub by <Channel>") into 16:9 MP4 with 10s heartbeat telemetry.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from scipy import ndimage

AICN_SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts"))
if AICN_SCRIPTS not in sys.path:
    sys.path.insert(0, AICN_SCRIPTS)
try:
    import aicn_platform as ap
except ImportError:
    ap = None


def remove_bg_decontaminate(rgb_arr: np.ndarray, knock_out_interior_holes: bool = False, min_hole_area: int = 25) -> Image.Image:
    """
    Border-seeded connected-component flood-fill + optional interior counter-hole knockout +
    1px foreground erosion + 3x3 minimum-filter RGB edge color decontamination.
    """
    arr = rgb_arr.astype(np.float32)
    h, w, _ = arr.shape
    luma = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    chroma = np.max(arr, axis=2) - np.min(arr, axis=2)

    is_white_cand = (luma > 232.0) & (chroma < 26.0)
    labeled, _ = ndimage.label(is_white_cand)

    border_labels = set()
    border_labels.update(np.unique(labeled[0, :]))
    border_labels.update(np.unique(labeled[-1, :]))
    border_labels.update(np.unique(labeled[:, 0]))
    border_labels.update(np.unique(labeled[:, -1]))
    border_labels.discard(0)

    bg_mask = np.isin(labeled, list(border_labels))

    if knock_out_interior_holes:
        # Also knock out enclosed white counter-holes inside letters (Đ, Ậ, Ỏ, A, O) or hollow seals
        counts = np.bincount(labeled.ravel())
        for lbl_id in range(1, len(counts)):
            if lbl_id not in border_labels and counts[lbl_id] >= min_hole_area:
                pts_y, _ = np.where(labeled == lbl_id)
                if np.mean(pts_y) > h * 0.48 or min_hole_area <= 20:
                    bg_mask |= (labeled == lbl_id)

    fg_mask = ~bg_mask
    # Remove tiny isolated dust specks (< 16 px)
    fg_lbl, n_fg = ndimage.label(fg_mask)
    if n_fg > 0:
        fg_counts = np.bincount(fg_lbl.ravel())
        for lid in range(1, len(fg_counts)):
            if fg_counts[lid] < 16:
                fg_mask[fg_lbl == lid] = False

    # 1px foreground erosion to strip JPEG white anti-aliasing halo
    fg_eroded = ndimage.binary_erosion(fg_mask, iterations=1)
    alpha = ndimage.gaussian_filter(fg_eroded.astype(np.float32), sigma=0.75) * 255.0
    alpha = np.clip(alpha, 0, 255).astype(np.uint8)

    # 3x3 minimum-filter RGB edge color decontamination on boundary pixels
    edge_band = ndimage.binary_dilation(fg_eroded, iterations=2) & (~ndimage.binary_erosion(fg_eroded, iterations=3))
    decontam = arr.copy()
    for c in range(3):
        min_ch = ndimage.minimum_filter(arr[:, :, c], size=3)
        decontam[:, :, c][edge_band] = min_ch[edge_band]

    rgba = np.dstack([np.clip(decontam, 0, 255).astype(np.uint8), alpha])
    pil_out = Image.fromarray(rgba)
    bbox = pil_out.getbbox()
    return pil_out.crop(bbox) if bbox else pil_out


def slice_brand_grid(grid_path: str, brand_dir: str):
    os.makedirs(brand_dir, exist_ok=True)
    shutil.copy2(grid_path, os.path.join(brand_dir, "00_dau_do_master_grid_2k.jpg"))
    img = Image.open(grid_path).convert("RGB").resize((2048, 2048), Image.Resampling.LANCZOS)
    arr = np.array(img)
    mid = 1024
    pad = 24

    q1 = arr[pad:mid - pad, pad:mid - pad]
    q2 = arr[pad:mid - pad, mid + pad:2048 - pad]
    q3 = arr[mid + pad:2048 - pad, pad:mid - pad]
    q4 = arr[mid + pad:2048 - pad, mid + pad:2048 - pad]

    p1 = remove_bg_decontaminate(q1, knock_out_interior_holes=False)
    p2 = remove_bg_decontaminate(q2, knock_out_interior_holes=True, min_hole_area=25)
    p3_hollow = remove_bg_decontaminate(q3, knock_out_interior_holes=True, min_hole_area=15)
    p3_solid = remove_bg_decontaminate(q3, knock_out_interior_holes=False)
    p4 = remove_bg_decontaminate(q4, knock_out_interior_holes=False)

    p1.save(os.path.join(brand_dir, "01_logo_icon_transparent.png"))
    p2.save(os.path.join(brand_dir, "02_logo_with_text_transparent.png"))
    p3_hollow.save(os.path.join(brand_dir, "03_watermark_seal_transparent.png"))
    p3_solid.save(os.path.join(brand_dir, "03b_watermark_seal_solid_transparent.png"))
    p4.save(os.path.join(brand_dir, "04_avatar_emblem_transparent.png"))

    # Isolate standalone typography from bottom of p2 via connected components
    p2_arr = np.array(p2)
    lbl, num = ndimage.label(p2_arr[:, :, 3] > 20)
    text_mask = np.zeros_like(lbl, dtype=bool)
    for cid in range(1, num + 1):
        ys, _ = np.where(lbl == cid)
        if np.mean(ys) > p2.height * 0.55:
            text_mask |= (lbl == cid)
    typo_arr = p2_arr.copy()
    typo_arr[~text_mask, 3] = 0
    p6 = Image.fromarray(typo_arr)
    if p6.getbbox():
        p6 = p6.crop(p6.getbbox())
    p6.save(os.path.join(brand_dir, "06_typography_only_transparent.png"))

    # Assemble horizontal lockup (01_logo_icon + 06_typography_only)
    icon_h = 220
    icon_w = int(round(p1.width * (icon_h / p1.height)))
    icon_r = p1.resize((icon_w, icon_h), Image.Resampling.LANCZOS)

    txt_h = 128
    txt_w = int(round(p6.width * (txt_h / p6.height)))
    txt_r = p6.resize((txt_w, txt_h), Image.Resampling.LANCZOS)

    gap = 18
    lock_w = icon_w + gap + txt_w
    lock_h = max(icon_h, txt_h) + 20
    p5 = Image.new("RGBA", (lock_w, lock_h), (0, 0, 0, 0))
    p5.alpha_composite(icon_r, (0, (lock_h - icon_h) // 2))
    p5.alpha_composite(txt_r, (icon_w + gap, (lock_h - txt_h) // 2 + 8))
    p5.save(os.path.join(brand_dir, "05_watermark_horizontal_lockup_transparent.png"))
    print(f"[BRANDING] Extracted 01..06 transparent RGBA assets in {brand_dir}", flush=True)


def make_shadowed(wm: Image.Image, target_h: int, opacity: float = 0.95) -> Image.Image:
    target_w = int(round(wm.width * (target_h / wm.height)))
    wm_r = wm.resize((target_w, target_h), Image.Resampling.LANCZOS)
    r, g, b, a = wm_r.split()
    a = a.point(lambda x: int(x * opacity))
    wm_op = Image.merge("RGBA", (r, g, b, a))
    pad = 14
    canvas = Image.new("RGBA", (target_w + pad * 2, target_h + pad * 2), (0, 0, 0, 0))
    sh_a = a.point(lambda x: int(x * 0.5))
    sh = Image.merge("RGBA", (Image.new("L", wm_r.size, 0), Image.new("L", wm_r.size, 0), Image.new("L", wm_r.size, 0), sh_a))
    canvas.paste(sh, (pad + 2, pad + 4))
    canvas = canvas.filter(ImageFilter.GaussianBlur(radius=5))
    canvas.alpha_composite(wm_op, (pad, pad))
    return canvas


def build_dual_corner_badges(brand_dir: str, channel_name: str = "Đậu Đỏ", credit_prefix: str = "Vietsub by "):
    wm_horiz = Image.open(os.path.join(brand_dir, "05_watermark_horizontal_lockup_transparent.png")).convert("RGBA")
    wm_seal = Image.open(os.path.join(brand_dir, "03b_watermark_seal_solid_transparent.png")).convert("RGBA")
    wordmark_path = os.path.join(brand_dir, "06_typography_only_transparent.png")

    # 1. Top-Left Large Logo (target_h=114 -> 255x142 with shadow)
    tl_logo = make_shadowed(wm_horiz, target_h=114, opacity=0.95)
    tl_logo.save(os.path.join(brand_dir, "13_top_left_logo_large.png"))

    font_paths = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/Library/Fonts/Arial Bold.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\segoeuib.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    font_path = next((p for p in font_paths if os.path.exists(p)), None)
    scale = 2
    margin = 16 * scale
    dummy = ImageDraw.Draw(Image.new("RGBA", (10, 10)))

    # 2. Style A: Frosted Pill Badge (14_top_right_vietsub_pill.png)
    f_small = ImageFont.truetype(font_path, 20 * scale)
    f_bold = ImageFont.truetype(font_path, 23 * scale)
    t_up1 = credit_prefix.upper()
    t_up2 = channel_name.upper()
    b1 = dummy.textbbox((0, 0), t_up1, font=f_small)
    b2 = dummy.textbbox((0, 0), t_up2, font=f_bold)
    w1, h1 = b1[2] - b1[0], b1[3] - b1[1]
    w2, h2 = b2[2] - b2[0], b2[3] - b2[1]
    seal_h = 34 * scale
    seal_r = wm_seal.resize((seal_h, seal_h), Image.Resampling.LANCZOS)
    pad_x, pad_y, gap = 20 * scale, 11 * scale, 10 * scale
    pill_w = pad_x * 2 + seal_h + gap + w1 + w2
    pill_h = seal_h + pad_y * 2

    canvas_a = Image.new("RGBA", (pill_w + margin * 2, pill_h + margin * 2), (0, 0, 0, 0))
    sh_layer = Image.new("RGBA", canvas_a.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh_layer).rounded_rectangle(
        [margin + 2, margin + 4, margin + pill_w + 2, margin + pill_h + 4],
        radius=pill_h // 2, fill=(0, 0, 0, 115)
    )
    canvas_a.alpha_composite(sh_layer.filter(ImageFilter.GaussianBlur(radius=6)))
    draw_a = ImageDraw.Draw(canvas_a)
    draw_a.rounded_rectangle(
        [margin, margin, margin + pill_w, margin + pill_h],
        radius=pill_h // 2, fill=(22, 12, 16, 175), outline=(238, 192, 105, 170), width=3
    )
    sx, sy = margin + pad_x, margin + (pill_h - seal_h) // 2
    canvas_a.alpha_composite(seal_r, (sx, sy))
    tx1 = sx + seal_h + gap
    draw_a.text((tx1, margin + (pill_h - h1) // 2 - b1[1]), t_up1, font=f_small, fill=(235, 225, 205, 235))
    draw_a.text((tx1 + w1, margin + (pill_h - h2) // 2 - b2[1]), t_up2, font=f_bold, fill=(255, 206, 98, 255))
    tr_a = canvas_a.resize((canvas_a.width // scale, canvas_a.height // scale), Image.Resampling.LANCZOS)
    tr_a.save(os.path.join(brand_dir, "14_top_right_vietsub_pill.png"))

    # 3. Style B (Default Recommended): Clean Floating Badge + Seal (15_top_right_vietsub_floating.png)
    f_b1 = ImageFont.truetype(font_path, 24 * scale)
    f_b2 = ImageFont.truetype(font_path, 26 * scale)
    bb1 = dummy.textbbox((0, 0), credit_prefix, font=f_b1)
    bb2 = dummy.textbbox((0, 0), channel_name, font=f_b2)
    wb1, hb1 = bb1[2] - bb1[0], bb1[3] - bb1[1]
    wb2, hb2 = bb2[2] - bb2[0], bb2[3] - bb2[1]
    seal_hb = 38 * scale
    seal_rb = wm_seal.resize((seal_hb, seal_hb), Image.Resampling.LANCZOS)
    tot_w = wb1 + wb2 + 12 * scale + seal_hb
    tot_h = max(hb2, seal_hb)

    canvas_b = Image.new("RGBA", (tot_w + margin * 2, tot_h + margin * 2), (0, 0, 0, 0))
    txt_layer = Image.new("RGBA", canvas_b.size, (0, 0, 0, 0))
    tdraw = ImageDraw.Draw(txt_layer)
    bx1 = margin
    by1 = margin + (tot_h - hb1) // 2 - bb1[1]
    tdraw.text((bx1, by1), credit_prefix, font=f_b1, fill=(248, 242, 230, 235))
    bx2 = bx1 + wb1
    by2 = margin + (tot_h - hb2) // 2 - bb2[1]
    tdraw.text((bx2, by2), channel_name, font=f_b2, fill=(255, 212, 108, 250), stroke_width=4, stroke_fill=(120, 16, 24, 240))
    txt_layer.alpha_composite(seal_rb, (bx2 + wb2 + 12 * scale, margin + (tot_h - seal_hb) // 2))

    _, _, _, a_b = txt_layer.split()
    sh_b = Image.merge("RGBA", (Image.new("L", txt_layer.size, 0), Image.new("L", txt_layer.size, 0), Image.new("L", txt_layer.size, 0), a_b.point(lambda x: int(x * 0.6))))
    canvas_b.paste(sh_b, (3, 5))
    canvas_b = canvas_b.filter(ImageFilter.GaussianBlur(radius=6))
    canvas_b.alpha_composite(txt_layer)
    tr_b = canvas_b.resize((canvas_b.width // scale, canvas_b.height // scale), Image.Resampling.LANCZOS)
    tr_b.save(os.path.join(brand_dir, "15_top_right_vietsub_floating.png"))

    # 4. Style C: 3D Wordmark Floating Badge (18_top_right_vietsub_3d_wordmark.png)
    if os.path.exists(wordmark_path):
        wm_tight = Image.open(wordmark_path).convert("RGBA")
        if wm_tight.getbbox():
            wm_tight = wm_tight.crop(wm_tight.getbbox())
        wm_h = 34
        wm_w = int(round(wm_tight.width * (wm_h / wm_tight.height)))
        wm_3d = wm_tight.resize((wm_w, wm_h), Image.Resampling.LANCZOS)
        seal_36 = wm_seal.resize((36, 36), Image.Resampling.LANCZOS)
        f_it_cands = [
            "/System/Library/Fonts/Supplemental/Georgia Bold Italic.ttf",
            r"C:\Windows\Fonts\georgiaz.ttf",
        ]
        f_it = next((p for p in f_it_cands if os.path.exists(p)), None)
        f_sub = ImageFont.truetype(f_it if f_it else font_path, 21)
        tb = dummy.textbbox((0, 0), credit_prefix.strip(), font=f_sub)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        in_w = 36 + 10 + tw + 9 + wm_w
        in_h = 36
        fg_c = Image.new("RGBA", (in_w + 56, in_h + 44), (0, 0, 0, 0))
        fg_d = ImageDraw.Draw(fg_c)
        xc, cy = 28, 22 + in_h // 2
        fg_c.alpha_composite(seal_36, (xc, cy - 18))
        xc += 46
        fg_d.text((xc, cy - th // 2 - tb[1] - 1), credit_prefix.strip(), font=f_sub, fill=(252, 240, 210, 245), stroke_width=2, stroke_fill=(45, 12, 16, 220))
        xc += tw + 9
        fg_c.alpha_composite(wm_3d, (xc, cy - wm_h // 2))
        _, _, _, ac = fg_c.split()
        sh_c = Image.merge("RGBA", (Image.new("L", fg_c.size, 0), Image.new("L", fg_c.size, 0), Image.new("L", fg_c.size, 0), ac.point(lambda x: int(x * 0.55))))
        can_c = Image.new("RGBA", fg_c.size, (0, 0, 0, 0))
        can_c.paste(sh_c, (2, 4))
        can_c = can_c.filter(ImageFilter.GaussianBlur(radius=4))
        can_c.alpha_composite(fg_c)
        can_c.save(os.path.join(brand_dir, "18_top_right_vietsub_3d_wordmark.png"))

    print(f"[BRANDING] Generated Top-Left Logo (13) & Top-Right Badges (14=pill, 15=floating, 18=3d) in {brand_dir}", flush=True)


def burn_dual_corner_to_video(in_mp4: str, out_mp4: str, brand_dir: str, style: str = "floating"):
    tl_png = os.path.join(brand_dir, "13_top_left_logo_large.png")
    badge_map = {
        "floating": "15_top_right_vietsub_floating.png",
        "pill": "14_top_right_vietsub_pill.png",
        "3d_wordmark": "18_top_right_vietsub_3d_wordmark.png",
    }
    tr_png = os.path.join(brand_dir, badge_map.get(style, "15_top_right_vietsub_floating.png"))
    if not os.path.exists(tl_png) or not os.path.exists(tr_png):
        build_dual_corner_badges(brand_dir)

    tl_im = Image.open(tl_png)
    tr_im = Image.open(tr_png)
    tl_x, tl_y = 42, 22
    center_y = tl_y + tl_im.height // 2
    tr_y = center_y - tr_im.height // 2

    res = subprocess.run(
        [ap.find_tool("ffprobe") if ap else "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", in_mp4],
        capture_output=True, text=True, check=True
    )
    total_sec = float(res.stdout.strip() or 1.0)
    tmp_out = out_mp4 + ".tmp_branding.mp4"

    print(f"[PLAN] Burning Dual-Corner Branding (style={style}) onto {in_mp4} ({total_sec:.2f}s) -> {out_mp4}", flush=True)
    vcodec_args = ap.vendor_h264_encode_args("5000k") if ap else ["-c:v", "h264_videotoolbox", "-b:v", "5000k", "-pix_fmt", "yuv420p"]
    cmd = [
        ap.find_tool("ffmpeg") if ap else "ffmpeg", "-y", "-nostats", "-progress", "pipe:1",
        "-i", in_mp4,
        "-i", tl_png,
        "-i", tr_png,
        "-filter_complex", f"[0:v][1:v]overlay={tl_x}:{tl_y}[v1];[v1][2:v]overlay=W-w-42:{tr_y},setsar=1:1[vout]",
        "-map", "[vout]",
        "-map", "0:a:0",
        "-sn",
        *vcodec_args,
        "-c:a", "copy",
        "-movflags", "+faststart",
        tmp_out
    ]

    t0 = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    last_print = t0
    cur_out_time = 0.0
    cur_speed = ""

    for line in proc.stdout:
        line = line.strip()
        if line.startswith("out_time_ms="):
            try:
                cur_out_time = int(line.split("=")[1]) / 1_000_000.0
            except ValueError:
                pass
        elif line.startswith("speed="):
            cur_speed = line.split("=")[1]
        now = time.time()
        if now - last_print >= 10.0:
            pct = min(100.0, cur_out_time / total_sec * 100.0)
            print(f"[PROGRESS] Branding Burn: {cur_out_time:.1f}s / {total_sec:.1f}s ({pct:.1f}%) | speed={cur_speed} | elapsed={now - t0:.1f}s", flush=True)
            last_print = now

    ret = proc.wait()
    if ret != 0:
        print(f"[ERROR] ffmpeg failed with code {ret}", file=sys.stderr)
        sys.exit(ret)

    shutil.move(tmp_out, out_mp4)
    print(f"[DONE] Dual-Corner Branded 16:9 MP4 saved to {out_mp4} in {time.time() - t0:.1f}s", flush=True)


def clean_and_brand_series_poster(
    raw_poster_path: str,
    series_dir: str,
    aspect: str,
    brand_dir: str,
    slug: str = "",
):
    """
    Removes the bottom-right Gemini 4-point star (✦) from a Google Flow generated series poster
    (9:16 or 16:9) via feathered texture patching and saves both the clean and branded versions
    into `<series_dir>/posters/` (Series-level folder, separate from episode deliverables).
    """
    posters_dir = os.path.join(series_dir, "posters")
    os.makedirs(posters_dir, exist_ok=True)
    series_slug = slug or os.path.basename(os.path.abspath(series_dir))

    im = Image.open(raw_poster_path).convert("RGB")
    w, h = im.size
    arr = np.array(im)

    def patch_box(y1, y2, x1, x2, sy1, sy2, sx1, sx2):
        hb, wb = y2 - y1, x2 - x1
        src = arr[sy1:sy2, sx1:sx2].copy()
        patch = np.array(Image.fromarray(src).resize((wb, hb), Image.Resampling.BICUBIC), dtype=np.float32)
        yy, xx = np.mgrid[0:hb, 0:wb]
        dy = np.minimum(yy, hb - 1 - yy) / max(1.0, hb * 0.18)
        dx = np.minimum(xx, wb - 1 - xx) / max(1.0, wb * 0.18)
        alpha = np.clip(np.minimum(dy, dx), 0.0, 1.0)[..., None]
        orig = arr[y1:y2, x1:x2].astype(np.float32)
        arr[y1:y2, x1:x2] = (patch * alpha + orig * (1.0 - alpha)).astype(np.uint8)

    if aspect == "9x16":
        patch_box(int(h * 0.92), int(h * 0.985), int(w * 0.88), int(w * 0.97), int(h * 0.84), int(h * 0.905), int(w * 0.88), int(w * 0.97))
        target_size = (1080, 1920)
        tl_pos = (36, 42)
        tr_margin_x, tr_y = 36, 78
    else:
        patch_box(int(h * 0.82), int(h * 0.955), int(w * 0.89), int(w * 0.965), int(h * 0.82), int(h * 0.955), int(w * 0.80), int(w * 0.875))
        target_size = (1920, 1080)
        tl_pos = (42, 22)
        tr_margin_x, tr_y = 42, 58

    im_clean = Image.fromarray(arr).resize(target_size, Image.Resampling.LANCZOS)
    clean_out = os.path.join(posters_dir, f"poster_{series_slug}_vi_{aspect}_clean.png")
    im_clean.save(clean_out)

    base = im_clean.convert("RGBA")
    tl_p = os.path.join(brand_dir, "13_top_left_logo_large.png")
    tr_p = os.path.join(brand_dir, "15_top_right_vietsub_floating.png")
    if os.path.exists(tl_p) and os.path.exists(tr_p):
        tl = Image.open(tl_p).convert("RGBA")
        tr = Image.open(tr_p).convert("RGBA")
        tl_a = np.array(tl, dtype=np.float32)
        tl_a[:, :, 3] *= 0.92
        tr_a = np.array(tr, dtype=np.float32)
        tr_a[:, :, 3] *= 0.92
        base.alpha_composite(Image.fromarray(tl_a.astype(np.uint8)), tl_pos)
        base.alpha_composite(Image.fromarray(tr_a.astype(np.uint8)), (target_size[0] - tr.width - tr_margin_x, tr_y))

    branded_out = os.path.join(posters_dir, f"poster_{series_slug}_vi_{aspect}_branded.png")
    base.convert("RGB").save(branded_out)
    print(f"[SERIES POSTER] Saved {aspect} clean & branded posters -> {posters_dir}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Channel Branding, 2x2 Grid Slicing, Series Posters & 16:9 Dual-Corner Watermark Engine.")
    parser.add_argument("--brand-dir", default=os.environ.get("AICN_BRAND_DIR", ""), help="Project-level brand assets directory (or AICN_BRAND_DIR env).")
    parser.add_argument("--series-dir", default="", help="Series folder path (e.g. workspace/drama-quy-khu-s01) where posters/ belong.")
    parser.add_argument("--slice-grid", default="", help="Optional path to 2x2 Master Brand Grid JPEG/PNG to slice into RGBA assets.")
    parser.add_argument("--build-badges", action="store_true", help="Build Top-Left Large Logo & Top-Right Vietsub Badges.")
    parser.add_argument("--channel-name", default="Đậu Đỏ", help="Channel display name for the Top-Right attribution badge.")
    parser.add_argument("--credit-prefix", default="Vietsub by ", help="Prefix text before channel name.")
    parser.add_argument("--style", choices=["floating", "pill", "3d_wordmark"], default="floating", help="Top-Right badge style (default: floating).")
    parser.add_argument("--clean-poster-9x16", default="", help="Raw Flow 9:16 poster path to remove Gemini star and save to <series-dir>/posters/.")
    parser.add_argument("--clean-poster-16x9", default="", help="Raw Flow 16:9 poster path to remove Gemini star and save to <series-dir>/posters/.")
    parser.add_argument("--burn-video", default="", help="Optional path to 16:9 MP4 to burn dual-corner watermark onto.")
    parser.add_argument("--output-video", default="", help="Optional output MP4 path (defaults to overwriting --burn-video safely).")
    args = parser.parse_args()

    if args.slice_grid:
        slice_brand_grid(args.slice_grid, args.brand_dir)
    if args.build_badges:
        build_dual_corner_badges(args.brand_dir, args.channel_name, args.credit_prefix)
    if args.clean_poster_9x16 and args.series_dir:
        clean_and_brand_series_poster(args.clean_poster_9x16, args.series_dir, "9x16", args.brand_dir)
    if args.clean_poster_16x9 and args.series_dir:
        clean_and_brand_series_poster(args.clean_poster_16x9, args.series_dir, "16x9", args.brand_dir)
    if args.burn_video:
        out_v = args.output_video or args.burn_video
        burn_dual_corner_to_video(args.burn_video, out_v, args.brand_dir, args.style)


if __name__ == "__main__":
    main()
