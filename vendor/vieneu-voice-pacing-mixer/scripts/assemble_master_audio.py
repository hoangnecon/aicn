#!/usr/bin/env python3
"""
Stage 3B: Assemble 48kHz stereo master audio and aligned subtitles.
Supports two modes:
  - 'voice_dub': Mix 48kHz VieNeu-TTS voice segments with hard-locked slot_start, breathing pauses, and auto-ducked BGM.
  - 'sub_only': Extract original native stereo audio at 48000 Hz losslessly from source video, preserving 100% of
    original acting, phone call acoustics, and SFX, and lock subtitle timestamps directly to 03_aligned_subtitles.json.
Runs Stage 3 CI/CD gate validator (.cicd/stage_3_gate.json) upon completion.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import numpy as np
import soundfile as sf

ORCH_SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "video-localization-orchestrator", "scripts"))
AICN_SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts"))
for _p in (ORCH_SCRIPTS, AICN_SCRIPTS, os.path.dirname(os.path.abspath(__file__))):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from cicd_gate import require_gate
from verify_stage_3 import run_stage_3_verification
try:
    import aicn_platform as ap
except ImportError:
    ap = None


def trim_silence(wav: np.ndarray, sr: int = 48000, thresh: float = 0.008) -> np.ndarray:
    idx = np.where(np.abs(wav) > thresh)[0]
    if len(idx) == 0:
        return wav
    pad = int(0.03 * sr)
    s = max(0, int(idx[0]) - pad)
    e = min(len(wav), int(idx[-1]) + pad)
    return wav[s:e]


def apply_atempo(wav: np.ndarray, sr: int, tempo: float) -> np.ndarray:
    if abs(tempo - 1.0) < 0.015:
        return wav
    cmd = [
        ap.find_tool("ffmpeg") if ap else "ffmpeg", "-y",
        "-f", "f32le", "-ar", str(sr), "-ac", "1", "-i", "pipe:0",
        "-filter:a", f"atempo={tempo:.4f}",
        "-f", "f32le", "-ar", str(sr), "-ac", "1", "pipe:1"
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    out_bytes, _ = proc.communicate(wav.astype(np.float32).tobytes())
    return np.frombuffer(out_bytes, dtype=np.float32)


def load_bgm_48k_stereo(bgm_path: str, total_samples: int, sr: int = 48000) -> np.ndarray:
    if not os.path.exists(bgm_path):
        return np.zeros((total_samples, 2), dtype=np.float32)
    cmd = [
        ap.find_tool("ffmpeg") if ap else "ffmpeg", "-y", "-i", bgm_path,
        "-f", "f32le", "-ar", str(sr), "-ac", "2", "pipe:1"
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    raw, _ = proc.communicate()
    bgm = np.frombuffer(raw, dtype=np.float32).reshape((-1, 2))
    if len(bgm) < total_samples:
        pad = np.zeros((total_samples - len(bgm), 2), dtype=np.float32)
        bgm = np.vstack([bgm, pad])
    else:
        bgm = bgm[:total_samples]
    return bgm


def fmt_srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h = ms // 3_600_000
    ms %= 3_600_000
    m = ms // 60_000
    ms %= 60_000
    s = ms // 1000
    ms %= 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def main():
    parser = argparse.ArgumentParser(description="Assemble 48kHz stereo master audio and aligned subtitles.")
    parser.add_argument("--workspace", required=True, help="Workspace path (workspace/<slug>).")
    parser.add_argument("--duration", type=float, default=0.0, help="Exact source video duration in seconds (auto-read from 00_video_profile.json if omitted).")
    parser.add_argument("--bgm-speech-gain", type=float, default=0.28, help="BGM gain during active speech (voice_dub mode).")
    parser.add_argument("--bgm-pause-gain", type=float, default=0.65, help="BGM gain during pauses (voice_dub mode).")
    parser.add_argument("--mode", default="auto", choices=["auto", "voice_dub", "sub_only"], help="Override localization mode from 00_video_profile.json.")
    args = parser.parse_args()

    ws = os.path.abspath(args.workspace)
    require_gate(ws, 2)

    t0 = time.time()
    sr = 48000
    profile_path = os.path.join(ws, "data", "00_video_profile.json")
    with open(profile_path, "r", encoding="utf-8") as f:
        profile = json.load(f)

    dur = args.duration if args.duration > 0.0 else float(profile["duration"])
    loc_mode = profile.get("localization_mode", "voice_dub") if args.mode == "auto" else args.mode
    if args.mode != "auto" and profile.get("localization_mode") != loc_mode:
        profile["localization_mode"] = loc_mode
        with open(profile_path, "w", encoding="utf-8") as f:
            json.dump(profile, f, ensure_ascii=False, indent=2)

    total_samples = int(round(dur * sr))

    script_path = os.path.join(ws, "data", "02_bilingual_script.json")
    wav_dir = os.path.join(ws, "media", "tts_segments")
    bgm_path = os.path.join(ws, "media", "demucs", "no_vocals.wav")
    out_master = os.path.join(ws, "media", "master_audio_48k.wav")
    out_aligned = os.path.join(ws, "data", "03_aligned_subtitles.json")
    out_srt = os.path.join(ws, "data", "subtitles_full.srt")
    out_report = os.path.join(ws, "03_pacing_and_audio_report.md")

    with open(script_path, "r", encoding="utf-8") as f:
        segments = json.load(f)

    if loc_mode == "sub_only":
        src_video = profile.get("source_video", "")
        print(f"[SUB_ONLY MODE] Extracting native 48kHz stereo master audio from {src_video} -> {out_master}", flush=True)
        native_stereo = load_bgm_48k_stereo(src_video, total_samples, sr)
        peak = float(np.max(np.abs(native_stereo))) if native_stereo.size > 0 else 0.0
        sf.write(out_master, native_stereo, sr)

        aligned = []
        pauses = []
        for idx, seg in enumerate(segments):
            st = float(seg["start"])
            et = float(seg["end"])
            if idx + 1 < len(segments):
                next_st = float(segments[idx + 1]["start"])
                sub_end = min(et, next_st - 0.02) if et > next_st - 0.02 else et
                sub_end = max(st + 0.15, sub_end)
                pause_after = max(0.0, next_st - sub_end)
                pauses.append(pause_after)
            else:
                sub_end = min(dur - 0.02, et)
                pause_after = 0.25

            aligned.append({
                "id": idx,
                "slot_start": round(st, 3),
                "slot_end": round(sub_end, 3),
                "speech_start": round(st, 3),
                "speech_end": round(sub_end, 3),
                "audio_end": round(sub_end, 3),
                "pause_after": round(pause_after, 3),
                "tempo": 1.0,
                "zh_text": seg.get("zh_text", ""),
                "sub_text": seg["sub_text"],
                "tts_text": seg.get("tts_text", seg["sub_text"]),
            })

        with open(out_aligned, "w", encoding="utf-8") as f:
            json.dump(aligned, f, ensure_ascii=False, indent=2)

        with open(out_srt, "w", encoding="utf-8") as f:
            for idx, item in enumerate(aligned, 1):
                f.write(f"{idx}\n")
                f.write(f"{fmt_srt_time(item['slot_start'])} --> {fmt_srt_time(item['slot_end'])}\n")
                f.write(f"{item['sub_text']}\n\n")

        p_arr = np.array(pauses) if pauses else np.array([0.22])
        report_md = f"""# 03 — Audio Passthrough & Subtitle Timing Report (`sub_only` Mode)

