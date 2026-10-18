"""Controlled chat/review, ordinary catalog and native indexed knowledge tools.

Success cases run the real local embedding/retrieval worker and validate its
actual source IDs and continuous quotes. Failure injection is labelled below.
All messages use private tmp_path SQLite; no configured user database is opened.
"""
import asyncio
from copy import deepcopy
import json
import subprocess

import pytest

from app.agents import orchestrator as module
from app.llm.tool_protocol import validate_tool_message_sequence
from app.schemas.chat import ChatRequest
from app.services.retrieval import KnowledgeService
from controlled_review import is_review_request, review_completion
from test_presales_orchestrator import call, completion, engine, review, same_history
from test_consultation_scope_convergence import item
from test_intake_convergence_orchestrator import run_prices
from test_consultation_anchor_scope import SSO_CONTEXT, MULTI, FEATURE_FIRST, LOCAL_TOPIC


def knowledge_provider(monkeypatch, items, *, invalid_explanation=None, insufficient_ids=(),
                       skip_explanations=(), late_search=None, review_intent="knowledge", reviewed_target=None,
                       extra_unit=None, extra_match=False, early_match=False, reviewed_groups=(), early_execution=None):
    class Provider:
        sent, reviews = [], []

        def __init__(self, settings):
            pass

        async def chat(self, **kwargs):
            validate_tool_message_sequence(kwargs["messages"])
            if is_review_request(kwargs):
                type(self).reviews.append(deepcopy(kwargs))
                assert len(type(self).reviews) == 1
                return review_completion(review(items, intent=review_intent, target=reviewed_target, groups=reviewed_groups))
            type(self).sent.append(deepcopy(kwargs))
            type(self).live_messages = kwargs["messages"]
            phase = len(type(self).sent)
            if phase == 1:
                calls = [call("update_requirements", {"intent": review_intent, "consultations": items}, "candidate"),
                    call("query_catalog", {}, "actual-catalog")]
                if extra_unit:
                    calls.append(call("query_catalog", {"plan_id": extra_unit}, "actual-unit"))
                return completion(*calls)
            if phase == 2:
                calls = [call("search_knowledge", {
                    "query": "SAML SSO 含义 使用前提 客户明确确认兼容身份提供方" if i.get("feature_id") == "saml_sso"
                        else "澄星协作 产品 API Webhook 通知 Team Basic 支持",
                    "consultation_item_ids": [i["item_id"]]}, "search-" + i["item_id"])
                    for i in items if i["item_id"] != late_search and i["aspect"] != "unit_price"]
                leading = []
                if early_execution:
                    args = {"plan_id": "team", "paid_seats": 80, "months": 12} if early_execution == "calculate_price" else {}
                    leading.append(call(early_execution, args, "early-execution"))
                elif early_match:
                    leading.append(call("match_requirements", {}, "early-match"))
                return completion(*leading, *calls)
            if phase == 3:
                actual = {}
                for m in kwargs["messages"]:
                    if m["role"] == "tool" and m.get("tool_call_id", "").startswith("search-"):
                        result = json.loads(m["content"])
                        if result.get("ok") and result.get("data", {}).get("status") == "retrieved":
                            actual[m["tool_call_id"][7:]] = result["data"]
                calls = []
                # Preserve the ordinary fail-stop contract. Obtain the other
                # independent answer before injecting an explanation failure.
                ordered = sorted(items, key=lambda i: i["item_id"] == invalid_explanation)
                for i in ordered:
                    if i["item_id"] in skip_explanations:
                        continue
                    data = actual.get(i["item_id"])
                    if data is None:
                        continue
                    sources = [h["source"] for h in data["hits"] if h["relevance"] == "related"]
                    if i.get("feature_id") == "saml_sso":
                        chosen = next((s for s in sources if s["section_id"] == "sso_concept"), None) \
                            if i["aspect"] == "meaning" else next((s for s in sources
                                if "saml_sso_compatible_idp" in s["condition_ids"]), None)
                    else:
                        chosen = next((s for s in sources if i.get("feature_id") in s["feature_ids"]), None)
                    assert chosen is not None, "Actual retrieval lacks the required current source"
                    quote = "受控的无效引文" if i["item_id"] == invalid_explanation else chosen["raw_text"]
                    calls.append(call("explain_knowledge", {"retrieval_id": data["retrieval_id"],
                        "answerability": "insufficient" if i["item_id"] in insufficient_ids else "supported",
                        "consultation_item_ids": [i["item_id"]],
                        "citations": [] if i["item_id"] in insufficient_ids else [
                            {"source_id": chosen["source_id"], "quote": quote}]}, "explain-" + i["item_id"]))
                if extra_match:
                    calls.append(call("match_requirements", {}, "unrequested-match"))
                return completion(*calls)
            if phase == 4 and late_search:
                return completion(call("search_knowledge", {
                    "query": "SAML SSO 含义 使用前提 客户明确确认兼容身份提供方",
                    "consultation_item_ids": [late_search]}, "search-" + late_search))
            assert phase <= 5
            return completion()
    monkeypatch.setattr(module, "DeepSeekClient", Provider)
    return Provider


