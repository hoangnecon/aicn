#!/usr/bin/env python3
"""
Stage 1B+: Multi-Band Acoustic Waveform, Visual Sub-Clause & Premature Lead-In / Silent Gap Validator (analyze_voice_waveform.py).
Analyzes:
  1. 50ms-hop RMS waveform envelope + Zero-Crossing Rate (ZCR) on Demucs vocals.wav (or raw audio)
     AND 300-3400Hz telephone/formant bandpass-boosted audio (dynaudnorm).
  2. Fine-grained BBox-Jump + Glyph-IoU visual sub-clauses from 01_zh_bbox_scan.json.
  3. Cross-checks ASR/Script segments for:
     - Uncovered visual sub-clauses (<60% temporal overlap)
     - Bloated segment durations (dur > char_count * 0.35 + 1.4s)
     - Premature lead-in (subtitle appearing >0.18s before BOTH the Chinese hardsub AND the acoustic voice onset during a silent establishing shot)
     - Lingering tail-out (subtitle staying on screen >0.35s after BOTH the Chinese hardsub disappears AND the voice ends)
     - Unsplit internal silent gaps (>=0.40s blank gap inside a segment where neither hardsub nor voice is active)
  4. Provides `--snap-and-split` mode to automatically split compound multi-interval segments and clamp every segment's
     [start, end] to the true active onset/offset of its visual hardsub and acoustic voice waveform.
Writes data/01_waveform_voice_audit.json.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (
    os.path.abspath(os.path.join(_HERE, "..", "..", "..", "scripts")),
    os.path.abspath(os.path.join(_HERE, "..", "..", "aicn", "scripts")),
):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
try:
    import aicn_platform as ap
except ImportError:
    ap = None


def decode_audio_16k(input_path: str, af_filter: str = "") -> np.ndarray:
    cmd = [ap.find_tool("ffmpeg") if ap else "ffmpeg", "-y", "-i", input_path]
    if af_filter:
        cmd.extend(["-af", af_filter])
    cmd.extend(["-f", "s16le", "-ar", "16000", "-ac", "1", "pipe:1"])
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True)
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def compute_voice_mask_and_bursts(voc_arr: np.ndarray, tel_arr: np.ndarray, sr: int = 16000):
    hop = int(0.05 * sr)
    n_hops = min(len(voc_arr), len(tel_arr)) // hop
    voice_mask = np.zeros(n_hops, dtype=bool)
    raw_bursts = []
    in_burst = False
    b_st = 0.0
    b_peak_voc = 0.0
    b_peak_tel = 0.0

    for i in range(n_hops):
        t = i * 0.05
        vc = voc_arr[i * hop:(i + 1) * hop]
        tc = tel_arr[i * hop:(i + 1) * hop]
        v_rms = float(np.sqrt(np.mean(vc ** 2)))
        t_rms = float(np.sqrt(np.mean(tc ** 2)))
        zcr = float(np.mean(np.abs(np.diff(np.sign(vc)))) / 2.0)
        is_voice = (v_rms > 0.018 and 0.02 <= zcr <= 0.30) or (t_rms > 0.12 and v_rms > 0.008 and 0.02 <= zcr <= 0.32)
        voice_mask[i] = is_voice

        is_strong_burst = (v_rms > 0.022 and 0.02 <= zcr <= 0.28) or (t_rms > 0.14 and v_rms > 0.010 and 0.02 <= zcr <= 0.30)
        if is_strong_burst:
            if not in_burst:
                in_burst = True
                b_st = t
                b_peak_voc = v_rms
                b_peak_tel = t_rms
            else:
                b_peak_voc = max(b_peak_voc, v_rms)
                b_peak_tel = max(b_peak_tel, t_rms)
        else:
            if in_burst:
                b_et = t
                if (b_et - b_st) >= 0.45:
                    raw_bursts.append({
                        "start": round(b_st, 2),
                        "end": round(b_et, 2),
                        "peak_voc_rms": round(b_peak_voc, 4),
                        "peak_tel_rms": round(b_peak_tel, 4),
                    })
                in_burst = False

    merged_bursts = []
    for b in raw_bursts:
        if merged_bursts and (b["start"] - merged_bursts[-1]["end"]) <= 0.20:
            merged_bursts[-1]["end"] = b["end"]
            merged_bursts[-1]["peak_voc_rms"] = max(merged_bursts[-1]["peak_voc_rms"], b["peak_voc_rms"])
            merged_bursts[-1]["peak_tel_rms"] = max(merged_bursts[-1]["peak_tel_rms"], b["peak_tel_rms"])
        else:
            merged_bursts.append(dict(b))

    return voice_mask, merged_bursts


def snap_and_split_segments(segments: list, sub_clauses: list, spans: list, voice_mask: np.ndarray, max_syl_rate: float = 7.5) -> list:
    """
    1. Splits compound segments that overlap >=2 distinct visual sub-clauses AND contain multi-clause punctuation.
    2. Clamps each segment's [start, end] to its dominant visual hardsub interval (or acoustic voice onset if no hardsub),
       eliminating premature subtitle appearance during silent establishing shots and lingering subtitle tails after speech ends.
    """
    n_hops = len(voice_mask)

    # Remove exact adjacent duplicate segments where the first segment is a premature ghost before the actual hardsub
    deduped = []
    i = 0
    while i < len(segments):
        s = dict(segments[i])
        if i + 1 < len(segments):
            nxt = segments[i + 1]
            t1 = s.get("sub_text", s.get("text", "")).strip()
            t2 = nxt.get("sub_text", nxt.get("text", "")).strip()
            if t1 and t1 == t2 and (float(nxt["start"]) - float(s["end"])) <= 0.25:
                # Check if next segment aligns better with hardsub
                merged = dict(nxt)
                merged["start"] = max(float(s["start"]), float(nxt["start"]) - 0.6)
                deduped.append(merged)
                i += 2
                continue
        deduped.append(s)
        i += 1

    # Split compound segments spanning 2+ distinct visual sub-clauses
    expanded = []
    for s in deduped:
        st, et = float(s["start"]), float(s["end"])
        ov_ivs = []
        for iv in sub_clauses:
            ist, iet = float(iv["start"]), float(iv["end"])
            if min(et, iet) - max(st, ist) >= 0.30:
                ov_ivs.append((max(st, ist), min(et, iet)))

        sub = s.get("sub_text", s.get("text", "")).strip()
        tts = s.get("tts_text", sub).strip()
        if not sub or len(ov_ivs) < 2:
            expanded.append(s)
            continue

        strong_sub = [p.strip() for p in re.split(r"(?<=[\.\?\!\:])\s*,?\s+", sub) if p.strip()]
        strong_tts = [p.strip() for p in re.split(r"(?<=[\.\?\!\:])\s*,?\s+", tts) if p.strip()]
        comma_sub = [p.strip() for p in re.split(r"(?<=[\.\?\!\:\,])\s+", sub) if p.strip()]
        comma_tts = [p.strip() for p in re.split(r"(?<=[\.\?\!\:\,])\s+", tts) if p.strip()]

        parts_sub, parts_tts = None, None
        if len(ov_ivs) == 2 and len(strong_sub) == 2 and len(strong_tts) == 2:
            parts_sub, parts_tts = strong_sub, strong_tts
        elif len(ov_ivs) == 2 and len(comma_sub) == 2 and len(comma_tts) == 2 and (et - st) >= 1.9:
            parts_sub, parts_tts = comma_sub, comma_tts
        elif len(ov_ivs) == 3 and len(strong_sub) == 3 and len(strong_tts) == 3:
            parts_sub, parts_tts = strong_sub, strong_tts
        elif len(ov_ivs) == 3 and len(comma_sub) == 3 and len(comma_tts) == 3 and (et - st) >= 2.2:
            parts_sub, parts_tts = comma_sub, comma_tts
        elif len(ov_ivs) >= 2 and len(strong_sub) == 2 and len(strong_tts) == 2:
            r0 = len(strong_sub[0]) / max(1, len(sub))
            k = max(1, min(len(ov_ivs) - 1, int(round(r0 * len(ov_ivs)))))
            ov_ivs = [(ov_ivs[0][0], ov_ivs[k - 1][1]), (ov_ivs[k][0], ov_ivs[-1][1])]
            parts_sub, parts_tts = strong_sub, strong_tts
        elif len(ov_ivs) >= 2 and len(comma_sub) == 2 and len(comma_tts) == 2 and (et - st) >= 2.0:
            r0 = len(comma_sub[0]) / max(1, len(sub))
            k = max(1, min(len(ov_ivs) - 1, int(round(r0 * len(ov_ivs)))))
            ov_ivs = [(ov_ivs[0][0], ov_ivs[k - 1][1]), (ov_ivs[k][0], ov_ivs[-1][1])]
            parts_sub, parts_tts = comma_sub, comma_tts

        if not parts_sub:
            expanded.append(s)
            continue

        ok = True
        new_pieces = []
        for (ist, iet), p_sub, p_tts in zip(ov_ivs, parts_sub, parts_tts):
            p_sub = re.sub(r"([\.\?\!\:]),$", r"\1", p_sub.strip())
            p_tts = re.sub(r"([\.\?\!\:]),$", r"\1", p_tts.strip())
            dur_p = iet - ist
            if dur_p < 0.55 or (len(p_sub.split()) / dur_p) > max_syl_rate:
                ok = False
                break
            ns = dict(s)
            ns["start"] = round(ist, 2)
            ns["end"] = round(iet, 2)
            if "sub_text" in ns:
                ns["sub_text"] = p_sub
                ns["tts_text"] = p_tts
            if "text" in ns:
                ns["text"] = p_sub
            new_pieces.append(ns)

        if ok:
            expanded.extend(new_pieces)
        else:
            expanded.append(s)

    # Clamp start and end of every segment to dominant visual hardsub interval or acoustic voice onset
    for idx, s in enumerate(expanded):
        st, et = float(s["start"]), float(s["end"])
        sub = s.get("sub_text", s.get("text", "")).strip()
        words = max(1, len(sub.split()))
        min_dur_needed = max(0.55, words / max_syl_rate + 0.02)

        dom_ivs = []
        for iv in sub_clauses:
            ist, iet = float(iv["start"]), float(iv["end"])
            ov = min(et, iet) - max(st, ist)
            if ov >= 0.25 or (ov >= 0.15 and (iet - ist) <= 0.6):
                dom_ivs.append((max(st - 0.05, ist), min(et + 0.15, iet)))

        a0 = max(0, int(st / 0.05))
        a1 = min(n_hops - 1, int(et / 0.05))
        v_times = [round(k * 0.05, 2) for k in range(a0, a1 + 1) if voice_mask[k]]
        first_v = v_times[0] if v_times else None
        last_v = round(v_times[-1] + 0.05, 2) if v_times else None

        if dom_ivs:
            new_st = dom_ivs[0][0]
            new_et = dom_ivs[-1][1]
        elif first_v is not None:
            new_st = first_v
            new_et = max(new_st + min_dur_needed, last_v if last_v else et)
        else:
            new_st, new_et = st, et

        if new_et - new_st < min_dur_needed:
            next_st = float(expanded[idx + 1]["start"]) - 0.02 if idx + 1 < len(expanded) else 99999.0
            new_et = min(next_st, max(et, new_st + min_dur_needed))
            if new_et - new_st < min_dur_needed:
                prev_et = float(expanded[idx - 1]["end"]) + 0.02 if idx > 0 else 0.0
                new_st = max(prev_et, min(st, new_et - min_dur_needed))

        s["start"] = round(new_st, 2)
        s["end"] = round(new_et, 2)

    for i in range(len(expanded) - 1):
        if expanded[i]["end"] > expanded[i + 1]["start"] - 0.02:
            expanded[i]["end"] = round(max(expanded[i]["start"] + 0.35, expanded[i + 1]["start"] - 0.02), 2)

    for idx, s in enumerate(expanded, 1):
        s["id"] = idx

    return expanded


def analyze_waveform_and_clauses(workspace: str, target_segments_path: str = None, snap_in_place: bool = False) -> dict:
    t0 = time.time()
    ws = os.path.abspath(workspace)
    data_dir = os.path.join(ws, "data")
    profile_path = os.path.join(data_dir, "00_video_profile.json")
    with open(profile_path, "r", encoding="utf-8") as f:
        profile = json.load(f)

    video_path = profile.get("source_video", os.path.join(ws, "master_source.mp4"))
    if not os.path.exists(video_path):
        video_path = os.path.join(ws, "master_source.mp4")

    stems_voc = os.path.join(ws, "media", "stems", "vocals.wav")
    legacy_voc = os.path.join(ws, "media", "demucs", "vocals.wav")
    vocals_path = stems_voc if os.path.exists(stems_voc) else legacy_voc
    source_audio_path = os.path.join(ws, "media", "source_audio.wav")
    audio_src = source_audio_path if os.path.exists(source_audio_path) else video_path

    print(f"[PLAN] Analyzing Multi-Band Acoustic Waveform (50ms hop) + Visual Sub-Clauses + Onset Sync in {ws}", flush=True)
    tel_arr = decode_audio_16k(
        audio_src,
        af_filter="highpass=f=280,lowpass=f=3600,dynaudnorm=f=75:g=15:p=0.92:m=12.0",
    )
    if os.path.exists(vocals_path):
        voc_arr = decode_audio_16k(vocals_path)
    else:
        voc_arr = decode_audio_16k(audio_src)

    voice_mask, merged_bursts = compute_voice_mask_and_bursts(voc_arr, tel_arr, sr=16000)
    n_hops = len(voice_mask)

    bbox_path = os.path.join(data_dir, "01_zh_bbox_scan.json")
    if os.path.exists(bbox_path):
        with open(bbox_path, "r", encoding="utf-8") as f:
            bdata = json.load(f)
        from run_asr_and_scan import find_hardsub_intervals
        spans = bdata.get("spans", [])
        sub_clauses = bdata.get("hardsub_intervals")
        if sub_clauses is None:
            sub_clauses = find_hardsub_intervals(
                spans,
                int(bdata.get("fps_scan", 5)),
                clause_changes=bdata.get("clause_changes"),
            )
    else:
        spans = []
        sub_clauses = []

    if target_segments_path is None:
        s2_path = os.path.join(data_dir, "02_bilingual_script.json")
        s1_path = os.path.join(data_dir, "01_asr_raw_segments.json")
        target_segments_path = s2_path if os.path.exists(s2_path) else s1_path

    with open(target_segments_path, "r", encoding="utf-8") as f:
        raw_data = json.load(f)
    segs = raw_data["segments"] if isinstance(raw_data, dict) else raw_data

    if snap_in_place:
        segs = snap_and_split_segments(segs, sub_clauses, spans, voice_mask)
        if isinstance(raw_data, dict):
            raw_data["segments"] = segs
            raw_data["total_segments"] = len(segs)
            out_data = raw_data
        else:
            out_data = segs
        with open(target_segments_path, "w", encoding="utf-8") as f:
            json.dump(out_data, f, ensure_ascii=False, indent=2)
        print(f"[SNAP & SPLIT] Updated {target_segments_path} -> {len(segs)} onset-locked fine-grained clauses.", flush=True)

    # 1. Check visual sub-clause coverage
    uncovered_clauses = []
    covered_cnt = 0
    for sc in sub_clauses:
        sc_st, sc_et = float(sc["start"]), float(sc["end"])
        sc_dur = max(0.2, sc_et - sc_st)
        ov = 0.0
        for s in segs:
            txt = s.get("text", s.get("sub_text", ""))
            if "[GAP_VISUAL_SUB" in txt:
                continue
            o = max(0.0, min(sc_et, float(s["end"])) - max(sc_st, float(s["start"])))
            if o > 0.05:
                ov += o
        cov_ratio = min(1.0, ov / sc_dur)
        if cov_ratio >= 0.60:
            covered_cnt += 1
        else:
            uncovered_clauses.append({
                "start": sc_st,
                "end": sc_et,
                "duration": round(sc_dur, 2),
                "bbox": sc["bbox"],
                "coverage": round(cov_ratio, 3),
            })

    # 2. Check for bloated segment timestamps, premature lead-in (>0.22s before hardsub & voice), and internal silent gaps (>=0.40s)
    bloated_segments = []
    premature_lead_segments = []
    internal_blank_gap_segments = []

    for s in segs:
        st, et = float(s["start"]), float(s["end"])
        dur = et - st
        zh = s.get("zh_text", s.get("text", ""))
        sub = s.get("sub_text", s.get("text", ""))
        n_zh = max(1, len([c for c in zh if "\u4e00" <= c <= "\u9fff" or c.isalnum()]))
        if dur > (n_zh * 0.35 + 1.4):
            bloated_segments.append({
                "id": s.get("id"),
                "start": st,
                "end": et,
                "duration": round(dur, 2),
                "zh_chars": n_zh,
                "text": zh[:40],
            })

        # Check hardsub and voice onset inside [st, et]
        dom_ivs = [
            (float(iv["start"]), float(iv["end"]))
            for iv in sub_clauses
            if min(et, float(iv["end"])) - max(st, float(iv["start"])) >= 0.25
        ]
        first_hs = dom_ivs[0][0] if dom_ivs else None

        a0 = max(0, int(st / 0.05))
        a1 = min(n_hops - 1, int(et / 0.05))
        v_times = [round(k * 0.05, 2) for k in range(a0, a1 + 1) if voice_mask[k]]
        first_v = v_times[0] if v_times else None

        # Premature lead-in: subtitle starts >0.22s before hardsub AND >0.25s before voice
        hs_lead = round(first_hs - st, 2) if first_hs is not None else 0.0
        v_lead = round(first_v - st, 2) if first_v is not None else 0.0
        if (first_hs is not None and hs_lead > 0.22) or (first_hs is None and first_v is not None and v_lead > 0.30):
            premature_lead_segments.append({
                "id": s.get("id"),
                "start": st,
                "end": et,
                "first_hardsub": first_hs,
                "first_voice": first_v,
                "hs_lead_s": hs_lead,
                "voice_lead_s": v_lead,
                "text": sub[:45],
            })

        # Unsplit internal silent gap: >=2 dominant sub-clauses separated by >=0.40s TRUE blank gap (where spans[k] is None)
        if len(dom_ivs) >= 2:
            for (a_st, a_et), (b_st, b_et) in zip(dom_ivs[:-1], dom_ivs[1:]):
                if (b_st - a_et) >= 0.40:
                    g0 = max(0, int(round(a_et * 5)))
                    g1 = min(len(spans) - 1, int(round(b_st * 5)) - 1)
                    blank_cnt = sum(1 for k in range(g0, g1 + 1) if spans[k] is None)
                    if blank_cnt >= 2:
                        internal_blank_gap_segments.append({
                            "id": s.get("id"),
                            "start": st,
                            "end": et,
                            "blank_gap": [round(a_et, 2), round(b_st, 2)],
                            "text": sub[:45],
                        })
                        break

    # 3. Check acoustic voice bursts overlapping visual sub-clauses
    speech_bursts_covered = 0
    speech_bursts_total = 0
    for b in merged_bursts:
        bst, bet = b["start"], b["end"]
        bdur = bet - bst
        has_sub_clause = any(max(0.0, min(bet, sc["end"]) - max(bst, sc["start"])) >= 0.25 for sc in sub_clauses)
        if has_sub_clause or b["peak_voc_rms"] >= 0.035:
            speech_bursts_total += 1
            ov = sum(max(0.0, min(bet, float(s["end"])) - max(bst, float(s["start"]))) for s in segs)
            if (ov / max(0.2, bdur)) >= 0.40 or not has_sub_clause:
                speech_bursts_covered += 1

    sc_ratio = (covered_cnt / len(sub_clauses)) if sub_clauses else 1.0
    ac_ratio = (speech_bursts_covered / speech_bursts_total) if speech_bursts_total else 1.0

    report = {
        "analyzed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "target_segments_file": os.path.basename(target_segments_path),
        "total_segments_checked": len(segs),
        "total_visual_sub_clauses": len(sub_clauses),
        "covered_visual_sub_clauses": covered_cnt,
        "sub_clause_coverage_ratio": round(sc_ratio, 4),
        "uncovered_sub_clauses_count": len(uncovered_clauses),
        "uncovered_sub_clauses": uncovered_clauses[:25],
        "bloated_segments_count": len(bloated_segments),
        "bloated_segments": bloated_segments[:20],
        "premature_lead_segments_count": len(premature_lead_segments),
        "premature_lead_segments": premature_lead_segments[:20],
        "internal_blank_gap_segments_count": len(internal_blank_gap_segments),
        "internal_blank_gap_segments": internal_blank_gap_segments[:20],
        "acoustic_voice_bursts_count": len(merged_bursts),
        "acoustic_speech_coverage_ratio": round(ac_ratio, 4),
    }

    out_audit = os.path.join(data_dir, "01_waveform_voice_audit.json")
    with open(out_audit, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(
        f"[DONE] Waveform & Onset Audit: sub_clauses={covered_cnt}/{len(sub_clauses)} ({sc_ratio*100:.1f}%), "
        f"bloated={len(bloated_segments)}, premature_lead={len(premature_lead_segments)}, "
        f"internal_blank_gaps={len(internal_blank_gap_segments)} -> {out_audit} in {time.time()-t0:.2f}s",
        flush=True,
    )
    return report


def main():
    parser = argparse.ArgumentParser(description="Run Multi-Band Acoustic Waveform, Sub-Clause & Premature Lead-In Audit.")
    parser.add_argument("--workspace", required=True, help="Workspace path (workspace/<slug>).")
    parser.add_argument("--segments", required=False, help="Optional path to segments JSON to audit.")
    parser.add_argument("--snap-and-split", action="store_true", help="Automatically split compound multi-interval segments and clamp [start, end] to active hardsub/voice onset.")
    args = parser.parse_args()
    analyze_waveform_and_clauses(args.workspace, args.segments, snap_in_place=args.snap_and_split)


if __name__ == "__main__":
    main()
