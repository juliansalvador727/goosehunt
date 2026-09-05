#!/usr/bin/env python3
"""
goosehunt scraper — WaterlooWorks job boards (Employer Direct, Full Cycle).

Uses WW's in-page JS API (credit: bryanling1/waterlooworks-scraper):
  1. Extract the 'action' key embedded in the page's JS.
  2. POST to the listing endpoint (page size from WW UI, usually 50) to collect job IDs + list metadata.
  3. Call window.getPostingOverview(jobId, cb) for each job → HTML string.
  4. Parse HTML in-memory; no new tabs opened.
  5. Call window.getPostingData(jobId, cb) → {divId, ...}, then
     window.getWorkTermRatingReportJson(divId, cb) → {sections: [...]} for the
     employer's work term ratings tab (cached per division within a run).

Usage:
  python -m scraper.scraper --board direct       # Employer Direct (default)
  python -m scraper.scraper --board full_cycle   # Full Cycle Service
  python -m scraper.scraper --probe-ratings 10   # dump ratings JSON for 10 jobs, no scrape
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

LISTING_READY_SELECTOR = (
    "table.data-viewer-table, "
    "input[name='dataViewerSelection'], "
    "tr.table__row--body"
)

ROOT = Path(__file__).parent.parent
PROFILE_DIR = Path(__file__).parent / "profile"
DATA_DIR = ROOT / "data"
OUTPUT_FILE = DATA_DIR / "postings.jsonl"

# WW listing grid needs time to render after pagination clicks
PAGE_NAV_DELAY_MIN_S = 3.0
PAGE_NAV_DELAY_MAX_S = 5.0

# Pagination safety limits (see should_stop_collecting)
MAX_NO_NEW_PAGES = 2      # stop after this many consecutive pages with no new IDs
MAX_LISTING_PAGES = 200   # hard cap so a broken pager can't loop forever

# Detail-fetch retry policy
DETAIL_FETCH_RETRIES = 3
DETAIL_FETCH_RETRY_DELAY_S = 2.0


@dataclass(frozen=True)
class BoardConfig:
    board_type: str
    label: str
    list_columns: tuple[str, ...]


BOARDS: dict[str, BoardConfig] = {
    "direct": BoardConfig(
        board_type="direct",
        label="Employer Direct",
        list_columns=(
            "work_term", "title", "org", "division", "openings",
            "location", "level", "deadline",
        ),
    ),
    "full_cycle": BoardConfig(
        board_type="full_cycle",
        label="Full Cycle Service",
        list_columns=(
            "title", "org", "division", "openings", "location",
            "level", "apps_count", "deadline",
        ),
    ),
}

FIELD_MAP: dict[str, list[str]] = {
    "title":            ["job title", "position title", "title"],
    "org":              ["organization", "employer", "company name", "org"],
    "location":         ["job - city", "city", "region", "work location", "location"],
    "deadline":         ["deadline", "application deadline", "apply by"],
    "work_term":        ["work term", "term", "co-op term"],
    "openings":         ["openings", "number of positions", "positions available",
                         "number of job openings"],
    "division":         ["division"],
    "level":            ["level"],
    "summary":          ["job summary", "summary", "overview", "job description", "description"],
    "responsibilities": ["job responsibilities", "responsibilities", "duties", "key responsibilities"],
    "required_skills":  ["required skills", "skills required", "qualifications", "technical skills"],
}

# List-column keys → FIELD_MAP column when names differ
_LIST_KEY_ALIASES: dict[str, str] = {
    "apps_count": "apps",
}


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape WaterlooWorks job postings.")
    parser.add_argument(
        "--board",
        choices=sorted(BOARDS.keys()),
        default="direct",
        help="Which WW board to scrape (default: direct)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Resume a crashed run: skip jobs already in postings.jsonl instead of "
            "re-fetching every listed posting. Default is a full refresh so deadlines "
            "and apps counts stay current."
        ),
    )
    parser.add_argument(
        "--no-ratings",
        action="store_true",
        help="Skip the work term ratings fetch (faster scrape).",
    )
    parser.add_argument(
        "--probe-ratings",
        nargs="?",
        const=10,
        default=0,
        type=int,
        metavar="N",
        help=(
            "Dump raw ratings JSON for the first N listed jobs to "
            "data/ratings_sample.json (and one overview HTML to "
            "data/overview_sample.html), then exit without scraping."
        ),
    )
    return parser.parse_args(argv)


# ── File I/O ──────────────────────────────────────────────────────────────────

def load_done() -> set[str]:
    if not OUTPUT_FILE.exists():
        return set()
    done: set[str] = set()
    with OUTPUT_FILE.open() as f:
        for line in f:
            line = line.strip()
            if line:
                done.add(json.loads(line)["job_id"])
    return done


def append_output(row: dict) -> None:
    with OUTPUT_FILE.open("a") as f:
        f.write(json.dumps(row) + "\n")


def listing_manifest_path(board_type: str) -> Path:
    return DATA_DIR / f"listing_{board_type}.json"


def write_listing_manifest(board_type: str, job_ids: list[str], now: str) -> None:
    """Record the full set of job IDs visible in this scrape's listing.

    Ingest uses this to purge postings that are no longer listed, instead of
    guessing liveness from a possibly-stale deadline.
    """
    path = listing_manifest_path(board_type)
    payload = {
        "board_type": board_type,
        "scraped_at": now,
        "job_ids": sorted(job_ids),
    }
    path.write_text(json.dumps(payload, indent=2))


# ── Pure parsing helpers (tested without browser) ─────────────────────────────

def parse_table_rows(rows_data: list[list[str]]) -> dict[str, str]:
    """
    Convert a list of rows (each row = list of cell strings) to a label→value dict.
    Handles both 2-column rows (label | value) and alternating single-column rows.
    """
    fields: dict[str, str] = {}
    i = 0
    while i < len(rows_data):
        cells = rows_data[i]
        if len(cells) == 2 and cells[0].strip():
            label = cells[0].strip().rstrip(":")
            value = cells[1].strip()
            if label:
                fields[label] = value
            i += 1
        elif len(cells) == 1 and cells[0].strip():
            label = cells[0].strip().rstrip(":")
            if i + 1 < len(rows_data) and len(rows_data[i + 1]) == 1:
                value = rows_data[i + 1][0].strip()
                fields[label] = value
                i += 2
            else:
                i += 1
        else:
            i += 1
    return fields


def pick_field(fields: dict[str, str], candidates: list[str]) -> str:
    """Return the first value whose key contains any candidate string (case-insensitive)."""
    lower_fields = {k.lower(): v for k, v in fields.items()}
    for candidate in candidates:
        for key, value in lower_fields.items():
            if candidate.lower() in key:
                return value
    return ""


def _merge_fields(
    detail_fields: dict[str, str],
    list_meta: dict[str, str] | None,
) -> dict[str, str]:
    """Merge list grid metadata with detail fields; detail wins on conflict."""
    merged: dict[str, str] = {}
    if list_meta:
        for key, value in list_meta.items():
            if value:
                merged[key] = value
                alias = _LIST_KEY_ALIASES.get(key)
                if alias:
                    merged[alias] = value
    for key, value in detail_fields.items():
        if value:
            merged[key] = value
    return merged


def build_row(
    job_id: str,
    board_type: str,
    fields: dict,
    now: str,
    list_meta: dict[str, str] | None = None,
) -> dict:
    merged = _merge_fields(fields, list_meta)
    row: dict = {
        "job_id": job_id,
        "board_type": board_type,
        "raw_fields_json": json.dumps(merged),
        "scraped_at": now,
        "updated_at": now,
    }
    for col, candidates in FIELD_MAP.items():
        row[col] = pick_field(merged, candidates)
    if "apps_count" in merged:
        row["apps_count"] = merged["apps_count"]
    elif merged.get("apps"):
        row["apps_count"] = merged["apps"]
    return row


def _cell_text(cell_html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", cell_html)
    return re.sub(r"\s+", " ", text).strip()


def parse_list_rows(html: str, config: BoardConfig) -> dict[str, dict[str, str]]:
    """Parse DataViewer listing HTML into job_id → list-column metadata."""
    results: dict[str, dict[str, str]] = {}
    row_pattern = re.compile(
        r'<tr[^>]*class="[^"]*table__row--body[^"]*"[^>]*>(.*?)</tr>',
        re.DOTALL | re.IGNORECASE,
    )
    td_pattern = re.compile(
        r'<td[^>]*class="[^"]*table__value[^"]*"[^>]*>(.*?)</td>',
        re.DOTALL | re.IGNORECASE,
    )
    for row_match in row_pattern.finditer(html):
        row_html = row_match.group(1)
        chk = re.search(
            r'name=["\']dataViewerSelection["\'][^>]*value=["\'](\d{5,})["\']',
            row_html,
            re.IGNORECASE,
        ) or re.search(
            r'value=["\'](\d{5,})["\'][^>]*name=["\']dataViewerSelection["\']',
            row_html,
            re.IGNORECASE,
        ) or re.search(r'id=["\']resultRow_(\d{5,})["\']', row_html, re.IGNORECASE)
        if not chk:
            continue
        job_id = chk.group(1)
        cells = [_cell_text(m.group(1)) for m in td_pattern.finditer(row_html)]
        meta: dict[str, str] = {}
        for i, col in enumerate(config.list_columns):
            if i < len(cells) and cells[i]:
                meta[col] = cells[i]
        if meta:
            results[job_id] = meta
    return results


_JSON_LIST_ALIASES: dict[str, tuple[str, ...]] = {
    "title": ("jobTitle", "title", "job_title", "JobTitle", "Job Title"),
    "org": ("organization", "employer", "org", "Organization"),
    "division": ("division", "Division"),
    "openings": ("openings", "Openings", "numberOfOpenings"),
    "location": ("city", "location", "City", "jobCity", "Job - City"),
    "level": ("level", "Level"),
    "apps_count": ("apps", "Apps", "applications", "applicationCount", "numApplications"),
    "deadline": ("deadline", "appDeadline", "applicationDeadline", "Deadline", "App Deadline"),
    "work_term": ("workTerm", "term", "WorkTerm", "Work Term"),
}


def _cell_value(raw) -> str:
    if raw is None:
        return ""
    if isinstance(raw, dict):
        return str(
            raw.get("text") or raw.get("value") or raw.get("label") or raw.get("display") or ""
        ).strip()
    return str(raw).strip()


def _apply_flat_field_scan(item: dict, meta: dict[str, str]) -> None:
    """Map loose JSON field names (e.g. Applications) onto list columns."""
    for key, raw in item.items():
        if key in ("id", "jobId", "postingId", "cells", "values", "columns", "fields", "row", "record"):
            continue
        val = _cell_value(raw)
        if not val:
            continue
        kl = str(key).lower().replace("_", " ").replace("-", " ")
        if any(x in kl for x in ("app", "application")) and "deadline" not in kl and "document" not in kl and "method" not in kl:
            if val.replace(",", "").isdigit():
                meta.setdefault("apps_count", val.replace(",", ""))
        elif "title" in kl or kl == "job":
            meta.setdefault("title", val)
        elif "org" in kl or "employer" in kl or "company" in kl:
            meta.setdefault("org", val)
        elif "division" in kl:
            meta.setdefault("division", val)
        elif "opening" in kl or "position" in kl:
            meta.setdefault("openings", val)
        elif "city" in kl or kl == "location":
            meta.setdefault("location", val)
        elif kl == "level":
            meta.setdefault("level", val)
        elif "deadline" in kl:
            meta.setdefault("deadline", val)
        elif "term" in kl:
            meta.setdefault("work_term", val)


def _meta_from_json_item(item: dict, config: BoardConfig) -> dict[str, str]:
    meta: dict[str, str] = {}
    for arr_key in ("cells", "values", "columns", "fields", "row", "cellValues", "record"):
        arr = item.get(arr_key)
        if isinstance(arr, dict):
            for col in config.list_columns:
                for alias in _JSON_LIST_ALIASES.get(col, (col,)):
                    if alias in arr:
                        val = _cell_value(arr[alias])
                        if val:
                            meta[col] = val
                            break
            if meta:
                break
        if not isinstance(arr, list) or not arr:
            continue
        for i, col in enumerate(config.list_columns):
            if i < len(arr):
                val = _cell_value(arr[i])
                if val:
                    meta[col] = val
        if meta:
            break

    for col in config.list_columns:
        if col in meta:
            continue
        for alias in _JSON_LIST_ALIASES.get(col, (col,)):
            if alias in item:
                val = _cell_value(item[alias])
                if val:
                    meta[col] = val
                    break

    _apply_flat_field_scan(item, meta)
    return meta


def parse_list_rows_from_json(data: dict | list | None, config: BoardConfig) -> dict[str, dict[str, str]]:
    """Parse DataViewer JSON rows when the POST response is not HTML."""
    if data is None:
        return {}
    if isinstance(data, dict):
        html = find_listing_html(data)
        if html:
            return parse_list_rows(html, config)
        rows = data.get("data") or data.get("rows") or data.get("results") or []
    elif isinstance(data, list):
        rows = data
    else:
        return {}
    if isinstance(rows, str) and "table__row--body" in rows:
        return parse_list_rows(rows, config)
    if not isinstance(rows, list):
        return {}

    results: dict[str, dict[str, str]] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        jid = str(item.get("id") or item.get("jobId") or item.get("postingId") or "")
        if not jid or len(jid) < 5:
            continue
        meta = _meta_from_json_item(item, config)
        if meta:
            results[jid] = meta
    return results


def merge_list_meta(*sources: dict[str, dict[str, str]] | None) -> dict[str, dict[str, str]]:
    """Merge job_id → list_meta; later non-empty values win (for pagination fixes)."""
    merged: dict[str, dict[str, str]] = {}
    for source in sources:
        if not source:
            continue
        for jid, meta in source.items():
            if jid not in merged:
                merged[jid] = {}
            for key, value in meta.items():
                if value:
                    merged[jid][key] = value
    return merged


def find_listing_html(obj) -> str:
    """Recursively find a listing table HTML fragment inside a JSON payload."""
    if isinstance(obj, str):
        return obj if "table__row--body" in obj else ""
    if isinstance(obj, dict):
        for key in ("html", "content", "markup", "table", "rowsHtml", "rowsHTML", "data", "body", "result"):
            if key in obj:
                found = find_listing_html(obj[key])
                if found:
                    return found
        for value in obj.values():
            found = find_listing_html(value)
            if found:
                return found
    if isinstance(obj, list):
        for value in obj:
            found = find_listing_html(value)
            if found:
                return found
    return ""


def listing_html_from_payload(text: str, data: dict | list | None) -> str:
    if data is not None:
        found = find_listing_html(data)
        if found:
            return found
    if text and "table__row--body" in text:
        return text
    return ""


def should_stop_collecting(
    *,
    page_ids: list[str],
    page_listings: dict,
    consecutive_no_new: int,
    page_num: int,
    max_no_new: int = MAX_NO_NEW_PAGES,
    max_pages: int = MAX_LISTING_PAGES,
) -> tuple[bool, str]:
    """Decide whether listing pagination is complete.

    A short page (fewer rows than a full page) is *not* treated as the end on its
    own — WW occasionally returns a partial page mid-run, and stopping there was
    dropping jobs. Instead we keep going until a page is genuinely empty, or until
    several consecutive pages add no new IDs (the real end, or a pager that keeps
    returning the same rows), or until a hard page cap as a safety net.
    """
    if not page_ids and not page_listings:
        return True, "empty page"
    if consecutive_no_new >= max_no_new:
        return True, f"no new jobs for {consecutive_no_new} consecutive page(s)"
    if page_num >= max_pages:
        return True, f"reached max page cap ({max_pages})"
    return False, ""


def extract_page_ids(
    page_listings: dict[str, dict[str, str]],
    html_chunk: str,
    raw_text: str,
    config: BoardConfig,
) -> list[str]:
    """Job IDs from parsed listing rows only (avoids phantom ck_jobid in scripts)."""
    ids = set(page_listings.keys())
    if html_chunk:
        ids.update(parse_list_rows(html_chunk, config))
    if ids:
        return sorted(ids)
    return extract_ids_from_html(raw_text or "")


def parse_listing_page_result(
    raw_text: str,
    data: dict | list | None,
    dom: dict | None,
    config: BoardConfig,
) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Merge POST JSON/HTML and DOM scrape into page listings + job IDs."""
    html_chunk = listing_html_from_payload(raw_text, data)
    page_listings = merge_list_meta(
        parse_list_rows(html_chunk, config) if html_chunk else {},
        parse_list_rows_from_json(data, config),
        dom or {},
    )
    page_ids = extract_page_ids(page_listings, html_chunk, raw_text, config)
    return page_listings, page_ids


