# Visual Keyframe Audit & ASR Disambiguation Handbook

Whisper ASR models process only the audio waveform. In fast-paced Chinese videos (especially Bilibili tech essays, Douyin short dramas, and finance documentaries), audio-only transcription suffers from severe **homophone corruption** and **English acronym mangling**. Visual keyframe auditing resolves 100% of these ambiguities before a single line of Vietnamese is written.

---

## 1. The 5 Visual Layers to Inspect in Every Extracted Frame

When calling `read_file` on `frames/audit/frame_XXXXs.jpg`, systematically inspect all 5 visual zones:

| Visual Zone | What to Look For | Impact on Localization |
| :--- | :--- | :--- |
| **Zone 1: Bottom Burned-In Hardsub (`y=[880..1080]`)** | Exact Chinese characters (`汉字`) and English tokens printed at the bottom. | Overrides phonetic Whisper hallucinations (`Ancelope` -> `Anthropic`, `DPSK21` -> `DeepSeek-R1`). |
| **Zone 2: Center Canvas / Slides / UI Pointers** | Highlighted text, red pencil circles, cursor clicks, code blocks, tweets, news clippings (e.g., `<|user|>`, `<|assistant|>`, `<|end|>`, `Fire-Flyer 3FS`). | Locks **Visual Anchor Timing**: ensures the Vietnamese translation names the exact UI element at the exact second the pointer touches it. |
| **Zone 3: Floating Impact Text (`花字` / Callouts)** | Large colored pop-up banners in the center or upper corners (`批量创建账号`, `监督微调`). | Confirms the structural section heading and technical register of the current scene. |
| **Zone 4: Lower-Thirds & Speaker Nameplates** | Chyrons introducing interviewees, CEOs, or characters (`黄仁勋`, `Dario Amodei`). | Prevents transliterating proper names into nonsense syllables. |
| **Zone 5: Hardsub Geometry & Background Luminance** | Is the hardsub 1 line or 2 lines tall? Does it sit over a dark studio background or a bright white paper slide? | Validates that `01_zh_bbox_scan.json` captured the true text width (`x0..x1`) rather than false-triggering on a white slide background. |

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

### B. Business, Finance & Internet Culture Traps
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

`verify_stage_1.py` parses `01_visual_audit_notes.md` and verifies that **every single frame** listed in `data/01_extracted_audit_manifest.json` has a documented row. Use this exact Markdown table format:

```markdown
| Timestamp (s) | Frame File | Raw Whisper ASR Text | Visual On-Screen Text / Graphics | Corrected Term & Context |
| :--- | :--- | :--- | :--- | :--- |
| `18.0s` | `frame_0018s.jpg` | `Ancelope 中转站` | Slide: `批量创建账号 / 第三方中转 / Anthropic` | Fix `Ancelope` -> `Anthropic`; translate `第三方中转` as `trạm trung chuyển bên thứ ba` |
| `113.0s` | `frame_0113s.jpg` | `user标签 end标签` | Red pencil pointing at `<|end|>` after `<|user|>` and `<|assistant|>` | Keep 3 separate short clauses locked to `108.1s`, `110.0s`, `111.8s` so each tag matches the pencil |
```
