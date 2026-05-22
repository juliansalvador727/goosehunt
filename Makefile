.PHONY: install scrape scrape-diag test ingest embed score serve pipeline run

PYTHON := .venv/bin/python

# ── setup ────────────────────────────────────────────────────────
install:
	uv venv
	uv pip install -r requirements.txt
	.venv/bin/playwright install chromium

# ── scraper ──────────────────────────────────────────────────────
scrape:
	$(PYTHON) -m scraper.scraper

scrape-diag:
	$(PYTHON) -m scraper.scraper --diag

# ── pipeline (ingest → embed → score) ───────────────────────────
pipeline:
	$(PYTHON) db/ingest.py
	$(PYTHON) embed/embed_postings.py
	$(PYTHON) classifier/scorer.py
	$(PYTHON) -m embed.embed_resume

# ── serve ────────────────────────────────────────────────────────
serve:
	$(PYTHON) -m uvicorn web.main:app --host 127.0.0.1 --port 8000 --reload

# ── run: scrape + pipeline + serve ──────────────────────────────
run: scrape pipeline serve

# ── individual steps (still available) ──────────────────────────
ingest:
	$(PYTHON) db/ingest.py

embed:
	$(PYTHON) embed/embed_postings.py

score:
	$(PYTHON) classifier/scorer.py
	$(PYTHON) -m embed.embed_resume

test:
	.venv/bin/pytest scraper/test_scraper.py -v
