#!/usr/bin/env python3
"""
Stage 1 CI/CD Gate Validator (verify_stage_1.py, v3.0.0 — voice_dub only).
Verifies 4 hard deterministic gates (100 points total):
  1. stems_ready (20 pts): media/stems/{vocals,accomp}.wav valid and matching source duration.
  2. asr_monotonic_and_clean (25 pts): fine-grained clauses (median <= 2.8s), 0 overlaps/inversions
     (0 <= start[i] < end[i] <= start[i+1] - 0.02), 0 <0.15s runts, 0 boundary duplicate fragments,
     and 0 empty/placeholder texts.
  3. acoustic_waveform_voice_coverage (30 pts): >= 90% acoustic burst coverage, 0 bloated,
     0 premature lead-in, longest_uncovered_vocal_gap_s <= 6.0s.
  4. visual_audit_frames_inspected_and_logged (25 pts): >= 10 frames intact (SHA-256 verified)
     and documented in 01_visual_audit_notes.md.
Writes workspace/<slug>/.cicd/stage_1_gate.json only on 100/100 PASS.
"""
import argparse
import json
import os
import sys
import numpy as np
import soundfile as sf

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from cicd_gate import require_gate, write_gate_token, sha256_file
try:
    import aicn_platform as ap  # noqa: F401
except ImportError:
    ap = None


