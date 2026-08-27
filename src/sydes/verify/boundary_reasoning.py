"""Evidence-backed boundary inference — Increment D.

Increments C/C.1/C.2 built a sound deterministic path: a boundary is
ESTABLISHED only when a real structural edge reaches a node whose
normalized evidence qualifies it. That path's *precision* is good and this
module does not touch it. Its *recall* is limited by what current
extraction encodes — and the deliberate decision is not to fix that by
adding framework parsers.

So this module adds a second, epistemically separate path: when
deterministic evidence cannot ESTABLISH a boundary, one bounded LLM call
reasons over a compact packet of real evidence and may propose INFERRED
boundaries.

    deterministic strong evidence  ->  ESTABLISHED boundary
    partial/ambiguous evidence     ->  this module  ->  INFERRED boundary

The separation is the point, and it is structural rather than advisory:

- Both states live in one `AffectedBoundary` list, distinguished by
  `status`. There is no separate `llm_boundaries[]` product concept.
- An inferred boundary cannot become proof. It never creates an
  `AffectedFlow`, a `VerificationObligation`, or an `AcceptedImpact`, never
  satisfies an unresolved changed symbol, and `affected_boundaries` is not
  an input to `_compute_summary` — so it cannot move a verdict toward
  VERIFIED. Nothing in this module writes to any of those.
- Deterministic precedence: a boundary already ESTABLISHED is never
  duplicated as INFERRED (see `_deterministic_keys`).

This is *not* another PR summary. Increment A (`pr_semantic_analysis`)
already answers "what does this change appear to do"; this answers "given
that reading plus real structural evidence, which architectural boundary is
likely affected" — and it must cite the supplied facts that support each
answer.

Reuses, rather than duplicating: the provider-neutral `LLMClient` factory
and its `stage=` tracing, `pr_semantic_analysis`, `ImpactResult` (its
boundaries, accepted entrypoints, unresolved symbols and bounded
`boundary_decisions` log), the C.1 production-candidate exclusion rules via
`is_production_boundary_candidate`, `source_preview` for bounded snippets,
and `AffectedBoundary` as the single product-facing model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sydes.code_intelligence.base import StructuralFacts
from sydes.impact.boundary_discovery import is_production_boundary_candidate
from sydes.impact.investigate import source_preview
from sydes.impact.models import (
    BOUNDARY_API,
    BOUNDARY_ASYNC,
    BOUNDARY_CALLABLE,
    BOUNDARY_EXTERNAL,
    BOUNDARY_UNKNOWN,
    IMPACT_STATUS_INFERRED,
    ImpactResult,
    SymbolIdentity,
)
from sydes.llm.client import (
    LLMClient,
    LLMClientError,
    LLMRequest,
    create_default_llm_client,
)
from sydes.verify.llm_findings import _extract_json_object
from sydes.verify.models import AffectedBoundary, ChangeSet

#: Packet budget. Small on purpose: this is the highest-value evidence, not
#: a graph dump. Every list below is truncated to these caps before the
#: packet is serialized.
MAX_CANDIDATES = 12
MAX_CHANGED_SYMBOLS = 20
MAX_DETERMINISTIC_BOUNDARIES = 10
MAX_ACCEPTED_IMPACTS = 12
MAX_SNIPPETS = 6
MAX_SNIPPET_CHARS = 600
MAX_BEHAVIOR_CHANGES = 6
MAX_PROMPT_CHARS = 20_000
#: At most this many inferred boundaries survive parsing — "prefer a small
#: number of high-value boundaries over broad speculation", enforced.
MAX_INFERRED_BOUNDARIES = 6

_ALLOWED_KINDS = frozenset({
    BOUNDARY_API, BOUNDARY_CALLABLE, BOUNDARY_ASYNC, BOUNDARY_EXTERNAL, BOUNDARY_UNKNOWN,
})


def _identity_of(repo: str, file: str, symbol: str, qualified: str = "") -> SymbolIdentity:
    return SymbolIdentity.from_fields(
        repo=repo, file=file, qualified_name=qualified or None, short_name=symbol,
    )


def _deterministic_keys(boundaries: list[AffectedBoundary]) -> set[tuple[str, str]]:
    """Keys identifying boundaries deterministic discovery already
    ESTABLISHED. An inferred boundary matching one of these is dropped:
    deterministic proof always wins, and showing the same boundary twice in
    two epistemic states would be actively misleading."""
    keys: set[tuple[str, str]] = set()
    for item in boundaries:
        if item.status != IMPACT_STATUS_INFERRED:
            keys.add((item.kind, (item.symbol or item.label or "").lower()))
    return keys


def _candidate_records(
    *,
    impact_result: ImpactResult,
    facts: StructuralFacts,
    repo: str,
) -> list[dict[str, Any]]:
    """The small production-candidate set the model may reason over.

    Drawn from evidence Sydes already computed — accepted entrypoints, the
    bounded boundary-decision log (nodes the ranked frontier actually
    considered), and unresolved changed symbols — never from a fresh graph
    walk. Tests and executable entrypoints are excluded here by exactly the
    C.1 rule (`is_production_boundary_candidate`), so legacy accepted-impact
    noise cannot contaminate an inferred boundary.
    """
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def _add(symbol: str, file: str, origin: str, detail: str = "") -> None:
        if not symbol:
            return
        key = (file, symbol)
        if key in seen:
            return
        identity = _identity_of(repo, file, symbol)
        if not is_production_boundary_candidate(identity, facts):
            return
        seen.add(key)
        records.append({
            "symbol": symbol, "file": file, "origin": origin, "detail": detail,
        })

    for entry in impact_result.affected:
        route = (
            f"{entry.route_method} {entry.route_path}"
            if entry.route_method and entry.route_path else ""
        )
        _add(entry.symbol, entry.file, f"reached_entrypoint:{entry.kind}", route)
    for decision in impact_result.boundary_decisions:
        candidate = str(decision.get("candidate") or "")
        _add(
            candidate.rsplit(".", 1)[-1], str(decision.get("file") or ""),
            f"frontier_candidate:{decision.get('decision')}",
            str(decision.get("reason") or ""),
        )
    for unresolved in impact_result.unresolved:
        _add(unresolved.symbol, "", "unresolved_changed_symbol", unresolved.reason)

    return records[:MAX_CANDIDATES]


def _snippets_for(
    candidates: list[dict[str, Any]], *, facts: StructuralFacts, repo: str,
    repo_root: Path | None,
) -> list[dict[str, str]]:
    """Bounded source previews for the top candidates, via the same
    `source_preview` the impact guide already uses — never a file dump."""
    if repo_root is None:
        return []
    snippets: list[dict[str, str]] = []
    for record in candidates[:MAX_SNIPPETS]:
        identity = _identity_of(repo, record["file"], record["symbol"])
        preview = source_preview(identity, facts, repo_root)
        if preview:
            snippets.append({
                "symbol": record["symbol"],
                "file": record["file"],
                "source": preview[:MAX_SNIPPET_CHARS],
            })
    return snippets


def build_reasoning_packet(
    *,
    change: ChangeSet,
    impact_result: ImpactResult,
    deterministic_boundaries: list[AffectedBoundary],
    semantic_analysis: Any | None,
    facts: StructuralFacts,
    repo: str,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Assemble the compact evidence packet. Deterministic and bounded —
    no LLM call, no CBM call, no graph walk of its own."""
    candidates = _candidate_records(
        impact_result=impact_result, facts=facts, repo=repo,
    )
    behavior_changes: list[str] = []
    uncertainties: list[str] = []
    change_summary = ""
    if semantic_analysis is not None:
        change_summary = semantic_analysis.change_summary
        behavior_changes = [
            item.description for item in semantic_analysis.behavior_changes
        ][:MAX_BEHAVIOR_CHANGES]
        uncertainties = list(semantic_analysis.uncertainties)[:MAX_BEHAVIOR_CHANGES]

    return {
        "version": "v1",
        "change_summary": change_summary,
        "behavior_changes": behavior_changes,
        "changed_symbols": [
            {"symbol": item.qualified_name or item.name, "file": item.file, "kind": item.kind}
            for item in change.symbols[:MAX_CHANGED_SYMBOLS]
        ],
        # Already ESTABLISHED — supplied so the model does not re-propose
        # what deterministic analysis already proved.
        "deterministic_boundaries": [
            {"kind": item.kind, "subtype": item.subtype, "label": item.label}
            for item in deterministic_boundaries[:MAX_DETERMINISTIC_BOUNDARIES]
        ],
        "boundary_candidates": candidates,
        "accepted_impacts": [
            {"label": item.label, "status": item.status}
            for item in impact_result.affected[:MAX_ACCEPTED_IMPACTS]
        ],
        "unresolved_changed_symbols": [
            item.symbol for item in impact_result.unresolved[:MAX_CHANGED_SYMBOLS]
        ],
        "relevant_source_snippets": _snippets_for(
            candidates, facts=facts, repo=repo, repo_root=repo_root,
        ),
        "uncertainties": uncertainties,
    }


