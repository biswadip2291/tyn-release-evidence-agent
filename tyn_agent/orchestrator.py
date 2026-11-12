"""Three-phase orchestration.

Phase 1  a discovery agent finds the manifest and its requirements (submit_requirements).
Phase 2  one agent per requirement, each with a fresh context (manifest rules as read from Jira, the
         target, and its single requirement), run in parallel, each ending with submit_decision.
Phase 3  the overall decision is computed deterministically in code.

The agents only ever see Jira through tools. Nothing here reads the import input file.
"""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agent import AgentResult, AgentRun, Budget
from .jira_client import JiraClient
from .model import Model
from .prompts import ASSESS_SYSTEM, DISCOVERY_SYSTEM, assess_user_message, discovery_user_message
from .report import write_outputs
from .tools import ToolBox
from .trace import Metrics, Tracer
from .validation import SUBMIT_DECISION_TOOL, SUBMIT_REQUIREMENTS_TOOL, validate_decision, validate_requirements

log = logging.getLogger("tyn.assess")

DISCOVERY_BUDGET = Budget(max_turns=20, max_tool_calls=40)
ASSESS_BUDGET = Budget(max_turns=30, max_tool_calls=60)


def overall_decision(results: list[dict]) -> tuple[str, str]:
    """BLOCKED if any mandatory FAIL; NEEDS_REVIEW if no FAIL but any INSUFFICIENT; READY if all PASS."""
    mandatory = [r for r in results if r["requirement"].get("mandatory", True)]
    if not mandatory:
        return "NEEDS_REVIEW", "No mandatory requirements were identified, so readiness cannot be established."
    fails = [r["requirement"]["source_id"] for r in mandatory if r["decision"] == "FAIL"]
    insuff = [r["requirement"]["source_id"] for r in mandatory if r["decision"] == "INSUFFICIENT_EVIDENCE"]
    if fails:
        return "BLOCKED", f"Mandatory requirement(s) failed: {', '.join(fails)}."
    if insuff:
        return "NEEDS_REVIEW", f"No mandatory requirement failed, but evidence is insufficient for: {', '.join(insuff)}."
    return "READY", "Every mandatory requirement passed."


def _enrich_evidence(items: list[dict], tb: ToolBox) -> list[dict]:
    out = []
    for ev in items or []:
        key = str(ev.get("issue_key") or "").upper()
        cid = ev.get("comment_id")
        cid = None if cid in (None, "", "null") else str(cid)
        c = tb.ledger.comments.get(cid) if cid else None
        iss = tb.ledger.issues.get(key)
        out.append({
            "issue_key": key,
            "source_id": (iss.source_id if iss else None) or tb.key_src.get(key, "unknown"),
            "comment_id": cid,
            "comment_number": c.number if c else None,
            "recorded_actor": c.actor if c else None,
            "recorded_role": c.role if c else ev.get("role"),
            "recorded_time": c.recorded_time if c else None,
            "location": "comment" if cid else "description",
            "applicability": ev.get("applicability"),
            "stance": ev.get("stance"),
            "decisive": bool(ev.get("decisive")),
            "excerpt": ev.get("excerpt"),
            "reason": ev.get("reason"),
        })
    return out


def _enrich_flags(flags: list[dict], tb: ToolBox) -> list[dict]:
    out = []
    for fl in flags or []:
        key = str(fl.get("issue_key") or "").upper()
        cid = fl.get("comment_id")
        cid = None if cid in (None, "", "null") else str(cid)
        c = tb.ledger.comments.get(cid) if cid else None
        out.append({
            "issue_key": key,
            "source_id": tb.key_src.get(key, "unknown"),
            "comment_id": cid,
            "comment_number": c.number if c else None,
            "recorded_actor": c.actor if c else None,
            "recorded_role": c.role if c else None,
            "recorded_time": c.recorded_time if c else None,
            "concern": fl.get("concern"),
            "excerpt": fl.get("excerpt"),
        })
    return out


def _agent_meta(res: AgentResult) -> dict:
    return {"turns": res.turns, "tool_calls": res.tool_calls, "submit_attempts": res.submit_attempts,
            "rejections": res.rejections, "seconds": res.seconds}


