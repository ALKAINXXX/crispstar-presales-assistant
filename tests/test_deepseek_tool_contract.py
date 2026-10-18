"""Finite transport checks; no real provider or business acceptance is claimed."""
from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from app.llm.deepseek_tool_contract import (
    NULL_KEY, ToolContract, ToolContractError, decode_arguments, encode_arguments, project_tools,
)
from app.llm.semantic_verifier import _CandidateEnvelope, validate_first_candidate
from app.schemas.business import RequirementPatch, SourceSpan
from app.schemas.semantic import SemanticReview
from app.services.pricing import get_unit_price
from app.tools.business import BusinessToolRegistry, PriceArguments, SearchArguments


def tool(name, schema):
    return {"type": "function", "function": {"name": name, "parameters": schema}}


def source_tools():
    tools = [tool(name, arguments.model_json_schema()) for name, arguments in BusinessToolRegistry._arguments.items()]
    review = SemanticReview.model_json_schema()
    review["required"] = list(dict.fromkeys((*review.get("required", ()),
        "consultations", "consultation_coverage_complete")))
    return [*tools, tool("adjudicate_turn", review)]


@pytest.fixture
def contract():
    return ToolContract(source_tools())


def cycle(contract, name, original):
    encoded = contract.encode_arguments(name, original)
    decoded = json.loads(contract.decode_arguments(name, encoded))
    assert decoded == original
    return json.loads(encoded), decoded


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def test_all_seven_tools_and_adjudication_project_without_mutating_source():
    sources = source_tools()
    before = deepcopy(sources)
    compiled = ToolContract(sources)
    assert len(compiled.tools) == 8
    for definition in compiled.tools:
        assert definition["function"]["strict"] is True
        for node in walk(definition["function"]["parameters"]):
            assert not {"$defs", "$ref", "minLength", "maxLength", "minItems", "maxItems", "default", "title"} & node.keys()
            assert node.get("type") != "null"
            if node.get("type") == "object":
                assert set(node["required"]) == set(node["properties"])
                assert node["additionalProperties"] is False
    exposed = compiled.tools
    exposed[0]["function"]["parameters"]["properties"].clear()
    assert compiled.tools[0]["function"]["parameters"]["properties"]
    assert sources == before and source_tools() == before
    assert project_tools(sources) == compiled.tools


@pytest.mark.parametrize("name,original", [
    ("update_requirements", {"intent": "knowledge"}),
    ("search_knowledge", {"query": "Business的SSO是什么？"}),
    ("query_catalog", {}),
    ("match_requirements", {}),
    ("calculate_price", {"plan_id": "team", "paid_seats": 50, "months": 6}),
    ("explain_knowledge", {"retrieval_id": "actual-retrieval", "citations": [], "answerability": "insufficient"}),
    ("query_quote", {"quote_id": "Q-" + "A" * 32}),
    ("adjudicate_turn", {"intent": "knowledge", "coverage_complete": True,
        "consultations": [], "consultation_coverage_complete": True}),
])
def test_each_registered_contract_round_trips_declared_arguments(contract, name, original):
    wire, decoded = cycle(contract, name, original)
    assert json.loads(encode_arguments(name, original, source_tools())) == wire
    assert json.loads(decode_arguments(name, wire, source_tools())) == decoded
    model = SemanticReview if name == "adjudicate_turn" else BusinessToolRegistry._arguments[name]
    model.model_validate_json(json.dumps(decoded))


def test_presence_preserves_missing_null_false_empty_and_literal_marker_text(contract):
    original = {"intent": "knowledge", "selected_plan_id": None, "context_quotes": [],
        "consultations": [{"item_id": "detail", "aspect": "detail", "topic": "null",
            "all_plans": False, "plan_ids": [], "feature_id": None,
            "span": {"quote": '原话“null”以及 {"$wire_null":"null"} 不作标记', "start": None}}]}
    wire, decoded = cycle(contract, "update_requirements", original)
    assert wire["selected_plan_id"] == {"state": "provided", "value": {NULL_KEY: "null"}}
    assert wire["selection_quote"] == {"state": "omitted"}
    assert wire["context_quotes"] == {"state": "provided", "value": []}
    consultation = wire["consultations"]["value"][0]
    assert consultation["all_plans"] == {"state": "provided", "value": False}
    assert consultation["plan_ids"] == {"state": "provided", "value": []}
    assert consultation["span"]["start"] == {"state": "provided", "value": {NULL_KEY: "null"}}
    assert consultation["span"]["end"] == {"state": "omitted"}
    assert "selection_quote" not in decoded and "end" not in decoded["consultations"][0]["span"]
    assert decoded["selected_plan_id"] is None and decoded["consultations"][0]["all_plans"] is False
    assert decoded["consultations"][0]["topic"] == "null"
    validate_first_candidate(decoded)


