"""Derivation of verification obligations from existing Sydes evidence.

Obligations are read out of artifacts Sydes already produces — the API contract,
the deterministic test matrix, and the layered trace's steps and sinks — rather
than invented. Each one keeps `source_refs` pointing back at the artifact entry
it came from, so a reader can check the claim against its origin.

An LLM is not the primary generator here. Where a hypothesis is used at all it
is labelled `origin = llm_hypothesis` and never displaces a grounded obligation.
"""

from __future__ import annotations

import re
from typing import Any

from sydes.core.models import ApiRouteContract, EvidenceRef, TestMatrix
from sydes.verify.models import (
    OBLIGATION_CROSS_REPO_CALL,
    OBLIGATION_EVENT_EMISSION,
    OBLIGATION_ROUTE_CONTRACT,
    OBLIGATION_SIDE_EFFECT,
    OBLIGATION_STATE_CONSISTENCY,
    OBLIGATION_VALIDATION,
    ORIGIN_API_CONTRACT,
    ORIGIN_CROSS_REPO_LINK,
    ORIGIN_TEST_MATRIX,
    ORIGIN_TRACE_SINK,
    ORIGIN_TRACE_STEP,
    AffectedFlow,
    VerificationObligation,
)

# Test-matrix categories mapped onto obligation kinds. Only categories the
# matrix actually produces appear here.
_CATEGORY_KINDS = {
    "happy_path": OBLIGATION_ROUTE_CONTRACT,
    "positive": OBLIGATION_ROUTE_CONTRACT,
    "validation": OBLIGATION_VALIDATION,
    "side_effects": OBLIGATION_SIDE_EFFECT,
    "state_consistency": OBLIGATION_STATE_CONSISTENCY,
    "edge_cases": OBLIGATION_VALIDATION,
    "auth": OBLIGATION_VALIDATION,
    "error_handling": OBLIGATION_ROUTE_CONTRACT,
}

_VALIDATION_STEP_KINDS = {"validation_branch"}

# A rejection branch is only meaningful together with the status it produces:
# "rejects blank names with 400" and "rejects an empty name with 422" are
# different obligations, and a test for one does not demonstrate the other.
_STATUS_LITERAL_RE = re.compile(r"\b(?:HTTP_)?(?P<code>4\d\d)(?:_[A-Z_]+)?\b")
_WRITE_STEP_KINDS = {"database_write"}
_READ_STEP_KINDS = {"database_read", "database_query"}
_EXTERNAL_STEP_KINDS = {"external_call", "storage_call"}


def _identifier(flow_id: str, index: int) -> str:
    return f"{flow_id}::obligation-{index}"


def _changed_lines(changed_symbols: list[Any]) -> list[tuple[str, int, int]]:
    """Line spans of changed symbols, for attributing obligations to the diff."""
    spans: list[tuple[str, int, int]] = []
    for symbol in changed_symbols:
        if symbol.start_line and symbol.end_line:
            spans.append((symbol.file, symbol.start_line, symbol.end_line))
    return spans


def _touches_change(
    file: str | None, line: int | None, spans: list[tuple[str, int, int]]
) -> bool:
    """True when a source location falls inside a changed symbol."""
    if not file or line is None:
        return False
    return any(path == file and start <= line <= end for path, start, end in spans)


def _contract_obligations(
    flow: AffectedFlow, contract: ApiRouteContract | None, counter: list[int]
) -> list[VerificationObligation]:
    """One obligation per declared response status on the route contract."""
    if contract is None:
        return []
    obligations: list[VerificationObligation] = []
    for status, response in sorted(contract.responses.items(), key=lambda item: str(item[0])):
        code = str(status)
        description = (response.description or "").strip()
        kind = OBLIGATION_VALIDATION if code.startswith("4") else OBLIGATION_ROUTE_CONTRACT
        statement = (
            f"{flow.entry_label} responds {code}"
            + (f" — {description}" if description else "")
        )
        counter[0] += 1
        obligations.append(
            VerificationObligation(
                id=_identifier(flow.id, counter[0]),
                flow_id=flow.id,
                kind=kind,
                statement=statement,
                origin=ORIGIN_API_CONTRACT,
                source_refs=[f"api_contract:{flow.method} {flow.path}:{code}"],
                evidence=[
                    EvidenceRef(
                        file=contract.file or flow.artifact_refs.get("route_file", ""),
                        symbol=contract.handler,
                        label="api_contract_response",
                        snippet=f"{code} {description}".strip(),
                    )
                ],
            )
        )
    return obligations