def _new_run_dir(out_root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_dir = out_root / f"run-{stamp}"
    n = 1
    while run_dir.exists():
        n += 1
        run_dir = out_root / f"run-{stamp}-{n}"
    run_dir.mkdir(parents=True)
    return run_dir


def assess(*, jira: JiraClient, model: Model, project: str, scope_label: str, release: str,
           out_root: str | Path = "outputs", max_parallel: int = 4,
           discovery_budget: Budget = DISCOVERY_BUDGET, assess_budget: Budget = ASSESS_BUDGET) -> dict:
    run_dir = _new_run_dir(Path(out_root))
    tracer = Tracer(run_dir)
    metrics = Metrics()
    tracer.event("orchestrator", "run_start", project=project, scope_label=scope_label, release=release,
                 model=getattr(model, "name", "unknown"))

    # ---------------- Phase 1: discovery
    t1 = time.monotonic()
    dtb = ToolBox(jira, project, scope_label)
    discovery = AgentRun(
        "discovery", model, dtb, system=DISCOVERY_SYSTEM,
        user_message=discovery_user_message(project, scope_label, release), phase="discover",
        submit_tool=SUBMIT_REQUIREMENTS_TOOL, validate=lambda s: validate_requirements(s, dtb.ledger, release),
        budget=discovery_budget, tracer=tracer, metrics=metrics,
    ).run()
    metrics.phase("discovery", time.monotonic() - t1)

    report: dict[str, Any] = {
        "run_id": run_dir.name,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project": project,
        "scope_label": scope_label,
        "release": release,
        "model": getattr(model, "name", "unknown"),
        "discovery": {"ok": discovery.ok, "failure": discovery.failure, **_agent_meta(discovery)},
    }

    if not discovery.ok:
        log.error("Discovery failed: %s", discovery.failure)
        report.update({"target": None, "manifest": None, "requirements": [],
                       "overall_decision": "NEEDS_REVIEW",
                       "overall_reason": f"AGENT FAILURE in discovery ({discovery.failure}); no requirement could "
                                         "be assessed. This is not a finding about the release."})
        metrics.phase("total", time.monotonic() - metrics.started)
        write_outputs(run_dir, report, metrics.snapshot(jira.stats), tracer.events)
        return {"run_dir": run_dir, "report": report, "exit_code": 3}

    sub = discovery.submission
    manifest_key = sub["manifest_key"].upper()
    manifest = dtb.ledger.issues[manifest_key]
    target = {k: sub["target"][k] for k in ("release", "commit", "service", "environment", "prompt")}
    reqs = [{**r, "issue_key": r["issue_key"].upper()} for r in sub["requirements"]]
    report.update({"target": target, "manifest": {"issue_key": manifest_key, "source_id": manifest.source_id}})
    tracer.event("orchestrator", "requirements_identified", count=len(reqs),
                 requirements=[r["source_id"] for r in reqs], target=target)

    # ---------------- Phase 2: one fresh-context agent per requirement
    t2 = time.monotonic()

    def assess_one(req: dict) -> dict:
        tb = ToolBox(jira, project, scope_label, target)
        name = f"assess:{req['source_id']}"
        res = AgentRun(
            name, model, tb, system=ASSESS_SYSTEM,
            user_message=assess_user_message(target, manifest_key, manifest.description, req), phase="assess",
            submit_tool=SUBMIT_DECISION_TOOL, validate=lambda s: validate_decision(s, tb.ledger),
            budget=assess_budget, tracer=tracer, metrics=metrics,
        ).run()
        base = {"requirement": req, "agent": _agent_meta(res), "refusals": tb.refusals}
        if not res.ok:
            return {**base, "decision": "INSUFFICIENT_EVIDENCE", "agent_failure": True,
                    "failure_reason": res.failure,
                    "explanation": f"AGENT FAILURE: {res.failure}. No decision was reached; this is not a finding "
                                   "about the requirement and it needs a re-run or manual review.",
                    "evidence": [], "outstanding": ["Re-run the assessment or review this requirement manually."],
                    "integrity_flags": []}
        s = res.submission
        return {**base, "decision": s["decision"], "agent_failure": False, "failure_reason": None,
                "explanation": s.get("explanation", ""), "evidence": _enrich_evidence(s.get("evidence"), tb),
                "outstanding": list(s.get("outstanding") or []),
                "integrity_flags": _enrich_flags(s.get("integrity_flags"), tb)}

    with ThreadPoolExecutor(max_workers=max(1, max_parallel)) as ex:
        results = list(ex.map(assess_one, reqs))
    metrics.phase("assessment", time.monotonic() - t2)

    # ---------------- Phase 3: deterministic overall decision
    t3 = time.monotonic()
    decision, reason = overall_decision(results)
    report.update({"overall_decision": decision, "overall_reason": reason, "requirements": results})
    tracer.event("orchestrator", "overall_decision", decision=decision, reason=reason,
                 per_requirement={r["requirement"]["source_id"]: r["decision"] for r in results})
    metrics.phase("decision", time.monotonic() - t3)
    metrics.phase("total", time.monotonic() - metrics.started)
    write_outputs(run_dir, report, metrics.snapshot(jira.stats), tracer.events)
    return {"run_dir": run_dir, "report": report, "exit_code": 0}


def run_assessment(*, project: str, scope_label: str, release: str, out_root: str = "outputs",
                   max_parallel: int = 4) -> int:
    from .config import JiraConfig, anthropic_settings
    from .model import AnthropicModel

    cfg = JiraConfig.from_env()
    api_key, model_name, effort = anthropic_settings()
    jira = JiraClient(cfg.base_url, cfg.email, cfg.api_token)
    model = AnthropicModel(api_key, model_name, effort=effort)
    out = assess(jira=jira, model=model, project=project, scope_label=scope_label, release=release,
                 out_root=out_root, max_parallel=max_parallel)
    rep = out["report"]
    print(f"Overall decision: {rep['overall_decision']} - {rep['overall_reason']}")
    for r in rep.get("requirements", []):
        tag = " (AGENT FAILURE)" if r.get("agent_failure") else ""
        print(f"  {r['requirement']['source_id']:<8} {r['decision']}{tag}")
    print(f"Outputs: {out['run_dir']}")
    print(json.dumps({"report": str(out['run_dir'] / 'report.md')}))
    return out["exit_code"]
