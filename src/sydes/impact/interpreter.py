"""From changed symbols to the entrypoints they could reach.

This layer answers exactly one question and stops. It does not decide what must
be verified, does not run tests, and does not produce a verdict — those live
downstream and are unchanged.

The strategies are deliberately generic. Each one keys off a *kind of
structural fact* rather than a framework: a symbol carrying route metadata, a
call edge, a usage reference, an identifier appearing in captured decorator
text, an identifier appearing in a signature. No decorator name, framework, or
library is hardcoded, because a rule that recognises one framework by name
fails silently on the next one and is worse than a rule that admits it cannot
tell.

Traversal is bounded on every axis — depth, visited nodes, and a global work
budget — and every bound that bites is reported rather than absorbed. A result
that stopped early is marked truncated, because "no entrypoint reached" and
"gave up before reaching one" must not look the same to a reader.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
import re
import time
from typing import Any, Iterable

from sydes.code_intelligence.base import StructuralFacts
from sydes.impact.boundary_discovery import BoundaryBudget, discover_boundaries
from sydes.impact.guide import GuideError, ImpactGuide
from sydes.impact.investigate import InvestigationExecutor, source_preview
from sydes.impact.models import (
    ACTION_INFER_IMPACT,
    ACTION_INSPECT_ENCLOSING_FUNCTION,
    ACTION_INSPECT_NEARBY_ENTRYPOINTS,
    ACTION_INSPECT_SOURCE_SPAN,
    ACTION_INSPECT_SYMBOL,
    ACTION_STOP_UNRESOLVED,
    COMPLETENESS_COMPLETE,
    COMPLETENESS_TRUNCATED,
    ENTRYPOINT_DECORATED,
    ENTRYPOINT_HTTP,
    ENTRYPOINT_UNKNOWN,
    GUIDE_AUTO,
    GUIDE_OFF,
    GUIDE_POLICIES,
    IMPACT_STATUS_INFERRED,
    IMPACT_STATUS_PROVEN,
    PROVENANCE_DETERMINISTIC,
    PROVENANCE_LLM_GUIDED_SOURCE_CONFIRMED,
    PROVENANCE_LLM_INFERRED_CORROBORATED,
    PROVENANCE_LLM_INFERRED_UNCORROBORATED,
    REASON_AMBIGUOUS_SYMBOL_IDENTITY,
    REASON_MULTIPLE_UNRESOLVED_CANDIDATES,
    REASON_NO_ENTRYPOINT_REACHED,
    REASON_PARTIAL_PATH_DEAD_END,
    REASON_TRAVERSAL_TRUNCATED,
    RELATION_CALLS,
    RELATION_DECORATOR_REFERENCE,
    RELATION_DIRECT,
    RELATION_LLM_INFERRED,
    RELATION_SIGNATURE_REFERENCE,
    RELATION_SOURCE_CONFIRMED,
    RELATION_USAGE,
    STRATEGY_CALL_REACHABILITY,
    STRATEGY_DECORATOR_REFERENCE,
    STRATEGY_DIRECT_ENTRYPOINT,
    STRATEGY_GUIDED_INVESTIGATION,
    STRATEGY_LLM_SEMANTIC_INFERENCE,
    STRATEGY_SIGNATURE_REFERENCE,
    STRATEGY_USAGE_REACHABILITY,
    AffectedEntrypoint,
    ImpactCandidate,
    ImpactPath,
    ImpactQuestion,
    ImpactResult,
    ImpactStep,
    SymbolIdentity,
    UnresolvedFrontier,
    UnresolvedImpact,
)

#: Identifiers in decorator or signature text. Deliberately crude: this finds
#: *candidate* names, and a candidate only becomes a reference when it matches
#: a symbol the change actually touched.
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: Names that appear in almost every decorator and would match everything.
#: Excluded because a reference that matches every handler distinguishes none.
_UNINFORMATIVE = frozenset({
    "self", "cls", "None", "True", "False", "str", "int", "bool", "float",
    "list", "dict", "set", "tuple", "Any", "Optional", "Union", "List", "Dict",
    "async", "def", "class", "return", "await", "import", "from",
})


@dataclass(frozen=True)
class TraversalBudget:
    """Hard limits on how far interpretation will walk.

    Bounded traversal is a correctness property, not a performance one: an
    unbounded walk over a dense import graph reaches everything, and an answer
    that names every entrypoint names none.
    """

    max_depth: int = 4
    max_visited: int = 5000
    max_paths_per_entrypoint: int = 4


@dataclass(frozen=True)
class GuideBudget:
    """Hard limits on the M3 guide loop, independent of traversal bounds.

    A guide turn is an LLM call plus one deterministic query or source read —
    orders of magnitude more expensive than a graph hop, so it gets its own,
    much smaller budget. Both limits are turn counts, not wall-clock time:
    a slow provider is a timeout/error, handled by `GuideError`, not a reason
    to loosen these.
    """

    max_turns_per_symbol: int = 3
    max_turns_total: int = 8


#: Whole-change context (other changed symbols, impacts already accepted)
#: shown to every guide turn is a short list, not a second diff dump.
_WHOLE_CHANGE_CONTEXT_CAP = 15


class ImpactInterpreter:
    """Interprets structural facts as reachable entrypoints.

    Construction is cheap and stateless; all the facts arrive at `interpret`.

    `guide`/`guide_policy` add the M3 investigation loop. With `guide_policy`
    left at `GUIDE_OFF` (the default), nothing here changes: no guide is
    consulted, no extra traversal runs, and `interpret`'s output is exactly
    the M2 deterministic result. The loop only ever runs against symbols this
    interpreter's own deterministic pass already marked unresolved, and only
    ever adds evidence back into the same bounded traversal machinery — it
    cannot classify a route as affected on its own say-so.
    """

    def __init__(
        self,
        budget: TraversalBudget | None = None,
        *,
        guide: ImpactGuide | None = None,
        guide_policy: str = GUIDE_OFF,
        guide_budget: GuideBudget | None = None,
        repo_root: Path | None = None,
    ) -> None:
        self.budget = budget or TraversalBudget()
        self._ambiguous_edges = 0
        self._guide = guide
        self._guide_policy = guide_policy if guide_policy in GUIDE_POLICIES else GUIDE_OFF
        self._guide_budget = guide_budget or GuideBudget()
        self._repo_root = repo_root

    # -- public API -------------------------------------------------------

    def interpret(
        self,
        changed_symbols: Iterable[dict[str, Any]],
        facts: StructuralFacts,
        *,
        repo: str | None = None,
        semantic_texts: list[str] | None = None,
        boundary_budget: BoundaryBudget | None = None,
    ) -> ImpactResult:
        """Find the entrypoints the changed symbols could reach.

        `changed_symbols` are mappings carrying at least `name`, and optionally
        `file` and `qualified_name` — the shape Sydes' change attribution
        already produces.

        `semantic_texts` (Increment C): plain strings extracted from
        `pr_semantic_analysis` (behavior-change descriptions, investigation
        concepts/related symbols) — used only to *rank* the boundary-discovery
        frontier below; see `boundary_discovery._semantic_relevance`. Never
        required: omitted or empty, boundary discovery is fully deterministic.
        """
        changed = [item for item in changed_symbols if item.get("name")]
        index = _FactIndex(facts, repo)
        result = ImpactResult()
        found: dict[str, AffectedEntrypoint] = {}
        truncated = False
        self._ambiguous_edges = 0
        # One entry per symbol the deterministic pass left unresolved, kept
        # only long enough to hand to the guide loop below. `dead_ends` is the
        # trail of every node `_reachability` actually visited without
        # reaching an entrypoint — the guide's only view into "how far did
        # this get."
        guide_candidates: list[tuple[dict[str, Any], str, SymbolIdentity,
                                      list[tuple[SymbolIdentity, tuple[ImpactStep, ...]]]]] = []

        if not index.entrypoints:
            result.notes.append(
                "no entrypoint symbols were supplied by the backend; nothing "
                "could be reached even if the change is reachable in principle"
            )

        for symbol in changed:
            name = str(symbol["name"])
            reached = False
            dead_ends: list[tuple[SymbolIdentity, tuple[ImpactStep, ...]]] = []

            for path, target in self._direct(symbol, index):
                self._record(found, target, name, path)
                reached = True

            for path, target, hit_limit in self._reachability(symbol, index, dead_ends=dead_ends):
                truncated = truncated or hit_limit
                if target is not None and path is not None:
                    self._record(found, target, name, path)
                    reached = True

            for path, target in self._decorator_references(symbol, index):
                self._record(found, target, name, path)
                reached = True

            for path, target in self._signature_references(symbol, index):
                # Weakest evidence this module produces — see `_record`'s
                # docstring. Never PROVEN on the strength of a signature
                # reference alone.
                self._record(found, target, name, path, status=IMPACT_STATUS_INFERRED)
                reached = True

            if not reached:
                result.unresolved.append(
                    UnresolvedImpact(
                        repo=index.repo_of(symbol),
                        symbol=name,
                        reason=REASON_NO_ENTRYPOINT_REACHED,
                        detail=(
                            "no route metadata, call path, usage reference, "
                            "decorator reference, or signature reference "
                            "connected this symbol to a known entrypoint"
                        ),
                    )
                )
                if self._guide_policy != GUIDE_OFF and self._guide is not None:
                    guide_candidates.append((symbol, name, index.identity_of(symbol), dead_ends))

        guide_metrics = self._run_guide_loop(
            guide_candidates, index, found, result, truncated_globally=truncated,
            all_changed_names=tuple(str(item["name"]) for item in changed),
        )

        # Deterministic ordering: identical facts must produce an identical
        # report, or two runs of the same analysis cannot be compared.
        result.affected = sorted(
            found.values(), key=lambda item: (item.kind, item.label, item.qualified_name)
        )
        for entrypoint in result.affected:
            entrypoint.changed_symbols = sorted(set(entrypoint.changed_symbols))
            entrypoint.paths = sorted(
                entrypoint.paths, key=lambda path: (path.length, path.strategy, path.describe())
            )[: self.budget.max_paths_per_entrypoint]
        result.unresolved = sorted(result.unresolved, key=lambda item: item.symbol)
        result.completeness = (
            COMPLETENESS_TRUNCATED if truncated else COMPLETENESS_COMPLETE
        )

        # Increment C: ranked, typed boundary discovery — a complementary
        # pass over the same `index`/`facts` this method already built,
        # never a second engine. See `boundary_discovery.discover_boundaries`
        # for the full soundness argument (every boundary emitted was
        # reached by a real walked edge; semantic hints only rank).
        boundaries, boundary_decisions, boundary_metrics = discover_boundaries(
            changed, index, facts,
            semantic_texts=semantic_texts, budget=boundary_budget,
        )
        result.boundaries = boundaries
        result.boundary_decisions = boundary_decisions

        result.metrics = {
            "changed_symbols": len(changed),
            "affected_entrypoints": len(result.affected),
            "http_entrypoints": len(result.http_entrypoints),
            "unresolved_symbols": len(result.unresolved),
            "known_entrypoints": len(index.entrypoints),
            "call_edges": len(facts.call_edges),
            "usage_edges": len(facts.usage_edges),
            "max_depth": self.budget.max_depth,
            "max_visited": self.budget.max_visited,
            "ambiguous_edges": self._ambiguous_edges,
            **guide_metrics,
            **boundary_metrics,
        }
        if truncated:
            result.notes.append(
                f"traversal hit a bound (depth {self.budget.max_depth}, "
                f"{self.budget.max_visited} nodes); reachable entrypoints "
                "beyond it are not listed"
            )
        if self._ambiguous_edges:
            result.notes.append(
                f"{self._ambiguous_edges} edge(s) referenced a symbol with no "
                "qualified name and no line number; traversal did not continue "
                "past them rather than guessing which symbol they were"
            )
        return result

    # -- M3: the guide loop -------------------------------------------------

    def _run_guide_loop(
        self,
        candidates: list[tuple[dict[str, Any], str, SymbolIdentity,
                                list[tuple[SymbolIdentity, tuple[ImpactStep, ...]]]]],
        index: _FactIndex,
        found: dict[str, AffectedEntrypoint],
        result: ImpactResult,
        *,
        truncated_globally: bool,
        all_changed_names: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Ask the guide about unresolved symbols, bounded on every axis.

        Every resolution here goes back through `_reachability` — the exact
        deterministic method M2 already trusted — rather than being
        constructed by hand from a guide's say-so. The guide and its executor
        only ever add one thing to the graph (a source-confirmed edge) or
        widen one thing about the search (its depth, for one flagged symbol);
        the entrypoint classification, path bookkeeping, and provenance
        tagging are the same code as the fully deterministic path.
        """
        metrics: dict[str, Any] = {
            "guide_triggered": False,
            "guide_calls": 0,
            "guide_actions": {},
            "evidence_confirmed": 0,
            "guide_no_progress": 0,
            "guide_errors": 0,
            "guide_budget_exhausted": False,
            "unresolved_before": len(candidates),
            "unresolved_after": len(candidates),
            #: Wall time actually spent inside `guide.investigate()` calls —
            #: the only part of this loop an external provider dominates.
            #: Separate from total `interpret()` time so a slow run can be
            #: attributed to CBM/traversal vs. the guide.
            "guide_latency_ms": 0.0,
            #: Sanitized `str(GuideError)` messages, in order — a provider
            #: failure must be visible in diagnostics, not just counted.
            "guide_error_details": [],
            #: How many `ImpactCandidate`s the guide proposed in total, how
            #: many were accepted into `result.affected` (PROVEN duplicates
            #: are not — see `_record_inferred`), how many corroborated vs.
            #: not, and how many were genuinely new entrypoints (as opposed
            #: to adding a second changed-symbol reason to one already found).
            "llm_candidates": 0,
            "llm_candidates_accepted": 0,
            "llm_candidates_corroborated": 0,
            "llm_candidates_uncorroborated": 0,
            "llm_new_entrypoints": 0,
            #: A candidate that is obviously the changed symbol itself,
            #: rejected before corroboration/merge — see `_is_self_referential`.
            "llm_candidates_self_referential": 0,
            #: A candidate with no causal reason at all, rejected before
            #: corroboration/merge — an accepted inference must say why.
            "llm_candidates_missing_reason": 0,
            #: An INFER_IMPACT turn that explicitly proposed zero candidates —
            #: a legitimate, encouraged outcome ("no meaningful downstream
            #: impact"), never counted toward `guide_errors`.
            "llm_no_candidate_turns": 0,
        }
        if not candidates or self._guide is None or self._guide_policy == GUIDE_OFF:
            return metrics

        unresolved_by_name = {item.symbol: item for item in result.unresolved}
        total_budget = self._guide_budget.max_turns_total
        resolved_count = 0

        # M4.2: one whole-change semantic turn before any per-symbol turn —
        # see `_run_whole_change_pass`. It always consumes exactly one call
        # against the shared budget (never zero, never more), and may name
        # which of the still-unresolved symbols below are worth investigating
        # first; unlisted ones keep their original order.
        follow_up_symbols: tuple[str, ...] = ()
        if total_budget > 0:
            follow_up_symbols = self._run_whole_change_pass(
                candidates, index, found, result, metrics,
            )
            total_budget -= 1
        if follow_up_symbols:
            priority = {name: position for position, name in enumerate(follow_up_symbols)}
            candidates = sorted(
                candidates, key=lambda item: priority.get(item[1], len(priority))
            )

        for symbol, name, start_identity, dead_ends in candidates:
            if total_budget <= 0:
                metrics["guide_budget_exhausted"] = True
                break

            reason = _classify_unresolved_reason(
                dead_ends, start_identity, truncated_globally, index,
            )
            # `has_a_lead` used to gate whether AUTO even asked the guide:
            # under M3, the guide was a graph-navigation controller, and
            # "no dead end, no truncation, identity resolved" genuinely meant
            # there was nothing to navigate toward. Under M4 the guide's
            # primary action is semantic inference (INFER_IMPACT), which
            # needs the changed symbol's own behavior, not a graph lead — a
            # symbol changed by nothing but adding a decorator has no dead
            # end and no truncation, yet is exactly the case semantic
            # inference exists for. So AUTO no longer skips a turn for lacking
            # one; every unresolved symbol gets a bounded chance at semantic
            # inference regardless of policy, and the existing per-symbol/
            # total budgets remain the only bound on how many turns that costs.

            # `known` grounds `target` (any name/file surfaced so far, meaningful
            # or not — a pseudo node's *file* can still be a legitimate place to
            # look for the real relationship, which is exactly how the guide
            # gets from a dead end to a same-file real symbol via
            # INSPECT_NEARBY_ENTRYPOINTS). `frontier.candidate_origins` is the
            # separate, narrower set legal for `sought_symbol` — no position is
            # ever picked on the loop's behalf; the guide names its own target
            # each turn from what is actually offered.
            known: dict[str, SymbolIdentity] = {start_identity.short_name: start_identity}
            for ident, _trail in dead_ends:
                known.setdefault(ident.short_name, ident)
            frontier = _build_frontier(start_identity, dead_ends, index)

            executor = InvestigationExecutor(index=index, facts=index.facts, repo_root=self._repo_root)
            tried: set[tuple[str, str, str]] = set()
            per_symbol_budget = self._guide_budget.max_turns_per_symbol
            tried_summary: list[str] = []
            metrics["guide_triggered"] = True
            symbol_resolved = False
            # Computed once: the changed symbol's own source never changes
            # mid-loop, so there is no reason to re-read it every turn.
            preview = source_preview(start_identity, index.facts, self._repo_root)

            while per_symbol_budget > 0 and total_budget > 0 and not symbol_resolved:
                origin_names = sorted(
                    set(frontier.candidate_origins)
                    | {name_ for name_, ident in known.items() if index.is_meaningful_symbol(ident)}
                )
                origins = {name_: known[name_] for name_ in origin_names}
                question = _build_question(
                    start_identity, reason, dead_ends, known, origin_names, tried_summary, index,
                    source_context=preview,
                    remaining=min(per_symbol_budget, total_budget),
                    repo=index.repo_of(symbol),
                    other_changed_symbols=tuple(
                        n for n in all_changed_names if n != name
                    )[:_WHOLE_CHANGE_CONTEXT_CAP],
                    accepted_impacts_so_far=tuple(
                        entry.label for entry in found.values()
                    )[:_WHOLE_CHANGE_CONTEXT_CAP],
                )
                turn_started = time.monotonic()
                turn_number = len(tried_summary) + 1
                try:
                    decision = self._guide.investigate(question)
                except GuideError as exc:
                    metrics["guide_errors"] += 1
                    # A provider/parse failure must stay visible, not just
                    # counted — the caller decides what to do with it, but it
                    # is never silently discarded.
                    metrics["guide_error_details"].append(f"guide_error={exc}")
                    metrics["guide_latency_ms"] += (time.monotonic() - turn_started) * 1000
                    break
                metrics["guide_latency_ms"] += (time.monotonic() - turn_started) * 1000
                metrics["guide_calls"] += 1
                per_symbol_budget -= 1
                total_budget -= 1
                actions = metrics["guide_actions"]
                actions[decision.action] = actions.get(decision.action, 0) + 1

                if decision.action == ACTION_STOP_UNRESOLVED:
                    break

                if decision.action == ACTION_INFER_IMPACT:
                    progress_key = (
                        decision.action, "",
                        "|".join(sorted(c.entrypoint_label for c in decision.candidates)),
                    )
                    if progress_key in tried:
                        metrics["guide_no_progress"] += 1
                        break
                    tried.add(progress_key)
                    if decision.candidates:
                        tried_summary.append(f"infer_impact -> {len(decision.candidates)} candidate(s)")
                    else:
                        tried_summary.append("infer_impact -> no meaningful downstream impact inferred")
                        metrics["llm_no_candidate_turns"] += 1
                    # A recorded inferred entrypoint does not "resolve" the
                    # symbol in the PROVEN sense — `symbol_resolved` stays
                    # False, so the symbol remains visible in
                    # `result.unresolved` too (see below the loop), distinct
                    # from a deterministic resolution. The turn loop simply
                    # continues, bounded by the same budget as any other
                    # action.
                    self._apply_inferred_candidates(
                        decision.candidates, executor=executor, found=found,
                        result=result, name=name, turn=turn_number, metrics=metrics,
                        changed_qualified_name=start_identity.qualified_name,
                        changed_repo=index.repo_of(symbol),
                    )
                    continue

                progress_key = (decision.action, decision.target, decision.sought_symbol)
                if progress_key in tried:
                    metrics["guide_no_progress"] += 1
                    break
                tried.add(progress_key)

                evidence = executor.execute(decision, known=known, origins=origins)
                tried_summary.append(
                    f"{decision.action}({decision.target}"
                    f"{', seeking ' + decision.sought_symbol if decision.sought_symbol else ''}) -> "
                    f"{'found: ' + evidence.detail if evidence.found else 'nothing new'}"
                )
                if not evidence.found:
                    continue
                metrics["evidence_confirmed"] += 1

                if decision.action in (
                    ACTION_INSPECT_SYMBOL, ACTION_INSPECT_ENCLOSING_FUNCTION, ACTION_INSPECT_SOURCE_SPAN,
                ):
                    target_identity = known[decision.target]
                    sought_identity = origins[decision.sought_symbol]
                    index.add_confirmed_edge(
                        caller=target_identity, callee=sought_identity, evidence=evidence.detail,
                    )
                elif decision.action == ACTION_INSPECT_NEARBY_ENTRYPOINTS:
                    continue  # `known` was extended in place; ask again next turn
                # TRACE_CALLERS/TRACE_USAGES/FIND_*_REFERENCES found=True means
                # the graph already had this edge; only the bounded walk
                # never reached it. Nothing to add — only to look at again,
                # below, with more room.

                fresh_dead_ends: list[tuple[SymbolIdentity, tuple[ImpactStep, ...]]] = []
                newly_resolved = False
                for path, entry, _hit_limit in self._reachability_extended(symbol, index, fresh_dead_ends):
                    if entry is not None and path is not None:
                        self._record(found, entry, name, path)
                        newly_resolved = True
                if newly_resolved:
                    symbol_resolved = True
                    break
                if fresh_dead_ends:
                    dead_ends = fresh_dead_ends
                    for ident, _trail in dead_ends:
                        known.setdefault(ident.short_name, ident)
                    frontier = _build_frontier(start_identity, dead_ends, index)

            item = unresolved_by_name.get(name)
            if symbol_resolved:
                resolved_count += 1
                if item is not None and item in result.unresolved:
                    result.unresolved.remove(item)
            elif item is not None:
                item.guide_investigated = True
                if tried_summary:
                    item.detail = item.detail + " | guide investigated: " + "; ".join(tried_summary[-2:])

        metrics["unresolved_after"] = len(candidates) - resolved_count
        return metrics

    def _run_whole_change_pass(
        self,
        candidates: list[tuple[dict[str, Any], str, SymbolIdentity,
                                list[tuple[SymbolIdentity, tuple[ImpactStep, ...]]]]],
        index: _FactIndex,
        found: dict[str, AffectedEntrypoint],
        result: ImpactResult,
        metrics: dict[str, Any],
    ) -> tuple[str, ...]:
        """One INFER_IMPACT turn about the whole change, before any
        per-symbol turn — the fix for a loop that used to reason about
        changed symbols one at a time until its budget ran out, with no
        opportunity to notice a PR-wide theme spanning several of them.

        Reuses every existing mechanism: the same `ImpactQuestion`/
        `InvestigationDecision` types, the same `_apply_inferred_candidates`
        (self-reference rejection, corroboration, `llm_candidate_log`), the
        same guide. The only new things are the question's shape (the whole
        change, not one symbol — see `ImpactQuestion.is_whole_change`) and
        `InvestigationDecision.follow_up_symbols`, an optional, non-binding
        priority order for the per-symbol turns that follow.

        Always counts as exactly one call against the shared budget,
        regardless of outcome (including a provider error) — the caller
        already checked `total_budget > 0` before calling this.
        """
        metrics["guide_triggered"] = True
        unresolved_names = tuple(name for _symbol, name, _identity, _dead_ends in candidates)
        # A handful of changed-symbol source previews, not the whole diff —
        # the same per-symbol preview machinery, just sampled across symbols
        # instead of read once per turn.
        previews = []
        for _symbol, name, start_identity, _dead_ends in candidates[:5]:
            preview = source_preview(start_identity, index.facts, self._repo_root)
            if preview:
                previews.append(f"# {name}\n{preview}")
        question = ImpactQuestion(
            repo=index.repo_of({}),
            changed_symbol="(whole change)",
            qualified_name="",
            file="",
            reason=(
                f"{len(candidates)} changed symbol(s) in this PR have no established "
                "structural path to a known entrypoint"
            ),
            source_context="\n\n".join(previews),
            remaining_budget=self._guide_budget.max_turns_total,
            other_changed_symbols=unresolved_names[:_WHOLE_CHANGE_CONTEXT_CAP],
            accepted_impacts_so_far=tuple(
                entry.label for entry in found.values()
            )[:_WHOLE_CHANGE_CONTEXT_CAP],
            known_entrypoints=tuple(
                f"{entry.get('route_method')} {entry.get('route_path')}".strip()
                if entry.get("route_method") and entry.get("route_path")
                else str(entry.get("symbol") or "")
                for entry in index.entrypoints[:_WHOLE_CHANGE_CONTEXT_CAP]
            ),
            is_whole_change=True,
            unresolved_symbols=unresolved_names[:_WHOLE_CHANGE_CONTEXT_CAP],
        )

        turn_started = time.monotonic()
        try:
            decision = self._guide.investigate(question)
        except GuideError as exc:
            metrics["guide_calls"] += 1
            metrics["guide_errors"] += 1
            metrics["guide_error_details"].append(f"guide_error={exc}")
            metrics["guide_latency_ms"] += (time.monotonic() - turn_started) * 1000
            return ()
        metrics["guide_latency_ms"] += (time.monotonic() - turn_started) * 1000
        metrics["guide_calls"] += 1
        actions = metrics["guide_actions"]
        actions[decision.action] = actions.get(decision.action, 0) + 1

        if decision.action != ACTION_INFER_IMPACT:
            return ()
        if not decision.candidates:
            metrics["llm_no_candidate_turns"] += 1
            return decision.follow_up_symbols

        executor = InvestigationExecutor(index=index, facts=index.facts, repo_root=self._repo_root)
        # `changed_qualified_name=""`: there is no single changed symbol for
        # `_is_self_referential` to compare a label against on this turn, so
        # it never trivially rejects anything here — the whole-change
        # question does not restate any one symbol's name. `name` is a
        # readable marker, not a real symbol, for `llm_candidate_log`/
        # `changed_symbols` attribution.
        self._apply_inferred_candidates(
            decision.candidates, executor=executor, found=found, result=result,
            name="(whole change)", turn=1, metrics=metrics, changed_repo=index.repo_of({}),
        )
        return decision.follow_up_symbols

    def _reachability_extended(
        self, symbol: dict[str, Any], index: _FactIndex,
        dead_ends: list[tuple[SymbolIdentity, tuple[ImpactStep, ...]]],
    ) -> list[tuple[ImpactPath | None, dict[str, Any] | None, bool]]:
        """Re-run `_reachability` with a bounded, temporary depth increase.

        Used only after the guide loop adds or uncovers one more edge: the
        graph may now connect the changed symbol to an entrypoint a few hops
        further out than the default budget walks, and re-running with the
        default budget would hit the exact same wall it hit the first time.
        The increase is fixed and restored immediately after, never widened
        further and never left in place for the rest of `interpret`.
        """
        original = self.budget
        self.budget = TraversalBudget(
            max_depth=original.max_depth + 4,
            max_visited=original.max_visited,
            max_paths_per_entrypoint=original.max_paths_per_entrypoint,
        )
        try:
            return self._reachability(symbol, index, dead_ends=dead_ends)
        finally:
            self.budget = original

    @staticmethod
    def _apply_inferred_candidates(
        candidates: tuple[ImpactCandidate, ...],
        *,
        executor: InvestigationExecutor,
        found: dict[str, AffectedEntrypoint],
        result: ImpactResult,
        name: str,
        turn: int,
        metrics: dict[str, Any],
        changed_qualified_name: str = "",
        changed_repo: str = "",
    ) -> None:
        """Corroborate and merge every candidate from one INFER_IMPACT turn.

        Corroboration (`InvestigationExecutor.corroborate_candidates`) is
        cheap and never a new search — see its own docstring. Every
        candidate is logged into `result.llm_candidate_log` regardless of
        outcome, and every candidate not already dominated by a PROVEN
        record is merged into `found` as `IMPACT_STATUS_INFERRED` — nothing
        proposed here is silently dropped, which is the exact failure mode
        this action exists to fix.

        Two things *are* rejected before any of that:
        - a candidate that is obviously the changed symbol itself (see
          `_is_self_referential`) — that is not a downstream impact, it is
          the guide restating its own input, and M4's "never drop a
          meaningful inference" guarantee was never meant to protect that;
        - a candidate with no causal reason at all — an accepted inference
          with nothing explaining *why* it might be affected is not
          meaningfully different from a guess, and a reviewer reading
          AFFECTED BEHAVIOR needs the "why" more than the "what."
        """
        corroborations = executor.corroborate_candidates(candidates)
        metrics["llm_candidates"] += len(candidates)
        for candidate, corroboration in zip(candidates, corroborations):
            if _is_self_referential(candidate, name, changed_qualified_name):
                metrics["llm_candidates_self_referential"] += 1
                result.llm_candidate_log.append({
                    "changed_symbol": name,
                    "turn": turn,
                    "candidate_entrypoint": candidate.entrypoint_label,
                    "candidate_symbol": candidate.entrypoint_symbol,
                    "confidence": candidate.confidence,
                    "rationale": candidate.reason,
                    "inference_type": candidate.inference_type,
                    "uncertainty": candidate.uncertainty,
                    "corroborated": False,
                    "corroboration_evidence": "",
                    "accepted": False,
                    "rejection_reason": (
                        "self_referential: candidate restates the changed symbol "
                        "itself rather than naming a downstream affected behavior"
                    ),
                })
                continue

            if not candidate.reason.strip():
                metrics["llm_candidates_missing_reason"] += 1
                result.llm_candidate_log.append({
                    "changed_symbol": name,
                    "turn": turn,
                    "candidate_entrypoint": candidate.entrypoint_label,
                    "candidate_symbol": candidate.entrypoint_symbol,
                    "confidence": candidate.confidence,
                    "rationale": candidate.reason,
                    "inference_type": candidate.inference_type,
                    "uncertainty": candidate.uncertainty,
                    "corroborated": False,
                    "corroboration_evidence": "",
                    "accepted": False,
                    "rejection_reason": "missing_reason: candidate had no causal reason to accept",
                })
                continue

            if corroboration["corroborated"]:
                metrics["llm_candidates_corroborated"] += 1
            else:
                metrics["llm_candidates_uncorroborated"] += 1

            is_new = ImpactInterpreter._record_inferred(found, candidate, corroboration, name, changed_repo)
            key = corroboration["qualified_name"] or corroboration["symbol"] or candidate.entrypoint_label
            existing = found.get(key)
            accepted = existing is not None and existing.status == IMPACT_STATUS_INFERRED
            if accepted:
                metrics["llm_candidates_accepted"] += 1
                if is_new:
                    metrics["llm_new_entrypoints"] += 1
            rejection_reason = "" if accepted else "a PROVEN record for this entrypoint already exists"

            result.llm_candidate_log.append({
                "changed_symbol": name,
                "turn": turn,
                "candidate_entrypoint": candidate.entrypoint_label,
                "candidate_symbol": candidate.entrypoint_symbol,
                "confidence": candidate.confidence,
                "rationale": candidate.reason,
                "inference_type": candidate.inference_type,
                "uncertainty": candidate.uncertainty,
                "corroborated": corroboration["corroborated"],
                "corroboration_evidence": corroboration["detail"],
                "accepted": accepted,
                "rejection_reason": rejection_reason,
            })

    # -- strategies -------------------------------------------------------

    def _direct(
        self, symbol: dict[str, Any], index: _FactIndex
    ) -> list[tuple[ImpactPath, dict[str, Any]]]:
        """DIRECT_ENTRYPOINT: the changed symbol *is* an entrypoint."""
        out = []
        for entry in index.entrypoints_named(str(symbol["name"]), symbol.get("file")):
            step = ImpactStep(
                symbol=entry["symbol"],
                qualified_name=entry.get("qualified_name", ""),
                file=entry.get("file", ""),
                relation=RELATION_DIRECT,
                evidence=_route_evidence(entry),
            )
            out.append((ImpactPath((step,), STRATEGY_DIRECT_ENTRYPOINT), entry))
        return out

    def _reachability(
        self, symbol: dict[str, Any], index: _FactIndex,
        *, dead_ends: list[tuple[SymbolIdentity, tuple[ImpactStep, ...]]] | None = None,
    ) -> list[tuple[ImpactPath | None, dict[str, Any] | None, bool]]:
        """CALL_REACHABILITY and USAGE_REACHABILITY.

        One bounded backward walk over both relationship kinds. They share a
        traversal because a real path can alternate — a changed class is *used*
        by a composing function which is *called* by a handler — and walking
        them separately would miss exactly those mixed paths.

        The walk is keyed by `SymbolIdentity`, never by bare name: adjacency
        that collapsed every symbol named `update` into one bucket is exactly
        the defect this replaces. An identity that resolved only through the
        tier-3 fallback (no qualified name, no line) is a leaf — traversal
        does not continue past it, because a further inbound edge for "some
        `update` in this file" cannot be trusted to belong to the same symbol
        as the one just reached.

        Only entrypoints terminate a path. Ordinary intermediate callers are
        recorded as steps, never reported as affected APIs in their own right.

        `dead_ends`, when given, collects every `(identity, trail)` visited
        without reaching an entrypoint. It costs nothing when the guide is
        off (callers simply pass `None`) and is the only view the M3 guide
        loop gets into "how far the deterministic walk actually got" for a
        symbol it could not fully resolve.
        """
        start = index.identity_of(symbol)
        results: list[tuple[ImpactPath | None, dict[str, Any] | None, bool]] = []
        visited: set[str] = {start.key}
        queue: deque[tuple[SymbolIdentity, tuple[ImpactStep, ...]]] = deque([(start, ())])
        hit_limit = False
        ambiguous_hits = 0

        while queue:
            if len(visited) > self.budget.max_visited:
                hit_limit = True
                break
            current, trail = queue.popleft()
            if dead_ends is not None and trail:
                dead_ends.append((current, trail))
            if len(trail) >= self.budget.max_depth:
                # Reaching the depth bound is only a truncation if this node
                # still had unexplored inbound edges.
                if index.inbound(current):
                    hit_limit = True
                continue
            if not current.resolved and trail:
                # An ambiguous node contributed its own step already; walking
                # further from it would extend a path built on a guess.
                continue

            for relation, predecessor_identity, predecessor in index.inbound(current):
                if predecessor_identity.key in visited:
                    continue  # cycle guard
                visited.add(predecessor_identity.key)
                if not predecessor_identity.resolved:
                    ambiguous_hits += 1
                step = ImpactStep(
                    symbol=predecessor_identity.short_name,
                    qualified_name=predecessor_identity.qualified_name,
                    file=predecessor_identity.file,
                    relation=relation,
                    evidence=predecessor.get("evidence", ""),
                    identity_resolved=predecessor_identity.resolved,
                    provenance=(
                        PROVENANCE_LLM_GUIDED_SOURCE_CONFIRMED
                        if relation == RELATION_SOURCE_CONFIRMED
                        else PROVENANCE_DETERMINISTIC
                    ),
                )
                extended = trail + (step,)
                entry = index.entrypoint_for_identity(predecessor_identity)
                if entry is not None:
                    if any(item.relation == RELATION_SOURCE_CONFIRMED for item in extended):
                        strategy = STRATEGY_GUIDED_INVESTIGATION
                    elif all(item.relation == RELATION_CALLS for item in extended):
                        strategy = STRATEGY_CALL_REACHABILITY
                    else:
                        strategy = STRATEGY_USAGE_REACHABILITY
                    results.append((ImpactPath(extended, strategy), entry, False))
                    # An entrypoint terminates this path; anything reaching it
                    # from further out is a different, longer story.
                    continue

                # A symbol reached mid-traversal may itself be named in a
                # decorator, which is how a dependency composed inside another
                # dependency reaches its handlers. Checking only the originally
                # changed symbol would miss every composed case, because the
                # new symbol never appears in a decorator itself.
                for referrer in index.entrypoints_referencing_any(
                    _reference_names(predecessor_identity)
                ):
                    reference_step = ImpactStep(
                        symbol=referrer["symbol"],
                        qualified_name=referrer.get("qualified_name", ""),
                        file=referrer.get("file", ""),
                        relation=RELATION_DECORATOR_REFERENCE,
                        evidence=_decorator_evidence(
                            referrer.get("decorators", ""), predecessor_identity.short_name
                        ),
                    )
                    results.append((
                        ImpactPath(extended + (reference_step,),
                                   STRATEGY_DECORATOR_REFERENCE),
                        referrer,
                        False,
                    ))

                queue.append((predecessor_identity, extended))

        if hit_limit:
            results.append((None, None, True))
        if ambiguous_hits:
            # Surfaced through the metrics/notes path in `interpret`, not as a
            # fabricated result — an ambiguous hop is evidence of uncertainty,
            # not evidence of an entrypoint.
            self._ambiguous_edges += ambiguous_hits
        return results

    def _decorator_references(
        self, symbol: dict[str, Any], index: _FactIndex
    ) -> list[tuple[ImpactPath, dict[str, Any]]]:
        """DECORATOR_REFERENCE: the changed symbol is named in a decorator.

        Generic by construction: it looks for candidate identifiers inside
        whatever decorator text the backend captured, without knowing what any
        decorator means. A dependency declared in a decorator argument never
        produces a call edge, so this is the only structural trace of it.

        A changed symbol that is a class method is attributed by its own
        short name (`has_required_permissions`), but a decorator referencing
        that dependency names the *class* (`SomePermission`), never the
        method. `_reference_names` supplies both candidates from the same
        qualified-name split the traversal-time lookup already uses, so a
        diff hunk landing on a method still finds the class its decorator
        argument would have named.
        """
        identity = index.identity_of(symbol)
        seen: dict[str, dict[str, Any]] = {}
        for name in _reference_names(identity):
            for entry in index.entrypoints_referencing(name):
                key = entry.get("qualified_name") or entry["symbol"]
                seen.setdefault(key, entry)
        out = []
        for entry in seen.values():
            matched = next(
                (name for name in _reference_names(identity)
                 if name in _IDENTIFIER_RE.findall(str(entry.get("decorators") or ""))),
                identity.short_name,
            )
            step = ImpactStep(
                symbol=entry["symbol"],
                qualified_name=entry.get("qualified_name", ""),
                file=entry.get("file", ""),
                relation=RELATION_DECORATOR_REFERENCE,
                evidence=_decorator_evidence(entry.get("decorators", ""), matched),
            )
            out.append((ImpactPath((step,), STRATEGY_DECORATOR_REFERENCE), entry))
        return out

    def _signature_references(
        self, symbol: dict[str, Any], index: _FactIndex
    ) -> list[tuple[ImpactPath, dict[str, Any]]]:
        """SIGNATURE_REFERENCE: a changed type named in an entrypoint signature.

        Conservative on purpose. It fires only when the backend actually
        recorded a signature containing the changed identifier — no ORM
        semantics are inferred, because CBM exposes none and inventing them
        would produce confident nonsense about persistence. Candidate names
        include the owning class alongside the symbol's own name, for the
        same reason `_decorator_references` does: a type used in a handler
        signature is named by class, and a changed method is attributed by
        its own short name.
        """
        identity = index.identity_of(symbol)
        seen: dict[str, dict[str, Any]] = {}
        for name in _reference_names(identity):
            for entry in index.entrypoints_with_signature_reference(name):
                key = entry.get("qualified_name") or entry["symbol"]
                seen.setdefault(key, entry)
        out = []
        for entry in seen.values():
            matched = next(
                (name for name in _reference_names(identity)
                 if name in _IDENTIFIER_RE.findall(str(entry.get("signature") or ""))),
                identity.short_name,
            )
            step = ImpactStep(
                symbol=entry["symbol"],
                qualified_name=entry.get("qualified_name", ""),
                file=entry.get("file", ""),
                relation=RELATION_SIGNATURE_REFERENCE,
                evidence=_signature_evidence(entry.get("signature", ""), matched),
            )
            out.append((ImpactPath((step,), STRATEGY_SIGNATURE_REFERENCE), entry))
        return out

    # -- accumulation -----------------------------------------------------

    @staticmethod
    def _record(
        found: dict[str, AffectedEntrypoint],
        entry: dict[str, Any],
        changed_symbol: str,
        path: ImpactPath,
        *,
        status: str = IMPACT_STATUS_PROVEN,
    ) -> None:
        """Record one deterministically-reached entrypoint.

        `status` defaults to `IMPACT_STATUS_PROVEN` for every strategy except
        `_signature_references` — a changed *type* merely named in an
        entrypoint's signature is the weakest evidence this module produces
        (no call, no usage, no decorator reference: just a name appearing in
        a type annotation), and must not read as established behavioral
        impact on its own. If a *stronger* path later reaches the same
        entrypoint (a real call/usage/decorator-reference from a different
        changed symbol), it upgrades an existing weak record to PROVEN —
        never the reverse: nothing here ever downgrades an already-PROVEN
        entry.
        """
        key = entry.get("qualified_name") or entry["symbol"]
        entrypoint = found.get(key)
        if entrypoint is None:
            entrypoint = AffectedEntrypoint(
                repo=entry.get("repo", ""),
                symbol=entry["symbol"],
                qualified_name=entry.get("qualified_name", ""),
                file=entry.get("file", ""),
                kind=_classify(entry),
                route_method=entry.get("route_method"),
                route_path=entry.get("route_path"),
                status=status,
            )
            found[key] = entrypoint
        elif status == IMPACT_STATUS_PROVEN and entrypoint.status != IMPACT_STATUS_PROVEN:
            entrypoint.status = IMPACT_STATUS_PROVEN
        entrypoint.changed_symbols.append(changed_symbol)
        if not any(existing.describe() == path.describe()
                   and existing.strategy == path.strategy
                   for existing in entrypoint.paths):
            entrypoint.paths.append(path)

    @staticmethod
    def _record_inferred(
        found: dict[str, AffectedEntrypoint],
        candidate: ImpactCandidate,
        corroboration: dict[str, Any],
        changed_symbol: str,
        changed_repo: str = "",
    ) -> bool:
        """Merge one LLM candidate into `found`, honoring PROVEN dominance.

        Uses the exact same key scheme as `_record` (`qualified_name` first,
        then `symbol`) so a corroborated candidate — which carries the real
        entry's own qualified_name/symbol — naturally lands on the same key
        a deterministic record for it would. Returns whether this candidate
        introduced a new entrypoint into `found` (for `llm_new_entrypoints`).
        """
        symbol = corroboration["symbol"] or candidate.entrypoint_label
        qualified_name = corroboration["qualified_name"]
        key = qualified_name or symbol
        existing = found.get(key)
        if existing is not None and existing.status == IMPACT_STATUS_PROVEN:
            # PROVEN already carries a deterministic path; an inferred
            # duplicate would add nothing but noise. The candidate is still
            # visible in guide diagnostics — only the merged result is skipped.
            return False

        is_new = existing is None
        if is_new:
            entrypoint = AffectedEntrypoint(
                # Corroboration only knows a repo when it matched a real,
                # already-known entrypoint (`corroboration["repo"]`); an
                # uncorroborated candidate has no such match, but the
                # changed symbol that produced this inference always has a
                # real repo of its own — using it here (never inventing
                # anything else) is what keeps downstream ids from
                # collapsing to `impact::symbol` instead of `impact:app:symbol`.
                repo=corroboration.get("repo") or changed_repo,
                symbol=symbol, qualified_name=qualified_name,
                file=corroboration["file"], kind=corroboration["kind"],
                route_method=corroboration["route_method"], route_path=corroboration["route_path"],
                status=IMPACT_STATUS_INFERRED,
            )
            found[key] = entrypoint
        else:
            entrypoint = existing

        entrypoint.changed_symbols.append(changed_symbol)
        entrypoint.llm_confidence = max(entrypoint.llm_confidence or 0.0, candidate.confidence)
        entrypoint.llm_reason = entrypoint.llm_reason or candidate.reason
        entrypoint.llm_inference_type = entrypoint.llm_inference_type or candidate.inference_type
        entrypoint.llm_uncertainty = entrypoint.llm_uncertainty or candidate.uncertainty
        entrypoint.corroborated = entrypoint.corroborated or corroboration["corroborated"]

        step = ImpactStep(
            symbol=symbol, qualified_name=qualified_name, file=corroboration["file"],
            relation=RELATION_LLM_INFERRED, evidence=candidate.reason,
            provenance=(
                PROVENANCE_LLM_INFERRED_CORROBORATED if corroboration["corroborated"]
                else PROVENANCE_LLM_INFERRED_UNCORROBORATED
            ),
        )
        path = ImpactPath((step,), STRATEGY_LLM_SEMANTIC_INFERENCE)
        if not any(existing_path.describe() == path.describe() for existing_path in entrypoint.paths):
            entrypoint.paths.append(path)
        return is_new


