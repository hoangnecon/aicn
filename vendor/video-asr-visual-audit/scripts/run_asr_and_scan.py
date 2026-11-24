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



class _Seg:
    def __init__(self, d):
        self.text, self.start, self.end = d["text"], d["start"], d["end"]
        self.avg_logprob = d.get("avg_logprob", -0.2)


class _MlxWhisper:
    """faster-whisper compatible facade over mlx-whisper (Metal GPU, ~4x faster on Apple Silicon).
    VAD options are ignored: mlx-whisper drops silence through no_speech_threshold."""
    REPO = {"small": "mlx-community/whisper-small-mlx", "base": "mlx-community/whisper-base-mlx",
            "medium": "mlx-community/whisper-medium-mlx", "large-v3": "mlx-community/whisper-large-v3-mlx"}

    def __init__(self, size):
        self.repo = self.REPO.get(size, size)

    def transcribe(self, audio, language="zh", word_timestamps=False, beam_size=None, vad_filter=None,
                   vad_parameters=None, condition_on_previous_text=True, temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0)):
        import mlx_whisper
        r = mlx_whisper.transcribe(np.asarray(audio, np.float32), path_or_hf_repo=self.repo, language=language,
                                   word_timestamps=word_timestamps, condition_on_previous_text=condition_on_previous_text,
                                   temperature=temperature, verbose=None,
                                   initial_prompt="以下是普通话的句子，使用简体中文。" if language == "zh" else None)
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
        for ctype in ("float16", "int8_float16"):
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
    model_size: str = "small",
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
        pass1_audio[:min_len] = 0.90 * voc_audio[:min_len] + 0.10 * raw_audio[:min_len]
    else:
        voc_audio = raw_audio
        pass1_audio = raw_audio

    model = load_whisper(model_size)
    vad_params = dict(threshold=0.35, min_speech_duration_ms=100, min_silence_duration_ms=250, speech_pad_ms=150)
    segments_gen, _ = model.transcribe(
        pass1_audio,
        language=whisper_lang,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters=vad_params,
    )

    results = []
    last_log = time.time()
    for idx, seg in enumerate(segments_gen):
        txt = seg.text.strip()
        if not txt:
            continue
        avg_prob = round(float(getattr(seg, "avg_logprob", -0.2)), 3)
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
                if not ch_txt:
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
    elif os.path.exists(vocals_path):
        # Pass 2 (v2.0.0 acoustic burst recovery): catch loud vocal dialogue bursts missed by Pass 1
        from analyze_voice_waveform import compute_voice_mask_and_bursts
        tel_voc = decode_audio_16k(
            vocals_path,
            af_filter="highpass=f=300,lowpass=f=3400,dynaudnorm=f=50:g=15:p=0.92:m=15.0",
        )
        _, merged_bursts = compute_voice_mask_and_bursts(voc_audio, tel_voc)
        sr = 16000
        uncovered_bursts = []
        for b in merged_bursts:
            bst, bet = float(b["start"]), float(b["end"])
            bdur = bet - bst
            if bdur < 0.35 or float(b.get("peak_voc_rms", 0.0)) < 0.09:
                continue
            ov = sum(max(0.0, min(bet, s["end"]) - max(bst, s["start"])) for s in results)
            if (ov / max(0.2, bdur)) < 0.35:
                uncovered_bursts.append(b)
        if uncovered_bursts:
            print(
                f"[PASS 2 ACOUSTIC RECOVERY] Probing {len(uncovered_bursts)} uncovered loud vocal bursts...",
                flush=True,
            )
            rec_cnt = 0
            for b in uncovered_bursts:
                pad_s = max(0.0, float(b["start"]) - 0.20)
                pad_e = min(len(voc_audio) / sr, float(b["end"]) + 0.20)
                clip = voc_audio[int(round(pad_s * sr)):int(round(pad_e * sr))]
                if len(clip) < int(0.3 * sr):
                    continue
                mx = float(np.max(np.abs(clip)))
                if mx > 0.005:
                    clip = clip / mx * 0.85
                sub_gen, _ = model.transcribe(
                    clip,
                    language=whisper_lang,
                    beam_size=5,
                    word_timestamps=True,
                    vad_filter=False,
                    condition_on_previous_text=False,
                    temperature=0.0,
                )
                sub_list = list(sub_gen)
                texts = [sg.text.strip() for sg in sub_list if sg.text.strip() and "索兰娅" not in sg.text]
                if texts:
                    rec_text = " ".join(texts)
                    rec_prob = round(float(getattr(sub_list[0], "avg_logprob", -0.6)), 3)
                    n_cjk = len([c for c in rec_text if "\u4e00" <= c <= "\u9fff"])
                    if rec_prob > -0.85 and n_cjk >= 2:
                        rec_cnt += 1
                        results.append({
                            "id": -1,
                            "start": round(float(b["start"]), 3),
                            "end": round(float(b["end"]), 3),
                            "text": rec_text,
                            "avg_logprob": rec_prob,
                            "source": "pass2_acoustic_burst_recovery",
                        })
            if rec_cnt:
                results.sort(key=lambda x: (x["start"], x["end"]))
                for idx, item in enumerate(results):
                    item["id"] = idx
            print(f"[PASS 2 DONE] Recovered {rec_cnt} spoken bursts via Pass 2 acoustic recovery.", flush=True)

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"[DONE] Saved {len(results)} total fine-grained ASR segments to {out_json} in {time.time()-t0:.2f}s", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Run Stage 1 Multi-Pass Whisper ASR and Acoustic Waveform Audit.")
    parser.add_argument("--video", required=True, help="Source video path.")
    parser.add_argument("--workspace", required=True, help="Workspace directory path (workspace/<slug>).")
    parser.add_argument("--color-mode", default="white", choices=["white", "yellow", "auto"], help="Optional bbox scan color mode (only used with --with-bbox-scan).")
    parser.add_argument("--source-lang", default=None, help="Source language code for Whisper (e.g. zh, en, auto). Defaults to project_config/series_bible or 'zh'.")
    parser.add_argument("--with-bbox-scan", action="store_true", help="Opt-in to legacy 5fps pixel bbox scan (disabled by default in v2.0.0 dub+watermark pipeline).")
    parser.add_argument("--skip-scan", action="store_true", help="Deprecated no-op (bbox scan is already skipped by default).")
    parser.add_argument("--skip-asr", action="store_true", help="Skip Whisper ASR if 01_asr_raw_segments.json already exists.")
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
        stems_voc = os.path.join(args.workspace, "media", "stems", "vocals.wav")
        legacy_voc = os.path.join(args.workspace, "media", "demucs", "vocals.wav")
        vocals_path = stems_voc if os.path.exists(stems_voc) else legacy_voc
        source_lang = resolve_source_language(args.workspace, args.source_lang)
        run_multipass_whisper_asr(
            video_path=args.video,
            vocals_path=vocals_path,
            bbox_json=bbox_json,
            out_json=asr_json,
            source_lang=source_lang,
        )

    # Always run Multi-Band Acoustic Waveform & Sub-Clause Alignment Audit
    from analyze_voice_waveform import analyze_waveform_and_clauses
    analyze_waveform_and_clauses(args.workspace)


if __name__ == "__main__":
    main()
