<p align="center">
  <img src="docs/logo.png" alt="goosehunt logo" width="420">
</p>

# goosehunt

Personal WaterlooWorks co-op posting aggregator. Scrapes the Employer Direct board, classifies postings by role relevance and resume similarity, and serves everything in a local no-pagination web UI.

> **Personal use only.** WaterlooWorks ToS likely prohibits automated scraping. Use at your own risk.

---

## What it does

1. **Scrapes** the currently visible Employer Direct results using Playwright with a persistent browser profile. You log in manually, set filters/work term in WaterlooWorks, then press Enter.
2. **Stores** every posting in a local SQLite database — resumable, so a crashed scrape picks up where it left off.
3. **Classifies** each posting against four target roles using tunable keyword lists in `config/roles.yaml`.
4. **Scores** each posting against your resume PDF using cosine similarity on sentence embeddings.
5. **Enriches API responses** with parsed compensation, application method/contact, and matched keyword hits.
6. **Serves** a local web UI — one page, every posting loaded, client-side sort/filter, keyboard navigation, no build step.

---

## Score fields

| Column           | What it measures                          |
|------------------|-------------------------------------------|
| `score_software` | Software / SWE keyword match             |
| `score_ai_ml`    | AI / ML / data science keyword match     |
| `score_firmware` | Firmware / embedded / mechatronics match |
| `score_hardware` | Hardware / FPGA / PCB keyword match      |
| `score_resume`   | Resume cosine similarity                  |
| `comp_score`     | Estimated pay, normalized to [0, 1]      |

All `score_*` values are stored in SQLite and are in [0, 1]. `comp_score` is API-only and normalizes estimated hourly pay: $16/hr → 0.0, $60/hr → 1.0.

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

The scraper uses WaterlooWorks' own in-page JavaScript API from the page you manually prepare:

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

Individual pipeline steps are available as `make ingest`, `make embed`, and `make score`. `make score` runs both the keyword scorer and the resume-similarity scorer.

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

The scraper starts from the filters currently visible in WaterlooWorks. Set the work term and any board filters before pressing Enter.

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

## Current UI

The UI lives in `web/static/index.html` and is served by FastAPI from `web/main.py`.

- Board dropdown: currently useful for Employer Direct data; the scraper writes `board_type = "direct"`.
- Filters: search, role chips (`SWE`, `AI/ML`, `FW`, `HW`), and apply-by chips (`Email`, `Link`).
- Default apply filter: Email and Link are enabled; WaterlooWorks-only postings (`apply_method = "ww"`) are intentionally hidden unless the UI is changed to expose that method.
- Table: sortable title/org/location/due/resume/role/pay/openings columns, click-to-copy job IDs.
- Detail panel: score grid, apply link/email, copy buttons, keyword-hit chips, summary/responsibilities/required skills.
- Keyboard: `j`/`k` navigate, `/` focuses search, `c` copies job ID, `m` copies email, `Shift+S` sorts by resume, `Shift+P` sorts by pay, `Ctrl+K` opens the command palette.

---

## Current status

- Scraper → JSONL: implemented for Employer Direct, headed Playwright, persistent local profile.
- Ingest → SQLite: implemented with idempotent insert/update/skip behavior.
- Embeddings: implemented with local `all-MiniLM-L6-v2`, stored as SQLite BLOBs.
- Scoring: implemented for four role keyword scores plus resume cosine similarity.
- API/UI: implemented with FastAPI + Alpine.js, request-time compensation/apply/keyword enrichment.
- Docker: implemented for pipeline + serving only; scraping still runs locally.

---

## License

This project is licensed under the [MIT License](LICENSE).

---

## Terms and Use Notice

### Mission

goosehunt is a personal tool for making WaterlooWorks job browsing faster, clearer, and easier to prioritize. It replaces scattered manual review with a local workflow for scraping visible postings, searching and filtering them, estimating relevance, and comparing jobs against a resume.

This project is not affiliated with, endorsed by, or operated by the University of Waterloo, WaterlooWorks, Orbis, or any employer listed in WaterlooWorks.

### Services

goosehunt provides a local interface for reviewing job postings that the user can already access through their own WaterlooWorks session.

The app currently supports:

- Scraping the user's currently visible Employer Direct results after manual login.
- Storing scraped posting data locally in `data/postings.jsonl` and `data/postings.db`.
- Filtering, sorting, and reviewing jobs in a local FastAPI/Alpine.js web UI.
- Computing local keyword scores, resume-similarity scores, compensation estimates, application method details, and matched keyword hits.

goosehunt does not provide hosted access to private job boards. It does not bypass login, Duo, permissions, or WaterlooWorks access controls. The scraper runs through the user's own browser session and starts only after the user manually logs in, navigates to the relevant board, applies any desired filters, and confirms that listings are visible.

### User responsibility

Users are responsible for how they run this software.

Before scraping or storing job posting information, users should review and comply with all applicable rules, contracts, website terms, platform policies, employer restrictions, university policies, and laws that apply to the source of the postings. If WaterlooWorks or another job board prohibits automated scraping, bulk downloading, caching, redistribution, or derivative use of posting data, users should not use goosehunt in a way that violates those restrictions.

This repository only provides software. It does not grant permission to access, copy, store, redistribute, or reuse any third-party job posting content.

### Copyright and posting content

Job postings may contain content owned by employers, WaterlooWorks, Orbis, the University of Waterloo, or other third parties. goosehunt is intended for personal, local review of postings that the user is authorized to view. Users should not publish, share, sell, or redistribute scraped posting data unless they have the right to do so.

Any reliance on private study, research, fair dealing, fair use, or similar copyright exceptions is the user's responsibility to evaluate. This project does not provide legal advice and does not assume liability for a user's copying, storage, or use of posting content.

### Data and privacy

goosehunt is local-first:

- Scraped job data is written to local files under `data/`.
- Resume data is read from the local `resume.pdf`.
- Posting embeddings and scores are stored in the local SQLite database.
- The web UI is served locally by default at `http://localhost:8000`.
- No scraped postings are sent to a goosehunt server because this project does not operate one.

The embedding model is downloaded from HuggingFace when needed, and frontend dependencies such as Alpine.js and fonts may be loaded from their configured CDNs by the browser. Users who need stricter offline or privacy guarantees should vendor those assets locally and review dependency behavior before use.

### No warranty

goosehunt is provided as-is, without warranty. Scraping logic, score calculations, compensation parsing, deadline parsing, and application-method detection may be incomplete or wrong. Users should verify important details in WaterlooWorks or with the employer before making application decisions.
