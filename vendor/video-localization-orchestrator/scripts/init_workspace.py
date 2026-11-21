#!/usr/bin/env python3
"""
Stage 0: Initialize standardized workspace directory, project topology (single | series_compilation | series_episodic),
multi-episode ingestion/concatenation (`source/episodes/` -> `source/master_source.mp4` + `data/00_episodes_manifest.json`),
config & JSON schema templates (`config/project_config.json`, `config/series_bible.json`, `config/schemas/*.template.json`),
genre/mode-matched Markdown logs (`00..04.md`), deliverables vault (`deliverables/{16x9,9x16,subtitles,episodes}`),
and sign Stage 0 CI/CD gate token (`.cicd/stage_0_gate.json`).
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cicd_gate import write_gate_token, sha256_file


def probe_video(video_path: str) -> dict:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration,size,bit_rate",
        "-show_entries", "stream=index,codec_name,codec_type,profile,time_base,start_time,duration,width,height,r_frame_rate,avg_frame_rate,sample_rate,channels",
        "-of", "json", video_path
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(res.stdout)


def calibrate_strip_y0(video_path: str, width: int, height: int, duration: float) -> tuple[int, int, bool]:
    """Sample ~12 keyframes across the video to auto-calibrate the hardsub vertical band (strip_y0, strip_h)."""
    is_portrait = height > width
    default_y0 = int(round(height * (0.63 if is_portrait else 0.8148)))
    default_h = int(round(height * 0.22)) if is_portrait else (height - default_y0)
    if duration <= 1.0 or not os.path.exists(video_path):
        return default_y0, default_h, False
    try:
        import numpy as np
    except Exception:
        return default_y0, default_h, False

    band_y0 = int(round(height * 0.52)) if is_portrait else int(round(height * 0.72))
    band_h = min(height - band_y0, int(round(height * (0.40 if is_portrait else 0.27))))
    if band_h < 60:
        return default_y0, default_h, False

    sample_fps = max(0.02, min(1.0, 12.0 / max(1.0, duration)))
    cmd = [
        "ffmpeg", "-v", "error", "-i", video_path,
        "-vf", f"fps={sample_fps:.5f},crop={width}:{band_h}:0:{band_y0}",
        "-frames:v", "14", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=20)
        buf = proc.stdout
    except Exception:
        return default_y0, default_h, False

    frame_bytes = width * band_h * 3
    n_frames = len(buf) // frame_bytes
    if n_frames < 2:
        return default_y0, default_h, False

    x0 = int(round(width * 0.15))
    x1 = int(round(width * 0.85))
    y_spans = []
    for k in range(n_frames):
        arr = np.frombuffer(buf[k * frame_bytes:(k + 1) * frame_bytes], dtype=np.uint8).reshape((band_h, width, 3))
        roi = arr[:, x0:x1, :]
        luma = (0.299 * roi[:, :, 0] + 0.587 * roi[:, :, 1] + 0.114 * roi[:, :, 2]).astype(np.float32)
        bright = luma >= 185.0
        if not np.any(bright):
            continue
        # Require dark stroke neighbor in a 5x5 window (approximated via 2-px shifted min)
        padded = np.pad(luma, 2, mode="edge")
        dark_nbr = np.minimum(
            np.minimum(padded[:-4, 2:-2], padded[4:, 2:-2]),
            np.minimum(padded[2:-2, :-4], padded[2:-2, 4:]),
        ) < 125.0
        stroke = bright & dark_nbr
        row_cnt = stroke.sum(axis=1)
        active_rows = np.where(row_cnt >= max(8, int(round((x1 - x0) * 0.015))))[0]
        if len(active_rows) >= 6:
            # Find largest contiguous vertical cluster (subtitle line(s))
            diffs = np.diff(active_rows)
            splits = np.where(diffs > 12)[0]
            groups = np.split(active_rows, splits + 1)
            best = max(groups, key=len)
            h_span = int(best[-1] - best[0] + 1)
            if 10 <= h_span <= int(round(height * 0.12)):
                y_spans.append((int(best[0]) + band_y0, int(best[-1]) + band_y0))

    if len(y_spans) < 2:
        return default_y0, default_h, False

    y_min = int(np.median([s[0] for s in y_spans]))
    y_max = int(np.median([s[1] for s in y_spans]))
    pad_top = max(48, int(round(height * 0.03)))
    pad_bot = max(60, int(round(height * 0.04)))
    cal_y0 = max(0, y_min - pad_top)
    cal_h = min(height - cal_y0, max(200, (y_max - cal_y0) + pad_bot))
    return cal_y0, cal_h, True


def parse_fps(fps_str: str) -> float:
    if not fps_str or fps_str == "0/0":
        return 0.0
    if "/" in fps_str:
        num, den = fps_str.split("/", 1)
        return float(num) / max(1.0, float(den))
    return float(fps_str)


def format_timecode(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = seconds % 60.0
    return f"{hrs:02d}:{mins:02d}:{secs:06.3f}"


def link_or_copy(src: str, dst: str):
    src = os.path.abspath(src)
    dst = os.path.abspath(dst)
    if src == dst:
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst) or os.path.islink(dst):
        os.remove(dst)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def ingest_and_build_episodes_manifest(
    base: str,
    slug: str,
    topology: str,
    video_arg: str = "",
    episodes_dir_arg: str = "",
    episodes_list_arg: list = None,
) -> tuple:
    """
    Ingests single video or multiple episodes into `source/`, concatenates into `source/master_source.mp4`
    if needed, and builds `data/00_episodes_manifest.json`.
    Returns (master_source_abs_path, episodes_manifest_dict).
    """
    source_dir = os.path.join(base, "source")
    eps_dir = os.path.join(source_dir, "episodes")
    os.makedirs(eps_dir, exist_ok=True)
    master_dst = os.path.join(source_dir, "master_source.mp4")

    ep_inputs = []
    if episodes_list_arg:
        ep_inputs = [os.path.abspath(p) for p in episodes_list_arg if os.path.exists(p)]
    elif episodes_dir_arg and os.path.isdir(episodes_dir_arg):
        cand = sorted(glob.glob(os.path.join(episodes_dir_arg, "*.mp4")))
        ep_inputs = [os.path.abspath(p) for p in cand if os.path.isfile(p)]
    else:
        # Check if workspace already has raw_episodes/ or source/episodes/
        legacy_raw = os.path.join(base, "raw_episodes")
        existing_eps = sorted(glob.glob(os.path.join(eps_dir, "*.mp4")))
        if existing_eps:
            ep_inputs = existing_eps
        elif os.path.isdir(legacy_raw):
            ep_inputs = sorted(glob.glob(os.path.join(legacy_raw, "*.mp4")))

    # Link episode files into source/episodes/
    canonical_eps = []
    for idx, src_ep in enumerate(ep_inputs, start=1):
        ep_name = f"ep{idx:02d}.mp4"
        dst_ep = os.path.join(eps_dir, ep_name)
        link_or_copy(src_ep, dst_ep)
        canonical_eps.append(dst_ep)

    # Determine or build source/master_source.mp4
    if video_arg and os.path.exists(video_arg):
        link_or_copy(video_arg, master_dst)
    elif os.path.exists(os.path.join(base, "master_source.mp4")) and not os.path.exists(master_dst):
        link_or_copy(os.path.join(base, "master_source.mp4"), master_dst)

    if len(canonical_eps) >= 2 and not os.path.exists(master_dst):
        # Probe episodes and concatenate into source/master_source.mp4
        concat_txt = os.path.join(source_dir, "concat_list.txt")
        specs = []
        ep_durations = []
        for ep_p in canonical_eps:
            pinfo = probe_video(ep_p)
            vs = next((s for s in pinfo.get("streams", []) if s.get("codec_type") == "video"), {})
            as_ = next((s for s in pinfo.get("streams", []) if s.get("codec_type") == "audio"), {})
            ep_durations.append(float((pinfo.get("format") or {}).get("duration") or vs.get("duration") or 0.0))
            specs.append((
                int(vs.get("width", 1920)),
                int(vs.get("height", 1080)),
                vs.get("codec_name", "h264"),
                vs.get("profile", ""),
                int(vs.get("index", 0)),
                vs.get("time_base", ""),
                round(parse_fps(vs.get("r_frame_rate", "30/1")), 3),
                round(parse_fps(vs.get("avg_frame_rate", "30/1")), 3),
                as_.get("codec_name", "aac"),
                as_.get("profile", ""),
                int(as_.get("sample_rate", 48000)),
                int(as_.get("channels", 2)),
            ))
        expected_dur = sum(ep_durations)
        uniform = len(set(specs)) == 1
        with open(concat_txt, "w", encoding="utf-8") as f:
            for ep_p in canonical_eps:
                rel_p = os.path.relpath(ep_p, source_dir)
                f.write(f"file '{rel_p}'\n")
        need_norm = not uniform
        if uniform:
            print(f"[INIT] Concatenating {len(canonical_eps)} uniform episodes -> {master_dst}", flush=True)
            subprocess.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_txt,
                 "-avoid_negative_ts", "make_zero", "-c", "copy", master_dst],
                check=True,
            )
            m_info = probe_video(master_dst)
            m_dur = float((m_info.get("format") or {}).get("duration") or 0.0)
            m_vs = next((s for s in m_info.get("streams", []) if s.get("codec_type") == "video"), {})
            m_as = next((s for s in m_info.get("streams", []) if s.get("codec_type") == "audio"), {})
            m_av_diff = abs(float(m_vs.get("duration") or m_dur) - float(m_as.get("duration") or m_dur))
            if (expected_dur > 0 and abs(m_dur - expected_dur) > max(3.0, 0.02 * expected_dur)) or m_av_diff > 0.30:
                print(
                    f"[INIT] WARNING: stream-copy concat mismatch (dur={m_dur:.2f}s vs expected {expected_dur:.2f}s, av_diff={m_av_diff:.3f}s); falling back to normalized concat.",
                    flush=True,
                )
                need_norm = True
        if need_norm:
            w0, h0 = specs[0][0], specs[0][1]
            fps_list = [sp[6] for sp in specs if sp[6] > 1.0]
            target_fps = max(set(fps_list), key=fps_list.count) if fps_list else 30.0
            fps_str = str(int(round(target_fps))) if abs(target_fps - round(target_fps)) < 1e-2 else f"{target_fps:.3f}"
            print(f"[INIT] Normalizing & concatenating {len(canonical_eps)} mixed-spec episodes ({w0}x{h0}@{fps_str}fps) -> {master_dst}", flush=True)
            norm_eps = []
            for idx, ep_p in enumerate(canonical_eps, start=1):
                norm_p = os.path.join(source_dir, f".norm_ep{idx:02d}.mp4")
                subprocess.run([
                    "ffmpeg", "-y", "-i", ep_p,
                    "-map", "0:v:0", "-map", "0:a:0",
                    "-vf", f"scale={w0}:{h0}:force_original_aspect_ratio=decrease,pad={w0}:{h0}:(ow-iw)/2:(oh-ih)/2,fps={fps_str},setpts=PTS-STARTPTS",
                    "-af", "asetpts=PTS-STARTPTS",
                    "-video_track_timescale", "15360",
                    "-avoid_negative_ts", "make_zero",
                    "-c:v", "h264_videotoolbox", "-b:v", "5500k",
                    "-c:a", "aac", "-ar", "48000", "-ac", "2",
                    norm_p
                ], check=True)
                norm_eps.append(norm_p)
            with open(concat_txt, "w", encoding="utf-8") as f:
                for np_path in norm_eps:
                    f.write(f"file '{os.path.basename(np_path)}'\n")
            subprocess.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_txt,
                 "-avoid_negative_ts", "make_zero", "-c", "copy", master_dst],
                check=True,
            )
            for np_path in norm_eps:
                if os.path.exists(np_path):
                    os.remove(np_path)
    elif len(canonical_eps) == 1 and not os.path.exists(master_dst):
        link_or_copy(canonical_eps[0], master_dst)

    if not os.path.exists(master_dst):
        raise FileNotFoundError(f"Could not provision master source video at {master_dst}. Provide --video or --episodes-dir.")

    # Ensure single-video projects also have ep01.mp4 linked in source/episodes/ for uniform manifest indexing
    if not canonical_eps:
        ep01_dst = os.path.join(eps_dir, "ep01.mp4")
        link_or_copy(master_dst, ep01_dst)
        canonical_eps = [ep01_dst]

    # Write source/concat_list.txt for multi-episode transparency
    concat_txt = os.path.join(source_dir, "concat_list.txt")
    with open(concat_txt, "w", encoding="utf-8") as f:
        for ep_p in canonical_eps:
            rel_p = os.path.relpath(ep_p, source_dir)
            f.write(f"file '{rel_p}'\n")

    # Build data/00_episodes_manifest.json
    episodes_meta = []
    cursor_s = 0.0
    for idx, ep_p in enumerate(canonical_eps, start=1):
        pinfo = probe_video(ep_p)
        fmt = pinfo.get("format", {})
        vs = next((s for s in pinfo.get("streams", []) if s.get("codec_type") == "video"), {})
        dur = round(float(fmt.get("duration", 0.0)), 3)
        st_s = round(cursor_s, 3)
        et_s = round(cursor_s + dur, 3)
        cursor_s = et_s
        ep_id = f"ep{idx:02d}"
        episodes_meta.append({
            "ep_index": idx,
            "ep_id": ep_id,
            "title": f"Episode {idx:02d}",
            "source_file": os.path.relpath(ep_p, base),
            "duration_s": dur,
            "timeline_start_s": st_s,
            "timeline_end_s": et_s,
            "timecode_start": format_timecode(st_s),
            "timecode_end": format_timecode(et_s),
            "width": int(vs.get("width", 1920)),
            "height": int(vs.get("height", 1080)),
            "fps": round(parse_fps(vs.get("r_frame_rate", "30/1")), 3),
            "sha256": sha256_file(ep_p),
        })

    master_info = probe_video(master_dst)
    master_dur = round(float(master_info.get("format", {}).get("duration", cursor_s)), 3)
    if episodes_meta:
        episodes_meta[-1]["timeline_end_s"] = master_dur
        episodes_meta[-1]["timecode_end"] = format_timecode(master_dur)

    manifest = {
        "schema_version": "2.0",
        "slug": slug,
        "topology": topology,
        "total_episodes": len(episodes_meta),
        "total_duration_s": master_dur,
        "master_source": "source/master_source.mp4",
        "episodes": episodes_meta,
    }
    manifest_json_path = os.path.join(base, "data", "00_episodes_manifest.json")
    with open(manifest_json_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    return master_dst, manifest


def write_schema_templates(base: str, genre: str, localization_mode: str, overlay_style: str, slug: str, series_slug: str):
    schemas_dir = os.path.join(base, "config", "schemas")
    os.makedirs(schemas_dir, exist_ok=True)

    asr_template = {
        "_comment": "Stage 1 canonical item schema for data/01_asr_raw_segments.json",
        "id": 0,
        "ep_id": "ep01",
        "start": 7.20,
        "end": 8.60,
        "text": "你好 外卖到了",
        "audio_type": "normal_speech",
        "source": "whisper_onset_clamped"
    }
    bilingual_template = {
        "_comment": "Stage 2 canonical item schema for data/02_bilingual_script.json",
        "id": 0,
        "ep_id": "ep01",
        "speaker": "M1",
        "audio_type": "normal_speech",
        "raw_ids": [0],
        "start": 7.20,
        "end": 8.60,
        "zh_text": "你好 外卖到了",
        "sub_text": "Xin chào, đồ ăn giao tới rồi,",
        "tts_text": "Xin chào, đồ ăn giao tới rồi,"
    }
    aligned_template = {
        "_comment": "Stage 3 canonical item schema for data/03_aligned_subtitles.json",
        "id": 0,
        "ep_id": "ep01",
        "speaker": "M1",
        "slot_start": 7.20,
        "slot_end": 8.60,
        "tts_dur_raw": 1.40,
        "tts_dur_final": 1.40,
        "tempo_ratio": 1.0,
        "sub_text": "Xin chào, đồ ăn giao tới rồi,",
        "tts_text": "Xin chào, đồ ăn giao tới rồi,",
        "mode": localization_mode
    }

    with open(os.path.join(schemas_dir, "01_asr_segment.template.json"), "w", encoding="utf-8") as f:
        json.dump(asr_template, f, ensure_ascii=False, indent=2)
    with open(os.path.join(schemas_dir, "02_bilingual_cue.template.json"), "w", encoding="utf-8") as f:
        json.dump(bilingual_template, f, ensure_ascii=False, indent=2)
    with open(os.path.join(schemas_dir, "03_aligned_cue.template.json"), "w", encoding="utf-8") as f:
        json.dump(aligned_template, f, ensure_ascii=False, indent=2)

    bible_path = os.path.join(base, "config", "series_bible.json")
    if not os.path.exists(bible_path):
        if genre == "short-drama":
            bible_data = {
                "schema_version": "2.0",
                "series_slug": series_slug or slug,
                "genre": genre,
                "localization_mode": localization_mode,
                "overlay_style": overlay_style,
                "characters": [
                    {
                        "speaker_id": "M1",
                        "name_zh": "男主",
                        "name_vi": "Nam chính",
                        "role": "Protagonist",
                        "voice_id": "Hải Đăng",
                        "default_self_pronoun": "tôi",
                        "notes": "Giữ nguyên cách xưng hô xuyên suốt các tập"
                    },
                    {
                        "speaker_id": "F1",
                        "name_zh": "女主",
                        "name_vi": "Nữ chính",
                        "role": "Lead Female",
                        "voice_id": "Ngọc",
                        "default_self_pronoun": "tôi",
                        "notes": "Xưng hô nhất quán theo ngữ cảnh quan hệ"
                    }
                ],
                "pronoun_matrix": [
                    {
                        "from_speaker": "M1",
                        "to_speaker": "F1",
                        "self_pronoun": "tôi",
                        "address_pronoun": "cô",
                        "context": "Đối thoại tiêu chuẩn giữa nam chính và nữ chính"
                    },
                    {
                        "from_speaker": "M1",
                        "to_speaker": "COMMANDER",
                        "self_pronoun": "tôi",
                        "address_pronoun": "Đội trưởng / Ngài",
                        "context": "Cấp dưới báo cáo cấp trên / quân đội"
                    }
                ],
                "glossary": [
                    {
                        "zh": "诡区",
                        "vi": "Quỷ Khu",
                        "tts_text": "Quỷ Khu",
                        "category": "World-building / Location"
                    },
                    {
                        "zh": "异常调查局",
                        "vi": "Cục Điều Tra Dị Thường",
                        "tts_text": "Cục Điều Tra Dị Thường",
                        "category": "Organization"
                    }
                ]
            }
        else:
            bible_data = {
                "schema_version": "2.0",
                "series_slug": series_slug or slug,
                "genre": genre,
                "localization_mode": localization_mode,
                "overlay_style": overlay_style,
                "characters": [
                    {
                        "speaker_id": "NARRATOR",
                        "name_zh": "旁白 / 博主",
                        "name_vi": "Người dẫn chuyện",
                        "role": "Host / Narrator",
                        "voice_id": "Hải Đăng",
                        "default_self_pronoun": "chúng ta / mình",
                        "notes": "Giọng phân tích rõ ràng, nhịp độ chuẩn"
                    }
                ],
                "pronoun_matrix": [],
                "glossary": [
                    {"zh": "思维链", "vi": "Chuỗi suy luận", "tts_text": "Chuỗi suy luận", "category": "AI/Tech"},
                    {"zh": "蒸馏", "vi": "Chưng cất mô hình", "tts_text": "Chưng cất mô hình", "category": "AI/Tech"},
                    {"zh": "强化学习", "vi": "Học tăng cường", "tts_text": "Học tăng cường", "category": "AI/Tech"}
                ]
            }
        with open(bible_path, "w", encoding="utf-8") as f:
            json.dump(bible_data, f, ensure_ascii=False, indent=2)


def provision_series_hub(workspace_root: str, series_slug: str, genre: str, localization_mode: str, overlay_style: str, export_formats: list, unit_slug: str) -> str:
    """
    Creates or updates a multi-episode Series Hub at `workspace/<series_slug>/`
    with shared `config/series_bible.json`, `source_inbox/`, `units/`, and `deliverables/`.
    """
    hub_dir = os.path.abspath(os.path.join(workspace_root, series_slug))
    for sd in [
        "config",
        "posters",
        "source_inbox",
        "units",
        "deliverables/16x9",
        "deliverables/9x16",
        "deliverables/subtitles",
        "deliverables/episodes",
    ]:
        os.makedirs(os.path.join(hub_dir, sd), exist_ok=True)

    series_cfg_path = os.path.join(hub_dir, "config", "series_config.json")
    series_cfg = {
        "schema_version": "2.0",
        "series_slug": series_slug,
        "genre": genre,
        "localization_mode": localization_mode,
        "overlay_style": overlay_style,
        "export_formats": export_formats,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(series_cfg_path, "w", encoding="utf-8") as f:
        json.dump(series_cfg, f, ensure_ascii=False, indent=2)

    hub_manifest = os.path.join(hub_dir, "00_series_manifest.md")
    if not os.path.exists(hub_manifest):
        with open(hub_manifest, "w", encoding="utf-8") as f:
            f.write(f"""# Series Hub Manifest: `{series_slug}`

