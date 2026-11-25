#!/usr/bin/env python3
"""
Stage 1A: Multi-Pass Acoustic & Multi-Band Whisper ASR (run_asr_and_scan.py, v3.0.0).
  - Supports warm multi-unit execution (--units u1,u2,...) loading Whisper ONCE in VRAM
    with background CPU audio pre-decoding, plus single-unit (--video/--workspace) mode.
  - Uses VRAM-aware ap.gpu_slot("whisper", vram_mb=2560) and pure content-addressed SHA-256
    cache keys via ap.stage1_asr_key() (zero path dependence, zero dead v1.x bbox scan).
  - Pass 1 DTW orphan guard prevents 1-CJK-char word splits across silences.
  - Pass 2B +0.08s protective collar, neighbor-clamped padding, word-midpoint exclusion,
    and strict boundary deduplication eliminate boundary fragments & duplicates.
Enforces Stage 0 CI/CD gate before running.
"""
import argparse
import contextlib
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from cicd_gate import require_gate
try:
    import aicn_platform as ap
except ImportError:
    ap = None

from analyze_voice_waveform import (
    _fast_file_sig,
    build_multiband_audio_streams,
    cjk_alnum_chars,
    cjk_chars,
    decode_audio_16k,
    detect_uncovered_multiband_gaps,
    enforce_monotonic_segments,
    is_boundary_duplicate_pair,
)


ASR_PIPELINE_VERSION = "asr-4.0"
HALLUCINATION_SUBSTRINGS = (
    "索兰娅", "明镜", "点点栏目", "打赏支持", "不吝点赞", "请不吝", "点赞订阅", "订阅转发", "欢迎订阅",
    "谢谢观看", "感谢观看", "感谢收看", "字幕由", "字幕制作", "中文字幕", "优优独播", "Amara",
    "普通话的句子", "简体中文", "体中文", "Thank you for watching", "Thanks for watching",
    "Please subscribe", "Subtitles by", "以下是普通话",
)


def _atomic_write_json(path: str, obj) -> None:
    if ap is not None and hasattr(ap, "atomic_write_json"):
        ap.atomic_write_json(path, obj)
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _stage1_asr_key(vid_sha: str, voc_sha: str, model_size: str, source_lang: str) -> str:
    if ap is not None and hasattr(ap, "stage1_asr_key"):
        return ap.stage1_asr_key(vid_sha, voc_sha, model_size, source_lang, ASR_PIPELINE_VERSION)
    payload = json.dumps(
        {"v": ASR_PIPELINE_VERSION, "src": vid_sha, "voc": voc_sha, "model": model_size, "lang": source_lang},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:24]


def _global_asr_cache_path(ws: str, asr_key: str) -> str:
    if ap is not None and hasattr(ap, "global_cache_path"):
        return ap.global_cache_path("asr", asr_key, ".json", hint_path=ws)
    gdir = os.path.join(ap.global_cache_dir(ws), "asr") if (ap and hasattr(ap, "global_cache_dir")) else os.path.join(ws, ".cache", "asr")
    os.makedirs(gdir, exist_ok=True)
    return os.path.join(gdir, f"{asr_key}.json")


def _whisper_gpu_ctx(bk_eng: str):
    if bk_eng not in ("mlx", "faster-cuda") or ap is None:
        return contextlib.nullcontext()
    if hasattr(ap, "gpu_slot"):
        return ap.gpu_slot("whisper", vram_mb=2560, label="WHISPER")
    return contextlib.nullcontext()


def _initial_prompt_for(source_lang: str) -> str | None:
    if source_lang == "zh":
        return "以下是普通话的句子，使用简体中文。"
    return None


def _is_hallucination(text: str) -> bool:
    t = text.strip()
    if not t:
        return True
    low = t.lower()
    return any(h.lower() in low for h in HALLUCINATION_SUBSTRINGS)


class _Seg:
    def __init__(self, d):
        self.text, self.start, self.end = d["text"], d["start"], d["end"]
        self.avg_logprob = d.get("avg_logprob", -0.2)
        self.no_speech_prob = d.get("no_speech_prob", 0.0)
        self.compression_ratio = d.get("compression_ratio", 1.0)


class _MlxWhisper:
    """faster-whisper compatible facade over mlx-whisper (Metal GPU)."""
    REPO = {
        "small": "mlx-community/whisper-small-mlx",
        "base": "mlx-community/whisper-base-mlx",
        "medium": "mlx-community/whisper-medium-mlx",
        "large-v3": "mlx-community/whisper-large-v3-mlx",
    }

    def __init__(self, size):
        self.repo = self.REPO.get(size, size)

    def transcribe(
        self, audio, language="zh", word_timestamps=False, beam_size=None, vad_filter=None,
        vad_parameters=None, condition_on_previous_text=False, temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
        initial_prompt=None,
    ):
        import mlx_whisper
        prompt = initial_prompt if initial_prompt is not None else (
            "以下是普通话的句子，使用简体中文。" if (language == "zh" and vad_filter is not False) else None
        )
        r = mlx_whisper.transcribe(
            np.asarray(audio, np.float32),
            path_or_hf_repo=self.repo,
            language=language,
            word_timestamps=word_timestamps,
            condition_on_previous_text=condition_on_previous_text,
            temperature=temperature,
            verbose=None,
            initial_prompt=prompt,
        )
        keep = [
            d for d in r["segments"]
            if not (d.get("no_speech_prob", 0) > 0.6 and d.get("avg_logprob", 0) < -1.0)
            and d.get("compression_ratio", 0) <= 2.4
        ]
        return [_Seg(d) for d in keep], r


def load_whisper(size):
    """Select Whisper backend via aicn_platform (mlx on Apple Silicon, CUDA faster-whisper on NVIDIA, CPU int8 fallback)."""
    if ap and hasattr(ap, "register_cuda_dll_dirs"):
        ap.register_cuda_dll_dirs()
    bk = ap.detect_asr_backend(sys.executable) if ap else {"engine": os.environ.get("AICN_ASR", "mlx")}
    eng = bk["engine"]
    if eng == "mlx":
        try:
            import mlx_whisper  # noqa: F401
            print(f"[ASR] engine=mlx-whisper ({size}) on Metal", flush=True)
            return _MlxWhisper(size)
        except ImportError:
            print("[ASR] mlx_whisper missing -> falling back to faster-whisper", flush=True)
            eng = "faster-cpu"
    if eng == "faster-cuda":
        try:
            import torch  # noqa: F401
        except ImportError:
            pass
    from faster_whisper import WhisperModel
    if eng == "faster-cuda":
        ctypes = ("int8_float16", "float16") if "large" in size else ("float16", "int8_float16")
        for ctype in ctypes:
            try:
                print(f"[ASR] engine=faster-whisper ({size}) on CUDA ({ctype})", flush=True)
                return WhisperModel(size, device="cuda", compute_type=ctype)
            except Exception as exc:
                print(f"[ASR] CUDA compute_type={ctype} failed ({exc}); trying fallback", flush=True)
    print(f"[ASR] engine=faster-whisper ({size}) on CPU (int8)", flush=True)
    return WhisperModel(size, device="cpu", compute_type="int8")


