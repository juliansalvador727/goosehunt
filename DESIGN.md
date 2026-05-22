# Design Notes — goosehunt

Decisions, tradeoffs, and implementation notes for the current implementation.

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

The scraper starts from the currently visible WaterlooWorks listing page. Work term, board, and any other WW filters must be set manually before pressing Enter.

### Politeness

```python
await asyncio.sleep(random.uniform(0.8, 1.2))
```

Single-threaded, no concurrent requests. ~1s random delay between postings. Manual login at startup — no credential storage in code.

### Diagnostic mode

`make scrape-diag` (`--diag` flag) fetches one posting, dumps the raw HTML and parsed fields to `data/diag.md`. Useful for debugging parsing regressions after WW UI updates.

---

## Database

### Why SQLite

Zero infrastructure. The entire corpus of WW postings for one term is small (hundreds to low thousands of rows — Employer Direct is ~300–500). SQLite is sufficient and the `.db` file is easy to inspect with any SQLite browser.

### Schema decisions

- `job_id TEXT PRIMARY KEY` — WW job IDs are numeric strings; TEXT avoids leading-zero issues.
- `raw_fields_json TEXT` — preserves the full label→value dict from the posting HTML. Used at API request time to extract compensation, application method, and apply contact info without re-scraping.
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

Reserved for future on-demand LLM evaluation (user clicks a posting, gets a structured fit analysis cached by `(resume_hash, job_id)`). Table is created empty up front to avoid migration later. Not used in the current UI.

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

The title is repeated 3× to weight it more heavily in the embedding. This is a known trick for short documents — the title is the highest-signal field and otherwise gets drowned out by boilerplate summary text. Empirically, this gives noticeably better resume-to-posting matching than concatenation without weighting.

### Storage

- `np.float32(vec).tobytes()` → BLOB column. ~1.5KB per row, ~3MB total for 2000 rows.
- Reading: `np.frombuffer(blob, dtype=np.float32)`.
- On scorer startup, load every row's embedding into a single `(N, 384)` numpy array. One matmul against the resume vector gives all scores in ~5ms.

### Re-embedding

If we change the model or the input text strategy, we need to re-embed everything. The `embed_postings.py` script is idempotent and re-embeds only rows where `embedding IS NULL` by default, with a `--force` flag to re-embed all rows. Re-embedding 2000 postings from scratch takes ~30s on CPU.

### Why not pgvector / FAISS / chromadb

- A WaterlooWorks term is small enough that all 384-dim posting embeddings fit comfortably in memory. Exact cosine similarity is a single numpy matmul.
- HNSW or IVFFlat indexes are overhead at this scale, not speedup. They exist for million-row corpora where O(N) is too slow.
- One source of truth: no Postgres instance, no separate vector-store service, no migrations. The whole thing is a `.db` file.

---

## Classifier

### Keyword scorer

Each role has a list of positive keywords in `config/roles.yaml`. Score = number of keyword hits in the concatenated text fields (`title + org + summary + responsibilities + required_skills`), normalized to [0, 1] by dividing by the max observed score across the corpus. Simple but tunable.

Four roles are currently active:

| Role       | Label  | Coverage                                               |
|------------|--------|--------------------------------------------------------|
| `software` | SWE    | Backend, frontend, cloud, distributed systems          |
| `ai_ml`    | AI/ML  | ML engineering, deep learning, LLMs, data science      |
| `firmware` | FW     | Firmware, embedded, mechatronics, PCB design           |
| `hardware` | HW     | FPGA, Verilog, circuit design, ASIC, signal integrity  |

Add/remove keywords in `config/roles.yaml` and re-run `make score` to retune.

### Why not an LLM for v1

Keyword scoring is transparent, instant, and free. You can look at a posting's score and immediately know which keywords fired — the UI shows matched keywords directly in the detail panel. LLM classification would be slower, cost money per run, and be harder to debug.