- **Series Root:** `{hub_dir}`
- **Genre:** `{genre}`
- **Default Localization Mode:** `{localization_mode}`
- **Default Overlay Style:** `{overlay_style}`
- **Default Export Formats:** `{', '.join(export_formats)}`
- **Shared Series Bible:** `config/series_bible.json`

## Working Units / Episodes Registry
| Unit Slug | Topology | Episodes | Stage Status | 16:9 Deliverable | 9:16 Deliverable | SRT Deliverable |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `{unit_slug}` | `series_episodic` | _See unit manifest_ | `INITIALIZED` | `deliverables/16x9/final_{unit_slug}_vi.mp4` | `deliverables/9x16/final_{unit_slug}_vi_9x16.mp4` | `deliverables/subtitles/{unit_slug}_vi.srt` |
""")
    return hub_dir


def find_ep_id_for_timestamp(ts: float, episodes: list) -> str:
    for ep in episodes:
        if float(ep["timeline_start_s"]) - 0.05 <= ts <= float(ep["timeline_end_s"]) + 0.5:
            return ep["ep_id"]
    return episodes[-1]["ep_id"] if episodes else "ep01"


def main():
    parser = argparse.ArgumentParser(description="Initialize video localization workspace (single, series_compilation, or series_episodic) and sign Stage 0 CI/CD gate.")
    parser.add_argument("--topology", default="auto", choices=["auto", "single", "series_compilation", "series_episodic"], help="Project storage & workflow topology.")
    parser.add_argument("--video", default="", help="Path to single source video file (or pre-merged master video).")
    parser.add_argument("--episodes-dir", default="", help="Directory containing raw episode MP4s (ep01.mp4, ep02.mp4, ...) for multi-episode compilation.")
    parser.add_argument("--episodes", nargs="*", default=None, help="Explicit list of episode MP4 files in order.")
    parser.add_argument("--slug", default="", help="Project or unit slug (lowercase, hyphenated).")
    parser.add_argument("--series-slug", default="", help="Parent series slug when using --topology series_episodic.")
    parser.add_argument("--unit-slug", default="", help="Unit/batch slug inside a series hub (e.g. s01-ep01-05 or ep01).")
    parser.add_argument("--workspace", default="", help="Existing workspace directory path (used with --sync-existing).")
    parser.add_argument("--workspace-root", default="workspace", help="Root directory for workspaces.")
    parser.add_argument("--genre", default="auto", choices=["auto", "tech-review", "short-drama", "documentary", "tutorial"], help="Content genre.")
    parser.add_argument(
        "--mode",
        default="auto",
        choices=["auto", "voice_dub", "sub_only"],
        help="Localization audio/subtitle mode: 'voice_dub' (VieNeu-TTS + auto-ducked BGM) or 'sub_only' (preserve original native audio + full Vietnamese subtitles)."
    )
    parser.add_argument(
        "--overlay-style",
        default="auto",
        choices=["auto", "frosted_glass", "solid_pill"],
        help="Subtitle panel visual style: 'frosted_glass' (sleek compact translucent glass blur) or 'solid_pill' (opaque dark capsule)."
    )
    parser.add_argument(
        "--export-formats",
        default="auto",
        help="Comma-separated target video export formats: e.g. '16x9' or '16x9,9x16'."
    )
    parser.add_argument(
        "--sync-existing",
        action="store_true",
        help="Upgrade/retrofit an existing workspace to the v2.0 storage & template contract without overwriting existing translations or renders."
    )
    args = parser.parse_args()

    # Resolve workspace path & slug
    if args.workspace:
        base = os.path.abspath(args.workspace)
        slug = args.slug or os.path.basename(base)
    elif args.topology == "series_episodic" and args.series_slug and (args.unit_slug or args.slug):
        unit = args.unit_slug or args.slug
        slug = unit
        base = os.path.abspath(os.path.join(args.workspace_root, args.series_slug, "units", unit))
    else:
        slug = args.slug or args.unit_slug
        if not slug:
            print("[ERROR] Must provide --slug (or --workspace with --sync-existing).", file=sys.stderr)
            sys.exit(1)
        base = os.path.abspath(os.path.join(args.workspace_root, slug))

    # Read existing profile if syncing an existing workspace
    existing_profile = {}
    existing_profile_path = os.path.join(base, "data", "00_video_profile.json")
    if os.path.exists(existing_profile_path):
        with open(existing_profile_path, "r", encoding="utf-8") as f:
            existing_profile = json.load(f)

    genre = existing_profile.get("genre", "tech-review") if args.genre == "auto" else args.genre
    if args.mode == "auto":
        localization_mode = existing_profile.get("localization_mode") or ("sub_only" if genre == "short-drama" else "voice_dub")
    else:
        localization_mode = args.mode

    if args.overlay_style == "auto":
        overlay_style = existing_profile.get("overlay_style") or ("frosted_glass" if genre == "short-drama" else "solid_pill")
    else:
        overlay_style = args.overlay_style

    if args.export_formats == "auto":
        export_formats = ["16x9"]
    else:
        export_formats = [fmt.strip() for fmt in args.export_formats.split(",") if fmt.strip()]

    # Determine topology
    if args.topology == "auto":
        if args.series_slug:
            topology = "series_episodic"
        elif args.episodes_dir or args.episodes or os.path.isdir(os.path.join(base, "raw_episodes")) or len(glob.glob(os.path.join(base, "source", "episodes", "*.mp4"))) >= 2:
            topology = "series_compilation"
        else:
            topology = existing_profile.get("topology", "single")
    else:
        topology = args.topology

    # Create standardized v2.0 directory tree
    subdirs = [
        ".cicd",
        "config/schemas",
        "posters",
        "source/episodes",
        "data",
        "media/demucs",
        "media/tts_segments",
        "frames/audit",
        "frames/qa",
        "frames/qa_9x16",
        "deliverables/16x9",
        "deliverables/9x16",
        "deliverables/subtitles",
        "deliverables/episodes",
        "scripts",
    ]
    for sd in subdirs:
        os.makedirs(os.path.join(base, sd), exist_ok=True)

    # If series_episodic, also provision the parent Series Hub
    series_hub_dir = ""
    if topology == "series_episodic" and args.series_slug:
        series_hub_dir = provision_series_hub(
            workspace_root=args.workspace_root,
            series_slug=args.series_slug,
            genre=genre,
            localization_mode=localization_mode,
            overlay_style=overlay_style,
            export_formats=export_formats,
            unit_slug=slug,
        )

    # Resolve source video and build episodes manifest
    video_input = args.video or existing_profile.get("source_video", "")
    master_source_path, episodes_manifest = ingest_and_build_episodes_manifest(
        base=base,
        slug=slug,
        topology=topology,
        video_arg=video_input,
        episodes_dir_arg=args.episodes_dir,
        episodes_list_arg=args.episodes,
    )

    # Probe canonical source/master_source.mp4
    info = probe_video(master_source_path)
    fmt = info.get("format", {})
    streams = info.get("streams", [])
    v_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    a_stream = next((s for s in streams if s.get("codec_type") == "audio"), {})

    width = int(v_stream.get("width", 1920))
    height = int(v_stream.get("height", 1080))
    duration = float(fmt.get("duration", 0.0))
    r_fps = parse_fps(v_stream.get("r_frame_rate", "30/1"))
    avg_fps = parse_fps(v_stream.get("avg_frame_rate", "30/1"))
    is_vfr = abs(r_fps - avg_fps) > 0.5

    aspect_mode = "landscape_16x9" if width >= height else "vertical_9x16"
    strip_y0, strip_h, strip_calibrated = calibrate_strip_y0(master_source_path, width, height, duration)

    profile = {
        "schema_version": "2.0",
        "slug": slug,
        "topology": topology,
        "series_slug": args.series_slug or slug,
        "genre": genre,
        "localization_mode": localization_mode,
        "overlay_style": overlay_style,
        "export_formats": export_formats,
        "total_episodes": episodes_manifest["total_episodes"],
        "source_video": master_source_path,
        "duration": duration,
        "width": width,
        "height": height,
        "r_fps": round(r_fps, 3),
        "avg_fps": round(avg_fps, 3),
        "is_vfr": is_vfr,
        "aspect_mode": aspect_mode,
        "strip_y0": strip_y0,
        "strip_h": strip_h,
        "strip_calibrated": strip_calibrated,
        "audio_codec": a_stream.get("codec_name"),
        "audio_sample_rate": int(a_stream.get("sample_rate", 48000)),
        "audio_channels": int(a_stream.get("channels", 2)),
    }
    profile_path = os.path.join(base, "data", "00_video_profile.json")
    with open(profile_path, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)

    # Write config/project_config.json
    brand_dir = os.path.abspath(os.path.join(args.workspace_root, "branding-dau-do"))
    tl_logo_path = os.path.join(brand_dir, "13_top_left_logo_large.png")
    tr_badge_path = os.path.join(brand_dir, "15_top_right_vietsub_floating.png")
    project_config = {
        "schema_version": "2.0",
        "slug": slug,
        "topology": topology,
        "series_slug": args.series_slug or slug,
        "series_hub_dir": series_hub_dir,
        "genre": genre,
        "localization_mode": localization_mode,
        "overlay_style": overlay_style,
        "export_formats": export_formats,
        "split_episodes_on_export": False,
        "watermark": {
            "enabled": os.path.exists(tl_logo_path) and os.path.exists(tr_badge_path),
            "brand_dir": brand_dir,
            "channel_name": "Đậu Đỏ",
            "credit_prefix": "Vietsub by ",
            "layout": "dual_corner",
            "style": "floating",
            "top_left_logo": tl_logo_path,
            "top_left_x": 42,
            "top_left_y": 22,
            "top_right_badge": tr_badge_path,
            "top_right_margin_x": 42,
            "top_right_y": 58
        },
        "paths": {
            "source_master": "source/master_source.mp4",
            "episodes_dir": "source/episodes",
            "concat_list": "source/concat_list.txt",
            "episodes_manifest": "data/00_episodes_manifest.json",
            "video_profile": "data/00_video_profile.json",
            "series_bible": "config/series_bible.json",
            "asr_segments": "data/01_asr_raw_segments.json",
            "bbox_scan": "data/01_zh_bbox_scan.json",
            "waveform_audit": "data/01_waveform_voice_audit.json",
            "audit_manifest": "data/01_extracted_audit_manifest.json",
            "bilingual_script": "data/02_bilingual_script.json",
            "aligned_subtitles": "data/03_aligned_subtitles.json",
            "overlay_geometry": "data/04_overlay_geometry_log.json",
            "qa_manifest": "data/04_qa_frames_manifest.json",
            "canonical_srt": "data/subtitles_full.srt",
            "posters_dir": "posters",
            "deliverable_16x9": f"deliverables/16x9/final_{slug}_vi.mp4",
            "deliverable_srt": f"deliverables/subtitles/{slug}_vi.srt",
            "deliverable_episodes_dir": "deliverables/episodes"
        }
    }
    project_cfg_path = os.path.join(base, "config", "project_config.json")
    with open(project_cfg_path, "w", encoding="utf-8") as f:
        json.dump(project_config, f, ensure_ascii=False, indent=2)

    # Write schema templates & series_bible.json
    write_schema_templates(
        base=base,
        genre=genre,
        localization_mode=localization_mode,
        overlay_style=overlay_style,
        slug=slug,
        series_slug=args.series_slug or slug,
    )
    if series_hub_dir:
        hub_bible = os.path.join(series_hub_dir, "config", "series_bible.json")
        unit_bible = os.path.join(base, "config", "series_bible.json")
        if not os.path.exists(hub_bible):
            link_or_copy(unit_bible, hub_bible)
        else:
            link_or_copy(hub_bible, unit_bible)

    # Backfill ep_id into existing data/01_asr_raw_segments.json and data/02_bilingual_script.json if syncing
    eps_list = episodes_manifest.get("episodes", [])
    for jname in ["01_asr_raw_segments.json", "02_bilingual_script.json", "03_aligned_subtitles.json"]:
        jpath = os.path.join(base, "data", jname)
        if os.path.exists(jpath):
            with open(jpath, "r", encoding="utf-8") as f:
                items = json.load(f)
            changed = False
            for item in items:
                if "ep_id" not in item:
                    st = float(item.get("start", item.get("slot_start", 0.0)))
                    item["ep_id"] = find_ep_id_for_timestamp(st, eps_list)
                    changed = True
            if changed:
                with open(jpath, "w", encoding="utf-8") as f:
                    json.dump(items, f, ensure_ascii=False, indent=2)

    # Sync deliverables vault links if final files already exist
    m_16x9 = os.path.join(base, "media", f"final_{slug}_vi.mp4")
    if os.path.exists(m_16x9):
        link_or_copy(m_16x9, os.path.join(base, "deliverables", "16x9", f"final_{slug}_vi.mp4"))
    m_9x16 = os.path.join(base, "media", f"final_{slug}_vi_9x16.mp4")
    if os.path.exists(m_9x16):
        link_or_copy(m_9x16, os.path.join(base, "deliverables", "9x16", f"final_{slug}_vi_9x16.mp4"))
    m_srt = os.path.join(base, "data", "subtitles_full.srt")
    if os.path.exists(m_srt):
        link_or_copy(m_srt, os.path.join(base, "deliverables", "subtitles", f"{slug}_vi.srt"))

    # Remove any stray duplicate .json / .srt files from scripts/ (enforcing zero-entropy storage)
    scripts_dir = os.path.join(base, "scripts")
    if os.path.isdir(scripts_dir):
        for fname in os.listdir(scripts_dir):
            if fname.endswith((".json", ".srt")):
                stray_p = os.path.join(scripts_dir, fname)
                print(f"[CLEANUP] Removing non-script data file from scripts/: {stray_p}", flush=True)
                os.remove(stray_p)

    # Remove legacy raw_episodes/ if already ingested into source/episodes/
    legacy_raw_dir = os.path.join(base, "raw_episodes")
    if os.path.isdir(legacy_raw_dir) and len(glob.glob(os.path.join(base, "source", "episodes", "*.mp4"))) >= 1:
        shutil.rmtree(legacy_raw_dir)

    # Build episode timeline markdown table for 00_project_manifest.md
    ep_rows = []
    for ep in eps_list:
        ep_rows.append(
            f"| `{ep['ep_id']}` | `{ep['source_file']}` | `{ep['duration_s']:.2f}s` | "
            f"`{ep['timeline_start_s']:.2f}s – {ep['timeline_end_s']:.2f}s` | `{ep['timecode_start']} -> {ep['timecode_end']}` |"
        )
    ep_table_md = "\n".join(ep_rows)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    manifest_path = os.path.join(base, "00_project_manifest.md")
    if not os.path.exists(manifest_path) or args.sync_existing:
        manifest_content = f"""# 00 — Project Manifest: `{slug}`

