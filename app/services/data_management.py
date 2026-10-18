"""Local, model-independent record lifecycle and durable operation receipts.

All mutations are one SQLite transaction. The caller coordinates active work;
the persisted epoch/version checks are an additional fence for stale requests.
No file outside the configured SaaS database is removed by this service.
"""

from datetime import datetime, timezone
import hashlib
import json
import os
from uuid import uuid4

from app.db.session import connect_saas, saas_database_path
from app.services.quotes import QuoteStore


class DataManagementError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class DataManagementStore:
    def __init__(self, settings):
        self.settings = settings

    @staticmethod
    def _meta(db):
        row = db.execute("SELECT * FROM data_meta WHERE id = 1").fetchone()
        if row is None:
            raise DataManagementError("data_position_unavailable")
        return dict(row)

    def validate_scope(self, epoch):
        with connect_saas(self.settings) as db:
            meta = self._meta(db)
            if ((epoch is None and meta["epoch_requires_scope"])
                    or (epoch is not None and epoch != meta["epoch"])):
                raise DataManagementError("dataset_epoch_changed")
            return meta

    @staticmethod
    def _counts(db):
        count = lambda sql: db.execute(sql).fetchone()[0]
        return {
            "conversations": count("SELECT COUNT(*) FROM conversations WHERE deleted_at IS NULL"),
            "messages": count("""SELECT COUNT(*) FROM conversation_messages m JOIN conversations c
                ON c.id = m.conversation_id WHERE c.deleted_at IS NULL"""),
            "quotes": count("""SELECT COUNT(*) FROM quotes q JOIN quote_states s ON s.quote_id = q.quote_id
                WHERE s.deleted_at IS NULL AND s.permanently_deleted_at IS NULL"""),
            "trash_conversations": count("SELECT COUNT(*) FROM conversations WHERE deleted_at IS NOT NULL"),
            "trash_messages": count("""SELECT COUNT(*) FROM conversation_messages m JOIN conversations c
                ON c.id = m.conversation_id WHERE c.deleted_at IS NOT NULL"""),
            "trash_quotes": count("""SELECT COUNT(*) FROM quotes q JOIN quote_states s ON s.quote_id = q.quote_id
                WHERE s.deleted_at IS NOT NULL AND s.permanently_deleted_at IS NULL"""),
        }

    def info(self):
        with connect_saas(self.settings) as db:
            db.execute("BEGIN")
            meta = self._meta(db)
            actual_path = str(saas_database_path(self.settings).resolve())
            location_key = actual_path.casefold() if os.name == "nt" else actual_path
            return {"dataset_id": meta["dataset_id"], "epoch": meta["epoch"],
                    "location_id": hashlib.sha256(location_key.encode("utf-8")).hexdigest(),
                    "revision": meta["revision"],
                    "epoch_requires_scope": bool(meta["epoch_requires_scope"]),
                    "database_path": actual_path,
                    "counts": self._counts(db),
                    "last_clear": json.loads(meta["last_clear_json"]) if meta["last_clear_json"] else None}

    def list_trash(self):
        with connect_saas(self.settings) as db:
            db.execute("BEGIN")
            chats = db.execute("""SELECT id AS object_id, title, created_at, updated_at,
                deleted_at, version, (SELECT COUNT(*) FROM conversation_messages m
                WHERE m.conversation_id = c.id) AS message_count FROM conversations c
                WHERE deleted_at IS NOT NULL""").fetchall()
            result = [dict(row) | {"object_type": "chat"} for row in chats]
            rows = db.execute("""SELECT q.*, s.deleted_at, s.version, a.action_ref,
                a.session_id, a.proposal_id FROM quotes q JOIN quote_states s
                ON s.quote_id = q.quote_id JOIN quote_actions a ON a.quote_id = q.quote_id
                WHERE s.deleted_at IS NOT NULL AND s.permanently_deleted_at IS NULL""").fetchall()
            for row in rows:
                quote = QuoteStore._decode(row)
                result.append({"object_type": "quote", "object_id": quote.quote_id,
                    "quote_id": quote.quote_id, "saved_at": quote.saved_at,
                    "title": quote.snapshot.selected_plan_name,
                    "selected_plan_name": quote.snapshot.selected_plan_name,
                    "deleted_at": row["deleted_at"], "version": row["version"],
                    "source_session_id": quote.snapshot.session_id,
                    "source_conversation_status": QuoteStore._source_status(db, quote.snapshot.session_id)})
            return sorted(result, key=lambda item: (item["deleted_at"], item["object_id"]), reverse=True)

    def resolve(self, action_ref):
        with connect_saas(self.settings) as db:
            row = db.execute("SELECT result_json FROM management_actions WHERE action_ref = ?", (action_ref,)).fetchone()
            if row is not None:
                return json.loads(row["result_json"])
            meta = self._meta(db)
            return {"status": "not_found", "code": "management_action_not_found",
                    "message": "当前记录中未找到这次管理操作；不能据此断言旧记录从未处理。",
                    "action_ref": action_ref, "dataset_id": meta["dataset_id"],
                    "epoch": meta["epoch"], "revision": meta["revision"]}

    @staticmethod
    def _before_commit(db, result):
        """Private deterministic fault seam; never exposed as a request option."""

    @staticmethod
    def _commit(db):
        db.commit()

    def _record_rejection(self, db, meta, action_ref, operation, object_type, object_id, code):
        messages = {
            "data_revision_changed": "记录已变化，本次未执行；请重新核对数量和范围后确认。",
            "object_not_found": "当前记录中未找到该对象，本次未执行。",
            "object_already_in_trash": "对象已在最近删除，本次没有重复移动。",
            "object_not_in_trash": "对象目前不在最近删除，本次未执行恢复或永久删除。",
        }
        result = {"status": "not_completed", "code": code, "message": messages[code],
            "action_ref": action_ref, "operation": operation, "object_type": object_type,
            "object_id": object_id, "dataset_id": meta["dataset_id"], "epoch": meta["epoch"],
            "revision": meta["revision"], "completed_count": 0, "remaining_count": 1,
            "completed_at": datetime.now(timezone.utc).isoformat()}
        db.execute("""INSERT INTO management_actions (action_ref, operation, object_type, object_id, result_json)
            VALUES (?, ?, ?, ?, ?)""", (action_ref, operation, object_type, object_id, json.dumps(result, ensure_ascii=False)))
        self._before_commit(db, result)
        self._commit(db)
        return self.resolve(action_ref)

    @staticmethod
    def _object_row(db, object_type, object_id):
        table, key = ("conversations", "id") if object_type == "chat" else ("quote_states", "quote_id")
        row = db.execute(f"SELECT * FROM {table} WHERE {key} = ?", (object_id,)).fetchone()
        if row is None or (object_type == "quote" and row["permanently_deleted_at"]):
            raise DataManagementError("object_not_found")
        return row

    @staticmethod
    def _purge(db, object_type, object_id, row, now):
        if object_type == "chat":
            db.execute("""INSERT INTO conversation_tombstones (id, version, permanently_deleted_at)
                VALUES (?, ?, ?)""", (object_id, row["version"] + 1, now))
            db.execute("DELETE FROM conversations WHERE id = ?", (object_id,))
        else:
            # Only identity/time/status and immutable action identity survive.
            # The full snapshot row is physically removed from application tables.
            db.execute("DELETE FROM quotes WHERE quote_id = ?", (object_id,))
            db.execute("""UPDATE quote_states SET deleted_at = NULL,
                permanently_deleted_at = ?, version = version + 1 WHERE quote_id = ?""", (now, object_id))

    def perform(self, action_ref, operation, expected_epoch, expected_revision=None,
                object_type=None, object_id=None):
        if operation not in {"trash", "restore", "purge", "empty_trash", "clear"}:
            raise DataManagementError("invalid_management_operation")
        single = operation in {"trash", "restore", "purge"}
        if (single and (object_type not in {"chat", "quote"} or not object_id)
                or not single and (object_type is not None or object_id is not None)):
            raise DataManagementError("invalid_management_object")
        db = connect_saas(self.settings, allow_delete=True)
        try:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM management_actions WHERE action_ref = ?", (action_ref,)).fetchone()
            if existing is not None:
                if (existing["operation"], existing["object_type"], existing["object_id"]) != (operation, object_type, object_id):
                    raise DataManagementError("management_action_mismatch")
                return json.loads(existing["result_json"])
            meta = self._meta(db)
            if expected_epoch != meta["epoch"]:
                raise DataManagementError("dataset_epoch_changed")
            if expected_revision is not None and expected_revision != meta["revision"]:
                return self._record_rejection(db, meta, action_ref, operation, object_type, object_id,
                                              "data_revision_changed")
            now = datetime.now(timezone.utc).isoformat()
            completed = 0
            if single:
                try:
                    row = self._object_row(db, object_type, object_id)
                except DataManagementError as exc:
                    return self._record_rejection(db, meta, action_ref, operation, object_type, object_id, exc.code)
                deleted = row["deleted_at"] is not None
                if operation == "trash" and deleted:
                    return self._record_rejection(db, meta, action_ref, operation, object_type, object_id,
                                                  "object_already_in_trash")
                if operation in {"restore", "purge"} and not deleted:
                    return self._record_rejection(db, meta, action_ref, operation, object_type, object_id,
                                                  "object_not_in_trash")
                if operation == "purge":
                    self._purge(db, object_type, object_id, row, now)
                else:
                    table, key = ("conversations", "id") if object_type == "chat" else ("quote_states", "quote_id")
                    db.execute(f"UPDATE {table} SET deleted_at = ?, version = version + 1 WHERE {key} = ?",
                               (now if operation == "trash" else None, object_id))
                completed = 1
            elif operation == "empty_trash":
                for object_type_value, table, key in (("chat", "conversations", "id"), ("quote", "quote_states", "quote_id")):
                    for row in db.execute(f"SELECT * FROM {table} WHERE deleted_at IS NOT NULL").fetchall():
                        self._purge(db, object_type_value, row[key], row, now)
                        completed += 1
            else:
                counts = self._counts(db)
                completed = sum(counts[key] for key in ("conversations", "quotes", "trash_conversations", "trash_quotes"))
                db.execute("DELETE FROM quote_actions")
                db.execute("DELETE FROM quotes")
                db.execute("DELETE FROM quote_states")
                db.execute("DELETE FROM conversations")
                db.execute("DELETE FROM conversation_tombstones")
                db.execute("DELETE FROM management_actions")
                # Erase old AUTOINCREMENT history too; epoch fences old message IDs.
                db.execute("DELETE FROM sqlite_sequence WHERE name = 'conversation_messages'")
                meta["epoch"] = str(uuid4())
                db.execute("UPDATE data_meta SET epoch = ?, epoch_requires_scope = 1 WHERE id = 1", (meta["epoch"],))
            meta["revision"] += 1
            db.execute("UPDATE data_meta SET revision = ? WHERE id = 1", (meta["revision"],))
            messages = {"trash": "已移入最近删除，内容仍保存在本机，可以恢复。",
                "restore": "已恢复历史记录；旧业务状态、方案和保存许可未恢复。",
                "purge": "已永久删除，应用内无法恢复；不承诺磁盘安全擦除。",
                "empty_trash": "最近删除已清空，应用内无法恢复。",
                "clear": "当前应用记录已清除，可以重新开始。其他数据位置、工程记录和外部副本未处理。"}
            result = {"status": "completed", "code": "management_completed", "message": messages[operation],
                "action_ref": action_ref, "operation": operation, "object_type": object_type,
                "object_id": object_id, "dataset_id": meta["dataset_id"], "epoch": meta["epoch"],
                "revision": meta["revision"], "completed_at": now,
                "completed_count": completed, "remaining_count": 0}
            if operation == "clear":
                db.execute("UPDATE data_meta SET last_clear_json = ? WHERE id = 1", (json.dumps(result, ensure_ascii=False),))
            db.execute("""INSERT INTO management_actions (action_ref, operation, object_type, object_id, result_json)
                VALUES (?, ?, ?, ?, ?)""", (action_ref, operation, object_type, object_id, json.dumps(result, ensure_ascii=False)))
            self._before_commit(db, result)
            self._commit(db)
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()
        # Report completion only from a fresh committed read.
        checked = self.resolve(action_ref)
        if checked["status"] != "completed":
            raise DataManagementError("management_commit_unverified")
        return checked