def resolve_source_language(workspace: str, cli_lang: str | None = None) -> str:
    """Resolve source audio language from CLI flag, project_config.json, unit series_bible.json, or hub series_bible.json."""
    if cli_lang:
        return cli_lang
    cfg = {}
    cfg_p = os.path.join(workspace, "config", "project_config.json")
    if os.path.exists(cfg_p):
        with open(cfg_p, "r", encoding="utf-8") as f:
            cfg = json.load(f) or {}
        if cfg.get("source_language"):
            return str(cfg["source_language"])
    unit_bible_p = os.path.join(workspace, "config", "series_bible.json")
    if os.path.exists(unit_bible_p):
        with open(unit_bible_p, "r", encoding="utf-8") as bf:
            ub = json.load(bf) or {}
        if ub.get("source_language"):
            return str(ub["source_language"])
    hub = ""
    if cfg.get("series_hub_rel"):
        cand_rel = os.path.abspath(os.path.join(workspace, str(cfg["series_hub_rel"])))
        if os.path.isdir(cand_rel):
            hub = cand_rel
    if not hub and cfg.get("series_hub_dir") and os.path.isdir(str(cfg["series_hub_dir"])):
        hub = str(cfg["series_hub_dir"])
    if not hub:
        cand_hub = os.path.dirname(os.path.dirname(os.path.abspath(workspace)))
        if os.path.basename(os.path.dirname(os.path.abspath(workspace))) == "units" and os.path.isdir(cand_hub):
            hub = cand_hub
    bible_p = os.path.join(hub, "config", "series_bible.json") if hub else ""
    if bible_p and os.path.exists(bible_p):
        with open(bible_p, "r", encoding="utf-8") as bf:
            bible = json.load(bf) or {}
        if bible.get("source_language"):
            return str(bible["source_language"])
    return "zh"


def _span_rms(audio: np.ndarray, start_s: float, end_s: float, sr: int = 16000) -> float:
    if audio is None or len(audio) == 0:
        return 0.0
    i0 = max(0, int(round(start_s * sr)))
    i1 = min(len(audio), int(round(end_s * sr)))
    if i1 <= i0:
        return 0.0
    return float(np.sqrt(np.mean(audio[i0:i1] ** 2) + 1e-12))


def _anchor_words_at(words: list, ts: float) -> list:
    return [
        type("_W", (), {"word": w.word, "start": ts, "end": ts})()
        for w in words
    ]


def split_pass1_words_with_orphan_guard(
    seg,
    voc_audio: np.ndarray,
    avg_prob: float,
    sr: int = 16000,
) -> list[dict]:
    """
    Split a Pass 1 Whisper segment on >= 1.0s word gaps while guarding against 1-CJK-char
    DTW-drifted word orphans (e.g. '系'/'统', '卫'/'生跟胖瘦没关系', '姜'/'应雪').
    """
    words = list(getattr(seg, "words", None) or [])
    txt = (seg.text or "").strip()
    if not words:
        st_s = round(max(0.0, float(seg.start)), 3)
        et_s = round(max(st_s + 0.15, float(seg.end)), 3)
        return [{"id": -1, "start": st_s, "end": et_s, "text": txt, "avg_logprob": avg_prob, "source": "pass1_asr"}]

    chunks = [[words[0]]]
    for w in words[1:]:
        if (float(w.start) - float(chunks[-1][-1].end)) >= 1.0:
            chunks.append([w])
        else:
            chunks[-1].append(w)

    # Guard 1-CJK-char DTW orphans created by splitting a single Whisper segment across silences
    if len(chunks) > 1:
        merged = []
        i = 0
        while i < len(chunks):
            ch = chunks[i]
            ch_txt = "".join(w.word for w in ch).strip()
            n_cjk = len(cjk_chars(ch_txt))
            is_standalone_excl = ch_txt.endswith(("！", "!", "？", "?"))
            st_c = max(float(seg.start), float(ch[0].start))
            et_c = min(float(seg.end), max(st_c + 0.15, float(ch[-1].end)))
            loc_rms = _span_rms(voc_audio, st_c, et_c, sr=sr)

            if n_cjk <= 1 and not is_standalone_excl:
                # Prefer merging forward into chunks[i+1] if present (DTW drags leading char early);
                # if chunks[i+1] itself is a late-drifted <=1-char silence orphan while ch has real voice,
                # pull chunks[i+1] onto ch's end instead of dragging ch into silence.
                if i + 1 < len(chunks):
                    nxt_ch = chunks[i + 1]
                    nxt_txt = "".join(w.word for w in nxt_ch).strip()
                    st_n = max(float(seg.start), float(nxt_ch[0].start))
                    et_n = min(float(seg.end), max(st_n + 0.15, float(nxt_ch[-1].end)))
                    nxt_rms = _span_rms(voc_audio, st_n, et_n, sr=sr)
                    if len(cjk_chars(nxt_txt)) <= 1 and loc_rms >= 0.018 and nxt_rms < max(0.018, loc_rms * 0.45):
                        ch = ch + _anchor_words_at(nxt_ch, ch[-1].end)
                        chunks[i + 1] = ch
                        i += 1
                        continue
                    if loc_rms < 0.035 or loc_rms < nxt_rms * 0.85 or len(cjk_chars(nxt_txt)) >= 1:
                        anchored = _anchor_words_at(ch, nxt_ch[0].start) if loc_rms < max(0.018, nxt_rms * 0.45) else ch
                        chunks[i + 1] = anchored + nxt_ch
                        i += 1
                        continue
                elif merged:
                    prev_ch = merged[-1]
                    st_p = max(float(seg.start), float(prev_ch[0].start))
                    et_p = min(float(seg.end), max(st_p + 0.15, float(prev_ch[-1].end)))
                    prev_rms = _span_rms(voc_audio, st_p, et_p, sr=sr)
                    anchored = _anchor_words_at(ch, prev_ch[-1].end) if loc_rms < max(0.018, prev_rms * 0.45) else ch
                    prev_ch.extend(anchored)
                    i += 1
                    continue
            merged.append(ch)
            i += 1
        chunks = merged

    out = []
    for ch in chunks:
        ch_txt = "".join(w.word for w in ch).strip()
        if not ch_txt or _is_hallucination(ch_txt):
            continue
        st_w = round(max(0.0, float(seg.start), float(ch[0].start)), 3)
        et_w = round(min(float(seg.end), max(st_w + 0.15, float(ch[-1].end))), 3)
        if et_w - st_w < 0.15:
            et_w = round(st_w + 0.15, 3)
        out.append({
            "id": -1,
            "start": st_w,
            "end": et_w,
            "text": ch_txt,
            "avg_logprob": avg_prob,
            "source": "pass1_asr",
        })
    return out


def _text_char_overlap(a: str, b: str) -> float:
    """Character-level Dice similarity (robust for Chinese clauses and short English phrases)."""
    sa = list(cjk_alnum_chars(a))
    sb = list(cjk_alnum_chars(b))
    if not sa or not sb:
        return 0.0
    set_a, set_b = set(sa), set(sb)
    return 2.0 * len(set_a & set_b) / (len(set_a) + len(set_b))


def _mask_covered_spans_in_clip(
    clip: np.ndarray,
    pad_s: float,
    covered_spans: list[tuple[float, float]] | None,
    sr: int = 16000,
) -> np.ndarray:
    """
    Apply a protective acoustic mask over any sample overlapping adjacent Pass 1 speech spans
    so Whisper in Pass 2B (vad_filter=False) cannot hear and echo neighbor boundary words.
    """
    if not covered_spans or len(clip) == 0:
        return clip
    pad_e = pad_s + len(clip) / sr
    out = None
    fade = max(1, int(round(0.020 * sr)))
    for cs, ce in covered_spans:
        ov_s = max(pad_s, cs - 0.02)
        ov_e = min(pad_e, ce + 0.02)
        if ov_e <= ov_s:
            continue
        if out is None:
            out = clip.copy()
        i0 = max(0, int(round((ov_s - pad_s) * sr)))
        i1 = min(len(out), int(round((ov_e - pad_s) * sr)))
        if i1 <= i0:
            continue
        out[i0:i1] = 0.0
        if i0 > 0:
            f_in = min(fade, i0)
            out[i0 - f_in:i0] *= np.linspace(1.0, 0.0, f_in, dtype=np.float32)
        if i1 < len(out):
            f_out = min(fade, len(out) - i1)
            out[i1:i1 + f_out] *= np.linspace(0.0, 1.0, f_out, dtype=np.float32)
    return out if out is not None else clip