- **Created / Synced At:** {now_str}
- **Project Topology:** `{topology}` (`total_episodes = {episodes_manifest['total_episodes']}`)
- **Genre Profile:** `{genre}` (`{aspect_mode}`)
- **Localization Mode:** `{localization_mode}` (`voice_dub` = VieNeu-TTS + BGM ducking | `sub_only` = Preserve native original audio + full Vietnamese subtitles)
- **Subtitle Overlay Style:** `{overlay_style}` (`frosted_glass` = compact Gaussian glass blur with stroke suppression | `solid_pill` = opaque dark capsule)
- **Target Export Formats:** `{', '.join(export_formats)}`
- **Master Source Video:** `source/master_source.mp4` (`{master_source_path}`)
- **Workspace Path:** `{base}`
- **Duration:** `{duration:.2f}s` (`{format_timecode(duration)}`)
- **Resolution:** `{width}x{height}` @ `{r_fps:.2f} fps` (avg `{avg_fps:.2f} fps`, VFR=`{is_vfr}`)
- **Audio Track:** `{profile['audio_codec']}`, `{profile['audio_sample_rate']} Hz`, `{profile['audio_channels']} channels`
- **Hardsub Scan Band:** `y=[{strip_y0}..{strip_y0 + strip_h}]` (strip height `{strip_h}px`)

