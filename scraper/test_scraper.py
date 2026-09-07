"""
Unit tests for pure-Python helpers in scraper.py.
No browser, no network required.

Run: pytest scraper/test_scraper.py -v
"""

import asyncio
import json
from pathlib import Path

import pytest
import scraper.scraper as scraper_module

from scraper.scraper import (
    BOARDS,
    DEFAULT_WORKERS,
    MAX_LISTING_PAGES,
    MAX_NO_NEW_PAGES,
    build_row,
    extract_ids_from_html,
    extract_page_ids,
    fetch_ratings,
    load_login_credentials,
    navigate_to_board,
    parse_args,
    parse_list_rows,
    parse_list_rows_from_json,
    parse_table_rows,
    pick_field,
    scrape_jobs,
    should_stop_collecting,
    submit_adfs_login,
    wait_for_authenticated_page,
)

FIXTURES = Path(__file__).parent / "fixtures"
FULL_CYCLE_LIST_HTML = (FIXTURES / "full_cycle_list.html").read_text(encoding="utf-8")


# ── CLI / workers ─────────────────────────────────────────────────────────────

def test_parse_args_defaults_to_five_workers():
    assert parse_args([]).workers == DEFAULT_WORKERS == 5


def test_parse_args_accepts_worker_override():
    assert parse_args(["--workers", "5"]).workers == 5


@pytest.mark.parametrize(
    ("board", "path"),
    [
        ("direct", "/myAccount/co-op/direct/jobs.htm"),
        ("full_cycle", "/myAccount/co-op/full/jobs.htm"),
    ],
)
def test_board_routes_match_waterlooworks(board, path):
    assert BOARDS[board].url.endswith(path)


def test_navigate_to_board_opens_configured_url():
    class FakePage:
        url = "https://waterlooworks.uwaterloo.ca/myAccount/dashboard.htm"

        def __init__(self):
            self.goto_calls = []
            self.selector_calls = []
            self.all_jobs = FakeAllJobs()

        def get_by_text(self, pattern):
            assert pattern.pattern == scraper_module.ALL_JOBS_PATTERN.pattern
            return self.all_jobs

        async def goto(self, url, **kwargs):
            self.url = url
            self.goto_calls.append((url, kwargs))

        async def wait_for_load_state(self, state, **kwargs):
            pass

        async def wait_for_selector(self, selector, **kwargs):
            self.selector_calls.append((selector, kwargs))

    class FakeAllJobs:
        def __init__(self):
            self.waited = False
            self.clicked = False

        @property
        def first(self):
            return self

        async def wait_for(self, **kwargs):
            self.waited = True

        async def click(self):
            self.clicked = True

    page = FakePage()
    asyncio.run(navigate_to_board(page, BOARDS["full_cycle"]))

    assert page.goto_calls == [(BOARDS["full_cycle"].url, {"wait_until": "domcontentloaded"})]
    assert page.all_jobs.waited is True
    assert page.all_jobs.clicked is True
    assert page.selector_calls[0][0] == scraper_module.LISTING_READY_SELECTOR


def test_load_login_credentials_from_environment(monkeypatch):
    monkeypatch.setenv("WATERLOOWORKS_EMAIL", "student@uwaterloo.ca")
    monkeypatch.setenv("WATERLOOWORKS_PASSWORD", "test-password")

    assert load_login_credentials() == ("student@uwaterloo.ca", "test-password")


def test_submit_adfs_login_fills_and_submits_credentials():
    class FakeLocator:
        def __init__(self):
            self.value = None
            self.clicked = False

        @property
        def first(self):
            return self

        async def wait_for(self, **kwargs):
            pass

        async def fill(self, value):
            self.value = value

        async def is_visible(self):
            return True

        async def press(self, key):
            raise AssertionError("Enter fallback should not be needed")

        async def click(self):
            self.clicked = True

    class FakePage:
        url = "https://adfs.uwaterloo.ca/adfs/ls/"

        def __init__(self):
            self.fields = {
                scraper_module.ADFS_USERNAME_SELECTOR: FakeLocator(),
                scraper_module.ADFS_PASSWORD_SELECTOR: FakeLocator(),
                scraper_module.ADFS_NEXT_SELECTOR: FakeLocator(),
                scraper_module.ADFS_SUBMIT_SELECTOR: FakeLocator(),
            }

        def locator(self, selector):
            return self.fields[selector]

    page = FakePage()
    submitted = asyncio.run(submit_adfs_login(
        page, "student@uwaterloo.ca", "test-password",
    ))

    assert submitted is True
    assert page.fields[scraper_module.ADFS_USERNAME_SELECTOR].value == "student@uwaterloo.ca"
    assert page.fields[scraper_module.ADFS_PASSWORD_SELECTOR].value == "test-password"
    assert page.fields[scraper_module.ADFS_SUBMIT_SELECTOR].clicked is True


