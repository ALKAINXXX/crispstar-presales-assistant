"""Read-only adapters: model candidates enter, validated service facts leave.

The registry lives for one actual user turn. Knowledge explanations can only
cite searches made by this registry; owning KnowledgeService rechecks sources.
"""

import asyncio
import inspect
import json
from typing import Literal

from pydantic import Field, ValidationError

from app.config import Settings
from app.schemas.business import INPUT_REJECTIONS, RejectedInputDetails, RequirementPatch
from app.schemas.catalog import Catalog, ReadOnlyModel
from app.schemas.knowledge import Source
from app.schemas.matching import MatchResult
from app.schemas.pricing import Months, PaidSeats, PlanIdentity, PricingResult, UnitPriceResult
from app.schemas.retrieval import RetrievalEvidence
from app.schemas.quote import Quote, quote_facts
from app.services import catalog as catalog_service
from app.services import pricing as pricing_service
from app.services.knowledge import KnowledgeError
from app.services.matching import match_requirements
from app.services.work_state import InputRejected, WorkState
from app.services.quotes import QuoteReadError, QuoteStore


class NoArguments(ReadOnlyModel):
    pass


class SearchArguments(ReadOnlyModel):
    query: str = Field(min_length=1, max_length=2000)
    consultation_item_ids: tuple[str, ...] = Field(default=(), max_length=24,
        description="本轮已接受咨询事项的原item_id或规范unit_id；复合问题逐项关联实际检索，不是业务修改或支持裁决。")


class CatalogArguments(ReadOnlyModel):
    plan_id: PlanIdentity | None = Field(default=None,
        description="省略此字段并传{}才返回完整目录及套餐能力支持矩阵。指定plan_id仅查该档单价，不返回支持关系；能力咨询必须用空参数查询。")


class QuoteArguments(ReadOnlyModel):
    quote_id: str = Field(min_length=1, max_length=120,
        description="用户明确要求查回的草案编号；合法格式为 Q- 加32位大写十六进制。")


class PriceArguments(ReadOnlyModel):
    plan_id: PlanIdentity
    paid_seats: PaidSeats
    months: Months


class Citation(ReadOnlyModel):
    source_id: str = Field(min_length=1, max_length=300)
    quote: str = Field(min_length=1, max_length=1000)


class ExplanationArguments(ReadOnlyModel):
    retrieval_id: str = Field(min_length=1, max_length=100)
    citations: tuple[Citation, ...] = Field(max_length=4,
        description="最多四段逐字连续原文。同一source_id可引用不同连续片段；不能拼接、改写或用省略号代替原文。")
    answerability: Literal["supported", "insufficient"]
    consultation_item_ids: tuple[str, ...] = Field(default=(), max_length=24,
        description="这份实际引用回答的本轮原item_id或规范unit_id；只能关联本次检索实际覆盖的单位或其子集，不能用任意一条引用代表整个复合问题。")


class InvalidToolResult(ValueError):
    pass


class QuoteQueryScopeRejected(ValueError):
    pass


class ConsultationBindingRejected(ValueError):
    pass


class EvidenceConflict(ValueError):
    def __init__(self, retrieval_ids: tuple[str, ...], condition_ids: tuple[str, ...]):
        self.retrieval_ids = retrieval_ids
        self.condition_ids = condition_ids
        super().__init__("Known conflict in selected condition sources")


