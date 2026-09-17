"""Merging VERIFIED AI-recovery findings into the canonical
`ChangeVerificationResult` — the one deliberate, narrow exception to the
rule that recovery never touches the structural result (see
`sydes.recovery.merge`, which builds the separate sidecar view and never
takes a `ChangeVerificationResult` at all).

This exists because a recovered, adversarially-verified path/test that
never reaches the canonical result is invisible to the one place a user
actually reads it — the rendered PR comment, which renders `affected_flows`
/ `accepted_impacts` / `summary.counts`, never `notes`. Teaching the
renderer to parse `notes` instead was explicitly rejected as a workaround;
this merge is the real fix, upstream of rendering.

Fail-safe and additive by construction:
- an unresolved/partial recovery attempt changes nothing here at all
- a verified finding is only ever ADDED; the one narrow exception is
  dropping a now-redundant INFERRED impact for the exact same changed
  symbol a newly-established path just proved reaches an entrypoint (see
  `_drop_subsumed_inferred_impacts`) — never a `status == "proven"`
  impact, and never anything an established path doesn't structurally
  correspond to
- whatever the deterministic pipeline already ESTABLISHED always wins —
  proven evidence is never removed, downgraded, or overwritten
- a route/target the result already covers (any provenance) is never
  duplicated
- `summary.verdict`/`risk`/`headline`, CBM's own graph, and
  `tests_executed` are never touched: recovery evidence is adversarially
  verified, not executed, and this merge only changes what Sydes reports
  finding, never a claim that the overall change is now more "verified"
  than the deterministic pipeline established on its own
"""

from __future__ import annotations

from sydes.recovery.schema import (
    EntityRef,
    PathRecoveryResult,
    ROOT_VERIFIED_BOUNDARY,
    RecoveredTest,
    STATUS_ESTABLISHED,
    STATUS_PARTIAL,
    TEST_STATUS_ACCEPTED,
    TestRecoveryResult,
)
from sydes.verify.models import (
    AcceptedImpact,
    AffectedFlow,
    ChangeVerificationResult,
    EVIDENCE_SCOPE_CHANGE,
    EVIDENCE_SCOPE_SYMBOL,
    MappedTest,
    SourceRef,
    TIER_DIRECT_INVOCATION,
    UnattachedEvidence,
)

PROVENANCE_AI_RECOVERY = "ai_recovery"


def _flow_id_for(entrypoint: str, target_symbol: str) -> str:
    return f"flow:ai_recovery:{entrypoint}:{target_symbol}"


def _already_covers(result: ChangeVerificationResult, entry_label: str, target_symbol: str) -> bool:
    """True if an existing flow (any provenance) already represents this
    same entrypoint -> changed-symbol pair. Recovery must never add a
    second, duplicate flow for something already covered — structural
    evidence or an earlier recovery merge, it does not matter which."""
    for flow in result.affected_flows:
        if flow.entry_label != entry_label:
            continue
        if flow.handler == target_symbol:
            return True
        if any(node.symbol == target_symbol for node in flow.changed_nodes):
            return True
    return False


def _changed_symbol_names_for_target(result: ChangeVerificationResult, target: EntityRef) -> set[str]:
    """The diff's OWN ground-truth changed-symbol name(s) (`change.symbols`,
    computed once, deterministically, before recovery ever runs) that this
    recovered target structurally corresponds to -- matched by the target's
    file (most reliable: the diff can only have one changed symbol per
    file in the common case) or by exact symbol/qualified-name equality.
    Never a fuzzy text/label comparison.
    """
    names: set[str] = set()
    for changed in result.change.symbols:
        same_file = bool(changed.file) and changed.file == target.file
        same_symbol = changed.name == target.symbol or changed.qualified_name == target.symbol
        if same_file or same_symbol:
            names.add(changed.name)
            if changed.qualified_name:
                names.add(changed.qualified_name)
    return names


def _is_subsumed_inferred_impact(impact: AcceptedImpact, changed_symbol_names: set[str]) -> bool:
    """True only for an INFERRED impact anchored to the same changed
    symbol a newly-established recovered path just proved reaches an
    entrypoint -- never a proven/structural impact (checked by the
    caller), and never anything matched by comparing `label`/
    `behavior_label` text.

    Two structured signals, both already present on `AcceptedImpact`:
    `changed_symbols` (when populated with real names, not the generic
    `"(whole change)"` placeholder a whole-change-level inference uses),
    and the `id` field's own documented `impact:{repo}:{qualified_name or
    symbol}` anchor convention -- the same identity a real, per-symbol
    impact's `changed_symbols` entry would carry, just encoded in `id`
    instead for this shape.
    """
    if impact.status != "inferred":
        return False
    if any(symbol in changed_symbol_names for symbol in impact.changed_symbols):
        return True
    anchor = impact.id.rsplit(":", 1)[-1]
    return anchor in changed_symbol_names


