"""Pricing business checks use the real Catalog and the ordinary service entry points.

These tests prove local deterministic pricing, not chat normalization, product
fit, proposal authorization or persisted quote drafts.
"""

from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from app.schemas.matching import HardRequirement, RequirementSet
from app.schemas.pricing import PricingResult, UnitPriceResult, format_cny_minor
from app.services import catalog as catalog_service
from app.services.matching import match_requirements
from app.services.pricing import calculate_subscription_price, get_unit_price


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated_catalog(tmp_path, monkeypatch):
    """Real JSON loading in isolation; never edit the accepted runtime Catalog."""
    runtime_path = catalog_service.DEFAULT_CATALOG_PATH
    runtime_bytes = runtime_path.read_bytes()
    data = json.loads(runtime_bytes.decode("utf-8"))
    test_path = tmp_path / "catalog.json"
    test_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(catalog_service, "DEFAULT_CATALOG_PATH", test_path)
    catalog_service.get_catalog.cache_clear()
    try:
        yield test_path, data
    finally:
        catalog_service.get_catalog.cache_clear()
        assert runtime_path.read_bytes() == runtime_bytes


@pytest.mark.parametrize(
    ("paid_seats", "expected_minor", "expected_formula"),
    [
        (80, 11_520_000, "120.00 元／席／月 × 80 席 × 12 月 = 115,200.00 元（订阅基价）"),
        (50, 7_200_000, "120.00 元／席／月 × 50 席 × 12 月 = 72,000.00 元（订阅基价）"),
    ],
)
def test_business_acceptance_amounts(paid_seats, expected_minor, expected_formula):
    result = calculate_subscription_price("business", paid_seats, 12)

    assert result.base_price_minor == expected_minor
    assert result.formula == expected_formula
    assert (result.unit_price_minor, result.paid_seats, result.months) == (12_000, paid_seats, 12)
    assert (result.product_id, result.plan_id, result.plan_name) == (
        "chengxing_collab", "business", "企业版 Business"
    )
    assert result.catalog_version == "chengxing-demo-v1"
    assert (result.currency, result.billing_unit, result.minor_units_per_major) == ("CNY", "seat_month", 100)
    assert (result.status, result.scope, result.fit_status) == (
        "calculated", "read_only_pricing", "not_assessed"
    )


@pytest.mark.parametrize(
    ("plan_id", "paid_seats", "months", "expected_minor"),
    [
        ("basic", 1, 1, 4_000),
        ("team", 1, 1, 8_000),
        ("business", 1, 1, 12_000),
        ("basic", 500, 12, 24_000_000),
        ("team", 500, 12, 48_000_000),
        ("business", 500, 12, 72_000_000),
    ],
)
def test_every_plan_and_both_input_boundaries(plan_id, paid_seats, months, expected_minor):
    result = calculate_subscription_price(plan_id, paid_seats, months)
    assert result.base_price_minor == expected_minor
    assert result.plan_id == plan_id


def test_parameter_changes_produce_independent_results():
    first = calculate_subscription_price("business", 80, 12)
    second = calculate_subscription_price("business", 50, 12)
    shorter = calculate_subscription_price("business", 50, 1)

    assert first.base_price_minor == 11_520_000
    assert second.base_price_minor == 7_200_000
    assert shorter.base_price_minor == 600_000
    assert first.paid_seats == 80
    assert second.months == 12
    assert first.catalog_version == second.catalog_version == shorter.catalog_version


def test_unit_price_query_requires_neither_quantity_nor_complete_matching():
    result = get_unit_price("team")
    payload = json.loads(result.model_dump_json())

    assert isinstance(result, UnitPriceResult)
    assert payload["unit_price_minor"] == 8_000
    assert payload["status"] == "unit_price_only"
    assert (payload["currency"], payload["billing_unit"], payload["minor_units_per_major"]) == (
        "CNY", "seat_month", 100
    )
    assert payload["catalog_version"] == "chengxing-demo-v1"
    assert payload["scope"] == "read_only_pricing"
    assert payload["fit_status"] == "not_assessed"
    assert not {"paid_seats", "months", "base_price_minor", "formula", "saveable", "confirmation", "quote_id"} & payload.keys()
    assert UnitPriceResult.model_validate_json(result.model_dump_json()) == result


