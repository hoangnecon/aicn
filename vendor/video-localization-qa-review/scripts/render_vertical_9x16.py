#!/usr/bin/env python3
"""
Stage 5D (Optional Vertical 9:16 Export): Render an ADDITIONAL 9:16 vertical short-form master MP4
(`media/final_<slug>_vi_9x16.mp4`) alongside the verified 16:9 master (`media/final_<slug>_vi.mp4`)
WITHOUT overwriting or replacing the 16:9 output.

Architecture:
  - Requires Stage 5 CI/CD Gate PASS (.cicd/stage_5_gate.json) so the 16:9 master is already verified.
  - Takes the clean source video (`0:v`, cropped to `y=0..880` for the blurred ambient backdrop so zero
    subtitle glow ever leaks into the backdrop) and the verified 16:9 master (`1:v`, center-cut `1440x1080`
    scaled uniformly `0.75x` to `1080x810` at `y=520..1330` with sleek `2px` specular top/bottom borders),
    preserving the exact Frosted-Glass subtitle capsule inside the video frame (`x=[240..1680]`) and
    leaving the bottom `590px` (`y=1330..1920`) completely clear for TikTok/Reels/Shorts UI overlays.
  - Extracts stratified 9:16 QA keyframes (`frames/qa_9x16/frame_9x16_*s.jpg`) and verifies the output stream via `ffprobe`.
"""
import argparse
import json
import os
import subprocess
import sys
import time


_HERE = os.path.dirname(os.path.abspath(__file__))
ORCH_SCRIPTS = os.path.abspath(os.path.join(_HERE, "..", "..", "video-localization-orchestrator", "scripts"))
for _p in (
    ORCH_SCRIPTS,
    os.path.abspath(os.path.join(_HERE, "..", "..", "..", "scripts")),
    os.path.abspath(os.path.join(_HERE, "..", "..", "aicn", "scripts")),
):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
from cicd_gate import require_gate
try:
    import aicn_platform as ap
except ImportError:
    ap = None


