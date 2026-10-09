"""Later, stronger facts dominate the early semantic hypotheses shown to the user.

The semantic pass runs before structural analysis (its hints rank the impact frontier). Its
uncertainties are typed; once the HTTP paths are established, a structural uncertainty those
paths answer (which route / which caller reaches the change) is no longer listed as open.
Runtime, dependency and other uncertainty is never touched. No model call.
"""

from __future__ import annotations

from sydes.report.verify_terminal import render_verify_change_terminal
from sydes.verify.analyzer import _reconcile_change_analysis
from sydes.verify.models import (
    AcceptedImpact,
    ChangedFile,
    ChangedSymbol,
    ChangeSet,
    ChangeVerificationResult,
)
from sydes.verify.pr_semantic_analysis import (
    parse_semantic_analysis,
    reconcile_uncertainties,
)

ROUTE_Q = "The supplied context does not show route decorators, so which HTTP endpoints reach create_speech cannot be established."
CALLER_Q = "Which callers reach the changed helper is not shown."
RUNTIME_Q = "Whether every client-abort path closes the response iterator depends on the server."


def _analysis(*uncertainties):
    return parse_semantic_analysis({"change_summary": "s", "uncertainties": list(uncertainties)})


def test_uncertainties_are_typed_and_older_plain_strings_still_parse() -> None:
    analysis = _analysis(
        {"kind": "route_identity", "text": ROUTE_Q, "symbols": ["create_speech"]},
        {"kind": "made_up_kind", "text": "x"},
        "a plain string uncertainty",
    )
    assert [(item.kind, item.symbols) for item in analysis.uncertainty_items] == [
        ("route_identity", ["create_speech"]), ("other", []), ("other", [])]
    assert analysis.uncertainties == [ROUTE_Q, "x", "a plain string uncertainty"]


def test_an_established_path_answers_route_questions_and_informs_caller_questions() -> None:
    analysis = _analysis(
        {"kind": "route_identity", "text": ROUTE_Q, "symbols": ["create_speech"]},
        {"kind": "caller_reachability", "text": CALLER_Q, "symbols": []},
        {"kind": "runtime_condition", "text": RUNTIME_Q, "symbols": ["create_speech"]},
        {"kind": "route_identity", "text": "Which route reaches orphan is unknown.", "symbols": ["orphan"]},
    )
    established = {"create_speech": ["POST /v1/audio/speech", "POST /dev/dialogue"], "helper": ["POST /x"]}
    answered = reconcile_uncertainties(analysis, established=established, changed_symbols=["create_speech", "helper"])
    assert answered == 1
    # a caller question stays open (other callers may exist) but carries what is established;
    # genuine runtime uncertainty and an unanswered route question stay as they were
    assert analysis.uncertainties == [
        (f"{CALLER_Q} (established paths: create_speech → POST /dev/dialogue, POST /v1/audio/speech; "
         "helper → POST /x)"),
        RUNTIME_Q, "Which route reaches orphan is unknown."]
    route = analysis.uncertainty_items[0]
    assert route.resolved_by == "established by structural analysis: POST /dev/dialogue; POST /v1/audio/speech"


GROUPED_Q = "The supplied diff does not show which concrete HTTP routes invoke loginUser, renewAccessToken, or authMiddleware."
LOGIN_PATHS = {"loginUser": ["POST /users/login"], "renewAccessToken": ["POST /tokens/renew_access"]}


def test_route_question_with_every_subject_established_is_answered() -> None:
    analysis = _analysis({"kind": "route_identity", "text": GROUPED_Q, "symbols": ["Server.loginUser", "Server.renewAccessToken"]})
    assert reconcile_uncertainties(analysis, established=LOGIN_PATHS, changed_symbols=[]) == 1
    assert analysis.uncertainties == []


