"""Single-process SQLite Quote storage; no client fact-package write API.

Creation is private to Orchestrator's existing session order boundary. History
reads deserialize saved facts only, without catalog, model or retrieval calls.
"""

from datetime import datetime, timezone
import hashlib
import re
from uuid import uuid4

from app.db.session import connect_saas
from app.schemas.proposal import Proposal
from app.schemas.quote import Quote


class QuoteReadError(RuntimeError):
    def __init__(self, code="quote_read_unavailable", *, quote_id=None, saved_at=None,
                 current_status=None):
        self.code = code
        self.quote_id = quote_id
        self.saved_at = saved_at
        self.current_status = current_status
        super().__init__(code)


class QuoteActionOwnershipError(ValueError):
    code = "action_not_owned"


class QuoteWriteError(RuntimeError):
    def __init__(self, *, definitive: bool):
        self.definitive = definitive
        self.code = "quote_transaction_rolled_back" if definitive else "quote_commit_unverified"
        super().__init__(self.code)


class QuoteStore:
    def __init__(self, settings):
        self.settings = settings

    @staticmethod
    def _decode(row, source_conversation_status="active"):
        raw = row["snapshot_json"]
        if hashlib.sha256(raw.encode("utf-8")).hexdigest() != row["snapshot_sha256"]:
            raise QuoteReadError("quote_snapshot_corrupt")
        quote = Quote(quote_id=row["quote_id"], saved_at=row["saved_at"],
            snapshot_version=row["snapshot_version"], snapshot=Proposal.model_validate_json(raw),
            source_conversation_status=source_conversation_status)
        if ("action_ref" in row.keys() and (row["action_ref"] != quote.snapshot.action_ref
                or row["session_id"] != quote.snapshot.session_id
                or row["proposal_id"] != quote.snapshot.proposal_id)):
            raise QuoteReadError("quote_action_corrupt")
        return quote

    @classmethod
    def _find_action(cls, db, session_id, action_ref):
        row = db.execute("""SELECT a.action_ref, a.session_id, a.proposal_id,
            a.quote_id AS original_quote_id, s.saved_at AS original_saved_at,
            s.deleted_at, s.permanently_deleted_at, q.*
            FROM quote_actions a LEFT JOIN quotes q ON q.quote_id = a.quote_id
            LEFT JOIN quote_states s ON s.quote_id = a.quote_id
            WHERE a.action_ref = ?""", (action_ref,)).fetchone()
        if row is None:
            return None
        if row["session_id"] != session_id:
            raise QuoteActionOwnershipError("动作不属于这份会话")
        cls._check_readable(row, row["original_quote_id"], row["original_saved_at"])
        return cls._decode(row, cls._source_status(db, row["session_id"]))

    @staticmethod
    def _check_readable(row, quote_id, saved_at):
        if row["permanently_deleted_at"] is not None:
            raise QuoteReadError("quote_permanently_deleted", quote_id=quote_id,
                                 saved_at=saved_at, current_status="permanently_deleted")
        if row["deleted_at"] is not None:
            raise QuoteReadError("quote_in_trash", quote_id=quote_id,
                                 saved_at=saved_at, current_status="trash")
        if row["snapshot_json"] is None:
            raise QuoteReadError("quote_snapshot_corrupt")

    @staticmethod
    def _validate_id(quote_id):
        if type(quote_id) is not str or not re.fullmatch(r"Q-[0-9A-F]{32}", quote_id):
            raise QuoteReadError("invalid_quote_id")

    def action_status(self, session_id, action_ref) -> dict | None:
        try:
            with connect_saas(self.settings) as db:
                db.execute("BEGIN")
                row = db.execute("""SELECT a.*, s.saved_at, s.deleted_at,
                    s.permanently_deleted_at FROM quote_actions a
                    LEFT JOIN quote_states s ON s.quote_id = a.quote_id
                    WHERE a.action_ref = ?""", (action_ref,)).fetchone()
                if row is None:
                    return None
                if row["session_id"] != session_id:
                    raise QuoteActionOwnershipError("动作不属于这份会话")
                return {"quote_id": row["quote_id"], "saved_at": row["saved_at"],
                        "current_status": "permanently_deleted" if row["permanently_deleted_at"] else
                                          "trash" if row["deleted_at"] else "active"}
        except QuoteActionOwnershipError:
            raise
        except Exception as exc:
            raise QuoteReadError() from exc

    def get_status(self, quote_id) -> dict:
        self._validate_id(quote_id)
        try:
            with connect_saas(self.settings) as db:
                db.execute("BEGIN")
                row = db.execute("""SELECT s.*, a.session_id FROM quote_states s
                    LEFT JOIN quote_actions a ON a.quote_id = s.quote_id
                    WHERE s.quote_id = ?""", (quote_id,)).fetchone()
                if row is None:
                    raise QuoteReadError("quote_not_found")
                return {"quote_id": quote_id, "saved_at": row["saved_at"], "version": row["version"],
                        "source_session_id": row["session_id"],
                        "source_conversation_status": self._source_status(db, row["session_id"]),
                        "current_status": "permanently_deleted" if row["permanently_deleted_at"] else
                                          "trash" if row["deleted_at"] else "active"}
        except QuoteReadError:
            raise
        except Exception as exc:
            raise QuoteReadError() from exc

    @staticmethod
    def _source_status(db, session_id):
        row = db.execute("SELECT deleted_at FROM conversations WHERE id = ?", (session_id,)).fetchone()
        if row is not None:
            return "trash" if row["deleted_at"] else "active"
        tombstone = db.execute("SELECT 1 FROM conversation_tombstones WHERE id = ?", (session_id,)).fetchone()
        return "permanently_deleted" if tombstone else "not_found"

    def list_quotes(self) -> list[dict]:
        try:
            with connect_saas(self.settings) as db:
                db.execute("BEGIN")
                rows = db.execute("""SELECT q.*, a.action_ref, a.session_id, a.proposal_id
                    FROM quotes q JOIN quote_actions a ON a.quote_id = q.quote_id
                    JOIN quote_states s ON s.quote_id = q.quote_id
                    WHERE s.deleted_at IS NULL AND s.permanently_deleted_at IS NULL
                    ORDER BY q.saved_at DESC, q.quote_id""").fetchall()
                result = []
                for row in rows:
                    quote = self._decode(row)
                    snapshot = quote.snapshot
                    result.append({"quote_id": quote.quote_id, "saved_at": quote.saved_at,
                        "selected_plan_name": snapshot.selected_plan_name,
                        "paid_seats": snapshot.pricing.paid_seats, "months": snapshot.pricing.months,
                        "base_price_minor": snapshot.pricing.base_price_minor,
                        "source_session_id": snapshot.session_id,
                        "source_conversation_status": self._source_status(db, snapshot.session_id)})
                return result
        except QuoteReadError:
            raise
        except Exception as exc:
            raise QuoteReadError() from exc

    def find_action(self, session_id, action_ref):
        try:
            with connect_saas(self.settings) as db:
                db.execute("BEGIN")
                return self._find_action(db, session_id, action_ref)
        except (QuoteActionOwnershipError, QuoteReadError):
            raise
        except Exception as exc:
            raise QuoteReadError() from exc

    def get(self, quote_id: str) -> Quote:
        self._validate_id(quote_id)
        try:
            with connect_saas(self.settings) as db:
                db.execute("BEGIN")
                row = db.execute("""SELECT s.saved_at AS original_saved_at,
                    s.deleted_at, s.permanently_deleted_at, q.* FROM quote_states s
                    LEFT JOIN quotes q ON q.quote_id = s.quote_id WHERE s.quote_id = ?""", (quote_id,)).fetchone()
                if row is None:
                    raise QuoteReadError("quote_not_found")
                self._check_readable(row, quote_id, row["original_saved_at"])
                quote = self._decode(row)
                return quote.model_copy(update={"source_conversation_status": self._source_status(db, quote.snapshot.session_id)})
        except QuoteReadError:
            raise
        except Exception as exc:
            raise QuoteReadError() from exc

    @staticmethod
    def _before_commit(db, proposal):
        """Private test seam inside the transaction; never a request option."""

    @staticmethod
    def _commit(db):
        db.commit()

    def _create(self, proposal: Proposal, *, expected_epoch=None,
                expected_conversation_version=None) -> Quote:
        # Called only after server ownership and current facts were checked.
        # No mapping/JSON or claimed confirmed field can substitute for it.
        if not isinstance(proposal, Proposal):
            raise TypeError("Creation requires a frozen server Proposal")
        db = None
        started = commit_attempted = False
        try:
            db = connect_saas(self.settings)
            db.execute("BEGIN IMMEDIATE")
            started = True
            if expected_epoch is not None:
                meta = db.execute("SELECT epoch FROM data_meta WHERE id = 1").fetchone()
                if meta["epoch"] != expected_epoch:
                    raise ValueError("dataset_epoch_changed")
            existing = self._find_action(db, proposal.session_id, proposal.action_ref)
            if existing is not None:
                db.rollback()
                return existing
            conversation = db.execute("SELECT deleted_at, version FROM conversations WHERE id = ?",
                                      (proposal.session_id,)).fetchone()
            if conversation is None or conversation["deleted_at"] is not None:
                raise ValueError("conversation_not_active")
            if (expected_conversation_version is not None
                    and conversation["version"] != expected_conversation_version):
                raise ValueError("conversation_version_changed")
            raw = proposal.model_dump_json()
            quote_id = "Q-" + uuid4().hex.upper()
            saved_at = datetime.now(timezone.utc).isoformat()
            db.execute("""INSERT INTO quotes
                (quote_id, saved_at, snapshot_version, snapshot_json, snapshot_sha256)
                VALUES (?, ?, 1, ?, ?)""",
                (quote_id, saved_at, raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()))
            db.execute("INSERT INTO quote_states (quote_id, saved_at) VALUES (?, ?)", (quote_id, saved_at))
            db.execute("""INSERT INTO quote_actions
                (action_ref, session_id, proposal_id, quote_id) VALUES (?, ?, ?, ?)""",
                (proposal.action_ref, proposal.session_id, proposal.proposal_id, quote_id))
            db.execute("UPDATE data_meta SET revision = revision + 1 WHERE id = 1")
            self._before_commit(db, proposal)
            commit_attempted = True
            self._commit(db)
        except Exception as exc:
            # A commit may have succeeded before an exception. Only a known
            # rollback (or never having entered a transaction) proves failure.
            definitive = not started and not commit_attempted
            if db is not None:
                try:
                    if db.in_transaction:
                        db.rollback()
                        definitive = True
                    elif started:
                        definitive = False
                except Exception:
                    definitive = False
            raise QuoteWriteError(definitive=definitive) from exc
        finally:
            if db is not None:
                db.close()
        # The receipt comes from committed facts on a fresh connection, never
        # the prospective ID or a subsequent language-model response.
        quote = self.find_action(proposal.session_id, proposal.action_ref)
        if quote is None:
            raise QuoteWriteError(definitive=False)
        return quote
