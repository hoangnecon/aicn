# Incremental Build & Content-Addressed Cache Model (`v3.0.0`)

## 1. Zero-Path-Dependence Cache Architecture (`aicn_platform.py`)

Every expensive artifact in `v3.0.0` is keyed strictly by **pure content SHA-256 + algorithm version + parameters** via `<global_cache_dir>/content_hash.db` (SQLite with `PRAGMA journal_mode=WAL; synchronous=NORMAL; busy_timeout=15000`).

- **How `ap.content_sha256(path)` works:** Looks up `(normcase(abspath(path)), size, mtime_ns)` in `content_hash.db` table `stat_hash`; on miss, streams 1 MiB chunks to compute true SHA-256 and updates the row inside a short transaction.
- **Why moving/renaming folders still hits 100% cache:** Only the stat lookup key uses `norm_path`. Downstream cache keys use ONLY the resulting file **content SHA-256**, never paths. Renaming a unit (`unit_01` → `unit_01_repaired`) or moving a series hub hashes the unchanged file once at the new path and matches the exact existing key in `asr/`, `stems/`, `tts_sent/`, `rb_stretch/`, `wm_overlay/`, and `build_state.json`.
- **Relocation-safe CI/CD gates (`cicd_gate.py`, `schema_version: "3.0"`):** Signed `.cicd/stage_N_gate.json` payloads record `workspace_slug` (no absolute `workspace` path) and workspace-relative POSIX artifact keys (`data/...`, `media/...`), verified via `ap.content_sha256` and written atomically via `ap.atomic_write_json`.

---

## 2. Single-Source Cache Key Builders & Stores

All scripts import their cache key builders from `scripts/aicn_platform.py` (no per-script duplicated key formulas):

| Output | Granularity | Key Function & Exact Inputs | Store |
|---|---|---|---|
| **Demucs Stems** | unit source | `stems_key(src_sha, demucs_model="htdemucs", STEMS_VERSION="aicn-audio-1.0")[:24]` | `<global_cache>/stems/<key>/{vocals,accomp}.wav` (`PCM_16`, hardlinked/copied into unit) |
| **Stage 1 ASR** | unit source | `stage1_asr_key(src_sha, voc_sha, model, source_lang, ASR_PIPELINE_VERSION="asr-4.0")[:24]` | `<global_cache>/asr/<key>.json` (also synced to `build_state.json["stage1_key"]`) |
| **Raw VieNeu TTS** | utterance | `tts_sent_key(text, voice, TTS_ENGINE_TAG="vieneu-v3-turbo-48k", temperature=0.8, top_k=25)` (64 hex) | `<global_cache>/tts_sent/<key>.wav` (`PCM_16`, 48 kHz) |
| **Rubberband Stretch** | utterance + tempo | `rb_stretch_key(raw_wav_sha, round(tempo, 3), RB_STRETCH_VERSION="rb3-f-3.0")[:24]` | `<global_cache>/rb_stretch/<key>.wav` (`PCM_16`, 48 kHz) |
| **Watermark Strip** | brand + resolution | `wm_overlay_key(width, height, tl_sha, tr_sha, scale, pos)[:24]` | `<global_cache>/wm_overlay/<key>.png` (pre-composited tight top-band RGBA) |
| **Stage 4 Render** | unit | `render_key(src_sha, audio_sha, wm_key, enc_dict, FF_RENDERER_VERSION="v3-dub-wm-3.0")` | `media/.staging_render.mp4` + unit `.cache/build_state.json` |

---

## 3. VRAM-Aware GPU Pool & Warm Multi-Unit Batching

- **VRAM-Aware Concurrent GPU Pool (No Single-Lock Serialization):** `ap.gpu_slot(task, vram_mb)` allocates OS-advisory-locked 512 MB VRAM slots (`gpu_vram_slot_0..M-1.lock`) inside `~/.cache/aicn/locks/` (`M = floor(budget_mb / 512)`, `85%` CUDA VRAM or `60%` Apple RAM):
  - `whisper` (`large-v3` `int8_float16`): `2560 MB` (5 slots)
  - `demucs` (`htdemucs`): `1024 MB` (2 slots)
  - `vieneu` (`v3 Turbo` CUDA `infer_batch`): `1024 MB` (2 slots)
  - On a 6 GB RTX 2060 (10 slots), `whisper(5) + demucs(2) + vieneu(2) = 9 <= 10` run concurrently without OOM. Process crashes release OS locks immediately. Parent→child lock ownership propagates via `AICN_HELD_GPU_SLOTS`.
- **Warm Multi-Unit Model Loading:** `run-series --phase prep` and `--phase produce` run `batch-separate`, `run_asr_and_scan.py --units`, and `batch-all` so Demucs, Whisper, and VieNeu stay loaded in GPU memory across all units in the batch instead of cold-starting per unit.
- **Lock-free Parallel Hardware Render:** NVENC / VideoToolbox Stage 4 renders and 4-worker Stage 5 QA extraction never acquire `gpu_slot` (`AICN_MAX_RENDER_JOBS=2` default).

---

## 4. Incremental Edit Loop Cost

| Edit Type | What Invalidates | Work Re-Run |
|---|---|---|
| **Fix `.srt` display text (`sub_text` only)** | Stage 2 gate, SRT export | `validate_bilingual_script.py` → `aicn_audio.py verify` (or re-export SRT) → `merge-series` (**0 TTS, 0 mix, 0 video render**) |
| **Edit 1 dubbed line (`tts_text`)** | 1 utterance (`tts_sent` + `rb_stretch`) + unit mix/master/render | 1 VieNeu inference (all unchanged utterances hit `tts_sent` + `rb_stretch` cache) → `<12s` single-STFT `mix` → `~4s` `master` → 1 fast hardware `render` → QA → `merge-series` |
| **Tune `config/audio_mix.json` mix/EQ/master** | `mix` + `master` + `render` | **0 TTS / 0 rubberband** → `<12s` `mix` → `~4s` `master` → 1 fast hardware `render` → QA → `merge-series` |
| **Change watermark (`--brand-dir`) or `--codec`** | `wm_overlay` (once globally) + `render` | **0 Stage 1–3 work** → 1 fast hardware `render` (or instant `-c:v copy` if watermark removed) → QA |
| **Re-run unchanged unit** | Nothing | `aicn.py plan` reports `render=current`; `render` and cached Stage 1/3 steps skip immediately |
| **Restore undone `tts_text` edit** | Restores old key | Old `tts_sent` & `rb_stretch` WAVs hit global cache immediately (0 synthesis) |

---

## 5. Correctness Invariants During Incremental Updates

- `aicn.py plan --unit U` compares the exact current `stage1_asr_key` and `render_key` inputs against `.cache/build_state.json` and prints `render=current` or `stale: <parts>`. If anything unexpected is stale, check before rendering.
- `render` uses `-fps_mode passthrough -sn` so output frame count matches `source/master_source.mp4` frame-for-frame and audio cannot drift.
- If `cmd_dub` encounters an utterance overflow after `tts_text` expansion, it fails fast (exit code `2`) **before** `cmd_mix`, preserving clean audio state and preventing wasted mix/master runs.