def merge_page_into(
    all_listings: dict[str, dict[str, str]],
    page_ids: list[str],
    page_listings: dict[str, dict[str, str]],
) -> int:
    """Add one listing page; return count of new job IDs."""
    prior = len(all_listings)
    for jid in page_ids:
        if jid not in all_listings:
            all_listings[jid] = dict(page_listings.get(jid, {}))
    for jid, meta in page_listings.items():
        entry = all_listings.setdefault(jid, {})
        for key, value in meta.items():
            if value:
                entry[key] = value
    return len(all_listings) - prior


def extract_ids_from_html(html: str) -> list[str]:
    """Pull WW job IDs out of the listing POST response HTML."""
    seen: set[str] = set()
    ids: list[str] = []
    for pattern in [
        r'ck_jobid[="\s:\']+(\d{5,})',
        r'data-jobid[="\s:\']+(\d{5,})',
        r'getPostingData\((\d{5,})',
        r'getPostingOverview\((\d{5,})',
        r'jobId[="\s:\']+(\d{5,})',
        r'name=["\']dataViewerSelection["\'][^>]*value=["\'](\d{5,})["\']',
        r'value=["\'](\d{5,})["\'][^>]*name=["\']dataViewerSelection["\']',
    ]:
        for m in re.finditer(pattern, html, re.IGNORECASE):
            jid = m.group(1)
            if jid not in seen:
                seen.add(jid)
                ids.append(jid)
    return ids


