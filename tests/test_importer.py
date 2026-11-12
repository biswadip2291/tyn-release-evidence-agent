from __future__ import annotations

from conftest import make_client, make_importer, read_state
from fake_jira import FakeJira, _adf_text
from tyn_agent.adf import adf_to_text, text_to_adf
from tyn_agent.importer import build_description, expected_link_pairs, parse_description


# ---------------------------------------------------------------- pure helpers
def test_adf_round_trip_is_lossless():
    for s in ["", "one", "a\nb", "a\n\nb\n", "Recorded event x | actor y | role z\nBody: \"quotes\" & <tags>"]:
        assert adf_to_text(text_to_adf(s)) == s


def test_description_header_round_trip():
    rec = {"source_id": "REC-1", "kind": "evidence", "release": "R2.4", "recorded_status": "Done",
           "description": "Line one.\nLine two.", "links": ["REQ-01", "REC-2"]}
    parsed = parse_description(build_description(rec))
    assert parsed["header_found"]
    assert parsed["source_id"] == "REC-1" and parsed["kind"] == "evidence"
    assert parsed["release"] == "R2.4" and parsed["recorded_status"] == "Done"
    assert parsed["declared_links"] == ["REQ-01", "REC-2"]
    assert parsed["original"] == rec["description"]
    assert "importer" in build_description(rec)


# ---------------------------------------------------------------- full real dataset
def test_full_import_is_complete(imported):
    res, ds, fj = imported["result"], imported["dataset"], imported["fake"]
    v = res["verify"]
    assert v["ok"], v
    assert v["issues"]["found_via_search"] == 400
    assert v["comments"]["found"] == 2059 == sum(len(r["comments"]) for r in ds["records"])
    assert v["links"]["found"] == v["links"]["expected"] == len(expected_link_pairs(ds["records"]))
    assert v["links"]["unexpected"] == 0
    assert v["statuses"]["matching"] == 400  # needs multi-hop transitions in the fake's linear workflow
    assert res["actions"]["issues_created"] == 400
    assert res["actions"]["comments_added"] == 2059


def test_issue_content_preserves_source(imported):
    ds, fj, mapping = imported["dataset"], imported["fake"], imported["importer"].mapping
    for rec in ds["records"]:
        issue = fj.state.issues[mapping[rec["source_id"]]]
        assert issue["summary"] == f"{rec['source_id']}: {rec['summary']}"
        assert set(issue["labels"]) == {ds["dataset_label"], f"src-{rec['source_id']}",
                                        f"rr-{rec['kind']}", f"release-{rec['release']}"}
        parsed = parse_description(_adf_text(issue["description"]))
        assert parsed["original"] == rec["description"]
        assert parsed["declared_links"] == rec["links"]
        assert issue["status"] == rec["recorded_status"]
        texts = [_adf_text(c["body"]) for c in fj.state.comments[issue["key"]]]
        assert texts == rec["comments"]  # verbatim and in original order


def test_state_file_written(imported):
    state = read_state(imported["state_path"])
    assert len(state["issues"]) == 400
    assert state["dataset_label"] == "tyn-release-assessment-v2"
    assert state["last_run"]["verify"]["ok"]


def test_rerun_is_idempotent(imported):
    fj = imported["fake"]
    before = (len(fj.state.issues), sum(map(len, fj.state.comments.values())), len(fj.state.links))
    imp = make_importer(fj.url, imported["dataset"], imported["state_path"], workers=8)
    res = imp.run()
    after = (len(fj.state.issues), sum(map(len, fj.state.comments.values())), len(fj.state.links))
    assert before == after
    assert res["actions"] == {"issues_created": 0, "comments_added": 0, "links_created": 0, "transitions_applied": 0}
    assert res["verify"]["ok"]


def test_enhanced_search_paginates(imported):
    client = make_client(imported["fake"].url)
    from tyn_agent.jql import scoped_jql
    keys = [i["key"] for i in client.search(scoped_jql("TYN", "tyn-release-assessment-v2"), ["labels"], page_size=37)]
    assert len(keys) == len(set(keys)) == 400
    assert client.stats["by_endpoint"]["POST /rest/api/3/search/jql"] == 11  # ceil(400/37)


