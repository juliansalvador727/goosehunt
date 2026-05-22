.PHONY: install scrape scrape-diag test

install:
	python -m venv .venv
	.venv/bin/pip install -r requirements.txt
	.venv/bin/playwright install chromium

scrape:
	.venv/bin/python -m scraper.scraper

scrape-diag:
	.venv/bin/python -m scraper.scraper --diag

test:
	.venv/bin/pytest scraper/test_scraper.py -v