# ── Browser helpers ───────────────────────────────────────────────────────────

def _is_jobs_url(url: str) -> bool:
    u = url.lower()
    return (
        "jobs.htm" in u
        or "postings" in u
        or "fullcycle" in u
        or "full_cycle" in u
        or "full-cycle" in u
    )


async def resolve_jobs_page(ctx):
    """Pick the tab that has the job listing grid (URL or DOM)."""
    for p in ctx.pages:
        if _is_jobs_url(p.url):
            return p
    for p in reversed(ctx.pages):
        try:
            if await p.locator(LISTING_READY_SELECTOR).count() > 0:
                return p
        except PlaywrightError:
            continue
    return ctx.pages[-1] if ctx.pages else None


async def wait_for_listing_ready(page, timeout_ms: int = 60_000) -> None:
    """Wait until WW finishes loading / navigating to the listing view."""
    print("[SCRAPER] Waiting for listings to settle...")
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except PlaywrightError:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except PlaywrightError:
        pass  # WW often keeps long-polling; selector wait is enough
    await page.wait_for_selector(LISTING_READY_SELECTOR, timeout=timeout_ms)
    print("[SCRAPER] Listings ready.\n")


async def evaluate_retry(
    page,
    expression: str,
    arg=None,
    *,
    retries: int = 5,
    delay: float = 1.5,
):
    """Run page.evaluate; retry if the SPA navigates and destroys the context."""
    last_err: PlaywrightError | None = None
    for attempt in range(1, retries + 1):
        try:
            if arg is None:
                return await page.evaluate(expression)
            return await page.evaluate(expression, arg)
        except PlaywrightError as e:
            last_err = e
            msg = str(e).lower()
            transient = (
                "execution context was destroyed" in msg
                or "navigation" in msg
                or "target closed" in msg
            )
            if not transient or attempt >= retries:
                raise
            print(f"[SCRAPER] Page changed — retrying ({attempt}/{retries})...")
            await asyncio.sleep(delay)
            try:
                await wait_for_listing_ready(page, timeout_ms=30_000)
            except PlaywrightError:
                await page.wait_for_load_state("domcontentloaded")
    if last_err:
        raise last_err
    return None


