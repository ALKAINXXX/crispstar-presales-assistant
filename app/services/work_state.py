"""Process-local business state and the shared single-process order boundary.

Text history is deliberately not a recovery source for authority. A restarted
process asks for current requirements again. Evidence stays with its issuer.
"""

import asyncio
import json
import re
from dataclasses import dataclass, field, replace
from uuid import uuid4

from app.config import Settings
from app.schemas.business import ConsultationItem, INPUT_REJECTIONS, RejectedInputDetails, RequirementPatch
from app.schemas.matching import HardRequirement, RequirementSet
from app.schemas.matching import ConditionAssertion, DeclaredParameter
from app.schemas.semantic import SemanticReview, SourceSpan
from app.services.catalog import get_catalog
from app.services.consultation_scope import accept_consultation_scope, selection_task_question
from app.services.matching import match_requirements
from app.services.knowledge import KnowledgeError
from app.services.retrieval import KnowledgeService


def _number(text: str) -> int | None:
    if re.fullmatch(r"\d+", text):
        return int(text)
    digits = {c: i for i, c in enumerate("零一二三四五六七八九")}
    digits["两"] = 2
    if re.search(r"[一二两三四五六七八九][零一二两三四五六七八九]", text):
        return None
    total = part = 0
    for char in text:
        if char in digits:
            part = digits[char]
        elif char in "十百":
            total += (part or 1) * (10 if char == "十" else 100)
            part = 0
        else:
            return None
    return total + part if text else None


def _quote_contexts(message: str, quote: str) -> tuple[str, ...]:
    """Check the actual clause, including qualifiers outside a model substring.

    This is a bounded guard for explicit numbers/the one condition, not a
    replacement for model meaning. Separate clauses may accept several facts.
    """
    contexts = []
    for found in re.finditer(re.escape(quote), message):
        start, end = found.span()
        left = re.search(r"[^，,；;。！？!?\n]*$", message[:start]).group()
        right = re.match(r"[^，,；;。！？!?\n]*", message[end:]).group()
        contexts.append(left + quote + right)
    return tuple(contexts)


def _parameter_text_supported(parameter, pending: tuple[str, ...], quote: str) -> bool:
    if re.search(r"约|大概|左右|可能|计划|以后|未来|假设|如果|负|半|至少|至多|超过|少于|不超过|到|至|[~～]|不是|不买|不购买|不订阅|不要|不需要", quote):
        return False
    number = r"([0-9零一二两三四五六七八九十百]+)"
    if parameter.name == "paid_seats":
        pattern = number + r"\s*(?:个)?\s*(?:付费席位|席位|席)"
        multiplier = 1
    else:
        pattern = number + r"\s*(?:个)?\s*(个月|月|年)"
        multiplier = None
    for match in re.finditer(pattern, quote):
        # Do not accept the integer suffix in 1.5席 or -80席.
        if match.start() and quote[match.start() - 1] in ".-−负0123456789":
            continue
        value = _number(match.group(1))
        factor = multiplier if multiplier else (12 if match.group(2) == "年" else 1)
        if value is not None and value * factor == parameter.value:
            return True
    # Bare number only answers a single explicitly pending numeric question.
    return pending == (parameter.name,) and _number(quote) == parameter.value


def _parameter_supported(parameter, pending: tuple[str, ...], message: str) -> bool:
    contexts = _quote_contexts(message, parameter.user_quote.strip())
    return any(_parameter_text_supported(parameter, pending, text.strip()) for text in contexts)


class InputRejected(ValueError):
    def __init__(self, details: RejectedInputDetails):
        self.details = details.model_dump(mode="json")
        self.retry_action = INPUT_REJECTIONS[details.issue][1]
        super().__init__(INPUT_REJECTIONS[details.issue][0])


@dataclass
class TaskContext:
    requirements: RequirementSet = field(default_factory=lambda: RequirementSet(requirement_id=uuid4().hex))
    pending: tuple[str, ...] = ()
    # A rejected numeric edit can be followed by the requested correction.
    # Keep its question separate from accepted facts and the old result.
    clarification_pending: tuple[str, ...] = ()
    questions: tuple[str, ...] = ()
    match: object | None = None
    pricing: object | None = None
    # Server execution receipt, separate from the independently usable Price.
    pricing_requirements: RequirementSet | None = None
    pricing_match: object | None = None
    wants_price: bool = False
    pricing_basis: str | None = None
    selected_plan_origin: str | None = None
    last_result: str = "not_updated"
    result_turn_id: str | None = None
    result_message_id: str | None = None
    # New source locations/background do not replace evidence-bound facts.
    source_notes: tuple[dict, ...] = ()


