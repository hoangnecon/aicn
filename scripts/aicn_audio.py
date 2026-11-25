#!/usr/bin/env python3
"""
aicn_audio.py - Stage 3 audio engine for AICN v3.0.0:
  separate       Demucs separation -> <global_cache>/stems + media/stems/{vocals,accomp}.wav
  dub            indivisible utterance VieNeu TTS -> rubberband R3 -> media/audio/dub_voice_48k.wav
  analyze        fast active-frame 9-band spectral audit -> data/03_spectral_analysis.json
  mix            single-STFT + C-speed follow_hold + combined iSTFT vocal-carving mix
  master         glue compression + native 48 kHz EBU R128 master -> media/master_audio_48k.wav
  verify         audio quality gate (data/03_mix_report.json) + Stage 3 gate (verify_stage_3.py)
  all            separate -> dub -> mix -> master -> verify
  batch-separate warm single-load Demucs across --units u1,u2,...
  batch-dub      warm single-load VieNeu CUDA infer_batch + parallel rubberband across --units u1,u2,...
  batch-all      batch-separate -> batch-dub (fail-fast per unit) -> parallel CPU mix/master/verify

Run with the VieNeu venv python ($AICN_PY). Parameters: config/audio_mix.json overrides DEFAULTS.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import soundfile as sf
from scipy import fft as sp_fft
from scipy.ndimage import minimum_filter1d
from scipy.signal import butter, get_window, lfilter, sosfilt
try:
    import torch
    import torch.nn.functional as F
except ImportError:  # pragma: no cover
    torch = None
    F = None

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import aicn_platform as ap  # noqa: E402

SR = 48000
AUDIO_VERSION = "aicn-audio-3.0"
STEMS_VERSION = getattr(ap, "STEMS_VERSION", "aicn-audio-1.0")
TTS_ENGINE_TAG = getattr(ap, "TTS_ENGINE_TAG", "vieneu-v3-turbo-48k")
RB_STRETCH_VERSION = getattr(ap, "RB_STRETCH_VERSION", "rb3-f-3.0")

DEFAULTS = {
    "voice": "Trúc Ly",
    "tts_workers": 0, "tts_threads": 0,
    "tts_batch_size": 16,
    "temperature": 0.8, "top_k": 25,
    "group_max_gap_s": 0.12,
    "max_group_clauses": 3,
    "max_group_span_s": 4.5,
    "base_tempo": 1.08,
    "max_tempo": 1.28,
    "max_tempo_short": 1.32,
    "lead_gap_s": 0.05,
    "max_overhang_s": 1.20,
    "min_utterance_s": 0.12,
    "expander_thresh_db": -50.0, "expander_depth_db": -15.0,
    "dub_over_vocal_lu": 1.5,
    "snr_vocal_db": 14.0, "snr_bgm_db": 6.0,
    "floor_vocal_db": -24.0, "floor_bgm_db": -9.0,
    "base_vocal_db": -10.0,
    "speech_vocal_dip_db": -16.0,
    "vocal_high_dip_db": -4.5,
    "attack_s": 0.015, "hold_s": 0.30, "release_s": 0.38,
    "dub_hp_hz": 90.0, "dub_lp_hz": 16000.0,
    "vocal_hp_hz": 95.0, "vocal_lp_hz": 0.0,
    "bgm_hp_hz": 35.0, "bgm_lp_hz": 18500.0,
    "target_lufs": -14.0, "true_peak_db": -1.0, "lra": 11.0,
    "master_mode": "fast",
    "demucs_model": "htdemucs",
}

GATE = {
    "lufs_tol": 1.0,
    "true_peak_max_db": -0.8,
    "dub_snr_median_min_db": 10.0,
    "dub_snr_min_db": 10.0,
    "dub_snr_p10_min_db": 4.0,
    "orig_presence_min_db": -6.0,
    "vocal_floor_margin_db": 6.0,
}


# ---------------------------------------------------------------- helpers
def _jload(p, default=None):
    if not p or not os.path.exists(p):
        return default
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _jdump(p, obj):
    ap.atomic_write_json(p, obj)


def _hash_file(p, fh=None):
    return fh(p) if fh is not None else ap.content_sha256(p)


def cfg(ws):
    c = dict(DEFAULTS)
    c.update(_jload(os.path.join(ws, "config", "audio_mix.json"), {}) or {})
    return c


def profile(ws):
    return _jload(os.path.join(ws, "data", "00_video_profile.json"), {}) or {}


def _assert_voice_dub(ws):
    mode = profile(ws).get("localization_mode", "voice_dub")
    if mode == "sub_only":
        sys.exit("[aicn-audio] sub_only mode is retired in AICN v3.0.0; full voice_dub is mandatory.")
    return mode


def source_path(ws):
    p = os.path.join(ws, "source", "master_source.mp4")
    if os.path.exists(p):
        return p
    sv = profile(ws).get("source_video", "")
    if not sv:
        return ""
    return sv if os.path.isabs(sv) else os.path.normpath(os.path.join(ws, sv))


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, **kw)
    if r.returncode:
        err = r.stderr[-800:] if isinstance(r.stderr, str) else r.stderr[-800:].decode("utf-8", errors="replace")
        sys.exit(f"[aicn-audio] command failed: {' '.join(map(str, cmd))[:200]}\n{err}")
    return r


def load(p, ch=2, sr=SR):
    raw = run([ap.find_tool("ffmpeg"), "-v", "error", "-i", p, "-f", "f32le", "-ar", str(sr), "-ac", str(ch), "pipe:1"]).stdout
    return np.frombuffer(raw, np.float32).reshape(-1, ch).copy()


def lufs_file(p):
    o = subprocess.run(
        [ap.find_tool("ffmpeg"), "-nostats", "-i", p, "-af", "ebur128=peak=true", "-f", "null", "-"],
        capture_output=True, text=True,
    ).stderr
    i_hits = re.findall(r"I:\s+(-?[\d.]+) LUFS", o)
    i = float(i_hits[-1]) if i_hits else -70.0
    pk = re.findall(r"Peak:\s+(-?[\d.inf]+) dBFS", o)
    return i, (float(pk[-1]) if pk and pk[-1] not in ("-inf", "inf") else -99.0)


# Precomputed ITU-R BS.1770-4 K-weighting SOS for 48 kHz (Stage 1 shelf + Stage 2 RLB HPF)
_BS1770_SOS_48K = np.array([
    [1.53512485958697, -2.69169618940638, 1.19839281085285, 1.0, -1.69065929318241, 0.73248077421585],
    [1.0, -2.0, 1.0, 1.0, -1.99004745483398, 0.99007225036621],
], dtype=np.float64)


def _anti_denormal(arr, dtype=None, inplace=False):
    """Add 12 kHz -300 dBFS bias so IIR filters over digital-silence gaps never hit x86_64 denormal traps."""
    dt = np.dtype(dtype) if dtype is not None else np.dtype(arr.dtype)
    out = arr if (inplace and np.dtype(arr.dtype) == dt) else np.array(arr, dtype=dt, copy=True)
    n_s = len(out)
    if n_s >= SR:
        pat = np.array([0.0, 1e-15, 0.0, -1e-15], dtype=dt)
        q = n_s // 4
        if out.ndim == 1:
            out[: q * 4].reshape(-1, 4)[:] += pat
        else:
            out[: q * 4].reshape(-1, 4, out.shape[1])[:] += pat[None, :, None]
    return out


def _sosfilt_parallel(sos, arr):
    """Apply sosfilt concurrently across stereo/multi-channel columns when audio is >=10s (denormal-safe)."""
    if arr.ndim == 2 and arr.shape[1] > 1 and len(arr) >= SR * 10:
        with ThreadPoolExecutor(max_workers=arr.shape[1]) as ex:
            cols = list(ex.map(lambda ch: sosfilt(sos, _anti_denormal(arr[:, ch], inplace=False)), range(arr.shape[1])))
        return np.stack(cols, axis=1)
    return sosfilt(sos, _anti_denormal(arr), axis=0)


def lufs(x):
    """Measure integrated ITU-R BS.1770-4 LUFS in RAM (~0.35s on 10-min audio, zero ffmpeg pipe)."""
    arr = np.asarray(x, dtype=np.float32)
    if arr.ndim == 1:
        arr = arr[:, None]
    if len(arr) < int(0.4 * SR):
        return -70.0
    step = int(0.1 * SR)
    nb = len(arr) // step
    if nb < 4:
        return -70.0
    n_use = nb * step

    def _ch_e100(col):
        ch64 = _anti_denormal(col, dtype=np.float64, inplace=False)
        yk = sosfilt(_BS1770_SOS_48K, ch64)[:n_use]
        return (yk.reshape(nb, step) ** 2).mean(axis=1)

    n_ch = arr.shape[1]
    if n_ch > 1 and len(arr) >= SR * 10:
        with ThreadPoolExecutor(max_workers=n_ch) as ex:
            e100 = sum(ex.map(lambda ch: _ch_e100(arr[:, ch]), range(n_ch)))
    else:
        e100 = sum(_ch_e100(arr[:, ch]) for ch in range(n_ch))
    e400 = 0.25 * (e100[:-3] + e100[1:-2] + e100[2:-1] + e100[3:])
    l_blk = -0.691 + 10.0 * np.log10(np.maximum(e400, 1e-12))
    g1 = e400[l_blk > -70.0]
    if not len(g1):
        return -70.0
    rel = -0.691 + 10.0 * np.log10(float(g1.mean())) - 10.0
    g2 = e400[l_blk > max(-70.0, rel)]
    return float(-0.691 + 10.0 * np.log10(float(g2.mean()))) if len(g2) else -70.0


def follow_hold(g, fps, att, hold_s, rel):
    """Vectorized C-speed attack-hold-release envelope follower over the last axis."""
    g_arr = np.atleast_2d(np.asarray(g, dtype=np.float64))
    n_bands, n_frames = g_arr.shape
    if n_frames == 0:
        return g_arr.copy()
    hold_frames = max(0, int(round(max(0.0, float(hold_s)) * fps)))
    a_att = float(np.exp(-1.0 / max(1e-6, float(att) * fps)))
    a_rel = float(np.exp(-1.0 / max(1e-6, float(rel) * fps)))

    if hold_frames > 0:
        sz = hold_frames + 1
        origin = (sz - 1) // 2
        pad = np.pad(g_arr, ((0, 0), (hold_frames, 0)), mode="constant", constant_values=1.0)
        g_held = minimum_filter1d(pad, size=sz, axis=1, mode="nearest", origin=origin)[:, hold_frames:]
    else:
        g_held = g_arr

    zi_att = np.full((n_bands, 1), a_att, dtype=np.float64)
    y_att, _ = lfilter([1.0 - a_att], [1.0, -a_att], g_held, axis=1, zi=zi_att)

    # Peak-dip exponential release from y_att via chunked maximum.accumulate (prevents float overflow)
    dip = np.maximum(0.0, 1.0 - y_att)
    blk = 512
    pows = a_rel ** np.arange(blk, dtype=np.float64)
    inv_pows = 1.0 / pows
    dip_rel = np.empty_like(dip)
    carry = np.zeros(n_bands, dtype=np.float64)
    for s in range(0, n_frames, blk):
        e = min(n_frames, s + blk)
        seg = dip[:, s:e] * inv_pows[None, : e - s]
        seg[:, 0] = np.maximum(seg[:, 0], carry * a_rel)
        out_blk = np.maximum.accumulate(seg, axis=1) * pows[None, : e - s]
        dip_rel[:, s:e] = out_blk
        carry = out_blk[:, -1]
    return np.clip(1.0 - dip_rel, 0.0, 1.0)


def follow(g, fps, att, rel):
    """Envelope follower over the last axis: fast down (attack), slow up (release)."""
    return follow_hold(g, fps, att, 0.0, rel)


def _tmp_path(path, suffix):
    return f"{path}.{os.getpid()}.{threading.get_ident()}.{suffix}"


def _atomic_replace(tmp, dst):
    for attempt in range(10):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.02 * (attempt + 1))


def _write_wav16(path, arr, sr=SR, inplace=False):
    """Write compact lossless 16-bit PCM WAV atomically (multi-threaded Torch int16 quantize on >=10s float32)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = _tmp_path(path, "part.wav")
    a = np.ascontiguousarray(arr, dtype=np.float32) if not (isinstance(arr, np.ndarray) and arr.dtype == np.float32 and arr.flags["C_CONTIGUOUS"]) else arr
    try:
        if torch is not None and len(a) >= SR * 10:
            mt = torch.from_numpy(a)
            if not inplace:
                mt = mt.clamp(-1.0, 0.999969).mul_(32767.0)
            else:
                mt.clamp_(-1.0, 0.999969).mul_(32767.0)
            sf.write(tmp, mt.round_().to(torch.int16).numpy(), sr, subtype="PCM_16")
        else:
            clipped = np.clip(a, -1.0, 0.999969, out=a if inplace else None)
            sf.write(tmp, clipped, sr, subtype="PCM_16")
        _atomic_replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def _link_or_copy(src_p, dst_p):
    os.makedirs(os.path.dirname(os.path.abspath(dst_p)), exist_ok=True)
    if os.path.exists(dst_p):
        try:
            if os.path.samefile(src_p, dst_p):
                return
        except OSError:
            pass
    tmp = _tmp_path(dst_p, "part.wav")
    with contextlib.suppress(OSError):
        if os.path.exists(tmp):
            os.remove(tmp)
    try:
        try:
            os.link(src_p, tmp)
        except OSError:
            shutil.copy2(src_p, tmp)
        _atomic_replace(tmp, dst_p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def _audio_state_load(ws):
    return _jload(os.path.join(ws, ".cache", "audio_state.json"), {}) or {}


def _audio_state_save(ws, st):
    _jdump(os.path.join(ws, ".cache", "audio_state.json"), st)


# ---------------------------------------------------------------- separate
def _separate_with_separator(sep, src, sr=SR):
    import torch
    import torchaudio.functional as TAF

    wav_44k = load(src, ch=2, sr=44100)
    wav_t = torch.from_numpy(wav_44k.T.copy())
    _, stems = sep.separate_tensor(wav_t, 44100)
    voc_t = stems["vocals"]
    acc_t = sum(v for k, v in stems.items() if k != "vocals")
    voc_48 = TAF.resample(voc_t, 44100, sr).cpu().numpy().T.astype(np.float32)
    acc_48 = TAF.resample(acc_t, 44100, sr).cpu().numpy().T.astype(np.float32)
    return voc_48, acc_48


def _separate_with_mlx(src, model_name):
    with tempfile.TemporaryDirectory() as tmp:
        wav = os.path.join(tmp, "source.wav")
        run([ap.find_tool("ffmpeg"), "-v", "error", "-y", "-i", src, "-vn", "-ac", "2", "-ar", "44100", wav])
        run([ap.find_tool("uvx"), "--from", "demucs-mlx", "demucs-mlx", "-n", model_name, "-o", tmp, wav])
        sd = os.path.join(tmp, model_name, "source")
        if not os.path.isdir(sd):
            sd = os.path.join(tmp, "source")
        voc = load(os.path.join(sd, "vocals.wav"))
        no_voc = os.path.join(sd, "no_vocals.wav")
        acc = load(no_voc) if os.path.exists(no_voc) else sum(load(os.path.join(sd, f"{s}.wav")) for s in ("drums", "bass", "other"))
        return voc, acc


def batch_separate(units, force=False, fh=None):
    """Separate stems across one or more units, loading Demucs ONCE in GPU memory across cache misses."""
    if not units:
        return
    bk = ap.detect_demucs_backend(sys.executable)
    if bk["engine"] == "missing":
        sys.exit("[aicn-audio] Demucs not available: install `demucs` in AICN_PY or install `uv` (for uvx)")

    misses = []
    for ws in units:
        ws = os.path.abspath(ws)
        _assert_voice_dub(ws)
        c = cfg(ws)
        src = source_path(ws)
        if not src or not os.path.exists(src):
            sys.exit(f"[aicn-audio] no source video in {ws} (source/master_source.mp4)")
        src_sha = _hash_file(src, fh)
        s_key = ap.stems_key(src_sha, c["demucs_model"], STEMS_VERSION)
        d = os.path.join(ws, "media", "stems")
        meta_p = os.path.join(d, "stems.json")
        g_dir = ap.global_cache_path("stems", s_key, "", hint_path=ws)
        g_voc, g_acc = os.path.join(g_dir, "vocals.wav"), os.path.join(g_dir, "accomp.wav")
        u_voc, u_acc = os.path.join(d, "vocals.wav"), os.path.join(d, "accomp.wav")

        if not force and _jload(meta_p, {}).get("key") == s_key and os.path.exists(u_voc) and os.path.exists(u_acc):
            print(f"[separate] cached ({os.path.basename(ws)}, key={s_key[:12]})")
            continue
        if not force and os.path.exists(g_voc) and os.path.exists(g_acc):
            _link_or_copy(g_voc, u_voc)
            _link_or_copy(g_acc, u_acc)
            _jdump(meta_p, {"key": s_key, "src_sha": src_sha, "model": c["demucs_model"], "backend": "global-cache", "seconds": 0.0})
            print(f"[separate] global-cache hit ({os.path.basename(ws)}, key={s_key[:12]})")
            continue
        misses.append((ws, c, src, src_sha, s_key, g_dir, g_voc, g_acc, u_voc, u_acc, meta_p))

    if not misses:
        return

    slot_ctx = ap.gpu_slot("demucs", vram_mb=1024, label="DEMUCS") if bk["engine"] in ("mlx", "torch-cuda") else contextlib.nullcontext()
    with slot_ctx:
        if bk["engine"] == "mlx":
            for ws, c, src, src_sha, s_key, g_dir, g_voc, g_acc, u_voc, u_acc, meta_p in misses:
                if not force and os.path.exists(g_voc) and os.path.exists(g_acc):
                    _link_or_copy(g_voc, u_voc)
                    _link_or_copy(g_acc, u_acc)
                    _jdump(meta_p, {"key": s_key, "src_sha": src_sha, "model": c["demucs_model"], "backend": "global-cache", "seconds": 0.0})
                    continue
                t0 = time.time()
                voc, acc = _separate_with_mlx(src, c["demucs_model"])
                os.makedirs(g_dir, exist_ok=True)
                _write_wav16(g_voc, voc, SR)
                _write_wav16(g_acc, acc, SR)
                _link_or_copy(g_voc, u_voc)
                _link_or_copy(g_acc, u_acc)
                _jdump(meta_p, {"key": s_key, "src_sha": src_sha, "model": c["demucs_model"], "backend": bk["engine"], "seconds": round(time.time() - t0, 1)})
                print(f"[separate] {os.path.basename(ws)} {c['demucs_model']} ({bk['label']}) -> media/stems in {time.time() - t0:.1f}s")
        else:
            import demucs.api
            import torch

            dev = "cuda" if bk["engine"] == "torch-cuda" else "cpu"
            by_model = defaultdict(list)
            for item in misses:
                by_model[item[1]["demucs_model"]].append(item)
            for model_name, items in by_model.items():
                sep = demucs.api.Separator(model=model_name, device=dev, shifts=1, split=True, overlap=0.25, progress=False)
                try:
                    for ws, c, src, src_sha, s_key, g_dir, g_voc, g_acc, u_voc, u_acc, meta_p in items:
                        if not force and os.path.exists(g_voc) and os.path.exists(g_acc):
                            _link_or_copy(g_voc, u_voc)
                            _link_or_copy(g_acc, u_acc)
                            _jdump(meta_p, {"key": s_key, "src_sha": src_sha, "model": model_name, "backend": "global-cache", "seconds": 0.0})
                            print(f"[separate] batch global-cache hit ({os.path.basename(ws)}, key={s_key[:12]})")
                            continue
                        t0 = time.time()
                        voc, acc = _separate_with_separator(sep, src, SR)
                        os.makedirs(g_dir, exist_ok=True)
                        _write_wav16(g_voc, voc, SR)
                        _write_wav16(g_acc, acc, SR)
                        _link_or_copy(g_voc, u_voc)
                        _link_or_copy(g_acc, u_acc)
                        _jdump(meta_p, {"key": s_key, "src_sha": src_sha, "model": model_name, "backend": bk["engine"], "seconds": round(time.time() - t0, 1)})
                        print(f"[separate] {os.path.basename(ws)} {model_name} ({bk['label']}) -> media/stems in {time.time() - t0:.1f}s")
                finally:
                    del sep
                    if dev == "cuda" and torch.cuda.is_available():
                        torch.cuda.empty_cache()


def cmd_separate(ws, c, fh=None):
    batch_separate([ws], force=bool(c.get("_force")), fh=fh)


# ---------------------------------------------------------------- dub (indivisible utterances)
_KNOWN_VOICES_CACHE = None


def _known_vieneu_voices():
    """Return set of valid VieNeu v3 Turbo preset voice IDs when vieneu assets are installed, else None."""
    global _KNOWN_VOICES_CACHE
    if _KNOWN_VOICES_CACHE is not None:
        return _KNOWN_VOICES_CACHE
    try:
        import importlib.util
        spec = importlib.util.find_spec("vieneu")
        if spec and spec.origin:
            v_json = os.path.join(os.path.dirname(spec.origin), "assets", "voices_v3_turbo.json")
            if os.path.isfile(v_json):
                raw = _jload(v_json, {}) or {}
                presets = raw.get("presets") if isinstance(raw.get("presets"), dict) else raw
                names = set(presets.keys()) if isinstance(presets, dict) else set()
                if isinstance(presets, dict):
                    for v_info in presets.values():
                        if isinstance(v_info, dict):
                            names.update(str(al) for al in (v_info.get("aliases") or []) if al)
                if names:
                    _KNOWN_VOICES_CACHE = names
                    return _KNOWN_VOICES_CACHE
    except Exception:
        pass
    _KNOWN_VOICES_CACHE = set()
    return _KNOWN_VOICES_CACHE


def resolve_speaker_voices(ws, c):
    """Build speaker -> VieNeu voice_id map from series_bible.json and config/audio_mix.json."""
    default_v = c.get("voice") or DEFAULTS["voice"]
    known = _known_vieneu_voices()
    mapping = {}
    hub = _resolve_hub(ws)
    bible_paths = []
    if hub:
        bible_paths.append(os.path.join(hub, "config", "series_bible.json"))
    bible_paths.append(os.path.join(ws, "config", "series_bible.json"))
    for bp in bible_paths:
        b = _jload(bp, {}) or {}
        for ch in b.get("characters") or []:
            if isinstance(ch, dict) and ch.get("speaker_id") and ch.get("voice_id"):
                vid = str(ch["voice_id"]).strip()
                if vid and (not known or vid in known):
                    mapping[str(ch["speaker_id"]).strip()] = vid
        if isinstance(b.get("voices"), dict):
            for spk, vid in b["voices"].items():
                vid_s = str(vid or "").strip()
                if spk and vid_s and (not known or vid_s in known):
                    mapping[str(spk).strip()] = vid_s
    if isinstance(c.get("voices"), dict):
        for spk, vid in c["voices"].items():
            vid_s = str(vid or "").strip()
            if spk and vid_s and (not known or vid_s in known):
                mapping[str(spk).strip()] = vid_s
    return mapping, default_v


def effective_seg_voice(seg, spk_map=None, default_voice=None):
    dv = default_voice or DEFAULTS["voice"]
    v = (seg.get("voice") or "").strip()
    if v:
        return v
    spk = str(seg.get("speaker") or "").strip()
    if spk and spk_map and spk in spk_map:
        return spk_map[spk]
    return dv


def group_lines(segs, max_gap=None, max_group_segs=None, max_group_dur=None, spk_map=None, default_voice=None):
    """Group adjacent clauses ONLY when continuous same-speaker/voice speech (gap <= 0.12s, <= 3 clauses, <= 4.5s)."""
    mg = DEFAULTS["group_max_gap_s"] if max_gap is None else float(max_gap)
    mc = DEFAULTS["max_group_clauses"] if max_group_segs is None else int(max_group_segs)
    md = DEFAULTS["max_group_span_s"] if max_group_dur is None else float(max_group_dur)
    dv = default_voice or DEFAULTS["voice"]
    groups, cur = [], []
    for i, s in enumerate(segs):
        if cur:
            p = segs[cur[-1]]
            chained = not re.search(r"[.!?…]\s*$", p["tts_text"].strip())
            v_cur = effective_seg_voice(s, spk_map, dv)
            v_prv = effective_seg_voice(p, spk_map, dv)
            same_spk = s.get("speaker") == p.get("speaker") and v_cur == v_prv
            gap = float(s["start"]) - float(p["end"])
            span = float(s["end"]) - float(segs[cur[0]]["start"])
            if chained and len(cur) < mc and span <= md and same_spk and gap <= mg:
                cur.append(i)
                continue
            groups.append(cur)
        cur = [i]
    if cur:
        groups.append(cur)
    return groups


def stretch(w, tempo):
    if abs(tempo - 1.0) < 0.005:
        return np.asarray(w, dtype=np.float32)
    with tempfile.TemporaryDirectory() as tmp:
        a, b = os.path.join(tmp, "a.wav"), os.path.join(tmp, "b.wav")
        sf.write(a, np.asarray(w, dtype=np.float32), SR, subtype="FLOAT")
        run([ap.find_tool("rubberband"), "-3", "-F", "-q", "--tempo", f"{tempo:.4f}", a, b])
        out, _ = sf.read(b, dtype="float32")
    return out if out.ndim == 1 else out.mean(1)


def _cosine_edge_fade(w, fade_s=0.010):
    out = np.asarray(w, dtype=np.float32).copy()
    f = min(int(fade_s * SR), len(out) // 4)
    if f > 0:
        ramp = (0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, f, dtype=np.float32))).astype(np.float32)
        out[:f] *= ramp
        out[-f:] *= ramp[::-1]
    return out


