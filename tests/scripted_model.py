"""A scripted stand-in for Claude: per-conversation generators that choose tool calls.

A script is a generator. It yields a list of tool calls ``[(name, input), ...]`` (one model turn) and
receives a ``Ctx`` with the parsed tool results of that turn and the full message list.
"""
from __future__ import annotations

import itertools
import json
import threading
from dataclasses import dataclass
from typing import Callable, Generator

from tyn_agent.model import ModelError, ModelTurn


@dataclass
class Ctx:
    results: list
    messages: list

    @property
    def notices(self) -> str:
        return " ".join(m["content"] for m in self.messages if m["role"] == "system" and isinstance(m["content"], str))


Script = Generator[list, Ctx, None]


class ScriptedModel:
    name = "scripted-fake"

    def __init__(self, choose: Callable[[str], Script]):
        self.choose = choose
        self._gens: dict[str, Script] = {}
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self.first_messages: list[str] = []
        self.calls = 0

    def __call__(self, system: str, messages: list[dict], tools: list[dict]) -> ModelTurn:
        first = messages[0]["content"]
        with self._lock:
            self.calls += 1
            gen = self._gens.get(first)
            if gen is None:
                gen = self.choose(first)
                self._gens[first] = gen
                self.first_messages.append(first)
                action = next(gen)
            else:
                last_user = next(m for m in reversed(messages) if m["role"] == "user")
                payloads = [json.loads(b["content"]) for b in last_user["content"]
                            if isinstance(last_user["content"], list) and b.get("type") == "tool_result"]
                action = gen.send(Ctx(payloads, messages))
        if action == "RAISE":
            raise ModelError("simulated API outage")
        blocks = []
        for name, inp in action:
            blocks.append({"type": "tool_use", "id": f"toolu_{next(self._ids)}", "name": name, "input": inp})
        return ModelTurn(content=blocks, tool_uses=[{"id": b["id"], "name": b["name"], "input": b["input"]} for b in blocks],
                         text="", stop_reason="tool_use",
                         usage={"input_tokens": 100, "output_tokens": 20, "cache_creation_input_tokens": 0,
                                "cache_read_input_tokens": 50},
                         trace_blocks=[{"type": "tool_use", "name": b["name"], "input": b["input"]} for b in blocks],
                         model=self.name)
