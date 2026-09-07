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

**`window.getPostingOverview(postingId, callback)`** — fires a `$.post` to `/myAccount/co-op/direct/jobs.htm` with the posting's action key and returns the full posting HTML via callback. No per-posting tab is opened.

**`window.getPostingData(postingId, callback)`** — returns `{org, div, divId, geoData, ...}`. Only `divId` is used: it keys the employer's work term ratings report.

**`window.getWorkTermRatingReportJson(divId, callback)`** — returns `{sections: [...]}` for the posting's "Work Term Ratings" tab. Each section is a `table` (`columns`, `rows`), `pieChart` (`data: [{name, y}]`), or `barChart`/`columnChart` (`categories`, `series`). Titles carry the employer name (`"Hires by Faculty<br>Acme"`). Fetched once per division per run and stored raw under `_ratings` in `raw_fields_json`; pass `--no-ratings` to skip it. `--probe-ratings N` dumps the raw JSON for N jobs to `data/ratings_sample.json` without scraping, which is how the parser in `web/main.py` gets checked against live data.

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

`getPostingOverview` returns HTML that uses `<div class="tag__key-value-list">` containers — not `<table>` elements. Each container has a `<span class="label">` (field name) plus one or more value nodes. The parser clones each container, removes the label, then reads the remaining text so multi-paragraph and list-based field bodies are preserved:

```javascript
div.querySelectorAll(".tag__key-value-list").forEach((container) => {
  const labelEl = container.querySelector(".label");
  const label = labelEl?.textContent?.trim()?.replace(/:$/, "");
  const valueRoot = container.cloneNode(true);
  valueRoot.querySelector(".label")?.remove();
  const value = valueRoot.innerText.trim();
  if (label && value) fields[label] = value;
});
```

Anchor `href`s inside each value (`http`, `https`, `mailto`) are collected separately as `{label: [href, ...]}` under the reserved key `_links`, since `innerText` drops them.

The full label→value dict is stored as `raw_fields_json` so schema changes don't require re-scraping. Keys starting with `_` (`_links`, `_ratings`) are scraper-added structured data, not WaterlooWorks labels; `pick_field` never matches them because no `FIELD_MAP` candidate is a substring of those names.

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

The scraper starts the WaterlooWorks login flow using a persistent Chromium profile. It loads credentials from the git-ignored `.env` file and fills them only on the exact UWaterloo ADFS host; Duo remains interactive. It detects the authenticated `/myAccount` redirect, navigates directly to the configured Employer-Student Direct or Full-Cycle Service route, and clicks the board's **ALL JOBS** control without a terminal handoff. Missing `.env` values fall back to manual login.

Because `data/postings.jsonl` is the scraper's skip list, parser improvements do not refresh already-scraped jobs automatically. To force a full refresh after changing HTML parsing or field mapping, move the old JSONL aside before scraping:

```bash
mv data/postings.jsonl data/postings.old.jsonl
make scrape
make ingest
.venv/bin/python embed/embed_postings.py --force
make score
```

`--force` matters because changed posting text should replace existing embedding BLOBs. Local `status` values live in SQLite and are preserved by ingest.

### Request pacing

Listing pagination is sequential with no fixed delay between page requests.
Posting details use five concurrent workers on the same authenticated page by
default, with a shared work queue and no delay between postings; `--workers N`
controls the pool size. A partial final listing page is processed afterward by
one worker. Credentials are submitted only once per expired session. JSONL writes are serialized,
and ratings requests share a cross-worker division cache. Manual login at startup
— no credential storage in code.

---

## Database

### Why SQLite

Zero infrastructure. The entire corpus of WW postings for one term is small (hundreds to low thousands of rows — Employer Direct is ~300–500). SQLite is sufficient and the `.db` file is easy to inspect with any SQLite browser.

### Schema decisions

