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
    MappedTest,
    SourceRef,
    TIER_DIRECT_INVOCATION,
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


def _matching_flow_by_handler(result: ChangeVerificationResult, target: EntityRef) -> AffectedFlow | None:
    """The one flow a recovered test's target can be honestly attached to.

    Matched the same way `_already_covers` above already treats as a real
    identity match: the target names this flow's own handler symbol, or
    resolves to the file that handler is defined in. Deliberately NOT a
    `changed_nodes` scan -- that list is the whole diff's changed-symbol set
    attached to every flow alike (see `AffectedFlow.changed_nodes`), so it
    would "match" almost every flow at once rather than the one the test
    actually concerns. `handler`/`handler_file` are per-flow-specific
    fields, so this resolves to exactly one flow or none -- ambiguity
    (more than one match) declines rather than guessing, same as every
    other identity check in this module.
    """
    matches = [
        flow for flow in result.affected_flows
        if flow.handler == target.symbol
        or (target.file and flow.artifact_refs.get("handler_file") == target.file)
    ]
    unique = {flow.id: flow for flow in matches}
    return next(iter(unique.values())) if len(unique) == 1 else None


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


def _merge_recovered_tests(result: ChangeVerificationResult, test_recovery: TestRecoveryResult) -> None:
    """Attach each accepted, verified test to a real obligation so it is
    nameable and queryable, not just an aggregate-counter bump.

    A recovered test previously only incremented `summary.counts` -- it was
    never attached to any `AffectedFlow`'s obligations, so nothing in
    `affected_flows` could ever show which test that count referred to,
    or that it existed at all. `summary.counts` and `flow.obligations[].
    mapped_tests` silently disagreed as a result (confirmed on a real run:
    counts reported one verifying test while every obligation's own
    `mapped_tests` was empty). Counts are now derived from what actually
    got attached, so the two can never drift apart again; a test that
    cannot be attached to exactly one flow (see `_matching_flow_by_handler`)
    is recorded in `notes` instead of silently inflating a count nothing
    else reflects.
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
    unattached = 0
    for test in accepted:
        key = (test.file, test.test)
        if key in existing or key in seen_keys:
            continue
        seen_keys.add(key)
        flow = _matching_flow_by_handler(result, test.target)
        obligation = next(iter(flow.obligations), None) if flow is not None else None
        if obligation is None:
            unattached += 1
            continue
        obligation.mapped_tests.append(_mapped_test_from_recovered(test))
        attached += 1

    if attached:
        # Relevant/mapped, never executed -- recovery does not run anything.
        result.summary.counts.mapped_tests += attached
        # A recovered test was adversarially verified to demonstrate the
        # claim (see module docstring), so it counts as real, distinct
        # verifying evidence too.
        result.summary.counts.tests_verifying_behavior += attached
        result.summary.counts.tests_exercising_flows += attached
    if unattached:
        result.notes.append(
            f"AI recovery found {unattached} additional verified test(s) that could not be "
            "attributed to exactly one flow's handler; not counted or shown as evidence."
        )


def merge_verified_recovery_into_result(
    result: ChangeVerificationResult, path_recovery: PathRecoveryResult, test_recovery: TestRecoveryResult,
) -> None:
    """Enrich `result` in place with whatever recovery actually
    ESTABLISHED — an unresolved or partial attempt leaves it completely
    unchanged (still surfaced separately via
    `sydes.recovery.merge.summarize_for_notes`, in `notes` only)."""
    _merge_established_paths(result, path_recovery)
    _merge_recovered_tests(result, test_recovery)
