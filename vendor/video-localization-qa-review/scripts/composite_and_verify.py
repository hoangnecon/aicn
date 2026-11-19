#!/usr/bin/env python3
"""
Stage 5A: Composite source video, ProRes 4444 alpha subtitle overlay, and 48kHz stereo master audio
into a LOCKED staging file (media/.staging_final.mp4) with -sn (no mov_text stream).
Then extract 12+ stratified QA frames into frames/qa/, run Automated Computer-Vision Pill & Perimeter
Inspection on every frame, and write data/04_qa_frames_manifest.json.
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
import numpy as np
from PIL import Image

ORCH_SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "video-localization-orchestrator", "scripts"))
sys.path.insert(0, ORCH_SCRIPTS)
from cicd_gate import require_gate, sha256_file


def probe_json(path: str) -> dict:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration,size,bit_rate",
        "-show_entries", "stream=index,codec_name,codec_type,width,height,r_frame_rate,sample_rate,channels",
        "-of", "json", path
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(res.stdout)


def find_active_cue(cues: list, ts: float):
    for c in cues:
        if float(c["slot_start"]) - 0.05 <= ts <= float(c["slot_end"]) + 0.05:
            return c
    return None


def cv_inspect_frame(jpg_path: str, cue: dict, strip_y0: int) -> dict:
    arr = np.array(Image.open(jpg_path).convert("RGB"), dtype=np.uint8)
    if cue is None:
        return {
            "has_active_cue": False,
            "dark_pill_ratio": 0.0,
            "white_glyph_pixels": 0,
            "cv_status": "PASS",
        }
    bx0, btop, bx1, bbot = cue["box"]
    style = cue.get("style", "frosted_glass")
    y0 = max(0, strip_y0 + btop + 4)
    y1 = min(arr.shape[0], strip_y0 + bbot - 4)
    x0 = max(0, bx0 + 10)
    x1 = min(arr.shape[1], bx1 - 10)
    roi = arr[y0:y1, x0:x1, :]
    luma = 0.299 * roi[:, :, 0] + 0.587 * roi[:, :, 1] + 0.114 * roi[:, :, 2]
    # In solid_pill mode, background is near-black (< 42). In frosted_glass mode, background is darkened blurred glass (< 135).
    luma_thresh = 138.0 if style == "frosted_glass" else 42.0
    dark_mask = luma < luma_thresh
    white_mask = (roi[:, :, 0] > 205) & (roi[:, :, 1] > 205) & (roi[:, :, 2] > 205)
    dark_ratio = float(np.mean(dark_mask)) if roi.size > 0 else 0.0
    white_px = int(np.sum(white_mask))
    passed = (dark_ratio >= 0.45) and (white_px >= 45)
    return {
        "has_active_cue": True,
        "cue_id": cue["id"],
        "style": style,
        "dark_pill_ratio": round(dark_ratio, 3),
        "white_glyph_pixels": white_px,
        "cv_status": "PASS" if passed else "FAIL",
    }


def main():
    parser = argparse.ArgumentParser(description="Composite localized video to locked staging file and run CV QA frame scan.")
    parser.add_argument("--video", required=True, help="Source video path.")
    parser.add_argument("--workspace", required=True, help="Workspace path (workspace/<slug>).")
    parser.add_argument("--slug", required=True, help="Project slug.")
    parser.add_argument("--qa-timestamps", default="18,37,76,113,136,196,310,533,600,655,681,683,738,745", help="Comma-separated QA frame timestamps.")
    parser.add_argument("--import-existing-mp4", default="", help="Optional path to an already-rendered MP4 to stage for verification.")
    args = parser.parse_args()

    ws = os.path.abspath(args.workspace)
    require_gate(ws, 4)

    t0 = time.time()
    with open(os.path.join(ws, "data", "00_video_profile.json"), "r", encoding="utf-8") as f:
        profile = json.load(f)
    strip_y0 = int(profile.get("strip_y0", 880))
    total_sec = float(profile.get("duration", 759.21))

    with open(os.path.join(ws, "data", "04_overlay_geometry_log.json"), "r", encoding="utf-8") as f:
        geom_cues = json.load(f).get("cues", [])

    overlay_in = os.path.join(ws, "media", "subtitles_overlay_alpha.mov")
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
                "-i", overlay_in,
                "-i", audio_in,
                "-i", tl_logo,
                "-i", tr_badge,
                "-filter_complex", (
                    f"[0:v][1:v]overlay=0:{strip_y0}:shortest=1[vsub];"
                    f"[vsub][3:v]overlay={tl_x}:{tl_y}[vtl];"
                    f"[vtl][4:v]overlay=W-w-{tr_mx}:{tr_y},setsar=1:1[vout]"
                ),
                "-map", "[vout]",
                "-map", "2:a:0",
                "-sn",
                "-c:v", "h264_videotoolbox",
                "-b:v", "5000k",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", "192k",
                "-ar", "48000",
                "-movflags", "+faststart",
                staging_mp4
            ]
        else:
            cmd = [
                "ffmpeg", "-y", "-nostats", "-progress", "pipe:1",
                "-i", args.video,
                "-i", overlay_in,
                "-i", audio_in,
                "-filter_complex", f"[0:v][1:v]overlay=0:{strip_y0}:shortest=1,setsar=1:1[vout]",
                "-map", "[vout]",
                "-map", "2:a:0",
                "-sn",
                "-c:v", "h264_videotoolbox",
                "-b:v", "5000k",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", "192k",
                "-ar", "48000",
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
                last_log = now
                last_print = now

        ret = proc.wait()
        if ret != 0:
            print(f"[ERROR] ffmpeg exited with code {ret}", file=sys.stderr)
            sys.exit(ret)

    ts_list = [float(x.strip()) for x in args.qa_timestamps.split(",") if x.strip()]

    def _scan_one(ts):
        ts_int = int(round(ts))
        fname = f"frame_{ts_int}s.jpg"
        out_jpg = os.path.join(qa_dir, fname)
        subprocess.run([
            "ffmpeg", "-y", "-ss", str(ts), "-i", staging_mp4,
            "-frames:v", "1", "-q:v", "2", "-loglevel", "error", out_jpg
        ], check=True)
        cue = find_active_cue(geom_cues, ts)
        cv_res = cv_inspect_frame(out_jpg, cue, strip_y0)
        print(f"[CV QA SCAN] {fname} (t={ts}s): cv_status={cv_res['cv_status']}, dark_pill={cv_res['dark_pill_ratio']}, white_px={cv_res['white_glyph_pixels']}", flush=True)
        return {
            "timestamp": ts,
            "filename": fname,
            "rel_path": os.path.join("frames", "qa", fname),
            "sha256": sha256_file(out_jpg),
            **cv_res,
        }

    with ThreadPoolExecutor(max_workers=min(4, max(1, len(ts_list)))) as ex:
        manifest_frames = list(ex.map(_scan_one, ts_list))

    manifest_path = os.path.join(ws, "data", "04_qa_frames_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({"num_frames": len(manifest_frames), "frames": manifest_frames}, f, indent=2)

    print(f"[STAGING LOCKED] Staging MP4 written to {staging_mp4} and {len(manifest_frames)} QA frames scanned in {time.time()-t0:.2f}s.", flush=True)
    print("[NEXT] Inspect frames/qa/frame_*.jpg with `read_file`, complete 04_qa_review_report.md, and run verify_stage_5.py to promote staging -> final MP4.", flush=True)


if __name__ == "__main__":
    main()
