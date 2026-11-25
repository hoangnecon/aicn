#!/usr/bin/env python3
"""
Stage 1A: Separate Demucs stems (optional if cached or sub_only mode), run Stroke-Validated
Center-Clustered 5fps Hardsub BBox + Glyph-IoU Clause Transition Scan, and run Multi-Pass
Acoustic Waveform + Hardsub-Guided Whisper ASR.
Enforces Stage 0 CI/CD gate before running.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import numpy as np
from scipy.ndimage import minimum_filter

_HERE = os.path.dirname(os.path.abspath(__file__))
ORCH_SCRIPTS = os.path.abspath(os.path.join(_HERE, "..", "..", "video-localization-orchestrator", "scripts"))
for _p in (
    ORCH_SCRIPTS,
    os.path.abspath(os.path.join(_HERE, "..", "..", "..", "scripts")),
    os.path.abspath(os.path.join(_HERE, "..", "..", "aicn", "scripts")),
):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
from cicd_gate import require_gate
try:
    import aicn_platform as ap
except ImportError:
    ap = None


def find_hardsub_intervals(
    spans: list,
    fps_scan: int = 5,
    max_sub_height: int = 62,
    min_dur_s: float = 0.40,
    clause_changes: list = None,
    width: int = 1920,
    strip_h: int = 200,
    height: int = 1080,
) -> list:
    """
    Identify fine-grained visual subtitle sub-clauses where a true subtitle bounding box is visible.
    Splits contiguous subtitle runs into separate sub-clauses whenever:
      1) Bounding box left/right edges jump by > dx_tol px, OR
      2) Pixel-level glyph mask IoU drops below 0.48 (recorded in clause_changes[j]), catching
         back-to-back subtitle lines of identical character count/width.
    Adapts center (`width // 2`) and vertical thresholds for 16:9 and 9:16 portrait (720w & 1080w).
    Bridges 1-frame (0.2s) flicker gaps when the bounding box is unchanged.
    """
    n = len(spans)
    xc = width // 2
    is_portrait = (height > width) or (width < 1000)
    h_min = 12 if width < 1000 else 16
    h_max = max(max_sub_height, int(round(115 * width / 1080)) if is_portrait else max_sub_height)
    y0_min = 12 if is_portrait else 24
    y0_max = max(112, strip_h - 45) if is_portrait else max(68, strip_h - 50)
    y1_max = (strip_h - 8) if is_portrait else min(strip_h, 98)
    w_min = 16 if is_portrait else 28
    w_max = int(round(width * 0.90)) if is_portrait else 1200
    cx_tol = max(140, int(round(width * 220 / 1920))) if is_portrait else 220
    span_tol = max(45, int(round(width * 80 / 1920))) if is_portrait else 80
    dx_tol = 16 if width < 1000 else 22
    active = [False] * n
    for i, sp in enumerate(spans):
        if sp is not None:
            x0, x1, y0, y1 = int(sp[0]), int(sp[1]), int(sp[2]), int(sp[3])
            h = y1 - y0
            w = x1 - x0
            cx = (x0 + x1) // 2
            if h_min <= h <= h_max and y0_min <= y0 <= y0_max and y1 <= y1_max and w_min <= w <= w_max and abs(cx - xc) <= cx_tol and (x0 <= xc + span_tol and x1 >= xc - span_tol):
                active[i] = True

    # Bridge 1-frame flicker gaps only if bounding boxes before and after match closely
    for i in range(1, n - 1):
        if not active[i] and active[i - 1] and active[i + 1]:
            sp_prev, sp_next = spans[i - 1], spans[i + 1]
            if sp_prev is not None and sp_next is not None:
                if abs(sp_prev[0] - sp_next[0]) <= dx_tol and abs(sp_prev[1] - sp_next[1]) <= dx_tol:
                    active[i] = True
                    spans[i] = sp_prev

    intervals = []
    i = 0
    while i < n:
        if active[i]:
            j = i
            boxes = [spans[i]]
            prev_sp = spans[i]
            j += 1
            while j < n and active[j]:
                cur_sp = spans[j]
                is_change = False
                if clause_changes is not None and j < len(clause_changes) and clause_changes[j]:
                    is_change = True
                if cur_sp is not None and prev_sp is not None:
                    if abs(cur_sp[0] - prev_sp[0]) > 22 or abs(cur_sp[1] - prev_sp[1]) > 22:
                        is_change = True
                if is_change:
                    break
                if cur_sp is not None:
                    boxes.append(cur_sp)
                    prev_sp = cur_sp
                j += 1

            st = round(i / fps_scan, 2)
            et = round(j / fps_scan, 2)
            dur = round(et - st, 2)
            if dur >= min_dur_s and boxes:
                x0 = int(np.median([b[0] for b in boxes]))
                x1 = int(np.median([b[1] for b in boxes]))
                y0 = int(min(b[2] for b in boxes))
                y1 = int(max(b[3] for b in boxes))
                intervals.append({
                    "start": st,
                    "end": et,
                    "duration": dur,
                    "mid": round((st + et) / 2.0, 2),
                    "bbox": [x0, x1, y0, y1],
                })
            i = j
        else:
            i += 1
    return intervals


def scan_hardsub_and_scenes(
    video_path: str,
    out_json: str,
    strip_y0: int = 880,
    strip_h: int = 200,
    width: int = 1920,
    fps_scan: int = 5,
    color_mode: str = "auto",
    height: int = 1080,
):
    """
    Stroke-Validated Center-Clustered Hardsub & Glyph-IoU Clause Transition Scanner:
      - Restricts vertical subtitle detection to the calibrated subtitle band y in [32..86] inside the 200px strip
        so white tables, cats, or license plates below y=86 never corrupt or reject the subtitle bbox.
      - Validates white/yellow glyph pixels by requiring a dark stroke/outline neighbor (minimum luma < 118 in a 7x7 window),
        recovering 100% of subtitles over bright backgrounds while rejecting solid bright walls/flashes.
      - Clusters active columns horizontally around screen center (xc=960, bridging <=55px inter-character gaps),
        ignoring isolated side reflections (e.g. gun barrels or wall lights at x > 1350).
      - Computes frame-to-frame glyph mask IoU (`glyph_iou`) to detect clause transitions even when two consecutive
        subtitle lines have identical bounding-box widths.
    """
    t0 = time.time()
    print(
        f"[PLAN] Stage 1 Stroke-Validated Scan: y=[{strip_y0}..{strip_y0+strip_h}], width={width}, fps={fps_scan}, color_mode={color_mode} -> {out_json}",
        flush=True,
    )
    cmd = [
        ap.find_tool("ffmpeg") if ap else "ffmpeg", "-i", video_path,
        "-vf", f"fps={fps_scan},crop={width}:{strip_h}:0:{strip_y0}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    frame_bytes = width * strip_h * 3
    spans = []
    clause_changes = []
    scene_cuts = []
    widest_frames = []

    is_portrait = (height > width) or (width < 1000)
    x_left = int(round(width * (0.06 if is_portrait else 0.135)))
    x_right = int(round(width * (0.94 if is_portrait else 0.865)))
    y_top = 14 if is_portrait else 32
    y_bot = min(strip_h - 6, max(128, strip_h - 14)) if is_portrait else 86
    center_col = (width // 2) - x_left
    gap_px = max(24, int(round(width * 36 / 1080))) if is_portrait else 55
    center_tol = max(75, int(round(width * 110 / 1920))) if is_portrait else 110
    min_px_cnt = 18 if width < 1000 else 35
    min_h = 12 if width < 1000 else 16
    min_w = 16 if width < 1000 else 28

    prev_gray = None
    prev_sub_mask = None
    f_idx = 0
    last_log = time.time()

    while True:
        buf = proc.stdout.read(frame_bytes)
        if len(buf) < frame_bytes:
            break
        arr = np.frombuffer(buf, dtype=np.uint8).reshape((strip_h, width, 3))

        # Scene cut detection via subsampled int16 luminance delta (~10x cheaper than full float64 mean)
        gray = arr[::4, ::4].astype(np.int16).sum(axis=2)
        if prev_gray is not None:
            diff = float(np.mean(np.abs(gray - prev_gray))) / 3.0
            if diff > 28.0:
                scene_cuts.append(round(f_idx / fps_scan, 2))
        prev_gray = gray

        sub_band = arr[y_top:y_bot, x_left:x_right, :]
        r8 = sub_band[:, :, 0]
        g8 = sub_band[:, :, 1]
        b8 = sub_band[:, :, 2]

        max_c = np.maximum(np.maximum(r8, g8), b8)
        min_c = np.minimum(np.minimum(r8, g8), b8)
        white_mask = (r8 > 230) & (g8 > 230) & (b8 > 230) & ((max_c - min_c) < 28)
        if color_mode == "yellow":
            color_mask = (r8 > 220) & (g8 > 190) & (b8 < 90)
        elif color_mode == "auto":
            yellow_mask = (r8 > 225) & (g8 > 195) & (b8 < 80)
            color_mask = white_mask | yellow_mask
        else:
            color_mask = white_mask

        # Fast-skip frames with zero white/yellow candidate pixels (avoids ~30% of 7x7 minimum_filter calls)
        if not color_mask.any():
            spans.append(None)
            clause_changes.append(False)
            prev_sub_mask = None
            f_idx += 1
            if time.time() - last_log >= 10.0:
                print(f"[PROGRESS] Scanned {f_idx} frames ({f_idx/fps_scan:.1f}s) in {time.time()-t0:.1f}s", flush=True)
                last_log = time.time()
            continue

        luma = (0.299 * r8 + 0.587 * g8 + 0.114 * b8).astype(np.float32)

        # Dark stroke outline check: within a 7x7 window around each glyph pixel, min luma must be < 118
        min_luma = minimum_filter(luma, size=(7, 7))
        stroke_mask = color_mask & (min_luma < 118)

        # Horizontal column clustering around screen center
        col_cnt = stroke_mask.sum(axis=0)
        active_cols = np.where(col_cnt >= 2)[0]
        clusters = []
        for c in active_cols:
            if clusters and (c - clusters[-1][1]) <= gap_px:
                clusters[-1][1] = int(c)
                clusters[-1][2] += int(col_cnt[c])
            else:
                clusters.append([int(c), int(c), int(col_cnt[c])])

        # Keep cluster that crosses near screen center
        valid_clusters = [
            cl for cl in clusters
            if cl[0] <= (center_col + center_tol) and cl[1] >= (center_col - center_tol) and cl[2] >= min_px_cnt
        ]

        if valid_clusters:
            best = max(valid_clusters, key=lambda x: x[2])
            c0, c1 = best[0], best[1]
            clean_mask = np.zeros_like(stroke_mask)
            clean_mask[:, c0:c1 + 1] = stroke_mask[:, c0:c1 + 1]
            ys = np.where(clean_mask.any(axis=1))[0]
            x0 = int(c0 + x_left)
            x1 = int(c1 + x_left)
            y0 = int(ys[0] + y_top)
            y1 = int(ys[-1] + y_top)

            if (y1 - y0) >= min_h and (x1 - x0) >= min_w:
                spans.append([x0, x1, y0, y1])
                widest_frames.append((x1 - x0, round(f_idx / fps_scan, 2)))
                if prev_sub_mask is not None:
                    inter = int((clean_mask & prev_sub_mask).sum())
                    union = int((clean_mask | prev_sub_mask).sum())
                    iou = inter / max(1, union)
                    clause_changes.append(bool(iou < 0.48))
                else:
                    clause_changes.append(True)
                prev_sub_mask = clean_mask
            else:
                spans.append(None)
                clause_changes.append(False)
                prev_sub_mask = None
        else:
            spans.append(None)
            clause_changes.append(False)
            prev_sub_mask = None

        f_idx += 1
        if time.time() - last_log >= 10.0:
            print(f"[PROGRESS] Scanned {f_idx} frames ({f_idx/fps_scan:.1f}s) in {time.time()-t0:.1f}s", flush=True)
            last_log = time.time()

    proc.wait()

    # Select top 8 widest subtitle timestamps spaced at least 15s apart
    widest_frames.sort(reverse=True)
    top_wide_ts = []
    for w_px, ts in widest_frames:
        if all(abs(ts - existing) >= 15.0 for existing in top_wide_ts):
            top_wide_ts.append(ts)
        if len(top_wide_ts) >= 8:
            break

    hardsub_intervals = find_hardsub_intervals(
        spans,
        fps_scan=fps_scan,
        clause_changes=clause_changes,
        width=width,
        strip_h=strip_h,
        height=height,
    )

    payload = {
        "strip_y0": strip_y0,
        "strip_h": strip_h,
        "width": width,
        "height": height,
        "fps_scan": fps_scan,
        "num_frames": len(spans),
        "duration_scanned": round(len(spans) / fps_scan, 2),
        "scene_cuts_count": len(scene_cuts),
        "scene_cuts_sample": scene_cuts[:50],
        "top_wide_timestamps": sorted(top_wide_ts),
        "hardsub_intervals_count": len(hardsub_intervals),
        "hardsub_intervals": hardsub_intervals,
        "clause_changes": clause_changes,
        "spans": spans,
    }
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    print(
        f"[DONE] Scanned {len(spans)} frames ({len(spans)/fps_scan:.1f}s), {len(hardsub_intervals)} fine-grained visual sub-clauses, "
        f"{len(scene_cuts)} visual cuts -> {out_json} in {time.time()-t0:.2f}s",
        flush=True,
    )


def decode_audio_16k(input_path: str, af_filter: str = "") -> np.ndarray:
    cmd = [ap.find_tool("ffmpeg") if ap else "ffmpeg", "-y", "-i", input_path]
    if af_filter:
        cmd.extend(["-af", af_filter])
    cmd.extend(["-f", "s16le", "-ar", "16000", "-ac", "1", "pipe:1"])
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True)
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0



ASR_PIPELINE_VERSION = "asr-3.0"
HALLUCINATION_SUBSTRINGS = (
    "索兰娅", "谢谢观看", "感谢观看", "感谢收看", "请不吝点赞", "点赞订阅",
    "字幕由", "优优独播剧场", "Amara.org", "Thank you for watching", "Thanks for watching",
    "Please subscribe", "Subtitles by", "以下是普通话的句子", "使用简体中文",
)


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
    """faster-whisper compatible facade over mlx-whisper (Metal GPU, ~4x faster on Apple Silicon).
    VAD options are ignored: mlx-whisper drops silence through no_speech_threshold."""
    REPO = {"small": "mlx-community/whisper-small-mlx", "base": "mlx-community/whisper-base-mlx",
            "medium": "mlx-community/whisper-medium-mlx", "large-v3": "mlx-community/whisper-large-v3-mlx"}

    def __init__(self, size):
        self.repo = self.REPO.get(size, size)

    def transcribe(self, audio, language="zh", word_timestamps=False, beam_size=None, vad_filter=None,
                   vad_parameters=None, condition_on_previous_text=False, temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
                   initial_prompt=None):
        import mlx_whisper
        prompt = initial_prompt if initial_prompt is not None else ("以下是普通话的句子，使用简体中文。" if language == "zh" else None)
        r = mlx_whisper.transcribe(np.asarray(audio, np.float32), path_or_hf_repo=self.repo, language=language,
                                   word_timestamps=word_timestamps, condition_on_previous_text=condition_on_previous_text,
                                   temperature=temperature, verbose=None,
                                   initial_prompt=prompt)
        # No VAD in mlx-whisper: drop what Whisper itself marks as silence or a repetition loop
        keep = [d for d in r["segments"]
                if not (d.get("no_speech_prob", 0) > 0.6 and d.get("avg_logprob", 0) < -1.0)
                and d.get("compression_ratio", 0) <= 2.4]
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
            import torch  # noqa: F401  # ensure PyTorch CUDA/cuBLAS DLLs are mapped on Windows
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
    """Resolve source audio language from CLI flag, project_config.json, or series_bible.json."""
    if cli_lang:
        return cli_lang
    cfg_p = os.path.join(workspace, "config", "project_config.json")
    if os.path.exists(cfg_p):
        with open(cfg_p, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if cfg.get("source_language"):
            return str(cfg["source_language"])
        hub = cfg.get("series_hub_dir") or ""
        bible_p = os.path.join(hub, "config", "series_bible.json") if hub else ""
        if bible_p and os.path.exists(bible_p):
            with open(bible_p, "r", encoding="utf-8") as bf:
                bible = json.load(bf)
            if bible.get("source_language"):
                return str(bible["source_language"])
    return "zh"


def run_multipass_whisper_asr(
    video_path: str,
    vocals_path: str,
    bbox_json: str,
    out_json: str,
    model_size: str = os.environ.get("AICN_WHISPER_MODEL", "large-v3"),
    source_lang: str = "zh",
):
    """
    Multi-Pass Acoustic & Visual Sub-Clause Guided ASR:
      - Pass 1: Transcribe full-mix/vocals blend with sensitive VAD (threshold=0.28).
      - Pass 2 (Fine-Grained Sub-Clause & Acoustic Waveform Recovery):
        Cross-references every fine-grained visual sub-clause from 01_zh_bbox_scan.json AND
        acoustic voice bursts against Pass 1 ASR segments. Any sub-clause with <65% coverage
        or swallowed inside a multi-clause segment is individually sliced and transcribed
        with bandpass-boosted audio (vad_filter=False).
    """
    t0 = time.time()
    whisper_lang = None if source_lang == "auto" else source_lang
    print(f"[PLAN] Running Multi-Pass whisper ({model_size}, lang={source_lang}) -> {out_json}", flush=True)

    raw_audio = decode_audio_16k(video_path)
    if os.path.exists(vocals_path):
        voc_audio = decode_audio_16k(vocals_path)
        min_len = min(len(raw_audio), len(voc_audio))
        pass1_audio = np.zeros_like(raw_audio)
        pass1_audio[:min_len] = 0.70 * voc_audio[:min_len] + 0.30 * raw_audio[:min_len]
    else:
        voc_audio = raw_audio
        pass1_audio = raw_audio

    model = load_whisper(model_size)
    prompt = _initial_prompt_for(source_lang)
    vad_params = dict(threshold=0.20, min_speech_duration_ms=80, min_silence_duration_ms=200, speech_pad_ms=200)
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
    for idx, seg in enumerate(segments_gen):
        txt = seg.text.strip()
        if not txt or _is_hallucination(txt):
            continue
        avg_prob = round(float(getattr(seg, "avg_logprob", -0.2)), 3)
        no_sp = float(getattr(seg, "no_speech_prob", 0.0))
        comp_r = float(getattr(seg, "compression_ratio", 1.0))
        if (no_sp > 0.75 and avg_prob < -1.0) or comp_r > 2.4:
            continue
        words = list(getattr(seg, "words", None) or [])
        if words:
            chunks = [[words[0]]]
            for w in words[1:]:
                if (float(w.start) - float(chunks[-1][-1].end)) >= 1.0:
                    chunks.append([w])
                else:
                    chunks[-1].append(w)
            for ch in chunks:
                ch_txt = "".join(w.word for w in ch).strip()
                if not ch_txt or _is_hallucination(ch_txt):
                    continue
                st_w = round(max(float(seg.start), float(ch[0].start)), 3)
                et_w = round(min(float(seg.end), max(st_w + 0.15, float(ch[-1].end))), 3)
                results.append({
                    "id": len(results),
                    "start": st_w,
                    "end": et_w,
                    "text": ch_txt,
                    "avg_logprob": avg_prob,
                    "source": "pass1_asr",
                })
        else:
            results.append({
                "id": len(results),
                "start": round(float(seg.start), 3),
                "end": round(float(seg.end), 3),
                "text": txt,
                "avg_logprob": avg_prob,
                "source": "pass1_asr",
            })
        if time.time() - last_log >= 15.0:
            print(f"[PROGRESS] Pass 1 transcribed {len(results)} segments (up to {seg.end:.1f}s) in {time.time()-t0:.1f}s", flush=True)
            last_log = time.time()

    print(f"[PASS 1 DONE] {len(results)} segments from primary ASR in {time.time()-t0:.1f}s", flush=True)

    # Pass 2: Fine-Grained Visual Sub-Clause & Bandpass Voice Recovery
    if os.path.exists(bbox_json):
        with open(bbox_json, "r", encoding="utf-8") as f:
            bdata = json.load(f)
        intervals = bdata.get("hardsub_intervals")
        if intervals is None:
            intervals = find_hardsub_intervals(
                bdata.get("spans", []),
                int(bdata.get("fps_scan", 5)),
                clause_changes=bdata.get("clause_changes"),
                width=int(bdata.get("width", 1920)),
                strip_h=int(bdata.get("strip_h", 200)),
            )

        uncovered = []
        for iv in intervals:
            ist, iet = float(iv["start"]), float(iv["end"])
            idur = max(0.2, iet - ist)
            ov = sum(max(0.0, min(iet, s["end"]) - max(ist, s["start"])) for s in results)
            if (ov / idur) < 0.60:
                uncovered.append(iv)

        if uncovered:
            print(
                f"[PASS 2 SUB-CLAUSE RECOVERY] Found {len(uncovered)} fine-grained visual sub-clauses with <60% coverage. "
                f"Running dual-channel bandpass + vocal slice recovery...",
                flush=True,
            )
            boosted_audio = decode_audio_16k(
                video_path,
                af_filter="highpass=f=250,lowpass=f=3800,dynaudnorm=f=75:g=15:p=0.92:m=12.0",
            )
            sr = 16000
            recovered_audio_cnt = 0
            visual_ocr_cnt = 0

            for iv in uncovered:
                ist, iet = float(iv["start"]), float(iv["end"])
                pad_s = max(0.0, ist - 0.25)
                pad_e = min(len(boosted_audio) / sr, iet + 0.25)
                s_idx = int(round(pad_s * sr))
                e_idx = int(round(pad_e * sr))
                clip = boosted_audio[s_idx:e_idx]

                recovered_text = ""
                recovered_prob = -0.99
                if len(clip) >= int(0.3 * sr):
                    mx = float(np.max(np.abs(clip)))
                    if mx > 0.005:
                        clip = clip / mx * 0.85
                    sub_gen, _ = model.transcribe(
                        clip,
                        language=whisper_lang,
                        beam_size=5,
                        vad_filter=False,
                        condition_on_previous_text=False,
                        temperature=0.0,
                    )
                    sub_list = list(sub_gen)
                    texts = [sg.text.strip() for sg in sub_list if sg.text.strip() and "索兰娅" not in sg.text]
                    if texts:
                        recovered_text = " ".join(texts)
                        recovered_prob = round(float(getattr(sub_list[0], "avg_logprob", -0.6)), 3)

                if recovered_text and recovered_prob > -1.25:
                    recovered_audio_cnt += 1
                    results.append({
                        "id": -1,
                        "start": round(ist, 3),
                        "end": round(iet, 3),
                        "text": recovered_text,
                        "avg_logprob": recovered_prob,
                        "source": "pass2_bandpass_recovery",
                        "needs_visual_ocr": True,
                        "mid_timestamp": iv["mid"],
                    })
                else:
                    visual_ocr_cnt += 1
                    results.append({
                        "id": -1,
                        "start": round(ist, 3),
                        "end": round(iet, 3),
                        "text": f"[GAP_VISUAL_SUB @ {iv['mid']}s]",
                        "avg_logprob": -1.5,
                        "source": "pass2_visual_hardsub_gap",
                        "needs_visual_ocr": True,
                        "mid_timestamp": iv["mid"],
                    })

            results.sort(key=lambda x: (x["start"], x["end"]))
            for idx, item in enumerate(results):
                item["id"] = idx

            print(
                f"[PASS 2 DONE] Recovered {recovered_audio_cnt} sub-clauses via bandpass audio + flagged "
                f"{visual_ocr_cnt} visual-only sub-clause gaps for mandatory keyframe OCR.",
                flush=True,
            )
    # Pass 2B: Gap-First Multi-Band Frequency ASR (Full / High / Mid / Low) + Reverse Cross-Check & Refine
    p2b_report_path = os.path.join(os.path.dirname(os.path.abspath(out_json)), "01_pass2b_recovery_report.json")
    results, _ = run_multiband_pass2b_recovery(
        model=model,
        results=results,
        voc_audio=voc_audio,
        raw_audio=raw_audio,
        whisper_lang=whisper_lang,
        source_lang=source_lang,
        sr=16000,
        report_path=p2b_report_path,
    )

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"[DONE] Saved {len(results)} total fine-grained ASR segments to {out_json} in {time.time()-t0:.2f}s", flush=True)


def _text_char_overlap(a: str, b: str) -> float:
    """Character-level Dice similarity (robust for Chinese clauses without spaces and short English phrases)."""
    sa = [c for c in a.lower() if ("\u4e00" <= c <= "\u9fff") or c.isalnum()]
    sb = [c for c in b.lower() if ("\u4e00" <= c <= "\u9fff") or c.isalnum()]
    if not sa or not sb:
        return 0.0
    set_a, set_b = set(sa), set(sb)
    return 2.0 * len(set_a & set_b) / (len(set_a) + len(set_b))


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
) -> list:
    """Run vad_filter=False Whisper on one frequency-band slice and return scored candidate segments."""
    s_idx = max(0, int(round(pad_s * sr)))
    e_idx = min(len(wave), int(round(pad_e * sr)))
    clip = wave[s_idx:e_idx]
    if len(clip) < int(0.30 * sr):
        return []
    mx = float(np.max(np.abs(clip)))
    if mx <= 0.002:
        return []
    clip = (clip / mx * 0.85).astype(np.float32)

    prompt = _initial_prompt_for(source_lang)
    kwargs = dict(
        language=whisper_lang,
        beam_size=3 if band_name in ("full_vocal", "raw_clean") else 2,
        word_timestamps=False,
        vad_filter=False,
        condition_on_previous_text=False,
        temperature=0.0,
    )
    if prompt:
        kwargs["initial_prompt"] = prompt
    try:
        sub_gen, _ = model.transcribe(clip, **kwargs)
    except TypeError:
        kwargs.pop("initial_prompt", None)
        sub_gen, _ = model.transcribe(clip, **kwargs)

    cands = []
    for sg in list(sub_gen):
        rec_text = sg.text.strip()
        if not rec_text or _is_hallucination(rec_text):
            continue
        rec_prob = round(float(getattr(sg, "avg_logprob", -0.6)), 3)
        no_sp = float(getattr(sg, "no_speech_prob", 0.0))
        comp_r = float(getattr(sg, "compression_ratio", 1.0))
        n_cjk = len([c for c in rec_text if "\u4e00" <= c <= "\u9fff"])
        if source_lang == "zh":
            # Allow crisp 1-char exclamations (e.g. "嗯", "啊", "走", "停") only when high-confidence & loud vocal
            has_words = (n_cjk >= 2) or (n_cjk == 1 and rec_prob > -0.45 and no_sp < 0.25 and peak_voc_rms >= 0.035)
        else:
            has_words = len(rec_text.strip()) >= 3
        if not (rec_prob > -0.95 and no_sp < 0.65 and comp_r <= 2.2 and has_words):
            continue

        seg_st = round(max(gst, pad_s + float(getattr(sg, "start", 0.0))), 3)
        seg_et = round(min(get, pad_s + float(getattr(sg, "end", get - pad_s))), 3)
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
    """Trim dead leading/trailing silence inside a recovered segment using 25ms RMS hops on the winning band."""
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
) -> list:
    """
    Reverse Cross-Check & Cross-Band Candidate Fusion:
      1. Clusters temporally overlapping ASR candidates across full_vocal / high / mid / low bands.
      2. Multi-band consensus (>=2 independent frequency bands agreeing) gets a strong confidence bonus (+0.18).
      3. Single-band-only detections undergo reverse verification:
         - Acoustic modulation/energy check on the winning band inside [start, end], AND
         - When vocals stem was attenuated (<0.022 RMS — e.g. Demucs stripped child/cartoon/phone voice into accomp),
           runs a reverse cross-check probe on `raw_clean` (100-7500 Hz unfiltered raw) to confirm or refine the text.
         - Unconfirmed single-band detections require tighter confidence (`avg_logprob > -0.78`, `no_speech_prob < 0.50`).
      4. Refines winning candidate's [start, end] to active band energy onset/offset.
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

        # Reverse cross-check against raw_clean when only 1 band fired and vocals stem is weak (Demucs attenuation)
        if len(bands_present) == 1 and model is not None and raw_clean_wave is not None:
            s0 = max(0, int(round(cl_st * sr)))
            s1 = min(len(voc_raw), int(round(cl_et * sr))) if voc_raw is not None else 0
            local_voc_rms = float(np.sqrt(np.mean(voc_raw[s0:s1] ** 2) + 1e-12)) if s1 > s0 else 0.0
            if local_voc_rms < 0.022 and bands_present[0] != "raw_clean":
                r_pad_s = max(0.0, cl_st - 0.20)
                r_pad_e = min(total_dur, cl_et + 0.20)
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
                )
                for rc in rev_cands:
                    ov = max(0.0, min(cl_et, rc["end"]) - max(cl_st, rc["start"]))
                    if ov >= 0.15:
                        cl.append(rc)
                bands_present = sorted({x["band"] for x in cl})

        # Score each candidate with cross-band consensus + text agreement
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
            # Slight preference for full_vocal/raw_clean on exact tie (broader natural bandwidth)
            bw_bonus = 0.03 if cand["band"] in ("full_vocal", "raw_clean") else 0.0
            total_score = cand["base_score"] + consensus_bonus + bw_bonus
            if total_score > best_score:
                best_score = total_score
                best_item = (cand, sorted(agreed_bands), round(total_score, 4))

        if best_item is None:
            continue
        win, agreed_bands, win_score = best_item

        # Reject unconfirmed single-band low-confidence detections (anti-hallucination guard)
        if len(agreed_bands) == 1:
            if not (win["avg_logprob"] > -0.78 and win["no_speech_prob"] < 0.50):
                continue
            # Acoustic energy confirmation on winning band raw waveform
            b_raw = streams.get(f"{win['band']}_raw")
            if b_raw is not None and len(b_raw) > 0:
                s0 = max(0, int(round(win["start"] * sr)))
                s1 = min(len(b_raw), int(round(win["end"] * sr)))
                if s1 > s0:
                    seg_rms = float(np.sqrt(np.mean(b_raw[s0:s1] ** 2) + 1e-12))
                    if seg_rms < 0.003:
                        continue

        ref_st, ref_et = _refine_span_to_band_energy(
            win["start"], win["end"], streams.get(f"{win['band']}_raw"), sr=sr
        )
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
    Gap-First Multi-Band Frequency ASR Recovery (v2.5.0):
      1. Decomposes audio into Full-Vocal (120-7500 Hz), High (900-7500 Hz), Mid (300-3400 Hz),
         Low (85-1400 Hz), and Raw-Clean (100-7500 Hz) streams in RAM.
      2. Scans ONLY uncovered timeline gaps (<= 10s energy-valley-split windows, zero RMS dead zone).
      3. Probes Full-Vocal first; whenever uncovered or borderline confidence, sweeps active High/Mid/Low
         frequency bands, runs Reverse Cross-Check & Fusion, refines timestamps, and logs verified non-speech gaps.
    """
    from analyze_voice_waveform import build_multiband_audio_streams, detect_uncovered_multiband_gaps

    streams = build_multiband_audio_streams(voc_audio, raw_audio, sr=sr)
    uncovered_gaps = detect_uncovered_multiband_gaps(
        results, voc_audio, raw_audio, streams=streams, sr=sr, max_window_s=10.0, min_gap_s=0.35
    )

    recovered_by_band = {"full_vocal": 0, "high": 0, "mid": 0, "low": 0, "raw_clean": 0}
    verified_non_speech_gaps = []
    rec_cnt = 0
    total_dur = len(streams["raw_raw"]) / sr

    if uncovered_gaps:
        print(
            f"[PASS 2B MULTI-BAND RECOVERY] Probing {len(uncovered_gaps)} uncovered acoustic gaps "
            f"(Full / High 900-7500Hz / Mid 300-3400Hz / Low 85-1400Hz + Reverse Refine)...",
            flush=True,
        )
        p2b_t0 = time.time()
        last_p2b_log = p2b_t0
        for g_idx, g in enumerate(uncovered_gaps, 1):
            gst, get = float(g["start"]), float(g["end"])
            gdur = max(0.2, get - gst)
            pad_s = max(0.0, gst - 0.25)
            pad_e = min(total_dur, get + 0.25)
            p_voc = float(g.get("peak_voc_rms", 0.0))

            # Step 2B-1: Full-Vocal band probe first (catches clear speech dropped by Pass 1 Silero VAD)
            all_cands = _probe_band_clip(
                model=model,
                wave=streams["full_vocal"],
                pad_s=pad_s,
                pad_e=pad_e,
                gst=gst,
                get=get,
                band_name="full_vocal",
                whisper_lang=whisper_lang,
                source_lang=source_lang,
                peak_voc_rms=p_voc,
                sr=sr,
            )

            # Check if Full-Vocal already covered >=65% of the gap with high confidence
            hi_conf_cov = sum(
                max(0.0, c["end"] - c["start"])
                for c in all_cands
                if c["avg_logprob"] > -0.42 and c["no_speech_prob"] < 0.30
            )
            need_multiband = (hi_conf_cov / gdur) < 0.65

            # Step 2B-2: Multi-Band Spectral Sweep (High / Mid / Low in dominance order, early-stop on strong consensus)
            probed_extra = []
            if need_multiband:
                for bname in g.get("probe_bands", ["high", "mid", "low"]):
                    wave = streams.get(bname)
                    if wave is None:
                        continue
                    probed_extra.append(bname)
                    b_cands = _probe_band_clip(
                        model=model,
                        wave=wave,
                        pad_s=pad_s,
                        pad_e=pad_e,
                        gst=gst,
                        get=get,
                        band_name=bname,
                        whisper_lang=whisper_lang,
                        source_lang=source_lang,
                        peak_voc_rms=p_voc,
                        sr=sr,
                    )
                    all_cands.extend(b_cands)
                    # Early stop once >=2 distinct bands strongly cover >=65% of this gap
                    strong_bands = {c["band"] for c in all_cands if c["avg_logprob"] > -0.45 and c["no_speech_prob"] < 0.25}
                    if len(strong_bands) >= 2:
                        strong_cov = sum(
                            max(0.0, c["end"] - c["start"])
                            for c in all_cands
                            if c["band"] == bname and c["avg_logprob"] > -0.45 and c["no_speech_prob"] < 0.25
                        )
                        if (strong_cov / gdur) >= 0.65:
                            break

            # Step 2B-3: Reverse Cross-Check, Consensus Fusion & Energy-Onset Refine
            refined = fuse_and_refine_multiband_candidates(
                candidates=all_cands,
                streams=streams,
                gap_meta=g,
                model=model,
                whisper_lang=whisper_lang,
                source_lang=source_lang,
                sr=sr,
            )

            gap_added = 0
            for item in refined:
                seg_st, seg_et = float(item["start"]), float(item["end"])
                ov_existing = sum(
                    max(0.0, min(seg_et, float(r["end"])) - max(seg_st, float(r["start"])))
                    for r in results
                )
                if (ov_existing / max(0.2, seg_et - seg_st)) >= 0.35:
                    continue
                results.append(item)
                rec_cnt += 1
                gap_added += 1
                wb = item.get("winning_band", "full_vocal")
                recovered_by_band[wb] = recovered_by_band.get(wb, 0) + 1

            if gap_added == 0:
                verified_non_speech_gaps.append({
                    "start": gst,
                    "end": get,
                    "duration": round(gdur, 2),
                    "kind": g.get("kind", "normal"),
                    "probed_bands": ["full_vocal"] + probed_extra,
                })
            if time.time() - last_p2b_log >= 15.0:
                print(
                    f"[PROGRESS] Pass 2B probed {g_idx}/{len(uncovered_gaps)} gaps (recovered={rec_cnt}) "
                    f"in {time.time()-p2b_t0:.1f}s",
                    flush=True,
                )
                last_p2b_log = time.time()

        if rec_cnt:
            results.sort(key=lambda x: (x["start"], x["end"]))
            for idx, item in enumerate(results):
                item["id"] = idx

        print(
            f"[PASS 2B DONE] Recovered {rec_cnt} segments across {len(uncovered_gaps)} gaps "
            f"(by_band={recovered_by_band}, verified_non_speech={len(verified_non_speech_gaps)}).",
            flush=True,
        )

    report = {
        "pipeline_version": ASR_PIPELINE_VERSION,
        "gaps_probed": len(uncovered_gaps),
        "recovered_total": rec_cnt,
        "recovered_by_band": recovered_by_band,
        "verified_non_speech_gaps": verified_non_speech_gaps,
    }
    if report_path:
        os.makedirs(os.path.dirname(os.path.abspath(report_path)), exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as rf:
            json.dump(report, rf, ensure_ascii=False, indent=2)
    return results, report


def main():
    parser = argparse.ArgumentParser(description="Run Stage 1 Multi-Pass Whisper ASR and Acoustic Waveform Audit.")
    parser.add_argument("--video", required=True, help="Source video path.")
    parser.add_argument("--workspace", required=True, help="Workspace directory path (workspace/<slug>).")
    parser.add_argument("--color-mode", default="white", choices=["white", "yellow", "auto"], help="Optional bbox scan color mode (only used with --with-bbox-scan).")
    parser.add_argument("--source-lang", default=None, help="Source language code for Whisper (e.g. zh, en, auto). Defaults to project_config/series_bible or 'zh'.")
    parser.add_argument("--model", default=None, help="Whisper model size (default: AICN_WHISPER_MODEL or 'large-v3').")
    parser.add_argument("--with-bbox-scan", action="store_true", help="Opt-in to legacy 5fps pixel bbox scan (disabled by default in v2.0.0 dub+watermark pipeline).")
    parser.add_argument("--skip-scan", action="store_true", help="Deprecated no-op (bbox scan is already skipped by default).")
    parser.add_argument("--skip-asr", action="store_true", help="Skip Whisper ASR if 01_asr_raw_segments.json already exists.")
    parser.add_argument("--force", action="store_true", help="Ignore Stage 1 content-hash cache and re-run ASR.")
    args = parser.parse_args()

    require_gate(args.workspace, 0)

    profile_path = os.path.join(args.workspace, "data", "00_video_profile.json")
    with open(profile_path, "r", encoding="utf-8") as f:
        profile = json.load(f)

    data_dir = os.path.join(args.workspace, "data")
    bbox_json = os.path.join(data_dir, "01_zh_bbox_scan.json")
    asr_json = os.path.join(data_dir, "01_asr_raw_segments.json")

    if args.with_bbox_scan and not (args.skip_scan and os.path.exists(bbox_json)):
        scan_hardsub_and_scenes(
            video_path=args.video,
            out_json=bbox_json,
            strip_y0=int(profile.get("strip_y0", 880)),
            strip_h=int(profile.get("strip_h", 200)),
            width=int(profile.get("width", 1920)),
            fps_scan=5,
            color_mode=args.color_mode,
            height=int(profile.get("height", 1080)),
        )
    elif os.path.exists(bbox_json):
        print(f"[SKIP] Using existing bbox scan: {bbox_json}", flush=True)
    else:
        print("[SKIP] 5fps hardsub bbox scan skipped (v2.0.0 dub+watermark pipeline uses Whisper ASR + acoustic waveform).", flush=True)

    if not (args.skip_asr and os.path.exists(asr_json)):
        import hashlib as _hashlib
        from analyze_voice_waveform import _fast_file_sig
        stems_voc = os.path.join(args.workspace, "media", "stems", "vocals.wav")
        legacy_voc = os.path.join(args.workspace, "media", "demucs", "vocals.wav")
        vocals_path = stems_voc if os.path.exists(stems_voc) else legacy_voc
        source_lang = resolve_source_language(args.workspace, args.source_lang)
        model_size = args.model or os.environ.get("AICN_WHISPER_MODEL", "large-v3")
        asr_key = _hashlib.sha256(
            f"{ASR_PIPELINE_VERSION}|{model_size}|{_fast_file_sig(args.video)}|{_fast_file_sig(vocals_path)}|{_fast_file_sig(bbox_json)}|{source_lang}".encode()
        ).hexdigest()[:16]
        asr_cache_p = os.path.join(args.workspace, ".cache", "stage1_asr_cache.json")
        prev_key = None
        if os.path.exists(asr_cache_p):
            try:
                with open(asr_cache_p, "r", encoding="utf-8") as cf:
                    prev_key = json.load(cf).get("key")
            except Exception:
                prev_key = None
        if not args.force and prev_key == asr_key and os.path.exists(asr_json):
            print(f"[CACHE] Stage 1 ASR hit ({asr_key}) -> keeping {asr_json}", flush=True)
        else:
            run_multipass_whisper_asr(
                video_path=args.video,
                vocals_path=vocals_path,
                bbox_json=bbox_json,
                out_json=asr_json,
                model_size=model_size,
                source_lang=source_lang,
            )
            os.makedirs(os.path.dirname(asr_cache_p), exist_ok=True)
            with open(asr_cache_p, "w", encoding="utf-8") as cf:
                json.dump({"key": asr_key, "version": ASR_PIPELINE_VERSION, "model": model_size, "source_lang": source_lang}, cf, indent=2)

    # Always run Multi-Band Acoustic Waveform & Sub-Clause Alignment Audit
    from analyze_voice_waveform import analyze_waveform_and_clauses
    analyze_waveform_and_clauses(args.workspace)


if __name__ == "__main__":
    main()