def _classify(entry: dict[str, Any]) -> str:
    """An entrypoint's kind, only as far as the facts support.

    Route metadata makes it HTTP. Decorator text alone makes it *some* kind of
    declared entrypoint without saying which. Neither makes it unknown — and
    unknown is reported as unknown rather than assumed to be HTTP.
    """
    if entry.get("route_path") or entry.get("route_method"):
        return ENTRYPOINT_HTTP
    if entry.get("decorators"):
        return ENTRYPOINT_DECORATED
    return ENTRYPOINT_UNKNOWN


def _reference_names(identity: SymbolIdentity) -> list[str]:
    """Names under which a reached symbol might be cited in a decorator.

    A dependency is usually declared by its class name while the symbol the
    graph reached is a method on that class, so the owning class is offered
    alongside the method's own name.
    """
    names = [identity.short_name]
    parts = identity.qualified_name.split(".")
    if len(parts) >= 2 and parts[-2] and parts[-2] != identity.short_name:
        names.append(parts[-2])
    return [name for name in names if name not in _UNINFORMATIVE and len(name) >= 3]


def _route_evidence(entry: dict[str, Any]) -> str:
    method = entry.get("route_method") or ""
    path = entry.get("route_path") or ""
    return f"{method} {path}".strip()


def _decorator_evidence(text: str, name: str) -> str:
    """The decorator fragment naming this symbol, for a reviewer to check."""
    for fragment in str(text).split("@"):
        if name in fragment:
            return ("@" + fragment.strip())[:200]
    return str(text)[:200]