def _matrix_obligations(
    flow: AffectedFlow, matrix: TestMatrix | None, counter: list[int]
) -> list[VerificationObligation]:
    """Obligations from the deterministic test matrix, preserving its refs."""
    if matrix is None:
        return []
    obligations: list[VerificationObligation] = []
    for group in matrix.groups:
        kind = _CATEGORY_KINDS.get(group.category.lower())
        if kind is None:
            continue
        for suggestion in group.tests:
            statement = (suggestion.purpose or suggestion.summary or suggestion.name).strip()
            if not statement:
                continue
            counter[0] += 1
            refs = [f"test_matrix:{group.category}:{suggestion.name}"]
            refs.extend(f"contract_ref:{item}" for item in suggestion.contract_refs)
            refs.extend(f"step:{item}" for item in suggestion.related_steps)
            refs.extend(f"sink:{item}" for item in suggestion.related_sinks)
            obligations.append(
                VerificationObligation(
                    id=_identifier(flow.id, counter[0]),
                    flow_id=flow.id,
                    kind=kind,
                    statement=statement,
                    origin=ORIGIN_TEST_MATRIX,
                    source_refs=refs,
                    evidence=[
                        EvidenceRef(
                            file=flow.artifact_refs.get("route_file", ""),
                            symbol=suggestion.name,
                            label="test_matrix_entry",
                            snippet=statement[:220],
                        )
                    ],
                )
            )
    return obligations


def _trace_obligations(
    flow: AffectedFlow, spans: list[tuple[str, int, int]], counter: list[int]
) -> list[VerificationObligation]:
    """Obligations for observable downstream effects the trace actually found."""
    obligations: list[VerificationObligation] = []

    for sink in flow.sinks:
        name = str(sink.get("name") or "").strip()
        kind_token = str(sink.get("kind") or "").lower()
        action = str(sink.get("action") or "").lower()
        if not name:
            continue
        if kind_token == "queue":
            kind, verb = OBLIGATION_EVENT_EMISSION, action or "published"
        elif kind_token == "external_api":
            kind, verb = OBLIGATION_CROSS_REPO_CALL, "called"
        else:
            kind = OBLIGATION_STATE_CONSISTENCY if action in {"write", "publish"} else OBLIGATION_SIDE_EFFECT
            verb = action or "accessed"
        counter[0] += 1
        obligations.append(
            VerificationObligation(
                id=_identifier(flow.id, counter[0]),
                flow_id=flow.id,
                kind=kind,
                statement=f"{flow.entry_label} {verb} {name}",
                origin=ORIGIN_TRACE_SINK,
                source_refs=[f"sink:{sink.get('id') or name}"],
                introduced_by_change=_touches_change(
                    sink.get("file"), sink.get("line"), spans
                ),
                evidence=[
                    EvidenceRef(
                        file=str(sink.get("file") or ""),
                        symbol=str(sink.get("symbol") or ""),
                        label="trace_sink",
                        snippet=str(sink.get("snippet") or name)[:220],
                    )
                ],
            )
        )

    # A validation branch inside a changed symbol is the clearest example of an
    # obligation the diff introduced, so it is attributed precisely.
    for position, step in enumerate(flow.steps):
        step_kind = str(step.get("kind") or "")
        if step_kind not in _VALIDATION_STEP_KINDS:
            continue
        detail = str(step.get("detail") or step.get("name") or "").strip()
        if not detail:
            continue
        line = step.get("line_start") or step.get("line")
        introduced = _touches_change(step.get("file"), line, spans)
        status_code = _rejection_status_after(flow.steps, position)
        statement = f"{flow.entry_label} enforces `{detail}`"
        if status_code:
            statement = f"{statement} and responds {status_code}"
        counter[0] += 1
        obligations.append(
            VerificationObligation(
                id=_identifier(flow.id, counter[0]),
                flow_id=flow.id,
                kind=OBLIGATION_VALIDATION,
                statement=statement,
                origin=ORIGIN_TRACE_STEP,
                source_refs=[f"step:{step.get('id') or detail}"],
                introduced_by_change=introduced,
                evidence=[
                    EvidenceRef(
                        file=str(step.get("file") or ""),
                        symbol=str(step.get("symbol") or ""),
                        label="trace_validation_branch",
                        snippet=detail[:220],
                    )
                ],
            )
        )

    for link in flow.cross_repo_links:
        label = str(link.get("target_label") or link.get("path") or "").strip()
        if not label:
            continue
        counter[0] += 1
        obligations.append(
            VerificationObligation(
                id=_identifier(flow.id, counter[0]),
                flow_id=flow.id,
                kind=OBLIGATION_CROSS_REPO_CALL,
                statement=f"{flow.entry_label} calls {label}",
                origin=ORIGIN_CROSS_REPO_LINK,
                source_refs=[f"cross_repo_link:{link.get('id') or label}"],
                evidence=[
                    EvidenceRef(
                        file=str(link.get("file") or ""),
                        label="cross_repo_link",
                        snippet=label[:220],
                    )
                ],
            )
        )
    return obligations


