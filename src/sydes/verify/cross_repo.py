"""Cross-repository impact representation for change verification.

V1 keeps this deliberately small: outbound calls leaving the changed repository
are represented as first-class impacts whether or not a matching repository was
supplied, so nothing in the model assumes every affected component lives in the
current repo. When sibling repos are passed with additional `--repo` flags,
their route surfaces are matched against the outbound call.
"""

from __future__ import annotations

import re

from sydes.core.models import EvidenceRef
from sydes.trace.cross_repo import normalize_api_path
from sydes.verify.models import (
    LINK_RESOLVED,
    LINK_UNRESOLVED,
    AffectedFlow,
    CrossRepoImpact,
)
from sydes.verify.surface import SystemSurface

_PATH_IN_LABEL = re.compile(r"(?P<path>/[A-Za-z0-9_{}:./\-]*)$")


def _route_index(surfaces: dict[str, SystemSurface]) -> dict[str, list[tuple[str, str]]]:
    """Index normalized route path -> [(repo, label)] across all known surfaces."""
    index: dict[str, list[tuple[str, str]]] = {}
    for repo, surface in surfaces.items():
        for binding in surface.routes:
            normalized = normalize_api_path(binding.endpoint.path)
            if not normalized:
                continue
            index.setdefault(normalized, []).append((repo, binding.label))
    return index


def detect_cross_repo_impacts(
    *,
    origin_repo: str,
    flows: list[AffectedFlow],
    surfaces: dict[str, SystemSurface],
) -> list[CrossRepoImpact]:
    """Represent outbound boundaries in affected flows as cross-repo impacts."""
    impacts: dict[str, CrossRepoImpact] = {}
    route_index = _route_index(surfaces)

    for flow in flows:
        for node in flow.nodes:
            if node.kind not in {"client", "external"}:
                continue
            label = node.name
            match = _PATH_IN_LABEL.search(label)
            normalized = normalize_api_path(match.group("path")) if match else None

            target_repo: str | None = None
            target_label = label
            status = LINK_UNRESOLVED
            reason = "outbound boundary reached by the affected flow; owning repository not configured"

            if normalized:
                for repo, route_label in route_index.get(normalized, []):
                    if repo == origin_repo:
                        continue
                    target_repo = repo
                    target_label = f"{repo}::{route_label}"
                    status = LINK_RESOLVED
                    reason = f"outbound call path matches route `{route_label}` in repo `{repo}`"
                    break

            impact_id = f"cross-repo:{target_repo or 'unknown'}:{target_label}"
            if impact_id in impacts:
                if flow.id not in impacts[impact_id].related_flow_ids:
                    impacts[impact_id].related_flow_ids.append(flow.id)
                continue

            impacts[impact_id] = CrossRepoImpact(
                id=impact_id,
                target_repo=target_repo,
                target_label=target_label,
                kind="http_call",
                status=status,
                reason=reason,
                related_flow_ids=[flow.id],
                evidence=[
                    EvidenceRef(file=node.file or "", symbol=node.name, label="outbound_call")
                ],
            )

        for node in flow.nodes:
            if node.kind != "consumer" or node.repo == origin_repo:
                continue
            impact_id = f"cross-repo:{node.repo}:{node.name}"
            if impact_id in impacts:
                continue
            impacts[impact_id] = CrossRepoImpact(
                id=impact_id,
                target_repo=node.repo,
                target_label=node.name,
                kind="event_consumer",
                status=LINK_RESOLVED,
                reason="consumer subscribed to an event published by the affected flow",
                related_flow_ids=[flow.id],
                evidence=[EvidenceRef(file=node.file or "", symbol=node.name, label="event_consumer")],
            )

    return sorted(impacts.values(), key=lambda item: (item.target_repo or "~", item.target_label))
