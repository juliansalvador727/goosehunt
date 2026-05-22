"""FastAPI app — serves /api/postings and the Alpine.js UI."""

import sqlite3
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

DB_PATH = Path(__file__).parent.parent / "data" / "postings.db"
STATIC_DIR = Path(__file__).parent / "static"

COLUMNS = [
    "job_id", "board_type", "title", "org", "location",
    "deadline", "deadline_iso", "work_term", "openings",
    "summary", "responsibilities", "required_skills",
    "scraped_at", "updated_at",
    "score_firmware", "score_embedded", "score_hardware",
    "score_software", "score_fde", "score_mts",
    "score_power_electronics", "score_resume",
]

app = FastAPI()


@app.get("/api/postings")
def get_postings() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT {', '.join(COLUMNS)} FROM postings"
        ).fetchall()
    return [dict(r) for r in rows]


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
