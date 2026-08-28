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
    BOUNDARY_SUBTYPE_DOMAIN,
    BOUNDARY_SUBTYPE_EVENT_HANDLER,
    BOUNDARY_SUBTYPE_EXTERNAL_SERVICE,
    BOUNDARY_SUBTYPE_HTTP,
    BOUNDARY_SUBTYPE_PERSISTENCE,
    BOUNDARY_SUBTYPE_PUBLIC_CALLABLE,
    BOUNDARY_SUBTYPE_QUEUE_CONSUMER,
    BOUNDARY_SUBTYPE_ROUTE_REGISTRATION,
    BOUNDARY_SUBTYPE_SCHEDULED_JOB,
    BOUNDARY_SUBTYPE_SERVICE,
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
#: Increment B: a few repository architecture facts, not a profile dump.
MAX_REPO_CONTEXT_FACTS = 6

_ALLOWED_KINDS = frozenset({
    BOUNDARY_API, BOUNDARY_CALLABLE, BOUNDARY_ASYNC, BOUNDARY_EXTERNAL, BOUNDARY_UNKNOWN,
})

#: Increment D.1: the ONLY kind/subtype pairs this stage will report. A
#: model-generated subtype outside this vocabulary is never surfaced
#: verbatim — see `_normalize_subtype`. `unknown` intentionally has no
#: subtype at all: a boundary Sydes cannot even name a kind for certainly
#: has nothing meaningful to say about its subtype either.
SUBTYPES_BY_KIND: dict[str, frozenset[str]] = {
    BOUNDARY_API: frozenset({BOUNDARY_SUBTYPE_HTTP, BOUNDARY_SUBTYPE_ROUTE_REGISTRATION}),
    BOUNDARY_CALLABLE: frozenset({
        BOUNDARY_SUBTYPE_SERVICE, BOUNDARY_SUBTYPE_DOMAIN, BOUNDARY_SUBTYPE_PUBLIC_CALLABLE,
    }),
    BOUNDARY_ASYNC: frozenset({
        BOUNDARY_SUBTYPE_EVENT_HANDLER, BOUNDARY_SUBTYPE_SCHEDULED_JOB, BOUNDARY_SUBTYPE_QUEUE_CONSUMER,
    }),
    BOUNDARY_EXTERNAL: frozenset({BOUNDARY_SUBTYPE_EXTERNAL_SERVICE, BOUNDARY_SUBTYPE_PERSISTENCE}),
    BOUNDARY_UNKNOWN: frozenset(),
}


def _normalize_subtype(kind: str, subtype: str | None) -> str | None:
    """The one centralized kind/subtype validator — every inferred boundary
    passes through here, rather than scattered ad hoc checks.

    A subtype outside the fixed vocabulary for its kind (whether wholly
    invented, like `http_handler_ui`, or borrowed from a different kind's
    vocabulary, like `kind=api, subtype=service`) is normalized to `None`
    rather than rejecting the whole boundary: the kind itself may still be
    correct even when the model's subtype word choice was not. This is the
    conservative, existing-model-compatible behavior the task calls for —
    stable subtype reporting without discarding an otherwise-valid kind.
    """
    if not subtype:
        return None
    return subtype if subtype in SUBTYPES_BY_KIND.get(kind, frozenset()) else None


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


def _repo_context(
    repo_profile: Any | None,
    *,
    change: ChangeSet,
    candidates: list[dict[str, Any]],
    semantic_analysis: Any | None,
) -> list[str]:
    """A handful of repository architecture facts relevant to *this* change.

    Increment B: `repo_profile` is retrieval memory, never permanent prompt
    context — the full profile is never injected. Only
    `RepoProfile.lookup()`'s small, capped, deterministic result reaches the
    prompt, keyed on the files and symbols actually in play here.
    """
    if repo_profile is None:
        return []
    concepts: list[str] = []
    if semantic_analysis is not None:
        concepts = [item.description for item in semantic_analysis.behavior_changes]
        for hint in semantic_analysis.investigation_hints:
            concepts.extend(hint.concepts)
    files = [item.file for item in change.symbols if item.file]
    files.extend(record["file"] for record in candidates if record.get("file"))
    symbols = [item.name for item in change.symbols]
    symbols.extend(record["symbol"] for record in candidates)
    try:
        facts_found = repo_profile.lookup(
            files=files, symbols=symbols, concepts=concepts, limit=MAX_REPO_CONTEXT_FACTS,
        )
    except Exception:  # noqa: BLE001 - repo context is never load-bearing
        return []
    return [fact.describe() for fact in facts_found]


