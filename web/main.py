"""FastAPI app — serves /api/postings and the Alpine.js UI."""

import json
import re
import sqlite3
from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

DB_PATH = Path(__file__).parent.parent / "data" / "postings.db"
STATIC_DIR = Path(__file__).parent / "static"
_ROLES_PATH = Path(__file__).parent.parent / "config" / "roles.yaml"


def _load_role_keywords() -> dict[str, list[str]]:
    with open(_ROLES_PATH) as f:
        config = yaml.safe_load(f)
    return {role: [kw.lower() for kw in data["keywords"]] for role, data in config.items()}


_ROLE_KEYWORDS = _load_role_keywords()


def keyword_hits(text: str) -> dict[str, list[str]]:
    tl = text.lower()
    return {role: [kw for kw in kws if kw in tl] for role, kws in _ROLE_KEYWORDS.items()}

COLUMNS = [
    "job_id", "board_type", "title", "org", "location",
    "deadline", "deadline_iso", "work_term", "openings",
    "summary", "responsibilities", "required_skills",
    "raw_fields_json", "scraped_at", "updated_at",
    "score_firmware", "score_hardware",
    "score_software", "score_ai_ml", "score_resume",
]

app = FastAPI()

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

    tl = text.lower()

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


def extract_apply_info(raw_json: str) -> dict:
    try:
        d = json.loads(raw_json)
    except Exception:
        return {}

    delivery = (d.get("Application Delivery") or "").lower()
    email = (d.get("If By Email, Send To") or "").strip()
    add_info = d.get("Additional Application Information") or ""

    # Pull first URL out of additional info
    m = _URL_RE.search(add_info)
    link = m.group(0).rstrip(".,)") if m else None

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
    }


@app.get("/api/postings")
def get_postings() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT {', '.join(COLUMNS)} FROM postings"
        ).fetchall()

    result = []
    for r in rows:
        row = dict(r)
        raw = row.pop("raw_fields_json") or ""
        hourly = extract_comp_hourly(raw)
        row["comp_hourly"] = round(hourly, 2) if hourly is not None else None
        row["comp_score"] = round(comp_score(hourly), 3) if hourly is not None else None
        row.update(extract_apply_info(raw))
        text = " ".join(filter(None, [
            row.get("title"), row.get("org"),
            row.get("summary"), row.get("responsibilities"), row.get("required_skills"),
        ]))
        row["keyword_hits"] = keyword_hits(text)
        result.append(row)

    return result


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
