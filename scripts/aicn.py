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
FF_RENDERER_VERSION = "ff-1.1"
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
    "mask_mode": "blur",        # blur | solid (black box, about 12% faster)
    "solid_rgb": [0, 0, 0],
    "codec": "h264",            # h264 (default: q=58, ~1.86 GB/100m) | hevc (q=60, ~1.35 GB/100m) | h264_high (5000k)
    "quality": None,            # optional VideoToolbox -q:v override (1-100; e.g. 55-62)
}
ENCODE_PRESETS = {
    "h264":      {"vcodec": "h264_videotoolbox", "q": 58, "pix_fmt": "yuv420p", "hwdec": True},
    "hevc":      {"vcodec": "hevc_videotoolbox", "q": 60, "tag": "hvc1", "pix_fmt": "yuv420p", "hwdec": True},
    "h264_high": {"vcodec": "h264_videotoolbox", "bitrate": "5000k", "pix_fmt": "yuv420p", "hwdec": True},
}
ENCODE = {"vcodec": "h264_videotoolbox", "bitrate": "5000k", "pix_fmt": "yuv420p"}  # legacy chunk renderer default


def resolve_encode(style, codec=None, quality=None):
    preset_name = codec or style.get("codec", "h264")
    enc = dict(ENCODE_PRESETS.get(preset_name, ENCODE_PRESETS["h264"]))
    q = quality if quality is not None else style.get("quality")
    if q is not None:
        enc["q"] = int(q)
        enc.pop("bitrate", None)
    return enc


