#!/usr/bin/env python3
"""
aicn_audio.py - Stage 3 audio for AICN: separation, sentence-level dub, frequency-aware mix, master.

  separate  Demucs (MLX) on the unit source -> media/stems/{vocals,accomp}.wav   (cached by source sha256)
  dub       sentence-level VieNeu TTS -> rubberband tempo -> media/audio/dub_voice_48k.wav
            + per-line media/tts_segments/seg_NNNN.wav + data/03_aligned_subtitles.json  (cached per sentence)
  mix       per-band, per-frame ducking of the ORIGINAL stems under the dub (never muted)
  master    glue compression + 2-pass loudnorm -> media/master_audio_48k.wav
  verify    audio gate (data/03_mix_report.json), then the Stage 3 gate (verify_stage_3.py)
  all       separate -> dub -> mix -> master -> verify

Run with the VieNeu venv python ($AICN_PY). Parameters: config/audio_mix.json overrides DEFAULTS.
In sub_only mode there is no dub: the original stereo is mastered and verified.
"""
import argparse, json, os, re, subprocess, sys, tempfile, threading, time
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import aicn  # noqa: E402  (jload, jdump, h_obj, FastHash, cicd)
import aicn_platform as ap  # noqa: E402

SR = 48000
AUDIO_VERSION = "aicn-audio-1.3"
STEMS_VERSION = "aicn-audio-1.0"  # unchanged Demucs separation; keep cache-compatible with v1.2.x stems
DEFAULTS = {
    "voice": "Trúc Ly",
    "tts_workers": 0, "tts_threads": 0,         # 0=auto-scale CPU fp32 ONNX workers (2x4 on 8-core M1, up to 4x4 on 16+ core desktop)
    "tts_batch_size": 16,                       # CUDA PyTorch static batch size (single process, ~0.5-0.8 GB VRAM, 3-4x faster than multi-proc)
    "temperature": 0.8, "top_k": 25,            # VieNeu v3 Turbo defaults (same as its app)
    "group_max_gap_s": 0.6,                     # clauses of one speaker closer than this are read as one sentence
    "base_tempo": 1.10,                         # model reads ~5.2 syl/s; 1.10 brings it to ~5.7
    "max_tempo": 1.25,                          # rubberband R3, formant preserved
    "lead_gap_s": 0.10,                         # keep this much silence before the next sentence
    "expander_thresh_db": -50.0, "expander_depth_db": -15.0,   # removes the TTS hiss floor between words
    "dub_over_vocal_lu": 1.5,                   # dub loudness relative to the original vocal stem
    "snr_vocal_db": 12.0, "snr_bgm_db": 6.0,    # wanted dub-to-original ratio per band while the dub talks
    "floor_vocal_db": -20.0, "floor_bgm_db": -9.0,   # the original is never turned down further than this
    "base_vocal_db": -6.5,                      # gentler broadband dip of original voice while keeping SFX natural
    "vocal_high_dip_db": -3.5,                  # dynamic >5 kHz vocal sibilance softening ONLY while dub speaks
    "attack_s": 0.02, "hold_s": 0.35, "release_s": 0.42,   # attack-hold-release eliminates inter-clause pumping
    "dub_hp_hz": 90.0, "dub_lp_hz": 16000.0,    # pre-mix HP/LP conditioning for dubbed voice
    "vocal_hp_hz": 95.0, "vocal_lp_hz": 0.0,    # gentle vocal sub-rumble HP; LP=0 preserves air when dub is silent
    "bgm_hp_hz": 35.0, "bgm_lp_hz": 18500.0,    # keep SFX/foley/music warm & full-bodied, strip subsonic DC only
    "target_lufs": -14.0, "true_peak_db": -1.0, "lra": 11.0,
    "master_mode": "fast",                      # fast (ebur128+alimiter, ~4s, 28x faster) | loudnorm (legacy 192kHz 2-pass, ~115s)
    "demucs_model": "htdemucs",
}
GATE = {"lufs_tol": 1.0, "dub_snr_min_db": 8.0, "orig_presence_min_db": -6.0, "vocal_floor_margin_db": 6.0}


# ---------------------------------------------------------------- helpers
def cfg(ws):
    c = dict(DEFAULTS)
    c.update(aicn.jload(os.path.join(ws, "config", "audio_mix.json"), {}) or {})
    return c


def profile(ws):
    return aicn.jload(os.path.join(ws, "data", "00_video_profile.json"), {})


def source_path(ws):
    p = os.path.join(ws, "source", "master_source.mp4")
    return p if os.path.exists(p) else profile(ws).get("source_video", "")


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, **kw)
    if r.returncode:
        sys.exit(f"[aicn-audio] command failed: {' '.join(map(str, cmd))[:200]}\n{r.stderr[-800:]}")
    return r


def load(p, ch=2, sr=SR):
    raw = run([ap.find_tool("ffmpeg"), "-v", "error", "-i", p, "-f", "f32le", "-ar", str(sr), "-ac", str(ch), "pipe:1"]).stdout
    return np.frombuffer(raw, np.float32).reshape(-1, ch).copy()


