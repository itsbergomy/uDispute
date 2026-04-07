# o3 Letter Batching Optimization — Work Log

## Date: April 6, 2026

---

## 1. ANALYSIS — Current State

### Bottleneck Location
`services/pipeline_engine.py` → `handle_generation()` (lines 752-845)

### How It Works Now
- For each dispute round, the pipeline fetches all pending `DisputeAccount` records
- **For each account, sequentially:**
  1. Builds prompt via `build_prompt()`
  2. Calls `generate_letter_with_quality_gate()` → which calls `generate_letter()` → which calls `openai_client.chat.completions.create(model="o3")`
  3. Quality gate runs (pure Python, <50ms)
  4. If quality fails, retries up to 2x (more API calls)
  5. Result appended to `generated_letters[]`
- After ALL letters done, saves to DB in batch

### The Problem
- Each o3 API call takes ~20-60 seconds
- 5 accounts = 100-300 seconds **minimum** (sequential)
- Quality gate retries can add 1-2 more API calls per letter
- `generate_dual_letters()` also makes 2 sequential calls

### What We Changed
- Added `generate_letter_async()` using OpenAI's `AsyncOpenAI` client
- Added `generate_letters_batch()` that fires all letters concurrently via `asyncio.gather()`
- Modified `handle_generation()` to use the batch function
- Added `generate_dual_letters_async()` for parallel dual-letter generation

### What We Did NOT Change
- Quality gate logic (pure Python, stays sync — it's fast)
- Prompt building (stays sync — no API calls)
- Database operations (stay sync — Flask-SQLAlchemy isn't async)
- Blueprint route handlers (stay sync — Flask isn't async)

### Files Modified
1. `services/letter_generator.py` — added async functions + `AsyncOpenAI` client
2. `services/pipeline_engine.py` — replaced sequential loop with batch generation

---

## 2. IMPLEMENTATION

### Phase 1: letter_generator.py changes

**New imports:**
- `import asyncio`
- `from openai import AsyncOpenAI`
- `async_openai_client = AsyncOpenAI()`

**New functions (all async):**
- `generate_letter_async()` — mirrors `generate_letter()`, uses `await async_openai_client.chat.completions.create()`
- `generate_letter_with_quality_gate_async()` — mirrors sync version, calls `generate_letter_async()`, quality gate stays sync (it's pure Python, <50ms)
- `generate_letters_batch(tasks)` — takes list of task dicts, fires all concurrently via `asyncio.gather()`, returns list of results with error isolation per task
- `generate_dual_letters_async()` — fires CRA + furnisher letters concurrently via `asyncio.gather()`

### Phase 2: pipeline_engine.py changes

**Architecture change in `handle_generation()`:**

OLD (sequential):
```
for each account:
    build prompt
    call API (blocking ~30s)
    quality gate
    append result
```

NEW (parallel):
```
Phase 2a: Build ALL prompts (sync loop, fast)
Phase 2b: Fire ALL API calls concurrently via asyncio.run(generate_letters_batch())
Phase 2c: Process results (quality gate already ran inside async tasks)
```

Key detail: `asyncio.run()` is used in the pipeline engine (which runs in a sync Flask context) to bridge into the async world. Each task inside `generate_letters_batch` runs its own quality gate loop independently.

---

## 3. DEBUG LOG

### Entry 1: Initial parallel-only test
- **Command:** `python autoresearch_batching.py --sample 3 --parallel-only`
- **Result:** 3 letters in 49.6s, all quality=100
- **Bug:** `KeyError: 'elapsed_seconds'` in format_results when running parallel-only (no sequential baseline to compare). Fixed by using `.get()`.

### Entry 2: Full sequential vs parallel comparison
- **Command:** `python autoresearch_batching.py --sample 3`
- **Sequential:** 106.5s (27.1s + 40.2s + 39.3s)
- **Parallel:** 58.5s (all 3 concurrent, bottlenecked by slowest single letter)
- **Speedup:** 1.82x
- **Quality:** All 6 letters scored 100/100 on quality gate
- **LLM Judge Composite Score:** 8.83/10
  - correctness: 10/10
  - error_handling: 10/10
  - quality_gate_pass_rate: 10/10
  - completeness: 10/10
  - consistency: 8/10
  - speedup: 5/10 (expected — 3 tasks, bottlenecked by slowest)

### Entry 3: Scaling analysis
- With 3 tasks, speedup is limited to ~2x (bottleneck = slowest task)
- With 5-10 tasks, speedup should approach 3-5x (more parallelism overlaps)
- OpenAI rate limits are the ceiling — burst of 10 concurrent o3 calls should be fine on standard tier

---

## 4. AUTORESEARCH HARNESS

**File:** `autoresearch_batching.py`

**What it tests:**
1. Parses PDF corpus to get real accounts
2. Builds prompts (no API calls)
3. Generates letters sequentially (baseline timing)
4. Generates same letters in parallel (test timing)
5. Compares: speedup, quality scores, error rates, completeness
6. LLM judge scores on 6 dimensions

**Scoring dimensions:**
- Speedup (1-10)
- Correctness (1-10)
- Error handling (1-10)
- Quality gate pass rate (1-10)
- Consistency (1-10)
- Completeness (1-10)

**Pass threshold:** Composite >= 8.0, 0 errors, quality identical

**Current score: 8.83/10 — PASSING**

---

## 5. REMAINING WORK

- [ ] Push to main and test on Render
- [ ] Monitor pipeline logs for the timing output: `[PIPELINE] Batch generation complete: N letters in Xs`
- [ ] Consider adding the same parallelization to blueprint routes (business.py, disputes.py) for interactive letter generation — lower priority since those are single-letter calls
