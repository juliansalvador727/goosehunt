"""FastAPI app — serves /api/postings and the Alpine.js UI."""

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

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
    conn.commit()

_NUM = r"[\d,]+(?:\.\d+)?"
_SEP = r"\s*[-–/]\s*|\s+to\s+"   # separators between range bounds
_PER_H = r"(?:(?:per|an?)\s+|/\s*)?(?:hr|h|hour)s?\b"
_PER_W = r"(?:per\s+|/\s*)?weeks?\b"
_PER_M = r"(?:per\s+|/\s*)?(?:month|mo)\b"
_PER_Y = r"(?:per\s+|/\s*)?(?:year|annuall?y?|annum|yr|y)\b"
_PER_BW = r"(?:/\s*)?bi-?\s*weekly\b"

# Ordered by specificity — first match wins within each period bucket.
_HOURLY_RE = [
    # "$25 - $29 hourly" / "$25-$30/hr" / "$25 to $30 per hour"
    re.compile(rf"\$({_NUM})(?:{_SEP})\$?({_NUM})\s*(?:hourly|{_PER_H})", re.I),
    # bare range with hr suffix: "20.29 - 24.11 /hour"
    re.compile(rf"({_NUM})(?:{_SEP})({_NUM})\s*{_PER_H}", re.I),
    # single: "$27 per hour" / "$27/hr" / "$25 CAD/hour"
    re.compile(rf"\$({_NUM})\s*(?:[A-Z]{{2,3}})?\s*/\s*(?:hr|h|hour)s?\b", re.I),
    re.compile(rf"\$({_NUM})\s*(?:per|an?)\s+(?:hr|h|hour)s?\b", re.I),
    re.compile(rf"\$({_NUM})\s*hourly\b", re.I),
    # "28-35$/hr" — $ after number
    re.compile(rf"({_NUM})\s*\$\s*/\s*(?:hr|h|hour)s?\b", re.I),
    # "Hourly Rate/Salary: 20.29 - 24.11" / "Pay Range: $21.73-$26.65"
    re.compile(rf"hourly\s+(?:rate|wage|pay)[^$\d]{{0,30}}\$?({_NUM})(?:{_SEP})\$?({_NUM})", re.I),
    re.compile(rf"hourly\s+(?:rate|wage|pay)[^$\d]{{0,30}}\$?({_NUM})", re.I),
    re.compile(rf"pay\s+range[^$\d]{{0,10}}\$?({_NUM})(?:{_SEP})\$?({_NUM})", re.I),
    re.compile(rf"pay\s+range[^$\d]{{0,10}}\$?({_NUM})", re.I),
    # "The hourly wage...is $26.49"  (multi-line ok)
    re.compile(rf"hourly.{{0,120}}\$({_NUM})", re.I | re.S),
    # "Pay rate $20.60"
    re.compile(rf"pay\s+rate[^$\d]{{0,10}}\$?({_NUM})", re.I),
    # "Rate: $19 - $24"
    re.compile(rf"\brate[:\s]+\$({_NUM})(?:{_SEP})\$?({_NUM})", re.I),
    re.compile(rf"\brate[:\s]+\$({_NUM})", re.I),
    # "Salary range: $22 - $28 per hour"
    re.compile(rf"salary\s+(?:range|scale)[^$\d]{{0,20}}\$({_NUM})(?:{_SEP})\$?({_NUM})\s*{_PER_H}", re.I),
    re.compile(rf"salary\s+(?:range|scale)[^$\d]{{0,20}}\$({_NUM})\s*{_PER_H}", re.I),
    # "Bachelor $22/h, Master $28/h"
    re.compile(rf"\$({_NUM})\s*/\s*h\b", re.I),
]
_WEEKLY_RE = [
    re.compile(rf"\$({_NUM})(?:{_SEP})\$?({_NUM})\s*{_PER_W}", re.I),
    re.compile(rf"\$({_NUM})\s*{_PER_W}", re.I),
    re.compile(rf"({_NUM})\s*/\s*week\b", re.I),
    re.compile(rf"({_NUM})(?:{_SEP})({_NUM})\s*{_PER_W}", re.I),
    re.compile(rf"\$({_NUM})(?:{_SEP})\$?({_NUM})\s*/\s*weekly\b", re.I),
    re.compile(rf"\$({_NUM})\s*/\s*weekly\b", re.I),
]
_BIWEEKLY_RE = [
    re.compile(rf"\$({_NUM})(?:{_SEP})\$?({_NUM})\s*{_PER_BW}", re.I),
    re.compile(rf"\$({_NUM})\s*{_PER_BW}", re.I),
    re.compile(rf"{_PER_BW}[^$\d]{{0,20}}\$({_NUM})(?:{_SEP})\$?({_NUM})", re.I),
    re.compile(rf"{_PER_BW}[^$\d]{{0,10}}\$?({_NUM})", re.I),
    # "salary range: $2,045 - $2,523 / bi-weekly"
    re.compile(rf"salary\s+range[^$\d]{{0,20}}\$({_NUM})(?:{_SEP})\$?({_NUM})\s*{_PER_BW}", re.I),
]
_MONTHLY_RE = [
    re.compile(rf"\$({_NUM})(?:{_SEP})\$?({_NUM})\s*{_PER_M}", re.I),
    re.compile(rf"\$({_NUM})\s*{_PER_M}", re.I),
    re.compile(rf"({_NUM})\s*(?:CAD|USD)?\s*{_PER_M}", re.I),
    re.compile(rf"({_NUM})(?:{_SEP})({_NUM})\s*{_PER_M}", re.I),
    # "monthly salary range...is $4,264 to $5,200"
    re.compile(rf"monthly.{{0,80}}\$({_NUM})(?:{_SEP})\$?({_NUM})", re.I | re.S),
    re.compile(rf"monthly.{{0,80}}\$({_NUM})", re.I | re.S),
    # "$4000/mo"
    re.compile(rf"\$({_NUM})\s*/\s*mo\b", re.I),
    # "Targeting $4000/mo CAD"
    re.compile(rf"\$({_NUM})(?:{_SEP})\$?({_NUM})\s*/\s*mo\b", re.I),
]
_ANNUAL_RE = [
    # explicit annual with (per year) suffix like "(per year)"
    re.compile(rf"\$({_NUM})(?:{_SEP})\$?({_NUM})\s*\(?{_PER_Y}\)?", re.I),
    re.compile(rf"\$({_NUM})\s*\(?{_PER_Y}\)?", re.I),
    re.compile(rf"({_NUM})(?:{_SEP})({_NUM})\s*{_PER_Y}", re.I),
    re.compile(rf"({_NUM})\s*{_PER_Y}", re.I),
    # "annual base salary range...is $X - $Y"
    re.compile(rf"annual.{{0,60}}\$({_NUM})(?:{_SEP})\$?({_NUM})", re.I | re.S),
    re.compile(rf"annual.{{0,60}}\$({_NUM})", re.I | re.S),
    # "Projected Minimum Salary per year\n57,886.40"
    re.compile(rf"minimum\s+salary\s+per\s+year\D{{0,5}}({_NUM})", re.I | re.S),
    # "Salary Range$X to $Y CAD per year" (no space before $)
    re.compile(rf"salary\s+range\$({_NUM})(?:{_SEP})\$?({_NUM})\s*(?:[A-Z]{{2,3}}\s*)?{_PER_Y}", re.I),
    # biweekly salary lines like "Annual salary: $2,257 - 2,658 biweekly"
    re.compile(rf"annual\s+salary[^$\d]{{0,20}}\$?({_NUM})(?:{_SEP})\$?({_NUM})\s*{_PER_BW}", re.I),
]


