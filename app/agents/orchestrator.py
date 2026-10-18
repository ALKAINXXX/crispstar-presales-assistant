# Modified from sairam-s0/personal-ai-assistant (Apache-2.0).
# Changes: replaced the personal tool loop with presales orchestration and state guards.
# Source and license: see README.md.

import asyncio
import json
import secrets
from uuid import uuid4

from app.config import Settings
from app.llm.deepseek_client import DeepSeekClient, ModelCallBudgetExceeded, DeepSeekToolOutputInvalid
from app.llm.tool_protocol import (ModelRequestInvalid, ToolMessageProtocolError,
                                   validate_new_tool_batch, validate_tool_message_sequence)
from app.llm.prompt_builder import PromptBuilder
from app.llm.semantic_verifier import SemanticVerifier, SemanticReviewFailed, validate_first_candidate
from app.llm.token_counter import count_text_tokens
from app.schemas.business import RequirementPatch
from app.schemas.catalog import Catalog
from app.schemas.chat import BusinessResponse, ChatRequest, ChatResponse, CurrentTask
from app.schemas.matching import MatchResult
from app.schemas.pricing import PricingResult, UnitPriceResult
from app.schemas.retrieval import RetrievalEvidence
from app.services.chat_render import organize_business, pending_questions
from app.services.conversation_store import ConversationStore
from app.services.catalog import get_catalog
from app.services.tool_display import tool_display_fields
from app.services.work_state import SessionWorkStore
from app.services.presales import TurnConsultationResults
from app.services.proposals import ProposalRejected, prepare_proposal, validate_proposal, selection_facts
from app.schemas.quote import QuoteActionResult, QuoteFacts
from app.services.quotes import (QuoteActionOwnershipError, QuoteStore,
                                 QuoteWriteError, QuoteReadError)
from app.services.operation_order import OperationOrder, completed_awaitable
from app.services.data_management import DataManagementStore
from app.tools.business import BusinessToolRegistry