def _drop_subsumed_inferred_impacts(
    result: ChangeVerificationResult, changed_symbol_names: set[str],
) -> None:
    if not changed_symbol_names:
        return
    kept: list[AcceptedImpact] = []
    for impact in result.accepted_impacts:
        if _is_subsumed_inferred_impact(impact, changed_symbol_names):
            result.summary.counts.impacts_inferred = max(0, result.summary.counts.impacts_inferred - 1)
            if impact.verification_model_status != "modeled":
                result.summary.counts.impacts_not_modeled = max(0, result.summary.counts.impacts_not_modeled - 1)
            continue
        kept.append(impact)
    result.accepted_impacts[:] = kept


def _existing_mapped_test_keys(result: ChangeVerificationResult) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for flow in result.affected_flows:
        for obligation in flow.obligations:
            for mapped in obligation.mapped_tests:
                if mapped.file and mapped.name:
                    keys.add((mapped.file, mapped.name))
    return keys


def _merge_established_paths(result: ChangeVerificationResult, path_recovery: PathRecoveryResult) -> None:
    if path_recovery.status != STATUS_ESTABLISHED:
        return
    for path in path_recovery.paths:
        if path.status != STATUS_ESTABLISHED or not path.nodes:
            continue
        entry_label = path.entrypoint
        target = path.nodes[-1]
        if not entry_label or not target.symbol or not target.file:
            continue
        if _already_covers(result, entry_label, target.symbol):
            continue

        if path.root_boundary_status != ROOT_VERIFIED_BOUNDARY:
            # `nodes[0]` (the claimed entrypoint) was never cross-checked
            # against a real, known entrypoint -- see
            # `sydes.recovery.agent._root_boundary_status_for`. Every EDGE
            # in this path may be fully evidence-proven, but the ROOT
            # itself is still only Stage A's free-text guess (its own
            # prompt explicitly allows "e.g. GET /users or a queue/job
            # name" with no requirement it name something real). Merging
            # this as a proven `AffectedFlow`/route identity would present
            # an unverified natural-language guess as established
            # structural truth -- exactly the bug this check exists to
            # prevent. Never silently dropped either: still surfaced as an
            # INFERRED impact (the renderer's existing "likely, not fully
            # established" path), honestly labeled uncertain rather than
            # invented as fact.
            changed_symbol_names = _changed_symbol_names_for_target(result, target)
            impact_id = f"impact:ai_recovery:candidate_boundary:{target.symbol}"
            if any(impact.id == impact_id for impact in result.accepted_impacts):
                continue
            result.accepted_impacts.append(AcceptedImpact(
                id=impact_id,
                label=target.symbol,
                kind="ai_recovery",
                status="inferred",
                changed_symbols=sorted(changed_symbol_names) or [target.symbol],
                llm_reason=path.entrypoint,
                llm_uncertainty=(
                    "AI recovery proposed this entrypoint from a free-text hypothesis; it could "
                    "not be matched to a known, structurally-confirmed entrypoint, so the exact "
                    "route/boundary identity is not established."
                ),
                verification_model_status="unsupported_or_partial",
                provenance=PROVENANCE_AI_RECOVERY,
            ))
            result.summary.counts.impacts_inferred += 1
            continue

        # A single node means the entrypoint's own decorator sits directly
        # on the target (see `sydes.recovery.agent._direct_entrypoint_edge`)
        # -- there is no separate "handler" to show. Two or more nodes:
        # the hop immediately after the entrypoint is the closest thing to
        # a single "handler" the route -> handler -> symbol display can
        # show; any longer internal chain (recursive decomposition) still
        # exists in full in the recovery sidecar artifact, never lost,
        # just not repeated at this summary level.
        handler = path.nodes[0].symbol if len(path.nodes) >= 2 else ""

        flow_id = _flow_id_for(entry_label, target.symbol)
        result.affected_flows.append(AffectedFlow(
            id=flow_id,
            entry_label=entry_label,
            handler=handler or None,
            changed_nodes=[SourceRef(file=target.file, symbol=target.symbol)],
            provenance=PROVENANCE_AI_RECOVERY,
            impact_status="proven",
        ))
        # counts.affected_flows is defined as len(result.affected_flows);
        # the append above must never silently drift it out of sync with
        # the list it's supposed to describe (see task item 7: "counts
        # match lists").
        result.summary.counts.affected_flows += 1
        changed_symbol_names = _changed_symbol_names_for_target(result, target)
        _drop_subsumed_inferred_impacts(result, changed_symbol_names)

        result.accepted_impacts.append(AcceptedImpact(
            id=flow_id,
            label=target.symbol,
            kind="ai_recovery",
            status="proven",
            changed_symbols=sorted(changed_symbol_names) or [target.symbol],
            verification_model_status="modeled",
            provenance=PROVENANCE_AI_RECOVERY,
        ))
        result.summary.counts.impacts_proven += 1