def _signature_evidence(text: str, name: str) -> str:
    signature = str(text)
    position = signature.find(name)
    if position < 0:
        return signature[:200]
    return signature[max(0, position - 40): position + 60]


def _normalize_for_self_reference(text: str) -> str:
    """Collapse whitespace/punctuation/case so `"tool.ruff.lint"`,
    `"tool_ruff_lint"`, and `" Tool.Ruff.Lint "` all compare equal — this is
    only meant to catch the changed symbol restated in a slightly different
    surface form, not to normalize meaning."""
    return re.sub(r"[\s\-_/.]+", "", text.strip().lower())


def _is_self_referential(candidate: ImpactCandidate, changed_name: str, changed_qualified_name: str) -> bool:
    """Reject only the obvious case: the candidate's *label* — the actual
    behavior description a reviewer would read — is nothing but the changed
    symbol's own name restated, under a name/whitespace/punctuation
    normalization. Never a semantic judgement about whether the label is a
    meaningful downstream behavior; that judgement belongs to the guide's
    own contract (the prompt), not to a deterministic classifier here.

    Deliberately checks only `entrypoint_label`, not `entrypoint_symbol`.
    `entrypoint_symbol` is guided to be one of the exact known names the
    question already listed — and the changed symbol's own short name is
    legitimately one of those (`_build_question` seeds `known` with it) —
    so a candidate is allowed to cite the changed symbol as its own nearest
    known anchor while its label still describes a distinct downstream
    behavior (e.g. changed symbol `Order.set_expires`, label "order expiry
    calculation now varies by sales channel"): rejecting on the symbol
    field alone would discard exactly that kind of legitimate candidate.
    """
    changed = {_normalize_for_self_reference(changed_name)}
    if changed_qualified_name:
        changed.add(_normalize_for_self_reference(changed_qualified_name))
    changed.discard("")

    label_norm = _normalize_for_self_reference(candidate.entrypoint_label)
    return bool(label_norm) and label_norm in changed