def resolve_watermark(ws, brand_dir=None, width=1920):
    """Return dual-corner watermark spec dict (scaled cleanly on narrow/portrait widths) or None."""
    wm_scale = round(max(0.5, width / 1920.0), 3) if width < 1200 else 1.0

    def _spec(tl, tr, tl_x=42, tl_y=22, tr_mx=42, tr_y=58):
        return {
            "tl": tl,
            "tr": tr,
            "scale": wm_scale,
            "tl_x": int(round(tl_x * wm_scale)),
            "tl_y": int(round(tl_y * wm_scale)),
            "tr_mx": int(round(tr_mx * wm_scale)),
            "tr_y": int(round(tr_y * wm_scale)),
        }

    if brand_dir:
        bd = os.path.abspath(brand_dir)
        tl = os.path.join(bd, "13_top_left_logo_large.png")
        tr = os.path.join(bd, "15_top_right_vietsub_floating.png")
        if os.path.exists(tl) and os.path.exists(tr):
            return _spec(tl, tr)
        sys.exit(f"[aicn] --brand-dir missing 13_top_left_logo_large.png or 15_top_right_vietsub_floating.png in {bd}")
    cfg = jload(os.path.join(ws, "config", "project_config.json"), {}) or {}
    wm = cfg.get("watermark") or {}
    tl, tr = wm.get("top_left_logo", ""), wm.get("top_right_badge", "")
    if wm.get("enabled") and tl and tr and os.path.exists(tl) and os.path.exists(tr):
        return _spec(
            tl,
            tr,
            int(wm.get("top_left_x", 42)),
            int(wm.get("top_left_y", 22)),
            int(wm.get("top_right_margin_x", 42)),
            int(wm.get("top_right_y", 58)),
        )
    return None


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
        units = [u for u in sorted(glob.glob(os.path.join(hub, "units", "*"))) if is_unit(u)]
        n_pass = 0
        print("  working batch (units/*)      gates(0-5)  adopted  batch_mp4")
        for u in units:
            gs = gate_status(u)
            if len(gs) >= 6 and gs[5] == "P":
                n_pass += 1
            adopted = os.path.exists(os.path.join(u, ".cache", "build_state.json")) or os.path.exists(os.path.join(u, "CHECKLIST.md"))
            final = glob.glob(os.path.join(u, "media", "final_*_vi.mp4")) + glob.glob(os.path.join(u, "deliverables", "16x9", "*.mp4"))
            print(f"  {os.path.basename(u):28s} {gs:10s}  {'yes' if adopted else 'no ':3s}      {'yes' if final else 'no'}")
        full_mp4 = os.path.join(hub, "deliverables", f"final_{os.path.basename(hub)}_full_vi.mp4")
        if os.path.exists(full_mp4):
            print(f"  FULL RELEASE: READY ({os.path.relpath(full_mp4, hub)}, {os.path.getsize(full_mp4)/1e6:.1f} MB; {n_pass}/{len(units)} batches PASS)")
        else:
            print(f"  FULL RELEASE: PENDING ({n_pass}/{len(units)} working batches PASS -> run `merge-series` once all batches PASS)")
    print("\nlegend: P=PASS F=FAIL x=PASS but artifact deleted -=missing. Working batches are internal slices; publish only the full-series release.")


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
    import re
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", a.series_slug):
        sys.exit(
            f"[init-series] invalid --series-slug '{a.series_slug}': "
            "must be translated to Vietnamese, no accents, lowercase, hyphen-separated "
            "(e.g. 'nam-18-tuoi-toi-che-tao-than-khi')"
        )
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
        {"series_slug": a.series_slug, "source_language": getattr(a, "source_lang", "zh"), "title_zh": "", "title_vi": a.title or "", "characters": [],
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
def resolve_style_for_profile(base_style, prof):
    """Adapt Style-B typography and mask padding cleanly when width < 1000 (9:16 portrait)."""
    style = dict(DEFAULT_STYLE, **(base_style if isinstance(base_style, dict) else {}))
    W = int((prof or {}).get("width", 1920))
    if W < 1000:
        if style.get("font_size") == DEFAULT_STYLE["font_size"]:
            style["font_size"] = max(24, min(32, int(round(W * 28 / 720))))
        portrait_defaults = {
            "pad_x": 14,
            "pad_top": 7,
            "pad_bottom": 8,
            "line_gap": 7,
            "plate_radius": 8,
            "box_pad_x": 14,
            "box_pad_y": 12,
            "blur_sigma": 18,
        }
        for k, pv in portrait_defaults.items():
            if style.get(k) == DEFAULT_STYLE[k]:
                style[k] = pv
        safe_max_px = max(320, W - 120 - 2 * int(style["pad_x"]) - 12)
        style["max_line_px"] = min(int(style.get("max_line_px", safe_max_px)), safe_max_px)
    return style


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
    style = resolve_style_for_profile(st_, prof)
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
    if getattr(a, "engine", "ffmpeg") == "ffmpeg":
        from PIL import ImageFont
        if getattr(a, "mask", None):
            style["mask_mode"] = a.mask
        enc = resolve_encode(style, getattr(a, "codec", None), getattr(a, "quality", None))
        W = int(prof.get("width", 1920))
        wm = resolve_watermark(ws, getattr(a, "brand_dir", None), width=W)
        font = ImageFont.truetype(style["font_path"], style["font_size"], index=style.get("font_index", 0))
        cy = centre_y_of(scan, prof)
        plates = {c["id"]: build_plate({"text": c["sub_text"]}, style, W, cy, font)[:2] for c in cues}
        parts, key = ff_render_key(ws, fh, cues, scan, style, band_y0(prof, scan, style, plates), enc, wm)
        prev = state.get("render", {})
        staging = os.path.exists(os.path.join(ws, "media", ".staging_render.mp4"))
        if prev.get("key") == key and staging:
            why, dirty = "current", []
        else:
            changed = [k for k, v in parts.items() if prev.get("parts", {}).get(k) != h_obj(v)] if prev else ["new"]
            why, dirty = "stale: " + ",".join(changed or ["staging missing"]), ["unit"]
        s1 = stage1_key(ws, fh); fh.save()
        if not quiet:
            print(f"[plan] unit={os.path.basename(ws)} engine=ffmpeg render={why}")
            print("  !! source changed since Stage 1 -> Stage 1 must be re-run for this unit"
                  if state.get("stage1_key") and state["stage1_key"] != s1 else
                  "  stage1: frozen (source unchanged) - do NOT re-run Whisper/scan")
        return dirty
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


def band_y0(prof, scan, style, plates):
    """Top of the band ffmpeg touches: above the highest Chinese mask and the highest plate, multiple of 8, >= 0."""
    sy = scan.get("strip_y0", prof.get("strip_y0", 880))
    grow = int(style["mask_feather"] * 1.5)
    tops = [sy + iv["bbox"][2] - style["box_pad_y"] - grow - 2 * style["mask_feather"] for iv in scan.get("hardsub_intervals", [])]
    tops += [oy for _, oy in plates.values()]
    y = min(tops) if tops else int(prof.get("height", 1080)) * 2 // 3
    return max(0, (int(y) - 8) // 8 * 8)


# ----------------------------------------------------------------- one-pass ffmpeg renderer (default)
def build_timeline(ws, prof, cues, scan, style, plates, work, y0):
    """Python draws only PNGs: one per distinct set of active Chinese masks, one per Vietnamese plate.
    Two ffconcat lists time them; ffmpeg does decode, mask, overlay, encode, mux in one process."""
    from PIL import Image, ImageDraw, ImageFilter
    W, H = int(prof.get("width", 1920)), int(prof.get("height", 1080))
    bh = H - y0
    sy, pad = scan.get("strip_y0", prof.get("strip_y0", 880)), style["temporal_pad_s"]
    grow = int(style["mask_feather"] * 1.5)
    items = [("m", tuple(iv["bbox"]), iv["start"] - pad, iv["end"] + pad) for iv in scan.get("hardsub_intervals", [])]
    items += [("p", c["id"], c["show_a"] - pad, c["show_b"] + pad) for c in cues]

    def mask_img(bbs):
        m = Image.new("L", (W, bh), 0); d = ImageDraw.Draw(m)
        for x0, x1, ry0, ry1 in bbs:
            b = (max(0, x0 - style["box_pad_x"] - grow), sy + ry0 - style["box_pad_y"] - grow - y0,
                 min(W, x1 + style["box_pad_x"] + grow), sy + ry1 + style["box_pad_y"] + grow - y0)
            d.rounded_rectangle(b, radius=max(1, (b[3] - b[1]) // 2), fill=255)
        return m.filter(ImageFilter.GaussianBlur(style["mask_feather"]))

    def plate_img(pid):
        band = Image.new("RGBA", (W, bh), (0, 0, 0, 0))
        if pid is not None:
            img, oy = plates[pid]
            band.alpha_composite(img.crop((0, max(0, y0 - oy), W, img.height)), (0, max(0, oy - y0)))
        return band

    dur = float(prof.get("duration") or ffprobe_duration(os.path.join(ws, "source", "master_source.mp4")))
    bounds = sorted({0.0, dur} | {max(0.0, min(dur, t)) for it in items for t in it[2:]})
    seqs, made = {"m": [], "p": []}, set()
    os.makedirs(work, exist_ok=True)
    for a_, b_ in zip(bounds, bounds[1:]):
        if b_ - a_ < 1e-4:
            continue
        mid = (a_ + b_) / 2
        act = sorted((it for it in items if it[2] <= mid < it[3]), key=lambda x: x[2])
        keys = {"m": tuple(sorted(it[1] for it in act if it[0] == "m")),
                "p": next((it[1] for it in act if it[0] == "p"), None)}
        for kind, key in keys.items():
            name = f"{kind}_{hashlib.sha1(repr(key).encode()).hexdigest()[:12]}.png"
            if name not in made:
                (mask_img(key) if kind == "m" else plate_img(key)).save(os.path.join(work, name), compress_level=1)
                made.add(name)
            sq = seqs[kind]
            if sq and sq[-1][0] == name:
                sq[-1][1] += b_ - a_
            else:
                sq.append([name, b_ - a_])
    for kind, sq in seqs.items():
        with open(os.path.join(work, kind + ".ffconcat"), "w") as f:
            f.write("ffconcat version 1.0\n")
            f.writelines(f"file '{n}'\nduration {d:.6f}\n" for n, d in sq)
            f.write(f"file '{sq[-1][0]}'\n")
    return len(made)


def ffprobe_duration(p):
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", p],
                                capture_output=True, text=True).stdout.strip() or 0)


def render_ffmpeg(ws, prof, style, work, audio, out, y0, enc=None, wm=None):
    enc = enc or resolve_encode(style)
    W, H = int(prof.get("width", 1920)), int(prof.get("height", 1080))
    bh = H - y0
    fps = prof.get("fps", 30)
    if style.get("mask_mode", "blur") == "solid":
        r, g, b = style.get("solid_rgb", [0, 0, 0])
        head = f"[0:v]null[base];color=c=0x{r:02x}{g:02x}{b:02x}:s={W}x{bh}:r={fps},format=rgba[cov];"
    else:
        sig, dk = style["blur_sigma"], style["blur_darken"]
        sw, sh = max(2, (W // 4) // 2 * 2), max(2, (bh // 4) // 2 * 2)
        # blur the band at 1/4 size: same look as sigma at full size, ~16x cheaper
        head = (f"[0:v]split[base][src];[src]crop={W}:{bh}:0:{y0},scale={sw}:{sh},gblur=sigma={sig / 4:.2f},"
                f"scale={W}:{bh},colorchannelmixer=rr={dk}:gg={dk}:bb={dk},format=rgba[cov];")
    fg = (head + f"[1:v]format=gray,fps={fps}[mk];[cov][mk]alphamerge[covm];"
          f"[base][covm]overlay=0:{y0}:eof_action=pass:format=auto[v1];")
    cmd = ["ffmpeg", "-v", "error", "-y"]
    if enc.get("hwdec"):
        cmd += ["-hwaccel", "videotoolbox"]
    cmd += ["-i", os.path.join(ws, "source", "master_source.mp4"),
            "-f", "concat", "-safe", "0", "-i", os.path.join(work, "m.ffconcat"),
            "-f", "concat", "-safe", "0", "-i", os.path.join(work, "p.ffconcat")]
    next_idx = 3
    audio_idx = None
    if os.path.exists(audio):
        cmd += ["-i", audio]; audio_idx = next_idx; next_idx += 1
    if wm:
        cmd += ["-i", wm["tl"], "-i", wm["tr"]]
        tl_i, tr_i = next_idx, next_idx + 1
        sc = float(wm.get("scale", 1.0))
        if sc < 0.999:
            fg += (f"[2:v]format=rgba,fps={fps}[pl];[v1][pl]overlay=0:{y0}:eof_action=pass:format=auto[v2];"
                   f"[{tl_i}:v]scale=trunc(iw*{sc:.3f}/2)*2:-1[tlw];"
                   f"[{tr_i}:v]scale=trunc(iw*{sc:.3f}/2)*2:-1[trw];"
                   f"[v2][tlw]overlay={wm['tl_x']}:{wm['tl_y']}:format=auto[v3];"
                   f"[v3][trw]overlay=W-w-{wm['tr_mx']}:{wm['tr_y']}:format=auto,format={enc['pix_fmt']}[v]")
        else:
            fg += (f"[2:v]format=rgba,fps={fps}[pl];[v1][pl]overlay=0:{y0}:eof_action=pass:format=auto[v2];"
                   f"[v2][{tl_i}:v]overlay={wm['tl_x']}:{wm['tl_y']}:format=auto[v3];"
                   f"[v3][{tr_i}:v]overlay=W-w-{wm['tr_mx']}:{wm['tr_y']}:format=auto,format={enc['pix_fmt']}[v]")
    else:
        fg += f"[2:v]format=rgba,fps={fps}[pl];[v1][pl]overlay=0:{y0}:eof_action=pass:format=auto,format={enc['pix_fmt']}[v]"
    cmd += ["-filter_complex", fg, "-map", "[v]"]
    cmd += ["-map", f"{audio_idx}:a", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"] if audio_idx is not None else ["-an"]
    cmd += ["-c:v", enc["vcodec"]]
    if "q" in enc:
        cmd += ["-q:v", str(enc["q"])]
    elif "bitrate" in enc:
        cmd += ["-b:v", enc["bitrate"]]
    if enc.get("tag"):
        cmd += ["-tag:v", enc["tag"]]
    # passthrough keeps every source frame with its own timestamp: VFR-safe, no drift against audio
    cmd += ["-g", str(int(round(fps * 2))), "-fps_mode", "passthrough", "-sn", "-movflags", "+faststart", out + ".part.mp4"]
    subprocess.run(cmd, check=True)
    os.replace(out + ".part.mp4", out)


def ff_render_key(ws, fh, cues, scan, style, y0, enc=None, wm=None):
    enc = enc or resolve_encode(style)
    src = os.path.join(ws, "source", "master_source.mp4")
    audio = os.path.join(ws, "media", "master_audio_48k.wav")
    font_p = style.get("font_path", "")
    font_sha = fh(font_p)[:16] if font_p and os.path.exists(font_p) else "nofont"
    wm_key = None
    if wm:
        wm_key = {"tl": fh(wm["tl"])[:16], "tr": fh(wm["tr"])[:16],
                  "scale": wm.get("scale", 1.0),
                  "pos": [wm["tl_x"], wm["tl_y"], wm["tr_mx"], wm["tr_y"]]}
    parts = {"cues": [(c["id"], c["sub_text"], round(c["show_a"], 3), round(c["show_b"], 3)) for c in cues],
             "ivs": [(iv["start"], iv["end"], iv["bbox"]) for iv in scan.get("hardsub_intervals", [])],
             "style": style, "font": font_sha, "wm": wm_key,
             "src": fh(src) if os.path.exists(src) else "NO_SOURCE",
             "audio": fh(audio) if os.path.exists(audio) else None, "enc": enc, "v": FF_RENDERER_VERSION, "y0": y0}
    return parts, h_obj(parts)


def cmd_render(a):
    if a.engine == "python":
        return cmd_render_chunks(a)
    from PIL import ImageFont
    ws = os.path.abspath(a.unit)
    if not a.no_gate:
        cicd().require_gate(ws, 3)
    fh = FastHash(ws)
    prof, eps, cues, scan, style = load_unit(ws)
    if a.mask:
        style["mask_mode"] = a.mask
    enc = resolve_encode(style, getattr(a, "codec", None), getattr(a, "quality", None))
    W = int(prof.get("width", 1920)); cy = centre_y_of(scan, prof)
    wm = resolve_watermark(ws, getattr(a, "brand_dir", None), width=W)
    font = ImageFont.truetype(style["font_path"], style["font_size"], index=style.get("font_index", 0))
    plates, geom_log = {}, []
    for c in cues:
        img, oy, g = build_plate({"text": c["sub_text"]}, style, W, cy, font)
        plates[c["id"]] = (img, oy)
        geom_log.append({"id": c["id"], "slot_start": c["show_a"], "slot_end": c["show_b"], "style": "frosted_glass",
                         "style_name": style["name"], "lines": g["lines"], "n_lines": g["n_lines"],
                         "box": [g["box_abs"][0], g["box_abs"][1] - prof.get("strip_y0", 880), g["box_abs"][2],
                                 g["box_abs"][3] - prof.get("strip_y0", 880)], "line_px": g["max_line_px"]})
    jdump(os.path.join(ws, "data", "04_overlay_geometry_log.json"), {"renderer": FF_RENDERER_VERSION, "centre_y": cy, "cues": geom_log})
    audio = os.path.join(ws, "media", "master_audio_48k.wav")
    y0 = band_y0(prof, scan, style, plates)
    parts, key = ff_render_key(ws, fh, cues, scan, style, y0, enc, wm)
    out = os.path.join(ws, "media", ".staging_render.mp4")
    state = jload(os.path.join(ws, ".cache", "build_state.json"), {"chunks": {}, "tts": {}})
    prev = state.get("render", {})
    if prev.get("key") == key and os.path.exists(out):
        print("[render] unchanged -> staging is current"); fh.save(); return
    t0 = time.time()
    work = os.path.join(ws, ".cache", "timeline", key[:16])
    n = build_timeline(ws, prof, cues, scan, style, plates, work, y0)
    t1 = time.time()
    enc_desc = f"{enc['vcodec']}(q={enc['q']})" if "q" in enc else f"{enc['vcodec']}({enc.get('bitrate')})"
    print(f"[render] timeline: {n} PNGs in {t1 - t0:.1f}s; ffmpeg one pass, mask={style['mask_mode']} enc={enc_desc} wm={'on' if wm else 'off'} band y>={y0}", flush=True)
    render_ffmpeg(ws, prof, style, work, audio, out, y0, enc=enc, wm=wm)
    sz_mb = os.path.getsize(out) / 1e6
    print(f"[render] ffmpeg {time.time() - t1:.1f}s -> {out} ({sz_mb:.1f} MB)")
    state["render"] = {"engine": "ffmpeg", "key": key, "parts": {k: h_obj(v) for k, v in parts.items()},
                       "mask": style["mask_mode"], "enc": enc, "wm": bool(wm), "renderer": FF_RENDERER_VERSION}
    state["last_render"] = {"at": now(), "rendered": 1, "reused": 0, "output": out,
                            "seconds": round(time.time() - t0, 1), "size_mb": round(sz_mb, 1)}
    jdump(os.path.join(ws, ".cache", "build_state.json"), state)
    fh.save()
    for old in glob.glob(os.path.join(ws, ".cache", "timeline", "*")):
        if not old.endswith(key[:16]):
            shutil.rmtree(old, ignore_errors=True)


def cmd_render_chunks(a):
    """Legacy per-frame Python renderer (refine_x re-measure). --engine python."""
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
    if state.get("render", {}).get("engine") == "ffmpeg":   # one-pass: the staging file is the only artifact
        chunk_files = [os.path.join(ws, "media", ".staging_render.mp4")]
    else:
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
    actual_renderer = FF_RENDERER_VERSION if state.get("render", {}).get("engine") == "ffmpeg" else RENDERER_VERSION
    g.write_gate_token(ws, 4, "Style-B Overlay (incremental)", checks,
                       {"drift_bad": len(drift_bad), "chunks": len(chunk_files), "renderer": actual_renderer}, arts)


# ----------------------------------------------------------------- clean / merge-series
def cmd_clean(a):
    """Safely prune heavy regenerable intermediates AFTER final_<unit>_vi.mp4 exists.
    Default keeps all Stage 0-5 gate-hashed artifacts (PPPPPP stays green); --deep also removes
    .staging_render.mp4 and master_audio_48k.wav (Stage 5 stays P, Stage 3/4 become x)."""
    ws = os.path.abspath(a.unit)
    finals = glob.glob(os.path.join(ws, "media", "final_*_vi.mp4")) + glob.glob(os.path.join(ws, "deliverables", "16x9", "*.mp4"))
    if not finals and not a.force:
        sys.exit(f"[clean] refusing to prune {ws}: no final MP4 found (pass --force to override)")
    targets = [
        os.path.join(ws, "media", "stems"),
        os.path.join(ws, "media", "audio", "premaster_48k.wav"),
        os.path.join(ws, "media", "audio", "dub_voice_48k.wav"),
        os.path.join(ws, "media", "subtitles_overlay_alpha.mov"),
        os.path.join(ws, "media", ".staging_final.mp4"),
        os.path.join(ws, ".cache", "timeline"),
    ]
    if getattr(a, "deep", False):
        targets += [
            os.path.join(ws, "media", ".staging_render.mp4"),
            os.path.join(ws, "media", "master_audio_48k.wav"),
        ]
    freed = 0
    for p in targets:
        if not os.path.exists(p):
            continue
        if os.path.isdir(p):
            sz = sum(os.path.getsize(os.path.join(dp, fn)) for dp, _, fns in os.walk(p) for fn in fns)
            if not a.dry_run:
                shutil.rmtree(p)
        else:
            sz = os.path.getsize(p)
            if not a.dry_run:
                os.remove(p)
        freed += sz
        print(f"  {'would-rm' if a.dry_run else 'removed ':8s} {sz / 1e6:7.1f} MB  {os.path.relpath(p, ws)}")
    note = "deep: Stage 3/4 gates will show x, Stage 5 stays P" if getattr(a, "deep", False) else "PPPPPP gates intact"
    print(f"[clean] {'would free' if a.dry_run else 'freed'} {freed / 1e6:.1f} MB in {os.path.basename(ws)} ({note})")


def _parse_srt(path):
    import re
    with open(path, "r", encoding="utf-8") as f:
        txt = f.read().strip()
    if not txt:
        return []
    blocks = []
    for b in re.split(r"\n\s*\n", txt):
        lines = [ln.strip() for ln in b.strip().splitlines() if ln.strip()]
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        a_s, b_s = [x.strip() for x in lines[1].split("-->")]
        def to_s(tc):
            h, m, rest = tc.split(":"); s, ms = rest.replace(".", ",").split(",")
            return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0
        blocks.append((to_s(a_s), to_s(b_s), "\n".join(lines[2:])))
    return blocks


def _fmt_srt(s):
    ms = int(round(s * 1000)); h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); sec, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def cmd_merge_series(a):
    """Assemble the single FULL-SERIES publishable master (MP4 + SRT) from all Stage-5-verified working batches."""
    hub = os.path.abspath(a.hub)
    if not is_hub(hub):
        sys.exit(f"[merge-series] {hub} is not a Series Hub")
    units = sorted(u for u in glob.glob(os.path.join(hub, "units", "*")) if is_unit(u))
    if not units:
        sys.exit(f"[merge-series] no working batches under {hub}/units/")
    mp4s, srts, durs, total_eps = [], [], [], 0
    for u in units:
        gs = gate_status(u)
        if not a.allow_incomplete and (len(gs) < 6 or gs[5] != "P"):
            sys.exit(f"[merge-series] {os.path.basename(u)} Stage 5 gate is not PASS ({gs}) — every batch must PASS before full-series release")
        cand_mp4 = (glob.glob(os.path.join(hub, "deliverables", "16x9", f"*{os.path.basename(u)}*.mp4"))
                    + glob.glob(os.path.join(hub, "deliverables", "9x16", f"*{os.path.basename(u)}*.mp4"))
                    + glob.glob(os.path.join(u, "media", "final_*_vi.mp4")))
        if not cand_mp4:
            sys.exit(f"[merge-series] missing batch MP4 for {os.path.basename(u)}")
        mp4s.append(cand_mp4[0])
        durs.append(ffprobe_duration(cand_mp4[0]))
        ep_man = jload(os.path.join(u, "data", "00_episodes_manifest.json"), [])
        total_eps += len(ep_man) if isinstance(ep_man, list) else len(ep_man.get("episodes", []))
        cand_srt = (glob.glob(os.path.join(hub, "deliverables", "subtitles", f"*{os.path.basename(u)}*.srt"))
                    + [os.path.join(u, "data", "subtitles_full.srt")])
        srts.append(next((p for p in cand_srt if os.path.exists(p)), None))
    slug = os.path.basename(hub)
    out_mp4 = os.path.join(hub, "deliverables", f"final_{slug}_full_vi.mp4")
    out_srt = os.path.join(hub, "deliverables", "subtitles", f"final_{slug}_full_vi.srt")
    os.makedirs(os.path.dirname(out_srt), exist_ok=True)
    lst = os.path.join(hub, ".cache", "merge_concat.txt")
    os.makedirs(os.path.dirname(lst), exist_ok=True)
    with open(lst, "w", encoding="utf-8") as f:
        f.writelines(f"file '{p}'\n" for p in mp4s)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                    "-c", "copy", "-movflags", "+faststart", out_mp4 + ".part.mp4"], check=True)
    os.replace(out_mp4 + ".part.mp4", out_mp4)
    offset, cue_idx = 0.0, 1
    with open(out_srt, "w", encoding="utf-8") as out_f:
        for sp, dur in zip(srts, durs):
            if sp:
                for st, et, txt in _parse_srt(sp):
                    out_f.write(f"{cue_idx}\n{_fmt_srt(st + offset)} --> {_fmt_srt(et + offset)}\n{txt}\n\n")
                    cue_idx += 1
            offset += dur
    sz_mb = os.path.getsize(out_mp4) / 1e6
    ep_note = f", {total_eps} eps" if total_eps else ""
    hub_log = os.path.join(hub, "CHANGELOG.md")
    if os.path.exists(hub_log):
        with open(hub_log, "a", encoding="utf-8") as f:
            f.write(f"- {now()} [release] hub: merged {len(mp4s)} batches ({sum(durs)/60:.1f} min{ep_note}, {sz_mb:.1f} MB) -> {os.path.relpath(out_mp4, hub)}\n")
    if os.path.exists(os.path.join(hub, "PROGRESS.md")):
        refresh_progress(hub)
    print(f"[merge-series] FULL SERIES READY: {len(mp4s)} batches ({sum(durs)/60:.1f} min{ep_note}, {sz_mb:.1f} MB) -> {out_mp4}")
    print(f"[merge-series] FULL SRT ({cue_idx - 1} cues) -> {out_srt}")


def _qa_timestamps_for(u):
    g = jload(os.path.join(u, "data", "04_overlay_geometry_log.json"), {}) or {}
    cues = g.get("cues", [])
    if not cues:
        return "10,30,60,120,180,240,300,360,420,480"
    step = max(1, len(cues) // 15)
    ts = [str(round((float(c["slot_start"]) + float(c["slot_end"])) / 2.0, 2)) for c in cues[::step]]
    return ",".join(ts[:18])


def cmd_run_series(a):
    """Hardware-overlapped full-series batch runner (Metal GPU + CPU + Media Engine).
    - prep:    separate (Metal) + Stage 1 ASR/scan/waveform/audit-frames for batches with Stage 0 PASS.
    - produce: 3-engine overlapped pipeline (Metal separate -> CPU dub/mix/master/verify -> Media Engine render/verify-overlay/QA-extract)
               for batches with Stage 2 PASS. Never bypasses visual QA or Stage 5 human/agent review."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    hub = os.path.abspath(a.hub)
    if not is_hub(hub):
        sys.exit(f"[run-series] {hub} is not a Series Hub")
    units = sorted(u for u in glob.glob(os.path.join(hub, "units", "*")) if is_unit(u))
    if not units:
        sys.exit(f"[run-series] no working batches under {hub}/units/")
    py = os.environ.get("AICN_PY", "/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python")
    if not os.path.exists(py):
        py = sys.executable
    audio_py = os.path.join(SKILL_DIR, "scripts", "aicn_audio.py")
    aicn_py = os.path.join(SKILL_DIR, "scripts", "aicn.py")
    s1_dir = os.path.join(VENDOR, "video-asr-visual-audit", "scripts")
    s5_dir = os.path.join(VENDOR, "video-localization-qa-review", "scripts")

    def _run(tag, cmd):
        t0 = time.time()
        print(f"[run-series] START {tag}", flush=True)
        if not a.dry_run:
            subprocess.run(cmd, check=True)
        print(f"[run-series] DONE  {tag} ({time.time() - t0:.1f}s)", flush=True)

    t_all = time.time()
    if a.phase == "prep":
        todo = [u for u in units if len(gate_status(u)) >= 2 and gate_status(u)[0] == "P" and gate_status(u)[1] != "P"]
        print(f"[run-series] phase=prep hub={os.path.basename(hub)} batches={len(todo)}/{len(units)} dry_run={a.dry_run}", flush=True)
        for u in todo:
            bn = os.path.basename(u)
            src = os.path.join(u, "source", "master_source.mp4")
            _run(f"{bn}:separate", [py, audio_py, "separate", "--unit", u])
            _run(f"{bn}:asr+scan", [py, os.path.join(s1_dir, "run_asr_and_scan.py"), "--video", src, "--workspace", u, "--color-mode", "auto"])
            _run(f"{bn}:waveform", [py, os.path.join(s1_dir, "analyze_voice_waveform.py"), "--workspace", u, "--snap-and-split"])
            _run(f"{bn}:audit-frames", [py, os.path.join(s1_dir, "extract_audit_frames.py"), "--video", src, "--workspace", u])
        print(f"[run-series] prep finished in {(time.time() - t_all) / 60:.1f} min. Next: visually inspect frames/audit/ and run verify_stage_1.py.", flush=True)
        return

    # phase == "produce": 3-engine overlapped pipeline across batches with Stage 2 PASS
    todo = [u for u in units if len(gate_status(u)) >= 6 and gate_status(u)[2] == "P" and gate_status(u)[5] != "P"]
    print(f"[run-series] phase=produce hub={os.path.basename(hub)} batches={len(todo)}/{len(units)} dry_run={a.dry_run}", flush=True)
    gpu_lock, cpu_lock, media_lock = threading.Lock(), threading.Lock(), threading.Lock()
    render_flags = []
    if a.codec:
        render_flags += ["--codec", a.codec]
    if a.quality is not None:
        render_flags += ["--quality", str(a.quality)]
    if a.mask:
        render_flags += ["--mask", a.mask]
    if a.brand_dir:
        render_flags += ["--brand-dir", os.path.abspath(a.brand_dir)]

    def _produce_one(u):
        bn = os.path.basename(u)
        gs = gate_status(u)
        if gs[3] != "P":
            with gpu_lock:
                _run(f"{bn}:separate[Metal]", [py, audio_py, "separate", "--unit", u])
            with cpu_lock:
                for step in ("dub", "mix", "master", "verify"):
                    _run(f"{bn}:{step}[CPU]", [py, audio_py, step, "--unit", u])
        qa_man = os.path.join(u, "data", "04_qa_frames_manifest.json")
        if gs[4] != "P" or not os.path.exists(qa_man):
            with media_lock:
                _run(f"{bn}:render[MediaEngine]", [py, aicn_py, "render", "--unit", u, *render_flags])
                _run(f"{bn}:verify-overlay", [py, aicn_py, "verify-overlay", "--unit", u])
                src = os.path.join(u, "source", "master_source.mp4")
                stg = os.path.join(u, "media", ".staging_render.mp4")
                ts = _qa_timestamps_for(u)
                _run(f"{bn}:qa-extract", [py, os.path.join(s5_dir, "composite_and_verify.py"),
                                          "--video", src, "--workspace", u, "--slug", bn,
                                          "--import-existing-mp4", stg, "--qa-timestamps", ts])
        return bn

    with ThreadPoolExecutor(max_workers=min(3, max(1, len(todo)))) as ex:
        done = list(ex.map(_produce_one, todo))
    print(f"[run-series] produce finished {len(done)} batches in {(time.time() - t_all) / 60:.1f} min.", flush=True)
    print(f"[run-series] Next: view frames/qa/ per batch -> verify_stage_5.py -> `aicn.py merge-series --hub {hub}` for the full-series release.", flush=True)


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
    units = [u for u in sorted(glob.glob(os.path.join(hub, "units", "*"))) if is_unit(u)]
    n_pass = 0
    rows = ["| working batch | gates 0-5 | last render | batch mp4 |", "|---|---|---|---|"]
    for u in units:
        gs = gate_status(u)
        if len(gs) >= 6 and gs[5] == "P":
            n_pass += 1
        st = jload(os.path.join(u, ".cache", "build_state.json"), {}) or {}
        lr = st.get("last_render", {})
        fin = "yes" if glob.glob(os.path.join(u, "media", "final_*_vi.mp4")) else "no"
        rows.append(f"| {os.path.basename(u)} | `{gs}` | {lr.get('at', '-')} ({lr.get('rendered', '-')} new/{lr.get('reused', '-')} reused) | {fin} |")
    full_mp4 = os.path.join(hub, "deliverables", f"final_{os.path.basename(hub)}_full_vi.mp4")
    if os.path.exists(full_mp4):
        rel = f"**Full-series release:** READY (`{os.path.relpath(full_mp4, hub)}`, {os.path.getsize(full_mp4)/1e6:.1f} MB; {n_pass}/{len(units)} batches PASS)"
    else:
        rel = f"**Full-series release:** PENDING ({n_pass}/{len(units)} batches PASS — run `aicn.py merge-series --hub .` once all batches PASS)"
    block = "<!-- AUTO:UNITS -->\n" + rel + "\n\n" + "\n".join(rows) + "\n<!-- /AUTO:UNITS -->"
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


# ----------------------------------------------------------------- doctor (machine ingest)
def cmd_doctor(a):
    """Probe the machine once and write config/machine.json so every stage uses the native stack."""
    import platform
    sh = lambda *c: subprocess.run(c, capture_output=True, text=True).stdout
    m = {"at": now(), "os": platform.platform(), "arch": platform.machine(),
         "chip": sh("sysctl", "-n", "machdep.cpu.brand_string").strip(),
         "cores": os.cpu_count(), "ram_gb": round(int(sh("sysctl", "-n", "hw.memsize") or 0) / 2**30, 1)}
    filters, encoders = sh("ffmpeg", "-hide_banner", "-filters"), sh("ffmpeg", "-hide_banner", "-encoders")
    m["ffmpeg"] = {"version": (sh("ffmpeg", "-version").split("\n") or [""])[0],
                   "filters": {f: f" {f} " in filters for f in ("gblur", "boxblur", "overlay", "alphamerge", "drawbox",
                                                                 "subtitles", "drawtext", "scale_vt")},
                   "encoders": {e: e in encoders for e in ("h264_videotoolbox", "hevc_videotoolbox", "libx264")}}
    py = os.environ.get("AICN_PY", "/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python")
    probe = lambda mod: subprocess.run([py, "-c", f"import {mod}"], capture_output=True).returncode == 0
    m["python"] = {"path": py, **{k: probe(k) for k in ("mlx.core", "mlx_whisper", "faster_whisper", "soundfile", "cv2")}}
    m["tools"] = {t: bool(shutil.which(t)) for t in ("rubberband", "uvx", "ffprobe")}
    hub = os.path.expanduser("~/.cache/huggingface/hub")
    m["models"] = sorted(d for d in (os.listdir(hub) if os.path.isdir(hub) else []) if "whisper" in d.lower())
    enc_choices = [e for e in ("h264_videotoolbox", "hevc_videotoolbox") if m["ffmpeg"]["encoders"][e]]
    m["choose"] = {
        "asr": "mlx-whisper (Metal)" if m["python"]["mlx_whisper"] else "faster-whisper (CPU int8)",
        "render": "ffmpeg one-pass" if m["ffmpeg"]["filters"]["gblur"] and m["ffmpeg"]["filters"]["alphamerge"] else "python chunks",
        "encoder": " + ".join(enc_choices) if enc_choices else "libx264",
        "stems": "demucs-mlx via uvx" if m["tools"]["uvx"] else "MISSING",
        "stretch": "rubberband R3" if m["tools"]["rubberband"] else "MISSING (brew install rubberband)",
    }
    out = os.path.join(os.path.abspath(a.root), "config", "machine.json")
    jdump(out, m)
    for k, v in m["choose"].items():
        print(f"  {k:8s} {v}")
    print(f"[doctor] {m['chip']} {m['cores']} cores {m['ram_gb']} GB -> {out}")


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("doctor"); p.add_argument("--root", default="."); p.set_defaults(f=cmd_doctor)
    p = sp.add_parser("discover"); p.add_argument("--root", default="."); p.set_defaults(f=cmd_discover)
    p = sp.add_parser("init-series"); p.add_argument("--series-slug", required=True); p.add_argument("--title", default="")
    p.add_argument("--workspace-root", default="workspace"); p.add_argument("--total-episodes", default="?")
    p.add_argument("--unit-size", default=10); p.add_argument("--source-lang", default="zh", help="source audio language (zh, en, auto)")
    p.add_argument("--rights", required=True,
                   help="license basis: own | licensed:<contract> | cps:<platform> | mcn:<name>")
    p.set_defaults(f=cmd_init_series)
    p = sp.add_parser("adopt"); p.add_argument("--unit", required=True); p.set_defaults(f=cmd_adopt)
    p = sp.add_parser("plan"); p.add_argument("--unit", required=True); p.add_argument("--chunk-s", type=float, default=90)
    p.add_argument("--engine", choices=["ffmpeg", "python"], default="ffmpeg")
    p.add_argument("--mask", choices=["blur", "solid"], default=None)
    p.add_argument("--codec", choices=list(ENCODE_PRESETS), default=None)
    p.add_argument("--quality", type=int, default=None)
    p.add_argument("--brand-dir", default=None)
    p.set_defaults(f=cmd_plan)
    p = sp.add_parser("tts-sync"); p.add_argument("--unit", required=True); p.add_argument("--voice", default="Trúc Ly")
    p.add_argument("--store", action="store_true"); p.set_defaults(f=cmd_tts_sync)
    p = sp.add_parser("render"); p.add_argument("--unit", required=True); p.add_argument("--chunk-s", type=float, default=90)
    p.add_argument("--only", default=""); p.add_argument("--no-gate", action="store_true", help="tests only")
    p.add_argument("--engine", choices=["ffmpeg", "python"], default="ffmpeg")
    p.add_argument("--mask", choices=["blur", "solid"], default=None, help="override style mask_mode")
    p.add_argument("--codec", choices=list(ENCODE_PRESETS), default=None, help="encoder preset: h264 | hevc | h264_high")
    p.add_argument("--quality", type=int, default=None, help="VideoToolbox -q:v quality (e.g. 55-62)")
    p.add_argument("--brand-dir", default=None, help="folder with 13_top_left_logo_large.png & 15_top_right_vietsub_floating.png")
    p.set_defaults(f=cmd_render)
    p = sp.add_parser("verify-overlay"); p.add_argument("--unit", required=True); p.add_argument("--no-gate", action="store_true")
    p.set_defaults(f=cmd_verify_overlay)
    p = sp.add_parser("clean"); p.add_argument("--unit", required=True); p.add_argument("--dry-run", action="store_true")
    p.add_argument("--deep", action="store_true", help="also remove .staging_render.mp4 and master_audio_48k.wav")
    p.add_argument("--force", action="store_true"); p.set_defaults(f=cmd_clean)
    p = sp.add_parser("merge-series"); p.add_argument("--hub", required=True)
    p.add_argument("--allow-incomplete", action="store_true"); p.set_defaults(f=cmd_merge_series)
    p = sp.add_parser("run-series"); p.add_argument("--hub", required=True)
    p.add_argument("--phase", choices=["prep", "produce"], required=True)
    p.add_argument("--codec", choices=list(ENCODE_PRESETS), default=None)
    p.add_argument("--quality", type=int, default=None)
    p.add_argument("--mask", choices=["blur", "solid"], default=None)
    p.add_argument("--brand-dir", default=None)
    p.add_argument("--dry-run", action="store_true"); p.set_defaults(f=cmd_run_series)
    p = sp.add_parser("log"); p.add_argument("--unit", required=True); p.add_argument("--kind", default="edit",
                      choices=["init", "stage", "edit", "fix", "render", "release", "note"]); p.add_argument("--message", required=True)
    p.set_defaults(f=cmd_log)
    p = sp.add_parser("progress"); p.add_argument("--hub", required=True); p.set_defaults(f=cmd_progress)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