def _n(s: str) -> float:
    return float(s.replace(",", ""))


def _mid(a: str, b: str | None = None) -> float:
    return (_n(a) + _n(b)) / 2 if b else _n(a)


def _first(patterns: list[re.Pattern], text: str) -> float | None:
    for p in patterns:
        m = p.search(text)
        if m:
            groups = [g for g in m.groups() if g and re.match(r"[\d,]", g)]
            if not groups:
                continue
            try:
                if len(groups) >= 2:
                    return _mid(groups[0], groups[1])
                return _mid(groups[0])
            except ValueError:
                continue
    return None


# Co-op full-time hours: 37.5–40 hrs/week. Use 40 for conversions.
_HRS_WEEK = 40.0


def extract_comp_hourly(raw_json: str) -> float | None:
    """Return estimated hourly CAD rate from raw_fields_json, or None."""
    try:
        d = json.loads(raw_json)
    except Exception:
        return None

    text = d.get("Compensation and Benefits") or ""
    if not text or len(text) < 4:
        return None

    # Hardcoded currency conversion — HKD figures are not CAD
    _HKD_TO_CAD = 0.175
    is_hkd = text.upper().startswith("HKD") or " HKD" in text.upper()

    tl = text.lower()

    hourly = _extract_hourly_raw(text, tl)
    if hourly is None:
        return None
    return hourly * _HKD_TO_CAD if is_hkd else hourly


def _extract_hourly_raw(text: str, tl: str) -> float | None:
    # Try hourly first — most common for co-op
    v = _first(_HOURLY_RE, text)
    if v and 10 <= v <= 300:
        return v

    # Bi-weekly (before weekly to avoid false matches on "bi-weekly" vs "week")
    v = _first(_BIWEEKLY_RE, text)
    if v and 500 <= v <= 20_000:
        return v / (_HRS_WEEK * 2)

    # Weekly
    v = _first(_WEEKLY_RE, text)
    if v and 300 <= v <= 10_000:
        return v / _HRS_WEEK

    # Monthly
    v = _first(_MONTHLY_RE, text)
    if v and 1_000 <= v <= 50_000:
        return v / (_HRS_WEEK * 52 / 12)

    # Annual
    v = _first(_ANNUAL_RE, text)
    if v and 10_000 <= v <= 500_000:
        return v / (_HRS_WEEK * 52)

    # Fallback: if "hourly" or "per hour" appears anywhere, grab the first $ amount
    if re.search(r"\bhourly\b|per hour\b", tl):
        m = re.search(rf"\$({_NUM})", text)
        if m:
            amt = _n(m.group(1))
            if 10 <= amt <= 300:
                return amt

    # Fallback: bare "$X to $Y" or "$X-$Y" — treat as hourly if midpoint in [10,100]
    m = re.search(rf"\$({_NUM})\s*[-–]\s*\$?({_NUM})", text)
    if not m:
        m = re.search(rf"\$({_NUM})\s+to\s+\$?({_NUM})", text)
    if m:
        try:
            mid = _mid(m.group(1), m.group(2))
            if 10 <= mid <= 100:
                return mid
        except ValueError:
            pass

    # Fallback: "Starting at $X.XX" where value looks hourly
    m = re.search(rf"starting\s+at\s+\$({_NUM})", text, re.I)
    if m:
        try:
            amt = _n(m.group(1))
            if 10 <= amt <= 100:
                return amt
        except ValueError:
            pass

    return None


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
        hourly = extract_comp_hourly(raw)
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