- **Localization Mode:** `sub_only` (Preserving 100% original native voice acting, telephone filters, and cinema SFX)
- **Master Audio Source:** `{src_video}` -> `{out_master}` (`48000 Hz` stereo)
- **Total Subtitle Segments:** `{len(aligned)}`
- **Tempo Statistics:** `mean = 1.000x`, `median = 1.000x`, `max = 1.000x` (`100%` native speed)
- **Inter-Cue Gap Statistics:** `mean = {p_arr.mean():.3f}s`, `min = {p_arr.min():.3f}s`, `max = {p_arr.max():.3f}s`
- **Master Audio Peak:** `{peak:.3f}` (`{out_master}`)
- **Exported SRT:** `{out_srt}`
"""
        with open(out_report, "w", encoding="utf-8") as f:
            f.write(report_md)

        print(f"[DONE] Saved native 48kHz stereo master {out_master} and {out_srt} in {time.time()-t0:.2f}s", flush=True)
        run_stage_3_verification(ws)
        return

    # VOICE_DUB MODE
    voice_track = np.zeros(total_samples, dtype=np.float32)
    speech_env = np.zeros(total_samples, dtype=np.float32)
    aligned = []
    tempos = []
    pauses = []

    for idx, seg in enumerate(segments):
        wav_file = os.path.join(wav_dir, f"seg_{idx:04d}.wav")
        wav, file_sr = sf.read(wav_file)
        if file_sr != sr:
            raise ValueError(f"Expected {sr}Hz in {wav_file}, got {file_sr}Hz")
        wav = trim_silence(wav.astype(np.float32), sr)
        raw_dur = len(wav) / sr

        st = float(seg["start"])
        et = float(seg["end"])
        next_st = float(segments[idx + 1]["start"]) if idx + 1 < len(segments) else dur - 0.05
        hard_ceiling = min(et + 0.12, next_st - 0.14)
        avail_dur = max(0.25, hard_ceiling - st)

        if raw_dur <= avail_dur:
            tempo = 1.0
            fitted = wav.copy()
        else:
            tempo = min(1.26, raw_dur / avail_dur)
            fitted = apply_atempo(wav, sr, tempo).copy()

        fade_len = min(int(0.015 * sr), len(fitted) // 4)
        if fade_len > 0:
            fitted[:fade_len] *= np.linspace(0.0, 1.0, fade_len, dtype=np.float32)
            fitted[-fade_len:] *= np.linspace(1.0, 0.0, fade_len, dtype=np.float32)

        s_sample = max(0, int(round(st * sr)))
        e_sample = min(total_samples, s_sample + len(fitted))
        seg_len = e_sample - s_sample
        voice_track[s_sample:e_sample] += fitted[:seg_len]
        speech_env[s_sample:e_sample] = 1.0

        actual_end = s_sample / sr + seg_len / sr
        sub_end = max(et, actual_end)
        if idx + 1 < len(segments):
            sub_end = min(sub_end, float(segments[idx + 1]["start"]) - 0.04)

        tempos.append(tempo)
        pause_after = max(0.0, float(segments[idx + 1]["start"]) - actual_end) if idx + 1 < len(segments) else 0.25
        if idx + 1 < len(segments):
            pauses.append(pause_after)

        aligned.append({
            "id": idx,
            "slot_start": round(st, 3),
            "slot_end": round(sub_end, 3),
            "speech_start": round(st, 3),
            "speech_end": round(actual_end, 3),
            "audio_end": round(actual_end, 3),
            "pause_after": round(pause_after, 3),
            "tempo": round(tempo, 3),
            "zh_text": seg.get("zh_text", ""),
            "sub_text": seg["sub_text"],
            "tts_text": seg["tts_text"]
        })

    win = int(0.40 * sr)
    kernel = np.hanning(win)
    kernel /= kernel.sum()
    try:
        from scipy.signal import fftconvolve
        smooth_speech = fftconvolve(speech_env, kernel, mode="same")
    except Exception:
        smooth_speech = np.convolve(speech_env, kernel, mode="same")
    bgm_gain = args.bgm_pause_gain - smooth_speech * (args.bgm_pause_gain - args.bgm_speech_gain)

    bgm_stereo = load_bgm_48k_stereo(bgm_path, total_samples, sr)
    master = np.zeros((total_samples, 2), dtype=np.float32)
    master[:, 0] = voice_track * 0.92 + bgm_stereo[:, 0] * bgm_gain
    master[:, 1] = voice_track * 0.92 + bgm_stereo[:, 1] * bgm_gain

    peak = float(np.max(np.abs(master)))
    if peak > 0.95:
        master *= (0.92 / peak)
        peak = float(np.max(np.abs(master)))

    sf.write(out_master, master, sr)

    with open(out_aligned, "w", encoding="utf-8") as f:
        json.dump(aligned, f, ensure_ascii=False, indent=2)

    with open(out_srt, "w", encoding="utf-8") as f:
        for idx, item in enumerate(aligned, 1):
            f.write(f"{idx}\n")
            f.write(f"{fmt_srt_time(item['slot_start'])} --> {fmt_srt_time(item['slot_end'])}\n")
            f.write(f"{item['sub_text']}\n\n")

    t_arr = np.array(tempos)
    p_arr = np.array(pauses) if pauses else np.array([0.22])
    cnt_1x = int(np.sum(t_arr == 1.0))

    report_md = f"""# 03 — Voice Pacing & BGM Mixing Report (`voice_dub` Mode)

- **TTS Engine:** `VieNeu-TTS v3 Turbo` (`48000 Hz` stereo master)
- **Total Segments:** `{len(aligned)}`
- **Tempo Statistics:** `mean = {t_arr.mean():.3f}x`, `median = {np.median(t_arr):.3f}x`, `max = {t_arr.max():.3f}x`, `count(tempo == 1.0x) = {cnt_1x}/{len(aligned)}`
- **Breathing Pause Statistics:** `mean = {p_arr.mean():.3f}s`, `min = {p_arr.min():.3f}s`, `max = {p_arr.max():.3f}s`
- **BGM Auto-Ducking:** `{args.bgm_speech_gain}` active speech gain, `{args.bgm_pause_gain}` pause gain
- **Master Audio Peak:** `{peak:.3f}` (`{out_master}`)
- **Exported SRT:** `{out_srt}`
"""
    with open(out_report, "w", encoding="utf-8") as f:
        f.write(report_md)

    print(f"[DONE] Saved {out_master} and {out_srt} in {time.time()-t0:.2f}s", flush=True)
    run_stage_3_verification(ws)


if __name__ == "__main__":
    main()
