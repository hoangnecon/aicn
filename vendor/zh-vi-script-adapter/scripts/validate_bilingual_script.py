#!/usr/bin/env python3
"""
Stage 2 CI/CD Gate Validator (validate_bilingual_script.py, v3.0.0 — voice_dub only).
Requires Stage 1 gate PASS (.cicd/stage_1_gate.json).
Validates 02_bilingual_script.json across 6 deterministic checks (100 points total):
  1. Schema & strict non-overlapping monotonic timing (0 <= start[i] < end[i] <= start[i+1] - 0.02, dur >= 0.15s) (15 pts)
  2. Full timeline & acoustic waveform coverage against 01_asr_raw_segments.json (15 pts)
  3. Zero foreign CJK/Korean characters & zero ',,' syntax typos in sub_text/tts_text (20 pts)
  4. Alphanumeric <en>...</en> tag compliance in tts_text (15 pts)
  5. Syllable density (mean <= 5.6, max <= 6.8 syl/s), line length (<= 96 chars), and calibrated
     VieNeu TTS duration budget across BOTH single lines AND v3 utterance groups (15 pts)
  6. Live sea-g2p IPA phonemization compilation on tts_text entries (20 pts)
Writes workspace/<slug>/.cicd/stage_2_gate.json only on 100/100 PASS.
"""
import argparse
import json
import os
import re
import sys
import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
ORCH_SCRIPTS = os.path.abspath(os.path.join(_HERE, "..", "..", "video-localization-orchestrator", "scripts"))
AUDIT_SCRIPTS = os.path.abspath(os.path.join(_HERE, "..", "..", "video-asr-visual-audit", "scripts"))
for _p in (
    ORCH_SCRIPTS,
    AUDIT_SCRIPTS,
    os.path.abspath(os.path.join(_HERE, "..", "..", "..", "scripts")),
    os.path.abspath(os.path.join(_HERE, "..", "..", "aicn", "scripts")),
):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
from cicd_gate import require_gate, write_gate_token
try:
    import aicn_platform as ap  # noqa: F401
except ImportError:
    ap = None


CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")
ALPHANUM_MIXED_RE = re.compile(r"\b(?:[A-Za-z]+\d+|\d+[A-Za-z]+)\b")


def strip_en_tags(text: str) -> str:
    return re.sub(r"<en>.*?</en>", "", text or "")


def count_tts_syllables(tts_text: str) -> int:
    """Count total spoken syllables (Vietnamese words + English tokens inside <en>...</en>)."""
    en_spans = re.findall(r"<en>(.*?)</en>", tts_text or "")
    en_words = sum(len(s.split()) for s in en_spans)
    vi_clean = re.sub(r"[^\w\sÀ-ỹ]", " ", strip_en_tags(tts_text or ""))
    vi_syls = len([w for w in vi_clean.split() if w])
    return vi_syls + en_words


def estimate_vieneu_tts_seconds(tts_text: str) -> float:
    """
    Calibrated VieNeu v3 Turbo p90 duration predictor (fitted on 753 real .cache/tts_sent WAVs):
      est = 0.185 * vi_syls + 0.22 + 0.32 * en_words + 0.08 * soft_pauses + 0.14 * hard_pauses.
    Catches short-utterance overflows (e.g. 1-2 syllable lines) before Stage 3 TTS/mix runs.
    """
    txt = (tts_text or "").strip()
    en_spans = re.findall(r"<en>(.*?)</en>", txt)
    en_words = sum(len(s.split()) for s in en_spans)
    vi_clean = re.sub(r"[^\w\sÀ-ỹ]", " ", strip_en_tags(txt))
    vi_syls = len([w for w in vi_clean.split() if w])
    inner = txt[:-1] if len(txt) > 1 else ""
    soft_pauses = len(re.findall(r"[,;:]", inner))
    hard_pauses = len(re.findall(r"[.!?…]", inner))
    return (0.185 * vi_syls) + 0.22 + (en_words * 0.32) + (soft_pauses * 0.08) + (hard_pauses * 0.14)


def group_script_utterances_v3(
    segments: list,
    max_gap_s: float = 0.12,
    max_clauses: int = 3,
    max_span_s: float = 4.5,
) -> list[list[int]]:
    """Mirror Stage 3 v3.0.0 indivisible utterance grouping: gap <= 0.12s, <= 3 clauses, span <= 4.5s."""
    groups, cur = [], []
    for i, s in enumerate(segments):
        if cur:
            p = segments[cur[-1]]
            chained = not re.search(r"[.!?…]\s*$", (p.get("tts_text") or "").strip())
            span_ok = (float(s.get("end", 0.0)) - float(segments[cur[0]].get("start", 0.0))) <= max_span_s
            same_speaker = s.get("speaker") == p.get("speaker")
            same_voice = s.get("voice") == p.get("voice")
            gap_ok = (float(s.get("start", 0.0)) - float(p.get("end", 0.0))) <= max_gap_s
            if chained and len(cur) < max_clauses and span_ok and same_speaker and same_voice and gap_ok:
                cur.append(i)
                continue
            groups.append(cur)
        cur = [i]
    if cur:
        groups.append(cur)
    return groups


