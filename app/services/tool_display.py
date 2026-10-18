"""Safe display views of actual tool execution records.

Raw arguments/results remain API diagnostics. The UI receives only schema-
checked necessary parameters after a successful execution, or a controlled
failure description. Rejected candidates and exception echoes are not display
facts. This module formats records; it never executes or authorizes a tool.
"""

from copy import deepcopy
import json
import re

from app.schemas.business import INPUT_REJECTIONS, RejectedInputDetails, RequirementPatch
from app.schemas.matching import RequirementSet
from app.tools.business import (
    CatalogArguments, ExplanationArguments, NoArguments, PriceArguments,
    SearchArguments, QuoteArguments,
)


ARGUMENT_MODELS = {
    "update_requirements": RequirementPatch,
    "search_knowledge": SearchArguments,
    "query_catalog": CatalogArguments,
    "match_requirements": NoArguments,
    "calculate_price": PriceArguments,
    "explain_knowledge": ExplanationArguments,
    "query_quote": QuoteArguments,
}

ERROR_MESSAGES = {
    "unknown_tool": "该工具未注册在只读业务链中。",
    "invalid_arguments": "工具参数不是合法的严格 JSON 对象，或类型、字段、范围不符合工具契约。",
    "arguments_budget": "工具参数超过体量预算，未执行。",
    "reference_repair_changed_candidate": "需求提取纠正改变了引用以外的候选信息；未执行更新，此前已接受的信息保留。",
    "invalid_business_input": "候选或业务参数未通过用户原话、当前工作状态或业务范围校验。",
    "invalid_tool_result": "业务服务结果未通过类型、身份、参数或当前目录的一致性校验。",
    "evidence_invalid": "引用未通过本业务链、实际检索或当前资料的校验；请重新检索。",
    "evidence_conflict": "所选资料关联的条件在本业务链已有已知冲突，不能作为干净解释依据。",
    "knowledge_explanation_invalid": "本次知识解释的结构、事项关联或连续原文引用未通过校验；相关回答尚未完成。",
    "knowledge_search_scope_invalid": "本次资料查询的咨询事项关联未通过校验；相关查询未执行。",
    "tool_failed": "本次业务工具未完成；已取得的其他可信结果保留。",
    "invalid_quote_id": "草案编号格式不合法，请核对编号。",
    "quote_not_found": "当前记录中未找到该编号；不能据此判断它从未保存。请核对编号和当前数据位置。",
    "quote_in_trash": "这份草案当时保存成功，目前在最近删除。请打开数据管理，在最近删除中恢复该草案后读取。",
    "quote_permanently_deleted": "这份草案此前保存成功，目前已永久删除，正文无法读取或恢复。旧保存动作不会重新创建它。",
    "quote_read_unavailable": "草案暂时无法读取，可以稍后按原编号重试。",
    "quote_query_read_only": "本轮只查询已保存草案，没有修改、重新匹配或计价当前选型。",
    "semantic_review_timeout": "本轮原话语义核对超时，尚未提交新业务事实；旧有效事实保留。等待超时不表示后台工作已经结束。",
    "semantic_review_unavailable": "本轮原话语义核对未完成，尚未提交新业务事实；旧有效事实保留。",
    "semantic_review_invalid": "系统没有取得有效的逐组核对结果，尚未提交新业务事实；旧有效事实保留。",
    "semantic_review_request_invalid": "独立核对请求未通过本地格式或预算校验，尚未派发独立核对，未提交新业务事实；旧有效事实保留。",
    "semantic_review_output_exhausted": "独立核对输出达到本次额度，未取得完整核对结果；未提交新业务事实，旧有效事实保留。",
    "semantic_review_candidate_invalid": "模型生成的首候选参数未通过格式或字段校验，尚未进行独立核对与业务执行；旧有效事实保留。",
    "semantic_review_facts_invalid": "核对候选没有通过服务端事实校验，尚未提交对应新事实。",
    "semantic_review_bypass_blocked": "后续工具试图引入未经本轮核对的新事实，该修改没有执行；已核定状态保留。",
    "semantic_review_required": "尚未取得已核对的本轮候选，依赖业务工具没有执行；旧有效事实保留。",
    "business_execution_not_requested": "本轮核定为独立咨询，未请求重新匹配或计价；此业务执行未进行，真实旧任务保留。",
    "quote_query_scope_mismatch": "工具编号与本轮已核定的查回对象不一致，没有读取其他草案。请核对本轮需要查询的编号。",
}


