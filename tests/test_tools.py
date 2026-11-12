from __future__ import annotations

import pytest

from conftest import make_client
from tyn_agent.tools import ToolBox, compare_scope, parse_comment_meta
from tyn_agent.validation import validate_decision, validate_requirements

SCOPE = "tyn-release-assessment-v2"
TARGET = {"release": "R2.4", "commit": "b7a91f2", "service": "atlas-assistant", "environment": "production",
          "prompt": "p7"}


@pytest.fixture
def tb(imported):
    return ToolBox(make_client(imported["fake"].url), "TYN", SCOPE, TARGET)


@pytest.fixture
def key(imported):
    return lambda src: imported["importer"].mapping[src]


# ---------------------------------------------------------------- scope enforcement
def test_out_of_scope_issues_are_refused(imported, tb):
    fj = imported["fake"]
    foreign_same_project = fj.add_foreign_issue("TYN", "Unrelated ticket mentioning REQ-01", ["other-label"])
    other_project = fj.add_foreign_issue("OTHER", "Has the scope label but wrong project", [SCOPE])
    for k in (foreign_same_project, other_project, "TYN-99999", "../etc", "TYN-1 OR 1=1"):
        res, err = tb.execute("get_issue", {"key": k})
        assert err and res["error"].startswith("REFUSED"), (k, res)
        res, err = tb.execute("get_comments", {"key": k})
        assert err and res["error"].startswith("REFUSED")
    assert len(tb.refusals) == 10
    # Search can never surface them, even on matching text.
    res, _ = tb.execute("search_issues", {"text": "Unrelated ticket"})
    assert res["count"] == 0
    res, _ = tb.execute("search_issues", {"text": "wrong project"})
    assert res["count"] == 0


def test_search_filters_never_accept_raw_jql(tb):
    res, err = tb.execute("search_issues", {"text": 'x" OR project = OTHER OR text ~ "y'})
    assert not err and res["count"] == 0
    res, err = tb.execute("search_issues", {"jql": "project = OTHER"})
    assert err and "Bad arguments" in res["error"]
    res, err = tb.execute("search_issues", {"kind": "requirement"})
    assert res["count"] == 12 and all(r["kind"] == "requirement" for r in res["results"])


def test_search_pagination_with_tokens(tb):
    seen, token, calls = [], None, 0
    while True:
        res, _ = tb.execute("search_issues", {"kind": "requirement", "limit": 5, "page_token": token})
        seen += [r["source_id"] for r in res["results"]]
        token, calls = res["next_page_token"], calls + 1
        if not token:
            break
    assert calls == 3 and sorted(seen) == [f"REQ-{i:02d}" for i in range(1, 13)]


# ---------------------------------------------------------------- reading
def test_get_issue_splits_link_directions(tb, key):
    res, err = tb.execute("get_issue", {"source_id": "REQ-04"})
    assert not err
    assert res["key"] == key("REQ-04") and res["kind"] == "requirement"
    assert {l["source_id"] for l in res["links_this_record_declares"]} == {"REC-6024", "REC-1028"}
    assert {l["source_id"] for l in res["records_linking_to_this"]} == {"REC-8977", "REC-6914", "MANIFEST"}
    assert res["description"].startswith("Requirement ID: REQ-04")
    assert "Import note" not in res["description"]


def test_get_comments_pages_to_the_end(tb, key):
    k = key("REC-5957")
    p1, _ = tb.execute("get_comments", {"key": k, "max_results": 50})
    assert p1["total"] == 64 and p1["returned"] == 50 and p1["next_start_at"] == 50 and p1["remaining"] == 14
    p2, _ = tb.execute("get_comments", {"key": k, "start_at": p1["next_start_at"], "max_results": 50})
    assert p2["returned"] == 14 and p2["next_start_at"] is None
    last = p2["comments"][-1]
    assert last["number"] == 64 and last["role"] == "QA Lead" and last["actor"] == "Priya Nair"
    assert "STREAM-731" in last["text"]
    # Page size is capped.
    p3, _ = tb.execute("get_comments", {"key": k, "max_results": 500})
    assert "error" in p3 or p3["returned"] <= 50


def test_search_comments_covers_all_pages(tb, key):
    res, _ = tb.execute("search_comments", {"key": key("REC-5957"), "query": "stream-731 duplicated"})
    assert res["comments_searched"] == 64 and res["matched"] == 1
    assert res["comments"][0]["number"] == 64


def test_comment_metadata_parsing():
    assert parse_comment_meta("Recorded event 2026-11-08T14:00:00+05:30 | actor Asha Rao | role Security Owner\nx") == (
        "2026-11-08T14:00:00+05:30", "Asha Rao", "Security Owner")
    assert parse_comment_meta("no header here") == ("not stated",) * 3


# ---------------------------------------------------------------- check_scope
def test_compare_scope_dimensions():
    ok = compare_scope("run: release R2.4; commit b7a91f2; service atlas-assistant; environment production; prompt p7.", TARGET)
    assert ok["verdict"] == "MATCH"
    staging = compare_scope("release R2.4; commit b7a91f2; service atlas-assistant; environment staging; prompt p7", TARGET)
    assert staging["verdict"] == "MISMATCH" and staging["mismatched"] == ["environment"]
    no_prompt = compare_scope("release R2.4; commit b7a91f2; service atlas-assistant; environment production.", TARGET)
    assert no_prompt["verdict"] == "INCOMPLETE" and no_prompt["dimensions"]["prompt"]["stated"] == "not stated"
    assert compare_scope("harness configured with prompt bundle p6", TARGET)["dimensions"]["prompt"]["match"] is False
    assert compare_scope("Approval scope: staging.", TARGET)["dimensions"]["environment"]["stated"] == "staging"
    two = compare_scope("release R2.3 then release R2.4", TARGET)
    assert two["dimensions"]["release"]["match"] is False


