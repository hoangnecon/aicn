#!/usr/bin/env python3
"""
Stage 2 CI/CD Gate Validator (validate_bilingual_script.py).
Requires Stage 1 gate PASS (.cicd/stage_1_gate.json).
Supports both 'voice_dub' and 'sub_only' localization modes configured in 00_video_profile.json.
Validates 02_bilingual_script.json across 6 deterministic checks (100 points total):
  1. Schema & strictly monotonic timestamps (15 pts)
  2. Full timeline & raw_id coverage against 01_asr_raw_segments.json (15 pts)
  3. Zero foreign CJK/Korean characters & zero syntax typos in sub_text/tts_text (20 pts)
  4. Alphanumeric <en>...</en> tag compliance in voice_dub mode (or clean orthography in sub_only mode) (15 pts)
  5. Syllable density (<= 6.8 syl/s in voice_dub, <= 7.8 syl/s reading speed in sub_only) & line length (<= 96 chars) (15 pts)
  6. Live sea-g2p IPA phonemization compilation on tts_text entries (20 pts)
Writes workspace/<slug>/.cicd/stage_2_gate.json only on 100/100 PASS.
"""
import argparse
import json
import os
import re
import sys
import numpy as np

ORCH_SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "video-localization-orchestrator", "scripts"))
sys.path.insert(0, ORCH_SCRIPTS)
from cicd_gate import require_gate, write_gate_token


CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")
ALPHANUM_MIXED_RE = re.compile(r"\b(?:[A-Za-z]+\d+|\d+[A-Za-z]+)\b")


def strip_en_tags(text: str) -> str:
    return re.sub(r"<en>.*?</en>", "", text)