def _matching_flows_by_handler(result: ChangeVerificationResult, target: EntityRef) -> list[AffectedFlow]:
    """Every flow a recovered test's target can be honestly attached to.

    Matched the same way `_already_covers` above already treats as a real
    identity match: the target names this flow's own handler symbol, or
    resolves to the file that handler is defined in. Deliberately NOT a
    `changed_nodes` scan -- that list is the whole diff's changed-symbol set
    attached to every flow alike (see `AffectedFlow.changed_nodes`), so it
    would "match" almost every flow at once rather than the one the test
    actually concerns.

    Previously this required resolving to EXACTLY ONE flow and declined
    (dropping the evidence to a notes-only line, never attached or
    counted) whenever several flows matched -- e.g. 5 HTTP routes all
    dispatching to the same real handler. Obligations across those route
    aliases already share a stable `canonical_id` for the same underlying
    claim (see `verify.obligations.compute_canonical_id`, assigned by
    `verify.analyzer._canonicalize_obligations_across_flows`), so
    attaching to every matching flow's own obligation copy is the correct
    behavior now, not an ambiguity to decline -- see `_merge_recovered_tests`.
    """
    return [
        flow for flow in result.affected_flows
        if flow.handler == target.symbol
        or (target.file and flow.artifact_refs.get("handler_file") == target.file)
    ]


def _matching_changed_symbol(result: ChangeVerificationResult, target: EntityRef) -> tuple[str, str] | None:
    """Does `target` (a recovered test's claimed coverage target) name one
    of THIS change's own changed symbols? `(symbol_name, file)` if so, else
    `None`.

    Same identity discipline as `_matching_flows_by_handler`: `file` is
    load-bearing (a bare-name match in an unrelated file is not this
    symbol), `symbol` matched against both the short name and the
    qualified name a changed symbol carries.
    """
    if not target.file:
        return None
    for symbol in result.change.symbols:
        if symbol.file != target.file:
            continue
        candidates = {symbol.name, symbol.qualified_name}
        if target.symbol in candidates or target.qualified_name in candidates:
            return (symbol.qualified_name or symbol.name, symbol.file)
    return None


def _mapped_test_from_recovered(test: RecoveredTest) -> MappedTest:
    return MappedTest(
        id=f"ai_recovery:{test.file}:{test.test}",
        name=test.test,
        case_name=test.test,
        file=test.file,
        match_rule=test.covers or "Recovered and adversarially verified by AI test recovery.",
        evidence_tier=TIER_DIRECT_INVOCATION,
        source_refs=[f"ai_recovery:{test.file}:{test.test}"],
    )


def _unattached_evidence_slot(
    result: ChangeVerificationResult, *, scope: str, target_symbol: str | None, target_file: str | None,
) -> UnattachedEvidence:
    """The one `UnattachedEvidence` entry for this (scope, target_symbol) --
    reused across calls so two tests falling to the same scope/symbol don't
    create two separate entries."""
    for entry in result.unattached_evidence:
        if entry.scope == scope and entry.target_symbol == target_symbol:
            return entry
    entry = UnattachedEvidence(
        scope=scope, target_symbol=target_symbol, target_file=target_file, provenance=PROVENANCE_AI_RECOVERY,
    )
    result.unattached_evidence.append(entry)
    return entry


