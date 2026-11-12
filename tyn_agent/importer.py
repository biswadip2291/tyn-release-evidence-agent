"""Idempotent importer: dataset.json -> Jira Cloud project.

Re-running only adds what is missing. Existing issues are found by their ``src-<source_id>``
label (via search) and, to survive search-index lag, by the mapping saved in
``import_state.json`` (confirmed with a direct GET, which does not depend on the index).
"""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .adf import adf_to_text, text_to_adf
from .jira_client import JiraClient, JiraError
from .jql import scoped_jql

log = logging.getLogger("tyn.import")

HEADER_START = "[Imported source record]"
HEADER_END = "--- Original description (verbatim) ---"
IMPORT_NOTE = (
    "Import note: Jira's native author and timestamps on this issue and its comments belong to the "
    "importer, not to the scenario actors. Each comment's scenario actor, role and event time are in "
    'its "Recorded event" line.'
)
LINK_TYPE_PREFERENCE = ("Relates", "Relates to")


# ---------------------------------------------------------------------- record helpers
def build_description(rec: dict) -> str:
    links = ", ".join(rec.get("links") or []) or "none"
    header = [
        HEADER_START,
        f"Source ID: {rec['source_id']}",
        f"Kind: {rec['kind']}",
        f"Recorded release: {rec['release']}",
        f"Recorded status: {rec['recorded_status']}",
        f"Source links declared by this record: {links}",
        IMPORT_NOTE,
        HEADER_END,
    ]
    return "\n".join(header) + "\n" + rec["description"]


def parse_description(text: str) -> dict[str, Any]:
    """Split an imported description into header fields and the verbatim original."""
    out: dict[str, Any] = {"header_found": False, "declared_links": [], "original": text}
    if not text.startswith(HEADER_START) or HEADER_END not in text:
        return out
    head, _, original = text.partition(HEADER_END)
    out["original"] = original[1:] if original.startswith("\n") else original
    out["header_found"] = True
    fields = {
        "Source ID": "source_id",
        "Kind": "kind",
        "Recorded release": "release",
        "Recorded status": "recorded_status",
    }
    for line in head.split("\n"):
        name, sep, value = line.partition(": ")
        if not sep:
            continue
        if name in fields:
            out[fields[name]] = value.strip()
        elif name == "Source links declared by this record":
            v = value.strip()
            out["declared_links"] = [] if v == "none" else [x.strip() for x in v.split(",") if x.strip()]
    return out


def labels_for(rec: dict, dataset_label: str) -> list[str]:
    return [dataset_label, f"src-{rec['source_id']}", f"rr-{rec['kind']}", f"release-{rec['release']}"]


def summary_for(rec: dict) -> str:
    s = f"{rec['source_id']}: {rec['summary']}"
    return s if len(s) <= 255 else s[:252] + "..."


