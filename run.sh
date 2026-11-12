#!/usr/bin/env bash
# Import the dataset into Jira (idempotent, verified), then run the release-readiness assessment.
# Requires a filled-in .env (see .env.example) and an existing Jira project with key TYN.
set -euo pipefail
cd "$(dirname "$0")"

if [ -x .venv/bin/python ]; then PY=.venv/bin/python
elif [ -x .venv/Scripts/python.exe ]; then PY=.venv/Scripts/python.exe
else PY=python3
fi

"$PY" -m tyn_agent import --dataset inputs/dataset.json --project TYN
"$PY" -m tyn_agent assess --project TYN --scope-label tyn-release-assessment-v2 --release R2.4 "$@"
