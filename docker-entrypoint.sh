#!/bin/sh
set -e
python db/ingest.py
python -m embed.embed_postings
python classifier/scorer.py
python -m embed.embed_resume
exec python -m uvicorn web.main:app --host 0.0.0.0 --port 8000
