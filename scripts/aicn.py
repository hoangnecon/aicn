#!/usr/bin/env python3
"""
aicn.py - incremental build & localization engine for the AICN v3.1.1 multi-provider plugin.

Provides unified CLI subcommands across all pipeline stages and user intents:

  doctor          probe local hardware (CUDA/Metal/CPU, NVENC/VideoToolbox, tools) -> config/machine.json
  discover        read a root folder: series hubs, units, gate status, progress
  locate          pinpoint exact unit, episode, cue, and utterance from full-series/unit timestamp or text query
  audit           strict read-only forensic QA across chained gates, script, TTS pacing, LUFS mix, frames & merge
  init-series     scaffold a Series Hub (CHANGELOG, PROGRESS, bible, inbox, units)
  update-series   update series_slug, title_vi, title_zh in a Series Hub
  adopt           add the incremental layer to a unit made by init_workspace.py
  plan            dry-run: whether Stage 1 or Stage 4 render is current or stale
  render          fast one-pass hardware render (cached tight-strip RGBA watermark + audio) -> media/.staging_render.mp4
  verify-render   Stage 4 gate (alias: verify-overlay): A/V sync, codec/audio compliance, frame integrity
  run-series      warm multi-unit batch prep/produce pipeline with parallel CPU & lock-free NVENC
  merge-series    concatenate all verified units into 1 full-series MP4 + SRT
  brand-poster    rename, resize, optionally de-star & dual-corner brand a poster
  clean           safely prune heavy regenerable intermediates after final MP4 exists
  progress        rebuild the hub PROGRESS.md table
  log             append a line to CHANGELOG.md and refresh PROGRESS.md
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import aicn_platform as ap  # noqa: E402

SKILL_DIR = os.path.normpath(os.path.join(HERE, ".."))

FF_RENDERER_VERSION = ap.FF_RENDERER_VERSION
TTS_ENGINE_TAG = ap.TTS_ENGINE_TAG
ENCODE_PRESETS = ("h264", "hevc", "h264_high")


def resolve_encode(cfg_or_style, codec=None, quality=None, backend=None):
    return ap.resolve_encode_preset(cfg_or_style, codec=codec, quality=quality, backend=backend)


def resolve_watermark(ws, brand_dir=None, width=1920):
    """Return dual-corner watermark spec dict (scaled cleanly on 720p/portrait widths) or None."""
    wm_scale = round(max(0.5, width / 1920.0), 3) if width < 1600 else 1.0

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
    if not wm.get("enabled"):
        return None

    def _find_brand_asset(stored_p, fname):
        if stored_p:
            cand = stored_p if os.path.isabs(stored_p) else os.path.normpath(os.path.join(ws, stored_p))
            if os.path.exists(cand):
                return cand
        hub = resolve_unit_hub(ws, cfg)
        fallback_dirs = []
        if wm.get("brand_dir"):
            bd_stored = wm["brand_dir"]
            bd_cand = bd_stored if os.path.isabs(bd_stored) else os.path.normpath(os.path.join(ws, bd_stored))
            fallback_dirs.append(bd_cand)
            if hub:
                fallback_dirs.append(os.path.join(os.path.dirname(hub), os.path.basename(bd_stored)))
        if hub:
            fallback_dirs.append(os.path.join(os.path.dirname(hub), "branding-dau-do"))
        proj = ap.find_project_root(ws)
        if proj is not None:
            fallback_dirs.append(str(proj / "workspace" / "branding-dau-do"))
        for d in fallback_dirs:
            if d:
                c = os.path.join(d, fname)
                if os.path.exists(c):
                    return c
        return ""

    tl = _find_brand_asset(wm.get("top_left_logo", ""), "13_top_left_logo_large.png")
    tr = _find_brand_asset(wm.get("top_right_badge", ""), "15_top_right_vietsub_floating.png")
    if tl and tr and os.path.exists(tl) and os.path.exists(tr):
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
    ap.atomic_write_json(p, obj)


h_obj = ap.hash_obj


def cicd():
    """Import cicd_gate.py from the unified scripts/ directory (or AICN_CICD_DIR override)."""
    cands = [os.environ.get("AICN_CICD_DIR", ""), HERE]
    for c in cands:
        if c and os.path.exists(os.path.join(c, "cicd_gate.py")):
            if c not in sys.path:
                sys.path.insert(0, c)
            import cicd_gate  # noqa
            return cicd_gate
    sys.exit("[aicn] cicd_gate.py not found; set AICN_CICD_DIR")


def is_unit(d):
    return os.path.exists(os.path.join(d, "config", "project_config.json"))


def is_hub(d):
    return os.path.isdir(os.path.join(d, "units")) and os.path.exists(os.path.join(d, "config", "series_bible.json"))


def resolve_unit_hub(ws, cfg=None):
    """Return active Series Hub directory for a unit (relocation-safe relative -> legacy abs -> parent-of-units)."""
    cfg = cfg if cfg is not None else (jload(os.path.join(ws, "config", "project_config.json"), {}) or {})
    hub_rel = cfg.get("series_hub_rel") or ""
    if hub_rel:
        cand_rel = os.path.abspath(os.path.join(ws, hub_rel))
        if os.path.isdir(cand_rel) and is_hub(cand_rel):
            return cand_rel
    hub = cfg.get("series_hub_dir") or ""
    if hub and os.path.isdir(hub) and is_hub(hub):
        return hub
    cand = os.path.dirname(os.path.dirname(os.path.abspath(ws)))
    if os.path.basename(os.path.dirname(os.path.abspath(ws))) == "units" and is_hub(cand):
        return cand
    return ""


def gate_status(ws):
    out = []
    prev_sig = None
    upstream_dirty = False
    for n in range(6):
        g = jload(os.path.join(ws, ".cicd", f"stage_{n}_gate.json"))
        if not g:
            out.append("-")
            prev_sig = None
            continue
        cur_sig = g.get("signature")
        if g.get("status") != "PASS" or float(g.get("score", 0)) < 100.0:
            out.append("F")
            prev_sig = cur_sig
            upstream_dirty = True
            continue
        up_sig = g.get("upstream_signature")
        chain_broken = upstream_dirty or (n > 0 and bool(up_sig) and up_sig != prev_sig)
        missing_art = False
        modified_art = False
        for k, meta in (g.get("artifacts") or {}).items():
            p = k if os.path.isabs(k) else os.path.join(ws, k)
            if not os.path.exists(p):
                missing_art = True
            elif isinstance(meta, dict) and meta.get("sha256") and ap.content_sha256(p) != meta.get("sha256"):
                modified_art = True
        if chain_broken or modified_art:
            out.append("x")
            upstream_dirty = True
        elif missing_art:
            out.append("x")
        else:
            out.append("P")
        prev_sig = cur_sig
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
        bible = jload(os.path.join(hub, "config", "series_bible.json"), {}) or {}
        scfg = jload(os.path.join(hub, "config", "series_config.json"), {}) or {}
        t_vi = bible.get("title_vi") or scfg.get("title_vi") or ""
        t_zh = bible.get("title_zh") or scfg.get("title_zh") or ""
        if t_vi:
            print(f"  title_vi: {t_vi}")
        if t_zh:
            print(f"  title_zh: {t_zh}")
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
            final = [
                p for p in glob.glob(os.path.join(u, "media", "final_*_vi.mp4"))
                if not os.path.basename(p).startswith(".")
            ] or glob.glob(os.path.join(u, "deliverables", "16x9", "*.mp4"))
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


def validate_series_slug(slug, ctx="init-series"):
    import re
    if not slug or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        sys.exit(
            f"[{ctx}] invalid --series-slug '{slug}': "
            "must be translated to Vietnamese, no accents, lowercase, hyphen-separated "
            "(e.g. 'nam-18-tuoi-toi-che-tao-than-khi')"
        )
    return slug


def _sync_series_manifest(hub, series_slug, title_vi="", title_zh="", scfg=None):
    scfg = scfg if scfg is not None else (jload(os.path.join(hub, "config", "series_config.json"), {}) or {})
    genre = scfg.get("genre", "short-drama")
    mode = scfg.get("localization_mode", "voice_dub")
    units = [u for u in sorted(glob.glob(os.path.join(hub, "units", "*"))) if is_unit(u)]
    rows = []
    for u in units:
        uslug = os.path.basename(u)
        ep_man = jload(os.path.join(u, "data", "00_episodes_manifest.json"), {}) or {}
        n_eps = len(ep_man) if isinstance(ep_man, list) else len(ep_man.get("episodes", []))
        gs = gate_status(u)
        status = "READY (Stage 5 PASS)" if (len(gs) >= 6 and gs[5] == "P") else f"GATES:{gs}"
        rows.append(
            f"| `{uslug}` | `series_episodic` | `{n_eps or '?'}` | `{status}` | "
            f"`units/{uslug}/media/final_{uslug}_vi.mp4` | `units/{uslug}/data/subtitles_full.srt` |"
        )
    if not rows:
        rows.append("| _pending_ | `series_episodic` | `-` | `PENDING` | `-` | `-` |")
    manifest_p = os.path.join(hub, "00_series_manifest.md")
    lines = [
        f"# Series Hub Manifest: `{series_slug}`",
        "",
        f"- **Series Slug:** `{series_slug}`",
        f"- **Title (VI):** {title_vi or series_slug}",
        f"- **Title (ZH):** {title_zh or '_unspecified_'}",
        f"- **Genre:** `{genre}`",
        f"- **Localization Mode:** `{mode}`",
        "- **Shared Series Bible:** `config/series_bible.json`",
        "",
        "## Working Units Registry",
        "| Unit Slug | Topology | Episodes | Stage Status | Batch MP4 | Batch SRT |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
        *rows,
        "",
    ]
    with open(manifest_p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _sync_progress_header(hub, series_slug, title_vi, old_slug=None):
    p = os.path.join(hub, "PROGRESS.md")
    if not os.path.exists(p):
        return
    with open(p, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    heading = f"# PROGRESS — {title_vi or series_slug} (`{series_slug}`)"
    if lines and lines[0].startswith("# PROGRESS"):
        lines[0] = heading
    else:
        lines.insert(0, heading)
    txt = "\n".join(lines) + "\n"
    if old_slug and old_slug != series_slug:
        txt = txt.replace(f"final_{old_slug}_full_vi", f"final_{series_slug}_full_vi")
    with open(p, "w", encoding="utf-8") as f:
        f.write(txt)


def cmd_init_series(a):
    validate_series_slug(a.series_slug, "init-series")
    title_vi = getattr(a, "title_vi", "") or a.title or ""
    title_zh = getattr(a, "title_zh", "") or ""
    hub = os.path.abspath(os.path.join(a.workspace_root, a.series_slug))
    for d in ("config", "source_inbox", "units", "deliverables/subtitles", "posters", ".cache"):
        os.makedirs(os.path.join(hub, d), exist_ok=True)
    kw = dict(series=a.series_slug, title=title_vi or a.series_slug, date=now(), total=a.total_episodes,
              unit_size=a.unit_size, rights=a.rights)
    write_if_absent(os.path.join(hub, "CHANGELOG.md"), render_tpl("CHANGELOG.md", **kw))
    write_if_absent(os.path.join(hub, "PROGRESS.md"), render_tpl("PROGRESS.md", **kw))
    write_if_absent(os.path.join(hub, "config", "rights.json"), json.dumps(
        {"series": a.series_slug, "basis": a.rights, "proof": "", "platforms_allowed": [], "content_id_risk": "unknown"},
        ensure_ascii=False, indent=2))
    write_if_absent(os.path.join(hub, "config", "series_bible.json"), json.dumps(
        {"schema_version": "3.0", "series_slug": a.series_slug, "source_language": getattr(a, "source_lang", "zh"),
         "title_zh": title_zh, "title_vi": title_vi, "characters": [],
         "pronoun_matrix": [], "glossary": [], "voices": {"default": "Trúc Ly"}},
        ensure_ascii=False, indent=2))
    scfg_p = os.path.join(hub, "config", "series_config.json")
    scfg = {"schema_version": "3.0", "series_slug": a.series_slug, "title_vi": title_vi, "title_zh": title_zh,
            "genre": "short-drama", "localization_mode": "voice_dub", "voice": "Trúc Ly", "updated_at": now()}
    write_if_absent(scfg_p, json.dumps(scfg, ensure_ascii=False, indent=2))
    if not os.path.exists(os.path.join(hub, "00_series_manifest.md")):
        _sync_series_manifest(hub, a.series_slug, title_vi=title_vi, title_zh=title_zh, scfg=scfg)
    print("[init-series] done. Next: drop episodes in source_inbox/<batch>/, then init_workspace.py --topology series_episodic, then `aicn.py adopt`.")


def _rename_hub_slug_artifacts(hub, old_slug, new_slug):
    renamed = []
    targets = [
        (os.path.join(hub, "deliverables", f"final_{old_slug}_full_vi.mp4"),
         os.path.join(hub, "deliverables", f"final_{new_slug}_full_vi.mp4")),
        (os.path.join(hub, "deliverables", "subtitles", f"final_{old_slug}_full_vi.srt"),
         os.path.join(hub, "deliverables", "subtitles", f"final_{new_slug}_full_vi.srt")),
    ]
    posters_dir = os.path.join(hub, "posters")
    if os.path.isdir(posters_dir):
        for fn in sorted(os.listdir(posters_dir)):
            if old_slug in fn:
                targets.append((os.path.join(posters_dir, fn),
                                os.path.join(posters_dir, fn.replace(old_slug, new_slug))))
    for src, dst in targets:
        if os.path.exists(src) and src != dst and not os.path.exists(dst):
            os.replace(src, dst)
            renamed.append(ap.normalize_rel_path(dst, hub))
    return renamed


def cmd_update_series(a):
    hub = os.path.abspath(a.hub)
    if not is_hub(hub):
        sys.exit(f"[update-series] {hub} is not a Series Hub (expected units/ + config/series_bible.json)")

    bible_p = os.path.join(hub, "config", "series_bible.json")
    scfg_p = os.path.join(hub, "config", "series_config.json")
    rights_p = os.path.join(hub, "config", "rights.json")
    bible = jload(bible_p, {}) or {}
    scfg = jload(scfg_p, {}) or {}
    old_slug = os.path.basename(hub)
    cur_vi = bible.get("title_vi") or scfg.get("title_vi") or ""
    cur_zh = bible.get("title_zh") or scfg.get("title_zh") or ""

    new_slug = a.series_slug if a.series_slug is not None else old_slug
    new_vi = a.title_vi if a.title_vi is not None else cur_vi
    new_zh = a.title_zh if a.title_zh is not None else cur_zh

    if a.series_slug is None and a.title_vi is None and a.title_zh is None:
        print(f"series_slug: {old_slug}\ntitle_vi:    {cur_vi}\ntitle_zh:    {cur_zh}")
        return

    validate_series_slug(new_slug, "update-series")

    renamed_hub = False
    if new_slug != old_slug:
        new_hub = os.path.join(os.path.dirname(hub), new_slug)
        if os.path.exists(new_hub):
            sys.exit(f"[update-series] target directory already exists: {new_hub}")
        os.rename(hub, new_hub)
        hub = new_hub
        bible_p = os.path.join(hub, "config", "series_bible.json")
        scfg_p = os.path.join(hub, "config", "series_config.json")
        rights_p = os.path.join(hub, "config", "rights.json")
        renamed_hub = True

    renamed_files = _rename_hub_slug_artifacts(hub, old_slug, new_slug) if new_slug != old_slug else []

    bible["series_slug"] = new_slug
    bible["title_vi"] = new_vi
    bible["title_zh"] = new_zh
    jdump(bible_p, bible)

    scfg["schema_version"] = "3.0"
    scfg["series_slug"] = new_slug
    scfg["title_vi"] = new_vi
    scfg["title_zh"] = new_zh
    scfg.setdefault("genre", "short-drama")
    scfg["localization_mode"] = "voice_dub"
    scfg.pop("export_formats", None)
    scfg["updated_at"] = now()
    jdump(scfg_p, scfg)

    if os.path.exists(rights_p):
        rights = jload(rights_p, {}) or {}
        rights["series"] = new_slug
        jdump(rights_p, rights)

    locked_units = []
    for u in [d for d in sorted(glob.glob(os.path.join(hub, "units", "*"))) if is_unit(d)]:
        ub_p = os.path.join(u, "config", "series_bible.json")
        if os.path.exists(ub_p):
            ub = jload(ub_p, {}) or {}
            ub["series_slug"] = new_slug
            ub["title_vi"] = new_vi
            ub["title_zh"] = new_zh
            jdump(ub_p, ub)
        if new_slug != old_slug:
            has_gate0 = os.path.exists(os.path.join(u, ".cicd", "stage_0_gate.json"))
            if getattr(a, "sync_unit_configs", False) or not has_gate0:
                pc_p = os.path.join(u, "config", "project_config.json")
                pc = jload(pc_p, {}) or {}
                pc["series_slug"] = new_slug
                pc["series_hub_rel"] = ap.normalize_rel_path(hub, u)
                pc.pop("series_hub_dir", None)
                jdump(pc_p, pc)
                vp_p = os.path.join(u, "data", "00_video_profile.json")
                if os.path.exists(vp_p):
                    vp = jload(vp_p, {}) or {}
                    vp["series_slug"] = new_slug
                    jdump(vp_p, vp)
            else:
                locked_units.append(os.path.basename(u))

    _sync_series_manifest(hub, new_slug, title_vi=new_vi, title_zh=new_zh, scfg=scfg)
    if os.path.exists(os.path.join(hub, "PROGRESS.md")):
        _sync_progress_header(hub, new_slug, new_vi, old_slug=old_slug)
        refresh_progress(hub)

    changes = []
    if new_slug != old_slug:
        changes.append(f"series_slug={old_slug}->{new_slug}")
    if a.title_vi is not None:
        changes.append(f"title_vi='{new_vi}'")
    if a.title_zh is not None:
        changes.append(f"title_zh='{new_zh}'")
    clog = os.path.join(hub, "CHANGELOG.md")
    if os.path.exists(clog):
        with open(clog, "a", encoding="utf-8") as f:
            f.write(f"- {now()} [edit] hub: update-series ({', '.join(changes)})\n")

    print(f"[update-series] hub={hub}")
    print(f"  series_slug: {new_slug}")
    print(f"  title_vi:    {new_vi}")
    print(f"  title_zh:    {new_zh}")
    if renamed_hub and renamed_files:
        print(f"  renamed:     {', '.join(renamed_files)}")
    if locked_units:
        print(f"  note:        kept Stage-0 signed project_config.json for {', '.join(locked_units)} (series_bible.json updated; pass --sync-unit-configs if re-initializing Stage 0)")


def cmd_adopt(a):
    ws = os.path.abspath(a.unit)
    if not is_unit(ws):
        sys.exit(f"[adopt] {ws} has no config/project_config.json - run init_workspace.py first")
    cfg = jload(os.path.join(ws, "config", "project_config.json"))
    os.makedirs(os.path.join(ws, ".cache"), exist_ok=True)
    kw = dict(unit=cfg.get("slug", os.path.basename(ws)), series=cfg.get("series_slug", ""), date=now(),
              mode=cfg.get("localization_mode", "voice_dub"))
    write_if_absent(os.path.join(ws, "CHECKLIST.md"), render_tpl("CHECKLIST.md", **kw))
    write_if_absent(os.path.join(ws, "CHANGELOG.md"), render_tpl("UNIT_CHANGELOG.md", **kw))
    state_p = os.path.join(ws, ".cache", "build_state.json")
    if not os.path.exists(state_p):
        jdump(state_p, {"created": now(), "stage1_key": None})
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


def _resolve_unit_asr_meta(ws, cfg=None):
    cfg = cfg if cfg is not None else (jload(os.path.join(ws, "config", "project_config.json"), {}) or {})
    asr_cache = jload(os.path.join(ws, ".cache", "stage1_asr_cache.json"), {}) or {}
    model = asr_cache.get("model") or os.environ.get("AICN_WHISPER_MODEL", "large-v3")
    src_lang = asr_cache.get("source_lang") or cfg.get("source_language")
    if not src_lang:
        bible = jload(os.path.join(ws, "config", "series_bible.json"), {}) or {}
        src_lang = bible.get("source_language") or "zh"
    return str(model), str(src_lang)


def stage1_key(ws, model=None, source_lang=None):
    src = os.path.join(ws, "source", "master_source.mp4")
    voc = os.path.join(ws, "media", "stems", "vocals.wav")
    det_model, det_lang = _resolve_unit_asr_meta(ws)
    return ap.stage1_asr_key(
        ap.content_sha256(src),
        ap.content_sha256(voc),
        model or det_model,
        source_lang or det_lang,
    )


def ff_render_key(ws, enc=None, wm=None, width=1920, height=1080):
    enc = enc or resolve_encode({})
    src = os.path.join(ws, "source", "master_source.mp4")
    audio = os.path.join(ws, "media", "master_audio_48k.wav")
    src_sha = ap.content_sha256(src) if os.path.exists(src) else "NO_SOURCE"
    audio_sha = ap.content_sha256(audio) if os.path.exists(audio) else None
    wm_key = None
    if wm:
        tl_sha = ap.content_sha256(wm["tl"])
        tr_sha = ap.content_sha256(wm["tr"])
        wm_key = ap.wm_overlay_key(
            width,
            height,
            tl_sha,
            tr_sha,
            wm.get("scale", 1.0),
            [wm["tl_x"], wm["tl_y"], wm["tr_mx"], wm["tr_y"]],
        )
    enc_effective = enc if wm else {"vcodec": "copy"}
    key = ap.render_key(src_sha, audio_sha, wm_key, enc_effective)
    parts = {
        "wm": wm_key,
        "src": src_sha,
        "audio": audio_sha,
        "enc": enc_effective,
        "v": FF_RENDERER_VERSION,
    }
    return parts, key


def resolve_target_dims(ws, prof, cfg=None):
    """Return target (W, H) from Series Hub target_resolution if configured, else unit profile."""
    hub = resolve_unit_hub(ws, cfg)
    if hub:
        scfg = jload(os.path.join(hub, "config", "series_config.json"), {}) or {}
        tr = scfg.get("target_resolution")
        if isinstance(tr, (list, tuple)) and len(tr) == 2 and int(tr[0]) > 0 and int(tr[1]) > 0:
            return int(tr[0]), int(tr[1])
    return int(prof.get("width", 1920)), int(prof.get("height", 1080))


def cmd_plan(a, quiet=False):
    ws = os.path.abspath(a.unit)
    prof, eps, cues, cfg = load_unit(ws)
    state = jload(os.path.join(ws, ".cache", "build_state.json"), {}) or {}
    enc = resolve_encode(cfg, getattr(a, "codec", None), getattr(a, "quality", None))
    W, H = resolve_target_dims(ws, prof, cfg)
    wm = resolve_watermark(ws, getattr(a, "brand_dir", None), width=W)
    parts, key = ff_render_key(ws, enc, wm, width=W, height=H)
    prev = state.get("render", {})
    staging = os.path.exists(os.path.join(ws, "media", ".staging_render.mp4"))
    if prev.get("key") == key and staging:
        why, dirty = "current", []
    else:
        changed = [k for k, v in parts.items() if prev.get("parts", {}).get(k) != h_obj(v)] if prev else ["new"]
        why, dirty = "stale: " + ",".join(changed or ["staging missing"]), ["unit"]
    s1 = stage1_key(ws)
    if not quiet:
        print(f"[plan] unit={os.path.basename(ws)} engine=ffmpeg render={why} wm={'on' if wm else 'off'}")
        print("  !! source changed since Stage 1 -> Stage 1 must be re-run for this unit"
              if state.get("stage1_key") and state["stage1_key"] != s1 else
              "  stage1: frozen (source unchanged) - do NOT re-run Whisper")
    return dirty


# ----------------------------------------------------------------- media helpers & fast Stage 4 renderer
def ffprobe_duration(p):
    return float(subprocess.run([ap.find_tool("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", p],
                                capture_output=True, text=True).stdout.strip() or 0)


def ffprobe_streams(p):
    out = subprocess.check_output([
        ap.find_tool("ffprobe"), "-v", "error", "-show_entries",
        "stream=index,codec_type,codec_name,codec_tag_string,width,height,r_frame_rate,avg_frame_rate,time_base,start_time,duration,nb_frames,sample_rate,channels",
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


def build_cached_watermark_strip(wm, width, height, hint_path=None):
    """Pre-composite dual-corner branding ONCE into a tight top-band RGBA PNG cached globally by content hash."""
    from PIL import Image

    tl_sha = ap.content_sha256(wm["tl"])
    tr_sha = ap.content_sha256(wm["tr"])
    pos = [int(wm["tl_x"]), int(wm["tl_y"]), int(wm["tr_mx"]), int(wm["tr_y"])]
    sc = float(wm.get("scale", 1.0))
    wm_key = ap.wm_overlay_key(width, height, tl_sha, tr_sha, sc, pos)
    overlay_png = ap.global_cache_path("wm_overlay", wm_key, ".png", hint_path=hint_path)
    if os.path.exists(overlay_png):
        return overlay_png, wm_key

    with Image.open(wm["tl"]) as _tl, Image.open(wm["tr"]) as _tr:
        tl_im = _tl.convert("RGBA")
        tr_im = _tr.convert("RGBA")

    if sc < 0.999:
        tl_w = max(2, int(round(tl_im.width * sc / 2.0)) * 2)
        tl_h = max(2, int(round(tl_im.height * sc / 2.0)) * 2)
        tr_w = max(2, int(round(tr_im.width * sc / 2.0)) * 2)
        tr_h = max(2, int(round(tr_im.height * sc / 2.0)) * 2)
        tl_im = tl_im.resize((tl_w, tl_h), Image.LANCZOS)
        tr_im = tr_im.resize((tr_w, tr_h), Image.LANCZOS)

    tl_x, tl_y, tr_mx, tr_y = pos
    raw_band_h = max(tl_y + tl_im.height, tr_y + tr_im.height) + 8
    band_h = min(int(height), raw_band_h + (raw_band_h % 2))
    band_h = max(2, band_h - (band_h % 2))

    canvas = Image.new("RGBA", (int(width), band_h), (0, 0, 0, 0))
    dx_tl, dy_tl = max(0, tl_x), max(0, tl_y)
    if dx_tl < int(width) and dy_tl < band_h:
        tl_crop = tl_im.crop((0, 0, min(tl_im.width, int(width) - dx_tl), min(tl_im.height, band_h - dy_tl)))
        if tl_crop.width > 0 and tl_crop.height > 0:
            canvas.alpha_composite(tl_crop, (dx_tl, dy_tl))
    dx_tr = max(0, int(width) - tr_im.width - tr_mx)
    dy_tr = max(0, tr_y)
    if dx_tr < int(width) and dy_tr < band_h:
        tr_crop = tr_im.crop((0, 0, min(tr_im.width, int(width) - dx_tr), min(tr_im.height, band_h - dy_tr)))
        if tr_crop.width > 0 and tr_crop.height > 0:
            canvas.alpha_composite(tr_crop, (dx_tr, dy_tr))

    tmp = f"{overlay_png}.{os.getpid()}.{threading.get_ident()}.png"
    try:
        canvas.save(tmp, "PNG")
        for attempt in range(8):
            try:
                os.replace(tmp, overlay_png)
                break
            except PermissionError:
                if os.path.exists(overlay_png):
                    break
                if attempt == 7:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return overlay_png, wm_key


def render_ffmpeg(ws, prof, audio, out, enc=None, wm=None, target_w=0, target_h=0):
    """Mux source video + 48kHz master audio + optional cached tight-strip watermark (lock-free hardware encode)."""
    enc = enc or resolve_encode({})
    fps = resolve_fps(ws, prof)
    src_mp4 = os.path.join(ws, "source", "master_source.mp4")
    has_audio = os.path.exists(audio)
    ff_bin = ap.find_tool("ffmpeg")
    part_out = out + ".part.mp4"
    src_w = int(prof.get("width") or 0)
    src_h = int(prof.get("height") or 0)
    if src_w <= 0 or src_h <= 0:
        vs = next((s for s in ffprobe_streams(src_mp4) if s.get("codec_type") == "video"), {})
        src_w = int(vs.get("width") or 1920)
        src_h = int(vs.get("height") or 1080)
    W = int(target_w or src_w)
    H = int(target_h or src_h)
    need_scale = (W != src_w or H != src_h)

    if wm or need_scale:
        overlay_png = build_cached_watermark_strip(wm, W, H, hint_path=ws)[0] if wm else None
        filters = ap.probe_ffmpeg_filters(ff_bin)
        use_cuda_overlay = (
            bool(wm)
            and enc.get("backend") == "nvenc"
            and filters.get("overlay_cuda")
            and filters.get("hwupload_cuda")
            and os.environ.get("AICN_NO_CUDA_OVERLAY") != "1"
        )
        scale_prefix = f"scale={W}:{H}:flags=bicubic," if need_scale else ""

        def _build_wm_cmd(cuda_mode):
            c = [ff_bin, "-v", "error", "-y"]
            if not cuda_mode:
                c += ap.build_hwaccel_input_args(enc)
            c += ["-i", src_mp4]
            audio_idx = None
            next_idx = 1
            if has_audio:
                c += ["-i", audio]
                audio_idx = next_idx
                next_idx += 1
            if wm:
                wm_idx = next_idx
                c += ["-i", overlay_png]
                if cuda_mode:
                    # Omit -hwaccel_output_format cuda: NVDEC pads coded frames (e.g. 240->256, 1080->1088)
                    fg = (
                        f"[0:v]{scale_prefix}format=yuv420p,hwupload_cuda[base];"
                        f"[{wm_idx}:v]format=yuva420p,hwupload_cuda[wm];"
                        f"[base][wm]overlay_cuda=0:0:eof_action=repeat[v]"
                    )
                elif need_scale:
                    fg = f"[0:v]{scale_prefix}format=yuv420p[base];[base][{wm_idx}:v]overlay=0:0:eof_action=repeat:format=auto,format={enc['pix_fmt']}[v]"
                else:
                    fg = f"[0:v][{wm_idx}:v]overlay=0:0:eof_action=repeat:format=auto,format={enc['pix_fmt']}[v]"
            else:
                fg = f"[0:v]{scale_prefix}format={enc['pix_fmt']}[v]"
            c += ["-filter_complex", fg, "-map", "[v]"]
            c += ["-map", f"{audio_idx}:a", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"] if audio_idx is not None else ["-an"]
            c += ap.build_vcodec_args(enc)
            c += ["-g", str(int(round(fps * 2))), "-fps_mode", "passthrough", "-sn", "-movflags", "+faststart", part_out]
            return c

        try:
            if use_cuda_overlay:
                try:
                    subprocess.run(_build_wm_cmd(True), check=True, capture_output=True, text=True)
                except subprocess.CalledProcessError:
                    if os.path.exists(part_out):
                        try:
                            os.remove(part_out)
                        except OSError:
                            pass
                    subprocess.run(_build_wm_cmd(False), check=True)
            else:
                subprocess.run(_build_wm_cmd(False), check=True)
            os.replace(part_out, out)
        finally:
            if os.path.exists(part_out):
                try:
                    os.remove(part_out)
                except OSError:
                    pass
    else:
        cmd = [ff_bin, "-v", "error", "-y", "-i", src_mp4]
        if has_audio:
            cmd += ["-i", audio, "-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
        else:
            cmd += ["-map", "0:v:0", "-an"]
        cmd += ["-c:v", "copy", "-sn", "-shortest", "-movflags", "+faststart", part_out]
        try:
            subprocess.run(cmd, check=True)
            os.replace(part_out, out)
        finally:
            if os.path.exists(part_out):
                try:
                    os.remove(part_out)
                except OSError:
                    pass


def cmd_render(a):
    ws = os.path.abspath(a.unit)
    if not a.no_gate:
        cicd().require_gate(ws, 3)
    prof, eps, cues, cfg = load_unit(ws)
    enc = resolve_encode(cfg, getattr(a, "codec", None), getattr(a, "quality", None))
    W, H = resolve_target_dims(ws, prof, cfg)
    wm = resolve_watermark(ws, getattr(a, "brand_dir", None), width=W)
    audio = os.path.join(ws, "media", "master_audio_48k.wav")
    parts, key = ff_render_key(ws, enc, wm, width=W, height=H)
    out = os.path.join(ws, "media", ".staging_render.mp4")
    state = jload(os.path.join(ws, ".cache", "build_state.json"), {}) or {}
    prev = state.get("render", {})
    if prev.get("key") == key and os.path.exists(out):
        print("[render] unchanged -> staging is current")
        return
    t0 = time.time()
    if not wm and W == int(prof.get("width", W)) and H == int(prof.get("height", H)):
        enc_desc = "stream-copy"
    elif "q" in enc:
        enc_desc = f"{enc['vcodec']}(q={enc['q']})"
    elif "cq" in enc:
        enc_desc = f"{enc['vcodec']}(cq={enc['cq']})"
    elif "crf" in enc:
        enc_desc = f"{enc['vcodec']}(crf={enc['crf']})"
    else:
        enc_desc = f"{enc['vcodec']}({enc.get('bitrate')})"
    print(f"[render] ffmpeg one pass, enc={enc_desc} wm={'on' if wm else 'off'} size={W}x{H} -> {out}", flush=True)
    render_ffmpeg(ws, prof, audio, out, enc=enc, wm=wm, target_w=W, target_h=H)
    sz_mb = os.path.getsize(out) / 1e6
    print(f"[render] ffmpeg {time.time() - t0:.1f}s -> {out} ({sz_mb:.1f} MB)")
    state["render"] = {"engine": "ffmpeg", "key": key, "parts": {k: h_obj(v) for k, v in parts.items()},
                       "enc": enc if wm else {"vcodec": "copy"}, "wm": bool(wm), "renderer": FF_RENDERER_VERSION}
    state["last_render"] = {"at": now(), "rendered": 1, "reused": 0, "output": "media/.staging_render.mp4",
                            "seconds": round(time.time() - t0, 1), "size_mb": round(sz_mb, 1)}
    jdump(os.path.join(ws, ".cache", "build_state.json"), state)


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
        src_p = os.path.join(ws, "source", "master_source.mp4")
        src_streams = ffprobe_streams(src_p) if os.path.exists(src_p) else []
        src_v = next((s for s in src_streams if s.get("codec_type") == "video"), {})
        src_a = next((s for s in src_streams if s.get("codec_type") == "audio"), {})

        v_codec = sv.get("codec_name", "")
        a_codec = sa.get("codec_name", "")
        a_sr = int(sa.get("sample_rate") or 0)
        a_ch = int(sa.get("channels") or 0)
        t_w, t_h = resolve_target_dims(ws, prof, cfg)
        target_w = int(t_w or src_v.get("width") or 0)
        target_h = int(t_h or src_v.get("height") or 0)
        out_w = int(sv.get("width") or 0)
        out_h = int(sv.get("height") or 0)
        dims_ok = (target_w <= 0 or target_h <= 0) or (out_w == target_w and out_h == target_h)
        has_master_audio = os.path.exists(os.path.join(ws, "media", "master_audio_48k.wav"))
        audio_ok = (a_codec == "aac" and a_sr == 48000 and a_ch == 2) if (sa or has_master_audio) else True
        codec_ok = (v_codec in ("h264", "hevc")) and audio_ok and len(ssub) == 0 and dims_ok
        codec_detail = f"v={v_codec}({out_w}x{out_h}/{target_w}x{target_h}), a={a_codec}({a_sr}Hz,{a_ch}ch), sub_streams={len(ssub)}"

        v_dur = float(sv.get("duration") or 0.0)
        a_dur = float(sa.get("duration") or v_dur)
        v_st = float(sv.get("start_time") or 0.0)
        a_st = float(sa.get("start_time") or 0.0)
        target_dur = float(src_v.get("duration") or 0.0) or float(prof.get("duration", v_dur))
        dur_ok = abs(v_dur - target_dur) <= 0.30
        av_dur_ok = abs(v_dur - a_dur) <= 0.30
        src_offset = float(src_v.get("start_time") or 0.0) - float(src_a.get("start_time") or 0.0)
        av_st_ok = abs(v_st - a_st) <= 0.12 or (bool(src_v) and abs((v_st - a_st) - src_offset) <= 0.12)
        frames_ok = True
        frame_note = ""
        if not prof.get("is_vfr") and src_v and sv.get("nb_frames"):
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
    Default keeps all Stage 0-5 gate-hashed artifacts (PPPPPP stays green) while removing stems,
    premaster/dub WAVs, per-utterance TTS WAVs, and waveform .npy caches;
    --deep also removes .staging_render.mp4, master_audio_48k.wav, and unit source copies."""
    ws = os.path.abspath(a.unit)
    finals = [
        p for p in glob.glob(os.path.join(ws, "media", "final_*_vi.mp4"))
        if not os.path.basename(p).startswith(".")
    ] or glob.glob(os.path.join(ws, "deliverables", "16x9", "*.mp4"))
    if not finals and not a.force:
        sys.exit(f"[clean] refusing to prune {ws}: no final MP4 found (pass --force to override)")
    targets = [
        os.path.join(ws, "media", "stems"),
        os.path.join(ws, "media", "tts_segments"),
        os.path.join(ws, "media", "source_audio.wav"),
        os.path.join(ws, "media", "audio", "premaster_48k.wav"),
        os.path.join(ws, "media", "audio", "dub_voice_48k.wav"),
        os.path.join(ws, "media", ".staging_final.mp4"),
        os.path.join(ws, ".cache", "rb_clause"),
        *glob.glob(os.path.join(ws, ".cache", "waveform_features_*.npy")),
    ]
    if getattr(a, "deep", False):
        targets += [
            os.path.join(ws, "media", ".staging_render.mp4"),
            os.path.join(ws, "media", "master_audio_48k.wav"),
            os.path.join(ws, "source", "master_source.mp4"),
            os.path.join(ws, "source", "episodes"),
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
        bn = os.path.basename(u)
        gs = gate_status(u)
        if not a.allow_incomplete and (len(gs) < 6 or gs[5] != "P"):
            sys.exit(f"[merge-series] {bn} Stage 5 gate is not PASS ({gs}) — every batch must PASS before full-series release")
        cand_mp4 = sorted(
            p for p in glob.glob(os.path.join(u, "media", "final_*_vi.mp4"))
            if not os.path.basename(p).startswith(".")
        ) or (
            glob.glob(os.path.join(hub, "deliverables", "16x9", f"*{bn}*.mp4"))
            + glob.glob(os.path.join(hub, "deliverables", "9x16", f"*{bn}*.mp4"))
        )
        if not cand_mp4:
            sys.exit(f"[merge-series] missing batch MP4 for {bn}")
        mp4s.append(cand_mp4[0])
        durs.append(ffprobe_duration(cand_mp4[0]))
        ep_man = jload(os.path.join(u, "data", "00_episodes_manifest.json"), [])
        total_eps += len(ep_man) if isinstance(ep_man, list) else len(ep_man.get("episodes", []))
        cand_srt = (
            [os.path.join(u, "data", "subtitles_full.srt")]
            + glob.glob(os.path.join(hub, "deliverables", "subtitles", f"*{bn}*.srt"))
        )
        srts.append(next((p for p in cand_srt if os.path.exists(p)), None))
    batch_specs, time_bases, codec_tags = set(), set(), set()
    v_codec = None
    for p in mp4s:
        st = ffprobe_streams(p)
        vs = next((s for s in st if s.get("codec_type") == "video"), {})
        as_ = next((s for s in st if s.get("codec_type") == "audio"), {})
        vd, ad = float(vs.get("duration") or 0.0), float(as_.get("duration") or 0.0)
        if abs(vd - ad) > 0.30:
            sys.exit(f"[merge-series] A/V duration drift in {os.path.basename(p)}: video={vd:.3f}s audio={ad:.3f}s (diff={vd-ad:+.3f}s)")
        v_codec = vs.get("codec_name")
        time_bases.add(vs.get("time_base"))
        codec_tags.add(vs.get("codec_tag_string"))
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
    ff_bin = ap.find_tool("ffmpeg")
    concat_inputs = mp4s
    norm_files = []
    if len(time_bases) > 1 or len(codec_tags) > 1:
        norm_dir = os.path.join(hub, ".cache", "merge_norm")
        os.makedirs(norm_dir, exist_ok=True)
        concat_inputs = []
        tag_args = ["-tag:v", "hev1"] if (v_codec == "hevc" and len(codec_tags) > 1) else []
        for idx, p in enumerate(mp4s):
            np_out = os.path.join(norm_dir, f"norm_{idx:03d}.mp4")
            subprocess.run([ff_bin, "-v", "error", "-y", "-i", p, "-c", "copy",
                            "-video_track_timescale", "90000", *tag_args, np_out], check=True)
            concat_inputs.append(np_out)
            norm_files.append(np_out)
    with open(lst, "w", encoding="utf-8") as f:
        f.writelines(ap.ffmpeg_concat_line(p) for p in concat_inputs)
    try:
        subprocess.run([ff_bin, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                        "-avoid_negative_ts", "make_zero", "-c", "copy", "-movflags", "+faststart", out_mp4 + ".part.mp4"], check=True)
    finally:
        for np_out in norm_files:
            if os.path.exists(np_out):
                os.remove(np_out)
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
    # Prune any legacy per-unit slice copies inside hub/deliverables/ so deliverables/ holds ONLY the single full-series master
    for sub_d in (os.path.join(hub, "deliverables", "16x9"), os.path.join(hub, "deliverables", "9x16")):
        if os.path.isdir(sub_d):
            for up in glob.glob(os.path.join(sub_d, "final_*_vi*.mp4")):
                if os.path.abspath(up) != os.path.abspath(out_mp4):
                    try:
                        os.remove(up)
                    except OSError:
                        pass
    for up_srt in glob.glob(os.path.join(hub, "deliverables", "subtitles", "*_vi.srt")):
        if os.path.abspath(up_srt) != os.path.abspath(out_srt):
            try:
                os.remove(up_srt)
            except OSError:
                pass
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
    """Warm multi-unit full-series batch runner (VRAM-slot GPU + parallel CPU + lock-free NVENC/MediaEngine).
    - prep:    batch-separate (warm Demucs) -> run_asr_and_scan --units (warm Whisper) -> parallel CPU waveform & audit-frames.
    - produce: batch-all (warm VieNeu CUDA + parallel CPU mix/master/verify) -> lock-free parallel render/verify-render/qa-extract."""
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
    audio_py = os.path.join(HERE, "aicn_audio.py")
    aicn_py = os.path.join(HERE, "aicn.py")
    s1_dir = HERE
    s5_dir = HERE

    gpu_info = ap.get_gpu_info(py)
    demucs_tag = "Metal" if ap.detect_demucs_backend(py, gpu_info)["device"] == "mps" else ("CUDA" if gpu_info.get("cuda") else "CPU")
    asr_tag = "Metal" if ap.detect_asr_backend(py, gpu_info)["device"] == "mps" else ("CUDA" if gpu_info.get("cuda") else "CPU")
    vid_tag = {"videotoolbox": "MediaEngine", "nvenc": "NVENC", "cpu": "CPU"}[ap.detect_video_backend()]

    def _run(tag, cmd, check=True):
        t0 = time.time()
        print(f"[run-series] START {tag}", flush=True)
        rc = 0
        if not a.dry_run:
            res = subprocess.run(cmd, check=check, env=ap.child_env_with_held_locks())
            rc = res.returncode
        print(f"[run-series] DONE  {tag} ({time.time() - t0:.1f}s, rc={rc})", flush=True)
        return rc

    t_all = time.time()
    if a.phase == "prep":
        todo = [u for u in units if len(gate_status(u)) >= 2 and gate_status(u)[0] == "P" and gate_status(u)[1] != "P"]
        print(f"[run-series] phase=prep hub={os.path.basename(hub)} batches={len(todo)}/{len(units)} dry_run={a.dry_run}", flush=True)
        if todo:
            unit_arg = ",".join(todo)
            _run(f"batch-separate({len(todo)})[{demucs_tag}]", [py, audio_py, "batch-separate", "--units", unit_arg])
            _run(f"batch-asr({len(todo)})[{asr_tag}]", [py, os.path.join(s1_dir, "run_asr_and_scan.py"), "--units", unit_arg, "--skip-waveform"])

            def _prep_cpu(u):
                bn = os.path.basename(u)
                src = os.path.join(u, "source", "master_source.mp4")
                _run(f"{bn}:waveform[CPU]", [py, os.path.join(s1_dir, "analyze_voice_waveform.py"), "--workspace", u, "--snap-and-split"])
                _run(f"{bn}:audit-frames[CPU]", [py, os.path.join(s1_dir, "extract_audit_frames.py"), "--video", src, "--workspace", u])
                return bn

            with ThreadPoolExecutor(max_workers=min(4, max(1, len(todo)))) as ex:
                list(ex.map(_prep_cpu, todo))
        print(f"[run-series] prep finished in {(time.time() - t_all) / 60:.1f} min. Next: visually inspect frames/audit/ and run verify_stage_1.py.", flush=True)
        return

    # phase == "produce": warm batch audio + lock-free parallel hardware render & QA extraction
    todo = [
        u for u in units
        if len(gate_status(u)) >= 6
        and gate_status(u)[0] == "P"
        and gate_status(u)[1] == "P"
        and gate_status(u)[2] == "P"
        and gate_status(u)[5] != "P"
    ]
    print(f"[run-series] phase=produce hub={os.path.basename(hub)} batches={len(todo)}/{len(units)} dry_run={a.dry_run}", flush=True)
    render_flags = []
    if a.codec:
        render_flags += ["--codec", a.codec]
    if a.quality is not None:
        render_flags += ["--quality", str(a.quality)]
    if a.brand_dir:
        render_flags += ["--brand-dir", os.path.abspath(a.brand_dir)]

    audio_todo = [u for u in todo if gate_status(u)[3] != "P"]
    audio_rc = 0
    if audio_todo:
        audio_rc = _run(
            f"batch-all({len(audio_todo)})",
            [py, audio_py, "batch-all", "--units", ",".join(audio_todo)],
            check=False,
        )

    def _render_is_dirty(u):
        p_args = argparse.Namespace(unit=u, codec=a.codec, quality=a.quality, brand_dir=a.brand_dir)
        return bool(cmd_plan(p_args, quiet=True))

    if a.dry_run:
        render_todo = list(todo)
    else:
        render_todo = [
            u for u in todo
            if gate_status(u)[3] == "P"
            and (
                gate_status(u)[4] != "P"
                or _render_is_dirty(u)
                or not os.path.exists(os.path.join(u, "data", "04_qa_frames_manifest.json"))
            )
        ]

    def _produce_render(u):
        bn = os.path.basename(u)
        gs = gate_status(u)
        need_render = a.dry_run or gs[4] != "P" or _render_is_dirty(u)
        if need_render:
            _run(f"{bn}:render[{vid_tag}]", [py, aicn_py, "render", "--unit", u, *render_flags])
            _run(f"{bn}:verify-render", [py, aicn_py, "verify-render", "--unit", u])
        qa_man = os.path.join(u, "data", "04_qa_frames_manifest.json")
        if need_render or not os.path.exists(qa_man):
            src = os.path.join(u, "source", "master_source.mp4")
            stg = os.path.join(u, "media", ".staging_render.mp4")
            ts = _qa_timestamps_for(u)
            _run(f"{bn}:qa-extract", [py, os.path.join(s5_dir, "composite_and_verify.py"),
                                      "--video", src, "--workspace", u, "--slug", bn,
                                      "--import-existing-mp4", stg, "--qa-timestamps", ts])
        return bn

    max_render = max(1, int(os.environ.get("AICN_MAX_RENDER_JOBS", "2") or "2"))
    done = []
    if render_todo:
        with ThreadPoolExecutor(max_workers=min(max_render, len(render_todo))) as ex:
            done = list(ex.map(_produce_render, render_todo))

    failed_audio = [] if a.dry_run else [os.path.basename(u) for u in audio_todo if gate_status(u)[3] != "P"]
    print(f"[run-series] produce finished {len(done)} render batch(es) in {(time.time() - t_all) / 60:.1f} min.", flush=True)
    if audio_rc != 0 or failed_audio:
        sys.exit(f"[run-series] Stage 3 audio failed/overflowed for unit(s): {', '.join(failed_audio) or 'see batch-all output'}")
    print(f"[run-series] Next: view frames/qa/ per batch -> verify_stage_5.py -> `aicn.py merge-series --hub {hub}` for the full-series release.", flush=True)


# ----------------------------------------------------------------- log / progress
def cmd_log(a):
    ws = os.path.abspath(a.unit)
    p = os.path.join(ws, "CHANGELOG.md")
    with open(p, "a", encoding="utf-8") as f:
        f.write(f"- {now()} [{a.kind}] {a.message}\n")
    cfg = jload(os.path.join(ws, "config", "project_config.json"), {})
    hub = resolve_unit_hub(ws, cfg)
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
    vram_slots = ap.gpu_vram_budget_mb(gpu) // ap.SLOT_UNIT_MB
    m["choose"] = {
        "asr": asr_info["label"],
        "render": f"ffmpeg one-pass ({video_backend}: cached tight-strip wm / stream-copy)",
        "encoder": " + ".join(enc_choices) if enc_choices else "libx264",
        "stems": demucs_info["label"],
        "stretch": "rubberband R3" if m["tools"]["rubberband"] else rb_hint,
        "gpu": (
            f"{gpu.get('backend')} ({gpu.get('name')}, {gpu.get('vram_gb')} GB, {vram_slots}x{ap.SLOT_UNIT_MB}MB slots)"
            if gpu.get("vram_gb")
            else f"{gpu.get('backend')} ({gpu.get('name')}, {vram_slots}x{ap.SLOT_UNIT_MB}MB slots)"
        ),
    }
    out = os.path.join(os.path.abspath(a.root), "config", "machine.json")
    jdump(out, m)
    for k, v in m["choose"].items():
        print(f"  {k:8s} {v}")
    print(f"[doctor] {m['chip']} | {m['cores']} cores | {m['ram_gb']} GB RAM -> {out}")


def _poster_detect_aspect(w, h, requested="auto"):
    if requested and requested != "auto":
        return requested
    ratio = float(w) / max(float(h), 1.0)
    if ratio < 0.65:
        return "9x16"
    if ratio > 1.25:
        return "16x9"
    return "3x4"


def _poster_inpaint_corner_star(im):
    """Smoothly inpaint a Flow/Imagen bottom-right 4-point corner star via boundary Laplace fill."""
    import numpy as np
    from PIL import Image

    arr = np.asarray(im.convert("RGB"), dtype=np.float32).copy()
    h, w, _ = arr.shape
    cx = int(round(w * (1285.0 / 1376.0)))
    cy = int(round(h * (662.0 / 768.0)))
    rad = max(16, int(round(min(w, h) * 0.048)))
    y0, y1 = max(2, cy - rad), min(h - 2, cy + rad)
    x0, x1 = max(2, cx - rad), min(w - 2, cx + rad)
    patch = arr[y0 - 2:y1 + 2, x0 - 2:x1 + 2].copy()
    ph, pw, _ = patch.shape
    yy, xx = np.ogrid[:ph, :pw]
    cy_l, cx_l = ph / 2.0, pw / 2.0
    mask = ((yy - cy_l) ** 2 + (xx - cx_l) ** 2) <= (rad * 0.92) ** 2
    mask[:2, :] = False; mask[-2:, :] = False; mask[:, :2] = False; mask[:, -2:] = False
    ring = (((yy - cy_l) ** 2 + (xx - cx_l) ** 2) <= (rad * 1.15) ** 2) & (~mask)
    if np.any(ring):
        fill_val = np.median(patch[ring], axis=0)
        patch[mask] = fill_val
        for _ in range(40):
            avg = 0.25 * (patch[:-2, 1:-1] + patch[2:, 1:-1] + patch[1:-1, :-2] + patch[1:-1, 2:])
            patch[1:-1, 1:-1][mask[1:-1, 1:-1]] = avg[mask[1:-1, 1:-1]]
        arr[y0 - 2:y1 + 2, x0 - 2:x1 + 2] = patch
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


def _poster_resize_cover(im, target_w, target_h):
    """Aspect-preserving center crop + Lanczos resize to exact target dimensions."""
    from PIL import Image

    w, h = im.size
    target_ar = target_w / float(target_h)
    cur_ar = w / float(h)
    if abs(cur_ar - target_ar) > 0.015:
        if cur_ar > target_ar:
            new_w = int(round(h * target_ar))
            x0 = (w - new_w) // 2
            im = im.crop((x0, 0, x0 + new_w, h))
        else:
            new_h = int(round(w / target_ar))
            y0 = (h - new_h) // 2
            im = im.crop((0, y0, w, y0 + new_h))
    if im.size != (target_w, target_h):
        im = im.resize((target_w, target_h), Image.LANCZOS)
    return im


def cmd_brand_poster(a):
    """Standardize a localized Vietnamese poster into Tier 2 _raw, _clean.png, and _branded.png."""
    try:
        from PIL import Image
    except ImportError:
        py = ap.find_python()
        if os.path.abspath(py) != os.path.abspath(sys.executable):
            subprocess.run([py] + sys.argv, check=True)
            return
        sys.exit("[brand-poster] Pillow (PIL) is required. Run with $AICN_PY.")

    src = os.path.abspath(a.input)
    if not os.path.isfile(src):
        sys.exit(f"[brand-poster] input image not found: {src}")

    if a.hub:
        hub = os.path.abspath(a.hub)
        out_dir = os.path.abspath(a.out_dir) if a.out_dir else os.path.join(hub, "posters")
        cfg = jload(os.path.join(hub, "config", "series_config.json"), {}) or {}
        slug = a.slug or cfg.get("series_slug") or os.path.basename(hub)
    else:
        if not a.out_dir or not a.slug:
            sys.exit("[brand-poster] provide --hub <series_hub> OR both --out-dir and --slug")
        out_dir = os.path.abspath(a.out_dir)
        slug = a.slug

    os.makedirs(out_dir, exist_ok=True)
    with Image.open(src) as _im:
        im = _im.convert("RGB")
    aspect = _poster_detect_aspect(im.size[0], im.size[1], a.aspect)
    targets = {"9x16": (1080, 1920), "16x9": (1920, 1080), "3x4": (1080, 1440)}
    tw, th = targets[aspect]

    src_ext = os.path.splitext(src)[1].lower() or ".png"
    if src_ext not in (".jpg", ".jpeg", ".png", ".webp"):
        src_ext = ".png"
    raw_path = os.path.join(out_dir, f"poster_{slug}_vi_{aspect}_raw{src_ext}")
    clean_path = os.path.join(out_dir, f"poster_{slug}_vi_{aspect}_clean.png")
    branded_path = os.path.join(out_dir, f"poster_{slug}_vi_{aspect}_branded.png")

    if os.path.abspath(src) != os.path.abspath(raw_path):
        shutil.copy2(src, raw_path)
    clean_im = im
    if a.remove_star:
        clean_im = _poster_inpaint_corner_star(clean_im)
    clean_im = _poster_resize_cover(clean_im, tw, th)
    clean_im.save(clean_path, "PNG", optimize=True)

    result = {
        "slug": slug,
        "aspect": aspect,
        "size": [tw, th],
        "raw": raw_path,
        "clean": clean_path,
        "branded": None,
    }

    if a.brand_dir:
        bd = os.path.abspath(a.brand_dir)
        tl_p = os.path.join(bd, "13_top_left_logo_large.png")
        tr_p = os.path.join(bd, "15_top_right_vietsub_floating.png")
        if not (os.path.isfile(tl_p) and os.path.isfile(tr_p)):
            sys.exit(f"[brand-poster] --brand-dir missing 13_top_left_logo_large.png or 15_top_right_vietsub_floating.png in {bd}")
        with Image.open(tl_p) as _tl, Image.open(tr_p) as _tr:
            tl = _tl.convert("RGBA")
            tr = _tr.convert("RGBA")
        canvas = clean_im.convert("RGBA")
        if aspect == "16x9":
            tl_x, tl_y = 42, 22
            center_y = tl_y + tl.size[1] // 2
            tr_x = tw - tr.size[0] - 42
            tr_y = center_y - tr.size[1] // 2
        elif aspect == "3x4":
            tl_x, tl_y = 38, 38
            center_y = tl_y + tl.size[1] // 2
            tr_x = tw - tr.size[0] - 36
            tr_y = center_y - tr.size[1] // 2
        else:  # 9x16 (1080x1920)
            tl_x, tl_y = 38, 46
            center_y = tl_y + tl.size[1] // 2
            tr_x = tw - tr.size[0] - 36
            tr_y = center_y - tr.size[1] // 2
        canvas.alpha_composite(tl, dest=(tl_x, tl_y))
        canvas.alpha_composite(tr, dest=(tr_x, tr_y))
        canvas.convert("RGB").save(branded_path, "PNG", optimize=True)
        result["branded"] = branded_path

    if a.delete_source and os.path.abspath(src) != os.path.abspath(raw_path):
        try:
            os.remove(src)
        except OSError:
            pass

    print(f"[brand-poster] {slug} ({aspect} {tw}x{th})")
    print(f"  raw:     {raw_path}")
    print(f"  clean:   {clean_path}")
    if result["branded"]:
        print(f"  branded: {result['branded']}")
    return result


# ----------------------------------------------------------------- locate & audit
def _parse_ts_sec(val):
    if val is None or str(val).strip() == "":
        return None
    s = str(val).strip().replace(",", ".")
    parts = s.split(":")
    try:
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            return int(parts[0]) * 60.0 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600.0 + int(parts[1]) * 60.0 + float(parts[2])
    except ValueError:
        pass
    sys.exit(f"[locate] invalid timestamp '{val}' (use SS, MM:SS, or HH:MM:SS)")


def _fmt_mmss(sec):
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec - (h * 3600 + m * 60)
    return f"{h:02d}:{m:02d}:{s:05.2f}" if h else f"{m:02d}:{s:05.2f}"


def _fold_vi_ascii(text):
    import unicodedata
    s = (text or "").replace("đ", "d").replace("Đ", "D").lower()
    return "".join(ch for ch in unicodedata.normalize("NFD", s) if unicodedata.category(ch) != "Mn")


def _unit_duration_s(u):
    finals = [p for p in sorted(glob.glob(os.path.join(u, "media", "final_*_vi.mp4"))) if not os.path.basename(p).startswith(".")]
    if finals:
        try:
            st = ffprobe_streams(finals[0])
            vs = next((s for s in st if s.get("codec_type") == "video"), {})
            d = float(vs.get("duration") or 0.0)
            if d > 0:
                return d
        except Exception:
            pass
    prof = jload(os.path.join(u, "data", "00_video_profile.json"), {}) or {}
    return float(prof.get("duration") or 0.0)


def _unit_episode_spans(u):
    man = jload(os.path.join(u, "data", "00_episodes_manifest.json"))
    eps = man.get("episodes", []) if isinstance(man, dict) else (man if isinstance(man, list) else [])
    spans = []
    cur = 0.0
    for idx, ep in enumerate(eps, 1):
        if not isinstance(ep, dict):
            continue
        dur = float(ep.get("duration") or 0.0)
        st = float(ep["start_s"]) if "start_s" in ep else (float(ep["start"]) if "start" in ep else cur)
        et = float(ep["end_s"]) if "end_s" in ep else (float(ep["end"]) if "end" in ep else (st + dur))
        ep_num = ep.get("episode_number") or ep.get("episode") or ep.get("index") or idx
        spans.append({"episode": ep_num, "file": ep.get("filename") or ep.get("source") or "", "start_s": round(st, 3), "end_s": round(et, 3)})
        cur = et
    return spans


def cmd_locate(a):
    """Pinpoint exact unit, episode, cue(s), and Stage 3 utterance telemetry without mutating any files."""
    if not a.hub and not a.unit:
        sys.exit("[locate] provide --hub <series_hub> and/or --unit <unit_dir_or_name>")
    hub = os.path.abspath(a.hub) if a.hub else ""
    units = []
    if a.unit and os.path.isdir(a.unit) and is_unit(a.unit):
        u_abs = os.path.abspath(a.unit)
        if not hub:
            hub = resolve_unit_hub(u_abs)
        units = [u_abs]
    elif hub:
        if not is_hub(hub):
            sys.exit(f"[locate] {hub} is not a Series Hub")
        all_u = [u for u in sorted(glob.glob(os.path.join(hub, "units", "*"))) if is_unit(u)]
        if a.unit:
            units = [u for u in all_u if os.path.basename(u) == a.unit]
            if not units:
                sys.exit(f"[locate] unit '{a.unit}' not found under {hub}/units/")
        else:
            units = all_u
    else:
        sys.exit(f"[locate] unit '{a.unit}' is not a valid unit workspace")

    all_hub_units = (
        [u for u in sorted(glob.glob(os.path.join(hub, "units", "*"))) if is_unit(u)]
        if hub and is_hub(hub) else list(units)
    )
    cum_map = {}
    cum = 0.0
    for u in all_hub_units:
        d = _unit_duration_s(u)
        cum_map[os.path.abspath(u)] = (cum, cum + d, d)
        cum += d

    full_at = _parse_ts_sec(a.full_at)
    local_at = _parse_ts_sec(a.at)
    q_raw = (a.query or "").strip()
    q_low = q_raw.lower()
    q_fold = _fold_vi_ascii(q_raw)

    if full_at is not None and not a.unit:
        chosen = None
        for u in all_hub_units:
            c0, c1, _ = cum_map[os.path.abspath(u)]
            if c0 <= full_at <= c1 + 0.05:
                chosen = u
                break
        if chosen is None and all_hub_units:
            chosen = all_hub_units[-1] if full_at > cum else all_hub_units[0]
        units = [chosen] if chosen else units

    matches = []
    for u in units:
        u_abs = os.path.abspath(u)
        u_name = os.path.basename(u_abs)
        c0, c1, u_dur = cum_map.get(u_abs, (0.0, _unit_duration_s(u_abs), _unit_duration_s(u_abs)))
        target_local = (full_at - c0) if full_at is not None else local_at

        ep_spans = _unit_episode_spans(u_abs)
        if a.ep is not None and ep_spans:
            ep_hit = next((es for es in ep_spans if str(es["episode"]) == str(a.ep)), None)
            if not ep_hit:
                continue
            if local_at is not None:
                target_local = ep_hit["start_s"] + local_at
            elif full_at is None and not q_raw:
                target_local = ep_hit["start_s"]

        script_p = os.path.join(u_abs, "data", "02_bilingual_script.json")
        cues = jload(script_p)
        cue_source = "data/02_bilingual_script.json"
        if not isinstance(cues, list):
            cues = jload(os.path.join(u_abs, "data", "01_asr_clean.json"))
            cue_source = "data/01_asr_clean.json"
        if not isinstance(cues, list):
            cues = jload(os.path.join(u_abs, "data", "01_asr_raw_segments.json"), [])
            cue_source = "data/01_asr_raw_segments.json"

        dub_sents = jload(os.path.join(u_abs, "data", "03_dub_sentences.json"), []) or []
        seg_to_utt = {}
        for utt in dub_sents:
            if isinstance(utt, dict):
                for sidx in (utt.get("seg_indices") or []):
                    seg_to_utt[int(sidx)] = utt

        hit_indices = []
        for idx, c in enumerate(cues):
            st = float(c.get("start", 0.0))
            et = float(c.get("end", st))
            ok_ts = True
            if target_local is not None:
                ok_ts = (st - 1.5 <= target_local <= et + 1.5)
            ok_q = True
            if q_raw:
                blob = " | ".join(str(c.get(k, "")) for k in ("zh_text", "text", "sub_text", "tts_text", "speaker"))
                ok_q = (q_low in blob.lower()) or (bool(q_fold) and q_fold in _fold_vi_ascii(blob))
            if ok_ts and ok_q and (target_local is not None or q_raw):
                hit_indices.append(idx)

        if not hit_indices and target_local is not None and not q_raw and cues:
            nearest_i = min(
                range(len(cues)),
                key=lambda i: min(abs(float(cues[i].get("start", 0.0)) - target_local), abs(float(cues[i].get("end", 0.0)) - target_local)),
            )
            hit_indices.append(nearest_i)

        for idx in hit_indices[: max(1, int(a.limit))]:
            w = max(0, int(a.window))
            ctx = []
            for j in range(max(0, idx - w), min(len(cues), idx + w + 1)):
                cj = cues[j]
                jst, jet = float(cj.get("start", 0.0)), float(cj.get("end", 0.0))
                utt = seg_to_utt.get(j)
                ctx.append({
                    "cue_index": j,
                    "is_target": j == idx,
                    "local_start_s": round(jst, 3),
                    "local_end_s": round(jet, 3),
                    "local_span": f"{_fmt_mmss(jst)} -> {_fmt_mmss(jet)}",
                    "full_start_s": round(c0 + jst, 3),
                    "full_span": f"{_fmt_mmss(c0 + jst)} -> {_fmt_mmss(c0 + jet)}",
                    "speaker": cj.get("speaker", ""),
                    "zh_text": cj.get("zh_text", cj.get("text", "")),
                    "sub_text": cj.get("sub_text", ""),
                    "tts_text": cj.get("tts_text", ""),
                    "utterance": (
                        {
                            "utt_id": utt.get("utt_id") or utt.get("id"),
                            "seg_indices": utt.get("seg_indices"),
                            "voice": utt.get("voice"),
                            "tempo": utt.get("tempo"),
                            "required_tempo": utt.get("required_tempo"),
                            "overflow_s": utt.get("overflow_s", 0.0),
                        }
                        if utt else None
                    ),
                })
            c_mid = 0.5 * (float(cues[idx].get("start", 0.0)) + float(cues[idx].get("end", 0.0)))
            ep_info = next((es for es in ep_spans if es["start_s"] - 0.05 <= c_mid <= es["end_s"] + 0.05), None)
            matches.append({
                "unit": u_name,
                "unit_dir": u_abs,
                "gates": gate_status(u_abs),
                "unit_full_offset_s": round(c0, 3),
                "unit_duration_s": round(u_dur, 3),
                "cue_source": cue_source,
                "episode": (
                    {
                        "episode": ep_info["episode"],
                        "ep_local_time": _fmt_mmss(max(0.0, c_mid - ep_info["start_s"])),
                    }
                    if ep_info else None
                ),
                "target_cue_index": idx,
                "context_cues": ctx,
                "surgical_rebuild": [
                    f"$AICN_PY {os.path.join(HERE, 'validate_bilingual_script.py')} --workspace {u_abs}",
                    f"$AICN_PY {os.path.join(HERE, 'aicn_audio.py')} all --unit {u_abs}",
                    f"$AICN_PY {os.path.join(HERE, 'aicn.py')} render --unit {u_abs}",
                    f"$AICN_PY {os.path.join(HERE, 'aicn.py')} verify-render --unit {u_abs}",
                    f"$AICN_PY {os.path.join(HERE, 'composite_and_verify.py')} --video {os.path.join(u_abs, 'source', 'master_source.mp4')} --workspace {u_abs} --slug {u_name} --import-existing-mp4 {os.path.join(u_abs, 'media', '.staging_render.mp4')}",
                    f"$AICN_PY {os.path.join(HERE, 'verify_stage_5.py')} --workspace {u_abs}",
                ] + ([f"$AICN_PY {os.path.join(HERE, 'aicn.py')} merge-series --hub {hub}"] if hub else []),
            })
            if len(matches) >= max(1, int(a.limit)):
                break
        if len(matches) >= max(1, int(a.limit)):
            break

    payload = {
        "hub": hub or None,
        "query": q_raw or None,
        "full_at_s": full_at,
        "local_at_s": local_at,
        "matches_count": len(matches),
        "matches": matches,
    }
    if a.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return payload
    if not matches:
        print(f"[locate] 0 matches (hub={hub or '-'}, full_at={a.full_at}, at={a.at}, query={q_raw!r})")
        return payload
    print(f"[locate] Found {len(matches)} match(es):")
    for m in matches:
        ep_str = f" | ep={m['episode']['episode']} ({m['episode']['ep_local_time']})" if m["episode"] else ""
        print(f"  UNIT {m['unit']} (gates={m['gates']}, full_offset={_fmt_mmss(m['unit_full_offset_s'])}{ep_str}) [{m['cue_source']}]")
        for cc in m["context_cues"]:
            mark = ">>" if cc["is_target"] else "  "
            u_meta = ""
            if cc["utterance"]:
                ut = cc["utterance"]
                u_meta = f" [utt={ut['utt_id']} tempo={ut['tempo']} req={ut['required_tempo']} over={ut['overflow_s']}s]"
            print(f"    {mark} [#{cc['cue_index']}] local {cc['local_span']} (full {cc['full_span']}) ({cc['speaker'] or '-'}){u_meta}")
            print(f"       ZH : {cc['zh_text']}")
            if cc["sub_text"] or cc["tts_text"]:
                print(f"       SUB: {cc['sub_text']}")
                print(f"       TTS: {cc['tts_text']}")
    return payload


def cmd_audit(a):
    """Strict read-only forensic quality audit across a Series Hub or single unit (never writes or mutates files)."""
    import re
    cjk_re = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")
    if not a.hub and not a.unit:
        sys.exit("[audit] provide --hub <series_hub> or --unit <unit_dir>")
    hub = os.path.abspath(a.hub) if a.hub else ""
    if a.unit:
        u_abs = os.path.abspath(a.unit) if os.path.isdir(a.unit) else os.path.join(hub, "units", a.unit)
        if not is_unit(u_abs):
            sys.exit(f"[audit] {u_abs} is not a valid unit workspace")
        if not hub:
            hub = resolve_unit_hub(u_abs)
        units = [u_abs]
    else:
        if not is_hub(hub):
            sys.exit(f"[audit] {hub} is not a Series Hub")
        units = [u for u in sorted(glob.glob(os.path.join(hub, "units", "*"))) if is_unit(u)]

    critical = []
    important = []
    unit_reports = []
    newest_unit_mp4_mtime = 0.0

    scfg = jload(os.path.join(hub, "config", "series_config.json"), {}) if hub else {}
    scfg = scfg if isinstance(scfg, dict) else {}
    house_voice = str(scfg.get("voice") or "Trúc Ly").strip() or "Trúc Ly"
    allow_multi_voice = bool(scfg.get("allow_multi_voice", False))

    def _collect_non_house_voices(bible_obj):
        bad = []
        if not isinstance(bible_obj, dict):
            return bad
        for ch in (bible_obj.get("characters") or []):
            if isinstance(ch, dict) and ch.get("voice_id"):
                v = str(ch["voice_id"]).strip()
                if v and v != house_voice:
                    bad.append(f"{ch.get('speaker_id', '?')}={v}")
        vmap = bible_obj.get("voices")
        if isinstance(vmap, dict):
            for k, v in vmap.items():
                vs = str(v or "").strip()
                if vs and vs != house_voice:
                    bad.append(f"voices.{k}={vs}")
        return bad

    if hub and not allow_multi_voice:
        hub_bad_voices = _collect_non_house_voices(jload(os.path.join(hub, "config", "series_bible.json"), {}))
        if hub_bad_voices:
            critical.append(f"hub: non-{house_voice} voice(s) in series_bible.json ({', '.join(hub_bad_voices[:5])}); single voice '{house_voice}' is mandatory unless allow_multi_voice=true")

    for u in units:
        bn = os.path.basename(u)
        gs = gate_status(u)
        if "F" in gs:
            critical.append(f"{bn}: failed gate(s) in '{gs}'")
        elif "x" in gs and gs[5] != "P":
            important.append(f"{bn}: stale/broken gate chain '{gs}' (upstream modified or intermediate missing before Stage 5 PASS)")

        pcfg_u = jload(os.path.join(u, "config", "project_config.json"), {}) or {}
        unit_multi_ok = allow_multi_voice or bool(pcfg_u.get("allow_multi_voice", False))
        if not unit_multi_ok:
            u_bad_bible = _collect_non_house_voices(jload(os.path.join(u, "config", "series_bible.json"), {}))
            if u_bad_bible and not hub:
                critical.append(f"{bn}: non-{house_voice} voice(s) in series_bible.json ({', '.join(u_bad_bible[:5])})")
            acfg_v = str((jload(os.path.join(u, "config", "audio_mix.json"), {}) or {}).get("voice") or house_voice).strip()
            if acfg_v and acfg_v != house_voice:
                critical.append(f"{bn}: audio_mix.json voice='{acfg_v}' != '{house_voice}'")

        script_issues = []
        cues = jload(os.path.join(u, "data", "02_bilingual_script.json"))
        if isinstance(cues, list):
            prev_end = -1.0
            for idx, c in enumerate(cues):
                st, et = float(c.get("start", 0.0)), float(c.get("end", 0.0))
                sub_t, tts_t = str(c.get("sub_text") or "").strip(), str(c.get("tts_text") or "").strip()
                if not sub_t or not tts_t:
                    script_issues.append(f"cue #{idx} empty sub_text/tts_text")
                if cjk_re.search(sub_t) or cjk_re.search(tts_t):
                    script_issues.append(f"cue #{idx} CJK/Hangul leak")
                if prev_end >= 0 and st < prev_end + 0.019:
                    script_issues.append(f"cue #{idx} monotonic/gap violation ({st:.3f}s vs prev_end {prev_end:.3f}s)")
                cue_v = str(c.get("voice") or "").strip()
                if not unit_multi_ok and cue_v and cue_v != house_voice:
                    script_issues.append(f"cue #{idx} non-{house_voice} voice override '{cue_v}'")
                prev_end = et
            if script_issues:
                critical.append(f"{bn}: {len(script_issues)} script defect(s) ({script_issues[0]})")

        pacing_summary = None
        dub_sents = jload(os.path.join(u, "data", "03_dub_sentences.json"))
        if isinstance(dub_sents, list) and dub_sents:
            acfg = jload(os.path.join(u, "config", "audio_mix.json"), {}) or {}
            max_t = float(acfg.get("max_tempo", 1.28))
            max_ts = float(acfg.get("max_tempo_short", max(max_t, 1.32)))
            overs = [s for s in dub_sents if float(s.get("overflow_s", 0.0) or 0.0) > 0.005]
            bad_tempo = []
            bad_voices = []
            runts = [s for s in dub_sents if s.get("runt")]
            worst_tempo = 0.0
            for s in dub_sents:
                slot = float(s.get("slot_end", 0.0)) - float(s.get("slot_start", 0.0))
                cap = max_ts if (int(s.get("n_clauses", 1)) == 1 and slot < 1.2) else max_t
                eff_t = max(float(s.get("tempo", 1.0) or 1.0), float(s.get("required_tempo", 0.0) or 0.0))
                worst_tempo = max(worst_tempo, eff_t)
                if eff_t > cap + 0.005:
                    bad_tempo.append(s.get("utt_id"))
                sv = str(s.get("voice") or "").strip()
                if not unit_multi_ok and sv and sv != house_voice:
                    bad_voices.append(f"{s.get('utt_id')}={sv}")
            pacing_summary = {
                "utterances": len(dub_sents),
                "overflows": len(overs),
                "tempo_violations": len(bad_tempo),
                "non_house_voices": len(bad_voices),
                "runts": len(runts),
                "worst_tempo": round(worst_tempo, 3),
            }
            if overs or bad_tempo or runts:
                critical.append(f"{bn}: Stage 3 pacing defects (overflows={len(overs)}, tempo_violations={len(bad_tempo)}, runts={len(runts)})")
            if bad_voices:
                critical.append(f"{bn}: Stage 3 non-{house_voice} utterance voice(s) ({', '.join(bad_voices[:4])})")

        mix_summary = None
        mix_rep = jload(os.path.join(u, "data", "03_mix_report.json"))
        if isinstance(mix_rep, dict):
            failed_ids = [c.get("id") for c in (mix_rep.get("checks") or []) if not c.get("passed")]
            mix_summary = {"passed": bool(mix_rep.get("passed")), "failed_checks": failed_ids}
            if not mix_rep.get("passed") or failed_ids:
                critical.append(f"{bn}: Stage 3 mix report failed ({failed_ids})")

        qa_frames = sorted(glob.glob(os.path.join(u, "frames", "qa", "*.jpg")))
        finals = [p for p in sorted(glob.glob(os.path.join(u, "media", "final_*_vi.mp4"))) if not os.path.basename(p).startswith(".")]
        if finals:
            newest_unit_mp4_mtime = max(newest_unit_mp4_mtime, os.path.getmtime(finals[0]))
        if a.strict and gs != "PPPPPP" and not (gs[:3] == "PPP" and gs[5] == "P"):
            important.append(f"{bn}: incomplete unit gates '{gs}'")

        unit_reports.append({
            "unit": bn,
            "gates": gs,
            "final_mp4": finals[0] if finals else None,
            "qa_frame_count": len(qa_frames),
            "sample_qa_frames": qa_frames[:: max(1, len(qa_frames) // 4)][:4] if qa_frames else [],
            "script_defect_count": len(script_issues),
            "pacing": pacing_summary,
            "mix": mix_summary,
        })

    release_info = None
    if hub and is_hub(hub) and not a.unit:
        slug = os.path.basename(hub)
        full_mp4 = os.path.join(hub, "deliverables", f"final_{slug}_full_vi.mp4")
        full_srt = os.path.join(hub, "deliverables", "subtitles", f"final_{slug}_full_vi.srt")
        posters = sorted(glob.glob(os.path.join(hub, "posters", "*_branded.png")))
        release_info = {
            "full_mp4": full_mp4 if os.path.exists(full_mp4) else None,
            "full_srt": full_srt if os.path.exists(full_srt) else None,
            "branded_posters": posters,
            "stale_merge": False,
            "av_sync_drift_s": None,
        }
        if os.path.exists(full_mp4):
            if newest_unit_mp4_mtime > os.path.getmtime(full_mp4) + 1.0:
                release_info["stale_merge"] = True
                critical.append("hub: deliverables/final_*_full_vi.mp4 is OLDER than a re-rendered unit MP4 (re-run merge-series!)")
            try:
                st = ffprobe_streams(full_mp4)
                vs = next((s for s in st if s.get("codec_type") == "video"), {})
                as_ = next((s for s in st if s.get("codec_type") == "audio"), {})
                drift = abs(float(vs.get("duration") or 0.0) - float(as_.get("duration") or 0.0))
                release_info["av_sync_drift_s"] = round(drift, 3)
                if drift > 0.50:
                    critical.append(f"hub: full MP4 A/V duration drift {drift:.3f}s > 0.50s")
            except Exception as exc:
                important.append(f"hub: ffprobe failed on full MP4 ({exc})")
        elif a.strict:
            important.append("hub: deliverables/final_*_full_vi.mp4 missing")

    ok = (len(critical) == 0) and (not a.strict or len(important) == 0)
    report = {
        "ok": ok,
        "hub": hub or None,
        "units_count": len(unit_reports),
        "critical": critical,
        "important": important,
        "release": release_info,
        "units": unit_reports,
    }
    if a.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        status_lbl = "PASS" if ok and not important else ("WARN" if ok else "FAIL")
        print(f"[audit] Status={status_lbl} | units={len(unit_reports)} | critical={len(critical)} | important={len(important)}")
        for u_rep in unit_reports:
            p_str = f"utt={u_rep['pacing']['utterances']} max_tempo={u_rep['pacing']['worst_tempo']} over={u_rep['pacing']['overflows']}" if u_rep["pacing"] else "pacing=-"
            print(f"  {u_rep['unit']:28s} gates={u_rep['gates']}  {p_str}  qa_frames={u_rep['qa_frame_count']}")
        if release_info:
            print(f"  full_mp4: {release_info['full_mp4'] or 'MISSING'} (stale_merge={release_info['stale_merge']}, av_drift={release_info['av_sync_drift_s']}s, branded_posters={len(release_info['branded_posters'])})")
        for c in critical:
            print(f"  [CRITICAL]  {c}")
        for imp in important:
            print(f"  [IMPORTANT] {imp}")
    if not ok:
        sys.exit(2)
    return report


# ----------------------------------------------------------------- main
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = parser.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("doctor"); p.add_argument("--root", default="."); p.set_defaults(f=cmd_doctor)
    p = sp.add_parser("discover"); p.add_argument("--root", default="."); p.set_defaults(f=cmd_discover)
    p = sp.add_parser("locate", help="pinpoint unit, episode, cue, and TTS utterance by full-series/unit time or text")
    p.add_argument("--hub", default=None, help="Series Hub directory")
    p.add_argument("--unit", default=None, help="Unit workspace directory or unit folder name")
    p.add_argument("--full-at", default=None, help="timestamp in merged full-series MP4 (SS, MM:SS, or HH:MM:SS)")
    p.add_argument("--at", default=None, help="local timestamp inside unit or episode (SS or MM:SS)")
    p.add_argument("--ep", type=int, default=None, help="episode number inside unit (if 00_episodes_manifest.json exists)")
    p.add_argument("--query", default="", help="substring in zh_text, sub_text, tts_text, or speaker (accent-insensitive)")
    p.add_argument("--window", type=int, default=1, help="context cues before/after each hit")
    p.add_argument("--limit", type=int, default=10, help="max hits to return")
    p.add_argument("--json", action="store_true")
    p.set_defaults(f=cmd_locate)
    p = sp.add_parser("audit", help="strict read-only forensic quality audit across gates, script, audio, frames & merge")
    p.add_argument("--hub", default=None, help="Series Hub directory")
    p.add_argument("--unit", default=None, help="Unit workspace directory or unit folder name")
    p.add_argument("--strict", action="store_true", help="exit non-zero if any unit or full release is incomplete")
    p.add_argument("--json", action="store_true")
    p.set_defaults(f=cmd_audit)
    p = sp.add_parser("init-series"); p.add_argument("--series-slug", required=True); p.add_argument("--title", default="")
    p.add_argument("--title-vi", default="", help="Vietnamese series title (alias for --title)")
    p.add_argument("--title-zh", default="", help="Original Chinese/source series title")
    p.add_argument("--workspace-root", default="workspace"); p.add_argument("--total-episodes", default="?")
    p.add_argument("--unit-size", default=10); p.add_argument("--source-lang", default="zh", help="source audio language (zh, en, auto)")
    p.add_argument("--rights", required=True,
                   help="license basis: own | licensed:<contract> | cps:<platform> | mcn:<name>")
    p.set_defaults(f=cmd_init_series)
    p = sp.add_parser("update-series", help="update series_slug, title_vi, and/or title_zh in a Series Hub")
    p.add_argument("--hub", required=True)
    p.add_argument("--series-slug", default=None, help="new Vietnamese hyphenated slug (renames hub & deliverables)")
    p.add_argument("--title-vi", default=None, help="Vietnamese series title")
    p.add_argument("--title-zh", default=None, help="Original Chinese/source series title")
    p.add_argument("--sync-unit-configs", action="store_true", help="also rewrite unit project_config.json/00_video_profile.json if Stage 0 already signed")
    p.set_defaults(f=cmd_update_series)
    p = sp.add_parser("adopt"); p.add_argument("--unit", required=True); p.set_defaults(f=cmd_adopt)
    p = sp.add_parser("plan"); p.add_argument("--unit", required=True)
    p.add_argument("--codec", choices=list(ENCODE_PRESETS), default=None)
    p.add_argument("--quality", type=int, default=None)
    p.add_argument("--brand-dir", default=None)
    p.set_defaults(f=cmd_plan)
    p = sp.add_parser("render"); p.add_argument("--unit", required=True)
    p.add_argument("--no-gate", action="store_true", help="tests only")
    p.add_argument("--codec", choices=list(ENCODE_PRESETS), default=None, help="encoder preset: h264 | hevc | h264_high")
    p.add_argument("--quality", type=int, default=None, help="Encoder quality (-q:v VideoToolbox / -cq NVENC / -crf x264)")
    p.add_argument("--brand-dir", default=None, help="folder with 13_top_left_logo_large.png & 15_top_right_vietsub_floating.png")
    p.set_defaults(f=cmd_render)
    p = sp.add_parser("verify-render"); p.add_argument("--unit", required=True); p.add_argument("--no-gate", action="store_true")
    p.set_defaults(f=cmd_verify_overlay)
    p = sp.add_parser("verify-overlay", help=argparse.SUPPRESS); p.add_argument("--unit", required=True); p.add_argument("--no-gate", action="store_true")
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
    p = sp.add_parser("brand-poster", help="standardize, optionally de-star, and dual-corner brand a localized poster")
    p.add_argument("--hub", default=None, help="Series Hub directory (infers --out-dir <hub>/posters and --slug)")
    p.add_argument("--out-dir", default=None, help="output posters/ directory (optional if --hub is set)")
    p.add_argument("--slug", default=None, help="series slug override")
    p.add_argument("--input", required=True, help="path to raw localized Vietnamese poster image")
    p.add_argument("--brand-dir", default=None, help="folder with 13_top_left_logo_large.png & 15_top_right_vietsub_floating.png")
    p.add_argument("--aspect", choices=["auto", "9x16", "16x9", "3x4"], default="auto")
    p.add_argument("--remove-star", action="store_true", help="inpaint Flow/Imagen bottom-right corner star before resizing")
    p.add_argument("--delete-source", action="store_true", help="remove original messy-named input file after archiving as _raw")
    p.set_defaults(f=cmd_brand_poster)
    p = sp.add_parser("progress"); p.add_argument("--hub", required=True); p.set_defaults(f=cmd_progress)
    a = parser.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
