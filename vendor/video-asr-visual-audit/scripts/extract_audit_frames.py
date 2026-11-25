#!/usr/bin/env python3
"""
Stage 1B: Extract visual audit keyframes (prioritizing Pass 2B recovered phone/whisper/high/low
segments and stratified checkpoints across the unit duration) and write data/01_extracted_audit_manifest.json
with SHA-256 hashes so verify_stage_1.py can enforce visual inspection in 01_visual_audit_notes.md.
"""
import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

_HERE = os.path.dirname(os.path.abspath(__file__))
ORCH_SCRIPTS = os.path.abspath(os.path.join(_HERE, "..", "..", "video-localization-orchestrator", "scripts"))
for _p in (
    ORCH_SCRIPTS,
    os.path.abspath(os.path.join(_HERE, "..", "..", "..", "scripts")),
    os.path.abspath(os.path.join(_HERE, "..", "..", "aicn", "scripts")),
):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
from cicd_gate import require_gate, sha256_file
try:
    import aicn_platform as ap
except ImportError:
    ap = None


def select_smart_timestamps(workspace: str, explicit_ts: str = "") -> list:
    if explicit_ts.strip():
        return sorted(set(round(float(x.strip()), 1) for x in explicit_ts.split(",") if x.strip()))

    profile_path = os.path.join(workspace, "data", "00_video_profile.json")
    asr_path = os.path.join(workspace, "data", "01_asr_raw_segments.json")
    dur = 600.0
    if os.path.exists(profile_path):
        with open(profile_path, "r", encoding="utf-8") as f:
            dur = float(json.load(f).get("duration", 600.0))

    # Priority 1: Pass 2B recovered lines (phone/radio/PA, high-pitch, whisper, low-voice)
    pass2b_priority = []
    pass2b_other = []
    if os.path.exists(asr_path):
        with open(asr_path, "r", encoding="utf-8") as f:
            asr_segs = json.load(f)
        for seg in asr_segs:
            src = str(seg.get("source", ""))
            if src.startswith("pass2b"):
                mid = round((float(seg["start"]) + float(seg["end"])) / 2.0, 1)
                cue = str(seg.get("cue_type", ""))
                if cue in ("phone_call", "mid_voice", "high_voice", "whisper", "low_voice"):
                    pass2b_priority.append(mid)
                else:
                    pass2b_other.append(mid)

    selected = []
    # Capture up to 8 diverse Pass 2B recovered lines spaced >= 4.0s apart
    for ts in pass2b_priority + pass2b_other:
        if 0.5 <= ts <= dur - 0.5 and all(abs(ts - s) >= 4.0 for s in selected):
            selected.append(ts)
        if len(selected) >= 8:
            break

    # Priority 2: Stratified checkpoints across unit duration to reach >= 12 frames total
    candidates = [
        round(dur * frac, 1)
        for frac in (0.03, 0.10, 0.18, 0.26, 0.34, 0.42, 0.50, 0.58, 0.66, 0.74, 0.82, 0.90, 0.95)
    ]
    min_spacing = min(12.0, max(1.0, dur * 0.05))
    for ts in candidates:
        if 0.5 <= ts <= dur - 0.5 and all(abs(ts - s) >= min_spacing for s in selected):
            selected.append(ts)
        if len(selected) >= 12:
            break

    # Backfill if very short clip still < 12 candidates and duration allows
    if len(selected) < 12 and dur >= 4.0:
        step = max(0.5, (dur - 1.0) / 12.0)
        t = 0.5
        while t <= dur - 0.5 and len(selected) < 12:
            rt = round(t, 1)
            if all(abs(rt - s) >= 0.4 for s in selected):
                selected.append(rt)
            t += step

    return sorted(selected)


def main():
    parser = argparse.ArgumentParser(description="Extract audit frames and generate SHA-256 audit manifest.")
    parser.add_argument("--video", required=False, default="", help="Source video path (defaults to source/master_source.mp4).")
    parser.add_argument("--workspace", required=True, help="Workspace path (workspace/<slug>).")
    parser.add_argument("--timestamps", default="", help="Optional comma-separated timestamps in seconds.")
    args = parser.parse_args()

    ws = os.path.abspath(args.workspace)
    require_gate(ws, 0)

    video = args.video
    if not video:
        cand = os.path.join(ws, "source", "master_source.mp4")
        if os.path.exists(cand):
            video = cand
        else:
            prof_p = os.path.join(ws, "data", "00_video_profile.json")
            if os.path.exists(prof_p):
                with open(prof_p, "r", encoding="utf-8") as f:
                    src_rel = json.load(f).get("source_video", "source/master_source.mp4")
                video = src_rel if os.path.isabs(src_rel) else os.path.join(ws, src_rel)
    if not video or not os.path.exists(video):
        sys.exit(f"[ERROR] Source video not found: {video}")

    out_dir = os.path.join(ws, "frames", "audit")
    os.makedirs(out_dir, exist_ok=True)
    ts_list = select_smart_timestamps(ws, args.timestamps)

    def _extract_one(ts):
        ts_int = int(round(ts))
        fname = f"frame_{ts_int:04d}s.jpg"
        out_jpg = os.path.join(out_dir, fname)
        cmd = [
            ap.find_tool("ffmpeg") if ap else "ffmpeg", "-y",
            "-ss", str(ts),
            "-i", video,
            "-frames:v", "1",
            "-q:v", "2",
            "-loglevel", "error",
            out_jpg,
        ]
        subprocess.run(cmd, check=True)
        rel_path = os.path.join("frames", "audit", fname).replace("\\", "/")
        print(f"[AUDIT FRAME] Extracted {rel_path} (t={ts}s)", flush=True)
        return {
            "timestamp": ts,
            "filename": fname,
            "rel_path": rel_path,
            "sha256": sha256_file(out_jpg),
        }

    with ThreadPoolExecutor(max_workers=min(4, max(1, len(ts_list)))) as ex:
        manifest_items = list(ex.map(_extract_one, ts_list))

    manifest_path = os.path.join(ws, "data", "01_extracted_audit_manifest.json")
    payload = {"num_frames": len(manifest_items), "frames": manifest_items}
    if ap is not None and hasattr(ap, "atomic_write_json"):
        ap.atomic_write_json(manifest_path, payload)
    else:
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    print(f"[DONE] Saved audit frame manifest ({len(manifest_items)} frames) to {manifest_path}", flush=True)
    print("[NEXT] Inspect each frame in frames/audit/ using `read_file`, log visual context/names/homophones in 01_visual_audit_notes.md, then run verify_stage_1.py.", flush=True)


if __name__ == "__main__":
    main()