def clean(w, c):
    """Trim silence cleanly (|w| > 0.006 with 5ms guard, NO 30ms dead-silence pad), gate hiss, apply 10ms cosine fade."""
    w = np.asarray(w, dtype=np.float32)
    if w.ndim > 1:
        w = w.mean(1)
    on = np.flatnonzero(np.abs(w) > 0.006)
    if not len(on):
        return np.zeros(0, dtype=np.float32)
    guard = int(0.005 * SR)
    w = w[max(0, int(on[0]) - guard): min(len(w), int(on[-1]) + guard + 1)]
    k = max(1, int(0.01 * SR))
    env = np.convolve(np.abs(w), np.ones(k, dtype=np.float32) / k, "same")
    g = np.where(env < 10.0 ** (c["expander_thresh_db"] / 20.0), 10.0 ** (c["expander_depth_db"] / 20.0), 1.0).astype(np.float32)
    w = w * np.convolve(g, np.ones(k, dtype=np.float32) / k, "same").astype(np.float32)
    return _cosine_edge_fade(w, fade_s=0.010)


def _count_syllables(text):
    en_spans = re.findall(r"<en>(.*?)</en>", text)
    en_words = sum(len(s.split()) for s in en_spans)
    vi_clean = re.sub(r"[^\w\sÀ-ỹ]", " ", re.sub(r"<en>.*?</en>", "", text))
    vi_syls = len([w for w in vi_clean.split() if w])
    return max(1, vi_syls + en_words)


