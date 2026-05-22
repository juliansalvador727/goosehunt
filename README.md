# goosehunt

Personal WaterlooWorks co-op posting aggregator. Scrapes Full-Cycle Service and Employer Direct boards, classifies postings by role relevance and resume similarity, and serves everything in a local no-pagination web UI.

> **Personal use only.** WaterlooWorks ToS likely prohibits automated scraping. Use at your own risk.

---

## What it does

1. **Scrapes** all postings from both WW boards using Playwright with a persistent browser profile (you log in once, Duo once, then leave it alone).
2. **Stores** every posting in a local SQLite database — resumable, so a crashed scrape picks up where it left off.
3. **Classifies** each posting against six target roles using tunable keyword lists in `config/roles.yaml`.
4. **Scores** each posting against your resume PDF using TF-IDF cosine similarity.
5. **Serves** a local web UI — one page, every posting loaded, client-side sort/filter, no build step.

---

## Target roles

| Score column      | Role                      |
|-------------------|---------------------------|
| `score_firmware`  | Firmware                  |
| `score_embedded`  | Embedded systems          |
| `score_hardware`  | Hardware / FPGA / PCB     |
| `score_software`  | Software / SWE            |
| `score_fde`       | Forward-deployed engineer |
| `score_mts`       | Member of technical staff |
| `score_resume`    | Resume TF-IDF similarity  |

---

## Project layout

```
goosehunt/
├── config/
│   └── roles.yaml          # keyword lists for each role scorer
├── scraper/
│   ├── scraper.py          # Playwright two-pass scraper → JSONL
│   └── profile/            # persistent Chromium profile (gitignored)
├── db/
│   ├── schema.sql          # CREATE TABLE statements
│   └── ingest.py           # JSONL → SQLite
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
│   └── postings.db         # SQLite DB (gitignored)
├── resume.pdf              # your resume (gitignored)
├── Makefile
└── requirements.txt
```

---

## Database schema

```sql
CREATE TABLE postings (
    job_id            TEXT PRIMARY KEY,
    board_type        TEXT,          -- "full_cycle" | "direct"
    title             TEXT,
    org               TEXT,
    location          TEXT,
    deadline          TEXT,
    work_term         TEXT,
    openings          INTEGER,
    summary           TEXT,
    responsibilities  TEXT,
    required_skills   TEXT,
    raw_fields_json   TEXT,          -- full label→value dict as JSON
    scraped_at        TEXT,
    updated_at        TEXT,
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

Two-pass approach:

**Pass 1 — collect links.** Walk pagination on both boards, harvest every `(job_id, onclick_handler)` pair. Store to a queue file so pass 2 is resumable.

**Pass 2 — extract details.** For each job not yet in the DB, fire the `onclick` handler via `page.evaluate(...)`, catch the new tab with `ctx.expect_page()`, walk all tables on the detail page to build a label→value dict, extract known fields, write to JSONL.

Politeness: 1.5–3.5 s random delay between postings, single-threaded, manual login at startup.

---

## Makefile targets

```
make scrape      # run scraper → data/postings.jsonl
make ingest      # ingest JSONL → SQLite
make score       # run classifier + resume scorer, update DB
make serve       # start FastAPI on localhost:8000
make all         # scrape + ingest + score + serve
```

---

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# drop your resume here
cp /path/to/resume.pdf resume.pdf

# first run: browser opens, you log in + set up filters manually
make scrape
```

---

## Build order (incremental)

- [x] README
- [ ] Scraper → JSONL (verify end-to-end before anything else)
- [ ] SQLite schema + JSONL ingestion
- [ ] Keyword classifier with YAML config
- [ ] Resume parser + TF-IDF scorer
- [ ] FastAPI + Alpine.js UI
- [ ] Makefile wiring everything together
