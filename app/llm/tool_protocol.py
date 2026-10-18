"""Local checks for assistant tool batches before a model dispatch.

These checks concern request construction only. They neither execute tools nor
decide whether a business result succeeded.
"""


class ModelRequestInvalid(RuntimeError):
    public_message = "模型请求构造未通过校验；本轮回答未完成，已取得的实际结果保留。"

    def __init__(self, reason: str) -> None:
        # Reasons are fixed codes, never provider payloads or exception text.
        self.reason = reason
        super().__init__(reason)


class ToolMessageProtocolError(ModelRequestInvalid):
    pass


def validate_tool_message_sequence(messages: list[dict]) -> None:
    """Require contiguous receipts and unique call identities in this request."""
    pending: set[str] = set()
    seen: set[str] = set()
    for message in messages:
        if type(message) is not dict:
            raise ToolMessageProtocolError("message_shape_invalid")
        role = message.get("role")
        if pending and role != "tool":
            raise ToolMessageProtocolError("tool_batch_interrupted")
        if role == "tool":
            identity = message.get("tool_call_id")
            if type(identity) is not str or identity not in pending:
                raise ToolMessageProtocolError("tool_receipt_identity_invalid")
            if type(message.get("content")) is not str:
                raise ToolMessageProtocolError("tool_receipt_content_invalid")
            pending.remove(identity)
            continue
        calls = message.get("tool_calls")
        if calls:
            if role != "assistant" or type(calls) is not list:
                raise ToolMessageProtocolError("tool_call_message_invalid")
            identities = []
            for call in calls:
                if type(call) is not dict:
                    raise ToolMessageProtocolError("tool_call_shape_invalid")
                function = call.get("function")
                identity = call.get("id")
                if (type(identity) is not str or not identity
                        or call.get("type") != "function"
                        or type(function) is not dict
                        or type(function.get("name")) is not str
                        or not function.get("name")
                        or type(function.get("arguments")) is not str):
                    raise ToolMessageProtocolError("tool_call_shape_invalid")
                identities.append(identity)
            if len(set(identities)) != len(identities):
                raise ToolMessageProtocolError("tool_call_identity_duplicate")
            if seen.intersection(identities):
                raise ToolMessageProtocolError("tool_call_identity_reused")
            seen.update(identities)
            pending = set(identities)
    if pending:
        raise ToolMessageProtocolError("tool_batch_unclosed")


def validate_new_tool_batch(messages: list[dict], calls: list[dict]) -> None:
    """Reject reused identities before any tool in the incoming batch runs."""
    # A hypothetical closed batch uses no business facts or real executions.
    # Reuse the exact request validator to check shape, duplicates and overlap.
    validate_tool_message_sequence(messages + [{"role": "assistant", "tool_calls": calls}] + [
        {"role": "tool", "tool_call_id": call.get("id"), "content": "{}"}
        for call in calls])