INVALID_QUANTITIES = [
    0, -1, 1.5, 1.0, True, False, "80", "80 人左右", "一年", None,
    [], {}, Decimal("80"), float("nan"), float("inf"),
]


@pytest.mark.parametrize("value", INVALID_QUANTITIES)
@pytest.mark.parametrize("parameter", ["paid_seats", "months"])
def test_direct_service_rejects_invalid_quantities_without_coercion(parameter, value):
    inputs = {"plan_id": "business", "paid_seats": 80, "months": 12}
    inputs[parameter] = value
    with pytest.raises(ValidationError):
        calculate_subscription_price(**inputs)


@pytest.mark.parametrize(("parameter", "value"), [("paid_seats", 501), ("months", 13)])
def test_direct_service_rejects_upper_overflow_instead_of_clamping(parameter, value):
    inputs = {"plan_id": "business", "paid_seats": 80, "months": 12}
    inputs[parameter] = value
    with pytest.raises(ValidationError):
        calculate_subscription_price(**inputs)


@pytest.mark.parametrize("entry", [get_unit_price, calculate_subscription_price])
@pytest.mark.parametrize("plan_id", [True, False, 1, 1.0, None, b"business", [], {}])
def test_direct_service_rejects_plan_identity_type(entry, plan_id):
    args = (plan_id,) if entry is get_unit_price else (plan_id, 80, 12)
    with pytest.raises(ValidationError):
        entry(*args)


@pytest.mark.parametrize("entry", [get_unit_price, calculate_subscription_price])
@pytest.mark.parametrize("plan_id", ["unknown", "Business", "企业版 Business", " ", " business", "business ", ""])
def test_unknown_or_nonexact_plan_identity_is_rejected(entry, plan_id):
    args = (plan_id,) if entry is get_unit_price else (plan_id, 80, 12)
    with pytest.raises(ValueError):
        entry(*args)


@pytest.mark.parametrize(
    "inputs",
    [
        {}, {"plan_id": "team"}, {"plan_id": "team", "paid_seats": 80},
        {"plan_id": "team", "months": 12}, {"paid_seats": 80, "months": 12},
    ],
)
def test_missing_parameters_do_not_yield_assumed_totals(inputs):
    with pytest.raises(ValidationError):
        calculate_subscription_price(**inputs)
    # Even after the rejected attempt, the known rate remains answerable.
    assert get_unit_price("team").unit_price_minor == 8_000


def test_unit_price_query_also_rejects_missing_plan():
    with pytest.raises(ValidationError):
        get_unit_price()


@pytest.mark.parametrize(
    "override",
    [
        {"unit_price_minor": 1}, {"unit_price": 0.01}, {"discount": 0.99},
        {"base_price_minor": 1}, {"total": 1}, {"catalog": {}},
        {"confirmed": True},
    ],
)
def test_caller_cannot_supply_price_discount_total_catalog_or_permission(override):
    with pytest.raises(ValidationError):
        calculate_subscription_price("business", 80, 12, **override)
    with pytest.raises(ValidationError):
        get_unit_price("business", **override)
    assert calculate_subscription_price("business", 80, 12).base_price_minor == 11_520_000


def test_extra_positional_price_is_not_a_backdoor():
    with pytest.raises(ValidationError):
        calculate_subscription_price("business", 80, 12, 1)


