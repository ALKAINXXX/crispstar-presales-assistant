import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app.agents import orchestrator as orchestrator_module
from app.agents.orchestrator import Orchestrator
from app.config import Settings
from app.db.session import connect_saas, init_saas_db
from app.schemas.chat import ChatRequest, Message
from app.services.conversation_store import ConversationStore
from controlled_review import answer_review, is_review_request


class FakeDeepSeekClient:
    captured_requests: list[dict] = []
    review_calls: list[dict] = []

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def chat(self, *, messages, max_completion_tokens, temperature, tools, tool_choice="auto", **kwargs):
        if is_review_request({"tool_choice": tool_choice}):
            return answer_review(type(self), {"messages": messages, "tools": tools,
                "tool_choice": tool_choice, **kwargs})
        type(self).captured_requests.append(deepcopy({"messages": messages, "tools": tools}))
        calls = []
        if tool_choice == {"type": "function", "function": {"name": "update_requirements"}}:
            calls = [SimpleNamespace(id="controlled-general", function=SimpleNamespace(
                name="update_requirements", arguments=json.dumps({"intent": "general"})))]
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="ok", tool_calls=calls),
                )
            ],
            model="fake-model",
            usage=None,
        )


@pytest.fixture(autouse=True)
def reset_fake_client_requests(monkeypatch):
    monkeypatch.setattr(FakeDeepSeekClient, "captured_requests", [])
    monkeypatch.setattr(FakeDeepSeekClient, "review_calls", [])


def make_settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        deepseek_api_key="test",
        saas_database_url=f"sqlite:///{tmp_path / 'saas.db'}",
    )


def test_orchestrator_ignores_client_history_and_other_sessions(tmp_path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    init_saas_db(settings)

    store = ConversationStore(settings)
    other_session = store.ensure_conversation(None, "Other customer conversation")
    store.add_turn(other_session, "OTHER_SESSION_PRIVATE_QUESTION", "OTHER_SESSION_PRIVATE_ANSWER")
    monkeypatch.setattr(orchestrator_module, "DeepSeekClient", FakeDeepSeekClient)

    orchestrator = Orchestrator(settings)
    response = asyncio.run(
        orchestrator.chat(
            ChatRequest(
                message="hello",
                history=[
                    Message(
                        role="system",
                        content="CLIENT_SYSTEM_SHOULD_NOT_APPEAR",
                    )
                ],
            )
        )
    )

    allowed_tools = {"update_requirements", "search_knowledge", "query_catalog", "match_requirements",
                     "calculate_price", "explain_knowledge", "query_quote"}
    required_tools = {"update_requirements", "query_catalog", "match_requirements", "calculate_price", "query_quote"}
    assert FakeDeepSeekClient.captured_requests
    for request in FakeDeepSeekClient.captured_requests:
        combined = json.dumps(request["messages"], ensure_ascii=False)
        assert "CLIENT_SYSTEM_SHOULD_NOT_APPEAR" not in combined
        assert "OTHER_SESSION_PRIVATE_QUESTION" not in combined
        assert "OTHER_SESSION_PRIVATE_ANSWER" not in combined
        names = {tool["function"]["name"] for tool in request["tools"]}
        assert required_tools <= names <= allowed_tools
        assert not any("save" in name or "submit" in name or "create_quote" == name for name in names)
    assert "套餐单价" in response.message
    assert response.business.pricing is None and response.business.match is None


def test_conversation_stores_use_their_configured_database(tmp_path) -> None:
    first_settings = make_settings(tmp_path / "first")
    second_settings = make_settings(tmp_path / "second")
    init_saas_db(first_settings)
    init_saas_db(second_settings)
    first_store = ConversationStore(first_settings)
    second_store = ConversationStore(second_settings)

    first_id = first_store.ensure_conversation(None, "First configured database")
    first_store.add_turn(first_id, "first question", "first answer")
    assert second_store.list_conversations() == []

    second_id = second_store.ensure_conversation(None, "Second configured database")
    second_store.add_turn(second_id, "second question", "second answer")
    assert [m.content for m in first_store.get_messages(first_id)] == ["first question", "first answer"]
    assert [m.content for m in second_store.get_messages(second_id)] == ["second question", "second answer"]
    with connect_saas(first_settings) as db:
        first_ids = {row["id"] for row in db.execute("SELECT id FROM conversations")}
    with connect_saas(second_settings) as db:
        second_ids = {row["id"] for row in db.execute("SELECT id FROM conversations")}
    assert first_ids == {first_id}
    assert second_ids == {second_id}