def _in_covered_spans(ts: float, covered_spans: list[tuple[float, float]] | None, margin: float = 0.02) -> bool:
    if not covered_spans:
        return False
    for cs, ce in covered_spans:
        if (cs - margin) <= ts <= (ce + margin):
            return True
    return False


def _neighbor_clamped_pad(
    gst: float,
    get: float,
    covered_spans: list[tuple[float, float]] | None,
    total_dur: float,
    prev_gap_end: float | None = None,
    next_gap_start: float | None = None,
) -> tuple[float, float]:
    """Clamp slice padding strictly outside neighboring Pass 1 spans and adjacent split gaps."""
    pad_s = max(0.0, gst - 0.15)
    pad_e = min(total_dur, get + 0.15)
    if covered_spans:
        prev_ends = [ce for _, ce in covered_spans if ce <= gst + 0.02]
        if prev_ends:
            pad_s = max(pad_s, max(prev_ends) + 0.02)
        next_starts = [cs for cs, _ in covered_spans if cs >= get - 0.02]
        if next_starts:
            pad_e = min(pad_e, min(next_starts) - 0.02)
    # Prevent consecutive split windows (>10s gap split at valley) from overlapping each other's padding
    if prev_gap_end is not None and (gst - prev_gap_end) < 0.30:
        pad_s = max(pad_s, 0.5 * (prev_gap_end + gst) - 0.01)
    if next_gap_start is not None and (next_gap_start - get) < 0.30:
        pad_e = min(pad_e, 0.5 * (get + next_gap_start) + 0.01)
    return pad_s, pad_e


def _probe_band_clip(
    model,
    wave: np.ndarray,
    pad_s: float,
    pad_e: float,
    gst: float,
    get: float,
    band_name: str,
    whisper_lang: str | None,
    source_lang: str,
    peak_voc_rms: float,
    sr: int = 16000,
    covered_spans: list[tuple[float, float]] | None = None,
) -> list:
    """Run vad_filter=False Whisper on one frequency-band slice with protective collar & word exclusion."""
    s_idx = max(0, int(round(pad_s * sr)))
    e_idx = min(len(wave), int(round(pad_e * sr)))
    clip = wave[s_idx:e_idx]
    if len(clip) < int(0.30 * sr):
        return []
    clip = _mask_covered_spans_in_clip(clip, pad_s, covered_spans, sr=sr)
    mx = float(np.max(np.abs(clip)))
    if mx <= 0.002:
        return []
    clip = (clip / mx * 0.85).astype(np.float32)

    kwargs = dict(
        language=whisper_lang,
        beam_size=3 if band_name in ("full_vocal", "raw_clean") else 2,
        word_timestamps=True,
        vad_filter=False,
        condition_on_previous_text=False,
        temperature=0.0,
    )
    sub_gen, _ = model.transcribe(clip, **kwargs)

    cands = []
    for sg in list(sub_gen):
        full_txt = (sg.text or "").strip()
        if not full_txt or _is_hallucination(full_txt):
            continue
        rec_prob = round(float(getattr(sg, "avg_logprob", -0.6)), 3)
        no_sp = float(getattr(sg, "no_speech_prob", 0.0))
        comp_r = float(getattr(sg, "compression_ratio", 1.0))

        words = list(getattr(sg, "words", None) or [])
        if words:
            kept_words = []
            for w in words:
                abs_wm = pad_s + 0.5 * (float(w.start) + float(w.end))
                if (gst - 0.02) <= abs_wm <= (get + 0.02) and not _in_covered_spans(abs_wm, covered_spans):
                    kept_words.append(w)
            if not kept_words:
                continue
            rec_text = "".join(w.word for w in kept_words).strip()
            if not rec_text or _is_hallucination(rec_text):
                continue
            seg_st = round(max(gst, pad_s + float(kept_words[0].start)), 3)
            seg_et = round(min(get, pad_s + float(kept_words[-1].end)), 3)
        else:
            rec_text = full_txt
            seg_st = round(max(gst, pad_s + float(getattr(sg, "start", 0.0))), 3)
            seg_et = round(min(get, pad_s + float(getattr(sg, "end", get - pad_s))), 3)

        n_cjk = len(cjk_chars(rec_text))
        n_chars = max(1, len(cjk_alnum_chars(rec_text)))
        if source_lang == "zh":
            has_words = (n_cjk >= 2) or (
                n_cjk == 1 and rec_prob > -0.35 and no_sp < 0.20 and peak_voc_rms >= 0.045
            )
        else:
            has_words = len(rec_text.strip()) >= 3
        if not (rec_prob > -0.78 and no_sp < 0.50 and comp_r <= 2.2 and has_words):
            continue

        max_ok_dur = n_chars * 0.35 + 1.25
        if (seg_et - seg_st) > max_ok_dur:
            seg_et = round(seg_st + max_ok_dur, 3)
        if seg_et - seg_st < 0.18:
            continue
        base_score = round(rec_prob - 0.80 * no_sp - max(0.0, comp_r - 1.8) * 0.40, 4)
        cands.append({
            "start": seg_st,
            "end": seg_et,
            "text": rec_text,
            "avg_logprob": rec_prob,
            "no_speech_prob": round(no_sp, 3),
            "compression_ratio": round(comp_r, 3),
            "band": band_name,
            "base_score": base_score,
        })
    return cands


def _refine_span_to_band_energy(seg_st: float, seg_et: float, band_raw: np.ndarray, sr: int = 16000):
    """Trim dead leading/trailing silence inside a recovered segment using 25ms RMS hops."""
    if band_raw is None or len(band_raw) == 0 or (seg_et - seg_st) <= 0.45:
        return round(seg_st, 3), round(seg_et, 3)
    hop = max(1, int(round(0.025 * sr)))
    i0 = max(0, int(round(seg_st * sr)))
    i1 = min(len(band_raw), int(round(seg_et * sr)))
    seg_x = band_raw[i0:i1]
    n_h = len(seg_x) // hop
    if n_h < 6:
        return round(seg_st, 3), round(seg_et, 3)
    blks = seg_x[: n_h * hop].reshape(n_h, hop)
    rms = np.sqrt(np.mean(blks ** 2, axis=1) + 1e-12)
    pk = float(np.max(rms))
    if pk <= 0.002:
        return round(seg_st, 3), round(seg_et, 3)
    act = np.where(rms >= max(0.0025, pk * 0.16))[0]
    if act.size == 0:
        return round(seg_st, 3), round(seg_et, 3)
    new_st = max(seg_st, seg_st + max(0, int(act[0]) - 2) * 0.025)
    new_et = min(seg_et, seg_st + min(n_h, int(act[-1]) + 3) * 0.025)
    if (new_et - new_st) < 0.25:
        return round(seg_st, 3), round(seg_et, 3)
    return round(new_st, 3), round(new_et, 3)