## 1. Episode & Timeline Map (`data/00_episodes_manifest.json`)
| Episode ID | Canonical Source File | Duration | Merged Timeline (s) | Timecode Range |
| :--- | :--- | :--- | :--- | :--- |
{ep_table_md}

## 2. Canonical Storage Contract (`config/project_config.json`)
- **Immutable Source Inputs:** `source/master_source.mp4`, `source/episodes/epXX.mp4`, `source/concat_list.txt`
- **Config & Series Bible:** `config/project_config.json`, `config/series_bible.json`, `config/schemas/*.template.json`
- **Single Source of Truth Data:** `data/00_video_profile.json`, `data/00_episodes_manifest.json`, `data/01_asr_raw_segments.json`, `data/01_zh_bbox_scan.json`, `data/01_waveform_voice_audit.json`, `data/02_bilingual_script.json`, `data/03_aligned_subtitles.json`, `data/04_overlay_geometry_log.json`, `data/subtitles_full.srt`
- **Final Release Deliverables Vault:**
  - Landscape `16:9`: `deliverables/16x9/final_{slug}_vi.mp4`
  - Vertical `9:16`: `deliverables/9x16/final_{slug}_vi_9x16.mp4`
  - Vietnamese Subtitles: `deliverables/subtitles/{slug}_vi.srt`
  - Per-Episode Split Exports: `deliverables/episodes/`

