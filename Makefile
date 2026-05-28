.PHONY: install scrape test ingest embed score serve pipeline run check-resume check-inputs

ifeq ($(OS),Windows_NT)
PYTHON := .venv/Scripts/python.exe
PLAYWRIGHT := .venv/Scripts/playwright.exe
else
PYTHON := .venv/bin/python
PLAYWRIGHT := .venv/bin/playwright
endif

# ── setup ────────────────────────────────────────────────────────
install:
	uv venv
	uv pip install -r requirements.txt
	$(PLAYWRIGHT) install chromium

# ── preflight ────────────────────────────────────────────────────
check-resume:
	$(PYTHON) scripts/preflight.py --resume

check-inputs:
	$(PYTHON) scripts/preflight.py --resume --jsonl

# ── scraper ──────────────────────────────────────────────────────
BOARD ?= direct

scrape:
	$(PYTHON) -m scraper.scraper --board $(BOARD)

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
# Usage: make run BOARD=full_cycle  (default BOARD=direct)
run: check-resume
	@echo goosehunt board=$(BOARD)
	$(MAKE) scrape pipeline serve BOARD=$(BOARD)

# ── individual steps (still available) ──────────────────────────
ingest:
	$(PYTHON) db/ingest.py

embed:
	$(PYTHON) embed/embed_postings.py

score: check-resume
	$(PYTHON) classifier/scorer.py
	$(PYTHON) -m embed.embed_resume

test:
	$(PYTHON) -m pytest scraper/test_scraper.py -v
