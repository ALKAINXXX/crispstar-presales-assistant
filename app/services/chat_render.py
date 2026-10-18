"""One task-oriented formal answer for the page, storage and later history."""
from datetime import datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pydantic import TypeAdapter
from app.schemas.chat import AnswerPresentation
from app.schemas.pricing import Months, PaidSeats, format_cny_minor
from app.services.catalog import get_catalog

REASONS = {
    "catalog_unsupported": "产品目录明确不支持", "unmodeled_requirement": "现有资料无法确认",
    "ambiguous_requirement": "需求含义尚需澄清", "condition_unknown": "当前使用前提待确认",
    "condition_not_met": "当前不具备使用前提", "condition_evidence_missing": "使用前提的资料依据不足",
    "condition_evidence_conflict": "相关条件资料存在已知冲突", "condition_evidence_version_mismatch": "条件资料版本无效",
    "condition_evidence_association_mismatch": "条件资料关联无效", "evidence_not_in_chain": "依据不属于当前任务",
    "retrieval_timeout": "资料查询超时", "retrieval_failed": "资料查询失败",
}

# Derive display limits from the strict service types rather than a second
# hardcoded pricing policy. They are input limits, not product capacity.
PRICE_INPUT_RANGES = {
    "paid_seats": ("付费席位", "席", TypeAdapter(PaidSeats).json_schema()),
    "months": ("订阅月份", "个月", TypeAdapter(Months).json_schema()),
}

FEEDBACK_FIELDS = {"paid_seats": "付费席位", "months": "订阅期限",
    "selected_plan_id": "套餐选择", "requirements": "功能需求",
    "saml_sso_compatible_idp": "当前 SSO 前提", "pricing": "计价请求", "candidate": "本轮信息"}
FEEDBACK_ISSUES = {
    "uncertain": "这项事实还不确定，不能作为新的确定事实",
    "approximate": "数量为约数，尚不能作为新的确定购买值",
    "ambiguous": "这项表述有多个可能含义，需要澄清",
    "invalid_type": "这项输入类型不合法；付费席位和月份须为整数",
    "invalid_range": "这项输入超出项目允许的输入范围；该范围不表示产品容量或商业政策",
    "not_actual_purchase": "公司人数或计划尚不能作为实际购买事实",
    "conditional": "购买仍依赖尚未成立的条件，不能独立接受购买参数",
    "future": "未来计划尚不能作为当前已具备的事实",
    "question": "问句没有确认执行这项修改",
    "quoted": "引述没有确认这是你的当前购买事实",
    "negative": "原话没有肯定这项候选事实",
    "candidate_disagreement": "系统提取与原话核对仍有实质分歧，尚未完成准确识别",
    "conflict": "终值相互冲突，不能任取其中一个",
    "target_unknown": "尚未核定这项修改属于哪个任务，不会修改猜测的对象",
    "reference_missing": "系统未能核对本轮原话出处，尚未完成接收",
    "unknown_identity": "尚未核定候选的目录或服务端对象身份",
    "dependency_unresolved": "同组前置项尚未解决，本组修改没有提交",
    "extraction_failed": "系统没有完成本轮信息提取",
    "detail_uncovered": "原话中的硬细节尚未完整核对，不能确认全部满足要求",
}


def _retained_value(field, previous):
    if field in {"paid_seats", "months"} and type(previous) is int:
        return f"仍保留此前核定的 {previous}{' 席' if field == 'paid_seats' else ' 个月'}。"
    if field == "saml_sso_compatible_idp" and previous in {"yes", "no", "unknown"}:
        return "此前自述仍为" + {"yes": "具备", "no": "不具备", "unknown": "不确定"}[previous] + "。"
    if field == "selected_plan_id" and previous in {"basic", "team", "business"}:
        return "仍保留此前核定的 " + previous.capitalize() + "。"
    return ""


def _operation_feedback(business):
    """Keep complete service verdicts for the existing disclosure.

    This does not inspect model candidates or infer a target from the current
    price/selection slots. A prior value is usable only with a known target.
    """
    feedback = []
    for item in getattr(business, "operation_feedback", ()):
        if type(item) is not dict or item.get("status") not in {"accepted", "rejected", "blocked"}:
            continue
        status = item["status"]
        field = item.get("field", "candidate")
        label = FEEDBACK_FIELDS.get(field, "本轮信息")
        detail = item.get("message")
        if type(detail) is not str or not detail.strip():
            detail = FEEDBACK_ISSUES.get(item.get("issue"), label + "已完成核对" if status == "accepted" else label + "尚未完成接收")
        detail = detail.strip().rstrip("。；")
        prefix = ("已核对并保留" if item.get("retained") else "已接受") if status == "accepted" else "未更新" if status == "rejected" else "依赖阻塞，尚未更新"
        target = {"selection": "当前选型的", "price": "独立计价的"}.get(item.get("target"), "")
        quotes = list(dict.fromkeys(span["quote"] for span in item.get("spans", ())
            if type(span) is dict and type(span.get("quote")) is str and span["quote"]))
        subject = target + label
        if quotes:
            subject += "（原话：“" + "”；“".join(quotes) + "”）"
        text = prefix + "：" + subject + "。" + detail + "。"
        if status != "accepted" and item.get("target") in {"selection", "price"} and item.get("issue") != "target_unknown":
            text += _retained_value(field, item.get("previous_value"))
        feedback.append({"group_id": str(item.get("group_id", "")), "status": status,
            "target": item.get("target", "none"), "field": field, "issue": item.get("issue"), "message": text})
    return feedback


