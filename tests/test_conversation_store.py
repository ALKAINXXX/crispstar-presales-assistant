# Modified from sairam-s0/personal-ai-assistant (Apache-2.0).
# Changes: verifies conversation persistence in the configured presales database.
# Source and license: see README.md.

from pathlib import Path

from app.config import Settings
from app.db.session import init_saas_db
from app.services.conversation_store import ConversationStore


def test_conversation_store_persists_messages(tmp_path: Path) -> None:
    database = tmp_path / "conversation-store.db"
    settings = Settings(
        _env_file=None,
        deepseek_api_key="test",
        saas_database_url=f"sqlite:///{database}",
    )
    init_saas_db(settings)
    store = ConversationStore(settings)

    session_id = store.ensure_conversation(None, "Hello assistant")
    store.add_message(session_id, "user", "Hello assistant")
    store.add_message(session_id, "assistant", "Hello.")

    messages = store.get_messages(session_id)

    assert [message.role for message in messages] == ["user", "assistant"]
    assert [message.content for message in messages] == ["Hello assistant", "Hello."]
    assert store.list_conversations()[0]["id"] == session_id
    assert database.is_file()