_TTS = None


def _tts_cuda_available():
    if os.environ.get("AICN_TTS_DEVICE", "").lower() == "cpu":
        return False
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _tts_init(threads=0, device="auto", max_batch_size=16):
    global _TTS
    from vieneu import Vieneu
    if device == "cuda":
        _TTS = Vieneu(mode="v3turbo", device="cuda", max_batch_size=int(max_batch_size))
    else:
        _TTS = Vieneu(mode="v3turbo", precision="fp32", threads=int(threads), device=device)


def _tts_job(job):
    text, voice, temp, top_k, path = job
    w = _TTS.infer(text=text, voice=voice, temperature=temp, top_k=top_k)
    _write_wav16(path, w, SR)
    return path


def presynth(jobs, workers, threads=0, batch_size=16):
    """Synthesize missing utterances under VRAM-aware gpu_slot (CUDA infer_batch) or CPU ONNX."""
    if not jobs:
        return
    if _tts_cuda_available():
        bs = max(1, int(batch_size or 16))
        by_cfg = defaultdict(list)
        for text, voice, temp, top_k, path in jobs:
            by_cfg[(voice, temp, top_k)].append((text, path))
        with ap.gpu_slot("vieneu", vram_mb=1024, label="VIENEU-TTS"):
            injected_tts = _TTS is not None
            if _TTS is None:
                _tts_init(device="cuda", max_batch_size=bs)
            try:
                for (voice, temp, top_k), items in by_cfg.items():
                    for start in range(0, len(items), bs):
                        chunk = items[start: start + bs]
                        texts = [t for t, _ in chunk]
                        wavs = _TTS.infer_batch(texts=texts, voice=voice, temperature=temp, top_k=top_k, batch_size=bs)
                        for (_, path), w in zip(chunk, wavs):
                            _write_wav16(path, w, SR)
            finally:
                if not injected_tts:
                    globals()["_TTS"] = None
                    try:
                        import gc
                        gc.collect()
                        if torch is not None and torch.cuda.is_available():
                            torch.cuda.empty_cache()
                    except Exception:
                        pass
        return
    if workers <= 1 or len(jobs) < 8:
        if _TTS is None:
            _tts_init(threads=0, device="cpu")
        for j in jobs:
            _tts_job(j)
        return
    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor
    n_w = min(workers, len(jobs))
    t_per_w = int(threads) if threads else max(1, (os.cpu_count() or 4) // n_w)
    with ProcessPoolExecutor(
        max_workers=n_w, mp_context=mp.get_context("spawn"),
        initializer=_tts_init, initargs=(t_per_w, "cpu"),
    ) as ex:
        list(ex.map(_tts_job, jobs))


def _resolve_hub(ws):
    pc = _jload(os.path.join(ws, "config", "project_config.json"), {}) or {}
    rel = pc.get("series_hub_rel")
    if rel:
        cand = os.path.normpath(os.path.join(ws, rel))
        if os.path.isdir(cand):
            return cand
    hub = pc.get("series_hub_dir") or ""
    if hub and os.path.isdir(hub):
        return hub
    cand = os.path.dirname(os.path.dirname(os.path.abspath(ws)))
    if os.path.basename(os.path.dirname(os.path.abspath(ws))) == "units" and os.path.isdir(os.path.join(cand, "units")):
        return cand
    return ""


def _resolve_tts_cached_path(ws, key):
    """Return global content-addressed tts_sent path, migrating legacy unit/hub copies if present."""
    dst = ap.global_cache_path("tts_sent", key, ".wav", hint_path=ws)
    if os.path.exists(dst):
        return dst
    hub = _resolve_hub(ws)
    for leg_base in ([os.path.join(hub, ".cache", "tts_sent")] if hub else []) + [os.path.join(ws, ".cache", "tts_sent")]:
        leg = os.path.join(leg_base, key + ".wav")
        if os.path.exists(leg) and os.path.abspath(leg) != os.path.abspath(dst):
            _link_or_copy(leg, dst)
            return dst
    return dst


def _resolve_tts_parallelism(c):
    cores = os.cpu_count() or 8
    w = int(c.get("tts_workers", 0) or 0)
    th = int(c.get("tts_threads", 0) or 0)
    if w <= 0:
        w = min(4, max(2, cores // 4))
    if th <= 0:
        th = max(2, cores // w)
    return w, th


def _dub_cache_key(ws, c, script_p, dur, spk_map=None):
    return ap.hash_obj({
        "v": AUDIO_VERSION,
        "script": ap.content_sha256(script_p),
        "dur": round(dur, 3),
        "spk_voices": spk_map or {},
        "cfg": {
            k: c.get(k) for k in (
                "voice", "temperature", "top_k", "group_max_gap_s", "max_group_clauses",
                "max_group_span_s", "base_tempo", "max_tempo", "max_tempo_short",
                "lead_gap_s", "max_overhang_s", "min_utterance_s",
                "expander_thresh_db", "expander_depth_db",
            )
        },
    })


def _dub_outputs_valid(ws, n_segs):
    dub_wav_p = os.path.join(ws, "media", "audio", "dub_voice_48k.wav")
    aligned_p = os.path.join(ws, "data", "03_aligned_subtitles.json")
    log_p = os.path.join(ws, "data", "03_dub_sentences.json")
    srt_p = os.path.join(ws, "data", "subtitles_full.srt")
    if not all(os.path.exists(p) for p in (dub_wav_p, aligned_p, log_p, srt_p)):
        return False
    log = _jload(log_p, []) or []
    aligned = _jload(aligned_p, []) or []
    if len(aligned) != n_segs or not log:
        return False
    for it in log:
        u_rel = it.get("utterance_wav") or f"media/tts_segments/utt_{int(it['utterance_id']):04d}.wav"
        if not os.path.exists(os.path.join(ws, u_rel)):
            return False
        cap_t = float(it.get("cap_tempo", 1.28))
        req_t = max(float(it.get("tempo", 1.0)), float(it.get("required_tempo", 1.0)))
        if bool(it.get("runt", False)) or float(it.get("overflow_s", 0.0)) > 0.05 or req_t > cap_t + 0.005:
            return False
    return True


def _plan_unit_dub(ws, c):
    """Load script, group continuous clauses, and enumerate TTS cache keys/paths for one unit."""
    _assert_voice_dub(ws)
    script_p = os.path.join(ws, "data", "02_bilingual_script.json")
    segs = _jload(script_p)
    if not segs:
        sys.exit(f"[aicn-audio] missing or empty {script_p}")
    dur = float(profile(ws)["duration"])
    spk_map, default_v = resolve_speaker_voices(ws, c)
    dub_key = _dub_cache_key(ws, c, script_p, dur, spk_map=spk_map)
    st_cache = _audio_state_load(ws)
    if not c.get("_force") and st_cache.get("dub_key") == dub_key and _dub_outputs_valid(ws, len(segs)):
        return {"cached": True, "ws": ws, "c": c, "segs": segs, "dur": dur, "dub_key": dub_key, "items": []}

    groups = group_lines(
        segs, c["group_max_gap_s"], c["max_group_clauses"], c["max_group_span_s"],
        spk_map=spk_map, default_voice=default_v,
    )
    items = []
    for gi, g in enumerate(groups):
        text = " ".join(segs[i]["tts_text"].strip() for i in g)
        voice = effective_seg_voice(segs[g[0]], spk_map=spk_map, default_voice=default_v)
        t_key = ap.tts_sent_key(text, voice, TTS_ENGINE_TAG, c["temperature"], c["top_k"])
        raw_p = _resolve_tts_cached_path(ws, t_key)
        items.append((gi, g, text, voice, t_key, raw_p))
    return {"cached": False, "ws": ws, "c": c, "segs": segs, "dur": dur, "dub_key": dub_key, "items": items}


def _assemble_unit_dub(plan, allow_overflow=False):
    """Clean raw WAVs, stretch once via global rb_stretch cache, place once onto dub_voice_48k.wav."""
    ws, c, segs, dur, dub_key, items = plan["ws"], plan["c"], plan["segs"], plan["dur"], plan["dub_key"], plan["items"]
    if plan["cached"]:
        print(f"[dub] cached ({os.path.basename(ws)}, {len(segs)} lines, key={dub_key[:12]})")
        return True, []

    t0 = time.time()
    n = int(round(dur * SR))
    wav_dir = os.path.join(ws, "media", "tts_segments")
    os.makedirs(wav_dir, exist_ok=True)
    # Remove legacy seg_*.wav or stale utt_*.wav left from earlier runs
    for fn in os.listdir(wav_dir):
        if fn.startswith("seg_") or (fn.startswith("utt_") and fn.endswith(".wav")):
            try:
                os.remove(os.path.join(wav_dir, fn))
            except OSError:
                pass

    base_t = float(c["base_tempo"])
    lead_gap = float(c["lead_gap_s"])
    max_overhang = float(c["max_overhang_s"])
    min_utt_s = float(c["min_utterance_s"])

    prep = []
    prev_est_end = -1.0
    for idx_pos, (gi, g, text, voice, t_key, raw_p) in enumerate(items):
        w_raw = clean(sf.read(raw_p, dtype="float32")[0], c)
        raw_dur = len(w_raw) / SR
        s0 = float(segs[g[0]]["start"])
        g_end = float(segs[g[-1]]["end"])
        start_u = s0 if prev_est_end < 0.0 else max(s0, prev_est_end + 0.03)
        next_start = float(segs[items[idx_pos + 1][1][0]]["start"]) if idx_pos + 1 < len(items) else dur
        eff_ceil = min(next_start - lead_gap, g_end + max_overhang) if idx_pos + 1 < len(items) else min(dur, g_end + max_overhang)
        avail_raw = eff_ceil - start_u
        avail = max(0.12, avail_raw)
        syl_cnt = _count_syllables(text)
        cap = float(c["max_tempo_short"]) if syl_cnt <= 2 else float(c["max_tempo"])
        req_tempo = raw_dur / avail if raw_dur > 0 else 1.0
        target_tempo = max(base_t, req_tempo) if raw_dur > 0 else 1.0
        stretch_tempo = min(cap, target_tempo)
        prev_est_end = start_u + (raw_dur / max(0.5, stretch_tempo))
        raw_wav_sha = hashlib.sha256(np.ascontiguousarray(w_raw, dtype=np.float32).tobytes()).hexdigest()
        rb_key = ap.rb_stretch_key(raw_wav_sha, stretch_tempo, RB_STRETCH_VERSION)
        rb_p = ap.global_cache_path("rb_stretch", rb_key, ".wav", hint_path=ws)
        prep.append((gi, g, text, w_raw, raw_dur, s0, g_end, next_start, eff_ceil, avail_raw, syl_cnt, cap, req_tempo, stretch_tempo, rb_p))

    unique_jobs = {}
    for it in prep:
        cp = it[14]
        if cp not in unique_jobs:
            unique_jobs[cp] = (it[3], it[13], cp)

    def _stretch_cached(job):
        w_raw, st_tempo, cp = job
        if len(w_raw) == 0:
            return w_raw, True
        if not c.get("_force") and os.path.exists(cp):
            return sf.read(cp, dtype="float32")[0], True
        out = stretch(w_raw, st_tempo)
        _write_wav16(cp, out, SR)
        return out, False

    job_list = list(unique_jobs.values())
    rb_workers = min(8, os.cpu_count() or 4, max(1, len(job_list)))
    with ThreadPoolExecutor(max_workers=rb_workers) as ex:
        unique_res = dict(zip((j[2] for j in job_list), ex.map(_stretch_cached, job_list)))
    stretched_res = [unique_res[it[14]] for it in prep]
    rb_hits = sum(1 for it in prep if unique_res[it[14]][1])

    track = np.zeros(n, dtype=np.float32)
    aligned = [None] * len(segs)
    log = []
    prev_sp_e = -1.0
    over_entries = []

    for idx_pos, (it, (piece_raw, _)) in enumerate(zip(prep, stretched_res)):
        gi, g, text, _w_raw, raw_dur, s0, g_end, next_start, eff_ceil, avail_raw, syl_cnt, cap, req_tempo, stretch_tempo, _cp = it
        piece = _cosine_edge_fade(piece_raw, fade_s=0.010)
        piece_dur = len(piece) / SR
        sp_s = s0 if prev_sp_e < 0.0 else max(s0, prev_sp_e + 0.03)
        a = min(n, max(0, int(round(sp_s * SR))))
        b = min(n, a + len(piece))
        if b > a:
            track[a:b] += piece[: b - a]
        sp_e = sp_s + piece_dur
        prev_sp_e = sp_e

        utt_rel = f"media/tts_segments/utt_{gi:04d}.wav"
        _write_wav16(os.path.join(ws, utt_rel), piece, SR)

        # Map clause start/end proportionally across the continuous indivisible group span for SRT/metadata
        g_span = max(0.05, g_end - s0)
        for j, i in enumerate(g):
            seg_s0, seg_e0 = float(segs[i]["start"]), float(segs[i]["end"])
            frac0 = max(0.0, min(1.0, (seg_s0 - s0) / g_span))
            frac1 = max(frac0, min(1.0, (seg_e0 - s0) / g_span))
            cl_sp_s = sp_s + frac0 * piece_dur
            cl_sp_e = sp_s + frac1 * piece_dur
            nxt_line = float(segs[i + 1]["start"]) if i + 1 < len(segs) else dur
            slot_end = min(dur, max(seg_e0, sp_e)) if i + 1 == len(segs) else max(seg_s0 + 0.05, min(max(seg_e0, cl_sp_e), nxt_line - 0.02))
            pause_after = max(0.0, nxt_line - sp_e) if (j + 1 == len(g)) else max(0.0, float(segs[i + 1]["start"]) - seg_e0)
            aligned[i] = {
                "id": segs[i].get("id", i),
                "slot_start": round(seg_s0, 3),
                "slot_end": round(slot_end, 3),
                "speech_start": round(cl_sp_s, 3),
                "speech_end": round(cl_sp_e, 3),
                "audio_end": round(cl_sp_e, 3),
                "utterance_id": gi,
                "group": gi,
                "utterance_wav": utt_rel,
                "utt_speech_start": round(sp_s, 3),
                "utt_speech_end": round(sp_e, 3),
                "pause_after": round(pause_after, 3),
                "tempo": round(stretch_tempo, 3),
                "tempo_ratio": round(stretch_tempo, 3),
                "zh_text": segs[i].get("zh_text", ""),
                "sub_text": segs[i]["sub_text"],
                "tts_text": segs[i]["tts_text"],
            }

        has_next = idx_pos + 1 < len(prep)
        over_ceil = sp_e - eff_ceil
        over_next = (sp_e - (next_start - 0.03)) if has_next else (sp_e - dur - 0.05)
        overflow_s = max(over_ceil, over_next)
        if req_tempo > cap + 0.005:
            overflow_s = max(overflow_s, raw_dur / cap - max(0.05, avail_raw))
        trim_syls = max(1, int(np.ceil(max(0.0, overflow_s) * 5.2 * cap))) if overflow_s > 0.0 else 0
        runt_bad = piece_dur < min_utt_s
        is_bad = runt_bad or (req_tempo > cap + 0.005) or (over_ceil > 0.05) or (over_next > 0.0)
        entry = {
            "utterance_id": gi,
            "group": gi,
            "ids": g,
            "utterance_wav": utt_rel,
            "start": round(s0, 3),
            "script_start": round(s0, 3),
            "script_end": round(g_end, 3),
            "speech_start": round(sp_s, 3),
            "speech_end": round(sp_e, 3),
            "natural_s": round(raw_dur, 3),
            "stretched_s": round(piece_dur, 3),
            "window_s": round(avail_raw, 3),
            "tempo": round(stretch_tempo, 3),
            "required_tempo": round(req_tempo, 3),
            "cap_tempo": round(cap, 3),
            "overflow_s": round(overflow_s, 3),
            "syl_count": syl_cnt,
            "syl_per_s": round(syl_cnt / max(0.05, piece_dur), 2),
            "suggested_trim_syls": trim_syls,
            "runt": runt_bad,
            "text": text,
        }
        log.append(entry)
        if is_bad:
            over_entries.append(entry)

    dub_wav_p = os.path.join(ws, "media", "audio", "dub_voice_48k.wav")
    aligned_p = os.path.join(ws, "data", "03_aligned_subtitles.json")
    log_p = os.path.join(ws, "data", "03_dub_sentences.json")
    srt_p = os.path.join(ws, "data", "subtitles_full.srt")
    _write_wav16(dub_wav_p, track, SR)
    _jdump(aligned_p, aligned)
    _jdump(log_p, log)
    tmp_srt = _tmp_path(srt_p, "tmp")
    try:
        with open(tmp_srt, "w", encoding="utf-8") as f:
            for k, it in enumerate(aligned, 1):
                f.write(f"{k}\n{srt_t(it['slot_start'])} --> {srt_t(it['slot_end'])}\n{it['sub_text']}\n\n")
        _atomic_replace(tmp_srt, srt_p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_srt)
        raise

    rates = [l["syl_per_s"] for l in log] or [0.0]
    tempos = [l["tempo"] for l in log] or [1.0]
    print(
        f"[dub] {os.path.basename(ws)}: {len(segs)} lines -> {len(items)} indivisible utterances, "
        f"rb_cached {rb_hits}/{len(prep)} in {time.time() - t0:.1f}s; "
        f"tempo median {np.median(tempos):.2f}x max {max(tempos):.2f}x; "
        f"rate median {np.median(rates):.2f} syl/s; overflow/runt {len(over_entries)}"
    )
    for l in over_entries[:10]:
        why = f"RUNT {l['stretched_s']:.3f}s < {min_utt_s}s" if l["runt"] else f"+{l['overflow_s']:.2f}s (req {l['required_tempo']:.2f}x > cap {l['cap_tempo']:.2f}x, trim ~{l['suggested_trim_syls']} syl)"
        print(f"   OVERFLOW utt {l['utterance_id']} ids {l['ids']} {why}: {l['text']}", file=sys.stderr)

    if over_entries:
        st_cache = _audio_state_load(ws)
        st_cache.pop("dub_key", None)
        _audio_state_save(ws, st_cache)
        return bool(allow_overflow), over_entries

    st_cache = _audio_state_load(ws)
    st_cache["dub_key"] = dub_key
    _audio_state_save(ws, st_cache)
    return True, over_entries


def batch_dub(units, force=False, allow_overflow=False, exit_on_overflow=True):
    """Synthesize missing utterances ONCE in GPU memory across all units, then assemble in parallel."""
    plans = []
    for ws in units:
        ws = os.path.abspath(ws)
        c = cfg(ws)
        if force:
            c["_force"] = True
        plans.append(_plan_unit_dub(ws, c))

    jobs, seen = [], set()
    ref_c = plans[0]["c"] if plans else DEFAULTS
    for pl in plans:
        if pl["cached"]:
            continue
        c = pl["c"]
        for _gi, _g, text, voice, _t_key, raw_p in pl["items"]:
            if not os.path.exists(raw_p) and raw_p not in seen:
                seen.add(raw_p)
                jobs.append((text, voice, c["temperature"], c["top_k"], raw_p))
    if jobs:
        tts_w, tts_th = _resolve_tts_parallelism(ref_c)
        presynth(jobs, tts_w, tts_th, batch_size=ref_c.get("tts_batch_size", 16))

    results = {}
    for pl in plans:
        ok, overs = _assemble_unit_dub(pl, allow_overflow=allow_overflow)
        results[pl["ws"]] = (ok, overs)
    bad = [os.path.basename(u) for u, (ok, _) in results.items() if not ok]
    if bad and exit_on_overflow:
        print(f"[aicn-audio] dub fail-fast OVERFLOW in units {bad} (aborted before mix/master; trim tts_text in Stage 2).", file=sys.stderr)
        sys.exit(2)
    return results


def cmd_dub(ws, c, fh=None):
    ws = os.path.abspath(ws)
    pl = _plan_unit_dub(ws, c)
    if not pl["cached"]:
        jobs, seen = [], set()
        for _gi, _g, text, voice, _t_key, raw_p in pl["items"]:
            if not os.path.exists(raw_p) and raw_p not in seen:
                seen.add(raw_p)
                jobs.append((text, voice, c["temperature"], c["top_k"], raw_p))
        if jobs:
            tts_w, tts_th = _resolve_tts_parallelism(c)
            presynth(jobs, tts_w, tts_th, batch_size=c.get("tts_batch_size", 16))
    ok, _ = _assemble_unit_dub(pl, allow_overflow=bool(c.get("_allow_overflow")))
    if not ok:
        sys.exit(2)


def srt_t(s):
    ms = int(round(s * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


# ---------------------------------------------------------------- mix (single-STFT + C-speed DSP)
NFFT, HOP = 2048, 512
EDGES = [0, 150, 300, 500, 800, 1200, 1800, 2600, 3600, 5000, 7000, 10000, 24001]
BAND_W = np.array([0.3, 0.5, 0.8, 1, 1, 1, 1, 1, 1, 0.8, 0.5, 0.3])
BAND_W_VOCAL = np.array([0.35, 0.60, 0.95, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 0.85, 0.55, 0.35])
SPEECH = slice(3, 9)   # 500 Hz - 5 kHz

SPEC_BANDS = [
    ("sub_rumble", 0, 80),
    ("bass_warmth", 80, 150),
    ("low_body", 150, 300),
    ("box_mud", 300, 500),
    ("vocal_fund", 500, 2000),
    ("presence", 2000, 5000),
    ("sibilance", 5000, 8000),
    ("air", 8000, 16000),
    ("ultrasonic", 16000, 24001),
]

_WIN_HANN = get_window("hann", NFFT, fftbins=True).astype(np.float32)
_WIN_SUM = np.float32(_WIN_HANN.sum())
_WIN_SQ = (_WIN_HANN * _WIN_HANN).astype(np.float32)
_FFT_WORKERS = min(4, os.cpu_count() or 1)
_WIN_HANN_T = torch.from_numpy(_WIN_HANN) if torch is not None else None


def apply_hp_lp(x, hp_hz=0.0, lp_hz=0.0, order=2):
    """Apply Butterworth high-pass and/or low-pass conditioning in one combined SOS pass along axis 0."""
    y = np.asarray(x, dtype=np.float32)
    nyq = 0.5 * SR
    sos_parts = []
    if hp_hz and 1.0 < float(hp_hz) < nyq * 0.95:
        sos_parts.append(butter(order, float(hp_hz), "hp", fs=SR, output="sos"))
    if lp_hz and 10.0 < float(lp_hz) < nyq * 0.98:
        sos_parts.append(butter(order, float(lp_hz), "lp", fs=SR, output="sos"))
    if not sos_parts:
        return y
    sos = np.vstack(sos_parts).astype(np.float32)
    return _sosfilt_parallel(sos, y).astype(np.float32, copy=False)


def hangover_hold(mask, fps, hold_s):
    """Bridge brief inactive gaps shorter than hold_s so ducking envelopes do not pump between clauses."""
    m = np.asarray(mask, dtype=bool)
    hold_frames = int(round(max(0.0, float(hold_s)) * fps))
    if hold_frames <= 1 or m.size == 0:
        return m
    out = m.copy()
    if out.ndim == 1:
        idx = np.flatnonzero(m)
        for a, b in zip(idx[:-1], idx[1:]):
            if (b - a - 1) <= hold_frames:
                out[a:b] = True
        return out
    for r in range(out.shape[0]):
        idx = np.flatnonzero(m[r])
        for a, b in zip(idx[:-1], idx[1:]):
            if (b - a - 1) <= hold_frames:
                out[r, a:b] = True
    return out


def _biquad_peaking(f0, gain_db, q, sr=SR):
    A = 10.0 ** (float(gain_db) / 40.0)
    w0 = 2.0 * np.pi * float(f0) / float(sr)
    alpha = np.sin(w0) / (2.0 * float(q))
    cw = np.cos(w0)
    b0, b1, b2 = 1.0 + alpha * A, -2.0 * cw, 1.0 - alpha * A
    a0, a1, a2 = 1.0 + alpha / A, -2.0 * cw, 1.0 - alpha / A
    return np.array([[b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]], dtype=np.float64)


def _biquad_highshelf(f0, gain_db, slope=0.7, sr=SR):
    A = 10.0 ** (float(gain_db) / 40.0)
    w0 = 2.0 * np.pi * float(f0) / float(sr)
    cw, sw = np.cos(w0), np.sin(w0)
    alpha = sw / 2.0 * np.sqrt(max(1e-6, (A + 1.0 / A) * (1.0 / float(slope) - 1.0) + 2.0))
    sq = 2.0 * np.sqrt(A) * alpha
    b0 = A * ((A + 1.0) + (A - 1.0) * cw + sq)
    b1 = -2.0 * A * ((A - 1.0) + (A + 1.0) * cw)
    b2 = A * ((A + 1.0) + (A - 1.0) * cw - sq)
    a0 = (A + 1.0) - (A - 1.0) * cw + sq
    a1 = 2.0 * ((A - 1.0) - (A + 1.0) * cw)
    a2 = (A + 1.0) - (A - 1.0) * cw - sq
    return np.array([[b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]], dtype=np.float64)


def spectral_profile_fast(x, sr=SR):
    """Fast non-overlapping strided Welch spectral balance, RMS/Peak dBFS, and crest factor (<0.25s on 10-min audio)."""
    arr = np.ascontiguousarray(x, dtype=np.float32)
    if torch is not None:
        xt = torch.from_numpy(arr)
        mt = xt if xt.ndim == 1 else ((xt[:, 0] + xt[:, 1]).mul_(0.5) if xt.shape[1] == 2 else xt.mean(dim=1))
        if len(mt) < NFFT:
            mt = F.pad(mt, (0, NFFT - len(mt)))
        peak = float(mt.abs().max() + 1e-12)
        peak_db = round(20.0 * np.log10(peak), 2)
        blk = max(1, sr // 50)
        n_blk = len(mt) // blk
        if n_blk > 0:
            b_rms_t = torch.sqrt(mt[: n_blk * blk].view(n_blk, blk).square().sum(dim=1).mul_(1.0 / blk) + 1e-15)
            act_b = b_rms_t > max(10.0 ** (-55.0 / 20.0), peak * 1e-3)
            act_n = int(act_b.sum())
            active_ratio = float(act_n / n_blk)
            rms = float(torch.sqrt(b_rms_t[act_b].square().sum() * (1.0 / act_n) + 1e-12)) if act_n else float(torch.sqrt(mt.square().sum() * (1.0 / len(mt)) + 1e-12))
        else:
            active_ratio = 1.0
            rms = float(torch.sqrt(mt.square().sum() * (1.0 / len(mt)) + 1e-12))
        rms_db = round(20.0 * np.log10(rms), 2)
        crest_db = round(peak_db - rms_db, 2)
        nf = len(mt) // NFFT
        step_f = max(1, nf // 2048) if nf > 4096 else 1
        Z_t = torch.fft.rfft(mt[: nf * NFFT].view(nf, NFFT)[::step_f] * _WIN_HANN_T, dim=1)
        Z2_t = Z_t.real * Z_t.real + Z_t.imag * Z_t.imag
        frame_e_t = Z2_t.sum(dim=1)
        act_f = frame_e_t > max(float(frame_e_t.max()) * 1e-5, 1e-12)
        fn = int(act_f.sum())
        mag2 = ((Z2_t[act_f].sum(dim=0, dtype=torch.float64) / fn) if fn else (Z2_t.sum(dim=0, dtype=torch.float64) / max(1, nf))).numpy()
    else:
        mono = arr if arr.ndim == 1 else arr.mean(axis=1)
        if len(mono) < NFFT:
            mono = np.pad(mono, (0, NFFT - len(mono)))
        peak = float(np.max(np.abs(mono)) + 1e-12)
        peak_db = round(20.0 * np.log10(peak), 2)
        blk = max(1, sr // 50)
        n_blk = len(mono) // blk
        if n_blk > 0:
            b_rms = np.sqrt(np.mean(mono[: n_blk * blk].reshape(n_blk, blk) ** 2, axis=1) + 1e-15)
            act_b = b_rms > max(10.0 ** (-55.0 / 20.0), peak * 1e-3)
            active_ratio = float(act_b.mean())
            rms = float(np.sqrt(np.mean(b_rms[act_b] ** 2) + 1e-12)) if act_b.any() else float(np.sqrt(np.mean(mono ** 2) + 1e-12))
        else:
            active_ratio = 1.0
            rms = float(np.sqrt(np.mean(mono ** 2) + 1e-12))
        rms_db = round(20.0 * np.log10(rms), 2)
        crest_db = round(peak_db - rms_db, 2)
        nf = len(mono) // NFFT
        frames = np.ascontiguousarray(mono[: nf * NFFT].reshape(nf, NFFT)) * _WIN_HANN[None, :]
        Z = sp_fft.rfft(frames, axis=1, workers=_FFT_WORKERS)
        Z2 = (Z.real * Z.real + Z.imag * Z.imag).astype(np.float64)
        frame_e = Z2.sum(axis=1)
        act_f = frame_e > max(float(frame_e.max()) * 1e-5, 1e-12)
        mag2 = Z2[act_f].mean(axis=0) if act_f.any() else Z2.mean(axis=0)
    f = np.fft.rfftfreq(NFFT, 1.0 / sr)
    tot = float(mag2.sum() + 1e-15)
    bands_pct, bands_db = {}, {}
    for name, lo, hi in SPEC_BANDS:
        sel = (f >= lo) & (f < hi)
        p = float(mag2[sel].sum()) if sel.any() else 0.0
        bands_pct[name] = round(100.0 * p / tot, 2)
        bands_db[name] = round(10.0 * np.log10(p / tot + 1e-12), 2)
    centroid_hz = round(float((f * mag2).sum() / tot), 1)
    return {
        "rms_dbfs": rms_db,
        "peak_dbfs": peak_db,
        "crest_factor_db": crest_db,
        "active_frame_ratio": round(active_ratio, 3),
        "spectral_centroid_hz": centroid_hz,
        "bands_pct": bands_pct,
        "bands_rel_db": bands_db,
    }


spectral_profile = spectral_profile_fast


def resolve_spectral_plan(stems_profiles, c, user_keys=None):
    ukeys = set(user_keys or ())
    dub_p = stems_profiles.get("dub", {})
    voc_p = stems_profiles.get("vocals", {})
    acc_p = stems_profiles.get("accomp", {})

    dub_hp = float(c.get("dub_hp_hz", DEFAULTS["dub_hp_hz"]))
    if "dub_hp_hz" not in ukeys and dub_p.get("bands_pct", {}).get("sub_rumble", 0.0) > 1.5:
        dub_hp = max(dub_hp, 100.0)
    dub_lp = float(c.get("dub_lp_hz", DEFAULTS["dub_lp_hz"]))
    mud_pct = dub_p.get("bands_pct", {}).get("box_mud", 0.0)
    dub_mud_cut_db = -1.5 if mud_pct > 28.0 else 0.0

    voc_hp = float(c.get("vocal_hp_hz", DEFAULTS["vocal_hp_hz"]))
    if "vocal_hp_hz" not in ukeys and (
        voc_p.get("bands_pct", {}).get("sub_rumble", 0.0) + voc_p.get("bands_pct", {}).get("bass_warmth", 0.0) > 8.0
    ):
        voc_hp = max(voc_hp, 120.0)
    voc_lp = float(c.get("vocal_lp_hz", DEFAULTS["vocal_lp_hz"]))
    voc_high_dip_db = float(c.get("vocal_high_dip_db", DEFAULTS["vocal_high_dip_db"]))
    if "vocal_high_dip_db" not in ukeys and (
        voc_p.get("bands_pct", {}).get("sibilance", 0.0) + voc_p.get("bands_pct", {}).get("air", 0.0) > 14.0
    ):
        voc_high_dip_db = min(voc_high_dip_db, -5.5)

    bgm_hp = float(c.get("bgm_hp_hz", DEFAULTS["bgm_hp_hz"]))
    bgm_lp = float(c.get("bgm_lp_hz", DEFAULTS["bgm_lp_hz"]))
    return {
        "dub_hp_hz": round(dub_hp, 1),
        "dub_lp_hz": round(dub_lp, 1),
        "dub_mud_cut_db": dub_mud_cut_db,
        "vocal_hp_hz": round(voc_hp, 1),
        "vocal_lp_hz": round(voc_lp, 1),
        "vocal_high_dip_db": round(voc_high_dip_db, 1),
        "bgm_hp_hz": round(bgm_hp, 1),
        "bgm_lp_hz": round(bgm_lp, 1),
        "bgm_sub_rumble_pct": acc_p.get("bands_pct", {}).get("sub_rumble", 0.0),
    }


def analyze_stems_and_dub(ws, c, V=None, B=None, T=None):
    stems_dir = os.path.join(ws, "media", "stems")
    raw_user = _jload(os.path.join(ws, "config", "audio_mix.json"), {}) or {}
    profiles = {}
    if V is None and os.path.exists(os.path.join(stems_dir, "vocals.wav")):
        V = sf.read(os.path.join(stems_dir, "vocals.wav"), dtype="float32", always_2d=True)[0]
    if B is None and os.path.exists(os.path.join(stems_dir, "accomp.wav")):
        B = sf.read(os.path.join(stems_dir, "accomp.wav"), dtype="float32", always_2d=True)[0]
    dub_p = os.path.join(ws, "media", "audio", "dub_voice_48k.wav")
    if T is None and os.path.exists(dub_p):
        T = sf.read(dub_p, dtype="float32")[0]
    if V is not None and len(V):
        profiles["vocals"] = spectral_profile_fast(V)
    if B is not None and len(B):
        profiles["accomp"] = spectral_profile_fast(B)
    if T is not None and len(T):
        profiles["dub"] = spectral_profile_fast(T)
    if not profiles and os.path.exists(source_path(ws) or ""):
        profiles["source"] = spectral_profile_fast(load(source_path(ws)))
    plan = resolve_spectral_plan(profiles, c, user_keys=raw_user.keys())
    report = {
        "analyzed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "version": AUDIO_VERSION,
        "stems": profiles,
        "filter_plan": plan,
    }
    _jdump(os.path.join(ws, "data", "03_spectral_analysis.json"), report)
    return report


def cmd_analyze(ws, c, fh=None):
    rep = analyze_stems_and_dub(ws, c)
    fp = rep["filter_plan"]
    print(f"[analyze] saved data/03_spectral_analysis.json | dub HP/LP={fp['dub_hp_hz']}/{fp['dub_lp_hz']} Hz, "
          f"vocal HP/LP={fp['vocal_hp_hz']}/{fp['vocal_lp_hz']} Hz (dyn_hi={fp['vocal_high_dip_db']} dB), "
          f"bgm HP/LP={fp['bgm_hp_hz']}/{fp['bgm_lp_hz']} Hz")


def voice_chain(x, hp_hz=90.0, lp_hz=16000.0, mud_cut_db=0.0):
    arr = np.asarray(x, dtype=np.float32)
    nyq = 0.5 * SR
    sos_list = []
    if hp_hz and 1.0 < float(hp_hz) < nyq * 0.95:
        sos_list.append(butter(2, float(hp_hz), "hp", fs=SR, output="sos"))
    if lp_hz and 10.0 < float(lp_hz) < nyq * 0.98:
        sos_list.append(butter(2, float(lp_hz), "lp", fs=SR, output="sos"))
    if mud_cut_db < -0.1:
        sos_list.append(_biquad_peaking(380.0, mud_cut_db, q=1.1, sr=SR))
    sos_list.append(_biquad_peaking(3100.0, 2.5, q=0.9, sr=SR))
    sos_list.append(_biquad_highshelf(8000.0, -2.0, slope=0.7, sr=SR))
    x = sosfilt(np.vstack(sos_list).astype(np.float32), _anti_denormal(arr, dtype=np.float32)).astype(np.float32)
    step = 480
    pad_n = (-len(x)) % step
    sq = np.pad(x * x, (0, pad_n)).reshape(-1, step).mean(axis=1)
    env_blk = np.sqrt(follow(sq[None, :], SR / step, 0.01, 0.15)[0] + 1e-12)
    lvl_blk = 20.0 * np.log10(env_blk + 1e-9)
    g_blk = 10.0 ** (np.where(lvl_blk > -20.0, (lvl_blk + 20.0) * (1.0 / 3.0 - 1.0), 0.0) / 20.0)
    return (x * g_blk.astype(np.float32).repeat(step)[: len(x)]).astype(np.float32)


_BAND_CACHE = None


def _band_indices():
    global _BAND_CACHE
    if _BAND_CACHE is None:
        f = np.fft.rfftfreq(NFFT, 1 / SR)
        idx = [np.flatnonzero((f >= a) & (f < b)) for a, b in zip(EDGES[:-1], EDGES[1:])]
        band_of_bin = np.zeros(len(f), dtype=np.int32)
        slices = []
        contiguous = True
        for k, ii in enumerate(idx):
            band_of_bin[ii] = k
            if len(ii) and np.array_equal(ii, np.arange(ii[0], ii[-1] + 1)):
                slices.append((int(ii[0]), int(ii[-1]) + 1))
            else:
                contiguous = False
        _BAND_CACHE = (idx, band_of_bin, tuple(slices) if contiguous else None)
    return _BAND_CACHE[0], _BAND_CACHE[1]


def _torch_istft(Z_stack, length, scale_win_sum=True):
    """Run C++ PyTorch center=True iSTFT with exact right-pad length alignment."""
    Z_in = (Z_stack * float(_WIN_SUM)) if scale_win_sum else Z_stack
    extra_r = _extra_right_pad(length)
    y = torch.istft(
        Z_in, n_fft=NFFT, hop_length=HOP, window=_WIN_HANN_T,
        center=True, length=length + extra_r,
    )[:, :length]
    return np.ascontiguousarray(y.t().numpy(), dtype=np.float32)


def _ola_from_Zs(Z_list, length):
    """Overlap-add iSTFT (PyTorch C++ center=True when available, SciPy OLA fallback) matching scipy.signal.istft to <1e-6."""
    n_ch = len(Z_list)
    if torch is not None:
        Z_stack = torch.stack([z if isinstance(z, torch.Tensor) else torch.from_numpy(np.asarray(z)) for z in Z_list], dim=0)
        return _torch_istft(Z_stack, length, scale_win_sum=True)

    nf = Z_list[0].shape[1]
    pad_l, pad_r = _pad_for_stft(length)
    total_len = length + pad_l + pad_r
    out = np.zeros((total_len, n_ch), dtype=np.float32)
    norm = np.zeros(total_len, dtype=np.float32)
    n_phases = NFFT // HOP
    for p in range(n_phases):
        cnt = len(range(p, nf, n_phases))
        if cnt:
            start = p * HOP
            norm[start: start + cnt * NFFT] += np.tile(_WIN_SQ, cnt)
    norm = np.maximum(norm, 1e-12)
    for ch, Z in enumerate(Z_list):
        y_f = sp_fft.irfft(Z.T * _WIN_SUM, n=NFFT, axis=1, workers=_FFT_WORKERS).astype(np.float32) * _WIN_HANN[None, :]
        for p in range(n_phases):
            sl = y_f[p::n_phases]
            if len(sl):
                start = p * HOP
                out[start: start + len(sl) * NFFT, ch] += sl.reshape(-1)
    return (out[pad_l: pad_l + length] / norm[pad_l: pad_l + length, None]).astype(np.float32)


def istft_from_Z(Zs, g, band_of_bin, length):
    return istft_combined([(Zs, g)], band_of_bin, length)


def istft_combined(stem_gain_pairs, band_of_bin, length):
    """Combine weighted STFTs linearly via contiguous band slices BEFORE running ONE iSTFT."""
    n_ch = len(stem_gain_pairs[0][0])
    nf = stem_gain_pairs[0][0][0].shape[1]
    if torch is not None:
        _band_indices()
        _, def_bob, band_slices = _BAND_CACHE
        use_slices = (
            band_slices is not None
            and len(stem_gain_pairs[0][1]) == len(band_slices)
            and np.array_equal(band_of_bin, def_bob)
        )
        Z_first = stem_gain_pairs[0][0]
        F_bins = Z_first[0].shape[0]
        if use_slices:
            Z_acc = torch.empty((n_ch, F_bins, nf), dtype=torch.complex64)
            g0_t = torch.from_numpy(np.ascontiguousarray(stem_gain_pairs[0][1][:, :nf], dtype=np.float32))
            Z0_st = Z_first if isinstance(Z_first, torch.Tensor) else torch.stack(
                [z if isinstance(z, torch.Tensor) else torch.from_numpy(np.asarray(z)) for z in Z_first], dim=0
            )
            for ch in range(n_ch):
                for k, (s, e) in enumerate(band_slices):
                    torch.mul(Z0_st[ch, s:e], g0_t[k], out=Z_acc[ch, s:e])
            if len(stem_gain_pairs) > 1:
                tmp_slice = torch.empty((max(e - s for s, e in band_slices), nf), dtype=torch.complex64)
            for Zs, g in stem_gain_pairs[1:]:
                gk_t = torch.from_numpy(np.ascontiguousarray(g[:, :nf], dtype=np.float32))
                Z_st = Zs if isinstance(Zs, torch.Tensor) else torch.stack(
                    [z if isinstance(z, torch.Tensor) else torch.from_numpy(np.asarray(z)) for z in Zs], dim=0
                )
                for ch in range(n_ch):
                    for k, (s, e) in enumerate(band_slices):
                        buf = tmp_slice[: e - s]
                        torch.mul(Z_st[ch, s:e], gk_t[k], out=buf)
                        Z_acc[ch, s:e].add_(buf)
        else:
            bob_t = torch.from_numpy(np.asarray(band_of_bin, dtype=np.int64))
            Z_acc = None
            for Zs, g in stem_gain_pairs:
                G_t = torch.from_numpy(np.ascontiguousarray(g[:, :nf], dtype=np.float32))[bob_t]
                Z_st = Zs if isinstance(Zs, torch.Tensor) else torch.stack(
                    [z if isinstance(z, torch.Tensor) else torch.from_numpy(np.asarray(z)) for z in Zs], dim=0
                )
                term = Z_st * G_t
                Z_acc = term if Z_acc is None else Z_acc.add_(term)
        Z_acc.mul_(float(_WIN_SUM))
        return _torch_istft(Z_acc, length, scale_win_sum=False)

    Z_sum = [None] * n_ch
    for Zs, g in stem_gain_pairs:
        G = g[band_of_bin, :nf].astype(np.float32)
        for ch in range(n_ch):
            term = Zs[ch] * G
            Z_sum[ch] = term if Z_sum[ch] is None else (Z_sum[ch] + term)
    return _ola_from_Zs(Z_sum, length)


def _pad_for_stft(length):
    pad_l = NFFT // 2
    rem = (length + pad_l - NFFT) % HOP
    pad_r = (HOP - rem) % HOP + NFFT // 2
    return pad_l, pad_r


def _extra_right_pad(length):
    pad_l = NFFT // 2
    rem = (length + pad_l - NFFT) % HOP
    return (HOP - rem) % HOP


def _band_matrix(idx):
    bmat = np.zeros((NFFT // 2 + 1, len(idx)), dtype=np.float32)
    for k, ii in enumerate(idx):
        bmat[ii, k] = 1.0
    return bmat


def stft_and_power(x, idx=None):
    """Compute complex64 STFT once (PyTorch C++ CPU with SciPy fallback) and return ((bands, frames) power, Zs, idx)."""
    if idx is None:
        idx, _ = _band_indices()
    arr = np.ascontiguousarray(x, dtype=np.float32)
    if arr.ndim == 1:
        arr = arr[:, None]
    n_raw, n_ch = arr.shape
    if torch is not None:
        extra_r = _extra_right_pad(n_raw)
        xt = torch.from_numpy(arr[:, 0]).unsqueeze(0) if n_ch == 1 else torch.from_numpy(arr).t().contiguous()
        xp = F.pad(xt, (0, extra_r)) if extra_r else xt
        Zt = torch.stft(
            xp, n_fft=NFFT, hop_length=HOP, window=_WIN_HANN_T,
            center=True, pad_mode="constant", return_complex=True,
        ) / float(_WIN_SUM)
        rv = torch.view_as_real(Zt)
        z2_all = rv[..., 0].square().add_(rv[..., 1].square())
        if n_ch == 1:
            Z2 = z2_all[0]
        elif n_ch == 2:
            Z2 = z2_all[0].add_(z2_all[1]).mul_(0.5)
        else:
            Z2 = z2_all.mean(dim=0)
        bmat_t = torch.from_numpy(_band_matrix(idx))
        P = (bmat_t.T @ Z2).numpy()
        return P, Zt, idx

    pad_l, pad_r = _pad_for_stft(n_raw)
    xp = np.pad(arr, ((pad_l, pad_r), (0, 0)))
    nf = (len(xp) - NFFT) // HOP + 1
    s0, s1 = xp.strides
    frames = np.lib.stride_tricks.as_strided(xp, shape=(nf, NFFT, n_ch), strides=(HOP * s0, s0, s1))
    Zs = []
    P = None
    bmat = _band_matrix(idx)
    for ch in range(n_ch):
        Z_ch = (sp_fft.rfft(frames[:, :, ch] * _WIN_HANN[None, :], axis=1, workers=_FFT_WORKERS) / _WIN_SUM).T
        Z_ch = np.ascontiguousarray(Z_ch, dtype=np.complex64)
        Zs.append(Z_ch)
        Z2 = Z_ch.real * Z_ch.real + Z_ch.imag * Z_ch.imag
        pc = bmat.T @ Z2
        P = pc if P is None else (P + pc)
    return P / n_ch, Zs, idx


def band_power_chunked(x, idx=None, chunk_frames=5625):
    """Low-RAM (<50 MiB) C++ PyTorch/SciPy chunked STFT band power, bit-exact to full-length stft_and_power."""
    if idx is None:
        idx, _ = _band_indices()
    arr = np.ascontiguousarray(x, dtype=np.float32)
    if arr.ndim == 1:
        arr = arr[:, None]
    n_raw, n_ch = arr.shape
    if n_raw <= chunk_frames * HOP:
        P, _, _ = stft_and_power(arr, idx)
        return P, idx
    pad_l, pad_r = _pad_for_stft(n_raw)
    n_frames = (n_raw + pad_l + pad_r - NFFT) // HOP + 1
    P = np.empty((len(idx), n_frames), dtype=np.float32)
    bmat = _band_matrix(idx)
    bmat_t = torch.from_numpy(bmat) if torch is not None else None
    for f0 in range(0, n_frames, chunk_frames):
        f1 = min(n_frames, f0 + chunk_frames)
        w0 = f0 * HOP - pad_l
        w1 = (f1 - 1) * HOP + NFFT - pad_l
        s0, s1 = max(0, w0), min(n_raw, w1)
        pl, pr = max(0, -w0), max(0, w1 - n_raw)
        seg = arr[s0:s1]
        if pl or pr:
            seg = np.pad(seg, ((pl, pr), (0, 0)))
        if torch is not None:
            xt = torch.from_numpy(seg[:, 0]).unsqueeze(0) if n_ch == 1 else torch.from_numpy(seg).t().contiguous()
            Zt = torch.stft(
                xt, n_fft=NFFT, hop_length=HOP, window=_WIN_HANN_T,
                center=False, return_complex=True,
            ) / float(_WIN_SUM)
            rv = torch.view_as_real(Zt)
            z2_all = rv[..., 0].square().add_(rv[..., 1].square())
            Z2 = z2_all[0] if n_ch == 1 else (z2_all[0].add_(z2_all[1]).mul_(0.5) if n_ch == 2 else z2_all.mean(dim=0))
            P[:, f0:f1] = (bmat_t.T @ Z2).numpy()[:, : f1 - f0]
        else:
            nf_c = f1 - f0
            st0, st1 = seg.strides
            frames = np.lib.stride_tricks.as_strided(seg, shape=(nf_c, NFFT, n_ch), strides=(HOP * st0, st0, st1))
            acc = None
            for ch in range(n_ch):
                Z_ch = (sp_fft.rfft(frames[:, :, ch] * _WIN_HANN[None, :], axis=1, workers=_FFT_WORKERS) / _WIN_SUM).T
                Z2 = (Z_ch.real * Z_ch.real + Z_ch.imag * Z_ch.imag).astype(np.float32)
                pc = bmat.T @ Z2
                acc = pc if acc is None else (acc + pc)
            P[:, f0:f1] = acc / n_ch
    return P, idx


def mix_stems_chunked(sig_gain_pairs, band_of_bin, length, idx=None, chunk_frames=5625):
    """Low-RAM (<140 MiB) halo-padded chunked combined STFT+iSTFT (<1e-6 vs full istft_combined)."""
    if idx is None:
        idx, _ = _band_indices()
    if length <= chunk_frames * HOP:
        z_pairs = [(stft_and_power(sig, idx)[1], g) for sig, g in sig_gain_pairs]
        return istft_combined(z_pairs, band_of_bin, length)
    first_sig = np.asarray(sig_gain_pairs[0][0], dtype=np.float32)
    is_1d = first_sig.ndim == 1
    n_ch = 1 if is_1d else first_sig.shape[1]
    out = np.empty((length, n_ch), dtype=np.float32)
    halo_f = 8
    total_hops = (length + HOP - 1) // HOP
    for f0 in range(0, total_hops, chunk_frames):
        s0 = f0 * HOP
        s1 = min(length, (f0 + chunk_frames) * HOP)
        f_lo = max(0, f0 - halo_f)
        lo_s = f_lo * HOP
        hi_s = min(length, (f0 + chunk_frames + halo_f) * HOP)
        z_pairs = []
        for sig, g in sig_gain_pairs:
            _, Z_blk, _ = stft_and_power(sig[lo_s:hi_s], idx)
            nf_blk = Z_blk[0].shape[1]
            g_blk = g[:, f_lo : f_lo + nf_blk]
            if g_blk.shape[1] < nf_blk:
                g_blk = np.pad(g_blk, ((0, 0), (0, nf_blk - g_blk.shape[1])), mode="edge")
            z_pairs.append((Z_blk, g_blk))
        y_blk = istft_combined(z_pairs, band_of_bin, hi_s - lo_s)
        out[s0:s1] = y_blk[s0 - lo_s : s1 - lo_s]
    return out[:, 0] if is_1d else out


def band_power(x):
    return band_power_chunked(x)


def apply_gain_chunked(x, g, band_of_bin, chunk_frames=5625):
    return mix_stems_chunked([(x, g)], band_of_bin, len(x), chunk_frames=chunk_frames)


def apply_gain(x, g, idx):
    _, band_of_bin = _band_indices()
    return mix_stems_chunked([(x, g)], band_of_bin, len(x), idx=idx)


def cmd_mix(ws, c, fh=None):
    _assert_voice_dub(ws)
    sd = os.path.join(ws, "media", "stems")
    voc_p, acc_p = os.path.join(sd, "vocals.wav"), os.path.join(sd, "accomp.wav")
    for f, p in (("vocals.wav", voc_p), ("accomp.wav", acc_p)):
        if not os.path.exists(p):
            sys.exit(f"[aicn-audio] missing media/stems/{f}: run `separate` (original audio is never replaced by silence)")
    dub_p = os.path.join(ws, "media", "audio", "dub_voice_48k.wav")
    pre_p = os.path.join(ws, "media", "audio", "premaster_48k.wav")
    stats_p = os.path.join(ws, "data", "03_mix_stats.json")
    spec_p = os.path.join(ws, "data", "03_spectral_analysis.json")
    dur = float(profile(ws)["duration"])
    if fh is None:
        with ThreadPoolExecutor(max_workers=3) as ex:
            hv, ha, hd = ex.submit(_hash_file, voc_p, fh), ex.submit(_hash_file, acc_p, fh), ex.submit(_hash_file, dub_p, fh)
            voc_h, acc_h, dub_h = hv.result(), ha.result(), hd.result()
    else:
        voc_h, acc_h, dub_h = _hash_file(voc_p, fh), _hash_file(acc_p, fh), _hash_file(dub_p, fh)
    mix_key = ap.hash_obj({
        "v": AUDIO_VERSION,
        "voc": voc_h,
        "acc": acc_h,
        "dub": dub_h,
        "dur": round(dur, 3),
        "cfg": {
            k: c.get(k) for k in (
                "dub_over_vocal_lu", "snr_vocal_db", "snr_bgm_db", "floor_vocal_db",
                "floor_bgm_db", "base_vocal_db", "speech_vocal_dip_db", "vocal_high_dip_db",
                "attack_s", "hold_s", "release_s", "dub_hp_hz", "dub_lp_hz",
                "vocal_hp_hz", "vocal_lp_hz", "bgm_hp_hz", "bgm_lp_hz",
            )
        },
    })
    st_cache = _audio_state_load(ws)
    if not c.get("_force") and st_cache.get("mix_key") == mix_key and all(os.path.exists(p) for p in (pre_p, stats_p, spec_p)):
        print(f"[mix] cached ({os.path.basename(ws)}, key={mix_key[:12]})")
        return

    n = int(round(dur * SR))
    fit = lambda a: a if len(a) == n else (a[:n] if len(a) > n else np.pad(a, ((0, n - len(a)), (0, 0))))
    with ThreadPoolExecutor(max_workers=3) as ex:
        fv_r = ex.submit(lambda: fit(sf.read(voc_p, dtype="float32", always_2d=True)[0]))
        fb_r = ex.submit(lambda: fit(sf.read(acc_p, dtype="float32", always_2d=True)[0]))
        ft_r = ex.submit(lambda: sf.read(dub_p, dtype="float32")[0])
        V, B, T = fv_r.result(), fb_r.result(), ft_r.result()
    if T.ndim > 1:
        T = T.mean(1)
    if len(T) > n:
        T = T[:n]
    elif len(T) < n:
        T = np.pad(T, (0, n - len(T)))

    t0 = time.time()
    spec_rep = analyze_stems_and_dub(ws, c, V=V, B=B, T=T)
    fp = spec_rep["filter_plan"]
    with ThreadPoolExecutor(max_workers=3) as ex:
        fv = ex.submit(apply_hp_lp, V, hp_hz=fp["vocal_hp_hz"], lp_hz=fp["vocal_lp_hz"], order=2)
        fb = ex.submit(apply_hp_lp, B, hp_hz=fp["bgm_hp_hz"], lp_hz=fp["bgm_lp_hz"], order=2)
        ft = ex.submit(voice_chain, T, hp_hz=fp["dub_hp_hz"], lp_hz=fp["dub_lp_hz"], mud_cut_db=fp["dub_mud_cut_db"])
        V, B, T = fv.result(), fb.result(), ft.result()

    with ThreadPoolExecutor(max_workers=2) as ex:
        flv = ex.submit(lufs, V)
        flt = ex.submit(lufs, T[:, None])
        lv = flv.result()
        lt_mono = flt.result()
        # T is added dual-mono to both stereo channels (mix += T[:, None]), which is +3.0103 LU louder than 1-ch mono;
        # guard quiet/whisper vocals (lv <= -60 LUFS) and clamp gain to [-12, +12] dB so dub never mutes or overdrives.
        lv_eff = -20.0 if lv <= -60.0 else max(lv, -30.0)
        lt_stereo = (lt_mono + 3.0103) if lt_mono > -65.0 else -20.0
        gain = float(np.clip(lv_eff + float(c["dub_over_vocal_lu"]) - lt_stereo, -12.0, 12.0))
    T = T * np.float32(10.0 ** (gain / 20.0))

    idx, band_of_bin = _band_indices()
    use_chunked_mix = n > 5625 * HOP
    if use_chunked_mix:
        PT, _ = band_power_chunked(T, idx)
        PV, _ = band_power_chunked(V, idx)
        PB, _ = band_power_chunked(B, idx)
        ZV = ZB = None
    else:
        PT, _, _ = stft_and_power(T, idx)
        PV, ZV, _ = stft_and_power(V, idx)
        del V
        PB, ZB, _ = stft_and_power(B, idx)
        del B
    fps = SR / HOP
    hold_s = float(c.get("hold_s", 0.30))

    pt_speech = PT[SPEECH].sum(0)
    pos_sp = pt_speech[pt_speech > 0.0]
    p95_sp = float(np.percentile(pos_sp, 95)) if len(pos_sp) else 0.0
    active_speech = pt_speech > max(p95_sp * 1e-3, 1e-10)
    raw_active = PT > np.maximum(PT.max(1, keepdims=True) * 10.0 ** (-45.0 / 10.0), 1e-12)
    speech_bands_mask = ((np.arange(len(BAND_W))[:, None] >= 2) & (np.arange(len(BAND_W))[:, None] < 10))
    raw_active_v = raw_active | (active_speech[None, :] & speech_bands_mask)

    tot = PT.sum(0)
    talk_mask = hangover_hold(tot > max(float(tot.max()) * 1e-4, 1e-10), fps, hold_s)
    talk = follow(talk_mask.astype(float)[None], fps, c["attack_s"], max(c["release_s"], 0.40))[0]

    def need(Pc, snr, floor, act_mask, ref_P=PT):
        g = np.sqrt(np.clip(ref_P / (Pc * 10.0 ** (snr / 10.0) + 1e-12), 10.0 ** (floor / 10.0), 1.0))
        return np.where(act_mask, g, 1.0)

    # Strong clean vocal carving whenever Vietnamese dub speaks, clamped directly at floor_vocal_db (no stacking)
    pt_v_ref = np.where(active_speech[None, :] & speech_bands_mask, np.maximum(PT, 0.15 * pt_speech[None, :]), PT)
    need_v = need(PV, c["snr_vocal_db"], c["floor_vocal_db"], raw_active_v, ref_P=pt_v_ref)

    base_dip = 1.0 - talk * (1.0 - 10.0 ** (float(c["base_vocal_db"]) / 20.0))
    sp_dip = np.ones((len(BAND_W), len(talk)), dtype=np.float64)
    sp_dip[2:9, :] = 1.0 - talk[None, :] * (1.0 - 10.0 ** (float(c["speech_vocal_dip_db"]) / 20.0))
    hi_dip_db = float(fp.get("vocal_high_dip_db", c.get("vocal_high_dip_db", -4.5)))
    hi_band = np.ones((len(BAND_W), len(talk)), dtype=np.float64)
    if hi_dip_db < -0.1:
        hi_band[9:, :] = 1.0 - talk[None, :] * (1.0 - 10.0 ** (hi_dip_db / 20.0))

    voc_floor_lin = 10.0 ** (float(c["floor_vocal_db"]) / 20.0)
    gV_raw = np.maximum(voc_floor_lin, np.minimum(need_v, np.minimum(base_dip[None, :], sp_dip)) * hi_band)
    gB_raw = need(PB, c["snr_bgm_db"], c["floor_bgm_db"], raw_active)

    gV = follow_hold(1.0 - BAND_W_VOCAL[:, None] * (1.0 - gV_raw), fps, c["attack_s"], hold_s, c["release_s"])
    gB = follow_hold(1.0 - BAND_W[:, None] * (1.0 - gB_raw), fps, c["attack_s"], hold_s, c["release_s"])

    if use_chunked_mix:
        mix = mix_stems_chunked([(V, gV), (B, gB)], band_of_bin, n, idx=idx)
        del V, B
    else:
        mix = istft_combined([(ZV, gV), (ZB, gB)], band_of_bin, n)
        del ZV, ZB
    if torch is not None and len(mix) >= SR * 10:
        mt_mix = torch.from_numpy(mix)
        mt_mix.add_(torch.from_numpy(np.ascontiguousarray(T, dtype=np.float32)).unsqueeze(1))
        pk_mix = float(mt_mix.abs().max())
        if pk_mix > 0.95:
            mt_mix.mul_(0.95 / pk_mix)
    else:
        mix += T[:, None]
        pk_mix = float(np.max(np.abs(mix))) if len(mix) else 0.0
        if pk_mix > 0.95:
            mix *= np.float32(0.95 / pk_mix)
    os.makedirs(os.path.join(ws, "media", "audio"), exist_ok=True)
    _write_wav16(pre_p, mix, SR, inplace=True)

    on = active_speech
    snr_on = pt_speech > max(p95_sp * 10.0 ** (-15.0 / 10.0), 1e-10)
    snr_sel = snr_on if snr_on.any() else active_speech
    comp = (PV[SPEECH] * gV[SPEECH] ** 2 + PB[SPEECH] * gB[SPEECH] ** 2).sum(0)
    snr = 10.0 * np.log10(PT[SPEECH].sum(0)[snr_sel] / (comp[snr_sel] + 1e-12) + 1e-12) if snr_sel.any() else np.array([99.0])
    stats = {
        "dub_gain_db": round(gain, 2),
        "orig_vocal_lufs": round(lv, 2),
        "dub_active_share": round(float(on.mean()), 3),
        "dub_snr_speech_db_median": round(float(np.median(snr)), 2),
        "dub_snr_speech_db_p10": round(float(np.percentile(snr, 10)), 2),
        "orig_vocal_atten_db_median_during_dub": round(float(np.median(20.0 * np.log10(gV[SPEECH][:, on].mean(0)))), 2) if on.any() else 0.0,
        "orig_vocal_atten_db_min": round(float(20.0 * np.log10(gV.min())), 2),
        "bgm_atten_db_median_during_dub": round(float(np.median(20.0 * np.log10(gB[SPEECH][:, on].mean(0)))), 2) if on.any() else 0.0,
        "bgm_atten_db_min": round(float(20.0 * np.log10(gB.min())), 2),
        "filter_plan": fp,
        "seconds": round(time.time() - t0, 1),
    }
    _jdump(stats_p, stats)
    st_cache = _audio_state_load(ws)
    st_cache["mix_key"] = mix_key
    _audio_state_save(ws, st_cache)
    print(f"[mix] {os.path.basename(ws)} " + json.dumps(stats))


# ---------------------------------------------------------------- master
def write_stage3_report(ws, checks, c):
    """Write 03_pacing_and_audio_report.md so verify_stage_3.py always hashes an up-to-date report."""
    spec = _jload(os.path.join(ws, "data", "03_spectral_analysis.json"), {}) or {}
    fp = spec.get("filter_plan", {})
    stems = spec.get("stems", {})
    lines = [
        "# Stage 3 Pacing & Audio Report (`voice_dub`)",
        "",
        f"- Engine: `{AUDIO_VERSION}` (indivisible utterances, single-STFT spectral mix)",
        f"- Target loudness: `{c['target_lufs']} LUFS`, peak `<= {GATE['true_peak_max_db']} dBFS`",
    ]
    if fp:
        lines += [
            "",
            "## Frequency Analysis & Pre-Mix HP/LP Conditioning (`data/03_spectral_analysis.json`)",
            f"- Dub HP/LP: `{fp.get('dub_hp_hz')} Hz` / `{fp.get('dub_lp_hz')} Hz` (mud trim `{fp.get('dub_mud_cut_db', 0.0)} dB`)",
            f"- Original Vocal HP/LP: `{fp.get('vocal_hp_hz')} Hz` / `{fp.get('vocal_lp_hz')} Hz`, base dip `{c.get('base_vocal_db')} dB`, speech dip `{c.get('speech_vocal_dip_db')} dB`, floor `{c.get('floor_vocal_db')} dB`",
            f"- SFX/Music HP/LP: `{fp.get('bgm_hp_hz')} Hz` / `{fp.get('bgm_lp_hz')} Hz`, envelope hold `{c.get('hold_s', 0.30)}s`, release `{c.get('release_s')}s`",
        ]
        for sname in ("vocals", "accomp", "dub"):
            sp = stems.get(sname)
            if sp:
                bp = sp.get("bands_pct", {})
                lines.append(
                    f"- Stem `{sname}`: RMS `{sp.get('rms_dbfs')} dBFS`, centroid `{sp.get('spectral_centroid_hz')} Hz`, "
                    f"sub<150Hz `{bp.get('sub_rumble', 0) + bp.get('bass_warmth', 0):.1f}%`, "
                    f"speech500-5k `{bp.get('vocal_fund', 0) + bp.get('presence', 0):.1f}%`, "
                    f"high>5k `{bp.get('sibilance', 0) + bp.get('air', 0):.1f}%`"
                )
    lines += ["", "## Audio Gate Checks", ""]
    for x in checks:
        lines.append(f"- [{'PASS' if x['passed'] else 'FAIL'}] **{x['id']}**: {x['detail']}")
    report_p = os.path.join(ws, "03_pacing_and_audio_report.md")
    tmp_p = _tmp_path(report_p, "tmp")
    try:
        with open(tmp_p, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        _atomic_replace(tmp_p, report_p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_p)
        raise


def cmd_master(ws, c, fh=None):
    _assert_voice_dub(ws)
    ff = ap.find_tool("ffmpeg")
    src = os.path.join(ws, "media", "audio", "premaster_48k.wav")
    out = os.path.join(ws, "media", "master_audio_48k.wav")
    dur = float(profile(ws)["duration"])
    if not os.path.exists(src):
        sys.exit("[aicn-audio] no premaster: run `mix`")

    master_key = ap.hash_obj({
        "v": AUDIO_VERSION,
        "src": _hash_file(src, fh),
        "dur": round(dur, 3),
        "cfg": {k: c.get(k) for k in ("target_lufs", "true_peak_db", "lra", "master_mode")},
    })
    st_cache = _audio_state_load(ws)
    if not c.get("_force") and st_cache.get("master_key") == master_key and os.path.exists(out):
        print(f"[master] cached ({os.path.basename(ws)}, key={master_key[:12]})")
        return

    pre = "acompressor=threshold=-18dB:ratio=2:attack=20:release=250:makeup=1"
    t0 = time.time()
    tmp_out = _tmp_path(out, "part.wav")
    try:
        if c.get("master_mode", "fast") == "loudnorm":
            ln = f"loudnorm=I={c['target_lufs']}:TP={c['true_peak_db']}:LRA={c['lra']}"
            o = subprocess.run([ff, "-hide_banner", "-nostats", "-i", src, "-af", f"{pre},{ln}:print_format=json", "-f", "null", "-"],
                               capture_output=True, text=True).stderr
            m = json.loads(o[o.rindex("{"): o.rindex("}") + 1])
            ln2 = (f"{ln}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}"
                   f":measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
            run([ff, "-v", "error", "-y", "-i", src, "-af", f"{pre},{ln2},aresample={SR},apad", "-t", f"{dur:.3f}",
                 "-ar", str(SR), "-ac", "2", "-c:a", "pcm_s24le", tmp_out])
            _atomic_replace(tmp_out, out)
            i, pk = lufs_file(out)
        else:
            o = subprocess.run([ff, "-hide_banner", "-nostats", "-i", src, "-af", f"{pre},ebur128=peak=true", "-f", "null", "-"],
                               capture_output=True, text=True).stderr
            hits = re.findall(r"I:\s+(-?[\d.]+) LUFS", o)
            meas_i = float(hits[-1]) if hits else float(c["target_lufs"])
            gain_db = float(c["target_lufs"]) - meas_i
            lim = 10.0 ** ((float(c["true_peak_db"]) - 0.55) / 20.0)
            af2 = f"{pre},volume={gain_db:.3f}dB,alimiter=limit={lim:.5f}:level=false:attack=5:release=50:asc=false,apad"
            run([ff, "-v", "error", "-y", "-i", src, "-af", af2, "-t", f"{dur:.3f}",
                 "-ar", str(SR), "-ac", "2", "-c:a", "pcm_s24le", tmp_out])
            _atomic_replace(tmp_out, out)
            i, pk = lufs_file(out)
            if pk > float(GATE["true_peak_max_db"]):
                lim_tp = 10.0 ** ((float(c["true_peak_db"]) - 0.1) / 20.0)
                af_tp = f"{pre},volume={gain_db:.3f}dB,aresample=192000,alimiter=limit={lim_tp:.5f}:level=false:attack=5:release=50:asc=false,aresample={SR},apad"
                run([ff, "-v", "error", "-y", "-i", src, "-af", af_tp, "-t", f"{dur:.3f}",
                     "-ar", str(SR), "-ac", "2", "-c:a", "pcm_s24le", tmp_out])
                _atomic_replace(tmp_out, out)
                i, pk = lufs_file(out)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_out)
        raise

    st_cache = _audio_state_load(ws)
    st_cache["master_key"] = master_key
    _audio_state_save(ws, st_cache)
    print(f"[master] {out}: {i:.1f} LUFS, peak {pk:.1f} dBFS ({time.time() - t0:.1f}s)")


# ---------------------------------------------------------------- verify
def _run_audio_checks(ws, c, fh=None):
    out = os.path.join(ws, "media", "master_audio_48k.wav")
    checks = []

    def chk(name, ok, detail):
        checks.append({"id": name, "passed": bool(ok), "detail": detail})

    i, pk = lufs_file(out) if os.path.exists(out) else (-99.0, 0.0)
    chk("loudness", abs(i - float(c["target_lufs"])) <= GATE["lufs_tol"] and pk <= GATE["true_peak_max_db"],
        f"{i:.1f} LUFS (target {c['target_lufs']}±{GATE['lufs_tol']}), peak {pk:.1f} dBFS (max {GATE['true_peak_max_db']})")

    meta = _jload(os.path.join(ws, "media", "stems", "stems.json"), {}) or {}
    cur = ap.stems_key(_hash_file(source_path(ws), fh), c["demucs_model"], STEMS_VERSION)
    chk("stems_match_source", meta.get("key") == cur, "media/stems built from the current source")

    st = _jload(os.path.join(ws, "data", "03_mix_stats.json"), {}) or {}
    snr_med = float(st.get("dub_snr_speech_db_median", -99.0))
    snr_p10 = float(st.get("dub_snr_speech_db_p10", -99.0))
    chk("dub_intelligible",
        snr_med >= GATE["dub_snr_median_min_db"] and snr_p10 >= GATE["dub_snr_p10_min_db"],
        f"dub over original in 500 Hz-5 kHz (active speech): median {snr_med} dB (>={GATE['dub_snr_median_min_db']}), p10 {snr_p10} dB (>={GATE['dub_snr_p10_min_db']})")

    chk("original_never_muted",
        st.get("orig_vocal_atten_db_min", -99.0) >= float(c["floor_vocal_db"]) - GATE["vocal_floor_margin_db"]
        and st.get("bgm_atten_db_min", -99.0) >= float(c["floor_bgm_db"]) - GATE["vocal_floor_margin_db"],
        f"deepest cut: vocal {st.get('orig_vocal_atten_db_min')} dB (floor {c['floor_vocal_db']}), music/sfx {st.get('bgm_atten_db_min')} dB (floor {c['floor_bgm_db']})")

    acc = sf.read(os.path.join(ws, "media", "stems", "accomp.wav"), dtype="float32", always_2d=True)[0]
    voc = sf.read(os.path.join(ws, "media", "stems", "vocals.wav"), dtype="float32", always_2d=True)[0]
    dub = sf.read(os.path.join(ws, "media", "audio", "dub_voice_48k.wav"), dtype="float32")[0]
    mst = sf.read(out, dtype="float32", always_2d=True)[0] if os.path.exists(out) else np.zeros((1, 2), dtype=np.float32)
    n = min(len(acc), len(voc), len(dub), len(mst))
    k = SR // 10
    if n >= k:
        e = lambda x: (x[: n // k * k] ** 2).reshape(-1, k, x.shape[1] if x.ndim > 1 else 1).mean((1, 2))
        quiet = e(dub[:n, None]) < 1e-7
        orig_e = e(acc[:n] + voc[:n])
        m_e = e(mst[:n])
        sel = quiet & (orig_e > np.percentile(orig_e, 30))
        ratio = 10.0 * np.log10(m_e[sel].mean() / (orig_e[sel].mean() + 1e-12) + 1e-12) if sel.any() else -99.0
    else:
        ratio = -99.0
    chk("original_present", ratio >= GATE["orig_presence_min_db"],
        f"master vs original where no dub: {ratio:.1f} dB (min {GATE['orig_presence_min_db']})")

    log = _jload(os.path.join(ws, "data", "03_dub_sentences.json"), []) or []
    min_utt_s = float(c.get("min_utterance_s", 0.12))
    over = [l for l in log if float(l.get("overflow_s", 0.0)) > 0.05]
    tempo_bad = [
        l for l in log
        if max(float(l.get("tempo", 1.0)), float(l.get("required_tempo", 1.0))) > float(l.get("cap_tempo", c["max_tempo"])) + 0.005
    ]
    runt_bad = []
    utt_overlap_bad = []
    for idx_u, l in enumerate(log):
        u_rel = l.get("utterance_wav") or f"media/tts_segments/utt_{int(l.get('utterance_id', idx_u)):04d}.wav"
        up = os.path.join(ws, u_rel)
        if not os.path.exists(up):
            runt_bad.append((idx_u, "missing"))
        else:
            info_u = sf.info(up)
            u_arr = sf.read(up, dtype="float32")[0]
            u_pk = float(np.max(np.abs(u_arr))) if len(u_arr) else 0.0
            if info_u.duration < min_utt_s or u_pk <= 0.005:
                runt_bad.append((idx_u, round(info_u.duration, 3), round(u_pk, 4)))
        if idx_u + 1 < len(log):
            e_cur = float(l.get("speech_end", 0.0))
            s_nxt = float(log[idx_u + 1].get("speech_start", 0.0))
            if e_cur > s_nxt - 0.02 + 1e-6:
                utt_overlap_bad.append((idx_u, round(e_cur, 3), round(s_nxt, 3)))

    chk("dub_fits_and_intact",
        bool(log) and not over and not tempo_bad and not runt_bad and not utt_overlap_bad,
        f"{len(log)} utterances, max tempo {max([l.get('tempo', 1.0) for l in log], default=0):.2f}x, "
        f"overflow={len(over)}, tempo_bad={len(tempo_bad)}, runt_bad={runt_bad[:3]}, utt_overlaps={utt_overlap_bad[:3]}")

    script_segs = _jload(os.path.join(ws, "data", "02_bilingual_script.json"), []) or []
    aligned_segs = _jload(os.path.join(ws, "data", "03_aligned_subtitles.json"), []) or []
    sync_bad = []
    slot_overlap_bad = []
    min_d, max_d = 0.0, 0.0
    if log:
        for l in log:
            d = float(l["speech_start"]) - float(l["script_start"])
            min_d, max_d = min(min_d, d), max(max_d, d)
            if d < -0.10 or d > 0.50:
                sync_bad.append((l.get("utterance_id"), round(d, 3)))
    for idx in range(len(aligned_segs) - 1):
        if float(aligned_segs[idx]["slot_end"]) > float(aligned_segs[idx + 1]["slot_start"]) - 0.005:
            slot_overlap_bad.append(idx)
    chk("dub_sync",
        len(aligned_segs) == len(script_segs) and not sync_bad and not slot_overlap_bad,
        f"utterance speech_start vs script_start in [{min_d:+.3f}s, {max_d:+.3f}s] (allowed [-0.10, +0.50]), bad={sync_bad[:5]}, slot_overlaps={slot_overlap_bad[:5]}")

    return checks


def cmd_verify(ws, c, fh=None):
    _assert_voice_dub(ws)
    checks = _run_audio_checks(ws, c, fh)
    passed = all(x["passed"] for x in checks)
    pub_params = {k: v for k, v in c.items() if not k.startswith("_")}
    _jdump(os.path.join(ws, "data", "03_mix_report.json"), {
        "passed": passed,
        "version": AUDIO_VERSION,
        "checks": checks,
        "params": pub_params,
    })
    write_stage3_report(ws, checks, pub_params)
    for x in checks:
        print(f"  [{'PASS' if x['passed'] else 'FAIL'}] {x['id']}: {x['detail']}")
    if not passed:
        sys.exit("[aicn-audio] audio gate FAILED: Stage 3 not signed")
    from verify_stage_3 import run_stage_3_verification
    run_stage_3_verification(ws)


def cmd_batch_all(units, force=False, allow_overflow=False):
    """Warm multi-unit pipeline: batch-separate -> batch-dub (fail-fast per unit) -> parallel CPU mix/master/verify."""
    units = [os.path.abspath(u) for u in units]
    batch_separate(units, force=force)
    dub_res = batch_dub(units, force=force, allow_overflow=allow_overflow, exit_on_overflow=False)
    ready = [u for u in units if dub_res[u][0]]
    failed = [os.path.basename(u) for u in units if not dub_res[u][0]]

    def _finish_one(ws):
        c = cfg(ws)
        if force:
            c["_force"] = True
        cmd_mix(ws, c)
        cmd_master(ws, c)
        cmd_verify(ws, c)
        return os.path.basename(ws)

    if ready:
        workers = min(4, os.cpu_count() or 2, len(ready))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(_finish_one, ready))
    if failed:
        print(f"[aicn-audio] batch-all aborted mix/master for overflowing units {failed}; trim tts_text in Stage 2.", file=sys.stderr)
        sys.exit(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cmd", choices=["separate", "dub", "analyze", "mix", "master", "verify", "all",
                                        "batch-separate", "batch-dub", "batch-all"])
    parser.add_argument("--unit", default=None, help="Single unit workspace path")
    parser.add_argument("--units", default=None, help="Comma-separated unit workspace paths for batch-* commands")
    parser.add_argument("--force", action="store_true", help="Ignore content-hash caches and recompute")
    parser.add_argument("--allow-overflow", action="store_true", help="Write dub output even on overflow (diagnostics only)")
    a = parser.parse_args()

    if a.cmd.startswith("batch-"):
        if not a.units:
            sys.exit(f"[aicn-audio] {a.cmd} requires --units u1,u2,...")
        units = [x.strip() for x in a.units.split(",") if x.strip()]
        if a.cmd == "batch-separate":
            batch_separate(units, force=a.force)
        elif a.cmd == "batch-dub":
            batch_dub(units, force=a.force, allow_overflow=a.allow_overflow, exit_on_overflow=not a.allow_overflow)
        else:
            cmd_batch_all(units, force=a.force, allow_overflow=a.allow_overflow)
        return

    if not a.unit:
        sys.exit(f"[aicn-audio] {a.cmd} requires --unit <workspace>")
    ws = os.path.abspath(a.unit)
    _assert_voice_dub(ws)
    c = cfg(ws)
    if a.force:
        c["_force"] = True
    if a.allow_overflow:
        c["_allow_overflow"] = True
    steps = {
        "separate": [cmd_separate],
        "dub": [cmd_dub],
        "analyze": [cmd_analyze],
        "mix": [cmd_mix],
        "master": [cmd_master],
        "verify": [cmd_verify],
        "all": [cmd_separate, cmd_dub, cmd_mix, cmd_master, cmd_verify],
    }
    for f in steps[a.cmd]:
        f(ws, c, None)


if __name__ == "__main__":
    main()