def _failure_view(name: str, result: dict) -> dict:
    error = result.get("error")
    error = error if type(error) is dict else {}
    code = error.get("code", "tool_failed")
    if type(code) is not str or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code):
        code = "tool_failed"
    # Error messages are service-owned constants. Never echo rejected values,
    # unknown fields, arbitrary exception messages or a candidate's own result.
    step = name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,99}", name) else "unknown_tool"
    shown_error = {
        "code": code, "step": step,
        "message": ERROR_MESSAGES.get(code, ERROR_MESSAGES["tool_failed"]),
    }
    if code in {"quote_in_trash", "quote_permanently_deleted", "quote_not_found"}:
        # No deleted snapshot, rejected message or exception echo is retained.
        shown_error["current_status"] = {"quote_in_trash": "trash",
            "quote_permanently_deleted": "permanently_deleted", "quote_not_found": "not_found"}[code]
        if code == "quote_in_trash":
            shown_error["recovery_entry"] = "数据管理 / 最近删除 / 恢复"
    if code in {"invalid_arguments", "invalid_business_input"}:
        try:
            details = RejectedInputDetails.model_validate(error.get("details"))
        except (ValueError, TypeError):
            pass
        else:
            message, retry_action = INPUT_REJECTIONS[details.issue]
            field_name = {"candidate": "本轮候选信息", "requirements": "功能需求", "paid_seats": "付费席位",
                "months": "订阅月份", "saml_sso_compatible_idp": "当前兼容身份提供方前提",
                "selected_plan_id": "套餐选择", "pricing": "计价请求"}[details.field]
            message = f"{field_name}：{message}"
            previous = details.previous_value
            if details.field in {"paid_seats", "months"} and type(previous) is int:
                message += f" 此前已接受的值仍为 {previous}{' 席' if details.field == 'paid_seats' else ' 个月'}。"
            elif details.field == "selected_plan_id" and previous in {"basic", "team", "business"}:
                message += f" 此前已接受的套餐仍为 {previous.capitalize()}。"
            elif details.field == "saml_sso_compatible_idp" and previous in {"yes", "no", "unknown"}:
                message += " 此前自述仍为" + {"yes": "具备", "no": "不具备", "unknown": "不确定"}[previous] + "。"
            if details.scope == "candidate_patch":
                message += " 本轮候选未更新；其他已接受信息保留。"
            shown_error.update(message=message, details=details.model_dump(mode="json"), retry_action=retry_action)
    # These associations are emitted by the owning conflict check, not copied
    # from rejected candidate parameters. Retain usable diagnostic identities.
    if code == "evidence_conflict":
        for field in ("retrieval_ids", "condition_ids"):
            identities = error.get(field)
            if type(identities) is list and all(type(value) is str and re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,299}", value) for value in identities):
                shown_error[field] = list(identities)
    return {"ok": False, "error": shown_error}


def _accepted_patch_arguments(patch: RequirementPatch, data: dict) -> dict:
    """Reflect only the candidate parts present in the accepted execution.

For example, a duplicate addition does not replace the old requirement's
wording or quote. The display must not imply that this unused candidate became
the accepted requirement. Deletions are verified by the owning tool before it
returns success; the removed requirement is intentionally absent afterwards.
"""
    requirements = RequirementSet.model_validate_json(json.dumps(data["requirements"], ensure_ascii=False))
    if data.get("intent") != patch.intent:
        raise ValueError("Accepted intent differs")
    shown = {"intent": patch.intent}
    accepted_add = []
    for candidate in patch.add:
        item = next((item for item in requirements.items
                     if all(getattr(item, field) == getattr(candidate, field)
                            for field in ("user_quote", "kind", "feature_id"))
                     and "已增加硬需求：" + item.statement in data.get("changes", ())), None)
        if item is not None:
            # The owning service can normalize the candidate's wording. Show
            # the actual accepted fields, never the unused model statement.
            accepted_add.append({field: getattr(item, field)
                                 for field in ("statement", "user_quote", "kind", "feature_id")})
    if accepted_add:
        shown["add"] = accepted_add
    if patch.remove:
        shown["remove"] = [candidate.model_dump(mode="json") for candidate in patch.remove]
    for field, accepted in (("parameters", requirements.parameters),
                            ("conditions", requirements.condition_assertions)):
        candidates = getattr(patch, field)
        values = [candidate.model_dump(mode="json") for candidate in candidates if candidate in accepted]
        if values:
            shown[field] = values
    if (patch.selection_quote is not None and patch.intent not in {"knowledge", "unit_price", "general"}
            and patch.selected_plan_id == requirements.selected_plan_id):
        shown["selected_plan_id"] = patch.selected_plan_id
        shown["selection_quote"] = patch.selection_quote
    if patch.pricing_quote is not None and data.get("wants_price") is True:
        shown["pricing_quote"] = patch.pricing_quote
    if patch.cancel_pricing_quote is not None and data.get("wants_price") is False:
        shown["cancel_pricing_quote"] = patch.cancel_pricing_quote
    contexts = [quote for quote in patch.context_quotes if quote in requirements.context_quotes]
    if contexts:
        shown["context_quotes"] = contexts
    return shown


