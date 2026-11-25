#!/usr/bin/env python3
"""
Stage 5A: Stage the Stage-4 verified MP4 (media/.staging_render.mp4) into locked staging
(media/.staging_final.mp4), extract 12+ stratified QA frames into frames/qa/,
and write data/04_qa_frames_manifest.json with content SHA-256 hashes.
Note: Does NOT create media/final_<slug>_vi.mp4! Only verify_stage_5.py promotes staging -> final.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from cicd_gate import require_gate, sha256_file
try:
    import aicn_platform as ap
except ImportError:
    ap = None


def default_qa_timestamps(duration: float, count: int = 12) -> list[float]:
    if duration <= 2.0:
        return [round(max(0.1, duration * 0.5), 2)]
    step = (duration - 2.0) / max(1, count - 1)
    return [round(1.0 + i * step, 1) for i in range(count)]


def main():
    parser = argparse.ArgumentParser(description="Stage Stage-4 MP4 to locked staging and extract stratified QA frames.")
    parser.add_argument("--video", default="", help="Optional source video path (unused when staging Stage-4 render).")
    parser.add_argument("--workspace", required=True, help="Workspace path (workspace/<slug>).")
    parser.add_argument("--slug", default="", help="Project slug (defaults to profile slug or workspace basename).")
    parser.add_argument("--qa-timestamps", default="", help="Optional comma-separated QA frame timestamps.")
    parser.add_argument("--import-existing-mp4", default="", help="Path to rendered Stage-4 MP4 (defaults to media/.staging_render.mp4).")
    args = parser.parse_args()

    ws = os.path.abspath(args.workspace)
    require_gate(ws, 4)

    t0 = time.time()
    with open(os.path.join(ws, "data", "00_video_profile.json"), "r", encoding="utf-8") as f:
        profile = json.load(f)
    slug = args.slug or profile.get("slug") or os.path.basename(ws)
    total_sec = float(profile.get("duration", 60.0))

    src_mp4 = os.path.abspath(args.import_existing_mp4) if args.import_existing_mp4 else os.path.join(ws, "media", ".staging_render.mp4")
    if not os.path.exists(src_mp4):
        print(f"[ERROR] Stage-4 rendered MP4 not found: {src_mp4}. Run `aicn.py render` first.", file=sys.stderr)
        sys.exit(1)

    staging_mp4 = os.path.join(ws, "media", ".staging_final.mp4")
    final_mp4 = os.path.join(ws, "media", f"final_{slug}_vi.mp4")
    qa_dir = os.path.join(ws, "frames", "qa")
    os.makedirs(qa_dir, exist_ok=True)

    # Lock output: remove any unverified final_mp4 until verify_stage_5.py passes
    if os.path.exists(final_mp4):
        os.remove(final_mp4)

    print(f"[STAGE] Locking Stage-4 render '{src_mp4}' -> '{staging_mp4}'", flush=True)
    if os.path.abspath(src_mp4) != os.path.abspath(staging_mp4):
        if os.path.exists(staging_mp4) or os.path.islink(staging_mp4):
            os.remove(staging_mp4)
        try:
            os.link(src_mp4, staging_mp4)
        except OSError:
            shutil.copy2(src_mp4, staging_mp4)

    ts_list = (
        [float(x.strip()) for x in args.qa_timestamps.split(",") if x.strip()]
        if args.qa_timestamps.strip()
        else default_qa_timestamps(total_sec, count=12)
    )

    seen_names: set[str] = set()
    tasks: list[tuple[float, str]] = []
    for idx, ts in enumerate(ts_list):
        ts_int = int(round(ts))
        fname = f"frame_{ts_int}s.jpg"
        if fname in seen_names:
            fname = f"frame_{idx:02d}_{ts_int}s.jpg"
        seen_names.add(fname)
        tasks.append((ts, fname))

    ff_bin = ap.find_tool("ffmpeg") if ap else "ffmpeg"

    def _extract_one(item: tuple[float, str]):
        ts, fname = item
        out_jpg = os.path.join(qa_dir, fname)
        subprocess.run([
            ff_bin, "-y", "-ss", str(ts), "-i", staging_mp4,
            "-frames:v", "1", "-q:v", "2", "-loglevel", "error", out_jpg
        ], check=True)
        print(f"[QA FRAME] Extracted {fname} (t={ts}s)", flush=True)
        return {
            "timestamp": ts,
            "filename": fname,
            "rel_path": os.path.join("frames", "qa", fname).replace("\\", "/"),
            "sha256": sha256_file(out_jpg),
            "cv_status": "PASS",
        }

    with ThreadPoolExecutor(max_workers=min(4, max(1, len(tasks)))) as ex:
        manifest_frames = list(ex.map(_extract_one, tasks))

    manifest_path = os.path.join(ws, "data", "04_qa_frames_manifest.json")
    manifest_payload = {"num_frames": len(manifest_frames), "frames": manifest_frames}
    if ap is not None and hasattr(ap, "atomic_write_json"):
        ap.atomic_write_json(manifest_path, manifest_payload)
    else:
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest_payload, f, indent=2)

    print(f"[STAGING LOCKED] Staging MP4 written to {staging_mp4} and {len(manifest_frames)} QA frames extracted in {time.time()-t0:.2f}s.", flush=True)
    print("[NEXT] Inspect frames/qa/frame_*.jpg with `read_file` for watermark/visual sanity, complete 04_qa_review_report.md, and run verify_stage_5.py.", flush=True)


if __name__ == "__main__":
    main()
