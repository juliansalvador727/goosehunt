.PHONY: install scrape scrape-diag test ingest embed score serve

PYTHON := .venv/bin/python

install:
	python -m venv .venv
	.venv/bin/pip install -r requirements.txt
	.venv/bin/playwright install chromium

scrape:
	$(PYTHON) -m scraper.scraper

scrape-diag:
	$(PYTHON) -m scraper.scraper --diag

test:
	.venv/bin/pytest scraper/test_scraper.py -v

ingest:
	$(PYTHON) db/ingest.py

embed:
	$(PYTHON) embed/embed_postings.py

score:
	$(PYTHON) classifier/scorer.py
	$(PYTHON) -m embed.embed_resume

serve:
	$(PYTHON) -m uvicorn web.main:app --host 127.0.0.1 --port 8000 --reload
