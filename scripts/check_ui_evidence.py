"""Read-only audit of recorded real API responses and actual browser DOM.

This never starts a model, computes a query vector, changes Sources, or repairs
evidence. It checks the recorded visible mapping; semantic Source review and
the user's local browser feedback remain separate acceptance responsibilities.
"""

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys

from app.schemas.chat import ChatResponse
from app.schemas.pricing import format_cny_minor
from app.services.catalog import get_catalog
from app.services.chat_render import _error_text
from app.services.knowledge import (
    DEFAULT_INDEX_PATH, KnowledgeError, _check_inputs, _read_index, model_digest, prepare_corpus,
)
from app.services.tool_display import tool_display_fields


ROOT = Path(__file__).resolve().parents[1]
NORMAL_KIND = "real_browser_real_model_formal_index_isolated_db"
REACT_UI_CONTRACT = "assistant-ui-external-store-v1"
LABELS = {
    "completed": "本轮执行完成", "needs_input": "等待补充信息", "incomplete": "本轮未完成",
    "knowledge": "知识问答", "unit_price": "目录与单价", "price": "只读计价", "selection": "需求选型",
    "general": "业务说明", "unsupported_action": "当前范围外的操作",
    "supported": "支持", "unsupported": "不支持", "unknown": "未知", "pending_condition": "前提待确认",
    "feature": "已识别能力", "unmodeled": "未建模，无法判断", "ambiguous": "有歧义，需澄清",
    "yes": "用户自述具备", "no": "用户明确不具备", "valid": "条件证据有效", "missing": "条件证据缺失",
    "conflict": "已知相关资料冲突", "unavailable": "执行不可用", "invalid": "条件证据无效",
    "matched": "匹配已完成", "blocked": "匹配受阻", "comparison_only": "仅比较目录，尚未完成选型",
    "retrieved": "取得相关片段", "empty": "没有可返回依据", "low_relevance": "仅有弱相关资料",
}
REASONS = {
    "catalog_unsupported": "目录明确不支持", "unmodeled_requirement": "当前未建模，无法判断",
    "ambiguous_requirement": "需求有歧义，须澄清", "condition_unknown": "用户前提待确认",
    "condition_not_met": "用户明确不具备前提", "condition_evidence_missing": "条件证据缺失",
    "condition_evidence_conflict": "已知相关条件资料冲突", "condition_evidence_version_mismatch": "条件证据版本无效",
    "condition_evidence_association_mismatch": "条件证据关联无效", "evidence_not_in_chain": "证据不属于当前业务链",
    "retrieval_timeout": "检索超时", "retrieval_failed": "检索故障", "empty_requirements": "尚未明确硬需求",
    "no_satisfying_plan": "没有完整满足档", "selected_plan_not_satisfying": "用户指定档不满足当前硬需求",
    "unknown_selected_plan": "指定套餐身份未知", "low_relevance": "只有弱相关资料", "no_hits": "没有返回片段",
    "source_budget_exceeded": "完整原文超过返回预算",
}


def require(condition, reason):
    if not condition:
        raise AssertionError(reason)


def normalized(text):
    return re.sub(r"\s+", "", text)


def includes(text, value, reason):
    require(normalized(str(value)) in normalized(text), reason)


def coded(text, value, reason, labels=LABELS):
    includes(text, labels.get(value, value), reason + "：业务含义缺失")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def latest_report(directory):
    candidates = []
    for path in directory.glob("browser-*.json"):
        value = read_json(path)
        if isinstance(value, dict) and value.get("kind") == NORMAL_KIND and value.get("cases"):
            candidates.append(path)
    require(candidates, "没有非空的真实正常浏览器报告；负面替身不能证明正常链路。")
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def ui_hashes(root=ROOT):
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [root / "app/ui/index.html", *sorted((root / "app/ui/assets").glob("*"))]
            if path.is_file()}


def runtime_hashes(root=ROOT):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((root / "app").rglob("*.py")) if "__pycache__" not in path.parts}