- `job_id TEXT PRIMARY KEY` — WW job IDs are numeric strings; TEXT avoids leading-zero issues.
- `raw_fields_json TEXT` — preserves the full label→value dict from the posting HTML plus the `_links` and `_ratings` reserved keys. Used to extract application method, apply contact info, duration, arrangement, level, location, documents, and the hiring-history summary without re-scraping. Because ratings live inside this JSON, a ratings change alone is enough for ingest to mark the row "updated".
- `comp_*` — persisted compensation interpretation: original pay text, native min/max/currency/period, stated hours, native and CAD hourly bounds/midpoint, FX provenance, parse status/confidence, and conditional-tier JSON. Ingest owns normalization so API requests do not reinterpret unchanged prose.
- `deadline` vs `deadline_iso` — raw deadline comes from the scraper with whitespace garbage (`"Jun 2, 2026\n\t\t\t\t\t\n\t\t\t\t\t\t11:00 PM"`). Keep the raw string for display fidelity; populate `deadline_iso` during ingest via `dateutil.parser` for sorting and filtering.
- `embedding BLOB` — `np.float32` array of shape (384,) serialized via `.tobytes()`. Decoded on read with `np.frombuffer(blob, dtype=np.float32)`.
- `score_*` columns are REAL, nullable — scores are populated by the scorer after ingestion, so freshly scraped rows have NULL scores until `make embed` and `make score` run.
- `scraped_at` vs `updated_at` — `scraped_at` is set once on first insert; `updated_at` is bumped on every upsert.
- `status TEXT NOT NULL DEFAULT 'new'` — local workflow state for the posting. Valid UI/API values are `new`, `maybe`, `applied`, and `ignored`.

### Upsert strategy

For each record, `ingest.py` pre-checks existence with a `SELECT`, then:

- **Not found** → `INSERT` all content fields; `embedding` and `score_*` are not included so they default to NULL.
- **Found, `raw_fields_json` unchanged** → skip (counts as "no change").
- **Found, content differs** → `UPDATE` content fields only; `scraped_at`, `embedding`, and `score_*` are not touched.

This approach (pre-check + compare) rather than `ON CONFLICT DO UPDATE` enables a clean three-way summary: inserted / updated / skipped.

Re-runs refresh posting content without nuking embeddings, scores, or local status.

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

Parser changes that affect `summary`, `responsibilities`, or `required_skills` also require `embed_postings.py --force`; otherwise existing rows keep stale embeddings from the previous text.

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

`resume.pdf` is a required input for resume scoring. `scripts/preflight.py` checks for it before `make run`, `make pipeline`, `make score`, and Docker startup so those workflows fail early with a clear message instead of after the expensive scrape/embed steps. The same preflight checks for `data/postings.jsonl` before pipeline and Docker runs.

### Why semantic over TF-IDF

TF-IDF rewards literal vocabulary overlap. Brittle for roles where titles vary wildly across companies and the relevant signal is conceptual, not lexical. Embeddings capture semantic similarity even with no shared tokens. The semantic scorer complements the keyword classifier instead of duplicating it.

---

## Web UI

### Why FastAPI + Alpine.js

- FastAPI: minimal boilerplate, automatic OpenAPI docs, serves static files trivially.
- Alpine.js: reactive without a build step. Loaded from CDN. The entire UI is a single `index.html`.

### No server-side pagination

The full corpus fits in one JSON response (a few MB at most, embeddings excluded). Client-side filtering and sorting via Alpine.js is instant for this data size. Eliminating pagination removes complexity on both the server and client.

### `/api/search`

