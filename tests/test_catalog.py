import copy
import json

import pytest
from pydantic import ValidationError

from app.schemas.catalog import Catalog
from app.services.catalog import DEFAULT_CATALOG_PATH, get_catalog, load_catalog, validate_catalog


@pytest.fixture
def catalog_data():
    return json.loads(DEFAULT_CATALOG_PATH.read_text(encoding="utf-8"))


def write_catalog(tmp_path, data):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_frozen_business_baseline():
    catalog = load_catalog(DEFAULT_CATALOG_PATH)
    assert catalog.version == "chengxing-demo-v1"
    assert (catalog.product.product_id, catalog.product.name) == ("chengxing_collab", "澄星协作")
    assert (catalog.currency, catalog.billing_unit, catalog.minor_units_per_major) == ("CNY", "seat_month", 100)
    assert [(plan.plan_id, plan.name, plan.rank, plan.unit_price_minor) for plan in catalog.plans] == [
        ("basic", "基础版 Basic", 1, 4000),
        ("team", "团队版 Team", 2, 8000),
        ("business", "企业版 Business", 3, 12000),
    ]
    assert {feature.feature_id: feature.name for feature in catalog.features} == {
        "project_collaboration": "任务与项目协作",
        "basic_roles": "基础成员角色",
        "data_export": "数据导出",
        "api_access": "产品 API",
        "webhook_notifications": "Webhook 通知",
        "saml_sso": "SAML SSO",
        "audit_logs": "审计日志",
        "private_deployment": "私有化部署",
    }
    expected_support = {
        "basic": {"project_collaboration", "basic_roles", "data_export"},
        "team": {"project_collaboration", "basic_roles", "data_export", "api_access", "webhook_notifications"},
        "business": {
            "project_collaboration", "basic_roles", "data_export", "api_access", "webhook_notifications",
            "saml_sso", "audit_logs",
        },
    }
    for plan in catalog.plans:
        assert {entry.feature_id for entry in plan.features if entry.supported} == expected_support[plan.plan_id]
    condition = catalog.conditions[0]
    assert (condition.condition_id, condition.feature_id, condition.name) == (
        "saml_sso_compatible_idp", "saml_sso", "兼容身份提供方前提",
    )
    assert condition.statement == "使用澄星协作的 SAML SSO 时，客户须明确确认已具备兼容的身份提供方。"
    assert [
        (plan.plan_id, entry.feature_id, entry.condition_id)
        for plan in catalog.plans for entry in plan.features if entry.condition_id is not None
    ] == [("business", "saml_sso", "saml_sso_compatible_idp")]


def test_catalog_is_cached_and_recursively_read_only():
    catalog = get_catalog()
    assert get_catalog() is catalog
    assert isinstance(catalog.plans, tuple)
    assert isinstance(catalog.plans[0].features, tuple)
    with pytest.raises(ValidationError):
        catalog.version = "changed"
    with pytest.raises(ValidationError):
        catalog.plans[0].unit_price_minor = 1
    with pytest.raises(ValidationError):
        catalog.plans[0].features[0].supported = False
    with pytest.raises(TypeError):
        catalog.plans[0] = catalog.plans[1]
    assert validate_catalog(catalog) == catalog