def _feedback_impacts(business, *, include_retained=True):
    """Unaccepted input remains visible without replaying the receipt list."""
    parts = []
    for item in getattr(business, "operation_feedback", ()):
        if item.get("status") not in {"rejected", "blocked"}:
            continue
        field = item.get("field", "candidate")
        label = FEEDBACK_FIELDS.get(field, "本轮信息")
        quotes = list(dict.fromkeys(span["quote"] for span in item.get("spans", ())
            if type(span) is dict and type(span.get("quote")) is str and span["quote"]))
        if field == "candidate":
            # An atomic multi-field group has no single field or prior value.
            # Verified spans identify affected input without guessing fields.
            if quotes:
                label = "本组信息（“" + "”；“".join(quotes) + "”）"
        detail = item.get("message") or FEEDBACK_ISSUES.get(item.get("issue"), "尚未完成接收")
        if item.get("issue") == "invalid_range" and field in PRICE_INPUT_RANGES:
            _, unit, limits = PRICE_INPUT_RANGES[field]
            detail = detail.rstrip("。；") + f"；请提供 {limits['minimum']}～{limits['maximum']} {unit}之间的整数"
        target = {"selection": "当前选型的", "price": "独立计价的"}.get(item.get("target"), "")
        rejected = ("“" + "”；“".join(quotes) + "”没有被接受；") if quotes and field != "candidate" else ""
        parts.append(rejected + target + label + "未更新：" + detail.rstrip("。；") + "。" + (
            _retained_value(field, item.get("previous_value"))
            if (include_retained or field not in {"paid_seats", "months"}) and item.get("target") in {"selection", "price"}
            and item.get("issue") != "target_unknown" else ""))
    return list(dict.fromkeys(parts))


def _parameter_changes(business):
    """Describe changes from accepted before/after facts, never user prose."""
    requirements = getattr(business, "requirements", None)
    params = {p.name: p.value for p in requirements.parameters} if requirements else {}
    changes = []
    for item in getattr(business, "operation_feedback", ()):
        field, previous = item.get("field"), item.get("previous_value")
        if (item.get("status") != "accepted" or item.get("target") != business.intent
                or field not in {"paid_seats", "months"} or type(previous) is not int):
            continue
        unit = "席" if field == "paid_seats" else "个月"
        if field not in params:
            changes.append(f"{FEEDBACK_FIELDS[field]}已从 {previous} {unit}清为未知")
        elif previous != params[field]:
            changes.append(f"{FEEDBACK_FIELDS[field]}从 {previous} {unit}改为 {params[field]} {unit}")
    return list(dict.fromkeys(changes))

def pending_questions(state, *, price_requested: bool):
    pending, questions = [], []
    req = state.requirements
    match = state.last_match if state.last_match and state.last_match.requirements == req else None
    if state.intent == "selection":
        if not req.items:
            pending.append("requirements")
            questions.append("你有哪些必须满足的功能或硬约束？")
        for item in req.items:
            if item.kind == "ambiguous":
                pending.append("clarify:" + item.requirement_id)
                questions.append(f"“{item.statement}”有歧义，请说明具体要求。")
        # A known unsupported/unknown hard requirement cannot be solved by
        # collecting price parameters or repeatedly asking to drop it.
        permanent_block = bool(match and any(
            all(any(row.requirement_id == item.requirement_id and
                    set(row.reason_codes) & {"catalog_unsupported", "unmodeled_requirement"}
                    for row in plan.items) for plan in match.plans)
            for item in req.items))
        assertions = {a.condition_id: a.value for a in req.condition_assertions}
        if not permanent_block and any(i.feature_id == "saml_sso" for i in req.items):
            for condition in get_catalog().conditions:
                if assertions.get(condition.condition_id, "unknown") == "unknown":
                    pending.append("condition:" + condition.condition_id)
                    questions.append("你目前是否已经具备兼容 SAML SSO 的身份提供方？可以回答具备、不具备或不确定。")
    # When only the user's applicable prerequisite is missing, quantities can
    # be supplied together. An unsupported, unknown or evidence-blocked plan
    # cannot be repaired by collecting quantities.
    can_price = state.intent == "price" or bool(match and (
        match.status == "matched" or any(plan.items and all(
            row.status == "supported" or (row.status == "pending_condition"
                and row.condition_evidence_status == "valid"
                and set(row.reason_codes) <= {"condition_unknown"})
            for row in plan.items) for plan in match.plans)))
    collect_partial_inputs = (state.intent == "selection" and bool(req.parameters)
        and getattr(state, "review_collect_purchase_inputs", False))
    if (price_requested or collect_partial_inputs) and can_price:
        params = {p.name: p.value for p in req.parameters}
        if "paid_seats" not in params:
            pending.append("paid_seats")
            questions.append("实际准备购买多少个付费席位？")
        if "months" not in params:
            pending.append("months")
            questions.append("明确订阅几个月（1～12 月）？")
        if state.intent == "price" and req.selected_plan_id is None:
            pending.append("plan")
            questions.append("明确计算哪个套餐？")
    return tuple(pending), questions

def _label(item):
    return item.statement.removeprefix("必须支持 ")

def _decision(match, names):
    if match.status == "comparison_only":
        return ["还不能推荐套餐：目前只有背景或订阅信息，尚未明确必须满足的功能。"]
    if match.selected_plan_id:
        if match.selection_reason == "user_selected":
            parts = [f"你选择的{names[match.selected_plan_id]}满足当前要求；最低满足档是{names[match.lowest_plan_id]}。"]
        else:
            parts = [f"建议采用{names[match.lowest_plan_id]}，这是满足当前要求的最低档。"]
    elif match.selection_reason == "selected_plan_not_satisfying":
        requested = names.get(match.requirements.selected_plan_id, "你指定的套餐")
        parts = [f"{requested}不满足当前要求，尚未采用；原要求已保留。"]
    else:
        parts = ["当前没有能够确认完整满足要求的套餐。"]
    items = {i.requirement_id: i for i in match.requirements.items}
    reasons = {}
    for plan in match.plans:
        for row in plan.items:
            if row.status != "supported":
                key = (_label(items[row.requirement_id]), tuple(row.reason_codes))
                reasons.setdefault(key, []).append(names[plan.plan_id])
    for (label, codes), plans in reasons.items():
        # With a satisfying plan, explain only the decisive lower-tier gap.
        # The comparison owns every individual support state and its detail.
        if match.selected_plan_id and not any(code == "catalog_unsupported" for code in codes):
            continue
        reason = "、".join(REASONS.get(code, code) for code in codes)
        if codes == ("catalog_unsupported",):
            parts.append(f"{'、'.join(plans)}不支持{label}。")
        else:
            parts.append(f"{'、'.join(plans)}的“{label}”：{reason or '尚不能确认满足'}。")
    # Eligible alternatives and chosen-versus-minimum identities belong to the
    # result sheet, not another repeated prose list before the same table.
    if match.requirements.condition_assertions:
        conditions = {c.condition_id: c.name for c in get_catalog().conditions}
        values = {"yes": "当前具备", "no": "当前不具备", "unknown": "尚不确定"}
        parts.append("当前前提：" + "；".join(
            f"{conditions[a.condition_id]}——用户自述{values[a.value]}"
            for a in match.requirements.condition_assertions) + "。助手未验证实际身份系统。")
    return parts

