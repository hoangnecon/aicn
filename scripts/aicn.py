#!/usr/bin/env python3
"""
aicn.py - incremental build engine for the AICN drama re-up / dub + watermark pipeline (v2.0.0).

Wraps the existing Antigravity stage skills (video-localization-orchestrator,
video-asr-visual-audit, zh-vi-script-adapter, vieneu-voice-pacing-mixer,
video-localization-qa-review) and adds:

  discover        read a root folder: series hubs, units, gate status, progress
  init-series     scaffold a Series Hub (CHANGELOG, PROGRESS, bible, inbox, units)
  adopt           add the incremental layer to a unit made by init_workspace.py
  plan            dry-run: whether the dub+watermark render is current or stale
  tts-sync        content-addressed TTS cache around synthesize_and_audit_pacing.py
  render          mux source video + 48kHz master audio + optional dual-corner
                  watermark (--brand-dir) -> media/.staging_render.mp4
  verify-overlay  Stage 4 gate (alias: verify-render): A/V sync, codec/audio
                  compliance, and frame integrity on media/.staging_render.mp4
  progress        rebuild the hub PROGRESS.md table
  log             append a line to CHANGELOG.md and refresh PROGRESS.md

Nothing here re-runs Whisper / Demucs. Stage 1 outputs are frozen unless the
source hash changes (see `plan`).
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

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import aicn_platform as ap  # noqa: E402

SKILL_DIR = os.path.normpath(os.path.join(HERE, ".."))
VENDOR = os.path.join(SKILL_DIR, "vendor")

FF_RENDERER_VERSION = "v2-dub-wm-2.0"
TTS_ENGINE_TAG = "vieneu-v3-turbo-48k"
ENCODE_PRESETS = ("h264", "hevc", "h264_high")


def resolve_encode(cfg_or_style, codec=None, quality=None, backend=None):
    return ap.resolve_encode_preset(cfg_or_style, codec=codec, quality=quality, backend=backend)


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
    found = []
    for base in [root] + sorted(glob.glob(os.path.join(root, "workspace", "*"))) + sorted(glob.glob(os.path.join(root, "*"))):
        if os.path.isdir(base) and is_hub(base) and base not in found:
            found.append(base)
    if not found:
        print(f"[discover] no Series Hub under {root} (looked in ./ and ./workspace/*)")
    for hub in found:
        print(f"\nSERIES  {ap.normalize_rel_path(hub, root)}")
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
            print(f"  FULL RELEASE: READY ({ap.normalize_rel_path(full_mp4, hub)}, {os.path.getsize(full_mp4)/1e6:.1f} MB; {n_pass}/{len(units)} batches PASS)")
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
              "deliverables/subtitles", "deliverables/episodes", "posters", ".cache/tts"):
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
         "pronoun_matrix": [], "glossary": [], "voices": {}},
        ensure_ascii=False, indent=2))
    print("[init-series] done. Next: drop episodes in source_inbox/<batch>/, then init_workspace.py --topology series_episodic, then `aicn.py adopt`.")


def cmd_adopt(a):
    ws = os.path.abspath(a.unit)
    if not is_unit(ws):
        sys.exit(f"[adopt] {ws} has no config/project_config.json - run init_workspace.py first")
    cfg = jload(os.path.join(ws, "config", "project_config.json"))
    for d in (".cache/tts",):
        os.makedirs(os.path.join(ws, d), exist_ok=True)
    kw = dict(unit=cfg.get("slug", os.path.basename(ws)), series=cfg.get("series_slug", ""), date=now(),
              mode=cfg.get("localization_mode", "voice_dub"))
    write_if_absent(os.path.join(ws, "CHECKLIST.md"), render_tpl("CHECKLIST.md", **kw))
    write_if_absent(os.path.join(ws, "CHANGELOG.md"), render_tpl("UNIT_CHANGELOG.md", **kw))
    state_p = os.path.join(ws, ".cache", "build_state.json")
    if not os.path.exists(state_p):
        jdump(state_p, {"created": now(), "tts": {}, "stage1_key": None})
        print(f"  create  {state_p}")
    print("[adopt] done.")


# ----------------------------------------------------------------- model & plan
def load_unit(ws):
    prof = jload(os.path.join(ws, "data", "00_video_profile.json"), {})
    eps = (jload(os.path.join(ws, "data", "00_episodes_manifest.json"), {}) or {}).get("episodes", [])
    cues = jload(os.path.join(ws, "data", "03_aligned_subtitles.json"))
    if cues is None:
        cues = jload(os.path.join(ws, "data", "02_bilingual_script.json"), [])
        cues = [dict(c, slot_start=c["start"], slot_end=c["end"]) for c in cues]
    if isinstance(cues, dict):
        cues = cues.get("segments", cues.get("cues", []))
    cfg = jload(os.path.join(ws, "config", "project_config.json"), {}) or {}
    return prof, eps, cues, cfg


def stage1_key(ws, fh):
    src = os.path.join(ws, "source", "master_source.mp4")
    asr_engine = ap.detect_asr_backend()["engine"]
    asr_tag = "mlx-whisper" if asr_engine == "mlx" else asr_engine
    return h_obj({"src": fh(src) if os.path.exists(src) else None, "asr": asr_tag})


def ff_render_key(ws, fh, enc=None, wm=None):
    enc = enc or resolve_encode({})
    src = os.path.join(ws, "source", "master_source.mp4")
    audio = os.path.join(ws, "media", "master_audio_48k.wav")
    wm_key = None
    if wm:
        wm_key = {"tl": fh(wm["tl"])[:16], "tr": fh(wm["tr"])[:16],
                  "scale": wm.get("scale", 1.0),
                  "pos": [wm["tl_x"], wm["tl_y"], wm["tr_mx"], wm["tr_y"]]}
    parts = {
        "wm": wm_key,
        "src": fh(src) if os.path.exists(src) else "NO_SOURCE",
        "audio": fh(audio) if os.path.exists(audio) else None,
        "enc": enc if wm else {"vcodec": "copy"},
        "v": FF_RENDERER_VERSION,
    }
    return parts, h_obj(parts)


def cmd_plan(a, quiet=False):
    ws = os.path.abspath(a.unit)
    fh = FastHash(ws)
    prof, eps, cues, cfg = load_unit(ws)
    state = jload(os.path.join(ws, ".cache", "build_state.json"), {"tts": {}})
    enc = resolve_encode(cfg, getattr(a, "codec", None), getattr(a, "quality", None))
    W = int(prof.get("width", 1920))
    wm = resolve_watermark(ws, getattr(a, "brand_dir", None), width=W)
    parts, key = ff_render_key(ws, fh, enc, wm)
    prev = state.get("render", {})
    staging = os.path.exists(os.path.join(ws, "media", ".staging_render.mp4"))
    if prev.get("key") == key and staging:
        why, dirty = "current", []
    else:
        changed = [k for k, v in parts.items() if prev.get("parts", {}).get(k) != h_obj(v)] if prev else ["new"]
        why, dirty = "stale: " + ",".join(changed or ["staging missing"]), ["unit"]
    s1 = stage1_key(ws, fh); fh.save()
    if not quiet:
        print(f"[plan] unit={os.path.basename(ws)} engine=ffmpeg render={why} wm={'on' if wm else 'off'}")
        print("  !! source changed since Stage 1 -> Stage 1 must be re-run for this unit"
              if state.get("stage1_key") and state["stage1_key"] != s1 else
              "  stage1: frozen (source unchanged) - do NOT re-run Whisper")
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


# ----------------------------------------------------------------- media helpers & renderer
def ffprobe_duration(p):
    return float(subprocess.run([ap.find_tool("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", p],
                                capture_output=True, text=True).stdout.strip() or 0)


def ffprobe_streams(p):
    out = subprocess.check_output([
        ap.find_tool("ffprobe"), "-v", "error", "-show_entries",
        "stream=index,codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,time_base,start_time,duration,nb_frames,sample_rate,channels",
        "-of", "json", p,
    ], text=True)
    return json.loads(out).get("streams", [])


def resolve_fps(ws, prof):
    """Return profile FPS or probe master_source.mp4 r_frame_rate so overlays match source cadence."""
    if prof.get("fps"):
        return prof["fps"]
    src = os.path.join(ws, "source", "master_source.mp4")
    if os.path.exists(src):
        try:
            out = subprocess.check_output(
                [ap.find_tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", src],
                text=True, timeout=10,
            ).strip().splitlines()[0].rstrip(",")
            if "/" in out:
                n, d = out.split("/", 1)
                val = float(n) / float(d)
            else:
                val = float(out)
            if val > 1.0:
                return int(val) if abs(val - round(val)) < 1e-3 else round(val, 3)
        except Exception:
            pass
    return 30


def render_ffmpeg(ws, prof, audio, out, enc=None, wm=None):
    """Mux source video + 48kHz master audio + optional dual-corner watermarks (or stream-copy video when no watermark)."""
    enc = enc or resolve_encode({})
    fps = resolve_fps(ws, prof)
    src_mp4 = os.path.join(ws, "source", "master_source.mp4")
    has_audio = os.path.exists(audio)

    cmd = [ap.find_tool("ffmpeg"), "-v", "error", "-y"]
    if wm:
        cmd += ap.build_hwaccel_input_args(enc)
        cmd += ["-i", src_mp4]
        audio_idx = None
        next_idx = 1
        if has_audio:
            cmd += ["-i", audio]
            audio_idx = next_idx
            next_idx += 1
        cmd += ["-framerate", str(fps), "-loop", "1", "-i", wm["tl"],
                "-framerate", str(fps), "-loop", "1", "-i", wm["tr"]]
        tl_i, tr_i = next_idx, next_idx + 1
        sc = float(wm.get("scale", 1.0))
        if sc < 0.999:
            fg = (
                f"[{tl_i}:v]scale=trunc(iw*{sc:.3f}/2)*2:-1[tlw];"
                f"[{tr_i}:v]scale=trunc(iw*{sc:.3f}/2)*2:-1[trw];"
                f"[0:v][tlw]overlay={wm['tl_x']}:{wm['tl_y']}:shortest=1:format=auto[v1];"
                f"[v1][trw]overlay=W-w-{wm['tr_mx']}:{wm['tr_y']}:shortest=1:format=auto,format={enc['pix_fmt']}[v]"
            )
        else:
            fg = (
                f"[0:v][{tl_i}:v]overlay={wm['tl_x']}:{wm['tl_y']}:shortest=1:format=auto[v1];"
                f"[v1][{tr_i}:v]overlay=W-w-{wm['tr_mx']}:{wm['tr_y']}:shortest=1:format=auto,format={enc['pix_fmt']}[v]"
            )
        cmd += ["-filter_complex", fg, "-map", "[v]"]
        cmd += ["-map", f"{audio_idx}:a", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"] if audio_idx is not None else ["-an"]
        cmd += ap.build_vcodec_args(enc)
        cmd += ["-g", str(int(round(fps * 2))), "-fps_mode", "passthrough", "-sn", "-movflags", "+faststart", out + ".part.mp4"]
    else:
        cmd += ["-i", src_mp4]
        if has_audio:
            cmd += ["-i", audio, "-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
        else:
            cmd += ["-map", "0:v:0", "-an"]
        cmd += ["-c:v", "copy", "-sn", "-shortest", "-movflags", "+faststart", out + ".part.mp4"]

    subprocess.run(cmd, check=True)
    os.replace(out + ".part.mp4", out)


def cmd_render(a):
    ws = os.path.abspath(a.unit)
    if not a.no_gate:
        cicd().require_gate(ws, 3)
    fh = FastHash(ws)
    prof, eps, cues, cfg = load_unit(ws)
    enc = resolve_encode(cfg, getattr(a, "codec", None), getattr(a, "quality", None))
    W = int(prof.get("width", 1920))
    wm = resolve_watermark(ws, getattr(a, "brand_dir", None), width=W)
    audio = os.path.join(ws, "media", "master_audio_48k.wav")
    parts, key = ff_render_key(ws, fh, enc, wm)
    out = os.path.join(ws, "media", ".staging_render.mp4")
    state = jload(os.path.join(ws, ".cache", "build_state.json"), {"tts": {}})
    prev = state.get("render", {})
    if prev.get("key") == key and os.path.exists(out):
        print("[render] unchanged -> staging is current"); fh.save(); return
    t0 = time.time()
    if not wm:
        enc_desc = "stream-copy"
    elif "q" in enc:
        enc_desc = f"{enc['vcodec']}(q={enc['q']})"
    elif "cq" in enc:
        enc_desc = f"{enc['vcodec']}(cq={enc['cq']})"
    elif "crf" in enc:
        enc_desc = f"{enc['vcodec']}(crf={enc['crf']})"
    else:
        enc_desc = f"{enc['vcodec']}({enc.get('bitrate')})"
    print(f"[render] ffmpeg one pass, enc={enc_desc} wm={'on' if wm else 'off'} -> {out}", flush=True)
    render_ffmpeg(ws, prof, audio, out, enc=enc, wm=wm)
    sz_mb = os.path.getsize(out) / 1e6
    print(f"[render] ffmpeg {time.time() - t0:.1f}s -> {out} ({sz_mb:.1f} MB)")
    state["render"] = {"engine": "ffmpeg", "key": key, "parts": {k: h_obj(v) for k, v in parts.items()},
                       "enc": enc if wm else {"vcodec": "copy"}, "wm": bool(wm), "renderer": FF_RENDERER_VERSION}
    state["last_render"] = {"at": now(), "rendered": 1, "reused": 0, "output": out,
                            "seconds": round(time.time() - t0, 1), "size_mb": round(sz_mb, 1)}
    jdump(os.path.join(ws, ".cache", "build_state.json"), state)
    fh.save()


# ----------------------------------------------------------------- Stage 4 gate (Dub + Watermark Render)
def cmd_verify_overlay(a):
    ws = os.path.abspath(a.unit)
    g = cicd()
    if not a.no_gate:
        g.require_gate(ws, 3)
    prof, eps, cues, cfg = load_unit(ws)
    staging = os.path.join(ws, "media", ".staging_render.mp4")
    staging_ok = os.path.exists(staging) and os.path.getsize(staging) > 0

    codec_ok = False
    codec_detail = "missing staging"
    dur_ok = False
    av_sync_ok = False
    av_detail = "missing staging"

    if staging_ok:
        stg_streams = ffprobe_streams(staging)
        sv = next((s for s in stg_streams if s.get("codec_type") == "video"), {})
        sa = next((s for s in stg_streams if s.get("codec_type") == "audio"), {})
        ssub = [s for s in stg_streams if s.get("codec_type") == "subtitle"]
        v_codec = sv.get("codec_name", "")
        a_codec = sa.get("codec_name", "")
        a_sr = int(sa.get("sample_rate") or 0)
        a_ch = int(sa.get("channels") or 0)
        has_master_audio = os.path.exists(os.path.join(ws, "media", "master_audio_48k.wav"))
        audio_ok = (a_codec == "aac" and a_sr == 48000 and a_ch == 2) if (sa or has_master_audio) else True
        codec_ok = (v_codec in ("h264", "hevc")) and audio_ok and len(ssub) == 0
        codec_detail = f"v={v_codec}, a={a_codec}({a_sr}Hz,{a_ch}ch), sub_streams={len(ssub)}"

        v_dur = float(sv.get("duration") or 0.0)
        a_dur = float(sa.get("duration") or v_dur)
        v_st = float(sv.get("start_time") or 0.0)
        a_st = float(sa.get("start_time") or 0.0)
        dur_ok = abs(v_dur - float(prof.get("duration", v_dur))) <= 0.30
        av_dur_ok = abs(v_dur - a_dur) <= 0.30
        av_st_ok = abs(v_st - a_st) <= 0.12
        frames_ok = True
        frame_note = ""
        src_p = os.path.join(ws, "source", "master_source.mp4")
        if not prof.get("is_vfr") and os.path.exists(src_p) and sv.get("nb_frames"):
            src_v = next((s for s in ffprobe_streams(src_p) if s.get("codec_type") == "video"), {})
            src_f = int(src_v.get("nb_frames") or 0)
            out_f = int(sv.get("nb_frames") or 0)
            if src_f > 0 and out_f > 0:
                frames_ok = abs(out_f - src_f) <= max(5, int(round(0.005 * src_f)))
                frame_note = f", frames={out_f}/{src_f}"
        av_sync_ok = dur_ok and av_dur_ok and av_st_ok and frames_ok
        av_detail = f"v={v_dur:.3f}s a={a_dur:.3f}s v_st={v_st:.3f}s a_st={a_st:.3f}s{frame_note}"

    checks = [
        {"id": "staging_mp4_present", "weight": 25, "passed": staging_ok,
         "detail": f"path={staging}, size={os.path.getsize(staging) if staging_ok else 0}"},
        {"id": "codec_and_audio_compliance", "weight": 35, "passed": codec_ok,
         "detail": codec_detail},
        {"id": "av_sync_and_frame_integrity", "weight": 40, "passed": av_sync_ok,
         "detail": av_detail},
    ]
    arts = ["media/.staging_render.mp4"]
    g.write_gate_token(ws, 4, "Dub & Watermark Render (incremental)", checks,
                       {"renderer": FF_RENDERER_VERSION}, arts)


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
        os.path.join(ws, "media", ".staging_final.mp4"),
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
        print(f"  {'would-rm' if a.dry_run else 'removed ':8s} {sz / 1e6:7.1f} MB  {ap.normalize_rel_path(p, ws)}")
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
    batch_specs = set()
    for p in mp4s:
        st = ffprobe_streams(p)
        vs = next((s for s in st if s.get("codec_type") == "video"), {})
        as_ = next((s for s in st if s.get("codec_type") == "audio"), {})
        vd, ad = float(vs.get("duration") or 0.0), float(as_.get("duration") or 0.0)
        if abs(vd - ad) > 0.30:
            sys.exit(f"[merge-series] A/V duration drift in {os.path.basename(p)}: video={vd:.3f}s audio={ad:.3f}s (diff={vd-ad:+.3f}s)")
        batch_specs.add((
            vs.get("width"), vs.get("height"), vs.get("codec_name"), vs.get("r_frame_rate"),
            as_.get("codec_name"), as_.get("sample_rate"), as_.get("channels"),
        ))
    if len(batch_specs) > 1:
        sys.exit(f"[merge-series] Mismatched stream specs across batches (cannot stream-copy safely): {batch_specs}")
    slug = os.path.basename(hub)
    out_mp4 = os.path.join(hub, "deliverables", f"final_{slug}_full_vi.mp4")
    out_srt = os.path.join(hub, "deliverables", "subtitles", f"final_{slug}_full_vi.srt")
    os.makedirs(os.path.dirname(out_srt), exist_ok=True)
    lst = os.path.join(hub, ".cache", "merge_concat.txt")
    os.makedirs(os.path.dirname(lst), exist_ok=True)
    with open(lst, "w", encoding="utf-8") as f:
        f.writelines(ap.ffmpeg_concat_line(p) for p in mp4s)
    subprocess.run([ap.find_tool("ffmpeg"), "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                    "-avoid_negative_ts", "make_zero", "-c", "copy", "-movflags", "+faststart", out_mp4 + ".part.mp4"], check=True)
    os.replace(out_mp4 + ".part.mp4", out_mp4)
    full_st = ffprobe_streams(out_mp4)
    f_vs = next((s for s in full_st if s.get("codec_type") == "video"), {})
    f_as = next((s for s in full_st if s.get("codec_type") == "audio"), {})
    f_vd, f_ad = float(f_vs.get("duration") or 0.0), float(f_as.get("duration") or 0.0)
    if abs(f_vd - f_ad) > 0.50 or abs(f_vd - sum(durs)) > 1.0:
        sys.exit(f"[merge-series] Full-series A/V sync gate failed: video={f_vd:.3f}s audio={f_ad:.3f}s expected={sum(durs):.3f}s")
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
            f.write(f"- {now()} [release] hub: merged {len(mp4s)} batches ({sum(durs)/60:.1f} min{ep_note}, {sz_mb:.1f} MB) -> {ap.normalize_rel_path(out_mp4, hub)}\n")
    if os.path.exists(os.path.join(hub, "PROGRESS.md")):
        refresh_progress(hub)
    print(f"[merge-series] FULL SERIES READY: {len(mp4s)} batches ({sum(durs)/60:.1f} min{ep_note}, {sz_mb:.1f} MB) -> {out_mp4}")
    print(f"[merge-series] FULL SRT ({cue_idx - 1} cues) -> {out_srt}")


def _qa_timestamps_for(u):
    prof = jload(os.path.join(u, "data", "00_video_profile.json"), {}) or {}
    dur = float(prof.get("duration") or 0.0)
    if dur > 2.0:
        step = (dur - 2.0) / 11.0
        return ",".join(str(round(1.0 + i * step, 1)) for i in range(12))
    cues = jload(os.path.join(u, "data", "03_aligned_subtitles.json")) or jload(os.path.join(u, "data", "02_bilingual_script.json"), [])
    if isinstance(cues, dict):
        cues = cues.get("segments", cues.get("cues", []))
    if not cues:
        return "10,30,60,120,180,240,300,360,420,480"
    step = max(1, len(cues) // 12)
    ts = [str(round((float(c.get("slot_start", c.get("start", 0))) + float(c.get("slot_end", c.get("end", 1)))) / 2.0, 2)) for c in cues[::step]]
    return ",".join(ts[:12])


def cmd_run_series(a):
    """Hardware-overlapped full-series batch runner (GPU + CPU + Video Encoder).
    - prep:    GPU/MLX (separate + ASR) overlapped with CPU (waveform + audit-frames) for Stage 0 PASS batches.
    - produce: 3-engine overlapped pipeline (GPU separate -> CPU dub/mix/master/verify -> VideoEncoder render/verify-render/QA-extract)
               for batches with Stage 2 PASS. Supports --units u1,u2 for streaming per-unit execution without waiting for all batches."""
    from concurrent.futures import ThreadPoolExecutor
    hub = os.path.abspath(a.hub)
    if not is_hub(hub):
        sys.exit(f"[run-series] {hub} is not a Series Hub")
    units = sorted(u for u in glob.glob(os.path.join(hub, "units", "*")) if is_unit(u))
    if getattr(a, "units", None):
        want = {x.strip() for x in a.units.split(",") if x.strip()}
        units = [u for u in units if os.path.basename(u) in want]
    if not units:
        sys.exit(f"[run-series] no matching working batches under {hub}/units/")
    py = ap.find_python()
    audio_py = os.path.join(SKILL_DIR, "scripts", "aicn_audio.py")
    aicn_py = os.path.join(SKILL_DIR, "scripts", "aicn.py")
    s1_dir = os.path.join(VENDOR, "video-asr-visual-audit", "scripts")
    s5_dir = os.path.join(VENDOR, "video-localization-qa-review", "scripts")

    gpu_info = ap.get_gpu_info(py)
    demucs_tag = "Metal" if ap.detect_demucs_backend(py, gpu_info)["device"] == "mps" else ("CUDA" if gpu_info.get("cuda") else "CPU")
    asr_tag = "Metal" if ap.detect_asr_backend(py, gpu_info)["device"] == "mps" else ("CUDA" if gpu_info.get("cuda") else "CPU")
    vid_tag = {"videotoolbox": "MediaEngine", "nvenc": "NVENC", "cpu": "CPU"}[ap.detect_video_backend()]

    lock_dir = os.path.join(hub, ".cache", "locks")
    os.makedirs(lock_dir, exist_ok=True)

    def _engine_lock(kind):
        return ap.file_lock(os.path.join(lock_dir, f"{kind}.lock"))

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

        def _prep_one(u):
            bn = os.path.basename(u)
            src = os.path.join(u, "source", "master_source.mp4")
            with _engine_lock("gpu"):
                _run(f"{bn}:separate[{demucs_tag}]", [py, audio_py, "separate", "--unit", u])
                _run(f"{bn}:asr[{asr_tag}]", [py, os.path.join(s1_dir, "run_asr_and_scan.py"), "--video", src, "--workspace", u])
            with _engine_lock("cpu"):
                _run(f"{bn}:waveform[CPU]", [py, os.path.join(s1_dir, "analyze_voice_waveform.py"), "--workspace", u, "--snap-and-split"])
                _run(f"{bn}:audit-frames[CPU]", [py, os.path.join(s1_dir, "extract_audit_frames.py"), "--video", src, "--workspace", u])
            return bn

        with ThreadPoolExecutor(max_workers=min(2, max(1, len(todo)))) as ex:
            list(ex.map(_prep_one, todo))
        print(f"[run-series] prep finished in {(time.time() - t_all) / 60:.1f} min. Next: visually inspect frames/audit/ and run verify_stage_1.py.", flush=True)
        return

    # phase == "produce": 3-engine overlapped pipeline across batches with Stage 2 PASS
    todo = [u for u in units if len(gate_status(u)) >= 6 and gate_status(u)[2] == "P" and gate_status(u)[5] != "P"]
    print(f"[run-series] phase=produce hub={os.path.basename(hub)} batches={len(todo)}/{len(units)} dry_run={a.dry_run}", flush=True)
    render_flags = []
    if a.codec:
        render_flags += ["--codec", a.codec]
    if a.quality is not None:
        render_flags += ["--quality", str(a.quality)]
    if a.brand_dir:
        render_flags += ["--brand-dir", os.path.abspath(a.brand_dir)]

    def _produce_one(u):
        bn = os.path.basename(u)
        gs = gate_status(u)
        if gs[3] != "P":
            with _engine_lock("gpu"):
                _run(f"{bn}:separate[{demucs_tag}]", [py, audio_py, "separate", "--unit", u])
            with _engine_lock("cpu"):
                for step in ("dub", "mix", "master", "verify"):
                    _run(f"{bn}:{step}[CPU]", [py, audio_py, step, "--unit", u])
        qa_man = os.path.join(u, "data", "04_qa_frames_manifest.json")
        if gs[4] != "P" or not os.path.exists(qa_man):
            with _engine_lock("media"):
                _run(f"{bn}:render[{vid_tag}]", [py, aicn_py, "render", "--unit", u, *render_flags])
                _run(f"{bn}:verify-render", [py, aicn_py, "verify-render", "--unit", u])
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
        rel = f"**Full-series release:** READY (`{ap.normalize_rel_path(full_mp4, hub)}`, {os.path.getsize(full_mp4)/1e6:.1f} MB; {n_pass}/{len(units)} batches PASS)"
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
    py = ap.find_python()
    gpu = ap.get_gpu_info(py)
    encoders = ap.probe_ffmpeg_encoders()
    filters = ap.probe_ffmpeg_filters()
    video_backend = ap.detect_video_backend(encoders)
    asr_info = ap.detect_asr_backend(py, gpu)
    demucs_info = ap.detect_demucs_backend(py, gpu)

    m = {
        "at": now(),
        "os": platform.platform(),
        "arch": platform.machine(),
        "chip": ap.get_cpu_brand(),
        "cores": os.cpu_count(),
        "ram_gb": ap.get_ram_gb(),
        "gpu": gpu,
        "ffmpeg": {
            "path": ap.find_tool("ffmpeg"),
            "version": ap.get_ffmpeg_version(),
            "filters": filters,
            "encoders": encoders,
            "backend": video_backend,
        },
    }
    probe = lambda mod: subprocess.run([py, "-c", f"import {mod}"], capture_output=True).returncode == 0
    m["python"] = {
        "path": py,
        **{k: probe(k) for k in ("torch", "demucs", "vieneu", "mlx.core", "mlx_whisper", "faster_whisper", "soundfile")},
    }
    tool_names = ("ffmpeg", "ffprobe", "rubberband", "uvx", "node")
    m["tools"] = {t: ap.tool_exists(t) for t in tool_names}
    m["tool_paths"] = {t: ap.find_tool(t) for t in tool_names}
    hub = os.path.expanduser("~/.cache/huggingface/hub")
    m["models"] = sorted(d for d in (os.listdir(hub) if os.path.isdir(hub) else []) if "whisper" in d.lower())
    if video_backend == "videotoolbox":
        enc_choices = [e for e in ("h264_videotoolbox", "hevc_videotoolbox") if encoders.get(e)]
    elif video_backend == "nvenc":
        enc_choices = [e for e in ("h264_nvenc", "hevc_nvenc") if encoders.get(e)]
    else:
        enc_choices = [e for e in ("libx264", "libx265") if encoders.get(e)]
    rb_hint = "MISSING (run install.ps1)" if ap.is_windows() else "MISSING (brew install rubberband)"
    m["choose"] = {
        "asr": asr_info["label"],
        "render": f"ffmpeg one-pass ({video_backend}: dub + watermark / stream-copy)",
        "encoder": " + ".join(enc_choices) if enc_choices else "libx264",
        "stems": demucs_info["label"],
        "stretch": "rubberband R3" if m["tools"]["rubberband"] else rb_hint,
        "gpu": f"{gpu.get('backend')} ({gpu.get('name')}, {gpu.get('vram_gb')} GB)" if gpu.get("vram_gb") else f"{gpu.get('backend')} ({gpu.get('name')})",
    }
    out = os.path.join(os.path.abspath(a.root), "config", "machine.json")
    jdump(out, m)
    for k, v in m["choose"].items():
        print(f"  {k:8s} {v}")
    print(f"[doctor] {m['chip']} | {m['cores']} cores | {m['ram_gb']} GB RAM -> {out}")


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
    p = sp.add_parser("plan"); p.add_argument("--unit", required=True)
    p.add_argument("--codec", choices=list(ENCODE_PRESETS), default=None)
    p.add_argument("--quality", type=int, default=None)
    p.add_argument("--brand-dir", default=None)
    p.set_defaults(f=cmd_plan)
    p = sp.add_parser("tts-sync"); p.add_argument("--unit", required=True); p.add_argument("--voice", default="Trúc Ly")
    p.add_argument("--store", action="store_true"); p.set_defaults(f=cmd_tts_sync)
    p = sp.add_parser("render"); p.add_argument("--unit", required=True)
    p.add_argument("--no-gate", action="store_true", help="tests only")
    p.add_argument("--codec", choices=list(ENCODE_PRESETS), default=None, help="encoder preset: h264 | hevc | h264_high")
    p.add_argument("--quality", type=int, default=None, help="Encoder quality (-q:v VideoToolbox / -cq NVENC / -crf x264)")
    p.add_argument("--brand-dir", default=None, help="folder with 13_top_left_logo_large.png & 15_top_right_vietsub_floating.png")
    p.set_defaults(f=cmd_render)
    p = sp.add_parser("verify-overlay"); p.add_argument("--unit", required=True); p.add_argument("--no-gate", action="store_true")
    p.set_defaults(f=cmd_verify_overlay)
    p = sp.add_parser("verify-render"); p.add_argument("--unit", required=True); p.add_argument("--no-gate", action="store_true")
    p.set_defaults(f=cmd_verify_overlay)
    p = sp.add_parser("clean"); p.add_argument("--unit", required=True); p.add_argument("--dry-run", action="store_true")
    p.add_argument("--deep", action="store_true", help="also remove .staging_render.mp4 and master_audio_48k.wav")
    p.add_argument("--force", action="store_true"); p.set_defaults(f=cmd_clean)
    p = sp.add_parser("merge-series"); p.add_argument("--hub", required=True)
    p.add_argument("--allow-incomplete", action="store_true"); p.set_defaults(f=cmd_merge_series)
    p = sp.add_parser("run-series"); p.add_argument("--hub", required=True)
    p.add_argument("--phase", choices=["prep", "produce"], required=True)
    p.add_argument("--units", default=None, help="comma-separated unit names to stream (e.g. unit_01_ep01_10,unit_02_ep11_20)")
    p.add_argument("--codec", choices=list(ENCODE_PRESETS), default=None)
    p.add_argument("--quality", type=int, default=None)
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