def test_submit_adfs_login_never_fills_another_host():
    class UnexpectedPage:
        url = "https://example.com/login"

        def locator(self, selector):
            raise AssertionError("credentials must not be exposed to another host")

    assert asyncio.run(submit_adfs_login(
        UnexpectedPage(), "student@uwaterloo.ca", "test-password",
    )) is False


def test_submit_adfs_login_advances_two_stage_form():
    class FakeField:
        def __init__(self, visible):
            self.visible = visible
            self.value = None

        @property
        def first(self):
            return self

        async def wait_for(self, **kwargs):
            assert self.visible

        async def is_visible(self):
            return self.visible

        async def fill(self, value):
            assert self.visible
            self.value = value

        async def press(self, key):
            raise AssertionError("the visible Next control should be clicked")

    class FakeButton(FakeField):
        def __init__(self, on_click=None):
            super().__init__(visible=True)
            self.clicked = False
            self.on_click = on_click

        async def click(self):
            self.clicked = True
            if self.on_click:
                self.on_click()

    username = FakeField(visible=True)
    password = FakeField(visible=False)
    next_button = FakeButton(on_click=lambda: setattr(password, "visible", True))
    submit_button = FakeButton()

    class FakePage:
        url = "https://adfs.uwaterloo.ca/adfs/ls/"
        fields = {
            scraper_module.ADFS_USERNAME_SELECTOR: username,
            scraper_module.ADFS_PASSWORD_SELECTOR: password,
            scraper_module.ADFS_NEXT_SELECTOR: next_button,
            scraper_module.ADFS_SUBMIT_SELECTOR: submit_button,
        }

        def locator(self, selector):
            return self.fields[selector]

    submitted = asyncio.run(submit_adfs_login(
        FakePage(), "student@uwaterloo.ca", "test-password",
    ))

    assert submitted is True
    assert username.value == "student@uwaterloo.ca"
    assert next_button.clicked is True
    assert password.value == "test-password"
    assert submit_button.clicked is True


def test_wait_for_authenticated_page_detects_dashboard_redirect():
    class FakePage:
        url = "https://adfs.uwaterloo.ca/adfs/ls/"

    class FakeContext:
        pages = [FakePage()]

    async def exercise():
        async def redirect():
            await asyncio.sleep(0.01)
            FakeContext.pages[0].url = (
                "https://waterlooworks.uwaterloo.ca/myAccount/dashboard.htm"
            )

        redirect_task = asyncio.create_task(redirect())
        page = await wait_for_authenticated_page(FakeContext(), timeout_ms=1_000)
        await redirect_task
        return page

    page = asyncio.run(exercise())
    assert page.url.endswith("/myAccount/dashboard.htm")


@pytest.mark.parametrize("value", ["0", "-1", "nope"])
def test_parse_args_rejects_invalid_worker_count(value):
    with pytest.raises(SystemExit):
        parse_args(["--workers", value])


def test_scrape_jobs_runs_three_workers_concurrently_on_one_page(monkeypatch):
    active = 0
    peak = 0
    written = []
    pages_seen = []

    async def fake_fetch_overview(page, job_id):
        nonlocal active, peak
        pages_seen.append(page)
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return f"overview-{job_id}"

    async def fake_parse_overview(page, html):
        return {"Job Title": html}

    monkeypatch.setattr(scraper_module, "fetch_overview_with_retry", fake_fetch_overview)
    monkeypatch.setattr(scraper_module, "parse_overview_html", fake_parse_overview)
    monkeypatch.setattr(scraper_module, "append_output", written.append)

    todo = [str(job_id) for job_id in range(1, 7)]
    shared_page = object()
    scraped, failed = asyncio.run(scrape_jobs(
        shared_page,
        3,
        todo,
        {},
        BOARDS["direct"],
        ratings=False,
    ))

    assert peak == 3
    assert all(page is shared_page for page in pages_seen)
    assert scraped == len(todo)
    assert failed == []
    assert {row["job_id"] for row in written} == set(todo)