def _rejection_status_after(steps: list[dict[str, Any]], position: int) -> str | None:
    """Find the rejection status a validation branch produces, if it declares one."""
    for step in steps[position + 1 : position + 4]:
        text = " ".join(
            str(step.get(key) or "")
            for key in ("detail", "name", "snippet")
        )
        match = _STATUS_LITERAL_RE.search(text)
        if match:
            return match.group("code")
    return None


def _dedupe(obligations: list[VerificationObligation]) -> list[VerificationObligation]:
    """Collapse obligations that make the same claim, preferring grounded origins."""
    priority = {
        ORIGIN_API_CONTRACT: 0,
        ORIGIN_TRACE_SINK: 1,
        ORIGIN_TRACE_STEP: 1,
        ORIGIN_CROSS_REPO_LINK: 1,
        ORIGIN_TEST_MATRIX: 2,
    }
    best: dict[tuple[str, str], VerificationObligation] = {}
    for obligation in obligations:
        key = (obligation.kind, re.sub(r"\s+", " ", obligation.statement.lower()).strip())
        current = best.get(key)
        if current is None:
            best[key] = obligation
            continue
        if priority.get(obligation.origin, 9) < priority.get(current.origin, 9):
            obligation.source_refs = [*obligation.source_refs, *current.source_refs]
            obligation.introduced_by_change = (
                obligation.introduced_by_change or current.introduced_by_change
            )
            best[key] = obligation
        else:
            current.source_refs.extend(obligation.source_refs)
            current.introduced_by_change = (
                current.introduced_by_change or obligation.introduced_by_change
            )
    return list(best.values())


def derive_obligations(
    *,
    flow: AffectedFlow,
    route_contract: ApiRouteContract | None,
    test_matrix: TestMatrix | None,
    changed_symbols: list[Any],
) -> list[VerificationObligation]:
    """Derive every obligation for one affected flow from existing evidence."""
    counter = [0]
    spans = _changed_lines(changed_symbols)

    obligations = [
        *_contract_obligations(flow, route_contract, counter),
        *_matrix_obligations(flow, test_matrix, counter),
        *_trace_obligations(flow, spans, counter),
    ]
    obligations = _dedupe(obligations)

    # A flow whose handler the diff changed puts every obligation about that
    # flow in scope; obligations already attributed to a precise changed line
    # keep that stronger attribution.
    handler_changed = any(
        path == flow.artifact_refs.get("handler_file") for path, _start, _end in spans
    )
    for obligation in obligations:
        if obligation.origin == ORIGIN_TEST_MATRIX:
            # The test matrix was built to suggest broad coverage, not to state
            # precisely what this change must demonstrate. Its generic entries
            # ("rejects invalid payloads") match any unrelated 4xx test, so they
            # are advisory suggestions here: never required, and never presented
            # as change-critical.
            obligation.required = False
            obligation.introduced_by_change = False
            continue
        if handler_changed and obligation.kind in {
            OBLIGATION_ROUTE_CONTRACT,
            OBLIGATION_VALIDATION,
        }:
            obligation.introduced_by_change = True

    return sorted(obligations, key=lambda item: (not item.introduced_by_change, item.kind, item.statement))