def load_dataset(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "records" not in data or "dataset_label" not in data:
        raise ValueError("dataset.json must contain 'dataset_label' and 'records'")
    ids = [r["source_id"] for r in data["records"]]
    if len(ids) != len(set(ids)):
        raise ValueError("dataset.json contains duplicate source_id values")
    known = set(ids)
    for r in data["records"]:
        for target in r.get("links") or []:
            if target not in known:
                raise ValueError(f"{r['source_id']} links to unknown record {target}")
    return data


def expected_link_pairs(records: list[dict]) -> set[frozenset]:
    return {frozenset((r["source_id"], t)) for r in records for t in r.get("links") or [] if t != r["source_id"]}


# ---------------------------------------------------------------------- importer
class Importer:
    def __init__(
        self,
        client: JiraClient,
        project: str,
        dataset: dict,
        *,
        issue_type: str = "Task",
        state_path: str | Path = "outputs/import_state.json",
        workers: int = 4,
        verify_attempts: int = 8,
        verify_delay: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.client = client
        self.project = project
        self.dataset = dataset
        self.label = dataset["dataset_label"]
        self.records: list[dict] = dataset["records"]
        self.by_src = {r["source_id"]: r for r in self.records}
        self.issue_type = issue_type
        self.state_path = Path(state_path)
        self.workers = max(1, workers)
        self.verify_attempts = verify_attempts
        self.verify_delay = verify_delay
        self._sleep = sleep
        self.mapping: dict[str, str] = {}
        self.actions: dict[str, int] = {
            "issues_created": 0,
            "comments_added": 0,
            "links_created": 0,
            "transitions_applied": 0,
        }
        self.warnings: list[str] = []

    # -------------------------------------------------------------- orchestration
    def run(self, *, verify: bool = True) -> dict:
        t0 = time.monotonic()
        self.client.get_project(self.project)  # fails fast with a clear error if missing / no access
        self._discover_existing()
        self._create_missing_issues()
        self._save_state()  # persist keys early so an interrupted run can resume safely
        self._sync_comments()
        self._sync_links()
        self._sync_statuses()
        result: dict[str, Any] = {"actions": dict(self.actions), "warnings": list(self.warnings)}
        if verify:
            result["verify"] = self.verify()
        result["seconds"] = round(time.monotonic() - t0, 2)
        result["jira_requests"] = self.client.stats["requests"]
        result["jira_retries"] = self.client.stats["retries"]
        self._save_state(result)
        return result

    def _pmap(self, fn: Callable, items: list) -> list:
        if self.workers == 1 or len(items) <= 1:
            return [fn(i) for i in items]
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            return list(ex.map(fn, items))

    # -------------------------------------------------------------- discovery
    def _discover_existing(self) -> None:
        found: dict[str, list[str]] = {}
        jql = scoped_jql(self.project, self.label)
        for issue in self.client.search(jql, ["labels"]):
            for lab in issue["fields"].get("labels", []):
                if lab.startswith("src-") and lab[4:] in self.by_src:
                    found.setdefault(lab[4:], []).append(issue["key"])
        for src, keys in found.items():
            keys.sort(key=_key_num)
            if len(keys) > 1:
                self.warnings.append(f"Duplicate issues for {src}: {keys}; using {keys[0]}")
            self.mapping[src] = keys[0]
        # Fall back to saved state for issues the search index may not show yet.
        saved = self._load_state().get("issues", {})
        for src, key in saved.items():
            if src in self.mapping or src not in self.by_src:
                continue
            try:
                issue = self.client.get_issue(key, ["labels"])
            except JiraError as exc:
                if exc.status == 404:
                    continue
                raise
            if f"src-{src}" in issue["fields"].get("labels", []) and self.label in issue["fields"].get("labels", []):
                self.mapping[src] = issue["key"]
        log.info("Discovered %d existing issues", len(self.mapping))

    # -------------------------------------------------------------- issues
    def _create_missing_issues(self) -> None:
        missing = [r for r in self.records if r["source_id"] not in self.mapping]
        log.info("Creating %d missing issues", len(missing))

        def create(rec: dict) -> tuple[str, str]:
            fields = {
                "project": {"key": self.project},
                "issuetype": {"name": self.issue_type},
                "summary": summary_for(rec),
                "labels": labels_for(rec, self.label),
                "description": text_to_adf(build_description(rec)),
            }
            return rec["source_id"], self.client.create_issue(fields)

        for src, key in self._pmap(create, missing):
            self.mapping[src] = key
            self.actions["issues_created"] += 1

    # -------------------------------------------------------------- comments
    def _sync_comments(self) -> None:
        def sync(rec: dict) -> int:
            key = self.mapping[rec["source_id"]]
            wanted: list[str] = rec.get("comments") or []
            existing = [adf_to_text(c.get("body")) for c in self.client.iter_comments(key)]
            if existing != wanted[: len(existing)]:
                self.warnings.append(
                    f"{rec['source_id']} ({key}): existing comments do not match the source prefix; "
                    "not adding comments to avoid reordering"
                )
                return 0
            added = 0
            for text in wanted[len(existing):]:
                self.client.add_comment(key, text)
                added += 1
            return added

        self.actions["comments_added"] += sum(self._pmap(sync, self.records))

    # -------------------------------------------------------------- links
    def _link_type(self) -> str:
        types = self.client.get_link_types()
        names = {t["name"].lower(): t["name"] for t in types}
        for pref in LINK_TYPE_PREFERENCE:
            if pref.lower() in names:
                return names[pref.lower()]
        if not types:
            raise JiraError(None, "No issue link types available")
        return types[0]["name"]

    def _linked_keys(self, key: str) -> set[str]:
        issue = self.client.get_issue(key, ["issuelinks"])
        out = set()
        for link in issue["fields"].get("issuelinks", []) or []:
            other = link.get("outwardIssue") or link.get("inwardIssue") or {}
            if other.get("key"):
                out.add(other["key"])
        return out

    def _sync_links(self) -> None:
        declaring = [r for r in self.records if r.get("links")]
        if not declaring:
            return
        link_type = self._link_type()
        done_pairs: set[frozenset] = set()
        for rec in declaring:  # sequential: avoids racing duplicate links for mutual declarations
            key = self.mapping[rec["source_id"]]
            existing = self._linked_keys(key)
            for target in rec["links"]:
                tkey = self.mapping[target]
                pair = frozenset((key, tkey))
                if tkey in existing or pair in done_pairs or tkey == key:
                    continue
                # Convention: the declaring record is the inward side of the request.
                self.client.create_link(link_type, inward_key=key, outward_key=tkey)
                done_pairs.add(pair)
                self.actions["links_created"] += 1

    # -------------------------------------------------------------- statuses
    def _sync_statuses(self) -> None:
        def move(rec: dict) -> None:
            key = self.mapping[rec["source_id"]]
            target = rec["recorded_status"]
            try:
                ok, applied = self._move_to_status(key, target)
            except JiraError as exc:
                ok, applied = False, 0
                self.warnings.append(f"{key}: transition error {exc}")
            self.actions["transitions_applied"] += applied
            if not ok:
                self.warnings.append(f"{rec['source_id']} ({key}): could not reach status '{target}'")

        self._pmap(move, self.records)

    def _move_to_status(self, key: str, target: str, max_hops: int = 4) -> tuple[bool, int]:
        visited: set[str] = set()
        applied = 0
        for _ in range(max_hops + 1):
            current = self.client.get_issue(key, ["status"])["fields"]["status"]["name"]
            if current.lower() == target.lower():
                return True, applied
            visited.add(current.lower())
            transitions = self.client.get_transitions(key)
            direct = [t for t in transitions if t["to"]["name"].lower() == target.lower()]
            choice = direct[0] if direct else next(
                (t for t in transitions if t["to"]["name"].lower() not in visited), None
            )
            if choice is None:
                return False, applied
            self.client.transition(key, choice["id"])
            applied += 1
        return False, applied

    # -------------------------------------------------------------- verify
    def verify(self) -> dict:
        exp_issues = len(self.records)
        exp_comments = sum(len(r.get("comments") or []) for r in self.records)
        exp_pairs = {frozenset((self.mapping[a], self.mapping[b])) for a, b in map(tuple, expected_link_pairs(self.records))}

        # 1) Search-index view of the scoped issues; retried because indexing lags bulk imports.
        jql = scoped_jql(self.project, self.label)
        search_count, attempts = -1, 0
        missing_srcs: list[str] = []
        for attempts in range(1, self.verify_attempts + 1):
            seen_srcs = set()
            search_count = 0
            for issue in self.client.search(jql, ["labels"]):
                search_count += 1
                seen_srcs.update(l[4:] for l in issue["fields"].get("labels", []) if l.startswith("src-"))
            missing_srcs = sorted(set(self.by_src) - seen_srcs)
            if search_count == exp_issues and not missing_srcs:
                break
            log.info("Verify: search shows %d/%d issues (attempt %d); waiting for index", search_count, exp_issues, attempts)
            if attempts < self.verify_attempts:
                self._sleep(self.verify_delay)

        # 2) Direct reads (not index dependent): comments, links, statuses.
        def check(rec: dict) -> dict:
            key = self.mapping[rec["source_id"]]
            issue = self.client.get_issue(key, ["issuelinks", "status"])
            texts = [adf_to_text(c.get("body")) for c in self.client.iter_comments(key)]
            pairs = set()
            for link in issue["fields"].get("issuelinks", []) or []:
                other = (link.get("outwardIssue") or link.get("inwardIssue") or {}).get("key")
                if other:
                    pairs.add(frozenset((key, other)))
            return {
                "src": rec["source_id"],
                "comments": len(texts),
                "comments_exact": texts == (rec.get("comments") or []),
                "pairs": pairs,
                "status_ok": issue["fields"]["status"]["name"].lower() == rec["recorded_status"].lower(),
            }

        rows = self._pmap(check, self.records)
        found_pairs = set().union(*(r["pairs"] for r in rows)) if rows else set()
        comment_total = sum(r["comments"] for r in rows)
        result = {
            "issues": {"expected": exp_issues, "found_via_search": search_count, "search_attempts": attempts,
                       "mapped": len(self.mapping), "missing_from_search": missing_srcs[:20]},
            "comments": {"expected": exp_comments, "found": comment_total,
                         "issues_with_mismatched_text": [r["src"] for r in rows if not r["comments_exact"]][:20]},
            "links": {"expected": len(exp_pairs), "found": len(found_pairs & exp_pairs),
                      "unexpected": len(found_pairs - exp_pairs)},
            "statuses": {"expected": exp_issues, "matching": sum(r["status_ok"] for r in rows)},
        }
        result["ok"] = (
            search_count == exp_issues
            and not missing_srcs
            and comment_total == exp_comments
            and not result["comments"]["issues_with_mismatched_text"]
            and result["links"]["found"] == len(exp_pairs)
        )
        # Status transitions are best effort, so they are reported but do not fail verification.
        return result

    # -------------------------------------------------------------- state
    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    def _save_state(self, last_run: dict | None = None) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        state = self._load_state()
        state.update({
            "project": self.project,
            "dataset_label": self.label,
            "jira_base_url": self.client.base_url,
            "issues": dict(sorted(self.mapping.items())),
        })
        if last_run is not None:
            state["last_run"] = last_run
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _key_num(key: str) -> int:
    try:
        return int(key.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return 0