def mutate(data, case):
    if case == "one_plan":
        data["plans"] = data["plans"][:1]
    elif case == "four_plans":
        data["plans"].append(copy.deepcopy(data["plans"][-1]))
    elif case == "too_few_features":
        data["features"] = data["features"][:5]
    elif case == "too_many_features":
        data["features"].extend({"feature_id": f"extra_{n}", "name": "extra"} for n in range(3))
    elif case == "missing_condition":
        data["conditions"] = []
    elif case == "second_condition":
        data["conditions"].append(copy.deepcopy(data["conditions"][0]))
    elif case == "duplicate_plan":
        data["plans"][1]["plan_id"] = data["plans"][0]["plan_id"]
    elif case == "duplicate_feature":
        data["features"][1]["feature_id"] = data["features"][0]["feature_id"]
    elif case == "cross_type_duplicate":
        data["product"]["product_id"] = data["plans"][0]["plan_id"]
    elif case == "blank_identity":
        data["product"]["product_id"] = "   "
    elif case == "identity_whitespace":
        data["product"]["product_id"] = " chengxing_collab"
    elif case == "unknown_condition_feature":
        data["conditions"][0]["feature_id"] = "missing"
    elif case == "condition_on_non_sso":
        data["conditions"][0]["feature_id"] = "api_access"
    elif case == "rank_gap":
        data["plans"][1]["rank"] = 4
    elif case == "rank_wrong_order":
        data["plans"][0], data["plans"][1] = data["plans"][1], data["plans"][0]
    elif case == "duplicate_plan_feature":
        data["plans"][0]["features"][1]["feature_id"] = data["plans"][0]["features"][0]["feature_id"]
    elif case == "missing_plan_feature":
        data["plans"][0]["features"].pop()
    elif case == "unknown_plan_feature":
        data["plans"][0]["features"][0]["feature_id"] = "missing"
    elif case == "capability_removed_in_higher_plan":
        data["plans"][1]["features"][0]["supported"] = False
    elif case == "identical_capabilities":
        data["plans"][1]["features"] = copy.deepcopy(data["plans"][0]["features"])
    elif case == "zero_price":
        data["plans"][0]["unit_price_minor"] = 0
    elif case == "negative_price":
        data["plans"][0]["unit_price_minor"] = -1
    elif case == "equal_price":
        data["plans"][1]["unit_price_minor"] = data["plans"][0]["unit_price_minor"]
    elif case == "price_decreases":
        data["plans"][1]["unit_price_minor"] = 3000
    elif case == "missing_sso_condition":
        data["plans"][2]["features"][5].pop("condition_id")
    elif case == "unknown_condition_reference":
        data["plans"][2]["features"][5]["condition_id"] = "missing"
    elif case == "condition_on_wrong_feature":
        data["plans"][2]["features"][0]["condition_id"] = "saml_sso_compatible_idp"
    elif case == "condition_on_unsupported_sso":
        data["plans"][0]["features"][5]["condition_id"] = "saml_sso_compatible_idp"
    elif case == "unused_condition":
        data["plans"][2]["features"][5] = {"feature_id": "saml_sso", "supported": False}
    elif case == "boolean_price":
        data["plans"][0]["unit_price_minor"] = True
    elif case == "decimal_price":
        data["plans"][0]["unit_price_minor"] = 4000.5
    elif case == "string_price":
        data["plans"][0]["unit_price_minor"] = "4000"
    elif case == "boolean_rank":
        data["plans"][0]["rank"] = True
    elif case == "string_support":
        data["plans"][0]["features"][0]["supported"] = "true"
    elif case == "wrong_currency":
        data["currency"] = "USD"
    elif case == "wrong_billing_unit":
        data["billing_unit"] = "month"
    elif case == "wrong_minor_unit":
        data["minor_units_per_major"] = 1
    elif case == "unknown_field":
        data["discount"] = 0.5
    else:
        raise AssertionError(f"Unhandled test case: {case}")


@pytest.mark.parametrize("case", [
    "one_plan", "four_plans", "too_few_features", "too_many_features", "missing_condition", "second_condition",
    "duplicate_plan", "duplicate_feature", "cross_type_duplicate", "blank_identity", "identity_whitespace",
    "unknown_condition_feature", "condition_on_non_sso", "rank_gap", "rank_wrong_order", "duplicate_plan_feature",
    "missing_plan_feature", "unknown_plan_feature", "capability_removed_in_higher_plan", "identical_capabilities",
    "zero_price", "negative_price", "equal_price", "price_decreases", "missing_sso_condition",
    "unknown_condition_reference", "condition_on_wrong_feature", "condition_on_unsupported_sso", "unused_condition",
    "boolean_price", "decimal_price", "string_price", "boolean_rank", "string_support", "wrong_currency",
    "wrong_billing_unit", "wrong_minor_unit", "unknown_field",
])
def test_invalid_catalog_rejected_without_repairs(tmp_path, catalog_data, case):
    mutate(catalog_data, case)
    path = write_catalog(tmp_path, catalog_data)
    original = path.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        load_catalog(path)
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("contents", ["{broken JSON", "{}", "null", "[]"])
def test_malformed_or_wrong_shape_catalog_file(tmp_path, contents):
    path = tmp_path / "bad.json"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid catalog file"):
        load_catalog(path)


def test_missing_catalog_file_is_explicit_error(tmp_path):
    with pytest.raises(ValueError, match="Cannot read catalog file"):
        load_catalog(tmp_path / "missing.json")


def test_unreadable_encoding_is_explicit_error(tmp_path):
    path = tmp_path / "bad_encoding.json"
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(ValueError, match="Cannot read catalog file"):
        load_catalog(path)


def test_load_catalog_accepts_string_path(tmp_path, catalog_data):
    catalog = load_catalog(str(write_catalog(tmp_path, catalog_data)))
    assert isinstance(catalog, Catalog)


def test_valid_two_plan_catalog_is_not_forced_to_three(tmp_path, catalog_data):
    catalog_data["plans"] = catalog_data["plans"][1:]
    for rank, plan in enumerate(catalog_data["plans"], start=1):
        plan["rank"] = rank
    catalog = load_catalog(write_catalog(tmp_path, catalog_data))
    assert [plan.plan_id for plan in catalog.plans] == ["team", "business"]


def test_validate_catalog_rejects_non_catalog():
    with pytest.raises(ValueError, match="requires a Catalog object"):
        validate_catalog({})


def test_validate_catalog_rechecks_unvalidated_model_copy():
    catalog = get_catalog()
    forged_plan = catalog.plans[0].model_copy(update={"unit_price_minor": True})
    forged_catalog = catalog.model_copy(update={"plans": (forged_plan, *catalog.plans[1:])})
    with pytest.raises(ValueError):
        validate_catalog(forged_catalog)


def test_validate_catalog_rechecks_unvalidated_model_construct():
    catalog = get_catalog()
    forged_catalog = Catalog.model_construct(**{**catalog.model_dump(), "currency": "USD"})
    with pytest.raises(ValueError):
        validate_catalog(forged_catalog)