def _json_tree(value):
    """Do not turn Python-only types, non-string keys or NaN into JSON facts."""
    if value is None or type(value) in (str, bool, int, float):
        return
    if type(value) is list:
        for item in value:
            _json_tree(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json_tree(item)
        return
    raise ValueError("Tool arguments must be JSON values")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _checked(value, cls):
    """Do not await, coerce or silently accept an untyped service result."""
    if inspect.iscoroutine(value):
        value.close()
        raise InvalidToolResult("Unexpected coroutine")
    if inspect.isawaitable(value) or not isinstance(value, cls):
        raise InvalidToolResult("Unexpected service result type")
    try:
        return cls.model_validate(value)
    except ValueError as exc:
        raise InvalidToolResult("Invalid service result") from exc


class BusinessToolRegistry:
    _arguments = {
        "update_requirements": RequirementPatch,
        "search_knowledge": SearchArguments,
        "query_catalog": CatalogArguments,
        "match_requirements": NoArguments,
        "calculate_price": PriceArguments,
        "explain_knowledge": ExplanationArguments,
        "query_quote": QuoteArguments,
    }
    _descriptions = {
        "update_requirements": "提交本轮用户原话支持的意图、硬需求候选、明确参数或前提答复；代码校验并增量保留状态。未知不补齐，未说期限必须省略months，没有默认期限；订阅基价不是期限原话。取消需求须明确原话。",
        "search_knowledge": "按用户知识问题检索本地真实资料；相关不等于原文支持，也不等于用户具备前提。保留检索故障。",
        "query_catalog": "空参数{}一次读取权威产品、完整套餐能力支持矩阵、条件和价格，支持咨询必须使用该路径；指定plan_id仅返回单档单价，不包含能力支持关系。查询不需要购买数量，不选档。",
        "match_requirements": "按全部已接受的硬需求匹配，代码决定最低满足档、阻塞和条件证据；不传模型筛选后的需求。",
        "calculate_price": "按当前已明确并接受的套餐、付费席位和月份计算订阅基价；选型必须先完成当前匹配。",
        "explain_knowledge": "从本回合实际检索选取相关原文连续子串。只展示引用，不展示自由解释；资料没说明所问细节则 answerability=insufficient。supported 至少一条引用。",
        "query_quote": "按用户指定的编号只读查询已保存内部草案。返回保存时业务快照；不重算、不重新检索，不创建草案，也不更改当前选型。",
    }

    def __init__(self, settings: Settings, state: WorkState, message: str, quotes=None,
                 quote_read_guard=None):
        self.settings = settings
        self.state = state
        self.message = message
        self.retrievals: dict[str, RetrievalEvidence] = {}
        self.retrieval_consultations: dict[str, tuple[str, ...]] = {}
        self.retrieval_consultation_units: dict[str, tuple[str, ...]] = {}
        self.knowledge_answers: list[dict] = []
        self.quotes = quotes or QuoteStore(settings)
        self.quote_query_only = False
        self.quote_read_guard = quote_read_guard

    def schemas(self) -> list[dict]:
        return [{"type": "function", "function": {
            "name": name, "description": self._descriptions[name],
            "parameters": arguments.model_json_schema(),
        }} for name, arguments in self._arguments.items()]

    def available_schemas(self) -> list[dict]:
        # A catalog observation is not a knowledge-search receipt. Expose the
        # explanation step only once this turn actually owns a search result,
        # including an empty result that can honestly support "insufficient".
        # The turn-local receipt set only grows, so previously called tools
        # remain available when strict transport re-encodes tool history.
        schemas = [schema for schema in self.schemas()
                   if schema["function"]["name"] != "explain_knowledge" or self.retrievals]
        mapping = self._consultation_unit_mapping()
        if mapping == {}:
            # There is no accepted independent query to bind. These tools have
            # not issued any real receipt in this turn, so hiding them cannot
            # invalidate a previous successful search in strict tool history.
            schemas = [schema for schema in schemas if schema["function"]["name"]
                       not in {"search_knowledge", "explain_knowledge"}]
        if mapping:
            for schema in schemas:
                if schema["function"]["name"] in {"search_knowledge", "explain_knowledge"}:
                    field = schema["function"]["parameters"]["properties"]["consultation_item_ids"]
                    field["items"]["enum"] = list(mapping)
                    field["description"] += " 当前可用身份：" + "、".join(mapping) + "。选型任务问题不是独立资料查询身份。"
        return schemas

    async def execute(self, name: str, raw_arguments: str | dict | None) -> dict:
        if type(name) is not str or name not in self._arguments:
            return self._error("unknown_tool", "该工具未注册在只读业务链中。")
        try:
            if raw_arguments is None:
                raw = {}
            elif type(raw_arguments) is str:
                if len(raw_arguments) > 16000:
                    raise ValueError("Argument budget exceeded")
                raw = json.loads(raw_arguments, parse_constant=lambda _: self._reject_constant(),
                                 object_pairs_hook=_unique_object)
            elif type(raw_arguments) is dict:
                raw = raw_arguments
            else:
                raise ValueError("Arguments must be an object")
            if type(raw) is not dict:
                raise ValueError("Arguments must be an object")
            _json_tree(raw)
            encoded = json.dumps(raw, ensure_ascii=False, allow_nan=False)
            if len(encoded) > 16000:
                raise ValueError("Argument budget exceeded")
            arguments = self._arguments[name].model_validate_json(encoded)
        except ValidationError as exc:
            if name == "explain_knowledge":
                return self._explanation_error()
            # Locations are schema metadata. Rejected values/messages remain
            # diagnostic data and are never copied to business display.
            first = exc.errors(include_input=False, include_context=False)[0]
            location = first.get("loc", ())
            field_name = "candidate"
            if name == "update_requirements":
                if "parameters" in location:
                    index = next((x for x in location if type(x) is int), None)
                    values = raw.get("parameters") if type(raw) is dict else None
                    candidate = values[index] if type(values) is list and index is not None and index < len(values) else None
                    if type(candidate) is dict and candidate.get("name") in {"paid_seats", "months"}:
                        field_name = candidate["name"]
                elif "conditions" in location:
                    field_name = "saml_sso_compatible_idp"
                elif "add" in location or "remove" in location:
                    field_name = "requirements"
                elif "selected_plan_id" in location or "selection_quote" in location:
                    field_name = "selected_plan_id"
            elif location and location[0] in {"paid_seats", "months", "plan_id"}:
                field_name = "selected_plan_id" if location[0] == "plan_id" else location[0]
            details = RejectedInputDetails(issue="invalid_field", field=field_name,
                previous_value=self._previous_value(field_name) if name != "update_requirements" else None,
                scope="candidate_patch" if name == "update_requirements" else "pricing")
            return self._input_error("invalid_arguments", details)
        except (ValueError, TypeError, OverflowError, RecursionError):
            if name == "explain_knowledge":
                return self._explanation_error()
            return self._error("invalid_arguments", "工具参数不是合法的严格 JSON 对象，或类型、字段、范围不符合工具契约。")
        try:
            if (name in {"match_requirements", "calculate_price"} and self.state.review_locked
                    and self.state.review_complete and self.state.review_preserve_results
                    and not self.state.review_blockers and not self.state.review_requested_execution
                    and any(not item.depends_on for item in self.state.turn_consultations)
                    and not any(item.aspect == "pricing" for item in self.state.turn_consultations)):
                retained = self.state.current_task()
                if retained and retained["status"] == "current":
                    return {"ok": False, "error": {"code": "business_execution_not_requested",
                        "scope": "retained_task", "message":
                        "本轮核定为独立咨询，未请求重新匹配或计价；此业务执行未进行，真实旧任务保留。"}}
            if (name == "query_quote" and getattr(self.state, "review_locked", False)
                    and (not getattr(self.state, "review_complete", False) or
                         arguments.quote_id not in getattr(self.state, "review_quote_ids", ()))):
                return self._error("quote_query_scope_mismatch",
                    "工具编号与本轮已核定的查回对象不一致，未读取其他草案。")
            if name == "query_quote" and self.quote_read_guard is not None:
                await self.quote_read_guard(arguments.quote_id)
            task = asyncio.create_task(asyncio.to_thread(self._execute, name, arguments))
            try:
                data = await asyncio.shield(task)
            except asyncio.CancelledError:
                # A native/synchronous call cannot be cancelled by cancelling
                # its awaiting coroutine. Repeated cancellation must also keep
                # the consumer's session lock until the worker has ended.
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not task.cancelled():
                    task.exception()  # Observe failure while preserving cancel.
                raise
            if type(data) is not dict:
                raise InvalidToolResult("Unexpected response type")
            _json_tree(data)
            # A result must also be serializable before entering history.
            json.dumps(data, ensure_ascii=False, allow_nan=False)
            return {"ok": True, "data": data}
        except EvidenceConflict as exc:
            return {"ok": False, "error": {
                "code": "evidence_conflict",
                "message": "所选资料关联的条件在本业务链已有已知冲突，不能作为干净解释依据。",
                "retrieval_ids": list(exc.retrieval_ids),
                "condition_ids": list(exc.condition_ids),
            }}
        except ConsultationBindingRejected:
            if name == "explain_knowledge":
                return self._explanation_error()
            return self._error("knowledge_search_scope_invalid",
                "本次资料查询的咨询事项关联未通过校验；相关查询未执行。")
        except QuoteReadError as exc:
            message = {"invalid_quote_id": "草案编号格式不合法，请核对编号。",
                "quote_not_found": "当前记录中未找到该编号；不能据此判断它从未保存。",
                "quote_in_trash": "当时保存成功；目前在最近删除。请到数据管理恢复后读取。",
                "quote_permanently_deleted": "此前保存成功，目前已永久删除，无法读取。"}.get(
                    exc.code, "草案暂时无法读取，可以稍后按原编号重试。")
            return self._error(exc.code if exc.code in {"invalid_quote_id", "quote_not_found", "quote_in_trash", "quote_permanently_deleted"}
                else "quote_read_unavailable", message)
        except QuoteQueryScopeRejected:
            return self._error("quote_query_read_only", "本轮只查询已保存草案，没有修改、重新匹配或计价当前选型。")
        except InvalidToolResult:
            if name == "match_requirements":
                self.state.last_match = None
            return self._error("invalid_tool_result", "业务服务结果未通过类型、身份、参数或当前目录的一致性校验。")
        except KnowledgeError:
            if name == "match_requirements":
                self.state.last_match = None
            return self._error("evidence_invalid", "引用未通过本业务链、实际检索或当前资料的校验；请重新检索。")
        except InputRejected as exc:
            if name == "match_requirements" and not self.state.review_preserve_results:
                self.state.last_match = None
            return self._input_error("invalid_business_input", RejectedInputDetails.model_validate(exc.details))
        except ValueError:
            if name == "match_requirements":
                self.state.last_match = None
            if name == "explain_knowledge":
                return self._explanation_error()
            return self._error("invalid_business_input", "候选或业务参数未通过用户原话、当前工作状态或业务范围校验。")
        except Exception:
            if name == "match_requirements":
                self.state.last_match = None
            # SDK/service exception text may contain credentials or URLs.
            return self._error("tool_failed", "本次业务工具未完成；已取得的其他可信结果保留。")

    @staticmethod
    def _reject_constant():
        raise ValueError("Non-finite JSON constant")

    @staticmethod
    def _error(code: str, message: str) -> dict:
        return {"ok": False, "error": {"code": code, "message": message}}

    @staticmethod
    def _explanation_error():
        return BusinessToolRegistry._error("knowledge_explanation_invalid",
            "本次知识解释的结构、事项关联或连续原文引用未通过校验；相关回答尚未完成。")

    def _previous_value(self, field_name):
        requirements = self.state.requirements
        if field_name in {"paid_seats", "months"}:
            return next((p.value for p in requirements.parameters if p.name == field_name), None)
        if field_name == "selected_plan_id":
            return requirements.selected_plan_id
        if field_name == "saml_sso_compatible_idp":
            return next((a.value for a in requirements.condition_assertions if a.condition_id == field_name), None)
        return None

    @staticmethod
    def _input_error(code, details: RejectedInputDetails):
        message, retry_action = INPUT_REJECTIONS[details.issue]
        return {"ok": False, "error": {"code": code, "message": message,
            "details": details.model_dump(mode="json"), "retry_action": retry_action}}

    def _catalog(self) -> Catalog:
        result = _checked(catalog_service.get_catalog(), Catalog)
        try:
            return catalog_service.validate_catalog(result)
        except ValueError as exc:
            raise InvalidToolResult("Invalid Catalog relationships") from exc

    def _price_identity(self, result, plan_id: str):
        catalog = self._catalog()
        plan = next((p for p in catalog.plans if p.plan_id == plan_id), None)
        if plan is None or (result.product_id, result.plan_id, result.plan_name,
                           result.catalog_version, result.currency, result.billing_unit,
                           result.minor_units_per_major, result.unit_price_minor) != (
                catalog.product.product_id, plan.plan_id, plan.name, catalog.version,
                catalog.currency, catalog.billing_unit, catalog.minor_units_per_major, plan.unit_price_minor):
            raise InvalidToolResult("Price is not current Catalog price")

    def unit_price(self, plan_id: str) -> UnitPriceResult:
        """Same checked rate for model tools and controlled response assembly."""
        result = _checked(pricing_service.get_unit_price(plan_id), UnitPriceResult)
        self._price_identity(result, plan_id)
        return result

    def _consultation_binding(self, identities, *, available=None):
        """Bind read-only receipts to accepted turn scope, never task facts."""
        items = tuple(getattr(self.state, "turn_consultations", ()))
        known = {item.item_id for item in items}
        mapping = self._consultation_unit_mapping()
        if mapping is not None:
            known = set(mapping)
        identities = tuple(identities)
        if len(set(identities)) != len(identities) or not set(identities) <= known:
            raise ConsultationBindingRejected("Consultation identity is outside the accepted turn")
        valid_sources = [item.item_id for item in items if item.item_id in known]
        if not identities and len(valid_sources) == 1:
            identities = (valid_sources[0],)
        if available is not None:
            if mapping is None:
                compatible = set(identities) <= set(available)
            else:
                compatible = set(self._binding_units(identities, mapping)) <= set(
                    self._binding_units(available, mapping))
            if not compatible:
                raise ConsultationBindingRejected("Explanation is outside its actual search scope")
        return identities

    def _consultation_unit_mapping(self):
        """Resolve only service-certified, meaning-compatible current identities.

        The accepted source item remains a usable tool identity. Normalized
        units additionally allow an explanation to select an actual subset of
        a multi-object search without borrowing another question's evidence.
        No user text is interpreted here.
        """
        if not getattr(self.state, "consultation_scope_reviewed", False):
            return None
        questions = getattr(self.state, "consultation_questions", None)
        if questions is None:
            return None
        items = {item.item_id: item for item in self.state.turn_consultations}
        mapping = {}
        for question in questions:
            if (question.get("status") != "accepted"
                    or question.get("aspect") not in {"support", "meaning", "prerequisite", "detail"}):
                continue
            unit_id = question["unit_id"]
            compatible = []
            for identity in question.get("source_item_ids", ()):
                item = items.get(identity)
                if (item is None or item.aspect != question["aspect"]
                        or item.feature_id != question.get("feature_id")
                        or (not item.all_plans and not set(question.get("plan_ids", ())) <= set(item.plan_ids))):
                    continue
                compatible.append(identity)
            if not compatible:
                continue
            mapping[unit_id] = (unit_id,)
            for identity in compatible:
                mapping[identity] = (*mapping.get(identity, ()), unit_id)
        for entry in getattr(self.state, "consultation_feedback", ()):
            if entry.get("purpose") != "answer_requirement" or entry.get("status") != "accepted":
                continue
            units = entry.get("unit_ids", ())
            if units and all(unit in mapping for unit in units):
                mapping[entry["item_id"]] = tuple(units)
        return {identity: tuple(dict.fromkeys(units)) for identity, units in mapping.items()}

    def _binding_units(self, identities, mapping=None):
        mapping = self._consultation_unit_mapping() if mapping is None else mapping
        if mapping is None:
            return tuple(identities)
        if any(identity not in mapping for identity in identities):
            raise ConsultationBindingRejected("Consultation identity is no longer accepted")
        return tuple(dict.fromkeys(unit for identity in identities for unit in mapping[identity]))

    def _execute(self, name, arguments) -> dict:
        if name == "query_quote":
            self.quote_query_only = True
            self.state.intent = "quote_query"
            quote = _checked(self.quotes.get(arguments.quote_id), Quote)
            if quote.quote_id != arguments.quote_id:
                raise InvalidToolResult("Quote identity differs from requested ID")
            return quote_facts(quote).model_dump(mode="json")
        if self.quote_query_only and name not in {"update_requirements", "query_catalog"}:
            raise QuoteQueryScopeRejected("Historical lookup cannot match or change current selection")
        if name == "update_requirements":
            if self.quote_query_only and arguments.intent != "quote_query":
                raise QuoteQueryScopeRejected("Historical lookup cannot switch to a modifying branch")
            if arguments.intent == "quote_query":
                # This turn classifier is deliberately not an accepted patch.
                # No current-task revalidation or evidence access is needed.
                self.state.intent = "quote_query"
                self.quote_query_only = True
                self.state.changes = ()
                return {"intent": "quote_query", "active_intent": self.state.active_intent,
                    "requirements": self.state.requirements.model_dump(mode="json"),
                    "pending": [], "wants_price": self.state.wants_price, "changes": []}
            result = self.state.apply_patch(arguments.model_dump(mode="json"), self.message)
            if type(result) is not dict:
                if inspect.iscoroutine(result):
                    result.close()
                raise InvalidToolResult("State update is not an object")
            # WorkState keeps immutable tuples internally. Its public prompt
            # object is converted to actual JSON arrays before tool history.
            try:
                return json.loads(json.dumps(result, ensure_ascii=False, allow_nan=False))
            except (ValueError, TypeError, OverflowError, RecursionError) as exc:
                raise InvalidToolResult("State result cannot be serialized") from exc
        if name == "query_catalog":
            if arguments.plan_id is None:
                return self._catalog().model_dump(mode="json")
            result = self.unit_price(arguments.plan_id)
            return result.model_dump(mode="json")
        if name == "calculate_price":
            self.state.accept_price_plan(arguments.plan_id, self.message)
            validation = self.state.validate_price(arguments.plan_id, arguments.paid_seats, arguments.months)
            if inspect.iscoroutine(validation):
                validation.close()
                raise InvalidToolResult("Price validation returned a coroutine")
            if validation is not None:
                raise InvalidToolResult("Price validation returned an unexpected result")
            result = _checked(pricing_service.calculate_subscription_price(
                arguments.plan_id, arguments.paid_seats, arguments.months), PricingResult)
            self._price_identity(result, arguments.plan_id)
            if (result.paid_seats, result.months) != (arguments.paid_seats, arguments.months):
                raise InvalidToolResult("Pricing operands differ")
            self.state.record_pricing_result(result)
            return result.model_dump(mode="json")
        if name == "match_requirements":
            result = _checked(self.state.match(), MatchResult)
            catalog = self._catalog()
            if (result.requirements != self.state.requirements or result.catalog_version != catalog.version
                    or result.evidence_origin == "test_fixture" or result.test_evidence):
                raise InvalidToolResult("Match identity or runtime evidence differs")
            identities = tuple(e.retrieval_id for e in result.retrieval_evidence)
            if any(e.chain_id != self.state.knowledge.chain_id
                   or _checked(self.state.knowledge.evidence(e.retrieval_id), RetrievalEvidence) != e
                   for e in result.retrieval_evidence):
                raise InvalidToolResult("Match evidence not held by owning chain")
            # This reuses IDs and never makes another query. It also checks the
            # plan/requirement matrix and recommendation against current facts.
            expected = _checked(match_requirements(self.state.requirements, catalog=catalog,
                knowledge=self.state.knowledge, retrieval_ids=identities), MatchResult)
            if result != expected:
                self.state.last_match = None
                raise InvalidToolResult("Match disagrees with authoritative evaluation")
            return result.model_dump(mode="json")
        if name == "search_knowledge":
            binding = self._consultation_binding(arguments.consultation_item_ids)
            result = _checked(self.state.knowledge.retrieve(arguments.query), RetrievalEvidence)
            if (result.chain_id != self.state.knowledge.chain_id or result.query != arguments.query
                    or result.scope != "formal"
                    or _checked(self.state.knowledge.evidence(result.retrieval_id), RetrievalEvidence) != result):
                raise InvalidToolResult("Search association differs")
            # Failures remain successful observations of an unavailable search.
            self.retrievals[result.retrieval_id] = result
            self.retrieval_consultations[result.retrieval_id] = binding
            self.retrieval_consultation_units[result.retrieval_id] = self._binding_units(binding)
            return result.model_dump(mode="json")
        if name == "explain_knowledge":
            result = self.retrievals.get(arguments.retrieval_id)
            if result is None:
                raise ValueError("Explanation must cite this turn's search")
            owned = _checked(self.state.knowledge.evidence(arguments.retrieval_id), RetrievalEvidence)
            if (result.chain_id != self.state.knowledge.chain_id or result.scope != "formal"
                    or owned.chain_id != self.state.knowledge.chain_id
                    or owned.retrieval_id != result.retrieval_id or owned.scope != "formal"):
                raise InvalidToolResult("Explanation search does not belong to the owning chain")
            binding = self._consultation_binding(arguments.consultation_item_ids,
                available=self.retrieval_consultations.get(arguments.retrieval_id, ()))
            units = self._binding_units(binding)
            if not set(units) <= set(self.retrieval_consultation_units.get(arguments.retrieval_id, ())):
                raise ConsultationBindingRejected("Explanation is outside its actual normalized search scope")
            if arguments.answerability == "supported" and not arguments.citations:
                raise ValueError("Supported explanation requires a citation")
            source_ids = tuple(dict.fromkeys(c.source_id for c in arguments.citations))
            related = {h.source.source_id for h in result.hits if h.relevance == "related"}
            if any(identity not in related for identity in source_ids):
                raise ValueError("Explanation needs an actually related hit")
            sources = self.state.knowledge.validate_citations(arguments.retrieval_id, source_ids)
            if inspect.iscoroutine(sources):
                sources.close()
                raise InvalidToolResult("Citation service returned a coroutine")
            if type(sources) is not tuple or len(sources) != len(source_ids):
                raise InvalidToolResult("Citation service returned an invalid source set")
            sources = tuple(_checked(source, Source) for source in sources)
            returned = {hit.source.source_id: hit.source for hit in result.hits}
            if any(source.source_id != identity or returned.get(identity) != source
                   for identity, source in zip(source_ids, sources)):
                raise InvalidToolResult("Citation sources differ from actual returned originals")
            source_map = {source.source_id: source for source in sources}
            if any(c.quote not in source_map[c.source_id].raw_text for c in arguments.citations):
                raise ValueError("Quote must be an exact continuous original substring")
            selected_conditions = tuple(dict.fromkeys(
                condition_id for source in sources for condition_id in source.condition_ids))
            conflict_ids = self.state.knowledge.condition_conflict_ids(selected_conditions)
            if inspect.iscoroutine(conflict_ids):
                conflict_ids.close()
                raise InvalidToolResult("Conflict service returned a coroutine")
            if type(conflict_ids) is not tuple or any(type(identity) is not str for identity in conflict_ids):
                raise InvalidToolResult("Conflict service returned invalid identities")
            relevant_conflicts, relevant_conditions = [], []
            for identity in conflict_ids:
                observation = _checked(self.state.knowledge.evidence(identity), RetrievalEvidence)
                if (observation.chain_id != self.state.knowledge.chain_id
                        or observation.retrieval_id != identity or observation.scope != "formal"):
                    raise InvalidToolResult("Conflict belongs to a different knowledge chain")
                for conflict in observation.conflicts:
                    if conflict.condition_id in selected_conditions:
                        relevant_conflicts.append(identity)
                        relevant_conditions.append(conflict.condition_id)
            if relevant_conflicts:
                raise EvidenceConflict(tuple(dict.fromkeys(relevant_conflicts)),
                                       tuple(dict.fromkeys(relevant_conditions)))
            answer = {"retrieval_id": result.retrieval_id, "query": result.query,
                      "answerability": arguments.answerability,
                      "consultation_item_ids": list(binding),
                      "citations": [{"source_id": identity, "quote": quote} for identity, quote in
                          dict.fromkeys((c.source_id, c.quote) for c in arguments.citations)]}
            if self._consultation_unit_mapping() is not None:
                answer["consultation_unit_ids"] = list(units)
            self.knowledge_answers.append(answer)
            return answer
        raise ValueError("Unregistered tool")