@dataclass
class WorkState:
    settings: Settings
    requirements: RequirementSet = field(default_factory=lambda: RequirementSet(requirement_id=uuid4().hex))
    intent: str = "general"
    active_intent: str | None = None
    pending: tuple[str, ...] = ()
    last_match: object | None = None
    evidence_bindings: dict = field(default_factory=dict)
    changes: tuple[str, ...] = ()
    wants_price: bool = False
    pricing_basis: str | None = None
    selected_plan_origin: str | None = None
    has_processed_turn: bool = False
    _working_intent: str | None = None
    _selection_context: TaskContext | None = None
    _price_context: TaskContext | None = None
    _last_price_receipt: tuple | None = None
    review_locked: bool = False
    review_complete: bool = False
    review_blockers: tuple[str, ...] = ()
    review_preserve_results: bool = False
    review_quote_id: str | None = None
    review_quote_ids: tuple[str, ...] = ()
    review_requested_execution: tuple[str, ...] = ()
    # A current accepted purchase parameter can make its missing counterpart
    # actionable without turning a functional selection into a price request.
    review_collect_purchase_inputs: bool = False
    operation_feedback: tuple[dict, ...] = ()
    source_notes: tuple[dict, ...] = ()
    # Cleared at the next turn; these never enter either persistent task.
    turn_consultations: tuple[ConsultationItem, ...] = ()
    consultation_scope_reviewed: bool = False
    consultation_contract_missing: bool = False
    consultation_complete: bool = False
    consultation_feedback: tuple[dict, ...] = ()
    consultation_questions: tuple[dict, ...] = ()
    task_answer_questions: tuple[dict, ...] = ()

    def __post_init__(self):
        self.knowledge = KnowledgeService(settings=self.settings)

    def prompt_state(self, *, recheck_current=True) -> dict:
        current = self.current_task(recheck=recheck_current)
        selection = None
        if current is not None:
            req = current["requirements"]
            selection = {"status": current["status"], "last_result": current["last_result"],
                "requirement_id": req["requirement_id"], "revision": req["revision"],
                "selected_plan_id": current["match"]["selected_plan_id"] if current["match"] else req["selected_plan_id"],
                "pending": current["pending"],
                "clarification_pending": list(self._selection_context.clarification_pending)}
            if self._working_intent != "selection":
                # Keep enough identity for a later one-item correction, without
                # duplicating full Sources, Match matrices or user history.
                selection["requirements"] = {"items": [{key: item[key] for key in ("requirement_id", "kind", "feature_id")} |
                    {"statement": item["statement"][:200]} for item in req["items"]],
                    "parameters": [{"name": p["name"], "value": p["value"]} for p in req["parameters"]],
                    "condition_assertions": [{"condition_id": a["condition_id"], "value": a["value"]} for a in req["condition_assertions"]]}
            if current["pricing"]:
                selection["pricing"] = {key: current["pricing"][key] for key in ("plan_id", "paid_seats", "months", "base_price_minor")}
        working = (self._selection_context if self._working_intent == "selection" else
                   self._price_context if self._working_intent == "price" else None)
        return {"requirements": self.requirements.model_dump(mode="json"),
                "intent": self.intent, "active_intent": self.active_intent,
                "pending": self.pending, "wants_price": self.wants_price,
                "clarification_pending": list(working.clarification_pending) if working else [],
                "current_selection": selection,
                "independent_pricing": {"selected_plan_id": self._price_context.requirements.selected_plan_id,
                    "clarification_pending": list(self._price_context.clarification_pending),
                    "parameters": [{"name": p.name, "value": p.value} for p in self._price_context.requirements.parameters]} if self._price_context else None,
                "source_notes": list(self.source_notes[-12:]),
                "semantic_review": {"complete": self.review_complete,
                    "blockers": list(self.review_blockers), "preserve_results": self.review_preserve_results},
                "turn_consultations": [item.model_dump(mode="json") for item in self.turn_consultations],
                "consultation_scope_reviewed": self.consultation_scope_reviewed,
                "consultation_contract_missing": self.consultation_contract_missing,
                "consultation_complete": self.consultation_complete,
                "consultation_feedback": list(self.consultation_feedback),
                "consultation_questions": list(self.consultation_questions),
                "task_answer_questions": list(self.task_answer_questions),
                "operation_feedback": list(self.operation_feedback),
                "recovery": "process_local; history is not accepted requirements"}

    def _capture_context(self):
        context = self._selection_context if self._working_intent == "selection" else self._price_context if self._working_intent == "price" else None
        if context is not None:
            if context.requirements != self.requirements:
                context.pricing = None
                context.pricing_requirements = context.pricing_match = None
                context.questions = ()
                context.last_result = "not_updated"
            context.requirements = self.requirements
            context.pending = self.pending
            context.match = self.last_match
            context.wants_price = self.wants_price
            context.pricing_basis = self.pricing_basis
            context.selected_plan_origin = self.selected_plan_origin
        return context

    def _reject(self, issue: str, field_name: str = "candidate", *, scope="candidate_patch", requirements=None):
        requirements = self.requirements if requirements is None else requirements
        previous = None
        if field_name in {"paid_seats", "months"}:
            previous = next((p.value for p in requirements.parameters if p.name == field_name), None)
        elif field_name == "selected_plan_id":
            previous = requirements.selected_plan_id
        elif field_name == "saml_sso_compatible_idp":
            previous = next((a.value for a in requirements.condition_assertions if a.condition_id == field_name), None)
        raise InputRejected(RejectedInputDetails(issue=issue, field=field_name, previous_value=previous, scope=scope))

    def remember_result(self, match, price, pending, questions, *, turn_id=None, selection_changed=False):
        """Record real selected-task facts; unrelated price turns cannot replace them."""
        context = self._capture_context()
        if self._selection_context is not None and turn_id is not None and (
                selection_changed or (self.intent == "selection" and match is not None)):
            # Accepted mutations during a knowledge detour also invalidate an
            # old assessment, regardless of that turn's inquiry classification.
            self._selection_context.result_turn_id = turn_id
            self._selection_context.result_message_id = None
        if self.intent != "selection" or context is None:
            return
        if match is not None and match.requirements == self.requirements:
            context.match = self.last_match = match
            context.last_result = "updated"
        else:
            context.last_result = "not_updated"
        if price is not None:
            self.validate_price(price.plan_id, price.paid_seats, price.months)
            context.pricing = price
            receipt = (self.requirements, self.last_match, price)
            if self._last_price_receipt == receipt:
                context.pricing_requirements, context.pricing_match = receipt[:2]
            else:
                context.pricing_requirements = context.pricing_match = None
        elif context.match is None or context.match.status != "matched" or not self.wants_price:
            context.pricing = None
        context.pending, context.questions = tuple(pending), tuple(questions)

    def record_pricing_result(self, price) -> None:
        """Called only after the thin Tool verifies its real pricing execution.

        Merely assigning equal amounts to a different requirement revision is
        insufficient. Proposal creation requires this exact current Match/Price pair.
        """
        self._last_price_receipt = ((self.requirements, self.last_match, price)
            if self.intent == "selection" else None)

    def bind_result_message(self, turn_id: str, message_id: str) -> None:
        """Associate the actual committed row with this process-owned result."""
        context = self._selection_context
        if context is not None and context.result_turn_id == turn_id:
            context.result_message_id = message_id

    def current_task(self, *, recheck=True) -> dict | None:
        self._capture_context()
        context = self._selection_context
        if context is None:
            return None
        match, pricing = context.match, context.pricing
        if recheck and match is not None:
            try:
                # Explicit IDs mean no new retrieval, even if the tuple is empty.
                checked = match_requirements(context.requirements, knowledge=self.knowledge,
                    retrieval_ids=tuple(e.retrieval_id for e in match.retrieval_evidence))
                if checked != match:
                    raise ValueError("Saved task evidence changed")
            except Exception:
                match = pricing = context.match = context.pricing = None
                if self._working_intent == "selection":
                    self.last_match = None
        if recheck and pricing is not None:
            params = {p.name: p.value for p in context.requirements.parameters}
            if (match is None or match.status != "matched" or match.selected_plan_id != pricing.plan_id
                    or params.get("paid_seats") != pricing.paid_seats or params.get("months") != pricing.months
                    or pricing.catalog_version != get_catalog().version or not context.wants_price):
                pricing = context.pricing = None
        return {"kind": "selection", "status": "current" if match is not None else "needs_reassessment",
            "requirements": context.requirements.model_dump(mode="json"),
            "match": match.model_dump(mode="json") if match else None,
            "pricing": pricing.model_dump(mode="json") if pricing else None,
            "pending": list(context.pending), "questions": list(context.questions), "last_result": context.last_result,
            "result_turn_id": context.result_turn_id, "result_message_id": context.result_message_id}

    def apply_patch(self, raw: dict, message: str) -> dict:
        if self.review_locked:
            return self._reviewed_patch_noop(raw, message)
        patch = RequirementPatch.model_validate_json(json.dumps(raw, ensure_ascii=False))
        catalog = get_catalog()
        feature_ids = {f.feature_id for f in catalog.features}
        condition_ids = {c.condition_id for c in catalog.conditions}
        self._capture_context()
        target_intent = patch.intent if patch.intent in {"selection", "price"} else self._working_intent
        switching = target_intent != self._working_intent and target_intent is not None
        target = self._selection_context if target_intent == "selection" else self._price_context if target_intent == "price" else None
        # Before the first selection, old price inputs may be reused for an
        # explicitly continued task. Once selection exists, inquiry is separate.
        if switching and target is None:
            target = TaskContext() if target_intent == "price" and self._selection_context is not None else TaskContext(
                requirements=self.requirements, pending=self.pending, wants_price=self.wants_price,
                pricing_basis=self.pricing_basis, selected_plan_origin=self.selected_plan_origin)
        old = target.requirements if switching else self.requirements
        old_pending = target.pending if switching else self.pending
        old_wants_price = target.wants_price if switching else self.wants_price
        old_pricing_basis = target.pricing_basis if switching else self.pricing_basis
        quotes = [(x.user_quote, "requirements") for x in (*patch.add, *patch.remove)]
        quotes += [(p.user_quote, p.name) for p in patch.parameters]
        quotes += [(a.user_quote, "saml_sso_compatible_idp") for a in patch.conditions]
        quotes += [(q, "candidate") for q in patch.context_quotes]
        if patch.selection_quote is not None:
            quotes.append((patch.selection_quote, "selected_plan_id"))
        if patch.pricing_quote is not None and not (old_wants_price and patch.pricing_quote == old_pricing_basis):
            quotes.append((patch.pricing_quote, "pricing"))
        if patch.cancel_pricing_quote is not None:
            quotes.append((patch.cancel_pricing_quote, "pricing"))
            if patch.pricing_quote is not None or patch.intent == "price" or not re.search(
                r"只比较|只选型|不计价|不算|不计算|不用计算|不要计算|无需计价|取消计价|不用.*(?:价格|金额|费用)", patch.cancel_pricing_quote):
                self._reject("cancellation_unconfirmed", "pricing", requirements=old)
        for quote, field_name in quotes:
            if not quote.strip() or quote not in message:
                self._reject("reference_missing", field_name, requirements=old)
        pending = tuple(p for p in old_pending if p not in {"paid_seats", "months", "plan"}) if patch.cancel_pricing_quote is not None else old_pending
        items = list(old.items)
        changes = []
        for removal in patch.remove:
            removal_target = next((i for i in items if i.requirement_id == removal.requirement_id), None)
            if removal_target is None or not re.search(r"不再|删除|取消|去掉|不要|不用|不需要|移除|放弃", removal.user_quote):
                self._reject("removal_unconfirmed", "requirements", requirements=old)
            feature = next((f for f in catalog.features if f.feature_id == removal_target.feature_id), None)
            labels = {removal_target.statement, removal_target.user_quote}
            if feature:
                labels.update((feature.name, feature.feature_id, feature.name.split()[-1]))
                labels.update({"saml_sso": ("SSO", "统一登录"), "api_access": ("API",),
                               "audit_logs": ("审计",), "data_export": ("导出",),
                               "project_collaboration": ("项目协作", "任务协作"),
                               "private_deployment": ("私有化",)}.get(feature.feature_id, ()))
            if not any(label.lower() in removal.user_quote.lower() for label in labels):
                self._reject("removal_target_mismatch", "requirements", requirements=old)
            items.remove(removal_target)
            changes.append("已删除硬需求：" + removal_target.statement)
        for candidate in patch.add:
            if candidate.kind == "feature" and candidate.feature_id not in feature_ids:
                self._reject("unknown_feature", "requirements", requirements=old)
            if candidate.kind != "feature" and candidate.feature_id is not None:
                self._reject("requirement_kind_mismatch", "requirements", requirements=old)
            # Candidate prose is not a business fact. Known capability wording
            # belongs to Catalog; unresolved wording belongs to the verified
            # actual user quote. Never carry a model's extra price/save claim
            # into controlled answers, persisted text or subsequent history.
            statement = ("必须支持 " + next(f.name for f in catalog.features if f.feature_id == candidate.feature_id)
                         if candidate.kind == "feature" else candidate.user_quote)
            item = HardRequirement(requirement_id=uuid4().hex,
                                   **(candidate.model_dump() | {"statement": statement}))
            if any((i.kind, i.feature_id, i.statement if i.feature_id is None else None) ==
                   (item.kind, item.feature_id, item.statement if item.feature_id is None else None) for i in items):
                continue
            items.append(item)
            changes.append("已增加硬需求：" + item.statement)
        params = {p.name: p for p in old.parameters}
        for parameter in patch.parameters:
            if not _parameter_supported(parameter, pending, message):
                self._reject("parameter_not_explicit", parameter.name, requirements=old)
            params[parameter.name] = parameter
            changes.append(f"已明确 {parameter.name}={parameter.value}；原话：{parameter.user_quote}")
        assertions = {a.condition_id: a for a in old.condition_assertions}
        for assertion in patch.conditions:
            if assertion.condition_id not in condition_ids:
                self._reject("condition_unknown_identity", "saml_sso_compatible_idp", requirements=old)
            quote = assertion.user_quote.strip()
            if assertion.value != "unknown":
                contexts = _quote_contexts(message, quote)
                actual = "；".join(contexts)
                if quote in {"好的", "好", "ok", "OK", "嗯", "可以"} or (assertion.value == "yes" and re.search(r"计划|以后|未来|将来|准备", actual)):
                    self._reject("condition_not_current", "saml_sso_compatible_idp", requirements=old)
                if re.search(r"不知道|不确定|尚不清楚|是否|可能|能否|不一定|有待|兼不兼容", actual):
                    self._reject("condition_unclear", "saml_sso_compatible_idp", requirements=old)
                directed = bool(re.search(r"身份|IdP|IDP|idp|兼容|SAML", quote))
                directed |= pending == ("condition:" + assertion.condition_id,)
                # A short exact quote can point to the only conditional
                # question through explicit condition wording in this turn.
                directed |= (tuple(p for p in pending if p.startswith("condition:")) ==
                             ("condition:" + assertion.condition_id,) and
                             bool(re.search(r"兼容.*(?:身份|IdP|IDP|idp)|(?:身份|IdP|IDP|idp).*兼容", message)))
                if not directed:
                    self._reject("condition_undirected", "saml_sso_compatible_idp", requirements=old)
                negative = bool(re.search(r"不具备|没有|尚未|不兼容|未具备|还没", actual))
                positive = bool(re.search(r"具备|已有|兼容|有|是的|已部署", quote))
                if assertion.value == "yes" and "兼容" not in quote and quote not in {"是的", "有", "有的", "具备", "已具备"}:
                    self._reject("condition_not_compatible", "saml_sso_compatible_idp", requirements=old)
                if (assertion.value == "yes" and (negative or not positive)) or (assertion.value == "no" and not negative):
                    self._reject("condition_contradiction", "saml_sso_compatible_idp", requirements=old)
            assertions[assertion.condition_id] = assertion
            changes.append(f"前提用户自述：{assertion.value}（{quote}）")
        selected = old.selected_plan_id
        origin = target.selected_plan_origin if switching else self.selected_plan_origin
        if patch.intent == "selection" and origin == "price":
            selected, origin = None, None
            changes.append("已按当前硬需求重新选型。")
        if patch.selection_quote is not None and patch.intent not in {"unit_price", "knowledge", "general"}:
            if patch.selected_plan_id is None:
                if not re.search(r"取消|不指定|自动|最低|不再", patch.selection_quote):
                    self._reject("selection_not_explicit", "selected_plan_id", requirements=old)
                selected = None
                origin = None
            else:
                plan = next((p for p in catalog.plans if p.plan_id == patch.selected_plan_id), None)
                if plan is None or not any(label.lower() in patch.selection_quote.lower()
                                           for label in (plan.plan_id, plan.name.split()[0])):
                    self._reject("selection_not_explicit", "selected_plan_id", requirements=old)
                selected = plan.plan_id
                origin = patch.intent
            changes.append("用户指定档：" + (selected or "取消指定，按最低满足档"))
        elif patch.selected_plan_id is not None and patch.selection_quote is None:
            self._reject("selection_not_explicit", "selected_plan_id", requirements=old)
        # A pure lookup's question/plan mention is not a selection revision.
        # Keep explicit mutations during a detour, but never invalidate the
        # active assessment just because a model recorded the lookup wording.
        accepts_context = patch.intent in {"selection", "price"} or any((patch.add, patch.remove,
            patch.parameters, patch.conditions, patch.cancel_pricing_quote))
        context_candidates = patch.context_quotes if accepts_context else ()
        selection_context = (patch.selection_quote,) if patch.selection_quote is not None and patch.intent not in {"knowledge", "unit_price", "general"} else ()
        contexts = tuple(dict.fromkeys((*old.context_quotes, *context_candidates,
            *((message,) if patch.conditions else ()),
            *selection_context)))[-12:]
        data = dict(items=tuple(items), parameters=tuple(params.values()),
                    condition_assertions=tuple(assertions.values()), selected_plan_id=selected, context_quotes=contexts)
        if len(items) > 24 or len(json.dumps(data, default=lambda x: x.model_dump(mode="json"), ensure_ascii=False)) > 14_000:
            self._reject("state_budget", "requirements", requirements=old)
        changed = any(getattr(old, k) != v for k, v in data.items())
        if switching:
            self.requirements, self.pending = old, target.pending
            self.last_match = target.match
            self.wants_price, self.pricing_basis = target.wants_price, target.pricing_basis
            self.selected_plan_origin = target.selected_plan_origin
            if target_intent == "selection":
                self._selection_context = target
            else:
                self._price_context = target
            self._working_intent = target_intent
        if changed:
            self.requirements = RequirementSet(requirement_id=old.requirement_id,
                revision=old.revision + 1, previous_revision=old.revision, **data)
            self.last_match = None
        self.intent = patch.intent
        if patch.intent in {"selection", "price"}:
            self.active_intent = patch.intent
        self.selected_plan_origin = origin
        if patch.intent == "price" or patch.pricing_quote is not None:
            self.wants_price = True
            if patch.pricing_quote is not None:
                self.pricing_basis = patch.pricing_quote
        if patch.cancel_pricing_quote is not None:
            self.wants_price = False
            self.pricing_basis = None
            self.pending = pending
            changes.append("已取消计价；已有明确参数保留。")
        self.changes = tuple(changes)
        self._capture_context()
        return self.prompt_state() | {"changes": self.changes}

    def begin_review_turn(self) -> None:
        """One turn has one acceptance boundary, including a failed review.

        The existing session lock owns this object. This adds no competing lock
        and does not recover authority from stored text.
        """
        self._capture_context()
        self.review_locked = True
        self.review_complete = False
        self.review_blockers = ()
        self.review_preserve_results = False
        self.review_quote_id = None
        self.review_quote_ids = ()
        self.review_requested_execution = ()
        self.review_collect_purchase_inputs = False
        self.operation_feedback = ()
        self.turn_consultations = ()
        self.consultation_scope_reviewed = False
        self.consultation_contract_missing = False
        self.consultation_complete = False
        self.consultation_feedback = ()
        self.consultation_questions = ()
        self.task_answer_questions = ()
        self.changes = ()

    @staticmethod
    def _locate_span(span: SourceSpan, message: str) -> dict:
        start, end, quote = span.start, span.end, span.quote
        if start is not None and end is not None and end > start and message[start:end] == quote:
            return {"quote": quote, "start": start, "end": end}
        positions = [match.start() for match in re.finditer(re.escape(quote), message)]
        if len(positions) != 1:
            raise ValueError("reference_missing")
        return {"quote": quote, "start": positions[0], "end": positions[0] + len(quote)}

    def _consultation_scopes(self, review, message):
        # The source inventory and acceptance share one finite interpretation;
        # no per-item scope interpreter can reject or enlarge it afterwards.
        scopes, feedback, questions = accept_consultation_scope(review, message, self._locate_span)
        self.consultation_questions = questions
        return scopes, feedback

    @staticmethod
    def _operation_field(operation) -> str:
        if operation.field is not None:
            return operation.field
        if operation.action in {"add", "remove", "clarify", "replace"}:
            return "requirements"
        if operation.action in {"select_plan", "cancel_selection"}:
            return "selected_plan_id"
        if operation.action in {"request_pricing", "cancel_pricing"}:
            return "pricing"
        return "candidate"

    @staticmethod
    def _previous(requirements, field_name):
        if requirements is None:
            return None
        if field_name in {"paid_seats", "months"}:
            return next((p.value for p in requirements.parameters if p.name == field_name), None)
        if field_name == "selected_plan_id":
            return requirements.selected_plan_id
        if field_name == "saml_sso_compatible_idp":
            return next((a.value for a in requirements.condition_assertions if a.condition_id == field_name), None)
        return None

    @staticmethod
    def _review_message(issue: str, accepted=False) -> str:
        if accepted:
            return "本组已核对并接受；未改动的已确认信息保留。"
        return {
            "not_actual_purchase": "本组是人口或背景，未更新实际购买参数。",
            "uncertain": "本组事实尚未确定，未更新已确认值。",
            "approximate": "本组数量为约数，未更新实际购买数量。",
            "ambiguous": "本组含义或对应关系仍有歧义，尚未更新。",
            "conditional": "本组购买依赖尚未成立的条件，未记为确定购买事实。",
            "future": "本组是未来计划，未记为当前确定事实。",
            "quoted": "本组是引述，未记为用户当前购买事实。",
            "question": "本组是问句，未执行对应业务修改。",
            "negative": "本组没有肯定提出这项要求，未新增该要求。",
            "invalid_type": "本组实际输入不是所需整数，未取整或更新。",
            "invalid_range": "本组数值超出项目输入范围，未截断或更新。",
            "candidate_disagreement": "系统对本组事实、终值或目标理解不一致，尚未更新。",
            "conflict": "本组终值冲突，未任取其中一个。",
            "target_unknown": "本组目标任务无法核定，未写入旧任务。",
            "reference_missing": "本组原话位置无法核查，尚未更新。",
            "unknown_identity": "本组对象或目录身份无效，尚未更新。",
            "dependency_unresolved": "本组依赖尚未确定，尚未执行。",
            "detail_uncovered": "本轮硬要求细节尚未全部核对，未生成新的完整适配结论。",
            "extraction_failed": "系统尚未完成本组提取核对，未提交新的业务事实。",
        }.get(issue, "本组尚未完成核对，未更新；旧有效事实保留。")

    def _reviewed_patch_noop(self, raw: dict, message: str) -> dict:
        """Later tools can consume the review, never introduce another fact."""
        if not self.review_complete:
            self._reject("review_not_completed")
        patch = RequirementPatch.model_validate_json(json.dumps(raw, ensure_ascii=False))
        if patch.intent in {"selection", "price"} and patch.intent != self._working_intent:
            self._reject("unreviewed_mutation")
        params = {p.name: p.value for p in self.requirements.parameters}
        conditions = {a.condition_id: a.value for a in self.requirements.condition_assertions}
        if (patch.remove or any(params.get(p.name) != p.value for p in patch.parameters)
                or any(conditions.get(a.condition_id) != a.value for a in patch.conditions)
                or (patch.selected_plan_id is not None and patch.selected_plan_id != self.requirements.selected_plan_id)
                or (patch.selection_quote is not None and patch.selected_plan_id != self.requirements.selected_plan_id)
                or (patch.pricing_quote is not None and not self.wants_price)
                or (patch.cancel_pricing_quote is not None and self.wants_price)):
            self._reject("unreviewed_mutation")
        for item in patch.add:
            if not any((old.kind, old.feature_id, old.statement if old.feature_id is None else None) ==
                       (item.kind, item.feature_id, item.user_quote if item.feature_id is None else None)
                       for old in self.requirements.items):
                self._reject("unreviewed_mutation", "requirements")
        # Do not change intent, quotes, revision, receipt, or selection origin.
        return self.prompt_state(recheck_current=False) | {"changes": ()}

    def _candidate_disagrees(self, operation, candidate: dict) -> bool:
        """A review may fill omissions/repair representation, not flip facts.

        Legacy first candidates have no source positions. Compare only the same
        fact identity and overlapping actual quoted clause. Representations such
        as 50.0 can become the original integer 50, but 50.5 cannot.
        """
        if not isinstance(candidate, dict):
            return False
        quote = operation.span.quote
        def related(other_quote):
            return isinstance(other_quote, str) and (other_quote in quote or quote in other_quote)
        if operation.action == "set" and operation.field in {"paid_seats", "months"}:
            return any(p.get("name") == operation.field and related(p.get("user_quote"))
                and p.get("value") != operation.value for p in candidate.get("parameters", ()) if isinstance(p, dict))
        if operation.action == "set" and operation.field == "saml_sso_compatible_idp":
            return any(a.get("condition_id") == operation.field and related(a.get("user_quote"))
                and a.get("value") != operation.value for a in candidate.get("conditions", ()) if isinstance(a, dict))
        if operation.action in {"select_plan", "cancel_selection"}:
            return (candidate.get("selection_quote") is not None
                and related(candidate.get("selection_quote"))
                and candidate.get("selected_plan_id") != operation.value)
        if operation.action in {"add", "clarify", "replace"} and operation.requirement is not None:
            reviewed = operation.requirement
            # Additional unmodeled hard detail alongside the same feature is
            # conservative coverage, never a claim that the detail is supported.
            if reviewed.kind != "feature":
                return False
            aliases = {"api_access": ("API", "程序接口", "编程接口"), "data_export": ("导出",),
                "audit_logs": ("审计",), "saml_sso": ("SSO", "统一登录", "单点登录"),
                "project_collaboration": ("项目协作", "任务协作"), "private_deployment": ("私有化",)}
            # Sharing a parallel clause is valid source attribution and does
            # not prove that two capability candidates represent one object.
            if any(label.lower() in quote.lower() for label in aliases.get(reviewed.feature_id, ())):
                return False
            matching = [a for a in candidate.get("add", ()) if isinstance(a, dict)
                        and a.get("user_quote") == quote and a.get("kind", "feature") == "feature"]
            return bool(matching and not any(a.get("feature_id") == reviewed.feature_id for a in matching))
        return False

    def _review_parameter(self, operation, span, pending, message):
        if type(operation.value) is not int:
            raise ValueError("invalid_type")
        quote = span["quote"]
        bare = bool(re.fullmatch(r"\s*[0-9零一二两三四五六七八九十百]+\s*", quote))
        left = re.search(r"[^，,；;。！？!?\n]*$", message[:span["start"]]).group()
        right = re.match(r"[^，,；;。！？!?\n]*", message[span["end"]:]).group()
        actual_clause = left + quote + right
        if (operation.field == "paid_seats" and re.search(r"公司|员工|总人数|人口", actual_clause)
                and not re.search(r"席|付费|购买|买|订购|开通|账号|名额", actual_clause)):
            raise ValueError("not_actual_purchase")
        # Tokenize complete numbers; never accept the suffix of 50.5 or -50.
        tokens = list(re.finditer(r"(?<![\d.])[-−]?\d+(?:\.\d+)?|[零一二两三四五六七八九十百]+", quote))
        supported = False
        if operation.field == "months" and "半年" in quote and operation.value == 6:
            supported = True
        for token in tokens:
            value = _number(token.group())
            if value is None:
                continue
            suffix = quote[token.end():]
            # Counted complete years remain years even when the classifier
            # contains an integrity modifier. This is unit normalization,
            # not a whitelist of subscription sentences or purchase intent.
            years = operation.field == "months" and re.match(
                r"\s*(?:个\s*)?(?:(?:整个|整|完整)(?:的)?\s*)?年(?:度)?(?P<half>半)?", suffix)
            normalized = value * 12 + (6 if years.group("half") else 0) if years else value
            if normalized == operation.value:
                prefix = message[max(0, span["start"] + token.start() - 24):span["start"] + token.start()]
                # These guards are narrowly attached to the chosen occurrence,
                # never a whole-message ban on corrections or unrelated plans.
                if re.search(r"(?:约|大概|差不多|暂定|预计|计划|可能)(?:购买|买|订购|订|为|是|数量|席位|付费|\s){0,6}$", prefix):
                    raise ValueError("uncertain")
                if re.search(r"(?:如果|假设|假如)(?:购买|买|订购|订|为|是|数量|席位|付费|\s){0,6}$|才会(?:购买|买|订阅|订)\s*$", prefix):
                    raise ValueError("conditional")
                if re.search(r"(?:不买|不购买|不订|不是|不要)(?:购买|买|席位|为|是|\s){0,4}$", prefix):
                    raise ValueError("negative")
                if re.search(r"负\s*$", prefix):
                    raise ValueError("invalid_type")
                supported = True
        if not supported:
            # Illegal source numbers are an input issue. A legal integer
            # whose source/normalization does not establish the proposed value
            # is an extraction failure, not a claim that the user typed a float.
            invalid_source = any(_number(token.group()) is None or
                re.search(r"负\s*$", quote[:token.start()]) for token in tokens)
            raise ValueError("invalid_type" if invalid_source else "extraction_failed")
        unassigned_numbers = bool(re.fullmatch(r"[\s0-9零一二两三四五六七八九十百和与及、,，]+", actual_clause))
        if bare and unassigned_numbers and tuple(pending) != (operation.field,):
            raise ValueError("ambiguous")
        upper = 500 if operation.field == "paid_seats" else 12
        if not 1 <= operation.value <= upper:
            raise ValueError("invalid_range")
        return DeclaredParameter(name=operation.field, value=operation.value, user_quote=quote)

    def _irrelevant_negative_add(self, group, issue, original, message):
        """A fabricated requirement from a negation is no target-task gap.

        This applies only to absent features explicitly negated by the user.
        A real existing requirement, replacement, detail, positive operation in
        the same closed group, or uncertain interpretation remains blocking.
        """
        if issue != "negative":
            return False
        aliases = {"api_access": ("API", "程序接口", "编程接口"), "data_export": ("导出",),
            "audit_logs": ("审计",), "saml_sso": ("SSO", "统一登录", "单点登录"),
            "project_collaboration": ("项目协作", "任务协作"), "private_deployment": ("私有化",)}
        operations = [op for op in group.operations if op.action != "note"]
        if not operations:
            return False
        for op in operations:
            item = op.requirement
            if op.action != "add" or item is None or item.kind != "feature" or any(
                    old.feature_id == item.feature_id for old in original.items):
                return False
            try:
                span = self._locate_span(op.span, message)
            except ValueError:
                return False
            mentions = [found for label in aliases.get(item.feature_id, ())
                for found in re.finditer(re.escape(label), span["quote"], re.I)]
            if not mentions or not all(re.search(r"(?:不需要|不要|不用|无须|无需|没有要求)\s*(?:有|支持|具备)?\s*$",
                    message[max(0, span["start"] + found.start() - 16):span["start"] + found.start()]) for found in mentions):
                return False
        return True

    def apply_review(self, raw, message: str, first_candidate: dict | None = None) -> dict:
        """Validate a temporary task and atomically commit confirmed groups.

        Each group is a closed transaction. Whole JSON/target failure commits
        no new fact. Product matching, pricing, evidence and permission remain
        the existing deterministic services' responsibility.
        """
        if not self.review_locked:
            self.begin_review_turn()
        if self.review_complete:
            self._reject("unreviewed_mutation")
        review = raw if isinstance(raw, SemanticReview) else SemanticReview.model_validate_json(
            json.dumps(raw, ensure_ascii=False))
        self.consultation_scope_reviewed = bool({"consultations", "consultation_coverage_complete"}
            & review.model_fields_set)
        self.consultation_contract_missing = review.consultation_contract_missing
        # A numeric declaration's replacement is the same field operation as
        # setting its final value. Canonicalize only that wire representation;
        # requirement replacement and clear-to-unknown retain their own rules.
        # All target, source, disagreement, value, range, conflict, and group
        # checks below still apply to the canonical candidate.
        # A feature-shaped wrapper with no feature/object identity and the
        # exact same source describes the numeric field, not another demand.
        # Identified features, unknown details and other sources are retained
        # for rejection rather than silently discarded.
        review = review.model_copy(update={"groups": tuple(group.model_copy(update={
            "operations": tuple(op.model_copy(update={"action": "set", "requirement": None}) if
                op.action == "replace" and op.field in {"paid_seats", "months"}
                and op.requirement_id is None and (op.requirement is None or
                    (op.requirement.kind == "feature" and op.requirement.feature_id is None
                        and op.requirement.user_quote == op.span.quote)) else op
                for op in group.operations)}) for group in review.groups)})
        self._capture_context()
        target = review.target
        existing = self._selection_context if target.kind == "selection" else self._price_context if target.kind == "price" else None
        fresh = target.relation in {"new", "independent"}
        initial_empty = (self._working_intent is None and not self.requirements.items
            and not self.requirements.parameters and not self.requirements.condition_assertions
            and self.requirements.selected_plan_id is None)
        target_known = True
        if fresh:
            if target.kind == "none" or (target.span is None and not initial_empty) or (target.relation == "independent" and target.kind != "price"):
                target_known = False
            elif target.span is not None:
                try:
                    self._locate_span(target.span, message)
                except ValueError:
                    target_known = False
        elif existing is not None and target.task_id is not None and target.task_id != existing.requirements.requirement_id:
            target_known = False
        elif existing is None and target.task_id is not None:
            # The initial empty process-owned candidate identity is acceptable;
            # an old task ID never creates a replacement for a missing object.
            target_known = self._working_intent is None and target.task_id == self.requirements.requirement_id
        if target.kind == "none" and any(op.action != "note" for group in review.groups for op in group.operations):
            target_known = False
        if any(group.verdict == "accept" and any(op.action != "note" for op in group.operations)
               for group in review.groups) and (target.kind not in {"selection", "price"}
                or review.intent != target.kind):
            # A read-only label cannot hide a confirmed edit and suppress its
            # existing matching/pricing duties. The single reviewer must
            # repair that representation before this acceptance boundary.
            target_known = False
        if not target_known:
            self.operation_feedback = tuple({"group_id": group.group_id, "status": "rejected",
                "target": "none", "field": self._operation_field(group.operations[0]),
                "issue": "target_unknown", "message": self._review_message("target_unknown")}
                for group in review.groups) or ({"group_id": "target", "status": "rejected", "target": "none",
                    "field": "candidate", "issue": "target_unknown", "message": self._review_message("target_unknown")},)
            self.review_blockers = ("target_unknown",)
            self.review_preserve_results = True
            self.intent = review.intent
            return self.prompt_state(recheck_current=False)
        consultation_scopes, consultation_feedback = self._consultation_scopes(review, message)
        if existing is None or fresh:
            initial = self.requirements if self._working_intent is None and not fresh else RequirementSet(requirement_id=uuid4().hex)
            staged = TaskContext(requirements=initial)
        else:
            staged = replace(existing)
        original = staged.requirements
        if target.reuse_fields:
            source = self._selection_context if target.copy_from == "selection" else self._price_context if target.copy_from == "price" else None
            if not fresh or source is None or target.span is None:
                self.review_blockers = ("target_unknown",)
                self.review_preserve_results = True
                self.operation_feedback = ({"group_id": "reuse", "status": "rejected", "target": "none",
                    "field": "candidate", "issue": "target_unknown", "message": self._review_message("target_unknown")},)
                return self.prompt_state(recheck_current=False)
            original = original.model_copy(update={
                "parameters": tuple(p for p in source.requirements.parameters if p.name in target.reuse_fields),
                "items": source.requirements.items if "requirements" in target.reuse_fields else (),
                "condition_assertions": source.requirements.condition_assertions if "conditions" in target.reuse_fields else ()})
            staged.requirements = original
            staged.source_notes = (*staged.source_notes, {"kind": "authorized_reuse",
                "from_task_id": source.requirements.requirement_id, **self._locate_span(target.span, message)})[-24:]
        items, parameters, assertions = list(original.items), {p.name: p for p in original.parameters}, {a.condition_id: a for a in original.condition_assertions}
        selected, wants_price, origin, basis = original.selected_plan_id, staged.wants_price, staged.selected_plan_origin, staged.pricing_basis
        notes, feedback, changed_fields, rejected_fields = list(staged.source_notes), [], set(), set()
        requested_execution = set()
        accepted_operations = []
        statuses, remaining = {}, list(review.groups)
        catalog = get_catalog()
        features = {f.feature_id: f for f in catalog.features}
        plans = {p.plan_id for p in catalog.plans}
        condition_ids = {c.condition_id for c in catalog.conditions}
        candidate = first_candidate or {}
        changes = []
        final_writes = {}
        for group in review.groups:
            if group.verdict != "accept":
                continue
            finals = {}
            for op in group.operations:
                if op.action in {"set", "clear", "select_plan", "cancel_selection", "request_pricing", "request_proposal", "cancel_pricing"}:
                    key = "pricing" if op.action == "request_proposal" else self._operation_field(op)
                    finals[key] = None if op.action in {"clear", "cancel_selection"} else False if op.action == "cancel_pricing" else True if op.action in {"request_pricing", "request_proposal"} else op.value
            for key, value in finals.items():
                final_writes.setdefault(key, []).append((group.group_id, value))
        conflicting_groups = {identity for writes_for_field in final_writes.values()
            if len({value for _, value in writes_for_field}) > 1
            for identity, _ in writes_for_field}
        # Topological evaluation makes a rejected dependency block its complete
        # dependent group. Cycles reject every remaining group without guessing.
        while remaining:
            ready = next((group for group in remaining if all(dep in statuses for dep in group.depends_on)), None)
            cycle = ready is None
            group = remaining.pop(0) if cycle else ready
            if not cycle:
                remaining.remove(group)
            fields = {self._operation_field(op) for op in group.operations}
            issue = "conflict" if group.group_id in conflicting_groups else "dependency_unresolved" if cycle or any(statuses.get(dep) != "accepted" for dep in group.depends_on) else (None if group.verdict == "accept" else group.reason)
            local_items, local_params, local_assertions = list(items), dict(parameters), dict(assertions)
            local_selected, local_wants, local_origin, local_basis = selected, wants_price, origin, basis
            local_notes, spans = [], []
            local_changes = []
            local_execution = set()
            writes = {}
            # Rejected groups still need real, verifiable source positions for
            # formal feedback. Never display a model's fabricated quote merely
            # because its verdict is conservative.
            for op in group.operations:
                try:
                    spans.append(self._locate_span(op.span, message))
                except ValueError:
                    issue = "reference_missing"
            if issue != "reference_missing":
                for op, span in zip(group.operations, spans):
                    if op.action == "set" and op.field in {"paid_seats", "months"} and type(op.value) is float:
                        # Wire values can describe a rejected actual decimal;
                        # never convert them to business integers. Distinguish
                        # actual decimal input from a model-only representation
                        # error without blaming the user's original integer.
                        decimals = [float(found.group()) for found in re.finditer(
                            r"(?<![\d.])[-−]?\d+\.\d+(?!\d)", span["quote"]) if "−" not in found.group()]
                        issue = "invalid_type" if op.value in decimals else "extraction_failed"
            if issue is None:
                try:
                    for op, span in zip(group.operations, spans):
                        field_name = self._operation_field(op)
                        if op.action == "note":
                            local_notes.append({"kind": "source_note", **span})
                            continue
                        if op.speech_act not in {"assertion", "instruction"}:
                            raise ValueError({"background": "not_actual_purchase", "hypothesis": "conditional"}.get(op.speech_act, op.speech_act))
                        if op.certainty != "certain" and not (op.action == "clear" or
                                (op.field == "saml_sso_compatible_idp" and op.value == "unknown") or
                                (op.action in {"add", "clarify", "replace"} and op.requirement is not None and op.requirement.kind != "feature")):
                            raise ValueError("conditional" if op.certainty == "conditional" else "uncertain")
                        if op.time_scope == "future" and op.action not in {"add", "note"}:
                            raise ValueError("future")
                        if op.action in {"set", "clear", "add", "remove", "clarify", "replace", "select_plan", "cancel_selection", "cancel_pricing"}:
                            left = re.search(r"[^，,；;。！？!?\n]*$", message[:span["start"]]).group()
                            right = re.match(r"[^，,；;。！？!?\n]*", message[span["end"]:]).group()
                            clause = left + span["quote"] + right
                            following = message[span["end"] + len(right):span["end"] + len(right) + 1]
                            explicit_edit = op.speech_act == "instruction" and bool(re.search(
                                r"(?:把|将).*(?:加|改|删|取消|撤回|替换)|(?:改为|改成|改到|增加到|加到|加入|添加|撤回|取消|删除|换成)", clause))
                            if not explicit_edit and not re.search(r"[，,；;。\n]", span["quote"]) and (following in {"？", "?"} or re.search(r"[？?]\s*$|(?:吗|呢)\s*$", clause)):
                                raise ValueError("question")
                            if op.action in {"remove", "clear", "cancel_selection", "cancel_pricing"} and re.search(
                                    r"不需要(?:取消|删除|撤回)|不要(?:取消|删除|撤回)|不用(?:取消|删除|撤回)", clause):
                                raise ValueError("negative")
                        if self._candidate_disagrees(op, candidate):
                            raise ValueError("candidate_disagreement")
                        if op.action in {"set", "clear", "select_plan", "cancel_selection", "request_pricing", "request_proposal", "cancel_pricing"}:
                            write_key = "pricing" if op.action == "request_proposal" else field_name
                            final_value = None if op.action in {"clear", "cancel_selection"} else False if op.action == "cancel_pricing" else True if op.action in {"request_pricing", "request_proposal"} else op.value
                            # Clear then adopt one final value is one valid closed
                            # correction. Two distinct non-null finals conflict.
                            if write_key in writes and writes[write_key] is not None and final_value is not None and writes[write_key] != final_value:
                                raise ValueError("conflict")
                            writes[write_key] = final_value
                        if op.action == "set" and op.field in {"paid_seats", "months"}:
                            parameter_pending = tuple(dict.fromkeys((*staged.pending, *staged.clarification_pending)))
                            parameter = self._review_parameter(op, span, parameter_pending, message)
                            old = local_params.get(op.field)
                            if old is None or old.value != parameter.value:
                                local_params[op.field] = parameter
                                label, unit = ("付费席位", "席") if op.field == "paid_seats" else ("订阅期限", "个月")
                                local_changes.append(f"已明确{label}：{parameter.value}{unit}（{span['quote']}）")
                            else:
                                local_notes.append({"kind": "equivalent_source", "field": op.field, **span})
                        elif op.action == "clear" and op.field in {"paid_seats", "months"}:
                            if local_params.pop(op.field, None) is not None:
                                local_changes.append("已清为未知：" + ("付费席位。" if op.field == "paid_seats" else "订阅期限。"))
                        elif op.action == "set" and op.field == "saml_sso_compatible_idp":
                            if op.field not in condition_ids or op.value not in {"yes", "no", "unknown"}:
                                raise ValueError("unknown_identity")
                            if (re.search(r"(?:不需要|不用|无需|不要)\s*(?:支持|提供|使用)?\s*(?:SAML\s*)?(?:SSO|统一登录|单点登录)", span["quote"], re.I)
                                    and not re.search(r"身份|IdP|提供方|兼容|适配", span["quote"], re.I)):
                                # Cancelling a capability is a requirement
                                # operation, never a statement about an IdP.
                                raise ValueError("negative")
                            short_answers = {"是": "yes", "是的": "yes", "否": "no", "不是": "no",
                                "不确定": "unknown", "不知道": "unknown"}
                            short = span["quote"].strip().strip("。！! ")
                            if short in short_answers:
                                if staged.pending != ("condition:" + op.field,):
                                    raise ValueError("ambiguous")
                                if op.value != short_answers[short]:
                                    raise ValueError("candidate_disagreement")
                            elif short in {"好的", "好", "嗯", "可以", "ok", "OK"}:
                                raise ValueError("ambiguous")
                            if op.value != "unknown" and re.search(r"不确定|不知道|不清楚|尚不清楚|是否兼容", span["quote"]):
                                raise ValueError("uncertain")
                            if op.value == "yes" and (re.search(r"公司账号|公司账户", span["quote"])
                                    or re.search(r"支持\s*SAML", span["quote"], re.I)) and not re.search(
                                    r"兼容|适配|正常运行|符合.*(?:SSO|你们|澄星)", span["quote"], re.I):
                                raise ValueError("ambiguous")
                            assertion = ConditionAssertion(condition_id=op.field, value=op.value, user_quote=span["quote"])
                            if op.field not in local_assertions or local_assertions[op.field].value != op.value:
                                local_assertions[op.field] = assertion
                                label = {"yes": "目前具备", "no": "目前不具备", "unknown": "目前不确定"}[op.value]
                                local_changes.append("兼容身份提供方用户自述：" + label + "。")
                            else:
                                local_notes.append({"kind": "equivalent_source", "field": op.field, **span})
                        elif op.action in {"add", "remove", "clarify", "replace"}:
                            old_item = next((item for item in local_items if item.requirement_id == op.requirement_id), None)
                            if op.action != "add" and old_item is None:
                                raise ValueError("unknown_identity")
                            if op.action == "remove":
                                local_items.remove(old_item)
                                local_changes.append("已撤回硬需求：" + old_item.statement)
                                continue
                            requirement = op.requirement
                            if requirement is None or requirement.user_quote not in span["quote"]:
                                raise ValueError("reference_missing")
                            if requirement.kind == "feature" and requirement.feature_id not in features:
                                raise ValueError("unknown_identity")
                            if requirement.kind != "feature" and requirement.feature_id is not None:
                                raise ValueError("unknown_identity")
                            feature_aliases = {"api_access": ("API", "程序接口", "编程接口"), "data_export": ("导出",),
                                "audit_logs": ("审计",), "saml_sso": ("SSO", "统一登录", "单点登录"),
                                "project_collaboration": ("项目协作", "任务协作"), "private_deployment": ("私有化",)}
                            mentions = [found for label in feature_aliases.get(requirement.feature_id, ())
                                for found in re.finditer(re.escape(label), span["quote"], re.I)]
                            if requirement.kind == "feature" and mentions and all(re.search(
                                r"(?:不需要|不要|不用|无须|无需|没有要求)\s*(?:有|支持|具备)?\s*$",
                                message[max(0, span["start"] + found.start() - 16):span["start"] + found.start()]) for found in mentions):
                                raise ValueError("negative")
                            if op.action == "replace" and requirement.kind == "ambiguous":
                                raise ValueError("ambiguous")
                            if op.action == "clarify" and old_item.kind != "ambiguous":
                                if old_item.kind == requirement.kind == "feature" and old_item.feature_id == requirement.feature_id:
                                    # An identified, already clear feature can be
                                    # restated without replacing its bound fact,
                                    # evidence or receipt identity. Unknown details
                                    # and a different feature still cannot use this
                                    # path to pretend they were resolved.
                                    local_notes.append({"kind": "equivalent_source", "field": "requirements", **span})
                                    continue
                                raise ValueError("unknown_identity")
                            statement = "必须支持 " + features[requirement.feature_id].name if requirement.kind == "feature" else requirement.user_quote
                            item = HardRequirement(requirement_id=old_item.requirement_id if op.action == "clarify" else uuid4().hex,
                                statement=statement, user_quote=requirement.user_quote, feature_id=requirement.feature_id, kind=requirement.kind)
                            if op.action in {"replace", "clarify"}:
                                local_items.remove(old_item)
                            if not any((item.kind, item.feature_id, item.statement if item.feature_id is None else None) ==
                                       (old.kind, old.feature_id, old.statement if old.feature_id is None else None) for old in local_items):
                                local_items.append(item)
                                local_changes.append("已澄清硬需求：" + item.statement if op.action == "clarify" else "已增加硬需求：" + item.statement)
                            else:
                                local_notes.append({"kind": "equivalent_source", "field": "requirements", **span})
                        elif op.action == "select_plan":
                            if op.value not in plans:
                                raise ValueError("unknown_identity")
                            # Membership proves a catalog object, not a user's
                            # choice. Minimum fitting tier belongs to matching.
                            aliases = {"basic": ("Basic", "基础套餐", "基础版", "基础档"),
                                "team": ("Team", "团队套餐", "团队版", "团队档"),
                                "business": ("Business", "商业套餐", "商业版", "商务套餐", "企业套餐", "企业版", "企业档")}
                            mentions = [found for label in aliases[op.value] for found in re.finditer(
                                r"(?<![A-Za-z])" + re.escape(label) + r"(?![A-Za-z])", span["quote"], re.I)]
                            if not mentions:
                                raise ValueError("unknown_identity")
                            if all(re.search(r"(?:不要|不选|不采用|不再选|不是|取消|不买|不算)\s*$",
                                message[max(0, span["start"] + found.start() - 12):span["start"] + found.start()]) for found in mentions):
                                raise ValueError("negative")
                            local_selected, local_origin = op.value, target.kind
                            local_notes.append({"kind": "selected_plan_source", **span})
                            local_changes.append("用户指定档：" + next(plan.name for plan in catalog.plans if plan.plan_id == op.value))
                        elif op.action == "cancel_selection":
                            local_selected, local_origin = None, None
                            local_changes.append("已取消指定，按最低满足档选型。")
                        elif op.action == "request_pricing":
                            local_wants, local_basis = True, span["quote"]
                            local_execution.add("price")
                            local_changes.append("已确认计算订阅基价。")
                        elif op.action == "request_evaluation":
                            if target.kind != "selection":
                                raise ValueError("unknown_identity")
                            local_execution.add("evaluate")
                        elif op.action == "request_proposal":
                            if target.kind != "selection":
                                raise ValueError("unknown_identity")
                            # A concrete internal Proposal includes a real
                            # subscription price. Preparing it requests that
                            # calculation once fit/inputs are complete; it is
                            # still not permission to create a saved Quote.
                            local_wants, local_basis = True, span["quote"]
                            local_execution.add("prepare")
                            local_changes.append("已接受内部草案准备请求；待适配、参数和真实计价均完成后展示具体对象。")
                        elif op.action == "cancel_pricing":
                            local_wants, local_basis = False, None
                            local_changes.append("已取消计价；已有明确参数保留。")
                        else:
                            raise ValueError("unknown_identity")
                    # Budget and model validity are checked before this group's
                    # candidate replaces any previously accepted group state.
                    data = dict(items=tuple(local_items), parameters=tuple(local_params.values()),
                        condition_assertions=tuple(local_assertions.values()), selected_plan_id=local_selected,
                        context_quotes=original.context_quotes)
                    RequirementSet(requirement_id=original.requirement_id, revision=original.revision, **data)
                    if len(local_items) > 24 or len(json.dumps(data, default=lambda item: item.model_dump(mode="json"), ensure_ascii=False)) > 14_000:
                        raise ValueError("invalid_range")
                except (ValueError, TypeError) as exc:
                    issue = str(exc) if str(exc) in {"reference_missing", "invalid_type", "invalid_range", "ambiguous", "conditional", "uncertain", "future", "quoted", "question", "negative", "not_actual_purchase", "candidate_disagreement", "conflict", "unknown_identity"} else "extraction_failed"
            if issue is None:
                statuses[group.group_id] = "accepted"
                accepted_operations.extend(group.operations)
                items, parameters, assertions = local_items, local_params, local_assertions
                selected, wants_price, origin, basis = local_selected, local_wants, local_origin, local_basis
                notes.extend(local_notes)
                requested_execution.update(local_execution)
                changes.extend(local_changes)
                if local_changes:
                    changed_fields.update(fields)
            else:
                statuses[group.group_id] = "blocked" if issue == "dependency_unresolved" else "rejected"
                if not self._irrelevant_negative_add(group, issue, original, message):
                    rejected_fields.update(fields - {"candidate"})
            entry = {"group_id": group.group_id, "status": statuses[group.group_id], "target": target.kind,
                "field": next(iter(fields)) if len(fields) == 1 else "candidate",
                "issue": issue or "confirmed", "message": "；".join(local_changes) if issue is None and local_changes else self._review_message(issue or "confirmed", issue is None),
                "spans": spans, "retained": issue is not None}
            if len(fields) == 1:
                previous = self._previous(original, next(iter(fields)))
                if previous is not None:
                    entry["previous_value"] = previous
            feedback.append(entry)
        numeric_fields = {"paid_seats", "months"}
        resolved_numbers = {op.field for op in accepted_operations
                            if op.action in {"set", "clear"} and op.field in numeric_fields}
        staged.clarification_pending = tuple(sorted(
            (set(staged.clarification_pending) - resolved_numbers) | (rejected_fields & numeric_fields)))
        data = dict(items=tuple(items), parameters=tuple(parameters.values()), condition_assertions=tuple(assertions.values()),
            selected_plan_id=selected, context_quotes=original.context_quotes)
        facts_changed = any(getattr(original, key) != value for key, value in data.items())
        if facts_changed:
            staged.requirements = RequirementSet(requirement_id=original.requirement_id,
                revision=original.revision + 1, previous_revision=original.revision, **data)
            staged.match = staged.pricing = None
            staged.pricing_requirements = staged.pricing_match = None
            staged.questions = ()
            staged.last_result = "not_updated"
            staged.result_turn_id = staged.result_message_id = None
        staged.source_notes = tuple(notes[-24:])
        if staged.wants_price and not wants_price:
            staged.pricing = staged.pricing_requirements = staged.pricing_match = None
            staged.pending = tuple(p for p in staged.pending if p not in {"paid_seats", "months", "plan"})
        staged.wants_price, staged.pricing_basis, staged.selected_plan_origin = wants_price, basis, origin
        if target.kind != "none":
            if target.kind == "selection":
                self._selection_context = staged
            else:
                self._price_context = staged
            self._working_intent = target.kind
            self.requirements, self.pending, self.last_match = staged.requirements, staged.pending, staged.match
            self.wants_price, self.pricing_basis, self.selected_plan_origin = wants_price, basis, origin
            self.source_notes = staged.source_notes
        else:
            self.source_notes = tuple((*self.source_notes, *notes)[-24:])
        self.intent = review.intent
        if target.kind in {"selection", "price"} and review.intent in {"selection", "price"}:
            self.active_intent = target.kind
        relevant = rejected_fields & {"paid_seats", "months", "selected_plan_id", "pricing", "requirements"}
        if "saml_sso_compatible_idp" in rejected_fields and any(item.feature_id == "saml_sso" for item in items):
            relevant.add("saml_sso_compatible_idp")
        # An unanswered correction still blocks a new dependent price/object.
        # It does not invalidate or relabel the retained accepted result.
        outstanding_numbers = set(staged.clarification_pending) if target.kind in {"selection", "price"} else set()
        self.review_blockers = tuple(sorted(relevant | outstanding_numbers |
            ({"detail_uncovered"} if not review.coverage_complete else set())))
        self.review_complete = True
        self.review_requested_execution = tuple(sorted(requested_execution))
        numeric_input = any(op.action in {"set", "clear"} and op.field in {"paid_seats", "months"}
            for op in accepted_operations)
        # Evaluation asks for a match; it is not an exclusive function-only
        # mode. Only an adjudicated cancellation suppresses input collection.
        function_only = any(op.action == "cancel_pricing" for op in accepted_operations)
        numeric_pending = bool(set(staged.pending) & {"paid_seats", "months"})
        self.review_collect_purchase_inputs = target.kind == "selection" and (numeric_input or numeric_pending) and not function_only
        self.review_preserve_results = bool(not facts_changed and not fresh and (
            self.review_blockers or (existing is not None and not requested_execution)))
        self.review_quote_id = review.quote_id if review.intent == "quote_query" and review.quote_id is not None and review.quote_id in message else None
        query_ids = (*review.quote_ids, *((self.review_quote_id,) if self.review_quote_id else ()))
        self.review_quote_ids = tuple(dict.fromkeys(value for value in query_ids if review.intent == "quote_query" and value in message))
        self.operation_feedback, self.changes = tuple(feedback), tuple(changes)
        accepted_consultations = []
        for item in consultation_scopes:
            entry = next(entry for entry in consultation_feedback if entry["item_id"] == item.item_id)
            if any(statuses.get(identity) != "accepted" for identity in item.depends_on):
                entry.update(status="blocked", issue="dependency_unresolved")
            else:
                entry.update(status="accepted", plan_ids=list(item.plan_ids))
                accepted_consultations.append(item)
        self.turn_consultations = tuple(accepted_consultations)
        for entry in consultation_feedback:
            if entry.get("purpose") == "answer_requirement":
                entry["status"] = "blocked" if any(statuses.get(dep) != "accepted"
                    for dep in entry["depends_on"]) else "accepted"
        # A total requested as part of an accepted business operation belongs
        # to the existing task completion contract. Do not demand a duplicate
        # pricing consultation from a genuinely pure business turn. Explicit
        # pricing consultations retain their separate dependency/result check.
        task_price_spans = [self._locate_span(operation.span, message)
            for operation in accepted_operations if operation.action == "request_pricing"]
        submitted_price_spans = [span for entry in consultation_feedback
            if entry["aspect"] == "pricing" and entry["item_id"] in {item.item_id for item in review.consultations}
            for span in entry["spans"]]
        task_questions = []
        # A recommendation already requested for an accepted hard requirement
        # is fulfilled by matching, not by a duplicate support consultation.
        # Rejected operations, different features and independent source
        # questions cannot acquire this ownership.
        accepted_features = {op.requirement.feature_id for op in accepted_operations
            if op.action in {"add", "replace", "clarify"} and op.requirement is not None
            and op.requirement.kind == "feature"
            and any(item.kind == "feature" and item.feature_id == op.requirement.feature_id for item in items)}
        task_item_ids = set()
        if target.kind == "selection" and review.intent == "selection":
            for item in review.consultations:
                if item.feature_id not in accepted_features or any(statuses.get(dep) != "accepted" for dep in item.depends_on):
                    continue
                question = selection_task_question(item, message, self._locate_span,
                    self.consultation_questions, requirement_spans=[op.span for op in accepted_operations
                        if op.action in {"add", "replace", "clarify"} and op.requirement is not None
                        and op.requirement.feature_id == item.feature_id])
                if question is not None:
                    task_questions.append(dict(question, task_id=staged.requirements.requirement_id))
                    task_item_ids.add(item.item_id)
        consultation_feedback = [entry for entry in consultation_feedback if entry["item_id"] not in task_item_ids]
        for question in self.consultation_questions:
            source = question["spans"][0]
            overlaps = lambda span: source["start"] < span["end"] and span["start"] < source["end"]
            if question["aspect"] == "pricing" and not question["source_item_ids"] \
                    and target.kind in {"selection", "price"} and any(map(overlaps, task_price_spans)) \
                    and not any(map(overlaps, submitted_price_spans)):
                task_questions.append(dict(question, status="business_owned", issue="confirmed",
                    task_id=staged.requirements.requirement_id))
        task_unit_ids = {identity for question in task_questions
                         for identity in (question["unit_id"], *question.get("owned_unit_ids", ()))}
        self.task_answer_questions = tuple(task_questions)
        self.consultation_questions = tuple(question for question in self.consultation_questions
            if question["unit_id"] not in task_unit_ids)
        consultation_feedback = [entry for entry in consultation_feedback if entry["item_id"] not in task_unit_ids]
        accepted_ids = {item.item_id for item in accepted_consultations}
        for question in self.consultation_questions:
            sources = question["source_item_ids"]
            if sources and not any(identity in accepted_ids for identity in sources):
                question.update(status="blocked", issue="dependency_unresolved")
        self.consultation_feedback = tuple(consultation_feedback)
        self.consultation_complete = bool(review.consultation_coverage_complete
            and not self.consultation_contract_missing
            and all(entry["status"] in {"accepted", "empty_scope"} for entry in self.consultation_questions)
            and all(entry["status"] == "accepted" for entry in consultation_feedback))
        return self.prompt_state(recheck_current=False) | {"changes": self.changes}

    def match(self):
        if self.review_locked and (not self.review_complete or "detail_uncovered" in self.review_blockers or self.review_preserve_results):
            self._reject("review_not_completed" if not self.review_complete else "review_dependency")
        from app.schemas.matching import MatchResult
        current = {(i.requirement_id, i.statement) for i in self.requirements.items if i.feature_id == "saml_sso"}
        version = get_catalog().version
        ids = []
        for identity, binding in self.evidence_bindings.items():
            if binding[0] != version or not binding[1] & current:
                continue
            try:
                evidence = self.knowledge.evidence(identity)
                if evidence.status != "unavailable":
                    ids.append(identity)
            except KnowledgeError:
                pass
        ids = tuple(ids)
        result = match_requirements(self.requirements, knowledge=self.knowledge,
                                    retrieval_ids=ids if ids else None)
        result = MatchResult.model_validate(result)
        if result.requirements != self.requirements or result.catalog_version != version:
            raise ValueError("匹配结果与当前需求或资料版本不相容。")
        for evidence in result.retrieval_evidence:
            self.evidence_bindings[evidence.retrieval_id] = (version, current)
        # Retain only current associations; recognized conflicts stay with KS.
        self.evidence_bindings = {k: v for k, v in self.evidence_bindings.items() if v[0] == version and v[1] & current}
        self.last_match = result
        return result

    def validate_price(self, plan_id: str, paid_seats: int, months: int) -> None:
        if self.review_locked and not self.review_complete:
            self._reject("review_not_completed", "pricing", scope="pricing")
        if self.review_locked and (self.review_blockers or self.review_preserve_results):
            self._reject("review_dependency", "pricing", scope="pricing")
        params = {p.name: p.value for p in self.requirements.parameters}
        if params.get("paid_seats") != paid_seats or params.get("months") != months:
            self._reject("price_parameters_mismatch", "paid_seats" if params.get("paid_seats") != paid_seats else "months", scope="pricing")
        if self.intent == "selection":
            if not self.wants_price:
                self._reject("price_not_requested", "pricing", scope="pricing")
            if self.last_match is None or self.last_match.requirements != self.requirements or self.last_match.selected_plan_id != plan_id:
                self._reject("price_match_not_current", "pricing", scope="pricing")
            checked = match_requirements(self.requirements, knowledge=self.knowledge,
                retrieval_ids=tuple(e.retrieval_id for e in self.last_match.retrieval_evidence))
            if checked != self.last_match or checked.status != "matched":
                self.last_match = None
                self._reject("price_evidence_invalid", "pricing", scope="pricing")
        elif self.requirements.selected_plan_id != plan_id:
            self._reject("price_plan_not_selected", "selected_plan_id", scope="pricing")

    def accept_price_plan(self, plan_id: str, message: str) -> None:
        """A pricing tool's plan candidate can reference this actual user turn.

        Exact catalog labels establish the input reference, not suitability.
        Ambiguous multiple labels cannot silently choose one. Prior explicit
        selection is changed only by the normal revision candidate path.
        """
        if self.review_locked:
            if not self.review_complete:
                self._reject("review_not_completed", "selected_plan_id", scope="pricing")
            selected = (self.last_match.selected_plan_id if self.intent == "selection"
                and self.last_match is not None and self.last_match.requirements == self.requirements
                else self.requirements.selected_plan_id if self.intent == "price" else None)
            if selected != plan_id:
                self._reject("unreviewed_mutation", "selected_plan_id", scope="pricing")
            return
        if self.intent != "price" or self.requirements.selected_plan_id is not None:
            return
        mentioned = {}
        for plan in get_catalog().plans:
            for label in (plan.plan_id, plan.name.split()[0]):
                match = re.search(r"(?<![a-zA-Z])" + re.escape(label) + r"(?![a-zA-Z])", message, re.I)
                if match:
                    mentioned[plan.plan_id] = match.group()
        if set(mentioned) != {plan_id}:
            self._reject("price_plan_not_unique", "selected_plan_id", scope="pricing")
        previous_changes = self.changes
        self.apply_patch({"intent": "price", "selected_plan_id": plan_id,
                          "selection_quote": mentioned[plan_id]}, message)
        self.changes = previous_changes + self.changes


