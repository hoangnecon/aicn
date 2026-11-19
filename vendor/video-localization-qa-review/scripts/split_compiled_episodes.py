#!/usr/bin/env python3
"""
Stage 5E (Multi-Episode Compilation Splitter):
For `series_compilation` projects with >= 2 episodes in `data/00_episodes_manifest.json`,
automatically splits the verified compiled master deliverables (`16:9` MP4, `9:16` MP4, and subtitles)
into individual per-episode deliverables in `deliverables/episodes/`:
  - `deliverables/episodes/<ep_id>_<slug>_vi_16x9.mp4`
  - `deliverables/episodes/<ep_id>_<slug>_vi_9x16.mp4` (if 9:16 master exists)
  - `deliverables/episodes/<ep_id>_<slug>_vi.srt` (with timestamps re-zeroed to 00:00:00.000 per episode)
"""
import argparse
import json
import os
import subprocess
import sys
import time

ORCH_SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "video-localization-orchestrator", "scripts"))
sys.path.insert(0, ORCH_SCRIPTS)
from cicd_gate import require_gate


def fmt_srt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms >= 1000:
        secs += 1
        ms -= 1000
    return f"{hrs:02d}:{mins:02d}:{secs:02d},{ms:03d}"


def slice_mp4_segment(src_mp4: str, dst_mp4: str, start_s: float, duration_s: float):
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-ss", f"{start_s:.3f}",
        "-i", src_mp4,
        "-t", f"{duration_s:.3f}",
        "-c:v", "h264_videotoolbox", "-b:v", "5200k",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-sn", "-movflags", "+faststart",
        dst_mp4,
    ]
    subprocess.run(cmd, check=True)


def export_episode_srt(cues: list, ep_start: float, ep_end: float, dst_srt: str) -> int:
    ep_dur = ep_end - ep_start
    blocks = []
    idx = 1
    for c in cues:
        st = float(c.get("slot_start", c.get("start", 0.0)))
        et = float(c.get("slot_end", c.get("end", 0.0)))
        txt = c.get("sub_text", "").strip()
        if not txt:
            continue
        # Check if cue belongs to this episode
        mid = 0.5 * (st + et)
        if ep_start - 0.05 <= mid <= ep_end + 0.05:
            rel_st = max(0.0, st - ep_start)
            rel_et = min(ep_dur, et - ep_start)
            if rel_et - rel_st >= 0.15:
                blocks.append(f"{idx}\n{fmt_srt_time(rel_st)} --> {fmt_srt_time(rel_et)}\n{txt}\n")
                idx += 1
    with open(dst_srt, "w", encoding="utf-8") as f:
        f.write("\n".join(blocks))
    return idx - 1


def main():
    parser = argparse.ArgumentParser(description="Split compiled multi-episode localized masters into per-episode MP4 & SRT deliverables.")
    parser.add_argument("--workspace", required=True, help="Workspace path (workspace/<slug>).")
    args = parser.parse_args()

    ws = os.path.abspath(args.workspace)
    require_gate(ws, 5)

    ep_manifest_path = os.path.join(ws, "data", "00_episodes_manifest.json")
    if not os.path.exists(ep_manifest_path):
        print(f"[ERROR] Missing {ep_manifest_path}. Run init_workspace.py first.", file=sys.stderr)
        sys.exit(1)

    with open(ep_manifest_path, "r", encoding="utf-8") as f:
        ep_manifest = json.load(f)

    slug = ep_manifest.get("slug", os.path.basename(ws))
    episodes = ep_manifest.get("episodes", [])
    if not episodes:
        print("[INFO] No episodes in manifest.", flush=True)
        return

    aligned_path = os.path.join(ws, "data", "03_aligned_subtitles.json")
    script_path = os.path.join(ws, "data", "02_bilingual_script.json")
    cues_src = aligned_path if os.path.exists(aligned_path) else script_path
    with open(cues_src, "r", encoding="utf-8") as f:
        cues = json.load(f)

    master_16x9 = os.path.join(ws, "media", f"final_{slug}_vi.mp4")
    master_9x16 = os.path.join(ws, "media", f"final_{slug}_vi_9x16.mp4")
    out_dir = os.path.join(ws, "deliverables", "episodes")
    os.makedirs(out_dir, exist_ok=True)

    t0 = time.time()
    print(f"[PLAN] Splitting {len(episodes)} episodes from compiled master -> {out_dir}", flush=True)
    exported_records = []

    for ep in episodes:
        ep_id = ep["ep_id"]
        st_s = float(ep["timeline_start_s"])
        et_s = float(ep["timeline_end_s"])
        dur_s = round(et_s - st_s, 3)

        ep_srt = os.path.join(out_dir, f"{ep_id}_{slug}_vi.srt")
        sub_count = export_episode_srt(cues, st_s, et_s, ep_srt)

        rec = {
            "ep_id": ep_id,
            "timeline_start_s": st_s,
            "timeline_end_s": et_s,
            "duration_s": dur_s,
            "subtitle_cues_count": sub_count,
            "srt_path": os.path.relpath(ep_srt, ws),
        }

        if os.path.exists(master_16x9):
            ep_16x9 = os.path.join(out_dir, f"{ep_id}_{slug}_vi_16x9.mp4")
            print(f"[SPLIT 16:9] {ep_id} ({st_s:.2f}s..{et_s:.2f}s, {sub_count} subs) -> {ep_16x9}", flush=True)
            slice_mp4_segment(master_16x9, ep_16x9, st_s, dur_s)
            rec["mp4_16x9_path"] = os.path.relpath(ep_16x9, ws)

        if os.path.exists(master_9x16):
            ep_9x16 = os.path.join(out_dir, f"{ep_id}_{slug}_vi_9x16.mp4")
            print(f"[SPLIT 9:16] {ep_id} ({st_s:.2f}s..{et_s:.2f}s, {sub_count} subs) -> {ep_9x16}", flush=True)
            slice_mp4_segment(master_9x16, ep_9x16, st_s, dur_s)
            rec["mp4_9x16_path"] = os.path.relpath(ep_9x16, ws)

        exported_records.append(rec)

    export_manifest_path = os.path.join(out_dir, "episodes_export_manifest.json")
    with open(export_manifest_path, "w", encoding="utf-8") as f:
        json.dump({
            "slug": slug,
            "exported_episodes": len(exported_records),
            "episodes": exported_records,
        }, f, ensure_ascii=False, indent=2)

    print(f"[SUMMARY] Split {len(exported_records)} episodes into {out_dir} in {time.time()-t0:.1f}s.", flush=True)


if __name__ == "__main__":
    main()