def _classify_unresolved_reason(
    dead_ends: list[tuple[SymbolIdentity, tuple[ImpactStep, ...]]],
    identity: SymbolIdentity,
    truncated_globally: bool,
    index: _FactIndex,
) -> str:
    """Which of the five guide-trigger reasons best describes this gap.

    Order matters: truncation and identity ambiguity are checked first
    because they point at a specific, fixable cause; a dead end with no
    named cause falls through to the more generic reasons.
    """
    if truncated_globally:
        return REASON_TRAVERSAL_TRUNCATED
    if not identity.resolved:
        return REASON_AMBIGUOUS_SYMBOL_IDENTITY
    # Prefer a real, defined symbol for the ambiguity check over whatever
    # dead end happened to be visited last — a pseudo/attribute-like node
    # can't meaningfully collide with "another symbol of the same name."
    meaningful = [ident for ident, _trail in dead_ends if index.is_meaningful_symbol(ident)]
    deepest = meaningful[-1] if meaningful else (dead_ends[-1][0] if dead_ends else None)
    if deepest is not None and index.candidate_count_for(deepest) > 1:
        return REASON_MULTIPLE_UNRESOLVED_CANDIDATES
    if dead_ends:
        return REASON_PARTIAL_PATH_DEAD_END
    return REASON_NO_ENTRYPOINT_REACHED


