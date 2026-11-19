# Chinese-to-Vietnamese Syntax Mapping, Visual Anchor Locking & Prosody Guide

Translating video narration from Chinese (`zh`) to Vietnamese (`vi`) is fundamentally different from translating static articles. Spoken video translation is constrained by **three simultaneous locks**:
1. **Visual Anchor Lock:** When an object, word, or diagram appears on screen at timestamp $t$, the Vietnamese word naming it must be spoken at timestamp $t$.
2. **Acoustic Slot Lock:** The spoken Vietnamese syllables must fit naturally inside `[slot_start, slot_end]` (`4.5–5.2 syllables/second`) with room for a `~0.22s` breathing pause.
3. **Phonetic G2P Lock:** `tts_text` must feed clean Vietnamese orthography plus `<en>...</en>` tags into `VieNeu-TTS` (`sea-g2p`) with punctuation placed to sculpt natural pitch and pauses.

---

## 1. Visual Anchor Locking: Solving the Head-Final vs. Head-Initial Inversion Trap

### The Linguistic Problem
Chinese is **head-final** in noun phrases, relative clauses, and prepositional modifiers:
- **Chinese order:** `在 [Modifier A] 的 [Modifier B] 中，通过 [Method C] 来 [Action D]`
- **Standard textbook Vietnamese order (Inverted!):** `[Action D] bằng [Method C] trong [Modifier B] của [Modifier A]`

If a 6-second video segment shows animation `A -> B -> C -> D` in chronological order and you merge the clauses and invert them into standard textbook Vietnamese `D -> C -> B -> A`:
- At $t=0\text{s}$ (screen shows `A`), the narrator speaks `D`.
- At $t=5\text{s}$ (screen shows `D`), the narrator speaks `A`.
- **Result:** Complete audio-visual desynchronization!

### The Solution: Clause-Preserving Progressive Syntax
Keep the short `1.5s–3.5s` clause boundaries (`A`, `B`, `C`, `D`) and use **progressive Vietnamese connectors** so the Vietnamese clause order stays 1:1 with the visual sequence on screen:

| Chinese Visual Sequence | Bad Inverted Paragraph (Desyncs Video!) | Good Clause-Locked Progressive Vietnamese (100% Visual Sync!) |
| :--- | :--- | :--- |
| **Cue 1 (`104.2s–108.1s`):**<br/>`在标注员准备好的结构化对话数据里` | *(Merged 10s sentence):*<br/>"Mô hình sẽ dừng xuất văn bản khi gặp thẻ End, và đặt thẻ Assistant sau thẻ User cùng câu hỏi trong bộ dữ liệu hội thoại có cấu trúc mà nhân viên gán nhãn đã chuẩn bị sẵn." | **Cue 1 (`104.2s–108.1s`):**<br/>"Trong dữ liệu hội thoại có cấu trúc mà nhân viên gán nhãn chuẩn bị sẵn," |
| **Cue 2 (`108.1s–111.7s`):**<br/>`模型就会学到在user标签和问题后面是assistant标签` | *(Pencil points at `<|user|>` then `<|assistant|>`, but audio is talking about something else)* | **Cue 2 (`108.1s–111.7s`):**<br/>"mô hình sẽ học được rằng: sau thẻ User và câu hỏi là thẻ Assistant," |
| **Cue 3 (`111.7s–114.2s`):**<br/>`遇到end标签就要停止输出` | *(Pencil points at `<|end|>`, but audio is talking about "nhân viên gán nhãn")* | **Cue 3 (`111.7s–114.2s`):**<br/>"và gặp thẻ End thì phải dừng xuất văn bản." |

---

## 2. Syllable-Budget Condensation Techniques (Fitting Dense Chinese into Natural Vietnamese)

Chinese packs dense meaning into 2-character verbs and 4-character idioms (`四字成语`). Literal translation into Vietnamese often inflates syllable count by `35%–50%`, forcing robotic `atempo > 1.35x` speedups. Use these 4 condensation rules to keep syllable rate at **`4.5–5.2 syllables/second`**:

1. **Strip Bureaucratic Nominalizations:**
   - ❌ Verbose (`14 syllables`): *"tiến hành việc thực hiện quá trình thu thập dữ liệu"*
   - ✅ Punchy (`5 syllables`): *"thu thập dữ liệu"* or *"quét dữ liệu"*
2. **Condense Chinese 4-Character Idioms (`成语`) into Idiomatic Vietnamese:**
   - `归根结底` -> ❌ *"quy về gốc rễ đến tận cùng"* (`7 syl`) -> ✅ *"suy cho cùng"* (`3 syl`)
   - `显而易见` -> ❌ *"hiển nhiên và dễ dàng nhìn thấy"* (`7 syl`) -> ✅ *"rõ ràng là"* (`3 syl`)
   - `高枕无忧` -> ❌ *"gối cao đầu mà không lo lắng"* (`7 syl`) -> ✅ *"kê cao gối ngủ kỹ"* (`5 syl`)
   - `炉火纯青` -> ❌ *"lửa lò thuần một màu xanh"* (`6 syl`) -> ✅ *"điêu luyện"* (`2 syl`)
   - `一针见血` -> ❌ *"một mũi kim nhìn thấy máu"* (`6 syl`) -> ✅ *"trúng tim đen"* / *"thẳng vào trọng tâm"* (`3–4 syl`)
3. **Prune Redundant Discourse Fillers When Slot is Tight (`< 1.8s`):**
   - Chinese speakers frequently chain fillers like `那么其实我们知道说白了就是...` ("Vậy thì thực ra chúng ta biết rằng nói trắng ra chính là...").
   - Condense to a single sharp transition: *"Nói thẳng ra,"* (`3 syl`) or *"Thực chất,"* (`2 syl`).

---

## 3. Genre Register & Pronoun Matrix

### A. Tech Review / Analytical Essay (`tech-review`)
- **Narrator Self-Reference:** `mình` or `chúng ta` (inclusive), or objective third-person.
- **Audience Address:** `bạn` / `các bạn` / `anh em`.
- **Style:** Sharp, analytical, slightly witty, zero awkward translationese (`"mùi vị giám sát"`, `"bò sát mạng"`).

### B. Short Drama / Multi-Character Storytelling (`short-drama`)
Lock character pronoun pairs in `02_glossary_and_pronunciation.md` before translating Episode 1 so relationships never flip mid-scene:
- **CEO / Superior <-> Subordinate:** `tôi` – `cậu / cô` (or `Ngài / Chủ tịch` – `tôi`)
- **Married / Romantic Couple:** `anh` – `em` (cold/estranged phase: `tôi` – `cô / anh`)
- **Hostile Rivals:** `tôi / tao` – `mày / các người / hắn`
- **Family (Parent <-> Child):** `bố / mẹ` – `con`

---

## 4. Punctuation Engineering for `VieNeu-TTS` Prosody

In `tts_text`, punctuation directly controls the acoustic pause duration and intonation curve generated by `VieNeu-TTS`:
- **Comma `,`**: Inserts a natural `120ms–180ms` micro-pause with continuation pitch. Use after introductory clauses and before `<en>...</en>` tags if a slight boundary helps clarity.
- **Colon `:` or Semicolon `;`**: Inserts a `200ms–260ms` explanatory pause. Note: In `tts_text`, replacing `:` with `,` often produces smoother continuous flow when the time slot is tight (`< 2.2s`).
- **Question Mark `?`**: Raises terminal pitch contour naturally for rhetorical questions (*"Vì sao lại thế?"*, *"Có thật vậy không?"*).
- **Exclamation Mark `!`**: Adds emphatic energy on punchlines (*"Vì đốt tiền không nổi!"*).
- **Never use double commas `,,` or raw brackets `()` / `[]` in `tts_text`:** Parenthetical explanations in `sub_text` (e.g., `tinh chỉnh có giám sát (SFT)`) should be written cleanly without parentheses in `tts_text` (`tinh chỉnh có giám sát <en>S F T</en>`).
