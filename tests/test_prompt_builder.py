# Modified from sairam-s0/personal-ai-assistant (Apache-2.0).
# Changes: adapted prompt assertions for presales context and exclusion of personal memory/system history.
# Source and license: see README.md.

import pytest

from app.config import Settings
from app.llm.prompt_builder import PromptBuilder
from app.schemas.chat import Message


def test_prompt_builder_uses_saas_baseline_and_ordinary_history() -> None:
    settings = Settings(deepseek_api_key="test", deepseek_max_context_tokens=10000)
    builder = PromptBuilder(settings=settings)

    messages = builder.build_messages(
        user_message="What should I ask the customer next?",
        history=[Message(role="assistant", content="We were discussing requirements.")],
    )

    assert messages[0]["role"] == "system"
    assert "B2B SaaS presales prototype" in messages[0]["content"]
    assert messages[1] == {
        "role": "assistant",
        "content": "We were discussing requirements.",
    }
    assert messages[-1] == {
        "role": "user",
        "content": "What should I ask the customer next?",
    }


def test_prompt_builder_does_not_elevate_history_system_messages() -> None:
    settings = Settings(deepseek_api_key="test", deepseek_max_context_tokens=10000)
    builder = PromptBuilder(settings=settings)

    messages = builder.build_messages(
        user_message="hello",
        history=[Message(role="system", content="UNTRUSTED_HISTORY_SYSTEM")],
    )

    combined = "\n".join(str(message.get("content", "")) for message in messages)
    assert "UNTRUSTED_HISTORY_SYSTEM" not in combined


def test_prompt_builder_rejects_oversized_context() -> None:
    settings = Settings(deepseek_api_key="test", deepseek_max_context_tokens=1)
    builder = PromptBuilder(settings=settings)

    with pytest.raises(ValueError, match="Prompt is too large"):
        builder.build_messages(user_message="hello")