def _strip_checkout_prefix(qualified_name: str, file: str) -> str:
    """Drop whatever project/checkout-path segment prefixes a qualified name.

    CBM derives its project identifier from the indexed repository's own
    path, so a qualified name for a symbol in `a/b/c.py` reads
    `<opaque-project-id>.a.b.c.<symbol>`. The identifier varies by checkout
    and is never meaningful to a guide; the dotted form of the symbol's own
    file always appears right after it, so finding *that* substring and
    keeping everything from there on strips the noise without knowing
    anything about how the prefix was built. Purely cosmetic: canonical
    identity (`SymbolIdentity.key`) never goes through this.
    """
    if not qualified_name or not file:
        return qualified_name
    dotted_file = file.rsplit(".", 1)[0].replace("/", ".")
    if not dotted_file:
        return qualified_name
    index = qualified_name.find(dotted_file)
    return qualified_name[index:] if index > 0 else qualified_name


def _display_label(identity: SymbolIdentity) -> str:
    """Guide-facing rendering of an identity: cleaned qualified name if
    known, else the short name. Never used for identity/equality."""
    cleaned = _strip_checkout_prefix(identity.qualified_name, identity.file)
    return cleaned or identity.short_name or identity.label


def _display_step(step: ImpactStep) -> str:
    """Guide-facing rendering of one traversal step, prefix-stripped."""
    cleaned = _strip_checkout_prefix(step.qualified_name, step.file)
    return f"{step.relation}:{cleaned or step.symbol}"


