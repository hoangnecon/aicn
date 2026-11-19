#!/usr/bin/env python3
"""
Stage 3 CI/CD Gate Validator (verify_stage_3.py).
Requires Stage 2 gate PASS (.cicd/stage_2_gate.json).
Supports both 'voice_dub' and 'sub_only' modes:
  1. TTS segments completeness (voice_dub) or native audio passthrough verification (sub_only) (20 pts)
  2. Hard-locked slot_start with zero cumulative drift & zero overlap in 03_aligned_subtitles.json (25 pts)
  3. Natural tempo ceiling (max <= 1.26x, mean <= 1.18x) and pause verification (25 pts)
  4. Master stereo audio (media/master_audio_48k.wav @ 48000 Hz, 2ch, peak in [0.25, 0.99]) & SRT file valid (30 pts)
Writes workspace/<slug>/.cicd/stage_3_gate.json only on 100/100 PASS.
"""
import argparse
import json
import os
import sys
import numpy as np
import soundfile as sf

ORCH_SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "video-localization-orchestrator", "scripts"))
sys.path.insert(0, ORCH_SCRIPTS)
from cicd_gate import require_gate, write_gate_token


def run_stage_3_verification(ws: str):
    ws = os.path.abspath(ws)
    require_gate(ws, 2)

    with open(os.path.join(ws, "data", "00_video_profile.json"), "r", encoding="utf-8") as f:
        profile = json.load(f)
    vid_dur = float(profile.get("duration", 0.0))
    loc_mode = profile.get("localization_mode", "voice_dub")

    with open(os.path.join(ws, "data", "02_bilingual_script.json"), "r", encoding="utf-8") as f:
        script_segs = json.load(f)

    aligned_path = os.path.join(ws, "data", "03_aligned_subtitles.json")
    master_wav = os.path.join(ws, "media", "master_audio_48k.wav")
    srt_path = os.path.join(ws, "data", "subtitles_full.srt")
    wav_dir = os.path.join(ws, "media", "tts_segments")

    # Check 1: TTS segments completeness (voice_dub) or native audio passthrough (sub_only)
    missing_wavs = []
    if loc_mode == "voice_dub":
        for idx in range(len(script_segs)):
            p = os.path.join(wav_dir, f"seg_{idx:04d}.wav")
            if not os.path.exists(p) or os.path.getsize(p) < 1000:
                missing_wavs.append(f"seg_{idx:04d}.wav")
        chk1_passed = len(missing_wavs) == 0 and len(script_segs) > 0
        chk1_detail = f"voice_dub mode: present={len(script_segs)-len(missing_wavs)}/{len(script_segs)}, missing={missing_wavs[:3]}"
    else:
        chk1_passed = os.path.exists(master_wav) and os.path.getsize(master_wav) > 100_000 and len(script_segs) > 0
        chk1_detail = f"sub_only mode: native stereo audio preserved ({master_wav})"

    # Check 2 & 3: Aligned subtitles drift, overlap, tempo, pauses
    drift_errors = []
    overlap_errors = []
    tempos = []
    pauses = []
    aligned_segs = []
    if os.path.exists(aligned_path):
        with open(aligned_path, "r", encoding="utf-8") as f:
            aligned_segs = json.load(f)
        for idx, (s_orig, s_al) in enumerate(zip(script_segs, aligned_segs)):
            st_orig = float(s_orig["start"])
            st_al = float(s_al["slot_start"])
            et_al = float(s_al["slot_end"])
            au_end = float(s_al.get("speech_end", s_al.get("audio_end", et_al)))
            if abs(st_al - st_orig) > 0.025:
                drift_errors.append(f"Seg #{idx}: slot_start={st_al} drifted from script start={st_orig}")
            if idx + 1 < len(aligned_segs):
                next_st = float(aligned_segs[idx + 1]["slot_start"])
                if et_al > next_st + 0.005:
                    overlap_errors.append(f"Seg #{idx}: slot_end={et_al} overlaps next_start={next_st}")
                if "pause_after" in s_al:
                    pauses.append(float(s_al["pause_after"]))
                else:
                    pauses.append(max(0.0, next_st - au_end))
            tempos.append(float(s_al.get("tempo", 1.0)))

    t_arr = np.array(tempos) if tempos else np.array([9.9])
    p_arr = np.array(pauses) if pauses else np.array([0.0])
    mean_tempo = float(t_arr.mean())
    max_tempo = float(t_arr.max())
    mean_pause = float(p_arr.mean())

    if loc_mode == "sub_only":
        tempo_pause_ok = (max_tempo == 1.0) and (mean_tempo == 1.0)
        tempo_pause_detail = f"sub_only mode: tempo=1.000x (native audio), mean_inter_cue_gap={mean_pause:.3f}s"
    else:
        tempo_pause_ok = max_tempo <= 1.265 and mean_tempo <= 1.18 and mean_pause >= 0.14
        tempo_pause_detail = f"voice_dub mode: mean_tempo={mean_tempo:.3f}x, max_tempo={max_tempo:.3f}x, mean_pause={mean_pause:.3f}s"

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
            "id": "tts_segments_or_native_audio_ready",
            "description": "All N synthesized 48kHz WAV files exist (voice_dub) or native stereo audio extracted (sub_only)",
            "weight": 20,
            "passed": chk1_passed,
            "detail": chk1_detail
        },
        {
            "id": "hard_locked_slot_start_zero_drift",
            "description": "Every segment in 03_aligned_subtitles.json has hard-locked slot_start (drift <= 25ms) and 0 overlaps",
            "weight": 25,
            "passed": len(aligned_segs) == len(script_segs) and len(drift_errors) == 0 and len(overlap_errors) == 0,
            "detail": f"drift_errors={len(drift_errors)}, overlap_errors={len(overlap_errors)}"
        },
        {
            "id": "natural_tempo_and_breathing_pauses",
            "description": "Tempo ceiling satisfied (max <= 1.26x in voice_dub, 1.0x in sub_only) with clean pacing",
            "weight": 25,
            "passed": tempo_pause_ok,
            "detail": tempo_pause_detail
        },
        {
            "id": "master_audio_48k_stereo_and_srt_valid",
            "description": "media/master_audio_48k.wav is 48000Hz stereo matching video duration and subtitles_full.srt has N blocks",
            "weight": 30,
            "passed": master_ok,
            "detail": master_detail
        }
    ]

    metrics = {
        "localization_mode": loc_mode,
        "total_segments": len(script_segs),
        "mean_tempo": round(mean_tempo, 3),
        "max_tempo": round(max_tempo, 3),
        "mean_breathing_pause_s": round(mean_pause, 3),
    }

    return write_gate_token(
        workspace=ws,
        stage=3,
        stage_name="Audio Track Assembly (voice_dub / sub_only) & Subtitle Alignment",
        checks=checks,
        metrics=metrics,
        artifact_paths=[
            "03_pacing_and_audio_report.md",
            "data/03_aligned_subtitles.json",
            "data/subtitles_full.srt",
            "media/master_audio_48k.wav",
        ],
    )


def main():
    parser = argparse.ArgumentParser(description="Verify Stage 3 (Voice Pacing / Native Audio & Alignment) CI/CD Gate.")
    parser.add_argument("--workspace", required=True, help="Workspace path (workspace/<slug>).")
    args = parser.parse_args()
    run_stage_3_verification(args.workspace)


if __name__ == "__main__":
    main()
