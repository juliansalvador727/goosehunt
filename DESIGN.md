# Design Notes — goosehunt

Decisions, tradeoffs, and implementation notes for each component. Updated as we build.

---

## Scraper

### Why Playwright

WaterlooWorks renders postings via JavaScript — raw HTTP won't give you posting content. Playwright runs a real Chromium instance and lets us call WW's own in-page JS functions directly via `page.evaluate()`, which is both simpler and more reliable than trying to replicate the AJAX calls ourselves.

### Persistent profile

`playwright.chromium.launch_persistent_context(user_data_dir="scraper/profile/")` keeps cookies and session state across runs. You Duo-authenticate once; subsequent runs reuse the session until it expires. The `profile/` directory is gitignored so credentials don't leak.

### WW JavaScript API

WaterlooWorks exposes several global functions on the jobs page that the scraper calls directly:

**`window.getPostingOverview(postingId, callback)`** — fires a `$.post` to `/myAccount/co-op/direct/jobs.htm` with the posting's action key and returns the full posting HTML via callback. No new tab is opened.

**DataViewer POST** — the job listing is backed by a data viewer component. Its `dataParams.action` key (a long encoded string embedded in the page's `<script>` tags) is used to POST to the current page URL with `isDataViewer: true`, returning JSON rows of job IDs. Supports pagination at 100 per page.

### Action key extraction

There are dozens of `_-_-...` action strings on the page for different endpoints. The scraper specifically targets the `dataParams` block:

```javascript
const m = t.match(
  /dataParams\s*:\s*\{[^}]*action\s*:\s*['"](_-_-[^'"]{20,})['"]/,
);
```

This avoids accidentally picking up `getPostingOverview`'s action string (a different endpoint) or any other unrelated action.

### Posting HTML parsing

`getPostingOverview` returns HTML that uses `<div class="tag__key-value-list">` containers — not `<table>` elements. Each container has a `<span class="label">` (field name) and a `<p>` (value). The parser queries these directly:

```javascript
div.querySelectorAll(".tag__key-value-list").forEach((container) => {
  const label = container
    .querySelector(".label")
    ?.textContent?.trim()
    ?.replace(/:$/, "");
  const value = container.querySelector("p")?.textContent?.trim();
  if (label && value) fields[label] = value;
});
```

The full label→value dict is stored as `raw_fields_json` so schema changes don't require re-scraping.

### Field mapping

Known fields are pulled from the raw dict by substring match on the label (case-insensitive). This tolerates minor label variations across posting types:

```python
FIELD_MAP = {
    "title":            ["job title", "position title", "title"],
    "org":              ["organization", "employer", "company name"],
    "location":         ["job - city", "city", "region", "work location"],
    "deadline":         ["deadline", "application deadline", "apply by"],
    ...
}
```

### Resumability

`data/postings.jsonl` is append-only. On each run `load_done()` reads all existing job IDs from it; any ID already present is skipped. A crashed scrape loses at most one posting.

### Politeness

```python
await asyncio.sleep(random.uniform(1.5, 3.5))
```

Single-threaded, no concurrent requests. Random delay between postings. Manual login at startup — no credential storage in code.

### Diagnostic mode

`make scrape-diag` (`--diag` flag) fetches one posting, dumps the raw HTML and parsed fields to `data/diag.md`. Useful for debugging parsing regressions after WW UI updates.

---

## Database

### Why SQLite

Zero infrastructure. The entire corpus of WW postings for one term is small (hundreds to low thousands of rows — Employer Direct is ~300–500, Full-Cycle adds another 500–1000 if we ever enable it). SQLite is sufficient and the `.db` file is easy to inspect with any SQLite browser.

### Schema decisions

- `job_id TEXT PRIMARY KEY` — WW job IDs are numeric strings; TEXT avoids leading-zero issues.
- `raw_fields_json TEXT` — preserves the full label→value dict from the posting HTML. Future re-processing doesn't require re-scraping.
- `deadline` vs `deadline_iso` — raw deadline comes from the scraper with whitespace garbage (`"Jun 2, 2026\n\t\t\t\t\t\n\t\t\t\t\t\t11:00 PM"`). Keep the raw string for display fidelity; populate `deadline_iso` during ingest via `dateutil.parser` for sorting and filtering.
- `embedding BLOB` — `np.float32` array of shape (384,) serialized via `.tobytes()`. Decoded on read with `np.frombuffer(blob, dtype=np.float32)`.
- `score_*` columns are REAL, nullable — scores are populated by the scorer after ingestion, so freshly scraped rows have NULL scores until `make embed` and `make score` run.
- `scraped_at` vs `updated_at` — `scraped_at` is set once on first insert; `updated_at` is bumped on every upsert.

### Upsert strategy

For each record, `ingest.py` pre-checks existence with a `SELECT`, then:

- **Not found** → `INSERT` all content fields; `embedding` and `score_*` are not included so they default to NULL.
- **Found, `raw_fields_json` unchanged** → skip (counts as "no change").
- **Found, content differs** → `UPDATE` content fields only; `scraped_at`, `embedding`, and `score_*` are not touched.

This approach (pre-check + compare) rather than `ON CONFLICT DO UPDATE` enables a clean three-way summary: inserted / updated / skipped.

Re-runs refresh posting content without nuking embeddings or scores.

### llm_evals table

Reserved for future on-demand LLM evaluation (user clicks a posting, gets a structured fit analysis cached by `(resume_hash, job_id)`). Table is created empty up front to avoid migration later. Not used in v1 of the UI.

---

## Embeddings

### Why local sentence-transformers

- No API key, no rate limits, no per-call cost. The corpus is small enough that any inference latency is dominated by I/O anyway.
- `all-MiniLM-L6-v2` is 384-dim, ~80MB on disk, runs on CPU in tens of milliseconds per posting. Cached in `~/.cache/huggingface/` after first download.
- Reproducible: the same input always gives the same vector, which matters when debugging score drift.

### What we embed

Per posting, the input string is:

```
title + " " + title + " " + title + " " + summary + " " + responsibilities + " " + required_skills
```

The title is repeated 3× to weight it more heavily in the embedding. This is a known trick for short documents — the title is the highest-signal field and otherwise gets drowned out by boilerplate summary text (especially common in postings where the "summary" is mostly the employer's marketing copy). Empirically, this gives noticeably better resume-to-posting matching than concatenation without weighting.

### Storage

- `np.float32(vec).tobytes()` → BLOB column. ~1.5KB per row, ~3MB total for 2000 rows.
- Reading: `np.frombuffer(blob, dtype=np.float32)`.
- On scorer startup, load every row's embedding into a single `(N, 384)` numpy array. One matmul against the resume vector gives all scores in ~5ms.

### Re-embedding

If we change the model or the input text strategy, we need to re-embed everything. The `embed_postings.py` script is idempotent and re-embeds only rows where `embedding IS NULL` by default, with a `--force` flag to re-embed all rows. Re-embedding 2000 postings from scratch takes ~30s on CPU.

### Why not pgvector / FAISS / chromadb

- 2000 rows × 384 dims = 3MB in memory. Exact cosine similarity is a single numpy matmul, ~5ms.
- HNSW or IVFFlat indexes are overhead at this scale, not speedup. They exist for million-row corpora where O(N) is too slow.
- One source of truth: SQLite. No sync layer between vector store and relational data.
- Zero infrastructure: no Postgres instance, no Supabase account, no Docker, no migrations. The whole thing is a `.db` file.

---

## Classifier

### Keyword scorer

Each role has a list of positive keywords in `config/roles.yaml`. Score = number of keyword hits in the concatenated text fields (`title + org + summary + responsibilities + required_skills`), normalized to [0, 1] by dividing by the max observed score across the corpus. Simple but tunable.

```yaml
firmware:
  keywords:
    - firmware
    - bare-metal
    - bootloader
    - RTOS
    - FreeRTOS
    - embedded C
    - device driver
    - interrupt handler
    - microcontroller
    - MCU
    - SPI bus
    - I2C
    - UART
    - CAN bus
```

### Why not an LLM for v1

Keyword scoring is transparent, instant, and free. You can look at a posting's score and immediately know which keywords fired. LLM classification would be slower, cost money per run, and be harder to debug. The YAML config makes tuning straightforward — add a keyword, re-run `make score`.

### Resume cosine-sim scorer

- Extract text from `resume.pdf` via pdfplumber (`resume/parser.py`).
- Embed the resume text once using the same `all-MiniLM-L6-v2` model (`embed/embed_resume.py`).
- Load all posting embeddings from the DB into one `(N, 384)` numpy array.
- `scores = matrix @ resume_vec` — one matmul. Both sides are unit vectors (sentence-transformers normalizes by default), so cosine similarity reduces to dot product.
- Write back to `score_resume`.

This rewards postings whose semantic content (responsibilities, required skills) is similar to your resume's content — not just literal word overlap. A posting titled "Mission Software Engineer" can score highly against a resume mentioning "forward-deployed work" even with zero shared keywords, because the embedding space captures the semantic relationship.

Re-scoring after updating your resume takes <1s. The classifier and the resume scorer are decoupled: `make score` runs both, but you can rerun the resume scorer alone after editing your resume.

### Why semantic over TF-IDF

We considered TF-IDF early on. The case against:

- TF-IDF rewards literal vocabulary overlap. Brittle for roles like FDE/MTS where titles vary wildly across companies and the relevant signal is conceptual, not lexical.
- Embeddings capture "Mission Software Engineer ≈ Forward Deployed Engineer ≈ Member of Technical Staff" even with no shared tokens.
- For technical-skill matches (firmware, embedded), the keyword classifier already handles literal-overlap cases — having the resume scorer do the same thing is redundant.

The semantic scorer complements the keyword classifier instead of duplicating it.

---

## Web UI

### Why FastAPI + Alpine.js

- FastAPI: minimal boilerplate, automatic OpenAPI docs, serves static files trivially.
- Alpine.js: reactive without a build step. Loaded from CDN. The entire UI is a single `index.html`.

### No server-side pagination

The full corpus fits in one JSON response (a few MB at most, embeddings excluded). Client-side filtering and sorting via Alpine.js is instant for this data size. Eliminating pagination removes complexity on both the server and client.

### `/api/postings` response shape

```json
[
  {
    "job_id": "12345",
    "board_type": "direct",
    "title": "Firmware Engineer",
    "org": "Some Corp",
    "location": "Waterloo",
    "deadline": "Jun 1, 2026",
    "deadline_iso": "2026-06-01T23:00:00",
    "score_firmware": 0.85,
    "score_software": 0.12,
    "score_resume": 0.43,
    ...
  }
]
```

The `embedding` column is never sent to the client — it's only needed server-side for scoring.

### UI features

- Search box: filters on `title`, `org`, `location` (client-side, instant).
- Role checkboxes: show only postings with score > threshold for checked roles.
- Column headers: click to sort ascending/descending. Default sort: `score_resume` desc.
- Color coding: high scores get a green tint, low scores grey.
- Each row shows the `job_id` so you can cross-reference on WaterlooWorks directly.
- Click a posting title: expands an inline detail panel (▸/▾ indicator) showing Summary, Responsibilities, and Required Skills — each truncated to 500 chars. One panel open at a time; click again to collapse.

---

## Docker

### What's in the image

`python:3.12-slim` base. CPU-only PyTorch is installed first (via `--index-url https://download.pytorch.org/whl/cpu`) before `requirements.txt`, so sentence-transformers doesn't pull the CUDA build (~750 MB vs ~3 GB). The Playwright Python package is installed (it's in `requirements.txt`) but the Chromium binary is not — the scraper cannot run inside the container.

### What's not in the image

`data/` and `resume.pdf` are excluded from the image via `.dockerignore` and bind-mounted at runtime. The HuggingFace model cache is a named Docker volume (`hf_cache`) so the model survives container recreates without re-downloading.

### Startup sequence

`docker-entrypoint.sh` runs the full pipeline on every container start, then hands off to uvicorn:

```
ingest → embed_postings → scorer → embed_resume → uvicorn
```

All pipeline steps are idempotent, so re-running them on an already-populated DB is fast (seconds). The `exec` before uvicorn ensures it gets PID 1 and receives signals cleanly.

### Scraper constraint

The scraper needs a headed Chromium window with a persistent WaterlooWorks session (`scraper/profile/`). This cannot run in Docker without X11 forwarding or VNC, and the session is tied to the local machine anyway. The workflow is: scrape locally with `make scrape`, then run the container anywhere with `data/postings.jsonl` bind-mounted in.

### Why not Dockerize the scraper

- WW bot detection is best handled by headed + persistent profile, which requires a real display
- The session cookies live in `scraper/profile/` on the local machine — moving them into a container adds complexity for no benefit
- The corpus is scraped once per term, not continuously; there's no operational reason to run the scraper on a remote device

---

## Data flow summary

```
WaterlooWorks (browser)
        │  Playwright + WW JS API
        │    getPostingOverview(jobId) × N
        ▼
data/postings.jsonl     ← raw scraped data
        │  db/ingest.py
        ▼
data/postings.db        ← SQLite (embedding NULL, score_* NULL)
        │  embed/embed_postings.py
        ▼
data/postings.db        ← embedding column populated
        │  classifier/scorer.py + embed/embed_resume.py
        ▼
data/postings.db        ← score_* columns populated
        │  web/main.py
        ▼
localhost:8000          ← FastAPI + Alpine.js UI
```

---

## What we're NOT doing (and why)

| Skipped feature                         | Reason                                                                             |
| --------------------------------------- | ---------------------------------------------------------------------------------- |
| Headless mode                           | WW has bot detection; persistent profile + headed = safest                         |
| Parallel scraping                       | Unnecessary for this corpus size; increases detection risk                         |
| Full-Cycle board                        | Employer Direct is sufficient for v1; Full-Cycle adds complexity for marginal gain |
| Model-based classification              | Keyword scoring is sufficient for v1; LLM adds cost and latency                    |
| Vector database (pgvector, FAISS, etc.) | 2000 × 384 fits in 3MB; numpy matmul is faster than any index at this scale        |
| OpenAI/Anthropic embeddings             | Local model is free, offline, and quality difference is small for this corpus      |
| Auth / multi-user                       | Personal tool; single local user                                                   |
| Public deployment                       | Personal tool; Docker is for local reproducibility, not public hosting             |
| On-demand LLM evaluation in v1          | `llm_evals` table is reserved for later; not wired into the UI yet                 |