def validate_report_identity(report, final_observation=None):
    require(report.get("kind") == NORMAL_KIND, "此报告不是正常真实模型／正式索引的浏览器记录。")
    require(bool(report.get("cases")), "报告没有已完成的真实浏览器回合。")
    catalog = get_catalog()
    require(report.get("catalog_version") == catalog.version, "报告与当前权威 Catalog 版本不同。")
    index = _read_index(DEFAULT_INDEX_PATH)
    inputs = prepare_corpus().inputs(index.inputs.embedding)
    _check_inputs(index, inputs)
    require(report.get("formal_index_sha256") == hashlib.sha256(DEFAULT_INDEX_PATH.read_bytes()).hexdigest(),
            "正式索引文件摘要已与浏览器运行时不同。")
    require(report.get("formal_inputs_digest") == model_digest(inputs), "正式资料／Catalog／Embedding 配置摘要不一致。")
    require(report.get("formal_index_unchanged") is True, "报告没有确认正式索引保持不变。")
    actual_ui_hashes = ui_hashes(ROOT)
    if final_observation is None:
        require(report.get("ui_sha256") == actual_ui_hashes, "当前 UI 源码与实际浏览器运行的 UI 摘要不同，须核对受影响页面。")
    else:
        # A browser can reload a later UI build and inspect the same real API
        # responses without paying for new model calls. Preserve the original
        # report and explicitly bind actual final observations to that run.
        require(final_observation.get("uiContract") == REACT_UI_CONTRACT
                and final_observation.get("reportRunId") == report.get("run_id")
                and bool(report.get("run_id")), "最终页面观察没有绑定原真实运行身份。")
        require(final_observation.get("browserReloadedAfterBuild") is True
                and isinstance(final_observation.get("capturedAt"), str), "最终页面观察没有记录构建后的实际浏览器重载。")
        require(final_observation.get("ui_sha256") == actual_ui_hashes, "最终观察的 UI 摘要不对应当前实际构建。")
        require(final_observation.get("runtime_code_sha256") == report.get("runtime_code_sha256")
                == runtime_hashes(ROOT), "业务代码改变后不能只重放旧响应，须重新核验受影响真实链路。")
    if "runtime_code_sha256" in report:
        require(report["runtime_code_sha256"] == runtime_hashes(ROOT),
                "当前业务代码与实际浏览器运行的业务代码摘要不同，须重跑受影响真实链路。")
    require(report.get("turn_count") == len(report["cases"]), "运行报告的回合计数不一致。")
    require(report.get("model_call_count") == sum(len(case.get("model_dialogue_inputs", [])) for case in report["cases"]),
            "运行报告的模型调用计数不一致。")
    return catalog, index


def validate_case(case, catalog, index):
    require(case.get("http_status") == 200, "真实 /chat 回合未取得成功响应结构。")
    result = ChatResponse.model_validate_json(json.dumps(case["response"], ensure_ascii=False))
    require(result.business is not None, "本轮没有受控 business 响应。")
    history = case.get("history", [])
    # The existing SQLite row id is now returned for locating the original
    # result. It is not part of a model message and must not be compared as one.
    dialogue_history = [{"role": message["role"], "content": message["content"]} for message in history]
    require(len(history) >= 2 and dialogue_history[-2] == {"role": "user", "content": case["user"]}, "历史末次用户输入与本轮不一致。")
    require(dialogue_history[-1] == {"role": "assistant", "content": result.message}, "历史助手文本与最终 message 不是逐字同源。")
    if result.message_id is not None:
        require(history[-1].get("id") == result.message_id, "响应的原回复定位 ID 不对应实际已存助手行。")
    require(case.get("text_matches_stored") is True, "运行报告没有确认已存文本一致。")
    inputs = case.get("model_dialogue_inputs", [])
    require(bool(inputs), "此回合没有真实模型调用记录。")
    previous = dialogue_history[:-2][-30:]
    require(case.get("next_model_uses_stored_history") is True and inputs[0][:len(previous)] == previous,
            "下一轮实际模型输入没有保留同源已存历史。")
    require(inputs[0][-1] == {"role": "user", "content": case["user"]}, "实际初次模型输入未对应当前用户消息。")
    require(case.get("request_session_id") in {None, result.session_id}, "响应会话身份与实际请求不同。")
    business = result.business
    require(business.catalog_context == catalog, "显示身份没有来自当前权威 Catalog。")
    if business.catalog:
        require(business.catalog == catalog, "实际目录工具结果与当前 Catalog 不同。")
    for price in (business.unit_price, business.pricing):
        if price:
            plan = next((p for p in catalog.plans if p.plan_id == price.plan_id), None)
            require(plan is not None and price.unit_price_minor == plan.unit_price_minor and price.plan_name == plan.name,
                    "受控价格的套餐身份或单价与 Catalog 不同。")
            require(price.catalog_version == catalog.version and price.product_id == catalog.product.product_id,
                    "受控价格采用了不同产品或资料版本。")
    if business.match:
        require(business.match.requirements == business.requirements, "Match 对应了不同需求修订。")
        require(business.match.catalog_version == catalog.version and business.match.evidence_origin != "test_fixture",
                "Match 不是当前正式目录与证据结果。")
    task = getattr(business, "current_task", None)
    if task is not None:
        require(task.match is None or task.match.requirements == task.requirements,
                "当前任务摘要的评估对应不同需求修订。")
        require(task.match is None or (task.match.catalog_version == catalog.version
                and task.match.evidence_origin != "test_fixture"), "当前任务摘要混入旧目录或测试匹配。")
        if task.pricing is not None:
            plan = next((plan for plan in catalog.plans if plan.plan_id == task.pricing.plan_id), None)
            parameters = {parameter.name: parameter.value for parameter in task.requirements.parameters}
            require(task.status == "current" and task.match is not None and task.match.status == "matched"
                    and task.match.selected_plan_id == task.pricing.plan_id
                    and parameters.get("paid_seats") == task.pricing.paid_seats
                    and parameters.get("months") == task.pricing.months,
                    "当前任务金额与有效评估、原套餐或原参数不同源。")
            require(plan is not None and task.pricing.unit_price_minor == plan.unit_price_minor
                    and task.pricing.catalog_version == catalog.version, "当前任务金额不来自正式目录。")
    formal_sources = {source.source_id: source for source in index.inputs.sources}
    if task is not None and task.match is not None:
        for evidence in task.match.retrieval_evidence:
            require(evidence.scope == "formal", "当前任务摘要混入实验资料。")
            for hit in evidence.hits:
                require(formal_sources.get(hit.source.source_id) == hit.source,
                        "当前任务的条件Source不能逐字还原到正式资料。")
    observations = {}
    for evidence in [*(business.match.retrieval_evidence if business.match else ()), *business.retrieval_evidence]:
        require(evidence.scope == "formal", "正常链混入负面资料实验。")
        if evidence.status != "unavailable":
            require(evidence.dataset_version == catalog.version and evidence.index_inputs_sha256 == model_digest(index.inputs),
                    "检索记录采用了不同正式资料摘要。")
            require(evidence.index_build_id == index.build_id and evidence.embedding == index.inputs.embedding,
                    "检索记录不是当前正式索引／Embedding 配置。")
        else:
            # Failed retrieval may stop before it can identify an index. Null
            # metadata remains unknown; it must never become usable evidence.
            for supplied, expected in ((evidence.dataset_version, catalog.version),
                                       (evidence.index_inputs_sha256, model_digest(index.inputs)),
                                       (evidence.index_build_id, index.build_id),
                                       (evidence.embedding, index.inputs.embedding)):
                require(supplied is None or supplied == expected, "检索故障携带了不同正式资料身份。")
        require(evidence.retrieval_id not in observations or observations[evidence.retrieval_id] == evidence,
                "同一个检索 ID 的关联内容不同。")
        observations[evidence.retrieval_id] = evidence
        for hit in evidence.hits:
            require(formal_sources.get(hit.source.source_id) == hit.source, "实际检索 Source 不能逐字还原到当前正式原文。")
    for record in result.tool_results:
        expected = tool_display_fields(record["name"], record["arguments"], record["result"])
        require(all(record.get(key) == value for key, value in expected.items()), "工具安全展示摘要不能对应真实执行记录。")
    return result, observations


