"""Model adapter. The agent loop talks to a callable ``model(system, messages, tools) -> ModelTurn``
so tests can substitute a scripted fake without any network access."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ModelTurn:
    content: list[Any]  # replayed verbatim as the assistant message (keeps thinking blocks intact)
    tool_uses: list[dict]  # [{id, name, input}]
    text: str
    stop_reason: str
    usage: dict[str, int] = field(default_factory=dict)
    trace_blocks: list[dict] = field(default_factory=list)  # JSON-safe view for the trace
    model: str = ""


class Model(Protocol):
    name: str

    def __call__(self, system: str, messages: list[dict], tools: list[dict]) -> ModelTurn: ...


class ModelError(RuntimeError):
    pass


USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


class AnthropicModel:
    """Claude via the official SDK (beta Messages endpoint for server-side refusal fallbacks)."""

    def __init__(self, api_key: str, model: str, *, effort: str = "high", max_tokens: int = 16000,
                 max_retries: int = 4, timeout: float = 300.0, fallbacks: bool = True):
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=max_retries, timeout=timeout)
        self.name = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.fallbacks = fallbacks

    def __call__(self, system: str, messages: list[dict], tools: list[dict]) -> ModelTurn:
        kwargs: dict[str, Any] = dict(
            model=self.name,
            max_tokens=self.max_tokens,
            system=system,
            messages=messages,
            tools=tools,
            tool_choice={"type": "auto"},
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},  # auto-cache the growing conversation prefix
        )
        if self.fallbacks:
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
        try:
            resp = self.client.beta.messages.create(**kwargs)
        except self._anthropic.APIStatusError as exc:
            raise ModelError(f"API error {exc.status_code}: {exc.message}") from exc
        except self._anthropic.APIConnectionError as exc:
            raise ModelError(f"API connection error: {exc}") from exc

        tool_uses, texts, trace_blocks = [], [], []
        for block in resp.content:
            btype = getattr(block, "type", "")
            if btype == "tool_use":
                tool_uses.append({"id": block.id, "name": block.name, "input": block.input or {}})
                trace_blocks.append({"type": "tool_use", "name": block.name, "input": block.input})
            elif btype == "text":
                texts.append(block.text)
                trace_blocks.append({"type": "text", "text": block.text})
            elif btype == "thinking":
                trace_blocks.append({"type": "thinking", "summary": getattr(block, "thinking", "")})
            else:
                trace_blocks.append({"type": btype})
        u = resp.usage
        usage = {f: int(getattr(u, f, 0) or 0) for f in USAGE_FIELDS}
        return ModelTurn(content=list(resp.content), tool_uses=tool_uses, text="\n".join(texts),
                         stop_reason=resp.stop_reason or "", usage=usage, trace_blocks=trace_blocks,
                         model=getattr(resp, "model", self.name))