@dataclass
class SessionRuntime:
    state: WorkState
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    interaction_revision: int = 0
    proposal: object | None = None
    current_view_id: str | None = None

    def invalidate_proposal(self, event: str) -> bool:
        """Caller holds the existing lock; invalidation never deletes selection."""
        had_proposal = self.proposal is not None
        self.interaction_revision += 1
        self.proposal = None
        return had_proposal


class SessionWorkStore:
    """Shared boundary for chat now and later confirmation consumers."""
    def __init__(self, settings: Settings):
        self.settings = settings
        self._sessions: dict[str, SessionRuntime] = {}

    def get(self, session_id: str) -> SessionRuntime:
        if session_id not in self._sessions:
            self._sessions[session_id] = SessionRuntime(WorkState(self.settings))
        return self._sessions[session_id]

    def has_state(self, session_id: str) -> bool:
        runtime = self._sessions.get(session_id)
        return runtime is not None and runtime.state.has_processed_turn

    def peek(self, session_id: str) -> SessionRuntime | None:
        """A read does not create an empty work session or restore text facts."""
        return self._sessions.get(session_id)

    def discard(self, session_id: str) -> None:
        """Caller holds the affected order boundary, including native work.

        Invalidate the object itself before releasing its registry reference:
        a queued request may already be holding that exact runtime object.
        """
        runtime = self._sessions.pop(session_id, None)
        if runtime is not None:
            runtime.invalidate_proposal("record_removed")
            runtime.current_view_id = None

    def clear(self) -> None:
        for session_id in tuple(self._sessions):
            self.discard(session_id)
