"""Render report.json / report.md, trace.md and metrics.json for one run."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PROVENANCE_NOTE = (
    "Recorded actor, role and time are taken from each comment's \"Recorded event\" line in the imported "
    "record. Jira's native comment authors and timestamps belong to the importer and are not used."
)


def _cell(v: Any, limit: int | None = None) -> str:
    s = "" if v is None else str(v)
    s = s.replace("\r", " ").replace("\n", " ").replace("|", "\\|").strip()
    if limit and len(s) > limit:
        s = s[: limit - 1] + "…"
    return s


def write_outputs(run_dir: Path, report: dict, metrics: dict, events: list[dict]) -> None:
    run_dir = Path(run_dir)
    report = {**report, "metrics": metrics}
    (run_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, default=str), encoding="utf-8")
    (run_dir / "report.md").write_text(render_report_md(report), encoding="utf-8")
    (run_dir / "trace.md").write_text(render_trace_md(events, report.get("run_id", "")), encoding="utf-8")


# --------------------------------------------------------------------------- report.md
def render_report_md(rep: dict) -> str:
    L: list[str] = []
    tgt = rep.get("target") or {}
    L.append(f"# Release readiness report: {rep.get('release')}"
             + (f" ({tgt.get('service')})" if tgt.get("service") else ""))
    L.append("")
    L.append(f"**Overall decision: {rep.get('overall_decision')}**. {rep.get('overall_reason', '')}")
    L.append("")
    L.append("| Item | Value |")
    L.append("|---|---|")
    L.append(f"| Run | `{rep.get('run_id')}` generated {rep.get('generated_at')} |")
    L.append(f"| Jira project / dataset scope | `{rep.get('project')}` / `{rep.get('scope_label')}` |")
    if rep.get("manifest"):
        L.append(f"| Release manifest | {rep['manifest']['issue_key']} ({rep['manifest']['source_id']}) |")
    if tgt:
        L.append("| Target | " + _cell(", ".join(f"{k} {v}" for k, v in tgt.items())) + " |")
    L.append(f"| Model | `{rep.get('model')}` |")
    L.append("")
    L.append("Decision rule (applied in code): BLOCKED if any mandatory requirement is FAIL; otherwise "
             "NEEDS_REVIEW if any is INSUFFICIENT_EVIDENCE; READY only if every mandatory requirement is PASS.")
    L.append("")
    d = rep.get("discovery") or {}
    if not d.get("ok"):
        L.append(f"> **AGENT FAILURE (discovery):** {d.get('failure')}. No requirements were assessed.")
        L.append("")

    reqs = rep.get("requirements") or []
    if reqs:
        L.append("## Summary")
        L.append("")
        L.append("| Requirement | Title | Mandatory | Decision | Decisive evidence | Flags |")
        L.append("|---|---|---|---|---|---|")
        for r in reqs:
            q = r["requirement"]
            dec = r["decision"] + (" (AGENT FAILURE)" if r.get("agent_failure") else "")
            decisive = ", ".join(sorted({f"{e['issue_key']}#{e['comment_number'] or e['comment_id']}"
                                         for e in r.get("evidence", []) if e.get("decisive")})) or "none"
            flags = len(r.get("integrity_flags") or [])
            L.append(f"| {q['source_id']} ({q['issue_key']}) | {_cell(q['title'])} | "
                     f"{'yes' if q.get('mandatory', True) else 'no'} | **{dec}** | {decisive} | {flags or ''} |")
        L.append("")
        counts: dict[str, int] = {}
        for r in reqs:
            counts[r["decision"]] = counts.get(r["decision"], 0) + 1
        L.append("Counts: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
                 + f"; agent failures {sum(1 for r in reqs if r.get('agent_failure'))}.")
        L.append("")
        L.append("## Requirements")
        L.append("")
        for r in reqs:
            L.extend(_render_requirement(r))

    L.extend(_render_metrics(rep.get("metrics") or {}))
    L.append("")
    L.append(f"_{PROVENANCE_NOTE}_")
    L.append("")
    return "\n".join(L)


def _render_requirement(r: dict) -> list[str]:
    q = r["requirement"]
    L = [f"### {q['source_id']}: {q['title']} ({q['issue_key']})", ""]
    L.append(f"- **Criterion:** {q['criterion']}")
    L.append(f"- **Mandatory:** {'yes' if q.get('mandatory', True) else 'no'}")
    if r.get("agent_failure"):
        L.append(f"- **Decision:** **INSUFFICIENT_EVIDENCE: AGENT FAILURE** ({r.get('failure_reason')})")
    else:
        L.append(f"- **Decision:** **{r['decision']}**")
    L.append("")
    L.append(r.get("explanation") or "")
    L.append("")
    ev = r.get("evidence") or []
    if ev:
        L.append("**Evidence considered**")
        L.append("")
        L.append("| Jira | Source | Comment (no. / id) | Recorded actor / role / time | Applicability | Stance | Excerpt |")
        L.append("|---|---|---|---|---|---|---|")
        order = {"applicable": 0, "superseded": 1, "wrong_scope": 2, "not_authoritative": 3, "context": 4}
        for e in sorted(ev, key=lambda e: (not e.get("decisive"), order.get(e.get("applicability"), 9))):
            where = (f"#{e['comment_number']} / {e['comment_id']}" if e.get("comment_id") else "description")
            who = " / ".join(_cell(x) for x in (e.get("recorded_actor"), e.get("recorded_role"), e.get("recorded_time")) if x)
            app = _cell(e.get("applicability")) + (" (decisive)" if e.get("decisive") else "")
            excerpt = f"“{_cell(e.get('excerpt'), 300)}”"
            if e.get("reason"):
                excerpt += f"<br>_{_cell(e['reason'], 300)}_"
            L.append(f"| {e['issue_key']} | {_cell(e['source_id'])} | {where} | {who} | {app} | "
                     f"{_cell(e.get('stance'))} | {excerpt} |")
        L.append("")
    out = r.get("outstanding") or []
    L.append("**Outstanding:** " + ("none" if not out else ""))
    for o in out:
        L.append(f"- {o}")
    L.append("")
    flags = r.get("integrity_flags") or []
    L.append("**Integrity flags:** " + ("none" if not flags else ""))
    for f in flags:
        loc = f"{f['issue_key']} ({f['source_id']})"
        if f.get("comment_id"):
            loc += f" comment #{f.get('comment_number')} / {f['comment_id']}"
            if f.get("recorded_actor"):
                loc += f" by {f['recorded_actor']} ({f.get('recorded_role')}) at {f.get('recorded_time')}"
        line = f"- {loc}: {f.get('concern')}"
        if f.get("excerpt"):
            line += f" “{_cell(f['excerpt'], 300)}”"
        L.append(line)
    L.append("")
    if r.get("refusals"):
        L.append(f"Scope refusals during investigation: {len(r['refusals'])}")
        L.append("")
    a = r.get("agent") or {}
    L.append(f"<sub>Agent: {a.get('turns')} turns, {a.get('tool_calls')} tool calls, "
             f"{a.get('submit_attempts')} submission attempt(s), {a.get('seconds')} s.</sub>")
    L.append("")
    return L


def _render_metrics(m: dict) -> list[str]:
    if not m:
        return []
    t = m.get("tokens", {})
    j = m.get("jira", {})
    L = ["## Run metrics", "", "| Metric | Value |", "|---|---|"]
    L.append(f"| Wall time | {m.get('wall_seconds')} s |")
    for k, v in (m.get("phase_seconds") or {}).items():
        L.append(f"| Phase: {k} | {v} s |")
    L.append(f"| Model calls | {m.get('model_calls')} ({m.get('model_seconds')} s in model) |")
    L.append(f"| Tokens: input / output | {t.get('input_tokens')} / {t.get('output_tokens')} |")
    L.append(f"| Tokens: cache write / cache read | {t.get('cache_creation_input_tokens')} / {t.get('cache_read_input_tokens')} |")
    L.append(f"| Tool calls | {m.get('tool_calls_total')} ({_cell(', '.join(f'{k} {v}' for k, v in (m.get('tool_calls') or {}).items()))}) |")
    L.append(f"| Tool errors (incl. scope refusals) | {m.get('tool_errors')} |")
    L.append(f"| Submission rejections (validation) | {m.get('submission_rejections')} |")
    L.append(f"| Agent failures | {len(m.get('agent_failures') or [])} |")
    L.append(f"| Jira requests / retries | {j.get('requests')} / {j.get('retries')} |")
    L.append(f"| Jira errors | {j.get('errors')} |")
    L.append("")
    return L


# --------------------------------------------------------------------------- trace.md
def _short(v: Any, limit: int) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + f"… [{len(s) - limit} more chars]"


def _result_summary(tool: str, result: Any) -> str:
    if not isinstance(result, dict):
        return _short(result, 300)
    if "error" in result:
        return "ERROR: " + _short(result["error"], 300)
    if tool == "search_issues":
        items = ", ".join(f"{r['key']}={r['source_id']}" for r in result.get("results", []))
        return f"{result.get('count')} result(s): {items}" + (" (more pages)" if result.get("next_page_token") else "")
    if tool == "get_issue":
        dec = ", ".join(l.get("source_id") or "?" for l in result.get("links_this_record_declares", []))
        frm = ", ".join(l.get("source_id") or "?" for l in result.get("records_linking_to_this", []))
        return (f"{result.get('key')} {result.get('source_id')} [{result.get('kind')}, {result.get('recorded_release')}, "
                f"Jira {result.get('jira_status')}], declares [{dec}], linked from [{frm}], "
                f"{result.get('comment_count')} comments")
    if tool in ("get_comments", "search_comments"):
        lines = [f"{len(result.get('comments', []))} comment(s)"
                 + (f", next_start_at={result.get('next_start_at')}" if tool == "get_comments" else
                    f", matched {result.get('matched')} of {result.get('comments_searched')}")]
        for c in result.get("comments", []):
            body = c["text"].split("\n", 1)[-1]
            lines.append(f"    - #{c['number']} id {c['comment_id']} | {c['recorded_time']} | {c['actor']} ({c['role']}): "
                         + _short(body, 160))
        return "\n".join(lines)
    if tool == "check_scope":
        return f"{result.get('verdict')} on {result.get('checked')}; mismatched {result.get('mismatched')}, not stated {result.get('not_stated')}"
    return _short(result, 300)


def render_trace_md(events: list[dict], run_id: str) -> str:
    agents: list[str] = []
    for e in events:
        if e["agent"] not in agents:
            agents.append(e["agent"])
    L = [f"# Investigation trace `{run_id}`", "",
         "Every model turn and tool call, grouped by agent, with timings (t = ms since run start). "
         "Full payloads are in trace.jsonl.", ""]
    for agent in agents:
        L.append(f"## {agent}")
        L.append("")
        for e in (x for x in events if x["agent"] == agent):
            k = e["kind"]
            head = f"- `t={e['t_ms']}`"
            if k == "model_turn":
                u = e.get("usage") or {}
                L.append(f"{head} **turn {e['turn']}** ({e['duration_ms']} ms, stop={e['stop_reason']}, "
                         f"in {u.get('input_tokens')}, out {u.get('output_tokens')}, "
                         f"cache r/w {u.get('cache_read_input_tokens')}/{u.get('cache_creation_input_tokens')})")
                for b in e.get("blocks") or []:
                    if b["type"] == "thinking" and b.get("summary"):
                        L.append(f"    - thinking: {_short(b['summary'].replace(chr(10), ' '), 500)}")
                    elif b["type"] == "text" and b.get("text"):
                        L.append(f"    - says: {_short(b['text'].replace(chr(10), ' '), 500)}")
                    elif b["type"] == "tool_use":
                        L.append(f"    - calls `{b['name']}` {_short(b.get('input'), 300)}")
            elif k == "tool_call":
                L.append(f"{head} tool `{e['tool']}` {_short(e.get('input'), 200)} ({e['duration_ms']} ms)"
                         + (" **error**" if e.get("is_error") else ""))
                L.append("    - " + _result_summary(e["tool"], e.get("result")).replace("\n", "\n    "))
            elif k == "submission":
                status = "accepted" if e.get("accepted") else "REJECTED"
                L.append(f"{head} submission attempt {e['attempt']}: **{status}**")
                sub = e.get("submission") or {}
                if sub.get("decision"):
                    L.append(f"    - decision {sub['decision']}")
                for err in e.get("errors") or []:
                    L.append(f"    - problem: {err}")
            elif k == "agent_failed":
                L.append(f"{head} **AGENT FAILURE**: {e.get('reason')}")
            elif k in ("budget_warning", "budget_forced_submission", "tool_refused_budget"):
                L.append(f"{head} {k} " + _short({x: e[x] for x in e if x not in ('seq', 'ts', 't_ms', 'agent', 'kind')}, 200))
            elif k == "agent_start":
                L.append(f"{head} start; budget {e.get('budget')}")
            else:
                L.append(f"{head} {k} " + _short({x: e[x] for x in e if x not in ('seq', 'ts', 't_ms', 'agent', 'kind')}, 400))
        L.append("")
    return "\n".join(L)
