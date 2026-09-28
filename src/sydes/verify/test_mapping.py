"""Conservative mapping of existing tests to specific verification obligations.

The rule this module exists to enforce: a test is evidence for *one obligation*,
and only when there is an inspectable reason. Bare identifier overlap with
something in the flow is a coincidence, not verification, and is rejected
outright — broadening the matcher to raise apparent coverage would make the
whole result untrustworthy.

Every accepted mapping records `match_rule`, `evidence_tier`, and `source_refs`.
"""

from __future__ import annotations

import re

from sydes.core.models import EvidenceRef
from sydes.trace.cross_repo import normalize_api_path
from sydes.verify.models import (
    OBLIGATION_CROSS_REPO_CALL,
    OBLIGATION_EVENT_EMISSION,
    OBLIGATION_ROUTE_CONTRACT,
    OBLIGATION_SIDE_EFFECT,
    OBLIGATION_STATE_CONSISTENCY,
    OBLIGATION_VALIDATION,
    TIER_ASSERTED_EFFECT,
    TIER_DECLARED,
    TIER_DIRECT_INVOCATION,
    TIER_DIRECT_ROUTE,
    AffectedFlow,
    Hunk,
    MappedTest,
    VerificationObligation,
)
from sydes.verify.test_index import ExistingTestIndex, LocatedTest

# Words that carry no discriminating power. Present only to document that these
# are explicitly refused, since they are exactly what a lexical matcher latches
# onto.
_NON_DISCRIMINATING = {
    "create", "get", "post", "put", "patch", "delete", "update", "list", "find",
    "save", "add", "remove", "data", "client", "session", "db", "user", "item",
    "record", "payload", "request", "response", "result", "value", "test", "app",
    "service", "handler", "router", "model", "schema", "config", "setup",
}

_ASSERT_RE = re.compile(r"\bassert\b|\bexpect\s*\(|\.should\b|assertEqual|assertTrue|assertRaises")
# The `\(` alternative covers a call-style assertion where the status code is
# the connector's own first/sole argument -- e.g. REST-assured's
# `.statusCode(200)` -- distinct from the other alternatives, which all
# expect the code to follow some other operator or a closing paren from an
# earlier call. Still requires the digits to appear immediately (mediated
# only by whitespace), so `statusCode(is(200))`/`statusCode(someVar)` do not
# match through it -- an unrecognized wrapper stays unrecognized rather than
# guessed at.
_STATUS_RE = re.compile(r"\b(?:status_code|statusCode|status)\b\s*(?:==|,|\)|\.toBe\(|\()\s*(?P<code>[45]\d\d|2\d\d)")
_COUNT_RE = re.compile(r"\b(?:count|len|times|calledOnce|call_count|assert_called_once)\b", re.IGNORECASE)
# A validation obligation claims "the API rejects something" -- a literal
# HTTP status code (`_STATUS_RE`, above) is one way a test can demonstrate
# that, but far from the only one: a test can equally prove rejection by
# asserting a thrown/rejected error directly, never naming a status code at
# all (e.g. `await expect(service.create(input)).rejects.toMatchObject({...})`,
# matched against the error's own `httpCode`/`message` fields on a variable,
# not a literal number `_STATUS_RE` could ever match). Generic across
# languages/frameworks -- jest/chai's promise-rejection and exception
# assertions, Python's `pytest.raises`/`assertRaises`, JUnit's
# `assertThrows` -- never a repo- or error-type-specific string.
_REJECTION_ASSERTION_RE = re.compile(
    r"\.rejects\b"
    r"|\.toThrow\b"
    r"|\.to\.throw\b"
    r"|\bassertThrows\b"
    r"|\bassertRaises\b"
    r"|\bpytest\.raises\b"
)
# Mockito's interaction-verification idiom: `verify(mock).method(...)` or
# `verify(mock, times(n)).method(...)` (also atLeast/atMost/atLeastOnce/
# only/never). Unlike `_asserts_effect_token`'s bare substring match, this is
# an explicit, unambiguous claim -- "this specific call on this specific
# mock happened" -- not merely a token appearing somewhere in the body, so
# it is checked first as the stronger signal when it applies. Still grounded
# against the obligation's own sink tokens (see `_asserts_mock_interaction`)
# before being accepted, the same discipline every other recognizer here
# uses: an explicit assertion of the WRONG interaction is not evidence for
# THIS obligation.
_MOCKITO_VERIFY_RE = re.compile(
    r"\bverify\s*\(\s*(?P<mock>[A-Za-z_]\w*)\s*"
    r"(?:,\s*(?:times|atLeast|atMost|atLeastOnce|only|never)\s*\([^)]*\)\s*)?\)"
    r"\s*\.\s*(?P<method>[A-Za-z_]\w*)\s*\("
)
_ARGUMENT_CAPTOR_RE = re.compile(r"\bArgumentCaptor\b")


