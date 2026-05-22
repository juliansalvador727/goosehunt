# Design Notes — goosehunt

Decisions, tradeoffs, and implementation notes for each component. Updated as we build.

---

## Scraper

### Why Playwright over requests/Selenium
WaterlooWorks renders postings via JavaScript — raw HTTP won't give you the posting content. Playwright handles dynamic content natively and makes multi-tab orchestration (catching the detail page popup) straightforward.

### Persistent profile
`playwright.chromium.launch_persistent_context(user_data_dir="scraper/profile/")` keeps cookies and session state across runs. You Duo-authenticate once; subsequent runs reuse the session until it expires. The `profile/` directory is gitignored so credentials don't leak.

### Two-pass design rationale
A single-pass approach (open listing → open detail → next) is fragile: if the detail page errors or the scraper dies, you lose your place in pagination. Splitting into:

1. **Link collection pass** — fast, no detail fetching. Writes `data/queue.jsonl` (`job_id`, `onclick_handler`, `board_type`). Idempotent: re-running deduplicates against existing IDs.
2. **Detail extraction pass** — reads queue, skips job IDs already in `postings.jsonl` / DB, fetches one at a time with random delay.

This means a crash during detail extraction loses at most one posting, and a re-run picks up where it left off with no duplicate work.

### onclick handling
WW posting links use `onclick` attributes rather than `href`. The pattern:

```python
# collect during pass 1
onclick = await element.get_attribute("onclick")
job_id  = re.search(r"openJob\((\d+)", onclick).group(1)

# fire during pass 2
async with ctx.expect_page() as page_info:
    await page.evaluate(onclick)
detail_page = await page_info.value
await detail_page.wait_for_load_state("networkidle")
```

### Detail extraction
Walk every `<table>` on the detail page and build a flat `label → value` dict. Store the full dict as `raw_fields_json` so we can re-parse without re-scraping if the schema changes. Pull known fields (`title`, `org`, `location`, etc.) from the dict by label name.

### Politeness
```python
import random, asyncio
await asyncio.sleep(random.uniform(1.5, 3.5))
```
Single-threaded, no concurrent requests. This is a personal tool on a university system — don't be aggressive.

---

## Database

### Why SQLite
Zero infrastructure. The entire corpus of WW postings for one term is small (hundreds to low thousands of rows). SQLite is sufficient and the `.db` file is easy to inspect with any SQLite browser.

### Schema decisions
- `job_id TEXT PRIMARY KEY` — WW job IDs are numeric strings; TEXT avoids leading-zero issues.
- `raw_fields_json TEXT` — preserves the full label→value dict from the detail page. Future re-processing doesn't require re-scraping.
- `score_*` columns are REAL, nullable — scores are populated by the classifier after ingestion, so freshly scraped rows have NULL scores until `make score` runs.
- `scraped_at` vs `updated_at` — `scraped_at` is set once on first insert; `updated_at` is bumped on every upsert (e.g., re-scrape that refreshes a posting).

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
This lets re-runs refresh posting content without nuking scores.

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
    "board_type": "full_cycle",
    "title": "Firmware Engineer",
    "org": "Some Corp",
    "location": "Waterloo, ON",
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
        │  Playwright
        ▼
data/queue.jsonl        ← pass 1: all job_id + onclick
        │
        │  pass 2: detail extraction
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
| Model-based classification | Keyword scoring is sufficient for v1; LLM adds cost and latency |
| Auth / multi-user | Personal tool; single local user |
| Deployment | Runs locally; no public hosting needed or wanted |
| Incremental TF-IDF | Corpus is small; re-fitting on every `make score` run is fast enough |
