"""The verification recorder's limits and failed-call accounting, no real API."""

import asyncio
import json
from types import SimpleNamespace
import subprocess
import sys

import pytest

from app.llm.deepseek_client import DeepSeekClient, ModelCallBudgetExceeded
from app.config import Settings
from scripts.preview_ui import request_record
from scripts.preview_ui_budget import BudgetedRealClient


@pytest.fixture
def recorder(tmp_path):
    class Recorder(BudgetedRealClient):
        output = tmp_path
        turns = []
        attempts = []
        blocked = []
        max_turns = 2
        max_calls = 3
    return Recorder


def test_attempt_failure_is_counted_and_call_cap_never_dispatches_extra(recorder, monkeypatch):
    dispatched = []
    async def controlled(self, **kwargs):
        dispatched.append(kwargs)
        if len(dispatched) == 1:
            raise RuntimeError("controlled API failure")
        return SimpleNamespace(model="controlled", choices=[])
    monkeypatch.setattr(DeepSeekClient, "chat", controlled)
    client = recorder.__new__(recorder)
    async def scenario():
        token = request_record.set({"user": "受控预算核对", "model_dialogue_inputs": []})
        try:
            with pytest.raises(RuntimeError, match="controlled API failure"):
                await client.chat(messages=[])
            await client.chat(messages=[])
            await client.chat(messages=[])
            with pytest.raises(RuntimeError, match="hard budget"):
                await client.chat(messages=[])
        finally:
            request_record.reset(token)
    asyncio.run(scenario())
    assert len(dispatched) == 3
    ledger = json.loads((recorder.output / "actual-model-ledger.json").read_text(encoding="utf-8"))
    assert ledger["actual_model_call_count"] == 3 and ledger["turn_count"] == 1
    assert [a["result"] for a in ledger["attempts"]] == ["failed", "returned", "returned"]


def test_turn_cap_and_unrecorded_calls_never_reach_model(recorder, monkeypatch):
    recorder.max_calls = 10
    dispatched = []
    async def controlled(self, **kwargs):
        dispatched.append(kwargs)
        return SimpleNamespace(model="controlled", choices=[])
    monkeypatch.setattr(DeepSeekClient, "chat", controlled)
    client = recorder.__new__(recorder)
    async def scenario():
        with pytest.raises(RuntimeError, match="recorded HTTP"):
            await client.chat(messages=[])
        for index in range(3):
            token = request_record.set({"user": f"受控回合{index}", "model_dialogue_inputs": []})
            try:
                if index == 2:
                    with pytest.raises(RuntimeError, match="hard budget"):
                        await client.chat(messages=[])
                else:
                    await client.chat(messages=[])
            finally:
                request_record.reset(token)
    asyncio.run(scenario())
    assert len(dispatched) == 2 and len(recorder.turns) == 2


def test_call_cap_denial_is_recorded_without_creating_a_dispatched_turn(recorder, monkeypatch):
    recorder.max_calls = 1
    dispatched = []
    async def controlled(self, **kwargs):
        dispatched.append(kwargs)
        return SimpleNamespace(model="controlled", choices=[])
    monkeypatch.setattr(DeepSeekClient, "chat", controlled)
    client = recorder.__new__(recorder)
    async def scenario():
        for index in range(3):
            record = {"user": f"受控回合{index}", "model_dialogue_inputs": []}
            token = request_record.set(record)
            try:
                if index:
                    with pytest.raises(ModelCallBudgetExceeded):
                        await client.chat(messages=[])
                    assert "w08_budget_turn" not in record
                    assert record["w08_budget_rejection"]["reason"] == "model-call"
                else:
                    await client.chat(messages=[])
            finally:
                request_record.reset(token)
    asyncio.run(scenario())
    ledger = json.loads((recorder.output / "actual-model-ledger.json").read_text(encoding="utf-8"))
    assert len(dispatched) == ledger["actual_model_call_count"] == ledger["turn_count"] == 1
    assert len(ledger["blocked_attempts"]) == 2
    assert ledger["chat_availability"]["available"] is False
    assert "不会恢复" in ledger["chat_availability"]["message"]


def test_resume_retains_attempts_failures_and_cumulative_limits(recorder, tmp_path):
    previous = tmp_path / "previous.json"
    previous.write_text(json.dumps({"kind":"real_model_attempt_ledger", "turn_count":1,
        "actual_model_call_count":2, "turns":[{"turn":1,"user":"旧回合"}],
        "attempts":[{"call":1,"result":"failed"},{"call":2,"result":"returned"}],
        "blocked_attempts":[{"reason":"model-call"}]}), encoding="utf-8")
    recorder.resume(previous)
    assert [a["result"] for a in recorder.attempts] == ["failed","returned"]
    assert recorder.availability()["remaining_calls"] == 1
    assert recorder.availability()["remaining_turns"] == 1
    recorder.max_calls = 1
    with pytest.raises(ValueError, match="cumulative"):
        recorder.resume(previous)


def test_cli_never_overwrites_existing_usage_without_explicit_resume(tmp_path):
    ledger = tmp_path / "actual-model-ledger.json"
    original = b'{"preserved":"previous actual calls"}'
    ledger.write_bytes(original)
    result = subprocess.run([sys.executable, "-X", "utf8", "-m", "scripts.preview_ui_budget",
        "--output", str(tmp_path)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 2 and "--resume-ledger" in result.stderr
    assert ledger.read_bytes() == original


def test_bounded_preview_disables_sdk_retries_without_an_api_request(recorder):
    client = recorder(Settings(_env_file=None, deepseek_api_key="controlled-no-dispatch"))
    assert client.client.max_retries == 0
