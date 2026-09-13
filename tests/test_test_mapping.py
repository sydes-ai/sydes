"""Regression tests for `sydes.verify.test_mapping`, reproducing the exact
bugs found diagnosing the real evaluation run
`sydes-examples/nestjs-boilerplate#1`:

1. `_invokes_symbol` treated a test's own human-written case name (e.g.
   "...: /api/v1/auth/email/login (POST)") as if it were a call to a
   `login` symbol, because "login (POST)" satisfied a too-permissive
   `name\\s*\\(` regex. That promoted unrelated, pre-existing tests to
   "evidence" for a flow they never actually invoke as code.
2. `changed_in_diff` was computed at file granularity (`case.file in
   changed_files`), so every test in a touched file -- including tests
   that predate the diff by months -- was reported as "changed in this
   diff".
3. An `OBLIGATION_ROUTE_CONTRACT` obligation whose own statement names no
   status code was "verified" by *any* assertion-bearing, non-4xx test,
   regardless of whether that test has anything to do with the
   obligation's actual claim (real example: a plain successful-login test
   promoted to evidence for "verifies POST /login handles downstream
   timeout safely").

None of these tests name NestJS, a route, or a framework in the fix itself
-- each reproduces the bug shape generically, per the "no special-casing"
constraint that shaped the fix.
"""

from __future__ import annotations

from sydes.verify.models import (
    OBLIGATION_ROUTE_CONTRACT,
    OBLIGATION_VALIDATION,
    ORIGIN_TEST_MATRIX,
    TIER_DECLARED,
    AffectedFlow,
    ChangedFile,
    Hunk,
    VerificationObligation,
)
from sydes.verify.source_files import SourceFile
from sydes.verify.test_index import ExistingTestIndex, LocatedTest, _extract_cases_from_file
from sydes.verify.test_mapping import (
    _invokes_symbol,
    _route_prefix_mismatch,
    case_changed_in_diff,
    map_tests_to_obligation,
)

REPO = "app"


def _case(
    name: str, body: str, *, line: int = 1, end_line: int | None = None,
    route_paths: set[str] | None = None, methods: set[str] | None = None,
) -> LocatedTest:
    import re

    identifiers = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", body))
    return LocatedTest(
        repo=REPO, file="test/thing.spec.ts", name=name, line=line,
        end_line=end_line or (line + body.count("\n")),
        body=body, route_paths=route_paths or set(), methods=methods or set(),
        identifiers=identifiers,
    )


def _flow(*, handler: str = "login", method: str = "POST", path: str = "/login") -> AffectedFlow:
    return AffectedFlow(id="flow:x", entry_label=f"{method} {path}", method=method, path=path, handler=handler)


def _obligation(kind: str, statement: str, origin: str = ORIGIN_TEST_MATRIX) -> VerificationObligation:
    return VerificationObligation(
        id="ob:1", flow_id="flow:x", kind=kind, statement=statement, origin=origin, required=False,
    )


# ---------------------------------------------------------------------------
# 1. `_invokes_symbol` must not match a test's own display name.
# ---------------------------------------------------------------------------


def test_invokes_symbol_rejects_display_name_that_looks_like_a_call():
    case = _case(
        "should successfully with unconfirmed email: /api/v1/auth/email/login (POST)",
        "it('should successfully with unconfirmed email: /api/v1/auth/email/login (POST)', () => {\n"
        "  return request(app).post('/api/v1/auth/email/login').expect(200);\n"
        "});\n",
    )
    assert _invokes_symbol(case, "login") is False


def test_invokes_symbol_accepts_a_real_call_site():
    case = _case(
        "logs the user in",
        "it('logs the user in', () => {\n"
        "  return login(app, credentials);\n"
        "});\n",
    )
    assert _invokes_symbol(case, "login") is True


def test_invokes_symbol_rejects_comment_mention_with_a_space_before_paren():
    """A second, independent guard: even outside the declared-name span, an
    identifier immediately followed by a space and a parenthetical is not a
    call site -- real call syntax never has that space."""
    case = _case(
        "unrelated case",
        "it('unrelated case', () => {\n"
        "  // see login (POST) docs for details\n"
        "  return doSomethingElse();\n"
        "});\n",
    )
    assert _invokes_symbol(case, "login") is False


# ---------------------------------------------------------------------------
# 2. `changed_in_diff` at test-case granularity, not file granularity.
# ---------------------------------------------------------------------------


def test_case_changed_in_diff_true_when_hunk_overlaps_case_range():
    case = _case("new case", "it('new case', () => {\n  doThing();\n});\n", line=10, end_line=12)
    hunks = [Hunk(start_line=11, end_line=11)]
    assert case_changed_in_diff(case, hunks) is True


def test_case_changed_in_diff_false_for_untouched_case_in_a_touched_file():
    """The exact NestJS shape: the diff added one new test elsewhere in the
    file, but a pre-existing, untouched case earlier in the same file must
    not be reported as changed."""
    case = _case("pre-existing case", "it('pre-existing case', () => {\n  doThing();\n});\n", line=10, end_line=12)
    hunks = [Hunk(start_line=200, end_line=210)]  # the diff touched a much later part of the file
    assert case_changed_in_diff(case, hunks) is False


