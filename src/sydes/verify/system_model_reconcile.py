"""Reconciles one `verify-change` run's results into the persistent
`SystemModelStore` (`sydes.store.system_model`), and the one gate that
lets an *identical* rerun skip the repository test suite.

Gated entirely behind `VerifyChangeOptions.persist_system_model` (off by
default) — nothing here runs, and nothing about existing behavior changes,
unless a caller explicitly opts in.

v1 safety rule (binding, see `sydes.store.system_model.VerificationRecord`):
`should_skip_test_execution` only ever returns `True` when EVERY obligation
in the current run already has a persisted record whose
`established_at_commit` exactly equals the current run's `head` AND whose
tracked input fingerprint still matches. A different `head` commit always
runs the suite normally, even if the fingerprint happens to still match —
test outcomes can depend on things (shared fixtures, config, lockfiles,
migrations, environment) this module's fingerprint does not, and cannot,
track.
"""

from __future__ import annotations

from pathlib import Path

from sydes.core.models import EvidenceRef, RepoRef
from sydes.store.system_model import (
    SystemModelStore,
    VerificationRecord,
    compute_fingerprint,
    new_run_timestamp,
    symbol_entity_id,
    verification_record_id,
)
from sydes.verify.models import (
    VERIFICATION_FAILED,
    VERIFICATION_PASSED,
    AffectedFlow,
    ChangeVerificationResult,
    VerificationObligation,
)

#: Only these two statuses represent a real, evidence-backed conclusion
#: worth persisting — UNVERIFIED/UNKNOWN mean "nothing was demonstrated,"
#: which has nothing durable to cache.
_PERSISTABLE_STATUSES = frozenset({VERIFICATION_PASSED, VERIFICATION_FAILED})


def _dependency_files(
    flow: AffectedFlow, obligation: VerificationObligation,
) -> list[tuple[str, str]]:
    """Every `(repo, path)` this obligation's status depends on: the flow's
    route/handler files, every changed-symbol file the flow's trace
    reached, and every mapped test's own file. Sorted and deduplicated so
    the fingerprint is stable regardless of collection order."""
    repo = flow.repo or ""
    files: set[tuple[str, str]] = set()
    for key in ("route_file", "handler_file"):
        path = flow.artifact_refs.get(key)
        if path:
            files.add((repo, path))
    for node in flow.changed_nodes:
        if node.file:
            files.add((node.repo or repo, node.file))
    for test in obligation.mapped_tests:
        if test.file:
            files.add((test.repo or repo, test.file))
    return sorted(files)


def _repo_roots(repos: list[RepoRef]) -> dict[str, Path]:
    return {repo.name: Path(repo.root) for repo in repos}


def _obligation_evidence(obligation: VerificationObligation) -> list[EvidenceRef]:
    """The real evidence behind a PASSED/FAILED status: the mapped tests
    that actually resolved it. `VerificationObligation.evidence` itself is
    never populated anywhere in the existing pipeline (confirmed by
    inspection — it stays at its default empty list throughout
    `analyzer.py`/`obligations.py`), so persisting it as-is would silently
    record no evidence at all. `mapped_tests` is what `resolve_obligation_
    status` actually decided the status from, so that's the real basis."""
    return [
        EvidenceRef(
            file=test.file or "", symbol=test.case_name or test.name,
            label="mapped test", snippet=None,
        )
        for test in obligation.mapped_tests
        if test.file
    ]