async def wait_for_login(ctx, config: BoardConfig):
    print()
    print("=" * 60)
    print("  MANUAL STEP REQUIRED")
    print("=" * 60)
    print("  1. Log in to WaterlooWorks (Duo if prompted).")
    print(f"  2. Go to the {config.label} board.")
    print("  3. Set ALL filters (work term, etc.).")
    print("  4. Make sure job listings are visible on screen.")
    print()
    print("  !! Do NOT press Enter until your filtered")
    print("  !! results are on screen — scraper starts from here.")
    print("=" * 60)
    print()

    input("  Press Enter when results are on screen... ")

    page = await resolve_jobs_page(ctx)
    if page is None:
        raise RuntimeError("No browser tab available after login.")

    if not _is_jobs_url(page.url):
        print(f"  Using tab (no jobs.htm in URL): {page.url}")
        print(f"  (If scraping fails, open {config.label} and re-run.)\n")
    else:
        print(f"  Using tab: {page.url}\n")

    await wait_for_listing_ready(page)
    return page


async def get_items_per_page(page) -> int:
    """Read WW DataViewer page size from the page (falls back to 50)."""
    value = await evaluate_retry(page, r"""
        () => {
            for (const script of document.querySelectorAll('script')) {
                const t = script.textContent || '';
                let m = t.match(/itemsPerPage\s*:\s*(\d+)/);
                if (m) return parseInt(m[1], 10);
                m = t.match(/pageSize\s*:\s*(\d+)/);
                if (m) return parseInt(m[1], 10);
            }
            const sel = document.querySelector(
                'select[name*="itemsPerPage"], select[aria-label*="per page" i], select[aria-label*="items per" i]'
            );
            if (sel && sel.value) return parseInt(sel.value, 10);
            return 50;
        }
    """)
    try:
        size = int(value)
    except (TypeError, ValueError):
        size = 50
    return max(10, min(size, 100))


