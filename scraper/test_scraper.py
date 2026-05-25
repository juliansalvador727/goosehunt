"""
Unit tests for pure-Python helpers in scraper.py.
No browser, no network required.

Run: pytest scraper/test_scraper.py -v
"""

import json
from pathlib import Path

import pytest

from scraper.scraper import (
    BOARDS,
    build_row,
    extract_ids_from_html,
    extract_page_ids,
    parse_list_rows,
    parse_list_rows_from_json,
    parse_table_rows,
    pick_field,
    should_stop_collecting,
)

FIXTURES = Path(__file__).parent / "fixtures"
FULL_CYCLE_LIST_HTML = (FIXTURES / "full_cycle_list.html").read_text(encoding="utf-8")


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
    assert "apps_count" not in row


def test_build_row_missing_fields_are_empty_string():
    row = build_row("11111", "direct", {}, "2026-05-01T00:00:00+00:00")
    assert row["title"] == ""
    assert row["org"] == ""
    assert row["summary"] == ""


def test_build_row_raw_fields_json_is_valid():
    fields = {"Job Title": "SWE"}
    row = build_row("22222", "direct", fields, "2026-05-01T00:00:00+00:00")
    assert json.loads(row["raw_fields_json"]) == fields


def test_build_row_merges_list_meta():
    detail = {"Job Title": "From Detail", "Job Summary": "Long description."}
    list_meta = {
        "title": "From List",
        "location": "Palo Alto",
        "apps_count": "33",
        "deadline": "May 26, 2026 9:00 AM",
    }
    row = build_row("472148", "full_cycle", detail, "2026-05-01T00:00:00+00:00", list_meta)
    assert row["title"] == "From Detail"
    assert row["location"] == "Palo Alto"
    assert row["apps_count"] == "33"
    assert row["deadline"] == "May 26, 2026 9:00 AM"
    merged = json.loads(row["raw_fields_json"])
    assert merged["apps_count"] == "33"
    assert merged["Job Summary"] == "Long description."


def test_build_row_list_fills_gaps_when_detail_empty():
    row = build_row(
        "472148",
        "full_cycle",
        {},
        "2026-05-01T00:00:00+00:00",
        {"title": "Data Engineering Co-op", "org": "Guidepoint Global LLC"},
    )
    assert row["title"] == "Data Engineering Co-op"
    assert row["org"] == "Guidepoint Global LLC"


# ── parse_list_rows ───────────────────────────────────────────────────────────

def test_parse_list_rows_full_cycle_fixture():
    config = BOARDS["full_cycle"]
    rows = parse_list_rows(FULL_CYCLE_LIST_HTML, config)
    assert "472148" in rows
    first = rows["472148"]
    assert first["title"] == "Forward Deployed Engineering Assistant - AI Agents"
    assert first["org"] == "Agent Dynamics Inc."
    assert first["division"] == "Divisional Office"
    assert first["openings"] == "1"
    assert first["location"] == "Palo Alto"
    assert first["level"] == "Senior"
    assert first["apps_count"] == "33"
    assert first["deadline"] == "May 26, 2026 9:00 AM"


def test_parse_list_rows_direct_columns():
    config = BOARDS["direct"]
    html = """
    <tr class="table__row--body">
      <th><input name="dataViewerSelection" value="12345"></th>
      <td class="table__value"><span class="overflow--ellipsis">Fall 2026</span></td>
      <td class="table__value"><span class="overflow--ellipsis">SWE Intern</span></td>
      <td class="table__value"><span class="overflow--ellipsis">Acme Corp</span></td>
      <td class="table__value"><span class="overflow--ellipsis">HQ</span></td>
      <td class="table__value"><span class="overflow--ellipsis">2</span></td>
      <td class="table__value"><span class="overflow--ellipsis">Waterloo</span></td>
      <td class="table__value"><span class="overflow--ellipsis">Junior</span></td>
      <td class="table__value"><span class="overflow--ellipsis">Jun 1, 2026</span></td>
    </tr>
    """
    rows = parse_list_rows(html, config)
    assert rows["12345"]["work_term"] == "Fall 2026"
    assert rows["12345"]["title"] == "SWE Intern"
    assert rows["12345"]["location"] == "Waterloo"
    assert "apps_count" not in rows["12345"]


def test_parse_list_rows_empty_html():
    assert parse_list_rows("", BOARDS["full_cycle"]) == {}


def test_parse_list_rows_from_json_cells_array():
    config = BOARDS["full_cycle"]
    data = {
        "data": [
            {
                "id": "472148",
                "cells": [
                    "Forward Deployed Engineering Assistant",
                    "Agent Dynamics Inc.",
                    "Divisional Office",
                    "1",
                    "Palo Alto",
                    "Senior",
                    "33",
                    "May 26, 2026 9:00 AM",
                ],
            }
        ]
    }
    rows = parse_list_rows_from_json(data, config)
    assert rows["472148"]["apps_count"] == "33"
    assert rows["472148"]["openings"] == "1"


def test_should_stop_collecting():
    stop, reason = should_stop_collecting(
        page_ids=["1"] * 26,
        page_listings={"1": {}},
        added=26,
        items_per_page=50,
    )
    assert stop and "last page" in reason

    stop, reason = should_stop_collecting(
        page_ids=[f"id{i}" for i in range(50)],
        page_listings={"id0": {}},
        added=0,
        items_per_page=50,
    )
    assert stop and "no new jobs" in reason

    stop, _ = should_stop_collecting(
        page_ids=["1"] * 50,
        page_listings={"1": {}},
        added=50,
        items_per_page=50,
    )
    assert not stop


def test_extract_page_ids_ignores_phantom_ck_jobid():
    """IDs embedded in scripts must not count unless they have a table row."""
    config = BOARDS["full_cycle"]
    html = (
        '<tr class="table__row--body">'
        '<th><input name="dataViewerSelection" value="472148"></th>'
        '<td class="table__value"><span>Job A</span></td>'
        '<td class="table__value"><span>Org</span></td>'
        '<td class="table__value"><span>Div</span></td>'
        '<td class="table__value"><span>1</span></td>'
        '<td class="table__value"><span>City</span></td>'
        '<td class="table__value"><span>Jr</span></td>'
        '<td class="table__value"><span>44</span></td>'
        '<td class="table__value"><span>Jun 1</span></td>'
        "</tr>"
    )
    raw = html + '<script>ck_jobid=999999</script><a href="?ck_jobid=888888">x</a>'
    listings = parse_list_rows(html, config)
    ids = extract_page_ids(listings, html, raw, config)
    assert ids == ["472148"]
    assert "999999" not in ids
    assert "888888" not in ids


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


def test_extract_ids_data_viewer_checkbox():
    html = '<input name="dataViewerSelection" value="472148" type="checkbox">'
    assert extract_ids_from_html(html) == ["472148"]
