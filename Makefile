.PHONY: install scrape scrape-diag test ingest embed score serve pipeline run check-resume check-inputs

PYTHON := .venv/bin/python

# ── setup ────────────────────────────────────────────────────────
install:
	uv venv
	uv pip install -r requirements.txt
	.venv/bin/playwright install chromium

# ── preflight ────────────────────────────────────────────────────
check-resume:
	$(PYTHON) scripts/preflight.py --resume

check-inputs:
	$(PYTHON) scripts/preflight.py --resume --jsonl

# ── scraper ──────────────────────────────────────────────────────
scrape:
	$(PYTHON) -m scraper.scraper

scrape-diag:
	$(PYTHON) -m scraper.scraper --diag

# ── pipeline (ingest → embed → score) ───────────────────────────
pipeline: check-inputs
	$(PYTHON) db/ingest.py
	$(PYTHON) embed/embed_postings.py
	$(PYTHON) classifier/scorer.py
	$(PYTHON) -m embed.embed_resume

# ── serve ────────────────────────────────────────────────────────
serve:
	$(PYTHON) -m uvicorn web.main:app --host 127.0.0.1 --port 8000 --reload

# ── run: scrape + pipeline + serve ──────────────────────────────
run: check-resume scrape pipeline serve

# ── individual steps (still available) ──────────────────────────
ingest:
	$(PYTHON) db/ingest.py

embed:
	$(PYTHON) embed/embed_postings.py

score: check-resume
	$(PYTHON) classifier/scorer.py
	$(PYTHON) -m embed.embed_resume

test:
	.venv/bin/pytest scraper/test_scraper.py -v
