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


WAVEFORM_CACHE_VERSION = "wf-3.0"


def _fast_file_sig(path: str) -> str:
    if not path or not os.path.exists(path):
        return "missing"
    st = os.stat(path)
    return f"{os.path.abspath(path)}:{st.st_size}:{st.st_mtime_ns}"


def compute_audio_spectral_profile(audio_arr: np.ndarray, sr: int = 16000) -> dict:
    """Compute active-frame 5-band frequency share (respecting Nyquist sr/2), centroid, RMS/Peak/Crest, and HP hint."""
    x = np.asarray(audio_arr, dtype=np.float32)
    if x.size == 0:
        return {}
    peak = float(np.max(np.abs(x)) + 1e-12)
    peak_db = round(20.0 * np.log10(peak), 2)

    win = min(len(x), 4096)
    n_blocks = max(1, len(x) // win)
    blk = x[: n_blocks * win].reshape(n_blocks, win)
    b_rms = np.sqrt(np.mean(blk ** 2, axis=1) + 1e-15)
    act = b_rms > max(10.0 ** (-55.0 / 20.0), peak * 1e-3)
    active_blk = blk[act] if act.any() else blk
    rms = float(np.sqrt(np.mean(active_blk ** 2) + 1e-12))
    rms_db = round(20.0 * np.log10(rms), 2)
    crest_db = round(peak_db - rms_db, 2)

    hann = np.hanning(win).astype(np.float32)[None, :]
    spec = np.fft.rfft(active_blk * hann, axis=1)
    mag2 = (spec.real ** 2 + spec.imag ** 2).mean(axis=0)
    freqs = np.fft.rfftfreq(win, 1.0 / sr)
    tot = float(mag2.sum() + 1e-15)
    nyq = sr / 2.0

    def band_pct(lo, hi):
        m = (freqs >= lo) & (freqs < min(hi, nyq + 1.0))
        return round(100.0 * float(mag2[m].sum()) / tot, 2) if m.any() else 0.0

    sub_pct = band_pct(0, 120)
    low_pct = band_pct(120, 300)
    speech_pct = band_pct(300, 3400)
    pres_pct = band_pct(3400, 6500)
    high_pct = band_pct(6500, nyq + 1.0)
    centroid_hz = round(float((freqs * mag2).sum() / tot), 1)
    rec_hp = 110 if sub_pct > 18.0 else (95 if sub_pct > 10.0 else 85)

    return {
        "sample_rate_hz": sr,
        "nyquist_hz": int(nyq),
        "waveform": {
            "rms_dbfs": rms_db,
            "peak_dbfs": peak_db,
            "crest_factor_db": crest_db,
            "active_block_ratio": round(float(act.mean()), 3),
        },
        "frequency_bands_pct": {
            "sub_lt120hz": sub_pct,
            "low_mid_120_300hz": low_pct,
            "speech_300_3400hz": speech_pct,
            "presence_3400_6500hz": pres_pct,
            "high_6500_nyquist": high_pct,
        },
        "spectral_centroid_hz": centroid_hz,
        "recommended_hp_hz": rec_hp,
        "lp_note": "Full-band LP cutoff is analyzed at 48 kHz in Stage 3 (aicn_audio.py analyze).",
    }


def _bandpass_in_ram(x: np.ndarray, lo_hz: float, hi_hz: float, sr: int = 16000, order: int = 4) -> np.ndarray:
    """Fast zero-disk 4th-order Butterworth SOS bandpass filter in RAM (-24 dB/oct isolation, zero ffmpeg spawns)."""
    from scipy.signal import butter, sosfilt
    nyq = 0.5 * sr
    sos = butter(order, [max(10.0, lo_hz) / nyq, min(nyq * 0.95, hi_hz) / nyq], btype="band", output="sos")
    return sosfilt(sos, np.asarray(x, dtype=np.float32)).astype(np.float32)


def _local_rms_norm(x: np.ndarray, sr: int = 16000, win_s: float = 0.10, floor: float = 0.035) -> np.ndarray:
    """Fast in-RAM dynamic gain normalization (replaces slow ffmpeg dynaudnorm passes)."""
    arr = np.asarray(x, dtype=np.float32)
    if arr.size == 0:
        return arr
    win_n = max(16, int(round(win_s * sr)))
    env = np.sqrt(np.convolve(arr ** 2, np.ones(win_n, dtype=np.float32) / win_n, mode="same") + 1e-8)
    return np.clip(arr / np.maximum(env * 3.0, floor), -0.95, 0.95).astype(np.float32)


def build_multiband_audio_streams(voc_arr: np.ndarray, raw_arr: np.ndarray, sr: int = 16000) -> dict:
    """
    Decompose vocals + raw full-mix audio into 4 speech-targeted frequency bands + clean raw reference:
      - full_vocal (120-7500 Hz, 70% voc + 30% raw): clean full-spectrum speech (VAD recovery)
      - high       (900-7500 Hz, 55% voc + 45% raw): child/cartoon high-pitch voices, bright female voices, breathy whispers
      - mid        (300-3400 Hz, 35% voc + 65% raw): core speech formants, phone/radio/PA comms (immune to Demucs phone loss)
      - low        ( 85-1400 Hz, 75% voc + 25% raw): deep male voices & muffled speech drowned by high-frequency SFX
      - raw_clean  (100-7500 Hz, 100% raw):          reverse cross-check stream when Demucs strips a voice into accomp
    Returns both raw band waveforms (`<band>_raw`) for energy measurement and locally-normalized (`<band>`) for ASR.
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
    Gap-First Multi-Band Uncovered Speech Scanner (independent of hardsubs):
      1. Masks out timeline regions ALREADY covered by `existing_segments` FIRST (eliminating giant-blob swallowing).
      2. Scans EVERY uncovered 50ms hop across 4 frequency bands (full_vocal, high, mid, low) with adaptive per-band
         noise floors and syllable modulation checks — eliminating the old 0.024-0.080 RMS dead zone.
      3. Automatically splits any contiguous uncovered speech region longer than `max_window_s` (default 10s) at its
         lowest-energy valley so Whisper never suffers 30s window drift.
      4. Annotates each gap with per-band normalized dominance scores and recommended probe order.
    """
    if streams is None:
        streams = build_multiband_audio_streams(voc_arr, raw_arr, sr=sr)

    hop = int(0.05 * sr)
    n_samples = min(len(streams["voc_raw"]), len(streams["raw_raw"]), len(streams["full_vocal_raw"]))
    n_hops = n_samples // hop
    if n_hops <= 0:
        return []

    # 1. Build covered mask from existing ASR segments (inset by 0.05s so adjacent missed speech stays exposed)
    covered = np.zeros(n_hops, dtype=bool)
    for s in existing_segments or []:
        st = float(s.get("start", 0.0)) + 0.05
        et = float(s.get("end", 0.0)) - 0.05
        if et <= st:
            st = float(s.get("start", 0.0))
            et = float(s.get("end", 0.0))
        i0 = max(0, int(round(st / 0.05)))
        i1 = min(n_hops, int(round(et / 0.05)))
        if i1 > i0:
            covered[i0:i1] = True

    # 2. Per-band 50ms RMS, ZCR, and local syllable modulation CV
    voc_rms, voc_zcr = _hop_rms_and_zcr(streams["voc_raw"], n_hops, hop)
    full_rms, full_zcr = _hop_rms_and_zcr(streams["full_vocal_raw"], n_hops, hop)
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
    full_mod = _sliding_mod_cv(full_rms, 10)
    hi_mod = _sliding_mod_cv(hi_rms, 10)
    mid_mod = _sliding_mod_cv(mid_rms, 10)
    lo_mod = _sliding_mod_cv(lo_rms, 10)

    def _floor(arr, lo_cap, hi_cap):
        return max(lo_cap, min(hi_cap, float(np.percentile(arr, 15))))

    voc_fl = _floor(voc_rms, 0.0020, 0.0070)
    hi_fl = _floor(hi_rms, 0.0015, 0.0065)
    mid_fl = _floor(mid_rms, 0.0025, 0.0100)
    lo_fl = _floor(lo_rms, 0.0025, 0.0100)

    # Active speech masks across all 4 bands (zero dead zone between whisper and loud speech!)
    is_full = (voc_rms > max(0.0075, voc_fl * 2.1)) & (voc_zcr >= 0.015) & (voc_zcr <= 0.45) & (
        (voc_rms > max(0.018, voc_fl * 3.2)) | (voc_mod >= 0.14)
    )
    # High band: child/cartoon voices (high pitch & ZCR up to 0.62) + breathy whispers
    is_high = (
        (hi_rms > max(0.0050, hi_fl * 2.2))
        & (hi_zcr >= 0.04)
        & (hi_zcr <= 0.62)
        & (hi_mod >= 0.15)
        & ((voc_hi_rms > max(0.0030, hi_fl * 1.4)) | (hi_rms > max(0.012, hi_fl * 3.5)))
    )
    # Mid band: telephony/radio/PA (300-3400 Hz roll-off above 3.6 kHz) or strong syllable-modulated mid-band speech
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
    # Low band: deep male voices / muffled speech where vocals stem has strong low-band speech modulation
    is_low = (
        (lo_rms > max(0.0080, lo_fl * 2.2))
        & (voc_lo_rms > max(0.0060, lo_fl * 1.8))
        & (lo_zcr >= 0.01)
        & (lo_zcr <= 0.28)
        & (lo_mod >= 0.15)
    )

    uncovered_active = (~covered) & (is_full | is_high | is_mid | is_low)

    # 3. Group uncovered active hops across <= 0.25s syllable pauses (never crossing covered hops!)
    raw_spans = []
    i = 0
    while i < n_hops:
        if uncovered_active[i]:
            j = i + 1
            while j < n_hops:
                if uncovered_active[j]:
                    j += 1
                    continue
                # Bridge up to 5 hops (0.25s) of silence ONLY if none of those hops are covered
                bridge = 0
                while (j + bridge) < n_hops and bridge < 5 and (not uncovered_active[j + bridge]) and (not covered[j + bridge]):
                    bridge += 1
                if bridge < 5 and (j + bridge) < n_hops and uncovered_active[j + bridge]:
                    j = j + bridge + 1
                else:
                    break
            raw_spans.append((i, j))
            i = j
        else:
            i += 1

    # 4. Split any long uncovered span (> max_window_s) at lowest-energy valleys so Whisper stays in crisp short windows
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

    # 5. Filter steady sustained instruments/BGM and score dominant frequency bands per gap
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

        # Reject steady unmodulated hum / sustained solo instrument unless vocals stem is clearly loud speech
        if best_mod < 0.14 and p_voc < 0.035:
            continue

        # Normalized band SNR above floor to rank probe priority (full_vocal, high, mid, low)
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
        active_bands = [b for b in ("high", "mid", "low") if band_hits[b] >= 2 or band_snr[b] >= 1.8]
        active_bands.sort(key=lambda b: (band_hits[b], band_snr[b]), reverse=True)
        if not active_bands:
            active_bands = sorted(("high", "mid", "low"), key=lambda b: band_snr[b], reverse=True)[:2]

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
    """
    Detect:
      - `voice_mask`: clean primary speech mask (for onset clamping without BGM/instrument false positives)
      - `merged_bursts`: normal, adaptive-floor `whisper`, and modulation-validated `phone_call` bursts
    """
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

    # Adaptive noise floor & speech reference so detection scales across quiet (-24 LUFS) and loud (-11 LUFS) dramas
    voc_floor = max(0.002, min(0.006, float(np.percentile(v_rms_arr, 15))))
    nz_v = v_rms_arr[v_rms_arr > voc_floor * 1.2]
    voc_ref = max(0.020, float(np.percentile(nz_v, 85))) if nz_v.size >= 10 else 0.035
    normal_thresh = max(0.016, min(0.024, voc_floor * 3.0))
    whisper_lo = max(0.0040, voc_floor * 1.8)
    whisper_hi = max(normal_thresh, min(0.022, voc_ref * 0.38))

    if raw_tel_arr is not None and raw_arr is not None:
        rt_blks = raw_tel_arr[: n_hops * hop].reshape(n_hops, hop)
        rt_rms_arr = np.sqrt(np.mean(rt_blks ** 2, axis=1) + 1e-12)
        # Upper-band (>3.5 kHz) energy on raw audio: real telephone lines roll off sharply above 3.4 kHz
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
        b_st = i0 * 0.05
        b_et = i1 * 0.05
        raw_bursts.append({
            "start": round(b_st, 2),
            "end": round(b_et, 2),
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
            # Works even with bass BGM present because it compares 300-3400Hz telephony vs >3.6kHz roll-off
            is_phone = (
                rt_rms > max(0.004, voc_floor * 1.8)
                and rt_rms > v_rms * 1.35
                and tel_vs_hi > 2.2
                and 0.02 <= t_zcr <= 0.36
            )
        else:
            is_phone = (t_rms > 0.12 and v_rms > 0.006 and 0.02 <= zcr <= 0.35)

        # Keep voice_mask clean for onset clamping (normal speech + confirmed vocals, not ambient hiss/instruments)
        voice_mask[i] = is_normal or (t_rms > 0.14 and v_rms > max(0.008, voc_floor * 2.5) and 0.02 <= zcr <= 0.32)

        is_candidate = is_normal or is_whisper or is_phone
        if is_candidate:
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
        else:
            if in_burst:
                _finalize_burst(b_i0, i, b_peak_voc, b_peak_tel, b_phone_hits, b_whisper_hits, b_normal_hits)
                in_burst = False

    if in_burst:
        _finalize_burst(b_i0, n_hops, b_peak_voc, b_peak_tel, b_phone_hits, b_whisper_hits, b_normal_hits)

    # Merge syllable-level micro-bursts across <=0.22s pauses BEFORE applying duration & modulation checks
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
        # Reject steady hum/sustained solo instruments using syllable envelope modulation CV across the full burst
        if n_ph > n_nm and n_ph >= 3:
            env = rt_rms_arr[i0:i1] if has_raw_tel else t_rms_arr[i0:i1]
            mod_cv = float(np.std(env) / (np.mean(env) + 1e-6))
            if mod_cv < 0.16:
                continue
            kind = "phone_call"
        elif n_wh >= 3 and n_nm == 0:
            env = v_rms_arr[i0:i1]
            mod_cv = float(np.std(env) / (np.mean(env) + 1e-6))
            if mod_cv < 0.14:
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
            zh = s.get("zh_text", s.get("text", ""))
            n_zh = max(1, len([c for c in zh if "\u4e00" <= c <= "\u9fff" or c.isalnum()]))
            max_ok_dur = max(min_dur_needed, n_zh * 0.35 + 1.35)

            clusters = []
            for vt in v_times:
                if clusters and (vt - clusters[-1][1]) <= 0.45:
                    clusters[-1][1] = round(vt + 0.05, 2)
                    clusters[-1][2] += 1
                else:
                    clusters.append([vt, round(vt + 0.05, 2), 1])

            # Trim isolated tiny noise ticks (<=2 hops) separated by >=0.50s gap when stronger clusters exist
            while len(clusters) >= 2 and clusters[0][2] <= 2 and (clusters[1][0] - clusters[0][1]) >= 0.50:
                clusters.pop(0)
            while len(clusters) >= 2 and clusters[-1][2] <= 2 and (clusters[-1][0] - clusters[-2][1]) >= 0.50:
                clusters.pop()

            new_st = clusters[0][0]
            new_et = clusters[-1][1]
            if (new_et - new_st) > max_ok_dur:
                # Choose the contiguous cluster range fitting <= max_ok_dur with maximum active voice hops
                # (preferring later anchor on tie since Whisper pads segment start back over preceding silence).
                best_score = -1
                best_pair = None
                for i in range(len(clusters)):
                    acc = 0
                    for j in range(i, len(clusters)):
                        if (clusters[j][1] - clusters[i][0]) > max_ok_dur:
                            break
                        acc += clusters[j][2]
                        if (acc > best_score) or (acc == best_score and best_pair is not None and i > best_pair[0]):
                            best_score = acc
                            best_pair = (i, j)
                if best_pair is not None:
                    new_st = clusters[best_pair[0]][0]
                    new_et = clusters[best_pair[1]][1]
                else:
                    # Single long cluster exceeding max_ok_dur: tighten to highest-density window of width max_ok_dur
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
            new_st, new_et = st, et

        s["start"] = round(new_st, 2)
        s["end"] = round(new_et, 2)
        s["_snap_meta"] = (st, et, min_dur_needed, (new_st if first_v is not None else None), bool(dom_ivs))

    # Second pass after all segment starts have snapped forward: pad short segments cleanly without premature lead
    for idx, s in enumerate(expanded):
        orig_st, orig_et, min_dur_needed, first_v, has_dom = s.pop("_snap_meta")
        new_st, new_et = float(s["start"]), float(s["end"])
        if new_et - new_st < min_dur_needed:
            next_st = float(expanded[idx + 1]["start"]) - 0.02 if idx + 1 < len(expanded) else 99999.0
            new_et = min(next_st, max(orig_et, new_st + min_dur_needed))
            if new_et - new_st < min_dur_needed:
                prev_et = float(expanded[idx - 1]["end"]) + 0.02 if idx > 0 else 0.0
                lead_floor = (first_v - 0.25) if (not has_dom and first_v is not None) else 0.0
                new_st = max(prev_et, lead_floor, min(orig_st, new_et - min_dur_needed))
        s["start"] = round(new_st, 2)
        s["end"] = round(new_et, 2)

    for i in range(len(expanded) - 1):
        if expanded[i]["end"] > expanded[i + 1]["start"] - 0.02:
            expanded[i]["end"] = round(max(expanded[i]["start"] + 0.35, expanded[i + 1]["start"] - 0.02), 2)
            # Re-check post-clamp premature lead if clamping shortened the end
            if expanded[i]["end"] - expanded[i]["start"] < 0.05:
                expanded[i]["end"] = round(expanded[i]["start"] + 0.05, 2)

    # Final safety guard: when padding/clamping pulls an utterance over a newly reached voice onset in v2.0.0 mode,
    # clamp start to stay within 0.25s of actual voice onset so premature_lead == 0 on first pass.
    if not sub_clauses:
        for s in expanded:
            st_f, et_f = float(s["start"]), float(s["end"])
            a0_f = max(0, int(st_f / 0.05))
            a1_f = min(n_hops - 1, int(et_f / 0.05))
            vt_f = [round(k * 0.05, 2) for k in range(a0_f, a1_f + 1) if voice_mask[k]]
            if vt_f and (vt_f[0] - st_f) > 0.28 and (et_f - (vt_f[0] - 0.22)) >= 0.20:
                s["start"] = round(vt_f[0] - 0.22, 2)

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
    import hashlib as _hashlib
    wf_sig = _hashlib.sha256(
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
        spectral_summary = cmeta["spectral_summary"]
        vocal_spectral_summary = cmeta["vocal_spectral_summary"]
        print(f"[CACHE] Waveform features hit ({wf_sig}) -> skipped ffmpeg decode", flush=True)
    else:
        tel_arr = decode_audio_16k(
            audio_src,
            af_filter="highpass=f=280,lowpass=f=3600,dynaudnorm=f=75:g=15:p=0.92:m=12.0",
        )
        raw_arr = decode_audio_16k(audio_src)
        raw_tel_arr = _bandpass_in_ram(raw_arr, 300.0, 3400.0, sr=16000)
        if os.path.exists(vocals_path):
            voc_arr = decode_audio_16k(vocals_path)
        else:
            voc_arr = raw_arr

        voice_mask, merged_bursts = compute_voice_mask_and_bursts(
            voc_arr, tel_arr, raw_tel_arr=raw_tel_arr, raw_arr=raw_arr, sr=16000
        )
        spectral_summary = compute_audio_spectral_profile(raw_arr, sr=16000)
        vocal_spectral_summary = compute_audio_spectral_profile(voc_arr, sr=16000) if os.path.exists(vocals_path) else {}
        os.makedirs(cache_dir, exist_ok=True)
        np.save(wf_mask_p, voice_mask)
        with open(wf_meta_p, "w", encoding="utf-8") as cf:
            json.dump({
                "sig": wf_sig,
                "version": WAVEFORM_CACHE_VERSION,
                "merged_bursts": merged_bursts,
                "spectral_summary": spectral_summary,
                "vocal_spectral_summary": vocal_spectral_summary,
            }, cf)
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

    # 3. Check acoustic voice bursts overlapping visual sub-clauses + multi-band Pass 2B recovery audit
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

    speech_bursts_covered = 0
    speech_bursts_total = 0
    uncovered_acoustic_bursts = []
    whisper_bursts_count = sum(1 for b in merged_bursts if b.get("kind") == "whisper")
    phone_call_bursts_count = sum(1 for b in merged_bursts if b.get("kind") == "phone_call")
    for b in merged_bursts:
        bst, bet = b["start"], b["end"]
        bdur = bet - bst
        has_sub_clause = any(max(0.0, min(bet, sc["end"]) - max(bst, sc["start"])) >= 0.25 for sc in sub_clauses)
        ov = sum(max(0.0, min(bet, float(s["end"])) - max(bst, float(s["start"]))) for s in segs)
        ov_ratio = ov / max(0.2, bdur)
        non_speech_ov = sum(
            max(0.0, min(bet, float(ns["end"])) - max(bst, float(ns["start"])))
            for ns in verified_non_speech
        )
        non_speech_ratio = non_speech_ov / max(0.2, bdur)
        # Smart Acoustic Gate (v2.5.0): never blindly mark unprobed strong vocal bursts as covered when sub_clauses is empty.
        # Pure BGM/SFX bursts explicitly probed across High/Mid/Low/Full bands by Pass 2B (`verified_non_speech_gaps`)
        # do not deadlock Stage 1 (preserving L-015), while unprobed strong vocal gaps are caught immediately.
        if has_sub_clause:
            speech_bursts_total += 1
            if ov_ratio >= 0.40:
                speech_bursts_covered += 1
            else:
                uncovered_acoustic_bursts.append({**b, "coverage_ratio": round(ov_ratio, 3)})
        elif b["peak_voc_rms"] >= 0.035:
            speech_bursts_total += 1
            if ov_ratio >= 0.40 or (ov_ratio + non_speech_ratio) >= 0.60:
                speech_bursts_covered += 1
            else:
                uncovered_acoustic_bursts.append({
                    **b,
                    "coverage_ratio": round(ov_ratio, 3),
                    "probed_non_speech_ratio": round(non_speech_ratio, 3),
                })
        elif b.get("kind") in ("whisper", "phone_call"):
            speech_bursts_total += 1
            if ov_ratio >= 0.40 or non_speech_ratio >= 0.50 or not verified_non_speech:
                speech_bursts_covered += 1
            else:
                uncovered_acoustic_bursts.append({
                    **b,
                    "coverage_ratio": round(ov_ratio, 3),
                    "probed_non_speech_ratio": round(non_speech_ratio, 3),
                })

    # Compute longest contiguous unprobed active-vocal gap (catches 20-90s Whisper stalls immediately)
    covered_or_verified = np.zeros(n_hops, dtype=bool)
    for s in segs:
        i0 = max(0, int(round(float(s["start"]) / 0.05)))
        i1 = min(n_hops, int(round(float(s["end"]) / 0.05)))
        if i1 > i0:
            covered_or_verified[i0:i1] = True
    for ns in verified_non_speech:
        i0 = max(0, int(round(float(ns["start"]) / 0.05)))
        i1 = min(n_hops, int(round(float(ns["end"]) / 0.05)))
        if i1 > i0:
            covered_or_verified[i0:i1] = True
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

    sc_ratio = (covered_cnt / len(sub_clauses)) if sub_clauses else 1.0
    ac_ratio = (speech_bursts_covered / speech_bursts_total) if speech_bursts_total else 1.0
    seg_durs = [max(0.0, float(s["end"]) - float(s["start"])) for s in segs]
    total_dur_min = max(0.01, float(profile.get("duration", n_hops * 0.05)) / 60.0)
    active_voice_pct = round(100.0 * float(voice_mask.mean()), 2) if n_hops else 0.0

    multiband_summary = {
        "gaps_probed": int(pass2b_report.get("gaps_probed", 0)),
        "recovered_total": int(pass2b_report.get("recovered_total", 0)),
        "recovered_by_band": pass2b_report.get("recovered_by_band", {}),
        "verified_non_speech_gaps_count": len(verified_non_speech),
    }

    report = {
        "analyzed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "target_segments_file": os.path.basename(target_segments_path),
        "total_segments_checked": len(segs),
        "clause_cadence": {
            "total_clauses": len(segs),
            "clauses_per_min": round(len(segs) / total_dur_min, 2),
            "median_clause_dur_s": round(float(np.median(seg_durs)), 2) if seg_durs else 0.0,
            "p90_clause_dur_s": round(float(np.percentile(seg_durs, 90)), 2) if seg_durs else 0.0,
            "short_clauses_lt1s": sum(1 for d in seg_durs if d < 1.0),
        },
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
        "whisper_bursts_count": whisper_bursts_count,
        "phone_call_bursts_count": phone_call_bursts_count,
        "active_voice_pct": active_voice_pct,
        "acoustic_speech_coverage_ratio": round(ac_ratio, 4),
        "uncovered_acoustic_bursts_count": len(uncovered_acoustic_bursts),
        "uncovered_acoustic_bursts": uncovered_acoustic_bursts[:25],
        "longest_uncovered_vocal_gap_s": longest_uncovered_vocal_gap_s,
        "multiband_pass2b_summary": multiband_summary,
        "source_spectral_profile": spectral_summary,
        "vocals_spectral_profile": vocal_spectral_summary,
    }

    out_audit = os.path.join(data_dir, "01_waveform_voice_audit.json")
    with open(out_audit, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(
        f"[DONE] Waveform & Spectral Audit: clauses={len(segs)} ({report['clause_cadence']['clauses_per_min']}/min), "
        f"sub_clauses={covered_cnt}/{len(sub_clauses)} ({sc_ratio*100:.1f}%), "
        f"whispers={whisper_bursts_count}, phone_calls={phone_call_bursts_count}, "
        f"uncovered_acoustic={len(uncovered_acoustic_bursts)}, max_unprobed_vocal_gap={longest_uncovered_vocal_gap_s}s, "
        f"pass2b_rec={multiband_summary['recovered_total']}, "
        f"HP/LP_hint={spectral_summary.get('recommended_hp_hz')}/{spectral_summary.get('recommended_lp_hz')}Hz, "
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