def run_knowledge(engine, monkeypatch, message, items, **options):
    provider = knowledge_provider(monkeypatch, items, **options)
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    assert len(provider.reviews) == 1
    same_history(engine, response, message)
    assert response.business.proposal is None
    assert not response.business.requirements.items and not response.business.requirements.parameters
    assert not response.business.requirements.condition_assertions
    return response


def assert_knowledge_complete(response, expected):
    assert response.status == "completed", response.message
    coverage = response.business.consultation_coverage
    assert coverage.scope_complete and coverage.status == "completed"
    assert [(x.aspect, tuple(x.plan_ids), x.feature_id) for x in coverage.items] == expected
    assert all(x.retrieval_ids and x.source_ids and x.stage == "answered" for x in coverage.items)
    records = response.tool_results
    searches = [r["result"]["data"] for r in records if r["name"] == "search_knowledge"]
    assert all(s["status"] == "retrieved" and s["scope"] == "formal" and s["index_build_id"] for s in searches)
    assert all(r["result"]["ok"] for r in records)
    assert response.business.presentation.used_source_ids
    assert response.business.pricing is None and response.business.current_task is None


@pytest.mark.parametrize("basis_kind", [None, "named"])
def test_original_Q3_background_inquiry_runs_actual_bound_prerequisite_chain(engine, monkeypatch, basis_kind):
    basis = {"scope_basis": {"kind": basis_kind,
        "span": {"quote": "我们正在了解企业版 Business 的 SAML SSO"}}} if basis_kind else {}
    response = run_knowledge(engine, monkeypatch, SSO_CONTEXT, [item("rule", SSO_CONTEXT, ("business",),
        aspect="prerequisite", feature="saml_sso", **basis)])
    assert_knowledge_complete(response, [("prerequisite", ("business",), "saml_sso")])
    assert "兼容的身份提供方" in response.message
    assert len(response.business.consultation_coverage.items[0].prerequisites) == 1


@pytest.mark.parametrize("omit_meaning", [False, True])
def test_original_multi_aspect_never_uses_one_quote_to_mask_missing_obligation(engine, monkeypatch, omit_meaning):
    items = ([] if omit_meaning else [item("meaning", "Business的SSO是什么", ("business",),
        aspect="meaning", feature="saml_sso")]) + [item("rule", "有什么使用前提", ("business",),
        aspect="prerequisite", feature="saml_sso")]
    response = run_knowledge(engine, monkeypatch, MULTI, items)
    if not omit_meaning:
        assert_knowledge_complete(response, [("meaning", ("business",), "saml_sso"),
            ("prerequisite", ("business",), "saml_sso")])
    else:
        assert response.status == "incomplete"
        assert not response.business.consultation_coverage.scope_complete
        assert [x.stage for x in response.business.consultation_coverage.items] == ["extraction_incomplete", "answered"]
        # The real prerequisite citation also contains the concept. It still
        # cannot grant the omitted meaning question accepted identity.
        assert response.business.consultation_coverage.items[0].retrieval_ids == []
        assert response.business.consultation_coverage.items[1].retrieval_ids


