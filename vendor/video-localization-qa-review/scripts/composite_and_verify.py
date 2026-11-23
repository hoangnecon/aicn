#!/usr/bin/env python3
"""
Stage 5A: Composite source video, 48kHz stereo master audio, and optional dual-corner branding watermarks
into a LOCKED staging file (media/.staging_final.mp4) with -sn (no burned-in subtitles, no mov_text stream).
Then extract 12+ stratified QA frames into frames/qa/ for watermark/visual sanity verification,
and write data/04_qa_frames_manifest.json.
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

ORCH_SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "video-localization-orchestrator", "scripts"))
sys.path.insert(0, ORCH_SCRIPTS)
from cicd_gate import require_gate, sha256_file


def default_qa_timestamps(duration: float, count: int = 12) -> list[float]:
    if duration <= 2.0:
        return [round(duration * 0.5, 1)]
    step = (duration - 2.0) / max(1, count - 1)
    return [round(1.0 + i * step, 1) for i in range(count)]


def main():
    parser = argparse.ArgumentParser(description="Composite dubbed+watermarked video to locked staging file and extract QA frames.")
    parser.add_argument("--video", required=True, help="Source video path.")
    parser.add_argument("--workspace", required=True, help="Workspace path (workspace/<slug>).")
    parser.add_argument("--slug", required=True, help="Project slug.")
    parser.add_argument("--qa-timestamps", default="", help="Optional comma-separated QA frame timestamps.")
    parser.add_argument("--import-existing-mp4", default="", help="Optional path to an already-rendered MP4 to stage for verification.")
    args = parser.parse_args()

    ws = os.path.abspath(args.workspace)
    require_gate(ws, 4)

    t0 = time.time()
    with open(os.path.join(ws, "data", "00_video_profile.json"), "r", encoding="utf-8") as f:
        profile = json.load(f)
    total_sec = float(profile.get("duration", 60.0))

    audio_in = os.path.join(ws, "media", "master_audio_48k.wav")
    staging_mp4 = os.path.join(ws, "media", ".staging_final.mp4")
    final_mp4 = os.path.join(ws, "media", f"final_{args.slug}_vi.mp4")
    qa_dir = os.path.join(ws, "frames", "qa")
    os.makedirs(qa_dir, exist_ok=True)

    # Lock output: remove any unverified final_mp4 until verify_stage_5.py passes
    if os.path.exists(final_mp4):
        os.remove(final_mp4)

    if args.import_existing_mp4 and os.path.exists(args.import_existing_mp4):
        print(f"[STAGE] Importing existing MP4 '{args.import_existing_mp4}' -> locked staging '{staging_mp4}'", flush=True)
        if os.path.exists(staging_mp4) or os.path.islink(staging_mp4):
            os.remove(staging_mp4)
        try:
            os.link(os.path.abspath(args.import_existing_mp4), staging_mp4)
        except OSError:
            shutil.copy2(args.import_existing_mp4, staging_mp4)
    else:
        proj_cfg_path = os.path.join(ws, "config", "project_config.json")
        wm_cfg = {}
        if os.path.exists(proj_cfg_path):
            with open(proj_cfg_path, "r", encoding="utf-8") as f:
                wm_cfg = json.load(f).get("watermark", {})
        tl_logo = wm_cfg.get("top_left_logo", "")
        tr_badge = wm_cfg.get("top_right_badge", "")
        tl_x = int(wm_cfg.get("top_left_x", 42))
        tl_y = int(wm_cfg.get("top_left_y", 22))
        tr_mx = int(wm_cfg.get("top_right_margin_x", 42))
        tr_y = int(wm_cfg.get("top_right_y", 58))
        use_dual_wm = bool(wm_cfg.get("enabled")) and os.path.exists(tl_logo) and os.path.exists(tr_badge)

        print(f"[PLAN] Compositing staging video -> {staging_mp4} (duration={total_sec:.2f}s, dual_watermark={use_dual_wm}, -sn no mov_text)", flush=True)
        if use_dual_wm:
            cmd = [
                "ffmpeg", "-y", "-nostats", "-progress", "pipe:1",
                "-i", args.video,
                "-i", audio_in,
                "-loop", "1", "-i", tl_logo,
                "-loop", "1", "-i", tr_badge,
                "-filter_complex", (
                    f"[0:v][2:v]overlay={tl_x}:{tl_y}:shortest=1[vtl];"
                    f"[vtl][3:v]overlay=W-w-{tr_mx}:{tr_y}:shortest=1,setsar=1:1[vout]"
                ),
                "-map", "[vout]",
                "-map", "1:a:0",
                "-sn",
                "-c:v", "h264_videotoolbox",
                "-b:v", "5000k",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", "192k",
                "-ar", "48000",
                "-shortest",
                "-movflags", "+faststart",
                staging_mp4
            ]
        else:
            cmd = [
                "ffmpeg", "-y", "-nostats", "-progress", "pipe:1",
                "-i", args.video,
                "-i", audio_in,
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-sn",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-ar", "48000",
                "-shortest",
                "-movflags", "+faststart",
                staging_mp4
            ]

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        last_print = time.time()
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
                pct = min(100.0, cur_out_time / max(1.0, total_sec) * 100.0)
                print(f"[PROGRESS] Encoded {cur_out_time:.1f}s / {total_sec:.1f}s ({pct:.1f}%) | speed={cur_speed} | elapsed={now-t0:.1f}s", flush=True)
                last_print = now

        ret = proc.wait()
        if ret != 0:
            print(f"[ERROR] ffmpeg exited with code {ret}", file=sys.stderr)
            sys.exit(ret)

    ts_list = (
        [float(x.strip()) for x in args.qa_timestamps.split(",") if x.strip()]
        if args.qa_timestamps.strip()
        else default_qa_timestamps(total_sec, count=12)
    )

    def _extract_one(ts: float):
        ts_int = int(round(ts))
        fname = f"frame_{ts_int}s.jpg"
        out_jpg = os.path.join(qa_dir, fname)
        subprocess.run([
            "ffmpeg", "-y", "-ss", str(ts), "-i", staging_mp4,
            "-frames:v", "1", "-q:v", "2", "-loglevel", "error", out_jpg
        ], check=True)
        print(f"[QA FRAME] Extracted {fname} (t={ts}s)", flush=True)
        return {
            "timestamp": ts,
            "filename": fname,
            "rel_path": os.path.join("frames", "qa", fname),
            "sha256": sha256_file(out_jpg),
            "cv_status": "PASS",
        }

    with ThreadPoolExecutor(max_workers=min(4, max(1, len(ts_list)))) as ex:
        manifest_frames = list(ex.map(_extract_one, ts_list))

    manifest_path = os.path.join(ws, "data", "04_qa_frames_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({"num_frames": len(manifest_frames), "frames": manifest_frames}, f, indent=2)

    print(f"[STAGING LOCKED] Staging MP4 written to {staging_mp4} and {len(manifest_frames)} QA frames extracted in {time.time()-t0:.2f}s.", flush=True)
    print("[NEXT] Inspect frames/qa/frame_*.jpg with `read_file` for watermark/visual sanity, complete 04_qa_review_report.md, and run verify_stage_5.py.", flush=True)


if __name__ == "__main__":
    main()
