# Modified from sairam-s0/personal-ai-assistant (Apache-2.0).
# Changes: adapted the FastAPI shell for presales routes, saved quotes and data management.
# Source and license: see README.md.

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from app.agents.orchestrator import Orchestrator
from app.config import get_settings
from app.db.session import init_saas_db
from app.schemas.chat import ChatRequest, ChatResponse
from app.schemas.quote import Quote, QuoteActionResult
from app.services.catalog import get_catalog
from app.services.conversation_store import ConversationStore
from app.services.quotes import QuoteReadError
from app.services.operation_order import completed_worker


settings = get_settings()
BASE_DIR = Path(__file__).resolve().parent
UI_DIR = BASE_DIR / "ui"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Validate product facts before initializing runtime storage.
    app.state.catalog = get_catalog()
    init_saas_db(settings)
    yield


app = FastAPI(
    title="CrispStar Presales Assistant",
    version="0.3.0",
    lifespan=lifespan,
)
app.mount("/assets", StaticFiles(directory=UI_DIR / "assets"), name="assets")
orchestrator = Orchestrator(settings=settings)
conversations = ConversationStore(settings=settings)


ViewEvent = Literal["refresh", "switch", "history_reenter", "new_chat", "view_exit"]


class ProposalInvalidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    event: ViewEvent


class ProposalViewInvalidationRequest(ProposalInvalidationRequest):
    # A page nonce can revoke an in-flight first turn before its session ID is
    # known. It is never confirmation authority or a model-visible parameter.
    view_id: str = Field(min_length=16, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")


class QuoteActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action_ref: str = Field(min_length=32, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")


class DataActionRequest(QuoteActionRequest):
    operation: Literal["trash", "restore", "purge", "empty_trash", "clear"]
    object_type: Literal["chat", "quote"] | None = None
    object_id: str | None = Field(default=None, min_length=1, max_length=128)
    expected_revision: int = Field(ge=0)


def _check_scope(request: Request, *, mutation=False):
    try:
        info = orchestrator.data.info()
    except Exception as exc:
        raise HTTPException(503, detail={"code": "data_scope_unavailable", "message":
            "暂时无法核实当前应用记录；本次操作未继续，请重试核对。"}) from exc
    epoch = request.headers.get("X-Data-Epoch")
    location = request.headers.get("X-Data-Location")
    if mutation and (epoch is None or location is None):
        raise HTTPException(409, detail={"code": "data_scope_required", "message":
            "请刷新页面核实当前保存位置和记录后再操作；旧页面的请求不会继续。"})
    if location is not None and location != info.get("location_id"):
        raise HTTPException(409, detail={"code": "data_location_changed", "message":
            "服务当前使用的数据位置已变化；请重新核实位置，旧请求未继续。"})
    if ((epoch is not None and epoch != info["epoch"]) or
            (mutation and epoch is None and info.get("epoch_requires_scope", False))):
        raise HTTPException(409, detail={"code": "data_epoch_changed", "message":
            "当前应用记录已变化；请刷新核实后再操作。旧请求不会写回。"})
    return info


@asynccontextmanager
async def _ordinary(request: Request, *, mutation=False):
    try:
        async with orchestrator.order.ordinary():
            _check_scope(request, mutation=mutation)
            yield
    except RuntimeError as exc:
        if str(exc) == "record_clear_in_progress":
            raise HTTPException(409, detail={"code": str(exc), "message":
                "正在清除当前应用记录；请等待核实结果，新发言和保存已暂停。"}) from exc
        raise


@app.get("/data-management")
async def data_info():
    try:
        return {**await completed_worker(orchestrator.data.info),
                "clearing": orchestrator.order.clearing}
    except Exception as exc:
        raise HTTPException(503, detail={"code": "data_info_unavailable", "message":
            "暂时无法核实保存位置和存档数量，请重试。"}) from exc


@app.get("/data-management/trash")
async def data_trash(http_request: Request):
    try:
        async with _ordinary(http_request):
            info = orchestrator.data.info()
            return {"items": await completed_worker(orchestrator.data.list_trash),
                    "dataset_id": info["dataset_id"], "epoch": info["epoch"],
                    "revision": info["revision"]}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, detail={"code": "trash_read_unavailable",
            "message": "最近删除暂时无法读取，不能核实是否为空。请重试。"}) from exc


@app.get("/data-management/actions/{action_ref}")
async def resolve_data_action(action_ref: str, http_request: Request):
    # A clear rotates epoch; its original receipt remains readable so loss of
    # that response cannot force an automatic repeat of the destructive action.
    try:
        info = orchestrator.data.info()
        location = http_request.headers.get("X-Data-Location")
        epoch = http_request.headers.get("X-Data-Epoch")
        if location is not None and location != info["location_id"]:
            raise HTTPException(409, detail={"code": "data_location_changed",
                "message": "服务保存位置已变化，无法从另一位置核对此次操作。请先核实保存位置。"})
        if epoch is not None and epoch != info["epoch"] and (
                info.get("last_clear") or {}).get("action_ref") != action_ref:
            raise HTTPException(409, detail={"code": "data_epoch_changed",
                "message": "当前记录已清理，旧管理痕迹已移除；不能据此断言旧操作从未执行。"})
        result = await completed_worker(orchestrator.data.resolve, action_ref)
        if result is None or result.get("status") == "not_found":
            raise HTTPException(404, detail={"code": "management_action_not_found",
                "message": "当前记录中未找到该管理动作，尚不能核实此前结果。请核实当前存档状态。"})
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, detail={"code": "management_result_unknown",
            "message": "结果待核对；暂时无法读取原管理动作，请重试核对。"}) from exc