@pytest.mark.parametrize("message,api_quote,hook_quote", [
    (FEATURE_FIRST, "API方面问Team", "Webhook方面问Basic"),
    (LOCAL_TOPIC, "团队版Team支持吗", "基础版Basic支持吗"),
])
def test_original_feature_first_pairs_run_actual_tools_without_cartesian_expansion(engine, monkeypatch, message, api_quote, hook_quote):
    response = run_knowledge(engine, monkeypatch, message, [
        item("api", api_quote, ("team",), aspect="support", feature="api_access"),
        item("hook", hook_quote, ("basic",), aspect="support", feature="webhook_notifications")])
    assert_knowledge_complete(response, [("support", ("team",), "api_access"),
        ("support", ("basic",), "webhook_notifications")])
    assert [x.support[0].supported for x in response.business.consultation_coverage.items] == [True, False]


def test_one_wrong_pair_preserves_other_answer_and_never_starts_wrong_search(engine, monkeypatch):
    response = run_knowledge(engine, monkeypatch, FEATURE_FIRST, [
        item("api", "API方面问Team", ("team",), aspect="support", feature="api_access"),
        item("wrong", "Webhook方面问Basic", ("team",), aspect="support", feature="webhook_notifications")], late_search="wrong")
    assert response.status == "incomplete" and not response.business.consultation_coverage.scope_complete
    api, hook = response.business.consultation_coverage.items
    assert api.status == "completed" and api.support[0].supported
    assert hook.plan_ids == ["basic"] and hook.stage == "extraction_incomplete"
    wrong_search = next(r for r in response.tool_results if r["tool_call_id"] == "search-wrong")
    assert not wrong_search["result"]["ok"]
    assert wrong_search["result"]["error"]["code"] == "knowledge_search_scope_invalid"


@pytest.mark.parametrize("skip_format", [False, True])
def test_two_same_feature_details_have_separate_real_search_and_explanation_obligations(engine, monkeypatch, skip_format):
    message = "Team的API限流是多少，返回格式是什么？"
    response = run_knowledge(engine, monkeypatch, message, [
        item("limit", "Team的API限流是多少", ("team",), aspect="detail", feature="api_access"),
        item("format", "返回格式是什么", ("team",), aspect="detail", feature="api_access")],
        insufficient_ids=("limit", "format"), skip_explanations=("format",) if skip_format else ())
    assert response.status == ("incomplete" if skip_format else "completed")
    limit, form = response.business.consultation_coverage.items
    assert limit.item_id != form.item_id and limit.question_id != form.question_id
    assert limit.stage == "evidence_insufficient"
    assert form.stage == ("explanation_pending" if skip_format else "evidence_insufficient")
    assert limit.retrieval_ids and form.retrieval_ids
    assert set(limit.retrieval_ids).isdisjoint(form.retrieval_ids)
    assert limit.source_item_ids == ["limit"] and form.source_item_ids == ["format"]
    assert not limit.support and not form.support


@pytest.mark.parametrize("failure", ["retrieval", "explanation"])
def test_multi_aspect_failure_retains_independent_real_answer(engine, monkeypatch, failure):
    items = [item("meaning", "Business的SSO是什么", ("business",), aspect="meaning", feature="saml_sso"),
        item("rule", "有什么使用前提", ("business",), aspect="prerequisite", feature="saml_sso")]
    if failure == "retrieval":
        native = KnowledgeService._execute
        invoked = 0
        def first_native_timeout(self, request):
            nonlocal invoked
            invoked += 1
            if invoked == 2:
                raise subprocess.TimeoutExpired("controlled-native-worker", .01)
            return native(self, request)
        monkeypatch.setattr(KnowledgeService, "_execute", first_native_timeout)
    response = run_knowledge(engine, monkeypatch, MULTI, items,
        invalid_explanation="meaning" if failure == "explanation" else None,
        late_search="meaning" if failure == "retrieval" else None)
    assert response.status == "incomplete"
    assert response.business.consultation_coverage.scope_complete
    meaning, rule = response.business.consultation_coverage.items
    assert meaning.stage == ("query_failed" if failure == "retrieval" else "explanation_rejected")
    assert rule.status == "completed" and rule.prerequisites and rule.retrieval_ids
    assert "兼容的身份提供方" in response.message


@pytest.mark.parametrize("plans,message", [
    (("basic", "team", "business"), "请分别告诉我基础版 Basic、团队版 Team、企业版 Business 的每席每月订阅单价。"),
    (("team",), "团队版 Team 的每席每月订阅单价是多少？"),
])
def test_original_fixed_price_questions(engine, monkeypatch, plans, message):
    response = run_prices(engine, monkeypatch, message, [item("prices", message, plans)], queried=plans)
    assert response.status == "completed"
    assert [x.plan_id for x in response.business.unit_prices] == list(plans)
    assert [x.unit_price_minor for x in response.business.unit_prices] == ([4000, 8000, 12000] if len(plans) == 3 else [8000])
    assert response.business.unit_price.plan_id == plans[-1]
    assert response.business.proposal is None