### Resume cosine-sim scorer

- Extract text from `resume.pdf` via pdfplumber (`resume/parser.py`).
- Embed the resume text once using the same `all-MiniLM-L6-v2` model (`embed/embed_resume.py`).
- Load all posting embeddings from the DB into one `(N, 384)` numpy array.
- `scores = matrix @ resume_vec` — one matmul. Both sides are unit vectors (sentence-transformers normalizes by default), so cosine similarity reduces to dot product.
- Write back to `score_resume`.

This rewards postings whose semantic content is similar to your resume's content — not just literal word overlap.

Re-scoring after updating your resume takes <1s. The classifier and the resume scorer are decoupled: `make score` runs both, but you can rerun the resume scorer alone after editing your resume.

### Why semantic over TF-IDF

TF-IDF rewards literal vocabulary overlap. Brittle for roles where titles vary wildly across companies and the relevant signal is conceptual, not lexical. Embeddings capture semantic similarity even with no shared tokens. The semantic scorer complements the keyword classifier instead of duplicating it.

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
    "job_id": "472013",
    "board_type": "direct",
    "title": "Firmware Engineer",
    "org": "Some Corp",
    "location": "Waterloo",
    "deadline": "Jun 1, 2026\n\t\t\t11:59 PM",
    "deadline_iso": "2026-06-01T23:59:00",
    "work_term": "2026 - Fall",
    "openings": 1,
    "summary": "...",
    "responsibilities": "...",
    "required_skills": "...",
    "scraped_at": "2026-05-20T04:12:00Z",
    "updated_at": "2026-05-20T04:12:00Z",
    "score_software": 0.12,
    "score_ai_ml": 0.03,
    "score_firmware": 0.85,
    "score_hardware": 0.10,
    "score_resume": 0.43,
    "comp_hourly": 26.49,
    "comp_score": 0.238,
    "apply_method": "email",
    "apply_email": "careers@example.com",
    "apply_link": null,
    "keyword_hits": {
      "software": [],
      "ai_ml": [],
      "firmware": ["firmware", "rtos", "spi"],
      "hardware": ["pcb"]
    }
  }
]
```

`embedding` and `raw_fields_json` are never sent to the client. `comp_hourly`, `comp_score`, `apply_*`, and `keyword_hits` are computed at request time from the DB row.

### Request-time enrichment (web/main.py)

Three enrichment passes run over each row before it's returned:

**Compensation parsing** — parses the free-text `Compensation and Benefits` field from `raw_fields_json` using a prioritized regex pipeline:

1. Hourly (most common): `$25–$29 hourly`, `$27/hr`, `Hourly Rate: 20.29–24.11`, `The hourly wage…is $26.49`
2. Bi-weekly: `$2,045 – $2,523 / bi-weekly` → ÷ 80 hrs
3. Weekly: `$1,600 per week` → ÷ 40 hrs
4. Monthly: `$4,264 to $5,200 per month` → ÷ 173.3 hrs
5. Annual: `$45,000 – $55,000 per year` → ÷ 2,080 hrs
6. Fallback: bare `$X – $Y` where midpoint is in [10, 100] → treated as hourly

Result is `comp_hourly` (est. $/hr). `comp_score` normalizes to [0, 1]: $16/hr → 0.0, $60/hr → 1.0.

**Application method detection** — inspects `Application Delivery`, `If By Email, Send To`, `If By Website, Go To`, and `Additional Application Information` fields from `raw_fields_json`:

- `apply_method`: `"email"` if delivery is by email or an email address is present; `"link"` if delivery is by website or a URL is found in additional info; `"ww"` otherwise (apply through WaterlooWorks only).
- `apply_email`: value of the `If By Email, Send To` field, or null.
- `apply_link`: explicit website field first, otherwise the first `http(s)://` URL found in additional application info, or null.