def check_tts_duration_budgets(segments: list, video_dur: float = 0.0) -> list[str]:
    """
    Validate every v3 utterance group (both single-line utterances and continuous <=0.12s comma chains)
    against Stage 3's exact elastic window:
      avail = min(next_group_start - 0.05, group_end + 1.20) - group_start
    at cap 1.26x (1.30x for <=2-syllable interjections), without falsely charging inter-utterance
    startup overhead or 50ms gaps to interior clauses of a valid continuous group.
    """
    errors = []
    if not segments:
        return errors

    groups = group_script_utterances_v3(segments)
    for gi, g in enumerate(groups):
        g_st = float(segments[g[0]]["start"])
        g_et = float(segments[g[-1]]["end"])
        nxt_g_st = (
            float(segments[groups[gi + 1][0]]["start"])
            if gi + 1 < len(groups)
            else (video_dur if video_dur > g_st else g_et + 1.20)
        )
        avail = max(0.15, min(nxt_g_st - 0.05, g_et + 1.20) - g_st)
        joined_tts = " ".join((segments[i].get("tts_text") or "").strip() for i in g).strip()
        if not joined_tts:
            continue
        est_s = estimate_vieneu_tts_seconds(joined_tts)
        syls = count_tts_syllables(joined_tts)
        cap = 1.30 if syls <= 2 else 1.26
        ratio = est_s / avail
        if ratio > cap:
            trim_need = max(1, int(np.ceil((est_s - cap * avail) / 0.185)))
            label = (
                f"Seg #{segments[g[0]].get('id', g[0])}"
                if len(g) == 1
                else f"Group ids={[segments[i].get('id', i) for i in g]}"
            )
            errors.append(
                f"{label}: predicted TTS overflow ({est_s:.2f}s / {avail:.2f}s = {ratio:.2f}x > {cap:.2f}x, trim ~{trim_need} syl): {joined_tts}"
            )

    return errors


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
    video_dur = 0.0
    if os.path.exists(profile_path):
        with open(profile_path, "r", encoding="utf-8") as f:
            _prof = json.load(f)
            loc_mode = _prof.get("localization_mode", "voice_dub")
            video_dur = float(_prof.get("duration", 0.0))
    if loc_mode != "voice_dub":
        sys.exit(f"[CI/CD GATE BLOCKED] AICN v3.0.0 requires localization_mode='voice_dub' (found '{loc_mode}').")

    with open(script_path, "r", encoding="utf-8") as f:
        segments = json.load(f)

    asr_path = os.path.join(ws, "data", "01_asr_raw_segments.json")
    with open(asr_path, "r", encoding="utf-8") as f:
        raw_asr = json.load(f)

    max_syl_cap = 6.8
    mean_syl_cap = 5.6

    schema_errors = []
    foreign_errors = []
    alphanum_errors = []
    budget_errors = []
    syl_rates = []
    char_lengths = []
    prev_end = -1.0

    for idx, seg in enumerate(segments):
        sid = seg.get("id", idx)
        st = float(seg.get("start", -1.0))
        et = float(seg.get("end", -1.0))
        dur = et - st
        sub = (seg.get("sub_text") or "").strip()
        tts = (seg.get("tts_text") or "").strip()

        if not sub or not tts or st < 0.0 or et <= st or dur < 0.15:
            schema_errors.append(f"Seg #{sid}: invalid span ({st}->{et}, dur={dur:.3f}s < 0.15s) or empty text")
        if idx > 0 and st < (prev_end + 0.02 - 1e-6):
            schema_errors.append(
                f"Seg #{sid}: overlap/gap violation start={st:.2f}s < prev_end+0.02={prev_end+0.02:.2f}s"
            )
        prev_end = et

        if CJK_RE.search(sub) or CJK_RE.search(tts) or ",," in sub or ",," in tts:
            foreign_errors.append(f"Seg #{sid}: CJK/Korean char or ',,' typo in text: sub='{sub}' | tts='{tts}'")

        tts_no_en = strip_en_tags(tts)
        if ALPHANUM_MIXED_RE.search(tts_no_en):
            alphanum_errors.append(f"Seg #{sid}: Unwrapped mixed alphanumeric token outside <en>: {tts}")

        words = len(sub.split())
        syl_rate = words / max(0.15, dur)
        syl_rates.append(syl_rate)
        char_lengths.append(len(sub))

        if len(sub) > 96:
            budget_errors.append(f"Seg #{sid}: sub_text length={len(sub)} chars (>96 max): {sub}")
        if syl_rate > max_syl_cap:
            budget_errors.append(f"Seg #{sid}: syllable rate={syl_rate:.2f} syl/s (>{max_syl_cap} max): {sub}")

    budget_errors.extend(check_tts_duration_budgets(segments, video_dur=video_dur))

    first_asr_start = float(raw_asr[0]["start"]) if raw_asr else 0.0
    last_asr_end = float(raw_asr[-1]["end"]) if raw_asr else 0.0
    first_seg_start = float(segments[0]["start"]) if segments else 999.0
    last_seg_end = float(segments[-1]["end"]) if segments else 0.0

    from analyze_voice_waveform import analyze_waveform_and_clauses
    wf_report = analyze_waveform_and_clauses(ws, script_path)
    ac_cov_ratio = wf_report["acoustic_speech_coverage_ratio"]
    bloated_cnt = wf_report["bloated_segments_count"]
    premature_cnt = wf_report.get("premature_lead_segments_count", 0)

    coverage_ok = (
        (abs(first_seg_start - first_asr_start) <= 1.5)
        and (abs(last_seg_end - last_asr_end) <= 1.5)
        and (len(segments) >= len(raw_asr) * 0.55)
        and (ac_cov_ratio >= 0.90)
        and (bloated_cnt == 0)
        and (premature_cnt == 0)
    )

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
            "description": "Strict non-overlapping monotonic timing (0 <= start[i] < end[i] <= start[i+1] - 0.02, dur >= 0.15s) and non-empty dual-layer text",
            "weight": 15,
            "passed": len(schema_errors) == 0 and len(segments) > 0,
            "detail": f"segments={len(segments)}, errors={schema_errors[:3]}",
        },
        {
            "id": "timeline_and_clause_granularity",
            "description": "Covers 100% raw ASR span, >=55% raw clause count, >=90% acoustic bursts, 0 bloated/premature",
            "weight": 15,
            "passed": coverage_ok,
            "detail": (
                f"span=[{first_seg_start:.2f}s..{last_seg_end:.2f}s], count={len(segments)}/{len(raw_asr)}, "
                f"acoustic_cov={ac_cov_ratio*100:.1f}%, bloated={bloated_cnt}, premature_lead={premature_cnt}"
            ),
        },
        {
            "id": "zero_foreign_char_leakage",
            "description": "Zero CJK/Japanese/Korean characters or double-comma syntax errors in sub_text and tts_text",
            "weight": 20,
            "passed": len(foreign_errors) == 0,
            "detail": f"foreign_errors={len(foreign_errors)} {foreign_errors[:3]}",
        },
        {
            "id": "alphanumeric_en_tag_compliance",
            "description": "All mixed alphanumeric tokens (e.g. 3FS, R1) in tts_text are wrapped inside <en>...</en>",
            "weight": 15,
            "passed": len(alphanum_errors) == 0,
            "detail": f"alphanum_errors={len(alphanum_errors)} {alphanum_errors[:3]}",
        },
        {
            "id": "syllable_and_tts_duration_budget",
            "description": f"sub_text <= 96 chars, mean_syl <= {mean_syl_cap}, max_syl <= {max_syl_cap}, and calibrated VieNeu duration fits single + grouped slack windows",
            "weight": 15,
            "passed": len(budget_errors) == 0 and mean_syl <= mean_syl_cap,
            "detail": f"mean_syl={mean_syl:.2f} syl/s, max_syl={max_syl:.2f} syl/s, max_chars={max_chars}, budget_errors={len(budget_errors)} {budget_errors[:5]}",
        },
        {
            "id": "live_seag2p_ipa_compilation",
            "description": "100% of tts_text segments compile cleanly to IPA via vieneu_utils.phonemize_text",
            "weight": 20,
            "passed": len(ipa_errors) == 0,
            "detail": f"compiled={len(segments) - len(ipa_errors)}/{len(segments)}, errors={ipa_errors[:3]}",
        },
    ]

    metrics = {
        "localization_mode": "voice_dub",
        "num_segments": len(segments),
        "raw_asr_clauses": len(raw_asr),
        "mean_syllable_rate": round(mean_syl, 3),
        "max_syllable_rate": round(max_syl, 3),
        "max_sub_chars": max_chars,
        "schema_errors_count": len(schema_errors),
        "budget_errors_count": len(budget_errors),
    }

    write_gate_token(
        workspace=ws,
        stage=2,
        stage_name="Bilingual Dual-Layer Script, Duration Budget & IPA Validation",
        checks=checks,
        metrics=metrics,
        artifact_paths=[
            "02_glossary_and_pronunciation.md",
            "data/02_bilingual_script.json",
        ],
    )


if __name__ == "__main__":
    main()
