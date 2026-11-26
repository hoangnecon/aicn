#!/usr/bin/env python3
"""
Stage 1B+: Multi-Band Acoustic Waveform, Onset Alignment & Monotonic Guard (analyze_voice_waveform.py, v3.0.0).
Analyzes:
  1. 50ms-hop RMS waveform envelope + ZCR across Demucs vocals.wav and 300-3400Hz formant-boosted raw audio.
  2. Gap-first multi-band uncovered speech detection with +0.08s protective collar around Pass 1 spans.
  3. Cross-checks ASR/Script segments for:
     - Timestamp overlaps, inverted spans, and <0.15s runt stubs
     - Boundary duplicate/echo fragments within 0.35s
     - Bloated segment durations (dur > char_count * 0.35 + 1.4s)
     - Premature lead-in (>0.30s before acoustic voice onset)
     - Uncovered acoustic voice bursts and longest unprobed vocal gap
  4. Provides `--snap-and-split` to absorb <0.25s micro-fragments, deduplicate boundary echoes,
     clamp segments to acoustic voice onset/offset, and enforce strict monotonic non-overlapping
     timestamps: 0.0 <= start[i] < end[i] <= start[i+1] - 0.02 and (end[i] - start[i]) >= 0.15.
Writes data/01_waveform_voice_audit.json.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
try:
    import aicn_platform as ap
except ImportError:
    ap = None


WAVEFORM_CACHE_VERSION = "wf-4.1"


def decode_audio_16k(input_path: str, af_filter: str = "") -> np.ndarray:
    cmd = [ap.find_tool("ffmpeg") if ap else "ffmpeg", "-y", "-i", input_path]
    if af_filter:
        cmd.extend(["-af", af_filter])
    cmd.extend(["-f", "s16le", "-ar", "16000", "-ac", "1", "pipe:1"])
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True)
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def _fast_file_sig(path: str) -> str:
    """Pure content-addressed SHA-256 signature (zero path/mtime dependence)."""
    if not path or not os.path.exists(path):
        return "missing"
    if ap is not None and hasattr(ap, "content_sha256"):
        return ap.content_sha256(path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write_json(path: str, obj) -> None:
    if ap is not None and hasattr(ap, "atomic_write_json"):
        ap.atomic_write_json(path, obj)
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def cjk_chars(text: str) -> str:
    """Return all CJK unified ideograph characters in text."""
    return "".join(c for c in (text or "") if "\u4e00" <= c <= "\u9fff")


def cjk_alnum_chars(text: str) -> str:
    """Return normalized lowercase CJK + alphanumeric characters in text."""
    return "".join(c.lower() for c in (text or "") if ("\u4e00" <= c <= "\u9fff") or c.isalnum())


def is_boundary_duplicate_pair(
    left_seg: dict,
    right_seg: dict,
    gap_thresh_s: float = 0.35,
    strict: bool | None = None,
) -> tuple[bool, str]:
    """
    Detect boundary duplicate/echo fragments between two temporally adjacent segments (gap <= gap_thresh_s).
    When either segment comes from Pass 2B (or source is unspecified), enforces strict v3.0.0 Pass 2B dedup
    (<=2-char shared CJK char or >=0.55 CJK character containment + containment/affix checks).
    """
    gap = float(right_seg.get("start", 0.0)) - float(left_seg.get("end", 0.0))
    if gap > gap_thresh_s + 1e-6:
        return False, ""

    lt = (left_seg.get("text", left_seg.get("sub_text", "")) or "").strip()
    rt = (right_seg.get("text", right_seg.get("sub_text", "")) or "").strip()
    la, ra = cjk_alnum_chars(lt), cjk_alnum_chars(rt)
    lc, rc = cjk_chars(lt), cjk_chars(rt)
    if not la or not ra:
        return False, ""

    src_l = str(left_seg.get("source", "") or "")
    src_r = str(right_seg.get("source", "") or "")
    if strict is None:
        involves_p2b = ("pass2b" in src_l) or ("pass2b" in src_r)
    else:
        involves_p2b = bool(strict)

    if la == ra:
        return True, "exact_duplicate"

    if (la in ra or ra in la) and (min(len(la), len(ra)) >= 2 or gap <= 0.20 or involves_p2b):
        return True, "substring_containment"

    max_k = min(len(la), len(ra))
    for k in range(max_k, 0, -1):
        if la[-k:] == ra[:k]:
            if k >= 2 or (min(len(la), len(ra)) <= 2 and (involves_p2b or gap <= 0.15)):
                return True, f"affix_overlap({la[-k:]})"
            break

    if involves_p2b and lc and rc:
        sa, sb = set(lc), set(rc)
        # Only flag <=2-char shared char when the <=2-char side is a Pass 2B fragment/echo
        # or shares a boundary char (prevents false positive on valid turns like '我是谢谢' + '你是').
        l_is_short_p2b = len(lc) <= 2 and ("pass2b" in src_l or (not src_l and not src_r))
        r_is_short_p2b = len(rc) <= 2 and ("pass2b" in src_r or (not src_l and not src_r))
        if (l_is_short_p2b or r_is_short_p2b) and (sa & sb):
            return True, f"short_shared_char({''.join(sorted(sa & sb))})"
        cont = len(sa & sb) / max(1, min(len(sa), len(sb)))
        if cont >= 0.55:
            return True, f"cjk_containment({cont:.2f})"

    return False, ""


def find_boundary_duplicate_pairs(segments: list, gap_thresh_s: float = 0.35) -> list:
    """Return list of adjacent boundary duplicate pairs for Stage 1 verification."""
    hits = []
    for i in range(len(segments) - 1):
        flag, reason = is_boundary_duplicate_pair(segments[i], segments[i + 1], gap_thresh_s=gap_thresh_s)
        if flag:
            hits.append({
                "left_id": segments[i].get("id", i),
                "right_id": segments[i + 1].get("id", i + 1),
                "gap_s": round(float(segments[i + 1]["start"]) - float(segments[i]["end"]), 3),
                "left_text": segments[i].get("text", segments[i].get("sub_text", ""))[:30],
                "right_text": segments[i + 1].get("text", segments[i + 1].get("sub_text", ""))[:30],
                "reason": reason,
            })
    return hits


def find_timestamp_violations(segments: list, min_dur_s: float = 0.15, min_gap_s: float = 0.02) -> list:
    """Check strict monotonic non-overlapping invariant: 0 <= start[i] < end[i] <= start[i+1] - 0.02 and dur >= 0.15s."""
    violations = []
    eps = 1e-6
    for i, s in enumerate(segments):
        st = float(s.get("start", -1.0))
        et = float(s.get("end", -1.0))
        sid = s.get("id", i)
        if st < 0.0 or et <= st:
            violations.append({"id": sid, "kind": "inverted_or_negative", "start": st, "end": et})
        elif (et - st) < (min_dur_s - eps):
            violations.append({"id": sid, "kind": "runt_lt_min_dur", "start": st, "end": et, "dur": round(et - st, 3)})
        if i + 1 < len(segments):
            nxt_st = float(segments[i + 1].get("start", 0.0))
            if et > nxt_st - min_gap_s + eps:
                violations.append({
                    "id": sid,
                    "next_id": segments[i + 1].get("id", i + 1),
                    "kind": "overlap_or_gap_violation",
                    "end": et,
                    "next_start": nxt_st,
                    "gap": round(nxt_st - et, 3),
                })
    return violations


def _bandpass_in_ram(x: np.ndarray, lo_hz: float, hi_hz: float, sr: int = 16000, order: int = 4) -> np.ndarray:
    """Fast zero-disk 4th-order Butterworth SOS bandpass filter in RAM (-24 dB/oct isolation)."""
    from scipy.signal import butter, sosfilt
    nyq = 0.5 * sr
    sos = butter(order, [max(10.0, lo_hz) / nyq, min(nyq * 0.95, hi_hz) / nyq], btype="band", output="sos")
    return sosfilt(sos, np.asarray(x, dtype=np.float32)).astype(np.float32)


def _local_rms_norm(x: np.ndarray, sr: int = 16000, win_s: float = 0.10, floor: float = 0.035) -> np.ndarray:
    """Fast in-RAM dynamic gain normalization."""
    arr = np.asarray(x, dtype=np.float32)
    if arr.size == 0:
        return arr
    win_n = max(16, int(round(win_s * sr)))
    env = np.sqrt(np.convolve(arr ** 2, np.ones(win_n, dtype=np.float32) / win_n, mode="same") + 1e-8)
    return np.clip(arr / np.maximum(env * 3.0, floor), -0.95, 0.95).astype(np.float32)


def build_multiband_audio_streams(voc_arr: np.ndarray, raw_arr: np.ndarray, sr: int = 16000) -> dict:
    """
    Decompose vocals + raw full-mix audio into 4 speech-targeted frequency bands + clean raw reference:
      - full_vocal (120-7500 Hz, 70% voc + 30% raw): clean full-spectrum speech
      - high       (900-7500 Hz, 55% voc + 45% raw): child/cartoon high-pitch voices & breathy whispers
      - mid        (300-3400 Hz, 35% voc + 65% raw): core speech formants, phone/radio/PA comms
      - low        ( 85-1400 Hz, 75% voc + 25% raw): deep male voices & muffled speech under high-freq SFX
      - raw_clean  (100-7500 Hz, 100% raw):          reverse cross-check stream
    """
    min_len = min(len(voc_arr), len(raw_arr))
    v = np.asarray(voc_arr[:min_len], dtype=np.float32)
    r = np.asarray(raw_arr[:min_len], dtype=np.float32)

    full_mix = 0.70 * v + 0.30 * r
    high_mix = 0.55 * v + 0.45 * r
    mid_mix = 0.35 * v + 0.65 * r
    low_mix = 0.75 * v + 0.25 * r

    full_bp = _bandpass_in_ram(full_mix, 120.0, 7500.0, sr=sr)
    high_bp = _bandpass_in_ram(high_mix, 900.0, 7500.0, sr=sr)
    mid_bp = _bandpass_in_ram(mid_mix, 300.0, 3400.0, sr=sr)
    low_bp = _bandpass_in_ram(low_mix, 85.0, 1400.0, sr=sr)
    raw_clean_bp = _bandpass_in_ram(r, 100.0, 7500.0, sr=sr)

    return {
        "full_vocal_raw": full_bp,
        "high_raw": high_bp,
        "mid_raw": mid_bp,
        "low_raw": low_bp,
        "raw_clean_raw": raw_clean_bp,
        "full_vocal": _local_rms_norm(full_bp, sr=sr, floor=0.040),
        "high": _local_rms_norm(high_bp, sr=sr, floor=0.030),
        "mid": _local_rms_norm(mid_bp, sr=sr, floor=0.035),
        "low": _local_rms_norm(low_bp, sr=sr, floor=0.040),
        "raw_clean": _local_rms_norm(raw_clean_bp, sr=sr, floor=0.040),
        "voc_raw": v,
        "raw_raw": r,
    }


def _hop_rms_and_zcr(x: np.ndarray, n_hops: int, hop: int):
    blks = x[: n_hops * hop].reshape(n_hops, hop)
    rms = np.sqrt(np.mean(blks ** 2, axis=1) + 1e-12)
    zcr = np.mean(np.abs(np.diff(np.sign(blks), axis=1)), axis=1) / 2.0
    return rms, zcr


def _sliding_mod_cv(rms_arr: np.ndarray, win_hops: int = 10) -> np.ndarray:
    """Compute local syllable envelope modulation CV (std/mean) in a sliding window (~0.5s at 50ms hop)."""
    n = len(rms_arr)
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    w = max(3, min(win_hops, n))
    kernel = np.ones(w, dtype=np.float32) / w
    mean = np.convolve(rms_arr, kernel, mode="same")
    mean_sq = np.convolve(rms_arr ** 2, kernel, mode="same")
    var = np.maximum(0.0, mean_sq - mean ** 2)
    return (np.sqrt(var) / (mean + 1e-6)).astype(np.float32)


def detect_uncovered_multiband_gaps(
    existing_segments: list,
    voc_arr: np.ndarray,
    raw_arr: np.ndarray,
    streams: dict = None,
    sr: int = 16000,
    max_window_s: float = 10.0,
    min_gap_s: float = 0.35,
) -> list:
    """
    Gap-First Multi-Band Uncovered Speech Scanner (v3.0.0):
      1. Masks out Pass 1 covered timeline regions with a +0.08s outward protective collar so Pass 2B
         never bleeds into Pass 1 boundary syllables, while trimming bloated spans (> max_cov_dur)
         to their primary active speech cluster so adjacent uncovered utterances stay exposed.
      2. Scans EVERY uncovered 50ms hop across 4 frequency bands (full_vocal, high, mid, low).
      3. Splits uncovered regions > max_window_s at lowest-energy valleys.
    """
    if streams is None:
        streams = build_multiband_audio_streams(voc_arr, raw_arr, sr=sr)

    hop = int(0.05 * sr)
    n_samples = min(len(streams["voc_raw"]), len(streams["raw_raw"]), len(streams["full_vocal_raw"]))
    n_hops = n_samples // hop
    if n_hops <= 0:
        return []
    total_dur = n_hops * 0.05

    voc_rms, voc_zcr = _hop_rms_and_zcr(streams["voc_raw"], n_hops, hop)
    full_rms, _ = _hop_rms_and_zcr(streams["full_vocal_raw"], n_hops, hop)
    hi_rms, hi_zcr = _hop_rms_and_zcr(streams["high_raw"], n_hops, hop)
    mid_rms, mid_zcr = _hop_rms_and_zcr(streams["mid_raw"], n_hops, hop)
    lo_rms, lo_zcr = _hop_rms_and_zcr(streams["low_raw"], n_hops, hop)
    raw_hi_bp = _bandpass_in_ram(streams["raw_raw"][: n_hops * hop], 3600.0, 7200.0, sr=sr)
    raw_hi_rms, _ = _hop_rms_and_zcr(raw_hi_bp, n_hops, hop)
    voc_hi_bp = _bandpass_in_ram(streams["voc_raw"][: n_hops * hop], 900.0, 7500.0, sr=sr)
    voc_hi_rms, _ = _hop_rms_and_zcr(voc_hi_bp, n_hops, hop)
    voc_lo_bp = _bandpass_in_ram(streams["voc_raw"][: n_hops * hop], 85.0, 1400.0, sr=sr)
    voc_lo_rms, _ = _hop_rms_and_zcr(voc_lo_bp, n_hops, hop)

    voc_mod = _sliding_mod_cv(voc_rms, 10)
    hi_mod = _sliding_mod_cv(hi_rms, 10)
    mid_mod = _sliding_mod_cv(mid_rms, 10)
    lo_mod = _sliding_mod_cv(lo_rms, 10)

    def _floor(arr, lo_cap, hi_cap):
        return max(lo_cap, min(hi_cap, float(np.percentile(arr, 15))))

    voc_fl = _floor(voc_rms, 0.0020, 0.0070)
    hi_fl = _floor(hi_rms, 0.0015, 0.0065)
    mid_fl = _floor(mid_rms, 0.0025, 0.0100)
    lo_fl = _floor(lo_rms, 0.0025, 0.0100)

    # Build covered mask with +0.08s outward protective collar (replacing old -0.05s shrink)
    covered = np.zeros(n_hops, dtype=bool)
    seg_act_mask = (voc_rms > max(0.0080, voc_fl * 2.1)) | (full_rms > max(0.0100, voc_fl * 2.5))
    for s in existing_segments or []:
        raw_st = float(s.get("start", 0.0))
        raw_et = float(s.get("end", 0.0))
        if raw_et <= raw_st:
            continue
        txt_s = s.get("zh_text", s.get("text", "")) or ""
        n_ch = len(cjk_alnum_chars(txt_s))
        max_cov_dur = (n_ch * 0.35 + 1.35) if n_ch > 0 else (raw_et - raw_st)
        if n_ch > 0 and (raw_et - raw_st) > max_cov_dur:
            a0 = max(0, int(round(raw_st / 0.05)))
            a1 = min(n_hops, int(round(raw_et / 0.05)))
            act_idx = [k for k in range(a0, a1) if seg_act_mask[k]]
            if act_idx:
                cl_list = [[act_idx[0], act_idx[0] + 1, float(full_rms[act_idx[0]])]]
                for k in act_idx[1:]:
                    if (k - cl_list[-1][1]) < 9:
                        cl_list[-1][1] = k + 1
                        cl_list[-1][2] += float(full_rms[k])
                    else:
                        cl_list.append([k, k + 1, float(full_rms[k])])
                if len(cl_list) >= 2:
                    best_e, best_pair = -1.0, None
                    for ci in range(len(cl_list)):
                        acc_e = 0.0
                        for cj in range(ci, len(cl_list)):
                            if ((cl_list[cj][1] - cl_list[ci][0]) * 0.05) > max_cov_dur and cj > ci:
                                break
                            acc_e += cl_list[cj][2]
                            if acc_e > best_e:
                                best_e, best_pair = acc_e, (ci, cj)
                    if best_pair is not None:
                        raw_st = max(raw_st, cl_list[best_pair[0]][0] * 0.05 - 0.05)
                        raw_et = min(raw_et, cl_list[best_pair[1]][1] * 0.05 + 0.05)
                else:
                    raw_st = max(raw_st, cl_list[0][0] * 0.05 - 0.05)
                    raw_et = min(raw_et, cl_list[0][1] * 0.05 + 0.05)
        st = max(0.0, raw_st - 0.08)
        et = min(total_dur, raw_et + 0.08)
        i0 = max(0, int(st / 0.05))
        i1 = min(n_hops, int(np.ceil(et / 0.05)))
        if i1 > i0:
            covered[i0:i1] = True

    is_full = (voc_rms > max(0.0075, voc_fl * 2.1)) & (voc_zcr >= 0.015) & (
        (voc_zcr <= 0.45) | ((voc_rms > 0.035) & (voc_zcr <= 0.58))
    ) & (
        (voc_rms > max(0.018, voc_fl * 3.2)) | (voc_mod >= 0.14)
    )
    is_high = (
        (hi_rms > max(0.0050, hi_fl * 2.2))
        & (hi_zcr >= 0.04)
        & (hi_zcr <= 0.62)
        & (hi_mod >= 0.15)
        & ((voc_hi_rms > max(0.0030, hi_fl * 1.4)) | (hi_rms > max(0.012, hi_fl * 3.5)))
    )
    mid_vs_hi = mid_rms / (raw_hi_rms + 1e-6)
    is_mid = (
        (mid_rms > max(0.0065, mid_fl * 2.2))
        & (mid_zcr >= 0.02)
        & (mid_zcr <= 0.42)
        & (mid_mod >= 0.16)
        & (
            (mid_vs_hi > 1.8)
            | (voc_rms > max(0.010, voc_fl * 2.4))
            | ((mid_rms > max(0.015, mid_fl * 3.5)) & (mid_mod >= 0.22))
        )
    )
    is_low = (
        (lo_rms > max(0.0080, lo_fl * 2.2))
        & (voc_lo_rms > max(0.0060, lo_fl * 1.8))
        & (lo_zcr >= 0.01)
        & (lo_zcr <= 0.28)
        & (lo_mod >= 0.15)
    )

    uncovered_active = (~covered) & (is_full | is_high | is_mid | is_low)
    onset_tail_floor = (~covered) & (
        (voc_rms > max(0.0035, voc_fl * 1.35))
        | (full_rms > max(0.0045, voc_fl * 1.45))
        | (mid_rms > max(0.0045, mid_fl * 1.45))
        | (hi_rms > max(0.0035, hi_fl * 1.45))
        | (lo_rms > max(0.0050, lo_fl * 1.45))
    )

    # Bridge intra-sentence clause pauses up to 0.35s (7 hops at 50ms) so 2-clause short lines
    # (e.g. "说吧，哪疼" with a 0.30s comma pause) stay intact without gluing >=0.45s separate turns/SFX.
    max_bridge_hops = 8
    raw_spans = []
    i = 0
    while i < n_hops:
        if uncovered_active[i]:
            j = i + 1
            while j < n_hops:
                if uncovered_active[j]:
                    j += 1
                    continue
                bridge = 0
                while (
                    (j + bridge) < n_hops
                    and bridge < max_bridge_hops
                    and (not uncovered_active[j + bridge])
                    and (not covered[j + bridge])
                ):
                    bridge += 1
                if bridge < max_bridge_hops and (j + bridge) < n_hops and uncovered_active[j + bridge]:
                    j = j + bridge + 1
                else:
                    break
            raw_spans.append([i, j])
            i = j
        else:
            i += 1

    # Backtrack initial consonant onset (up to 0.45s = 9 hops, bridging 1 quiet stop-closure hop) &
    # trailing vowel release (up to 0.20s = 4 hops), keeping >= 2 hops (0.10s) clear of neighbor spans.
    for idx_sp, sp in enumerate(raw_spans):
        lo_bound = (raw_spans[idx_sp - 1][1] + 2) if idx_sp > 0 else 0
        hi_bound = (raw_spans[idx_sp + 1][0] - 2) if idx_sp + 1 < len(raw_spans) else n_hops
        s0, s1 = sp[0], sp[1]
        for _ in range(9):
            if s0 - 1 >= lo_bound and onset_tail_floor[s0 - 1]:
                s0 -= 1
            elif s0 - 2 >= lo_bound and (not covered[s0 - 1]) and onset_tail_floor[s0 - 2]:
                s0 -= 2
            else:
                break
        for _ in range(4):
            if s1 < hi_bound and onset_tail_floor[s1]:
                s1 += 1
            else:
                break
        sp[0], sp[1] = s0, s1
    raw_spans = [(sp[0], sp[1]) for sp in raw_spans]

    min_hops = max(2, int(round(min_gap_s / 0.05)))
    max_hops = max(min_hops + 4, int(round(max_window_s / 0.05)))
    min_split_hops = max(min_hops, int(round(3.5 / 0.05)))
    composite_env = np.maximum(voc_rms, 0.65 * full_rms)

    split_spans = []
    for i0, i1 in raw_spans:
        cur = i0
        while (i1 - cur) > max_hops:
            lo_cut = cur + min_split_hops
            hi_cut = min(i1 - min_hops, cur + max_hops)
            if hi_cut <= lo_cut:
                cut = min(i1, cur + max_hops)
            else:
                rel_min = int(np.argmin(composite_env[lo_cut:hi_cut + 1]))
                cut = lo_cut + rel_min
            split_spans.append((cur, cut))
            cur = cut
        if (i1 - cur) >= min_hops:
            split_spans.append((cur, i1))

    gaps = []
    for i0, i1 in split_spans:
        dur = round((i1 - i0) * 0.05, 2)
        if dur < min_gap_s:
            continue
        p_voc = float(np.max(voc_rms[i0:i1]))
        p_full = float(np.max(full_rms[i0:i1]))
        p_hi = float(np.max(hi_rms[i0:i1]))
        p_mid = float(np.max(mid_rms[i0:i1]))
        p_lo = float(np.max(lo_rms[i0:i1]))

        m_voc = float(np.std(voc_rms[i0:i1]) / (np.mean(voc_rms[i0:i1]) + 1e-6))
        m_hi = float(np.std(hi_rms[i0:i1]) / (np.mean(hi_rms[i0:i1]) + 1e-6))
        m_mid = float(np.std(mid_rms[i0:i1]) / (np.mean(mid_rms[i0:i1]) + 1e-6))
        m_lo = float(np.std(lo_rms[i0:i1]) / (np.mean(lo_rms[i0:i1]) + 1e-6))
        best_mod = max(m_voc, m_hi, m_mid, m_lo)

        if best_mod < 0.14 and p_voc < 0.035:
            continue

        if dur < 1.0:
            pk_mid_ratio = float(np.max(mid_vs_hi[i0:i1])) if i1 > i0 else 0.0
            has_short_vocal = p_voc >= max(0.018, voc_fl * 3.2)
            has_short_phone = (p_mid >= max(0.018, mid_fl * 3.2)) and (m_mid >= 0.20 or pk_mid_ratio > 2.4)
            if not (has_short_vocal or has_short_phone):
                continue

        band_snr = {
            "full_vocal": round(float(np.mean(full_rms[i0:i1])) / max(1e-4, voc_fl), 2),
            "high": round(float(np.mean(hi_rms[i0:i1])) / max(1e-4, hi_fl), 2),
            "mid": round(float(np.mean(mid_rms[i0:i1])) / max(1e-4, mid_fl), 2),
            "low": round(float(np.mean(lo_rms[i0:i1])) / max(1e-4, lo_fl), 2),
        }
        band_hits = {
            "full_vocal": int(is_full[i0:i1].sum()),
            "high": int(is_high[i0:i1].sum()),
            "mid": int(is_mid[i0:i1].sum()),
            "low": int(is_low[i0:i1].sum()),
        }
        dominant = max(("full_vocal", "high", "mid", "low"), key=lambda b: (band_hits[b], band_snr[b]))
        if band_hits["mid"] > band_hits["full_vocal"] and band_hits["mid"] >= 3 and p_voc < p_mid * 0.75:
            kind = "phone_call"
        elif band_hits["high"] >= band_hits["full_vocal"] and band_hits["high"] >= 3 and p_voc < 0.030:
            kind = "high_voice"
        elif p_voc < 0.022 and band_hits["full_vocal"] >= 2:
            kind = "whisper"
        elif band_hits["low"] > band_hits["high"] and band_hits["low"] >= 3 and p_voc < 0.035:
            kind = "low_voice"
        else:
            kind = "normal"

        # Select specialist sub-bands ONLY when acoustically warranted (prevents wasting 3 extra sub-band
        # passes on loud broadband SFX/barks that full_vocal already covers cleanly).
        mean_raw_hi = float(np.mean(raw_hi_rms[i0:i1])) if i1 > i0 else 0.0
        mean_lo = float(np.mean(lo_rms[i0:i1])) if i1 > i0 else 0.0
        active_bands = []
        if kind == "phone_call" or band_hits["mid"] > band_hits["full_vocal"] or (band_hits["mid"] >= 2 and (p_voc < 0.040 or p_mid >= p_voc * 0.85)):
            active_bands.append("mid")
        if kind in ("high_voice", "whisper") or (band_hits["high"] >= 2 and (p_voc < 0.040 or p_hi >= p_voc * 0.65)):
            active_bands.append("high")
        if kind == "low_voice" or (band_hits["low"] >= 2 and (p_voc < 0.060 or mean_raw_hi > mean_lo * 0.45)):
            active_bands.append("low")
        active_bands.sort(key=lambda b: (band_hits[b], band_snr[b]), reverse=True)
        if not active_bands and p_voc < 0.035:
            active_bands = sorted(("high", "mid", "low"), key=lambda b: band_snr[b], reverse=True)[:1]

        gaps.append({
            "start": round(i0 * 0.05, 2),
            "end": round(i1 * 0.05, 2),
            "duration": dur,
            "kind": kind,
            "dominant_band": dominant,
            "probe_bands": active_bands,
            "band_snr": band_snr,
            "peak_voc_rms": round(p_voc, 4),
            "peak_full_rms": round(p_full, 4),
            "peak_high_rms": round(p_hi, 4),
            "peak_mid_rms": round(p_mid, 4),
            "peak_low_rms": round(p_lo, 4),
            "mean_voc_zcr": round(float(np.mean(voc_zcr[i0:i1])) if i1 > i0 else 0.0, 4),
            "mod_cv": round(best_mod, 3),
        })

    return gaps


def compute_voice_mask_and_bursts(
    voc_arr: np.ndarray,
    tel_arr: np.ndarray,
    raw_tel_arr: np.ndarray = None,
    raw_arr: np.ndarray = None,
    sr: int = 16000,
):
    """Detect clean primary `voice_mask` and `merged_bursts` (normal, whisper, phone_call)."""
    hop = int(0.05 * sr)
    lens = [len(voc_arr), len(tel_arr)]
    if raw_tel_arr is not None:
        lens.append(len(raw_tel_arr))
    if raw_arr is not None:
        lens.append(len(raw_arr))
    n_hops = min(lens) // hop
    if n_hops <= 0:
        return np.zeros(0, dtype=bool), []

    v_blks = voc_arr[: n_hops * hop].reshape(n_hops, hop)
    t_blks = tel_arr[: n_hops * hop].reshape(n_hops, hop)
    v_rms_arr = np.sqrt(np.mean(v_blks ** 2, axis=1) + 1e-12)
    t_rms_arr = np.sqrt(np.mean(t_blks ** 2, axis=1) + 1e-12)
    zcr_arr = np.mean(np.abs(np.diff(np.sign(v_blks), axis=1)), axis=1) / 2.0
    t_zcr_arr = np.mean(np.abs(np.diff(np.sign(t_blks), axis=1)), axis=1) / 2.0

    voc_floor = max(0.002, min(0.006, float(np.percentile(v_rms_arr, 15))))
    nz_v = v_rms_arr[v_rms_arr > voc_floor * 1.2]
    voc_ref = max(0.020, float(np.percentile(nz_v, 85))) if nz_v.size >= 10 else 0.035
    normal_thresh = max(0.016, min(0.024, voc_floor * 3.0))
    whisper_lo = max(0.0040, voc_floor * 1.8)
    whisper_hi = max(normal_thresh, min(0.022, voc_ref * 0.38))

    if raw_tel_arr is not None and raw_arr is not None:
        rt_blks = raw_tel_arr[: n_hops * hop].reshape(n_hops, hop)
        rt_rms_arr = np.sqrt(np.mean(rt_blks ** 2, axis=1) + 1e-12)
        hi_arr = _bandpass_in_ram(raw_arr[: n_hops * hop], 3600.0, 7200.0, sr=sr)
        hi_rms_arr = np.sqrt(np.mean(hi_arr.reshape(n_hops, hop) ** 2, axis=1) + 1e-12)
        tel_vs_hi_arr = rt_rms_arr / (hi_rms_arr + 1e-6)
        has_raw_tel = True
    else:
        rt_rms_arr = np.zeros(n_hops, dtype=np.float32)
        tel_vs_hi_arr = np.zeros(n_hops, dtype=np.float32)
        has_raw_tel = False

    voice_mask = np.zeros(n_hops, dtype=bool)
    raw_bursts = []
    in_burst = False
    b_i0 = 0
    b_peak_voc = 0.0
    b_peak_tel = 0.0
    b_phone_hits = 0
    b_whisper_hits = 0
    b_normal_hits = 0

    def _finalize_burst(i0, i1, peak_v, peak_t, n_ph, n_wh, n_nm):
        if (i1 - i0) < 2:
            return
        raw_bursts.append({
            "start": round(i0 * 0.05, 2),
            "end": round(i1 * 0.05, 2),
            "peak_voc_rms": round(float(peak_v), 4),
            "peak_tel_rms": round(float(peak_t), 4),
            "_i0": int(i0),
            "_i1": int(i1),
            "_ph": int(n_ph),
            "_wh": int(n_wh),
            "_nm": int(n_nm),
        })

    for i in range(n_hops):
        v_rms = float(v_rms_arr[i])
        t_rms = float(t_rms_arr[i])
        zcr = float(zcr_arr[i])
        t_zcr = float(t_zcr_arr[i])

        is_normal = (v_rms > normal_thresh and 0.02 <= zcr <= 0.32)
        is_whisper = (whisper_lo <= v_rms <= whisper_hi and 0.02 <= zcr <= 0.45 and not is_normal)
        if has_raw_tel:
            rt_rms = float(rt_rms_arr[i])
            tel_vs_hi = float(tel_vs_hi_arr[i])
            is_phone = (
                rt_rms > max(0.004, voc_floor * 1.8)
                and rt_rms > v_rms * 1.35
                and tel_vs_hi > 2.2
                and 0.02 <= t_zcr <= 0.36
            )
        else:
            is_phone = (t_rms > 0.12 and v_rms > 0.006 and 0.02 <= zcr <= 0.35)

        voice_mask[i] = is_normal or (t_rms > 0.14 and v_rms > max(0.008, voc_floor * 2.5) and 0.02 <= zcr <= 0.32)

        if is_normal or is_whisper or is_phone:
            if not in_burst:
                in_burst = True
                b_i0 = i
                b_peak_voc = v_rms
                b_peak_tel = t_rms
                b_phone_hits = 1 if is_phone else 0
                b_whisper_hits = 1 if is_whisper else 0
                b_normal_hits = 1 if is_normal else 0
            else:
                b_peak_voc = max(b_peak_voc, v_rms)
                b_peak_tel = max(b_peak_tel, t_rms)
                if is_phone:
                    b_phone_hits += 1
                if is_whisper:
                    b_whisper_hits += 1
                if is_normal:
                    b_normal_hits += 1
        elif in_burst:
            _finalize_burst(b_i0, i, b_peak_voc, b_peak_tel, b_phone_hits, b_whisper_hits, b_normal_hits)
            in_burst = False

    if in_burst:
        _finalize_burst(b_i0, n_hops, b_peak_voc, b_peak_tel, b_phone_hits, b_whisper_hits, b_normal_hits)

    grouped = []
    for b in raw_bursts:
        if grouped and (b["start"] - grouped[-1]["end"]) <= 0.22:
            prev = grouped[-1]
            prev["end"] = b["end"]
            prev["_i1"] = b["_i1"]
            prev["peak_voc_rms"] = max(prev["peak_voc_rms"], b["peak_voc_rms"])
            prev["peak_tel_rms"] = max(prev["peak_tel_rms"], b["peak_tel_rms"])
            prev["_ph"] += b["_ph"]
            prev["_wh"] += b["_wh"]
            prev["_nm"] += b["_nm"]
        else:
            grouped.append(dict(b))

    merged_bursts = []
    for b in grouped:
        i0, i1 = b["_i0"], b["_i1"]
        if (i1 - i0) < 7:
            continue
        n_ph, n_wh, n_nm = b["_ph"], b["_wh"], b["_nm"]
        if n_ph > n_nm and n_ph >= 3:
            env = rt_rms_arr[i0:i1] if has_raw_tel else t_rms_arr[i0:i1]
            if float(np.std(env) / (np.mean(env) + 1e-6)) < 0.16:
                continue
            kind = "phone_call"
        elif n_wh >= 3 and n_nm == 0:
            env = v_rms_arr[i0:i1]
            if float(np.std(env) / (np.mean(env) + 1e-6)) < 0.14:
                continue
            kind = "whisper"
        else:
            kind = "normal"
        merged_bursts.append({
            "start": b["start"],
            "end": b["end"],
            "peak_voc_rms": b["peak_voc_rms"],
            "peak_tel_rms": b["peak_tel_rms"],
            "kind": kind,
        })

    return voice_mask, merged_bursts


def _merge_segment_texts(left_text: str, right_text: str) -> str:
    """Join two adjacent segment texts without duplicating shared boundary substrings."""
    lt = (left_text or "").strip()
    rt = (right_text or "").strip()
    if not lt:
        return rt
    if not rt:
        return lt
    la, ra = cjk_alnum_chars(lt), cjk_alnum_chars(rt)
    if la == ra or ra in la:
        return lt
    if la in ra:
        return rt
    if not any("\u4e00" <= c <= "\u9fff" for c in lt + rt):
        return f"{lt} {rt}"
    return f"{lt}{rt}"


def _dedup_adjacent_segments(segments: list, gap_thresh_s: float = 0.35) -> list:
    """Merge/drop adjacent boundary duplicate fragments within gap_thresh_s cleanly."""
    deduped = []
    for raw in segments:
        s = dict(raw)
        txt = (s.get("sub_text", s.get("text", "")) or "").strip()
        if not txt:
            continue
        st = round(max(0.0, float(s.get("start", 0.0))), 2)
        et = round(float(s.get("end", st + 0.20)), 2)
        if et <= st:
            et = round(st + 0.20, 2)
        s["start"], s["end"] = st, et

        if deduped:
            prev = deduped[-1]
            is_dup, _ = is_boundary_duplicate_pair(prev, s, gap_thresh_s=gap_thresh_s)
            if is_dup:
                pt = (prev.get("sub_text", prev.get("text", "")) or "").strip()
                pt_len = len(cjk_alnum_chars(pt))
                ct_len = len(cjk_alnum_chars(txt))
                prev_p2b = "pass2b" in str(prev.get("source", ""))
                cur_p2b = "pass2b" in str(s.get("source", ""))
                if (prev_p2b and not cur_p2b and ct_len >= pt_len) or (not prev_p2b and cur_p2b and ct_len > pt_len + 1) or (prev_p2b == cur_p2b and ct_len > pt_len):
                    merged = dict(s)
                    merged["start"] = min(float(prev["start"]), st)
                    merged["end"] = max(float(prev["end"]), et)
                    deduped[-1] = merged
                elif cjk_alnum_chars(txt) in cjk_alnum_chars(pt):
                    prev["end"] = max(float(prev["end"]), et)
                continue
        deduped.append(s)
    return deduped


def _absorb_duplicate_and_micro_fragments(segments: list) -> list:
    """
    1. Merge/drop adjacent boundary duplicate fragments within 0.35s.
    2. Absorb ultra-short <0.25s same-speaker stutters/1-char orphans within 0.20s of a neighbor,
       or drop degenerate <0.15s stubs.
    """
    if not segments:
        return []

    deduped = _dedup_adjacent_segments(segments, gap_thresh_s=0.35)

    # Absorb ultra-short <0.25s stutters / orphans cleanly into adjacent speech when within 0.20s
    cleaned = []
    i = 0
    while i < len(deduped):
        cur = dict(deduped[i])
        dur = float(cur["end"]) - float(cur["start"])
        txt = (cur.get("sub_text", cur.get("text", "")) or "").strip()
        n_cjk = len(cjk_chars(txt))
        is_excl = txt.endswith(("！", "!", "？", "?"))
        if dur < 0.25:
            prev = cleaned[-1] if cleaned else None
            nxt = deduped[i + 1] if i + 1 < len(deduped) else None
            g_prev = (float(cur["start"]) - float(prev["end"])) if prev else 999.0
            g_next = (float(nxt["start"]) - float(cur["end"])) if nxt else 999.0
            same_prev = prev is not None and (prev.get("speaker") == cur.get("speaker")) and g_prev <= 0.20
            same_next = nxt is not None and (nxt.get("speaker") == cur.get("speaker")) and g_next <= 0.20

            cur_a = cjk_alnum_chars(txt)
            should_absorb = (
                (dur < 0.15)
                or (n_cjk <= 1 and not is_excl)
                or (same_prev and bool(cur_a) and (cur_a in cjk_alnum_chars(prev.get("text", "")) or cjk_alnum_chars(prev.get("text", "")) in cur_a))
                or (same_next and bool(cur_a) and (cur_a in cjk_alnum_chars(nxt.get("text", "")) or cjk_alnum_chars(nxt.get("text", "")) in cur_a))
            )
            if should_absorb and (same_prev or same_next):
                # Prefer merging 1-char leading orphan forward into nxt if closer or equal
                if same_next and (not same_prev or g_next <= g_prev):
                    nxt_m = dict(nxt)
                    if n_cjk <= 1 or cjk_alnum_chars(txt) not in cjk_alnum_chars(nxt_m.get("text", "")):
                        merged_txt = _merge_segment_texts(txt, nxt_m.get("text", ""))
                        if "text" in nxt_m:
                            nxt_m["text"] = merged_txt
                        if "sub_text" in nxt_m:
                            nxt_m["sub_text"] = _merge_segment_texts(cur.get("sub_text", txt), nxt_m.get("sub_text", ""))
                        if "tts_text" in nxt_m:
                            nxt_m["tts_text"] = _merge_segment_texts(cur.get("tts_text", txt), nxt_m.get("tts_text", ""))
                    if dur >= 0.15 or g_next <= 0.08:
                        nxt_m["start"] = min(float(cur["start"]), float(nxt_m["start"]))
                    deduped[i + 1] = nxt_m
                    i += 1
                    continue
                if same_prev:
                    merged_txt = _merge_segment_texts(prev.get("text", ""), txt)
                    if "text" in prev:
                        prev["text"] = merged_txt
                    if "sub_text" in prev:
                        prev["sub_text"] = _merge_segment_texts(prev.get("sub_text", ""), cur.get("sub_text", txt))
                    if "tts_text" in prev:
                        prev["tts_text"] = _merge_segment_texts(prev.get("tts_text", ""), cur.get("tts_text", txt))
                    prev["end"] = max(float(prev["end"]), float(cur["end"]))
                    i += 1
                    continue

            if dur < 0.15:
                # Isolated degenerate <0.15s stub: drop if tiny noise, or expand to 0.16s if strong standalone exclamation
                if is_excl and len(cjk_alnum_chars(txt)) >= 1 and float(cur.get("avg_logprob", -0.5)) > -0.40:
                    cur["end"] = round(float(cur["start"]) + 0.20, 2)
                else:
                    i += 1
                    continue

        cleaned.append(cur)
        i += 1
    # Re-run boundary deduplication after micro-fragment absorption so merged spans don't expose new boundary echoes
    final_cleaned = []
    for s in cleaned:
        if final_cleaned:
            is_dup, _ = is_boundary_duplicate_pair(final_cleaned[-1], s, gap_thresh_s=0.35)
            if is_dup:
                pt = (final_cleaned[-1].get("sub_text", final_cleaned[-1].get("text", "")) or "").strip()
                ct = (s.get("sub_text", s.get("text", "")) or "").strip()
                if len(cjk_alnum_chars(ct)) > len(cjk_alnum_chars(pt)):
                    s["start"] = min(float(final_cleaned[-1]["start"]), float(s["start"]))
                    final_cleaned[-1] = s
                else:
                    final_cleaned[-1]["end"] = max(float(final_cleaned[-1]["end"]), float(s["end"]))
                continue
        final_cleaned.append(s)
    return final_cleaned


def enforce_monotonic_segments(segments: list, min_dur_s: float = 0.15, min_gap_s: float = 0.02) -> list:
    """
    Guarantee strict monotonic non-overlapping invariant across ALL segments:
      0.0 <= start[i] < end[i] <= start[i+1] - min_gap_s  AND  (end[i] - start[i]) >= min_dur_s.
    """
    if not segments:
        return []
    ordered = sorted((dict(s) for s in segments), key=lambda x: (float(x.get("start", 0.0)), float(x.get("end", 0.0))))
    out = []
    for s in ordered:
        txt = (s.get("sub_text", s.get("text", "")) or "").strip()
        if not txt:
            continue
        st = round(max(0.0, float(s.get("start", 0.0))), 2)
        et = round(max(st + min_dur_s, float(s.get("end", st + min_dur_s))), 2)
        s["start"], s["end"] = st, et

        if out:
            prev = out[-1]
            if float(prev["end"]) > st - min_gap_s:
                trimmed_prev_end = round(st - min_gap_s, 2)
                if trimmed_prev_end - float(prev["start"]) >= min_dur_s:
                    prev["end"] = trimmed_prev_end
                else:
                    min_prev_end = round(float(prev["start"]) + min_dur_s, 2)
                    prev["end"] = min_prev_end
                    pushed_st = round(min_prev_end + min_gap_s, 2)
                    if et - pushed_st >= min_dur_s:
                        s["start"] = pushed_st
                    else:
                        # Too tight to hold two >=0.15s non-overlapping segments: merge cleanly
                        merged_txt = _merge_segment_texts(prev.get("text", ""), txt)
                        if "text" in prev:
                            prev["text"] = merged_txt
                        if "sub_text" in prev:
                            prev["sub_text"] = _merge_segment_texts(prev.get("sub_text", ""), s.get("sub_text", txt))
                        if "tts_text" in prev:
                            prev["tts_text"] = _merge_segment_texts(prev.get("tts_text", ""), s.get("tts_text", txt))
                        prev["end"] = round(max(float(prev["end"]), et), 2)
                        continue
        out.append(s)

    for idx, s in enumerate(out, 1):
        s["id"] = idx
        s["start"] = round(float(s["start"]), 2)
        s["end"] = round(float(s["end"]), 2)
        assert 0.0 <= s["start"] < s["end"], f"Invalid span on seg #{idx}: {s}"
        assert (s["end"] - s["start"]) >= min_dur_s - 1e-6, f"Runt span on seg #{idx}: {s}"
        if idx > 1:
            assert out[idx - 2]["end"] <= s["start"] - min_gap_s + 1e-6, f"Overlap between #{idx-1} and #{idx}"
    return out


def snap_and_split_segments(segments: list, voice_mask: np.ndarray, max_syl_rate: float = 7.5) -> list:
    """
    1. Deduplicate adjacent boundary fragments and absorb <0.25s micro-fragments/stutters.
    2. Clamp each segment's [start, end] to active acoustic voice clusters (eliminating premature
       silent lead-ins and bloated silent spans).
    3. Enforce strict monotonic non-overlapping invariant:
       0.0 <= start[i] < end[i] <= start[i+1] - 0.02 and (end[i] - start[i]) >= 0.15.
    """
    n_hops = len(voice_mask)
    cleaned = _absorb_duplicate_and_micro_fragments(segments)

    # Acoustic onset/offset clamping per segment
    meta_list = []
    for s in cleaned:
        st, et = float(s["start"]), float(s["end"])
        sub = (s.get("sub_text", s.get("text", "")) or "").strip()
        words = max(1, len(sub.split()))
        min_dur_needed = max(0.25, min(0.55, words / max_syl_rate + 0.02))

        a0 = max(0, int(st / 0.05))
        a1 = min(n_hops - 1, int(et / 0.05)) if n_hops > 0 else -1
        v_times = [round(k * 0.05, 2) for k in range(a0, a1 + 1) if voice_mask[k]] if a1 >= a0 >= 0 else []
        first_v = v_times[0] if v_times else None

        if first_v is not None:
            zh = s.get("zh_text", s.get("text", ""))
            n_zh = max(1, len(cjk_alnum_chars(zh)))
            max_ok_dur = max(min_dur_needed, n_zh * 0.35 + 1.35)

            clusters = []
            for vt in v_times:
                if clusters and (vt - clusters[-1][1]) <= 0.45:
                    clusters[-1][1] = round(vt + 0.05, 2)
                    clusters[-1][2] += 1
                else:
                    clusters.append([vt, round(vt + 0.05, 2), 1])

            while len(clusters) >= 2 and clusters[0][2] <= 2 and (clusters[1][0] - clusters[0][1]) >= 0.50:
                clusters.pop(0)
            while len(clusters) >= 2 and clusters[-1][2] <= 2 and (clusters[-1][0] - clusters[-2][1]) >= 0.50:
                clusters.pop()

            new_st = clusters[0][0]
            new_et = clusters[-1][1]
            if (new_et - new_st) > max_ok_dur:
                best_score = -1
                best_pair = None
                for ci in range(len(clusters)):
                    acc = 0
                    for cj in range(i_start := ci, len(clusters)):
                        if (clusters[cj][1] - clusters[i_start][0]) > max_ok_dur:
                            break
                        acc += clusters[cj][2]
                        if (acc > best_score) or (acc == best_score and best_pair is not None and i_start > best_pair[0]):
                            best_score = acc
                            best_pair = (i_start, cj)
                if best_pair is not None:
                    new_st = clusters[best_pair[0]][0]
                    new_et = clusters[best_pair[1]][1]
                else:
                    best_c = max(clusters, key=lambda c: (c[2], c[0]))
                    w_hops = max(1, int(max_ok_dur / 0.05))
                    ca0 = max(a0, int(round(best_c[0] / 0.05)))
                    ca1 = min(a1, int(round(best_c[1] / 0.05)))
                    if (ca1 - ca0 + 1) > w_hops:
                        best_k = ca1 - w_hops + 1
                        best_cnt = -1
                        for k0 in range(ca0, ca1 - w_hops + 2):
                            cnt = int(voice_mask[k0:k0 + w_hops].sum())
                            if cnt >= best_cnt:
                                best_cnt = cnt
                                best_k = k0
                        active_k = [k for k in range(best_k, best_k + w_hops) if voice_mask[k]]
                        new_st = round((active_k[0] if active_k else best_k) * 0.05, 2)
                        new_et = round(((active_k[-1] + 1) if active_k else (best_k + w_hops)) * 0.05, 2)
                    else:
                        new_st, new_et = best_c[0], best_c[1]
            new_et = max(new_st + min_dur_needed, new_et)
        else:
            new_st, new_et = st, max(st + min_dur_needed, et)

        s["start"] = round(new_st, 2)
        s["end"] = round(new_et, 2)
        meta_list.append((st, et, min_dur_needed, new_st if first_v is not None else None))

    # Expand short segments into silent slack ONLY when slack actually exists (never invert start > end!)
    for idx, s in enumerate(cleaned):
        orig_st, orig_et, min_dur_needed, first_v = meta_list[idx]
        cur_st, cur_et = float(s["start"]), float(s["end"])
        if cur_et - cur_st < min_dur_needed:
            next_st = float(cleaned[idx + 1]["start"]) - 0.02 if idx + 1 < len(cleaned) else 99999.0
            if next_st > cur_st + 0.15:
                cur_et = min(next_st, max(orig_et, cur_st + min_dur_needed))
            if cur_et - cur_st < min_dur_needed:
                prev_et = float(cleaned[idx - 1]["end"]) + 0.02 if idx > 0 else 0.0
                lead_floor = (first_v - 0.25) if first_v is not None else 0.0
                cand_st = max(prev_et, lead_floor, min(orig_st, cur_et - min_dur_needed))
                if cur_et - cand_st >= 0.15:
                    cur_st = cand_st
        # Guard voice onset without breaking invariants
        if n_hops > 0 and cur_et > cur_st + 0.20:
            a0_f = max(0, int(cur_st / 0.05))
            a1_f = min(n_hops - 1, int(cur_et / 0.05))
            vt_f = [round(k * 0.05, 2) for k in range(a0_f, a1_f + 1) if voice_mask[k]]
            if vt_f and (vt_f[0] - cur_st) > 0.28:
                cand_st = round(vt_f[0] - 0.22, 2)
                if cur_et - cand_st >= 0.20:
                    cur_st = cand_st
        s["start"] = round(cur_st, 2)
        s["end"] = round(max(cur_st + 0.15, cur_et), 2)

    cleaned = _dedup_adjacent_segments(cleaned, gap_thresh_s=0.35)
    return enforce_monotonic_segments(cleaned, min_dur_s=0.15, min_gap_s=0.02)


def analyze_waveform_and_clauses(workspace: str, target_segments_path: str = None, snap_in_place: bool = False) -> dict:
    t0 = time.time()
    ws = os.path.abspath(workspace)
    data_dir = os.path.join(ws, "data")
    profile_path = os.path.join(data_dir, "00_video_profile.json")
    with open(profile_path, "r", encoding="utf-8") as f:
        profile = json.load(f)

    src_rel = profile.get("source_video", "source/master_source.mp4")
    video_path = src_rel if os.path.isabs(src_rel) else os.path.join(ws, src_rel)
    if not os.path.exists(video_path):
        video_path = os.path.join(ws, "source", "master_source.mp4")
    if not os.path.exists(video_path):
        video_path = os.path.join(ws, "master_source.mp4")

    vocals_path = os.path.join(ws, "media", "stems", "vocals.wav")
    source_audio_path = os.path.join(ws, "media", "source_audio.wav")
    audio_src = source_audio_path if os.path.exists(source_audio_path) else video_path

    print(f"[PLAN] Analyzing Multi-Band Acoustic Waveform (50ms hop) + Onset Sync in {ws}", flush=True)
    wf_sig = hashlib.sha256(
        f"{WAVEFORM_CACHE_VERSION}|{_fast_file_sig(audio_src)}|{_fast_file_sig(vocals_path)}".encode()
    ).hexdigest()[:16]
    cache_dir = os.path.join(ws, ".cache")
    wf_meta_p = os.path.join(cache_dir, f"waveform_features_{wf_sig}.json")
    wf_mask_p = os.path.join(cache_dir, f"waveform_features_{wf_sig}.npy")

    if os.path.exists(wf_meta_p) and os.path.exists(wf_mask_p):
        with open(wf_meta_p, "r", encoding="utf-8") as cf:
            cmeta = json.load(cf)
        voice_mask = np.load(wf_mask_p).astype(bool)
        merged_bursts = cmeta["merged_bursts"]
        print(f"[CACHE] Waveform features hit ({wf_sig}) -> skipped ffmpeg decode", flush=True)
    else:
        tel_arr = decode_audio_16k(
            audio_src,
            af_filter="highpass=f=280,lowpass=f=3600,dynaudnorm=f=75:g=15:p=0.92:m=12.0",
        )
        raw_arr = decode_audio_16k(audio_src)
        raw_tel_arr = _bandpass_in_ram(raw_arr, 300.0, 3400.0, sr=16000)
        voc_arr = decode_audio_16k(vocals_path) if os.path.exists(vocals_path) else raw_arr

        voice_mask, merged_bursts = compute_voice_mask_and_bursts(
            voc_arr, tel_arr, raw_tel_arr=raw_tel_arr, raw_arr=raw_arr, sr=16000
        )
        os.makedirs(cache_dir, exist_ok=True)
        np.save(wf_mask_p, voice_mask)
        _atomic_write_json(wf_meta_p, {
            "sig": wf_sig,
            "version": WAVEFORM_CACHE_VERSION,
            "merged_bursts": merged_bursts,
        })
    n_hops = len(voice_mask)

    if target_segments_path is None:
        s2_path = os.path.join(data_dir, "02_bilingual_script.json")
        s1_path = os.path.join(data_dir, "01_asr_raw_segments.json")
        target_segments_path = s2_path if os.path.exists(s2_path) else s1_path

    with open(target_segments_path, "r", encoding="utf-8") as f:
        raw_data = json.load(f)
    segs = raw_data["segments"] if isinstance(raw_data, dict) else raw_data

    if snap_in_place:
        segs = snap_and_split_segments(segs, voice_mask)
        if isinstance(raw_data, dict):
            raw_data["segments"] = segs
            raw_data["total_segments"] = len(segs)
            out_data = raw_data
        else:
            out_data = segs
        _atomic_write_json(target_segments_path, out_data)
        print(f"[SNAP & ALIGN] Updated {target_segments_path} -> {len(segs)} monotonic onset-locked clauses.", flush=True)

    ts_violations = find_timestamp_violations(segs, min_dur_s=0.15, min_gap_s=0.02)
    dup_pairs = find_boundary_duplicate_pairs(segs, gap_thresh_s=0.35)

    bloated_segments = []
    premature_lead_segments = []
    for s in segs:
        st, et = float(s["start"]), float(s["end"])
        dur = et - st
        zh = s.get("zh_text", s.get("text", ""))
        sub = s.get("sub_text", s.get("text", ""))
        n_zh = max(1, len(cjk_alnum_chars(zh)))
        if dur > (n_zh * 0.35 + 1.4):
            bloated_segments.append({
                "id": s.get("id"),
                "start": st,
                "end": et,
                "duration": round(dur, 2),
                "zh_chars": n_zh,
                "text": zh[:40],
            })

        if n_hops > 0 and et > st:
            a0 = max(0, int(st / 0.05))
            a1 = min(n_hops - 1, int(et / 0.05))
            v_times = [round(k * 0.05, 2) for k in range(a0, a1 + 1) if voice_mask[k]] if a1 >= a0 else []
            first_v = v_times[0] if v_times else None
            v_lead = round(first_v - st, 2) if first_v is not None else 0.0
            if first_v is not None and v_lead > 0.30:
                premature_lead_segments.append({
                    "id": s.get("id"),
                    "start": st,
                    "end": et,
                    "first_voice": first_v,
                    "voice_lead_s": v_lead,
                    "text": sub[:45],
                })

    pass2b_report_path = os.path.join(data_dir, "01_pass2b_recovery_report.json")
    pass2b_report = {}
    verified_non_speech = []
    if os.path.exists(pass2b_report_path):
        try:
            with open(pass2b_report_path, "r", encoding="utf-8") as pf:
                pass2b_report = json.load(pf)
            verified_non_speech = list(pass2b_report.get("verified_non_speech_gaps", []))
        except Exception:
            pass2b_report = {}
            verified_non_speech = []

    covered_or_verified = np.zeros(n_hops, dtype=bool)
    for s in segs:
        i0 = max(0, int(round(float(s["start"]) / 0.05)))
        i1 = min(n_hops, int(round(float(s["end"]) / 0.05)))
        if i1 > i0:
            covered_or_verified[i0:i1] = True
    for ns in verified_non_speech:
        if ns.get("ambiguous_vocal"):
            continue
        i0 = max(0, int(round(float(ns["start"]) / 0.05)))
        i1 = min(n_hops, int(round(float(ns["end"]) / 0.05)))
        if i1 > i0:
            covered_or_verified[i0:i1] = True

    speech_bursts_covered = 0
    speech_bursts_total = 0
    uncovered_acoustic_bursts = []
    whisper_bursts_count = sum(1 for b in merged_bursts if b.get("kind") == "whisper")
    phone_call_bursts_count = sum(1 for b in merged_bursts if b.get("kind") == "phone_call")
    for b in merged_bursts:
        bst, bet = b["start"], b["end"]
        bdur = bet - bst
        bi0 = max(0, int(round(bst / 0.05)))
        bi1 = min(n_hops, int(round(bet / 0.05)))
        v_hops = int(voice_mask[bi0:bi1].sum()) if bi1 > bi0 else 0
        v_cov_hops = int((voice_mask[bi0:bi1] & covered_or_verified[bi0:bi1]).sum()) if bi1 > bi0 else 0
        v_cov_ratio = (v_cov_hops / v_hops) if v_hops > 0 else 0.0
        ov = sum(max(0.0, min(bet, float(s["end"])) - max(bst, float(s["start"]))) for s in segs)
        ov_ratio = ov / max(0.2, bdur)
        non_speech_ov = sum(
            max(0.0, min(bet, float(ns["end"])) - max(bst, float(ns["start"])))
            for ns in verified_non_speech
            if not ns.get("ambiguous_vocal")
        )
        non_speech_ratio = non_speech_ov / max(0.2, bdur)

        if b["peak_voc_rms"] >= 0.035:
            speech_bursts_total += 1
            if (
                ov_ratio >= 0.40
                or (ov_ratio + non_speech_ratio) >= 0.55
                or (v_hops >= 4 and v_cov_ratio >= 0.65)
                or (v_hops < 4 and (ov_ratio + non_speech_ratio) >= 0.25)
            ):
                speech_bursts_covered += 1
            else:
                uncovered_acoustic_bursts.append({
                    **b,
                    "coverage_ratio": round(ov_ratio, 3),
                    "probed_non_speech_ratio": round(non_speech_ratio, 3),
                    "vocal_hop_coverage": round(v_cov_ratio, 3),
                })
        elif b.get("kind") in ("whisper", "phone_call"):
            if bdur < 1.0 and b["peak_voc_rms"] < 0.025:
                continue
            speech_bursts_total += 1
            if (
                ov_ratio >= 0.35
                or (ov_ratio + non_speech_ratio) >= 0.45
                or (v_hops == 0 and bool(verified_non_speech))
                or not verified_non_speech
            ):
                speech_bursts_covered += 1
            else:
                uncovered_acoustic_bursts.append({
                    **b,
                    "coverage_ratio": round(ov_ratio, 3),
                    "probed_non_speech_ratio": round(non_speech_ratio, 3),
                })

    unprobed_vocal = voice_mask & (~covered_or_verified)
    longest_gap_hops = 0
    cur_run = 0
    gap_tol = 0
    for k in range(n_hops):
        if unprobed_vocal[k]:
            cur_run += 1 + gap_tol
            gap_tol = 0
            if cur_run > longest_gap_hops:
                longest_gap_hops = cur_run
        elif cur_run > 0 and (not covered_or_verified[k]) and gap_tol < 8:
            gap_tol += 1
        else:
            cur_run = 0
            gap_tol = 0
    longest_uncovered_vocal_gap_s = round(longest_gap_hops * 0.05, 2)

    ac_ratio = (speech_bursts_covered / speech_bursts_total) if speech_bursts_total else 1.0
    seg_durs = [max(0.0, float(s["end"]) - float(s["start"])) for s in segs]
    total_dur_min = max(0.01, float(profile.get("duration", n_hops * 0.05)) / 60.0)
    active_voice_pct = round(100.0 * float(voice_mask.mean()), 2) if n_hops else 0.0

    multiband_summary = {
        "gaps_probed": int(pass2b_report.get("gaps_probed", 0)),
        "recovered_total": int(pass2b_report.get("recovered_total", 0)),
        "recovered_by_band": pass2b_report.get("recovered_by_band", {}),
        "dedup_rejected": int(pass2b_report.get("dedup_rejected", 0)),
        "verified_non_speech_gaps_count": len(verified_non_speech),
    }

    report = {
        "analyzed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "version": WAVEFORM_CACHE_VERSION,
        "target_segments_file": os.path.basename(target_segments_path),
        "total_segments_checked": len(segs),
        "clause_cadence": {
            "total_clauses": len(segs),
            "clauses_per_min": round(len(segs) / total_dur_min, 2),
            "median_clause_dur_s": round(float(np.median(seg_durs)), 2) if seg_durs else 0.0,
            "p90_clause_dur_s": round(float(np.percentile(seg_durs, 90)), 2) if seg_durs else 0.0,
            "short_clauses_lt1s": sum(1 for d in seg_durs if d < 1.0),
        },
        "timestamp_violations_count": len(ts_violations),
        "timestamp_violations": ts_violations[:25],
        "boundary_duplicates_count": len(dup_pairs),
        "boundary_duplicates": dup_pairs[:25],
        "bloated_segments_count": len(bloated_segments),
        "bloated_segments": bloated_segments[:20],
        "premature_lead_segments_count": len(premature_lead_segments),
        "premature_lead_segments": premature_lead_segments[:20],
        "acoustic_voice_bursts_count": len(merged_bursts),
        "whisper_bursts_count": whisper_bursts_count,
        "phone_call_bursts_count": phone_call_bursts_count,
        "active_voice_pct": active_voice_pct,
        "acoustic_speech_coverage_ratio": round(ac_ratio, 4),
        "uncovered_acoustic_bursts_count": len(uncovered_acoustic_bursts),
        "uncovered_acoustic_bursts": uncovered_acoustic_bursts[:25],
        "longest_uncovered_vocal_gap_s": longest_uncovered_vocal_gap_s,
        "multiband_pass2b_summary": multiband_summary,
    }

    out_audit = os.path.join(data_dir, "01_waveform_voice_audit.json")
    _atomic_write_json(out_audit, report)

    print(
        f"[DONE] Acoustic Waveform Audit: clauses={len(segs)} ({report['clause_cadence']['clauses_per_min']}/min), "
        f"whispers={whisper_bursts_count}, phone_calls={phone_call_bursts_count}, "
        f"uncovered_acoustic={len(uncovered_acoustic_bursts)}, max_unprobed_vocal_gap={longest_uncovered_vocal_gap_s}s, "
        f"pass2b_rec={multiband_summary['recovered_total']}, "
        f"ts_violations={len(ts_violations)}, boundary_dups={len(dup_pairs)}, "
        f"bloated={len(bloated_segments)}, premature_lead={len(premature_lead_segments)} "
        f"-> {out_audit} in {time.time()-t0:.2f}s",
        flush=True,
    )
    return report


def main():
    parser = argparse.ArgumentParser(description="Run Multi-Band Acoustic Waveform & Monotonic Onset Audit.")
    parser.add_argument("--workspace", required=True, help="Workspace path (workspace/<slug>).")
    parser.add_argument("--segments", required=False, help="Optional path to segments JSON to audit.")
    parser.add_argument("--snap-and-split", action="store_true", help="Absorb <0.25s micro-fragments, deduplicate echoes, snap to voice onset, and enforce strict monotonic timing.")
    args = parser.parse_args()
    analyze_waveform_and_clauses(args.workspace, args.segments, snap_in_place=args.snap_and_split)


if __name__ == "__main__":
    main()
