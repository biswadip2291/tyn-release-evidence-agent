"""In-process fake of the subset of the Jira Cloud REST v3 API used by this project.

It starts empty and is populated only through the real importer. Extras for testing:
- ``inject_fault(status, count, path_contains=None, retry_after=None)`` returns errors
  (e.g. 429 with Retry-After, 503) before the request is processed;
- ``index_lag``: a newly created issue becomes visible to search only after that many
  further search calls, mimicking Jira's eventually consistent search index;
- ``workflow``: allowed status transitions (defaults to a linear To Do -> In Progress -> Done,
  plus a reopen path) so that multi-hop transitions are exercised.
"""
from __future__ import annotations

import base64
import itertools
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

STATUS_IDS = {"To Do": "11", "In Progress": "21", "Done": "31"}
DEFAULT_WORKFLOW = {
    "To Do": ["In Progress"],
    "In Progress": ["To Do", "Done"],
    "Done": ["In Progress"],
}
LINK_TYPES = [
    {"id": "10000", "name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
    {"id": "10003", "name": "Relates", "inward": "relates to", "outward": "relates to"},
]


def _adf_text(doc) -> str:
    if not doc:
        return ""
    lines = []
    for block in doc.get("content", []):
        lines.append("".join(n.get("text", "") for n in block.get("content", []) or []))
    return "\n".join(lines)


class FakeJiraState:
    def __init__(self, project: str = "TYN", workflow: dict | None = None, index_lag: int = 0):
        self.lock = threading.RLock()
        self.projects = {project}
        self.workflow = workflow or DEFAULT_WORKFLOW
        self.index_lag = index_lag
        self.issues: dict[str, dict] = {}
        self.comments: dict[str, list[dict]] = {}
        self.links: list[dict] = []
        self.counters = {p: itertools.count(1) for p in self.projects}
        self.comment_ids = itertools.count(10001)
        self.link_ids = itertools.count(20001)
        self.search_calls = 0
        self.faults: list[dict] = []
        self.request_log: list[tuple[str, str, int]] = []

    def inject_fault(self, status: int, count: int = 1, path_contains: str | None = None,
                     retry_after: str | None = None, method: str | None = None) -> None:
        with self.lock:
            self.faults.append({"status": status, "count": count, "path": path_contains,
                                "retry_after": retry_after, "method": method})

    def take_fault(self, method: str, path: str) -> dict | None:
        with self.lock:
            for f in self.faults:
                if f["count"] > 0 and (f["path"] is None or f["path"] in path) and (f["method"] in (None, method)):
                    f["count"] -= 1
                    return f
        return None


class _Handler(BaseHTTPRequestHandler):
    state: FakeJiraState  # set on subclass
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence
        pass

    # ------------------------------------------------------------------ plumbing
    def _send(self, status: int, body=None, headers: dict | None = None):
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)
        self.state.request_log.append((self.command, self.path, status))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else None

    def _dispatch(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        body = self._body() if self.command in ("POST", "PUT") else None
        fault = self.state.take_fault(self.command, path)
        if fault:
            headers = {"Retry-After": fault["retry_after"]} if fault["retry_after"] is not None else None
            return self._send(fault["status"], {"errorMessages": ["injected fault"]}, headers)
        routes = [
            ("GET", r"^/rest/api/3/project/([^/]+)$", self.get_project),
            ("GET", r"^/rest/api/3/issueLinkType$", self.get_link_types),
            ("POST", r"^/rest/api/3/search/jql$", self.search),
            ("POST", r"^/rest/api/3/issue$", self.create_issue),
            ("GET", r"^/rest/api/3/issue/([^/]+)$", self.get_issue),
            ("GET", r"^/rest/api/3/issue/([^/]+)/comment$", self.get_comments),
            ("POST", r"^/rest/api/3/issue/([^/]+)/comment$", self.add_comment),
            ("POST", r"^/rest/api/3/issueLink$", self.create_link),
            ("GET", r"^/rest/api/3/issue/([^/]+)/transitions$", self.get_transitions),
            ("POST", r"^/rest/api/3/issue/([^/]+)/transitions$", self.do_transition),
        ]
        for method, pattern, fn in routes:
            m = re.match(pattern, path)
            if m and method == self.command:
                with self.state.lock:
                    return fn(*m.groups(), query=query, body=body)
        return self._send(404, {"errorMessages": [f"No route {self.command} {path}"]})

    do_GET = do_POST = _dispatch

    def _issue_or_404(self, key):
        issue = self.state.issues.get(key)
        if issue is None:
            self._send(404, {"errorMessages": ["Issue does not exist or you do not have permission to see it."]})
        return issue

    # ------------------------------------------------------------------ endpoints
    def get_project(self, key, **_):
        if key not in self.state.projects:
            return self._send(404, {"errorMessages": [f"No project could be found with key '{key}'."]})
        return self._send(200, {"key": key, "name": key})

    def get_link_types(self, **_):
        return self._send(200, {"issueLinkTypes": LINK_TYPES})

    def create_issue(self, body, **_):
        fields = body["fields"]
        project = fields["project"]["key"]
        if project not in self.state.projects:
            return self._send(400, {"errors": {"project": "project is required"}})
        if any(" " in l for l in fields.get("labels", [])):
            return self._send(400, {"errors": {"labels": "labels cannot contain spaces"}})
        n = next(self.state.counters[project])
        key = f"{project}-{n}"
        self.state.issues[key] = {
            "id": str(10000 + n),
            "key": key,
            "summary": fields["summary"],
            "labels": list(fields.get("labels", [])),
            "description": fields.get("description"),
            "status": "To Do",
            "issuetype": fields.get("issuetype", {}).get("name", "Task"),
            "visible_after_search": self.state.search_calls + self.state.index_lag,
        }
        self.state.comments[key] = []
        return self._send(201, {"id": self.state.issues[key]["id"], "key": key})

    def _links_for(self, key):
        out = []
        for l in self.state.links:
            ltype = next(t for t in LINK_TYPES if t["name"] == l["type"])
            if l["inward"] == key:
                other = self.state.issues[l["outward"]]
                out.append({"id": l["id"], "type": ltype, "outwardIssue": self._brief(other)})
            elif l["outward"] == key:
                other = self.state.issues[l["inward"]]
                out.append({"id": l["id"], "type": ltype, "inwardIssue": self._brief(other)})
        return out

    def _brief(self, issue):
        return {"id": issue["id"], "key": issue["key"],
                "fields": {"summary": issue["summary"], "status": {"name": issue["status"]}}}

    def _render(self, issue, fields):
        want = set(fields) if fields else {"*all"}
        allf = "*all" in want or "*navigable" in want
        out = {}
        mapping = {
            "summary": lambda: issue["summary"],
            "labels": lambda: issue["labels"],
            "description": lambda: issue["description"],
            "status": lambda: {"name": issue["status"], "id": STATUS_IDS[issue["status"]]},
            "issuetype": lambda: {"name": issue["issuetype"]},
            "issuelinks": lambda: self._links_for(issue["key"]),
            "created": lambda: "2026-11-12T10:00:00.000+0000",
            "updated": lambda: "2026-11-12T10:00:00.000+0000",
        }
        for name, fn in mapping.items():
            if allf or name in want:
                out[name] = fn()
        return {"id": issue["id"], "key": issue["key"], "fields": out}

    def get_issue(self, key, query, **_):
        issue = self._issue_or_404(key)
        if issue is None:
            return
        fields = query.get("fields", [""])[0].split(",") if "fields" in query else None
        return self._send(200, self._render(issue, [f for f in fields or [] if f] or None))

    # --- comments
    def get_comments(self, key, query, **_):
        if self._issue_or_404(key) is None:
            return
        start = int(query.get("startAt", ["0"])[0])
        size = min(int(query.get("maxResults", ["50"])[0]), 100)
        allc = self.state.comments[key]
        page = allc[start:start + size]
        return self._send(200, {"startAt": start, "maxResults": size, "total": len(allc), "comments": page})

    def add_comment(self, key, body, **_):
        if self._issue_or_404(key) is None:
            return
        c = {"id": str(next(self.state.comment_ids)), "body": body["body"],
             "author": {"displayName": "Import Bot", "accountId": "importer"},
             "created": "2026-11-12T10:00:00.000+0000"}
        self.state.comments[key].append(c)
        return self._send(201, c)

    # --- links
    def create_link(self, body, **_):
        name = body["type"]["name"]
        if not any(t["name"] == name for t in LINK_TYPES):
            return self._send(404, {"errorMessages": ["No issue link type with name"]})
        a, b = body["inwardIssue"]["key"], body["outwardIssue"]["key"]
        if a not in self.state.issues or b not in self.state.issues:
            return self._send(404, {"errorMessages": ["Issue does not exist"]})
        self.state.links.append({"id": str(next(self.state.link_ids)), "type": name, "inward": a, "outward": b})
        return self._send(201)

    # --- transitions
    def get_transitions(self, key, **_):
        issue = self._issue_or_404(key)
        if issue is None:
            return
        ts = [{"id": STATUS_IDS[s], "name": f"Move to {s}", "to": {"name": s, "id": STATUS_IDS[s]}}
              for s in self.state.workflow.get(issue["status"], [])]
        return self._send(200, {"transitions": ts})

    def do_transition(self, key, body, **_):
        issue = self._issue_or_404(key)
        if issue is None:
            return
        tid = body["transition"]["id"]
        allowed = {STATUS_IDS[s]: s for s in self.state.workflow.get(issue["status"], [])}
        if tid not in allowed:
            return self._send(400, {"errorMessages": ["Transition not valid"]})
        issue["status"] = allowed[tid]
        return self._send(204)

    # --- search
    def search(self, body, **_):
        self.state.search_calls += 1
        try:
            preds, order = parse_jql(body.get("jql", ""))
        except ValueError as exc:
            return self._send(400, {"errorMessages": [str(exc)]})
        fields = body.get("fields") or ["*navigable"]
        size = min(int(body.get("maxResults", 50)), 100)
        token = body.get("nextPageToken")
        start = int(base64.b64decode(token).decode()) if token else 0
        hits = []
        for issue in self.state.issues.values():
            if issue["visible_after_search"] >= self.state.search_calls:
                continue  # not yet indexed
            text = " ".join([issue["summary"], _adf_text(issue["description"])] +
                            [_adf_text(c["body"]) for c in self.state.comments[issue["key"]]])
            if all(p(issue, text) for p in preds):
                hits.append(issue)
        if order:
            hits.sort(key=lambda i: int(i["key"].rsplit("-", 1)[1]))
        page = hits[start:start + size]
        resp = {"issues": [self._render(i, fields) for i in page]}
        if start + size < len(hits):
            resp["nextPageToken"] = base64.b64encode(str(start + size).encode()).decode()
            resp["isLast"] = False
        else:
            resp["isLast"] = True
        return self._send(200, resp)


_CLAUSE = re.compile(r'^\s*(project|labels|status|text|key)\s*(=|~)\s*("(?:[^"\\]|\\.)*"|[A-Za-z0-9_.\-]+)\s*$')


def _split_and(jql: str) -> list[str]:
    parts, buf, inq, esc = [], "", False, False
    i = 0
    while i < len(jql):
        ch = jql[i]
        if esc:
            buf += ch
            esc = False
        elif ch == "\\" and inq:
            buf += ch
            esc = True
        elif ch == '"':
            inq = not inq
            buf += ch
        elif not inq and jql[i:i + 5] == " AND ":
            parts.append(buf)
            buf = ""
            i += 5
            continue
        else:
            buf += ch
        i += 1
    parts.append(buf)
    return parts


def parse_jql(jql: str):
    """Parse the restricted JQL this project emits; anything else is a 400, like a typo in real Jira."""
    order = False
    m = re.search(r"\s+ORDER BY key ASC\s*$", jql)
    if m:
        order, jql = True, jql[: m.start()]
    preds = []
    for clause in _split_and(jql):
        cm = _CLAUSE.match(clause)
        if not cm:
            raise ValueError(f"Unsupported JQL clause: {clause!r}")
        field, op, raw = cm.groups()
        val = json.loads(raw) if raw.startswith('"') else raw
        if field == "text":
            if op != "~":
                raise ValueError("text requires ~")
            needle = val.lower()
            preds.append(lambda i, t, n=needle: n in t.lower())
            continue
        if op != "=":
            raise ValueError(f"{field} requires =")
        if field == "project":
            preds.append(lambda i, t, v=val: i["key"].startswith(v + "-"))
        elif field == "labels":
            preds.append(lambda i, t, v=val: v in i["labels"])
        elif field == "status":
            preds.append(lambda i, t, v=val: i["status"].lower() == v.lower())
        elif field == "key":
            preds.append(lambda i, t, v=val: i["key"] == v)
    return preds, order


class FakeJira:
    """Context manager running the fake server on a free localhost port."""

    def __init__(self, **state_kwargs):
        self.state = FakeJiraState(**state_kwargs)
        handler = type("Handler", (_Handler,), {"state": self.state})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    # convenience for tests
    def add_foreign_issue(self, project: str, summary: str, labels: list[str]) -> str:
        with self.state.lock:
            if project not in self.state.counters:
                self.state.projects.add(project)
                self.state.counters[project] = itertools.count(1)
            n = next(self.state.counters[project])
            key = f"{project}-{n}"
            self.state.issues[key] = {"id": str(90000 + n), "key": key, "summary": summary, "labels": labels,
                                      "description": None, "status": "To Do", "issuetype": "Task",
                                      "visible_after_search": -1}
            self.state.comments[key] = []
            return key