def section_map(observation):
    require(isinstance(observation.get("visible"), str) and isinstance(observation.get("allText"), str), "DOM 记录缺少实际可见文本或完整文本。")
    sections = {}
    for area in observation.get("sections", []):
        require(area["section"] not in sections, "DOM 同一轮存在重复业务区域。")
        require(isinstance(area.get("visible"), str) and isinstance(area.get("allText"), str), "DOM 区域缺少文本。")
        includes(observation["visible"], area["visible"], "区域可见文本不属于记录的实际卡片")
        includes(observation["allText"], area["allText"], "区域完整文本不属于记录的实际卡片")
        sections[area["section"]] = area
    return sections


def validate_view(result, observation, catalog, evidence, *, current_expected=None, current_from_record=False):
    if observation.get("uiContract") == REACT_UI_CONTRACT:
        return validate_projected_view(result, observation, catalog, evidence,
                                       current_expected=current_expected, current_from_record=current_from_record)
    return validate_legacy_view(result, observation, catalog, evidence)


def validate_projected_view(result, observation, catalog, evidence, *, current_expected=None, current_from_record=False):
    """Audit actual React DOM without requiring the old execution-report layout.

    The answer partition and the visible business components together carry
    the same facts as the server's independent formal history text. A current
    result may be elsewhere in Thread; ``currentResult`` must be captured from
    the element reached by the page's locator, never assembled from the API.
    """
    business = result.business
    presentation = business.presentation
    require(presentation is not None, "新界面响应没有服务端同源展示契约。")
    sections = section_map(observation)

    def area(name, all_text=False):
        require(name in sections, f"{observation['label']}：实际页面缺少 {name} 区域。")
        return sections[name]["allText" if all_text else "visible"]

    require(observation.get("turnId") == business.turn_id, "实际页面的本轮 ID 不对应响应。")
    require(observation.get("sessionId") == result.session_id, "实际页面的会话 ID 不对应响应。")
    # Controlled answer currently uses ordinary text/paragraphs. Do not permit
    # a separate client-generated explanation or hide a subset in details.
    includes(area("answer"), presentation.answer_markdown, "正式回答展示分区没有默认显示，或被前端改写。")
    diagnostics = area("tools", True)
    for value in (business.turn_id, result.session_id, result.status):
        includes(diagnostics, value, "真实响应身份／状态不能按需核对")
    require(sections["tools"].get("intent") == business.intent, "技术记录的真实意图身份不能按需核对。")
    plans = {plan.plan_id: plan for plan in catalog.plans}
    conditions = {condition.condition_id: condition for condition in catalog.conditions}
    plan_name = lambda identity: plans[identity].name if identity else "无"
    main = observation["visible"]
    for question in business.questions:
        includes(main, question, "影响结论的待答问题被折叠或遗漏")
    for error in business.errors:
        includes(main, _error_text(error), "实际未完成影响没有默认可见")
        includes(diagnostics, error["code"], "实际错误码不能按需核对")

    empty_comparison = (presentation.show_comparison and business.match is not None
                        and business.match.status == "comparison_only"
                        and not business.match.requirements.items)
    if presentation.show_comparison and not empty_comparison:
        match = business.match
        require(match is not None and business.intent == "selection", "新界面把插问或空评估当作本轮选型。")
        visible = area("match")
        cells = observation.get("matrixCells", [])
        expected = {(plan.plan_id, item.requirement_id): item for plan in match.plans for item in plan.items}
        actual = {(cell["planId"], cell["requirementId"]): cell for cell in cells}
        require(len(actual) == len(cells) and set(actual) == set(expected), "实际页面缺少、重复或混用了逐项套餐判断。")
        statements = {item.requirement_id: item.statement for item in match.requirements.items}
        for identity, item in expected.items():
            cell = actual[identity]
            includes(visible, cell["visible"], "逐项可见观察不属于实际 Match 区域。")
            includes(visible, cell.get("rowVisible", cell["visible"]), "需求行观察不属于实际 Match 区域。")
            includes(cell.get("rowVisible", cell["visible"]), statements[item.requirement_id], "逐项需求文字被遗漏")
            require(cell.get("status") == item.status, "四种支持状态的 DOM 身份不对应真实判断。")
            coded(cell["visible"], item.status, "逐项支持／不支持／未知／待确认显示不一致")
            if item.condition_id:
                includes(visible, conditions[item.condition_id].statement, "影响判断的实际条件没有默认可见")
            if item.evidence_source_ids:
                links = [link for link in observation.get("claimEvidence", []) if link.get("forId") == item.requirement_id]
                associated = {identity for link in links for identity in link.get("sourceIds", [])}
                require(set(item.evidence_source_ids) <= associated, "结论依据入口未包含实际采用的来源。")
        # Choice meanings are business facts; headings and fixed phrase order
        # are intentionally left to the adopted component's local renderer.
        choice = observation.get("choice", {})
        require(choice.get("lowestPlanId") == match.lowest_plan_id
                and choice.get("selectedPlanId") == match.selected_plan_id
                and choice.get("eligiblePlanIds") == list(match.eligible_plan_ids)
                and choice.get("selectionReason") == match.selection_reason,
                "最低满足、其他满足或实际选择的 DOM 身份与 Match 不同。")
        if match.lowest_plan_id:
            includes(main, plan_name(match.lowest_plan_id), "最低满足档没有默认可见")
        else:
            require(any(word in main for word in ("没有", "尚不能", "不能", "未", "无")), "没有最低满足档的受阻含义没有默认可见。")
        if match.selected_plan_id:
            includes(main, plan_name(match.selected_plan_id), "用户实际采用套餐没有默认可见")
        for identity in match.eligible_plan_ids:
            includes(main, plan_name(identity), "其他满足套餐没有默认可见")
    else:
        require("match" not in sections, "不需要比较的回复重复展示了旧选型表。")
        if empty_comparison:
            require(business.intent == "selection" and not business.requirements.items,
                    "未明确需求的空评估混入了其他真实需求或意图。")
            require(not observation.get("matrixCells"), "没有需求的评估伪造了套餐判断行。")
            require("No rows" not in main, "未明确需求时渲染了空比较表。")
            require(not any(value in main for value in ("已满足", "全部满足", "满足当前要求", "没有满足", "无满足", "尚无满足")),
                    "尚未明确需求被误说成已满足或无满足套餐。")
            require(any(value in area("answer") for value in ("还不能推荐", "尚不能推荐", "尚未评估", "尚未明确", "需求未明确")),
                    "没有需求时未默认说明尚不能评估。")

    price = business.pricing or business.unit_price
    if price is not None:
        includes(main, plan_name(price.plan_id), "采用的计价套餐不一致")
        includes(main, format_cny_minor(price.unit_price_minor), "受控单价显示不一致")
        includes(main, "元／席／月", "受控单价单位显示不一致")
        includes(diagnostics, price.catalog_version, "计价资料版本不能按需核对")
        if business.pricing:
            includes(main, f"{price.paid_seats} 席", "实际计价席位显示不一致")
            require(any(normalized(value) in normalized(main) for value in (f"{price.months} 月", f"{price.months} 个月")),
                    "实际计价月份显示不一致")
            includes(main, format_cny_minor(price.base_price_minor), "受控订阅基价显示不一致")
            require(price.formula in main, "服务端原算式未逐字显示。")

    task = current_expected if current_from_record else business.current_task
    if observation.get("captureMode") == "retrospective-live-message":
        require(current_from_record, "历史卡片补采必须绑定观察时最新的真实同会话状态。")
        own = business.current_task
        historical = observation.get("messageResult")
        if presentation.show_current_result:
            require(own is not None and isinstance(historical, dict), "历史卡片缺少其原真实结果快照。")
            require(historical.get("resultTurnId") == own.result_turn_id
                    and historical.get("resultMessageId") == own.result_message_id
                    and historical.get("requirementId") == own.requirements.requirement_id
                    and historical.get("revision") == own.requirements.revision
                    and historical.get("status") == own.status, "补采的历史卡片混用了其他结果身份。")
            still_current = (task is not None and own.result_turn_id == task.result_turn_id
                             and own.result_message_id == task.result_message_id)
            require(historical.get("current") is still_current, "历史选型结果在观察时冒充了当前结果。")
            includes(historical.get("visible", ""), "当前选型结果" if still_current else "历史选型结果", "当前／历史结果标题不对应实际业务身份")
            if own.pricing:
                includes(historical.get("visible", ""), format_cny_minor(own.pricing.base_price_minor), "历史金额被最新当前金额改写")
                require(own.pricing.formula in historical.get("visible", ""), "历史原算式被改写。")
        else:
            require(historical is None, "原插问被补造了自己的选型结果。")
    captured = observation.get("currentResult")
    if task is not None:
        require(isinstance(captured, dict), "没有实际定位并记录唯一当前结果。")
        require(captured.get("count") == 1, "页面存在多个当前结果或没有当前结果。")
        require(captured.get("resultTurnId") == task.result_turn_id
                and captured.get("resultMessageId") == task.result_message_id
                and captured.get("requirementId") == task.requirements.requirement_id
                and captured.get("revision") == task.requirements.revision
                and captured.get("status") == task.status,
                "当前结果的 DOM 身份不是服务端真实任务状态。")
        current_visible = captured.get("visible", "")
        if task.match and task.match.status == "comparison_only" and not task.requirements.items:
            require(task.match.selected_plan_id is None and task.match.lowest_plan_id is None
                    and not task.match.eligible_plan_ids and task.pricing is None,
                    "需求未明确的当前状态携带有效选型或金额。")
            includes(current_visible, "尚未评估（需求未明确）", "当前结果未说明需求未明确的真实评估状态")
        for parameter in task.requirements.parameters:
            values = [f"{parameter.value} 席"] if parameter.name == "paid_seats" else [f"{parameter.value} 月", f"{parameter.value} 个月"]
            require(any(normalized(value) in normalized(current_visible) for value in values), "当前结果丢失已接受参数。")
        if task.match and task.match.selected_plan_id:
            includes(current_visible, plan_name(task.match.selected_plan_id), "当前结果丢失实际选择套餐")
        if task.pricing:
            includes(current_visible, format_cny_minor(task.pricing.base_price_minor), "插问金额替换了原任务金额")
        if task.status == "needs_reassessment":
            require(task.match is None and task.pricing is None, "待重新评估状态携带了旧有效金额。")
            require(any(value in current_visible for value in ("重新评估", "尚未", "未完成", "等待")), "过期评估被误当作有效结果。")
    else:
        require(captured is None or captured.get("count", 0) == 0, "页面从历史或最新消息伪造了当前选型。")

    # Only adopted sources appear as answer evidence. Other retrieval hits can
    # stay in diagnostics, and cannot be presented as used by the conclusion.
    used = set(presentation.used_source_ids)
    source_views = observation.get("sources", [])
    adopted = {source["sourceId"] for source in source_views if source.get("adopted")}
    require(adopted == used, "来源入口混入未采用片段，或遗漏结论实际采用依据。")
    source_objects = {hit.source.source_id: hit.source for record in evidence.values() for hit in record.hits}
    if business.current_task and business.current_task.match:
        source_objects.update({hit.source.source_id: hit.source for record in business.current_task.match.retrieval_evidence for hit in record.hits})
    for view in source_views:
        require(view["sourceId"] in source_objects, "界面显示了本业务链之外的来源。")
        source = source_objects[view["sourceId"]]
        require(view.get("rawText") == source.raw_text, "可展开来源原文未逐字保留。")
        for value in (source.document_title, source.section_title, source.dataset_version,
                      source.relative_path, f"{source.line_start}–{source.line_end}"):
            includes(view.get("allText", ""), value, "实际来源标题、版本或定位被遗漏")
        require(not view.get("syntheticUrl"), "来源虚构了网址。")

    # The actual DOM supplies structured safety records as well as visible
    # text. Never reconstruct them from raw model arguments in the checker.
    tool_views = observation.get("toolRecords", [])
    require(len(tool_views) == len(result.tool_results), "实际工具记录缺少或伪造。")
    for view, record in zip(tool_views, result.tool_results, strict=True):
        require(view.get("toolCallId") == record["tool_call_id"]
                and view.get("displayArguments") == record["display_arguments"]
                and view.get("displayResult") == record["display_result"],
                "工具记录顺序、安全参数或受控结果被改写。")
        includes(diagnostics, record["display_name"], "实际工具名称不能按需核对")
    mode = "历史卡片补采及观察时当前结果" if observation.get("captureMode") == "retrospective-live-message" else "即时当前结果"
    return f"[{observation['label']}] {result.status}；新界面同源展示与{mode}映射通过"


