# Modified from sairam-s0/personal-ai-assistant (Apache-2.0).
# Changes: adapted chat transport to DeepSeek and added strict tool-output handling.
# Source and license: see README.md.

from typing import Any, cast

from openai import APIConnectionError, APIStatusError, AsyncOpenAI, RateLimitError
from openai.types.chat import ChatCompletion

from app.config import Settings
from app.llm.tool_protocol import ModelRequestInvalid, validate_tool_message_sequence


class ModelCallBudgetExceeded(RuntimeError):
    """A local preview limit, distinct from an upstream API failure."""

    public_message = (
        "本次体验额度已用完，聊天已暂停。刷新或稍后重试不会恢复额度；"
        "已有聊天记录仍可查看，需要追加额度后才能继续提问。"
    )


class DeepSeekToolOutputInvalid(ValueError):
    """A rejected transport value, never permission to repair business facts."""


STRICT_TRANSPORT_PROMPT = """本请求的工具Schema使用显式运输契约；业务说明中的字段是解码后的字段。非必填字段须用state=omitted表示确实省略，或state=provided并在value中保留实际值。显式null用Schema规定的独立$wire_null标记；false和空数组是provided的真实值，不能改成省略或null。各工具名、字段含义和引用原话规则不变；不要为填满结构猜事实。query_catalog的业务空参数{}通过各可选字段omitted表达。此格式只约束运输，服务端仍核对原话、事实、范围、身份和权限。"""
STRICT_TRANSPORT_PROMPT += """
运输契约递归适用于每一层对象，业务示例中的省略字段也必须按Schema包装。尤其所有SourceSpan（包括target.span、operations[].span、consultations[].span及其嵌套出处）在不提供索引时完整写成 {"quote":"逐字连续原话","start":{"state":"omitted"},"end":{"state":"omitted"}}，不能只写quote。若span本身可选，先用{"state":"provided","value":上述完整对象}包装；确实没有span则用{"state":"omitted"}。不要为了补齐结构计算或猜测字符位置。
"""


class DeepSeekClient:
    """
    DeepSeek chat adapter with compatibility settings.

    Preferred settings use DEEPSEEK_* names. Settings also accepts legacy
    GROQ_* configuration inputs without changing the provider or request.
    """

    def __init__(self, settings: Settings, *, tool_output_mode: str | None = None) -> None:
        if not settings.deepseek_api_key:
            raise ValueError(
                "API key is required. Add it to your .env file."
            )

        tool_output_mode = settings.deepseek_tool_output_mode if tool_output_mode is None else tool_output_mode
        if tool_output_mode not in {"standard", "deepseek_strict_beta"}:
            raise ValueError("Unknown tool output mode")
        self.settings = settings
        self.tool_output_mode = tool_output_mode

        self.client = AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=("https://api.deepseek.com/beta" if tool_output_mode == "deepseek_strict_beta"
                      else "https://api.deepseek.com"),
        )

    async def chat(
        self,
        messages: list[dict],
        max_completion_tokens: int | None = None,
        temperature: float = 0.2,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = "auto",
        max_retries: int | None = None,
    ) -> ChatCompletion:
        validate_tool_message_sequence(messages)
        contract = None
        if self.tool_output_mode == "deepseek_strict_beta" and tools:
            from copy import deepcopy
            import json
            from app.llm.deepseek_tool_contract import ToolContract
            from app.llm.token_counter import count_text_tokens
            try:
                contract = ToolContract(tools)
                tools = contract.tools
                messages = deepcopy(messages)
                for message in messages:
                    for call in message.get("tool_calls", ()):
                        function = call["function"]
                        function["arguments"] = contract.encode_arguments(
                            function["name"], function["arguments"])
                messages.append({"role": "system", "content": STRICT_TRANSPORT_PROMPT})
            except (ValueError, TypeError, KeyError):
                raise ModelRequestInvalid("strict_tool_contract_invalid") from None
            actual_max_tokens = max_completion_tokens or self.settings.deepseek_default_max_completion_tokens
            if count_text_tokens(json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False)) + actual_max_tokens + 256 > self.settings.deepseek_max_context_tokens:
                raise ModelRequestInvalid("strict_wire_context_budget")
        request_payload: dict[str, Any] = {
            "model": self.settings.deepseek_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": (
                max_completion_tokens
                or self.settings.deepseek_default_max_completion_tokens
            ),
            "extra_body": {
                "thinking": {
                    "type": "disabled"
                }
            },
        }

        if tools:
            request_payload["tools"] = tools

            if tool_choice is not None:
                request_payload["tool_choice"] = tool_choice

        try:
            # The semantic check has one actual dispatch. Other existing
            # requests retain their original SDK retry policy.
            client = (self.client.with_options(max_retries=max_retries,
                       timeout=45.0) if max_retries is not None else self.client)
            create_completion = cast(
                Any,
                client.chat.completions.create,
            )

            completion = await create_completion(
                **request_payload
            )
            if contract is not None:
                try:
                    completion = completion.model_copy(deep=True)
                    for choice in completion.choices:
                        # A truncated value must retain its actual stop reason
                        # for the existing orchestration failure, never repair.
                        if choice.finish_reason == "length":
                            continue
                        for call in choice.message.tool_calls or ():
                            call.function.arguments = contract.decode_arguments(
                                call.function.name, call.function.arguments)
                except (ValueError, TypeError, KeyError, AttributeError):
                    raise DeepSeekToolOutputInvalid("strict_tool_output_invalid") from None
            return completion

        except RateLimitError as exc:
            raise RuntimeError(
                "DeepSeek rate limit reached. Try again shortly."
            ) from exc

        except APIConnectionError as exc:
            raise RuntimeError(
                "Could not connect to DeepSeek API."
            ) from exc

        except APIStatusError as exc:
            if exc.status_code == 400:
                raise ModelRequestInvalid("upstream_bad_request") from exc
            raise RuntimeError(
                f"DeepSeek API error: {exc.status_code} - {exc.message}"
            ) from exc
