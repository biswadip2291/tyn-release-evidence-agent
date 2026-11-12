"""Thin Jira Cloud REST v3 client with retry/backoff and request accounting."""
from __future__ import annotations

import email.utils
import random
import threading
import time
from typing import Any, Callable, Iterator

import requests

from .adf import text_to_adf

RETRY_STATUSES = {429, 500, 502, 503, 504}


class JiraError(RuntimeError):
    def __init__(self, status: int | None, message: str, body: Any = None):
        super().__init__(f"Jira error {status}: {message}")
        self.status = status
        self.body = body


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(value)
        return max(0.0, dt.timestamp() - time.time())
    except (TypeError, ValueError):
        return None


class JiraClient:
    def __init__(
        self,
        base_url: str,
        email: str | None = None,
        api_token: str | None = None,
        *,
        max_retries: int = 6,
        backoff_base: float = 1.0,
        max_backoff: float = 60.0,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self._auth = (email, api_token) if email and api_token else None
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.max_backoff = max_backoff
        self.timeout = timeout
        self._sleep = sleep
        self._local = threading.local()
        self._lock = threading.Lock()
        self.stats: dict[str, Any] = {
            "requests": 0,
            "retries": 0,
            "retry_statuses": {},
            "errors": [],
            "by_endpoint": {},
        }

    # ------------------------------------------------------------------ plumbing
    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            if self._auth:
                s.auth = self._auth
            s.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
            self._local.session = s
        return s

    def _count(self, key: str, sub: str | None = None) -> None:
        with self._lock:
            if sub is None:
                self.stats[key] += 1
            else:
                self.stats[key][sub] = self.stats[key].get(sub, 0) + 1

    def _endpoint_name(self, method: str, path: str) -> str:
        parts = [p for p in path.split("?")[0].split("/") if p]
        norm = []
        for i, p in enumerate(parts):
            prev = parts[i - 1] if i else ""
            norm.append("{key}" if prev in ("issue", "project") and p not in ("bulk",) else p)
        return f"{method} /" + "/".join(norm)

    def request(self, method: str, path: str, *, params: dict | None = None, json: Any = None) -> Any:
        url = self.base_url + path
        endpoint = self._endpoint_name(method, path)
        attempt = 0
        while True:
            self._count("requests")
            self._count("by_endpoint", endpoint)
            try:
                resp = self._session().request(method, url, params=params, json=json, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt >= self.max_retries:
                    self._record_error(endpoint, None, str(exc))
                    raise JiraError(None, f"{endpoint}: {exc}") from exc
                self._backoff(attempt, None, "connection")
                attempt += 1
                continue
            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                self._backoff(attempt, resp.headers.get("Retry-After"), str(resp.status_code))
                attempt += 1
                continue
            if resp.status_code >= 400:
                try:
                    body = resp.json()
                except ValueError:
                    body = resp.text[:500]
                self._record_error(endpoint, resp.status_code, str(body)[:300])
                raise JiraError(resp.status_code, f"{endpoint}: {str(body)[:300]}", body)
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()

    def _backoff(self, attempt: int, retry_after: str | None, reason: str) -> None:
        self._count("retries")
        self._count("retry_statuses", reason)
        delay = _parse_retry_after(retry_after)
        if delay is None:
            delay = min(self.max_backoff, self.backoff_base * (2**attempt)) * (0.5 + random.random() / 2)
        self._sleep(min(delay, self.max_backoff))

    def _record_error(self, endpoint: str, status: int | None, msg: str) -> None:
        with self._lock:
            self.stats["errors"].append({"endpoint": endpoint, "status": status, "message": msg})

    # ------------------------------------------------------------------ API
    def get_project(self, key: str) -> dict:
        return self.request("GET", f"/rest/api/3/project/{key}")

    def search_page(
        self, jql: str, fields: list[str], *, max_results: int = 100, next_page_token: str | None = None
    ) -> tuple[list[dict], str | None]:
        body: dict[str, Any] = {"jql": jql, "fields": fields, "maxResults": max_results}
        if next_page_token:
            body["nextPageToken"] = next_page_token
        data = self.request("POST", "/rest/api/3/search/jql", json=body) or {}
        token = data.get("nextPageToken")
        if data.get("isLast", token is None):
            token = None
        return data.get("issues", []), token

    def search(self, jql: str, fields: list[str], *, page_size: int = 100, limit: int | None = None) -> Iterator[dict]:
        token = None
        seen = 0
        while True:
            issues, token = self.search_page(jql, fields, max_results=page_size, next_page_token=token)
            for issue in issues:
                yield issue
                seen += 1
                if limit is not None and seen >= limit:
                    return
            if not token or not issues:
                return

    def get_issue(self, key: str, fields: list[str]) -> dict:
        return self.request("GET", f"/rest/api/3/issue/{key}", params={"fields": ",".join(fields)})

    def create_issue(self, fields: dict) -> str:
        data = self.request("POST", "/rest/api/3/issue", json={"fields": fields})
        return data["key"]

    def get_comments_page(self, key: str, *, start_at: int = 0, max_results: int = 50) -> dict:
        return self.request(
            "GET",
            f"/rest/api/3/issue/{key}/comment",
            params={"startAt": start_at, "maxResults": max_results, "orderBy": "created"},
        )

    def iter_comments(self, key: str, *, page_size: int = 50) -> Iterator[dict]:
        start = 0
        while True:
            page = self.get_comments_page(key, start_at=start, max_results=page_size)
            comments = page.get("comments", [])
            yield from comments
            start += len(comments)
            if not comments or start >= page.get("total", 0):
                return

    def add_comment(self, key: str, text: str) -> dict:
        return self.request("POST", f"/rest/api/3/issue/{key}/comment", json={"body": text_to_adf(text)})

    def get_link_types(self) -> list[dict]:
        return (self.request("GET", "/rest/api/3/issueLinkType") or {}).get("issueLinkTypes", [])

    def create_link(self, type_name: str, *, inward_key: str, outward_key: str) -> None:
        self.request(
            "POST",
            "/rest/api/3/issueLink",
            json={"type": {"name": type_name}, "inwardIssue": {"key": inward_key}, "outwardIssue": {"key": outward_key}},
        )

    def get_transitions(self, key: str) -> list[dict]:
        return (self.request("GET", f"/rest/api/3/issue/{key}/transitions") or {}).get("transitions", [])

    def transition(self, key: str, transition_id: str) -> None:
        self.request("POST", f"/rest/api/3/issue/{key}/transitions", json={"transition": {"id": transition_id}})
