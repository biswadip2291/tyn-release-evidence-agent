# TYN Release Evidence Agent

This project imports the supplied TYN dataset into Jira Cloud. A tool-using Claude agent then works
out, using **only Jira**, whether release **R2.4** is ready. It writes an evidence-backed report with
PASS / FAIL / INSUFFICIENT_EVIDENCE for each requirement and an overall READY / NEEDS_REVIEW / BLOCKED
decision.

**Submitted run:** [`outputs/run-20261112-141611/report.md`](outputs/run-20261112-141611/report.md).
The overall decision is **BLOCKED**: REQ-02, REQ-05, REQ-08 and REQ-12 fail; REQ-03, REQ-06, REQ-09
and REQ-11 have insufficient evidence; REQ-01, REQ-04, REQ-07 and REQ-10 pass. The run took 101 s on
`claude-sonnet-5-5`.

## Setup

Requirements: Python 3.12, a Jira Cloud site, and an Anthropic API key.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip install -r requirements.txt
cp .env.example .env                             # then fill in the four values
```

| `.env` setting | Value |
|---|---|
| `JIRA_BASE_URL` | `https://<your-site>.atlassian.net` |
| `JIRA_EMAIL` | The Atlassian account email that owns the token |
| `JIRA_API_TOKEN` | A classic (unscoped) token from id.atlassian.com → Security → API tokens |
| `ANTHROPIC_API_KEY` | From console.anthropic.com |
| `ANTHROPIC_MODEL` (optional) | Default `claude-sonnet-5-5` |
| `ANTHROPIC_EFFORT` (optional) | Default `high` |
| `JIRA_ISSUE_TYPE` (optional) | Default `Task` |

The Jira project with key **TYN** must exist. A team-managed Kanban software project was used here;
its To Do / In Progress / Done statuses match the dataset. `.env` is git-ignored.

## Run

```bash
./run.sh                                   # import (idempotent and verified), then assess
```

Or run the two steps separately:

```bash
python -m tyn_agent import                 # --dataset inputs/dataset.json --project TYN --workers 4
python -m tyn_agent assess                 # --project TYN --scope-label tyn-release-assessment-v2 --release R2.4 --parallel 4
```

- **`import`** creates one issue per record, and one comment per source comment, verbatim and in order.
  It adds the issue links, moves each issue to its recorded status, and then verifies issue, comment,
  link and status counts against the dataset. It also checks every comment's text matches exactly.
  Re-running it only adds what is missing. It writes `outputs/import_state.json` (source id → Jira key,
  plus the last run's verification). Exit code 2 means verification failed.
- **`assess`** never reads `dataset.json`. Every fact the agents see comes through Jira tools. It writes
  `outputs/run-<UTC timestamp>/`, and its exit code is 3 if discovery failed.

## Tests (no Jira, no API key)

```bash
python -m pytest -q
```

There are 38 tests, which take about 15 seconds. They run against an in-process fake Jira HTTP server
([tests/fake_jira.py](tests/fake_jira.py)) that starts empty and is filled by the real importer. The fake
can inject 429/503 errors with Retry-After, and can simulate search-index lag. The tests cover:

- import completeness and verbatim content, idempotency, resuming after a crash, retry and backoff,
  and index lag;
- scope enforcement, pagination (search tokens and 64-comment threads) and `check_scope`;
- citation validation;
- one full orchestration with a scripted fake model. It includes a fabricated citation, an
  out-of-scope request, a model outage, a loop that runs out of budget, a forced submission, and a
  guard that fails if anything opens `dataset.json`.

## Outputs of a run (`outputs/run-<timestamp>/`)

| File | Contents |
|---|---|
| `report.md` / `report.json` | A summary table, then for each requirement: the criterion, decision, explanation, and cited evidence. Each citation gives the Jira key, source id, comment number and id, the recorded actor, role and time, the verbatim excerpt, and its applicability (including evidence rejected as wrong scope, superseded or not authoritative). Then outstanding items, integrity flags, and run metrics. |
| `trace.jsonl` | Every model turn (usage, stop reason, thinking summary, tool calls) and every tool call (input, full result, duration), plus budget events, submissions and validation errors. |
| `trace.md` | The same trace, readable and grouped by agent. |
| `metrics.json` | Wall time and time per phase; tokens including cache reads and writes, in total and per agent; model and tool call counts; Jira requests, retries and errors; validation rejections; agent failures. |

Recorded actors, roles and times come from each comment's `Recorded event … | actor … | role …` line.
Jira's native authors and timestamps belong to the importer and are never shown as scenario facts.

See [DESIGN.md](DESIGN.md) for architecture, design choices, known limits and AI usage.
