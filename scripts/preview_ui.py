"""Real browser /chat verification, isolated synthetic conversations only.

Serves the unchanged production app/UI and records actual requests/responses.
No business fixtures, extra web endpoints, model or retrieval replacements.
Stop with Ctrl+C or create the run's printed .stop file for graceful shutdown.
The owned temporary SQLite database is then removed. Optional inherited history
is a read-only backup of a previous owned preview DB; evidence state is fresh.
"""

import argparse
from contextlib import closing
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from uuid import uuid4

import uvicorn
from fastapi import Request
from fastapi.responses import Response

from app.agents import orchestrator as orchestrator_module
from app.agents.orchestrator import Orchestrator
from app.config import get_settings
from app.llm.deepseek_client import DeepSeekClient
from app.services.catalog import get_catalog
from app.services.conversation_store import ConversationStore
from app.services.knowledge import DEFAULT_INDEX_PATH, _check_inputs, _read_index, model_digest, prepare_corpus


request_record = ContextVar("preview_browser_request", default=None)


class RecordedRealClient(DeepSeekClient):
    async def chat(self, **kwargs):
        record_dialogue(kwargs)
        return await super().chat(**kwargs)


def record_dialogue(kwargs):
    record = request_record.get()
    if record is not None:
        # No credentials, hidden reasoning or complete system prompt.
        record["model_dialogue_inputs"].append(deepcopy([
            {"role": m["role"], "content": m.get("content")}
            for m in kwargs["messages"] if m["role"] in {"user", "assistant"}
        ]))


def now():
    return datetime.now(timezone.utc).isoformat()


def history_source_path(history_db: Path) -> Path:
    source = history_db.resolve(strict=True)
    temporary_root = Path(tempfile.gettempdir()).resolve()
    if (not source.is_file() or source.name != "chat.db" or source.parent.parent != temporary_root
            or not source.parent.name.startswith("ui-preview-") or source.parent.name == "ui-preview-"):
        raise ValueError("历史来源只接受当前系统 Temp/ui-preview-*/chat.db 的既有预览数据库。")
    return source


def clone_history(source: Path, destination: Path) -> dict:
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    # SQLite backup reads a consistent snapshot; URI mode=ro prevents source
    # database writes. The target is only this run's fresh owned temporary DB.
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as previous:
        counts = {"sessions": previous.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
                  "messages": previous.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0]}
        with closing(sqlite3.connect(destination)) as current:
            previous.backup(current)
    after = hashlib.sha256(source.read_bytes()).hexdigest()
    if after != before:
        raise RuntimeError("历史来源在备份期间变化，请先停止其预览运行后重试。")
    return {"inherited_history": True, "history_source_database": str(source),
            "history_source_sha256": before, "history_source_sha256_after_backup": after,
            "history_source_unchanged": True, "inherited_history_counts": counts,
            "inherited_state": "stored_text_only; no requirement or tool-evidence state restored"}