def current_at_capture(report, observation):
    """Select state from the report timeline, independently of DOM claims."""
    try:
        captured = datetime.fromisoformat(observation["capturedAt"].replace("Z", "+00:00"))
        require(captured.tzinfo is not None, "观察时间缺少时区，不能绑定当前状态。")
        candidates = [(datetime.fromisoformat(case["finished_at"].replace("Z", "+00:00")), case)
                      for case in report["cases"] if case["response"]["session_id"] == observation["sessionId"]]
        completed = [(finished, case) for finished, case in candidates if finished <= captured]
    except (KeyError, TypeError, ValueError) as error:
        raise AssertionError("历史卡片观察缺少真实完成／捕获时间或会话身份。") from error
    require(completed, "观察时间之前没有此会话的真实已完成结果。")
    latest = max(completed, key=lambda value: value[0])[1]
    return ChatResponse.model_validate_json(json.dumps(latest["response"], ensure_ascii=False)).business.current_task


def validate_legacy_view(result, observation, catalog, evidence):
    business = result.business
    sections = section_map(observation)

    def area(name, all_text=False):
        require(name in sections, f"{observation['label']}：实际页面缺少 {name} 区域。")
        return sections[name]["allText" if all_text else "visible"]

    # The normal answer is also persisted for history. Layout can change;
    # replacing the answer with an execution status cannot pass this check.
    require(result.message in area("answer"), "正式回答没有默认显示，或与存储文本不同。")
    includes(area("status", True), business.turn_id, "实际页面的本轮 ID 不对应响应")
    includes(area("status", True), result.session_id, "实际页面的会话 ID 不对应响应")
    coded(area("status", True), result.status, "实际执行状态不能按需核对")
    includes(area("status", True), business.intent, "本轮意图身份被遗漏")
    plans = {p.plan_id: p for p in catalog.plans}
    conditions = {c.condition_id: c for c in catalog.conditions}

    def plan_name(identity):
        return plans[identity].name if identity else "无"

    if business.intent in {"selection", "price"}:
        demands = area("requirements") + (area("current-task") if "current-task" in sections else "")
        includes(area("requirements", True), business.requirements.requirement_id, "需求 ID 不能展开核对")
        includes(area("requirements", True), f"修订 {business.requirements.revision}", "需求修订不能展开核对")
        for change in business.changes:
            includes(area("requirements", True), change, "本轮需求变更不能展开核对")
        if business.intent == "selection":
            for item in business.requirements.items:
                includes(demands, item.statement, "硬需求被页面遗漏")
                includes(area("requirements", True), item.requirement_id, "硬需求身份被页面遗漏")
                if item.kind != "feature":
                    coded(demands, item.kind, "硬需求的未知／歧义没有默认可见")
                includes(area("requirements", True), item.user_quote, "硬需求不能展开核对用户原话")
            for assertion in business.requirements.condition_assertions:
                includes(area("requirements", True), assertion.condition_id, "用户自述前提的身份不能展开核对")
                includes(area("requirements", True), assertion.user_quote, "用户自述前提的原话不能按需核对")
                if assertion.value == "unknown":
                    require(any(value in demands for value in ("需要确认你是否具备", "用户尚不确定", "前提待确认")),
                            "未知前提没有默认可见")
                else:
                    coded(demands, assertion.value, "用户自述前提的含义没有默认可见")
        for parameter in business.requirements.parameters:
            require(any(normalized(value) in normalized(demands) for value in
                        ([f"{parameter.value} 席"] if parameter.name == "paid_seats" else
                         [f"{parameter.value} 月", f"{parameter.value} 个月"])), "实际已接受参数没有默认可见")
            includes(area("requirements", True), parameter.user_quote, "实际已接受参数的原话不能按需核对")
    if business.pending or business.questions:
        questions = area("questions")
        for question in business.questions:
            includes(questions, question, "影响结论的待答问题被折叠或遗漏")
        for pending in business.pending:
            includes(questions, {"paid_seats": "付费席位", "months": "订阅月份", "plan": "套餐", "requirements": "功能需求"}.get(pending,
                     conditions[pending[10:]].name if pending.startswith("condition:") else "待澄清需求" if pending.startswith("clarify:") else pending),
                     "影响结论的待确认项被折叠或遗漏")
    for error in business.errors:
        failures = area("errors")
        includes(area("errors", True), error["code"], "实际错误码不能展开核对")
        includes(failures, error["message"], "实际未完成原因没有默认可见")
    if business.intent == "selection" and business.match:
        match = business.match
        visible = normalized(area("match"))
        if match.status == "comparison_only":
            includes(visible, "还不能判断哪个套餐适合你", "未评估被误表示为已评估")
            require("未完整满足" not in visible and "全部硬需求满足" not in visible,
                    "没有硬需求时把未评估套餐错误标成满足或不满足。")
            for plan in match.plans:
                includes(area("match", True), plan_name(plan.plan_id) + "：尚未判断", "目录比较身份遗漏")
        else:
            coded(visible, match.status, "Match 状态没有默认可见")
            includes(visible, f"最低满足档{plan_name(match.lowest_plan_id)}", "最低满足档显示不一致")
            includes(visible, f"本轮采用套餐{plan_name(match.selected_plan_id)}", "当前采用套餐显示不一致")
            includes(visible, "完整满足集合" + ("、".join(plan_name(p) for p in match.eligible_plan_ids) or "无"), "完整满足集合显示不一致")
        statements = {r.requirement_id: r.statement for r in match.requirements.items}
        # The same facts may be shown as a compact requirements × plans matrix.
        # Keep identities and every assessment, without requiring repeated cards.
        cells = observation.get("matrixCells")
        if cells is None:
            cells = [{**item, "planId": plan["planId"], "rowVisible": item["visible"]}
                     for plan in observation.get("plans", []) for item in plan["items"]]
        expected_items = {(plan.plan_id, item.requirement_id): item
                          for plan in match.plans for item in plan.items}
        if match.status != "comparison_only":
            actual = {(cell["planId"], cell["requirementId"]): cell for cell in cells}
            require(len(actual) == len(cells) and set(actual) == set(expected_items),
                    "实际页面缺少、重复或混用了逐项套餐判断。")
            for identity, item in expected_items.items():
                view = actual[identity]
                row = view["visible"]
                row_context = view.get("rowVisible", row)
                includes(visible, row, "逐项可见观察不属于实际 Match 区域。")
                includes(visible, row_context, "需求行可见观察不属于实际 Match 区域。")
                includes(row_context, statements[item.requirement_id], "逐项需求文字被遗漏")
                require(view["badges"] and view["badges"][0] == LABELS[item.status],
                        "支持、不支持、未知或待确认徽标不对应真实逐项状态。")
                coded(row, item.status, "逐项支持／不支持／未知／待确认显示不一致")
                for reason in item.reason_codes:
                    coded(row, reason, "逐项阻断原因没有默认可见", REASONS)
                if item.condition_id:
                    includes(row_context, conditions[item.condition_id].statement, "影响判断的实际条件没有默认可见")
                    if item.condition_evidence_status:
                        coded(row_context, item.condition_evidence_status, "条件资料与故障含义混淆")
                    if item.condition_assertion_status:
                        coded(row_context, item.condition_assertion_status, "客户前提没有默认可见", {**LABELS, "unknown": "用户前提待确认"})
                if item.evidence_source_ids:
                    links = [link for link in observation.get("claimEvidence", [])
                             if link.get("forId") == item.requirement_id]
                    require(links, "采用的条件依据没有直接关联到具体需求结论。")
                    associated = {source_id for link in links for source_id in link.get("sourceIds", [])}
                    require(set(item.evidence_source_ids) <= associated, "结论依据入口未包含实际采用的来源。")
    if business.intent in {"selection", "price", "unit_price"} and (business.pricing or business.unit_price):
        price = business.pricing or business.unit_price
        amount = area("pricing")
        includes(amount, plan_name(price.plan_id), "采用的计价套餐不一致")
        includes(amount, f"单价{format_cny_minor(price.unit_price_minor)} 元／席／月", "受控单价或单位显示不一致")
        includes(amount, price.currency, "币种没有默认可见")
        includes(area("pricing", True), price.catalog_version, "计价资料版本不能按需核对")
        if business.pricing:
            includes(amount, f"实际采用付费席位{price.paid_seats} 席", "实际计价席位显示不一致")
            includes(amount, f"实际采用订阅月份{price.months} 月", "实际计价月份显示不一致")
            includes(amount, f"{format_cny_minor(price.base_price_minor)} 元", "受控订阅基价显示不一致")
            require(price.formula in amount, "服务端原算式未逐字显示。")
        else:
            includes(amount, "仅有单价，未计算总价", "单价被误表示为已计算总价")
    if business.intent in {"knowledge", "unit_price"}:
        require("match" not in sections and "requirements" not in sections,
                "插问把旧选型结果冒充成本轮新评估；原任务可通过独立摘要保留。")
    current_task = getattr(business, "current_task", None)
    if current_task is not None:
        task = current_task.model_dump(mode="json") if hasattr(current_task, "model_dump") else current_task
        task_visible, task_all = area("current-task"), area("current-task", True)
        requirements = task["requirements"]
        includes(task_all, requirements["requirement_id"], "当前任务摘要没有原需求身份")
        includes(task_all, f"修订 {requirements['revision']}", "当前任务摘要对应不同需求修订")
        for item in requirements.get("items", []):
            includes(task_visible, item["statement"], "插问期间的原任务需求被隐藏或丢失")
        for parameter in requirements.get("parameters", []):
            require(any(normalized(value) in normalized(task_visible) for value in
                        ([f"{parameter['value']} 席"] if parameter["name"] == "paid_seats" else
                         [f"{parameter['value']} 月", f"{parameter['value']} 个月"])), "当前任务摘要没有保留原参数")
        if task.get("match"):
            selected = task["match"].get("selected_plan_id")
            includes(task_visible, plan_name(selected), "当前任务摘要丢失采用套餐")
        if task.get("pricing"):
            includes(task_visible, format_cny_minor(task["pricing"]["base_price_minor"]),
                     "插问金额替换了原任务金额")
        for question in task.get("questions", []):
            includes(task_visible, question, "当前任务未解决项不能直接找到")
        if task["status"] == "needs_reassessment":
            require(any(value in task_visible for value in ("重新", "待核对", "尚未")),
                    "过期评估被当前任务摘要误当成有效结果。")
        if task.get("last_result") == "not_updated":
            require(any(value in task_visible for value in ("本轮没有更新", "保留此前", "未更新")),
                    "原任务旧评估被摘要冒充本轮已经更新。")
    if evidence:
        source_text, source_all = area("sources"), area("sources", True)
        current_ids = {record["result"]["data"]["retrieval_id"] for record in result.tool_results
                       if record["name"] == "search_knowledge" and record["result"].get("ok") is True}
        for identity, record in evidence.items():
            includes(source_all, identity, "实际检索身份不能展开核对")
            coded(source_all, record.status, "检索状态不能按需核对")
            if identity not in current_ids:
                require(any(value in source_all for value in ("不代表本轮新检索", "不宣称本轮新检索", "前序证据", "已取得")),
                        "前序检索被误表示为当轮新调用")
            for hit in record.hits:
                source = hit.source
                require(source.raw_text in source_all, "可展开来源原文未逐字保留。")
                for value in (source.source_id, source.dataset_version,
                              f"{source.relative_path}:{source.line_start}–{source.line_end}"):
                    includes(source_all, value, "实际来源、原文或定位没有在可展开区域保留")
            if record.error:
                includes(source_all, record.error.code, "检索故障不能按需核对")
                includes(observation["visible"], record.error.message, "受影响的检索故障原因被折叠或遗漏")
            for conflict in record.conflicts:
                includes(observation["visible"], conflict.reason, "已知相关资料冲突被折叠或遗漏")
                for source_id in conflict.source_ids:
                    includes(source_all, source_id, "冲突来源不能按需核对")
    for answer in business.knowledge_answers:
        includes(area("knowledge", True), answer["query"], "知识引用对应了不同检索问题")
        includes(area("knowledge", True), answer["retrieval_id"], "知识引用的检索身份丢失")
        if answer["answerability"] == "insufficient":
            require(any(value in observation["visible"] for value in ("没有说明", "未说明", "未规定", "没有回答", "不足")),
                    "资料不足被误表示为已经回答。")
        sources = {h.source.source_id: h.source for h in evidence[answer["retrieval_id"]].hits}
        for citation in answer["citations"]:
            require(citation["source_id"] in sources and citation["quote"] in sources[citation["source_id"]].raw_text, "实际引用不属于本次返回原文。")
            require(citation["quote"] in area("knowledge", True), "实际引用原文不能逐字按需核对。")
    tools_all = area("tools", True)
    if not result.tool_results:
        includes(tools_all, "本轮没有工具调用记录", "没有工具的回合伪造了调用轨迹")
    cursor = 0
    decoder = json.JSONDecoder()
    for record in result.tool_results:
        includes(tools_all, record["display_name"], "实际工具名称不能展开核对")
        call_id = tools_all.find(record["tool_call_id"], cursor)
        require(call_id >= cursor, "DOM 工具记录的调用身份或顺序与实际响应不同。")
        args_marker = tools_all.find("必要调用参数（安全展示）", call_id)
        args_start = tools_all.find("{", args_marker)
        require(args_marker >= 0 and args_start >= 0, "DOM 缺少真实安全调用参数。")
        arguments, args_length = decoder.raw_decode(tools_all[args_start:])
        require(arguments == record["display_arguments"], "DOM 回放了原始未校验参数，或安全参数不同。")
        result_marker = tools_all.find("实际结果／错误（受控展示摘要）", args_start + args_length)
        result_start = tools_all.find("{", result_marker)
        require(result_marker >= 0 and result_start >= 0, "DOM 缺少真实安全结果摘要。")
        displayed, result_length = decoder.raw_decode(tools_all[result_start:])
        require(displayed == record["display_result"], "DOM 回放了原始未校验结果，或结果摘要不同。")
        cursor = result_start + result_length
    summary = f"[{observation['label']}] {result.status}"
    if business.match:
        summary += f"；最低满足档 {plan_name(business.match.lowest_plan_id)}；采用 {plan_name(business.match.selected_plan_id)}"
    if business.pricing:
        summary += "；" + business.pricing.formula
    if business.requirements.condition_assertions and business.intent == "selection":
        summary += "；前提自述 " + "、".join(f"{a.condition_id}={a.value}" for a in business.requirements.condition_assertions)
    return summary