def _price_text(price, *, include_formula=True):
    amount = (f"按 {price.plan_name}、{price.paid_seats} 个付费席位和 {price.months} 个月计算，"
        f"订阅基价为 **{format_cny_minor(price.base_price_minor)} 元**。")
    detail = "\n\n" + price.formula if include_formula else ""
    return amount + detail


def _priced_selection(business):
    """Integrate accepted quantity edits with their actual resulting price."""
    price = business.pricing
    previous = {item.get("field"): item.get("previous_value")
        for item in getattr(business, "operation_feedback", ())
        if item.get("status") == "accepted" and item.get("target") == "selection"
        and item.get("field") in {"paid_seats", "months"} and type(item.get("previous_value")) is int}
    seat_change = "paid_seats" in previous and previous["paid_seats"] != price.paid_seats
    term_change = "months" in previous and previous["months"] != price.months
    if not seat_change and not term_change:
        return _price_text(price, include_formula=False)
    seats = (f"付费席位从 {previous['paid_seats']} 席改为 {price.paid_seats} 席"
        if seat_change else f"付费席位为 {price.paid_seats} 席")
    term = (f"订阅期限从 {previous['months']} 个月改为 {price.months} 个月"
        if term_change else f"订阅期限为 {price.months} 个月")
    return (f"当前 {price.plan_name} 方案的{seats}，{term}，"
        f"订阅基价为 **{format_cny_minor(price.base_price_minor)} 元**。")


def _confirmation_text(proposal):
    if getattr(proposal, "save_available", True):
        return "尚未保存，请核对下方的内部草案摘要，再点击“确认并保存草案”。"
    # Legacy read-only objects carry their own notice and never gain a save
    # invitation merely because a Proposal is present in the response.
    return proposal.notice


def _question_text(questions):
    """Present only this turn's applicable service questions, once."""
    # Earlier controlled responses may carry the old generic population
    # warning. Actual accepted background is expressed separately below.
    return "".join(question.replace("公司人数不会自动作为席位。", "") for question in questions)


def _population_note(business):
    """Use only accepted background quotations; never infer buying seats."""
    quotes = list(getattr(business.requirements, "context_quotes", ()))
    for item in getattr(business, "operation_feedback", ()):
        if item.get("status") == "accepted" and item.get("field") == "candidate":
            quotes.extend(span["quote"] for span in item.get("spans", ())
                if type(span) is dict and type(span.get("quote")) is str)
    population = list(dict.fromkeys(quote for quote in quotes
        if re.search(r"(?:公司|团队|员工|我们).*(?:人|人数)", quote)))
    return "“" + "”；“".join(population) + "”没有作为付费席位采用。" if population else ""


def _scope_changes(business):
    changes = []
    additions = {change.removeprefix("已增加硬需求：") for change in business.changes
        if change.startswith("已增加硬需求：")}
    # Revision can also advance when background is accepted. Existing scope
    # is established only by unchanged current items or explicit withdrawals.
    had_scope = (any(item.statement not in additions for item in business.requirements.items)
        or any(change.startswith(("已删除", "已撤回", "已澄清")) for change in business.changes)) if additions else False
    for change in business.changes:
        if change.startswith("已增加"):
            # The first comparison already expresses the accepted scope.
            # Later additions must identify what this turn actually changed.
            if had_scope:
                changes.append(_human_change(change).replace("已增加硬需求：必须支持 ", "已加入需求："))
        elif change.startswith(("已删除", "已撤回", "已取消", "已澄清")):
            changes.append(_human_change(change))
    return "；".join(changes).rstrip("。；") + "。" if changes else ""


def _selection_answer(business, names, show_comparison, questions, failures):
    """Compose the selection's conclusion, effect and necessary question."""
    match = business.match
    paragraphs = []
    decision = _decision(match, names)
    scope = _scope_changes(business)
    reasons = [part for part in decision[1:] if not part.startswith("当前前提：")]
    assertions = [part for part in decision[1:] if part.startswith("当前前提：")]
    if match.selected_plan_id:
        if show_comparison:
            paragraphs.append(scope + decision[0] + "".join(reasons))
        elif scope:
            paragraphs.append(scope)
    else:
        paragraphs.append(scope + decision[0])
        if reasons:
            paragraphs.append("\n".join("- " + reason for reason in reasons))
    if business.pricing:
        result = _priced_selection(business)
        if business.proposal:
            result += _confirmation_text(business.proposal)
        else:
            result += "尚未保存；本轮没有可提交的保存对象。"
        if scope and show_comparison and paragraphs:
            paragraphs[-1] += result
        else:
            paragraphs.append(result)
    else:
        changes = _parameter_changes(business)
        params = {p.name: p.value for p in business.requirements.parameters}
        adopted = []
        changed_fields = {item.get("field") for item in getattr(business, "operation_feedback", ())
            if item.get("status") == "accepted" and item.get("target") == "selection"
            and type(item.get("previous_value")) is int}
        if "paid_seats" in params and "paid_seats" not in changed_fields:
            adopted.append(f"付费席位采用 {params['paid_seats']} 席")
        if "months" in params and "months" not in changed_fields:
            adopted.append(f"订阅期限已采用 {params['months']} 个月")
        purchase = "；".join([*changes, *adopted]).rstrip("。；")
        missing = [FEEDBACK_FIELDS[field] for field in ("paid_seats", "months") if field in getattr(business, "pending", ())]
        if match.selected_plan_id and missing:
            purchase += ("；" if purchase else "") + "暂不能计算订阅总价，还缺实际购买的" + "、".join(missing)
        if purchase:
            purchase += "。"
        if "paid_seats" in getattr(business, "pending", ()):
            purchase += _population_note(business)
        if failures:
            purchase += "".join(failures)
        if _unresolved_price_edit(business):
            fields = list(dict.fromkeys(FEEDBACK_FIELDS.get(item.get("field"), "相关要求")
                for item in getattr(business, "operation_feedback", ())
                if item.get("status") in {"rejected", "blocked"} and item.get("target") == "selection"))
            purchase += "由于" + "、".join(fields or ["本轮相关要求"]) + "的修改尚未确定，还没有计算出新的订阅基价；本轮没有可提交的保存对象。"
        if questions:
            purchase += ("请补充：" if any(p.startswith("review:") for p in getattr(business, "pending", ())) else "") + _question_text(questions)
        if purchase:
            paragraphs.append(purchase)
        if not match.selected_plan_id and match.status != "comparison_only":
            blocked = "当前不能确认完整适配或准备可保存方案。"
            if not questions:
                blocked += "请明确尚未解决的要求或前提；如需调整硬要求，请说明调整内容。"
            paragraphs.append(blocked)
    if assertions:
        paragraphs.append("".join(assertions))
    if getattr(business, "selection_unchanged", False):
        paragraphs.append("已重新评估；功能要求、套餐选择和计价参数没有变化。")
    return paragraphs


