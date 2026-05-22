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
const m = t.match(/dataParams\s*:\s*\{[^}]*action\s*:\s*['"](_-_-[^'"]{20,})['"]/);
```

This avoids accidentally picking up `getPostingOverview`'s action string (a different endpoint) or any other unrelated action.

### Posting HTML parsing
`getPostingOverview` returns HTML that uses `<div class="tag__key-value-list">` containers — not `<table>` elements. Each container has a `<span class="label">` (field name) and a `<p>` (value). The parser queries these directly:

```javascript
div.querySelectorAll('.tag__key-value-list').forEach(container => {
    const label = container.querySelector('.label')?.textContent?.trim()?.replace(/:$/, '');
    const value = container.querySelector('p')?.textContent?.trim();
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
Zero infrastructure. The entire corpus of WW postings for one term is small (hundreds to low thousands of rows). SQLite is sufficient and the `.db` file is easy to inspect with any SQLite browser.

### Schema decisions
- `job_id TEXT PRIMARY KEY` — WW job IDs are numeric strings; TEXT avoids leading-zero issues.
- `raw_fields_json TEXT` — preserves the full label→value dict from the posting HTML. Future re-processing doesn't require re-scraping.
- `score_*` columns are REAL, nullable — scores are populated by the classifier after ingestion, so freshly scraped rows have NULL scores until `make score` runs.
- `scraped_at` vs `updated_at` — `scraped_at` is set once on first insert; `updated_at` is bumped on every upsert.

### Upsert strategy
```sql
INSERT INTO postings (...) VALUES (...)
ON CONFLICT(job_id) DO UPDATE SET
    updated_at = excluded.updated_at,
    raw_fields_json = excluded.raw_fields_json,
    -- ... other fields ...
    -- score_* columns are NOT overwritten here;
    -- scorer handles those separately
```
Re-runs refresh posting content without nuking scores.

---

## Classifier

### Keyword scorer
Each role has a list of positive keywords in `config/roles.yaml`. Score = number of keyword hits in the concatenated text fields (`title + org + summary + responsibilities + required_skills`), normalized to [0, 1] by dividing by the max observed score across the corpus. Simple but tunable.

```yaml
firmware:
  keywords:
    - firmware
    - RTOS
    - bare-metal
    - embedded C
    - bootloader
    - HAL
    - register
```

### Why not an LLM for v1
Keyword scoring is transparent, instant, and free. You can look at a posting's score and immediately know which keywords fired. LLM classification would be slower, cost money per run, and be harder to debug. The YAML config makes tuning straightforward — add a keyword, re-run `make score`.

### Resume TF-IDF scorer
- Fit `TfidfVectorizer` on the corpus of all posting text fields.
- Transform the resume text and each posting into TF-IDF vectors.
- Score = cosine similarity between resume vector and posting vector.
- Scores live in `score_resume`.

This rewards postings that use similar vocabulary to your resume — a crude but useful signal. Re-scoring after updating your resume takes seconds.

---

## Web UI

### Why FastAPI + Alpine.js
- FastAPI: minimal boilerplate, automatic OpenAPI docs, serves static files trivially.
- Alpine.js: reactive without a build step. Loaded from CDN. The entire UI is a single `index.html`.

### No server-side pagination
The full corpus fits in one JSON response (a few MB at most). Client-side filtering and sorting via Alpine.js is instant for this data size. Eliminating pagination removes complexity on both the server and client.

### `/api/postings` response shape
```json
[
  {
    "job_id": "12345",
    "board_type": "direct",
    "title": "Firmware Engineer",
    "org": "Some Corp",
    "location": "Waterloo",
    "deadline": "2026-06-01",
    "score_firmware": 0.85,
    "score_software": 0.12,
    "score_resume": 0.43,
    ...
  }
]
```

### UI features
- Search box: filters on `title`, `org`, `location` (client-side, instant).
- Role checkboxes: show only postings with score > threshold for checked roles.
- Column headers: click to sort ascending/descending.
- Color coding: high scores get a green tint, low scores grey.

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
data/postings.db        ← SQLite (score_* columns NULL)
        │  classifier/scorer.py
        ▼
data/postings.db        ← SQLite (score_* columns populated)
        │  web/main.py
        ▼
localhost:8000          ← FastAPI + Alpine.js UI
```

---

## What we're NOT doing (and why)

| Skipped feature | Reason |
|-----------------|--------|
| Headless mode | WW has bot detection; persistent profile + headed = safest |
| Parallel scraping | Unnecessary for this corpus size; increases detection risk |
| Full-Cycle board | Employer Direct is sufficient; Full-Cycle adds complexity for little gain |
| Model-based classification | Keyword scoring is sufficient for v1; LLM adds cost and latency |
| Auth / multi-user | Personal tool; single local user |
| Deployment | Runs locally; no public hosting needed or wanted |
| Incremental TF-IDF | Corpus is small; re-fitting on every `make score` run is fast enough |