async def _perform_data_action(request, http_request, progress):
    engine = orchestrator
    async def perform_locked():
        info = _check_scope(http_request, mutation=True)
        existing = await completed_worker(engine.data.resolve, request.action_ref)
        if existing is not None and existing.get("status") != "not_found":
            if (existing["operation"], existing.get("object_type"), existing.get("object_id")) != (
                    request.operation, request.object_type, request.object_id):
                raise HTTPException(409, detail={"code": "management_action_mismatch",
                    "message": "原动作与当前操作不一致；请核对原动作。"})
            return existing
        try:
            result = await completed_worker(engine.data.perform,
                request.action_ref, request.operation, expected_epoch=info["epoch"],
                expected_revision=request.expected_revision,
                object_type=request.object_type, object_id=request.object_id)
        except Exception:
            # SQLite may commit before a worker loses its receipt. Reconcile
            # under the same ordering boundary before dropping any locks.
            try:
                result = await completed_worker(engine.data.resolve, request.action_ref)
            except Exception:
                # An ambiguous clear/delete must not retain an old creation
                # permission in process memory while its archive result is
                # unknown. This ends transient work; it is not a claim that
                # persistent deletion succeeded.
                if request.operation == "clear":
                    engine.work.clear()
                    engine._revoked_views.clear()
                    progress["business_ended"] = True
                elif request.object_type == "chat":
                    engine.work.discard(request.object_id)
                raise
            if result.get("status") not in {"completed", "not_completed"}:
                if request.operation == "clear":
                    engine.work.clear()
                    engine._revoked_views.clear()
                    progress["business_ended"] = True
                elif request.object_type == "chat":
                    engine.work.discard(request.object_id)
                raise
        if result["status"] == "completed":
            if request.operation == "clear":
                engine.work.clear()
                engine._revoked_views.clear()
                progress["business_ended"] = True
            elif request.object_type == "chat":
                engine.work.discard(request.object_id)
            elif request.operation == "empty_trash":
                # Deleted sessions have no remaining business authority.
                for sid in tuple(engine.work._sessions):
                    try:
                        engine.conversations.version(sid)
                    except (ValueError, RuntimeError):
                        engine.work.discard(sid)
        return result

    if request.operation == "clear":
        async with engine.order.clear():
            async with engine.order.management_lock:
                return await perform_locked()
    async with _ordinary(http_request, mutation=True):
        async with engine.order.management_lock:
            sid = request.object_id if request.object_type == "chat" else None
            if request.object_type == "quote":
                try:
                    state = await completed_worker(engine.quotes.get_status, request.object_id)
                except QuoteReadError as exc:
                    if exc.code not in {"quote_not_found", "invalid_quote_id"}:
                        raise
                    state = None
                sid = state.get("source_session_id") if state else None
            runtime = engine.work.peek(sid) if sid else None
            quote_ids = [request.object_id] if request.object_type == "quote" else []
            if request.operation == "empty_trash":
                quote_ids = [item["object_id"] for item in
                    await completed_worker(engine.data.list_trash) if item["object_type"] == "quote"]
            if runtime is not None:
                async with runtime.lock:
                    async with engine.order.manage_quotes(quote_ids):
                        return await perform_locked()
            async with engine.order.manage_quotes(quote_ids):
                return await perform_locked()


