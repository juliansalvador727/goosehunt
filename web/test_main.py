"""
Unit tests for request-time enrichment helpers in web/main.py.
No database, no server required.

Run: pytest web/test_main.py -v
"""

import json
import sqlite3
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

import web.main as main
from web.main import (
    app,
    extract_apply_info,
    extract_posting_attrs,
    extract_ratings,
    parse_applied_job_ids,
    rank_embeddings,
)

FIXTURES = Path(__file__).parent / "fixtures"
RATINGS_SECTIONS = json.loads((FIXTURES / "ratings_sample.json").read_text())["sections"]


def raw(**fields) -> str:
    return json.dumps(fields)


# ── extract_posting_attrs ─────────────────────────────────────────────────────

def test_duration_months_variants():
    assert extract_posting_attrs(raw(**{"Work Term Duration": "4 month work term"}))["duration_months"] == 4
    assert extract_posting_attrs(
        raw(**{"Work Term Duration": "8 month consecutive work term required"})
    )["duration_months"] == 8
    assert extract_posting_attrs(
        raw(**{"Work Term Duration": "8 month consecutive work term preferred"})
    )["duration_months"] == 8
    assert extract_posting_attrs(
        raw(**{"Work Term Duration": "2 work term commitment preferred"})
    )["duration_months"] is None


def test_level_whitespace_garble_is_split():
    attrs = extract_posting_attrs(raw(Level="Junior\n\t\t\t\t\n\t\t\tIntermediate"))
    assert attrs["level"] == "Junior, Intermediate"
    assert attrs["levels"] == ["Junior", "Intermediate"]


def test_documents_and_cover_letter():
    attrs = extract_posting_attrs(raw(**{
        "Application Documents Required":
            "University of Waterloo Co-op Work History,Cover Letter,Résumé,Grade Report",
        "Employment Location Arrangement": "Hybrid",
        "Job - Country": "Canada",
        "Region": "ON - Toronto",
    }))
    assert attrs["documents_required"] == [
        "University of Waterloo Co-op Work History", "Cover Letter", "Résumé", "Grade Report",
    ]
    assert attrs["needs_cover_letter"] is True
    assert attrs["arrangement"] == "Hybrid"
    assert attrs["country"] == "Canada"
    assert attrs["region"] == "ON - Toronto"


def test_attrs_missing_fields():
    attrs = extract_posting_attrs(raw())
    assert attrs["duration_months"] is None
    assert attrs["levels"] == []
    assert attrs["documents_required"] == []
    assert attrs["needs_cover_letter"] is False
    assert attrs["special_dates"] is None


def test_special_dates():
    attrs = extract_posting_attrs(raw(**{
        "Special Work Term Start/End Date Considerations": "January 4 to April 30, 2027",
    }))
    assert attrs["special_dates"] == "January 4 to April 30, 2027"


# ── extract_apply_info ────────────────────────────────────────────────────────

def test_apply_link_from_anchor_href():
    info = extract_apply_info(raw(**{
        "Application Method": "WaterlooWorks",
        "Additional Application Information": "Apply here too.",
        "_links": {"Additional Application Information": ["https://jobs.example.com/123"]},
    }))
    assert info["apply_method"] == "link"
    assert info["apply_link"] == "https://jobs.example.com/123"
    assert info["apply_email"] is None


def test_apply_email_from_mailto_href():
    info = extract_apply_info(raw(**{
        "_links": {"Additional Application Information": ["mailto:jobs@example.com"]},
    }))
    assert info["apply_method"] == "email"
    assert info["apply_email"] == "jobs@example.com"


def test_apply_link_regex_fallback_when_no_anchor():
    info = extract_apply_info(raw(**{
        "Additional Application Information":
            "Also apply at https://jobs.example.com/abc?x=1 to be considered.",
    }))
    assert info["apply_method"] == "link"
    assert info["apply_link"] == "https://jobs.example.com/abc?x=1"


def test_explicit_website_field_beats_anchor():
    info = extract_apply_info(raw(**{
        "If By Website, Go To": "https://explicit.example.com",
        "_links": {"Additional Application Information": ["https://other.example.com"]},
    }))
    assert info["apply_link"] == "https://explicit.example.com"


def test_apply_ww_only():
    info = extract_apply_info(raw(**{"Application Method": "WaterlooWorks"}))
    assert info == {
        "apply_method": "ww", "apply_email": None, "apply_link": None, "apply_links": [],
    }