def run(port: int, output: Path, *, client_type=RecordedRealClient, controlled_fault=None,
        history_db: Path | None = None, preserve_history: bool = False):
    import app.main as main

    settings = get_settings()
    if not settings.deepseek_api_key:
        raise RuntimeError("缺少现有模型配置，不能验证真实浏览器业务链。")
    original_index = hashlib.sha256(DEFAULT_INDEX_PATH.read_bytes()).hexdigest()
    index = _read_index(DEFAULT_INDEX_PATH)
    inputs = prepare_corpus().inputs(index.inputs.embedding)
    _check_inputs(index, inputs)
    run_id = uuid4().hex
    history_source = history_source_path(history_db) if history_db is not None else None
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"browser-{run_id}.json"
    stop_file = destination.with_suffix(".stop").resolve()
    root = Path(__file__).resolve().parents[1]
    report = {
        "run_id": run_id, "kind": "controlled_model_fault_real_services_isolated_db" if controlled_fault else "real_browser_real_model_formal_index_isolated_db",
        "started_at": now(), "model": settings.deepseek_model,
        "catalog_version": get_catalog().version,
        "formal_index_sha256": original_index, "formal_inputs_digest": model_digest(inputs),
        "ui_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in [root / "app/ui/index.html", *sorted((root / "app/ui/assets").glob("*"))]
                      if p.is_file()},
        "runtime_code_sha256": {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in sorted((root / "app").rglob("*.py"))},
        "requests": [], "cases": [], "status": "running_pending_browser_review",
        "process_id": os.getpid(), "stop_file": str(stop_file), "lifecycle_state": "preparing",
        "inherited_history": False, "temporary_database_cleaned": False, "shutdown_completed": False,
    }
    if controlled_fault:
        report["controlled_fault"] = controlled_fault
        report["model"] = "controlled-fault-only"

    def publish():
        report["formal_index_unchanged"] = hashlib.sha256(DEFAULT_INDEX_PATH.read_bytes()).hexdigest() == original_index
        report["turn_count"] = len(report["cases"])
        report["model_call_count"] = sum(len(c["model_dialogue_inputs"]) for c in report["cases"])
        staging = destination.with_suffix(".tmp")
        staging.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        staging.replace(destination)

    original_settings, original_orch, original_store = main.settings, main.orchestrator, main.conversations
    original_client = orchestrator_module.DeepSeekClient
    original_availability = getattr(main.app.state, "preview_chat_availability", None)
    owned_folder = None
    watcher_finished = threading.Event()
    stop_observed = threading.Event()
    watcher = None
    try:
        with tempfile.TemporaryDirectory(prefix="ui-preview-") as folder:
            owned_folder = Path(folder).resolve()
            database = owned_folder / "chat.db"
            report.update({"temporary_directory": str(owned_folder), "temporary_database": str(database)})
            if history_source is not None:
                report.update(clone_history(history_source, database))
            isolated = settings.model_copy(update={"saas_database_url": "sqlite:///" + str(database)})
            main.settings = isolated
            main.orchestrator = Orchestrator(isolated)
            main.conversations = ConversationStore(isolated)
            orchestrator_module.DeepSeekClient = client_type
            main.app.state.preview_chat_availability = getattr(client_type, "availability", None)

            @main.app.middleware("http")
            async def record_browser(request: Request, call_next):
                report["requests"].append({"method": request.method, "path": request.url.path})
                if request.method != "POST" or request.url.path != "/chat":
                    return await call_next(request)
                payload = await request.json()
                record = {"user": payload.get("message"), "request_session_id": payload.get("session_id"),
                          "started_at": now(), "model_dialogue_inputs": []}
                token = request_record.set(record)
                try:
                    response = await call_next(request)
                    body = b"".join([chunk async for chunk in response.body_iterator])
                    record["http_status"] = response.status_code
                    record["response"] = json.loads(body)
                    if response.status_code == 200:
                        sid = record["response"]["session_id"]
                        history = [m.model_dump() for m in main.conversations.get_messages(sid, limit=100)]
                        record["history"] = history
                        record["text_matches_stored"] = bool(history and history[-1]["content"] == record["response"]["message"])
                        previous = [{"role": message["role"], "content": message["content"]}
                                    for message in history[:-2][-30:]]
                        first = record["model_dialogue_inputs"][0] if record["model_dialogue_inputs"] else []
                        record["next_model_uses_stored_history"] = (
                            first[:len(previous)] == previous if record["model_dialogue_inputs"] else None)
                    record["finished_at"] = now()
                    report["cases"].append(record)
                    publish()
                    print(f"真实浏览器回合 {len(report['cases'])}: HTTP {response.status_code}; "
                          f"模型调用 {len(record['model_dialogue_inputs'])}; {record['user']}", flush=True)
                    return Response(content=body, status_code=response.status_code, headers=dict(response.headers))
                finally:
                    request_record.reset(token)

            server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning"))

            def watch_owned_stop_file():
                while not watcher_finished.wait(0.25):
                    if stop_file.is_file():
                        stop_observed.set()
                        server.should_exit = True
                        return

            watcher = threading.Thread(target=watch_owned_stop_file, name=f"preview-stop-{run_id}", daemon=True)
            watcher.start()
            report["lifecycle_state"] = "running"
            publish()
            print(f"隔离真实预览：http://127.0.0.1:{port}\n记录：{destination.resolve()}\n"
                  + ("使用隔离模型故障替身、真实领域服务；不能证明正常模型。" if controlled_fault else "使用正式资料/索引和现有真实模型；不写用户会话。")
                  + f"\nPID：{os.getpid()}\n临时数据库：{database}\n创建此文件即可安全停止：{stop_file}\nCtrl+C 也可结束。", flush=True)
            try:
                server.run()
                report["stop_reason"] = "owned_stop_file" if stop_observed.is_set() else "server_returned"
                report["shutdown_completed"] = bool(server.started and not server.force_exit)
            except KeyboardInterrupt:
                report["stop_reason"] = "keyboard_interrupt"
                report["shutdown_completed"] = bool(server.started and server.should_exit and not server.force_exit)
            finally:
                report["lifecycle_state"] = "stopping"
                watcher_finished.set()
                watcher.join(timeout=1)
            if preserve_history:
                # The server is stopped; keep this run's user text before its
                # owned temporary DB is cleaned. Never touch a formal DB.
                archive = (output / f"history-{run_id}.sqlite3").resolve()
                with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as previous:
                    with closing(sqlite3.connect(archive)) as current:
                        previous.backup(current)
                report.update({"preserved_history_database": str(archive),
                               "preserved_history_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()})
        # Only the successful TemporaryDirectory exit can establish cleanup.
        report["temporary_database_cleaned"] = not owned_folder.exists()
        report["lifecycle_state"] = "stopped" if report["shutdown_completed"] else "interrupted"
    except BaseException as error:
        report["stop_reason"] = "keyboard_interrupt" if isinstance(error, KeyboardInterrupt) else "preview_error"
        report["lifecycle_state"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        report["preview_error_type"] = type(error).__name__
        raise
    finally:
        watcher_finished.set()
        if watcher is not None:
            watcher.join(timeout=1)
        main.settings, main.orchestrator, main.conversations = original_settings, original_orch, original_store
        orchestrator_module.DeepSeekClient = original_client
        main.app.state.preview_chat_availability = original_availability
        report["finished_at"] = now()
        report["status"] = ("recorded_pending_semantic_and_user_browser_acceptance" if report["shutdown_completed"]
                            else "preview_stopped_incomplete_pending_review")
        publish()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8007)
    parser.add_argument("--output", type=Path, default=Path("data/ui_review"))
    parser.add_argument("--history-db", type=Path, help="只读继承 Temp/ui-preview-*/chat.db 的真实已存聊天文本")
    args = parser.parse_args()
    run(args.port, args.output, history_db=args.history_db)