## 3. Pipeline CI/CD Gate Checklist
- [x] **Stage 0:** Workspace Topology, Episode Ingestion & Config Templates (`00_project_manifest.md`, `.cicd/stage_0_gate.json`)
- [ ] **Stage 1:** ASR, 5fps BBox Scan, Waveform Onset & Visual Audit (`01_visual_audit_notes.md`, `.cicd/stage_1_gate.json`)
- [ ] **Stage 2:** Bilingual Dual-Layer Script & Series Bible Compliance (`02_glossary_and_pronunciation.md`, `.cicd/stage_2_gate.json`)
- [ ] **Stage 3:** 48kHz Audio Master (`sub_only` native stereo or `voice_dub` VieNeu-TTS) (`03_pacing_and_audio_report.md`, `.cicd/stage_3_gate.json`)
- [ ] **Stage 4:** Dynamic Frosted-Glass / Pill Alpha Subtitle Overlay (`media/subtitles_overlay_alpha.mov`, `.cicd/stage_4_gate.json`)
- [ ] **Stage 5:** Final Composite, CV QA Audit & Deliverables Publishing (`04_qa_review_report.md`, `.cicd/stage_5_gate.json`)
"""
        with open(manifest_path, "w", encoding="utf-8") as f:
            f.write(manifest_content)

    # Genre- & Mode-Matched Markdown Templates
    if genre == "short-drama":
        glossary_template = f"""# 02 — Series Bible, Character Pronouns (`Xưng Hô`) & Glossary (`{slug}`)

