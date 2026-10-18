"""Explicit review-provider substitute for historical scheduling tests.

This fixture translates *scripted test candidates* into a review envelope. It
does not establish language accuracy. Both actual provider requests and the
real SemanticVerifier / WorkState gates remain enabled; review_calls is kept
separate from the legacy main-chain captured calls and output queue.

Special repair/failure tests supply review_candidates or review_outputs rather
than changing production behavior or consuming a second correction loop.
"""
from copy import deepcopy
import json
import re
from types import SimpleNamespace


def is_review_request(kwargs):
    return kwargs.get("tool_choice") == {"type": "function", "function": {"name": "adjudicate_turn"}}


def review_payload(kwargs):
    assert is_review_request(kwargs)
    assert kwargs.get("max_retries") == 0
    payload = json.loads(kwargs["messages"][-1]["content"])
    assert {"original_message", "first_candidate", "current_tasks", "identities"} <= payload.keys()
    assert "plans" in payload["identities"] and "features" in payload["identities"]
    return payload


def review_from_candidate(payload, candidate=None):
    message = payload["original_message"]
    patch = deepcopy(payload["first_candidate"] if candidate is None else candidate)
    current = payload["current_tasks"]
    intent = patch.get("intent", "general")
    kind = intent if intent in {"selection", "price"} else "none"
    selection = current.get("current_selection") or {}
    independent = current.get("independent_pricing") or {}
    task = (selection.get("requirements") if kind == "selection" else independent.get("requirements")) or current.get("requirements") or {}
    owned_task = (selection.get("requirements") if kind == "selection" else independent.get("requirements")) or {}
    target = {"kind": kind, "relation": "continue" if kind != "none" else "detour",
        "task_id": owned_task.get("requirement_id") if kind != "none" else None}
    groups = []

    def span(quote):
        index = message.find(quote) if isinstance(quote, str) else -1
        result = {"quote": quote}
        if index >= 0:
            result.update(start=index, end=index + len(quote))
        return result

    def add(action, quote, *, reason=None, **values):
        op = {"action": action, "span": span(quote), **values}
        groups.append({"group_id": f"controlled-{len(groups)}", "verdict": "reject" if reason else "accept",
            "reason": reason or "confirmed", "operations": [op]})

    for item in patch.get("add", []):
        add("add", item["user_quote"], requirement=item)
    for item in patch.get("remove", []):
        original = next((old for old in task.get("items", []) if old["requirement_id"] == item["requirement_id"]), None)
        wrong_target = bool(original and original.get("feature_id") == "api_access" and "导出" in item["user_quote"] and "API" not in item["user_quote"])
        add("remove", item["user_quote"], requirement_id=item["requirement_id"], reason="unknown_identity" if wrong_target else None)
    for item in patch.get("parameters", []):
        uncertain = item["name"] == "paid_seats" and bool(re.search(r"大约|约\d|预计|差不多|暂定", message))
        add("set", item["user_quote"], field=item["name"], value=item["value"],
            reason="uncertain" if uncertain else None, certainty="approximate" if uncertain else "certain")
    for item in patch.get("conditions", []):
        future = "以后" in message or "未来" in message
        add("set", item["user_quote"], field=item["condition_id"], value=item["value"],
            reason="future" if future else None, time_scope="future" if future else "current")
    if patch.get("selection_quote") is not None:
        add("select_plan" if patch.get("selected_plan_id") else "cancel_selection", patch["selection_quote"],
            value=patch.get("selected_plan_id"))
    if patch.get("pricing_quote"):
        add("request_pricing", patch["pricing_quote"])
    if patch.get("cancel_pricing_quote"):
        add("cancel_pricing", patch["cancel_pricing_quote"])
    for quote in patch.get("context_quotes", []):
        add("note", quote)
    if not patch.get("parameters") and kind == "selection" and "席位" in message and re.search(r"改.*(?:约|预计|差不多|暂定)\d", message):
        value = int(re.search(r"(?:约|预计|差不多|暂定)(\d+)", message).group(1))
        add("set", message, field="paid_seats", value=value, reason="uncertain", certainty="approximate")
    # Historical tests explicitly request reassessment/preparation while often
    # scripting an empty patch. This is a declared controlled provider result.
    if kind == "selection" and re.search(r"重新|重算|评估|准备|保存|核对功能", message):
        add("request_evaluation", message)
    if kind == "selection" and re.search(r"准备草案|准备保存|保存这个|方案准备", message):
        add("request_proposal", message)
    if intent == "quote_query":
        quote_ids = re.findall(r"Q-[0-9A-F]{32}", message)
    else:
        quote_ids = []
    # Historical scripted rate/prerequisite fixtures now declare their scope.
    # This translation is explicitly controlled evidence, not a production
    # language parser or a claim about an actual model's completeness.
    consultations = deepcopy(patch.get("consultations", []))
    if not consultations and intent in {"unit_price", "knowledge"}:
        aliases = {"basic": ("Basic", "基础版", "基础套餐"),
            "team": ("Team", "团队版", "团队套餐"),
            "business": ("Business", "企业版", "商务套餐")}
        plan_ids = [identity for identity, labels in aliases.items()
            if any(label.lower() in message.lower() for label in labels)]
        if intent == "unit_price" and plan_ids:
            consultations = [{"item_id": "controlled-unit", "aspect": "unit_price",
                "plan_ids": plan_ids, "topic": message, "span": span(message)}]
        elif intent == "knowledge":
            sso = bool(re.search(r"SSO|SAML|统一登录|单点登录", message, re.I))
            consultations = [{"item_id": "controlled-knowledge",
                "aspect": "prerequisite" if sso and re.search(r"前提|条件", message) else "meaning",
                "plan_ids": plan_ids, "feature_id": "saml_sso" if sso else None,
                "topic": message, "span": span(message)}]
    return {"intent": intent, "target": target, "coverage_complete": True,
        "groups": groups, "quote_ids": list(dict.fromkeys(quote_ids)),
        "consultations": consultations, "consultation_coverage_complete": True}


def review_completion(review, *, finish_reason="stop"):
    return SimpleNamespace(model="controlled-review-provider", usage=None, choices=[SimpleNamespace(
        message=SimpleNamespace(content="", tool_calls=[SimpleNamespace(id="controlled-review-call",
            function=SimpleNamespace(name="adjudicate_turn", arguments=json.dumps(review, ensure_ascii=False)))]),
        finish_reason=finish_reason)])


def answer_review(client_type, kwargs):
    payload = review_payload(kwargs)
    client_type.review_calls.append(deepcopy(kwargs))
    outputs = getattr(client_type, "review_outputs", None)
    if outputs:
        output = outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return output
    candidates = getattr(client_type, "review_candidates", None)
    candidate = candidates.pop(0) if candidates else None
    return review_completion(review_from_candidate(payload, candidate))