def check(report_path, observations_path):
    report = read_json(report_path)
    recorded = read_json(observations_path)
    sidecar = recorded if isinstance(recorded, dict) else None
    catalog, index = validate_report_identity(report, sidecar)
    if sidecar is not None:
        recorded = sidecar.get("observations")
    require(isinstance(recorded, list) and recorded, "没有实际浏览器 DOM 观察记录。")
    cases = {}
    for case in report["cases"]:
        result, evidence = validate_case(case, catalog, index)
        require(result.business.turn_id not in cases, "两个实际回合复用了本轮 ID。")
        cases[result.business.turn_id] = result, evidence
    seen, summaries = set(), []
    for observation in recorded:
        ids = ([observation.get("turnId")] if observation.get("uiContract") == REACT_UI_CONTRACT else
               [identity for identity in cases if identity in observation.get("allText", "")])
        require(len(ids) == 1, f"{observation.get('label', '未命名观察')}：DOM 本轮 ID 不属于此正常运行，或混合了多轮卡片。")
        identity = ids[0]
        require(identity in cases, "实际 DOM 响应身份不属于此正常运行。")
        result, evidence = cases[identity]
        retrospective = observation.get("captureMode") == "retrospective-live-message"
        expected_current = current_at_capture(report, observation) if retrospective else None
        summaries.append(validate_view(result, observation, catalog, evidence,
                                       current_expected=expected_current, current_from_record=retrospective))
        seen.add(identity)
    require(seen == set(cases), f"实际浏览器记录尚缺 {len(set(cases) - seen)} 个响应的页面核对；接口成功不能代替页面。")
    return summaries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="默认选择最新非空真实正常 browser-*.json")
    parser.add_argument("--observations", type=Path, default=ROOT / "data/ui_review/browser-observations.json")
    args = parser.parse_args()
    try:
        report_path = args.report or latest_report(ROOT / "data/ui_review")
        summaries = check(report_path, args.observations)
        for summary in summaries:
            print(summary)
        print(f"报告：{report_path.resolve()}\nDOM 记录：{args.observations.resolve()}")
        print("本次记录中的 API 响应与实际页面映射核对通过。")
    except (AssertionError, ValueError, KeyError, OSError, TypeError, KnowledgeError) as error:
        print(f"页面证据核对未通过：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