`GET /api/search?q=...` embeds the query with the same `all-MiniLM-L6-v2` model used for postings and returns `{job_id: cosine}` for every embedded posting, best first. The model is loaded lazily on the first call and kept for the life of the process (about 80 MB, 2–5 s to load, ~20 ms per query after that); the embedding matrix is cached and reloaded whenever the DB file's mtime changes. An empty `q` returns `{}` but still warms the model, which the UI uses when you switch to semantic mode. `make serve` runs uvicorn with `--reload`, so every file save drops the model and the next query pays the load again. `sentence_transformers` is imported inside the loader so server start and the test suite stay light.

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
    "status": "new",
    "score_software": 0.12,
    "score_ai_ml": 0.03,
    "score_firmware": 0.85,
    "score_hardware": 0.10,
    "score_resume": 0.43,
    "comp_native_min": 24.00,
    "comp_native_max": 28.00,
    "comp_currency": "CAD",
    "comp_period": "hour",
    "comp_hourly_cad_min": 24.00,
    "comp_hourly_cad_max": 28.00,
    "comp_hourly_cad_mid": 26.00,
    "comp_parse_status": "conditional",
    "comp_confidence": "medium",
    "comp_hourly": 26.00,
    "comp_score": 0.227,
    "apply_method": "email",
    "apply_email": "careers@example.com",
    "apply_link": null,
    "apply_links": [],
    "work_term_duration": "8 month consecutive work term preferred",
    "duration_months": 8,
    "arrangement": "Hybrid",
    "level": "Junior, Intermediate",
    "levels": ["Junior", "Intermediate"],
    "country": "Canada",
    "region": "ON - Waterloo Region",
    "documents_required": ["Résumé", "Grade Report"],
    "needs_cover_letter": false,
    "hires_total": 16,
    "hires_by_term": {"1": 12.5, "2": 25.0, "3": 37.5, "4": 25.0},
    "hires_by_faculty": {"Engineering": 35.0, "Mathematics": 65.0},
    "top_programs": [["Computer Science/BCS", 9], ["Computer Engineering", 4]],
    "special_dates": "January 4 to April 30, 2027",
    "rating_avg": 8.7,
    "rating_count": 27,
    "rating_all_avg": 8.5,
    "keyword_hits": {
      "software": [],
      "ai_ml": [],
      "firmware": ["firmware", "rtos", "spi"],
      "hardware": ["pcb"]
    }
  }
]
```

`embedding` and `raw_fields_json` are never sent to the client. Compensation normalization is persisted at ingest; the other enrichments below are computed at request time from the DB row.

### Enrichment

Compensation is normalized during ingest; four additional enrichment passes run over each row before it is returned:

**Compensation parsing** — ingest parses the free-text `Compensation and Benefits` field into auditable `comp_*` columns:

1. Hourly (most common): `$25–$29 hourly`, `$27/hr`, `Hourly Rate: 20.29–24.11`, `The hourly wage…is $26.49`
2. Bi-weekly: `$2,045 – $2,523 / bi-weekly` → ÷ 80 hrs
3. Weekly: `$1,600 per week` → ÷ 40 hrs
4. Monthly: `$4,264 to $5,200 per month` → ÷ 173.3 hrs
5. Annual: `$45,000 – $55,000 per year` → ÷ 2,080 hrs
6. Term totals, `K` suffixes, tiered schedules, currency placement variants, and low-confidence unit inference

Native min/max and currency remain the primary display. Weekly/monthly/annual/term values are converted using stated hours when available. Cross-currency sorting uses the dated Bank of Canada snapshot in `config/fx_rates.json`; the rate and date are persisted beside each interpretation. `comp_hourly` is the CAD midpoint used for filtering and sorting. `comp_score` clamps $16–$60/hr to [0, 1] only for colour intensity.

**Application method detection** — on the Full Cycle board the only application labels are `Application Method` (always `WaterlooWorks`) and `Additional Application Information`, so links come from the scraper's `_links` anchors on those labels plus a URL regex over the additional-info text. Anchors there are often team LinkedIn profiles or UW visa pages rather than an application form, so URLs on `linkedin.com/in/`, `uwaterloo.ca`, and `google.com` are dropped. The older `Application Delivery` / `If By Email, Send To` / `If By Website, Go To` labels are still honoured for boards that have them.

- `apply_links`: every remaining URL in order (explicit website field, anchors, then text URLs), deduplicated.
- `apply_link`: the first of those, or null.
- `apply_email`: the `If By Email, Send To` field, else the first `mailto:` anchor, or null.
- `apply_method`: `"email"` if delivery is by email or an email address is present; `"link"` if delivery is by website or any link was found; `"ww"` otherwise (apply through WaterlooWorks only).

**Posting attributes** — straight from `raw_fields_json` labels: `work_term_duration` / `duration_months` (parsed from `Work Term Duration`, so `2 work term commitment` yields null), `arrangement` (`Employment Location Arrangement`), `level` / `levels` (`Level` split on whitespace, since WW joins multiple levels with tab/newline runs), `country`, `region`, `documents_required` (split on commas), `needs_cover_letter`, and `special_dates` (`Special Work Term Start/End Date Considerations`).

**Hiring history** — summarises `_ratings` (verified against live output, see `web/fixtures/ratings_sample.json`). Section titles are matched by lowercase prefix after stripping HTML (`<b>Hiring History</b>`) and the ` - Employer` suffix.

- `hires_total`: sum of the nine per-term counts on the `Employer Division` row of the `Hiring History` table.
- `hires_by_term`: `{"1": pct, ...}` from the `Hires by Student Work Term Number` pie, whose slices are ordinal words (`First` ... `Sixth +`).
- `hires_by_faculty`: from the `Hires by Faculty` pie. `top_programs`: top three of `Most Frequently Hired Programs`.
- `rating_avg` (/10), `rating_count`, `rating_all_avg`: from the `Work Term Ratings Summary` table, which WW only includes when the employer has 5+ ratings. The per-question (1-5) and distribution charts are stored raw but not parsed.

Employers with no report return `{"missingReportStructure": ...}`; the scraper stores nothing for them and every field above is null or empty.

**Keyword hit extraction** — `config/roles.yaml` is loaded once at startup. For each posting, the concatenated text is scanned for each role's keyword list. Returns `keyword_hits`: a dict mapping each role to the list of keywords that matched.

**Placeholder text cleanup** — some employers put section headings or pointers in WaterlooWorks fields instead of actual content, such as `Requirements`, `Qualifications`, `Key Responsibilities`, or `Check our list of projects...`. The API blanks those known placeholders before sending rows to the UI so they don't appear as real responsibilities or skills.

### UI

Three regions: a resizable filter sidebar on the left, the job list, and a detail drawer that slides over the list from the right when a posting is selected. Both the sidebar and the drawer have a drag handle (double-click resets) and remember their width in `localStorage`.

**Topbar:** board picker (`Employer Direct` / `Full Cycle`; the UI opens on whichever board has data), add-applied, delete-expired, Ctrl+K, day/night. When the sidebar is hidden a "Show filters" button appears here (`f` toggles it).

**List toolbar:** the search box with a Text / Semantic toggle, a spinning search icon and "Loading model…" placeholder while the first semantic query warms the server (nothing reflows), a "Sorted by …" pill (click opens the palette), and the visible / total count.

- Text search matches title, org, location, region, country, arrangement, level, job ID, and the three description fields.
- Semantic search sends the query to `/api/search` (debounced 300 ms, in-flight requests aborted), keeps postings with cosine similarity ≥ 0.2 (top 60), and sorts by similarity until another sort is chosen. Sidebar filters compose on top. Switching modes is remembered.

**Filter sidebar:** every group is a collapsible `<details>` with option counts for the current board. Checkbox groups come from one `groups` registry in `app()` that also drives the palette's toggle entries, the active-filter chips, and "Clear all", so a filter is declared once. One rule for every group: nothing checked means the group is not filtering; checked options are OR'd. Groups: Status (with a separate "Hide ignored postings" switch, on by default), Role, Apply by, Hired my term before (yes / hires but not my term / no history), Duration, Level, Arrangement, Country, Region (the last two derived from the data). "My work term" (segmented 1–8) sits at the top because it drives the T‹n› column and the term group. Other controls: min pay with "include unlisted", has hiring history, cover letter any / not required / required, max applicants (Full Cycle), min openings, due within N days, hide expired. Everything currently applied is repeated as removable chips in a row above the table, and an active group's heading turns blue.

**Table:** Title with org underneath · Location · Due · Resume · role scores · Pay · Open · Apps (Full Cycle only) · T‹n› · Status. Pay displays the native range/period and sorts by its unbounded CAD hourly midpoint; unknown pay remains last and ties use title/job ID. Fixed column widths, title takes the remaining space, and the table scrolls sideways below about 1040px. When the Role filter is active only the checked roles' score columns are shown. T‹n› is a tri-state: ✓ the employer's hiring history shows hires at your work term number, ✗ it has a history without any, – no history (the tooltip has the full breakdown). Any header sorts; palette sorts also cover fields not shown as columns (previous hires, rating, similarity).

**Drawer:** sticky header with title, org, status buttons, and close. Then a facts grid (due, pay, location, country, arrangement, duration, level, openings, applicants, hired at my term, employer rating), the apply box (email with copy, every application link, or the WaterlooWorks fallback, plus required documents with the cover letter highlighted), a scores strip (resume, four roles, similarity when in semantic mode), matched keywords, then Summary / Responsibilities / Required skills as collapsible sections. WaterlooWorks emits list items as tab-indented lines; `renderPostingText()` turns those (and pasted bullet glyphs) into real paragraphs and nested lists, building DOM nodes only so no posting text is ever parsed as HTML. Hiring history follows (previous hires, satisfaction, a by-term bar strip with your term highlighted, faculties, top programs), then term-date notes and the job ID.

**Keyboard shortcuts:**

| Key      | Action                          |
|----------|---------------------------------|
| `j` / `k` | Next / previous posting (opens the drawer) |
| `Esc`    | Close the drawer, or blur search |
| `/`      | Focus search                    |
| `f`      | Show / hide the filter sidebar  |
| `c`      | Copy selected job ID            |
| `m`      | Copy apply email                |
| `Shift+S` | Sort by resume match           |
| `Shift+P` | Sort by pay                    |
| `Ctrl+K` | Command palette                 |

**Add applied:** a full-screen overlay (`Esc` closes, `Ctrl+Enter` submits) with a textarea for the WaterlooWorks applications page. It POSTs the raw paste to `/api/postings/applied`, which parses it in `web/applied.py` and flips the matched postings to `applied`.

Parsing anchors on the only two machine-shaped lines in a row — a bare 5-8 digit ID immediately followed by a work term (`2027 - Winter`) — which is why years, opening counts, page numbers, and the "48 of 50" header can't be mistaken for job IDs. Some browsers flatten a copied table into a single line, so when the structured pass finds nothing the parser falls back to every bare 5-8 digit number; that stays safe because the endpoint only writes IDs that already exist in `postings`.

The write is additive: IDs absent from the paste keep their status, and re-pasting the same page is a no-op (`updated: 0`). The response reports `parsed` / `matched` / `updated` / `already_applied` plus the `unknown` IDs — postings on the other board, or ones pruned since the last scrape — and the UI patches the in-memory rows from `applied_job_ids` so the table, status column, and sidebar counts update without a reload.

**Command palette (Ctrl+K):** sorts, one toggle per filter option, cover letter / history / expired toggles, clear filters, clear search, switch search mode, show/hide filters, close posting, theme, add applied, copy actions, and the shortcut list.

**Error states:**

- If `/api/postings` cannot load the SQLite database or postings table, the UI shows the API error instead of staying in a loading state.
- If postings load but all `score_resume` values are empty, the UI shows a warning that resume scores are unavailable and tells the user to add `resume.pdf` and rerun scoring.

---

## Docker

### What's in the image

`python:3.12-slim` base with uv copied from `ghcr.io/astral-sh/uv:latest`. CPU-only PyTorch is installed first (via `--index-url https://download.pytorch.org/whl/cpu`) before `requirements.txt`, so sentence-transformers doesn't pull the CUDA build (~750 MB vs ~3 GB). uv's `--system` flag installs directly into the image Python without a venv. The Playwright Python package is installed (it's in `requirements.txt`) but the Chromium binary is not — the scraper cannot run inside the container.

### What's not in the image

`data/` and `resume.pdf` are excluded from the image via `.dockerignore` and bind-mounted at runtime. The HuggingFace model cache is a named Docker volume (`hf_cache`) so the model survives container recreates without re-downloading.

### Startup sequence

`docker-entrypoint.sh` runs the full pipeline on every container start, then hands off to uvicorn:

```
preflight → ingest → embed_postings → scorer → embed_resume → uvicorn
```

The preflight step fails early if `resume.pdf` or `data/postings.jsonl` is missing or invalid. All pipeline steps after that are idempotent, so re-running them on an already-populated DB is fast (seconds). The `exec` before uvicorn ensures it gets PID 1 and receives signals cleanly.

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
| Model-based classification              | Keyword scoring is transparent, instant, and tunable via YAML                      |
| Vector database (pgvector, FAISS, etc.) | The corpus is small; numpy matmul is simpler and fast enough                       |
| OpenAI/Anthropic embeddings             | Local model is free, offline, and quality difference is small for this corpus      |
| Auth / multi-user                       | Personal tool; single local user                                                   |
| Public deployment                       | Personal tool; Docker is for local reproducibility, not public hosting             |
| On-demand LLM evaluation                | `llm_evals` table is reserved for later; not wired into the UI yet                 |
