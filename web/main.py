"""FastAPI app — serves /api/postings and the Alpine.js UI."""

import json
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import numpy as np
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from db.compensation import (
    COMPENSATION_COLUMN_TYPES,
    COMPENSATION_VERSION,
    normalize_compensation,
)
from web.applied import parse_applied_job_ids

DB_PATH = Path(__file__).parent.parent / "data" / "postings.db"
STATIC_DIR = Path(__file__).parent / "static"
_ROLES_PATH = Path(__file__).parent.parent / "config" / "roles.yaml"


def _load_role_keywords() -> dict[str, list[str]]:
    with open(_ROLES_PATH) as f:
        config = yaml.safe_load(f)
    return {role: [kw.lower() for kw in data["keywords"]] for role, data in config.items()}


_ROLE_KEYWORDS = _load_role_keywords()
STATUS_VALUES = {"new", "maybe", "applied", "ignored"}
PLACEHOLDER_TEXTS = {
    "key responsibilities",
    "responsibilities",
    "qualifications",
    "requirements",
}
PLACEHOLDER_PREFIXES = (
    "check our list of projects",
)


def keyword_hits(text: str) -> dict[str, list[str]]:
    tl = text.lower()
    return {role: [kw for kw in kws if kw in tl] for role, kws in _ROLE_KEYWORDS.items()}


def clean_posting_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    if normalized in PLACEHOLDER_TEXTS:
        return ""
    if any(normalized.startswith(prefix) for prefix in PLACEHOLDER_PREFIXES):
        return ""
    return text

COLUMNS = [
    "job_id", "board_type", "title", "org", "location",
    "deadline", "deadline_iso", "work_term", "openings", "apps_count",
    "summary", "responsibilities", "required_skills",
    "raw_fields_json", "scraped_at", "updated_at", "status",
    "score_firmware", "score_hardware",
    "score_software", "score_ai_ml", "score_resume",
    "comp_raw_text", "comp_native_min", "comp_native_max",
    "comp_currency", "comp_period", "comp_hours_per_week",
    "comp_hourly_native_min", "comp_hourly_native_max",
    "comp_hourly_cad_min", "comp_hourly_cad_max", "comp_hourly_cad_mid",
    "comp_fx_rate", "comp_fx_date", "comp_parse_status", "comp_confidence",
    "comp_tiers_json", "comp_parser_version",
]

app = FastAPI()


def ensure_postings_schema(conn: sqlite3.Connection) -> None:
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(postings)").fetchall()
    }
    if not columns:
        raise sqlite3.OperationalError("postings table missing")
    if "status" not in columns:
        conn.execute("ALTER TABLE postings ADD COLUMN status TEXT NOT NULL DEFAULT 'new'")
    if "apps_count" not in columns:
        conn.execute("ALTER TABLE postings ADD COLUMN apps_count INTEGER")
    for name, sql_type in COMPENSATION_COLUMN_TYPES.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE postings ADD COLUMN {name} {sql_type}")
    conn.commit()
def extract_comp_hourly(raw_json: str) -> float | None:
    """Compatibility helper; ingest persists this CAD midpoint on each posting."""
    return normalize_compensation(raw_json)["comp_hourly_cad_mid"]


# Normalize $16–$60/hr → 0–1; anything outside is clamped.
_COMP_LOW = 16.0
_COMP_HIGH = 60.0


def comp_score(hourly: float | None) -> float | None:
    if hourly is None:
        return None
    return max(0.0, min(1.0, (hourly - _COMP_LOW) / (_COMP_HIGH - _COMP_LOW)))


_URL_RE = re.compile(r'https?://[^\s\]>)\'"]+')

# Labels whose anchor hrefs (scraper `_links`) count as an application link.
_APPLY_LINK_LABELS = (
    "If By Website, Go To",
    "Application Method",
    "Additional Application Information",
)


# Hosts that show up in application text but are never where you apply
# (team LinkedIn profiles, UW visa/work-abroad pages, image searches).
_NON_APPLY_HOSTS = ("linkedin.com/in/", "uwaterloo.ca", "google.com")