def main():
    parser = argparse.ArgumentParser(description="Validate 02_bilingual_script.json and sign Stage 2 CI/CD gate.")
    parser.add_argument("--workspace", help="Workspace path (workspace/<slug>).")
    parser.add_argument("--script", help="Path to 02_bilingual_script.json (optional if --workspace is given).")
    parser.add_argument("--glossary-doc", required=False, help="Optional path to 02_glossary_and_pronunciation.md.")
    args = parser.parse_args()

    if args.workspace:
        ws = os.path.abspath(args.workspace)
        script_path = args.script or os.path.join(ws, "data", "02_bilingual_script.json")
    elif args.script:
        script_path = os.path.abspath(args.script)
        ws = os.path.abspath(os.path.join(os.path.dirname(script_path), ".."))
    else:
        print("[ERROR] Must provide --workspace or --script", file=sys.stderr)
        sys.exit(1)

    require_gate(ws, 1)

    profile_path = os.path.join(ws, "data", "00_video_profile.json")
    loc_mode = "voice_dub"
    if os.path.exists(profile_path):
        with open(profile_path, "r", encoding="utf-8") as f:
            loc_mode = json.load(f).get("localization_mode", "voice_dub")

    with open(script_path, "r", encoding="utf-8") as f:
        segments = json.load(f)

    asr_path = os.path.join(ws, "data", "01_asr_raw_segments.json")
    with open(asr_path, "r", encoding="utf-8") as f:
        raw_asr = json.load(f)

    max_syl_cap = 7.8 if loc_mode == "sub_only" else 6.8
    mean_syl_cap = 6.2 if loc_mode == "sub_only" else 5.6

    schema_errors = []
    foreign_errors = []
    alphanum_errors = []
    budget_errors = []
    syl_rates = []
    char_lengths = []
    prev_start = -1.0
    modified_tts_mirror = False

    for idx, seg in enumerate(segments):
        sid = seg.get("id", idx)
        st = float(seg.get("start", -1))
        et = float(seg.get("end", -1))
        dur = max(0.1, et - st)
        sub = seg.get("sub_text", "").strip()
        tts = seg.get("tts_text", "").strip()
        if loc_mode == "sub_only" and not tts and sub:
            tts = sub
            seg["tts_text"] = sub
            modified_tts_mirror = True

        if not sub or not tts or st < 0 or et <= st or st < prev_start:
            schema_errors.append(f"Seg #{sid}: invalid start/end ({st}->{et}) or empty text")
        prev_start = st

        if CJK_RE.search(sub) or CJK_RE.search(tts) or ",," in sub or ",," in tts:
            foreign_errors.append(f"Seg #{sid}: CJK/Korean char or ',,' typo in text: sub='{sub}' | tts='{tts}'")

        if loc_mode == "voice_dub":
            tts_no_en = strip_en_tags(tts)
            if ALPHANUM_MIXED_RE.search(tts_no_en):
                alphanum_errors.append(f"Seg #{sid}: Unwrapped mixed alphanumeric token outside <en>: {tts}")

        words = len(sub.split())
        syl_rate = words / dur
        syl_rates.append(syl_rate)
        char_lengths.append(len(sub))

        if len(sub) > 96:
            budget_errors.append(f"Seg #{sid}: sub_text length={len(sub)} chars (>96 max): {sub}")
        if syl_rate > max_syl_cap:
            budget_errors.append(f"Seg #{sid}: syllable rate={syl_rate:.2f} syl/s (>{max_syl_cap} max): {sub}")

    if modified_tts_mirror:
        with open(script_path, "w", encoding="utf-8") as f:
            json.dump(segments, f, ensure_ascii=False, indent=2)

    # Check 2: Timeline & Fine-Grained Visual Sub-Clause + Acoustic Waveform Coverage
    first_asr_start = float(raw_asr[0]["start"]) if raw_asr else 0.0
    last_asr_end = float(raw_asr[-1]["end"]) if raw_asr else 0.0
    first_seg_start = float(segments[0]["start"]) if segments else 999.0
    last_seg_end = float(segments[-1]["end"]) if segments else 0.0

    AUDIT_SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "video-asr-visual-audit", "scripts"))
    if AUDIT_SCRIPTS not in sys.path:
        sys.path.insert(0, AUDIT_SCRIPTS)
    from analyze_voice_waveform import analyze_waveform_and_clauses
    wf_report = analyze_waveform_and_clauses(ws, script_path)
    sc_cov_ratio = wf_report["sub_clause_coverage_ratio"]
    bloated_cnt = wf_report["bloated_segments_count"]
    premature_cnt = wf_report.get("premature_lead_segments_count", 0)
    internal_gap_cnt = wf_report.get("internal_blank_gap_segments_count", 0)

    coverage_ok = (
        (abs(first_seg_start - first_asr_start) <= 1.5)
        and (abs(last_seg_end - last_asr_end) <= 1.5)
        and (len(segments) >= len(raw_asr) * 0.55)
        and (sc_cov_ratio >= 0.96)
        and (bloated_cnt == 0)
        and (premature_cnt == 0)
        and (internal_gap_cnt == 0)
    )

    # Check 6: Live sea-g2p IPA compilation
    ipa_errors = []
    try:
        from vieneu_utils.phonemize_text import phonemize_text_with_emotions
        for idx, seg in enumerate(segments):
            ipa = phonemize_text_with_emotions(seg["tts_text"])
            if not ipa or len(ipa.strip()) == 0:
                ipa_errors.append(f"Seg #{seg.get('id', idx)}: empty IPA output")
    except Exception as exc:
        ipa_errors.append(f"sea-g2p import/runtime failure: {exc}")

    mean_syl = float(np.mean(syl_rates)) if syl_rates else 0.0
    max_syl = float(np.max(syl_rates)) if syl_rates else 0.0
    max_chars = int(np.max(char_lengths)) if char_lengths else 0

    checks = [
        {
            "id": "schema_and_monotonic_timing",
            "description": "All segments have valid schema, non-empty sub_text/tts_text, and monotonic timestamps",
            "weight": 15,
            "passed": len(schema_errors) == 0 and len(segments) > 0,
            "detail": f"segments={len(segments)}, mode={loc_mode}, errors={schema_errors[:3]}"
        },
        {
            "id": "timeline_and_clause_granularity",
            "description": "Script covers >=96% of fine-grained visual sub-clauses with 0 bloated, 0 premature lead-in, and 0 internal blank gap segments",
            "weight": 15,
            "passed": coverage_ok,
            "detail": (
                f"span=[{first_seg_start:.2f}s..{last_seg_end:.2f}s], sub_clauses={wf_report['covered_visual_sub_clauses']}/{wf_report['total_visual_sub_clauses']} "
                f"({sc_cov_ratio*100:.1f}%), bloated={bloated_cnt}, premature_lead={premature_cnt}, internal_blank_gaps={internal_gap_cnt}"
            )
        },
        {
            "id": "zero_foreign_char_leakage",
            "description": "Zero CJK/Japanese/Korean characters or double-comma syntax errors in sub_text and tts_text",
            "weight": 20,
            "passed": len(foreign_errors) == 0,
            "detail": f"foreign_errors={len(foreign_errors)} {foreign_errors[:3]}"
        },
        {
            "id": "alphanumeric_en_tag_compliance",
            "description": "All mixed alphanumeric tokens (e.g. 3FS, R1) in tts_text are properly wrapped inside <en>...</en> (voice_dub)",
            "weight": 15,
            "passed": len(alphanum_errors) == 0,
            "detail": f"mode={loc_mode}, alphanum_errors={len(alphanum_errors)} {alphanum_errors[:3]}"
        },
        {
            "id": "syllable_and_line_length_budget",
            "description": f"All segments satisfy sub_text <= 96 chars, mean syllable rate <= {mean_syl_cap} syl/s, max <= {max_syl_cap} syl/s",
            "weight": 15,
            "passed": len(budget_errors) == 0 and mean_syl <= mean_syl_cap,
            "detail": f"mean_syl={mean_syl:.2f} syl/s, max_syl={max_syl:.2f} syl/s, max_chars={max_chars}, budget_errors={len(budget_errors)}"
        },
        {
            "id": "live_seag2p_ipa_compilation",
            "description": "100% of tts_text segments compile cleanly to IPA via vieneu_utils.phonemize_text",
            "weight": 20,
            "passed": len(ipa_errors) == 0,
            "detail": f"compiled={len(segments) - len(ipa_errors)}/{len(segments)}, errors={ipa_errors[:3]}"
        }
    ]

    metrics = {
        "localization_mode": loc_mode,
        "num_segments": len(segments),
        "raw_asr_clauses": len(raw_asr),
        "mean_syllable_rate": round(mean_syl, 3),
        "max_syllable_rate": round(max_syl, 3),
        "max_sub_chars": max_chars,
    }

    write_gate_token(
        workspace=ws,
        stage=2,
        stage_name="Bilingual Dual-Layer Script & IPA Validation",
        checks=checks,
        metrics=metrics,
        artifact_paths=[
            "02_glossary_and_pronunciation.md",
            "data/02_bilingual_script.json",
        ],
    )


if __name__ == "__main__":
    main()
