"""Parse job IDs out of a pasted WaterlooWorks "Applications" page.

The user copies the whole page (Ctrl+A, Ctrl+C) from
`Postings / Applications → Applications` and pastes it into the UI. Every
application there is one row rendered as a run of lines:

    preview
    print
    cancel
    Software Engineering Intern
    483949
    2027 - Winter
    Fable Security Inc
    Applied
    ...

Only the job ID matters, so the parser anchors on the two lines that are
machine-shaped — a bare numeric ID immediately followed by a work term
("2027 - Winter") — and ignores everything else on the page.
"""

import re

# WW job IDs are 6 digits today; 5-8 leaves room without matching years,
# opening counts, or the times in the deadline columns.
_ID_RE = re.compile(r"\A\d{5,8}\Z")
_TERM_RE = re.compile(r"\A\d{4}\s*-\s*\S")
_LOOSE_ID_RE = re.compile(r"\b\d{5,8}\b")


def _dedupe(ids) -> list[str]:
    seen: dict[str, None] = {}
    for job_id in ids:
        seen.setdefault(job_id, None)
    return list(seen)


def parse_applied_job_ids(text: str) -> list[str]:
    """Job IDs in the pasted applications page, in the order they appear.

    Prefers the ID-then-work-term row structure. Falls back to every bare
    5-8 digit number when the paste arrived without line breaks (some
    browsers flatten a copied table), which is safe because the caller only
    keeps IDs that already exist in the database.
    """
    lines = [line.strip() for line in (text or "").splitlines()]
    lines = [line for line in lines if line]

    structured = [
        line
        for line, nxt in zip(lines, lines[1:])
        if _ID_RE.match(line) and _TERM_RE.match(nxt)
    ]
    if structured:
        return _dedupe(structured)
    return _dedupe(_LOOSE_ID_RE.findall(text or ""))
