"""The only place JQL strings are built. Callers pass structured filters, never raw JQL.

Every query starts with ``project = <KEY> AND labels = "<scope label>"``, so nothing outside
the imported dataset can be returned.
"""
from __future__ import annotations

import re

_SAFE_PROJECT = re.compile(r"^[A-Z][A-Z0-9_]{0,19}$")


class JqlError(ValueError):
    pass


def quote(value: str) -> str:
    if not isinstance(value, str):
        raise JqlError("JQL values must be strings")
    if any(c in value for c in "\r\n"):
        raise JqlError("JQL values cannot contain newlines")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def scoped_jql(
    project: str,
    scope_label: str,
    *,
    labels: list[str] | None = None,
    status: str | None = None,
    text: str | None = None,
    order_by_key: bool = True,
) -> str:
    if not _SAFE_PROJECT.match(project):
        raise JqlError(f"Invalid project key: {project!r}")
    clauses = [f"project = {project}", f"labels = {quote(scope_label)}"]
    for lab in labels or []:
        clauses.append(f"labels = {quote(lab)}")
    if status:
        clauses.append(f"status = {quote(status)}")
    if text:
        clauses.append(f"text ~ {quote(text)}")
    jql = " AND ".join(clauses)
    if order_by_key:
        jql += " ORDER BY key ASC"
    return jql