def extract_apply_info(raw_json: str) -> dict:
    try:
        d = json.loads(raw_json)
    except Exception:
        return {}

    links = d.get("_links") or {}
    hrefs = [h for label in _APPLY_LINK_LABELS for h in links.get(label, [])]
    mailto = next((h[7:] for h in hrefs if h.lower().startswith("mailto:")), "")
    add_info = d.get("Additional Application Information") or ""

    # Explicit website field first, then anchor hrefs, then bare URLs in the text.
    candidates = [(d.get("If By Website, Go To") or "").strip()]
    candidates += [h for h in hrefs if h.lower().startswith("http")]
    candidates += _URL_RE.findall(add_info)
    apply_links: list[str] = []
    for url in candidates:
        url = url.rstrip(".,)")
        if url and url not in apply_links and not any(h in url.lower() for h in _NON_APPLY_HOSTS):
            apply_links.append(url)

    delivery = (d.get("Application Delivery") or "").lower()
    email = (d.get("If By Email, Send To") or "").strip() or mailto
    link = apply_links[0] if apply_links else None

    if "email" in delivery or email:
        method = "email"
    elif "website" in delivery or link:
        method = "link"
    else:
        method = "ww"

    return {
        "apply_method": method,
        "apply_email": email or None,
        "apply_link": link,
        "apply_links": apply_links,
    }


_MONTHS_RE = re.compile(r"(\d+)\s*month", re.I)


def extract_posting_attrs(raw_json: str) -> dict:
    """Duration, arrangement, level, location, and documents from raw_fields_json."""
    try:
        d = json.loads(raw_json)
    except Exception:
        return {}

    duration = (d.get("Work Term Duration") or "").strip()
    m = _MONTHS_RE.search(duration)
    # WW joins multi-level values with whitespace runs ("Junior\n\t\tIntermediate").
    levels = (d.get("Level") or "").split()
    docs = [x.strip() for x in (d.get("Application Documents Required") or "").split(",")]
    docs = [x for x in docs if x]
    return {
        "work_term_duration": duration or None,
        "duration_months": int(m.group(1)) if m else None,
        "arrangement": (d.get("Employment Location Arrangement") or "").strip() or None,
        "level": ", ".join(levels) or None,
        "levels": levels,
        "country": (d.get("Job - Country") or "").strip() or None,
        "region": (d.get("Region") or "").strip() or None,
        "documents_required": docs,
        "needs_cover_letter": any("cover letter" in x.lower() for x in docs),
        "special_dates": (d.get("Special Work Term Start/End Date Considerations") or "").strip() or None,
    }


_TAG_RE = re.compile(r"<[^>]+>")


def _section_title(section: dict) -> str:
    """Lowercase title with HTML and the trailing org/division name removed.

    Real titles look like "<b>Hiring History</b>", "Hires by Faculty<br>Acme - HQ",
    "Most Frequently Hired Programs - Acme - HQ".
    """
    text = _TAG_RE.sub("\n", str(section.get("title") or "")).strip()
    return text.split("\n")[0].split(" - ")[0].strip().lower()


# "Hires by Student Work Term Number" pie slices are ordinal words.
_ORDINALS = {
    "first": "1", "second": "2", "third": "3", "fourth": "4",
    "fifth": "5", "sixth": "6", "seventh": "7", "eighth": "8",
}


def _term_number(name: str) -> str | None:
    m = re.search(r"\d+", name)
    if m:
        return m.group(0)
    for word, num in _ORDINALS.items():
        if name.lower().startswith(word):
            return num
    return None


def _division_row(section: dict) -> list:
    """Rows are [org, division, (all students)]; prefer the division row."""
    rows = section.get("rows") or []
    for row in rows:
        if row and "division" in str(row[0]).lower():
            return row
    return rows[0] if rows else []


def _num(value) -> float | None:
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (ValueError, TypeError):
        return None


