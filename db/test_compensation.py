import json
import sqlite3

import pytest

from db.compensation import FX_DATE, normalize_compensation
from db.ingest import build_params, init_db, upsert_posting


def raw(text: str, country: str = "Canada", **fields) -> str:
    return json.dumps({
        "Compensation and Benefits": text,
        "Job - Country": country,
        **fields,
    })


def test_hourly_range_keeps_native_bounds():
    result = normalize_compensation(raw("$25-$30/hr"))
    assert result["comp_native_min"] == 25
    assert result["comp_native_max"] == 30
    assert result["comp_period"] == "hour"
    assert result["comp_hourly_cad_mid"] == 27.5


def test_annual_currency_after_amount_and_explicit_hours():
    result = normalize_compensation(raw("$90,000 (CAD) per year, 37.5 hours per week"))
    assert result["comp_currency"] == "CAD"
    assert result["comp_hours_per_week"] == 37.5
    assert result["comp_hourly_cad_mid"] == pytest.approx(90_000 / (37.5 * 52))


def test_country_infers_native_currency_and_uses_dated_fx_snapshot():
    result = normalize_compensation(raw("Base salary is $300,000.", "United States"))
    assert result["comp_currency"] == "USD"
    assert result["comp_period"] == "year"
    assert result["comp_fx_date"] == FX_DATE
    assert result["comp_hourly_native_min"] == pytest.approx(300_000 / 2080)
    assert result["comp_hourly_cad_mid"] == pytest.approx(300_000 / 2080 * 1.384)


def test_k_range_for_term_is_not_mistaken_for_hourly():
    result = normalize_compensation(raw("The numerical range would be $12-16K for the term."))
    assert result["comp_native_min"] == 12_000
    assert result["comp_native_max"] == 16_000
    assert result["comp_period"] == "term"
    assert result["comp_hourly_cad_mid"] == pytest.approx(14_000 / (40 * 52 / 3))


def test_base_salary_beats_housing_allowance():
    result = normalize_compensation(raw(
        "Base salary of USD 9,000. A housing allowance of up to USD $1,500/mo.",
        "United States",
    ))
    assert result["comp_native_min"] == 9_000
    assert result["comp_period"] == "month"
    assert result["comp_currency"] == "USD"


def test_conditional_schedule_preserves_full_range_and_tiers():
    result = normalize_compensation(raw(
        "Students in second year Pharmacy $24.00 per hour. "
        "Students in third year Pharmacy $26.00 per hour. "
        "Students in fourth year Pharmacy $28.00 per hour."
    ))
    assert result["comp_native_min"] == 24
    assert result["comp_native_max"] == 28
    assert result["comp_parse_status"] == "conditional"
    assert len(json.loads(result["comp_tiers_json"])) == 3


def test_compact_tier_schedule_without_units_is_inferred_cautiously():
    result = normalize_compensation(raw(
        "2nd year student: $23.55 3rd year student: $24.55 4th year student: $25.55"
    ))
    assert result["comp_native_min"] == 23.55
    assert result["comp_native_max"] == 25.55
    assert result["comp_parse_status"] == "conditional"
    assert result["comp_confidence"] == "low"


def test_explicit_pay_without_unit_is_low_confidence_hourly():
    result = normalize_compensation(raw("Our co-op positions are paid at $21.00 CAD"))
    assert result["comp_hourly_cad_mid"] == 21
    assert result["comp_confidence"] == "low"


def test_benefit_amount_is_not_treated_as_base_pay():
    result = normalize_compensation(raw(
        "Base pay is competitive. A $200 wellness benefit per work term is provided."
    ))
    assert result["comp_hourly_cad_mid"] is None


def test_foreign_monthly_formats_and_decimal_comma():
    taiwan = normalize_compensation(raw("Salary: NT$35,000 (for each month)", "."))
    germany = normalize_compensation(raw("Salary: 992,00 € monthly", "Germany"))
    assert taiwan["comp_currency"] == "TWD"
    assert taiwan["comp_period"] == "month"
    assert germany["comp_native_min"] == 992
    assert germany["comp_currency"] == "EUR"


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("Pay follows the Government of Canada Student Rates of Pay.", "reference_only"),
        ("Competitive salary, to be discussed during the interview.", "not_disclosed"),
    ],
)
def test_non_numeric_statuses_are_distinct(text, status):
    assert normalize_compensation(raw(text))["comp_parse_status"] == status


def test_ingest_persists_normalized_compensation():
    record = {
        "job_id": "example",
        "raw_fields_json": raw("$24-$28/hour"),
    }
    with sqlite3.connect(":memory:") as conn:
        init_db(conn)
        assert upsert_posting(conn, build_params(record)) == "inserted"
        row = conn.execute(
            "SELECT comp_native_min, comp_native_max, comp_hourly_cad_mid, comp_parse_status "
            "FROM postings WHERE job_id = 'example'"
        ).fetchone()
    assert row == (24, 28, 26, "parsed")