> **Canonical JSON Config:** Synced with `config/series_bible.json`. Keep character names and pronouns 100% consistent across all episodes.

## 1. Bảng Nhân Vật & Giọng Thoại (Character Roster)
| Speaker ID | Tên Gốc (ZH) | Tên Chuẩn (VI) | Vai Trò | Xưng Hô Mặc Định | Ghi Chú Ngữ Điệu |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `M1` | 男主 / 外卖员 | Nam chính | Nhân vật chính | `tôi` | Bình tĩnh, dứt khoát |
| `F1` | 女主 | Nữ chính | Nhân vật chính | `tôi` | Lạnh lùng, cảnh giác |
| `COMMANDER` | 队长 / 指挥 | Đội trưởng | Chỉ huy | `tôi / chúng ta` | Nghiêm nghị, mệnh lệnh |

## 2. Ma Trận Xưng Hô Giữa Các Nhân Vật (Pronoun Matrix)
| Người Nói (`From`) | Người Nghe (`To`) | Tự Xưng (`Self`) | Gọi Đối Phương (`Address`) | Ngữ Cảnh Quan Hệ |
| :--- | :--- | :--- | :--- | :--- |
| `M1` | `COMMANDER` | `tôi` | `Đội trưởng / Ngài` | Báo cáo cấp trên / quân sự |
| `M1` | `F1` | `tôi` | `cô` | Đồng đội sinh tồn |
| `M1` | `MONSTER / ENEMY` | `tao` | `mày` | Đối đầu sinh tử |