@app.post("/data-management/actions")
async def manage_data(request: DataActionRequest, http_request: Request):
    single = request.operation in {"trash", "restore", "purge"}
    if (single and (request.object_type is None or request.object_id is None)
            or not single and (request.object_type is not None or request.object_id is not None)):
        raise HTTPException(422, detail="单项操作需要对象身份，批量操作不能携带对象。")
    # Shield the whole operation, including removal of process business state,
    # rather than just the SQLite call. A disconnected page is not a rollback.
    progress = {"business_ended": False}
    task = asyncio.create_task(_perform_data_action(request, http_request, progress))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise
    except HTTPException:
        raise
    except Exception:
        # Never turn loss of a receipt into an invented success or retry write.
        return JSONResponse(status_code=503, content={
            "status": "unknown", "code": "management_result_unknown",
            "message": ("当前业务状态已结束；存档清理结果待核对。请核对原管理动作，不要重复执行。"
                        if progress["business_ended"] else
                        "结果待核对；请核对原管理动作，不要重复执行。"),
            "action_ref": request.action_ref, "operation": request.operation,
            "object_type": request.object_type, "object_id": request.object_id,
            "completed_count": None, "remaining_count": None})


@app.post("/sessions/{session_id}/quotes", response_model=QuoteActionResult)
async def submit_quote(session_id: str, request: QuoteActionRequest, response: Response, http_request: Request):
    async with _ordinary(http_request, mutation=True):
        info = _check_scope(http_request, mutation=True)
        result = await orchestrator.submit_quote(session_id, request.action_ref)
        if (result.status == "not_saved" and result.code in {"action_not_current", "action_closed_without_save"}
                and info.get("last_clear") and conversations.status(session_id)["status"] == "not_found"):
            result = orchestrator._action_result("not_saved", "current_record_not_found")
    if result.status == "unknown":
        response.status_code = 503
    return result


@app.post("/sessions/{session_id}/quote-actions/resolve", response_model=QuoteActionResult)
async def resolve_quote_action(session_id: str, request: QuoteActionRequest, response: Response, http_request: Request):
    async with _ordinary(http_request, mutation=True):
        info = _check_scope(http_request, mutation=True)
        result = await orchestrator.resolve_quote_action(session_id, request.action_ref)
        if (result.status == "not_saved" and result.code in {"action_not_current", "action_closed_without_save"}
                and info.get("last_clear") and conversations.status(session_id)["status"] == "not_found"):
            result = orchestrator._action_result("not_saved", "current_record_not_found")
    if result.status == "unknown":
        response.status_code = 503
    return result


@app.get("/quotes")
async def quote_list(http_request: Request):
    try:
        async with _ordinary(http_request):
            return {"quotes": await completed_worker(orchestrator.quotes.list_quotes)}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, detail={"code": "quote_directory_unavailable",
            "message": "完整草案目录暂时无法读取，不能核实是否为空。请重试读取。"}) from exc


