"""Minimal, lossless conversion between plain text and Atlassian Document Format.

Each text line becomes one ADF paragraph, so ``adf_to_text(text_to_adf(s)) == s`` for any
string without carriage returns. That round trip is what lets the importer check idempotency
and the agent quote source text verbatim.
"""
from __future__ import annotations

from typing import Any


def text_to_adf(text: str) -> dict[str, Any]:
    paragraphs = []
    for line in text.split("\n"):
        para: dict[str, Any] = {"type": "paragraph", "content": []}
        if line:
            para["content"].append({"type": "text", "text": line})
        paragraphs.append(para)
    return {"type": "doc", "version": 1, "content": paragraphs}


def _inline_text(node: dict[str, Any]) -> str:
    t = node.get("type")
    if t == "text":
        return node.get("text", "")
    if t == "hardBreak":
        return "\n"
    if t in ("mention", "emoji"):
        return node.get("attrs", {}).get("text", "")
    if t == "inlineCard":
        return node.get("attrs", {}).get("url", "")
    return "".join(_inline_text(c) for c in node.get("content", []) or [])


def adf_to_text(doc: Any) -> str:
    """Flatten ADF to text: block nodes are joined by newlines."""
    if doc is None:
        return ""
    if isinstance(doc, str):
        return doc
    blocks = doc.get("content", []) or []
    lines: list[str] = []
    for block in blocks:
        if block.get("type") in ("paragraph", "heading", "codeBlock"):
            lines.append("".join(_inline_text(c) for c in block.get("content", []) or []))
        elif block.get("type") in ("bulletList", "orderedList"):
            for item in block.get("content", []) or []:
                lines.append(adf_to_text(item))
        else:
            lines.append(adf_to_text(block) if "content" in block else _inline_text(block))
    return "\n".join(lines)