@pytest.mark.parametrize("plans,message", [
    (("team", "basic"), "我们需要通过产品 API 与自己的系统集成，还需要 Webhook 通知。团队版 Team 是否支持这两项能力？基础版 Basic 是否支持？"),
    (("team",), "团队版 Team 是否支持产品 API 和 Webhook 通知？请分别说明。"),
    (("basic",), "基础版 Basic 是否支持产品 API 和 Webhook 通知？请分别说明。"),
])
def test_original_fixed_support_questions(engine, monkeypatch, plans, message):
    items = [item(p + f, message, (p,), aspect="support", feature=f)
        for p in plans for f in ("api_access", "webhook_notifications")]
    response = run_knowledge(engine, monkeypatch, message, items)
    assert_knowledge_complete(response, [("support", (p,), f) for p in plans
        for f in ("api_access", "webhook_notifications")])
    assert [x.support[0].supported for x in response.business.consultation_coverage.items] == (
        [True, True, False, False] if len(plans) == 2 else [plans[0] == "team"] * 2)


@pytest.mark.parametrize("early_tool", [None, "match_requirements", "calculate_price"])
def test_retained_task_consultation_closes_unrequested_execution_and_preserves_real_result(engine, monkeypatch, early_tool):
    from test_proposals import seed
    sid, runtime, old = asyncio.run(seed(engine))
    before = runtime.state.current_task(recheck=False)
    message = "保持刚才方案不变，插问两件事：基础版Basic支持产品API吗？企业版Business的每席每月单价是多少？"
    items = [item("api", "基础版Basic支持产品API吗", ("basic",), aspect="support", feature="api_access"),
        item("price", "企业版Business的每席每月单价是多少", ("business",))]
    provider = knowledge_provider(monkeypatch, items, review_intent="selection",
        reviewed_target={"kind": "selection", "relation": "continue", "task_id": before["requirements"]["requirement_id"]},
        extra_unit="business", extra_match=not early_tool, early_execution=early_tool)
    from app.tools.business import BusinessToolRegistry
    worker = BusinessToolRegistry._execute
    def no_new_business_worker(self, name, arguments):
        assert name not in {"match_requirements", "calculate_price"}, "Unrequested business worker must not execute"
        return worker(self, name, arguments)
    monkeypatch.setattr(BusinessToolRegistry, "_execute", no_new_business_worker)
    response = asyncio.run(engine.chat(ChatRequest(session_id=sid, message=message)))
    after = runtime.state.current_task(recheck=False)
    for field in ("requirements", "pricing", "result_turn_id", "result_message_id"):
        assert after[field] == before[field]
    assert after["status"] == "current" and after["pricing"]["base_price_minor"] == 7680000
    assert response.status == "completed"
    assert response.business.pricing is None and response.business.proposal is None and runtime.proposal is None
    assert not response.business.presentation.show_comparison and not response.business.presentation.show_current_result
    assert response.business.unit_price is None  # Preserve this existing selection+price-consultation projection.
    assert [p.unit_price_minor for p in response.business.unit_prices] == [12000]
    if early_tool:
        assert not response.business.errors and response.business.consultation_coverage.items[0].stage == "answered"
        failed = next(r for r in response.tool_results if r["tool_call_id"] == "early-execution")
        assert not failed["result"]["ok"] and failed["result"]["error"]["code"] == "business_execution_not_requested"
        assert failed["result"]["error"]["scope"] == "retained_task"
        assert "未请求重新匹配或计价" in failed["display_result"]["error"]["message"]
        assert "此业务执行未进行" in failed["display_result"]["error"]["message"]
    else:
        assert not response.business.errors
        assert response.business.consultation_coverage.items[0].support[0].supported is False
        assert all(i.stage == "answered" for i in response.business.consultation_coverage.items)
        assert not any(r["name"] == "match_requirements" for r in response.tool_results)
        assert len(provider.sent) == 3
        validate_tool_message_sequence(provider.live_messages)
        skipped = next(m for m in provider.live_messages if m.get("tool_call_id") == "unrequested-match")
        assert json.loads(skipped["content"])["error"]["code"] == "dependency_not_executed"
    assert len(provider.reviews) == 1
    same_history(engine, response, message)
    assert asyncio.run(engine.submit_quote(sid, old.action_ref)).status == "not_saved"