## 3. Thuật Ngữ Bối Cảnh & Thế Giới Quan (World-Building Glossary)
| Thuật Ngữ Gốc (ZH) | Dịch Sai (Nghĩa Đen) | Thuật Ngữ Chuẩn (VI) | Phân Loại |
| :--- | :--- | :--- | :--- |
| `诡区` | khu quỷ dị | `Quỷ Khu` | Địa danh cốt lõi |
| `异常调查局` | cục điều tra bất thường | `Cục Điều Tra Dị Thường` | Tổ chức |
"""
    else:
        glossary_template = f"""# 02 — Domain Glossary & VieNeu-TTS Pronunciation Rules (`{slug}`)

> **Canonical JSON Config:** Synced with `config/series_bible.json`.

## 1. Domain Terminology Mapping (Chinese -> Vietnamese)
| Chinese Term | Bad Literal Translation | Canonical Vietnamese Tech Term |
| :--- | :--- | :--- |
| `思维链` | 4 năm / chuỗi tư duy | `Chuỗi suy luận (Chain of Thought)` |
| `预训练` | tập trước | `Tiền huấn luyện (Pre-training)` |
| `监督微调` | mùi vị giám sát | `Tinh chỉnh có giám sát (SFT)` |
| `强化学习` | học mạnh | `Học tăng cường (RL)` |
| `奖励机制` | cơ chế thưởng | `Cơ chế thưởng phạt (Reward)` |
| `损失函数` | hàm hại | `Hàm mất mát (Loss)` |
| `蒸馏` | chưng cất | `Chưng cất mô hình (Distillation)` |

## 2. Dual-Layer `<en>...</en>` Phonemization Rules for `VieNeu-TTS` (`sea-g2p`)
| Display Subtitle (`sub_text`) | TTS Input (`tts_text`) | Verified IPA Output |
| :--- | :--- | :--- |
| `Anthropic` | `<en>An thro pic</en>` | `ænˈθɹɑːpɪk` |
| `Claude` | `<en>Claude</en>` | `klɔːd` |
| `DeepSeek-R1` | `<en>Deep Seek R one</en>` | `diːp siːk ɑːɹ wʌn` |
"""

    if localization_mode == "sub_only":
        pacing_template = f"""# 03 — Native Audio Preservation & Subtitle Reading-Speed Report (`{slug}`)