def _merge_recovered_tests(result: ChangeVerificationResult, test_recovery: TestRecoveryResult) -> None:
    """Attach each accepted, verified test at the STRONGEST scope actually
    proven, so it is nameable and queryable rather than a bare counter bump
    or, worse, silently discarded:

    1. An exact flow/obligation match (`_matching_flows_by_handler`) --
       attaches directly to every matching flow's own obligation, mirrored
       across route aliases (see that function's docstring).
    2. Failing that, a changed symbol this test demonstrably covers
       (`_matching_changed_symbol`) -- preserved as `UnattachedEvidence`
       with `scope=EVIDENCE_SCOPE_SYMBOL`. Confirmed real case: Unleash
       PR #12632's duplicate-parameter-name tests target `strategySchema`,
       a real changed symbol, but no HTTP route was ever structurally
       connected to it (route composition is a separate, deferred gap) --
       under the old "exactly one flow or nothing" rule this evidence
       vanished into a notes-only line despite being real, verified, and
       plainly about this change.
    3. Failing that, the test is still adversarially verified (Layer 0/1/2
       passed -- see `sydes.recovery.verify`) and therefore genuinely
       source-backed and relevant to this change, even without a provable
       symbol/flow anchor -- preserved as `UnattachedEvidence` with
       `scope=EVIDENCE_SCOPE_CHANGE`. This is the floor, not a guess: an
       `accepted` `RecoveredTest` already cleared the same identity/
       evidence/adversarial-judgment gates a flow-attached test did: see
       `sydes.recovery.verify`'s module docstring. Nothing here claims a
       route, a handler, or a boundary -- only that a real, verified test
       exists and relates to this change at the stated scope.

    None of this claims a system boundary or fabricates a route -- see
    `UnattachedEvidence`'s own docstring. Counts are derived from what
    actually got attached (at any of the three levels), so
    `summary.counts` and the canonical data it describes can never drift
    apart -- this was the original bug this function fixed, now extended
    to the two additional fallback scopes.
    """
    if test_recovery.status not in (STATUS_ESTABLISHED, STATUS_PARTIAL):
        return
    accepted = [t for t in test_recovery.tests if t.status == TEST_STATUS_ACCEPTED]
    if not accepted:
        return
    existing = _existing_mapped_test_keys(result)
    # Dedup by (file, test) -- the agent proposing (or two separate recovery
    # attempts each accepting) the same real test twice must never inflate
    # counts beyond the number of actually-distinct tests recovered.
    seen_keys: set[tuple[str, str]] = set()
    attached = 0
    for test in accepted:
        key = (test.file, test.test)
        if key in existing or key in seen_keys:
            continue
        seen_keys.add(key)
        mapped = _mapped_test_from_recovered(test)

        matching_flows = _matching_flows_by_handler(result, test.target)
        attached_to_any = False
        for flow in matching_flows:
            # Attach to every matching flow's own obligation copy -- when
            # several flows are route aliases of the same handler, their
            # obligations already share a `canonical_id` for the same
            # underlying claim (see `_matching_flows_by_handler`'s
            # docstring); mirroring here keeps them agreeing instead of
            # attaching to one arbitrary alias and leaving its siblings
            # stale.
            obligation = next(iter(flow.obligations), None)
            if obligation is None:
                continue
            if any(m.file == mapped.file and m.name == mapped.name for m in obligation.mapped_tests):
                attached_to_any = True  # already there (e.g. a prior recovery pass) -- still a real attachment
                continue
            obligation.mapped_tests.append(mapped)
            attached_to_any = True

        if not attached_to_any:
            symbol_match = _matching_changed_symbol(result, test.target)
            if symbol_match is not None:
                symbol_name, symbol_file = symbol_match
                slot = _unattached_evidence_slot(
                    result, scope=EVIDENCE_SCOPE_SYMBOL, target_symbol=symbol_name, target_file=symbol_file,
                )
            else:
                # Floor of the ladder: no exact flow, no provable changed-
                # symbol anchor -- but this test already passed Layer 0/1/2
                # (it is in `accepted`), so it is genuinely source-backed
                # and relevant to this change. Preserved, never discarded.
                slot = _unattached_evidence_slot(
                    result, scope=EVIDENCE_SCOPE_CHANGE, target_symbol=None, target_file=None,
                )
            if not any(m.file == mapped.file and m.name == mapped.name for m in slot.mapped_tests):
                slot.mapped_tests.append(mapped)
            attached_to_any = True

        if attached_to_any:
            attached += 1

    if attached:
        # Relevant/mapped, never executed -- recovery does not run anything.
        result.summary.counts.mapped_tests += attached
        # A recovered test was adversarially verified to demonstrate the
        # claim (see module docstring), so it counts as real, distinct
        # verifying evidence too -- regardless of which scope it landed at.
        result.summary.counts.tests_verifying_behavior += attached
        result.summary.counts.tests_exercising_flows += attached


def merge_verified_recovery_into_result(
    result: ChangeVerificationResult, path_recovery: PathRecoveryResult, test_recovery: TestRecoveryResult,
) -> bool:
    """Enrich `result` in place with whatever recovery actually
    ESTABLISHED — an unresolved or partial attempt leaves it completely
    unchanged (still surfaced separately via
    `sydes.recovery.merge.summarize_for_notes`, in `notes` only).

    Returns whether anything was actually appended to `result` this call
    (a new flow, accepted impact, or mapped test) -- callers use this to
    stop claiming "not merged into structural results" unconditionally
    (see `sydes.recovery.merge.summarize_for_notes`) immediately after a
    call that, in fact, did merge something.
    """
    flows_before = len(result.affected_flows)
    impacts_before = len(result.accepted_impacts)
    mapped_before = result.summary.counts.mapped_tests
    _merge_established_paths(result, path_recovery)
    _merge_recovered_tests(result, test_recovery)
    return (
        len(result.affected_flows) > flows_before
        or len(result.accepted_impacts) > impacts_before
        or result.summary.counts.mapped_tests > mapped_before
    )
