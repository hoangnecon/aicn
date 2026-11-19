#!/usr/bin/env python3
"""
aicn.py - incremental build engine for the AICN drama re-up / Vietsub / dub pipeline.

Wraps the existing Antigravity stage skills (video-localization-orchestrator,
video-asr-visual-audit, zh-vi-script-adapter, vieneu-voice-pacing-mixer,
video-localization-qa-review) and adds:

  discover        read a root folder: series hubs, units, gate status, progress
  init-series     scaffold a Series Hub (CHANGELOG, PROGRESS, bible, inbox, units)
  adopt           add the incremental layer to a unit made by init_workspace.py
  plan            dry-run: which chunks / TTS segments are dirty and why
  tts-sync        content-addressed TTS cache around synthesize_and_audit_pacing.py
  render          style-B overlay, rendered per chunk, cached by content hash,
                  then concat (-c copy) + mux master audio -> media/.staging_render.mp4
  verify-overlay  Stage 4 gate for style B (blank coverage, line count, chunks, duration)
  progress        rebuild the hub PROGRESS.md table
  log             append a line to CHANGELOG.md and refresh PROGRESS.md

Nothing here re-runs Whisper / Demucs / the bbox scan. Stage 1 outputs are frozen
unless the source hash changes (see `plan`).
"""
import argparse
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime

SKILL_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
VENDOR = os.path.join(SKILL_DIR, "vendor")

RENDERER_VERSION = "styleB-1.4"
TTS_ENGINE_TAG = "vieneu-v3-turbo-48k"
DEFAULT_STYLE = {
    "name": "B_hug_box",
    "font_path": "/System/Library/Fonts/Avenir Next.ttc",
    "font_index": 2,            # Demi Bold
    "font_size": 48,
    "text_rgb": [255, 255, 255],
    "plate_rgba": [0, 0, 0, 135],
    "plate_radius": 10,
    "pad_x": 20, "pad_top": 10, "pad_bottom": 12,
    "line_gap": 12,
    "max_line_px": 1500,
    "max_lines": 2,
    "blur_sigma": 22, "blur_darken": 0.82, "mask_feather": 10,
    "box_pad_x": 18, "box_pad_y": 14,
    "temporal_pad_s": 0.05,
    "drift_tolerance_s": 0.30,
}
ENCODE = {"vcodec": "h264_videotoolbox", "bitrate": "5000k", "pix_fmt": "yuv420p"}