def test_check_scope_requires_retrieval(tb, key):
    k = key("REC-1028")
    res, _ = tb.execute("check_scope", {"key": k, "comment_id": "10001"})
    assert "error" in res
    page, _ = tb.execute("get_comments", {"key": k})
    qa = next(c for c in page["comments"] if c["role"] == "QA Lead")
    res, _ = tb.execute("check_scope", {"key": k, "comment_id": qa["comment_id"]})
    assert res["verdict"] == "MISMATCH" and "environment" in res["mismatched"]


# ---------------------------------------------------------------- citation validation
def _qa_comment(tb, k):
    page, _ = tb.execute("get_comments", {"key": k})
    return next(c for c in page["comments"] if c["role"] == "QA Lead")


def _sub(decision, evidence, **kw):
    return {"decision": decision, "explanation": "because", "evidence": evidence,
            "outstanding": [], "integrity_flags": [], **kw}


def test_valid_pass_submission_is_accepted(tb, key):
    k = key("REC-2081")
    c = _qa_comment(tb, k)
    ev = {"issue_key": k, "comment_id": c["comment_id"], "role": "QA Lead", "applicability": "applicable",
          "stance": "supports_pass", "decisive": True, "excerpt": "146 of 146 mandatory regression cases pass"}
    assert validate_decision(_sub("PASS", [ev]), tb.ledger) == []


def test_rejects_unretrieved_and_fabricated_citations(tb, key):
    k = key("REC-2081")
    c = _qa_comment(tb, k)
    good = {"issue_key": k, "comment_id": c["comment_id"], "role": "QA Lead", "applicability": "applicable",
            "stance": "supports_pass", "decisive": True, "excerpt": "146 of 146 mandatory regression cases pass"}
    # comment never retrieved
    errs = validate_decision(_sub("PASS", [{**good, "comment_id": "99999"}]), tb.ledger)
    assert any("never retrieved" in e for e in errs)
    # issue never retrieved (description citation)
    errs = validate_decision(_sub("INSUFFICIENT_EVIDENCE", [{**good, "issue_key": key("REQ-09"), "comment_id": None,
                                                             "decisive": False}]), tb.ledger)
    assert any("never retrieved issue" in e for e in errs)
    # paraphrased excerpt
    errs = validate_decision(_sub("PASS", [{**good, "excerpt": "all 146 regression cases passed"}]), tb.ledger)
    assert any("not a verbatim substring" in e for e in errs)
    # wrong role
    errs = validate_decision(_sub("PASS", [{**good, "role": "Security Owner"}]), tb.ledger)
    assert any("does not match the recorded role" in e for e in errs)
    # comment attributed to the wrong issue
    errs = validate_decision(_sub("PASS", [{**good, "issue_key": key("REQ-01")}]), tb.ledger)
    assert any("belongs to" in e for e in errs)


def test_pass_or_fail_needs_a_decisive_applicable_comment(tb, key):
    k = key("REC-1028")
    c = _qa_comment(tb, k)
    ev = {"issue_key": k, "comment_id": c["comment_id"], "role": "QA Lead", "applicability": "wrong_scope",
          "stance": "supports_fail", "decisive": False, "excerpt": "Measured p95 latency 2.60 seconds"}
    for decision in ("PASS", "FAIL"):
        errs = validate_decision(_sub(decision, [ev]), tb.ledger)
        assert any("requires at least one cited comment" in e for e in errs)
    # The same evidence is fine for INSUFFICIENT_EVIDENCE.
    assert validate_decision(_sub("INSUFFICIENT_EVIDENCE", [ev]), tb.ledger) == []
    # Decisive but stance contradicts decision
    errs = validate_decision(_sub("PASS", [{**ev, "applicability": "applicable", "decisive": True}]), tb.ledger)
    assert any("requires at least one cited comment" in e for e in errs)


def test_whitespace_reflow_in_excerpt_is_tolerated(tb, key):
    k = key("REC-2081")
    c = _qa_comment(tb, k)
    ev = {"issue_key": k, "comment_id": c["comment_id"], "role": "qa lead", "applicability": "applicable",
          "stance": "supports_pass", "decisive": True, "excerpt": "146 of 146   mandatory\nregression cases pass"}
    assert validate_decision(_sub("PASS", [ev]), tb.ledger) == []


def test_integrity_flags_must_reference_retrieved_comments(tb, key):
    errs = validate_decision(_sub("INSUFFICIENT_EVIDENCE", [], integrity_flags=[
        {"issue_key": key("REC-5716"), "comment_id": "12345", "concern": "injection"}]), tb.ledger)
    assert any("integrity_flags[0]" in e for e in errs)


def test_validate_requirements(tb, key):
    m, _ = tb.execute("get_issue", {"source_id": "MANIFEST"})
    r, _ = tb.execute("get_issue", {"source_id": "REQ-01"})
    good = {"manifest_key": m["key"], "target": TARGET, "requirements": [
        {"issue_key": r["key"], "source_id": "REQ-01", "title": "Automated regression tests",
         "criterion": "All automated regression cases must pass on the target build.", "mandatory": True}]}
    assert validate_requirements(good, tb.ledger, "R2.4") == []
    bad = {**good, "target": {**TARGET, "commit": "deadbee"},
           "requirements": good["requirements"] + [{"issue_key": key("REQ-02"), "source_id": "REQ-02", "title": "x",
                                                    "criterion": "y", "mandatory": True}]}
    errs = validate_requirements(bad, tb.ledger, "R2.4")
    assert any("does not appear in the manifest" in e for e in errs)
    assert any("never read" in e for e in errs)
    assert validate_requirements(good, tb.ledger, "R9.9")