def _unresolved_price_edit(business):
    """A rejected edit blocks its dependent price, not accepted old values."""
    errors = business.errors
    return bool(business.match and not business.pricing
        and any((error.get("details") or {}).get("issue") == "review_dependency" for error in errors)
        and any(error.get("code") == "pricing_not_completed" for error in errors))

def _summary(requirements, match, price, names):
    parts = []
    selected = match.selected_plan_id if match else requirements.selected_plan_id
    parts.append("选择：" + (names[selected] if selected else "尚未确定"))
    if match and requirements.selected_plan_id and match.selected_plan_id is None:
        parts.append("用户指定：" + names[requirements.selected_plan_id] + "（未采用）")
    if match and match.lowest_plan_id:
        parts.append("最低满足档：" + names[match.lowest_plan_id])
        alternatives = [names[p] for p in match.eligible_plan_ids if p != match.lowest_plan_id]
        if alternatives:
            parts.append("其他满足档：" + "、".join(alternatives))
        parts.append("选择依据：" + ("用户主动选择" if match.selection_reason == "user_selected" else "按最低满足档"))
    params = {p.name: p.value for p in requirements.parameters}
    parts.append("参数：" + "；".join((
        f"{params['paid_seats']} 个付费席位" if "paid_seats" in params else "付费席位未明确",
        f"{params['months']} 个月" if "months" in params else "订阅期限未明确")))
    if price:
        parts.append("订阅基价：" + format_cny_minor(price.base_price_minor) + " 元")
        parts.append("算式：" + price.formula)
        parts.append("本项目不计算税、折扣、实施费等")
    elif params:
        parts.append("当前没有有效总价")
    if requirements.condition_assertions:
        values = {"yes": "具备", "no": "不具备", "unknown": "不确定"}
        parts.append("前提自述：" + "、".join(values[a.value] for a in requirements.condition_assertions))
    return "；".join(parts) + "。"

def _human_change(change):
    change = re.sub(r"已明确 paid_seats=(\d+)；原话：.*", r"付费席位更新为 \1 席", change)
    change = re.sub(r"已明确 months=(\d+)；原话：.*", r"订阅期限更新为 \1 个月", change)
    change = change.replace("前提用户自述：yes", "当前前提：用户明确具备").replace(
        "前提用户自述：no", "当前前提：用户明确不具备").replace(
        "前提用户自述：unknown", "当前前提：用户尚不确定")
    for plan in get_catalog().plans:
        change = change.replace("用户指定档：" + plan.plan_id, "用户指定档：" + plan.name)
    return change

