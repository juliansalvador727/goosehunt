# goosehunt

Personal WaterlooWorks co-op posting aggregator. Scrapes the Employer Direct board, classifies postings by role relevance and resume similarity, and serves everything in a local no-pagination web UI.

> **Personal use only.** WaterlooWorks ToS likely prohibits automated scraping. Use at your own risk.

---

## What it does

1. **Scrapes** all postings from the Employer Direct board using Playwright with a persistent browser profile (you log in once, Duo once, then leave it alone).
2. **Stores** every posting in a local SQLite database — resumable, so a crashed scrape picks up where it left off.
3. **Classifies** each posting against six target roles using tunable keyword lists in `config/roles.yaml`.
4. **Scores** each posting against your resume PDF using cosine similarity on sentence embeddings.
5. **Serves** a local web UI — one page, every posting loaded, client-side sort/filter, no build step.

---

## Target roles

| Score column      | Role                      |
|-------------------|---------------------------|
| `score_firmware`  | Firmware                  |
| `score_embedded`  | Embedded systems          |
| `score_hardware`  | Hardware / FPGA / PCB     |
| `score_software`  | Software / SWE            |
| `score_fde`               | Forward-deployed engineer |
| `score_mts`               | Member of technical staff |
| `score_power_electronics` | Power electronics / drives |
| `score_resume`            | Resume cosine similarity  |

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
│   └── embed_postings.py   # sentence-transformers → BLOB column
├── classifier/
│   └── scorer.py           # keyword scorer + TF-IDF resume scorer
├── resume/
│   └── parser.py           # pdfplumber PDF → text
├── web/
│   ├── main.py             # FastAPI app
│   └── static/
│       └── index.html      # Alpine.js UI, no build step
├── data/
│   ├── postings.jsonl      # scraper output (gitignored)
│   ├── postings.db         # SQLite DB (gitignored)
│   └── diag.md             # diagnostic output from --diag (gitignored)
├── resume.pdf              # your resume (gitignored)
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
    score_embedded    REAL,
    score_hardware    REAL,
    score_software    REAL,
    score_fde         REAL,
    score_mts         REAL,
    score_resume      REAL
);
```

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
make install     # create venv, pip install, playwright install chromium
make scrape      # run scraper → data/postings.jsonl
make scrape-diag # inspect page state, fetch one posting, write data/diag.md
make test        # run unit tests (no browser)
make ingest      # ingest JSONL → SQLite
make embed       # embed postings → BLOB column
make score       # run classifier + resume scorer  (not yet implemented)
make serve       # start FastAPI on localhost:8000  (not yet implemented)
```

---

## Setup

```bash
make install

# drop your resume here
cp /path/to/resume.pdf resume.pdf

# first run: browser opens, you log in, navigate to Employer Direct, press Enter
make scrape
```

On first run, a Chromium window opens. Log in (Duo if prompted), navigate to the Employer Direct board, apply any filters you want, wait for job listings to appear, then press Enter in the terminal.

---

## Build order (incremental)

- [x] README
- [x] Scraper → JSONL (end-to-end verified)
- [x] SQLite schema + JSONL ingestion
- [x] Posting embedding pipeline (sentence-transformers → BLOB column)
- [ ] Keyword classifier with YAML config
- [ ] Resume parser + cosine-sim scorer
- [ ] FastAPI + Alpine.js UI
- [ ] Makefile wiring everything together