def _build_frontier(
    start_identity: SymbolIdentity,
    dead_ends: list[tuple[SymbolIdentity, tuple[ImpactStep, ...]]],
    index: _FactIndex,
) -> UnresolvedFrontier:
    """Describe one changed symbol's unresolved reach without picking an
    origin. Every dead end the deterministic walk actually visited is
    classified once, by whether it is backed by a real definition — never by
    where it happened to land in the traversal order."""
    depth_of: dict[str, int] = {}
    meaningful: list[SymbolIdentity] = []
    pseudo: list[SymbolIdentity] = []
    seen: set[str] = set()
    for identity, trail in dead_ends:
        if identity.short_name in seen:
            continue
        seen.add(identity.short_name)
        depth_of[identity.short_name] = len(trail)
        (meaningful if index.is_meaningful_symbol(identity) else pseudo).append(identity)
    meaningful.sort(key=lambda ident: (-depth_of.get(ident.short_name, 0), ident.short_name))

    partial_paths = tuple(
        " -> ".join(_display_step(step) for step in trail) or _display_label(identity)
        for identity, trail in dead_ends[-5:]
    )
    candidate_origins = tuple(ident.short_name for ident in meaningful) or (start_identity.short_name,)
    return UnresolvedFrontier(
        start_symbol=start_identity.short_name,
        frontier_nodes=tuple(ident.short_name for ident in meaningful),
        partial_paths=partial_paths,
        pseudo_or_low_value_nodes=tuple(ident.short_name for ident in pseudo),
        candidate_origins=candidate_origins,
    )


