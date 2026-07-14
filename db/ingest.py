"""JSONL → SQLite ingest for goosehunt postings."""

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Iterator

from dateutil import parser as dateutil_parser

logging.basicConfig(format="%(levelname)s: %(message)s", level=logging.WARNING)
log = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "postings.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"
JSONL_PATH = DATA_DIR / "postings.jsonl"

_INSERT_SQL = """
INSERT INTO postings (
    job_id, board_type, title, org, location,
    deadline, deadline_iso, work_term, openings, apps_count,
    summary, responsibilities, required_skills,
    raw_fields_json, scraped_at, updated_at
) VALUES (
    :job_id, :board_type, :title, :org, :location,
    :deadline, :deadline_iso, :work_term, :openings, :apps_count,
    :summary, :responsibilities, :required_skills,
    :raw_fields_json, :scraped_at, :updated_at
)
"""

_UPDATE_SQL = """
UPDATE postings SET
    board_type       = :board_type,
    title            = :title,
    org              = :org,
    location         = :location,
    deadline         = :deadline,
    deadline_iso     = :deadline_iso,
    work_term        = :work_term,
    openings         = :openings,
    apps_count       = :apps_count,
    summary          = :summary,
    responsibilities = :responsibilities,
    required_skills  = :required_skills,
    raw_fields_json  = :raw_fields_json,
    updated_at       = :updated_at
WHERE job_id = :job_id
"""


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(postings)").fetchall()
    }
    if "status" not in columns:
        conn.execute("ALTER TABLE postings ADD COLUMN status TEXT NOT NULL DEFAULT 'new'")
    if "apps_count" not in columns:
        conn.execute("ALTER TABLE postings ADD COLUMN apps_count INTEGER")
    conn.commit()


def parse_deadline_iso(raw: str | None) -> str | None:
    if not raw:
        return None
    normalized = re.sub(r"\s+", " ", raw).strip()
    try:
        return dateutil_parser.parse(normalized).isoformat()
    except (ValueError, OverflowError):
        log.warning("Could not parse deadline: %r", raw)
        return None


def coerce_openings(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None


def coerce_apps_count(raw: str | int | None) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None


def load_jsonl(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def build_params(record: dict) -> dict:
    return {
        "job_id":          record["job_id"],
        "board_type":      record.get("board_type"),
        "title":           record.get("title"),
        "org":             record.get("org"),
        "location":        record.get("location"),
        "deadline":        record.get("deadline"),
        "deadline_iso":    parse_deadline_iso(record.get("deadline")),
        "work_term":       record.get("work_term"),
        "openings":        coerce_openings(record.get("openings")),
        "apps_count":      coerce_apps_count(record.get("apps_count")),
        "summary":         record.get("summary"),
        "responsibilities": record.get("responsibilities"),
        "required_skills": record.get("required_skills"),
        "raw_fields_json": record.get("raw_fields_json"),
        "scraped_at":      record.get("scraped_at"),
        "updated_at":      record.get("updated_at"),
    }


def load_listing_manifests() -> dict[str, set[str]]:
    """Read scraper listing manifests → {board_type: set of currently-listed job_ids}.

    Each `data/listing_<board>.json` is written by the scraper and reflects the
    full set of job IDs visible on that board during the latest scrape.
    """
    manifests: dict[str, set[str]] = {}
    for path in sorted(DATA_DIR.glob("listing_*.json")):
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Could not read listing manifest %s: %s", path.name, exc)
            continue
        board = payload.get("board_type")
        ids = payload.get("job_ids") or []
        if board and ids:
            manifests[board] = {str(j) for j in ids}
    return manifests


def purge_unlisted(conn: sqlite3.Connection) -> int:
    """Delete postings no longer present in their board's latest listing.

    Replaces the old deadline-based purge: liveness now comes from what the
    scraper actually saw on the board, not from a possibly-stale stored deadline.
    Boards without a manifest are left untouched (nothing to compare against).
    """
    manifests = load_listing_manifests()
    if not manifests:
        return 0

    removed = 0
    for board, live_ids in manifests.items():
        rows = conn.execute(
            "SELECT job_id FROM postings WHERE board_type = ?", (board,)
        ).fetchall()
        stale = [r[0] for r in rows if r[0] not in live_ids]
        if stale:
            conn.executemany(
                "DELETE FROM postings WHERE job_id = ?", [(j,) for j in stale]
            )
            removed += len(stale)
    conn.commit()
    return removed


def upsert_posting(conn: sqlite3.Connection, params: dict) -> str:
    row = conn.execute(
        "SELECT raw_fields_json FROM postings WHERE job_id = ?",
        (params["job_id"],),
    ).fetchone()

    if row is None:
        conn.execute(_INSERT_SQL, params)
        return "inserted"

    if row[0] == params["raw_fields_json"]:
        return "skipped"

    conn.execute(_UPDATE_SQL, params)
    return "updated"


def main() -> None:
    if not JSONL_PATH.exists():
        print(f"Error: {JSONL_PATH} not found. Run `make scrape` first.")
        raise SystemExit(1)

    counts = {"inserted": 0, "updated": 0, "skipped": 0}

    with sqlite3.connect(DB_PATH) as conn:
        init_db(conn)
        for record in load_jsonl(JSONL_PATH):
            params = build_params(record)
            result = upsert_posting(conn, params)
            counts[result] += 1
        conn.commit()
        removed = purge_unlisted(conn)

    print(f"Ingested {JSONL_PATH} → {DB_PATH}")
    print(
        f"Inserted: {counts['inserted']}  "
        f"Updated: {counts['updated']}  "
        f"Skipped: {counts['skipped']}"
    )
    if removed:
        print(f"Removed {removed} posting(s) no longer listed on their board.")


if __name__ == "__main__":
    main()