def fuse_and_refine_multiband_candidates(
    candidates: list,
    streams: dict,
    gap_meta: dict,
    model=None,
    whisper_lang: str | None = None,
    source_lang: str = "zh",
    sr: int = 16000,
    covered_spans: list[tuple[float, float]] | None = None,
) -> list:
    """
    Reverse Cross-Check & Cross-Band Candidate Fusion:
      1. Clusters temporally overlapping ASR candidates across full_vocal / high / mid / low bands.
      2. Multi-band consensus (>=2 bands) gets +0.18 confidence bonus.
      3. Single-band detections with weak vocals (<0.022 RMS) cross-check on raw_clean.
      4. Refines winning span to active band energy onset/offset.
    """
    if not candidates:
        return []

    ordered = sorted(candidates, key=lambda c: (c["start"], c["end"]))
    clusters = []
    for c in ordered:
        placed = False
        for cl in clusters:
            cl_st = min(x["start"] for x in cl)
            cl_et = max(x["end"] for x in cl)
            ov = max(0.0, min(cl_et, c["end"]) - max(cl_st, c["start"]))
            min_d = max(0.15, min(cl_et - cl_st, c["end"] - c["start"]))
            union_d = max(0.15, max(cl_et, c["end"]) - min(cl_st, c["start"]))
            if (ov / min_d) >= 0.45 or (ov / union_d) >= 0.30:
                cl.append(c)
                placed = True
                break
        if not placed:
            clusters.append([c])

    band_to_cue = {
        "full_vocal": gap_meta.get("kind", "normal"),
        "high": "high_voice",
        "mid": "phone_call" if gap_meta.get("kind") == "phone_call" else "mid_voice",
        "low": "low_voice",
        "raw_clean": "raw_verified",
    }

    fused = []
    voc_raw = streams.get("voc_raw")
    raw_clean_wave = streams.get("raw_clean")
    total_dur = (len(raw_clean_wave) / sr) if raw_clean_wave is not None else float(gap_meta.get("end", 0.0))

    for cl in clusters:
        bands_present = sorted({x["band"] for x in cl})
        cl_st = min(x["start"] for x in cl)
        cl_et = max(x["end"] for x in cl)

        if len(bands_present) == 1 and model is not None and raw_clean_wave is not None:
            s0 = max(0, int(round(cl_st * sr)))
            s1 = min(len(voc_raw), int(round(cl_et * sr))) if voc_raw is not None else 0
            local_voc_rms = float(np.sqrt(np.mean(voc_raw[s0:s1] ** 2) + 1e-12)) if s1 > s0 else 0.0
            if local_voc_rms < 0.022 and bands_present[0] != "raw_clean":
                r_pad_s, r_pad_e = _neighbor_clamped_pad(cl_st, cl_et, covered_spans, total_dur)
                rev_cands = _probe_band_clip(
                    model=model,
                    wave=raw_clean_wave,
                    pad_s=r_pad_s,
                    pad_e=r_pad_e,
                    gst=cl_st,
                    get=cl_et,
                    band_name="raw_clean",
                    whisper_lang=whisper_lang,
                    source_lang=source_lang,
                    peak_voc_rms=float(gap_meta.get("peak_voc_rms", 0.0)),
                    sr=sr,
                    covered_spans=covered_spans,
                )
                for rc in rev_cands:
                    if max(0.0, min(cl_et, rc["end"]) - max(cl_st, rc["start"])) >= 0.15:
                        cl.append(rc)

        best_item = None
        best_score = -999.0
        for cand in cl:
            other_bands = [x for x in cl if x["band"] != cand["band"]]
            agreed_bands = {cand["band"]}
            max_txt_sim = 0.0
            for ob in other_bands:
                sim = _text_char_overlap(cand["text"], ob["text"])
                max_txt_sim = max(max_txt_sim, sim)
                if sim >= 0.25 or (min(cand["end"], ob["end"]) - max(cand["start"], ob["start"])) >= 0.25:
                    agreed_bands.add(ob["band"])
            consensus_bonus = (0.18 if len(agreed_bands) >= 2 else 0.0) + (0.10 * max_txt_sim)
            bw_bonus = 0.03 if cand["band"] in ("full_vocal", "raw_clean") else (
                0.01 if cand["band"] == gap_meta.get("dominant_band") else 0.0
            )
            total_score = cand["base_score"] + consensus_bonus + bw_bonus
            if total_score > best_score:
                best_score = total_score
                best_item = (cand, sorted(agreed_bands), round(total_score, 4))

        if best_item is None:
            continue
        win, agreed_bands, win_score = best_item

        s0_v = max(0, int(round(win["start"] * sr)))
        s1_v = min(len(voc_raw), int(round(win["end"] * sr))) if voc_raw is not None else 0
        seg_voc_rms = float(np.sqrt(np.mean(voc_raw[s0_v:s1_v] ** 2) + 1e-12)) if s1_v > s0_v else 0.0
        is_phone_gap = (gap_meta.get("kind") == "phone_call") or (win["band"] == "mid" and float(gap_meta.get("peak_mid_rms", 0.0)) > 0.025)
        if voc_raw is not None and float(np.max(np.abs(voc_raw))) > 0.02 and seg_voc_rms < 0.0040 and not is_phone_gap:
            if not (len(agreed_bands) >= 2 and win["avg_logprob"] > -0.50 and win["no_speech_prob"] < 0.25):
                continue
        if len(agreed_bands) == 1:
            if not (win["avg_logprob"] > -0.72 and win["no_speech_prob"] < 0.45):
                continue
            b_raw = streams.get(f"{win['band']}_raw")
            if b_raw is not None and len(b_raw) > 0:
                s0 = max(0, int(round(win["start"] * sr)))
                s1 = min(len(b_raw), int(round(win["end"] * sr)))
                if s1 > s0 and float(np.sqrt(np.mean(b_raw[s0:s1] ** 2) + 1e-12)) < 0.003:
                    continue

        ref_st, ref_et = _refine_span_to_band_energy(
            win["start"], win["end"], streams.get(f"{win['band']}_raw"), sr=sr
        )
        n_chars = max(1, len(cjk_alnum_chars(win["text"])))
        if (ref_et - ref_st) > (n_chars * 0.35 + 1.25):
            ref_et = round(ref_st + n_chars * 0.35 + 1.25, 3)
        fused.append({
            "id": -1,
            "start": ref_st,
            "end": ref_et,
            "text": win["text"],
            "avg_logprob": win["avg_logprob"],
            "cue_type": band_to_cue.get(win["band"], "normal"),
            "winning_band": win["band"],
            "bands_agreed": agreed_bands,
            "fusion_score": win_score,
            "source": f"pass2b_multiband_{win['band']}",
        })

    return fused


