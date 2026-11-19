#!/usr/bin/env python3
"""
Stage 1 CI/CD Gate Validator (verify_stage_1.py).
Verifies:
  1. Audio stem readiness (Demucs no_vocals.wav in voice_dub mode, or source audio in sub_only mode) (15 pts)
  2. Fine-grained Whisper ASR clauses (median duration <= 2.8s, 0 unresolved [GAP_VISUAL_SUB] placeholders) (25 pts)
  3. 5fps hardsub bounding-box scan coverage >= 97% of video duration (15 pts)
  4. Hardsub-to-ASR gap coverage >= 95% (ensuring phone calls, whispers, PA broadcasts & screams are captured) (20 pts)
  5. Visual keyframe audit completion (>= 10 frames extracted, SHA-256 verified, and logged in 01_visual_audit_notes.md) (25 pts)
Writes workspace/<slug>/.cicd/stage_1_gate.json only on 100/100 PASS.
"""
import argparse
import json
import os
import sys
import numpy as np

ORCH_SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "video-localization-orchestrator", "scripts"))
sys.path.insert(0, ORCH_SCRIPTS)
from cicd_gate import require_gate, write_gate_token, sha256_file


def main():
    parser = argparse.ArgumentParser(description="Verify Stage 1 (ASR, Hardsub BBox Scan, Phone/Whisper Gap Coverage & Visual Audit) CI/CD Gate.")
    parser.add_argument("--workspace", required=True, help="Workspace path (workspace/<slug>).")
    args = parser.parse_args()

    ws = os.path.abspath(args.workspace)
    require_gate(ws, 0)

    with open(os.path.join(ws, "data", "00_video_profile.json"), "r", encoding="utf-8") as f:
        profile = json.load(f)
    vid_dur = float(profile.get("duration", 0.0))
    loc_mode = profile.get("localization_mode", "voice_dub")

    checks = []
    metrics = {"localization_mode": loc_mode}

    # 1. Check audio stem readiness (aicn_audio.py media/stems/accomp.wav or legacy media/demucs/no_vocals.wav)
    stems_acc = os.path.join(ws, "media", "stems", "accomp.wav")
    legacy_acc = os.path.join(ws, "media", "demucs", "no_vocals.wav")
    no_vocals = stems_acc if os.path.exists(stems_acc) else legacy_acc
    if loc_mode == "sub_only":
        src_vid = profile.get("source_video", "")
        has_bgm = os.path.exists(src_vid) or (os.path.exists(no_vocals) and os.path.getsize(no_vocals) > 100_000)
        bgm_detail = f"sub_only mode: native source audio ready ({src_vid})"
    else:
        has_bgm = os.path.exists(no_vocals) and os.path.getsize(no_vocals) > 100_000
        bgm_detail = f"voice_dub mode: path={no_vocals}, size={os.path.getsize(no_vocals) if os.path.exists(no_vocals) else 0} bytes"

    checks.append({
        "id": "audio_stem_or_native_source_ready",
        "description": "Separated BGM stem (voice_dub) or native stereo source audio (sub_only) is ready",
        "weight": 15,
        "passed": has_bgm,
        "detail": bgm_detail
    })

    # 2. Check fine-grained ASR segments & zero unresolved [GAP_VISUAL_SUB] placeholders
    asr_path = os.path.join(ws, "data", "01_asr_raw_segments.json")
    asr_ok = False
    asr_detail = "Missing data/01_asr_raw_segments.json"
    asr_segs = []
    unresolved_gaps = []
    if os.path.exists(asr_path):
        with open(asr_path, "r", encoding="utf-8") as f:
            asr_segs = json.load(f)
        if len(asr_segs) >= 10:
            durs = [float(s["end"]) - float(s["start"]) for s in asr_segs]
            med_dur = float(np.median(durs))
            max_dur = float(np.max(durs))
            for s in asr_segs:
                txt = s.get("text", "").strip()
                if (not txt) or ("[GAP_VISUAL_SUB" in txt):
                    unresolved_gaps.append(f"Seg #{s.get('id')} ({s.get('start')}s-{s.get('end')}s)")
            metrics["asr_segment_count"] = len(asr_segs)
            metrics["asr_median_duration_s"] = round(med_dur, 3)
            metrics["unresolved_gap_placeholders"] = len(unresolved_gaps)
            asr_ok = (med_dur <= 2.8) and (len(unresolved_gaps) == 0)
            asr_detail = (
                f"count={len(asr_segs)}, median_dur={med_dur:.2f}s (<=2.8s), "
                f"unresolved_gaps={len(unresolved_gaps)} {unresolved_gaps[:3]}"
            )
    checks.append({
        "id": "asr_fine_grained_clauses_and_no_placeholders",
        "description": "ASR segments are fine-grained clauses (median <= 2.8s) with 0 unresolved [GAP_VISUAL_SUB] placeholders",
        "weight": 25,
        "passed": asr_ok,
        "detail": asr_detail
    })

    # 3. Check 5fps hardsub bounding-box scan & 4. Check Hardsub-to-ASR Gap Coverage (Phone/Whisper/PA completeness)
    bbox_path = os.path.join(ws, "data", "01_zh_bbox_scan.json")
    bbox_ok = False
    bbox_detail = "Missing data/01_zh_bbox_scan.json"
    gap_cov_ok = False
    gap_cov_detail = "Missing data/01_zh_bbox_scan.json"

    if os.path.exists(bbox_path):
        with open(bbox_path, "r", encoding="utf-8") as f:
            bdata = json.load(f)
        spans = bdata.get("spans", [])
        fps_scan = int(bdata.get("fps_scan", 5))
        scanned_dur = len(spans) / max(1, fps_scan)
        non_empty = sum(1 for sp in spans if sp is not None)
        metrics["bbox_scanned_frames"] = len(spans)
        metrics["bbox_active_frames"] = non_empty
        bbox_ok = scanned_dur >= vid_dur * 0.97 and len(spans) > 50
        bbox_detail = f"scanned={scanned_dur:.1f}s/{vid_dur:.1f}s ({len(spans)} frames @ {fps_scan}fps), active_sub_frames={non_empty}"

        # Hardsub-to-ASR gap coverage & Acoustic Waveform Audit check
        from analyze_voice_waveform import analyze_waveform_and_clauses
        wf_report = analyze_waveform_and_clauses(ws, asr_path)
        sc_total = wf_report["total_visual_sub_clauses"]
        sc_cov = wf_report["covered_visual_sub_clauses"]
        cov_ratio = wf_report["sub_clause_coverage_ratio"]
        bloated_cnt = wf_report["bloated_segments_count"]
        premature_cnt = wf_report.get("premature_lead_segments_count", 0)
        internal_gap_cnt = wf_report.get("internal_blank_gap_segments_count", 0)
        ac_bursts = wf_report["acoustic_voice_bursts_count"]
        ac_ratio = wf_report["acoustic_speech_coverage_ratio"]
        missed_ivs = [f"{u['start']:.1f}s-{u['end']:.1f}s" for u in wf_report.get("uncovered_sub_clauses", [])]

        metrics["hardsub_intervals_total"] = sc_total
        metrics["hardsub_intervals_covered"] = sc_cov
        metrics["hardsub_coverage_ratio"] = cov_ratio
        metrics["bloated_segments_count"] = bloated_cnt
        metrics["premature_lead_segments_count"] = premature_cnt
        metrics["internal_blank_gap_segments_count"] = internal_gap_cnt
        metrics["acoustic_voice_bursts_count"] = ac_bursts
        metrics["acoustic_speech_coverage_ratio"] = ac_ratio

        gap_cov_ok = (
            (cov_ratio >= 0.96)
            and (bloated_cnt == 0)
            and (premature_cnt == 0)
            and (internal_gap_cnt == 0)
            and (len(unresolved_gaps) == 0)
        )
        gap_cov_detail = (
            f"sub_clauses={sc_cov}/{sc_total} ({cov_ratio*100:.1f}%, need >=96.0%), "
            f"bloated={bloated_cnt} (need 0), premature_lead={premature_cnt} (need 0), "
            f"internal_blank_gaps={internal_gap_cnt} (need 0), uncovered={missed_ivs[:5]}"
        )

    checks.append({
        "id": "hardsub_bbox_scan_complete",
        "description": "5fps bottom-strip pixel scan covers >= 97% of source video duration",
        "weight": 15,
        "passed": bbox_ok,
        "detail": bbox_detail
    })

    checks.append({
        "id": "hardsub_gap_coverage_phone_whisper_complete",
        "description": ">= 95% of visual hardsub intervals (including telephone calls, radio comms, PA & whispers) are covered by ASR/OCR segments",
        "weight": 20,
        "passed": gap_cov_ok,
        "detail": gap_cov_detail
    })

    # 5. Check visual audit frames manifest & 01_visual_audit_notes.md cross-check
    manifest_path = os.path.join(ws, "data", "01_extracted_audit_manifest.json")
    notes_path = os.path.join(ws, "01_visual_audit_notes.md")
    audit_ok = False
    audit_detail = "Missing data/01_extracted_audit_manifest.json or 01_visual_audit_notes.md"
    if os.path.exists(manifest_path) and os.path.exists(notes_path):
        with open(manifest_path, "r", encoding="utf-8") as f:
            mdata = json.load(f)
        with open(notes_path, "r", encoding="utf-8") as f:
            notes_txt = f.read()

        frames_list = mdata.get("frames", [])
        missing_on_disk = []
        unlogged_in_md = []
        for item in frames_list:
            f_abs = os.path.join(ws, item["rel_path"])
            if not os.path.exists(f_abs) or sha256_file(f_abs) != item.get("sha256"):
                missing_on_disk.append(item["filename"])
            ts_str = f"{int(round(float(item['timestamp'])))}s"
            ts_float_str = f"{float(item['timestamp']):.1f}s"
            if (item["filename"] not in notes_txt) and (ts_str not in notes_txt) and (ts_float_str not in notes_txt):
                unlogged_in_md.append(item["filename"])

        metrics["audit_frames_extracted"] = len(frames_list)
        metrics["audit_frames_unlogged"] = len(unlogged_in_md)
        audit_ok = len(frames_list) >= 10 and len(missing_on_disk) == 0 and len(unlogged_in_md) == 0
        if audit_ok:
            audit_detail = f"All {len(frames_list)} extracted keyframes verified on disk and documented in 01_visual_audit_notes.md"
        else:
            audit_detail = f"extracted={len(frames_list)} (need >=10), missing_files={missing_on_disk}, unlogged_in_md={unlogged_in_md}"

    checks.append({
        "id": "visual_audit_frames_inspected_and_logged",
        "description": ">=10 audit keyframes extracted, SHA-256 verified, and documented in 01_visual_audit_notes.md",
        "weight": 25,
        "passed": audit_ok,
        "detail": audit_detail
    })

    write_gate_token(
        workspace=ws,
        stage=1,
        stage_name="Multi-Pass ASR, Phone/Whisper Gap Recovery & Visual Keyframe Audit",
        checks=checks,
        metrics=metrics,
        artifact_paths=[
            "01_visual_audit_notes.md",
            "data/01_asr_raw_segments.json",
            "data/01_zh_bbox_scan.json",
            "data/01_extracted_audit_manifest.json",
        ],
    )


if __name__ == "__main__":
    main()