async def get_action_key(page) -> str:
    """Extract the DataViewer action key from the page's embedded script tags."""
    result = await evaluate_retry(page, r"""
        () => {
            for (const script of document.querySelectorAll('script')) {
                const t = script.textContent;
                const m = t.match(/dataParams\s*:\s*\{[^}]*action\s*:\s*['"](_-_-[^'"]{20,})['"]/);
                if (m) return m[1];
            }
            return '';
        }
    """)
    return result or ""


async def fetch_listing_page(
    page,
    action: str,
    page_num: int,
    config: BoardConfig,
    items_per_page: int,
    page_nav_delay_ms: int,
) -> dict:
    """POST to the WW DataViewer endpoint; return IDs and list metadata for one page."""
    columns = list(config.list_columns)
    result = await evaluate_retry(page, """
        async ({action, pageNum, columns, itemsPerPage, pageNavDelayMs}) => {
            const scrapeDom = () => {
                const dom = {};
                document.querySelectorAll('tr.table__row--body').forEach((tr) => {
                    const cb = tr.querySelector('input[name="dataViewerSelection"]');
                    if (!cb?.value) return;
                    const cells = [...tr.querySelectorAll('td.table__value')].map((td) =>
                        (td.innerText || td.textContent || '').trim()
                    );
                    const meta = {};
                    columns.forEach((col, i) => {
                        if (cells[i]) meta[col] = cells[i];
                    });
                    if (Object.keys(meta).length) dom[cb.value] = meta;
                });
                return dom;
            };

            const goToPage = async (n) => {
                const targets = [...document.querySelectorAll(
                    'button, a, [role="button"], .pagination button, .pagination a'
                )];
                const btn = targets.find((el) => {
                    const text = (el.textContent || '').trim();
                    const aria = el.getAttribute('aria-label') || '';
                    return text === String(n)
                        || aria === `Page ${n}`
                        || aria === `Go to page ${n}`;
                });
                if (btn) {
                    btn.click();
                    await new Promise((r) => setTimeout(r, pageNavDelayMs));
                }
            };

            const form = new FormData();
            form.append('action', action);
            form.append('page', String(pageNum));
            form.append('itemsPerPage', String(itemsPerPage));
            form.append('sort', '');
            form.append('filters', '');
            form.append('columns', '');
            form.append('keyword', '');
            form.append('isDataViewer', 'true');
            const resp = await fetch(window.location.href, {
                method: 'POST',
                body: form,
                credentials: 'same-origin',
            });
            const text = await resp.text();
            let json = null;
            try { json = JSON.parse(text); } catch (e) {}

            await goToPage(pageNum);
            return { json, text, dom: scrapeDom() };
        }
    """, {
        "action": action,
        "pageNum": page_num,
        "columns": columns,
        "itemsPerPage": items_per_page,
        "pageNavDelayMs": page_nav_delay_ms,
    })

    raw_text = result.get("text") or ""
    data = result.get("json")
    page_listings, page_ids = parse_listing_page_result(
        raw_text, data, result.get("dom"), config,
    )
    return {"ids": page_ids, "listings": page_listings}