def _error_text(error):
    """Short user-facing impact, while full safe diagnostics stay available."""
    details = error.get("details") or {}
    if details.get("scope") == "pricing" and error.get("code") == "invalid_arguments":
        field = details.get("field")
        previous = details.get("previous_value")
        if field in PRICE_INPUT_RANGES and type(previous) is int:
            label, unit, limits = PRICE_INPUT_RANGES[field]
            lower, upper = limits["minimum"], limits["maximum"]
            if not lower <= previous <= upper:
                return (f"当前已记录 {previous} {unit}，超出计价允许范围（{lower}～{upper} {unit}），"
                    f"未生成订阅基价。请提供范围内的{label}。")
        return "计价参数未通过校验，尚未生成新的订阅基价；已接受信息保留，请修正计价参数。"
    if details.get("scope") == "candidate_patch":
        label = FEEDBACK_FIELDS.get(details.get("field"), "这项信息")
        previous = details.get("previous_value")
        retained = "其他已接受信息保留。"
        if type(previous) is int:
            retained = f"仍为 {previous}{' 席' if details.get('field') == 'paid_seats' else ' 个月'}；其他已接受信息保留。"
        elif previous in {"yes", "no", "unknown"}:
            retained = "此前自述仍为" + {"yes": "具备", "no": "不具备", "unknown": "不确定"}[previous] + "。"
        if details.get("issue") == "reference_missing":
            return f"系统未能正确接收你的表述；{label}尚未更新。候选原话出处未核定。{retained}"
        issue = details.get("issue")
        reasons = {"parameter_not_explicit": "尚未核定实际购买数量或期限；公司人数、计划、约数和假设不能补成确定购买参数",
            "condition_unclear": "当前前提尚不确定，需要明确具备、不具备或不确定",
            "condition_not_current": "未来计划不能作为当前已具备的前提",
            "condition_undirected": "尚未核定答复对应的待答条件",
            "condition_not_compatible": "有身份系统尚不能证明它目前与本产品兼容",
            "condition_contradiction": "系统候选与原话的肯否不一致，尚未完成准确识别",
            "selection_not_explicit": "尚未核定本轮主动选择的套餐与方向",
            "removal_unconfirmed": "原话尚未确认撤回已有需求",
            "removal_target_mismatch": "撤回对象尚未核定，不会删除其他需求",
            "unreviewed_mutation": "后续工具包含未经本轮核对的新事实，该修改没有执行",
            "review_not_completed": "本轮原话语义核对尚未完成，依赖修改没有执行",
            "invalid_field": "候选的类型、字段或输入范围未通过校验；这是系统接收校验失败，请核对具体提示"}
        reason = reasons.get(issue, FEEDBACK_ISSUES.get(issue, "系统未完成这项输入的准确接收"))
        return f"{label}没有更新：{reason}。{retained}"
    return {
        "semantic_review_failed": "本轮原话语义核对未完成，尚未提交新业务事实；此前有效事实和结果保留，旧未提交保存许可已失效。",
        "semantic_review_timeout": "本轮原话语义核对超时，尚未提交新业务事实；此前有效事实和结果保留。等待超时不表示后台工作已经结束，旧未提交保存许可已失效。",
        "semantic_review_parse_failed": "系统没有取得可解析的原话核对结果，尚未提交新业务事实；此前有效事实和结果保留，旧未提交保存许可已失效。",
        "semantic_review_unavailable": "本轮原话语义核对未完成，尚未提交新业务事实；此前有效事实和结果保留，旧未提交保存许可已失效。",
        "semantic_review_invalid": "系统没有取得有效的逐组核对结果，尚未提交新业务事实；此前有效事实和结果保留，旧未提交保存许可已失效。",
        "semantic_review_request_invalid": "独立核对请求未通过本地格式或预算校验，尚未派发独立核对，未提交新业务事实；此前有效事实和结果保留。",
        "semantic_review_output_exhausted": "独立核对输出达到本次额度，未取得完整核对结果；未提交新业务事实，此前有效事实和结果保留。",
        "semantic_review_candidate_invalid": "模型生成的首候选参数未通过格式或字段校验，尚未进行独立核对与业务执行；此前有效事实和结果保留。",
        "semantic_review_facts_invalid": "核对候选没有通过服务端事实校验，对应新事实没有提交；此前有效事实保留。",
        "semantic_review_bypass_blocked": "后续工具试图引入未经本轮核对的新事实，该修改没有执行；已核定状态保留。",
        "semantic_review_required": "尚未取得已核对的本轮候选，依赖业务工具没有执行；旧有效事实保留。",
        "quote_query_scope_mismatch": "工具编号与本轮已核定的查回对象不一致，没有读取其他草案。请核对本轮需要查询的编号。",
        "extraction_failed": "系统没有完成本轮信息提取，尚未提交新业务事实；此前有效事实和结果保留。",
        "quote_in_trash": "这份草案当时保存成功，目前在最近删除。请打开数据管理，在最近删除中恢复该草案后读取。",
        "quote_permanently_deleted": "这份草案此前保存成功，目前已永久删除，正文无法读取或恢复。旧保存动作不会重新创建它。",
        "quote_not_found": "当前记录中未找到该编号；不能据此判断它从未保存。请核对编号和当前数据位置。",
        "model_unavailable": "暂时未完成回答，已接受的信息和取得的结果保留，可以稍后重试。",
        "matching_not_completed": "需求已记录，但还没有完成评估；请重新评估后再采用推荐或金额。",
        "pricing_not_completed": "席位和期限已明确，但还没有计算出新的订阅基价。",
        "knowledge_not_completed": "暂时没有完成有原文依据的回答，可以稍后重试。",
        "knowledge_explanation_invalid": "本次知识解释的引用或关联未通过校验，相关回答尚未完成；已取得的其他结果保留。",
        "knowledge_search_scope_invalid": "本次资料查询的咨询事项关联未通过校验，相关查询尚未执行。",
        "consultation_review_incomplete": "本轮核对未完整提交咨询范围及覆盖结果；已接受的业务修改和实际结果保留，咨询部分尚未完成。",
        "catalog_not_completed": "暂时没有取得套餐单价，可以稍后重试。",
        "invalid_arguments": "这项输入未被接受，已接受信息保留；请明确需要修改的那一项。",
        "invalid_business_input": "这项输入未被接受，已接受信息保留；请明确需要修改的那一项。",
        "reference_repair_changed_candidate": "这次未能准确提取你的需求，尚未更新；此前已接受的信息保留。",
        "history_write_unconfirmed": "会话记录写入未确认；回答中的结果保留，但刷新后可能看不到这条消息。",
        "tool_failed": "这次处理没有完成，已取得的其他结果保留，可以稍后重试。",
    }.get(error.get("code"), error["message"])


def _comparison_text(match, names):
    states = {"supported": "支持", "unsupported": "不支持", "unknown": "未知", "pending_condition": "前提待确认"}
    rows = []
    for requirement in match.requirements.items:
        cells = []
        for plan in match.plans:
            row = next(item for item in plan.items if item.requirement_id == requirement.requirement_id)
            reason = "、".join(REASONS.get(code, code) for code in row.reason_codes)
            cells.append(names[plan.plan_id] + "：" + states[row.status] + ("（" + reason + "）" if reason else ""))
        rows.append("- " + _label(requirement) + " — " + "；".join(cells))
    return "\n".join(rows)


def _quote_saved_time(saved_at):
    # Windows may lack IANA data without an extra tzdata package. Beijing's
    # current UTC+8 offset also has an exact standard-library representation.
    try:
        beijing = ZoneInfo("Asia/Shanghai")
    except ZoneInfoNotFoundError:
        beijing = timezone(timedelta(hours=8))
    return datetime.fromisoformat(saved_at).astimezone(beijing).strftime("%Y-%m-%d %H:%M:%S") + "（北京时间）"


def _quote_answer_text(quote):
    """Visible saved facts, also used verbatim as the formal stored answer."""
    s = quote.snapshot
    p = s.pricing
    parts = [f"已查回已保存内部草案 {quote.quote_id}。以下内容来自保存时快照。",
        "保存时间：" + _quote_saved_time(quote.saved_at) + "。",
        "保存时需求：" + "、".join(_label(item) for item in s.requirements.items) + "。",
        f"最低满足档：{s.lowest_plan_name}；所选套餐：{s.selected_plan_name}。",
        f"{p.paid_seats} 个付费席位，{p.months} 个月；单价 {format_cny_minor(p.unit_price_minor)} 元／席／月；订阅基价 {format_cny_minor(p.base_price_minor)} 元。",
        "算式：" + p.formula,
        "这是内部草案的订阅基价；本项目不计算税、折扣、实施费等。"]
    source_status = getattr(quote, "source_conversation_status", "active")
    source_notice = {"trash": "原聊天在最近删除；这份独立草案仍保留。",
        "permanently_deleted": "原聊天已永久删除；这份独立草案仍保留。",
        "not_found": "当前记录中未找到原聊天；这份独立草案仍保留。"}.get(source_status)
    if source_notice:
        parts.append(source_notice)
    for condition in s.conditions:
        parts.append("使用前提：" + condition.statement + "；用户自述：“" + condition.user_quote + "”。")
    if s.conditions:
        parts.append("前提来自保存时的用户自述；助手没有验证实际身份系统。")
    if s.requirements.context_quotes:
        parts.append("保存时用户自述：" + "；".join(s.requirements.context_quotes) + "。")
    return "\n\n".join(parts)