def test_empty_scope_expression_and_nested_spans_survive_transport(contract):
    message = "所有套餐的单价都查，但排除Basic、Team和Business。"
    original = {"intent": "unit_price", "coverage_complete": True, "consultation_coverage_complete": True,
        "consultations": [{"item_id": "empty", "aspect": "unit_price", "topic": "单价", "plan_ids": [],
            "all_plans": False, "span": {"quote": message},
            "scope_basis": {"kind": "all_plans", "span": {"quote": "所有套餐"}},
            "scope_exclusions": [{"quote": "排除Basic、Team和Business"}]}]}
    _, decoded = cycle(contract, "adjudicate_turn", original)
    accepted = SemanticReview.model_validate_json(json.dumps(decoded, ensure_ascii=False))
    assert accepted.consultations[0].plan_ids == () and accepted.consultations[0].scope_basis.kind == "all_plans"
    assert accepted.consultations[0].scope_exclusions[0].quote == "排除Basic、Team和Business"
    # Structurally valid expression remains subject to normal source/scope acceptance.
    assert decoded == original


@pytest.mark.parametrize("number", [50, 50.0, 50.5])
def test_candidate_numbers_keep_their_type_without_repairing_illegal_purchase(number, contract):
    original = {"intent": "selection", "parameters": [{"name": "paid_seats", "value": number, "user_quote": str(number)}]}
    _, decoded = cycle(contract, "update_requirements", original)
    assert type(decoded["parameters"][0]["value"]) is type(number)
    validate_first_candidate(decoded)
    if type(number) is float:
        with pytest.raises(ValidationError):
            RequirementPatch.model_validate_json(json.dumps(decoded))
    provided_source = [tool("update_requirements", _CandidateEnvelope.model_json_schema())]
    explicit = ToolContract(provided_source, candidate_update=False)
    assert json.loads(explicit.decode_arguments("update_requirements", explicit.encode_arguments("update_requirements", original))) == original


def test_required_dispatch_fields_and_caller_guidance_remain_required():
    sources = source_tools()
    update = sources[0]["function"]["parameters"]
    update["required"].append("consultations")
    contract = ToolContract(sources)
    wire = json.loads(contract.encode_arguments("update_requirements", {"intent": "knowledge", "consultations": []}))
    assert wire["consultations"] == []
    with pytest.raises(ToolContractError):
        contract.encode_arguments("update_requirements", {"intent": "knowledge"})
    review_wire = json.loads(contract.encode_arguments("adjudicate_turn", {
        "intent": "knowledge", "coverage_complete": True, "consultations": [], "consultation_coverage_complete": False}))
    assert review_wire["consultation_coverage_complete"] is False
    for name in ("consultations", "consultation_coverage_complete"):
        invalid = deepcopy(review_wire)
        invalid[name] = {"state": "omitted"}
        with pytest.raises(ToolContractError):
            contract.decode_arguments("adjudicate_turn", invalid)


@pytest.mark.parametrize("invalid", [
    {"state": "unknown"}, {"state": "provided"}, {"state": "omitted", "value": []},
    {"state": "omitted", "extra": 1}, {"state": "provided", "value": None},
    {"state": "provided", "value": {NULL_KEY: "wrong"}},
    {"state": "provided", "value": {NULL_KEY: "null", "extra": 1}},
    {"state": "provided", "value": {NULL_KEY: True}}, [], None,
])
def test_malformed_presence_and_null_are_never_normalized(contract, invalid):
    wire = json.loads(contract.encode_arguments("query_catalog", {}))
    wire["plan_id"] = invalid
    with pytest.raises(ToolContractError):
        contract.decode_arguments("query_catalog", wire)


@pytest.mark.parametrize("name,value", [("calculate_price", True), ("calculate_price", 50.0),
    ("calculate_price", None), ("calculate_price", {NULL_KEY: "null"})])
def test_required_integer_never_coerces_or_accepts_nullable_marker(contract, name, value):
    raw = {"plan_id": "team", "paid_seats": value, "months": 6}
    for operation in (contract.encode_arguments, contract.decode_arguments):
        with pytest.raises(ToolContractError):
            operation(name, raw)


@pytest.mark.parametrize("raw", [
    '{"plan_id":"team","paid_seats":50,}',
    '{"plan_id":"team","paid_seats":50,"paid_seats":60,"months":6}',
    '{"plan_id":"team","paid_seats":NaN,"months":6}',
    '{"plan_id":"team","paid_seats":1e999,"months":6}',
    '[]', 'null', '', None,
    {"plan_id": "team", "paid_seats": 50, "months": 6, "unknown": True},
    {"plan_id": "team", "paid_seats": 50},
    {"plan_id": "team", "paid_seats": 50, "months": (6,)},
])
def test_invalid_json_keys_and_required_values_fail_closed(contract, raw):
    for operation in (contract.encode_arguments, contract.decode_arguments):
        with pytest.raises(ToolContractError):
            operation("calculate_price", raw)


