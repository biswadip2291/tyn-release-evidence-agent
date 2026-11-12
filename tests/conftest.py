from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_jira import FakeJira  # noqa: E402
from tyn_agent.importer import Importer, load_dataset  # noqa: E402
from tyn_agent.jira_client import JiraClient  # noqa: E402

DATASET_PATH = ROOT / "inputs" / "dataset.json"
NO_SLEEP = lambda _s: None  # noqa: E731


def make_client(url: str, sleeps: list | None = None) -> JiraClient:
    sink = sleeps.append if sleeps is not None else NO_SLEEP
    return JiraClient(url, "tester@example.com", "token", sleep=sink, max_retries=6)


def make_importer(url: str, dataset: dict, state_path: Path, **kw) -> Importer:
    client = kw.pop("client", None) or make_client(url)
    return Importer(client, "TYN", dataset, state_path=state_path, sleep=NO_SLEEP,
                    verify_delay=0, **{"workers": 4, **kw})


def small_dataset() -> dict:
    """A tiny synthetic dataset with the same shape as the real one."""
    hdr = "Recorded event 2026-11-0{d}T10:00:00+05:30 | actor Test Person | role QA Lead\n"
    recs = [
        {"source_id": "MANIFEST", "kind": "manifest", "summary": "Release manifest", "release": "R9.9",
         "recorded_status": "Done", "description": "Rules here.\nSecond line.", "comments": [hdr.format(d=1) + "Defines rules."],
         "links": ["REQ-A", "REQ-B"]},
        {"source_id": "REQ-A", "kind": "requirement", "summary": "Req A", "release": "R9.9",
         "recorded_status": "Done", "description": "Acceptance criterion: A.", "comments": [], "links": ["EV-1"]},
        {"source_id": "REQ-B", "kind": "requirement", "summary": "Req B", "release": "R9.9",
         "recorded_status": "In Progress", "description": "Acceptance criterion: B.", "comments": [], "links": []},
        {"source_id": "EV-1", "kind": "evidence", "summary": "Evidence 1", "release": "R9.9",
         "recorded_status": "To Do", "description": "Ev",
         "comments": [hdr.format(d=i % 9 + 1) + f"Note {i}." for i in range(7)], "links": ["REQ-A"]},
    ]
    return {"dataset_label": "tyn-test-scope", "target": {"release": "R9.9"}, "records": recs}


@pytest.fixture(scope="session")
def real_dataset() -> dict:
    return load_dataset(DATASET_PATH)


@pytest.fixture(scope="session")
def imported(real_dataset, tmp_path_factory):
    """One fake Jira populated with the full real dataset through the real importer."""
    state_path = tmp_path_factory.mktemp("state") / "import_state.json"
    with FakeJira() as fj:
        imp = make_importer(fj.url, real_dataset, state_path, workers=8)
        result = imp.run()
        yield {"fake": fj, "importer": imp, "result": result, "state_path": state_path,
               "dataset": real_dataset}


@pytest.fixture
def small():
    return copy.deepcopy(small_dataset())


def read_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
