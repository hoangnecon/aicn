# Visual Keyframe Audit & ASR Disambiguation Handbook (`v3.0.0`)

Whisper ASR models process only the audio waveform. In fast-paced Chinese and English videos (short dramas, Bilibili tech essays, documentaries), audio-only transcription suffers from **homophone corruption**, **proper-noun misspelling**, and **English acronym mangling**. Visual keyframe auditing (`extract_audit_frames.py` → `frames/audit/frame_*s.jpg`) resolves these ambiguities before Stage 2 translation begins.

---

## 1. Visual Layers to Inspect in Every Extracted Audit Frame

When calling `read_file` on every frame in `data/01_extracted_audit_manifest.json` (`frames/audit/frame_XXXXs.jpg`), systematically inspect these visual cues:

| Visual Layer | What to Look For | Impact on Localization |
| :--- | :--- | :--- |
| **1. On-Screen Dialogue / Hardsubs (if present)** | Exact Chinese characters (`汉字`) or English tokens burned into the source frame. | Overrides phonetic Whisper homophones (`Ancelope` -> `Anthropic`, `DPSK21` -> `DeepSeek-R1`, character surname homophones). |
| **2. Center Action / Slides / UI Pointers** | Highlighted text, diagrams, props, phone screens, contracts, system HUDs (e.g., `<\|user\|>`, `<\|end\|>`, `Fire-Flyer 3FS`, drama system pop-ups). | Locks **Visual Anchor Timing**: ensures the Vietnamese dub names the exact on-screen entity at the right moment and matches Pass 2B recovered lines to visible speakers. |
| **3. Floating Impact Text (`花字` / Callouts)** | Large stylized pop-up banners or drama titles (`批量创建账号`, `系统任务`, `监督微调`). | Confirms scene context, proper names, and comedic/dramatic register. |
| **4. Lower-Thirds & Character Nameplates** | Chyrons introducing characters, titles, CEOs, or locations (`黄仁勋`, `Dario Amodei`, character roles). | Locks spelling and gender/hierarchy in `config/series_bible.json` before Stage 2 translation. |

---

## 2. Systematic Chinese ASR Homophone Trap Catalog

### A. AI, Software & Hardware Traps
| Whisper Phonetic Error | True On-Screen Text | Correct Vietnamese Adaptation |
| :--- | :--- | :--- |
| `Ancelope` / `安瑟罗` / `安索匹克` | `Anthropic` | `Anthropic` (`<en>An thro pic</en>`) |
| `克劳德` / `Cloud` | `Claude` | `Claude` (`<en>Claude</en>`) |
| `DPSK21` / `Deep Sick` | `DeepSeek-R1` | `DeepSeek-R1` (`<en>Deep Seek R one</en>`) |
| `DSH 3FS Deep in Four` | `DSH, 3FS, DeepInfer` | `DSH, 3FS, DeepInfer` (`<en>D S H</en>, <en>Three F S</en>, <en>Deep In fer</en>`) |
| `千问` / `前文` / `Queen` | `通义千问` / `Qwen` | `Qwen` (`<en>Quen</en>`) |
| `思维链` -> mistranslated as `4 năm` | `思维链 (Chain of Thought)` | `chuỗi suy luận` |
| `微调` -> mistranslated as `mùi vị` | `监督微调 (SFT)` | `tinh chỉnh có giám sát (SFT)` |
| `爬虫` -> mistranslated as `bò sát` | `网络爬虫 (Crawler)` | `công cụ thu thập dữ liệu (crawler)` |
| `中转站` -> `trạm xe buýt` | `API 中转站` | `trạm trung chuyển API` |
| `算力` -> `toán lực` | `算力 (Compute)` | `năng lực tính toán` / `hạ tầng GPU` |
| `幻觉` -> `ảo giác ma thuật` | `模型幻觉 (Hallucination)` | `ảo giác (hallucination)` |
| `对齐` -> `xếp hàng ngang` | `安全对齐 (Alignment)` | `căn chỉnh an toàn (alignment)` |
| `套壳` -> `bọc vỏ ốc` | `套壳模型 (Wrapper)` | `bọc vỏ API (wrapper)` |

### B. Drama, Business, Finance & Internet Culture Traps
| Whisper / Literal Error | True On-Screen Text | Correct Vietnamese Adaptation |
| :--- | :--- | :--- |
| `割韭菜` -> `cắt hẹ` | `割韭菜` | `lùa gà` / `vặt lông người dùng` |
| `护城河` -> `hào nước quanh thành` | `技术护城河 (Moat)` | `lợi thế độc quyền` / `hào phòng thủ công nghệ` |
| `内卷` -> `cuộn vào trong` | `内卷` | `cạnh tranh khốc liệt` / `đua nhau đốt sức` |
| `草台班子` -> `gánh hát cỏ` | `草台班子` | `đội ngũ chắp vá` / `tổ chức nghiệp dư` |
| `白嫖` -> `chơi chùa` | `白嫖算力 / 数据` | `xài chùa` / `khai thác miễn phí` |
| `闭源` vs `开源` | `闭源` / `开源` | `mã nguồn đóng` / `mã nguồn mở` |

---

## 3. How to Populate `01_visual_audit_notes.md` (Mandatory Gate Format)

`verify_stage_1.py` parses `01_visual_audit_notes.md` and verifies that **every single frame** listed in `data/01_extracted_audit_manifest.json` (`>= 10` frames, prioritizing Pass 2B recovered timestamps + stratified checkpoints) has a documented row. Use this exact Markdown table format:

```markdown
| Timestamp (s) | Frame File | Raw Whisper ASR Text | Visual On-Screen Context / Text | Corrected Term & Notes |
| :--- | :--- | :--- | :--- | :--- |
| `18.0s` | `frame_0018s.jpg` | `Ancelope 中转站` | Slide: `批量创建账号 / 第三方中转 / Anthropic` | Fix `Ancelope` -> `Anthropic`; translate `第三方中转` as `trạm trung chuyển bên thứ ba` |
| `113.0s` | `frame_0113s.jpg` | `user标签 end标签` | Red pencil pointing at `<\|end\|>` after `<\|user\|>` and `<\|assistant\|>` | Keep 3 separate short clauses locked to `108.1s`, `110.0s`, `111.8s` so each tag matches the pointer |
```
