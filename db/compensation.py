"""Parse free-form posting compensation into persisted, auditable fields."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path


ROOT = Path(__file__).parent.parent
FX_PATH = ROOT / "config" / "fx_rates.json"

with FX_PATH.open(encoding="utf-8") as _fh:
    _FX = json.load(_fh)

FX_DATE: str = _FX["date"]
FX_SOURCE: str = _FX["source"]
FX_RATES_TO_CAD: dict[str, float] = {
    key: float(value) for key, value in _FX["rates_to_cad"].items()
}

DEFAULT_HOURS_PER_WEEK = 40.0
DEFAULT_TERM_MONTHS = 4.0
COMPENSATION_VERSION = 1

_COUNTRY_CURRENCY = {
    "canada": "CAD",
    "united states": "USD",
    "germany": "EUR",
    "singapore": "SGD",
    "china": "CNY",
    "turkey": "TRY",
    "india": "INR",
}

_CURRENCY_REPLACEMENTS = (
    (re.compile(r"CA\$", re.I), "CAD $"),
    (re.compile(r"US\$", re.I), "USD $"),
    (re.compile(r"NT\$", re.I), "TWD $"),
    (re.compile(r"\bRMB\b", re.I), "CNY"),
    (re.compile(r"\beuros?\b", re.I), "EUR"),
)
_CURRENCY_RE = re.compile(r"CAD|USD|CNY|EUR|TWD|INR|HKD|SGD|GBP|TRY|[$€£]", re.I)

# Order matters: European and grouped formats must win before the simple number.
_NUMBER = (
    r"(?:\d{1,3}(?:\.\d{3})+,\d{2}|"
    r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|"
    r"\d{1,3}(?:\s\d{3})+(?:[.,]\d+)?|"
    r"\d+,\d{2}|\d+(?:\.\d+)?)"
)
_CUR_CODE = r"(?:CAD|USD|CNY|EUR|TWD|INR|HKD|SGD|GBP|TRY)"
_CUR_MARK = rf"(?:{_CUR_CODE}|[$€£])"
_AMOUNT = rf"(?:(?:{_CUR_CODE})\s*)?(?:[$€£]\s*)?{_NUMBER}\s*[kK]?(?:\s*(?:{_CUR_MARK}))?"
_RANGE_RE = re.compile(
    rf"(?P<lo>{_AMOUNT})\s*(?:--+|[-–—~]|\bto\b)\s*(?P<hi>{_AMOUNT})",
    re.I,
)
_TOKEN_RE = re.compile(_AMOUNT, re.I)

_PERIOD_PATTERNS = (
    ("biweekly", re.compile(r"bi[- ]?weekly|per\s+two\s+weeks?|/\s*bi[- ]?week", re.I)),
    ("hour", re.compile(r"hourly|hrly|per\s+(?:an?\s+)?hours?|an?\s+hour|/\s*(?:h|hr|hour)s?\b|\bhour\s*/|(?<=\d)\s*hrs?\b", re.I)),
    ("day", re.compile(r"per\s+day|/\s*day\b|daily\s+(?:rate|pay|salary|stipend)", re.I)),
    ("week", re.compile(r"(?<!bi[- ])weekly|per\s+(?:\d+(?:\.\d+)?[- ]hour\s+work\s+)?weeks?|/\s*(?:wk|week)s?\b|work\s+week", re.I)),
    ("month", re.compile(r"monthly|per\s+months?|/\s*(?:mo|month)s?\b|for\s+each\s+month", re.I)),
    ("year", re.compile(r"annual(?:ly|ized)?|per\s+years?|/\s*(?:yr|year)s?\b|per\s+annum", re.I)),
    ("term", re.compile(r"per\s+(?:work\s+)?term|for\s+(?:each|the|a)?\s*(?:\d+[- ]month\s+)?(?:work\s+)?term|/\s*term\b", re.I)),
)
_PAY_WORD_RE = re.compile(
    r"compensation|base\s+salary|salary|\bpay(?:ed|ing)?\b|paid|"
    r"hourly\s+(?:rate|wage|pay|salary)|wage|stipend|honorarium|rate\s*:|\brange\s*:",
    re.I,
)
_BASE_WORD_RE = re.compile(r"base\s+(?:pay|salary)|salary|compensation|wage|pay\s+rate", re.I)
_COMPONENT_RE = re.compile(
    r"housing|relocation|allowance|bonus|equity|reimburse|vacation|meal|lunch|"
    r"dinner|gym|wellness|technology|equipment|travel|sign[- ]?on",
    re.I,
)
_REFERENCE_RE = re.compile(
    r"rates? of pay|pay scale|pay grid|earnings (?:chart|report)|waterloo.*(?:average|guideline)|"
    r"average earnings|government.*rates?|treasury board",
    re.I,
)
_UNDISCLOSED_RE = re.compile(
    r"\bTBD\b|to be determined|to be confirmed|to be discussed|discussed? (?:during|in|at|with)|"
    r"competitive (?:salary|pay|wage|compensation)|commensurate with|upon match|offer letter",
    re.I,
)
_CONDITIONAL_RE = re.compile(
    r"depending|based on|work term\s*\d|first\s+(?:year|term)|second\s+(?:year|term)|"
    r"third\s+(?:year|term)|fourth\s+(?:year|term)|fifth\s+(?:year|term)|sixth\s+(?:year|term)|"
    r"\d+(?:st|nd|rd|th)\s+(?:year|term)|"
    r"bachelor|master|phd|academic (?:level|year)|year of study",
    re.I,
)


@dataclass
class Candidate:
    native_min: float
    native_max: float
    currency: str
    period: str
    score: int
    explicit_period: bool
    inferred_currency: bool
    inferred_period: bool
    context: str


def _clean_text(value: object) -> str:
    text = str(value or "").replace("\u00a0", " ")
    for pattern, replacement in _CURRENCY_REPLACEMENTS:
        text = pattern.sub(replacement, text)
    text = re.sub(r"\b(CAD|USD|CNY|EUR|TWD|INR|HKD|SGD|GBP|TRY)(?=\d)", r"\1 ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def _number(value: str) -> float:
    value = value.strip().replace(" ", "")
    if "." in value and "," in value:
        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", "")
    elif "," in value:
        tail = value.rsplit(",", 1)[1]
        value = value.replace(",", ".") if len(tail) == 2 else value.replace(",", "")
    return float(value)


def _amount(token: str) -> tuple[float, str | None, bool]:
    currency = None
    upper = token.upper()
    for code in FX_RATES_TO_CAD:
        if code in upper:
            currency = code
            break
    if currency is None:
        if "€" in token:
            currency = "EUR"
        elif "£" in token:
            currency = "GBP"
    number_match = re.search(_NUMBER, token)
    if number_match is None:
        raise ValueError(f"No number in amount token: {token!r}")
    amount = _number(number_match.group(0))
    is_k = bool(re.search(r"[kK](?:\s|$)", token))
    if is_k:
        amount *= 1000
    return amount, currency, "$" in token or currency is not None


def _nearest_period(text: str, start: int, end: int) -> tuple[str | None, bool]:
    matches: list[tuple[int, int, str]] = []
    for priority, (period, pattern) in enumerate(_PERIOD_PATTERNS):
        for match in pattern.finditer(text):
            if match.end() < start:
                distance = start - match.end()
            elif match.start() > end:
                distance = match.start() - end
            else:
                distance = 0
            if distance <= 65:
                between = text[match.end():start] if match.end() < start else text[end:match.start()]
                if _TOKEN_RE.search(between):
                    continue
                matches.append((distance, priority, period))
    return (min(matches)[2], True) if matches else (None, False)


def _infer_period(lo: float, hi: float, context: str, country: str) -> str | None:
    lower = context.lower()
    if "base salary" in lower:
        if lo >= 30_000:
            return "year"
        if lo >= 1_000 and country in {"united states", "canada"}:
            return "month"
    if re.search(r"annual(?:ly|ized)?", lower):
        return "year"
    if _PAY_WORD_RE.search(context):
        if 10 <= lo <= hi <= 300:
            return "hour"
        if lo >= 30_000:
            return "year"
    # A bare range in the dedicated compensation field is still meaningful;
    # keep the inference low-confidence rather than silently discarding it.
    if lo != hi:
        if 10 <= lo <= hi <= 300:
            return "hour"
        if lo >= 30_000:
            return "year"
    return None


def _plausible(period: str, lo: float, hi: float) -> bool:
    limits = {
        "hour": (10, 300),
        "day": (20, 3_000),
        "week": (300, 10_000),
        "biweekly": (500, 20_000),
        "month": (300, 50_000),
        "year": (10_000, 1_000_000),
        "term": (1_000, 250_000),
    }
    low_limit, high_limit = limits[period]
    return low_limit <= lo <= hi <= high_limit


def _candidate(
    text: str,
    start: int,
    end: int,
    lo_token: str,
    hi_token: str | None,
    country: str,
) -> Candidate | None:
    lo, lo_currency, lo_marked = _amount(lo_token)
    hi, hi_currency, hi_marked = _amount(hi_token or lo_token)
    # "$12-16K" means $12K-$16K, not $12-$16K.
    if hi_token and hi >= 1_000 and lo < 1_000 and re.search(r"[kK]", hi_token):
        lo *= 1_000
    if hi < lo:
        lo, hi = hi, lo

    before = text[max(0, start - 70):start]
    after = text[end:min(len(text), end + 85)]
    context = (before + text[start:end] + after).strip()
    currency = lo_currency or hi_currency
    inferred_currency = currency is None
    if currency is None:
        currency = _COUNTRY_CURRENCY.get(country, "CAD")

    period, explicit_period = _nearest_period(text, start, end)
    inferred_period = False
    if period is not None and not _plausible(period, lo, hi):
        period = None
        explicit_period = False
    if period is None:
        period = _infer_period(lo, hi, context, country)
        inferred_period = period is not None
    if period is None and (lo_marked or hi_marked) and _CONDITIONAL_RE.search(text):
        if 10 <= lo <= hi <= 300:
            period = "hour"
            inferred_period = True
    if period is None and (lo_marked or hi_marked) and len(text) <= 40:
        if 10 <= lo <= hi <= 300:
            period = "hour"
        elif lo >= 30_000:
            period = "year"
        inferred_period = period is not None
    if period is None or not _plausible(period, lo, hi):
        return None

    has_pay_word = bool(_PAY_WORD_RE.search(context))
    if not (lo_marked or hi_marked or has_pay_word or hi_token):
        return None

    score = 5 if explicit_period else 1
    score += 2 if (lo_marked or hi_marked) else 0
    score += 3 if has_pay_word else 0
    score += 2 if hi_token else 0
    if _BASE_WORD_RE.search(context):
        score += 2
    immediate_before = text[max(0, start - 35):start]
    if _COMPONENT_RE.search(immediate_before):
        return None
    immediate_after = text[end:min(len(text), end + 30)]
    if _COMPONENT_RE.search(immediate_after) and not re.search(
        r"base\s+salary|salary\s*:\s*|compensation\s*:\s*", immediate_before, re.I
    ):
        return None
    return Candidate(
        native_min=lo,
        native_max=hi,
        currency=currency,
        period=period,
        score=score,
        explicit_period=explicit_period,
        inferred_currency=inferred_currency,
        inferred_period=inferred_period,
        context=context[:220],
    )


def _collect_candidates(text: str, country: str) -> list[Candidate]:
    candidates: list[Candidate] = []
    range_spans: list[tuple[int, int]] = []
    for match in _RANGE_RE.finditer(text):
        candidate = _candidate(
            text, match.start(), match.end(), match.group("lo"), match.group("hi"), country
        )
        if candidate:
            candidates.append(candidate)
            range_spans.append(match.span())

    for match in _TOKEN_RE.finditer(text):
        if any(start <= match.start() and match.end() <= end for start, end in range_spans):
            continue
        candidate = _candidate(text, match.start(), match.end(), match.group(0), None, country)
        if candidate:
            candidates.append(candidate)
    return candidates


def _hours_per_week(text: str) -> float | None:
    patterns = (
        r"(\d{2}(?:\.\d+)?)\s*(?:hours?|hrs?)\s*(?:per|/)\s*week",
        r"(\d{2}(?:\.\d+)?)\s*[- ]?hours?\s+work\s+week",
        r"work\s+week\D{0,15}(\d{2}(?:\.\d+)?)\s*hours?",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            value = float(match.group(1))
            if 20 <= value <= 80:
                return value
    return None


def _term_months(data: dict) -> float:
    duration = str(data.get("Work Term Duration") or "")
    match = re.search(r"(\d+)\s*month", duration, re.I)
    return float(match.group(1)) if match else DEFAULT_TERM_MONTHS


def _hourly(amount: float, period: str, hours_week: float, term_months: float) -> float:
    divisors = {
        "hour": 1.0,
        "day": 8.0,
        "week": hours_week,
        "biweekly": hours_week * 2,
        "month": hours_week * 52 / 12,
        "year": hours_week * 52,
        "term": hours_week * 52 * term_months / 12,
    }
    return amount / divisors[period]


def _empty(raw_text: str, status: str, confidence: str | None = None) -> dict:
    return {
        "comp_raw_text": raw_text,
        "comp_native_min": None,
        "comp_native_max": None,
        "comp_currency": None,
        "comp_period": None,
        "comp_hours_per_week": None,
        "comp_hourly_native_min": None,
        "comp_hourly_native_max": None,
        "comp_hourly_cad_min": None,
        "comp_hourly_cad_max": None,
        "comp_hourly_cad_mid": None,
        "comp_fx_rate": None,
        "comp_fx_date": None,
        "comp_parse_status": status,
        "comp_confidence": confidence,
        "comp_tiers_json": None,
        "comp_parser_version": COMPENSATION_VERSION,
    }


def normalize_compensation(raw_json: str | dict) -> dict:
    """Return columns for one posting's persisted compensation interpretation."""
    try:
        data = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
    except (TypeError, json.JSONDecodeError):
        return _empty("", "unparsed")
    if not isinstance(data, dict):
        return _empty("", "unparsed")

    raw_text = _clean_text(data.get("Compensation and Benefits"))
    if not raw_text:
        return _empty(raw_text, "not_disclosed")
    country = str(data.get("Job - Country") or "").strip().lower()
    candidates = _collect_candidates(raw_text, country)
    if not candidates:
        if _REFERENCE_RE.search(raw_text):
            return _empty(raw_text, "reference_only")
        if _UNDISCLOSED_RE.search(raw_text) or not re.search(r"[$€£]|\d", raw_text):
            return _empty(raw_text, "not_disclosed")
        return _empty(raw_text, "unparsed")

    best = max(candidates, key=lambda candidate: candidate.score)
    selected = [
        candidate for candidate in candidates
        if candidate.period == best.period
        and candidate.currency == best.currency
        and candidate.score >= best.score - 1
    ]
    native_min = min(candidate.native_min for candidate in selected)
    native_max = max(candidate.native_max for candidate in selected)
    hours = _hours_per_week(raw_text)
    conversion_hours = hours or DEFAULT_HOURS_PER_WEEK
    term_months = _term_months(data)
    hourly_native_min = _hourly(native_min, best.period, conversion_hours, term_months)
    hourly_native_max = _hourly(native_max, best.period, conversion_hours, term_months)
    fx_rate = FX_RATES_TO_CAD.get(best.currency)
    hourly_cad_min = hourly_native_min * fx_rate if fx_rate is not None else None
    hourly_cad_max = hourly_native_max * fx_rate if fx_rate is not None else None

    inferred = any(candidate.inferred_period or candidate.inferred_currency for candidate in selected)
    confidence = "medium" if inferred or hours is None and best.period != "hour" else "high"
    if best.inferred_period:
        confidence = "low"
    conditional = len(selected) > 1 or bool(_CONDITIONAL_RE.search(raw_text))
    tiers = None
    if conditional or len(selected) > 1:
        tiers = json.dumps([asdict(candidate) for candidate in selected], separators=(",", ":"))

    result = _empty(raw_text, "conditional" if conditional else "parsed", confidence)
    result.update({
        "comp_native_min": native_min,
        "comp_native_max": native_max,
        "comp_currency": best.currency,
        "comp_period": best.period,
        "comp_hours_per_week": hours,
        "comp_hourly_native_min": hourly_native_min,
        "comp_hourly_native_max": hourly_native_max,
        "comp_hourly_cad_min": hourly_cad_min,
        "comp_hourly_cad_max": hourly_cad_max,
        "comp_hourly_cad_mid": (
            (hourly_cad_min + hourly_cad_max) / 2
            if hourly_cad_min is not None and hourly_cad_max is not None else None
        ),
        "comp_fx_rate": fx_rate,
        "comp_fx_date": FX_DATE if fx_rate is not None and best.currency != "CAD" else None,
        "comp_tiers_json": tiers,
    })
    return result


COMPENSATION_COLUMN_TYPES = {
    "comp_raw_text": "TEXT",
    "comp_native_min": "REAL",
    "comp_native_max": "REAL",
    "comp_currency": "TEXT",
    "comp_period": "TEXT",
    "comp_hours_per_week": "REAL",
    "comp_hourly_native_min": "REAL",
    "comp_hourly_native_max": "REAL",
    "comp_hourly_cad_min": "REAL",
    "comp_hourly_cad_max": "REAL",
    "comp_hourly_cad_mid": "REAL",
    "comp_fx_rate": "REAL",
    "comp_fx_date": "TEXT",
    "comp_parse_status": "TEXT",
    "comp_confidence": "TEXT",
    "comp_tiers_json": "TEXT",
    "comp_parser_version": "INTEGER",
}
COMPENSATION_COLUMNS = tuple(COMPENSATION_COLUMN_TYPES)