def has_reasonable_evidence(packet: dict[str, Any]) -> bool:
    """Whether the packet is worth an LLM call at all.

    Increment D is evidence-*backed*: a semantic hypothesis with no
    structural or source candidate behind it must never produce a boundary,
    so with no candidates there is nothing to reason over and the call is
    skipped entirely rather than invited to speculate.
    """
    return bool(packet.get("boundary_candidates"))


_SYSTEM_PROMPT = """You infer likely affected software BOUNDARIES for a code change, from evidence that has already been gathered for you. One language- and framework-neutral task; you are never given a repository to browse.

You are given: a semantic interpretation of the PR, real structural facts, a small set of candidate symbols, and partial architectural evidence that deterministic analysis could NOT fully establish.

Your job: infer which architectural boundaries are likely affected, and say exactly which supplied facts support each one.

Boundary kinds (use only these):
- api       — a request/routing surface (HTTP handler, route registration, RPC entry)
- callable  — a public/service/domain surface other code calls
- async     — an event/signal handler, scheduled job, or queue consumer

Rules — these are absolute:
1. NEVER invent a structural edge, caller, or relationship that was not supplied.
2. NEVER claim a concrete route method/path (e.g. "GET /orders/{id}") unless that exact route evidence appears in the supplied facts. You may describe a routing surface in words without inventing its path.
3. NEVER treat test-only code as a production boundary. Test code may support an inference; it is never itself the boundary.
4. Distinguish clearly between what the supplied evidence supports, what is uncertain, and what is simply missing. Put the latter two in `uncertainty`.
5. If the evidence is insufficient, return an EMPTY list. That is a correct, preferred answer — not a failure.
6. Prefer a small number of high-value boundaries over broad speculation.
7. `supporting_evidence` must quote or name the specific supplied facts (a candidate symbol, a source snippet, a structural fact) behind the inference. An inference you cannot ground this way should not be returned.
8. Do NOT re-propose a boundary already listed in `deterministic_boundaries` — those are already proven.
9. Do not infer `async` merely because a symbol is decorated; the supplied evidence must actually show event/scheduler/queue semantics. Likewise do not infer `api` merely because a symbol looks web-ish.

Do not summarize the PR — that is already done and supplied to you. Answer only the boundary question.

`subtype` is optional and free-form but should be short and useful for reporting (e.g. http, route_registration, public_callable, service, domain, event_handler, scheduled_job). Omit it or use null when unclear.

`confidence` is your own bounded self-assessment in [0,1], not a calibrated probability.

Return strict JSON only, exactly this shape:
{"inferred_boundaries":[{"kind":"api|callable|async","subtype":"...","label":"short reviewer-facing behavior name","symbol":"...","file":"...","changed_symbols":["..."],"reason":"one sentence","supporting_evidence":["..."],"uncertainty":"...","confidence":0.0}]}"""


