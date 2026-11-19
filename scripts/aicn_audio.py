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
import argparse, json, os, re, subprocess, sys, tempfile, time
import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import aicn  # noqa: E402  (jload, jdump, h_obj, FastHash, cicd)

SR = 48000
AUDIO_VERSION = "aicn-audio-1.0"
DEFAULTS = {
    "voice": "Trúc Ly",
    "tts_workers": 1,                           # VieNeu ONNX already uses every core: 3 workers measured 9% faster, 4 slower
    "temperature": 0.8, "top_k": 25,            # VieNeu v3 Turbo defaults (same as its app)
    "group_max_gap_s": 0.6,                     # clauses of one speaker closer than this are read as one sentence
    "base_tempo": 1.10,                         # model reads ~5.2 syl/s; 1.10 brings it to ~5.7
    "max_tempo": 1.25,                          # rubberband R3, formant preserved
    "lead_gap_s": 0.10,                         # keep this much silence before the next sentence
    "expander_thresh_db": -50.0, "expander_depth_db": -15.0,   # removes the TTS hiss floor between words
    "dub_over_vocal_lu": 1.0,                   # dub loudness relative to the original vocal stem
    "snr_vocal_db": 10.0, "snr_bgm_db": 6.0,    # wanted dub-to-original ratio per band while the dub talks
    "floor_vocal_db": -18.0, "floor_bgm_db": -9.0,   # the original is never turned down further than this
    "base_vocal_db": -4.0,                      # broadband dip of the original voice while the dub talks
    "attack_s": 0.02, "release_s": 0.25,
    "target_lufs": -14.0, "true_peak_db": -1.0, "lra": 11.0,
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
    raw = run(["ffmpeg", "-v", "error", "-i", p, "-f", "f32le", "-ar", str(sr), "-ac", str(ch), "pipe:1"]).stdout
    return np.frombuffer(raw, np.float32).reshape(-1, ch).copy()


def lufs_file(p):
    o = subprocess.run(["ffmpeg", "-nostats", "-i", p, "-af", "ebur128=peak=true", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
    i = float(re.findall(r"I:\s+(-?[\d.]+) LUFS", o)[-1])
    pk = re.findall(r"Peak:\s+(-?[\d.inf]+) dBFS", o)
    return i, (float(pk[-1]) if pk and pk[-1] not in ("-inf", "inf") else -99.0)


def lufs(x):
    fd, p = tempfile.mkstemp(suffix=".wav"); os.close(fd)
    sf.write(p, x, SR)
    try:
        return lufs_file(p)[0]
    finally:
        os.remove(p)


def follow(g, fps, att, rel):
    """Envelope follower over the last axis: fast down (attack), slow up (release)."""
    a_att, a_rel = np.exp(-1 / (att * fps)), np.exp(-1 / (rel * fps))
    g = np.atleast_2d(g); out = np.empty_like(g); cur = np.ones(g.shape[0])
    for t in range(g.shape[1]):
        tgt = g[:, t]; a = np.where(tgt < cur, a_att, a_rel)
        cur = a * cur + (1 - a) * tgt; out[:, t] = cur
    return out


# ---------------------------------------------------------------- separate
def cmd_separate(ws, c, fh):
    src = source_path(ws)
    if not src or not os.path.exists(src):
        sys.exit("[aicn-audio] no source video (source/master_source.mp4)")
    key = aicn.h_obj({"src": fh(src), "model": c["demucs_model"], "v": AUDIO_VERSION})
    d = os.path.join(ws, "media", "stems"); meta_p = os.path.join(d, "stems.json")
    meta = aicn.jload(meta_p, {})
    if meta.get("key") == key and all(os.path.exists(os.path.join(d, f)) for f in ("vocals.wav", "accomp.wav")):
        print("[separate] cached"); return
    os.makedirs(d, exist_ok=True); t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        wav = os.path.join(tmp, "source.wav")
        run(["ffmpeg", "-v", "error", "-y", "-i", src, "-vn", "-ac", "2", "-ar", "44100", wav])
        run(["uvx", "--from", "demucs-mlx", "demucs-mlx", "-n", c["demucs_model"], "-o", tmp, wav])
        sd = os.path.join(tmp, c["demucs_model"], "source")
        if not os.path.isdir(sd):
            sd = os.path.join(tmp, "source")
        voc = load(os.path.join(sd, "vocals.wav"))
        acc = sum(load(os.path.join(sd, f"{s}.wav")) for s in ("drums", "bass", "other"))
    sf.write(os.path.join(d, "vocals.wav"), voc, SR, subtype="FLOAT")
    sf.write(os.path.join(d, "accomp.wav"), acc, SR, subtype="FLOAT")
    aicn.jdump(meta_p, {"key": key, "source": src, "model": c["demucs_model"], "seconds": round(time.time() - t0, 1)})
    print(f"[separate] {c['demucs_model']} -> media/stems in {time.time() - t0:.1f}s")


# ---------------------------------------------------------------- dub
def group_lines(segs, max_gap):
    """Comma-chained clauses of one speaker become one sentence (TTS reads short fragments badly)."""
    groups, cur = [], []
    for i, s in enumerate(segs):
        if cur:
            p = segs[cur[-1]]
            chained = not re.search(r"[.!?…]\s*$", p["tts_text"].strip())
            if chained and s.get("speaker") == p.get("speaker") and float(s["start"]) - float(p["end"]) < max_gap:
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
        run(["rubberband", "-3", "-F", "-q", "--tempo", f"{tempo:.4f}", a, b])
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


def _tts_init():
    global _TTS
    from vieneu import Vieneu
    _TTS = Vieneu()


def _tts_job(job):
    text, voice, temp, top_k, path = job
    w = _TTS.infer(text=text, voice=voice, temperature=temp, top_k=top_k)
    sf.write(path + ".part.wav", w, SR, subtype="FLOAT")
    os.replace(path + ".part.wav", path)
    return path


def presynth(jobs, workers):
    """Synthesize missing sentences. VieNeu ONNX runs on CPU and one process does not fill the cores,
    so several processes (each with its own model) run side by side."""
    if not jobs:
        return
    if workers <= 1 or len(jobs) == 1:
        _tts_init()
        for j in jobs:
            _tts_job(j)
        return
    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor
    with ProcessPoolExecutor(max_workers=min(workers, len(jobs)), mp_context=mp.get_context("spawn"),
                             initializer=_tts_init) as ex:
        list(ex.map(_tts_job, jobs))


def cmd_dub(ws, c, fh):
    segs = aicn.jload(os.path.join(ws, "data", "02_bilingual_script.json"))
    dur = float(profile(ws)["duration"]); n = int(round(dur * SR))
    groups = group_lines(segs, c["group_max_gap_s"])
    cache = os.path.join(ws, ".cache", "tts_sent"); os.makedirs(cache, exist_ok=True)
    tts = None; track = np.zeros(n, np.float32); aligned = [None] * len(segs); log = []; synth = 0; t0 = time.time()
    wav_dir = os.path.join(ws, "media", "tts_segments"); os.makedirs(wav_dir, exist_ok=True)
    jobs, seen = [], set()
    for g in groups:
        text = " ".join(segs[i]["tts_text"].strip() for i in g)
        voice = segs[g[0]].get("voice") or c["voice"]
        key = aicn.h_obj({"t": text, "v": voice, "e": aicn.TTS_ENGINE_TAG, "temp": c["temperature"], "k": c["top_k"]})
        p_ = os.path.join(cache, key + ".wav")
        if not os.path.exists(p_) and p_ not in seen:
            seen.add(p_); jobs.append((text, voice, c["temperature"], c["top_k"], p_))
    presynth(jobs, int(c.get("tts_workers", 1))); synth = len(jobs)
    for gi, g in enumerate(groups):
        text = " ".join(segs[i]["tts_text"].strip() for i in g)
        voice = segs[g[0]].get("voice") or c["voice"]
        key = aicn.h_obj({"t": text, "v": voice, "e": aicn.TTS_ENGINE_TAG, "temp": c["temperature"], "k": c["top_k"]})
        raw_p = os.path.join(cache, key + ".wav")
        w = clean(sf.read(raw_p, dtype="float32")[0], c)
        st = float(segs[g[0]]["start"])
        nxt = float(segs[groups[gi + 1][0]]["start"]) if gi + 1 < len(groups) else dur - 0.05
        window = max(0.25, nxt - c["lead_gap_s"] - st)
        natural = len(w) / SR
        tempo = max(c["base_tempo"], min(c["max_tempo"], natural / window))
        w = stretch(w, tempo)
        overflow = len(w) / SR - window
        a = int(round(st * SR)); b = min(n, a + len(w)); track[a:b] += w[: b - a]
        cuts = split_points(w, [max(1, len(segs[i]["tts_text"].split())) for i in g])
        for j, i in enumerate(g):
            piece = w[cuts[j]:cuts[j + 1]]
            sf.write(os.path.join(wav_dir, f"seg_{i:04d}.wav"), piece if len(piece) > 480 else np.zeros(4800, np.float32), SR)
            sp_s, sp_e = st + cuts[j] / SR, st + cuts[j + 1] / SR
            s0, e0 = float(segs[i]["start"]), float(segs[i]["end"])
            nxt_line = float(segs[i + 1]["start"]) if i + 1 < len(segs) else dur
            slot_end = max(s0 + 0.15, min(max(e0, sp_e), nxt_line - 0.04))
            aligned[i] = {"id": i, "slot_start": round(s0, 3), "slot_end": round(slot_end, 3),
                          "speech_start": round(sp_s, 3), "speech_end": round(sp_e, 3), "audio_end": round(sp_e, 3),
                          "pause_after": round(max(0.0, nxt_line - sp_e), 3), "tempo": round(tempo, 3),
                          "group": gi, "zh_text": segs[i].get("zh_text", ""), "sub_text": segs[i]["sub_text"],
                          "tts_text": segs[i]["tts_text"]}
        syl = len(text.split())
        log.append({"group": gi, "ids": g, "start": round(st, 3), "natural_s": round(natural, 2), "window_s": round(window, 2),
                    "tempo": round(tempo, 3), "overflow_s": round(overflow, 2), "syl_per_s": round(syl / (len(w) / SR), 2),
                    "text": text})
    os.makedirs(os.path.join(ws, "media", "audio"), exist_ok=True)
    sf.write(os.path.join(ws, "media", "audio", "dub_voice_48k.wav"), track, SR, subtype="FLOAT")
    aicn.jdump(os.path.join(ws, "data", "03_aligned_subtitles.json"), aligned)
    aicn.jdump(os.path.join(ws, "data", "03_dub_sentences.json"), log)
    with open(os.path.join(ws, "data", "subtitles_full.srt"), "w", encoding="utf-8") as f:
        for k, it in enumerate(aligned, 1):
            f.write(f"{k}\n{srt_t(it['slot_start'])} --> {srt_t(it['slot_end'])}\n{it['sub_text']}\n\n")
    over = [l for l in log if l["overflow_s"] > 0.05]
    rates = [l["syl_per_s"] for l in log]
    print(f"[dub] {len(segs)} lines -> {len(groups)} sentences, synthesized {synth} (rest cached) in {time.time() - t0:.1f}s; "
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


def voice_chain(x):
    from scipy.signal import butter, sosfilt
    x = sosfilt(butter(2, 90, "hp", fs=SR, output="sos"), x)
    pres = sosfilt(butter(2, [2200, 4500], "bp", fs=SR, output="sos"), x)
    hi = sosfilt(butter(2, 8000, "hp", fs=SR, output="sos"), x)
    x = x + 0.33 * pres - 0.2 * hi
    env = np.sqrt(follow((x ** 2)[None, :][:, ::48], SR / 48, 0.01, 0.15)[0] + 1e-12).repeat(48)[: len(x)]
    lvl = 20 * np.log10(env + 1e-9)
    return (x * 10 ** (np.where(lvl > -20, (lvl + 20) * (1 / 3 - 1), 0) / 20)).astype(np.float32)


def band_power(x):
    """x: (n,) or (n, ch). Returns (bands, frames) power, summed over channels."""
    from scipy.signal import stft
    x = x if x.ndim == 2 else x[:, None]
    f = np.fft.rfftfreq(NFFT, 1 / SR); idx = [np.flatnonzero((f >= a) & (f < b)) for a, b in zip(EDGES[:-1], EDGES[1:])]
    P = 0
    for ch in range(x.shape[1]):
        _, _, Z = stft(x[:, ch], SR, nperseg=NFFT, noverlap=NFFT - HOP)
        Z = np.abs(Z.astype(np.complex64)) ** 2
        P = P + np.stack([Z[i].sum(0) for i in idx])
    return P / x.shape[1], idx


def apply_gain(x, g, idx):
    from scipy.signal import stft, istft
    out = np.empty_like(x)
    for ch in range(x.shape[1]):
        _, _, Z = stft(x[:, ch], SR, nperseg=NFFT, noverlap=NFFT - HOP)
        G = np.ones(Z.shape, np.float32)
        for k, i in enumerate(idx):
            G[i] = g[k][: Z.shape[1]]
        _, y = istft(Z * G, SR, nperseg=NFFT, noverlap=NFFT - HOP)
        out[:, ch] = y[: len(x)]
    return out


def cmd_mix(ws, c, fh):
    sd = os.path.join(ws, "media", "stems")
    for f in ("vocals.wav", "accomp.wav"):
        if not os.path.exists(os.path.join(sd, f)):
            sys.exit(f"[aicn-audio] missing media/stems/{f}: run `separate` (the original audio is never replaced by silence)")
    n = int(round(float(profile(ws)["duration"]) * SR))
    fit = lambda a: np.pad(a, ((0, max(0, n - len(a))), (0, 0)))[:n]
    V, B = fit(sf.read(os.path.join(sd, "vocals.wav"), dtype="float32", always_2d=True)[0]), \
        fit(sf.read(os.path.join(sd, "accomp.wav"), dtype="float32", always_2d=True)[0])
    T = sf.read(os.path.join(ws, "media", "audio", "dub_voice_48k.wav"), dtype="float32")[0]
    T = np.pad(T, (0, max(0, n - len(T))))[:n]
    t0 = time.time()
    T = voice_chain(T.astype(np.float64))
    lv = lufs(V); gain = lv + c["dub_over_vocal_lu"] - lufs(T[:, None]); T = T * np.float32(10 ** (gain / 20))
    PT, idx = band_power(T); PV, _ = band_power(V); PB, _ = band_power(B)
    fps = SR / HOP; W = BAND_W[:, None]
    active = PT > PT.max(1, keepdims=True) * 10 ** (-45 / 10)
    tot = PT.sum(0); talk = follow((tot > tot.max() * 1e-4).astype(float)[None], fps, c["attack_s"], 0.3)[0]

    def need(Pc, snr, floor):
        g = np.sqrt(np.clip(PT / (Pc * 10 ** (snr / 10) + 1e-12), 10 ** (floor / 10), 1.0))
        return np.where(active, g, 1.0)
    gV = need(PV, c["snr_vocal_db"], c["floor_vocal_db"]) * (1 - talk * (1 - 10 ** (c["base_vocal_db"] / 20)))
    gB = need(PB, c["snr_bgm_db"], c["floor_bgm_db"])
    gV = follow(1 - W * (1 - gV), fps, c["attack_s"], c["release_s"])
    gB = follow(1 - W * (1 - gB), fps, c["attack_s"], c["release_s"])
    mix = apply_gain(V, gV, idx) + apply_gain(B, gB, idx) + T[:, None]
    os.makedirs(os.path.join(ws, "media", "audio"), exist_ok=True)
    sf.write(os.path.join(ws, "media", "audio", "premaster_48k.wav"), mix.astype(np.float32), SR, subtype="FLOAT")
    on = talk > 0.5
    comp = (PV[SPEECH] * gV[SPEECH] ** 2 + PB[SPEECH] * gB[SPEECH] ** 2).sum(0)
    snr = 10 * np.log10(PT[SPEECH].sum(0)[on] / (comp[on] + 1e-12) + 1e-12) if on.any() else np.array([99.0])
    stats = {"dub_gain_db": round(gain, 2), "orig_vocal_lufs": round(lv, 2), "dub_active_share": round(float(on.mean()), 3),
             "dub_snr_speech_db_median": round(float(np.median(snr)), 2),
             "dub_snr_speech_db_p10": round(float(np.percentile(snr, 10)), 2),
             "orig_vocal_atten_db_median_during_dub": round(float(np.median(20 * np.log10(gV[SPEECH][:, on].mean(0)))), 2) if on.any() else 0,
             "orig_vocal_atten_db_min": round(float(20 * np.log10(gV.min())), 2),
             "bgm_atten_db_median_during_dub": round(float(np.median(20 * np.log10(gB[SPEECH][:, on].mean(0)))), 2) if on.any() else 0,
             "bgm_atten_db_min": round(float(20 * np.log10(gB.min())), 2), "seconds": round(time.time() - t0, 1)}
    aicn.jdump(os.path.join(ws, "data", "03_mix_stats.json"), stats)
    print("[mix] " + json.dumps(stats))


# ---------------------------------------------------------------- master
def cmd_master(ws, c, fh):
    mode = profile(ws).get("localization_mode", "voice_dub")
    src = os.path.join(ws, "media", "audio", "premaster_48k.wav")
    if mode == "sub_only":
        os.makedirs(os.path.dirname(src), exist_ok=True)
        run(["ffmpeg", "-v", "error", "-y", "-i", source_path(ws), "-vn", "-ac", "2", "-ar", str(SR), "-c:a", "pcm_f32le", src])
    if not os.path.exists(src):
        sys.exit("[aicn-audio] no premaster: run `mix`")
    pre = "acompressor=threshold=-18dB:ratio=2:attack=20:release=250:makeup=1"
    ln = f"loudnorm=I={c['target_lufs']}:TP={c['true_peak_db']}:LRA={c['lra']}"
    o = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", src, "-af", f"{pre},{ln}:print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
    m = json.loads(o[o.rindex("{"): o.rindex("}") + 1])
    ln2 = (f"{ln}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}"
           f":measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
    out = os.path.join(ws, "media", "master_audio_48k.wav")
    dur = float(profile(ws)["duration"])
    run(["ffmpeg", "-v", "error", "-y", "-i", src, "-af", f"{pre},{ln2},aresample={SR},apad", "-t", f"{dur:.3f}",
         "-ar", str(SR), "-ac", "2", "-c:a", "pcm_s24le", out])
    i, pk = lufs_file(out)
    print(f"[master] {out}: {i:.1f} LUFS, peak {pk:.1f} dBFS")


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
        cur = aicn.h_obj({"src": fh(source_path(ws)), "model": c["demucs_model"], "v": AUDIO_VERSION})
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
    passed = all(x["passed"] for x in checks)
    aicn.jdump(os.path.join(ws, "data", "03_mix_report.json"), {"passed": passed, "version": AUDIO_VERSION, "checks": checks,
                                                                 "params": c})
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
    ap.add_argument("cmd", choices=["separate", "dub", "mix", "master", "verify", "all"])
    ap.add_argument("--unit", required=True)
    a = ap.parse_args()
    ws = os.path.abspath(a.unit); c = cfg(ws); fh = aicn.FastHash(ws)
    mode = profile(ws).get("localization_mode", "voice_dub")
    steps = {"separate": [cmd_separate], "dub": [cmd_dub], "mix": [cmd_mix], "master": [cmd_master], "verify": [cmd_verify],
             "all": ([cmd_master, cmd_verify] if mode == "sub_only" else [cmd_separate, cmd_dub, cmd_mix, cmd_master, cmd_verify])}
    for f in steps[a.cmd]:
        f(ws, c, fh)
    fh.save()


if __name__ == "__main__":
    main()