def _normalize(path: str | None) -> str:
    """Normalize a route path for comparison across parameter syntaxes."""
    return (normalize_api_path(path) or "").lower()


def _route_match(case: LocatedTest, method: str | None, path: str | None) -> str | None:
    """Return the literal a test used to exercise this exact route, if any."""
    target = _normalize(path)
    if not target or target == "/":
        return None
    for literal in case.route_paths:
        if _normalize(literal) != target:
            continue
        if method and method != "ANY" and case.methods and method.upper() not in case.methods:
            continue
        return literal
    return None


def _route_prefix_mismatch(case: LocatedTest, flow_path: str | None) -> str | None:
    """Detect a likely missing route prefix, as a diagnostic only.

    `flow.path` sometimes carries only the method-decorator's own path
    segment (e.g. `/login`) while the route a test actually issues includes
    a global/controller prefix the flow object never recorded (e.g.
    `/api/v1/auth/email/login`). That is a route-composition gap upstream of
    this module, out of scope to fix here -- but silently mapping tests
    across it would be exactly the kind of filename/heuristic compensation
    this module exists to refuse. Instead, surface it as a distinct note so
    the mismatch is visible rather than either silently wrong (mapped) or
    silently invisible (dropped).
    """
    target = _normalize(flow_path)
    if not target or target == "/":
        return None
    for literal in case.route_paths:
        candidate = _normalize(literal)
        if candidate is None or candidate == target:
            continue
        # `target` always starts with "/" (guaranteed by `_normalize`, which
        # rejects "/" alone above), so an `endswith` match already lands on
        # a real path-segment boundary in `candidate` -- no separate
        # boundary check needed.
        if candidate.endswith(target):
            return (
                f"possible missing route prefix: flow path {flow_path!r} looks like a "
                f"suffix of {literal!r} used in {case.file}:{case.line} -- route "
                "composition was not fixed here, so this was not used to map any test"
            )
    return None


def _body_without_declared_name(case: LocatedTest) -> str:
    """The case body with its own declared name/description removed.

    A JS/TS case is commonly named after the very route it exercises, e.g.
    `it('should successfully login: /api/v1/auth/email/login (POST)', ...)`.
    That string is prose written by a human for a reader, not code -- but
    "login (POST)" still matches a naive `name\\s*\\(` invocation regex,
    making the test look like it *calls* a `login` symbol when it never
    does. The declared name is a known, fixed span (`case.name`, captured by
    the same regex that found this case), so it can be excluded outright
    rather than trying to teach the invocation check to see through prose.
    """
    if not case.name:
        return case.body
    return case.body.replace(case.name, "", 1)