def _pie_data(section: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    for point in section.get("data") or []:
        y = _num(point.get("y"))
        if y is not None and point.get("name"):
            out[str(point["name"])] = y
    return out


def extract_ratings(raw_json: str) -> dict:
    """Summarise the `_ratings` sections (WW work term ratings tab) stored by the scraper.

    Shape follows bryanling1/waterlooworks-scraper's typing of
    getWorkTermRatingReportJson: table / pieChart / barChart / columnChart sections
    whose titles carry the employer name (e.g. "Hires by Faculty<br>Acme").
    """
    empty = {
        "hires_total": None,
        "hires_by_term": {},
        "hires_by_faculty": {},
        "top_programs": [],
        "rating_avg": None,
        "rating_count": None,
        "rating_all_avg": None,
    }
    try:
        sections = json.loads(raw_json).get("_ratings") or []
    except Exception:
        return empty
    if not isinstance(sections, list):
        return empty

    out = dict(empty)
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        title = _section_title(sec)
        kind = sec.get("type")
        if kind == "table" and title.startswith("hiring history"):
            # Row: ["Employer Division", "<name>", <count per term> x 9]
            nums = [n for n in (_num(c) for c in _division_row(sec)[2:]) if n is not None]
            if nums:
                out["hires_total"] = int(sum(nums))
        elif kind == "table" and title.startswith("work term ratings summary"):
            # Row: ["Employer Division", "<name>", "<avg /10>", "<count>"]
            row = _division_row(sec)
            if len(row) >= 4:
                out["rating_avg"] = _num(row[2])
                count = _num(row[3])
                out["rating_count"] = int(count) if count is not None else None
            for row in sec.get("rows") or []:
                if row and "all co-op" in str(row[0]).lower() and len(row) >= 3:
                    out["rating_all_avg"] = _num(row[2])
        elif kind == "pieChart" and title.startswith("hires by") and "work term" in title:
            for name, pct in _pie_data(sec).items():
                num = _term_number(name)
                if num:
                    out["hires_by_term"][num] = pct
        elif kind == "pieChart" and title.startswith("hires by faculty"):
            out["hires_by_faculty"] = _pie_data(sec)
        elif kind in ("barChart", "columnChart") and title.startswith("most frequently hired"):
            cats = sec.get("categories") or []
            series = sec.get("series") or []
            data = series[0].get("data") or [] if series else []
            pairs = [
                (str(c), int(v)) for c, v in zip(cats, data)
                if c and _num(v) is not None and _num(v) > 0
            ]
            out["top_programs"] = sorted(pairs, key=lambda x: -x[1])[:3]
    return out


@app.get("/api/postings")
def get_postings() -> list[dict]:
    if not DB_PATH.exists():
        raise HTTPException(
            status_code=503,
            detail="Postings database not found. Run `make scrape && make pipeline` first.",
        )

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        try:
            ensure_postings_schema(conn)
            rows = conn.execute(
                f"SELECT {', '.join(COLUMNS)} FROM postings"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            raise HTTPException(
                status_code=503,
                detail="Postings database is not initialized. Run `make ingest` or `make pipeline` first.",
            ) from exc

    result = []
    for r in rows:
        row = dict(r)
        raw = row.pop("raw_fields_json") or ""
        row["responsibilities"] = clean_posting_text(row.get("responsibilities"))
        row["required_skills"] = clean_posting_text(row.get("required_skills"))
        # Legacy databases get an in-memory fallback until the next ingest backfills
        # the persisted compensation columns.
        if row.get("comp_parser_version") != COMPENSATION_VERSION:
            row.update(normalize_compensation(raw))
        tiers_json = row.pop("comp_tiers_json", None)
        try:
            row["comp_tiers"] = json.loads(tiers_json) if tiers_json else []
        except (TypeError, json.JSONDecodeError):
            row["comp_tiers"] = []
        hourly = row.get("comp_hourly_cad_mid")
        row["comp_hourly"] = round(hourly, 2) if hourly is not None else None
        row["comp_score"] = round(comp_score(hourly), 3) if hourly is not None else None
        row.update(extract_apply_info(raw))
        row.update(extract_posting_attrs(raw))
        row.update(extract_ratings(raw))
        text = " ".join(filter(None, [
            row.get("title"), row.get("org"),
            row.get("summary"), row.get("responsibilities"), row.get("required_skills"),
        ]))
        row["keyword_hits"] = keyword_hits(text)
        result.append(row)

    return result


# ── Semantic search ───────────────────────────────────────────────────────────
# Same model as embed/embed_postings.py. Not imported from there: that module pulls
# torch in at import time, which would slow every server start and test run.
MODEL_NAME = "all-MiniLM-L6-v2"
_MODEL = None
_MODEL_LOCK = threading.Lock()
_MATRIX_CACHE: tuple[int, list[str], np.ndarray] | None = None  # (db mtime_ns, ids, matrix)


def _get_model():
    """Load the sentence-transformer once per process (lazy, thread-safe)."""
    global _MODEL
    with _MODEL_LOCK:
        if _MODEL is None:
            from sentence_transformers import SentenceTransformer
            _MODEL = SentenceTransformer(MODEL_NAME)
        return _MODEL


def load_embedding_matrix(conn: sqlite3.Connection) -> tuple[list[str], np.ndarray]:
    rows = conn.execute(
        "SELECT job_id, embedding FROM postings WHERE embedding IS NOT NULL"
    ).fetchall()
    ids = [r[0] for r in rows]
    if not rows:
        return ids, np.zeros((0, 384), dtype=np.float32)
    matrix = np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return ids, matrix / np.where(norms == 0, 1, norms)


def _get_matrix() -> tuple[list[str], np.ndarray]:
    """Embeddings for all postings, reloaded whenever the DB file changes."""
    global _MATRIX_CACHE
    mtime = DB_PATH.stat().st_mtime_ns
    if _MATRIX_CACHE is None or _MATRIX_CACHE[0] != mtime:
        with sqlite3.connect(DB_PATH) as conn:
            ids, matrix = load_embedding_matrix(conn)
        _MATRIX_CACHE = (mtime, ids, matrix)
    return _MATRIX_CACHE[1], _MATRIX_CACHE[2]


def rank_embeddings(matrix: np.ndarray, ids: list[str], qvec) -> list[tuple[str, float]]:
    """Cosine similarity of every row against qvec, best first."""
    q = np.asarray(qvec, dtype=np.float32)
    norm = np.linalg.norm(q)
    if norm:
        q = q / norm
    scores = matrix @ q
    order = np.argsort(-scores)
    return [(ids[i], float(scores[i])) for i in order]


@app.get("/api/search")
def semantic_search(q: str = "") -> dict[str, float]:
    """Rank postings by semantic similarity to q. Empty q only warms the model."""
    if not DB_PATH.exists():
        raise HTTPException(
            status_code=503,
            detail="Postings database not found. Run `make scrape && make pipeline` first.",
        )
    try:
        ids, matrix = _get_matrix()
    except sqlite3.OperationalError as exc:
        raise HTTPException(
            status_code=503,
            detail="Postings database is not initialized. Run `make ingest` or `make pipeline` first.",
        ) from exc
    if not ids:
        raise HTTPException(
            status_code=503,
            detail="No posting embeddings yet. Run `make embed` or `make pipeline` first.",
        )
    model = _get_model()
    q = q.strip()
    if not q:
        return {}
    with _MODEL_LOCK:
        vec = model.encode(q, convert_to_numpy=True)
    return {job_id: round(score, 4) for job_id, score in rank_embeddings(matrix, ids, vec)}


@app.patch("/api/postings/{job_id}/status")
def update_posting_status(job_id: str, payload: dict) -> dict:
    status = payload.get("status")
    if status not in STATUS_VALUES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Expected one of: {', '.join(sorted(STATUS_VALUES))}.",
        )
    if not DB_PATH.exists():
        raise HTTPException(
            status_code=503,
            detail="Postings database not found. Run `make scrape && make pipeline` first.",
        )

    with sqlite3.connect(DB_PATH) as conn:
        try:
            ensure_postings_schema(conn)
            cur = conn.execute(
                "UPDATE postings SET status = ? WHERE job_id = ?",
                (status, job_id),
            )
            conn.commit()
        except sqlite3.OperationalError as exc:
            raise HTTPException(
                status_code=503,
                detail="Postings database is not initialized. Run `make ingest` or `make pipeline` first.",
            ) from exc

    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Posting not found.")
    return {"job_id": job_id, "status": status}


# SQLite's default parameter limit is 999; applications pages are far smaller
# than that, but chunk anyway so a multi-page paste can never overflow it.
_ID_CHUNK = 400


def _chunks(items: list[str], size: int = _ID_CHUNK):
    for i in range(0, len(items), size):
        yield items[i:i + size]


@app.post("/api/postings/applied")
def mark_applied(payload: dict) -> dict:
    """Mark every posting named in a pasted WaterlooWorks applications page as applied.

    Additive only: IDs missing from the paste keep whatever status they have.
    """
    job_ids = parse_applied_job_ids(payload.get("text") or "")
    if not job_ids:
        raise HTTPException(
            status_code=400,
            detail="No job IDs found in that paste. Copy the whole Applications page and try again.",
        )
    if not DB_PATH.exists():
        raise HTTPException(
            status_code=503,
            detail="Postings database not found. Run `make scrape && make pipeline` first.",
        )

    with sqlite3.connect(DB_PATH) as conn:
        try:
            ensure_postings_schema(conn)
            known: dict[str, str] = {}
            for chunk in _chunks(job_ids):
                placeholders = ", ".join("?" * len(chunk))
                known.update(
                    conn.execute(
                        f"SELECT job_id, status FROM postings WHERE job_id IN ({placeholders})",
                        chunk,
                    ).fetchall()
                )
            updated = [j for j in job_ids if known.get(j, "applied") != "applied"]
            for chunk in _chunks(updated):
                placeholders = ", ".join("?" * len(chunk))
                conn.execute(
                    f"UPDATE postings SET status = 'applied' WHERE job_id IN ({placeholders})",
                    chunk,
                )
            conn.commit()
        except sqlite3.OperationalError as exc:
            raise HTTPException(
                status_code=503,
                detail="Postings database is not initialized. Run `make ingest` or `make pipeline` first.",
            ) from exc

    matched = [j for j in job_ids if j in known]
    return {
        "parsed": len(job_ids),
        "matched": len(matched),
        "updated": len(updated),
        "already_applied": len(matched) - len(updated),
        "unknown": [j for j in job_ids if j not in known],
        "applied_job_ids": matched,
    }


@app.delete("/api/postings/expired")
def delete_expired_postings() -> dict:
    """Delete every posting whose deadline has already passed."""
    if not DB_PATH.exists():
        raise HTTPException(
            status_code=503,
            detail="Postings database not found. Run `make scrape && make pipeline` first.",
        )

    # deadline_iso is a naive local datetime ("YYYY-MM-DDTHH:MM:SS"), so a
    # lexicographic comparison against the current time is a valid ordering.
    now_iso = datetime.now().isoformat(timespec="seconds")

    with sqlite3.connect(DB_PATH) as conn:
        try:
            ensure_postings_schema(conn)
            cur = conn.execute(
                "DELETE FROM postings "
                "WHERE deadline_iso IS NOT NULL AND deadline_iso <> '' AND deadline_iso < ?",
                (now_iso,),
            )
            conn.commit()
        except sqlite3.OperationalError as exc:
            raise HTTPException(
                status_code=503,
                detail="Postings database is not initialized. Run `make ingest` or `make pipeline` first.",
            ) from exc

    return {"deleted": cur.rowcount}


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
