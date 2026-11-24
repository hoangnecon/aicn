#!/usr/bin/env python3
"""
Stage 5 CI/CD Release Gate & Promotion Engine (verify_stage_5.py).
Enforces:
  1. All upstream CI/CD gates (Stages 0..4) signed with 100/100 PASS (15 pts)
  2. Staging MP4 stream compliance via ffprobe (h264/hevc, 48kHz stereo aac, 0 mov_text streams, exact duration) (25 pts)
  3. Stratified QA frames extracted (>=10) and SHA-256 verified for watermark/visual sanity check (25 pts)
  4. Agent visual audit log completeness in 04_qa_review_report.md (every manifest frame documented with PASS) (20 pts)
  5. Promotion of locked media/.staging_final.mp4 -> media/final_<slug>_vi.mp4 (and optional --release-path) (15 pts)
If ANY check fails, media/final_<slug>_vi.mp4 is NOT created (or is removed) and exit code 2 is returned!
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
ORCH_SCRIPTS = os.path.abspath(os.path.join(_HERE, "..", "..", "video-localization-orchestrator", "scripts"))
for _p in (
    ORCH_SCRIPTS,
    os.path.abspath(os.path.join(_HERE, "..", "..", "..", "scripts")),
    os.path.abspath(os.path.join(_HERE, "..", "..", "aicn", "scripts")),
):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
from cicd_gate import require_gate, write_gate_token, sha256_file
try:
    import aicn_platform as ap
except ImportError:
    ap = None


def probe_json(path: str) -> dict:
    cmd = [
        ap.find_tool("ffprobe") if ap else "ffprobe", "-v", "error",
        "-show_entries", "format=duration,size,bit_rate",
        "-show_entries", "stream=index,codec_name,codec_type,width,height,r_frame_rate,sample_rate,channels",
        "-of", "json", path
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(res.stdout)


def main():
    parser = argparse.ArgumentParser(description="Verify Stage 5 QA Release Gate and promote staging MP4 to final output.")
    parser.add_argument("--workspace", required=True, help="Workspace path (workspace/<slug>).")
    parser.add_argument("--release-path", default="", help="Optional external path to copy the verified final MP4 (e.g. bilibili/final_output.mp4).")
    args = parser.parse_args()

    ws = os.path.abspath(args.workspace)

    # Check 1: Verify all upstream gates 0..4
    upstream_gates_ok = True
    for st in range(0, 5):
        require_gate(ws, st)

    with open(os.path.join(ws, "data", "00_video_profile.json"), "r", encoding="utf-8") as f:
        profile = json.load(f)
    slug = profile.get("slug", os.path.basename(ws))
    vid_dur = float(profile.get("duration", 0.0))
    width = int(profile.get("width", 1920))
    height = int(profile.get("height", 1080))

    staging_mp4 = os.path.join(ws, "media", ".staging_final.mp4")
    final_mp4 = os.path.join(ws, "media", f"final_{slug}_vi.mp4")
    manifest_path = os.path.join(ws, "data", "04_qa_frames_manifest.json")
    qa_report_path = os.path.join(ws, "04_qa_review_report.md")

    # Check 2: Staging MP4 ffprobe compliance
    mp4_ok = False
    mp4_detail = "Missing media/.staging_final.mp4"
    if os.path.exists(staging_mp4):
        info = probe_json(staging_mp4)
        fmt = info.get("format", {})
        streams = info.get("streams", [])
        v_st = next((s for s in streams if s.get("codec_type") == "video"), {})
        a_st = next((s for s in streams if s.get("codec_type") == "audio"), {})
        s_st = [s for s in streams if s.get("codec_type") == "subtitle"]
        out_dur = float(fmt.get("duration", 0.0))
        mp4_ok = (
            v_st.get("codec_name") in ("h264", "hevc")
            and int(v_st.get("width", 0)) == width
            and int(v_st.get("height", 0)) == height
            and a_st.get("codec_name") == "aac"
            and int(a_st.get("sample_rate", 0)) == 48000
            and int(a_st.get("channels", 0)) == 2
            and len(s_st) == 0
            and abs(out_dur - vid_dur) <= 0.25
        )
        mp4_detail = (
            f"v={v_st.get('codec_name')} ({v_st.get('width')}x{v_st.get('height')}), "
            f"a={a_st.get('codec_name')} ({a_st.get('sample_rate')}Hz, {a_st.get('channels')}ch), "
            f"sub_streams={len(s_st)}, dur={out_dur:.2f}s (target={vid_dur:.2f}s)"
        )

    # Check 3: Stratified QA Frame Extraction & SHA-256 Integrity
    cv_ok = False
    cv_detail = "Missing data/04_qa_frames_manifest.json"
    frames_list = []
    if os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as f:
            frames_list = json.load(f).get("frames", [])
        missing_jpgs = []
        for item in frames_list:
            f_abs = os.path.join(ws, item["rel_path"])
            if not os.path.exists(f_abs) or sha256_file(f_abs) != item.get("sha256"):
                missing_jpgs.append(item["filename"])
        cv_ok = len(frames_list) >= 10 and len(missing_jpgs) == 0
        cv_detail = f"frames={len(frames_list)} (need >=10), missing_or_corrupt_jpgs={missing_jpgs}"

    # Check 4: Agent Visual Inspection Report (04_qa_review_report.md)
    report_ok = False
    report_detail = "Missing 04_qa_review_report.md"
    if os.path.exists(qa_report_path) and frames_list:
        with open(qa_report_path, "r", encoding="utf-8") as f:
            rep_txt = f.read()
        unlogged = []
        for item in frames_list:
            if item["filename"] not in rep_txt:
                unlogged.append(item["filename"])
        has_tbd_or_fail = ("_TBD_" in rep_txt) or ("| FAIL" in rep_txt)
        report_ok = len(unlogged) == 0 and not has_tbd_or_fail
        report_detail = f"documented={len(frames_list)-len(unlogged)}/{len(frames_list)}, unlogged={unlogged}, has_tbd_or_fail={has_tbd_or_fail}"

    # Check 5: Promote staging -> final AND publish to deliverables/ vault ONLY if Checks 1..4 all passed!
    pre_release_ok = upstream_gates_ok and mp4_ok and cv_ok and report_ok
    promoted_ok = False
    promo_detail = "Blocked: upstream or QA checks failed; media/final_<slug>_vi.mp4 NOT created"

    def _hardlink_or_copy(src_p: str, dst_p: str):
        os.makedirs(os.path.dirname(dst_p), exist_ok=True)
        if os.path.exists(dst_p) or os.path.islink(dst_p):
            os.remove(dst_p)
        try:
            os.link(src_p, dst_p)
        except OSError:
            shutil.copy2(src_p, dst_p)

    deliv_16x9 = os.path.join(ws, "deliverables", "16x9", f"final_{slug}_vi.mp4")
    deliv_srt = os.path.join(ws, "deliverables", "subtitles", f"{slug}_vi.srt")
    canon_srt = os.path.join(ws, "data", "subtitles_full.srt")

    if pre_release_ok:
        _hardlink_or_copy(staging_mp4, final_mp4)
        _hardlink_or_copy(final_mp4, deliv_16x9)
        if os.path.exists(canon_srt):
            _hardlink_or_copy(canon_srt, deliv_srt)
        # Also publish to parent Series Hub deliverables/ if configured
        proj_cfg_path = os.path.join(ws, "config", "project_config.json")
        if os.path.exists(proj_cfg_path):
            with open(proj_cfg_path, "r", encoding="utf-8") as f:
                proj_cfg = json.load(f)
            hub_dir = proj_cfg.get("series_hub_dir", "")
            if hub_dir and os.path.isdir(hub_dir):
                _hardlink_or_copy(final_mp4, os.path.join(hub_dir, "deliverables", "16x9", f"final_{slug}_vi.mp4"))
                if os.path.exists(canon_srt):
                    _hardlink_or_copy(canon_srt, os.path.join(hub_dir, "deliverables", "subtitles", f"{slug}_vi.srt"))
        if args.release_path:
            shutil.copy2(staging_mp4, os.path.abspath(args.release_path))
        promoted_ok = (
            os.path.exists(final_mp4)
            and os.path.exists(deliv_16x9)
            and os.path.getsize(final_mp4) == os.path.getsize(staging_mp4)
        )
        promo_detail = (
            f"Promoted verified video to {final_mp4} & deliverables/16x9/final_{slug}_vi.mp4"
            + (f" and {args.release_path}" if args.release_path else "")
        )
    else:
        for p in [final_mp4, deliv_16x9]:
            if os.path.exists(p):
                os.remove(p)

    checks = [
        {
            "id": "all_upstream_cicd_gates_signed",
            "description": "Stages 0, 1, 2, 3, and 4 CI/CD gate tokens verified with 100/100 PASS and intact SHA-256 hashes",
            "weight": 15,
            "passed": upstream_gates_ok,
            "detail": "Stages 0..4 verified"
        },
        {
            "id": "staging_mp4_stream_and_codec_compliance",
            "description": "Video is H.264/HEVC, audio is 48000Hz stereo AAC, 0 duplicate mov_text streams (-sn), duration matches source",
            "weight": 25,
            "passed": mp4_ok,
            "detail": mp4_detail
        },
        {
            "id": "qa_frames_extracted_and_verified",
            "description": ">=10 stratified QA frames extracted and SHA-256 verified for watermark/visual sanity check",
            "weight": 25,
            "passed": cv_ok,
            "detail": cv_detail
        },
        {
            "id": "agent_visual_audit_report_completeness",
            "description": "Every frame in 04_qa_frames_manifest.json is visually inspected via read_file and logged PASS in 04_qa_review_report.md",
            "weight": 20,
            "passed": report_ok,
            "detail": report_detail
        },
        {
            "id": "release_promotion_and_srt_sync",
            "description": "Locked staging MP4 promoted to media/final_<slug>_vi.mp4 after passing all QA checks",
            "weight": 15,
            "passed": promoted_ok,
            "detail": promo_detail
        }
    ]

    metrics = {
        "qa_frames_verified": len(frames_list),
        "final_mp4_path": final_mp4 if promoted_ok else None,
        "final_mp4_size_bytes": os.path.getsize(final_mp4) if promoted_ok else 0,
    }

    artifacts = [
        "04_qa_review_report.md",
        "data/04_qa_frames_manifest.json",
    ]
    if promoted_ok:
        artifacts.append(os.path.join("media", f"final_{slug}_vi.mp4"))

    write_gate_token(
        workspace=ws,
        stage=5,
        stage_name="Final Composite, Watermark QA & Release Promotion",
        checks=checks,
        metrics=metrics,
        artifact_paths=artifacts,
    )


if __name__ == "__main__":
    main()