- **Localization Mode:** `sub_only` (100% Native Stereo Audio Preserved — Zero TTS Overdub)
- **Master Audio Track:** `media/master_audio_48k.wav` (`48000 Hz`, `2 channels`, lossless PCM extracted from `source/master_source.mp4`)
- **Total Subtitle Clauses:** _TBD_
- **Subtitle Reading Speed (Syllables/sec):** `mean = TBD`, `max = TBD` (Target `<= 7.8 syl/s`)
- **Onset & Gap Alignment:** `premature_lead_segments = 0`, `internal_blank_gap_segments = 0`
"""
    else:
        pacing_template = f"""# 03 — Voice Pacing & BGM Mixing Report (`{slug}`)

- **Localization Mode:** `voice_dub` (`VieNeu-TTS v3 Turbo` `48000 Hz` + Auto-Ducked BGM)
- **Total Segments:** _TBD_
- **Tempo Ratio Statistics:** `mean = TBD`, `median = TBD`, `max = TBD` (Hard cap `<= 1.24x`)
- **Breathing Pause Statistics:** `mean = TBD`, `min = TBD`, `max = TBD`
- **BGM Auto-Ducking:** `0.28` gain during active speech, `0.65` gain during pauses (`media/demucs/no_vocals.wav` resampled to `48000 Hz` stereo)
"""

    notes_templates = {
        "01_visual_audit_notes.md": f"""# 01 — Visual Frame Audit & Acoustic Voice Recovery Log (`{slug}`)

## 1. Visual Keyframe OCR & Homophone Audit Table
| Timestamp (s) | Episode | Frame File | Raw Whisper ASR Text | Visual On-Screen Text / Graphics | Corrected Term & Context |
| :--- | :--- | :--- | :--- | :--- | :--- |

## 2. Acoustic Waveform & Filtered Voice Recovery (Phone Calls, Whispers, Radio/PA)
- **Total Visual Hardsub Sub-Clauses:** _TBD_
- **Sub-Clause Coverage Ratio:** _TBD_ (Target `>= 96%`)
- **Premature Lead Segments (`start` before voice/hardsub):** `0`
- **Internal Blank-Gap Segments (unsplit two-clause merges):** `0`
""",
        "02_glossary_and_pronunciation.md": glossary_template,
        "03_pacing_and_audio_report.md": pacing_template,
        "04_qa_review_report.md": f"""# 04 — Final QA, Deliverables Vault & Visual Frame Verification Report (`{slug}`)

## 1. Stream & Format Verification (`ffprobe`)
- **16:9 Master (`deliverables/16x9/final_{slug}_vi.mp4`):** _TBD_
- **9:16 Vertical Master (`deliverables/9x16/final_{slug}_vi_9x16.mp4`):** _TBD_
- **Canonical SRT (`deliverables/subtitles/{slug}_vi.srt`):** _TBD_
- **Subtitle Streams (`mov_text`):** `0` (verified `-sn`)

## 2. Stratified Visual Frame Inspection Checklist
| Timestamp | Frame Path | Chinese Hardsub 100% Covered? | Lines <= 2 & Centered? | Onset & Visual Cue Sync Verified? |
| :--- | :--- | :--- | :--- | :--- |
"""
    }

    for fname, content in notes_templates.items():
        fpath = os.path.join(base, fname)
        if not os.path.exists(fpath):
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(content)

    # Check zero stray data files in scripts/
    stray_script_files = [
        f for f in os.listdir(os.path.join(base, "scripts"))
        if f.endswith((".json", ".srt"))
    ]

    checks = [
        {
            "id": "source_video_and_episodes_manifest_ready",
            "description": "Canonical source/master_source.mp4 and data/00_episodes_manifest.json are provisioned and valid",
            "weight": 25,
            "passed": duration > 1.0 and width > 0 and height > 0 and os.path.exists(os.path.join(base, "data", "00_episodes_manifest.json")),
            "detail": f"topology={topology}, episodes={episodes_manifest['total_episodes']}, {width}x{height}, duration={duration:.2f}s"
        },
        {
            "id": "audio_stream_present",
            "description": "Source video contains a valid audio stream",
            "weight": 15,
            "passed": bool(a_stream),
            "detail": f"codec={profile['audio_codec']}, sr={profile['audio_sample_rate']}Hz"
        },
        {
            "id": "config_and_schema_templates_provisioned",
            "description": "config/project_config.json, config/series_bible.json, and config/schemas/*.template.json exist",
            "weight": 25,
            "passed": all(os.path.exists(os.path.join(base, p)) for p in [
                "config/project_config.json",
                "config/series_bible.json",
                "config/schemas/01_asr_segment.template.json",
                "config/schemas/02_bilingual_cue.template.json",
                "config/schemas/03_aligned_cue.template.json",
            ]),
            "detail": f"mode={localization_mode}, style={overlay_style}, exports={export_formats}"
        },
        {
            "id": "workspace_tree_and_deliverables_vault_ready",
            "description": "Standardized workspace tree, deliverables/{16x9,9x16,subtitles,episodes}, and 00..04 Markdown logs exist with 0 stray data files in scripts/",
            "weight": 20,
            "passed": (
                all(os.path.exists(os.path.join(base, f)) for f in [
                    "00_project_manifest.md",
                    "01_visual_audit_notes.md",
                    "02_glossary_and_pronunciation.md",
                    "03_pacing_and_audio_report.md",
                    "04_qa_review_report.md",
                    "deliverables/16x9",
                    "deliverables/9x16",
                    "deliverables/subtitles",
                    "deliverables/episodes",
                ])
                and len(stray_script_files) == 0
            ),
            "detail": f"base={base}, stray_files_in_scripts={stray_script_files}"
        },
        {
            "id": "video_profile_json_saved",
            "description": "data/00_video_profile.json written with aspect ratio & scan coordinates",
            "weight": 15,
            "passed": os.path.exists(profile_path),
            "detail": f"aspect={aspect_mode}, strip_y0={strip_y0}, strip_h={strip_h}"
        }
    ]

    write_gate_token(
        workspace=base,
        stage=0,
        stage_name="Workspace Topology, Storage Contract & Video Profile Initialization",
        checks=checks,
        metrics=profile,
        artifact_paths=[
            "00_project_manifest.md",
            "config/project_config.json",
            "data/00_video_profile.json",
            "data/00_episodes_manifest.json",
        ],
    )


if __name__ == "__main__":
    main()