def _invokes_symbol(case: LocatedTest, symbol: str | None) -> bool:
    """True when the test calls the given symbol as code, not merely names it.

    Requires an actual call site, not a mention: a name appearing in a
    comment, an import list, an unrelated attribute, or the test's own
    human-written description is not an invocation.
    """
    if not symbol:
        return False
    leaf = symbol.rsplit(".", 1)[-1]
    if not leaf or leaf.lower() in _NON_DISCRIMINATING:
        return False
    if leaf not in case.identifiers:
        return False
    body = _body_without_declared_name(case)
    # A real call site has the identifier immediately followed by `(` -- no
    # intervening space. Prose like "login (POST)" always has one; this is
    # the second, independent guard against exactly that shape, in case the
    # match happens to fall outside the declared-name span above (e.g. a
    # comment echoing the case name).
    return bool(re.search(rf"\b{re.escape(leaf)}\(", body))


def _has_assertion(case: LocatedTest) -> bool:
    """True when the test body contains a recognizable assertion."""
    return bool(_ASSERT_RE.search(case.body))


def _asserts_status(case: LocatedTest, codes: set[str]) -> str | None:
    """Return the asserted status code when it matches one the obligation names."""
    for match in _STATUS_RE.finditer(case.body):
        if match.group("code") in codes:
            return match.group("code")
    return None


def _asserts_status_class(case: LocatedTest, first_digit: str) -> str | None:
    """Return an asserted status code in the given class, e.g. any 4xx."""
    for match in _STATUS_RE.finditer(case.body):
        if match.group("code").startswith(first_digit):
            return match.group("code")
    return None


def _asserts_rejection(case: LocatedTest) -> bool:
    """True when the test asserts a thrown/rejected error directly (see
    `_REJECTION_ASSERTION_RE`) -- the non-status-code way a test can prove
    a validation obligation's "rejects this input" claim."""
    return bool(_REJECTION_ASSERTION_RE.search(case.body))


def _asserts_effect_token(case: LocatedTest, tokens: set[str]) -> str | None:
    """Return a sink-identifying token the test asserts on."""
    body = case.body.lower()
    for token in tokens:
        candidate = token.strip().lower()
        if len(candidate) < 4 or candidate in _NON_DISCRIMINATING:
            continue
        if candidate in body:
            return token
    return None


def _asserts_mock_interaction(case: LocatedTest, tokens: set[str]) -> tuple[str, str] | None:
    """Return `(mock, method)` for a Mockito `verify(mock).method(...)` call
    whose mock or method name matches one of this obligation's sink tokens.

    Grounded the same way `_asserts_effect_token` is grounded (a token this
    obligation's own sinks named, not any name anywhere): the difference is
    only in the shape of assertion recognized, not the standard of evidence.
    A test can `verify()` an entirely unrelated mock and still exercise this
    flow — that must not count as evidence for this obligation, so the
    sink-token match is still required, not optional.

    A sink's `name`/`target` is commonly the whole statement snippet the
    trace layer captured (e.g. `"redisUtil.delete(redisKeys);"`), not a bare
    identifier -- so this checks whether the short, clean `mock`/`method`
    name is a substring of that token (the same direction that succeeds for
    a token that already IS a bare identifier, since a string is always a
    substring of itself), not the other way around.
    """
    for match in _MOCKITO_VERIFY_RE.finditer(case.body):
        mock, method = match.group("mock"), match.group("method")
        for name in (mock, method):
            candidate = name.strip().lower()
            if len(candidate) < 4 or candidate in _NON_DISCRIMINATING:
                continue
            for token in tokens:
                if candidate in token.lower():
                    return mock, method
    return None


def case_changed_in_diff(case: LocatedTest, hunks: list[Hunk] | None) -> bool:
    """True only when a diff hunk actually falls inside *this* case's own
    line range -- not merely when its file was touched somewhere.

    A file-level check would call every case in a touched test file
    "changed", including cases that predate the diff by months. `hunks` and
    `case.line`/`end_line` share the same post-image line numbering (see
    `Hunk`, `git_change._parse_hunks`), so range overlap is a direct,
    generic test, not a per-repo heuristic.
    """
    if not hunks:
        return False
    case_end = case.end_line or case.line
    return any(case.line <= hunk.end_line and case_end >= hunk.start_line for hunk in hunks)