**Keyword hit extraction** — `config/roles.yaml` is loaded once at startup. For each posting, the concatenated text is scanned for each role's keyword list. Returns `keyword_hits`: a dict mapping each role to the list of keywords that matched.

### UI

Two-pane layout: compact sortable table on the left, sticky detail panel on the right.

**Header controls:**

- Board dropdown: `Employer Direct` and `Full Cycle` options exist in the UI model, but only `Employer Direct` is populated by the current scraper (`board_type = "direct"`).
- Search box: instant client-side filter on title, org, location, summary, responsibilities, required skills, and job ID.
- Role chips (SWE, AI/ML, FW, HW): toggle to show only postings with a non-zero score for that role. Multiple roles are OR'd.
- Apply by chips (Email, Link): filter to postings that require applying by email or external URL. Stacks with role filters. Both are enabled by default, so `apply_method = "ww"` postings are hidden in the current UI.
- Posting count: shows `filtered / total`.
- Ctrl+K button: opens the command palette.

**Table:**

- All columns sortable (click header). Default sort: `score_resume` desc.
- Score cells color-coded: green tint scales with score, grey for null/zero.
- Pay column: displays estimated hourly as `$26/h`; hover tooltip shows full `est. $26.49/hr`.
- Job ID column: click to copy to clipboard.

**Keyboard shortcuts:**

| Key      | Action                  |
|----------|-------------------------|
| `j` / `k` | Navigate rows          |
| `/`      | Focus search            |
| `Esc`    | Blur search             |
| `c`      | Copy selected job ID    |
| `e`      | Copy apply email        |
| `Shift+S` | Sort by resume score   |
| `Shift+P` | Sort by pay            |
| `Ctrl+K` | Open command palette    |

**Detail panel:**

- Title, company, location, deadline, pay in the header.
- Score grid: one box per role + resume + pay, color-coded.
- Apply row with one-click copy for email jobs or direct link for external applications.
- WaterlooWorks-only postings, if surfaced by changing filters/UI, link back to the Employer Direct jobs page.
- Role-labeled keyword chips showing exactly which keywords fired and for which role.
- Scrollable summary, responsibilities, and required skills sections.

**Command palette (Ctrl+K):**

Searchable list of all actions: sort by any column, toggle any filter, copy job ID/email, clear search and filters.

---

## Docker

### What's in the image

`python:3.12-slim` base with uv copied from `ghcr.io/astral-sh/uv:latest`. CPU-only PyTorch is installed first (via `--index-url https://download.pytorch.org/whl/cpu`) before `requirements.txt`, so sentence-transformers doesn't pull the CUDA build (~750 MB vs ~3 GB). uv's `--system` flag installs directly into the image Python without a venv. The Playwright Python package is installed (it's in `requirements.txt`) but the Chromium binary is not — the scraper cannot run inside the container.

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
        │  web/main.py  (+ request-time enrichment)
        ▼
localhost:8000          ← FastAPI + Alpine.js UI
```

---

## What we're NOT doing (and why)

| Skipped feature                         | Reason                                                                             |
| --------------------------------------- | ---------------------------------------------------------------------------------- |
| Headless mode                           | WW has bot detection; persistent profile + headed = safest                         |
| Parallel scraping                       | Unnecessary for this corpus size; increases detection risk                         |
| Full-Cycle scraping                     | UI has a board selector placeholder, but the scraper currently writes only Employer Direct rows |
| Model-based classification              | Keyword scoring is transparent, instant, and tunable via YAML                      |
| Vector database (pgvector, FAISS, etc.) | The corpus is small; numpy matmul is simpler and fast enough                       |
| OpenAI/Anthropic embeddings             | Local model is free, offline, and quality difference is small for this corpus      |
| Auth / multi-user                       | Personal tool; single local user                                                   |
| Public deployment                       | Personal tool; Docker is for local reproducibility, not public hosting             |
| On-demand LLM evaluation                | `llm_evals` table is reserved for later; not wired into the UI yet                 |