def _probe_band_gaps_stitched(
    model,
    wave: np.ndarray,
    gap_entries: list,
    band_name: str,
    whisper_lang: str | None,
    source_lang: str,
    total_dur: float,
    sr: int = 16000,
    max_batch_s: float = 22.0,
    sep_s: float = 0.65,
    force_stitch: bool = False,
    covered_spans: list[tuple[float, float]] | None = None,
) -> dict:
    """
    Stitched Multi-Gap Batching with Neighbor-Clamped Padding & Word-Midpoint Exclusion:
      - Clamps slice padding strictly outside neighboring Pass 1 segments and adjacent split gaps.
      - Masks any sample inside Pass 1 spans via _mask_covered_spans_in_clip().
      - Drops any decoded word whose midpoint falls outside [gst - 0.02, get + 0.02] or inside Pass 1 spans.
    """
    by_gap = {idx: [] for idx, _ in gap_entries}
    if not gap_entries:
        return by_gap

    can_stitch = force_stitch or (hasattr(model, "feature_extractor") and len(gap_entries) >= 2)
    if not can_stitch:
        for pos, (g_idx, g) in enumerate(gap_entries):
            gst, get = float(g["start"]), float(g["end"])
            prev_ge = float(gap_entries[pos - 1][1]["end"]) if pos > 0 else None
            next_gs = float(gap_entries[pos + 1][1]["start"]) if pos + 1 < len(gap_entries) else None
            pad_s, pad_e = _neighbor_clamped_pad(gst, get, covered_spans, total_dur, prev_ge, next_gs)
            by_gap[g_idx] = _probe_band_clip(
                model=model,
                wave=wave,
                pad_s=pad_s,
                pad_e=pad_e,
                gst=gst,
                get=get,
                band_name=band_name,
                whisper_lang=whisper_lang,
                source_lang=source_lang,
                peak_voc_rms=float(g.get("peak_voc_rms", 0.0)),
                sr=sr,
                covered_spans=covered_spans,
            )
        return by_gap

    sep_samples = np.zeros(int(round(sep_s * sr)), dtype=np.float32)
    batches = []
    cur_pieces = []
    cur_slots = []
    cur_samples = 0
    max_samples = int(round(max_batch_s * sr))

    for pos, (g_idx, g) in enumerate(gap_entries):
        gst, get = float(g["start"]), float(g["end"])
        prev_ge = float(gap_entries[pos - 1][1]["end"]) if pos > 0 else None
        next_gs = float(gap_entries[pos + 1][1]["start"]) if pos + 1 < len(gap_entries) else None
        pad_s, pad_e = _neighbor_clamped_pad(gst, get, covered_spans, total_dur, prev_ge, next_gs)
        s_i = max(0, int(round(pad_s * sr)))
        e_i = min(len(wave), int(round(pad_e * sr)))
        clip = wave[s_i:e_i]
        if len(clip) < int(0.30 * sr):
            continue
        clip = _mask_covered_spans_in_clip(clip, pad_s, covered_spans, sr=sr)
        mx = float(np.max(np.abs(clip)))
        if mx <= 0.002:
            continue
        clip_norm = (clip / mx * 0.85).astype(np.float32)
        needed = len(clip_norm) + (len(sep_samples) if cur_pieces else 0)
        if cur_pieces and (cur_samples + needed) > max_samples:
            batches.append((np.concatenate(cur_pieces), cur_slots))
            cur_pieces, cur_slots, cur_samples = [], [], 0
        if cur_pieces:
            cur_pieces.append(sep_samples)
            cur_samples += len(sep_samples)
        buf_st = cur_samples / sr
        cur_pieces.append(clip_norm)
        cur_samples += len(clip_norm)
        buf_et = cur_samples / sr
        cur_slots.append((buf_st, buf_et, g_idx, pad_s, gst, get, float(g.get("peak_voc_rms", 0.0))))

    if cur_pieces:
        batches.append((np.concatenate(cur_pieces), cur_slots))

    for buf, slots in batches:
        kwargs = dict(
            language=whisper_lang,
            beam_size=2,
            word_timestamps=True,
            vad_filter=False,
            condition_on_previous_text=False,
            temperature=0.0,
        )
        sub_gen, _ = model.transcribe(buf, **kwargs)

        for sg in list(sub_gen):
            full_sg_txt = (sg.text or "").strip()
            # Check hallucination on FULL unchopped segment BEFORE splitting across stitched slots
            if not full_sg_txt or _is_hallucination(full_sg_txt):
                continue
            rec_prob = round(float(getattr(sg, "avg_logprob", -0.6)), 3)
            no_sp = float(getattr(sg, "no_speech_prob", 0.0))
            comp_r = float(getattr(sg, "compression_ratio", 1.0))
            if rec_prob <= -0.78 or no_sp >= 0.48 or comp_r > 2.2:
                continue
            words = list(getattr(sg, "words", None) or [])

            if words:
                slot_buckets = {k: [] for k in range(len(slots))}
                for w in words:
                    wm = 0.5 * (float(w.start) + float(w.end))
                    for si, (bst, bet, g_idx, pad_s, gst, get, _pv) in enumerate(slots):
                        if (bst - 0.18) <= wm <= (bet + 0.18):
                            abs_wm = pad_s + (wm - bst)
                            # Drop collar-bleed words outside [gst - 0.02, get + 0.02] or inside Pass 1 spans
                            if (gst - 0.02) <= abs_wm <= (get + 0.02) and not _in_covered_spans(abs_wm, covered_spans):
                                slot_buckets[si].append(w)
                            break

                for si, wlist in slot_buckets.items():
                    if not wlist:
                        continue
                    bst, bet, g_idx, pad_s, gst, get, p_voc = slots[si]
                    sub_chunks = [[wlist[0]]]
                    for w in wlist[1:]:
                        if (float(w.start) - float(sub_chunks[-1][-1].end)) >= 0.85:
                            sub_chunks.append([w])
                        else:
                            sub_chunks[-1].append(w)
                    for ch in sub_chunks:
                        txt = "".join(x.word for x in ch).strip()
                        if not txt or _is_hallucination(txt):
                            continue
                        w_probs = [float(getattr(x, "probability", 0.85)) for x in ch]
                        if w_probs and (sum(w_probs) / len(w_probs)) < 0.42:
                            continue
                        n_cjk = len(cjk_chars(txt))
                        n_chars = max(1, len(cjk_alnum_chars(txt)))
                        has_words = (
                            (n_cjk >= 2) or (n_cjk == 1 and rec_prob > -0.35 and no_sp < 0.20 and p_voc >= 0.045)
                            if source_lang == "zh" else (len(txt) >= 3)
                        )
                        if not has_words:
                            continue
                        loc_s = max(0.0, float(ch[0].start) - bst)
                        loc_e = min(bet - bst, max(loc_s + 0.18, float(ch[-1].end) - bst))
                        seg_st = round(max(gst, pad_s + loc_s), 3)
                        seg_et = round(min(get, pad_s + loc_e), 3)
                        max_ok_dur = n_chars * 0.35 + 1.25
                        if (seg_et - seg_st) > max_ok_dur:
                            seg_et = round(seg_st + max_ok_dur, 3)
                        if seg_et - seg_st < 0.18:
                            continue
                        base_score = round(rec_prob - 0.80 * no_sp - max(0.0, comp_r - 1.8) * 0.40, 4)
                        by_gap[g_idx].append({
                            "start": seg_st, "end": seg_et, "text": txt,
                            "avg_logprob": rec_prob, "no_speech_prob": round(no_sp, 3),
                            "compression_ratio": round(comp_r, 3), "band": band_name, "base_score": base_score,
                        })
            else:
                txt = full_sg_txt
                sg_s, sg_e = float(getattr(sg, "start", 0.0)), float(getattr(sg, "end", 0.0))
                best_si, best_ov = None, 0.0
                for si, (bst, bet, *_rest) in enumerate(slots):
                    ov = max(0.0, min(bet, sg_e) - max(bst, sg_s))
                    if ov > best_ov:
                        best_ov, best_si = ov, si
                if best_si is None or best_ov < 0.15:
                    continue
                bst, bet, g_idx, pad_s, gst, get, p_voc = slots[best_si]
                n_cjk = len(cjk_chars(txt))
                n_chars = max(1, len(cjk_alnum_chars(txt)))
                has_words = (
                    (n_cjk >= 2) or (n_cjk == 1 and rec_prob > -0.35 and no_sp < 0.20 and p_voc >= 0.045)
                    if source_lang == "zh" else (len(txt) >= 3)
                )
                if not has_words:
                    continue
                loc_s = max(0.0, sg_s - bst)
                loc_e = min(bet - bst, max(loc_s + 0.18, sg_e - bst))
                seg_st = round(max(gst, pad_s + loc_s), 3)
                seg_et = round(min(get, pad_s + loc_e), 3)
                max_ok_dur = n_chars * 0.35 + 1.25
                if (seg_et - seg_st) > max_ok_dur:
                    seg_et = round(seg_st + max_ok_dur, 3)
                if seg_et - seg_st < 0.18:
                    continue
                base_score = round(rec_prob - 0.80 * no_sp - max(0.0, comp_r - 1.8) * 0.40, 4)
                by_gap[g_idx].append({
                    "start": seg_st, "end": seg_et, "text": txt,
                    "avg_logprob": rec_prob, "no_speech_prob": round(no_sp, 3),
                    "compression_ratio": round(comp_r, 3), "band": band_name, "base_score": base_score,
                })
    return by_gap