def _build_question(
    start_identity: SymbolIdentity,
    reason: str,
    dead_ends: list[tuple[SymbolIdentity, tuple[ImpactStep, ...]]],
    known: dict[str, SymbolIdentity],
    candidate_origin_names: list[str],
    attempted_actions: list[str],
    index: _FactIndex,
    *,
    source_context: str,
    remaining: int,
    repo: str,
    other_changed_symbols: tuple[str, ...] = (),
    accepted_impacts_so_far: tuple[str, ...] = (),
) -> ImpactQuestion:
    """Render what the deterministic pass already found as an `ImpactQuestion`.

    Every field is a plain string or tuple built from facts already in
    `dead_ends`/`known`/`index` — nothing here queries CBM or a file (source
    reading, when it happens, is done once by the caller and passed in as
    `source_context`), so building a question costs nothing beyond string
    formatting and a handful of lookups already held in memory.
    """
    partial_paths = tuple(
        " -> ".join(_display_step(step) for step in trail) or _display_label(identity)
        for identity, trail in dead_ends[-5:]
    )
    known_files = tuple(sorted({ident.file for ident in known.values() if ident.file}))
    known_entrypoints = tuple(sorted(
        name for name, ident in known.items() if index.entrypoint_for_identity(ident) is not None
    ))
    candidate_entrypoints = tuple(sorted(known.keys()))
    return ImpactQuestion(
        repo=repo,
        changed_symbol=start_identity.short_name,
        qualified_name=_strip_checkout_prefix(start_identity.qualified_name, start_identity.file),
        file=start_identity.file,
        reason=reason,
        partial_paths=partial_paths,
        known_files=known_files,
        known_entrypoints=known_entrypoints,
        attempted_actions=tuple(attempted_actions),
        candidate_entrypoints=candidate_entrypoints,
        candidate_origins=tuple(candidate_origin_names),
        source_context=source_context,
        remaining_budget=remaining,
        other_changed_symbols=other_changed_symbols,
        accepted_impacts_so_far=accepted_impacts_so_far,
    )


def _identity_of_entry(entry: dict[str, Any], default_repo: str | None) -> SymbolIdentity:
    """The identity of a fact-index entry (entrypoint, edge endpoint, ...)."""
    return SymbolIdentity.from_fields(
        repo=str(entry.get("repo") or default_repo or ""),
        file=str(entry.get("file") or ""),
        qualified_name=entry.get("qualified_name"),
        short_name=entry.get("symbol"),
        line=entry.get("line"),
    )