def test_apply_links_skip_profiles_and_dedupe():
    info = extract_apply_info(raw(**{
        "Additional Application Information":
            "Meet the team, then apply at https://jobs.lever.co/acme/123.",
        "_links": {
            "Additional Application Information": [
                "https://www.linkedin.com/in/someone/",
                "https://uwaterloo.ca/co-operative-education/work-abroad",
                "https://jobs.lever.co/acme/123",
                "https://acme.example/careers",
            ],
        },
    }))
    assert info["apply_links"] == [
        "https://jobs.lever.co/acme/123", "https://acme.example/careers",
    ]
    assert info["apply_link"] == "https://jobs.lever.co/acme/123"
    assert info["apply_method"] == "link"


# ── extract_ratings ───────────────────────────────────────────────────────────

def test_ratings_from_fixture():
    r = extract_ratings(raw(_ratings=RATINGS_SECTIONS))
    assert r["hires_total"] == 51  # division row, 9 terms summed
    assert r["hires_by_faculty"] == {
        "Arts": 27.0, "Engineering": 53.0, "Mathematics": 16.0, "Science": 4.0,
    }
    assert r["hires_by_term"] == {
        "1": 4.0, "2": 20.0, "3": 27.0, "4": 24.0, "5": 22.0, "6": 4.0,
    }
    assert r["top_programs"] == [
        ("Electrical Engineering", 7), ("Mechatronics Engineering", 7),
        ("Nanotechnology Engineering", 7),
    ]
    assert r["rating_avg"] == 8.7
    assert r["rating_count"] == 27
    assert r["rating_all_avg"] == 8.5


def test_ratings_without_summary_table():
    sections = [s for s in RATINGS_SECTIONS if "ratings summary" not in str(s.get("title", "")).lower()]
    r = extract_ratings(raw(_ratings=sections))
    assert r["hires_total"] == 51
    assert r["rating_avg"] is None
    assert r["rating_count"] is None


def test_ratings_absent():
    r = extract_ratings(raw(**{"Job Title": "x"}))
    assert r["hires_total"] is None
    assert r["hires_by_term"] == {}
    assert r["top_programs"] == []


def test_ratings_ignores_malformed_sections():
    r = extract_ratings(raw(_ratings=["junk", {"type": "pieChart"}, {"type": "table", "title": "Hiring History"}]))
    assert r["hires_total"] is None


# ── semantic search ───────────────────────────────────────────────────────────

def _unit(i: int) -> np.ndarray:
    v = np.zeros(384, dtype=np.float32)
    v[i] = 1.0
    return v


def test_rank_embeddings_orders_by_cosine():
    matrix = np.stack([_unit(0), _unit(1), (_unit(0) + _unit(1)) / np.sqrt(2)])
    ranked = rank_embeddings(matrix, ["a", "b", "ab"], _unit(0) * 5)  # unnormalised query
    assert [jid for jid, _ in ranked] == ["a", "ab", "b"]
    assert ranked[0][1] == 1.0
    assert abs(ranked[1][1] - 1 / np.sqrt(2)) < 1e-6
    assert ranked[2][1] == 0.0


def _make_db(path: Path, embeddings: dict[str, np.ndarray | None]) -> None:
    schema = (Path(__file__).parent.parent / "db" / "schema.sql").read_text()
    with sqlite3.connect(path) as conn:
        conn.executescript(schema)
        for jid, vec in embeddings.items():
            conn.execute(
                "INSERT INTO postings (job_id, title, embedding) VALUES (?, ?, ?)",
                (jid, "t", vec.tobytes() if vec is not None else None),
            )


class _StubModel:
    def encode(self, text, convert_to_numpy=True):
        return _unit(1)


def test_search_endpoint(tmp_path, monkeypatch):
    db = tmp_path / "p.db"
    diag = ((_unit(0) + _unit(1)) / np.sqrt(2)).astype(np.float32)
    _make_db(db, {"a": _unit(0), "b": _unit(1), "c": diag})
    monkeypatch.setattr(main, "DB_PATH", db)
    monkeypatch.setattr(main, "_MATRIX_CACHE", None)
    monkeypatch.setattr(main, "_get_model", lambda: _StubModel())
    client = TestClient(app)

    r = client.get("/api/search", params={"q": "anything"})
    assert r.status_code == 200
    body = r.json()
    assert list(body) == ["b", "c", "a"]
    assert body["b"] == 1.0 and body["a"] == 0.0

    assert client.get("/api/search", params={"q": "   "}).json() == {}