async def collect_all_listings(page, config: BoardConfig) -> dict[str, dict[str, str]]:
    """Walk all listing pages; return job_id → list metadata."""
    action = await get_action_key(page)
    if not action:
        print("[COLLECT] WARNING: action key not found. Confirm the job listing grid is visible on this tab.")
        return {}
    print(f"[COLLECT] action key found ({len(action)} chars)")

    items_per_page = await get_items_per_page(page)
    print(f"[COLLECT] items per page: {items_per_page}")

    all_listings: dict[str, dict[str, str]] = {}
    page_num = 1
    consecutive_no_new = 0

    while True:
        page_nav_s = random.uniform(PAGE_NAV_DELAY_MIN_S, PAGE_NAV_DELAY_MAX_S)
        page_nav_ms = int(page_nav_s * 1000)
        print(f"[COLLECT] page {page_num} (wait {page_nav_s:.1f}s after click)...")
        page_result = await fetch_listing_page(
            page, action, page_num, config, items_per_page, page_nav_ms,
        )
        page_listings = page_result.get("listings") or {}
        page_ids = page_result["ids"]
        added = merge_page_into(all_listings, page_ids, page_listings)
        consecutive_no_new = consecutive_no_new + 1 if added == 0 else 0

        short = bool(page_ids) and len(page_ids) < items_per_page
        short_note = " (short page)" if short else ""
        print(
            f"[COLLECT]   page {page_num}: {len(page_ids)} rows, +{added} new "
            f"(total {len(all_listings)}){short_note}"
        )
        if config.board_type == "full_cycle" and page_ids:
            matched = sum(
                1 for jid in page_ids if page_listings.get(jid, {}).get("apps_count")
            )
            print(
                f"[COLLECT]   apps_count {matched}/{len(page_ids)}"
            )

        stop, reason = should_stop_collecting(
            page_ids=page_ids,
            page_listings=page_listings,
            consecutive_no_new=consecutive_no_new,
            page_num=page_num,
        )
        if stop:
            print(f"[COLLECT] {reason} — done ({len(all_listings)} jobs).")
            break

        page_num += 1

    with_apps = sum(1 for m in all_listings.values() if m.get("apps_count"))
    if config.board_type == "full_cycle" and all_listings:
        print(
            f"[COLLECT] full_cycle apps_count: {with_apps}/{len(all_listings)} "
            f"({100 * with_apps // len(all_listings)}%)"
        )

    return all_listings


async def get_posting_overview(page, job_id: str) -> str | None:
    """Call window.getPostingOverview(jobId) → posting HTML string."""
    try:
        return await evaluate_retry(page, """
            (jobId) => new Promise((resolve, reject) => {
                if (typeof window.getPostingOverview !== 'function') {
                    reject(new Error('getPostingOverview not defined on this page'));
                    return;
                }
                window.getPostingOverview(jobId, (html) => resolve(html || ''));
                setTimeout(() => reject(new Error('timeout after 15s')), 15000);
            })
        """, job_id)
    except PlaywrightError as e:
        print(f"[SCRAPE]   ERROR: {e}")
        return None


async def fetch_overview_with_retry(
    page,
    job_id: str,
    *,
    retries: int = DETAIL_FETCH_RETRIES,
    delay: float = DETAIL_FETCH_RETRY_DELAY_S,
) -> str | None:
    """get_posting_overview with retries — WW times out on individual postings."""
    for attempt in range(1, retries + 1):
        html = await get_posting_overview(page, job_id)
        if html:
            return html
        if attempt < retries:
            print(
                f"[SCRAPE]   no HTML (attempt {attempt}/{retries}) — "
                f"retrying in {delay:.1f}s..."
            )
            await asyncio.sleep(delay)
    return None


