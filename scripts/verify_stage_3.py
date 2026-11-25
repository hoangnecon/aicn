#!/usr/bin/env python3
"""
Stage 3 CI/CD Gate Validator (verify_stage_3.py) for AICN v3.0.0 (`voice_dub` only).
Requires Stage 2 gate PASS (.cicd/stage_2_gate.json).
Validates 4 weighted checks (100/100 required to sign .cicd/stage_3_gate.json):
  1. Audio gate report (`03_mix_report.json` passed, loudness -14+-1 LUFS, peak <= -0.8 dBFS,
     active-speech dub SNR median >= 10 dB & p10 >= 4 dB, original audio present) (20 pts)
  2. Indivisible utterance WAVs (`media/tts_segments/utt_NNNN.wav` present, 48kHz, >= 0.12s, non-silent,
     0 overflows, 0 tempo violations) (20 pts)
  3. Zero utterance overlaps, sync drift in [-0.10, +0.50]s, monotonic aligned slots (30 pts)
  4. Master stereo 48kHz audio (`media/master_audio_48k.wav`), N-block SRT, and Stage 3 report (30 pts)
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
from cicd_gate import require_gate, write_gate_token


def run_stage_3_verification(ws: str):
    ws = os.path.abspath(ws)
    require_gate(ws, 2)

    with open(os.path.join(ws, "data", "00_video_profile.json"), "r", encoding="utf-8") as f:
        profile = json.load(f)
    vid_dur = float(profile.get("duration", 0.0))
    loc_mode = profile.get("localization_mode", "voice_dub")
    if loc_mode == "sub_only":
        raise SystemExit("[Stage 3 Gate] sub_only mode is retired in AICN v3.0.0; full voice_dub is mandatory.")

    with open(os.path.join(ws, "data", "02_bilingual_script.json"), "r", encoding="utf-8") as f:
        script_segs = json.load(f)

    mix_rep_path = os.path.join(ws, "data", "03_mix_report.json")
    dub_sent_path = os.path.join(ws, "data", "03_dub_sentences.json")
    aligned_path = os.path.join(ws, "data", "03_aligned_subtitles.json")
    master_wav = os.path.join(ws, "media", "master_audio_48k.wav")
    srt_path = os.path.join(ws, "data", "subtitles_full.srt")

    # Check 1: Audio mix & loudness/SNR gate report
    mix_ok = False
    mix_detail = "Missing data/03_mix_report.json"
    if os.path.exists(mix_rep_path):
        with open(mix_rep_path, "r", encoding="utf-8") as f:
            mix_rep = json.load(f)
        cmap = {ck["id"]: ck for ck in mix_rep.get("checks", [])}
        req_ids = ("loudness", "dub_intelligible", "original_present")
        req_ok = all(cmap.get(k, {}).get("passed", False) for k in req_ids)
        mix_ok = bool(mix_rep.get("passed")) and req_ok
        mix_detail = f"passed={mix_rep.get('passed')}, loudness={cmap.get('loudness', {}).get('detail')}, snr={cmap.get('dub_intelligible', {}).get('detail')}"

    # Check 2: Indivisible utterance WAVs & pacing/overflow
    utts = []
    utt_bad = []
    over_bad = []
    tempos = []
    if os.path.exists(dub_sent_path):
        with open(dub_sent_path, "r", encoding="utf-8") as f:
            utts = json.load(f)
    for idx_u, u in enumerate(utts):
        rel = u.get("utterance_wav") or f"media/tts_segments/utt_{int(u.get('utterance_id', idx_u)):04d}.wav"
        up = os.path.join(ws, rel)
        if not os.path.exists(up):
            utt_bad.append(f"{rel}:missing")
        else:
            info_u = sf.info(up)
            arr_u = sf.read(up, dtype="float32")[0]
            pk_u = float(np.max(np.abs(arr_u))) if len(arr_u) else 0.0
            if info_u.samplerate != 48000 or info_u.duration < 0.12 or pk_u <= 0.005:
                utt_bad.append(f"{rel}:sr={info_u.samplerate},dur={info_u.duration:.3f}s,pk={pk_u:.4f}")
        cap_t = float(u.get("cap_tempo", 1.28))
        t_val = float(u.get("tempo", 1.0))
        req_t = max(t_val, float(u.get("required_tempo", t_val)))
        tempos.append(t_val)
        if bool(u.get("runt", False)) or float(u.get("overflow_s", 0.0)) > 0.05 or req_t > cap_t + 0.005:
            over_bad.append(f"utt#{idx_u}:runt={u.get('runt', False)},ov={u.get('overflow_s')}s,req_t={req_t}>{cap_t}")
    chk2_ok = len(utts) > 0 and len(utt_bad) == 0 and len(over_bad) == 0
    mean_tempo = float(np.mean(tempos)) if tempos else 0.0
    max_tempo = float(np.max(tempos)) if tempos else 0.0
    chk2_detail = f"utterances={len(utts)}, mean_tempo={mean_tempo:.3f}x, max_tempo={max_tempo:.3f}x, bad_wavs={utt_bad[:3]}, overflows={over_bad[:3]}"

    # Check 3: Zero utterance overlap, sync drift in [-0.10, +0.50]s, monotonic aligned slots
    drift_errors = []
    speech_drift_errors = []
    slot_overlap_errors = []
    utt_overlap_errors = []
    aligned_segs = []
    if os.path.exists(aligned_path):
        with open(aligned_path, "r", encoding="utf-8") as f:
            aligned_segs = json.load(f)
        for idx, (s_orig, s_al) in enumerate(zip(script_segs, aligned_segs)):
            st_orig = float(s_orig["start"])
            st_al = float(s_al["slot_start"])
            et_al = float(s_al["slot_end"])
            if abs(st_al - st_orig) > 0.025:
                drift_errors.append(f"Seg #{idx}: slot_start={st_al} != script start={st_orig}")
            if idx + 1 < len(aligned_segs):
                next_st = float(aligned_segs[idx + 1]["slot_start"])
                if et_al > next_st - 0.005 + 1e-6:
                    slot_overlap_errors.append(f"Seg #{idx}: slot_end={et_al} overlaps next_start={next_st}")
    for idx_u, u in enumerate(utts):
        sp_st = float(u.get("speech_start", 0.0))
        sp_en = float(u.get("speech_end", 0.0))
        sc_st = float(u.get("script_start", sp_st))
        diff = sp_st - sc_st
        if diff < -0.10 or diff > 0.50:
            speech_drift_errors.append(f"Utt #{idx_u}: speech_start={sp_st:.3f}s drifted {diff:+.3f}s from {sc_st:.3f}s")
        if idx_u + 1 < len(utts):
            nxt_sp = float(utts[idx_u + 1].get("speech_start", 0.0))
            if sp_en > nxt_sp - 0.02 + 1e-6:
                utt_overlap_errors.append(f"Utt #{idx_u}: speech_end={sp_en:.3f}s overlaps next={nxt_sp:.3f}s")
    chk3_ok = (
        len(aligned_segs) == len(script_segs) and len(script_segs) > 0
        and len(drift_errors) == 0 and len(speech_drift_errors) == 0
        and len(slot_overlap_errors) == 0 and len(utt_overlap_errors) == 0
    )
    chk3_detail = (
        f"aligned={len(aligned_segs)}/{len(script_segs)}, slot_drift={len(drift_errors)}, "
        f"utt_sync_errors={len(speech_drift_errors)} {speech_drift_errors[:2]}, "
        f"slot_overlaps={len(slot_overlap_errors)}, utt_overlaps={len(utt_overlap_errors)} {utt_overlap_errors[:2]}"
    )

    # Check 4: Master 48kHz stereo audio & SRT
    master_ok = False
    master_detail = "Missing media/master_audio_48k.wav or data/subtitles_full.srt"
    if os.path.exists(master_wav) and os.path.exists(srt_path):
        info = sf.info(master_wav)
        with open(srt_path, "r", encoding="utf-8") as f:
            srt_blocks = [b.strip() for b in f.read().strip().split("\n\n") if b.strip()]
        dur_diff = abs(info.duration - vid_dur)
        master_ok = (
            info.samplerate == 48000
            and info.channels == 2
            and dur_diff <= 0.25
            and len(srt_blocks) == len(script_segs)
        )
        master_detail = (
            f"sr={info.samplerate}Hz, ch={info.channels}, dur={info.duration:.2f}s (target={vid_dur:.2f}s), "
            f"srt_blocks={len(srt_blocks)}/{len(script_segs)}"
        )

    checks = [
        {
            "id": "audio_gate_report_passed",
            "description": "03_mix_report.json PASS (loudness -14+-1 LUFS, peak <= -0.8 dBFS, dub SNR median >= 10 dB & p10 >= 4 dB, original present)",
            "weight": 20,
            "passed": mix_ok,
            "detail": mix_detail,
        },
        {
            "id": "utterance_wavs_intact_and_non_runt",
            "description": "All indivisible utterance WAVs (media/tts_segments/utt_NNNN.wav) exist at 48kHz, >= 0.12s, non-silent, 0 overflows",
            "weight": 20,
            "passed": chk2_ok,
            "detail": chk2_detail,
        },
        {
            "id": "utterance_sync_and_zero_overlaps",
            "description": "Zero utterance overlaps, sync drift in [-0.10s, +0.50s], and hard-locked monotonic subtitle slots",
            "weight": 30,
            "passed": chk3_ok,
            "detail": chk3_detail,
        },
        {
            "id": "master_audio_48k_stereo_and_srt_valid",
            "description": "media/master_audio_48k.wav is 48000Hz stereo matching video duration and subtitles_full.srt has N blocks",
            "weight": 30,
            "passed": master_ok,
            "detail": master_detail,
        },
    ]

    metrics = {
        "localization_mode": "voice_dub",
        "total_segments": len(script_segs),
        "total_utterances": len(utts),
        "mean_tempo": round(mean_tempo, 3),
        "max_tempo": round(max_tempo, 3),
    }

    return write_gate_token(
        workspace=ws,
        stage=3,
        stage_name="Audio Track Assembly (voice_dub) & Subtitle Alignment",
        checks=checks,
        metrics=metrics,
        artifact_paths=[
            "03_pacing_and_audio_report.md",
            "data/03_aligned_subtitles.json",
            "data/03_dub_sentences.json",
            "data/03_mix_report.json",
            "data/subtitles_full.srt",
            "media/master_audio_48k.wav",
        ],
    )


def main():
    parser = argparse.ArgumentParser(description="Verify Stage 3 (Voice Dubbing, Mixing & Alignment) CI/CD Gate.")
    parser.add_argument("--workspace", required=True, help="Workspace path (workspace/<slug>).")
    args = parser.parse_args()
    run_stage_3_verification(args.workspace)


if __name__ == "__main__":
    main()