def _accept_pass2b_candidate(
    item: dict,
    results: list,
    source_lang: str = "zh",
) -> tuple[bool, str]:
    """
    Validate a Pass 2B candidate against existing Pass 1 + accepted Pass 2B segments:
      - Duration >= 0.15s
      - Overlap ratio < 0.20
      - 1-char CJK exclamations must be >= 0.30s clear of neighbors with high confidence
      - Strict boundary deduplication within 0.35s (affix / containment / <=2-char shared / >=0.55 set containment)
      - Strict non-overlapping clamp (>= 0.02s gap from both neighbors)
    """
    seg_st = float(item["start"])
    seg_et = float(item["end"])
    dur = seg_et - seg_st
    if dur < 0.15:
        return False, "runt"

    ov_existing = sum(
        max(0.0, min(seg_et, float(r["end"])) - max(seg_st, float(r["start"])))
        for r in results
    )
    if (ov_existing / max(0.15, dur)) >= 0.20:
        return False, "overlap_existing"

    # Neighbor distance & 1-char exclamation guard
    prev_end = None
    next_start = None
    for r in results:
        rst, ret = float(r["start"]), float(r["end"])
        if ret <= seg_st + 0.10:
            prev_end = ret if prev_end is None else max(prev_end, ret)
        if rst >= seg_et - 0.10:
            next_start = rst if next_start is None else min(next_start, rst)

    if source_lang == "zh" and len(cjk_chars(item.get("text", ""))) == 1:
        gap_l = (seg_st - prev_end) if prev_end is not None else 99.0
        gap_r = (next_start - seg_et) if next_start is not None else 99.0
        if min(gap_l, gap_r) < 0.30 or float(item.get("avg_logprob", -1.0)) <= -0.35:
            return False, "1char_near_neighbor"

    # Strict boundary deduplication within 0.35s against BOTH Pass 1 and accepted Pass 2B segments
    for r in results:
        rst, ret = float(r["start"]), float(r["end"])
        dist = max(rst - seg_et, seg_st - ret)
        if dist <= 0.35:
            if seg_st <= rst:
                flag, reason = is_boundary_duplicate_pair(item, r, gap_thresh_s=0.35, strict=True)
            else:
                flag, reason = is_boundary_duplicate_pair(r, item, gap_thresh_s=0.35, strict=True)
            if flag:
                return False, f"boundary_dup:{reason}"

    # Clamp strictly inside [prev_end + 0.02, next_start - 0.02]
    if prev_end is not None:
        seg_st = max(seg_st, prev_end + 0.02)
    if next_start is not None:
        seg_et = min(seg_et, next_start - 0.02)
    if (seg_et - seg_st) < 0.15:
        return False, "clamped_too_short"

    item["start"] = round(seg_st, 2)
    item["end"] = round(seg_et, 2)
    return True, "ok"


def run_multiband_pass2b_recovery(
    model,
    results: list,
    voc_audio: np.ndarray,
    raw_audio: np.ndarray,
    whisper_lang: str | None,
    source_lang: str,
    sr: int = 16000,
    report_path: str | None = None,
):
    """
    Gap-First Stitched Multi-Band Frequency ASR Recovery (v3.0.0):
      - Uses +0.08s protective collar around Pass 1 spans.
      - Clamps Pass 2B slices strictly outside neighboring spans & masks Pass 1 bleed.
      - Deduplicates boundary fragments & enforces strict monotonic non-overlapping spans.
    """
    streams = build_multiband_audio_streams(voc_audio, raw_audio, sr=sr)
    uncovered_gaps = detect_uncovered_multiband_gaps(
        results, voc_audio, raw_audio, streams=streams, sr=sr, max_window_s=10.0, min_gap_s=0.40
    )

    covered_spans = sorted(
        (float(r["start"]), float(r["end"]))
        for r in (results or [])
        if float(r["end"]) > float(r["start"])
    )

    recovered_by_band = {"full_vocal": 0, "high": 0, "mid": 0, "low": 0, "raw_clean": 0}
    verified_non_speech_gaps = []
    rec_cnt = 0
    dedup_rejected = 0
    total_dur = len(streams["raw_raw"]) / sr

    if uncovered_gaps:
        p2b_t0 = time.time()
        print(
            f"[PASS 2B MULTI-BAND RECOVERY] Probing {len(uncovered_gaps)} uncovered acoustic gaps "
            f"via stitched <=22s batching (protective collar + boundary dedup)...",
            flush=True,
        )
        indexed_gaps = list(enumerate(uncovered_gaps))
        cands_by_gap = {gi: [] for gi, _ in indexed_gaps}
        probed_bands_by_gap = {gi: ["full_vocal"] for gi, _ in indexed_gaps}

        fv_map = _probe_band_gaps_stitched(
            model=model,
            wave=streams["full_vocal"],
            gap_entries=indexed_gaps,
            band_name="full_vocal",
            whisper_lang=whisper_lang,
            source_lang=source_lang,
            total_dur=total_dur,
            sr=sr,
            covered_spans=covered_spans,
        )
        unresolved = []
        for gi, g in indexed_gaps:
            cands_by_gap[gi].extend(fv_map.get(gi, []))
            gdur = max(0.2, float(g["end"]) - float(g["start"]))
            hi_conf_cov = sum(
                max(0.0, c["end"] - c["start"])
                for c in cands_by_gap[gi]
                if c["avg_logprob"] > -0.42 and c["no_speech_prob"] < 0.30
            )
            if (hi_conf_cov / gdur) < 0.65:
                unresolved.append((gi, g))

        for bname in ("high", "mid", "low"):
            wave = streams.get(bname)
            if wave is None or not unresolved:
                continue
            band_todo = []
            for gi, g in unresolved:
                if bname not in g.get("probe_bands", ["high", "mid", "low"]):
                    continue
                gdur = max(0.2, float(g["end"]) - float(g["start"]))
                strong_bands = {c["band"] for c in cands_by_gap[gi] if c["avg_logprob"] > -0.45 and c["no_speech_prob"] < 0.25}
                if len(strong_bands) >= 2:
                    best_b_cov = max(
                        sum(max(0.0, c["end"] - c["start"]) for c in cands_by_gap[gi] if c["band"] == sb and c["avg_logprob"] > -0.45)
                        for sb in strong_bands
                    )
                    if (best_b_cov / gdur) >= 0.65:
                        continue
                band_todo.append((gi, g))
                probed_bands_by_gap[gi].append(bname)
            if not band_todo:
                continue
            b_map = _probe_band_gaps_stitched(
                model=model,
                wave=wave,
                gap_entries=band_todo,
                band_name=bname,
                whisper_lang=whisper_lang,
                source_lang=source_lang,
                total_dur=total_dur,
                sr=sr,
                covered_spans=covered_spans,
            )
            for gi, _ in band_todo:
                cands_by_gap[gi].extend(b_map.get(gi, []))

        for gi, g in indexed_gaps:
            gst, get = float(g["start"]), float(g["end"])
            gdur = max(0.2, get - gst)
            refined = fuse_and_refine_multiband_candidates(
                candidates=cands_by_gap[gi],
                streams=streams,
                gap_meta=g,
                model=model,
                whisper_lang=whisper_lang,
                source_lang=source_lang,
                sr=sr,
                covered_spans=covered_spans,
            )
            gap_added = 0
            for item in sorted(refined, key=lambda x: (float(x["start"]), float(x["end"]))):
                ok, reason = _accept_pass2b_candidate(item, results, source_lang=source_lang)
                if not ok:
                    if reason.startswith("boundary_dup") or reason == "1char_near_neighbor":
                        dedup_rejected += 1
                    continue
                results.append(item)
                rec_cnt += 1
                gap_added += 1
                wb = item.get("winning_band", "full_vocal")
                recovered_by_band[wb] = recovered_by_band.get(wb, 0) + 1

            verified_non_speech_gaps.append({
                "start": gst,
                "end": get,
                "duration": round(gdur, 2),
                "recovered_in_gap": gap_added,
                "kind": g.get("kind", "normal"),
                "probed_bands": probed_bands_by_gap[gi],
            })

        print(
            f"[PASS 2B DONE] Recovered {rec_cnt} segments (dedup_rejected={dedup_rejected}) across "
            f"{len(uncovered_gaps)} gaps in {time.time()-p2b_t0:.1f}s "
            f"(by_band={recovered_by_band}, verified_non_speech={len(verified_non_speech_gaps)}).",
            flush=True,
        )

    if results:
        results = enforce_monotonic_segments(results, min_dur_s=0.15, min_gap_s=0.02)

    report = {
        "pipeline_version": ASR_PIPELINE_VERSION,
        "gaps_probed": len(uncovered_gaps),
        "recovered_total": rec_cnt,
        "dedup_rejected": dedup_rejected,
        "recovered_by_band": recovered_by_band,
        "verified_non_speech_gaps": verified_non_speech_gaps,
    }
    if report_path:
        _atomic_write_json(report_path, report)
    return results, report


