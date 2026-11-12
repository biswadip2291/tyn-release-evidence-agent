"""Bounded tool-use agent loop.

The model chooses every evidence-gathering action; code only executes the chosen tools, enforces
budgets and validates the final submission. Any model failure, refusal or exhausted budget ends the
run as an AGENT FAILURE, which the orchestrator reports as INSUFFICIENT_EVIDENCE (not a finding).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .model import Model, ModelError
from .prompts import budget_exhausted, budget_warning
from .tools import ToolBox
from .trace import Metrics, Tracer


FORCED_GRACE_TURNS = 2  # turns allowed after the budget is exhausted, to submit (and fix one rejection)


def _append_system(messages: list[dict], text: str) -> None:
    """Add an operator notice as a mid-conversation system message (it must follow a user message)."""
    last = messages[-1]
    if last["role"] == "system":
        last["content"] = last["content"] + "\n\n" + text
    elif last["role"] == "user":
        messages.append({"role": "system", "content": text})
    else:
        messages.append({"role": "user", "content": text})


@dataclass
class Budget:
    max_turns: int = 30
    max_tool_calls: int = 60
    warn_fraction: float = 0.75
    max_submit_attempts: int = 3


@dataclass
class AgentResult:
    agent: str
    ok: bool
    submission: dict | None = None
    failure: str | None = None
    turns: int = 0
    tool_calls: int = 0
    submit_attempts: int = 0
    rejections: list[list[str]] = field(default_factory=list)
    seconds: float = 0.0
    first_user_message: str = ""


class AgentRun:
    def __init__(self, name: str, model: Model, toolbox: ToolBox, *, system: str, user_message: str,
                 phase: str, submit_tool: dict, validate: Callable[[dict], list[str]], budget: Budget,
                 tracer: Tracer, metrics: Metrics):
        self.name = name
        self.model = model
        self.toolbox = toolbox
        self.system = system
        self.user_message = user_message
        self.submit_tool = submit_tool
        self.submit_name = submit_tool["name"]
        self.validate = validate
        self.budget = budget
        self.tracer = tracer
        self.metrics = metrics
        self.tools = toolbox.definitions(phase) + [submit_tool]  # fixed list keeps the prompt cache stable

    # ------------------------------------------------------------------
    def run(self) -> AgentResult:
        t0 = time.monotonic()
        res = AgentResult(agent=self.name, ok=False, first_user_message=self.user_message)
        messages: list[dict] = [{"role": "user", "content": self.user_message}]
        self.tracer.event(self.name, "agent_start", budget=self.budget.__dict__, user_message=self.user_message)
        warned = False
        forced, forced_at = False, 0
        try:
            while True:
                if not forced and (res.turns >= self.budget.max_turns or res.tool_calls >= self.budget.max_tool_calls):
                    forced, forced_at = True, res.turns
                    _append_system(messages, budget_exhausted(self.submit_name))
                    self.tracer.event(self.name, "budget_forced_submission", turns=res.turns, tool_calls=res.tool_calls)
                if forced and res.turns >= forced_at + FORCED_GRACE_TURNS:
                    return self._fail(res, t0, f"budget exhausted after {res.turns} turns / {res.tool_calls} tool "
                                               "calls without a valid submission")

                res.turns += 1
                tm = time.monotonic()
                turn = self.model(self.system, messages, self.tools)
                dt = time.monotonic() - tm
                self.metrics.add_usage(self.name, turn.usage, dt, getattr(turn, "model", ""))
                self.tracer.event(self.name, "model_turn", turn=res.turns, duration_ms=round(dt * 1000, 1),
                                  stop_reason=turn.stop_reason, usage=turn.usage, blocks=turn.trace_blocks)
                if turn.stop_reason == "refusal":
                    return self._fail(res, t0, "model refused the request (stop_reason=refusal)")
                messages.append({"role": "assistant", "content": turn.content})

                if not turn.tool_uses:
                    if turn.stop_reason == "max_tokens":
                        nudge = "Your response was cut off. Continue, and finish by calling " + self.submit_name + "."
                    else:
                        nudge = f"Continue the investigation with tools, and finish by calling {self.submit_name}."
                    messages.append({"role": "user", "content": nudge})
                    continue

                results: list[dict] = []
                accepted: dict | None = None
                for tu in turn.tool_uses:
                    if tu["name"] == self.submit_name:
                        content, is_error, accepted_now = self._handle_submit(res, tu["input"])
                        if accepted_now:
                            accepted = tu["input"]
                    elif forced or res.tool_calls >= self.budget.max_tool_calls:
                        content, is_error = json.dumps({"error": budget_exhausted(self.submit_name)}), True
                        self.tracer.event(self.name, "tool_refused_budget", tool=tu["name"], input=tu["input"])
                    else:
                        content, is_error = self._run_tool(res, tu)
                    results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": content,
                                    **({"is_error": True} if is_error else {})})

                if accepted is not None:
                    res.ok, res.submission = True, accepted
                    res.seconds = round(time.monotonic() - t0, 3)
                    self.tracer.event(self.name, "agent_done", turns=res.turns, tool_calls=res.tool_calls,
                                      seconds=res.seconds, submit_attempts=res.submit_attempts)
                    return res
                if res.submit_attempts >= self.budget.max_submit_attempts:
                    return self._fail(res, t0, f"submission rejected {res.submit_attempts} times by validation")

                messages.append({"role": "user", "content": results})
                if not warned and not forced and (
                    res.turns >= self.budget.warn_fraction * self.budget.max_turns
                    or res.tool_calls >= self.budget.warn_fraction * self.budget.max_tool_calls
                ):
                    warned = True
                    _append_system(messages, budget_warning(
                        self.budget.max_turns - res.turns, self.budget.max_tool_calls - res.tool_calls))
                    self.tracer.event(self.name, "budget_warning", turns=res.turns, tool_calls=res.tool_calls)
        except ModelError as exc:
            return self._fail(res, t0, f"model call failed: {exc}")
        except Exception as exc:  # noqa: BLE001 - any crash is reported honestly, never as a finding
            return self._fail(res, t0, f"agent crashed: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    def _run_tool(self, res: AgentResult, tu: dict) -> tuple[str, bool]:
        res.tool_calls += 1
        t = time.monotonic()
        result, is_error = self.toolbox.execute(tu["name"], tu["input"])
        dt = time.monotonic() - t
        self.metrics.add_tool(tu["name"], dt, is_error)
        self.tracer.event(self.name, "tool_call", tool=tu["name"], input=tu["input"], duration_ms=round(dt * 1000, 1),
                          is_error=is_error, result=result)
        return json.dumps(result, ensure_ascii=False), is_error

    def _handle_submit(self, res: AgentResult, sub: dict) -> tuple[str, bool, bool]:
        res.submit_attempts += 1
        errors = self.validate(sub or {})
        self.tracer.event(self.name, "submission", attempt=res.submit_attempts, accepted=not errors,
                          errors=errors, submission=sub)
        if errors:
            res.rejections.append(errors)
            self.metrics.add_rejection()
            left = self.budget.max_submit_attempts - res.submit_attempts
            msg = {"error": "Submission rejected by validation. Fix every problem and submit again.",
                   "problems": errors, "attempts_left": left}
            return json.dumps(msg), True, False
        return json.dumps({"status": "accepted"}), False, True

    def _fail(self, res: AgentResult, t0: float, reason: str) -> AgentResult:
        res.ok = False
        res.failure = reason
        res.seconds = round(time.monotonic() - t0, 3)
        self.metrics.add_failure(self.name, reason)
        self.tracer.event(self.name, "agent_failed", reason=reason, turns=res.turns, tool_calls=res.tool_calls)
        return res


def tool_result_payloads(messages: list[dict]) -> list[Any]:
    """Helper for scripted models: parse the tool results in the last user message."""
    for m in reversed(messages):
        if m["role"] == "user":
            if isinstance(m["content"], list):
                return [json.loads(b["content"]) for b in m["content"] if b.get("type") == "tool_result"]
            return []
    return []