def test_scrape_jobs_uses_one_worker_for_partial_final_page(monkeypatch):
    active = 0
    parallel_peak = 0
    tail_peak = 0
    tail_ids = {"6", "7", "8"}

    async def fake_fetch_overview(page, job_id):
        nonlocal active, parallel_peak, tail_peak
        active += 1
        if job_id in tail_ids:
            tail_peak = max(tail_peak, active)
        else:
            parallel_peak = max(parallel_peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return f"overview-{job_id}"

    async def fake_parse_overview(page, html):
        return {"Job Title": html}

    monkeypatch.setattr(scraper_module, "fetch_overview_with_retry", fake_fetch_overview)
    monkeypatch.setattr(scraper_module, "parse_overview_html", fake_parse_overview)
    monkeypatch.setattr(scraper_module, "append_output", lambda row: None)

    todo = [str(job_id) for job_id in range(1, 9)]
    scraped, failed = asyncio.run(scrape_jobs(
        object(),
        5,
        todo,
        {},
        BOARDS["direct"],
        ratings=False,
        single_worker_ids=tail_ids,
    ))

    assert parallel_peak == 5
    assert tail_peak == 1
    assert scraped == 8
    assert failed == []


def test_fetch_ratings_deduplicates_concurrent_division_requests(monkeypatch):
    requests = 0

    async def fake_posting_data(page, job_id):
        return {"divId": 42}

    async def fake_work_term_ratings(page, div_id):
        nonlocal requests
        requests += 1
        await asyncio.sleep(0.01)
        return {"sections": [{"title": "Shared employer"}]}

    monkeypatch.setattr(scraper_module, "get_posting_data", fake_posting_data)
    monkeypatch.setattr(scraper_module, "get_work_term_ratings", fake_work_term_ratings)

    async def exercise():
        cache = {}
        inflight = {}
        lock = asyncio.Lock()
        return await asyncio.gather(*(
            fetch_ratings(
                object(),
                str(job_id),
                cache,
                inflight=inflight,
                cache_lock=lock,
            )
            for job_id in range(3)
        ))

    results = asyncio.run(exercise())
    assert requests == 1
    assert all(result == [{"title": "Shared employer"}] for result in results)


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
    # An empty page always ends the crawl.
    stop, reason = should_stop_collecting(
        page_ids=[],
        page_listings={},
        consecutive_no_new=0,
        page_num=3,
    )
    assert stop and "empty page" in reason

    # A short page mid-run is NOT the end on its own — it still added new IDs.
    stop, _ = should_stop_collecting(
        page_ids=["1"] * 26,
        page_listings={"1": {}},
        consecutive_no_new=0,
        page_num=3,
    )
    assert not stop

    # Enough consecutive no-new pages ends the crawl.
    stop, reason = should_stop_collecting(
        page_ids=[f"id{i}" for i in range(50)],
        page_listings={"id0": {}},
        consecutive_no_new=MAX_NO_NEW_PAGES,
        page_num=5,
    )
    assert stop and "no new jobs" in reason

    # A single no-new page is tolerated (below the threshold).
    stop, _ = should_stop_collecting(
        page_ids=[f"id{i}" for i in range(50)],
        page_listings={"id0": {}},
        consecutive_no_new=1,
        page_num=5,
    )
    assert not stop

    # Hard page cap is a safety net against a broken pager.
    stop, reason = should_stop_collecting(
        page_ids=["1"] * 50,
        page_listings={"1": {}},
        consecutive_no_new=0,
        page_num=MAX_LISTING_PAGES,
    )
    assert stop and "max page cap" in reason

    # A normal full page keeps going.
    stop, _ = should_stop_collecting(
        page_ids=["1"] * 50,
        page_listings={"1": {}},
        consecutive_no_new=0,
        page_num=2,
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


def test_build_row_reserved_keys_pass_through():
    """`_links` / `_ratings` ride along in raw_fields_json and never hit a mapped column."""
    fields = {
        "Job Title": "SWE",
        "Level": "Junior",
        "_links": {"Additional Application Information": ["https://x.example/apply"]},
        "_ratings": [{"type": "table", "title": "Hiring History", "rows": [["1"]]}],
    }
    row = build_row("33333", "full_cycle", fields, "2026-05-01T00:00:00+00:00")
    merged = json.loads(row["raw_fields_json"])
    assert merged["_links"] == fields["_links"]
    assert merged["_ratings"] == fields["_ratings"]
    for col in ("title", "org", "location", "deadline", "work_term", "openings",
                "division", "level", "summary", "responsibilities", "required_skills"):
        assert isinstance(row[col], str)
    assert row["title"] == "SWE"
    assert row["level"] == "Junior"