def _consultation_citations(business):
    """Place accepted quotations beside their canonical answer units.

    Binding and evidence acceptance are service results. A shared explanation's
    verified rule quote belongs to that rule; its other quotes can accompany
    the other bound units. This does not classify original questions or infer
    new facts from quotation text. Source adoption retains tool receipt order.
    """
    coverage = getattr(business, "consultation_coverage", None)
    if coverage is None:
        return {}, set()
    by_item, consumed = {}, set()
    for answer_index, answer in enumerate(business.knowledge_answers):
        if "consultation_unit_ids" in answer:
            identities = set(answer.get("consultation_unit_ids") or ())
            bound = [item for item in coverage.items if item.item_id in identities]
        else:
            identities = set(answer.get("consultation_item_ids") or ())
            bound = [item for item in coverage.items
                     if identities & {item.item_id, *item.source_item_ids}]
        for citation_index, citation in enumerate(answer["citations"]):
            candidates = [item for item in bound
                          if answer["retrieval_id"] in item.retrieval_ids
                          and citation["source_id"] in item.source_ids]
            rule_owners = [item for item in candidates if any(
                rule.retrieval_id == answer["retrieval_id"]
                and rule.source_id == citation["source_id"]
                and rule.quote == citation["quote"] for rule in item.prerequisites)]
            # A known rule's distinct concept citation must not be moved to
            # that rule merely because a shared explain call bound both units.
            owners = rule_owners or [item for item in candidates if not item.prerequisites]
            if not owners:
                continue
            by_item.setdefault(owners[0].item_id, []).append(
                (answer_index, "> " + citation["quote"].replace("\n", "\n> ")))
            consumed.add((answer_index, citation_index))
    return by_item, consumed


def _consultation_answer(business, names, citations=None):
    """Direct current-turn answers, in accepted scope order, exactly once."""
    coverage = getattr(business, "consultation_coverage", None)
    if coverage is None:
        return []
    parts, priced, supported, shown_rules, introduced_answers = [], set(), set(), set(), set()
    if not coverage.scope_complete and not any(
            error.get("code") == "consultation_review_incomplete" for error in business.errors):
        parts.append("本轮咨询范围尚未完整核定；已取得的实际结果保留，尚不能确认全部问题已回答。")
    for item in coverage.items:
        if item.stage == "empty_scope":
            parts.append("本次没有剩余查询对象。")
            continue
        for price in item.unit_prices:
            identity = (price.product_id, price.plan_id)
            if identity not in priced:
                parts.append(f"{price.plan_name}的订阅单价为 **{format_cny_minor(price.unit_price_minor)} 元／席／月**。")
                priced.add(identity)
        order = {identity: index for index, identity in enumerate(item.plan_ids)}
        relationships = [("support", relation) for relation in item.support]
        relationships.extend(("prerequisite", rule) for rule in item.prerequisites)
        relationships.sort(key=lambda entry: order.get(entry[1].plan_id, len(order)))
        for kind, relation in relationships:
            if kind == "support":
                identity = (relation.product_id, relation.plan_id, relation.feature_id)
                if identity not in supported:
                    text = (relation.plan_name + ("支持" if relation.supported else "不支持")
                            + relation.feature_name + "。")
                    if relation.supported and relation.condition_id:
                        text += "使用仍须满足对应的适用前提。"
                    parts.append(text)
                    supported.add(identity)
            else:
                # Applicability and accepted original association are service
                # results. Inquiry plan_ids only order these verified rows.
                identity = (relation.product_id, relation.plan_id, relation.condition_id)
                if identity not in shown_rules:
                    parts.append(relation.plan_name + "的 " + relation.feature_name + " 使用前提：" + relation.statement)
                    shown_rules.add(identity)
        if citations and item.item_id in citations:
            for answer_index, quote in citations[item.item_id]:
                if answer_index not in introduced_answers:
                    parts.append("根据本次实际检索到的产品资料，原文说明：")
                    introduced_answers.add(answer_index)
                parts.append(quote)
        if item.limitation:
            parts.append(item.limitation)
        if item.status == "completed":
            continue
        topic = "“" + item.topic + "”"
        if item.stage == "extraction_incomplete":
            objects = "、".join(names.get(identity, identity) for identity in item.plan_ids)
            parts.append((objects + "的" if objects else "") + topic
                         + "本次范围核对未完成，相关回答尚未完成；已取得的实际回执保留。")
        elif item.status == "needs_input" and item.stage != "dependency_unresolved":
            objects = "、".join(names.get(identity, identity) for identity in item.plan_ids)
            parts.append((objects + "的" if objects else "") + topic + "咨询范围尚未核定，相关部分需要澄清；查询结果不能替代范围核定，尚不能确认这一事项已完成。")
        elif item.aspect == "unit_price":
            missing = "、".join(names.get(identity, identity) for identity in item.missing_plan_ids)
            parts.append((missing or topic) + ("的单价结果存在身份、目录版本或金额冲突，尚不能采用。"
                if item.stage == "result_conflict" else "的订阅单价查询失败，尚未取得本次结果。"
                if item.stage == "query_failed" else "的订阅单价尚未取得。"))
        elif item.stage == "query_failed":
            parts.append("本次" + topic + "资料查询失败，相关问题尚未回答。")
        elif item.stage == "explanation_pending":
            parts.append("已取得" + topic + "的检索原文，但基于本次引用的说明尚未完成。")
        elif item.stage == "explanation_rejected":
            missing = "、".join(names.get(identity, identity) for identity in item.missing_plan_ids)
            parts.append((missing + "的" if missing else "") + topic + "的解释引用未通过核对，相关回答尚未完成。")
        elif item.stage == "evidence_insufficient":
            parts.append("本次资料查询未取得足以说明" + topic + "的依据；不能据此认定产品不支持。")
        elif item.stage == "result_conflict":
            parts.append(topic + "的实际查询结果存在冲突，尚不能作出结论。")
        elif item.stage == "dependency_unresolved":
            parts.append(topic + "依赖的修改或新计价尚未完成；不能用此前结果代答。")
        else:
            if item.aspect == "support" and item.missing_plan_ids:
                missing = "、".join(names.get(identity, identity) for identity in item.missing_plan_ids)
                parts.append(missing + "的" + topic + "尚未取得实际目录支持记录。")
            else:
                parts.append(topic + "尚未取得实际查询和被接受的说明。")
    return parts


