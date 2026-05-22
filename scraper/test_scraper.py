"""
Unit tests for pure-Python helpers in scraper.py.
No browser, no network required.

Run: pytest scraper/test_scraper.py -v
"""

import json
import pytest
from scraper.scraper import build_row, extract_ids_from_html, parse_table_rows, pick_field


# ── parse_table_rows ──────────────────────────────────────────────────────────

def test_parse_two_column_rows():
    rows = [
        ["Job Title:", "Firmware Engineer"],
        ["Organization:", "Acme Corp"],
        ["Location:", "Waterloo, ON"],
    ]
    result = parse_table_rows(rows)
    assert result["Job Title"] == "Firmware Engineer"
    assert result["Organization"] == "Acme Corp"
    assert result["Location"] == "Waterloo, ON"


def test_parse_strips_trailing_colon():
    rows = [["Deadline:", "2026-07-01"]]
    result = parse_table_rows(rows)
    assert "Deadline" in result
    assert "Deadline:" not in result


def test_parse_alternating_single_column():
    rows = [
        ["Job Summary"],
        ["We are looking for a firmware engineer..."],
        ["Required Skills"],
        ["C, RTOS, embedded Linux"],
    ]
    result = parse_table_rows(rows)
    assert result["Job Summary"] == "We are looking for a firmware engineer..."
    assert result["Required Skills"] == "C, RTOS, embedded Linux"


def test_parse_skips_empty_rows():
    rows = [["", ""], ["Job Title:", "SWE Intern"], []]
    result = parse_table_rows(rows)
    assert "Job Title" in result
    assert len(result) == 1


def test_parse_mixed_layouts():
    rows = [
        ["Job Title:", "Embedded Developer"],
        ["Job Summary"],
        ["Build cool things."],
        ["Location:", "Toronto, ON"],
    ]
    result = parse_table_rows(rows)
    assert result["Job Title"] == "Embedded Developer"
    assert result["Job Summary"] == "Build cool things."
    assert result["Location"] == "Toronto, ON"


def test_parse_empty_input():
    assert parse_table_rows([]) == {}


def test_parse_single_column_label_with_no_following_value():
    rows = [["Orphaned Label"]]
    assert parse_table_rows(rows) == {}


# ── pick_field ────────────────────────────────────────────────────────────────

def test_pick_exact_match():
    fields = {"Job Title": "Engineer", "Location": "Waterloo"}
    assert pick_field(fields, ["job title"]) == "Engineer"


def test_pick_substring_match():
    fields = {"Application Deadline": "2026-07-01"}
    assert pick_field(fields, ["deadline"]) == "2026-07-01"


def test_pick_case_insensitive():
    fields = {"ORGANIZATION NAME": "Acme"}
    assert pick_field(fields, ["organization"]) == "Acme"


def test_pick_first_candidate_wins():
    fields = {"Job Summary": "Short summary.", "Description": "Longer text."}
    assert pick_field(fields, ["job summary", "description"]) == "Short summary."


def test_pick_fallback_to_second_candidate():
    fields = {"Description": "Some text."}
    assert pick_field(fields, ["job summary", "description"]) == "Some text."


def test_pick_no_match_returns_empty():
    assert pick_field({"Location": "Waterloo"}, ["organization", "employer"]) == ""


def test_pick_empty_fields():
    assert pick_field({}, ["title"]) == ""


# ── build_row ─────────────────────────────────────────────────────────────────

def test_build_row_basic():
    fields = {
        "Job Title": "Firmware Engineer",
        "Organization": "Acme Corp",
        "Location": "Waterloo, ON",
        "Application Deadline": "2026-08-01",
        "Work Term": "Fall 2026",
        "Number of Openings": "2",
        "Job Summary": "Build firmware.",
        "Responsibilities": "Write C code.",
        "Required Skills": "C, RTOS",
    }
    row = build_row("99999", "direct", fields, "2026-05-01T00:00:00+00:00")
    assert row["job_id"] == "99999"
    assert row["board_type"] == "direct"
    assert row["title"] == "Firmware Engineer"
    assert row["org"] == "Acme Corp"
    assert row["location"] == "Waterloo, ON"
    assert row["deadline"] == "2026-08-01"
    assert row["work_term"] == "Fall 2026"
    assert row["openings"] == "2"
    assert row["summary"] == "Build firmware."
    assert row["responsibilities"] == "Write C code."
    assert row["required_skills"] == "C, RTOS"
    assert row["scraped_at"] == "2026-05-01T00:00:00+00:00"


def test_build_row_missing_fields_are_empty_string():
    row = build_row("11111", "direct", {}, "2026-05-01T00:00:00+00:00")
    assert row["title"] == ""
    assert row["org"] == ""
    assert row["summary"] == ""


def test_build_row_raw_fields_json_is_valid():
    fields = {"Job Title": "SWE"}
    row = build_row("22222", "direct", fields, "2026-05-01T00:00:00+00:00")
    assert json.loads(row["raw_fields_json"]) == fields


# ── extract_ids_from_html ─────────────────────────────────────────────────────

def test_extract_ids_ck_jobid_query_param():
    html = '<a href="?ck_jobid=12345">Job Title</a>'
    assert "12345" in extract_ids_from_html(html)


def test_extract_ids_data_attribute():
    html = '<tr data-jobid="67890"><td>Something</td></tr>'
    assert "67890" in extract_ids_from_html(html)


def test_extract_ids_getPostingData_call():
    html = "getPostingData(54321, function(data) {})"
    assert "54321" in extract_ids_from_html(html)


def test_extract_ids_deduplicates():
    html = "ck_jobid=99999 ck_jobid=99999 ck_jobid=99999"
    ids = extract_ids_from_html(html)
    assert ids.count("99999") == 1


def test_extract_ids_ignores_short_numbers():
    html = "some number 123 and another 456 here"
    assert extract_ids_from_html(html) == []


def test_extract_ids_multiple_jobs():
    html = 'ck_jobid=11111 something ck_jobid=22222 something ck_jobid=33333'
    ids = extract_ids_from_html(html)
    assert set(ids) == {"11111", "22222", "33333"}


def test_extract_ids_empty_html():
    assert extract_ids_from_html("") == []