def test_route_question_with_some_subjects_established_is_scoped_to_the_rest() -> None:
    analysis = _analysis({"kind": "route_identity", "text": GROUPED_Q,
                          "symbols": ["Server.loginUser", "Server.renewAccessToken", "authMiddleware"]})
    assert reconcile_uncertainties(analysis, established=LOGIN_PATHS, changed_symbols=[]) == 0
    [text] = analysis.uncertainties
    # the established subjects are no longer described as unknown; the unresolved one stays visible
    assert text == ("Which HTTP route reaches authMiddleware is not established by this analysis. "
                    "(routes established for the others: loginUser → POST /users/login; "
                    "renewAccessToken → POST /tokens/renew_access)")
    assert GROUPED_Q not in text
    item = analysis.uncertainty_items[0]
    assert item.text == GROUPED_Q and item.resolved_by is None  # the original stays in the data
    # never a claim that no other route or caller exists
    assert "no other" not in text and "only" not in text


def test_route_question_with_no_subject_established_is_unchanged() -> None:
    analysis = _analysis({"kind": "route_identity", "text": GROUPED_Q, "symbols": ["authMiddleware"]})
    assert reconcile_uncertainties(analysis, established=LOGIN_PATHS, changed_symbols=[]) == 0
    assert analysis.uncertainties == [GROUPED_Q]


def test_caller_questions_stay_conservative_and_runtime_is_untouched() -> None:
    caller = "Which gRPC methods call authorizeUser, or whether every protected operation passes through it, is not shown."
    analysis = _analysis(
        {"kind": "caller_reachability", "text": caller, "symbols": ["Server.loginUser", "authorizeUser"]},
        {"kind": "runtime_condition", "text": RUNTIME_Q, "symbols": ["Server.loginUser"]},
    )
    assert reconcile_uncertainties(analysis, established=LOGIN_PATHS, changed_symbols=[]) == 0
    assert analysis.uncertainties == [
        f"{caller} (established paths: loginUser → POST /users/login)", RUNTIME_Q]


def test_the_analyzer_reconciles_against_proven_http_impacts_only() -> None:
    change = ChangeSet(base="main", files=[
        ChangedFile(repo="app", path="api/routes.py"),
        ChangedFile(repo="app", path="api/tests/test_routes.py", role="test_usage_candidate"),
    ], symbols=[
        ChangedSymbol(id="1", repo="app", file="api/routes.py", name="create_speech"),
        ChangedSymbol(id="2", repo="app", file="api/tests/test_routes.py", name="test_speech"),
    ])
    result = ChangeVerificationResult(change=change)
    result.pr_semantic_analysis = _analysis(
        {"kind": "route_identity", "text": ROUTE_Q},
        {"kind": "runtime_condition", "text": RUNTIME_Q},
    )
    result.accepted_impacts = [
        AcceptedImpact(id="i1", label="POST /v1/audio/speech", status="proven", route_method="POST",
                       route_path="/v1/audio/speech", changed_symbols=["create_speech"]),
        AcceptedImpact(id="i2", label="GET /maybe", status="inferred", route_method="GET",
                       route_path="/maybe", changed_symbols=["test_speech"]),
    ]
    _reconcile_change_analysis(result, change)
    # test functions are not program symbols an endpoint must reach; inferred impacts prove nothing
    assert result.pr_semantic_analysis.uncertainties == [RUNTIME_Q]
    assert "change_analysis_uncertainties_answered_by_structure=1" in result.diagnostics
    text = render_verify_change_terminal(result)
    assert ROUTE_Q not in text and RUNTIME_Q in text
    verbose = render_verify_change_terminal(result, verbose=True)
    assert "Answered by structural analysis:" in verbose and "POST /v1/audio/speech" in verbose


def test_nothing_established_leaves_every_uncertainty_open() -> None:
    analysis = _analysis({"kind": "route_identity", "text": ROUTE_Q})
    assert reconcile_uncertainties(analysis, established={}, changed_symbols=["create_speech"]) == 0
    assert analysis.uncertainties == [ROUTE_Q]
