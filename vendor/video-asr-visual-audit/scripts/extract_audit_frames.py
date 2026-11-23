#!/usr/bin/env python3
"""
Stage 1B: Extract visual audit keyframes (combining Pass 2 telephone/whisper/PA gap frames,
widest hardsubs, scene cuts, and stratified checkpoints) and write data/01_extracted_audit_manifest.json
with SHA-256 hashes so verify_stage_1.py can enforce that the agent visually inspected and logged every frame.
"""
import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

ORCH_SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "video-localization-orchestrator", "scripts"))
AICN_SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts"))
for _p in (ORCH_SCRIPTS, AICN_SCRIPTS):
    if _p not in sys.path:
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
    bbox_path = os.path.join(workspace, "data", "01_zh_bbox_scan.json")
    asr_path = os.path.join(workspace, "data", "01_asr_raw_segments.json")
    dur = 600.0
    if os.path.exists(profile_path):
        with open(profile_path, "r", encoding="utf-8") as f:
            dur = float(json.load(f).get("duration", 600.0))

    # Priority 1: Any Pass 2 recovered/gap segments (phone calls, whispers, PA broadcasts, screams)
    gap_timestamps = []
    if os.path.exists(asr_path):
        with open(asr_path, "r", encoding="utf-8") as f:
            asr_segs = json.load(f)
        for seg in asr_segs:
            if seg.get("needs_visual_ocr") or "[GAP_VISUAL_SUB" in seg.get("text", ""):
                mid = float(seg.get("mid_timestamp", (float(seg["start"]) + float(seg["end"])) / 2.0))
                gap_timestamps.append(round(mid, 1))

    candidates = []
    if os.path.exists(bbox_path):
        with open(bbox_path, "r", encoding="utf-8") as f:
            bdata = json.load(f)
        candidates.extend(bdata.get("top_wide_timestamps", [])[:6])
        candidates.extend(bdata.get("scene_cuts_sample", [])[:6])

    for frac in [0.03, 0.10, 0.18, 0.26, 0.34, 0.42, 0.50, 0.58, 0.66, 0.74, 0.82, 0.90]:
        candidates.append(round(dur * frac, 1))

    selected = []
    # Include gap timestamps first (spaced >= 2.0s apart so distinct phone/whisper lines are all captured)
    for ts in sorted(gap_timestamps):
        if 0.5 <= ts <= dur - 0.5 and all(abs(ts - s) >= 2.0 for s in selected):
            selected.append(round(ts, 1))

    # Fill up to at least 12 frames with stratified candidates
    min_spacing = min(12.0, max(1.0, dur * 0.05))
    for ts in sorted(candidates):
        if 0.5 <= ts <= dur - 0.5 and all(abs(ts - s) >= min_spacing for s in selected):
            selected.append(round(ts, 1))
        if len(selected) >= max(12, len(gap_timestamps)):
            break
    return sorted(selected)


def main():
    parser = argparse.ArgumentParser(description="Extract audit frames and generate SHA-256 audit manifest.")
    parser.add_argument("--video", required=True, help="Source video path.")
    parser.add_argument("--workspace", required=True, help="Workspace path (workspace/<slug>).")
    parser.add_argument("--timestamps", default="", help="Optional comma-separated timestamps in seconds.")
    args = parser.parse_args()

    require_gate(args.workspace, 0)

    out_dir = os.path.join(args.workspace, "frames", "audit")
    os.makedirs(out_dir, exist_ok=True)
    ts_list = select_smart_timestamps(args.workspace, args.timestamps)

    def _extract_one(ts):
        ts_int = int(round(ts))
        fname = f"frame_{ts_int:04d}s.jpg"
        out_jpg = os.path.join(out_dir, fname)
        cmd = [
            ap.find_tool("ffmpeg") if ap else "ffmpeg", "-y",
            "-ss", str(ts),
            "-i", args.video,
            "-frames:v", "1",
            "-q:v", "2",
            "-loglevel", "error",
            out_jpg
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

    manifest_path = os.path.join(args.workspace, "data", "01_extracted_audit_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({"num_frames": len(manifest_items), "frames": manifest_items}, f, indent=2)

    print(f"[DONE] Saved audit frame manifest ({len(manifest_items)} frames) to {manifest_path}", flush=True)
    print("[NEXT] Inspect each frame in frames/audit/ using `read_file`, resolve any [GAP_VISUAL_SUB] entries in 01_asr_raw_segments.json, log findings in 01_visual_audit_notes.md, then run verify_stage_1.py.", flush=True)


if __name__ == "__main__":
    main()