@pytest.mark.parametrize("kind", ["unknown_keyword", "open_object", "no_extra_policy", "external_ref",
    "missing_ref", "cyclic_ref", "constrained_ref", "reserved_null", "untyped", "invalid_required", "nested_unknown"])
def test_unknown_or_unclosed_source_schema_is_rejected(kind):
    schema = {"type": "object", "properties": {"query": {"type": "string"}},
        "required": ["query"], "additionalProperties": False}
    if kind == "unknown_keyword": schema["unevaluatedProperties"] = False
    if kind == "open_object": schema["additionalProperties"] = True
    if kind == "no_extra_policy": del schema["additionalProperties"]
    if kind == "external_ref": schema["properties"]["query"] = {"$ref": "https://example.invalid/schema"}
    if kind == "missing_ref": schema["properties"]["query"] = {"$ref": "#/$defs/Missing"}
    if kind == "cyclic_ref":
        schema["$defs"] = {"Loop": {"$ref": "#/$defs/Loop"}}
    if kind == "constrained_ref":
        schema["$defs"] = {"Text": {"type": "string"}}
        schema["properties"]["query"] = {"$ref": "#/$defs/Text", "minLength": 1}
    if kind == "reserved_null": schema["properties"][NULL_KEY] = {"type": "string"}
    if kind == "untyped": schema["properties"]["query"] = {}
    if kind == "invalid_required": schema["required"].append("other")
    if kind == "nested_unknown": schema["properties"]["query"]["format"] = "unknown-format"
    with pytest.raises(ToolContractError):
        ToolContract([tool("search_knowledge", schema)])


def test_unsupported_lengths_still_reject_at_the_original_input_gate(contract):
    for raw in ({"query": ""}, {"query": "x" * 2001},
        {"query": "API", "consultation_item_ids": ["item"] * 25}):
        _, decoded = cycle(contract, "search_knowledge", raw)
        with pytest.raises(ValidationError):
            SearchArguments.model_validate_json(json.dumps(decoded))
    raw = {"quote": "x" * 4001}
    original = {"intent": "knowledge", "consultations": [{"item_id": "detail", "aspect": "detail",
        "topic": "配置", "span": raw}]}
    _, decoded = cycle(contract, "update_requirements", original)
    with pytest.raises(ValidationError):
        SourceSpan.model_validate_json(json.dumps(decoded["consultations"][0]["span"]))


def test_price_ranges_remain_in_the_projection_and_original_gate(contract):
    projected = next(t for t in contract.tools if t["function"]["name"] == "calculate_price")
    assert projected["function"]["parameters"]["properties"]["paid_seats"]["maximum"] == 500
    raw = {"plan_id": "team", "paid_seats": 501, "months": 6}
    with pytest.raises(ToolContractError):
        contract.decode_arguments("calculate_price", raw)
    with pytest.raises(ValidationError):
        PriceArguments.model_validate_json(json.dumps(raw))


def test_transport_does_not_make_an_unknown_product_identity_valid(contract):
    original = {"plan_id": "not-a-catalog-plan", "paid_seats": 50, "months": 6}
    _, decoded = cycle(contract, "calculate_price", original)
    PriceArguments.model_validate_json(json.dumps(decoded))
    with pytest.raises(ValueError):
        get_unit_price(decoded["plan_id"])


def test_unknown_function_and_bare_legacy_arguments_cannot_enter_wire_decode(contract):
    for name in ("save_quote", "new_tool", None):
        with pytest.raises(ToolContractError):
            contract.encode_arguments(name, {})
    with pytest.raises(ToolContractError):
        contract.decode_arguments("query_catalog", {})
    with pytest.raises(ToolContractError):
        contract.decode_arguments("search_knowledge", {"query": "API", "consultation_item_ids": []})
    invalid = source_tools()
    invalid[0]["function"]["name"] = "save_quote"
    with pytest.raises(ToolContractError):
        ToolContract(invalid)


@pytest.mark.parametrize("verdict", ["reject", "unresolved", "accept"])
def test_original_conditional_rejection_alias_still_belongs_to_server_validator(contract, verdict):
    value = {"intent": "selection", "coverage_complete": True, "consultations": [],
        "consultation_coverage_complete": True, "groups": [{"group_id": "g", "verdict": verdict,
            "reason": "hypothesis", "operations": [{"action": "set", "field": "paid_seats", "value": 50,
                "span": {"quote": "如果买50席"}, "speech_act": "hypothesis"}]}]}
    _, decoded = cycle(contract, "adjudicate_turn", value)
    assert decoded["groups"][0]["reason"] == "hypothesis"
    if verdict == "accept":
        with pytest.raises(ValidationError):
            SemanticReview.model_validate_json(json.dumps(decoded))
    else:
        parsed = SemanticReview.model_validate_json(json.dumps(decoded))
        assert parsed.groups[0].reason == "conditional" and parsed.groups[0].verdict == verdict