async def parse_overview_html(page, html: str) -> dict[str, str]:
    """Inject overview HTML into a temp div, extract key-value pairs."""
    return await evaluate_retry(page, """
        (html) => {
            const div = document.createElement('div');
            div.innerHTML = html;
            const fields = {};
            const links = {};
            div.querySelectorAll('.tag__key-value-list').forEach(container => {
                const labelEl = container.querySelector('.label');
                if (!labelEl) return;
                const label = (labelEl.innerText || labelEl.textContent || '')
                    .trim().replace(/:$/, '');
                const valueRoot = container.cloneNode(true);
                valueRoot.querySelector('.label')?.remove();
                const value = (valueRoot.innerText || valueRoot.textContent || '').trim();
                if (label && value) fields[label] = value;
                const hrefs = [...valueRoot.querySelectorAll('a[href]')]
                    .map(a => (a.getAttribute('href') || '').trim())
                    .filter(h => /^(https?:|mailto:)/i.test(h));
                if (label && hrefs.length) links[label] = [...new Set(hrefs)];
            });
            if (Object.keys(links).length) fields._links = links;
            return fields;
        }
    """, html)


async def get_posting_data(page, job_id: str) -> dict | None:
    """Call window.getPostingData(jobId) → {org, div, divId, geoData, ...}."""
    try:
        return await evaluate_retry(page, """
            (jobId) => new Promise((resolve, reject) => {
                if (typeof window.getPostingData !== 'function') {
                    reject(new Error('getPostingData not defined on this page'));
                    return;
                }
                window.getPostingData(jobId, (data) => {
                    if (typeof data === 'string') {
                        try { data = JSON.parse(data); } catch (e) { data = null; }
                    }
                    resolve(data || null);
                });
                setTimeout(() => reject(new Error('timeout after 15s')), 15000);
            })
        """, job_id)
    except PlaywrightError as e:
        print(f"[RATINGS]   ERROR: {e}")
        return None


async def get_work_term_ratings(page, div_id) -> dict | None:
    """Call window.getWorkTermRatingReportJson(divId) → {sections: [...]}."""
    try:
        return await evaluate_retry(page, """
            (divId) => new Promise((resolve, reject) => {
                if (typeof window.getWorkTermRatingReportJson !== 'function') {
                    reject(new Error('getWorkTermRatingReportJson not defined on this page'));
                    return;
                }
                window.getWorkTermRatingReportJson(divId, (data) => {
                    if (typeof data === 'string') {
                        try { data = JSON.parse(data); } catch (e) { data = null; }
                    }
                    resolve(data || null);
                });
                setTimeout(() => reject(new Error('timeout after 15s')), 15000);
            })
        """, div_id)
    except PlaywrightError as e:
        print(f"[RATINGS]   ERROR: {e}")
        return None


async def ratings_api_available(page) -> bool:
    return bool(await evaluate_retry(page, """
        () => typeof window.getPostingData === 'function'
            && typeof window.getWorkTermRatingReportJson === 'function'
    """))


async def fetch_ratings(page, job_id: str, cache: dict) -> list | None:
    """Ratings sections for a posting's division; cached per divId for the run."""
    data = await get_posting_data(page, job_id)
    div_id = (data or {}).get("divId")
    if div_id is None:
        return None
    key = str(div_id)
    if key not in cache:
        report = await get_work_term_ratings(page, div_id)
        cache[key] = (report or {}).get("sections") or []
    return cache[key]


async def probe_ratings(page, config: BoardConfig, n: int) -> None:
    """Dump raw ratings JSON for the first N listed jobs; touches no scrape output."""
    manifest = listing_manifest_path(config.board_type)
    if manifest.exists():
        ids = json.loads(manifest.read_text()).get("job_ids") or []
        print(f"[PROBE] {len(ids)} job IDs from {manifest.name}")
    else:
        action = await get_action_key(page)
        items = await get_items_per_page(page)
        ids = (await fetch_listing_page(page, action, 1, config, items, 0))["ids"]
        print(f"[PROBE] {len(ids)} job IDs from listing page 1")
    ids = ids[:n]

    if not await ratings_api_available(page):
        print("[PROBE] getPostingData / getWorkTermRatingReportJson not defined on this page.")
        return

    samples = []
    for i, job_id in enumerate(ids, 1):
        if i == 1:
            html = await fetch_overview_with_retry(page, job_id) or ""
            (DATA_DIR / "overview_sample.html").write_text(html)
            print(f"[PROBE] overview HTML for {job_id} → overview_sample.html "
                  f"({html.count('<a ')} anchor tags)")
        data = await get_posting_data(page, job_id)
        div_id = (data or {}).get("divId")
        report = await get_work_term_ratings(page, div_id) if div_id is not None else None
        sections = (report or {}).get("sections") or []
        print(f"[PROBE] ({i}/{len(ids)}) job {job_id} div {div_id} "
              f"{(data or {}).get('org', '')!r}: {len(sections)} section(s)")
        for sec in sections:
            print(f"[PROBE]     {sec.get('type')}: {sec.get('title')!r}")
        samples.append({
            "job_id": job_id,
            "posting_data": data,
            "ratings": report,
        })
        await asyncio.sleep(random.uniform(0.8, 1.2))

    out = DATA_DIR / "ratings_sample.json"
    out.write_text(json.dumps(samples, indent=2))
    print(f"[PROBE] wrote {out}")