def test_search_endpoint_without_embeddings(tmp_path, monkeypatch):
    db = tmp_path / "p.db"
    _make_db(db, {"a": None})
    monkeypatch.setattr(main, "DB_PATH", db)
    monkeypatch.setattr(main, "_MATRIX_CACHE", None)
    monkeypatch.setattr(main, "_get_model", lambda: _StubModel())
    r = TestClient(app).get("/api/search", params={"q": "x"})
    assert r.status_code == 503
    assert "embed" in r.json()["detail"].lower()


# ── add applied ───────────────────────────────────────────────────────────────

APPLICATIONS_PAGE = """
WaterlooWorks
Home
arrow_back
Applications
You have submitted 48 of 50 applications for the current recruiting term.
Job Title
swap_vert
App Submitted On (1)

preview
print
cancel
Software Engineering Intern
483949
2027 - Winter
Fable Security Inc
Applied
Open for Applications
Divisional Office
USA - West
San Francisco
2
Sep 17, 2026 9:00 AM
Sep 6, 2026 9:13 PM
Julian Einard Salvador

preview
print
cancel
Software Developer
484037
2027 - Winter
Open Text Corporation
Applied
Open for Applications
Corporate Headquarters
ON - Waterloo Region
Waterloo
3
Sep 17, 2026 9:00 AM
Sep 6, 2026 4:38 PM
Julian Einard Salvador
fast_rewind
1
2
48 results
1 - 45
© 2026 Orbis Communications Inc.
"""


def test_parse_applied_job_ids_from_page():
    assert parse_applied_job_ids(APPLICATIONS_PAGE) == ["483949", "484037"]


def test_parse_applied_ignores_years_counts_and_times():
    # Years (2027, 2026), openings (2, 3), "48 of 50" and page numbers must not
    # be mistaken for job IDs.
    for noise in ("2027", "2026", "48", "50", "45", "9:00"):
        assert noise not in parse_applied_job_ids(APPLICATIONS_PAGE)


def test_parse_applied_dedupes_repeated_pastes():
    assert parse_applied_job_ids(APPLICATIONS_PAGE + APPLICATIONS_PAGE) == ["483949", "484037"]


def test_parse_applied_falls_back_to_bare_ids_without_line_structure():
    flat = "Software Engineering Intern 483949 2027 - Winter Applied 484037 stuff"
    assert parse_applied_job_ids(flat) == ["483949", "484037"]


def test_parse_applied_empty():
    assert parse_applied_job_ids("") == []
    assert parse_applied_job_ids("no ids here, just 2027 and 48") == []


def _applied_db(path: Path, statuses: dict[str, str]) -> None:
    schema = (Path(__file__).parent.parent / "db" / "schema.sql").read_text()
    with sqlite3.connect(path) as conn:
        conn.executescript(schema)
        for jid, status in statuses.items():
            conn.execute(
                "INSERT INTO postings (job_id, title, status) VALUES (?, ?, ?)",
                (jid, "t", status),
            )


def test_mark_applied_endpoint(tmp_path, monkeypatch):
    db = tmp_path / "p.db"
    _applied_db(db, {"483949": "new", "484037": "applied", "999999": "maybe"})
    monkeypatch.setattr(main, "DB_PATH", db)

    r = TestClient(app).post("/api/postings/applied", json={"text": APPLICATIONS_PAGE})
    assert r.status_code == 200
    body = r.json()
    assert body["parsed"] == 2
    assert body["matched"] == 2
    assert body["updated"] == 1
    assert body["already_applied"] == 1
    assert body["unknown"] == []
    assert body["applied_job_ids"] == ["483949", "484037"]

    with sqlite3.connect(db) as conn:
        rows = dict(conn.execute("SELECT job_id, status FROM postings").fetchall())
    # Postings absent from the paste keep their status.
    assert rows == {"483949": "applied", "484037": "applied", "999999": "maybe"}


def test_mark_applied_reports_unknown_ids(tmp_path, monkeypatch):
    db = tmp_path / "p.db"
    _applied_db(db, {"483949": "new"})
    monkeypatch.setattr(main, "DB_PATH", db)

    r = TestClient(app).post("/api/postings/applied", json={"text": APPLICATIONS_PAGE})
    body = r.json()
    assert body["updated"] == 1
    assert body["unknown"] == ["484037"]
    assert body["applied_job_ids"] == ["483949"]


def test_mark_applied_rejects_paste_without_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "p.db")
    r = TestClient(app).post("/api/postings/applied", json={"text": "nothing useful"})
    assert r.status_code == 400
    assert "job id" in r.json()["detail"].lower()
