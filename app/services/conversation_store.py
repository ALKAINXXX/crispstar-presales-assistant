# Modified from sairam-s0/personal-ai-assistant (Apache-2.0).
# Changes: isolated presales history and added scoped, transactional conversation operations.
# Source and license: see README.md.

from uuid import uuid4

from app.config import Settings
from app.db.session import connect_saas
from app.schemas.chat import Message


class ConversationStore:
    """Conversation persistence for the isolated SaaS runtime."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @staticmethod
    def _check_active(db, session_id, *, expected_epoch=None, expected_version=None):
        meta = db.execute("SELECT epoch FROM data_meta WHERE id = 1").fetchone()
        if expected_epoch is not None and meta["epoch"] != expected_epoch:
            raise ValueError("dataset_epoch_changed")
        row = db.execute("SELECT * FROM conversations WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise ValueError("conversation_not_found")
        if row["deleted_at"] is not None:
            raise ValueError("conversation_in_trash")
        if expected_version is not None and row["version"] != expected_version:
            raise ValueError("conversation_version_changed")
        return row

    def status(self, session_id: str) -> dict:
        with connect_saas(self.settings) as db:
            db.execute("BEGIN")
            row = db.execute("SELECT id, deleted_at, version FROM conversations WHERE id = ?", (session_id,)).fetchone()
            if row is not None:
                return {"id": session_id, "status": "trash" if row["deleted_at"] else "active",
                        "version": row["version"]}
            tombstone = db.execute("SELECT version FROM conversation_tombstones WHERE id = ?", (session_id,)).fetchone()
            return {"id": session_id, "status": "permanently_deleted" if tombstone else "not_found",
                    "version": tombstone["version"] if tombstone else None}

    def assert_active(self, session_id: str, *, expected_version=None) -> dict:
        with connect_saas(self.settings) as db:
            db.execute("BEGIN")
            row = self._check_active(db, session_id, expected_version=expected_version)
            if expected_version is None and row["version"] > 0:
                raise ValueError("conversation_version_changed")
            return dict(row)

    def version(self, session_id: str) -> int:
        with connect_saas(self.settings) as db:
            return self._check_active(db, session_id)["version"]

    def ensure_conversation(self, session_id: str | None, first_message: str,
                            *, expected_epoch=None, expected_version=None) -> str:
        if session_id:
            with connect_saas(self.settings) as db:
                db.execute("BEGIN")
                self._check_active(db, session_id, expected_epoch=expected_epoch,
                                   expected_version=expected_version)
                return session_id

        new_id = str(uuid4())
        title = first_message.strip().replace("\n", " ")[:60] or "New chat"
        with connect_saas(self.settings) as db:
            db.execute("BEGIN IMMEDIATE")
            if expected_epoch is not None:
                epoch = db.execute("SELECT epoch FROM data_meta WHERE id = 1").fetchone()["epoch"]
                if epoch != expected_epoch:
                    raise ValueError("dataset_epoch_changed")
            db.execute(
                "INSERT INTO conversations (id, title) VALUES (?, ?)",
                (new_id, title),
            )
            db.execute("UPDATE data_meta SET revision = revision + 1 WHERE id = 1")
        return new_id

    def add_message(self, session_id: str, role: str, content: str,
                    *, expected_epoch=None, expected_version=None) -> None:
        with connect_saas(self.settings) as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_active(db, session_id, expected_epoch=expected_epoch,
                               expected_version=expected_version)
            db.execute(
                """
                INSERT INTO conversation_messages (conversation_id, role, content)
                VALUES (?, ?, ?)
                """,
                (session_id, role, content),
            )
            db.execute(
                "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (session_id,),
            )
            db.execute("UPDATE data_meta SET revision = revision + 1 WHERE id = 1")

    def add_turn(self, session_id: str, user_text: str, assistant_text: str,
                 *, expected_epoch=None, expected_version=None) -> str:
        """Commit the user and the exact displayed assistant text together."""
        with connect_saas(self.settings) as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_active(db, session_id, expected_epoch=expected_epoch,
                               expected_version=expected_version)
            db.execute(
                "INSERT INTO conversation_messages (conversation_id, role, content) VALUES (?, ?, ?)",
                (session_id, "user", user_text),
            )
            cursor = db.execute(
                "INSERT INTO conversation_messages (conversation_id, role, content) VALUES (?, ?, ?)",
                (session_id, "assistant", assistant_text),
            )
            db.execute("UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (session_id,))
            db.execute("UPDATE data_meta SET revision = revision + 1 WHERE id = 1")
            return str(cursor.lastrowid)

    def get_messages(self, session_id: str, limit: int | None = 30) -> list[Message]:
        with connect_saas(self.settings) as db:
            db.execute("BEGIN")
            self._check_active(db, session_id)
            rows = db.execute(
                """
                SELECT id, role, content
                FROM conversation_messages
                WHERE conversation_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, -1 if limit is None else limit),
            ).fetchall()

        # SaaS history is ordinary dialogue data only. Stored system rows are
        # intentionally not promoted into the active model context.
        return [
            Message(id=str(row["id"]), role=row["role"], content=row["content"])
            for row in reversed(rows)
            if row["role"] in {"user", "assistant"}
        ]

    def list_conversations(self, limit: int | None = None) -> list[dict]:
        with connect_saas(self.settings) as db:
            rows = db.execute(
                """
                SELECT c.id, c.title, c.created_at, c.updated_at, c.version,
                    (SELECT MAX(m.id) FROM conversation_messages m WHERE m.conversation_id = c.id) AS last_message_id
                FROM conversations c
                WHERE deleted_at IS NULL
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (-1 if limit is None else limit,),
            ).fetchall()
        return [dict(row) for row in rows]