@app.get("/quotes/{quote_id}")
async def read_quote(quote_id: str, http_request: Request, response: Response):
    # A Quote's current deletion state can change. Neither a deleted reply nor
    # an old readable snapshot may substitute for the next real guarded read.
    response.headers["Cache-Control"] = "no-store"
    try:
        async with _ordinary(http_request):
            quote = await completed_worker(orchestrator.quotes.get, quote_id)
            return quote.model_dump(mode="json")
    except QuoteReadError as exc:
        status, message = {
            "invalid_quote_id": (400, "Quote 编号格式不合法。"),
            "quote_not_found": (404, "当前记录中未找到该编号；不能据此判断它从未保存。"),
            "quote_in_trash": (410, "当时保存成功；目前在最近删除。可前往数据管理恢复。"),
            "quote_permanently_deleted": (410, "此前保存成功，目前已永久删除，无法读取。"),
        }.get(exc.code, (503, "草案读取失败，当前无法取得已存事实；不会新建草案。"))
        raise HTTPException(status_code=status, detail={"code": exc.code, "message": message},
            headers={"Cache-Control": "no-store"}) from exc
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
        raise


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(UI_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return Response(status_code=204)


@app.get("/health")
async def health() -> dict:
    result = {"status": "ok", "env": settings.app_env}
    # Only an explicitly configured isolated preview supplies this status.
    # Normal local operation retains its existing health response and model.
    availability = getattr(app.state, "preview_chat_availability", None)
    if callable(availability):
        result["chat_availability"] = availability()
    return result


@app.get("/sessions")
async def sessions(http_request: Request) -> dict[str, list[dict]]:
    async with _ordinary(http_request):
        return {"sessions": conversations.list_conversations(limit=None)}


@app.get("/sessions/{session_id}")
async def session_messages(session_id: str, http_request: Request) -> dict:
    try:
        async with _ordinary(http_request):
            return await _session_messages(session_id)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(410, detail={"code": getattr(exc, "code", str(exc)),
            "message": "原聊天在最近删除、已永久删除或当前记录中未找到，无法打开。请核实数据管理中的状态。"}) from exc


async def _session_messages(session_id):
    runtime = orchestrator.work.peek(session_id)
    current_task = None
    if runtime is not None:
        # Read after any in-flight turn has committed its state and text.
        # The same per-session boundary protects chat and read-only recovery.
        async with runtime.lock:
            version = conversations.version(session_id)
            conversations.assert_active(session_id, expected_version=version)
            runtime.invalidate_proposal("history_reenter")
            messages = conversations.get_messages(session_id, limit=None)
            available = runtime.state.has_processed_turn
            current_task = runtime.state.current_task() if available else None
    else:
        version = conversations.version(session_id)
        conversations.assert_active(session_id, expected_version=version)
        messages = conversations.get_messages(session_id, limit=None)
        available = False
    return {"messages": [message.model_dump() for message in messages],
            "session_version": version,
            "business_state_available": available,
            "current_task": current_task,
            "catalog_context": get_catalog().model_dump(mode="json"),
            "recovery_notice": None if available else
                "已恢复历史文字；服务端没有恢复这份会话的业务需求、参数和评估。"
                "继续选型前请重新说明并核对当前要求；历史文字不会自动成为当前方案。"}


@app.post("/sessions/{session_id}/proposal/invalidate")
async def invalidate_proposal(session_id: str, request: ProposalInvalidationRequest, http_request: Request) -> dict:
    async with _ordinary(http_request, mutation=True):
        return await orchestrator.invalidate_proposal(session_id, request.event)


@app.post("/proposal/view/invalidate")
async def invalidate_proposal_view(request: ProposalViewInvalidationRequest, http_request: Request) -> dict:
    async with _ordinary(http_request, mutation=True):
        return await orchestrator.invalidate_view(request.view_id, request.event)


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, http_request: Request) -> ChatResponse:
    try:
        if request.session_id:
            raw = http_request.headers.get("X-Session-Version")
            try:
                version = int(raw) if raw is not None else request.session_version
            except ValueError as exc:
                raise HTTPException(400, detail="会话版本无效，请刷新核实。") from exc
            request = request.model_copy(update={"session_version": version})
        async with _ordinary(http_request, mutation=True):
            return await orchestrator.chat(request)
    except ValueError as exc:
        if str(exc) in {"conversation_not_found", "conversation_in_trash", "conversation_version_changed", "dataset_epoch_changed"}:
            raise HTTPException(409, detail={"code": str(exc), "message":
                "这份聊天的状态已变化，旧请求未继续。请重新打开核实。"}) from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        code = getattr(exc, "code", str(exc))
        if code in {"conversation_not_found", "conversation_in_trash", "conversation_permanently_deleted",
                    "conversation_version_changed", "session_version_changed", "data_epoch_changed"}:
            raise HTTPException(409, detail={"code": code, "message":
                "这份聊天的状态已变化，旧请求未继续。请重新打开核实。"}) from exc
        raise HTTPException(status_code=502, detail=str(exc)) from exc
