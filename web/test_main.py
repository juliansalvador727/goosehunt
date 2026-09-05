"""
Unit tests for request-time enrichment helpers in web/main.py.
No database, no server required.

Run: pytest web/test_main.py -v
"""

import json
from pathlib import Path

from web.main import extract_apply_info, extract_posting_attrs, extract_ratings

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