class Orchestrator:
    MAX_BATCHES = 5
    MAX_CALLS_PER_BATCH = 4
    MAX_RESULT_CHARS = 24_000
    MAX_ARGUMENT_CHARS = 12_000
    MODEL_TIMEOUT_SECONDS = 45

    @staticmethod
    def _reference_shape(arguments):
        """Limit a reference retry to quotation changes, never different facts.

        Canonical defaults make omission and explicit defaults equivalent.
        Quote presence and list length remain part of the candidate: removing
        a request or a requirement is not a quotation correction.
        """
        patch = RequirementPatch.model_validate_json(arguments).model_dump(mode="json")

        def shape(value):
            if isinstance(value, list):
                return [shape(item) for item in value]
            if isinstance(value, dict):
                return {key: ([True] * len(item) if key == "context_quotes" else
                    item is not None if key in {"user_quote", "selection_quote",
                        "pricing_quote", "cancel_pricing_quote"} else shape(item))
                    for key, item in value.items()}
            return value

        return shape(patch)

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.prompt_builder = PromptBuilder(settings=settings)
        self.conversations = ConversationStore(settings=settings)
        self.work = SessionWorkStore(settings)
        self.quotes = QuoteStore(settings)
        self.data = DataManagementStore(settings)
        self.order = OperationOrder()
        # Process-local page revocation also covers the first turn before its
        # session ID reaches the browser. No business authority is recovered.
        self._revoked_views: set[str] = set()

    def _check_budget(self, messages, schemas, completion_tokens):
        tokens = count_text_tokens(json.dumps({"messages": messages, "tools": schemas}, ensure_ascii=False))
        if tokens + completion_tokens + 256 > self.settings.deepseek_max_context_tokens:
            raise ValueError("上下文预算不足；本轮未完成，可缩短输入或新建会话重新说明需求。")

    async def chat(self, request: ChatRequest) -> ChatResponse:
        session_id = self.conversations.ensure_conversation(request.session_id, request.message)
        runtime = self.work.get(session_id)
        async with runtime.lock:
            # A turn can wait behind a delete under this same session lock.
            # Recheck the persisted lifecycle before using its old runtime.
            self.conversations.assert_active(session_id,
                expected_version=request.session_version)
            runtime.invalidate_proposal("next_chat")
            runtime.current_view_id = request.view_id
            quote_reads = set()
            async def protect_quote_read(quote_id):
                if quote_id not in quote_reads:
                    await self.order.begin_quote_read(quote_id, continuing=bool(quote_reads))
                    quote_reads.add(quote_id)
            try:
                result = await self._chat_locked(request, session_id, runtime, protect_quote_read)
            finally:
                await completed_awaitable(self.order.end_quote_reads(quote_reads))
            return result.model_copy(update={"session_version":
                self.conversations.version(session_id)})

    async def invalidate_proposal(self, session_id: str, event: str) -> dict:
        runtime = self.work.peek(session_id)
        if runtime is None:
            return {"invalidated": False, "interaction_revision": None, "save_available": False}
        async with runtime.lock:
            invalidated = runtime.invalidate_proposal(event)
            return {"invalidated": invalidated, "interaction_revision": runtime.interaction_revision,
                    "save_available": False}

    async def invalidate_view(self, view_id: str, event: str) -> dict:
        # Mark before waiting for an in-flight model under its existing lock.
        # An incoming first chat with this nonce can never mint a permission.
        self._revoked_views.add(view_id)
        invalidated = False
        for runtime in tuple(self.work._sessions.values()):
            if runtime.current_view_id != view_id:
                continue
            async with runtime.lock:
                if runtime.current_view_id == view_id:
                    invalidated = runtime.invalidate_proposal(event) or invalidated
        return {"invalidated": invalidated, "view_revoked": True, "save_available": False}

    def _current_proposal(self, runtime, session_id, action_ref):
        proposal = runtime.proposal
        if (proposal is None or type(action_ref) is not str
                or not action_ref.isascii()
                or not secrets.compare_digest(proposal.action_ref, action_ref)
                or proposal.session_id != session_id
                or proposal.interaction_revision != runtime.interaction_revision
                or proposal.view_id != runtime.current_view_id
                or proposal.view_id in self._revoked_views):
            raise ProposalRejected("action_not_current")
        return proposal

    async def _guarded_submit_locked(self, runtime, session_id, action_ref, submitter):
        proposal = self._current_proposal(runtime, session_id, action_ref)
        try:
            validate_proposal(proposal, runtime.state)
            return await submitter(proposal)
        finally:
            runtime.invalidate_proposal("controlled_submission_attempt")

    async def guarded_submit(self, session_id: str, action_ref: str, submitter):
        """Controlled submission interface using the same checks and order.

        Production Quote callers use submit_quote, which queries persistence
        before asking for a new creation permission. No nested lock acquisition.
        """
        runtime = self.work.peek(session_id)
        if runtime is None:
            raise ProposalRejected("action_not_current")
        async with runtime.lock:
            return await self._guarded_submit_locked(runtime, session_id, action_ref, submitter)

    @staticmethod
    def _action_result(status, code, quote=None):
        if status == "saved":
            message = "草案已保存，编号、保存时间和内容来自实际存储记录。"
        elif status == "unknown":
            message = ("保存结果待核对；当前无法确认原动作结果。请使用原动作核对，"
                       "不要新建动作补存。")
        elif code == "action_not_owned":
            message = "动作不属于这份会话；未为本会话保存草案。"
        elif code == "quote_transaction_rolled_back":
            message = "草案事务已回滚，明确未保存；请重新展示并确认方案。"
        elif code == "proposal_facts_changed":
            message = "方案或资料已变化，明确未保存；请重新展示并确认方案。"
        elif code == "current_record_not_found":
            message = "当前记录中未找到原保存动作；不能据此判断它从未保存。旧保存许可不会恢复。"
        else:
            message = "原动作没有保存草案，创建许可已失效；请重新展示并确认方案。"
        return QuoteActionResult(status=status, code=code, message=message, quote=quote,
            quote_id=quote.quote_id if quote is not None else None,
            saved_at=quote.saved_at if quote is not None else None,
            current_status="active" if quote is not None else None)

    async def _persisted_quote_result(self, session_id, action_ref):
        try:
            quote = await asyncio.to_thread(self.quotes.find_action, session_id, action_ref)
            if quote is not None:
                return self._action_result("saved", "quote_saved", quote)
            return None
        except QuoteActionOwnershipError:
            return self._action_result("not_saved", "action_not_owned")
        except QuoteReadError as exc:
            if exc.code in {"quote_in_trash", "quote_permanently_deleted"}:
                try:
                    state = await asyncio.to_thread(self.quotes.action_status, session_id, action_ref)
                    current = state["current_status"]
                    return QuoteActionResult(status="saved", code=exc.code,
                        message="当时保存成功；目前在最近删除，可恢复。" if current == "trash" else
                            "此前保存成功，目前已永久删除，无法读取。",
                        quote=None, quote_id=state["quote_id"], saved_at=state["saved_at"],
                        current_status=current)
                except QuoteActionOwnershipError:
                    return self._action_result("not_saved", "action_not_owned")
                except Exception:
                    return self._action_result("unknown", "action_read_unavailable")
            return self._action_result("unknown", "action_read_unavailable")
        except Exception:
            return self._action_result("unknown", "action_read_unavailable")

    async def _write_quote(self, proposal):
        task = asyncio.create_task(asyncio.to_thread(self.quotes._create, proposal))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Cancelling a request does not stop a SQLite worker. Keep the
            # original session lock until it ends, including repeat cancellation.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled():
                task.exception()  # Retrieve failure; the original request is gone.
            raise

    async def submit_quote(self, session_id: str, action_ref: str) -> QuoteActionResult:
        runtime = self.work.peek(session_id)
        if runtime is None:
            found = await self._persisted_quote_result(session_id, action_ref)
            return found or self._action_result("not_saved", "action_not_current")
        async with runtime.lock:
            # Persisted success survives messages, view revocation and restarts.
            found = await self._persisted_quote_result(session_id, action_ref)
            if found is not None:
                return found
            try:
                quote = await self._guarded_submit_locked(runtime, session_id, action_ref, self._write_quote)
                return self._action_result("saved", "quote_saved", quote)
            except ProposalRejected as exc:
                code = "action_not_current" if exc.code == "action_not_current" else "proposal_facts_changed"
                return self._action_result("not_saved", code)
            except ValueError:
                return self._action_result("not_saved", "proposal_facts_changed")
            except QuoteWriteError as exc:
                # A known rollback is sufficient proof. Any ambiguous commit
                # must be checked against persistence before reporting a result.
                if exc.definitive:
                    return self._action_result("not_saved", exc.code)
                found = await self._persisted_quote_result(session_id, action_ref)
                if found is not None:
                    return found
                return self._action_result("unknown", exc.code)
            except Exception:
                found = await self._persisted_quote_result(session_id, action_ref)
                if found is not None:
                    return found
                return self._action_result("unknown", "quote_commit_unverified")

    async def resolve_quote_action(self, session_id: str, action_ref: str) -> QuoteActionResult:
        runtime = self.work.peek(session_id)
        if runtime is not None and runtime.lock.locked():
            # A read may observe absence while a worker can still commit. Such
            # absence is not proof of failure and never closes a live operation.
            found = await self._persisted_quote_result(session_id, action_ref)
            return found or self._action_result("unknown", "action_in_flight")
        if runtime is None:
            found = await self._persisted_quote_result(session_id, action_ref)
            return found or self._action_result("not_saved", "action_not_current")
        async with runtime.lock:
            found = await self._persisted_quote_result(session_id, action_ref)
            if found is not None:
                return found
            # Successful absence check plus the same lock excludes a pending
            # writer. Close only this action's permission before definite failure.
            proposal = runtime.proposal
            if (proposal is not None and type(action_ref) is str and action_ref.isascii()
                    and secrets.compare_digest(proposal.action_ref, action_ref)):
                runtime.invalidate_proposal("quote_result_checked_without_save")
            return self._action_result("not_saved", "action_closed_without_save")

    async def _chat_locked(self, request, session_id, runtime, quote_read_guard=None):
        state = runtime.state
        turn_id = uuid4().hex
        # This is an identity comparison, not a new evaluation. In particular
        # a later historical lookup must not revalidate or erase current facts.
        selection_before = state.current_task(recheck=False)
        state.changes = ()
        state.begin_review_turn()
        registry = BusinessToolRegistry(self.settings, state, request.message, quotes=self.quotes,
                                       quote_read_guard=quote_read_guard)
        records, errors, evidence, turn_changes = [], [], {}, []
        catalog = unit = price = match = None
        consultation_results = TurnConsultationResults()
        answers, queried_quotes = [], []
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        model = self.settings.deepseek_model
        content, messages, updated = "", [], False
        reused_receipt = False
        executed_batches, repair_sent = 0, False
        review_metrics = None
        max_tokens = request.max_completion_tokens or self.settings.deepseek_default_max_completion_tokens
        history = self.conversations.get_messages(session_id)
        recovered = bool(history and not getattr(state, "has_processed_turn", False))
        state.has_processed_turn = True

        def error(code, message, step="orchestrator"):
            errors.append({"code": code, "message": message, "step": step})

        def consultation_coverage():
            if (not state.turn_consultations and not state.consultation_feedback
                    and not getattr(state, "consultation_scope_reviewed", False)):
                return None
            return consultation_results.coverage(state.turn_consultations, answers,
                list(evidence.values()), records, state.consultation_feedback,
                scope_complete=state.consultation_complete, pricing=price,
                questions=state.consultation_questions)

        def consultation_missing():
            coverage = consultation_coverage()
            return [] if coverage is None else [item for item in coverage.items
                if item.status == "incomplete" and item.stage in {
                    "not_queried", "query_failed", "evidence_insufficient",
                    "explanation_pending", "explanation_rejected", "result_conflict"}]

        def independent_inquiry_pending():
            independent = {item.item_id for item in state.turn_consultations if not item.depends_on}
            return any(item.item_id in independent or independent.intersection(item.source_item_ids)
                       for item in consultation_missing())

        pending_calls = {}
        active_call = active_result = None

        def tool_receipt(call, result):
            serialized = json.dumps(result, ensure_ascii=False, allow_nan=False)
            over_budget = len(serialized) > self.MAX_RESULT_CHARS
            if over_budget:
                # The full real result remains in tool_results. A bounded
                # protocol receipt closes its identity without replaying it.
                serialized = json.dumps({"ok": result.get("ok") is True,
                    "result_omitted": True, "code": "tool_result_budget",
                    "message": "工具已有实际回执，但返回体量超限；完整记录已保留。"},
                    ensure_ascii=False)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": serialized})
            pending_calls.pop(call["id"])
            return over_budget

        def close_pending(code="dependency_not_executed", message="前置结果未完成；此调用未执行。"):
            for remaining in tuple(pending_calls.values()):
                if active_call is not None and remaining["id"] == active_call["id"]:
                    result = active_result or {"ok": False, "error": {"code": code, "message": message}}
                else:
                    result = {"ok": False, "error": {"code": "dependency_not_executed",
                        "message": "前置结果未完成；此调用未执行。"}}
                tool_receipt(remaining, result)

        try:
            messages = self.prompt_builder.build_messages(user_message=request.message,
                history=history, business_state=state.prompt_state(recheck_current=False) | {"recovery_required": recovered})
            llm = DeepSeekClient(settings=self.settings)
            # At most five execution batches, plus one bounded reminder for a
            # model that stops before obtaining a required controlled result.
            for _ in range(self.MAX_BATCHES + 2):
                schemas = registry.available_schemas()
                validate_tool_message_sequence(messages)
                self._check_budget(messages, schemas, max_tokens)
                try:
                    completion = await completed_awaitable(asyncio.wait_for(llm.chat(messages=messages,
                        max_completion_tokens=max_tokens, temperature=request.temperature, tools=schemas,
                        tool_choice={"type": "function", "function": {"name": "update_requirements"}}
                        if executed_batches == 0 else "auto"),
                        timeout=self.MODEL_TIMEOUT_SECONDS))
                except ModelCallBudgetExceeded as exc:
                    error("model_budget_exhausted", exc.public_message, "model")
                    break
                except ModelRequestInvalid as exc:
                    error("model_request_invalid", exc.public_message, "model_request")
                    errors[-1]["reason"] = exc.reason
                    break
                except DeepSeekToolOutputInvalid:
                    if not updated:
                        review_metrics = {"status": "failed", "dispatched": 0, "elapsed_ms": 0,
                            "usage": {}, "error_code": "candidate_invalid"}
                        error("semantic_review_candidate_invalid", "模型生成的首候选参数未通过格式或字段校验，尚未进行独立核对与业务执行。", "model")
                    else:
                        error("model_tool_arguments_invalid", "模型生成的工具参数未通过格式契约，相关工具未执行；已取得的真实结果保留。", "model")
                    break
                except Exception:
                    error("model_unavailable", "模型调用未完成；已有只读结果保留，可稍后重试。", "model")
                    break
                model = getattr(completion, "model", None) or model
                raw_usage = getattr(completion, "usage", None)
                if raw_usage:
                    for key in usage:
                        usage[key] += getattr(raw_usage, key, 0) or 0
                try:
                    choice = completion.choices[0]
                    response = choice.message
                    content = response.content or ""
                    calls = getattr(response, "tool_calls", None) or []
                except (AttributeError, IndexError, TypeError):
                    error("bad_model_response", "模型返回结构无效，本轮未完成。", "model")
                    break
                if calls and getattr(choice, "finish_reason", None) == "length":
                    if not updated:
                        review_metrics = {"status": "failed", "dispatched": 0, "elapsed_ms": 0,
                            "usage": {}, "error_code": "candidate_truncated"}
                    error("model_output_exhausted", "模型输出达到本次额度，未取得完整工具参数；本批工具未执行。", "model")
                    break
                if not calls:
                    if not updated:
                        review_metrics = {"status": "failed", "dispatched": 0, "elapsed_ms": 0,
                            "usage": {}, "error_code": "candidate_missing"}
                        error("semantic_review_required", "系统未取得本轮候选，无法核对原话；未提交新事实。", "model")
                    if not content.strip():
                        error("empty_model_response", "模型没有给出完成响应，本轮未完成。", "model")
                    if getattr(choice, "finish_reason", None) == "length":
                        error("model_output_exhausted", "模型输出额度耗尽，本轮未完成。", "model")
                    missing = []
                    if updated and state.intent == "selection" and match is None and not state.review_preserve_results:
                        missing.append("match_requirements")
                    if updated and state.intent == "knowledge" and not answers and consultation_coverage() is None:
                        missing.append("search_knowledge 后 explain_knowledge")
                    if updated and state.intent == "unit_price" and unit is None and catalog is None and consultation_coverage() is None:
                        missing.append("query_catalog")
                    if updated:
                        for item in consultation_missing():
                            missing.append("咨询事项 " + item.item_id + "（" + item.topic + "）：" +
                                ("query_catalog；尚缺 " + "、".join(item.missing_plan_ids)
                                 if item.aspect == "unit_price" else
                                 "query_catalog()及关联此item_id的search_knowledge/explain_knowledge"
                                 if item.aspect == "support" else
                                 "关联此item_id的search_knowledge/explain_knowledge"))
                    if updated and state.intent == "quote_query" and not queried_quotes:
                        missing.append("query_quote（只使用用户指定的编号）")
                    params = {p.name for p in state.requirements.parameters}
                    if updated and price is None and params == {"paid_seats", "months"} and (
                        state.intent == "price" or (state.intent == "selection" and state.wants_price and match is not None and match.status == "matched")):
                        missing.append("calculate_price")
                    if missing and not errors and not repair_sent and executed_batches < self.MAX_BATCHES:
                        repair_sent = True
                        messages.append({"role": "assistant", "content": content})
                        messages.append({"role": "system", "content": "应用核对：本轮尚缺实际受控结果：" +
                            "、".join(missing) + "。请执行必要只读工具；不猜前提或参数。selection即使前提为no/unknown也须取得逐档匹配，之后结束并追问必要缺项。这是本轮唯一一次补全提醒。"})
                        continue
                    break
                if executed_batches == self.MAX_BATCHES:
                    error("tool_batches_exhausted", "五批工具预算已耗尽；剩余调用未执行，已有结果保留。")
                    break
                if len(calls) > self.MAX_CALLS_PER_BATCH:
                    error("tool_batch_too_large", "单批工具超过四项；本批未执行。")
                    break
                encoded_calls = []
                for call in calls:
                    function = getattr(call, "function", None)
                    if (not isinstance(getattr(call, "id", None), str) or not call.id
                            or not isinstance(getattr(function, "name", None), str)
                            or not isinstance(getattr(function, "arguments", None), str)):
                        raise ToolMessageProtocolError("tool_call_shape_invalid")
                    encoded_calls.append({"id": call.id, "type": "function", "function":
                        {"name": call.function.name, "arguments": call.function.arguments}})
                if len({c["id"] for c in encoded_calls}) != len(encoded_calls):
                    raise ToolMessageProtocolError("tool_call_identity_duplicate")
                validate_new_tool_batch(messages, encoded_calls)
                messages.append({"role": "assistant", "content": content, "tool_calls": encoded_calls})
                pending_calls = {call["id"]: call for call in encoded_calls}
                batch_reminders = []
                # A rejected batch consumes the same budget as a successful
                # one. Correction is inside the existing five-batch limit.
                executed_batches += 1

                def skip_dependencies(remaining, message="前置候选未被接受；此调用未执行。"):
                    for skipped in remaining:
                        tool_receipt(skipped, {"ok": False, "error": {
                                "code": "dependency_not_executed",
                                "message": message}})

                for call_index, call in enumerate(encoded_calls):
                    active_call, active_result = call, None
                    name, arguments = call["function"]["name"], call["function"]["arguments"]
                    if len(arguments) > self.MAX_ARGUMENT_CHARS:
                        result = {"ok": False, "error": {"code": "arguments_budget", "message": "工具参数超过体量预算，未执行。"}}
                    elif not updated and review_metrics is None and name == "update_requirements":
                        try:
                            from app.tools.business import _unique_object
                            first_candidate = json.loads(arguments, object_pairs_hook=_unique_object,
                                parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite")))
                            if type(first_candidate) is not dict:
                                raise ValueError("Candidate is not an object")
                            validate_first_candidate(first_candidate)
                            review, review_metrics = await SemanticVerifier(self.settings).verify(
                                llm, request.message, first_candidate, state)
                            for key in usage:
                                usage[key] += review_metrics["usage"].get(key, 0)
                            # Apply all accepted groups on a temporary copy;
                            # the existing locks/gate still enclose this commit.
                            data = state.apply_review(review, request.message, first_candidate)
                            if not state.review_complete:
                                raise ValueError("Review target is not established")
                            registry.quote_query_only = state.intent == "quote_query"
                            result = {"ok": True, "data": json.loads(json.dumps(data, ensure_ascii=False))}
                        except SemanticReviewFailed as exc:
                            review_metrics = exc.metrics
                            for key in usage:
                                usage[key] += review_metrics["usage"].get(key, 0)
                            result = {"ok": False, "error": {"code": exc.code,
                                "message": "本轮语义核对未完成，未提交新事实；此前有效结果保留。"}}
                        except (ValueError, TypeError):
                            if review_metrics is None:
                                review_metrics = {"status": "failed", "dispatched": 0, "elapsed_ms": 0,
                                    "usage": {}, "error_code": "candidate_invalid"}
                                result = {"ok": False, "error": {"code": "semantic_review_candidate_invalid",
                                    "message": "模型生成的首候选参数未通过格式或字段校验，尚未进行独立核对与业务执行。"}}
                            else:
                                review_metrics.update(status="failed", error_code="semantic_review_facts_invalid")
                                result = {"ok": False, "error": {"code": "semantic_review_facts_invalid",
                                    "message": "本轮核对结果未通过出处、任务或操作组校验，未提交新事实。"}}
                    elif not updated:
                        result = {"ok": False, "error": {"code": "semantic_review_required",
                            "message": "尚未取得已核对的本轮候选，依赖工具未执行。"}}
                    else:
                        try:
                            result = await registry.execute(name, arguments)
                        except Exception:
                            result = {"ok": False, "error": {"code": "tool_failed",
                                "message": "本次业务工具未完成；已取得的其他可信结果保留。"}}
                    active_result = result
                    for change in state.changes:
                        if change not in turn_changes:
                            turn_changes.append(change)
                    record = {"name": name, "arguments": arguments, "result": result,
                                    "tool_call_id": call["id"], "batch": executed_batches,
                                    **tool_display_fields(name, arguments, result)}
                    records.append(record)
                    if not result["ok"]:
                        # Error echoes from rejected candidates are raw API
                        # diagnostics, not accepted user-visible facts.
                        unrequested_execution = (name in {"match_requirements", "calculate_price"}
                            and result["error"].get("code") == "business_execution_not_requested"
                            and result["error"].get("scope") == "retained_task"
                            and state.review_preserve_results and state.review_complete
                            and not state.review_blockers and not state.review_requested_execution)
                        if not unrequested_execution:
                            errors.append(record["display_result"]["error"])
                        for retrieval_id in result["error"].get("retrieval_ids", []):
                            try:
                                observation = state.knowledge.evidence(retrieval_id)
                                evidence[observation.retrieval_id] = observation
                            except Exception:
                                pass
                    else:
                        data = result["data"]
                        if name == "update_requirements":
                            updated = True
                            if match is not None and match.requirements != state.requirements:
                                match, price = None, None
                            if (state.intent == "selection" and "prepare" in state.review_requested_execution
                                    and not state.review_blockers and selection_before is not None
                                    and selection_before["requirements"] == state.requirements.model_dump(mode="json")):
                                try:
                                    _, retained_match, retained_price, _, _, _ = selection_facts(state)
                                    match, price, reused_receipt = retained_match, retained_price, True
                                    evidence.update((e.retrieval_id, e) for e in match.retrieval_evidence)
                                except (ValueError, TypeError):
                                    pass
                        elif name == "query_catalog":
                            if "status" in data:
                                queried_unit = consultation_results.add_unit(data, call["id"])
                                # The legacy inquiry projection is the last real
                                # successful single-plan query. Task rates belong
                                # to their chosen match/independent price instead.
                                if (state.intent == "unit_price" or
                                        (state.intent == "selection" and not any(
                                            item.aspect == "unit_price" for item in state.turn_consultations))):
                                    unit = queried_unit
                            else:
                                catalog = consultation_results.add_catalog(data, call["id"])
                        elif name == "match_requirements":
                            match = MatchResult.model_validate_json(json.dumps(data))
                            evidence.update((e.retrieval_id, e) for e in match.retrieval_evidence)
                        elif name == "calculate_price":
                            price = PricingResult.model_validate_json(json.dumps(data))
                            reused_receipt = False
                        elif name == "search_knowledge":
                            e = RetrievalEvidence.model_validate_json(json.dumps(data))
                            evidence[e.retrieval_id] = e
                            if e.status == "unavailable":
                                error("retrieval_unavailable", "知识检索未完成；相关解释与适配不能视为通过。", name)
                        elif name == "explain_knowledge":
                            answers.append(data)
                        elif name == "query_quote":
                            queried_quotes.append(QuoteFacts.model_validate_json(json.dumps(data)))
                    if tool_receipt(call, result):
                        error("tool_result_budget", "工具已经执行，但返回体量超限；停止后续模型调用。", name)
                        skip_dependencies(encoded_calls[call_index + 1:])
                        break
                    if name == "update_requirements" and result["ok"]:
                        batch_reminders.append({"role": "system", "content":
                            "本轮候选已完成唯一一次核对。后续工具仅消费刚返回的实际requirements；"
                            "被拒参数或无关候选不能重提。不得通过update或calculate_price另加事实/改套餐。"
                            "匹配、价格、知识及草案读取仍须实际执行；原话有未决细节不能宣称完整适配。"})
                    if name == "update_requirements" and state.review_preserve_results and not independent_inquiry_pending():
                        skip_dependencies(encoded_calls[call_index + 1:])
                        break
                    if (state.review_preserve_results and not state.review_blockers
                            and not state.review_requested_execution and not independent_inquiry_pending()):
                        skip_dependencies(encoded_calls[call_index + 1:],
                            "当前任务保留，独立咨询已完成；无本轮业务执行请求，此调用未执行。")
                        break
                    if errors:
                        # Later calls may depend on a rejected candidate. Do
                        # not execute them against the previous accepted scope.
                        skip_dependencies(encoded_calls[call_index + 1:])
                        break
                    if name == "update_requirements" and state.intent == "price":
                        needed, _ = pending_questions(state, price_requested=True)
                        if needed and not independent_inquiry_pending():
                            # Missing user facts cannot be supplied by another
                            # tool call, including later calls in this batch.
                            skip_dependencies(encoded_calls[call_index + 1:])
                            break
                close_pending()
                # Other roles may only follow a fully closed assistant batch.
                messages.extend(batch_reminders)
                if state.review_preserve_results and not independent_inquiry_pending():
                    break
                if errors:
                    break
                # Matching is the answer to a task-owned selection question.
                # An explicit catalog-wide rejection needs no further model
                # update. Keep waiting for any independent consultation, and
                # never turn unknown details or review blockers into this exit.
                if (state.intent == "selection" and state.review_complete and not state.review_blockers
                        and any(question["aspect"] == "support" for question in state.task_answer_questions)
                        and match is not None and match.requirements == state.requirements
                        and match.selection_reason == "no_satisfying_plan"
                        and set(match.blocking_reasons) == {"catalog_unsupported"}):
                    answered = consultation_coverage()
                    if answered is not None and answered.scope_complete and answered.status == "completed":
                        break
                if state.intent == "price":
                    needed, _ = pending_questions(state, price_requested=True)
                    if needed and not independent_inquiry_pending():
                        break
                if state.intent == "selection" and match is not None and match.requirements == state.requirements:
                    needed, _ = pending_questions(state, price_requested=state.wants_price)
                    if needed and not independent_inquiry_pending():
                        # A controlled match already establishes which user
                        # facts are missing. End this turn to ask for them;
                        # another model call cannot supply those facts.
                        break
        except asyncio.CancelledError:
            # Existing awaits drain actual model/native work before reaching
            # here. Only close receipts; never start remaining dependent work.
            close_pending("tool_cancelled", "本次工具等待已取消；实际工作已结束，结果未确认。")
            raise
        except ModelRequestInvalid as exc:
            error("model_request_invalid", exc.public_message, "model_request")
            errors[-1]["reason"] = exc.reason
        except ValueError as exc:
            error("invalid_or_budget", str(exc))
        except Exception:
            error("orchestration_failed", "业务编排未完成；已执行工具记录保留，请核对当前步骤。")
        finally:
            close_pending("tool_failed", "本次工具执行未完成；已取得的其他可信结果保留。")

        if match is not None and match.requirements != state.requirements:
            match, price = None, None
        if price is not None:
            try:
                state.validate_price(price.plan_id, price.paid_seats, price.months)
            except ValueError:
                price = None
                error("stale_price", "计价与当前需求或明确参数不相容；须重新计算。")
        price_requested = state.intent == "price" or (state.intent == "selection" and state.wants_price)
        coverage = consultation_coverage()
        if state.consultation_contract_missing:
            error("consultation_review_incomplete", "本轮核对遗漏了咨询范围或完整性字段；已接受的业务修改及实际结果保留，咨询核对尚未完成。", "semantic_review")
            if coverage is not None:
                coverage.status = "incomplete"
        if consultation_results.conflicts:
            error("consultation_result_conflict", "本轮目录结果的身份、版本或值有冲突；受影响结论未采用。", "consultation")
            if unit is not None and not consultation_results.price_valid(unit.plan_id):
                unit = None
        pending, questions = pending_questions(state, price_requested=price_requested)
        if (match is not None and match.selected_plan_id and {"paid_seats", "months"} & set(pending)
                and (unit is None or unit.plan_id != match.selected_plan_id)):
            # The already-chosen match can expose its known catalog rate while
            # awaiting quantities. This is response assembly, not a model tool
            # call, and must not create a fictitious tool execution record.
            try:
                unit = registry.unit_price(match.selected_plan_id)
            except Exception:
                error("unit_price_unavailable", "已选档的目录单价未取得；已完成匹配保留。", "pricing")
        if state.intent in {"selection", "price"}:
            state.pending = pending
        # A knowledge or unit-price detour has no current pricing questions,
        # but does not erase still-unanswered questions from the active task.
        if updated and state.intent == "selection" and match is None and not state.review_preserve_results:
            error("matching_not_completed", "尚未取得当前需求的匹配结果；不能给出完整选型结论。", "match")
        if updated and state.intent == "unit_price" and unit is None and catalog is None and coverage is None:
            error("catalog_not_completed", "尚未取得目录单价。", "catalog")
        if updated and state.intent == "knowledge" and not answers and coverage is None:
            error("knowledge_not_completed", "尚未完成基于本次原文的知识解释。", "knowledge")
        if updated and state.intent == "quote_query" and not queried_quotes and not errors:
            error("quote_query_not_completed", "尚未取得已保存草案，请核对编号后再查询。", "quote")
        pricing_input_rejected = any(e.get("code") == "invalid_arguments" and
            (e.get("details") or {}).get("scope") == "pricing" for e in errors)
        if updated and price_requested and not pending and (state.intent == "price" or (match and match.status == "matched")) and price is None and not pricing_input_rejected and not state.review_preserve_results:
            error("pricing_not_completed", "参数已明确，但本轮尚未取得订阅基价。", "pricing")
        selection_after = state.current_task(recheck=state.intent != "quote_query")
        selection_changed = bool(selection_after and (selection_before is None or
            selection_after["requirements"] != selection_before["requirements"]))
        # This display relation describes selection facts, not all accepted
        # input. Background context stays stored but is not a hard requirement
        # or pricing parameter. Preserve quotes within the compared facts.
        selection_fields = {"items", "selected_plan_id", "parameters", "condition_assertions"}
        selection_unchanged = bool(not reused_receipt and selection_before and selection_after and match is not None and
            {key: value for key, value in selection_before["requirements"].items()
                if key in selection_fields} ==
            {key: value for key, value in selection_after["requirements"].items()
                if key in selection_fields})
        # Quantities and term do not change capability comparisons. Read the
        # accepted facts and real match receipts, never infer this from prose.
        comparison_fields = {"items", "selected_plan_id", "condition_assertions"}
        previous_match = selection_before.get("match") if selection_before else None
        comparison_changed = bool(match is not None and (not previous_match or
            {key: value for key, value in selection_before["requirements"].items()
                if key in comparison_fields} !=
            {key: value for key, value in match.requirements.model_dump(mode="json").items()
                if key in comparison_fields} or
            previous_match["selected_plan_id"] != match.selected_plan_id or
            previous_match["plans"] != [plan.model_dump(mode="json") for plan in match.plans]))
        if state.intent != "quote_query" and not reused_receipt:
            state.remember_result(match, price, list(pending), questions,
                turn_id=turn_id, selection_changed=selection_changed)
        current_task = state.current_task(recheck=state.intent != "quote_query")
        if current_task is not None:
            current_task = CurrentTask.model_validate_json(json.dumps(current_task, ensure_ascii=False))
        # Unaccepted relevant edits need an actionable question even when an
        # older complete result remains valid. Keep these turn questions out
        # of the old task/result identity and its stored pending state.
        review_questions = {
            "paid_seats": ("这次要采用的实际付费席位是多少？请给出确定的整数；此前核定值仍保留。"
                if any(p.name == "paid_seats" for p in state.requirements.parameters)
                else "实际购买多少个付费席位？请给出确定的整数；公司人数不会自动作为席位。"),
            "months": "这次要采用的订阅期限是多少个月？请明确当前确定的期限。",
            "selected_plan_id": "这次要采用哪个套餐，还是按最低满足档选型？",
            "requirements": "请澄清这次尚未更新的具体要求或硬细节。",
            "saml_sso_compatible_idp": "你目前是否具备兼容 SAML SSO 的身份提供方？可以回答具备、不具备或不确定。",
            "pricing": "请明确这次是否继续计算订阅基价。",
            "detail_uncovered": "本轮硬要求尚未完整核对，请重新说明未接收的具体要求。",
        }
        turn_pending, turn_questions = list(pending), list(questions)
        if updated and state.review_complete:
            for field in state.review_blockers:
                key = "review:" + field
                ordinary_key = "condition:" + field if field == "saml_sso_compatible_idp" else field
                if key not in turn_pending and ordinary_key not in turn_pending:
                    turn_pending.append(key)
                    turn_questions.append(review_questions.get(field, "请明确这次修改属于哪个任务。"))
        # The new contract owns formal answer membership, including an empty
        # or failed acceptance. Real receipts and the single-plan legacy view
        # remain available through their original fields.
        formal_prices = consultation_results.unit_prices if coverage is None else []
        included_prices = set()
        if coverage is not None:
            for inquiry in coverage.items:
                for observed in inquiry.unit_prices:
                    if observed.plan_id not in included_prices:
                        formal_prices.append(observed)
                        included_prices.add(observed.plan_id)
        business = BusinessResponse(turn_id=turn_id, intent=state.intent, requirements=state.requirements,
            changes=turn_changes, pending=turn_pending, questions=turn_questions, catalog=catalog,
            catalog_context=None if state.intent == "quote_query" else get_catalog(),
            match=match, unit_price=unit, pricing=price, retrieval_evidence=list(evidence.values()),
            unit_prices=formal_prices,
            unit_price_observations=consultation_results.observations,
            consultation_coverage=coverage,
            knowledge_answers=answers, queried_quotes=queried_quotes, errors=errors, current_task=current_task,
            semantic_review=review_metrics,
            operation_feedback=list(state.operation_feedback),
            selection_unchanged=selection_unchanged,
            comparison_changed=comparison_changed,
            recovery_notice=("历史文字仍在，但服务重新开始后，之前的业务需求、参数和评估没有恢复。"
                             "本轮只采用你重新提供并核对的内容；继续选型请重新说明当前要求。") if recovered else None)
        # Eligibility belongs to this target's confirmed receipts. An extra
        # read-only request can fail after real Match/Price have completed;
        # selection_facts still checks every actual dependency and version.
        if (updated and state.intent == "selection" and not pending
                and state.review_complete and not state.review_blockers and not state.review_preserve_results
                and request.view_id not in self._revoked_views):
            try:
                runtime.proposal = prepare_proposal(state, session_id=session_id,
                    turn_id=turn_id, interaction_revision=runtime.interaction_revision,
                    view_id=request.view_id)
                business.proposal = runtime.proposal
            except (ValueError, TypeError):
                # Unsaveable read-only selections remain useful. No synthetic
                # successful Proposal or missing facts are filled from text.
                runtime.proposal = None
        status = "incomplete" if errors else "needs_input" if turn_pending else "completed"
        if coverage is not None:
            if coverage.status == "incomplete":
                status = "incomplete"
            elif coverage.status == "needs_input" and status == "completed":
                status = "needs_input"
        business.presentation, text = organize_business(business, status)
        message_id = None
        try:
            message_id = self.conversations.add_turn(session_id, request.message, text)
            state.bind_result_message(turn_id, message_id)
            if business.current_task and business.current_task.result_turn_id == turn_id:
                business.current_task.result_message_id = message_id
        except Exception:
            # Preserve actual tool results even if the final text transaction
            # cannot be confirmed. Do not pretend history was saved or retry.
            business.errors.append({"code": "history_write_unconfirmed", "step": "conversation",
                "message": "本轮会话文本写入未确认；已有只读结果保留，刷新后可能看不到此轮。"})
            status = "incomplete"
            runtime.invalidate_proposal("history_write_unconfirmed")
            business.proposal = None
            business.presentation, text = organize_business(business, status)
        return ChatResponse(message=text, model=model, usage=usage if usage["total_tokens"] else None,
                            session_id=session_id, tool_results=records, status=status, business=business,
                            message_id=message_id)
