# Modified from sairam-s0/personal-ai-assistant (Apache-2.0).
# Changes: made the offline fallback token estimate use the UTF-8 byte count.
# Source and license: see README.md.

from functools import lru_cache
from typing import Protocol

import tiktoken


class _TokenEncoder(Protocol):
    def encode(self, text: str) -> list[int]:
        ...


class ApproximateEncoder:
    def encode(self, text: str) -> list[int]:
        # Conservative fallback for offline/dev environments where tiktoken
        # cannot load its encoder data.
        return [0] * max(1, len(text.encode("utf-8")))


@lru_cache
def _encoding() -> _TokenEncoder:
    try:
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return ApproximateEncoder()


def count_text_tokens(text: str) -> int:
    return len(_encoding().encode(text))


def count_message_tokens(messages: list[dict[str, str]]) -> int:
    token_count = 0
    for message in messages:
        token_count += 4
        token_count += count_text_tokens(message.get("role", ""))
        token_count += count_text_tokens(message.get("content", ""))
    return token_count + 2
