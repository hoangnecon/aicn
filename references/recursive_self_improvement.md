# Recursive Self-Improvement (RSI) Protocol for `aicn` (`v3.0.0`)

`aicn` is a self-improving skill: every upstream API breakage, QA/acoustic gate failure, user correction, or completed series must make the next run faster, more reliable, and less error-prone — **in executable code, tests, and hard gates, never just prose reminders**.

---

## 1. When the RSI Loop Triggers (Mandatory)

Run a brief RSI cycle whenever any of these 4 events occurs:
1. **Upstream / Tooling Failure:** An API endpoint changes, a downloader returns empty/403, or a dependency breaks (e.g., `openhongguo` returning HTTP 200 / 0 bytes).
2. **User Correction or Missing Deliverable:** The user has to ask twice for something that should have been automatic (e.g., missing Vietnamese translation/synopsis/thumbnails in a candidate list, or recommending stale series instead of fresh releases).
3. **Pipeline / QA Gate Failure:** A crash, audio chop/bleed defect, timestamp overlap, disk-full abort, or visual/stream QA defect caught in Stage 1–6.
4. **Full-Series Release Milestone:** Immediately after `aicn.py merge-series` succeeds and the master is delivered, review forensic timings, audio quality metrics, and bottleneck waits.

---

## 2. The 4-Step RSI Loop

```text
[1. Detect & Measure] -> [2. Root-Cause Audit] -> [3. Codify Guardrail] -> [4. Verify & Bump]
```

### Step 1 — Detect & Measure
Capture the exact failure or friction with **measured evidence** (command run, exit code, HTTP headers/status, file path, line number, timing, forensic WAV/JSON stats). Separate **Measured**, **Assumed**, and **Unknown**.

### Step 2 — Root-Cause Audit (Log to `LESSONS.md`)
Append a numbered entry (`L-NNN`) to `<skill>/LESSONS.md` containing:
- Date & context (series/stage/tool)
- Symptom (exact error, measured metric, or user feedback)
- Verified root cause (not a guess)
- Concrete guardrail implemented

### Step 3 — Codify into Executable Guardrails
A lesson is **NOT closed** until it is encoded into at least one durable mechanism so the agent cannot repeat the mistake:
- **Script / CLI fix:** Patch `scripts/aicn_platform.py`, `scripts/aicn.py`, `scripts/aicn_audio.py`, `scripts/hg_tool.cjs`, or `vendor/*/scripts/`.
- **Automated test:** Add a regression test in `tests/` (`unittest`) covering the exact edge case.
- **Hard contract / Red Flag:** Update `SKILL.md` and the relevant file under `references/` or `vendor/`.

### Step 4 — Verify, Commit & Version Bump
1. Run the full unit test suite:
   ```powershell
   # Windows:
   & $env:AICN_PY -m unittest discover -s <skill>\tests -v
   ```
   ```bash
   # macOS / Linux:
   /Users/Shared/Tools/VieNeu-TTS/.venv/bin/python -m unittest discover -s <skill>/tests -v
   ```
2. Run a live smoke test of any modified external integration or pipeline path (e.g., `hg_tool.cjs`, `aicn.py plan`, or audio/render benchmark).
3. Update `<skill>/CHANGELOG.md`, run `install.ps1` / `install.sh` to keep installed copies synchronized, and commit cleanly to the `aicn` git repo with a version tag (`vX.Y.Z`).

---

## 3. Anti-Patterns (What RSI Is NOT)
- **No prose-only "I will remember next time":** Memory resets across sessions; only code, tests, CLI flags, and `SKILL.md` contracts persist.
- **No silent scope creep:** Fix the actual root cause cleanly; do not refactor unrelated working stages during a hotfix.
- **No unverified upgrades:** Never bump a skill version without running tests and confirming measured pass output.
