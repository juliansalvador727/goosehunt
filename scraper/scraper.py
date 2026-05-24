#!/usr/bin/env python3
"""
goosehunt scraper — WaterlooWorks job boards (Employer Direct, Full Cycle).

Uses WW's in-page JS API (credit: bryanling1/waterlooworks-scraper):
  1. Extract the 'action' key embedded in the page's JS.
  2. POST to the listing endpoint (100 per page) to collect job IDs + list metadata.
  3. Call window.getPostingOverview(jobId, cb) for each job → HTML string.
  4. Parse HTML in-memory; no new tabs opened.

Usage:
  python -m scraper.scraper --board direct       # Employer Direct (default)
  python -m scraper.scraper --board full_cycle   # Full Cycle Service
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
    fields: dict[str, str],
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
    apps = merged.get("apps_count") or merged.get("apps") or ""
    if apps:
        row["apps_count"] = apps
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


async def fetch_listing_page(page, action: str, page_num: int) -> dict:
    """POST to the WW DataViewer endpoint; return IDs and raw HTML when available."""
    result = await evaluate_retry(page, """
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
            const text = await resp.text();
            try {
                return {json: JSON.parse(text), text: text};
            } catch (e) {
                return {json: null, text: text};
            }
        }
    """, {"action": action, "pageNum": page_num})

    raw_text = result.get("text") or ""
    ids: list[str] = []

    if result["json"] is not None:
        data = result["json"]
        rows = data.get("data") or data.get("rows") or data.get("resultIds") or []
        if isinstance(rows, list):
            for item in rows:
                if isinstance(item, dict):
                    jid = str(item.get("id") or item.get("jobId") or item.get("postingId") or "")
                    if jid and len(jid) >= 5:
                        ids.append(jid)
                elif isinstance(item, (str, int)) and len(str(item)) >= 5:
                    ids.append(str(item))
        if not ids:
            ids = extract_ids_from_html(raw_text or str(data))
    else:
        ids = extract_ids_from_html(raw_text)

    return {"ids": ids, "html": raw_text or None}


async def collect_all_listings(page, config: BoardConfig) -> dict[str, dict[str, str]]:
    """Walk all listing pages; return job_id → list metadata."""
    action = await get_action_key(page)
    if not action:
        print("[COLLECT] WARNING: action key not found. Confirm the job listing grid is visible on this tab.")
        return {}
    print(f"[COLLECT] action key found ({len(action)} chars)")

    all_listings: dict[str, dict[str, str]] = {}
    page_num = 1

    while True:
        print(f"[COLLECT] page {page_num}...")
        page_result = await fetch_listing_page(page, action, page_num)
        page_listings = (
            parse_list_rows(page_result["html"], config)
            if page_result["html"]
            else {}
        )
        prior_count = len(all_listings)

        for jid in page_result["ids"]:
            if jid not in all_listings:
                all_listings[jid] = page_listings.get(jid, {})

        for jid, meta in page_listings.items():
            if jid not in all_listings:
                all_listings[jid] = dict(meta)
            else:
                all_listings[jid].update(meta)

        added = len(all_listings) - prior_count
        if added == 0:
            print(f"[COLLECT] no new listings on page {page_num} — done.")
            break

        print(f"[COLLECT] +{added} (total: {len(all_listings)})")
        page_num += 1
        await asyncio.sleep(random.uniform(0.5, 1.0))

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


async def parse_overview_html(page, html: str) -> dict[str, str]:
    """Inject overview HTML into a temp div, extract key-value pairs."""
    return await evaluate_retry(page, """
        (html) => {
            const div = document.createElement('div');
            div.innerHTML = html;
            const fields = {};
            div.querySelectorAll('.tag__key-value-list').forEach(container => {
                const labelEl = container.querySelector('.label');
                if (!labelEl) return;
                const label = (labelEl.innerText || labelEl.textContent || '')
                    .trim().replace(/:$/, '');
                const valueRoot = container.cloneNode(true);
                valueRoot.querySelector('.label')?.remove();
                const value = (valueRoot.innerText || valueRoot.textContent || '').trim();
                if (label && value) fields[label] = value;
            });
            return fields;
        }
    """, html)


# ── Scrape loop ───────────────────────────────────────────────────────────────

async def run_scrape(ctx, page, config: BoardConfig) -> None:
    listings = await collect_all_listings(page, config)
    if not listings:
        print("[SCRAPE] No job listings found. Confirm filters are set and listings are visible, then re-run.")
        return

    all_ids = list(listings.keys())
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
            list_meta = listings.get(job_id, {})
            now = datetime.now(timezone.utc).isoformat()
            row = build_row(job_id, config.board_type, fields, now, list_meta)
            append_output(row)
            title = row.get("title") or "(no title)"
            org = row.get("org") or "(no org)"
            apps = row.get("apps_count", "")
            extra = f" apps={apps}" if apps else ""
            print(f"[SCRAPE]   → {title} @ {org}{extra}")
            scraped += 1

        if i < len(todo):
            delay = random.uniform(0.8, 1.2)
            print(f"[SCRAPE]   sleeping {delay:.1f}s...")
            await asyncio.sleep(delay)

    print(f"\n[SCRAPE] Done. {scraped} scraped this run. Total: {len(load_done())}.")


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

        await run_scrape(ctx, jobs_page, config)

        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
