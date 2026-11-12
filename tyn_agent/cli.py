"""Command line entry point: ``python -m tyn_agent import|assess``."""
from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import DEFAULT_PROJECT, DEFAULT_RELEASE, DEFAULT_SCOPE_LABEL, ConfigError, JiraConfig
from .jira_client import JiraClient, JiraError


def _client(cfg: JiraConfig) -> JiraClient:
    return JiraClient(cfg.base_url, cfg.email, cfg.api_token)


def cmd_import(args) -> int:
    from .importer import Importer, load_dataset

    cfg = JiraConfig.from_env()
    dataset = load_dataset(args.dataset)
    imp = Importer(_client(cfg), args.project, dataset, issue_type=cfg.issue_type,
                   state_path=args.state, workers=args.workers)
    result = imp.run(verify=not args.no_verify)
    print(json.dumps(result, indent=2))
    if result.get("verify") and not result["verify"]["ok"]:
        print("VERIFY FAILED: counts in Jira do not match the dataset (see above).", file=sys.stderr)
        return 2
    return 0


def cmd_assess(args) -> int:
    from .orchestrator import run_assessment

    return run_assessment(project=args.project, scope_label=args.scope_label, release=args.release,
                          out_root=args.out, max_parallel=args.parallel)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tyn_agent")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("import", help="Import dataset.json into Jira (idempotent) and verify")
    pi.add_argument("--dataset", default="inputs/dataset.json")
    pi.add_argument("--project", default=DEFAULT_PROJECT)
    pi.add_argument("--state", default="outputs/import_state.json")
    pi.add_argument("--workers", type=int, default=4)
    pi.add_argument("--no-verify", action="store_true")
    pi.set_defaults(fn=cmd_import)

    pa = sub.add_parser("assess", help="Run the release-readiness agent against Jira")
    pa.add_argument("--project", default=DEFAULT_PROJECT)
    pa.add_argument("--scope-label", default=DEFAULT_SCOPE_LABEL)
    pa.add_argument("--release", default=DEFAULT_RELEASE)
    pa.add_argument("--out", default="outputs")
    pa.add_argument("--parallel", type=int, default=4)
    pa.set_defaults(fn=cmd_assess)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return args.fn(args)
    except (ConfigError, JiraError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