def _parse_boundary(raw: Any, *, repo: str, position: int) -> AffectedBoundary | None:
    """Parse one inferred boundary conservatively. A malformed entry is
    dropped rather than coerced — an inference Sydes cannot read is not an
    inference it should report."""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in _ALLOWED_KINDS:
        return None
    label = str(raw.get("label") or "").strip()
    if not label:
        return None
    reason = str(raw.get("reason") or "").strip()
    if not reason:
        # Rule 7 in practice: an inference with no stated basis is a guess.
        return None
    symbol = str(raw.get("symbol") or "").strip()
    file = str(raw.get("file") or "").strip()
    subtype = str(raw.get("subtype") or "").strip() or None
    confidence = raw.get("confidence")
    confidence_value = (
        max(0.0, min(1.0, float(confidence)))
        if isinstance(confidence, int | float) else None
    )
    evidence = [
        str(item).strip()[:300]
        for item in (raw.get("supporting_evidence") or [])
        if isinstance(item, str) and item.strip()
    ][:6]
    changed_symbols = [
        str(item).strip()
        for item in (raw.get("changed_symbols") or [])
        if isinstance(item, str) and item.strip()
    ][:6]
    return AffectedBoundary(
        id=f"boundary:inferred:{kind}:{repo}:{symbol or label}:{position}",
        kind=kind,
        subtype=subtype,
        repo=repo,
        file=file or None,
        symbol=symbol or None,
        label=label[:200],
        changed_symbols=changed_symbols,
        evidence=evidence,
        distance=0,
        evidence_strength="medium",
        status=IMPACT_STATUS_INFERRED,
        reason=reason[:400],
        uncertainty=str(raw.get("uncertainty") or "").strip()[:300] or None,
        llm_confidence=confidence_value,
    )