def lufs_file(p):
    o = subprocess.run([ap.find_tool("ffmpeg"), "-nostats", "-i", p, "-af", "ebur128=peak=true", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
    i = float(re.findall(r"I:\s+(-?[\d.]+) LUFS", o)[-1])
    pk = re.findall(r"Peak:\s+(-?[\d.inf]+) dBFS", o)
    return i, (float(pk[-1]) if pk and pk[-1] not in ("-inf", "inf") else -99.0)


def lufs(x):
    """Measure integrated LUFS by piping float32 PCM directly into ffmpeg (zero temp-file disk I/O)."""
    arr = np.asarray(x, dtype=np.float32)
    ch = 1 if arr.ndim == 1 else arr.shape[1]
    proc = subprocess.run(
        [ap.find_tool("ffmpeg"), "-nostats", "-f", "f32le", "-ar", str(SR), "-ac", str(ch),
         "-i", "pipe:0", "-af", "ebur128=peak=true", "-f", "null", "-"],
        input=np.ascontiguousarray(arr).tobytes(), capture_output=True,
    )
    o = proc.stderr.decode("utf-8", errors="replace")
    hits = re.findall(r"I:\s+(-?[\d.]+) LUFS", o)
    return float(hits[-1]) if hits else -70.0


def follow(g, fps, att, rel):
    """Envelope follower over the last axis: fast down (attack), slow up (release)."""
    a_att, a_rel = np.exp(-1 / (att * fps)), np.exp(-1 / (rel * fps))
    g = np.atleast_2d(g); out = np.empty_like(g); cur = np.ones(g.shape[0])
    for t in range(g.shape[1]):
        tgt = g[:, t]; a = np.where(tgt < cur, a_att, a_rel)
        cur = a * cur + (1 - a) * tgt; out[:, t] = cur
    return out


def follow_hold(g, fps, att, hold_s, rel):
    """Attack-hold-release gain follower over the last axis: fast attack down, flat hold during micro-pauses, smooth release up."""
    a_att = np.exp(-1.0 / max(1e-6, att * fps))
    a_rel = np.exp(-1.0 / max(1e-6, rel * fps))
    hold_frames = max(0, int(round(max(0.0, float(hold_s)) * fps)))
    g = np.atleast_2d(np.asarray(g, dtype=np.float64))
    out = np.empty_like(g)
    cur = np.ones(g.shape[0], dtype=np.float64)
    rem = np.zeros(g.shape[0], dtype=np.int32)
    for t in range(g.shape[1]):
        tgt = g[:, t]
        attacking = tgt < cur
        holding = (~attacking) & (rem > 0)
        releasing = (~attacking) & (~holding)
        cur = np.where(
            attacking,
            a_att * cur + (1.0 - a_att) * tgt,
            np.where(releasing, a_rel * cur + (1.0 - a_rel) * tgt, cur),
        )
        rem = np.where(attacking, hold_frames, np.maximum(0, rem - 1))
        out[:, t] = cur
    return out


def _write_wav16(path, arr, sr=SR):
    """Write compact lossless 16-bit PCM WAV atomically (-50% disk vs FLOAT32, -96 dB dynamic range)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.part.wav"
    sf.write(tmp, np.clip(np.asarray(arr, dtype=np.float32), -1.0, 0.999969), sr, subtype="PCM_16")
    for attempt in range(6):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if os.path.exists(path) and os.path.getsize(path) > 44:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                return
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, path)


# ---------------------------------------------------------------- separate
def cmd_separate(ws, c, fh):
    import contextlib
    src = source_path(ws)
    if not src or not os.path.exists(src):
        sys.exit("[aicn-audio] no source video (source/master_source.mp4)")
    key = aicn.h_obj({"src": fh(src), "model": c["demucs_model"], "v": STEMS_VERSION})
    d = os.path.join(ws, "media", "stems"); meta_p = os.path.join(d, "stems.json")
    meta = aicn.jload(meta_p, {})
    if meta.get("key") == key and all(os.path.exists(os.path.join(d, f)) for f in ("vocals.wav", "accomp.wav")):
        print("[separate] cached"); return
    os.makedirs(d, exist_ok=True); t0 = time.time()
    bk = ap.detect_demucs_backend(sys.executable)
    if bk["engine"] == "missing":
        sys.exit("[aicn-audio] Demucs not available: install `demucs` in AICN_PY or install `uv` (for uvx)")
    lock_ctx = ap.gpu_lock() if bk["engine"] in ("mlx", "torch-cuda") else contextlib.nullcontext()
    with lock_ctx:
        with tempfile.TemporaryDirectory() as tmp:
            wav = os.path.join(tmp, "source.wav")
            run([ap.find_tool("ffmpeg"), "-v", "error", "-y", "-i", src, "-vn", "-ac", "2", "-ar", "44100", wav])
            if bk["engine"] == "mlx":
                run([ap.find_tool("uvx"), "--from", "demucs-mlx", "demucs-mlx", "-n", c["demucs_model"], "-o", tmp, wav])
            else:
                dev = "cuda" if bk["engine"] == "torch-cuda" else "cpu"
                run([sys.executable, "-m", "demucs.separate", "--two-stems=vocals", "--device", dev,
                     "-n", c["demucs_model"], "-o", tmp, wav])
            sd = os.path.join(tmp, c["demucs_model"], "source")
            if not os.path.isdir(sd):
                sd = os.path.join(tmp, "source")
            voc = load(os.path.join(sd, "vocals.wav"))
            no_voc = os.path.join(sd, "no_vocals.wav")
            acc = load(no_voc) if os.path.exists(no_voc) else sum(load(os.path.join(sd, f"{s}.wav")) for s in ("drums", "bass", "other"))
    _write_wav16(os.path.join(d, "vocals.wav"), voc, SR)
    _write_wav16(os.path.join(d, "accomp.wav"), acc, SR)
    aicn.jdump(meta_p, {"key": key, "source": src, "model": c["demucs_model"], "backend": bk["engine"], "seconds": round(time.time() - t0, 1)})
    print(f"[separate] {c['demucs_model']} ({bk['label']}) -> media/stems in {time.time() - t0:.1f}s")


# ---------------------------------------------------------------- dub
def group_lines(segs, max_gap, max_group_segs=4, max_group_dur=8.0):
    """Comma-chained clauses of one speaker become one sentence (TTS reads short fragments badly),
    capped in count and span so prosody stays natural without risking multi-clause drift."""
    groups, cur = [], []
    for i, s in enumerate(segs):
        if cur:
            p = segs[cur[-1]]
            chained = not re.search(r"[.!?…]\s*$", p["tts_text"].strip())
            span_ok = (float(s["end"]) - float(segs[cur[0]]["start"])) <= max_group_dur
            if (chained and len(cur) < max_group_segs and span_ok
                    and s.get("speaker") == p.get("speaker") and float(s["start"]) - float(p["end"]) < max_gap):
                cur.append(i); continue
            groups.append(cur)
        cur = [i]
    if cur:
        groups.append(cur)
    return groups


def stretch(w, tempo):
    if abs(tempo - 1.0) < 0.005:
        return w
    with tempfile.TemporaryDirectory() as tmp:
        a, b = os.path.join(tmp, "a.wav"), os.path.join(tmp, "b.wav")
        sf.write(a, w, SR, subtype="FLOAT")
        run([ap.find_tool("rubberband"), "-3", "-F", "-q", "--tempo", f"{tempo:.4f}", a, b])
        out, _ = sf.read(b, dtype="float32")
    return out if out.ndim == 1 else out.mean(1)


def clean(w, c):
    w = w.astype(np.float32)
    on = np.flatnonzero(np.abs(w) > 0.008)
    if not len(on):
        return w
    w = w[max(0, on[0] - int(0.03 * SR)): on[-1] + int(0.03 * SR)]
    k = int(0.01 * SR)
    env = np.convolve(np.abs(w), np.ones(k) / k, "same")
    g = np.where(env < 10 ** (c["expander_thresh_db"] / 20), 10 ** (c["expander_depth_db"] / 20), 1.0)
    w = w * np.convolve(g, np.ones(k) / k, "same").astype(np.float32)
    f = min(int(0.015 * SR), len(w) // 4)
    if f:
        w[:f] *= np.linspace(0, 1, f); w[-f:] *= np.linspace(1, 0, f)
    return w


def split_points(w, weights):
    """Cut a sentence into its clauses: proportional to syllables, snapped to the quietest 10 ms nearby."""
    k = int(0.01 * SR); env = np.convolve(np.abs(w), np.ones(k) / k, "same")
    cuts, tot, acc = [0], sum(weights), 0
    for wt in weights[:-1]:
        acc += wt; guess = int(len(w) * acc / tot); r = int(0.25 * SR)
        lo, hi = max(cuts[-1] + k, guess - r), min(len(w) - k, guess + r)
        cuts.append(lo + int(np.argmin(env[lo:hi])) if hi > lo else guess)
    return cuts + [len(w)]


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
    """Synthesize missing sentences.
    - CUDA GPU: single in-process PyTorch instance under machine-wide gpu_lock using infer_batch()
      (avoids multi-process RAM/VRAM duplication & ~3-4x faster at ~0.5 GB VRAM).
    - CPU / Apple Silicon: ONNX fp32 ProcessPoolExecutor when >=8 jobs."""
    if not jobs:
        return
    if _tts_cuda_available():
        bs = max(1, int(batch_size or 16))
        from collections import defaultdict
        by_cfg = defaultdict(list)
        for j in jobs:
            text, voice, temp, top_k, path = j
            by_cfg[(voice, temp, top_k)].append((text, path))
        with ap.gpu_lock():
            if _TTS is None:
                _tts_init(device="cuda", max_batch_size=bs)
            for (voice, temp, top_k), items in by_cfg.items():
                for start in range(0, len(items), bs):
                    chunk = items[start:start + bs]
                    texts = [t for t, _ in chunk]
                    wavs = _TTS.infer_batch(texts=texts, voice=voice, temperature=temp, top_k=top_k, batch_size=bs)
                    for (_, path), w in zip(chunk, wavs):
                        _write_wav16(path, w, SR)
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
    with ProcessPoolExecutor(max_workers=n_w, mp_context=mp.get_context("spawn"),
                             initializer=_tts_init, initargs=(t_per_w, "cpu")) as ex:
        list(ex.map(_tts_job, jobs))


def _tts_cache_dir(ws):
    """Return persistent cross-series TTS cache (<project_root>/.cache/tts_sent) so pruning a series hub never wipes cached voices."""
    d = os.path.join(ap.global_cache_dir(ws), "tts_sent")
    os.makedirs(d, exist_ok=True)
    return d


def _resolve_tts_cached_path(ws, cache_dir, key):
    """Return target cache path, migrating from legacy <hub>/.cache/tts_sent or <ws>/.cache/tts_sent if present."""
    dst = os.path.join(cache_dir, key + ".wav")
    if os.path.exists(dst):
        return dst
    hub = aicn.resolve_unit_hub(ws)
    for leg_base in ([os.path.join(hub, ".cache", "tts_sent")] if hub else []) + [os.path.join(ws, ".cache", "tts_sent")]:
        leg = os.path.join(leg_base, key + ".wav")
        if os.path.exists(leg) and os.path.abspath(leg) != os.path.abspath(dst):
            try:
                os.link(leg, dst)
            except OSError:
                import shutil
                shutil.copy2(leg, dst)
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


def _audio_state_load(ws):
    return aicn.jload(os.path.join(ws, ".cache", "audio_state.json"), {}) or {}


def _audio_state_save(ws, st):
    aicn.jdump(os.path.join(ws, ".cache", "audio_state.json"), st)


def cmd_dub(ws, c, fh):
    script_p = os.path.join(ws, "data", "02_bilingual_script.json")
    segs = aicn.jload(script_p)
    dur = float(profile(ws)["duration"]); n = int(round(dur * SR))
    dub_wav_p = os.path.join(ws, "media", "audio", "dub_voice_48k.wav")
    aligned_p = os.path.join(ws, "data", "03_aligned_subtitles.json")
    log_p = os.path.join(ws, "data", "03_dub_sentences.json")
    srt_p = os.path.join(ws, "data", "subtitles_full.srt")
    wav_dir = os.path.join(ws, "media", "tts_segments"); os.makedirs(wav_dir, exist_ok=True)
    dub_key = None
    if fh is not None and os.path.exists(script_p):
        dub_key = aicn.h_obj({
            "v": AUDIO_VERSION, "script": fh(script_p), "dur": round(dur, 3),
            "cfg": {k: c.get(k) for k in ("voice", "temperature", "top_k", "group_max_gap_s", "base_tempo",
                                          "max_tempo", "lead_gap_s", "expander_thresh_db", "expander_depth_db")},
        })
        st_cache = _audio_state_load(ws)
        outs_ok = (
            all(os.path.exists(p) for p in (dub_wav_p, aligned_p, log_p, srt_p))
            and all(os.path.exists(os.path.join(wav_dir, f"seg_{i:04d}.wav")) for i in range(len(segs)))
        )
        if not c.get("_force") and st_cache.get("dub_key") == dub_key and outs_ok:
            print(f"[dub] cached ({len(segs)} lines, key={dub_key[:12]})")
            return

    groups = group_lines(segs, c["group_max_gap_s"])
    cache = _tts_cache_dir(ws)
    rb_cache = os.path.join(ws, ".cache", "rb_clause"); os.makedirs(rb_cache, exist_ok=True)
    track = np.zeros(n, np.float32); aligned = [None] * len(segs); log = []; synth = 0; t0 = time.time()
    jobs, seen = [], set()
    for g in groups:
        text = " ".join(segs[i]["tts_text"].strip() for i in g)
        voice = segs[g[0]].get("voice") or c["voice"]
        key = aicn.h_obj({"t": text, "v": voice, "e": aicn.TTS_ENGINE_TAG, "temp": c["temperature"], "k": c["top_k"]})
        p_ = _resolve_tts_cached_path(ws, cache, key)
        if not os.path.exists(p_) and p_ not in seen:
            seen.add(p_); jobs.append((text, voice, c["temperature"], c["top_k"], p_))
    tts_w, tts_th = _resolve_tts_parallelism(c)
    presynth(jobs, tts_w, tts_th, batch_size=c.get("tts_batch_size", 16)); synth = len(jobs)
    # Prepare raw cleaned waves, split into per-clause pieces, and run rubberband time-stretch with per-clause cache
    prep_clauses = []
    group_meta = []
    for gi, g in enumerate(groups):
        text = " ".join(segs[i]["tts_text"].strip() for i in g)
        voice = segs[g[0]].get("voice") or c["voice"]
        key = aicn.h_obj({"t": text, "v": voice, "e": aicn.TTS_ENGINE_TAG, "temp": c["temperature"], "k": c["top_k"]})
        raw_p = _resolve_tts_cached_path(ws, cache, key)
        w_raw = clean(sf.read(raw_p, dtype="float32")[0], c)
        st = float(segs[g[0]]["start"])
        nxt_grp = float(segs[groups[gi + 1][0]]["start"]) if gi + 1 < len(groups) else dur - 0.05
        window = max(0.25, nxt_grp - c["lead_gap_s"] - st)
        natural = len(w_raw) / SR
        cuts = split_points(w_raw, [max(1, len(segs[i]["tts_text"].split())) for i in g])
        c_start_idx = len(prep_clauses)
        for j, i in enumerate(g):
            raw_piece = w_raw[cuts[j]:cuts[j + 1]]
            s0 = float(segs[i]["start"])
            nxt_line = float(segs[i + 1]["start"]) if i + 1 < len(segs) else dur - 0.05
            gap = c["lead_gap_s"] if (j + 1 == len(g)) else 0.03
            win_j = max(0.25, nxt_line - gap - s0)
            nat_j = len(raw_piece) / SR
            tempo_j = max(c["base_tempo"], min(c["max_tempo"], nat_j / win_j)) if len(raw_piece) > 480 else 1.0
            rb_k = aicn.h_obj({"sk": key, "a": int(cuts[j]), "b": int(cuts[j + 1]), "t": round(tempo_j, 4)})[:20]
            prep_clauses.append((gi, j, i, raw_piece, tempo_j, os.path.join(rb_cache, rb_k + ".wav")))
        group_meta.append((gi, g, text, st, window, natural, c_start_idx, len(prep_clauses)))

    rb_hits = 0

    def _stretch_cached(it):
        raw_p, tempo_j, cp = it[3], it[4], it[5]
        if len(raw_p) <= 480:
            return raw_p, True
        if not c.get("_force") and os.path.exists(cp):
            return sf.read(cp, dtype="float32")[0], True
        out = stretch(raw_p, tempo_j)
        _write_wav16(cp, out, SR)
        return out, False

    rb_workers = min(8, os.cpu_count() or 4, max(1, len(prep_clauses)))
    with ThreadPoolExecutor(max_workers=rb_workers) as ex:
        res_clauses = list(ex.map(_stretch_cached, prep_clauses))
    stretched_clauses = [r[0] for r in res_clauses]
    rb_hits = sum(1 for r in res_clauses if r[1])
    prev_sp_e = -1.0
    for gi, g, text, st, window, natural, c0, c1 in group_meta:
        piece_tempos = []
        piece_durs = 0.0
        for idx_c in range(c0, c1):
            _, j, i, _raw_p, tempo_j, _cp = prep_clauses[idx_c]
            piece = stretched_clauses[idx_c].astype(np.float32, copy=True)
            f = min(int(0.005 * SR), len(piece) // 4)
            if f > 0:
                piece[:f] *= np.linspace(0, 1, f, dtype=np.float32)
                piece[-f:] *= np.linspace(1, 0, f, dtype=np.float32)
            s0, e0 = float(segs[i]["start"]), float(segs[i]["end"])
            sp_s = max(s0, prev_sp_e + 0.02) if prev_sp_e >= 0.0 else s0
            a = min(n, max(0, int(round(sp_s * SR)))); b = min(n, a + len(piece))
            if b > a:
                track[a:b] += piece[: b - a]
            sf.write(os.path.join(wav_dir, f"seg_{i:04d}.wav"), piece if len(piece) > 480 else np.zeros(4800, np.float32), SR)
            sp_e = sp_s + len(piece) / SR
            prev_sp_e = sp_e
            piece_tempos.append(tempo_j)
            piece_durs += len(piece) / SR
            nxt_line = float(segs[i + 1]["start"]) if i + 1 < len(segs) else dur
            slot_end = max(s0 + 0.15, min(max(e0, sp_e), nxt_line - 0.04))
            aligned[i] = {"id": i, "slot_start": round(s0, 3), "slot_end": round(slot_end, 3),
                          "speech_start": round(sp_s, 3), "speech_end": round(sp_e, 3), "audio_end": round(sp_e, 3),
                          "pause_after": round(max(0.0, nxt_line - sp_e), 3), "tempo": round(tempo_j, 3),
                          "group": gi, "zh_text": segs[i].get("zh_text", ""), "sub_text": segs[i]["sub_text"],
                          "tts_text": segs[i]["tts_text"]}
        overflow = prev_sp_e - (st + window)
        syl = len(text.split())
        log.append({"group": gi, "ids": g, "start": round(st, 3), "natural_s": round(natural, 2), "window_s": round(window, 2),
                    "tempo": round(max(piece_tempos, default=1.0), 3), "overflow_s": round(overflow, 2),
                    "syl_per_s": round(syl / max(0.05, piece_durs), 2), "text": text})
    os.makedirs(os.path.dirname(dub_wav_p), exist_ok=True)
    _write_wav16(dub_wav_p, track, SR)
    aicn.jdump(aligned_p, aligned)
    aicn.jdump(log_p, log)
    with open(srt_p, "w", encoding="utf-8") as f:
        for k, it in enumerate(aligned, 1):
            f.write(f"{k}\n{srt_t(it['slot_start'])} --> {srt_t(it['slot_end'])}\n{it['sub_text']}\n\n")
    if dub_key is not None:
        st_cache = _audio_state_load(ws)
        st_cache["dub_key"] = dub_key
        _audio_state_save(ws, st_cache)
    over = [l for l in log if l["overflow_s"] > 0.05]
    rates = [l["syl_per_s"] for l in log]
    print(f"[dub] {len(segs)} lines -> {len(groups)} sentences, synth {synth}, rb_cached {rb_hits}/{len(prep_clauses)} in {time.time() - t0:.1f}s; "
          f"tempo median {np.median([l['tempo'] for l in log]):.2f}x max {max(l['tempo'] for l in log):.2f}x; "
          f"rate median {np.median(rates):.2f} syl/s; overflow {len(over)}")
    for l in over[:10]:
        print(f"   OVERFLOW group {l['group']} ids {l['ids']} +{l['overflow_s']}s at max tempo: shorten -> {l['text']}")


def srt_t(s):
    ms = int(round(s * 1000)); h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); sec, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


# ---------------------------------------------------------------- mix
NFFT, HOP = 2048, 512
EDGES = [0, 150, 300, 500, 800, 1200, 1800, 2600, 3600, 5000, 7000, 10000, 24001]
BAND_W = np.array([0.3, 0.5, 0.8, 1, 1, 1, 1, 1, 1, 0.8, 0.5, 0.3])   # speech bands get full treatment
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


def apply_hp_lp(x, hp_hz=0.0, lp_hz=0.0, order=2):
    """Apply clean Butterworth high-pass and/or low-pass conditioning along time axis 0."""
    from scipy.signal import butter, sosfilt
    y = np.asarray(x, dtype=np.float32)
    nyq = 0.5 * SR
    if hp_hz and 1.0 < float(hp_hz) < nyq * 0.95:
        sos_hp = butter(order, float(hp_hz), "hp", fs=SR, output="sos").astype(np.float32)
        y = sosfilt(sos_hp, y, axis=0).astype(np.float32)
    if lp_hz and 10.0 < float(lp_hz) < nyq * 0.98:
        sos_lp = butter(order, float(lp_hz), "lp", fs=SR, output="sos").astype(np.float32)
        y = sosfilt(sos_lp, y, axis=0).astype(np.float32)
    return y


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
    """RBJ Audio EQ Cookbook peaking biquad returned as a (1, 6) SOS array (zero comb-filtering)."""
    A = 10.0 ** (float(gain_db) / 40.0)
    w0 = 2.0 * np.pi * float(f0) / float(sr)
    alpha = np.sin(w0) / (2.0 * float(q))
    cw = np.cos(w0)
    b0 = 1.0 + alpha * A
    b1 = -2.0 * cw
    b2 = 1.0 - alpha * A
    a0 = 1.0 + alpha / A
    a1 = -2.0 * cw
    a2 = 1.0 - alpha / A
    return np.array([[b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]], dtype=np.float64)


def _biquad_highshelf(f0, gain_db, slope=0.7, sr=SR):
    """RBJ Audio EQ Cookbook high-shelf biquad returned as a (1, 6) SOS array."""
    A = 10.0 ** (float(gain_db) / 40.0)
    w0 = 2.0 * np.pi * float(f0) / float(sr)
    cw = np.cos(w0)
    sw = np.sin(w0)
    alpha = sw / 2.0 * np.sqrt(max(1e-6, (A + 1.0 / A) * (1.0 / float(slope) - 1.0) + 2.0))
    sq = 2.0 * np.sqrt(A) * alpha
    b0 = A * ((A + 1.0) + (A - 1.0) * cw + sq)
    b1 = -2.0 * A * ((A - 1.0) + (A + 1.0) * cw)
    b2 = A * ((A + 1.0) + (A - 1.0) * cw - sq)
    a0 = (A + 1.0) - (A - 1.0) * cw + sq
    a1 = 2.0 * ((A - 1.0) - (A + 1.0) * cw)
    a2 = (A + 1.0) - (A - 1.0) * cw - sq
    return np.array([[b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]], dtype=np.float64)


def spectral_profile(x, sr=SR):
    """Measure active-frame multi-band spectral share, centroid, RMS/Peak dBFS, and crest factor."""
    from scipy.signal import stft
    arr = np.asarray(x, dtype=np.float32)
    mono = arr if arr.ndim == 1 else arr.mean(axis=1)
    if len(mono) < NFFT:
        mono = np.pad(mono, (0, NFFT - len(mono)))
    peak = float(np.max(np.abs(mono)) + 1e-12)
    peak_db = round(20.0 * np.log10(peak), 2)

    # Measure RMS and spectrum on active frames (> -55 dBFS) so silence padding does not skew speech stats
    blk = max(1, sr // 50)  # 20 ms blocks
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

    # Chunked STFT (120s windows) keeps peak RAM < 50 MiB instead of 3.4 GiB complex128 on 20-min units
    mono32 = np.asarray(mono, dtype=np.float32)
    chunk_n = sr * 120
    f = None
    chunk_res = []
    global_max_e = 0.0
    for off in range(0, len(mono32), chunk_n):
        seg = mono32[off : off + chunk_n]
        if len(seg) < NFFT and off > 0:
            continue
        f_c, _, Z = stft(seg, sr, nperseg=NFFT, noverlap=NFFT - HOP)
        f = f_c
        Z2 = (Z.real * Z.real + Z.imag * Z.imag).astype(np.float64)
        fe = Z2.sum(axis=0)
        cmax = float(fe.max()) if fe.size else 0.0
        if cmax > global_max_e:
            global_max_e = cmax
        chunk_res.append((Z2.shape[1], fe, Z2.mean(axis=1)))
        del Z, Z2
    thresh = max(global_max_e * 1e-5, 1e-12)
    mag2_sum = np.zeros(len(f), dtype=np.float64)
    n_act = 0
    for off, (nf_c, fe, _) in zip(range(0, len(mono32), chunk_n), chunk_res):
        act_c = fe > thresh
        if not act_c.any():
            continue
        seg = mono32[off : off + chunk_n]
        _, _, Z = stft(seg, sr, nperseg=NFFT, noverlap=NFFT - HOP)
        Z2 = (Z.real * Z.real + Z.imag * Z.imag).astype(np.float64)
        mag2_sum += Z2[:, act_c].sum(axis=1)
        n_act += int(act_c.sum())
        del Z, Z2
    if n_act > 0:
        mag2 = mag2_sum / float(n_act)
    else:
        mag2 = np.mean([m for _, _, m in chunk_res], axis=0)
    tot = float(mag2.sum() + 1e-15)
    bands_pct = {}
    bands_db = {}
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


def resolve_spectral_plan(stems_profiles, c, user_keys=None):
    """Derive adaptive HP/LP cutoffs and subtle mud trim from spectral analysis while strictly respecting user overrides."""
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
        voc_high_dip_db = min(voc_high_dip_db, -4.5)

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
    """Run frequency analysis on stems + dub and save data/03_spectral_analysis.json."""
    stems_dir = os.path.join(ws, "media", "stems")
    raw_user = aicn.jload(os.path.join(ws, "config", "audio_mix.json"), {}) or {}
    profiles = {}
    if V is None and os.path.exists(os.path.join(stems_dir, "vocals.wav")):
        V = sf.read(os.path.join(stems_dir, "vocals.wav"), dtype="float32", always_2d=True)[0]
    if B is None and os.path.exists(os.path.join(stems_dir, "accomp.wav")):
        B = sf.read(os.path.join(stems_dir, "accomp.wav"), dtype="float32", always_2d=True)[0]
    dub_p = os.path.join(ws, "media", "audio", "dub_voice_48k.wav")
    if T is None and os.path.exists(dub_p):
        T = sf.read(dub_p, dtype="float32")[0]
    if V is not None and len(V):
        profiles["vocals"] = spectral_profile(V)
    if B is not None and len(B):
        profiles["accomp"] = spectral_profile(B)
    if T is not None and len(T):
        profiles["dub"] = spectral_profile(T)
    if not profiles and os.path.exists(source_path(ws) or ""):
        profiles["source"] = spectral_profile(load(source_path(ws)))
    plan = resolve_spectral_plan(profiles, c, user_keys=raw_user.keys())
    report = {
        "analyzed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "version": AUDIO_VERSION,
        "stems": profiles,
        "filter_plan": plan,
    }
    aicn.jdump(os.path.join(ws, "data", "03_spectral_analysis.json"), report)
    return report


def cmd_analyze(ws, c, fh):
    rep = analyze_stems_and_dub(ws, c)
    fp = rep["filter_plan"]
    print(f"[analyze] saved data/03_spectral_analysis.json | dub HP/LP={fp['dub_hp_hz']}/{fp['dub_lp_hz']} Hz, "
          f"vocal HP/LP={fp['vocal_hp_hz']}/{fp['vocal_lp_hz']} Hz (dyn_hi={fp['vocal_high_dip_db']} dB), "
          f"bgm HP/LP={fp['bgm_hp_hz']}/{fp['bgm_lp_hz']} Hz")


def voice_chain(x, hp_hz=90.0, lp_hz=16000.0, mud_cut_db=0.0):
    from scipy.signal import sosfilt
    x = apply_hp_lp(x, hp_hz=hp_hz, lp_hz=lp_hz, order=2).astype(np.float64)
    sos_list = []
    if mud_cut_db < -0.1:
        sos_list.append(_biquad_peaking(380.0, mud_cut_db, q=1.1, sr=SR))
    sos_list.append(_biquad_peaking(3100.0, 2.5, q=0.9, sr=SR))
    sos_list.append(_biquad_highshelf(8000.0, -2.0, slope=0.7, sr=SR))
    x = sosfilt(np.vstack(sos_list), x).astype(np.float32)
    # 100 Hz envelope (10 ms step matches 10 ms attack; 10x faster than 1 kHz Python loop)
    step = 480
    pad_n = (-len(x)) % step
    sq = np.pad(x * x, (0, pad_n)).reshape(-1, step).mean(axis=1)
    env = np.sqrt(follow(sq[None, :], SR / step, 0.01, 0.15)[0] + 1e-12).repeat(step)[: len(x)]
    lvl = 20.0 * np.log10(env + 1e-9)
    return (x * 10.0 ** (np.where(lvl > -20.0, (lvl + 20.0) * (1.0 / 3.0 - 1.0), 0.0) / 20.0)).astype(np.float32)


def _band_indices():
    f = np.fft.rfftfreq(NFFT, 1 / SR)
    idx = [np.flatnonzero((f >= a) & (f < b)) for a, b in zip(EDGES[:-1], EDGES[1:])]
    band_of_bin = np.zeros(len(f), dtype=np.int32)
    for k, ii in enumerate(idx):
        band_of_bin[ii] = k
    return idx, band_of_bin


def band_power_chunked(x, idx=None, chunk_frames=11250):
    """Low-RAM (<60 MiB) sample-exact band power over arbitrary-length audio via chunked STFT."""
    from scipy.signal import stft
    if idx is None:
        idx, _ = _band_indices()
    x = np.asarray(x, dtype=np.float32)
    x = x if x.ndim == 2 else x[:, None]
    n, nch = x.shape
    n_frames = (n + HOP - 1) // HOP + 1
    P = np.zeros((len(idx), n_frames), dtype=np.float64)
    half = NFFT // 2
    for ch in range(nch):
        xp = np.pad(x[:, ch], (half, NFFT + HOP))
        for f0 in range(0, n_frames, chunk_frames):
            f1 = min(n_frames, f0 + chunk_frames)
            seg = xp[f0 * HOP : (f1 - 1) * HOP + NFFT]
            _, _, Z = stft(seg, SR, nperseg=NFFT, noverlap=NFFT - HOP, boundary=None, padded=False)
            Z = Z.astype(np.complex64, copy=False)
            nf = min(Z.shape[1], f1 - f0)
            Z2 = (Z[:, :nf].real * Z[:, :nf].real + Z[:, :nf].imag * Z[:, :nf].imag)
            for b, ii in enumerate(idx):
                P[b, f0 : f0 + nf] += Z2[ii].sum(0)
            del Z, Z2
    return P / nch, idx


def apply_gain_chunked(x, g, band_of_bin, chunk_frames=11250):
    """Low-RAM (<80 MiB) overlap-trimmed chunked STFT/iSTFT gain application (exact to ~1e-6)."""
    import warnings
    from scipy.signal import stft, istft
    x = np.asarray(x, dtype=np.float32)
    is_1d = (x.ndim == 1)
    x2 = x[:, None] if is_1d else x
    n, nch = x2.shape
    n_frames = min(g.shape[1], (n + HOP - 1) // HOP + 1)
    half = NFFT // 2
    pad_f = 4
    out = np.empty((n, nch), dtype=np.float32)
    for ch in range(nch):
        xp = np.pad(x2[:, ch], (half, NFFT + HOP))
        for f0 in range(0, n_frames, chunk_frames):
            f1 = min(n_frames, f0 + chunk_frames)
            flo = max(0, f0 - pad_f)
            fhi = min(n_frames, f1 + pad_f)
            seg = xp[flo * HOP : (fhi - 1) * HOP + NFFT]
            _, _, Z = stft(seg, SR, nperseg=NFFT, noverlap=NFFT - HOP, boundary=None, padded=False)
            nf = min(Z.shape[1], fhi - flo)
            G = g[band_of_bin, flo : flo + nf].astype(np.float32)
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message="NOLA condition failed")
                _, y = istft(Z[:, :nf].astype(np.complex64, copy=False) * G, SR, nperseg=NFFT, noverlap=NFFT - HOP, boundary=None)
            # Sample i in y aligns with xp[flo * HOP + i] == x[flo * HOP + i - half]
            dst0 = max(0, f0 * HOP - half) if f0 > 0 else 0
            dst1 = min(n, f1 * HOP - half) if f1 < n_frames else n
            src0 = dst0 + half - flo * HOP
            src1 = dst1 + half - flo * HOP
            out[dst0:dst1, ch] = y[src0:src1]
            del Z, G, y
    return out[:, 0] if is_1d else out


def stft_and_power(x, idx=None):
    """Compute complex64 STFT once and return ((bands, frames) power, list of Z per channel, idx)."""
    from scipy.signal import stft
    if idx is None:
        idx, _ = _band_indices()
    x = x if x.ndim == 2 else x[:, None]
    Zs, P = [], None
    for ch in range(x.shape[1]):
        _, _, Z = stft(x[:, ch], SR, nperseg=NFFT, noverlap=NFFT - HOP)
        Z = Z.astype(np.complex64, copy=False)
        Zs.append(Z)
        Z2 = Z.real * Z.real + Z.imag * Z.imag
        pc = np.stack([Z2[i].sum(0) for i in idx])
        P = pc if P is None else (P + pc)
    return P / x.shape[1], Zs, idx


def band_power(x):
    """x: (n,) or (n, ch). Returns (bands, frames) power, summed over channels."""
    P, _, idx = stft_and_power(x)
    return P, idx


def istft_from_Z(Zs, g, band_of_bin, length):
    """Apply per-band gains directly to cached complex64 STFTs and run iSTFT (avoids duplicate STFT)."""
    from scipy.signal import istft
    out = np.empty((length, len(Zs)), dtype=np.float32)
    nf = Zs[0].shape[1]
    G = g[band_of_bin, :nf].astype(np.float32)
    for ch, Z in enumerate(Zs):
        _, y = istft(Z * G, SR, nperseg=NFFT, noverlap=NFFT - HOP)
        out[:, ch] = y[:length]
    return out


def apply_gain(x, g, idx):
    _, Zs, _ = stft_and_power(x, idx)
    _, band_of_bin = _band_indices()
    return istft_from_Z(Zs, g, band_of_bin, len(x))


def cmd_mix(ws, c, fh):
    sd = os.path.join(ws, "media", "stems")
    voc_p, acc_p = os.path.join(sd, "vocals.wav"), os.path.join(sd, "accomp.wav")
    for f, p in (("vocals.wav", voc_p), ("accomp.wav", acc_p)):
        if not os.path.exists(p):
            sys.exit(f"[aicn-audio] missing media/stems/{f}: run `separate` (the original audio is never replaced by silence)")
    dub_p = os.path.join(ws, "media", "audio", "dub_voice_48k.wav")
    pre_p = os.path.join(ws, "media", "audio", "premaster_48k.wav")
    stats_p = os.path.join(ws, "data", "03_mix_stats.json")
    spec_p = os.path.join(ws, "data", "03_spectral_analysis.json")
    dur = float(profile(ws)["duration"])
    mix_key = None
    if fh is not None and os.path.exists(dub_p):
        mix_key = aicn.h_obj({
            "v": AUDIO_VERSION, "voc": fh(voc_p), "acc": fh(acc_p), "dub": fh(dub_p), "dur": round(dur, 3),
            "cfg": {k: c.get(k) for k in ("dub_over_vocal_lu", "snr_vocal_db", "snr_bgm_db", "floor_vocal_db",
                                          "floor_bgm_db", "base_vocal_db", "vocal_high_dip_db", "attack_s", "hold_s",
                                          "release_s", "dub_hp_hz", "dub_lp_hz", "vocal_hp_hz", "vocal_lp_hz",
                                          "bgm_hp_hz", "bgm_lp_hz")},
        })
        st_cache = _audio_state_load(ws)
        if not c.get("_force") and st_cache.get("mix_key") == mix_key and all(os.path.exists(p) for p in (pre_p, stats_p, spec_p)):
            print(f"[mix] cached (key={mix_key[:12]})")
            return
    n = int(round(dur * SR))
    fit = lambda a: np.pad(a, ((0, max(0, n - len(a))), (0, 0)))[:n]
    V, B = fit(sf.read(voc_p, dtype="float32", always_2d=True)[0]), \
        fit(sf.read(acc_p, dtype="float32", always_2d=True)[0])
    T = sf.read(dub_p, dtype="float32")[0]
    T = np.pad(T, (0, max(0, n - len(T))))[:n]
    t0 = time.time()
    spec_rep = analyze_stems_and_dub(ws, c, V=V, B=B, T=T)
    fp = spec_rep["filter_plan"]
    V = apply_hp_lp(V, hp_hz=fp["vocal_hp_hz"], lp_hz=fp["vocal_lp_hz"], order=2)
    B = apply_hp_lp(B, hp_hz=fp["bgm_hp_hz"], lp_hz=fp["bgm_lp_hz"], order=2)
    T = voice_chain(T, hp_hz=fp["dub_hp_hz"], lp_hz=fp["dub_lp_hz"], mud_cut_db=fp["dub_mud_cut_db"])
    lv = lufs(V); gain = lv + c["dub_over_vocal_lu"] - lufs(T[:, None]); T = T * np.float32(10 ** (gain / 20))
    idx, band_of_bin = _band_indices()
    PT, _ = band_power_chunked(T, idx)
    PV, _ = band_power_chunked(V, idx)
    PB, _ = band_power_chunked(B, idx)
    fps = SR / HOP; W = BAND_W[:, None]
    hold_s = float(c.get("hold_s", 0.35))
    raw_active = PT > PT.max(1, keepdims=True) * 10 ** (-45 / 10)
    tot = PT.sum(0)
    talk_mask = hangover_hold(tot > tot.max() * 1e-4, fps, hold_s)
    talk = follow(talk_mask.astype(float)[None], fps, c["attack_s"], max(c["release_s"], 0.40))[0]

    def need(Pc, snr, floor):
        g = np.sqrt(np.clip(PT / (Pc * 10 ** (snr / 10) + 1e-12), 10 ** (floor / 10), 1.0))
        return np.where(raw_active, g, 1.0)
    base_dip = 1.0 - talk * (1.0 - 10.0 ** (c["base_vocal_db"] / 20.0))
    hi_dip_db = float(fp.get("vocal_high_dip_db", c.get("vocal_high_dip_db", -3.5)))
    hi_band = np.ones((len(BAND_W), len(talk)), dtype=np.float64)
    if hi_dip_db < -0.1:
        hi_band[9:, :] = 1.0 - talk[None, :] * (1.0 - 10.0 ** (hi_dip_db / 20.0))
    voc_floor_lin = 10.0 ** ((c["floor_vocal_db"] + c["base_vocal_db"]) / 20.0)
    gV_raw = np.maximum(voc_floor_lin, need(PV, c["snr_vocal_db"], c["floor_vocal_db"]) * base_dip * hi_band)
    gB_raw = need(PB, c["snr_bgm_db"], c["floor_bgm_db"])
    gV = follow_hold(1 - W * (1 - gV_raw), fps, c["attack_s"], hold_s, c["release_s"])
    gB = follow_hold(1 - W * (1 - gB_raw), fps, c["attack_s"], hold_s, c["release_s"])
    mix = apply_gain_chunked(V, gV, band_of_bin)
    del V
    mix += apply_gain_chunked(B, gB, band_of_bin)
    del B
    mix += T[:, None]
    os.makedirs(os.path.join(ws, "media", "audio"), exist_ok=True)
    _write_wav16(os.path.join(ws, "media", "audio", "premaster_48k.wav"), mix.astype(np.float32), SR)
    on = talk > 0.5
    comp = (PV[SPEECH] * gV[SPEECH] ** 2 + PB[SPEECH] * gB[SPEECH] ** 2).sum(0)
    snr = 10 * np.log10(PT[SPEECH].sum(0)[on] / (comp[on] + 1e-12) + 1e-12) if on.any() else np.array([99.0])
    stats = {"dub_gain_db": round(gain, 2), "orig_vocal_lufs": round(lv, 2), "dub_active_share": round(float(on.mean()), 3),
             "dub_snr_speech_db_median": round(float(np.median(snr)), 2),
             "dub_snr_speech_db_p10": round(float(np.percentile(snr, 10)), 2),
             "orig_vocal_atten_db_median_during_dub": round(float(np.median(20 * np.log10(gV[SPEECH][:, on].mean(0)))), 2) if on.any() else 0,
             "orig_vocal_atten_db_min": round(float(20 * np.log10(gV.min())), 2),
             "bgm_atten_db_median_during_dub": round(float(np.median(20 * np.log10(gB[SPEECH][:, on].mean(0)))), 2) if on.any() else 0,
             "bgm_atten_db_min": round(float(20 * np.log10(gB.min())), 2),
             "filter_plan": fp, "seconds": round(time.time() - t0, 1)}
    aicn.jdump(stats_p, stats)
    if mix_key is not None:
        st_cache = _audio_state_load(ws)
        st_cache["mix_key"] = mix_key
        _audio_state_save(ws, st_cache)
    print("[mix] " + json.dumps(stats))


# ---------------------------------------------------------------- master
def write_sub_only_alignment(ws):
    """Build 03_aligned_subtitles.json and subtitles_full.srt directly from Stage 2 in sub_only mode."""
    segs = aicn.jload(os.path.join(ws, "data", "02_bilingual_script.json"), [])
    dur = float(profile(ws).get("duration", 0.0))
    aligned = []
    for i, s in enumerate(segs):
        s0, e0 = float(s["start"]), float(s["end"])
        nxt = float(segs[i + 1]["start"]) if i + 1 < len(segs) else dur
        slot_end = max(s0 + 0.15, min(e0, nxt - 0.005)) if i + 1 < len(segs) else e0
        aligned.append({
            "id": s.get("id", i), "slot_start": round(s0, 3), "slot_end": round(slot_end, 3),
            "speech_start": round(s0, 3), "speech_end": round(slot_end, 3), "audio_end": round(slot_end, 3),
            "pause_after": round(max(0.0, nxt - slot_end), 3), "tempo": 1.0,
            "zh_text": s.get("zh_text", ""), "sub_text": s["sub_text"], "tts_text": s.get("tts_text", s["sub_text"]),
        })
    aicn.jdump(os.path.join(ws, "data", "03_aligned_subtitles.json"), aligned)
    with open(os.path.join(ws, "data", "subtitles_full.srt"), "w", encoding="utf-8") as f:
        for k, it in enumerate(aligned, 1):
            f.write(f"{k}\n{srt_t(it['slot_start'])} --> {srt_t(it['slot_end'])}\n{it['sub_text']}\n\n")
    return len(aligned)


def write_stage3_report(ws, mode, checks, c):
    """Write 03_pacing_and_audio_report.md so verify_stage_3.py always hashes an up-to-date report."""
    spec = aicn.jload(os.path.join(ws, "data", "03_spectral_analysis.json"), {}) or {}
    fp = spec.get("filter_plan", {})
    stems = spec.get("stems", {})
    lines = [
        f"# Stage 3 Pacing & Audio Report ({mode})",
        "",
        f"- Engine: `{AUDIO_VERSION}`",
        f"- Target loudness: `{c['target_lufs']} LUFS`, peak `<= {c['true_peak_db']} dBFS`",
    ]
    if fp:
        lines += [
            "",
            "## Frequency Analysis & Pre-Mix HP/LP Conditioning (`data/03_spectral_analysis.json`)",
            f"- Dub HP/LP: `{fp.get('dub_hp_hz')} Hz` / `{fp.get('dub_lp_hz')} Hz` (mud trim `{fp.get('dub_mud_cut_db', 0.0)} dB`)",
            f"- Original Vocal HP/LP: `{fp.get('vocal_hp_hz')} Hz` / `{fp.get('vocal_lp_hz')} Hz`, base dip `{c.get('base_vocal_db')} dB`",
            f"- SFX/Music HP/LP: `{fp.get('bgm_hp_hz')} Hz` / `{fp.get('bgm_lp_hz')} Hz`, envelope hold `{c.get('hold_s', 0.35)}s`, release `{c.get('release_s')}s`",
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
    lines += [
        "",
        "## Audio Gate Checks",
        "",
    ]
    for x in checks:
        lines.append(f"- [{'PASS' if x['passed'] else 'FAIL'}] **{x['id']}**: {x['detail']}")
    with open(os.path.join(ws, "03_pacing_and_audio_report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def cmd_master(ws, c, fh):
    ff = ap.find_tool("ffmpeg")
    mode = profile(ws).get("localization_mode", "voice_dub")
    src = os.path.join(ws, "media", "audio", "premaster_48k.wav")
    out = os.path.join(ws, "media", "master_audio_48k.wav")
    dur = float(profile(ws)["duration"])
    script_p = os.path.join(ws, "data", "02_bilingual_script.json")
    master_key = None
    if fh is not None:
        in_ref = source_path(ws) if mode == "sub_only" else src
        if in_ref and os.path.exists(in_ref):
            master_key = aicn.h_obj({
                "v": AUDIO_VERSION, "mode": mode, "src": fh(in_ref),
                "script": fh(script_p) if (mode == "sub_only" and os.path.exists(script_p)) else None,
                "dur": round(dur, 3),
                "cfg": {k: c.get(k) for k in ("target_lufs", "true_peak_db", "lra", "master_mode")},
            })
            st_cache = _audio_state_load(ws)
            sub_ok = (mode != "sub_only") or (
                os.path.exists(os.path.join(ws, "data", "03_aligned_subtitles.json"))
                and os.path.exists(os.path.join(ws, "data", "subtitles_full.srt"))
            )
            if not c.get("_force") and st_cache.get("master_key") == master_key and os.path.exists(out) and sub_ok:
                print(f"[master] cached (key={master_key[:12]})")
                return
    if mode == "sub_only":
        n_cues = write_sub_only_alignment(ws)
        print(f"[master] sub_only: aligned {n_cues} cues -> 03_aligned_subtitles.json & subtitles_full.srt")
        os.makedirs(os.path.dirname(src), exist_ok=True)
        run([ff, "-v", "error", "-y", "-i", source_path(ws), "-vn", "-ac", "2", "-ar", str(SR), "-c:a", "pcm_f32le", src])
    if not os.path.exists(src):
        sys.exit("[aicn-audio] no premaster: run `mix`")
    pre = "acompressor=threshold=-18dB:ratio=2:attack=20:release=250:makeup=1"
    t0 = time.time()
    if c.get("master_mode", "fast") == "loudnorm":
        ln = f"loudnorm=I={c['target_lufs']}:TP={c['true_peak_db']}:LRA={c['lra']}"
        o = subprocess.run([ff, "-hide_banner", "-nostats", "-i", src, "-af", f"{pre},{ln}:print_format=json", "-f", "null", "-"],
                           capture_output=True, text=True).stderr
        m = json.loads(o[o.rindex("{"): o.rindex("}") + 1])
        ln2 = (f"{ln}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}"
               f":measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
        run([ff, "-v", "error", "-y", "-i", src, "-af", f"{pre},{ln2},aresample={SR},apad", "-t", f"{dur:.3f}",
             "-ar", str(SR), "-ac", "2", "-c:a", "pcm_s24le", out])
        i, pk = lufs_file(out)
    else:
        # Native 48 kHz linear EBU R128 master (~28x faster than 192 kHz loudnorm; identical linear gain + lookahead limiter)
        o = subprocess.run([ff, "-hide_banner", "-nostats", "-i", src, "-af", f"{pre},ebur128=peak=true", "-f", "null", "-"],
                           capture_output=True, text=True).stderr
        hits = re.findall(r"I:\s+(-?[\d.]+) LUFS", o)
        meas_i = float(hits[-1]) if hits else float(c["target_lufs"])
        gain_db = float(c["target_lufs"]) - meas_i
        # -0.55 dB sample-peak margin guarantees 4x oversampled true peak stays comfortably below true_peak_db + 0.3
        lim = 10.0 ** ((float(c["true_peak_db"]) - 0.55) / 20.0)
        af2 = f"{pre},volume={gain_db:.3f}dB,alimiter=limit={lim:.5f}:level=false:attack=5:release=50:asc=false,apad"
        run([ff, "-v", "error", "-y", "-i", src, "-af", af2, "-t", f"{dur:.3f}",
             "-ar", str(SR), "-ac", "2", "-c:a", "pcm_s24le", out])
        i, pk = lufs_file(out)
        if pk > float(c["true_peak_db"]) + 0.2:
            # Rare extreme inter-sample transient: 4x oversampled true-peak limit (~2s)
            lim_tp = 10.0 ** ((float(c["true_peak_db"]) - 0.1) / 20.0)
            af_tp = f"{pre},volume={gain_db:.3f}dB,aresample=192000,alimiter=limit={lim_tp:.5f}:level=false:attack=5:release=50:asc=false,aresample={SR},apad"
            run([ff, "-v", "error", "-y", "-i", src, "-af", af_tp, "-t", f"{dur:.3f}",
                 "-ar", str(SR), "-ac", "2", "-c:a", "pcm_s24le", out])
            i, pk = lufs_file(out)
    if master_key is not None:
        st_cache = _audio_state_load(ws)
        st_cache["master_key"] = master_key
        _audio_state_save(ws, st_cache)
    print(f"[master] {out}: {i:.1f} LUFS, peak {pk:.1f} dBFS ({time.time() - t0:.1f}s)")


# ---------------------------------------------------------------- verify
def cmd_verify(ws, c, fh):
    mode = profile(ws).get("localization_mode", "voice_dub")
    out = os.path.join(ws, "media", "master_audio_48k.wav")
    checks = []
    def chk(name, ok, detail):
        checks.append({"id": name, "passed": bool(ok), "detail": detail})
    i, pk = lufs_file(out) if os.path.exists(out) else (-99, 0)
    chk("loudness", abs(i - c["target_lufs"]) <= GATE["lufs_tol"] and pk <= c["true_peak_db"] + 0.3,
        f"{i:.1f} LUFS (target {c['target_lufs']}), peak {pk:.1f} dBFS")
    if mode != "sub_only":
        meta = aicn.jload(os.path.join(ws, "media", "stems", "stems.json"), {})
        cur = aicn.h_obj({"src": fh(source_path(ws)), "model": c["demucs_model"], "v": STEMS_VERSION})
        chk("stems_match_source", meta.get("key") == cur, "media/stems built from the current source")
        st = aicn.jload(os.path.join(ws, "data", "03_mix_stats.json"), {})
        chk("dub_intelligible", st.get("dub_snr_speech_db_median", -99) >= GATE["dub_snr_min_db"],
            f"dub over original in 500 Hz-5 kHz: median {st.get('dub_snr_speech_db_median')} dB (min {GATE['dub_snr_min_db']})")
        chk("original_never_muted",
            st.get("orig_vocal_atten_db_min", -99) >= c["floor_vocal_db"] + c["base_vocal_db"] - GATE["vocal_floor_margin_db"]
            and st.get("bgm_atten_db_min", -99) >= c["floor_bgm_db"] - GATE["vocal_floor_margin_db"],
            f"deepest cut: vocal {st.get('orig_vocal_atten_db_min')} dB, music/sfx {st.get('bgm_atten_db_min')} dB")
        # original audio really present in the master where nobody dubs (catches a silent-BGM fallback)
        acc = sf.read(os.path.join(ws, "media", "stems", "accomp.wav"), dtype="float32", always_2d=True)[0]
        voc = sf.read(os.path.join(ws, "media", "stems", "vocals.wav"), dtype="float32", always_2d=True)[0]
        dub = sf.read(os.path.join(ws, "media", "audio", "dub_voice_48k.wav"), dtype="float32")[0]
        mst = sf.read(out, dtype="float32", always_2d=True)[0]
        n = min(len(acc), len(voc), len(dub), len(mst)); k = SR // 10
        e = lambda x: (x[: n // k * k] ** 2).reshape(-1, k, x.shape[1] if x.ndim > 1 else 1).mean((1, 2))
        quiet = e(dub[:n, None]) < 1e-7; orig_e = e(acc[:n] + voc[:n]); m_e = e(mst[:n])
        sel = quiet & (orig_e > np.percentile(orig_e, 30))
        ratio = 10 * np.log10(m_e[sel].mean() / (orig_e[sel].mean() + 1e-12) + 1e-12) if sel.any() else -99
        chk("original_present", ratio >= GATE["orig_presence_min_db"],
            f"master vs original where no dub: {ratio:.1f} dB (min {GATE['orig_presence_min_db']})")
        log = aicn.jload(os.path.join(ws, "data", "03_dub_sentences.json"), [])
        over = [l for l in log if l["overflow_s"] > 0.05]
        chk("dub_fits", not over and all(l["tempo"] <= c["max_tempo"] + 1e-6 for l in log),
            f"{len(log)} sentences, max tempo {max([l['tempo'] for l in log], default=0):.2f}x, overflow {[l['ids'] for l in over][:5]}")
        script_segs = aicn.jload(os.path.join(ws, "data", "02_bilingual_script.json"), [])
        aligned_segs = aicn.jload(os.path.join(ws, "data", "03_aligned_subtitles.json"), [])
        sync_bad = []
        min_d, max_d = 0.0, 0.0
        for idx, (s0_it, al_it) in enumerate(zip(script_segs, aligned_segs)):
            d = float(al_it.get("speech_start", al_it["slot_start"])) - float(s0_it["start"])
            min_d, max_d = min(min_d, d), max(max_d, d)
            if d < -0.15 or d > 0.75:
                sync_bad.append((idx, round(d, 3)))
        chk("dub_sync", len(aligned_segs) == len(script_segs) and not sync_bad,
            f"speech_start vs script start in [{min_d:+.3f}s, {max_d:+.3f}s] (allowed [-0.15, +0.75]), bad={sync_bad[:5]}")
    passed = all(x["passed"] for x in checks)
    pub_params = {k: v for k, v in c.items() if not k.startswith("_")}
    aicn.jdump(os.path.join(ws, "data", "03_mix_report.json"), {"passed": passed, "version": AUDIO_VERSION, "checks": checks,
                                                                 "params": pub_params})
    write_stage3_report(ws, mode, checks, pub_params)
    for x in checks:
        print(f"  [{'PASS' if x['passed'] else 'FAIL'}] {x['id']}: {x['detail']}")
    if not passed:
        sys.exit("[aicn-audio] audio gate FAILED: Stage 3 not signed")
    sys.path.insert(0, os.path.join(aicn.VENDOR, "vieneu-voice-pacing-mixer", "scripts"))
    sys.path.insert(0, os.path.join(aicn.VENDOR, "video-localization-orchestrator", "scripts"))
    from verify_stage_3 import run_stage_3_verification
    run_stage_3_verification(ws)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["separate", "dub", "analyze", "mix", "master", "verify", "all"])
    ap.add_argument("--unit", required=True)
    ap.add_argument("--force", action="store_true", help="Ignore step/clause content-hash caches and recompute")
    a = ap.parse_args()
    ws = os.path.abspath(a.unit); c = cfg(ws); fh = aicn.FastHash(ws)
    if a.force:
        c["_force"] = True
    mode = profile(ws).get("localization_mode", "voice_dub")
    steps = {"separate": [cmd_separate], "dub": [cmd_dub], "analyze": [cmd_analyze], "mix": [cmd_mix], "master": [cmd_master], "verify": [cmd_verify],
             "all": ([cmd_master, cmd_verify] if mode == "sub_only" else [cmd_separate, cmd_dub, cmd_mix, cmd_master, cmd_verify])}
    for f in steps[a.cmd]:
        f(ws, c, fh)
    fh.save()


if __name__ == "__main__":
    main()