# ----------------------------------------------------------------- utilities
def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def jload(p, default=None):
    if not os.path.exists(p):
        return default
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def jdump(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def h_obj(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class FastHash:
    """sha256 with a (size, mtime_ns) stat cache so big media is hashed once."""

    def __init__(self, ws):
        self.path = os.path.join(ws, ".cache", "stat_hash.json")
        self.db = jload(self.path, {})

    def __call__(self, p):
        st = os.stat(p)
        k = os.path.abspath(p)
        rec = self.db.get(k)
        if rec and rec["size"] == st.st_size and rec["mtime_ns"] == st.st_mtime_ns:
            return rec["sha256"]
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        self.db[k] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": h.hexdigest()}
        return self.db[k]["sha256"]

    def save(self):
        jdump(self.path, self.db)


def cicd():
    """Import the existing cicd_gate.py so gate tokens stay format-compatible."""
    cands = [os.environ.get("AICN_CICD_DIR", ""),
             os.path.join(SKILL_DIR, "vendor", "video-localization-orchestrator", "scripts"),
             os.path.expanduser("~/.gemini/config/skills/video-localization-orchestrator/scripts"),
             os.path.join(os.getcwd(), ".agents/skills/video-localization-orchestrator/scripts")]
    for c in cands:
        if c and os.path.exists(os.path.join(c, "cicd_gate.py")):
            sys.path.insert(0, c)
            import cicd_gate  # noqa
            return cicd_gate
    sys.exit("[aicn] cicd_gate.py not found; set AICN_CICD_DIR")


def is_unit(d):
    return os.path.exists(os.path.join(d, "config", "project_config.json"))


def is_hub(d):
    return os.path.isdir(os.path.join(d, "units")) and os.path.exists(os.path.join(d, "config", "series_bible.json"))


def gate_status(ws):
    out = []
    for n in range(6):
        g = jload(os.path.join(ws, ".cicd", f"stage_{n}_gate.json"))
        if not g:
            out.append("-")
        elif g.get("status") != "PASS":
            out.append("F")
        else:   # 'x' = signed PASS but a hashed artifact is gone (e.g. media deleted): gate no longer holds
            gone = any(not os.path.exists(k if os.path.isabs(k) else os.path.join(ws, k)) for k in g.get("artifacts", {}))
            out.append("x" if gone else "P")
    return "".join(out)


# ----------------------------------------------------------------- discover
def cmd_discover(a):
    root = os.path.abspath(a.root)
    hubs = [d for d in glob.glob(os.path.join(root, "**"), recursive=False)]
    found = []
    for base in [root] + sorted(glob.glob(os.path.join(root, "workspace", "*"))) + sorted(glob.glob(os.path.join(root, "*"))):
        if os.path.isdir(base) and is_hub(base) and base not in found:
            found.append(base)
    if not found:
        print(f"[discover] no Series Hub under {root} (looked in ./ and ./workspace/*)")
    for hub in found:
        print(f"\nSERIES  {os.path.relpath(hub, root)}")
        for f in ("CHANGELOG.md", "PROGRESS.md", "config/series_bible.json", "00_series_manifest.md"):
            print(f"  {'ok ' if os.path.exists(os.path.join(hub, f)) else 'MISSING'} {f}")
        inbox = glob.glob(os.path.join(hub, "source_inbox", "**", "*.mp4"), recursive=True)
        print(f"  source_inbox: {len(inbox)} mp4")
        print("  unit                         gates(0-5)  adopted  final")
        for u in sorted(glob.glob(os.path.join(hub, "units", "*"))):
            if not is_unit(u):
                continue
            adopted = os.path.exists(os.path.join(u, ".cache", "build_state.json")) or os.path.exists(os.path.join(u, "CHECKLIST.md"))
            final = glob.glob(os.path.join(u, "media", "final_*_vi.mp4")) + glob.glob(os.path.join(u, "deliverables", "16x9", "*.mp4"))
            print(f"  {os.path.basename(u):28s} {gate_status(u):10s}  {'yes' if adopted else 'no ':3s}      {'yes' if final else 'no'}")
    print("\nlegend: P=PASS F=FAIL x=PASS but artifact deleted -=missing. Next stage for a unit = first non-P position.")


# ----------------------------------------------------------------- scaffolding
TPL_DIR = os.path.join(SKILL_DIR, "templates")


def render_tpl(name, **kw):
    with open(os.path.join(TPL_DIR, name), "r", encoding="utf-8") as f:
        s = f.read()
    for k, v in kw.items():
        s = s.replace("{{" + k + "}}", str(v))
    return s


def write_if_absent(p, content):
    if os.path.exists(p):
        print(f"  keep    {p}")
        return
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  create  {p}")


def cmd_init_series(a):
    hub = os.path.abspath(os.path.join(a.workspace_root, a.series_slug))
    for d in ("config", "source_inbox", "units", "deliverables/16x9", "deliverables/9x16",
              "deliverables/subtitles", "deliverables/episodes", "posters", ".cache/chunks", ".cache/tts"):
        os.makedirs(os.path.join(hub, d), exist_ok=True)
    kw = dict(series=a.series_slug, title=a.title or a.series_slug, date=now(), total=a.total_episodes,
              unit_size=a.unit_size, rights=a.rights)
    write_if_absent(os.path.join(hub, "CHANGELOG.md"), render_tpl("CHANGELOG.md", **kw))
    write_if_absent(os.path.join(hub, "PROGRESS.md"), render_tpl("PROGRESS.md", **kw))
    write_if_absent(os.path.join(hub, "config", "rights.json"), json.dumps(
        {"series": a.series_slug, "basis": a.rights, "proof": "", "platforms_allowed": [], "content_id_risk": "unknown"},
        ensure_ascii=False, indent=2))
    write_if_absent(os.path.join(hub, "config", "series_bible.json"), json.dumps(
        {"series_slug": a.series_slug, "title_zh": "", "title_vi": a.title or "", "characters": [],
         "pronoun_matrix": [], "glossary": [], "voices": {}, "overlay_style": DEFAULT_STYLE},
        ensure_ascii=False, indent=2))
    print("[init-series] done. Next: drop episodes in source_inbox/<batch>/, then init_workspace.py --topology series_episodic, then `aicn.py adopt`.")


def cmd_adopt(a):
    ws = os.path.abspath(a.unit)
    if not is_unit(ws):
        sys.exit(f"[adopt] {ws} has no config/project_config.json - run init_workspace.py first")
    cfg = jload(os.path.join(ws, "config", "project_config.json"))
    for d in (".cache/chunks", ".cache/tts", ".cache/plates", "media/chunks"):
        os.makedirs(os.path.join(ws, d), exist_ok=True)
    kw = dict(unit=cfg.get("slug", os.path.basename(ws)), series=cfg.get("series_slug", ""), date=now(),
              mode=cfg.get("localization_mode", ""), style=DEFAULT_STYLE["name"])
    write_if_absent(os.path.join(ws, "CHECKLIST.md"), render_tpl("CHECKLIST.md", **kw))
    write_if_absent(os.path.join(ws, "CHANGELOG.md"), render_tpl("UNIT_CHANGELOG.md", **kw))
    style_p = os.path.join(ws, "config", "overlay_style.json")
    if not os.path.exists(style_p):
        hub_bible = jload(os.path.join(cfg.get("series_hub_dir", ""), "config", "series_bible.json"), {}) or {}
        bs = hub_bible.get("overlay_style")   # older bibles store a style name string here
        jdump(style_p, bs if isinstance(bs, dict) else DEFAULT_STYLE)
        print(f"  create  {style_p}")
    state_p = os.path.join(ws, ".cache", "build_state.json")
    if not os.path.exists(state_p):
        jdump(state_p, {"created": now(), "chunks": {}, "tts": {}, "stage1_key": None})
        print(f"  create  {state_p}")
    print("[adopt] done.")


# ----------------------------------------------------------------- model
def load_unit(ws):
    prof = jload(os.path.join(ws, "data", "00_video_profile.json"), {})
    eps = (jload(os.path.join(ws, "data", "00_episodes_manifest.json"), {}) or {}).get("episodes", [])
    cues = jload(os.path.join(ws, "data", "03_aligned_subtitles.json"))
    if cues is None:
        cues = jload(os.path.join(ws, "data", "02_bilingual_script.json"), [])
        cues = [dict(c, slot_start=c["start"], slot_end=c["end"]) for c in cues]
    if isinstance(cues, dict):
        cues = cues.get("segments", cues.get("cues", []))
    scan = jload(os.path.join(ws, "data", "01_zh_bbox_scan.json"), {}) or {}
    st_ = jload(os.path.join(ws, "config", "overlay_style.json"), {})
    style = dict(DEFAULT_STYLE, **(st_ if isinstance(st_, dict) else {}))
    apply_display_windows(cues, scan.get("hardsub_intervals", []), style)
    return prof, eps, cues, scan, style


def apply_display_windows(cues, ivs, style):
    """Display timing follows the Chinese hardsub; audio timing (slot_*) is untouched.
    - hold: a cue ending inside a Chinese interval stays up until that interval ends or the next cue starts
    - snap: the first cue inside an interval starts with it if it would start <= max_snap late
    Result in c['show_a'], c['show_b']; used by render keys, renderer and the Stage 4 gate."""
    max_snap = style.get("max_snap_s", 1.0)
    order = sorted(cues, key=lambda c: float(c["slot_start"]))
    for i, c in enumerate(order):
        a_, b_ = float(c["slot_start"]), float(c["slot_end"])
        prev_end = float(order[i - 1]["slot_end"]) if i else -1e9
        nxt = float(order[i + 1]["slot_start"]) if i + 1 < len(order) else 1e9
        for iv in ivs:
            if iv["start"] <= a_ < iv["end"] and 0 < a_ - iv["start"] <= max_snap and prev_end <= iv["start"] + 0.3:   # previous cue may brush the new line; the no-overlap pass trims it
                a_ = iv["start"]
        for iv in ivs:
            # hold only on the cue's own line: it must start at/after that line began (not merely brush its head)
            if iv["start"] <= b_ < iv["end"] and b_ - max(a_, iv["start"]) >= 0.25:
                b_ = max(b_, min(iv["end"], nxt))
        fq = style.get("display_fps", 30.0)        # frame-quantized: sub-frame TTS jitter never dirties a chunk
        c["show_a"], c["show_b"] = round(round(a_ * fq) / fq, 3), round(round(b_ * fq) / fq, 3)
    for i in range(len(order) - 1):  # never overlap the next cue
        order[i]["show_b"] = min(order[i]["show_b"], order[i + 1]["show_a"])


def chunk_windows(prof, eps, chunk_s):
    """Episode boundaries are the natural chunks; long episodes are split every chunk_s."""
    dur = float(prof.get("duration", 0))
    bounds = []
    if eps:
        for e in eps:
            s, t = float(e["timeline_start_s"]), float(e["timeline_end_s"])
            n = max(1, int((t - s) // chunk_s + (1 if (t - s) % chunk_s > 1 else 0)))
            step = (t - s) / n
            for i in range(n):
                bounds.append((e["ep_id"] + (f"_{i:02d}" if n > 1 else ""), s + i * step, s + (i + 1) * step))
    else:
        n = max(1, int(dur // chunk_s) + 1)
        step = dur / n
        bounds = [(f"c{i:03d}", i * step, (i + 1) * step) for i in range(n)]
    return bounds


def chunk_inputs(cid, s, t, cues, scan, style, src_sha, prof):
    pad = style["temporal_pad_s"]
    my_cues = [{"id": c["id"], "a": c["show_a"], "b": c["show_b"], "text": c["sub_text"]} for c in cues
               if c["show_b"] + pad > s and c["show_a"] - pad < t]
    my_ivs = [{"a": iv["start"], "b": iv["end"], "bbox": iv["bbox"]} for iv in scan.get("hardsub_intervals", [])
              if iv["end"] + pad > s and iv["start"] - pad < t]
    font_sha = hashlib.sha256(open(style["font_path"], "rb").read()).hexdigest()[:16] if os.path.exists(style["font_path"]) else "nofont"
    inp = {"v": RENDERER_VERSION, "src": src_sha, "win": [round(s, 3), round(t, 3)], "cues": my_cues,
           "ivs": my_ivs, "style": style, "font": font_sha, "enc": ENCODE,
           "strip_y0": scan.get("strip_y0", prof.get("strip_y0", 880)), "fps": prof.get("r_fps", 30)}
    return inp, h_obj(inp)


def stage1_key(ws, fh):
    src = os.path.join(ws, "source", "master_source.mp4")
    return h_obj({"src": fh(src) if os.path.exists(src) else None, "asr": "mlx-whisper", "scan_fps": 5})


def cmd_plan(a, quiet=False):
    ws = os.path.abspath(a.unit)
    fh = FastHash(ws)
    prof, eps, cues, scan, style = load_unit(ws)
    state = jload(os.path.join(ws, ".cache", "build_state.json"), {"chunks": {}, "tts": {}})
    src = os.path.join(ws, "source", "master_source.mp4")
    src_sha = fh(src) if os.path.exists(src) else "NO_SOURCE"
    rows, dirty = [], []
    for cid, s, t in chunk_windows(prof, eps, a.chunk_s):
        inp, key = chunk_inputs(cid, s, t, cues, scan, style, src_sha, prof)
        cached = os.path.exists(os.path.join(ws, ".cache", "chunks", key + ".mp4"))
        prev = state["chunks"].get(cid, {})
        why = "cached" if cached else ("new" if not prev else "changed:" + ",".join(
            k for k in ("cues", "ivs", "style", "src", "win", "font", "enc", "v") if prev.get("parts", {}).get(k) != h_obj(inp[k])))
        rows.append((cid, s, t, len(inp["cues"]), key[:12], why))
        if not cached:
            dirty.append(cid)
    s1 = stage1_key(ws, fh)
    fh.save()
    if not quiet:
        print(f"[plan] unit={os.path.basename(ws)} chunks={len(rows)} dirty={len(dirty)}")
        for r in rows:
            print(f"  {r[0]:10s} {r[1]:8.2f}-{r[2]:8.2f}s cues={r[3]:3d} key={r[4]} {r[5]}")
        if state.get("stage1_key") and state["stage1_key"] != s1:
            print("  !! source changed since Stage 1 -> Stage 1 must be re-run for this unit")
        else:
            print("  stage1: frozen (source unchanged) - do NOT re-run Whisper/scan")
    return dirty


# ----------------------------------------------------------------- TTS cache
def tts_key(item, default_voice):
    return h_obj({"t": item.get("tts_text", ""), "v": item.get("voice") or default_voice, "e": TTS_ENGINE_TAG})


def cmd_tts_sync(a):
    ws = os.path.abspath(a.unit)
    script = jload(os.path.join(ws, "data", "02_bilingual_script.json"), [])
    wav_dir = os.path.join(ws, "media", "tts_segments")
    cache = os.path.join(ws, ".cache", "tts")
    os.makedirs(wav_dir, exist_ok=True)
    os.makedirs(cache, exist_ok=True)
    idx_p = os.path.join(ws, ".cache", "tts_index.json")
    index = jload(idx_p, {})
    kept = restored = stale = stored = 0
    for i, item in enumerate(script):
        k = tts_key(item, a.voice)
        seg = os.path.join(wav_dir, f"seg_{i:04d}.wav")
        cpath = os.path.join(cache, k + ".wav")
        if a.store:
            if os.path.exists(seg) and not os.path.exists(cpath):
                shutil.copy2(seg, cpath); stored += 1
            if os.path.exists(seg):
                index[str(i)] = k
            continue
        if os.path.exists(seg) and index.get(str(i)) == k:
            kept += 1
        elif os.path.exists(cpath):
            shutil.copy2(cpath, seg); index[str(i)] = k; restored += 1
        elif os.path.exists(seg):
            os.remove(seg); index.pop(str(i), None); stale += 1
    for extra in glob.glob(os.path.join(wav_dir, "seg_*.wav")):
        n = int(os.path.basename(extra)[4:8])
        if n >= len(script):
            os.remove(extra); index.pop(str(n), None); stale += 1
    jdump(idx_p, index)
    if a.store:
        print(f"[tts-sync --store] cached {stored} new wavs")
    else:
        missing = sum(1 for i in range(len(script)) if not os.path.exists(os.path.join(wav_dir, f"seg_{i:04d}.wav")))
        print(f"[tts-sync] kept={kept} restored_from_cache={restored} removed_stale={stale} to_synthesize={missing}")


# ----------------------------------------------------------------- style B renderer
def split_lines(text, font, max_px, max_lines):
    from PIL import Image, ImageDraw
    d = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    w = lambda s: d.textbbox((0, 0), s, font=font)[2]
    if w(text) <= max_px or max_lines == 1:
        return [text]
    words = text.split()
    best, best_score = None, 1e18
    for i in range(1, len(words)):
        l1, l2 = " ".join(words[:i]), " ".join(words[i:])
        score = abs(w(l1) - w(l2)) + (0 if l1[-1] in ",;:!?.—" else 120) + (1e6 if max(w(l1), w(l2)) > max_px else 0)
        if score < best_score:
            best, best_score = [l1, l2], score
    return best


def build_plate(cue, style, W, centre_y, font):
    """RGBA full-width band image for one cue + geometry. Cached by caller."""
    from PIL import Image, ImageDraw
    lines = split_lines(cue["text"], font, style["max_line_px"], style["max_lines"])
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    boxes = [probe.textbbox((0, 0), l, font=font) for l in lines]
    lh = max(b[3] - b[1] for b in boxes)
    gap = style["line_gap"]
    total = lh * len(lines) + gap * (len(lines) - 1)
    top = int(centre_y - total / 2)
    H = total + style["pad_top"] + style["pad_bottom"] + 8
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    oy = top - style["pad_top"] - 4          # band y of image row 0
    x0s, x1s = [], []
    for i, (l, b) in enumerate(zip(lines, boxes)):
        tw = b[2] - b[0]
        x = W // 2 - tw // 2
        y = style["pad_top"] + 4 + i * (lh + gap)
        d.rounded_rectangle((x - style["pad_x"], y - style["pad_top"], x + tw + style["pad_x"], y + lh + style["pad_bottom"]),
                            radius=style["plate_radius"], fill=tuple(style["plate_rgba"]))
        x0s.append(x - style["pad_x"]); x1s.append(x + tw + style["pad_x"])
    for i, (l, b) in enumerate(zip(lines, boxes)):
        tw = b[2] - b[0]
        d.text((W // 2 - tw // 2 - b[0], style["pad_top"] + 4 + i * (lh + gap) - b[1]), l, font=font, fill=tuple(style["text_rgb"]))
    geom = {"lines": lines, "n_lines": len(lines), "box_abs": [min(x0s), oy, max(x1s), oy + H], "max_line_px": max(x1s) - min(x0s)}
    return img, oy, geom


def render_chunk(ws, cid, s, t, inp, out_mp4, prof, plates):
    import numpy as np
    import cv2
    W, Hh = int(prof.get("width", 1920)), int(prof.get("height", 1080))
    fps = float(inp["fps"])
    style = inp["style"]
    y_lo, y_hi = 760, Hh                      # band we touch (hardsub + plates)
    pad = style["temporal_pad_s"]
    sy = inp["strip_y0"]
    grow = int(style["mask_feather"] * 1.5)   # feather eats the box edge; grow so the box itself gets full blur

    def make_mask(x0, x1, ry0, ry1):
        box = (max(0, x0 - style["box_pad_x"] - grow), max(0, sy + ry0 - style["box_pad_y"] - grow - y_lo),
               min(W, x1 + style["box_pad_x"] + grow), min(y_hi - y_lo - 1, sy + ry1 + style["box_pad_y"] + grow - y_lo))
        m = np.zeros((y_hi - y_lo, W), np.uint8)
        r = max(1, (box[3] - box[1]) // 2)
        cv2.rectangle(m, (box[0] + r, box[1]), (box[2] - r, box[3]), 255, -1)
        cv2.circle(m, (box[0] + r, box[1] + r), r, 255, -1); cv2.circle(m, (box[2] - r, box[1] + r), r, 255, -1)
        m = cv2.GaussianBlur(m, (0, 0), style["mask_feather"]).astype(np.float32)[..., None] / 255.0
        ys, xs = np.nonzero(m[..., 0] > 0.004)
        return m, (int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1)

    def refine_x(band_u8, x0, x1, ry0, ry1):
        """The 5 fps scan can clip outer glyphs. Re-measure on the real frame: outlined white glyph
        pixels in the line's rows; grow [x0,x1] across glyph columns separated by gaps <= 90 px."""
        r0, r1 = max(0, sy + ry0 - 6 - y_lo), min(band_u8.shape[0], sy + ry1 + 6 - y_lo)
        roi = band_u8[r0:r1]
        white = roi.min(axis=2) > 200
        dark = cv2.dilate((roi.max(axis=2) < 80).astype(np.uint8), np.ones((7, 7), np.uint8)) > 0
        cols = np.nonzero((white & dark).sum(axis=0) >= 2)[0]
        for _ in range(2):
            left = cols[(cols < x0) & (cols >= x0 - 90)]
            right = cols[(cols > x1) & (cols <= x1 + 90)]
            if len(left):
                x0 = int(left.min())
            if len(right):
                x1 = int(right.max())
            if not len(left) and not len(right):
                break
        return x0, x1

    masks = []
    for iv in inp["ivs"]:
        x0, x1, ry0, ry1 = iv["bbox"]
        m, roi = make_mask(x0, x1, ry0, ry1)
        masks.append({"a": iv["a"] - pad, "b": iv["b"] + pad, "bbox": [x0, x1, ry0, ry1], "m": m, "roi": roi, "refined": False})
    sig, mg = style["blur_sigma"], int(style["blur_sigma"] * 3)
    # Snap to the frame grid: chunk k owns frames [round(s*fps), round(t*fps)), so concatenated
    # chunks have exactly the source frame count -> zero cumulative drift against master audio.
    f0, f1 = int(round(s * fps)), int(round(t * fps))
    s = f0 / fps
    dec = subprocess.Popen(["ffmpeg", "-v", "error", "-ss", f"{s:.6f}", "-i", os.path.join(ws, "source", "master_source.mp4"),
                            "-an", "-sn", "-vf", f"fps={fps}", "-frames:v", str(f1 - f0),
                            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                           stdout=subprocess.PIPE)
    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{Hh}", "-r", str(fps),
                            "-i", "-", "-an", "-c:v", ENCODE["vcodec"], "-b:v", ENCODE["bitrate"], "-pix_fmt", ENCODE["pix_fmt"],
                            "-g", str(int(fps * 2)), "-video_track_timescale", "90000", out_mp4 + ".part.mp4"], stdin=subprocess.PIPE)
    fsz = W * Hh * 3
    plate_x = {}
    for cid_, (rgba, _) in plates.items():
        cols = np.nonzero(rgba[..., 3].max(axis=0))[0]
        plate_x[cid_] = (int(cols.min()), int(cols.max()) + 1) if len(cols) else (0, 1)
    i = 0
    while True:
        buf = dec.stdout.read(fsz)
        if len(buf) < fsz:
            break
        ts = s + i / fps
        fr = np.frombuffer(buf, np.uint8).reshape(Hh, W, 3).copy()
        band = fr[y_lo:y_hi]
        for mk in masks:
            if not mk["refined"] and mk["a"] <= ts <= mk["b"]:
                x0, x1, ry0, ry1 = mk["bbox"]
                nx0, nx1 = refine_x(band, x0, x1, ry0, ry1)
                if (nx0, nx1) != (x0, x1):
                    mk["m"], mk["roi"] = make_mask(nx0, nx1, ry0, ry1)
                    mk["bbox"] = [nx0, nx1, ry0, ry1]
                mk["refined"] = True
        act = [(mk["m"], mk["roi"]) for mk in masks if mk["a"] <= ts <= mk["b"]]
        touched = bool(act)
        if act:
            # Blur only the union ROI (+3 sigma context), at 1/4 resolution: same look, ~20x cheaper.
            y0r, y1r = min(r[0] for _, r in act), max(r[1] for _, r in act)
            x0r, x1r = min(r[2] for _, r in act), max(r[3] for _, r in act)
            cy0, cy1, cx0, cx1 = max(0, y0r - mg), min(band.shape[0], y1r + mg), max(0, x0r - mg), min(W, x1r + mg)
            ctx = band[cy0:cy1, cx0:cx1]
            small = cv2.resize(ctx, (max(1, (cx1 - cx0) // 4), max(1, (cy1 - cy0) // 4)), interpolation=cv2.INTER_AREA)
            small = cv2.GaussianBlur(small, (0, 0), sig / 4)
            bl = cv2.resize(small, (cx1 - cx0, cy1 - cy0), interpolation=cv2.INTER_LINEAR).astype(np.float32) * style["blur_darken"]
            m = act[0][0][cy0:cy1, cx0:cx1]
            for mm, _ in act[1:]:
                m = np.maximum(m, mm[cy0:cy1, cx0:cx1])
            band = band.astype(np.float32)
            band[cy0:cy1, cx0:cx1] = bl * m + band[cy0:cy1, cx0:cx1] * (1 - m)
        for c in inp["cues"]:
            if c["a"] - pad <= ts <= c["b"] + pad:
                rgba, oy = plates[c["id"]]
                y0 = oy - y_lo                       # oy is absolute frame y of plate row 0
                h = rgba.shape[0]
                ya, yb = max(0, y0), min(band.shape[0], y0 + h)
                xs_ = plate_x[c["id"]]
                al = rgba[ya - y0:yb - y0, xs_[0]:xs_[1], 3:4].astype(np.float32) / 255.0
                band = band.astype(np.float32)
                band[ya:yb, xs_[0]:xs_[1]] = rgba[ya - y0:yb - y0, xs_[0]:xs_[1], :3] * al + band[ya:yb, xs_[0]:xs_[1]] * (1 - al)
                touched = True
                break
        if touched:
            fr[y_lo:y_hi] = np.clip(band, 0, 255).astype(np.uint8)
        enc.stdin.write(fr.tobytes())
        i += 1
    enc.stdin.close(); enc.wait(); dec.wait()
    if enc.returncode != 0 or i == 0:
        raise RuntimeError(f"chunk {cid} encode failed (frames={i})")
    os.replace(out_mp4 + ".part.mp4", out_mp4)
    return i


def centre_y_of(scan, prof):
    import statistics
    ivs = scan.get("hardsub_intervals", [])
    sy = scan.get("strip_y0", prof.get("strip_y0", 880))
    if not ivs:
        return int(prof.get("height", 1080)) - 110
    return int(sy + statistics.median((iv["bbox"][2] + iv["bbox"][3]) / 2 for iv in ivs))


def cmd_render(a):
    import numpy as np
    from PIL import ImageFont
    ws = os.path.abspath(a.unit)
    if not a.no_gate:
        cicd().require_gate(ws, 3)
    fh = FastHash(ws)
    prof, eps, cues, scan, style = load_unit(ws)
    src_sha = fh(os.path.join(ws, "source", "master_source.mp4"))
    state = jload(os.path.join(ws, ".cache", "build_state.json"), {"chunks": {}, "tts": {}})
    font = ImageFont.truetype(style["font_path"], style["font_size"], index=style.get("font_index", 0))
    W = int(prof.get("width", 1920))
    cy = centre_y_of(scan, prof)
    plates, geom_log = {}, []
    for c in cues:
        img, oy, g = build_plate({"text": c["sub_text"]}, style, W, cy, font)
        plates[c["id"]] = (np.array(img), oy)
        geom_log.append({"id": c["id"], "slot_start": c["show_a"], "slot_end": c["show_b"], "style": "frosted_glass",
                         "style_name": style["name"], "lines": g["lines"], "n_lines": g["n_lines"],
                         "box": [g["box_abs"][0], g["box_abs"][1] - prof.get("strip_y0", 880), g["box_abs"][2],
                                 g["box_abs"][3] - prof.get("strip_y0", 880)], "line_px": g["max_line_px"]})
    jdump(os.path.join(ws, "data", "04_overlay_geometry_log.json"), {"renderer": RENDERER_VERSION, "centre_y": cy, "cues": geom_log})
    only = set(a.only.split(",")) if a.only else None
    t0 = time.time()
    order, made, reused = [], 0, 0
    for cid, s, t in chunk_windows(prof, eps, a.chunk_s):
        inp, key = chunk_inputs(cid, s, t, cues, scan, style, src_sha, prof)
        cpath = os.path.join(ws, ".cache", "chunks", key + ".mp4")
        if not os.path.exists(cpath):
            if only and cid not in only:
                sys.exit(f"[render] chunk {cid} is dirty but not in --only; refusing to concat stale output")
            print(f"[render] {cid} {s:.2f}-{t:.2f}s cues={len(inp['cues'])} -> rendering", flush=True)
            n = render_chunk(ws, cid, s, t, inp, cpath, prof, plates)
            made += 1
            print(f"[render] {cid} done frames={n} elapsed={time.time() - t0:.1f}s", flush=True)
        else:
            reused += 1
        state["chunks"][cid] = {"key": key, "win": [s, t], "parts": {k: h_obj(inp[k]) for k in
                                ("cues", "ivs", "style", "src", "win", "font", "enc", "v")}}
        order.append(cpath)
    lst = os.path.join(ws, "media", "chunks", "concat.txt")
    os.makedirs(os.path.dirname(lst), exist_ok=True)
    with open(lst, "w") as f:
        f.writelines(f"file '{p}'\n" for p in order)
    audio = os.path.join(ws, "media", "master_audio_48k.wav")
    out = os.path.join(ws, "media", ".staging_render.mp4")
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst]
    if os.path.exists(audio):
        cmd += ["-i", audio, "-map", "0:v", "-map", "1:a", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    else:
        cmd += ["-map", "0:v"]
    cmd += ["-c:v", "copy", "-sn", "-movflags", "+faststart", out]
    subprocess.run(cmd, check=True)
    state["last_render"] = {"at": now(), "rendered": made, "reused": reused, "output": out}
    jdump(os.path.join(ws, ".cache", "build_state.json"), state)
    fh.save()
    print(f"[render] chunks rendered={made} reused={reused} -> {out} ({time.time() - t0:.1f}s)")


# ----------------------------------------------------------------- Stage 4 gate (style B)
def cmd_verify_overlay(a):
    ws = os.path.abspath(a.unit)
    g = cicd()
    if not a.no_gate:
        g.require_gate(ws, 3)
    prof, eps, cues, scan, style = load_unit(ws)
    geom = jload(os.path.join(ws, "data", "04_overlay_geometry_log.json"), {}).get("cues", [])
    state = jload(os.path.join(ws, ".cache", "build_state.json"), {})
    ivs = scan.get("hardsub_intervals", [])
    W = int(prof.get("width", 1920))
    tol = style["drift_tolerance_s"]
    # Timing truth is the audio (Whisper slot_*), never moved. The only display error is "blank":
    # a Chinese line is blurred while no Vietnamese is on screen for > tol (untranslated line / missed clause).
    drift_bad = []
    spans = sorted((c["show_a"], c["show_b"]) for c in cues)
    for iv in ivs:
        t_, worst = iv["start"], 0.0
        for a_, b_ in spans:
            if b_ <= t_ or a_ >= iv["end"]:
                continue
            worst = max(worst, a_ - t_); t_ = max(t_, b_)
        worst = max(worst, iv["end"] - t_) if t_ > iv["start"] else iv["end"] - iv["start"]
        if worst > tol:
            drift_bad.append((round(iv["start"], 1), "blank", round(worst, 2)))
    # 2. lines / width
    lines_bad = [x["id"] for x in geom if x["n_lines"] > style["max_lines"] or x["line_px"] > W - 120]
    # 3. renderer masks every hardsub interval by construction; check every chunk output exists
    chunk_files = [os.path.join(ws, ".cache", "chunks", v["key"] + ".mp4") for v in state.get("chunks", {}).values()]
    missing_chunks = [p for p in chunk_files if not os.path.exists(p)]
    staging = os.path.join(ws, "media", ".staging_render.mp4")
    dur_ok = False
    if os.path.exists(staging):
        d = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", staging],
                                 capture_output=True, text=True).stdout.strip() or 0)
        dur_ok = abs(d - float(prof.get("duration", d))) <= 0.30
    checks = [
        {"id": "no_blank_blurred_hardsub", "weight": 30, "passed": not drift_bad,
         "detail": f"{len(drift_bad)} Chinese lines blurred with no Vietnamese > {tol}s: {drift_bad[:12]}"},
        {"id": "max_two_lines_and_width", "weight": 25, "passed": bool(geom) and not lines_bad and len(geom) == len(cues),
         "detail": f"geometry={len(geom)}/{len(cues)} bad={lines_bad[:12]}"},
        {"id": "per_interval_mask_chunks_present", "weight": 20, "passed": bool(chunk_files) and not missing_chunks,
         "detail": f"chunks={len(chunk_files)} missing={len(missing_chunks)} intervals={len(ivs)}"},
        {"id": "staging_render_duration", "weight": 25, "passed": dur_ok, "detail": staging},
    ]
    arts = ["data/04_overlay_geometry_log.json", "config/overlay_style.json", "media/.staging_render.mp4"]
    g.write_gate_token(ws, 4, "Style-B Overlay (incremental)", checks,
                       {"drift_bad": len(drift_bad), "chunks": len(chunk_files), "renderer": RENDERER_VERSION}, arts)


# ----------------------------------------------------------------- log / progress
def cmd_log(a):
    ws = os.path.abspath(a.unit)
    p = os.path.join(ws, "CHANGELOG.md")
    with open(p, "a", encoding="utf-8") as f:
        f.write(f"- {now()} [{a.kind}] {a.message}\n")
    cfg = jload(os.path.join(ws, "config", "project_config.json"), {})
    hub = cfg.get("series_hub_dir")
    if hub and os.path.exists(os.path.join(hub, "PROGRESS.md")):
        refresh_progress(hub)
    print(f"[log] appended to {p}")


def refresh_progress(hub):
    p = os.path.join(hub, "PROGRESS.md")
    if not os.path.exists(p):   # hubs created before this skill: add the files, rights stay "unknown" until a human sets them
        kw = dict(series=os.path.basename(hub), title=os.path.basename(hub), date=now(), total="?", unit_size="?", rights="unknown")
        write_if_absent(p, render_tpl("PROGRESS.md", **kw))
        write_if_absent(os.path.join(hub, "CHANGELOG.md"), render_tpl("CHANGELOG.md", **kw))
        write_if_absent(os.path.join(hub, "config", "rights.json"), json.dumps(
            {"series": kw["series"], "basis": "unknown", "proof": "", "platforms_allowed": [], "content_id_risk": "unknown"}, indent=2))
    with open(p, "r", encoding="utf-8") as f:
        txt = f.read()
    rows = ["| unit | gates 0-5 | last render | final |", "|---|---|---|---|"]
    for u in sorted(glob.glob(os.path.join(hub, "units", "*"))):
        if not is_unit(u):
            continue
        st = jload(os.path.join(u, ".cache", "build_state.json"), {}) or {}
        lr = st.get("last_render", {})
        fin = "yes" if glob.glob(os.path.join(u, "media", "final_*_vi.mp4")) else "no"
        rows.append(f"| {os.path.basename(u)} | `{gate_status(u)}` | {lr.get('at', '-')} ({lr.get('rendered', '-')} new/{lr.get('reused', '-')} reused) | {fin} |")
    block = "<!-- AUTO:UNITS -->\n" + "\n".join(rows) + "\n<!-- /AUTO:UNITS -->"
    if "<!-- AUTO:UNITS -->" in txt:
        pre, rest = txt.split("<!-- AUTO:UNITS -->", 1)
        post = rest.split("<!-- /AUTO:UNITS -->", 1)[1]
        txt = pre + block + post
    else:
        txt += "\n" + block + "\n"
    with open(p, "w", encoding="utf-8") as f:
        f.write(txt)


def cmd_progress(a):
    refresh_progress(os.path.abspath(a.hub))
    print("[progress] refreshed")


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("discover"); p.add_argument("--root", default="."); p.set_defaults(f=cmd_discover)
    p = sp.add_parser("init-series"); p.add_argument("--series-slug", required=True); p.add_argument("--title", default="")
    p.add_argument("--workspace-root", default="workspace"); p.add_argument("--total-episodes", default="?")
    p.add_argument("--unit-size", default=10); p.add_argument("--rights", required=True,
                   help="license basis: own | licensed:<contract> | cps:<platform> | mcn:<name>")
    p.set_defaults(f=cmd_init_series)
    p = sp.add_parser("adopt"); p.add_argument("--unit", required=True); p.set_defaults(f=cmd_adopt)
    p = sp.add_parser("plan"); p.add_argument("--unit", required=True); p.add_argument("--chunk-s", type=float, default=90)
    p.set_defaults(f=cmd_plan)
    p = sp.add_parser("tts-sync"); p.add_argument("--unit", required=True); p.add_argument("--voice", default="Trúc Ly")
    p.add_argument("--store", action="store_true"); p.set_defaults(f=cmd_tts_sync)
    p = sp.add_parser("render"); p.add_argument("--unit", required=True); p.add_argument("--chunk-s", type=float, default=90)
    p.add_argument("--only", default=""); p.add_argument("--no-gate", action="store_true", help="tests only")
    p.set_defaults(f=cmd_render)
    p = sp.add_parser("verify-overlay"); p.add_argument("--unit", required=True); p.add_argument("--no-gate", action="store_true")
    p.set_defaults(f=cmd_verify_overlay)
    p = sp.add_parser("log"); p.add_argument("--unit", required=True); p.add_argument("--kind", default="edit",
                      choices=["init", "stage", "edit", "fix", "render", "release", "note"]); p.add_argument("--message", required=True)
    p.set_defaults(f=cmd_log)
    p = sp.add_parser("progress"); p.add_argument("--hub", required=True); p.set_defaults(f=cmd_progress)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