def parse_inferred_boundaries(
    raw: dict[str, Any], *, repo: str, deterministic: list[AffectedBoundary],
) -> list[AffectedBoundary]:
    """Parse, validate and de-duplicate one boundary-reasoning response."""
    established = _deterministic_keys(deterministic)
    out: list[AffectedBoundary] = []
    seen: set[tuple[str, str]] = set()
    for position, item in enumerate(raw.get("inferred_boundaries") or []):
        boundary = _parse_boundary(item, repo=repo, position=position)
        if boundary is None:
            continue
        key = (boundary.kind, (boundary.symbol or boundary.label or "").lower())
        if key in established:
            continue  # deterministic proof wins; never duplicate it as inferred
        if key in seen:
            continue
        seen.add(key)
        out.append(boundary)
        if len(out) >= MAX_INFERRED_BOUNDARIES:
            break
    return out


def infer_boundaries(
    *,
    change: ChangeSet,
    impact_result: ImpactResult,
    deterministic_boundaries: list[AffectedBoundary],
    semantic_analysis: Any | None,
    facts: StructuralFacts,
    repo: str,
    repo_root: Path | None = None,
    model_spec: str | None = None,
    llm_client: LLMClient | None = None,
) -> tuple[list[AffectedBoundary], list[str]]:
    """Run the one bounded boundary-reasoning call for the whole change.

    Returns `(inferred_boundaries, notes)`. Exactly one LLM call per change
    — never one per symbol or per boundary, no retry, no agent loop, and no
    call at all when the packet carries no candidate to reason over.

    Never raises: a provider failure or unparseable response yields
    `([], notes)` so the deterministic result stays valid and the run
    continues, with the reason visible in diagnostics. There is no fallback
    boundary — an inference Sydes could not obtain is simply absent.
    """
    packet = build_reasoning_packet(
        change=change, impact_result=impact_result,
        deterministic_boundaries=deterministic_boundaries,
        semantic_analysis=semantic_analysis, facts=facts, repo=repo,
        repo_root=repo_root,
    )
    if not has_reasonable_evidence(packet):
        return [], ["boundary_reasoning skipped: no production boundary candidate to reason over"]

    client = llm_client
    if client is None:
        try:
            client = create_default_llm_client(
                model_spec=model_spec, temperature=None, stage="boundary_reasoning",
            )
        except LLMClientError as exc:
            return [], [f"boundary_reasoning unavailable: {exc}"]

    prompt = ("Evidence:\n" + json.dumps(packet, ensure_ascii=True, separators=(",", ":")))[
        :MAX_PROMPT_CHARS
    ]
    try:
        response = client.generate(
            LLMRequest(prompt=prompt, system=_SYSTEM_PROMPT, temperature=None)
        )
    except LLMClientError as exc:
        return [], [f"boundary_reasoning unavailable: {exc}"]

    raw = _extract_json_object(response.text)
    if raw is None:
        return [], ["boundary_reasoning unavailable: model output was not valid JSON."]

    boundaries = parse_inferred_boundaries(
        raw, repo=repo, deterministic=deterministic_boundaries,
    )
    return boundaries, [
        f"boundary_reasoning: candidates={len(packet['boundary_candidates'])} "
        f"inferred={len(boundaries)}"
    ]
