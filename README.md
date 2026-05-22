# goosehunt

Personal WaterlooWorks co-op posting aggregator. Scrapes the Employer Direct board, classifies postings by role relevance and resume similarity, and serves everything in a local no-pagination web UI.

> **Personal use only.** WaterlooWorks ToS likely prohibits automated scraping. Use at your own risk.

---

## What it does

1. **Scrapes** all postings from the Employer Direct board using Playwright with a persistent browser profile (you log in once, Duo once, then leave it alone).
2. **Stores** every posting in a local SQLite database — resumable, so a crashed scrape picks up where it left off.
3. **Classifies** each posting against four target roles using tunable keyword lists in `config/roles.yaml`.
4. **Scores** each posting against your resume PDF using cosine similarity on sentence embeddings.
5. **Parses compensation** from each posting's free-text pay field and normalizes it to an estimated hourly rate.
6. **Serves** a local web UI — one page, every posting loaded, client-side sort/filter, no build step.

---

## Score columns

| Column           | What it measures                          |
|------------------|-------------------------------------------|
| `score_software` | Software / SWE keyword match             |
| `score_ai_ml`    | AI / ML / data science keyword match     |
| `score_firmware` | Firmware / embedded / mechatronics match |
| `score_hardware` | Hardware / FPGA / PCB keyword match      |
| `score_resume`   | Resume cosine similarity                  |
| `comp_score`     | Estimated pay, normalized to [0, 1]      |

All `score_*` values are in [0, 1]. `comp_score` normalizes estimated hourly pay: $16/hr → 0.0, $60/hr → 1.0.

---

## Project layout

```
goosehunt/
├── config/
│   └── roles.yaml          # keyword lists for each role scorer
├── scraper/
│   ├── scraper.py          # Playwright scraper → JSONL
│   ├── test_scraper.py     # unit tests (no browser required)
│   └── profile/            # persistent Chromium profile (gitignored)
├── db/
│   ├── schema.sql          # CREATE TABLE statements
│   └── ingest.py           # JSONL → SQLite
├── embed/
│   ├── embed_postings.py   # sentence-transformers → BLOB column
│   └── embed_resume.py     # embed resume PDF → score_resume column
├── classifier/
│   └── scorer.py           # keyword scorer → score_* columns
├── resume/
│   └── parser.py           # pdfplumber PDF → plain text
├── web/
│   ├── main.py             # FastAPI app + compensation/apply/keyword parsing
│   └── static/
│       └── index.html      # Alpine.js UI, no build step
├── data/
│   ├── postings.jsonl      # scraper output (gitignored)
│   ├── postings.db         # SQLite DB (gitignored)
│   └── diag.md             # diagnostic output from --diag (gitignored)
├── resume.pdf              # your resume (gitignored)
├── Dockerfile
├── docker-compose.yml
├── docker-entrypoint.sh
├── .dockerignore
├── Makefile
└── requirements.txt
```

---

## Database schema

```sql
CREATE TABLE postings (
    job_id            TEXT PRIMARY KEY,
    board_type        TEXT,          -- "direct"
    title             TEXT,
    org               TEXT,
    location          TEXT,
    deadline          TEXT,          -- raw string from scraper
    deadline_iso      TEXT,          -- ISO 8601, parsed during ingest
    work_term         TEXT,
    openings          INTEGER,
    summary           TEXT,
    responsibilities  TEXT,
    required_skills   TEXT,
    raw_fields_json   TEXT,          -- full label→value dict as JSON
    scraped_at        TEXT,
    updated_at        TEXT,
    embedding         BLOB,          -- float32[384] via all-MiniLM-L6-v2
    score_firmware    REAL,
    score_hardware    REAL,
    score_software    REAL,
    score_ai_ml       REAL,
    score_resume      REAL
);
```

`comp_score`, `comp_hourly`, `apply_method`, `apply_email`, `apply_link`, and `keyword_hits` are computed at API request time from `raw_fields_json` and the text columns — they are not stored in the DB.

---

## Scraper design

The scraper uses WaterlooWorks' own in-page JavaScript API (no tab navigation, no HTML scraping of the listing page):

1. Extract the `dataParams.action` key embedded in the page's `<script>` tags.
2. POST to the listing endpoint (`isDataViewer: true`) with 100 results per page — returns JSON rows with numeric job IDs.
3. Call `window.getPostingOverview(jobId, callback)` for each job — returns posting HTML.
4. Parse HTML by querying `.tag__key-value-list` containers (WW's key-value layout).

All calls go through `page.evaluate()` — no new tabs are opened.

---

## Makefile targets

```
make install     # create venv (uv), install deps, playwright install chromium
make run         # scrape + pipeline + serve (full end-to-end)
make scrape      # scrape only → data/postings.jsonl
make pipeline    # ingest → embed → score (run after scrape)
make serve       # start FastAPI on localhost:8000
make scrape-diag # inspect page state, fetch one posting, write data/diag.md
make test        # run unit tests (no browser)
```

Individual pipeline steps are still available as `make ingest`, `make embed`, `make score`.

---

## Setup

### Local (venv)

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`).

```bash
make install

# drop your resume here
cp /path/to/resume.pdf resume.pdf

# first run: browser opens, you log in, navigate to Employer Direct, press Enter
# then the pipeline runs automatically and the UI starts
make run
```

On subsequent runs where you just want to re-serve existing data:

```bash
make serve
```

Or re-scrape and reprocess without restarting the server:

```bash
make scrape && make pipeline
```

On first run, a Chromium window opens. Log in (Duo if prompted), navigate to the Employer Direct board, wait for job listings to appear, then press Enter in the terminal.

### Docker (any device, no Python setup)

Scraping must still run locally (needs your WaterlooWorks session). Everything after that runs in the container.

```bash
# scrape locally first
make scrape

# on any device with Docker — copy the repo + data/postings.jsonl + resume.pdf, then:
docker compose up
```

`docker compose up` runs ingest → embed → score → serve on every start, then keeps the UI alive at `http://localhost:8000`. The HuggingFace model is cached in a named volume so it's only downloaded once.

---

## Build order (incremental)

- [x] README
- [x] Scraper → JSONL (end-to-end verified)
- [x] SQLite schema + JSONL ingestion
- [x] Posting embedding pipeline (sentence-transformers → BLOB column)
- [x] Keyword classifier with YAML config
- [x] Resume parser + cosine-sim scorer
- [x] FastAPI + Alpine.js UI
- [x] Makefile wiring everything together
- [x] Compensation parsing + sortable Pay column in UI
- [x] pip → uv migration (faster installs)
- [x] Application method detection (email vs external link) + filter chips
- [x] Keyword hit display in detail panel
- [x] Job ID click-to-copy
- [x] UI redesign: dark mode, two-pane layout, keyboard navigation, command palette
- [x] Role consolidation: SWE, AI/ML, FW, HW
