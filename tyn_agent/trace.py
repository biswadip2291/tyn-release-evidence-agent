"""Run trace (every model turn and tool call, with timings) and run metrics."""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .model import USAGE_FIELDS


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Tracer:
    def __init__(self, run_dir: Path):
        self.path = Path(run_dir) / "trace.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self._seq = 0
        self.events: list[dict] = []

    def event(self, agent: str, kind: str, **data: Any) -> dict:
        with self._lock:
            self._seq += 1
            ev = {"seq": self._seq, "ts": _utc(), "t_ms": round((time.monotonic() - self._t0) * 1000, 1),
                  "agent": agent, "kind": kind, **data}
            self.events.append(ev)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
            return ev


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started = time.monotonic()
        self.phases: dict[str, float] = {}
        self.tokens = {f: 0 for f in USAGE_FIELDS}
        self.tokens_by_agent: dict[str, dict[str, int]] = {}
        self.model_calls = 0
        self.model_seconds = 0.0
        self.tool_calls: dict[str, int] = {}
        self.tool_errors = 0
        self.tool_seconds = 0.0
        self.submission_rejections = 0
        self.agent_failures: list[dict] = []
        self.errors: list[dict] = []
        self.models_used: set[str] = set()

    def phase(self, name: str, seconds: float) -> None:
        with self._lock:
            self.phases[name] = round(seconds, 3)

    def add_usage(self, agent: str, usage: dict[str, int], seconds: float, model: str = "") -> None:
        with self._lock:
            self.model_calls += 1
            self.model_seconds += seconds
            if model:
                self.models_used.add(model)
            per = self.tokens_by_agent.setdefault(agent, {f: 0 for f in USAGE_FIELDS})
            for f in USAGE_FIELDS:
                self.tokens[f] += int(usage.get(f, 0) or 0)
                per[f] += int(usage.get(f, 0) or 0)

    def add_tool(self, name: str, seconds: float, is_error: bool) -> None:
        with self._lock:
            self.tool_calls[name] = self.tool_calls.get(name, 0) + 1
            self.tool_seconds += seconds
            self.tool_errors += int(is_error)

    def add_rejection(self) -> None:
        with self._lock:
            self.submission_rejections += 1

    def add_failure(self, agent: str, reason: str) -> None:
        with self._lock:
            self.agent_failures.append({"agent": agent, "reason": reason})

    def add_error(self, where: str, message: str) -> None:
        with self._lock:
            self.errors.append({"where": where, "message": message})

    def snapshot(self, jira_stats: dict | None = None) -> dict:
        with self._lock:
            jira = jira_stats or {}
            return {
                "wall_seconds": round(time.monotonic() - self.started, 3),
                "phase_seconds": dict(self.phases),
                "models": sorted(self.models_used),
                "model_calls": self.model_calls,
                "model_seconds": round(self.model_seconds, 3),
                "tokens": {**self.tokens, "total_input_including_cache": self.tokens["input_tokens"]
                           + self.tokens["cache_creation_input_tokens"] + self.tokens["cache_read_input_tokens"]},
                "tokens_by_agent": self.tokens_by_agent,
                "tool_calls": dict(sorted(self.tool_calls.items())),
                "tool_calls_total": sum(self.tool_calls.values()),
                "tool_errors": self.tool_errors,
                "tool_seconds": round(self.tool_seconds, 3),
                "submission_rejections": self.submission_rejections,
                "agent_failures": list(self.agent_failures),
                "errors": list(self.errors),
                "jira": {
                    "requests": jira.get("requests", 0),
                    "retries": jira.get("retries", 0),
                    "retry_statuses": jira.get("retry_statuses", {}),
                    "errors": len(jira.get("errors", [])),
                    "by_endpoint": jira.get("by_endpoint", {}),
                },
            }