def test_rejection_does_not_poison_later_legal_calculation():
    with pytest.raises(ValidationError):
        calculate_subscription_price("business", True, 12)
    with pytest.raises(ValueError):
        calculate_subscription_price("missing", 80, 12)
    assert calculate_subscription_price("business", 80, 12).base_price_minor == 11_520_000


def test_price_and_identity_come_from_the_real_validated_catalog(isolated_catalog):
    path, data = isolated_catalog
    data["version"] = "isolated-price-source"
    data["plans"][2]["unit_price_minor"] = 12_345
    data["plans"][2]["name"] = "隔离测试企业套餐"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    result = calculate_subscription_price("business", 3, 2)

    assert result.unit_price_minor == 12_345
    assert result.base_price_minor == 74_070
    assert result.formula == "123.45 元／席／月 × 3 席 × 2 月 = 740.70 元（订阅基价）"
    assert (result.plan_name, result.catalog_version) == ("隔离测试企业套餐", "isolated-price-source")
    assert get_unit_price("business").unit_price_minor == 12_345
    assert PricingResult.model_validate_json(result.model_dump_json()) == result


def test_one_run_keeps_its_loaded_catalog_snapshot(isolated_catalog):
    path, data = isolated_catalog
    first = calculate_subscription_price("business", 80, 12)
    loaded_catalog = catalog_service.get_catalog()
    data["version"] = "test-next-run-price-source"
    data["plans"][2]["unit_price_minor"] = 12_345
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    assert catalog_service.get_catalog() is loaded_catalog
    assert calculate_subscription_price("business", 80, 12) == first
    assert get_unit_price("business").catalog_version == "chengxing-demo-v1"
    # Clearing here simulates another isolated process loading its own version.
    catalog_service.get_catalog.cache_clear()
    assert get_unit_price("business").catalog_version == "test-next-run-price-source"
    assert first.base_price_minor == 11_520_000


def test_invalid_catalog_is_an_error_not_a_fallback_price(isolated_catalog):
    path, data = isolated_catalog
    data["plans"][2]["unit_price_minor"] = 0
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid catalog"):
        calculate_subscription_price("business", 80, 12)
    with pytest.raises(ValueError, match="Invalid catalog"):
        get_unit_price("business")
    data["plans"][2]["unit_price_minor"] = 12_000
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert calculate_subscription_price("business", 80, 12).base_price_minor == 11_520_000


def test_pricing_json_keeps_exact_integer_amount_and_all_adopted_values():
    original = calculate_subscription_price("business", 80, 12)
    payload = json.loads(original.model_dump_json())
    assert type(payload["unit_price_minor"]) is int
    assert type(payload["base_price_minor"]) is int
    assert payload["base_price_minor"] == 11_520_000
    assert payload["formula"] == "120.00 元／席／月 × 80 席 × 12 月 = 115,200.00 元（订阅基价）"
    for field in (
        "product_id", "plan_id", "plan_name", "catalog_version", "currency", "billing_unit",
        "minor_units_per_major", "unit_price_minor", "paid_seats", "months", "base_price_minor",
        "formula", "scope", "fit_status", "status",
    ):
        assert payload[field] == getattr(original, field)
    assert PricingResult.model_validate_json(json.dumps(payload)) == original


@pytest.mark.parametrize(
    "changes",
    [
        {"base_price_minor": 1}, {"paid_seats": 50}, {"months": 1}, {"unit_price_minor": 1},
        {"formula": "已折扣，最终应付款 1 元"}, {"currency": "USD"},
        {"billing_unit": "seat_year"}, {"minor_units_per_major": 1},
        {"unit_price_minor": 12_000.0}, {"base_price_minor": "11520000"},
        {"paid_seats": True}, {"months": 13}, {"catalog_version": " "},
        {"fit_status": "matched"}, {"scope": "saveable_proposal"}, {"confirmed": True},
    ],
)
def test_deserialized_result_rejects_inconsistent_or_invalid_facts(changes):
    payload = json.loads(calculate_subscription_price("business", 80, 12).model_dump_json())
    payload.update(changes)
    with pytest.raises(ValidationError):
        PricingResult.model_validate_json(json.dumps(payload))