def build_reasoning_packet(
    *,
    change: ChangeSet,
    impact_result: ImpactResult,
    deterministic_boundaries: list[AffectedBoundary],
    semantic_analysis: Any | None,
    facts: StructuralFacts,
    repo: str,
    repo_root: Path | None = None,
    repo_profile: Any | None = None,
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
        # A few retrieved repository architecture facts — never the whole
        # profile. Context for interpreting candidates (is this package
        # backend or frontend-only? a publishable library surface?), never
        # itself evidence of a structural relationship.
        "repo_context": _repo_context(
            repo_profile, change=change, candidates=candidates,
            semantic_analysis=semantic_analysis,
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

THE CENTRAL QUESTION — ask it before you propose anything: "What crosses this boundary?" A boundary is a meaningful architectural CUT where some interaction crosses from one component/layer/system surface into another. An affected behavior is not automatically a boundary just because it matters.

Valid — something identifiable is crossing:
- api:      an HTTP request crosses into a route/handler ("kind=api, subtype=http"), or a symbol registers a route that requests will cross into later ("kind=api, subtype=route_registration").
- callable: a caller/component crosses into a service/domain/public callable ("kind=callable, subtype=service" or "domain" or "public_callable").
- async:    an event/message/scheduler crosses into a handler/callback ("kind=async, subtype=event_handler", "scheduled_job", or "queue_consumer").

NOT a boundary — nothing is shown crossing into or out of anything else:
- "template renders new form fields" — a real, possibly important affected behavior, but no request/API/service boundary is shown being crossed. Return no boundary for this.
- UI rendering changes may be important but are NOT automatically `api` boundaries.
- Form validation may be behaviorally important but is NOT automatically a `callable` boundary unless the evidence shows it is a meaningful callable/service/domain surface (not just "a function got called").
- Configuration changes are NOT automatically boundaries.
- Database/model FIELD changes are NOT automatically `callable` boundaries.
- File or directory role ALONE (including `repo_context`) is NEVER enough — see rule 10.
- Test changes are supporting evidence only, never themselves a production boundary.
When in doubt, return no boundary. An empty result is the correct, preferred answer whenever the evidence does not establish a real architectural cut — not a failure of this task.

Rules — these are absolute:
1. NEVER invent a structural edge, caller, or relationship that was not supplied.
2. NEVER claim a concrete route method/path (e.g. "GET /orders/{id}") unless that exact route evidence appears in the supplied facts. You may describe a routing surface in words without inventing its path.
3. NEVER treat test-only code as a production boundary. Test code may support an inference; it is never itself the boundary.
4. Distinguish clearly between what the supplied evidence supports, what is uncertain, and what is simply missing. Put the latter two in `uncertainty`.
5. If the evidence is insufficient, return an EMPTY list. That is a correct, preferred answer — not a failure.
6. Prefer a small number of high-value boundaries over broad speculation. If several changed symbols clearly represent ONE architectural surface, group them under one boundary and list them all in `changed_symbols` rather than emitting one boundary per symbol.
7. `supporting_evidence` must quote or name a SPECIFIC, CONCRETE supplied production fact — a candidate symbol, a source snippet, an accepted impact, a structural relation. A vague restatement of the change summary ("the change affects the UI") is not evidence. `repo_context` is never sufficient evidence on its own (see rule 10). An inference you cannot ground this way should not be returned.
8. Do NOT re-propose a boundary already listed in `deterministic_boundaries` — those are already proven.
9. Do not infer `async` merely because a symbol is decorated; the supplied evidence must actually show event/scheduler/queue semantics. Likewise do not infer `api` merely because a symbol looks web-ish, and do not infer `callable` merely because something is exported or generically "used".
10. `repo_context` describes the repository's architecture (which package is backend or frontend-only, which is a publishable library, which directories are internal). Use it ONLY to INTERPRET candidates you already have concrete evidence for — e.g. do not propose a backend boundary inside a frontend-only package. `repo_context` PLUS the PR's semantic summary, with no concrete candidate/source/structural fact behind it, is NEVER enough to emit a boundary.

Do not summarize the PR — that is already done and supplied to you. Answer only the boundary question.

`subtype` MUST be one of this fixed vocabulary for the chosen `kind` (use `null` if none fits — do not invent a new word):
- api:      http | route_registration
- callable: service | domain | public_callable
- async:    event_handler | scheduled_job | queue_consumer

`confidence` is your own bounded self-assessment in [0,1], not a calibrated probability.

Return strict JSON only, exactly this shape:
{"inferred_boundaries":[{"kind":"api|callable|async","subtype":"...","label":"short reviewer-facing behavior name","symbol":"...","file":"...","changed_symbols":["..."],"reason":"one sentence","supporting_evidence":["..."],"uncertainty":"...","confidence":0.0}]}"""


def _grounded_evidence_tokens(packet: dict[str, Any]) -> frozenset[str]:
    """Tokens for genuine PRODUCTION facts the packet supplied — candidate
    symbols/files, accepted-impact labels, unresolved symbols, changed
    symbols, source-snippet symbols. Deliberately EXCLUDES `repo_context`:
    a repository-architecture fact ("packages/core is backend") is context
    for interpreting a candidate, never itself the concrete production
    evidence an inferred boundary must cite (see `_is_evidence_grounded`).
    """
    tokens: set[str] = set()

    def _add(text: str | None) -> None:
        if text:
            tokens.add(text.lower())

    for candidate in packet.get("boundary_candidates", []):
        _add(candidate.get("symbol"))
        _add(candidate.get("file"))
    for impact in packet.get("accepted_impacts", []):
        _add(impact.get("label"))
    for symbol in packet.get("unresolved_changed_symbols", []):
        _add(symbol)
    for changed in packet.get("changed_symbols", []):
        _add(changed.get("symbol"))
        _add(changed.get("file"))
    for snippet in packet.get("relevant_source_snippets", []):
        _add(snippet.get("symbol"))
        _add(snippet.get("file"))
    return frozenset(token for token in tokens if len(token) > 2)


def _is_evidence_grounded(
    *, symbol: str, supporting_evidence: list[str], grounded_tokens: frozenset[str],
) -> bool:
    """Whether at least one concrete PRODUCTION fact backs this boundary —
    not merely `repo_context`, not merely the PR's semantic summary.

    The `symbol` field matching a real candidate is sufficient on its own.
    Otherwise, at least one `supporting_evidence` line must reference a real
    supplied fact (a candidate symbol or file name) — citing only a
    `repo_context` fact ("packages/core is backend") or vague prose
    ("the change affects the UI") never counts, because neither is
    concrete evidence that a boundary is actually crossed here.
    """
    if symbol and symbol.lower() in grounded_tokens:
        return True
    for line in supporting_evidence:
        low = line.lower()
        if any(token in low for token in grounded_tokens):
            return True
    return False


def _known_symbol_files(packet: dict[str, Any]) -> dict[str, str]:
    """symbol name (lowercased) -> file, from the exact same packet facts
    `_grounded_evidence_tokens` already draws on. Used only to resolve a
    *grouped* boundary's `changed_symbols` entries (bare names, no file) to
    a file `is_production_boundary_candidate` can actually check."""
    mapping: dict[str, str] = {}

    def _add(symbol: str | None, file: str | None) -> None:
        if symbol and symbol.lower() not in mapping:
            mapping[symbol.lower()] = file or ""

    for candidate in packet.get("boundary_candidates", []):
        _add(candidate.get("symbol"), candidate.get("file"))
    for changed in packet.get("changed_symbols", []):
        _add(changed.get("symbol"), changed.get("file"))
    for snippet in packet.get("relevant_source_snippets", []):
        _add(snippet.get("symbol"), snippet.get("file"))
    return mapping


def _is_production_eligible(
    *,
    symbol: str,
    file: str,
    changed_symbols: list[str],
    repo: str,
    facts: StructuralFacts,
    known_symbol_files: dict[str, str],
) -> bool:
    """Boundary ELIGIBILITY, not evidence grounding — a test symbol can be a
    real changed symbol (and so satisfy `_is_evidence_grounded`) while still
    being categorically invalid as a production boundary.

    Reuses `is_production_boundary_candidate` — the exact same C.1
    exclusion `boundary_discovery`'s own deterministic traversal already
    applies to every candidate it considers — rather than a second,
    independently-drifting test/`main` check. A test symbol may still
    appear in `supporting_evidence`; it can never BE the boundary.

    Concrete `symbol`/`file`: checked directly. Grouped boundary (a label
    with no single resolvable symbol): eligible if AT LEAST ONE of
    `changed_symbols` resolves (via the packet's own facts) to an eligible
    production identity — never if the only symbols we can resolve are all
    test-only. A `changed_symbols` entry this packet never mentioned at all
    cannot be checked either way and does not by itself block the group.
    """
    if symbol:
        identity = _identity_of(repo, file, symbol)
        return is_production_boundary_candidate(identity, facts)

    resolved: list[bool] = []
    for name in changed_symbols:
        known_file = known_symbol_files.get(name.lower())
        if known_file is None:
            continue  # not one of our own supplied facts; cannot be checked
        identity = _identity_of(repo, known_file, name)
        resolved.append(is_production_boundary_candidate(identity, facts))
    if not resolved:
        return True  # nothing resolvable to check — do not block on this rule alone
    return any(resolved)


def _parse_boundary(
    raw: Any, *, repo: str, position: int, grounded_tokens: frozenset[str],
    facts: StructuralFacts, known_symbol_files: dict[str, str],
) -> AffectedBoundary | None:
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
    subtype = _normalize_subtype(kind, str(raw.get("subtype") or "").strip() or None)
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
    # Increment D.1's central discipline: "template renders new form fields"
    # is a real affected behavior, but it is not itself a boundary unless
    # something concrete supplied actually crosses one. A repo-context fact
    # or the PR's own semantic summary is never enough on its own.
    if not _is_evidence_grounded(symbol=symbol, supporting_evidence=evidence,
                                  grounded_tokens=grounded_tokens):
        return None
    # Grounding is not eligibility: a real changed test symbol can satisfy
    # the check above while still being categorically invalid as a
    # production boundary (e.g. TestLogout). Reuses the exact same C.1
    # production-boundary predicate boundary_discovery applies deterministically
    # — never a second, independently-drifting test/main exclusion.
    if not _is_production_eligible(
        symbol=symbol, file=file, changed_symbols=changed_symbols,
        repo=repo, facts=facts, known_symbol_files=known_symbol_files,
    ):
        return None
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
    facts: StructuralFacts,
    packet: dict[str, Any] | None = None,
) -> list[AffectedBoundary]:
    """Parse, validate and de-duplicate one boundary-reasoning response.

    `packet` (the evidence packet this response was reasoned over) grounds
    the evidence-quality check — omit it only in tests that do not care
    about that rule, where an empty set of grounded tokens means every
    boundary is rejected on that basis, which is the same conservative
    default the real call site always supplies a real packet against.
    """
    established = _deterministic_keys(deterministic)
    grounded_tokens = _grounded_evidence_tokens(packet or {})
    known_symbol_files = _known_symbol_files(packet or {})
    out: list[AffectedBoundary] = []
    seen: set[tuple[str, str]] = set()
    for position, item in enumerate(raw.get("inferred_boundaries") or []):
        boundary = _parse_boundary(
            item, repo=repo, position=position, grounded_tokens=grounded_tokens,
            facts=facts, known_symbol_files=known_symbol_files,
        )
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
    repo_profile: Any | None = None,
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
        repo_root=repo_root, repo_profile=repo_profile,
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
        raw, repo=repo, deterministic=deterministic_boundaries, facts=facts, packet=packet,
    )
    return boundaries, [
        f"boundary_reasoning: candidates={len(packet['boundary_candidates'])} "
        f"inferred={len(boundaries)}"
    ]