def _mapped(
    case: LocatedTest,
    *,
    rule: str,
    tier: str,
    source_refs: list[str],
    snippet: str | None,
    changed_in_diff: bool,
) -> MappedTest:
    """Build a mapped-test record carrying its own justification."""
    return MappedTest(
        id=f"{case.file}::{case.name}",
        name=case.display_name,
        case_name=case.name,
        repo=case.repo,
        file=case.file,
        line=case.line,
        suite=case.suite,
        match_rule=rule,
        evidence_tier=tier,
        source_refs=source_refs,
        changed_in_diff=changed_in_diff,
        evidence=[
            EvidenceRef(file=case.file, symbol=case.display_name, label=rule, snippet=snippet)
        ],
    )


def _status_codes_for(obligation: VerificationObligation) -> set[str]:
    """Status codes an obligation's statement refers to."""
    return set(re.findall(r"\b([45]\d\d|2\d\d)\b", obligation.statement))


def map_tests_to_obligation(
    *,
    obligation: VerificationObligation,
    flow: AffectedFlow,
    test_index: ExistingTestIndex,
    changed_symbol_names: set[str],
    changed_file_hunks: dict[str, list[Hunk]] | None = None,
) -> tuple[list[MappedTest], list[MappedTest], list[str]]:
    """Return (evidence, supporting, route_notes) tests for one obligation.

    `evidence` can determine the obligation's status. `supporting` exercises the
    same flow but does not demonstrate this specific claim — it is reported as
    regression context and can never satisfy the obligation. `route_notes` are
    diagnostic-only observations (e.g. a likely missing route prefix) that were
    deliberately never used to map or promote a test — see
    `_route_prefix_mismatch`.
    """
    evidence: list[MappedTest] = []
    supporting: list[MappedTest] = []
    route_notes: list[str] = []
    changed_file_hunks = changed_file_hunks or {}
    target_symbol = flow.handler
    codes = _status_codes_for(obligation)
    sink_tokens = {
        str(sink.get("name") or "") for sink in flow.sinks if sink.get("name")
    } | {str(sink.get("target") or "") for sink in flow.sinks if sink.get("target")}

    for case in test_index.cases:
        literal = _route_match(case, flow.method, flow.path)
        invokes = _invokes_symbol(case, target_symbol) or bool(
            changed_symbol_names and any(_invokes_symbol(case, name) for name in changed_symbol_names)
        )
        exercises = literal is not None or invokes
        if not exercises:
            mismatch = _route_prefix_mismatch(case, flow.path)
            if mismatch:
                route_notes.append(mismatch)
            continue

        rule_base = (
            f"issues {flow.method} {literal}" if literal else f"invokes {target_symbol}"
        )
        tier_base = TIER_DIRECT_ROUTE if literal else TIER_DIRECT_INVOCATION
        changed_in_diff = case_changed_in_diff(case, changed_file_hunks.get(case.file))

        # Exercising the flow is necessary but not sufficient; the test must
        # also assert the thing this obligation claims.
        if obligation.kind == OBLIGATION_VALIDATION:
            # A validation obligation claims the API *rejects* something. Only a
            # test asserting a rejection can demonstrate that — a passing
            # happy-path request proves the opposite case, not this one.
            asserted = (
                _asserts_status(case, codes)
                if codes
                else _asserts_status_class(case, "4")
            )
            if asserted and asserted.startswith("4"):
                evidence.append(
                    _mapped(
                        case,
                        rule=f"{rule_base} and asserts rejection status {asserted}",
                        tier=tier_base,
                        source_refs=obligation.source_refs,
                        snippet=literal or target_symbol,
                        changed_in_diff=changed_in_diff,
                    )
                )
                continue
            # No literal status code anywhere in the test body at all is
            # common at the service-method (not HTTP-route) level: a test
            # asserting `await expect(service.create(input)).rejects.
            # toMatchObject({httpCode, message})` proves the same rejection
            # claim just as directly, comparing against the real error's own
            # fields on a variable rather than a literal number `_STATUS_RE`
            # can match. Still requires this obligation to be validation-kind
            # AND the test to already exercise this flow (checked above) --
            # never promotes a test merely for calling the changed function.
            if _asserts_rejection(case):
                evidence.append(
                    _mapped(
                        case,
                        rule=f"{rule_base} and asserts a thrown/rejected error",
                        tier=tier_base,
                        source_refs=obligation.source_refs,
                        snippet=literal or target_symbol,
                        changed_in_diff=changed_in_diff,
                    )
                )
                continue

        elif obligation.kind == OBLIGATION_ROUTE_CONTRACT:
            # Only a status code this obligation's own statement actually
            # names can promote a test to evidence here. There used to be a
            # fallback that promoted *any* assertion-bearing, non-4xx test
            # when the obligation named no code at all -- that let a plain
            # successful-login test "verify" fabricated claims like "handles
            # downstream timeout safely" that it never exercises. An
            # obligation vague enough to name no code is, by the same
            # token, too vague to be verified by a generic assertion; it
            # stays supporting, below.
            asserted = _asserts_status(case, codes) if codes else None
            if asserted:
                evidence.append(
                    _mapped(
                        case,
                        rule=f"{rule_base} and asserts status {asserted}",
                        tier=tier_base,
                        source_refs=obligation.source_refs,
                        snippet=literal or target_symbol,
                        changed_in_diff=changed_in_diff,
                    )
                )
                continue

        elif obligation.kind in {
            OBLIGATION_SIDE_EFFECT,
            OBLIGATION_STATE_CONSISTENCY,
            OBLIGATION_EVENT_EMISSION,
            OBLIGATION_CROSS_REPO_CALL,
        }:
            mock_interaction = _asserts_mock_interaction(case, sink_tokens)
            if mock_interaction:
                mock, method = mock_interaction
                captor_note = (
                    " (with a captured argument)" if _ARGUMENT_CAPTOR_RE.search(case.body) else ""
                )
                evidence.append(
                    _mapped(
                        case,
                        rule=f"{rule_base} and verifies `{mock}.{method}(...)`{captor_note}",
                        tier=TIER_ASSERTED_EFFECT,
                        source_refs=obligation.source_refs,
                        snippet=f"verify({mock}).{method}(...)",
                        changed_in_diff=changed_in_diff,
                    )
                )
                continue
            token = _asserts_effect_token(case, sink_tokens)
            if token and _has_assertion(case):
                evidence.append(
                    _mapped(
                        case,
                        rule=f"{rule_base} and asserts on `{token}`",
                        tier=TIER_ASSERTED_EFFECT,
                        source_refs=obligation.source_refs,
                        snippet=token,
                        changed_in_diff=changed_in_diff,
                    )
                )
                continue
            if _COUNT_RE.search(case.body) and obligation.kind == OBLIGATION_EVENT_EMISSION:
                evidence.append(
                    _mapped(
                        case,
                        rule=f"{rule_base} and asserts a cardinality",
                        tier=TIER_ASSERTED_EFFECT,
                        source_refs=obligation.source_refs,
                        snippet="cardinality assertion",
                        changed_in_diff=changed_in_diff,
                    )
                )
                continue

        # Exercises the flow, but does not demonstrate this obligation.
        supporting.append(
            _mapped(
                case,
                rule=f"{rule_base}; does not assert this obligation",
                tier=TIER_DECLARED,
                source_refs=obligation.source_refs,
                snippet=literal or target_symbol,
                changed_in_diff=changed_in_diff,
            )
        )

    return evidence, supporting, sorted(set(route_notes))
