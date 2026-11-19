#!/usr/bin/env python3
"""
Stage 3A: Synthesize 48kHz voice segments with VieNeu-TTS and audit tempo ratios against slot durations
(in 'voice_dub' mode), OR verify native audio passthrough readiness (in 'sub_only' mode).
Requires Stage 2 CI/CD gate token (.cicd/stage_2_gate.json == PASS).
"""
import argparse
import json
import os
import sys
import time
import numpy as np
import soundfile as sf

ORCH_SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "video-localization-orchestrator", "scripts"))
sys.path.insert(0, ORCH_SCRIPTS)
from cicd_gate import require_gate


def trim_silence(wav: np.ndarray, sr: int = 48000, thresh: float = 0.008) -> np.ndarray:
    idx = np.where(np.abs(wav) > thresh)[0]
    if len(idx) == 0:
        return wav
    pad = int(0.03 * sr)
    s = max(0, int(idx[0]) - pad)
    e = min(len(wav), int(idx[-1]) + pad)
    return wav[s:e]


def main():
    parser = argparse.ArgumentParser(description="Synthesize VieNeu-TTS segments and audit pacing ratios (or skip TTS in sub_only mode).")
    parser.add_argument("--workspace", required=True, help="Workspace path (workspace/<slug>).")
    parser.add_argument("--voice", default="Hải Đăng", help="Default VieNeu-TTS preset voice name.")
    parser.add_argument("--max-ratio", type=float, default=1.24, help="Maximum allowed tempo ratio before requiring text rewrite.")
    parser.add_argument("--only-missing", action="store_true", help="Only synthesize segments whose WAV file does not exist yet.")
    parser.add_argument("--mode", default="auto", choices=["auto", "voice_dub", "sub_only"], help="Override localization mode from 00_video_profile.json.")
    args = parser.parse_args()

    ws = os.path.abspath(args.workspace)
    require_gate(ws, 2)

    profile_path = os.path.join(ws, "data", "00_video_profile.json")
    with open(profile_path, "r", encoding="utf-8") as f:
        profile = json.load(f)

    loc_mode = profile.get("localization_mode", "voice_dub") if args.mode == "auto" else args.mode
    if args.mode != "auto" and profile.get("localization_mode") != loc_mode:
        profile["localization_mode"] = loc_mode
        with open(profile_path, "w", encoding="utf-8") as f:
            json.dump(profile, f, ensure_ascii=False, indent=2)

    script_path = os.path.join(ws, "data", "02_bilingual_script.json")
    with open(script_path, "r", encoding="utf-8") as f:
        segments = json.load(f)

    if loc_mode == "sub_only":
        print(
            f"[SUB_ONLY MODE] Skipping VieNeu-TTS voice synthesis for {len(segments)} segments. "
            f"Original native audio track will be preserved at 48kHz stereo in assemble_master_audio.py.",
            flush=True,
        )
        return

    wav_dir = os.path.join(ws, "media", "tts_segments")
    os.makedirs(wav_dir, exist_ok=True)

    missing_indices = [
        idx for idx in range(len(segments))
        if not (args.only_missing and os.path.exists(os.path.join(wav_dir, f"seg_{idx:04d}.wav")))
    ]

    if missing_indices:
        from vieneu import Vieneu
        t0 = time.time()
        print(f"[PLAN] Synthesizing {len(missing_indices)}/{len(segments)} segments with VieNeu-TTS (default voice='{args.voice}') -> {wav_dir}", flush=True)
        tts = Vieneu()
        last_log = time.time()

        for count, idx in enumerate(missing_indices, 1):
            seg = segments[idx]
            out_wav = os.path.join(wav_dir, f"seg_{idx:04d}.wav")
            voice_name = seg.get("voice", args.voice)
            audio = tts.infer(text=seg["tts_text"], voice=voice_name)
            sf.write(out_wav, audio, 48000)
            if time.time() - last_log >= 15.0:
                print(f"[PROGRESS] Synthesized {count}/{len(missing_indices)} segments in {time.time()-t0:.1f}s", flush=True)
                last_log = time.time()
    else:
        print(f"[CACHE] All {len(segments)} WAV files already exist in {wav_dir}; running pacing audit.", flush=True)

    # Audit tempo ratios
    over_budget = []
    ratios = []
    for idx, seg in enumerate(segments):
        out_wav = os.path.join(wav_dir, f"seg_{idx:04d}.wav")
        wav, sr = sf.read(out_wav)
        trimmed = trim_silence(wav, sr)
        raw_dur = len(trimmed) / sr

        st = float(seg["start"])
        et = float(seg["end"])
        next_st = float(segments[idx + 1]["start"]) if idx + 1 < len(segments) else et + 0.5
        avail_dur = max(0.25, min(et + 0.12, next_st - 0.14) - st)
        ratio = raw_dur / avail_dur
        ratios.append(ratio)
        if ratio > args.max_ratio:
            over_budget.append({
                "id": idx,
                "start": st,
                "end": et,
                "avail_dur": round(avail_dur, 2),
                "raw_dur": round(raw_dur, 2),
                "ratio": round(ratio, 3),
                "sub_text": seg["sub_text"]
            })

    arr_r = np.array(ratios)
    print(
        f"[AUDIT] Total={len(segments)} | ratio mean={arr_r.mean():.3f}x, median={np.median(arr_r):.3f}x, max={arr_r.max():.3f}x | "
        f"over_budget (>{args.max_ratio}x)={len(over_budget)}",
        flush=True
    )

    if over_budget:
        print("[CI/CD PACING AUDIT FAILED] Shorten the following segments in 02_bilingual_script.json, re-run validate_bilingual_script.py, delete their WAVs, and re-run with --only-missing:", file=sys.stderr)
        for item in over_budget:
            print(f"  Seg #{item['id']:03d} ({item['start']}s-{item['end']}s): ratio={item['ratio']}x | {item['sub_text']}", file=sys.stderr)
        sys.exit(2)

    print("[PASS] All 100% of synthesized segments satisfy the natural tempo ceiling.")


if __name__ == "__main__":
    main()
