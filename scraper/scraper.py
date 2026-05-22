#!/usr/bin/env python3
"""
goosehunt scraper — WaterlooWorks Employer Direct.

Uses WW's in-page JS API (credit: bryanling1/waterlooworks-scraper):
  1. Extract the 'action' key embedded in the page's JS.
  2. POST to the listing endpoint (100 per page) to collect all job IDs.
  3. Call window.getPostingOverview(jobId, cb) for each job → HTML string.
  4. Parse HTML in-memory; no new tabs opened.

Usage:
  python -m scraper.scraper        # full scrape
  python -m scraper.scraper --diag # inspect page state, try one posting, exit
"""

import asyncio
import json
import random
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).parent.parent
PROFILE_DIR = Path(__file__).parent / "profile"
DATA_DIR = ROOT / "data"
OUTPUT_FILE = DATA_DIR / "postings.jsonl"

BOARD_TYPE = "direct"

FIELD_MAP: dict[str, list[str]] = {
    "title":            ["job title", "position title", "title"],
    "org":              ["organization", "employer", "company name"],
    "location":         ["location", "city", "work location"],
    "deadline":         ["deadline", "application deadline", "apply by"],
    "work_term":        ["work term", "term", "co-op term"],
    "openings":         ["openings", "number of positions", "positions available"],
    "summary":          ["job summary", "summary", "overview", "job description", "description"],
    "responsibilities": ["responsibilities", "duties", "key responsibilities"],
    "required_skills":  ["required skills", "skills required", "qualifications", "technical skills"],
}


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


def build_row(job_id: str, board_type: str, fields: dict[str, str], now: str) -> dict:
    row: dict = {
        "job_id": job_id,
        "board_type": board_type,
        "raw_fields_json": json.dumps(fields),
        "scraped_at": now,
        "updated_at": now,
    }
    for col, candidates in FIELD_MAP.items():
        row[col] = pick_field(fields, candidates)
    return row


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
    ]:
        for m in re.finditer(pattern, html, re.IGNORECASE):
            jid = m.group(1)
            if jid not in seen:
                seen.add(jid)
                ids.append(jid)
    return ids


# ── Browser helpers ───────────────────────────────────────────────────────────

async def wait_for_login(page) -> None:
    print()
    print("=" * 60)
    print("  MANUAL STEP REQUIRED")
    print("=" * 60)
    print("  1. Log in to WaterlooWorks (Duo if prompted).")
    print("  2. Go to the Employer Direct board.")
    print("  3. Set ALL filters (work term, etc.).")
    print("  4. Make sure job listings are visible on screen.")
    print()
    print("  !! Do NOT press Enter until your filtered")
    print("  !! results are on screen — scraper starts from here.")
    print("=" * 60)
    print()
    input("  Press Enter when results are on screen... ")
    print()


async def get_action_key(page) -> str:
    """Extract the WW action key from the page's embedded script tags."""
    return await page.evaluate("""
        () => {
            for (const script of document.querySelectorAll('script')) {
                const t = script.textContent;
                // Pattern 1: dataParams key
                let m = t.match(/dataParams[^'"]*['"]([^'"]{20,})['"]/);
                if (m) return m[1];
                // Pattern 2: action key starting with _-_-
                m = t.match(/['"](_-_-[^'"]{10,})['"]/);
                if (m) return m[1];
            }
            return '';
        }
    """) or ""


async def fetch_page_of_ids(page, action: str, page_num: int) -> list[str]:
    """POST to the WW listing endpoint and return job IDs from that page."""
    html = await page.evaluate("""
        async ({action, pageNum}) => {
            const form = new FormData();
            form.append('action',       action);
            form.append('page',         String(pageNum));
            form.append('itemsPerPage', '100');
            form.append('sort',         '');
            form.append('filters',      '');
            form.append('columns',      '');
            form.append('keyword',      '');
            form.append('isDataViewer', 'true');
            const resp = await fetch(window.location.href, {
                method: 'POST',
                body: form,
                credentials: 'same-origin',
            });
            return await resp.text();
        }
    """, {"action": action, "pageNum": page_num})
    return extract_ids_from_html(html)


async def collect_all_job_ids(page) -> list[str]:
    """Walk all pages of the listing; return every unique job ID."""
    action = await get_action_key(page)
    if not action:
        print("[COLLECT] WARNING: action key not found. Run --diag to troubleshoot.")
        return []
    print(f"[COLLECT] action key found ({len(action)} chars)")

    all_ids: list[str] = []
    seen: set[str] = set()
    page_num = 1

    while True:
        print(f"[COLLECT] page {page_num}...")
        ids = await fetch_page_of_ids(page, action, page_num)
        new_ids = [i for i in ids if i not in seen]
        if not new_ids:
            print(f"[COLLECT] no new IDs on page {page_num} — done.")
            break
        for jid in new_ids:
            seen.add(jid)
            all_ids.append(jid)
        print(f"[COLLECT] +{len(new_ids)} (total: {len(all_ids)})")
        page_num += 1
        await asyncio.sleep(random.uniform(0.5, 1.0))

    return all_ids


