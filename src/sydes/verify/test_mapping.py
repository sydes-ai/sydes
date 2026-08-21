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
_STATUS_RE = re.compile(r"\b(?:status_code|statusCode|status)\b\s*(?:==|,|\)|\.toBe\()\s*(?P<code>[45]\d\d|2\d\d)")
_COUNT_RE = re.compile(r"\b(?:count|len|times|calledOnce|call_count|assert_called_once)\b", re.IGNORECASE)


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


def _invokes_symbol(case: LocatedTest, symbol: str | None) -> bool:
    """True when the test imports and calls the given symbol by name.

    Requires an actual call site, not a mention: a name appearing in a comment,
    an import list alone, or an unrelated attribute is not an invocation.
    """
    if not symbol:
        return False
    leaf = symbol.rsplit(".", 1)[-1]
    if not leaf or leaf.lower() in _NON_DISCRIMINATING:
        return False
    if leaf not in case.identifiers:
        return False
    return bool(re.search(rf"\b{re.escape(leaf)}\s*\(", case.body))


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


def _mapped(
    case: LocatedTest,
    *,
    rule: str,
    tier: str,
    source_refs: list[str],
    snippet: str | None,
    changed_files: set[str],
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
        changed_in_diff=case.file in changed_files,
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
    changed_files: set[str],
) -> tuple[list[MappedTest], list[MappedTest]]:
    """Return (evidence, supporting) tests for one obligation.

    `evidence` can determine the obligation's status. `supporting` exercises the
    same flow but does not demonstrate this specific claim — it is reported as
    regression context and can never satisfy the obligation.
    """
    evidence: list[MappedTest] = []
    supporting: list[MappedTest] = []
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
            continue

        rule_base = (
            f"issues {flow.method} {literal}" if literal else f"invokes {target_symbol}"
        )
        tier_base = TIER_DIRECT_ROUTE if literal else TIER_DIRECT_INVOCATION

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
                        changed_files=changed_files,
                    )
                )
                continue

        elif obligation.kind == OBLIGATION_ROUTE_CONTRACT:
            asserted = _asserts_status(case, codes) if codes else None
            if asserted:
                evidence.append(
                    _mapped(
                        case,
                        rule=f"{rule_base} and asserts status {asserted}",
                        tier=tier_base,
                        source_refs=obligation.source_refs,
                        snippet=literal or target_symbol,
                        changed_files=changed_files,
                    )
                )
                continue
            # Without a declared status, a success-path contract obligation is
            # demonstrated by a request that asserts a non-error outcome.
            if not codes and _has_assertion(case) and not _asserts_status_class(case, "4"):
                evidence.append(
                    _mapped(
                        case,
                        rule=f"{rule_base} and asserts on the response",
                        tier=tier_base,
                        source_refs=obligation.source_refs,
                        snippet=literal or target_symbol,
                        changed_files=changed_files,
                    )
                )
                continue

        elif obligation.kind in {
            OBLIGATION_SIDE_EFFECT,
            OBLIGATION_STATE_CONSISTENCY,
            OBLIGATION_EVENT_EMISSION,
            OBLIGATION_CROSS_REPO_CALL,
        }:
            token = _asserts_effect_token(case, sink_tokens)
            if token and _has_assertion(case):
                evidence.append(
                    _mapped(
                        case,
                        rule=f"{rule_base} and asserts on `{token}`",
                        tier=TIER_ASSERTED_EFFECT,
                        source_refs=obligation.source_refs,
                        snippet=token,
                        changed_files=changed_files,
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
                        changed_files=changed_files,
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
                changed_files=changed_files,
            )
        )

    return evidence, supporting