def main():
    parser = argparse.ArgumentParser(description="Verify Stage 1 (Demucs Stems, Multi-Band ASR, Monotonic Acoustic Alignment & Visual Audit) CI/CD Gate.")
    parser.add_argument("--workspace", required=True, help="Workspace path (workspace/<slug>).")
    args = parser.parse_args()

    ws = os.path.abspath(args.workspace)
    require_gate(ws, 0)

    with open(os.path.join(ws, "data", "00_video_profile.json"), "r", encoding="utf-8") as f:
        profile = json.load(f)
    vid_dur = float(profile.get("duration", 0.0))
    loc_mode = profile.get("localization_mode", "voice_dub")
    if loc_mode != "voice_dub":
        sys.exit(f"[CI/CD GATE BLOCKED] AICN v3.0.0 requires localization_mode='voice_dub' (found '{loc_mode}').")

    checks = []
    metrics = {"localization_mode": "voice_dub"}

    # 1. Stems readiness (media/stems/{vocals,accomp}.wav valid & matching duration)
    voc_p = os.path.join(ws, "media", "stems", "vocals.wav")
    acc_p = os.path.join(ws, "media", "stems", "accomp.wav")
    min_stem_bytes = 100_000 if vid_dur > 5.0 else 4_096
    stems_ok = False
    stems_detail = f"missing media/stems/{{vocals,accomp}}.wav"
    if os.path.exists(voc_p) and os.path.exists(acc_p):
        v_sz, a_sz = os.path.getsize(voc_p), os.path.getsize(acc_p)
        dur_match = True
        dur_note = ""
        try:
            v_info = sf.info(voc_p)
            a_info = sf.info(acc_p)
            if vid_dur > 0.5:
                dur_match = (abs(v_info.duration - vid_dur) <= 1.5) and (abs(a_info.duration - vid_dur) <= 1.5)
            dur_note = f", v_dur={v_info.duration:.2f}s, a_dur={a_info.duration:.2f}s (src={vid_dur:.2f}s)"
        except Exception as exc:
            dur_match = False
            dur_note = f", wav_err={exc}"
        stems_ok = (v_sz > min_stem_bytes) and (a_sz > min_stem_bytes) and dur_match
        stems_detail = f"vocals={v_sz}B, accomp={a_sz}B{dur_note}"

    checks.append({
        "id": "stems_ready",
        "description": "media/stems/{vocals,accomp}.wav exist, non-empty, and match source duration",
        "weight": 20,
        "passed": stems_ok,
        "detail": stems_detail,
    })

    # 2. ASR monotonic, clean & deduplicated
    from analyze_voice_waveform import (
        analyze_waveform_and_clauses,
        find_boundary_duplicate_pairs,
        find_timestamp_violations,
    )

    asr_path = os.path.join(ws, "data", "01_asr_raw_segments.json")
    asr_ok = False
    asr_detail = "Missing data/01_asr_raw_segments.json"
    asr_segs = []
    ts_violations = []
    dup_pairs = []
    bad_texts = []
    min_segs_required = 10 if vid_dur >= 30.0 else 1

    if os.path.exists(asr_path):
        with open(asr_path, "r", encoding="utf-8") as f:
            asr_segs = json.load(f)
        if len(asr_segs) >= min_segs_required:
            durs = [float(s["end"]) - float(s["start"]) for s in asr_segs]
            med_dur = float(np.median(durs))
            for s in asr_segs:
                txt = (s.get("text") or "").strip()
                if (not txt) or ("[GAP_" in txt):
                    bad_texts.append(f"Seg #{s.get('id')} ({s.get('start')}s-{s.get('end')}s)")
            ts_violations = find_timestamp_violations(asr_segs, min_dur_s=0.15, min_gap_s=0.02)
            dup_pairs = find_boundary_duplicate_pairs(asr_segs, gap_thresh_s=0.35)

            metrics["asr_segment_count"] = len(asr_segs)
            metrics["asr_median_duration_s"] = round(med_dur, 3)
            metrics["timestamp_violations_count"] = len(ts_violations)
            metrics["boundary_duplicates_count"] = len(dup_pairs)
            metrics["unresolved_gap_placeholders"] = len(bad_texts)

            asr_ok = (
                (med_dur <= 2.8)
                and (len(bad_texts) == 0)
                and (len(ts_violations) == 0)
                and (len(dup_pairs) == 0)
            )
            asr_detail = (
                f"count={len(asr_segs)}, median_dur={med_dur:.2f}s (<=2.8s), "
                f"ts_violations={len(ts_violations)} {ts_violations[:2]}, "
                f"boundary_dups={len(dup_pairs)} {dup_pairs[:2]}, "
                f"bad_texts={len(bad_texts)} {bad_texts[:2]}"
            )
        else:
            asr_detail = f"Only {len(asr_segs)} segments (need >= {min_segs_required})"

    checks.append({
        "id": "asr_monotonic_and_clean",
        "description": "ASR clauses have median <= 2.8s, 0 overlaps/inversions/runts, 0 boundary duplicates, and 0 empty/placeholder texts",
        "weight": 25,
        "passed": asr_ok,
        "detail": asr_detail,
    })

    # 3. Acoustic Waveform Voice Coverage & Gap Bound
    ac_ok = False
    ac_detail = "Missing data/01_asr_raw_segments.json"
    if os.path.exists(asr_path):
        wf_report = analyze_waveform_and_clauses(ws, asr_path)
        bloated_cnt = wf_report["bloated_segments_count"]
        premature_cnt = wf_report.get("premature_lead_segments_count", 0)
        ac_bursts = wf_report["acoustic_voice_bursts_count"]
        ac_ratio = wf_report["acoustic_speech_coverage_ratio"]
        uncov_ac = wf_report.get("uncovered_acoustic_bursts_count", 0)
        max_gap_s = float(wf_report.get("longest_uncovered_vocal_gap_s", 0.0))
        p2b_sum = wf_report.get("multiband_pass2b_summary", {})

        metrics["bloated_segments_count"] = bloated_cnt
        metrics["premature_lead_segments_count"] = premature_cnt
        metrics["acoustic_voice_bursts_count"] = ac_bursts
        metrics["acoustic_speech_coverage_ratio"] = ac_ratio
        metrics["uncovered_acoustic_bursts_count"] = uncov_ac
        metrics["longest_uncovered_vocal_gap_s"] = max_gap_s
        metrics["multiband_pass2b_summary"] = p2b_sum

        ac_ok = (
            (ac_ratio >= 0.90)
            and (bloated_cnt == 0)
            and (premature_cnt == 0)
            and (max_gap_s <= 6.0)
            and (len(bad_texts) == 0)
        )
        ac_detail = (
            f"acoustic_cov={ac_ratio*100:.1f}% (>=90.0%), bursts={ac_bursts}, uncovered={uncov_ac}, "
            f"max_unprobed_vocal_gap={max_gap_s:.1f}s (<=6.0s), pass2b_rec={p2b_sum.get('recovered_total', 0)} "
            f"({p2b_sum.get('recovered_by_band', {})}), "
            f"bloated={bloated_cnt}, premature_lead={premature_cnt}"
        )

    checks.append({
        "id": "acoustic_waveform_voice_coverage",
        "description": ">= 90% acoustic burst coverage, longest unprobed vocal gap <= 6.0s, 0 bloated and 0 premature lead-in segments",
        "weight": 30,
        "passed": ac_ok,
        "detail": ac_detail,
    })

    # 4. Visual audit frames manifest & 01_visual_audit_notes.md cross-check
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
            audit_detail = f"extracted={len(frames_list)} (>=10), missing_files={missing_on_disk}, unlogged_in_md={unlogged_in_md}"

    checks.append({
        "id": "visual_audit_frames_inspected_and_logged",
        "description": ">=10 audit keyframes extracted, SHA-256 verified, and documented in 01_visual_audit_notes.md",
        "weight": 25,
        "passed": audit_ok,
        "detail": audit_detail,
    })

    write_gate_token(
        workspace=ws,
        stage=1,
        stage_name="Multi-Band Whisper ASR, Monotonic Acoustic Alignment & Visual Keyframe Audit",
        checks=checks,
        metrics=metrics,
        artifact_paths=[
            "01_visual_audit_notes.md",
            "data/01_asr_raw_segments.json",
            "data/01_extracted_audit_manifest.json",
        ],
    )


if __name__ == "__main__":
    main()