# ── Scrape loop ───────────────────────────────────────────────────────────────

async def run_scrape(
    ctx, page, config: BoardConfig, *, resume: bool = False, ratings: bool = True,
) -> None:
    listings = await collect_all_listings(page, config)
    if not listings:
        print("[SCRAPE] No job listings found. Confirm filters are set and listings are visible, then re-run.")
        return

    all_ids = list(listings.keys())

    # Record the full listing so ingest can purge postings no longer on the board,
    # regardless of whether every detail fetch below succeeds.
    manifest_now = datetime.now(timezone.utc).isoformat()
    write_listing_manifest(config.board_type, all_ids, manifest_now)

    done = load_done()
    if resume:
        todo = [jid for jid in all_ids if jid not in done]
        skipped_done = len(all_ids) - len(todo)
        print(
            f"\n[SCRAPE] Listing collected {len(all_ids)} jobs. "
            f"Resume mode: {len(todo)} to scrape, {skipped_done} already in postings.jsonl.\n"
        )
    else:
        todo = list(all_ids)
        print(
            f"\n[SCRAPE] Listing collected {len(all_ids)} jobs. "
            f"Full refresh: re-scraping all {len(todo)} "
            f"({len(done)} already in postings.jsonl will be refreshed).\n"
        )

    if ratings and not await ratings_api_available(page):
        print("[SCRAPE] Ratings API not defined on this page — skipping work term ratings.")
        ratings = False
    ratings_cache: dict[str, list] = {}

    scraped = 0
    failed: list[str] = []
    for i, job_id in enumerate(todo, 1):
        print(f"[SCRAPE] ({i}/{len(todo)}) job {job_id}")

        html = await fetch_overview_with_retry(page, job_id)
        if not html:
            print("[SCRAPE]   FAILED (no HTML after retries).")
            failed.append(job_id)
        else:
            fields = await parse_overview_html(page, html)
            if ratings:
                sections = await fetch_ratings(page, job_id, ratings_cache)
                if sections:
                    fields["_ratings"] = sections
            list_meta = listings.get(job_id, {})
            now = datetime.now(timezone.utc).isoformat()
            row = build_row(job_id, config.board_type, fields, now, list_meta)
            append_output(row)
            title = row.get("title") or "(no title)"
            org = row.get("org") or "(no org)"
            apps = row.get("apps_count", "")
            extra = f" apps={apps}" if apps else ""
            if fields.get("_ratings"):
                extra += f" ratings={len(fields['_ratings'])}"
            print(f"[SCRAPE]   → {title} @ {org}{extra}")
            scraped += 1

        if i < len(todo):
            delay = random.uniform(0.8, 1.2)
            print(f"[SCRAPE]   sleeping {delay:.1f}s...")
            await asyncio.sleep(delay)

    # Diagnostics: make loss between "listed" and "scraped" visible.
    if failed:
        fail_path = DATA_DIR / f"failed_{config.board_type}.json"
        fail_path.write_text(json.dumps({
            "board_type": config.board_type,
            "scraped_at": manifest_now,
            "job_ids": failed,
        }, indent=2))
        print(
            f"\n[SCRAPE] {len(failed)} posting(s) failed detail fetch — "
            f"IDs written to {fail_path.name} (re-run to retry)."
        )

    print(
        f"\n[SCRAPE] Done. Listed {len(all_ids)}, attempted {len(todo)}, "
        f"scraped {scraped}, failed {len(failed)}. "
        f"postings.jsonl now holds {len(load_done())} unique jobs."
    )


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    args = parse_args()
    config = BOARDS[args.board]

    DATA_DIR.mkdir(exist_ok=True)
    PROFILE_DIR.mkdir(exist_ok=True)

    print(f"[goosehunt] board: {config.label} ({config.board_type})")

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://waterlooworks.uwaterloo.ca/", wait_until="domcontentloaded")
        jobs_page = await wait_for_login(ctx, config)
        await wait_for_listing_ready(jobs_page)

        if args.probe_ratings:
            await probe_ratings(jobs_page, config, args.probe_ratings)
        else:
            await run_scrape(
                ctx, jobs_page, config,
                resume=args.resume, ratings=not args.no_ratings,
            )

        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
