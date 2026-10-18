"""Production React rendering checks; not real-model/browser acceptance evidence."""

import asyncio
from copy import deepcopy
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from controlled_review import answer_review, is_review_request

from app.agents import orchestrator as orchestrator_module
from app.agents.orchestrator import Orchestrator
from app.config import Settings
from app.db.session import init_saas_db
from app.schemas.chat import ChatRequest


def test_read_only_business_evidence_rendering() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js and frontend npm ci are needed for the business UI regression")

    script = Path(__file__).with_name("business_ui.test.cjs")
    result = subprocess.run(
        [node, "--test", str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_blocked_model_body_stays_blocked_through_actual_response_storage_next_input_and_ui(
    tmp_path, monkeypatch,
) -> None:
    """Negative model substitute only; domain services/store/response/UI are real."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for the controlled response-to-UI regression")

    hidden = "HIDDEN_BAD_BODY：Business只需1.00元；已保存BAD-QUOTE-ID"
    users = ["需要API，明确购买80席一年，帮我算基价", "Team单价", "说明刚才的结果"]

    def tool(name, arguments, identity=None):
        return SimpleNamespace(id=uuid4().hex if identity is None else identity, function=SimpleNamespace(
            name=name, arguments=json.dumps(arguments, ensure_ascii=False),
        ))

    def completion(*calls):
        return SimpleNamespace(model="negative-scripted-ui-model", usage=None, choices=[
            SimpleNamespace(message=SimpleNamespace(content=hidden, tool_calls=list(calls)), finish_reason="stop"),
        ])

    patch = {"intent": "selection", "add": [{"feature_id": "api_access", "statement": "产品API",
        "user_quote": "需要API", "kind": "feature"}], "parameters": [
        {"name": "paid_seats", "value": 80, "user_quote": "明确购买80席"},
        {"name": "months", "value": 12, "user_quote": "一年"},
    ], "pricing_quote": "帮我算基价"}

    class ScriptedClient:
        captured = []
        review_calls = []
        remaining = [
            completion(tool("update_requirements", patch), tool("match_requirements", {}, "match")),
            completion(tool("calculate_price", {"plan_id": "team", "paid_seats": 80, "months": 12}, "price")),
            completion(),
            completion(tool("update_requirements", {"intent": "unit_price"}, "reviewed-rate-candidate"),
                tool("query_catalog", {"plan_id": "team", "unaccepted_answer": hidden})),
            completion(tool("update_requirements", {"intent": "general"})),
            completion(),
        ]

        def __init__(self, settings):
            self.settings = settings

        async def chat(self, **kwargs):
            if is_review_request(kwargs):
                return answer_review(type(self), kwargs)
            type(self).captured.append(deepcopy(kwargs))
            assert type(self).remaining, "Unexpected extra model call in bounded negative test"
            return type(self).remaining.pop(0)

    monkeypatch.setattr(orchestrator_module, "DeepSeekClient", ScriptedClient)
    settings = Settings(deepseek_api_key="test", deepseek_default_max_completion_tokens=512,
        saas_database_url=f"sqlite:///{tmp_path / 'controlled-business-ui.db'}")
    init_saas_db(settings)
    engine = Orchestrator(settings)

    async def run():
        first = await engine.chat(ChatRequest(message=users[0]))
        second = await engine.chat(ChatRequest(message=users[1], session_id=first.session_id))
        third = await engine.chat(ChatRequest(message=users[2], session_id=first.session_id))
        return first, second, third

    responses = asyncio.run(run())
    first, second, _ = responses
    assert first.status == "completed" and first.business.pricing.base_price_minor == 7_680_000
    assert first.business.match.lowest_plan_id == "team"
    assert second.status == "incomplete"
    rejected = second.tool_results[-1]
    assert hidden in rejected["arguments"]
    assert rejected["display_arguments_status"] == "rejected" and rejected["display_arguments"] == {}
    history = engine.conversations.get_messages(first.session_id)
    assert [item.content for item in history if item.role == "assistant"] == [item.message for item in responses]
    assert hidden not in json.dumps([message.model_dump() for message in history], ensure_ascii=False)
    # The first invocation of the next turn consumes persisted history. The
    # scripted model may still emit its own poisoned body inside that turn.
    next_turn_input = ScriptedClient.captured[4]["messages"]
    assert hidden not in json.dumps(next_turn_input, ensure_ascii=False)
    assert [item["content"] for item in next_turn_input if item["role"] == "assistant"] == [first.message, second.message]
    fixture = tmp_path / "actual-controlled-responses.json"
    fixture.write_text(json.dumps({
        "hidden": hidden, "users": users,
        "responses": [item.model_dump(mode="json") for item in responses],
        "history": [item.model_dump(mode="json") for item in history],
    }, ensure_ascii=False), encoding="utf-8")
    bridge = Path(__file__).with_name("response_bridge.cjs")
    result = subprocess.run([node, str(bridge), str(fixture)], capture_output=True,
        text=True, encoding="utf-8", errors="replace", check=False)
    assert result.returncode == 0, result.stdout + result.stderr