def probe_json(path: str) -> dict:
    cmd = [
        ap.find_tool("ffprobe") if ap else "ffprobe", "-v", "error",
        "-show_entries", "format=duration,size,bit_rate",
        "-show_entries", "stream=index,codec_name,codec_type,width,height,sample_aspect_ratio,display_aspect_ratio,r_frame_rate,sample_rate,channels",
        "-of", "json", path
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(res.stdout)


def main():
    parser = argparse.ArgumentParser(description="Render an additional 9:16 vertical version alongside the 16:9 master MP4.")
    parser.add_argument("--workspace", required=True, help="Workspace path (workspace/<slug>).")
    parser.add_argument("--qa-timestamps", default="17.4,24.0,30.3,82.5,238.5,383.6,574.2,707.7,764.9,998.6", help="Timestamps for 9:16 QA frame extraction.")
    args = parser.parse_args()

    ws = os.path.abspath(args.workspace)
    # Require Stage 5 gate PASS so the 16:9 master is guaranteed verified and untouched
    require_gate(ws, 5)

    with open(os.path.join(ws, "data", "00_video_profile.json"), "r", encoding="utf-8") as f:
        profile = json.load(f)

    slug = profile.get("slug", os.path.basename(ws))
    src_video = profile.get("source_video", "")
    total_sec = float(profile.get("duration", 1009.15))

    master_16x9 = os.path.join(ws, "media", f"final_{slug}_vi.mp4")
    out_9x16 = os.path.join(ws, "media", f"final_{slug}_vi_9x16.mp4")
    qa_9x16_dir = os.path.join(ws, "frames", "qa_9x16")
    os.makedirs(qa_9x16_dir, exist_ok=True)

    if not os.path.exists(master_16x9):
        print(f"[ERROR] Verified 16:9 master not found: {master_16x9}", file=sys.stderr)
        sys.exit(1)

    t0 = time.time()
    print(
        f"[PLAN] Rendering additional 9:16 vertical master (1080x1920) -> {out_9x16} "
        f"(preserving 16:9 master at {master_16x9})",
        flush=True,
    )

    # Filter graph:
    # [0:v] clean source cropped above hardsub band with true 9:16 aspect ratio (495x880 = 9:16 from center) -> scaled to 1080x1920 -> gblur -> darkened ambient canvas with setsar=1:1
    # [1:v] verified 16:9 master center-cropped 1440x1080 (x=240..1680) -> scaled 1080x810 -> overlaid at y=520 with 2px specular borders and setsar=1:1
    filter_complex = (
        "[0:v]crop=495:880:712:0,scale=270:480,gblur=sigma=12,scale=1080:1920:flags=bicubic,setsar=1:1,"
        "eq=brightness=-0.16:saturation=0.85,"
        "drawbox=x=0:y=518:w=1080:h=814:color=white@0.22:t=fill[bg];"
        "[1:v]crop=1440:1080:240:0,scale=1080:810:flags=lanczos,setsar=1:1[fg];"
        "[bg][fg]overlay=0:520:shortest=1,setsar=1:1[vout]"
    )

    ff_bin = ap.find_tool("ffmpeg") if ap else "ffmpeg"
    vcodec_args = ap.vendor_h264_encode_args("5200k") if ap else ["-c:v", "h264_videotoolbox", "-b:v", "5200k", "-pix_fmt", "yuv420p"]
    cmd = [
        ff_bin, "-y", "-nostats", "-progress", "pipe:1",
        "-i", src_video,
        "-i", master_16x9,
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-map", "1:a:0",
        "-sn",
        "-aspect", "9:16",
        *vcodec_args,
        "-c:a", "copy",
        "-movflags", "+faststart",
        out_9x16,
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
            print(
                f"[PROGRESS 9:16] Encoded {cur_out_time:.1f}s / {total_sec:.1f}s ({pct:.1f}%) | "
                f"speed={cur_speed} | elapsed={now-t0:.1f}s",
                flush=True,
            )
            last_print = now

    ret = proc.wait()
    if ret != 0:
        print(f"[ERROR] ffmpeg 9:16 encode exited with code {ret}", file=sys.stderr)
        sys.exit(ret)

    # Extract QA keyframes from the 9:16 MP4
    ts_list = [float(x.strip()) for x in args.qa_timestamps.split(",") if x.strip()]
    for ts in ts_list:
        ts_int = int(round(ts))
        out_jpg = os.path.join(qa_9x16_dir, f"frame_9x16_{ts_int}s.jpg")
        subprocess.run([
            ff_bin, "-y", "-ss", str(ts), "-i", out_9x16,
            "-frames:v", "1", "-q:v", "2", "-loglevel", "error", out_jpg
        ], check=True)

    info = probe_json(out_9x16)
    v_st = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    a_st = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})
    dur_9x16 = float(info.get("format", {}).get("duration", 0.0))
    sar = v_st.get("sample_aspect_ratio", "1:1")
    dar = v_st.get("display_aspect_ratio", "9:16")
    if sar != "1:1" or dar != "9:16":
        print(f"[ERROR] Invalid 9:16 SAR/DAR metadata: SAR={sar}, DAR={dar} (expected SAR=1:1, DAR=9:16)", file=sys.stderr)
        sys.exit(2)

    # Publish to deliverables/9x16/ vault (and Series Hub if configured)
    deliv_9x16 = os.path.join(ws, "deliverables", "9x16", f"final_{slug}_vi_9x16.mp4")
    os.makedirs(os.path.dirname(deliv_9x16), exist_ok=True)
    if os.path.exists(deliv_9x16) or os.path.islink(deliv_9x16):
        os.remove(deliv_9x16)
    try:
        os.link(out_9x16, deliv_9x16)
    except OSError:
        import shutil
        shutil.copy2(out_9x16, deliv_9x16)

    proj_cfg_path = os.path.join(ws, "config", "project_config.json")
    if os.path.exists(proj_cfg_path):
        with open(proj_cfg_path, "r", encoding="utf-8") as f:
            proj_cfg = json.load(f)
        hub_dir = proj_cfg.get("series_hub_dir", "")
        if hub_dir and os.path.isdir(hub_dir):
            hub_9x16 = os.path.join(hub_dir, "deliverables", "9x16", f"final_{slug}_vi_9x16.mp4")
            os.makedirs(os.path.dirname(hub_9x16), exist_ok=True)
            if os.path.exists(hub_9x16) or os.path.islink(hub_9x16):
                os.remove(hub_9x16)
            try:
                os.link(out_9x16, hub_9x16)
            except OSError:
                import shutil
                shutil.copy2(out_9x16, hub_9x16)

    print(
        f"[DONE 9:16] Saved {out_9x16} & {deliv_9x16} ({v_st.get('width')}x{v_st.get('height')}, {a_st.get('codec_name')} "
        f"{a_st.get('sample_rate')}Hz, dur={dur_9x16:.2f}s) in {time.time()-t0:.2f}s "
        f"and extracted {len(ts_list)} QA frames to {qa_9x16_dir}",
        flush=True,
    )


if __name__ == "__main__":
    main()