def test_untouched_case_immediately_before_a_new_case_is_not_swept_into_its_hunk():
    """Real bug found rerunning sydes-examples/nestjs-boilerplate#1: a
    pre-existing case's heuristic `end_line` used to be "next TEST's start
    minus one", which does not stop at an intervening `describe(...)`
    header. When a brand-new test is inserted in its own new `describe`
    block immediately after an untouched one, that untouched case's range
    swallowed the new block's own opening line -- making it falsely overlap
    the new hunk. `describe`/class headers must count as boundaries too.
    """
    text = (
        "describe('Auth Module', () => {\n"
        "  describe('Login', () => {\n"
        "    it('should successfully for user with confirmed email', () => {\n"
        "      return request(app).post('/api/v1/auth/email/login').expect(200);\n"
        "    });\n"
        "  });\n"
        "\n"
        "  describe('Forgot password', () => {\n"
        "    it('should reset password only once per link', async () => {\n"
        "      await request(app).post('/api/v1/auth/reset/password');\n"
        "    });\n"
        "  });\n"
        "});\n"
    )
    source = SourceFile(repo=REPO, path="test/x.spec.ts", text=text, role="test_usage_candidate", extension=".ts")
    cases = {c.name: c for c in _extract_cases_from_file(source)}
    untouched = cases["should successfully for user with confirmed email"]
    new_case = cases["should reset password only once per link"]

    # The hunk is a pure insertion starting exactly at the new describe
    # block's own header line.
    hunk = Hunk(start_line=new_case.line - 1, end_line=new_case.line + 5)
    assert case_changed_in_diff(untouched, [hunk]) is False
    assert case_changed_in_diff(new_case, [hunk]) is True


def test_case_changed_in_diff_false_when_file_has_no_hunks():
    case = _case("case", "it('case', () => {});\n", line=1, end_line=1)
    assert case_changed_in_diff(case, None) is False
    assert case_changed_in_diff(case, []) is False


# ---------------------------------------------------------------------------
# 3. Route-contract evidence-tier promotion must be tied to the obligation's
#    own claim, not just "has an assertion, isn't a 4xx".
# ---------------------------------------------------------------------------


def _index(cases: list[LocatedTest]) -> ExistingTestIndex:
    return ExistingTestIndex(repo=REPO, cases=cases)


def test_route_contract_with_no_declared_code_is_supporting_not_evidence():
    """The real fabricated-obligation shape: an obligation with no status
    code in its own statement ("verifies POST /login handles downstream
    timeout safely") must not be "verified" by a test that merely asserts
    *some* non-4xx outcome on the same route -- it has nothing to do with
    a timeout."""
    case = _case(
        "plain success case",
        "it('plain success case', () => {\n"
        "  return request(app).post('/login').send({}).expect(200);\n"
        "});\n",
        route_paths={"/login"}, methods={"POST"},
    )
    obligation = _obligation(OBLIGATION_ROUTE_CONTRACT, "verifies POST /login handles downstream timeout safely")
    evidence, supporting, _notes = map_tests_to_obligation(
        obligation=obligation, flow=_flow(), test_index=_index([case]), changed_symbol_names=set(),
    )
    assert evidence == []
    assert len(supporting) == 1
    assert supporting[0].evidence_tier == TIER_DECLARED


def test_route_contract_with_declared_code_still_gets_evidence():
    """The one path that must keep working: an obligation whose own
    statement names a real status code, matched by a test that actually
    asserts that code, is still promoted to evidence."""
    case = _case(
        "returns 201",
        "it('returns 201', () => {\n"
        "  const res = request(app).post('/login').send({});\n"
        "  expect(res.status == 201);\n"
        "});\n",
        route_paths={"/login"}, methods={"POST"},
    )
    obligation = _obligation(OBLIGATION_ROUTE_CONTRACT, "POST /login responds 201")
    evidence, supporting, _notes = map_tests_to_obligation(
        obligation=obligation, flow=_flow(), test_index=_index([case]), changed_symbol_names=set(),
    )
    assert len(evidence) == 1
    assert supporting == []


def test_validation_obligation_rejection_path_is_unaffected():
    """A sanity check that the unrelated VALIDATION branch (a genuinely
    different, already-correct code path) still works after the
    ROUTE_CONTRACT branch was tightened."""
    case = _case(
        "rejects bad payload",
        "it('rejects bad payload', () => {\n"
        "  const res = request(app).post('/login').send({});\n"
        "  expect(res.status == 400);\n"
        "});\n",
        route_paths={"/login"}, methods={"POST"},
    )
    obligation = _obligation(OBLIGATION_VALIDATION, "rejects malformed payloads")
    evidence, supporting, _notes = map_tests_to_obligation(
        obligation=obligation, flow=_flow(), test_index=_index([case]), changed_symbol_names=set(),
    )
    assert len(evidence) == 1
    assert supporting == []


# ---------------------------------------------------------------------------
# 4. Route-prefix mismatch: a diagnostic only, never used to map a test.
# ---------------------------------------------------------------------------


def test_route_prefix_mismatch_is_reported_but_never_maps_the_test():
    case = _case(
        "full path case",
        "it('full path case', () => {\n"
        "  return request(app).post('/api/v1/auth/email/login').send({}).expect(200);\n"
        "});\n",
        route_paths={"/api/v1/auth/email/login"}, methods={"POST"},
    )
    obligation = _obligation(OBLIGATION_ROUTE_CONTRACT, "contract happy path")
    evidence, supporting, notes = map_tests_to_obligation(
        obligation=obligation,
        flow=_flow(handler="somethingElseEntirely", path="/login"),
        test_index=_index([case]),
        changed_symbol_names=set(),
    )
    assert evidence == []
    assert supporting == []
    assert len(notes) == 1
    assert "possible missing route prefix" in notes[0]


def test_route_prefix_mismatch_none_when_paths_are_unrelated():
    assert _route_prefix_mismatch(
        _case("x", "it('x', () => { request(app).get('/other/thing'); });\n", route_paths={"/other/thing"}),
        "/login",
    ) is None


def test_route_prefix_mismatch_none_on_exact_match():
    assert _route_prefix_mismatch(
        _case("x", "it('x', () => { request(app).get('/login'); });\n", route_paths={"/login"}),
        "/login",
    ) is None