def test_result_snapshot_is_read_only():
    result = calculate_subscription_price("business", 80, 12)
    price = get_unit_price("business")
    with pytest.raises(ValidationError):
        result.base_price_minor = 1
    with pytest.raises(ValidationError):
        price.unit_price_minor = 1
    assert result.base_price_minor == 11_520_000
    assert price.unit_price_minor == 12_000


@pytest.mark.parametrize(
    ("minor", "expected"),
    [(0, "0.00"), (1, "0.01"), (101, "1.01"), (11_520_000, "115,200.00"),
     (9_007_199_254_740_993, "90,071,992,547,409.93")],
)
def test_money_display_is_exact_including_beyond_float_integer_precision(minor, expected):
    assert format_cny_minor(minor) == expected


@pytest.mark.parametrize("minor", [-1, True, False, 12000.0, "12000", None])
def test_money_display_does_not_accept_approximate_or_invalid_inputs(minor):
    with pytest.raises(ValueError):
        format_cny_minor(minor)


@pytest.mark.parametrize("feature_id", ["private_deployment", "saml_sso", "scim_provisioning"])
def test_blocked_matching_stays_blocked_while_explicit_read_only_price_is_available(feature_id):
    requirements = RequirementSet(
        requirement_id="pricing-blocked-needs",
        items=(HardRequirement(
            requirement_id="need-blocking-feature",
            feature_id=feature_id,
            statement=f"必须支持 {feature_id}",
            user_quote=f"我们必须使用 {feature_id}",
        ),),
    )
    # Compare deterministic Catalog/pre-retrieval results. A new real
    # retrieval has its own observation ID/time and separate retrieval tests.
    before = match_requirements(requirements, retrieval_ids=())
    match_snapshot = before.model_dump_json()
    assert before.status == "blocked"
    assert before.selected_plan_id is None

    price = calculate_subscription_price("business", 80, 12)

    assert price.base_price_minor == 11_520_000
    assert price.fit_status == "not_assessed"
    assert price.scope == "read_only_pricing"
    assert not {"saveable", "confirmation", "quote_id"} & price.model_dump().keys()
    assert before.model_dump_json() == match_snapshot
    assert match_requirements(requirements, retrieval_ids=()) == before
    assert price.catalog_version == before.catalog_version


def test_ordinary_pricing_runs_without_database_network_or_model_and_does_not_write(tmp_path):
    # A new interpreter avoids passing because an earlier test preloaded services.
    script = """
import builtins
import io
import json
import socket
import sqlite3
import sys

def forbidden(*args, **kwargs):
    raise AssertionError('Read-only pricing attempted database or network access')

sqlite3.connect = forbidden
socket.create_connection = forbidden
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden

def protect_writes(real_open):
    def read_only_open(file, mode='r', *args, **kwargs):
        if any(flag in mode for flag in 'wax+'):
            raise AssertionError('Read-only pricing attempted a file write')
        return real_open(file, mode, *args, **kwargs)
    return read_only_open

builtins.open = protect_writes(builtins.open)
io.open = protect_writes(io.open)
from app.services.pricing import calculate_subscription_price, get_unit_price

assert get_unit_price('team').unit_price_minor == 8000
result = calculate_subscription_price('business', 80, 12)
assert result.base_price_minor == 11520000
assert not any(name in ('groq', 'openai')
               or name.startswith(('groq.', 'openai.', 'app.db.', 'app.agents.', 'app.llm.'))
               for name in sys.modules)
print(json.dumps({'base_price_minor': result.base_price_minor, 'scope': result.scope}))
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, env=environment,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout) == {"base_price_minor": 11_520_000, "scope": "read_only_pricing"}
    assert list(tmp_path.iterdir()) == []