def test_unrequested_execution_gate_does_not_relax_strict_input(engine, monkeypatch):
    from test_proposals import seed
    from app.tools.business import BusinessToolRegistry
    sid, runtime, _ = asyncio.run(seed(engine))
    before = runtime.state.current_task(recheck=False)
    message = "插问Basic支持API吗？"
    submitted = [item("api", "Basic支持API吗", ("basic",), aspect="support", feature="api_access")]
    from test_consultation_scope_convergence import apply
    apply(message, submitted, intent="knowledge", state=runtime.state)
    # Ordinary production review has established the retained-task boundary;
    # malformed fields still fail before that boundary can classify execution.
    result = asyncio.run(BusinessToolRegistry(runtime.state.settings, runtime.state, message).execute(
        "calculate_price", '{"plan_id":"team","paid_seats":80.5,"months":12}'))
    assert not result["ok"] and result["error"]["code"] == "invalid_arguments"
    assert runtime.state.current_task(recheck=False)["pricing"] == before["pricing"]


def test_malformed_first_candidate_is_not_repaired_or_sent_to_another_review(engine, monkeypatch):
    from test_presales_orchestrator import script
    message = "企业版Business的SAML SSO需要什么前提，以及这项功能是什么意思？"
    submitted = [item("rule", message, ("business",), aspect="prerequisite", feature="saml_sso"),
        item("meaning", message, ("business",), aspect="meaning", feature="saml_sso")]
    candidate = call("update_requirements", {"intent": "knowledge", "consultations": submitted}, "candidate")
    candidate.function.arguments = candidate.function.arguments.replace("}}, {", "}, {", 1)
    provider = script(monkeypatch, [completion(candidate)], review(submitted, intent="knowledge"))
    response = asyncio.run(engine.chat(ChatRequest(message=message)))
    assert response.status == "incomplete" and response.business.semantic_review["dispatched"] == 0
    assert response.business.semantic_review["error_code"] == "candidate_invalid" and not provider.reviews
    assert [r["name"] for r in response.tool_results] == ["update_requirements"]
    assert not response.tool_results[0]["result"]["ok"]
    assert response.business.errors[0]["code"] == "semantic_review_candidate_invalid"
    assert "首候选参数未通过格式或字段校验" in response.message
    assert "尚未进行独立核对与业务执行" in response.message
    assert "核对候选没有通过服务端事实校验" not in response.message
    assert "首候选参数" in response.tool_results[0]["display_result"]["error"]["message"]
    same_history(engine, response, message)
    assert engine.quotes.list_quotes() == []


def test_actual_rejected_work_keeps_dependency_failure_and_old_task_receipt(engine, monkeypatch):
    from test_proposals import seed
    from test_presales_orchestrator import group
    sid, runtime, old = asyncio.run(seed(engine))
    before = runtime.state.current_task(recheck=False)
    message = "改成约70席后多少钱？另外，Basic支持API吗？"
    provider = knowledge_provider(monkeypatch, [item("api", "Basic支持API吗", ("basic",),
        aspect="support", feature="api_access")], review_intent="selection", early_match=True,
        reviewed_target={"kind": "selection", "relation": "continue", "task_id": before["requirements"]["requirement_id"]},
        reviewed_groups=[group("seats", "改成约70席", 70, verdict="reject", reason="uncertain", certainty="approximate")])
    response = asyncio.run(engine.chat(ChatRequest(session_id=sid, message=message)))
    assert response.status == "incomplete" and response.business.errors
    failed = next(r for r in response.tool_results if r["tool_call_id"] == "early-match")
    assert not failed["result"]["ok"] and failed["result"]["error"]["details"]["issue"] == "review_dependency"
    after = runtime.state.current_task(recheck=False)
    assert all(after[k] == before[k] for k in ("requirements", "pricing", "result_turn_id", "result_message_id"))
    assert response.business.pricing is None and response.business.proposal is None
    assert len(provider.reviews) == 1
    assert asyncio.run(engine.submit_quote(sid, old.action_ref)).status == "not_saved"
