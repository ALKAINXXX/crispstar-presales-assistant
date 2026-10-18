"""Production-page verification with an isolated chat DB and hard model budget.

Reuses the production server recorder; never substitutes business/model facts.
The separate ledger counts attempted real API calls, including failures, before
dispatch. No credentials, complete prompts or hidden reasoning are recorded.
"""

import argparse
from copy import deepcopy
from pathlib import Path
import json

from app.llm.deepseek_client import DeepSeekClient, ModelCallBudgetExceeded
from scripts.preview_ui import now, record_dialogue, request_record, run


class BudgetedRealClient(DeepSeekClient):
    output: Path
    turns: list[dict] = []
    attempts: list[dict] = []
    blocked: list[dict] = []
    max_turns = 10
    max_calls = 30

    def __init__(self, settings):
        super().__init__(settings)
        # Every new counted attempt makes one HTTP dispatch. The production
        # client's retry policy is unchanged; a bounded preview never retries
        # invisibly beneath its ledger.
        self.client = self.client.with_options(max_retries=0)

    @classmethod
    def availability(cls):
        remaining_turns = max(0, cls.max_turns - len(cls.turns))
        remaining_calls = max(0, cls.max_calls - len(cls.attempts))
        available = remaining_turns > 0 and remaining_calls > 0
        return {"available": available,
                "message": (f"隔离体验可提问：剩余 {remaining_turns} 条聊天、{remaining_calls} 次模型调用。"
                    if available else ModelCallBudgetExceeded.public_message),
                "remaining_turns": remaining_turns, "remaining_calls": remaining_calls}

    @classmethod
    def resume(cls, ledger: Path):
        previous = json.loads(ledger.read_text(encoding="utf-8-sig"))
        if previous.get("kind") != "real_model_attempt_ledger":
            raise ValueError("Resume requires an actual-model attempt ledger")
        turns, attempts = previous["turns"], previous["attempts"]
        if (len(turns) != previous["turn_count"] or len(attempts) != previous["actual_model_call_count"]
                or len(turns) > cls.max_turns or len(attempts) > cls.max_calls):
            raise ValueError("Resume counts do not match the approved cumulative limits")
        cls.turns, cls.attempts = deepcopy(turns), deepcopy(attempts)
        cls.blocked = deepcopy(previous.get("blocked_attempts", []))

    @classmethod
    def reject(cls, record, reason):
        # Keep existing serialized preview-report field names stable.
        event = {"user": record["user"], "at": now(), "reason": reason,
                 "turn": record.get("w08_budget_turn"),
                 "actual_dispatched_turns": len(cls.turns), "actual_model_calls": len(cls.attempts)}
        cls.blocked.append(event)
        record["w08_budget_rejection"] = event
        cls.publish()
        raise ModelCallBudgetExceeded(f"Preview {reason} hard budget reached; no model call dispatched")

    @classmethod
    def publish(cls):
        cls.output.mkdir(parents=True, exist_ok=True)
        path = cls.output / "actual-model-ledger.json"
        staging = path.with_suffix(".tmp")
        staging.write_text(json.dumps({
            "kind": "real_model_attempt_ledger", "max_turns": cls.max_turns,
            "max_calls": cls.max_calls, "turn_count": len(cls.turns),
            "actual_model_call_count": len(cls.attempts),
            "turns": cls.turns, "attempts": cls.attempts,
            "blocked_attempts": cls.blocked, "chat_availability": cls.availability(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        staging.replace(path)

    async def chat(self, **kwargs):
        cls = type(self)
        record = request_record.get()
        if record is None:
            raise RuntimeError("Real preview verification requires a recorded HTTP chat request")
        # A denied first call is not an actual dispatched chat turn. Publish
        # its denial independently so zero-call retries cannot vanish.
        if len(cls.attempts) >= cls.max_calls:
            cls.reject(record, "model-call")
        if not record.get("w08_budget_turn"):
            if len(cls.turns) >= cls.max_turns:
                cls.reject(record, "real-chat")
            record["w08_budget_turn"] = len(cls.turns) + 1
            cls.turns.append({"turn": record["w08_budget_turn"],
                "user": record["user"], "started_at": now()})
        attempt = {"call": len(cls.attempts) + 1, "turn": record["w08_budget_turn"],
            "started_at": now(), "result": "dispatched_pending"}
        cls.attempts.append(attempt)
        record_dialogue(kwargs)
        cls.publish()
        try:
            completion = await super().chat(**kwargs)
            attempt["result"] = "returned"
            attempt["model"] = getattr(completion, "model", None)
            attempt["choices"] = [{"finish_reason": choice.finish_reason, "message": {
                "content": choice.message.content,
                "tool_calls": [deepcopy(call.model_dump(mode="json"))
                    for call in (choice.message.tool_calls or [])],
            }} for choice in completion.choices]
            return completion
        except BaseException as error:
            attempt["result"] = "failed"
            attempt["error_type"] = type(error).__name__
            raise
        finally:
            attempt["finished_at"] = now()
            cls.publish()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--output", type=Path, default=Path("data/budgeted_ui_review/browser"))
    parser.add_argument("--history-db", type=Path,
        help="Read-only inherit text from an owned Temp/ui-preview-*/chat.db; never business authority")
    parser.add_argument("--resume-ledger", type=Path,
        help="Continue cumulative counts; never reset the previous real-call budget")
    parser.add_argument("--max-turns", type=int, default=10,
        help="Cumulative dispatched-turn cap; increases require explicit user authorization")
    parser.add_argument("--max-calls", type=int, default=30,
        help="Cumulative actual-call cap; increases require explicit user authorization")
    args = parser.parse_args()
    BudgetedRealClient.output = args.output.resolve()
    if (BudgetedRealClient.output / "actual-model-ledger.json").exists() and not args.resume_ledger:
        parser.error("This output already has a model ledger; explicitly --resume-ledger to retain cumulative usage")
    if args.max_turns < 1 or args.max_calls < 1:
        parser.error("Budget limits must be positive")
    BudgetedRealClient.max_turns, BudgetedRealClient.max_calls = args.max_turns, args.max_calls
    if args.resume_ledger:
        BudgetedRealClient.resume(args.resume_ledger)
    BudgetedRealClient.publish()
    run(args.port, args.output, client_type=BudgetedRealClient, history_db=args.history_db, preserve_history=True)