def should_skip_test_execution(
    result: ChangeVerificationResult, store: SystemModelStore, *, repos: list[RepoRef],
) -> dict[str, VerificationRecord] | None:
    """Returns a `{obligation_id: VerificationRecord}` map to restore from
    when every obligation that actually NEEDS the test suite is
    skip-eligible, else `None` (meaning: run the suite normally, exactly as
    if this feature didn't exist).

    An obligation with no `mapped_tests` at all never depends on the suite
    in the first place — `resolve_obligation_status` resolves it to
    UNVERIFIED from `mapped_tests` alone, before it would even look at
    `ci_suite` — so it is excluded from this check entirely rather than
    forced to have a cached record it could never earn. Only obligations
    with at least one mapped test must have one.

    Skip-eligible (for those obligations) = a persisted record exists for
    `(flow_id, kind, statement)`, `record.established_at_commit ==
    result.change.head` (the v1 safety rule — never relaxed to "any commit
    whose fingerprint matches"), and the fingerprint still matches today
    (guards against `includes_working_tree` making "the same head" cover
    different uncommitted content across two invocations).
    """
    head = result.change.head
    if not head:
        return None  # no stable commit identity to compare against; never skip

    testable_obligations = [
        (flow, obligation)
        for flow in result.affected_flows
        for obligation in flow.obligations
        if obligation.mapped_tests
    ]
    if not testable_obligations:
        # Nothing in this run actually depends on the suite -- a vacuous
        # "every obligation is skip-eligible" must not be read as "skip the
        # suite." `run_ci_suite` still has meaning with no testable
        # obligations (repo-health baseline, diagnostics), and must run
        # exactly as it would without this feature.
        return None

    repo_roots = _repo_roots(repos)
    restorable: dict[str, VerificationRecord] = {}
    for flow, obligation in testable_obligations:
        record_id = verification_record_id(flow.id, obligation.kind, obligation.statement)
        record = store.latest_for(record_id)
        if record is None or record.established_at_commit != head:
            return None
        dependency_files = [(repo, path) for repo, path in record.input_files]
        fingerprint, all_present = compute_fingerprint(dependency_files, repo_roots)
        if not all_present or fingerprint != record.input_fingerprint:
            return None
        restorable[obligation.id] = record
    return restorable


def restore_from_records(
    result: ChangeVerificationResult, restorable: dict[str, VerificationRecord],
) -> None:
    """Apply a `should_skip_test_execution` result: populate every
    obligation's status/reason/evidence from its cached record instead of
    running the suite. Never called unless every obligation in the run had
    a restorable record (see that function's contract)."""
    for flow in result.affected_flows:
        for obligation in flow.obligations:
            record = restorable.get(obligation.id)
            if record is None:
                continue
            obligation.status = record.status
            obligation.reason = None
            obligation.evidence = [EvidenceRef(**item) for item in record.evidence]
    total = sum(len(flow.obligations) for flow in result.affected_flows)
    result.notes.append(
        f"{total} obligation(s) restored unchanged from an identical prior run "
        f"(commit {result.change.head}); test suite not re-executed."
    )


def reconcile(
    result: ChangeVerificationResult, store: SystemModelStore, *, repos: list[RepoRef], run_id: str,
) -> None:
    """Persist this run's topology (flows/symbols/REACHES) and append a
    `VerificationRecord` for every obligation that reached a real,
    evidence-backed PASSED/FAILED conclusion. Always appends to history,
    never overwrites — see `SystemModelStore.append_verification_record`.

    Safe to call after a skipped run too: the restored obligations still
    carry the same status/evidence, so this just re-confirms the existing
    latest record (a fresh, identical entry — `confirmed_at`/`run_id`
    updated, everything else unchanged) rather than requiring special-case
    handling for "was this run skipped."
    """
    repo_roots = _repo_roots(repos)
    head = result.change.head or ""
    confirmed_at = new_run_timestamp()

    for flow in result.affected_flows:
        store.upsert_flow(flow.id, repo=flow.repo, label=flow.entry_label)
        for node in flow.changed_nodes:
            if not node.file or not node.symbol:
                continue
            node_repo = node.repo or flow.repo or ""
            symbol_id = symbol_entity_id(node_repo, node.symbol)
            store.upsert_symbol(symbol_id, repo=node_repo, qualified_name=node.symbol)
            store.add_reaches_relation(flow.id, symbol_id)

        for obligation in flow.obligations:
            if obligation.status not in _PERSISTABLE_STATUSES:
                continue
            dependency_files = _dependency_files(flow, obligation)
            fingerprint, _all_present = compute_fingerprint(dependency_files, repo_roots)
            record = VerificationRecord(
                id=verification_record_id(flow.id, obligation.kind, obligation.statement),
                flow_id=flow.id, kind=obligation.kind, statement=obligation.statement,
                status=obligation.status,
                evidence=[item.model_dump() for item in _obligation_evidence(obligation)],
                established_at_commit=head,
                input_files=[[repo, path] for repo, path in dependency_files],
                input_fingerprint=fingerprint,
                run_id=run_id, confirmed_at=confirmed_at,
            )
            store.append_verification_record(record)

    store.save()
