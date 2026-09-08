"""Matching logic for resolving trace targets against discovered endpoints."""

from __future__ import annotations

from sydes.core.models import EndpointCandidate, TargetMatchResult


def _normalize_path(path: str | None) -> str | None:
    """Normalize endpoint paths for matching."""
    if path is None:
        return None
    normalized = path.strip()
    if not normalized:
        return None
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    if normalized != "/" and normalized.endswith("/"):
        normalized = normalized[:-1]
    return normalized


def _normalize_method(method: str | None) -> str | None:
    """Normalize HTTP method values for matching."""
    if method is None:
        return None
    normalized = method.strip().upper()
    return normalized or None


def resolve_trace_target(
    endpoints: list[EndpointCandidate],
    *,
    path: str,
    method: str | None = None,
    file: str | None = None,
) -> TargetMatchResult:
    """Resolve target path/method against discovered endpoint candidates.

    `file`, when given, disambiguates a target derived from an already-known
    concrete route (never from a bare user-typed method+path — those callers
    have nothing to pass here, and omitting it keeps their existing
    ambiguous-match behavior unchanged). A path shape as generic as
    `DELETE /{id}` recurs across unrelated services/crates/examples in one
    repository; a caller that already knows which file it means can avoid
    the same-name/same-path confusion this would otherwise produce.
    """
    target_path = _normalize_path(path)
    target_method = _normalize_method(method)
    notes: list[str] = []

    if target_path is None:
        return TargetMatchResult(notes=["Trace target path was empty."], confidence=0.0)

    path_matches = [
        endpoint
        for endpoint in endpoints
        if _normalize_path(endpoint.path) == target_path
    ]
    if not path_matches:
        return TargetMatchResult(
            selected=None,
            alternatives=[],
            notes=[f"No discovered endpoints matched path '{target_path}'."],
            confidence=0.0,
        )

    method_matches = path_matches
    if target_method is not None:
        method_matches = [
            endpoint
            for endpoint in path_matches
            if _normalize_method(endpoint.method) == target_method
        ]
        if not method_matches:
            notes.append(
                f"No endpoint matched method '{target_method}' for path '{target_path}'; "
                "falling back to path-only candidates."
            )
            method_matches = path_matches

    if file:
        # A known file scopes the candidate set before anything else is
        # tried: a single method+path match from a *different* file is not
        # a safe unambiguous answer, it is a same-shape route this caller
        # already has contradicting evidence about (an extremely common
        # situation for a generic REST shape like `DELETE /{id}`, reused
        # across unrelated services/crates/examples in one repository).
        file_matches = [endpoint for endpoint in method_matches if (endpoint.file or "") == file]
        if len(file_matches) == 1:
            selected = file_matches[0]
            confidence = selected.confidence if selected.confidence is not None else 0.85
            return TargetMatchResult(
                selected=selected,
                alternatives=[],
                notes=notes,
                confidence=confidence,
            )
        if not file_matches:
            # Every method+path candidate is in a file other than the one
            # this caller already knows the target lives in — declining
            # beats returning a confidently wrong same-shape match from
            # elsewhere in the repo.
            return TargetMatchResult(
                selected=None,
                alternatives=method_matches,
                notes=[
                    *notes,
                    f"Route method+path matched only outside the known file '{file}'; "
                    "not treated as the same route.",
                ],
                confidence=0.0,
            )
        method_matches = file_matches

    if len(method_matches) == 1:
        selected = method_matches[0]
        confidence = selected.confidence if selected.confidence is not None else 0.85
        return TargetMatchResult(
            selected=selected,
            alternatives=[],
            notes=notes,
            confidence=confidence,
        )

    repos = sorted({endpoint.repo for endpoint in method_matches})
    services = sorted(
        {
            endpoint.service
            for endpoint in method_matches
            if isinstance(endpoint.service, str) and endpoint.service
        }
    )
    notes.append("Multiple candidate endpoints matched target; selecting highest-confidence.")
    if len(repos) > 1:
        notes.append(f"Ambiguous match spans multiple repos: {', '.join(repos)}.")
    if len(services) > 1:
        notes.append(f"Ambiguous match spans multiple services: {', '.join(services)}.")
    ranked = sorted(
        method_matches,
        key=lambda endpoint: endpoint.confidence if endpoint.confidence is not None else 0.0,
        reverse=True,
    )
    selected = ranked[0]
    alternatives = ranked[1:]
    confidence = selected.confidence if selected.confidence is not None else 0.6
    return TargetMatchResult(
        selected=selected,
        alternatives=alternatives,
        notes=notes,
        confidence=confidence,
    )