# ---------------------------------------------------------------- resilience
def test_retries_on_429_and_503_honouring_retry_after(small, tmp_path):
    with FakeJira() as fj:
        fj.state.inject_fault(429, count=3, path_contains="/comment", retry_after="7", method="POST")
        fj.state.inject_fault(503, count=2, path_contains="/rest/api/3/issue", method="POST")
        fj.state.inject_fault(502, count=1, path_contains="/search/jql")
        sleeps: list[float] = []
        imp = make_importer(fj.url, small, tmp_path / "s.json", client=make_client(fj.url, sleeps), workers=1)
        res = imp.run()
        assert res["verify"]["ok"], res
        assert imp.client.stats["retries"] == 6
        assert sleeps.count(7.0) == 3  # Retry-After honoured exactly
        assert len(fj.state.issues) == 4
        assert sum(map(len, fj.state.comments.values())) == 8


def test_gives_up_after_max_retries(small, tmp_path):
    import pytest
    from tyn_agent.jira_client import JiraError
    with FakeJira() as fj:
        fj.state.inject_fault(503, count=100, path_contains="/project/")
        client = make_client(fj.url)
        client.max_retries = 2
        with pytest.raises(JiraError) as ei:
            make_importer(fj.url, small, tmp_path / "s.json", client=client).run()
        assert ei.value.status == 503
        assert client.stats["requests"] == 3


def test_verify_retries_while_search_index_lags(small, tmp_path):
    with FakeJira(index_lag=4) as fj:
        imp = make_importer(fj.url, small, tmp_path / "s.json", workers=1)
        res = imp.run()
        assert res["verify"]["ok"], res
        assert res["verify"]["issues"]["search_attempts"] > 1
        # Immediately re-run while the index still lags: the state file prevents duplicates.
        fj.state.index_lag = 1000
        for issue in fj.state.issues.values():
            issue["visible_after_search"] = 10**9
        imp2 = make_importer(fj.url, small, tmp_path / "s.json", workers=1, verify_attempts=1)
        res2 = imp2.run()
        assert res2["actions"]["issues_created"] == 0
        assert len(fj.state.issues) == 4


def test_resume_after_interrupted_import(small, tmp_path):
    import pytest
    from tyn_agent.jira_client import JiraError
    with FakeJira() as fj:
        # A non-retryable failure part-way through the comment phase.
        fj.state.inject_fault(400, count=1, path_contains="/comment", method="POST")
        with pytest.raises(JiraError):
            make_importer(fj.url, small, tmp_path / "s.json", workers=1).run()
        partial = sum(map(len, fj.state.comments.values()))
        assert partial < 8
        res = make_importer(fj.url, small, tmp_path / "s.json", workers=1).run()
        assert res["verify"]["ok"], res
        assert res["actions"]["issues_created"] == 0
        assert res["actions"]["comments_added"] == 8 - partial
        key = read_state(tmp_path / "s.json")["issues"]["EV-1"]
        texts = [_adf_text(c["body"]) for c in fj.state.comments[key]]
        assert texts == small["records"][3]["comments"]


def test_mutual_links_create_one_jira_link(small, tmp_path):
    # REQ-A -> EV-1 and EV-1 -> REQ-A are one relationship.
    with FakeJira() as fj:
        res = make_importer(fj.url, small, tmp_path / "s.json", workers=1).run()
        assert len(fj.state.links) == 3  # MANIFEST-REQ-A, MANIFEST-REQ-B, REQ-A-EV-1
        assert res["verify"]["links"] == {"expected": 3, "found": 3, "unexpected": 0}


def test_missing_project_fails_clearly(small, tmp_path):
    import pytest
    from tyn_agent.jira_client import JiraError
    with FakeJira(project="OTHER") as fj:
        with pytest.raises(JiraError) as ei:
            make_importer(fj.url, small, tmp_path / "s.json").run()
        assert ei.value.status == 404