class _FactIndex:
    """Lookup structures over one `StructuralFacts`, built once per interpret.

    Every lookup here is keyed by `SymbolIdentity.key`, never by bare short
    name. Short names remain available on entrypoint dicts purely for display
    and for the decorator/signature text scans, which search identifier text
    rather than adjacency and are unaffected by the name-collision defect this
    index exists to fix.
    """

    def __init__(self, facts: StructuralFacts, repo: str | None) -> None:
        self.facts = facts
        self._repo = repo
        self.entrypoints: list[dict[str, Any]] = [
            entry for entry in facts.entrypoints
            if repo is None or entry.get("repo") == repo
        ]

        # Bare-name lookup survives only for DIRECT_ENTRYPOINT, where the
        # caller already narrows by file and a same-file collision is rare
        # enough that falling back to "ambiguous, do not guess" is enough.
        self._by_name: dict[str, list[dict[str, Any]]] = {}
        self._by_identity: dict[str, dict[str, Any]] = {}
        for entry in self.entrypoints:
            self._by_name.setdefault(entry["symbol"], []).append(entry)
            self._by_identity[_identity_of_entry(entry, repo).key] = entry

        # (file, short_name) -> qualified_name, learned from every identity
        # this index has seen. A changed symbol commonly arrives with no
        # qualified name of its own — plain functions carry none in either
        # Sydes' native attribution or CBM's own symbol translation — while
        # CBM's edges almost always carry one for the same physical symbol.
        # Without this table the two identities for one symbol would resolve
        # to different tiers and simply never meet. This is exact lookup
        # against facts already collected, not a guess: a (file, name) pair
        # that maps to more than one qualified name is left unresolved rather
        # than picked between.
        self._known_qualified: dict[tuple[str, str], str | None] = {}

        def _learn(file: str, name: str, qualified: str) -> None:
            if not file or not name or not qualified:
                return
            fkey = (file, name)
            existing = self._known_qualified.get(fkey, "")
            if existing == "":
                self._known_qualified[fkey] = qualified
            elif existing is not None and existing != qualified:
                self._known_qualified[fkey] = None  # genuinely ambiguous

        for entry in self.entrypoints:
            _learn(str(entry.get("file") or ""), str(entry.get("symbol") or ""),
                   str(entry.get("qualified_name") or ""))
        for edge in facts.call_edges:
            if repo is not None and edge.get("repo") != repo:
                continue
            _learn(str(edge.get("caller_file") or ""), str(edge.get("caller_symbol") or ""),
                   str(edge.get("caller_qualified_name") or ""))
            _learn(str(edge.get("callee_file") or ""), str(edge.get("callee_symbol") or ""),
                   str(edge.get("callee_qualified_name") or ""))
        for edge in facts.usage_edges:
            if repo is not None and edge.get("repo") != repo:
                continue
            _learn(str(edge.get("user_file") or ""), str(edge.get("user_symbol") or ""),
                   str(edge.get("user_qualified_name") or ""))
            _learn(str(edge.get("used_file") or ""), str(edge.get("used_symbol") or ""),
                   str(edge.get("used_qualified_name") or ""))

        # Inbound adjacency, keyed by the *identity* of the callee/used
        # symbol, combining both relationship kinds so one traversal can
        # alternate between them. This is the fix: the previous key was the
        # bare symbol string, which merged every `update` in the repository
        # into one adjacency bucket regardless of file or class.
        self._inbound: dict[str, list[tuple[str, SymbolIdentity, dict[str, Any]]]] = {}
        for edge in facts.call_edges:
            if repo is not None and edge.get("repo") != repo:
                continue
            callee = SymbolIdentity.from_fields(
                repo=str(repo or edge.get("repo") or ""),
                file=str(edge.get("callee_file") or ""),
                qualified_name=edge.get("callee_qualified_name"),
                short_name=edge.get("callee_symbol"),
                line=edge.get("callee_line"),
            )
            caller = SymbolIdentity.from_fields(
                repo=str(repo or edge.get("repo") or ""),
                file=str(edge.get("caller_file") or ""),
                qualified_name=edge.get("caller_qualified_name"),
                short_name=edge.get("caller_symbol"),
                line=edge.get("caller_line"),
            )
            self._inbound.setdefault(callee.key, []).append((RELATION_CALLS, caller, {}))
        for edge in facts.usage_edges:
            if repo is not None and edge.get("repo") != repo:
                continue
            used = SymbolIdentity.from_fields(
                repo=str(repo or edge.get("repo") or ""),
                file=str(edge.get("used_file") or ""),
                qualified_name=edge.get("used_qualified_name"),
                short_name=edge.get("used_symbol"),
            )
            user = SymbolIdentity.from_fields(
                repo=str(repo or edge.get("repo") or ""),
                file=str(edge.get("user_file") or ""),
                qualified_name=edge.get("user_qualified_name"),
                short_name=edge.get("user_symbol"),
            )
            self._inbound.setdefault(used.key, []).append((RELATION_USAGE, user, {}))
        # Stable adjacency order keeps traversal deterministic.
        for key, items in self._inbound.items():
            self._inbound[key] = sorted(
                items, key=lambda item: (item[0], item[1].key)
            )

    def repo_of(self, symbol: dict[str, Any]) -> str:
        return str(symbol.get("repo") or self._repo or "")

    def identity_of(self, symbol: dict[str, Any]) -> SymbolIdentity:
        """The identity of a changed symbol.

        A caller-supplied qualified name is trusted only when it is actually
        qualified — Sydes' own change attribution falls back to the bare short
        name when no real one is known (`qualified_name = name`), and a bare
        name is exactly the collision this type exists to prevent: nothing in
        CBM's facts uses an undotted string as a qualified name, so trusting
        it verbatim would resolve to an identity that matches no edge at all.
        A qualified name without a `.` is therefore treated as absent, and the
        (file, short_name) pair is checked instead against every qualified
        name this index has already observed for that exact pair — an exact
        lookup against collected facts, not a guess — before falling back to
        the line-scoped or file-scoped tiers.
        """
        file = str(symbol.get("file") or "")
        name = str(symbol.get("name") or "")
        qualified = str(symbol.get("qualified_name") or "")
        if "." not in qualified:
            qualified = ""
        if not qualified:
            learned = self._known_qualified.get((file, name))
            if learned:
                qualified = learned
        return SymbolIdentity.from_fields(
            repo=self.repo_of(symbol),
            file=file,
            qualified_name=qualified,
            short_name=name,
            line=symbol.get("start_line") or symbol.get("line"),
        )

    def inbound(
        self, identity: SymbolIdentity
    ) -> list[tuple[str, SymbolIdentity, dict[str, Any]]]:
        return self._inbound.get(identity.key, [])

    def add_confirmed_edge(
        self, *, caller: SymbolIdentity, callee: SymbolIdentity, evidence: str,
    ) -> None:
        """Insert one inbound edge the graph never reported, as CBM would have.

        The only mutation in this index, and only ever called by the M3
        guide loop after source inspection — never the graph loader, never
        the guide itself — has confirmed the reference in text. Tagged with
        `RELATION_SOURCE_CONFIRMED` so every step built from it downstream
        stays distinguishable from a graph-derived one.
        """
        self._inbound.setdefault(callee.key, []).append(
            (RELATION_SOURCE_CONFIRMED, caller, {"evidence": evidence})
        )

    def candidate_count_for(self, identity: SymbolIdentity) -> int:
        """How many same-file, same-name entrypoints an unresolved identity
        could plausibly mean. Used only to label *why* a symbol is
        unresolved for the guide — never to pick between them."""
        if identity.resolved:
            return 0
        candidates = self._by_name.get(identity.short_name, [])
        if identity.file:
            candidates = [c for c in candidates if c.get("file") == identity.file]
        return len(candidates)

    def is_meaningful_symbol(self, identity: SymbolIdentity) -> bool:
        """Whether this node is backed by an actual definition — a function,
        method, or class Sydes has a real span for — as opposed to a
        synthetic or attribute-like reference (a module dunder, an import
        target, anything CBM's usage extraction surfaced without a body).

        Structural, never name-based: this is a presence check against the
        shared symbol index, not a pattern match on the identifier itself —
        it must generalize to any language or naming convention without
        special-casing a single name like `__file__`.
        """
        if not identity.file or not identity.short_name:
            return False
        return any(
            str(entry.get("name") or "") == identity.short_name
            for entry in self.facts.symbols_for_file(identity.repo, identity.file)
        )

    def entrypoints_named(self, name: str, file: str | None = None) -> list[dict[str, Any]]:
        """Entrypoints defined by this symbol name, narrowed by file if known."""
        candidates = self._by_name.get(name, [])
        if file:
            narrowed = [entry for entry in candidates if entry.get("file") == file]
            if narrowed:
                return narrowed
        return candidates

    def entrypoint_for_identity(self, identity: SymbolIdentity) -> dict[str, Any] | None:
        """An entrypoint matching this exact identity, if one exists.

        Falls back to a name match only when the identity itself did not
        resolve, and even then only among entrypoints in the *same file* —
        the file is always known even when the qualified name is not, and
        dropping that scope would let two same-named entrypoints in unrelated
        files both claim an unresolved hop. If more than one candidate
        survives that narrowing, the hop stays unresolved rather than
        guessing between them.
        """
        if identity.key in self._by_identity:
            return self._by_identity[identity.key]
        if not identity.resolved:
            candidates = self._by_name.get(identity.short_name, [])
            if identity.file:
                candidates = [c for c in candidates if c.get("file") == identity.file]
            if len(candidates) == 1:
                return candidates[0]
        return None

    def entrypoints_referencing(self, name: str) -> list[dict[str, Any]]:
        """Entrypoints whose captured decorator text names this identifier."""
        out = []
        for entry in self.entrypoints:
            text = entry.get("decorators")
            if not text:
                continue
            if name in _IDENTIFIER_RE.findall(str(text)):
                out.append(entry)
        return out

    def entrypoints_referencing_any(self, names: list[str]) -> list[dict[str, Any]]:
        """Entrypoints whose decorator text names any of these identifiers."""
        if not names:
            return []
        seen: dict[str, dict[str, Any]] = {}
        for name in names:
            for entry in self.entrypoints_referencing(name):
                seen.setdefault(entry.get("qualified_name") or entry["symbol"], entry)
        return [seen[key] for key in sorted(seen)]

    def entrypoints_with_signature_reference(self, name: str) -> list[dict[str, Any]]:
        out = []
        for entry in self.entrypoints:
            text = entry.get("signature")
            if not text:
                continue
            if name in _IDENTIFIER_RE.findall(str(text)):
                out.append(entry)
        return out