def _decode_unit_audio(video_path: str, vocals_path: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    raw_audio = decode_audio_16k(video_path)
    if vocals_path and os.path.exists(vocals_path):
        voc_audio = decode_audio_16k(vocals_path)
        min_len = min(len(raw_audio), len(voc_audio))
        pass1_audio = np.zeros_like(raw_audio)
        pass1_audio[:min_len] = 0.70 * voc_audio[:min_len] + 0.30 * raw_audio[:min_len]
    else:
        voc_audio = raw_audio
        pass1_audio = raw_audio
    return raw_audio, voc_audio, pass1_audio


def _run_multipass_with_loaded_model(
    t0: float,
    model,
    raw_audio: np.ndarray,
    voc_audio: np.ndarray,
    pass1_audio: np.ndarray,
    out_json: str,
    source_lang: str,
    whisper_lang: str | None,
):
    prompt = _initial_prompt_for(source_lang)
    vad_params = dict(threshold=0.20, min_speech_duration_ms=80, min_silence_duration_ms=250, speech_pad_ms=80)
    transcribe_kwargs = dict(
        language=whisper_lang,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters=vad_params,
        condition_on_previous_text=False,
    )
    if prompt:
        transcribe_kwargs["initial_prompt"] = prompt
    segments_gen, _ = model.transcribe(pass1_audio, **transcribe_kwargs)

    results = []
    last_log = time.time()
    for seg in segments_gen:
        txt = seg.text.strip()
        if not txt or _is_hallucination(txt):
            continue
        avg_prob = round(float(getattr(seg, "avg_logprob", -0.2)), 3)
        no_sp = float(getattr(seg, "no_speech_prob", 0.0))
        comp_r = float(getattr(seg, "compression_ratio", 1.0))
        if (no_sp > 0.75 and avg_prob < -1.0) or comp_r > 2.4:
            continue
        results.extend(split_pass1_words_with_orphan_guard(seg, voc_audio, avg_prob, sr=16000))
        if time.time() - last_log >= 15.0:
            print(f"[PROGRESS] Pass 1 transcribed {len(results)} segments (up to {seg.end:.1f}s) in {time.time()-t0:.1f}s", flush=True)
            last_log = time.time()

    if results:
        results = enforce_monotonic_segments(results, min_dur_s=0.15, min_gap_s=0.02)

    print(f"[PASS 1 DONE] {len(results)} segments from primary ASR in {time.time()-t0:.1f}s", flush=True)

    p2b_report_path = os.path.join(os.path.dirname(os.path.abspath(out_json)), "01_pass2b_recovery_report.json")
    results, p2b_report = run_multiband_pass2b_recovery(
        model=model,
        results=results,
        voc_audio=voc_audio,
        raw_audio=raw_audio,
        whisper_lang=whisper_lang,
        source_lang=source_lang,
        sr=16000,
        report_path=p2b_report_path,
    )

    _atomic_write_json(out_json, results)
    print(f"[DONE] Saved {len(results)} total fine-grained ASR segments to {out_json} in {time.time()-t0:.2f}s", flush=True)
    return results, p2b_report


def run_multipass_whisper_asr(
    video_path: str,
    vocals_path: str,
    out_json: str,
    model_size: str = os.environ.get("AICN_WHISPER_MODEL", "large-v3"),
    source_lang: str = "zh",
):
    """Single-unit Multi-Pass Whisper ASR wrapper (acquires VRAM-aware whisper gpu_slot)."""
    t0 = time.time()
    whisper_lang = None if source_lang == "auto" else source_lang
    print(f"[PLAN] Running Multi-Pass whisper ({model_size}, lang={source_lang}) -> {out_json}", flush=True)
    raw_audio, voc_audio, pass1_audio = _decode_unit_audio(video_path, vocals_path)

    bk_eng = ap.detect_asr_backend(sys.executable)["engine"] if ap else "mlx"
    with _whisper_gpu_ctx(bk_eng):
        model = load_whisper(model_size)
        try:
            return _run_multipass_with_loaded_model(
                t0=t0,
                model=model,
                raw_audio=raw_audio,
                voc_audio=voc_audio,
                pass1_audio=pass1_audio,
                out_json=out_json,
                source_lang=source_lang,
                whisper_lang=whisper_lang,
            )
        finally:
            del model
            try:
                import gc
                gc.collect()
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass


def _resolve_unit_video_and_vocals(ws: str, cli_video: str = "") -> tuple[str, str]:
    video_path = cli_video
    if not video_path:
        cand = os.path.join(ws, "source", "master_source.mp4")
        if os.path.exists(cand):
            video_path = cand
        else:
            prof_p = os.path.join(ws, "data", "00_video_profile.json")
            if os.path.exists(prof_p):
                with open(prof_p, "r", encoding="utf-8") as f:
                    src_rel = json.load(f).get("source_video", "source/master_source.mp4")
                video_path = src_rel if os.path.isabs(src_rel) else os.path.join(ws, src_rel)
    vocals_path = os.path.join(ws, "media", "stems", "vocals.wav")
    return video_path, vocals_path


def _sync_build_state_stage1_key(ws: str, asr_key: str) -> None:
    bs_p = os.path.join(ws, ".cache", "build_state.json")
    if not os.path.exists(bs_p):
        return
    try:
        with open(bs_p, "r", encoding="utf-8") as f:
            bs = json.load(f)
        bs["stage1_key"] = asr_key
        _atomic_write_json(bs_p, bs)
    except Exception:
        pass


def _resolve_asr_workers(todo_len: int, cli_workers: int = 0) -> int:
    if todo_len <= 1:
        return 1
    req = int(cli_workers or os.environ.get("AICN_ASR_WORKERS", "0") or 0)
    budget_mb = ap.gpu_vram_budget_mb() if (ap and hasattr(ap, "gpu_vram_budget_mb")) else 4096
    max_vram_streams = max(1, budget_mb // 2560)
    if req > 0:
        return max(1, min(todo_len, req, max_vram_streams))
    # Auto: 1 warm stream on <=8 GB GPUs (leaves room for concurrent Demucs/VieNeu), scale up on >=9 GB GPUs
    auto_streams = max(1, budget_mb // 3584)
    return max(1, min(todo_len, auto_streams))


def _run_warm_asr_shard(shard: list[dict], model_size: str, shard_label: str = "WHISPER") -> None:
    if not shard:
        return
    bk_eng = ap.detect_asr_backend(sys.executable)["engine"] if ap else "mlx"
    with _whisper_gpu_ctx(bk_eng):
        model = load_whisper(model_size)
        try:
            with ThreadPoolExecutor(max_workers=1) as decode_ex:
                fut = decode_ex.submit(_decode_unit_audio, shard[0]["video_path"], shard[0]["vocals_path"])
                for idx, job in enumerate(shard):
                    raw_audio, voc_audio, pass1_audio = fut.result()
                    if idx + 1 < len(shard):
                        nxt = shard[idx + 1]
                        fut = decode_ex.submit(_decode_unit_audio, nxt["video_path"], nxt["vocals_path"])
                    t_u = time.time()
                    w_lang = None if job["source_lang"] == "auto" else job["source_lang"]
                    print(f"[{shard_label}] [{idx+1}/{len(shard)}] {os.path.basename(job['ws'])} (lang={job['source_lang']})", flush=True)
                    segs_saved, p2b_saved = _run_multipass_with_loaded_model(
                        t0=t_u,
                        model=model,
                        raw_audio=raw_audio,
                        voc_audio=voc_audio,
                        pass1_audio=pass1_audio,
                        out_json=job["asr_json"],
                        source_lang=job["source_lang"],
                        whisper_lang=w_lang,
                    )
                    _atomic_write_json(job["asr_cache_p"], {
                        "key": job["asr_key"],
                        "version": ASR_PIPELINE_VERSION,
                        "model": model_size,
                        "source_lang": job["source_lang"],
                    })
                    _sync_build_state_stage1_key(job["ws"], job["asr_key"])
                    try:
                        _atomic_write_json(job["global_asr_p"], {
                            "key": job["asr_key"],
                            "version": ASR_PIPELINE_VERSION,
                            "model": model_size,
                            "segments": segs_saved,
                            "pass2b_report": p2b_saved,
                        })
                    except Exception:
                        pass
        finally:
            del model
            try:
                import gc
                gc.collect()
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass


def run_units_asr_batch(
    unit_specs: list[tuple[str, str]],
    cli_source_lang: str | None = None,
    cli_model: str | None = None,
    skip_asr: bool = False,
    skip_waveform: bool = False,
    force: bool = False,
    workers: int = 0,
) -> None:
    """
    Process 1..N units cleanly:
      1. Enforce Stage 0 gate & resolve pure content SHA-256 ASR cache keys.
      2. Restore cache hits instantaneously without touching GPU.
      3. Load Whisper ONCE per warm worker stream (scaled to VRAM capacity when --workers > 1)
         while pre-decoding unit k+1 audio in a background CPU thread.
    """
    model_size = cli_model or os.environ.get("AICN_WHISPER_MODEL", "large-v3")
    todo = []
    all_workspaces = []

    for ws_raw, cli_vid in unit_specs:
        ws = os.path.abspath(ws_raw)
        require_gate(ws, 0)
        all_workspaces.append(ws)
        data_dir = os.path.join(ws, "data")
        asr_json = os.path.join(data_dir, "01_asr_raw_segments.json")
        p2b_json = os.path.join(data_dir, "01_pass2b_recovery_report.json")
        if skip_asr and os.path.exists(asr_json):
            continue

        video_path, vocals_path = _resolve_unit_video_and_vocals(ws, cli_vid)
        if not video_path or not os.path.exists(video_path):
            sys.exit(f"[ERROR] Source video not found for unit {ws}: {video_path}")
        source_lang = resolve_source_language(ws, cli_source_lang)
        vid_sha = _fast_file_sig(video_path)
        voc_sha = _fast_file_sig(vocals_path)
        asr_key = _stage1_asr_key(vid_sha, voc_sha, model_size, source_lang)

        asr_cache_p = os.path.join(ws, ".cache", "stage1_asr_cache.json")
        global_asr_p = _global_asr_cache_path(ws, asr_key)

        prev_key = None
        if os.path.exists(asr_cache_p):
            try:
                with open(asr_cache_p, "r", encoding="utf-8") as cf:
                    prev_key = json.load(cf).get("key")
            except Exception:
                prev_key = None

        if not force and prev_key == asr_key and os.path.exists(asr_json):
            print(f"[CACHE] {os.path.basename(ws)}: Stage 1 local ASR hit ({asr_key[:12]})", flush=True)
            _sync_build_state_stage1_key(ws, asr_key)
        elif not force and os.path.exists(global_asr_p):
            with open(global_asr_p, "r", encoding="utf-8") as gf:
                g_payload = json.load(gf)
            _atomic_write_json(asr_json, g_payload["segments"])
            if "pass2b_report" in g_payload:
                _atomic_write_json(p2b_json, g_payload["pass2b_report"])
            _atomic_write_json(asr_cache_p, {
                "key": asr_key, "version": ASR_PIPELINE_VERSION, "model": model_size, "source_lang": source_lang,
            })
            _sync_build_state_stage1_key(ws, asr_key)
            print(
                f"[CACHE] {os.path.basename(ws)}: Stage 1 global SHA-256 ASR hit ({asr_key[:12]}) -> "
                f"restored {len(g_payload['segments'])} segments in 0.01s!",
                flush=True,
            )
        else:
            todo.append({
                "ws": ws,
                "video_path": video_path,
                "vocals_path": vocals_path,
                "asr_json": asr_json,
                "p2b_json": p2b_json,
                "asr_cache_p": asr_cache_p,
                "global_asr_p": global_asr_p,
                "asr_key": asr_key,
                "source_lang": source_lang,
            })

    if todo:
        n_workers = _resolve_asr_workers(len(todo), cli_workers=workers)
        print(f"[WARM ASR] Loading Whisper ({model_size}) across {n_workers} worker stream(s) for {len(todo)} unit(s)...", flush=True)
        if n_workers <= 1:
            _run_warm_asr_shard(todo, model_size=model_size, shard_label="ASR")
        else:
            shards = [todo[i::n_workers] for i in range(n_workers)]
            with ThreadPoolExecutor(max_workers=n_workers) as wex:
                futs = [
                    wex.submit(_run_warm_asr_shard, sh, model_size, f"ASR-W{wi+1}")
                    for wi, sh in enumerate(shards) if sh
                ]
                for f in futs:
                    f.result()

    if not skip_waveform and all_workspaces:
        from analyze_voice_waveform import analyze_waveform_and_clauses
        if len(all_workspaces) == 1:
            analyze_waveform_and_clauses(all_workspaces[0])
        else:
            with ThreadPoolExecutor(max_workers=min(4, len(all_workspaces))) as ex:
                list(ex.map(analyze_waveform_and_clauses, all_workspaces))


def main():
    parser = argparse.ArgumentParser(description="Run Stage 1 Multi-Pass Whisper ASR and Acoustic Waveform Audit.")
    parser.add_argument("--video", required=False, default="", help="Source video path (optional when --workspace or --units is given).")
    parser.add_argument("--workspace", required=False, default="", help="Single unit workspace path (workspace/<slug>).")
    parser.add_argument("--units", required=False, default="", help="Comma-separated unit workspace paths for warm multi-unit batch ASR.")
    parser.add_argument("--workers", type=int, default=0, help="Parallel Whisper worker streams when VRAM allows (0=auto).")
    parser.add_argument("--source-lang", default=None, help="Source language code for Whisper (e.g. zh, en, auto).")
    parser.add_argument("--model", default=None, help="Whisper model size (default: AICN_WHISPER_MODEL or 'large-v3').")
    parser.add_argument("--skip-asr", action="store_true", help="Skip Whisper ASR if 01_asr_raw_segments.json already exists.")
    parser.add_argument("--skip-waveform", action="store_true", help="Skip trailing analyze_waveform_and_clauses call.")
    parser.add_argument("--force", action="store_true", help="Ignore Stage 1 content-hash cache and re-run ASR.")
    args = parser.parse_args()

    unit_specs = []
    if args.units.strip():
        for u in args.units.split(","):
            if u.strip():
                unit_specs.append((u.strip(), ""))
    elif args.workspace.strip():
        unit_specs.append((args.workspace.strip(), args.video.strip()))
    else:
        parser.error("Must provide --workspace or --units")

    run_units_asr_batch(
        unit_specs=unit_specs,
        cli_source_lang=args.source_lang,
        cli_model=args.model,
        skip_asr=args.skip_asr,
        skip_waveform=args.skip_waveform,
        force=args.force,
        workers=args.workers,
    )


if __name__ == "__main__":
    main()