async def get_posting_overview(page, job_id: str) -> str | None:
    """Call window.getPostingOverview(jobId) → posting HTML string."""
    try:
        return await page.evaluate("""
            (jobId) => new Promise((resolve, reject) => {
                if (typeof window.getPostingOverview !== 'function') {
                    reject(new Error('getPostingOverview not defined on this page'));
                    return;
                }
                window.getPostingOverview(jobId, (html) => resolve(html || ''));
                setTimeout(() => reject(new Error('timeout after 15s')), 15000);
            })
        """, job_id)
    except Exception as e:
        print(f"[SCRAPE]   ERROR: {e}")
        return None


async def parse_overview_html(page, html: str) -> dict[str, str]:
    """Inject overview HTML into a temp div, extract table rows, parse into dict."""
    rows_data = await page.evaluate("""
        (html) => {
            const div = document.createElement('div');
            div.innerHTML = html;
            const rows = [];
            div.querySelectorAll('table tr').forEach(tr => {
                const cells = [];
                tr.querySelectorAll('td, th').forEach(td => {
                    cells.push((td.innerText || td.textContent || '').trim());
                });
                if (cells.some(c => c.length > 0)) rows.push(cells);
            });
            return rows;
        }
    """, html)
    return parse_table_rows(rows_data)


# ── Scrape loop ───────────────────────────────────────────────────────────────

async def run_scrape(ctx) -> None:
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()

    all_ids = await collect_all_job_ids(page)
    if not all_ids:
        print("[SCRAPE] No job IDs found. Run --diag to troubleshoot.")
        return

    done = load_done()
    todo = [jid for jid in all_ids if jid not in done]
    print(f"\n[SCRAPE] {len(todo)} to scrape, {len(done)} already done.\n")

    scraped = 0
    for i, job_id in enumerate(todo, 1):
        print(f"[SCRAPE] ({i}/{len(todo)}) job {job_id}")

        html = await get_posting_overview(page, job_id)
        if not html:
            print("[SCRAPE]   skipped (no HTML returned).")
        else:
            fields = await parse_overview_html(page, html)
            now = datetime.now(timezone.utc).isoformat()
            row = build_row(job_id, BOARD_TYPE, fields, now)
            append_output(row)
            title = row.get("title") or "(no title)"
            org   = row.get("org")   or "(no org)"
            print(f"[SCRAPE]   → {title} @ {org}")
            scraped += 1

        if i < len(todo):
            delay = random.uniform(1.5, 3.5)
            print(f"[SCRAPE]   sleeping {delay:.1f}s...")
            await asyncio.sleep(delay)

    print(f"\n[SCRAPE] Done. {scraped} scraped this run. Total: {len(load_done())}.")


# ── Diagnostic ────────────────────────────────────────────────────────────────

async def run_diag(ctx) -> None:
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    print(f"\nDIAG  url: {page.url}\n")

    action = await get_action_key(page)
    print(f"action key : {'found (' + str(len(action)) + ' chars)' if action else 'NOT FOUND'}")
    if action:
        print(f"  {action[:100]}")

    for fn in ["getPostingOverview", "getPostingData", "orbisAppSr"]:
        exists = await page.evaluate(f"() => typeof window.{fn} !== 'undefined'")
        status = "found" if exists else "NOT FOUND"
        print(f"window.{fn:25s}: {status}")

    if not action:
        print("\nCannot proceed without action key.")
        return

    print("\nFetching page 1 of job IDs via POST...")
    ids = await fetch_page_of_ids(page, action, 1)
    print(f"  job IDs found: {len(ids)}  {ids[:8]}")

    if not ids:
        print("  No IDs found — check extract_ids_from_html patterns.")
        print("  Printing 2000 chars of POST response HTML for debugging:")
        html = await page.evaluate("""
            async (action) => {
                const form = new FormData();
                form.append('action', action);
                form.append('page', '1');
                form.append('itemsPerPage', '100');
                form.append('isDataViewer', 'true');
                const resp = await fetch(window.location.href, {method:'POST', body:form, credentials:'same-origin'});
                return await resp.text();
            }
        """, action)
        print(html[:2000])
        return

    print(f"\nTrying getPostingOverview({ids[0]})...")
    html = await get_posting_overview(page, ids[0])
    if html:
        print(f"  HTML returned: {len(html)} chars")
        fields = await parse_overview_html(page, html)
        print(f"  Fields extracted: {list(fields.keys())[:10]}")
        print(f"  Title   : {fields.get('Job Title') or fields.get('Position Title') or '(not found)'}")
        print(f"  Org     : {fields.get('Organization') or '(not found)'}")
    else:
        print("  No HTML returned.")
        print("  First 500 chars of raw overview call:")
        raw = await page.evaluate("""
            (jobId) => new Promise((resolve) => {
                if (typeof window.getPostingOverview !== 'function') { resolve('function not found'); return; }
                window.getPostingOverview(jobId, (h) => resolve(h || '(empty)'));
                setTimeout(() => resolve('(timeout)'), 10000);
            })
        """, ids[0])
        print(f"  {str(raw)[:500]}")


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    PROFILE_DIR.mkdir(exist_ok=True)

    diag = "--diag" in sys.argv[1:]

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://waterlooworks.uwaterloo.ca/", wait_until="domcontentloaded")
        await wait_for_login(page)

        if diag:
            await run_diag(ctx)
        else:
            await run_scrape(ctx)

        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
