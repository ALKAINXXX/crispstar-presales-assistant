# Modified from sairam-s0/personal-ai-assistant (Apache-2.0).
# Changes: extended SQLite connections and added presales storage and migrations.
# Source and license: see README.md.

import sqlite3
from pathlib import Path
from uuid import uuid4

from app.config import Settings


_SQLITE_PREFIX = "sqlite:///"


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def database_path_from_url(database_url: str) -> Path:
    if not database_url.startswith(_SQLITE_PREFIX):
        raise ValueError("Only sqlite:/// database URLs are supported.")

    path = Path(database_url.removeprefix(_SQLITE_PREFIX))
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def saas_database_path(settings: Settings) -> Path:
    """Dedicated SaaS conversation/business database path."""
    return database_path_from_url(settings.saas_database_url)


def _connect_path(path: Path, *, allow_delete: bool = False) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, factory=_ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    # Only the local management transaction opts into physical Quote removal.
    # Ordinary persistence connections retain the old accidental-delete guard.
    connection.create_function("w11_can_delete", 0, lambda: int(allow_delete))
    return connection


def connect_saas(settings: Settings, *, allow_delete: bool = False) -> sqlite3.Connection:
    """Connect to the configured presales database."""
    return _connect_path(saas_database_path(settings), allow_delete=allow_delete)


def init_saas_db(settings: Settings) -> None:
    """Initialize and migrate presales tables while preserving stored records."""
    with connect_saas(settings, allow_delete=True) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS conversation_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS quotes (
                quote_id TEXT PRIMARY KEY NOT NULL,
                saved_at TEXT NOT NULL,
                snapshot_version INTEGER NOT NULL CHECK (snapshot_version = 1),
                snapshot_json TEXT NOT NULL,
                snapshot_sha256 TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS quote_actions (
                action_ref TEXT PRIMARY KEY NOT NULL,
                session_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                quote_id TEXT NOT NULL UNIQUE
            );

            """
        )
        # Apply additive, transactional migrations in place. The frozen
        # snapshot and its original action identity are copied without edits.
        db.execute("BEGIN IMMEDIATE")
        columns = {row["name"] for row in db.execute("PRAGMA table_info(conversations)")}
        if "deleted_at" not in columns:
            db.execute("ALTER TABLE conversations ADD COLUMN deleted_at TEXT")
        if "version" not in columns:
            db.execute("ALTER TABLE conversations ADD COLUMN version INTEGER NOT NULL DEFAULT 0")
        for name in ("quotes_no_delete", "quote_actions_no_delete", "quote_actions_no_update"):
            db.execute(f"DROP TRIGGER IF EXISTS {name}")
        if db.execute("PRAGMA foreign_key_list(quote_actions)").fetchall():
            db.execute("ALTER TABLE quote_actions RENAME TO quote_actions_w10")
            db.execute("""CREATE TABLE quote_actions (
                action_ref TEXT PRIMARY KEY NOT NULL, session_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL, quote_id TEXT NOT NULL UNIQUE)""")
            db.execute("""INSERT INTO quote_actions (action_ref, session_id, proposal_id, quote_id)
                SELECT action_ref, session_id, proposal_id, quote_id FROM quote_actions_w10""")
            db.execute("DROP TABLE quote_actions_w10")
        statements = (
            """CREATE INDEX IF NOT EXISTS conversation_messages_by_session
                ON conversation_messages (conversation_id, id)""",
            """CREATE TABLE IF NOT EXISTS quote_states (
                quote_id TEXT PRIMARY KEY NOT NULL, saved_at TEXT NOT NULL,
                deleted_at TEXT, permanently_deleted_at TEXT,
                version INTEGER NOT NULL DEFAULT 0)""",
            """CREATE TABLE IF NOT EXISTS conversation_tombstones (
                id TEXT PRIMARY KEY NOT NULL, version INTEGER NOT NULL,
                permanently_deleted_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS data_meta (
                id INTEGER PRIMARY KEY CHECK (id = 1), dataset_id TEXT NOT NULL,
                epoch TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
                epoch_requires_scope INTEGER NOT NULL DEFAULT 1, last_clear_json TEXT)""",
            """CREATE TABLE IF NOT EXISTS management_actions (
                action_ref TEXT PRIMARY KEY NOT NULL, operation TEXT NOT NULL,
                object_type TEXT, object_id TEXT, result_json TEXT NOT NULL)""",
            """CREATE TRIGGER IF NOT EXISTS quotes_no_update BEFORE UPDATE ON quotes
                BEGIN SELECT RAISE(ABORT, 'Quotes are immutable'); END""",
            """CREATE TRIGGER quotes_no_delete BEFORE DELETE ON quotes
                WHEN w11_can_delete() = 0
                BEGIN SELECT RAISE(ABORT, 'Quotes are immutable; use data management to delete'); END""",
            """CREATE TRIGGER quote_actions_no_update BEFORE UPDATE ON quote_actions
                BEGIN SELECT RAISE(ABORT, 'Quote actions are immutable'); END""",
            """CREATE TRIGGER quote_actions_no_delete BEFORE DELETE ON quote_actions
                WHEN w11_can_delete() = 0
                BEGIN SELECT RAISE(ABORT, 'Quote actions are immutable; use data management to delete'); END""",
        )
        for statement in statements:
            db.execute(statement)
        db.execute("""INSERT OR IGNORE INTO data_meta (id, dataset_id, epoch)
            VALUES (1, ?, ?)""", (str(uuid4()), str(uuid4())))
        # Clients must identify both the active location and generation.
        # An older open page must refresh before issuing any mutation.
        db.execute("UPDATE data_meta SET epoch_requires_scope = 1 WHERE id = 1")
        db.execute("""INSERT OR IGNORE INTO quote_states (quote_id, saved_at)
            SELECT quote_id, saved_at FROM quotes""")