def organize_business(business, status: str):
    """Produce visible partitions and independently understandable history.

    Both projections use accepted service results. No browser sentence removal
    or second unbounded model explanation participates in this contract.
    """
    names = {} if business.intent == "quote_query" else {p.plan_id: p.name for p in get_catalog().plans}
    parts, sources, used = [], [], []
    task = business.current_task
    show_result = bool(task and task.result_turn_id == business.turn_id)
    show_comparison = bool(business.match is not None and getattr(business, "comparison_changed", None) is not False)
    feedback = _operation_feedback(business)
    questions = list(dict.fromkeys([*business.questions, *(task.questions if show_result else [])]))
    retained_selection = bool(business.intent == "selection" and task and not show_result
        and task.last_result == "not_updated" and task.status == "current")
    impacts = _feedback_impacts(business, include_retained=not retained_selection)
    parts.extend(impacts)
    failures = []
    if business.errors:
        visible_errors = [error for error in business.errors if not (_unresolved_price_edit(business)
            and (error.get("code") == "pricing_not_completed"
                or (error.get("details") or {}).get("issue") == "review_dependency"))]
        # The selection paragraph explains this particular dependency in terms
        # of its accepted and rejected groups. Other failures keep their own
        # reason and existing continuation rather than receiving a stock tail.
        failures.extend(dict.fromkeys(_error_text(e) for e in visible_errors))
        failures.extend(dict.fromkeys(e["retry_action"].replace("本轮", "这次") for e in visible_errors if e.get("retry_action")))
        failed_codes = {e.get("code") for e in business.errors}
        if "semantic_review_timeout" in failed_codes:
            failures.append("请等待本次后台处理结束，再重新发送尚未接收的信息或修改。")
        elif "extraction_failed" in failed_codes or any(
                code and code.startswith("semantic_review_") for code in failed_codes):
            failures.append("请稍后重新发送尚未接收的信息或修改；此前有效事实无需重述。")
    if not (business.match and not business.pricing):
        parts.extend(failures)
    if business.recovery_notice:
        parts.append(business.recovery_notice)
    if not business.match and not business.pricing:
        changes = _parameter_changes(business)
        if changes:
            parts.append("；".join(changes) + "。")
    for quote in business.queried_quotes:
        parts.append(_quote_answer_text(quote))
        sources.extend(f"{a.source.document_title} / {a.source.section_title}，{a.source.relative_path}:{a.source.line_start}–{a.source.line_end}"
            for a in quote.snapshot.adopted_sources)
    if business.match:
        parts.extend(_selection_answer(business, names, show_comparison, questions, failures))
        adopted = {sid for plan in business.match.plans for row in plan.items
                   if row.condition_evidence_status == "valid" for sid in row.evidence_source_ids}
        shown_conditions = set()
        for observation in business.match.retrieval_evidence:
            for hit in observation.hits:
                source = hit.source
                if source.source_id not in adopted:
                    continue
                used.append(source.source_id)
                for condition in get_catalog().conditions:
                    if (condition.condition_id in source.condition_ids
                            and condition.condition_id not in shown_conditions
                            and condition.statement in source.raw_text):
                        parts.append("使用前提：“" + condition.statement + "”")
                        shown_conditions.add(condition.condition_id)
                sources.append(f"{source.document_title} / {source.section_title}，{source.relative_path}:{source.line_start}–{source.line_end}")
    elif business.pricing:
        parts.append(_price_text(business.pricing))
        parts.append("这是指定套餐的独立计价，还没有评估它是否满足你的需求，也不取得保存许可。"
            "本项目不计算税、折扣、实施费等。")
    consultation = getattr(business, "consultation_coverage", None)
    inquiry_units = getattr(business, "unit_prices", ())
    collection_price_scope = bool(consultation and any(item.aspect == "unit_price"
        and item.status != "needs_input" for item in consultation.items))
    consultation_citations, consumed_citations = _consultation_citations(business)
    parts.extend(_consultation_answer(business, names, consultation_citations))
    if consultation is None and inquiry_units and not business.pricing and not business.match:
        parts.extend(f"{p.plan_name}的订阅单价为 **{format_cny_minor(p.unit_price_minor)} 元／席／月**。" for p in inquiry_units)
    if consultation is None and business.unit_price and not business.pricing and not inquiry_units:
        p = business.unit_price
        if not business.match:
            parts.append(f"{p.plan_name}的订阅单价为 **{format_cny_minor(p.unit_price_minor)} 元／席／月**。")
    if business.catalog and not business.unit_price and consultation is None and not inquiry_units:
        parts.append("当前套餐：" + "；".join(f"{p.name} {format_cny_minor(p.unit_price_minor)} 元／席／月" for p in business.catalog.plans) + "。")
    for answer_index, answer in enumerate(business.knowledge_answers):
        if answer["answerability"] == "insufficient" and consultation is None:
            parts.append("现有资料不足以回答这个具体问题；这不表示产品明确不支持。")
        evidence = next(e for e in business.retrieval_evidence if e.retrieval_id == answer["retrieval_id"])
        originals = {h.source.source_id: h.source for h in evidence.hits}
        if (not any(index == answer_index for index, _ in consumed_citations)
                and answer["citations"]):
            parts.append("根据本次实际检索到的产品资料，原文说明：")
        for citation_index, citation in enumerate(answer["citations"]):
            # Clear short quotations remain exact actually retrieved text.
            if (answer_index, citation_index) not in consumed_citations:
                parts.append("> " + citation["quote"].replace("\n", "\n> "))
            source = originals[citation["source_id"]]
            used.append(source.source_id)
            sources.append(f"{source.document_title} / {source.section_title}，{source.relative_path}:{source.line_start}–{source.line_end}")
    cited = {a["retrieval_id"] for a in business.knowledge_answers}
    for evidence in business.retrieval_evidence:
        if evidence.status == "unavailable":
            parts.append("资料查询失败，相关解释尚不能确认；仍可查询单价或进行不依赖该资料的独立计价。")
        elif (consultation is None and evidence.retrieval_id not in cited
                and business.intent == "knowledge" and not business.knowledge_answers):
            if evidence.status == "retrieved":
                parts.append("已取得本次检索原文，但有引用依据的解释尚未完成；不能将执行中断说成资料未说明。")
            else:
                parts.append("本次查询尚未取得足以回答具体问题的相关依据，暂时不能作出产品承诺。")
    # Removed requirements no longer occur in the comparison. Retain their
    # impact in the answer as well as parameter changes above.
    if not business.match:
        scope = _scope_changes(business)
        if scope:
            parts.append(scope)
    if business.intent == "selection":
        if task and task.last_result == "not_updated":
            if not show_result and task.status == "current":
                if (business.errors and not impacts) or any(
                        item.get("status") in {"rejected", "blocked"} and item.get("target") == "selection"
                        for item in getattr(business, "operation_feedback", ())):
                    # State the actual retained facts with their old identity,
                    # including a rejected field-level edit. This amount is
                    # never attributed to a new calculation or save object.
                    params = {p.name: p.value for p in task.requirements.parameters}
                    retained = []
                    if task.match and task.match.selected_plan_id:
                        retained.append(names[task.match.selected_plan_id])
                    if "paid_seats" in params:
                        retained.append(f"{params['paid_seats']} 席")
                    if "months" in params:
                        retained.append(f"{params['months']} 个月")
                    if task.pricing:
                        retained.append("订阅基价 " + format_cny_minor(task.pricing.base_price_minor) + " 元")
                    if retained:
                        retained_text = ("此前评估仍有效。此前保留的结果：" + "、".join(retained) +
                            "；不是本轮新计算的结果，可通过“查看当前结果”继续查看。")
                        if len(impacts) == 1:
                            # A rejected field or atomic group and its actual
                            # retained result can be understood together.
                            parts[0] += retained_text
                        else:
                            parts.append(retained_text)
                    else:
                        parts.append("此前评估仍有效；这次没有改变当前结果，也没有生成新金额，可通过“查看当前结果”继续查看。")
                else:
                    parts.append("此前评估仍有效；这次没有改变当前结果，也没有生成新金额，可通过“查看当前结果”继续查看。")
        if task and task.status == "needs_reassessment":
            parts.append("当前需求需要重新评估，旧金额已不再作为当前结果。")
    elif business.intent == "price" and not business.pricing:
        parts.append("独立计价：" + _summary(business.requirements, None, None, names))
    if business.intent != "selection" and business.current_task:
        if show_result:
            parts.append("选型需求已更新，需要重新评估；旧金额不再有效。")
    if questions and not (business.match and not business.pricing):
        parts.append(_question_text(questions))
    if business.intent == "unsupported_action":
        parts.append("这个操作尚未开放。可以咨询产品、选择套餐、计算订阅基价，核对方案后在页面确认保存，或按编号查回已存草案。")
    if getattr(business, "proposal", None) and (not business.match or not business.pricing):
        parts.append(_confirmation_text(business.proposal))
    if not parts:
        parts.append("可以询问产品功能、使用前提或套餐单价，也可以说明需求来选择套餐。")
    answer = "\n\n".join(dict.fromkeys(parts))
    presentation = AnswerPresentation(answer_markdown=answer, show_comparison=show_comparison,
        show_current_result=show_result and not getattr(business, "proposal", None),
        used_source_ids=list(dict.fromkeys(used)), feedback=feedback)
    formal = [answer]
    if show_comparison and business.match.requirements.items:
        formal.append("需求比较：\n\n" + _comparison_text(business.match, names))
    if task and (show_result or getattr(business, "proposal", None)):
        if not show_comparison and task.requirements.items:
            formal.append("已接受需求：" + "、".join(_label(i) for i in task.requirements.items) + "。")
        if not business.match:
            formal.append(_summary(task.requirements, task.match, task.pricing, names))
        elif task.match and task.match.lowest_plan_id:
            alternatives = [names[p] for p in task.match.eligible_plan_ids if p != task.match.lowest_plan_id]
            if alternatives:
                formal.append("其他满足档：" + "、".join(alternatives) + "。")
        if business.unit_price and not task.pricing and not collection_price_scope:
            formal.append(f"{business.unit_price.plan_name}单价为 {format_cny_minor(business.unit_price.unit_price_minor)} 元／席／月。")
    if business.match and not show_comparison and business.match.lowest_plan_id:
        formal.append("选型依据：最低满足档为 " + names[business.match.lowest_plan_id] + "；" +
            ("当前套餐由用户选择。" if business.match.selection_reason == "user_selected" else "按最低满足档选择。"))
    if business.match and business.pricing:
        # The page's concrete confirmation object shows these details once.
        # Formal text keeps them so a later plain history retains its facts.
        formal.append(("内部草案核对算式：" if getattr(business, "proposal", None) else "订阅基价算式：") + business.pricing.formula)
    if business.pricing and business.match:
        formal.append("金额口径：这是内部草案的订阅基价；本项目不计算税、折扣、实施费等。"
            if getattr(business, "proposal", None) else "金额口径：订阅基价；本项目不计算税、折扣、实施费等。")
    elif (collection_price_scope or (business.intent == "unit_price" and inquiry_units)
          or (business.unit_price and business.intent in {"unit_price", "selection", "price"})) and not business.pricing:
        formal.append("单价口径：订阅基价；本项目不计算税、折扣、实施费等。")
    if sources:
        formal.append("采用依据：" + "；".join(dict.fromkeys(sources)) + "。")
    return presentation, "\n\n".join(formal)


def render_business(business, status: str) -> str:
    """Compatibility entry point for the exact formal stored answer."""
    return organize_business(business, status)[1]

