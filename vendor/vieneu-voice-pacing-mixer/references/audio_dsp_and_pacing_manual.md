# Audio DSP, Elastic Pacing Mathematics & BGM Auto-Ducking Manual

This manual specifies the acoustic signal-processing rules, timeline alignment equations, and loudness controls for Stage 3 (`vieneu-voice-pacing-mixer`).

---

## 1. `VieNeu-TTS v3 Turbo` Engine & Voice Persona Guide

### Runtime Environment
- **Interpreter:** Always invoke `/Users/Shared/Tools/VieNeu-TTS/.venv/bin/python`.
- **Precision:** Use default cached `fp32` ONNX weights (`Vieneu()`). On Apple Silicon M-series CPUs, `fp32` runs at `RTF ≈ 0.32` (~3x faster than real-time) with zero HuggingFace network wait.
- **Native Sample Rate:** `48000 Hz` mono float32 output per segment.

### Preset Voice Selection by Content Genre
| Voice Name | Gender / Timbre | Best Used For |
| :--- | :--- | :--- |
| `Hải Đăng` | Male, crisp, analytical, modern Northern tech cadence | Bilibili/YouTube tech reviews, AI deep-dives, coding tutorials, finance essays |
| `Bình` | Male, warm, steady, narrative gravitas | Documentaries, historical/investigative essays, male lead in dramas |
| `Ngọc` | Female, clear, expressive, natural warmth | Female lead in short dramas, lifestyle/product reviews, bilingual explainers |
| `Tuyên` | Male, authoritative, news/commentary projection | News briefings, geopolitical commentary, authoritative narrator |

---

## 2. Plosive-Safe Silence Trimming (`trim_silence`)

Neural TTS models typically pad `120ms–280ms` of leading and trailing silence around each synthesized clause. Leaving raw silence untouched wastes ~20% of the available slot window and forces unnecessary `atempo` speedup; however, aggressive trimming (`threshold > 0.03` with `0ms` pad) clips initial unvoiced plosives (`p, t, c, k, s, kh`) and trailing nasal/stop codas (`-m, -n, -ng, -p, -t, -c`).

### Canonical Trimming Formula
- Amplitude threshold: `thresh = 0.008` (on normalized `[-1.0, 1.0]` float32 waveform).
- Safety margin: `pad = int(0.030 * 48000)` (`30ms` = `1440` samples preserved before first active sample and after last active sample).
- Boundary fade-in / fade-out: Apply a `15ms` (`720` samples) linear ramp at both ends of every fitted segment to eliminate zero-crossing DC clicks.

---

## 3. Hard-Locked `slot_start` & Elastic Slot Ceiling Mathematics

### Why Cumulative Shifting (`seg[i+1].start += overflow`) Is Strictly Forbidden
In a 306-segment video, if each segment overflows by just `80ms` and shifts the next segment forward, by segment #150 the narration is **12 seconds behind the video**!
- **Rule 1 (Hard-Locked Start):** Segment `i` MUST start at `s_sample = int(round(seg[i].start * 48000))`. `slot_start` never moves based on previous segments.
- **Rule 2 (Breathing Room Reservation):** Reserve at least `140ms` (`0.14s`) before `seg[i+1].start` so the listener hears a distinct human breathing boundary between clauses (`mean breathing pause ≈ 0.22s`).
- **Rule 3 (Elastic Ceiling):** Allow segment `i` to absorb at most `120ms` (`0.12s`) of slack after `seg[i].end` if and only if `seg[i+1].start` is far enough away:
  $$\text{ceiling}_i = \min\left(\text{end}_i + 0.12,\; \text{start}_{i+1} - 0.14\right)$$
  $$\text{avail\_dur}_i = \max\left(0.25,\; \text{ceiling}_i - \text{start}_i\right)$$
- **Rule 4 (Bounded Tempo):**
  $$\text{raw\_ratio}_i = \frac{\text{trimmed\_wav\_dur}_i}{\text{avail\_dur}_i}$$
  - If $\text{raw\_ratio}_i \le 1.0$: keep $\text{tempo}_i = 1.00\times$ (100% natural, unstretched voice).
  - If $1.0 < \text{raw\_ratio}_i \le 1.24$: apply WSOLA phase-locked time-stretch (`ffmpeg -filter:a atempo=...`).
  - If $\text{raw\_ratio}_i > 1.24$: **REJECT at Stage 3A audit**. Rewrite `sub_text` and `tts_text` for segment $i$ to be 15–25% shorter and re-synthesize only segment $i$.

---

## 4. 48kHz Stereo BGM Resampling & Smooth Cosine Auto-Ducking

1. **Sample-Rate Normalization:** `htdemucs` outputs `no_vocals.wav` at `44100 Hz` stereo. Always pipe `no_vocals.wav` through `ffmpeg -f f32le -ar 48000 -ac 2` so its sample count matches the `48000 Hz` voice array sample-for-sample.
2. **Cosine Auto-Ducking Envelope (`400ms` Hanning Window):**
   - Build a binary speech indicator array `speech_env[t] = 1.0` where voice is active and `0.0` during pauses.
   - Convolve `speech_env` with a `400ms` (`19,200` samples at `48kHz`) normalized Hanning window (`np.hanning(19200)`).
   - Compute dynamic BGM gain:
     $$G_{\text{bgm}}(t) = G_{\text{pause}} - \text{smooth\_speech}(t) \cdot (G_{\text{pause}} - G_{\text{speech}})$$
     where $G_{\text{speech}} = 0.28$ during active narration and $G_{\text{pause}} = 0.65$ during pauses/transitions.
3. **Stereo Master Summing & Peak Limiting:**
   - Mix center-panned voice (`0.92` gain on both L and R) with stereo ducked BGM:
     $$\text{Master}_{L,R}(t) = 0.92 \cdot V(t) + G_{\text{bgm}}(t) \cdot \text{BGM}_{L,R}(t)$$
   - If peak amplitude exceeds `0.95`, normalize softly to `0.92` (`-0.7 dBFS` headroom) so AAC encoding never clips.