def tool_display_fields(name: str, arguments: str, result: dict) -> dict:
    """Add a display projection without changing any raw execution record.

``accepted`` means the real execution succeeded and its necessary parameters
can be shown. ``rejected`` means the tool returned a real error. ``unavailable``
means no trusted display mapping is available; it does not reverse the actual
execution's ok value or invent a new tool execution.
"""
    display_name = name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,99}", name) else "未注册工具（名称无效）"
    if type(result) is dict and result.get("ok") is False:
        return {"display_name": display_name, "display_arguments": {}, "display_arguments_status": "rejected",
                "display_result": _failure_view(name, result)}
    try:
        if type(result) is not dict or result.get("ok") is not True or name not in ARGUMENT_MODELS:
            raise ValueError("No accepted execution")
        data = result["data"]
        if type(data) is not dict:
            raise ValueError("No domain result")
        if name == "update_requirements" and "operation_feedback" in data:
            # The first candidate is retained in the raw record for audit. It
            # may have representation errors or rejected groups, so parsing it
            # again cannot define what this reviewed execution accepted.
            requirements = RequirementSet.model_validate_json(json.dumps(data["requirements"], ensure_ascii=False))
            shown_data = {key: deepcopy(data[key]) for key in (
                "intent", "active_intent", "pending", "wants_price", "changes",
                "operation_feedback", "semantic_review",
                "turn_consultations", "consultation_complete", "consultation_feedback",
            ) if key in data}
            shown_data["requirements"] = requirements.model_dump(mode="json")
            return {"display_name": display_name, "display_arguments": {},
                "display_arguments_status": "accepted",
                "display_result": {"ok": True, "data": shown_data}}
        checked = ARGUMENT_MODELS[name].model_validate_json(arguments)
        if name == "update_requirements":
            necessary = _accepted_patch_arguments(checked, data)
            # Only owning WorkState's accepted public fields are display data.
            shown_data = {key: deepcopy(data[key]) for key in (
                "requirements", "intent", "active_intent", "pending", "wants_price", "changes",
            ) if key in data}
            shown_result = {"ok": True, "data": shown_data}
        elif name == "explain_knowledge":
            # Only accepted segments and bindings are display facts. Exact
            # duplicate candidates may have been removed by the owning tool.
            accepted = ExplanationArguments.model_validate_json(json.dumps({key: data[key]
                for key in ("retrieval_id", "answerability", "citations", "consultation_item_ids")
                if key in data}, ensure_ascii=False))
            if (accepted.retrieval_id != checked.retrieval_id or accepted.answerability != checked.answerability
                    or (checked.consultation_item_ids and accepted.consultation_item_ids != checked.consultation_item_ids)
                    or [(c.source_id, c.quote) for c in accepted.citations] != list(dict.fromkeys(
                        (c.source_id, c.quote) for c in checked.citations))):
                raise ValueError("Accepted explanation differs from checked candidate")
            necessary = accepted.model_dump(mode="json", exclude_none=True)
            shown_result = deepcopy(result)
        else:
            necessary = checked.model_dump(mode="json", exclude_none=True)
            shown_result = deepcopy(result)
        return {"display_name": display_name, "display_arguments": necessary, "display_arguments_status": "accepted",
                "display_result": shown_result}
    except (ValueError, TypeError, KeyError):
        return {"display_name": display_name, "display_arguments": {}, "display_arguments_status": "unavailable",
                "display_result": {"ok": type(result) is dict and result.get("ok") is True,
                    "display_status": "unavailable",
                    "message": "本次调用没有取得可信的参数与结果展示映射；原始记录仅用于接口调试。"}}
