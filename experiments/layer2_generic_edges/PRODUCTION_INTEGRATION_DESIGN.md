# Phase D — production integration design note (written before/alongside implementation)

## Where Layer 2 facts enter production

`src/sydes/discover/layer2_declaration_bridge.py`, called from
`verify/analyzer.py` right after `_attach_bounded_graph_edges` (the same
call site `bridge_interface_call_edges`/`bridge_member_call_edges` already
use — same discipline: additive synthetic edges, added only when
unambiguous). Scoped to the diff's own changed `.py` files, not a
repo-wide sweep, matching that call site's own "bounded neighborhood, not
whole-repository" performance discipline.

## Backend scope: CBM only, deliberately

`ImpactInterpreter`/`_FactIndex`/`_reachability` are only consulted for
`structural.backend == CBM_BACKEND and structural.provides_call_graph`
(`analyzer.py:1634`) — the native backend uses a completely different
"file-level reverse-reach heuristic" path in production and never touches
`usage_edges` for reachability at all. Extending native's benefit would
require separately wiring `route_entrypoints.py`'s already-built
entrypoint bridge into native's `StructuralFacts.entrypoints` (currently
unused there) and broadening that branch condition — real, but
out-of-scope, follow-up work. This round targets CBM specifically, which
is what both real fork PR workflows already run.

## SymbolIdentity bridging

Every extracted edge looks up the backend's own canonical qualified name
(`cbm_qualified_name` on the matching `symbol_index` entry) for both
`user_symbol` and `used_symbol`, via `_qualified_name_for`. Without this,
an edge with no qualified name resolves at `SymbolIdentity` tier 3
(file+short_name) while the SAME real symbol's own changed-symbol dict
resolves at tier 1 (canonical qualified name) — two different identity
keys for one symbol, never connecting. Found and fixed empirically against
the real Kokoro-FastAPI PR6 diff (`_within_duration_ceiling` has a real
`cbm_qualified_name` despite CBM's own call graph never mentioning it).

Cross-file resolution reuses `symbol_index`'s already-computed
`imports[].resolved_file` (no second full-repo scan), with one additional
correction (`_defining_file_for`): when the resolved file only re-exports
the name rather than defining it (a package `__init__.py` barrel), follow
one more hop to the file that actually defines it — otherwise that edge's
`used_file` would disagree with the same symbol's own same-file
declaration edges, an identity mismatch rather than a real connection.

## Citation verification gate

Every candidate edge is re-checked against the real file/line it cites
(`_citation_verified`, ported from `experiments/claim_verifier/verifier.py`,
trimmed to the whole-word + statement-span logic these three relation
kinds need) before being admitted. A failed citation is silently dropped —
never a diagnostic-only warning that could be mistaken for admitted
evidence.

## Traversal: existing machinery, unchanged

Edges are appended directly to `structural.usage_edges` in the exact
existing shape (`user_file/user_symbol/user_qualified_name/used_file/
used_symbol/used_qualified_name/source`). Zero changes to `_FactIndex`,
`_reachability`, or `ImpactInterpreter` — they already fold `usage_edges`
generically, regardless of source.

## A real, load-bearing limit found during validation (not a bug)

`_reachability` deliberately refuses to traverse PAST a node whose
identity resolved only through the tier-3 (unresolved) fallback — exactly
the safety gate this engagement's `SymbolIdentity` work exists to enforce
(no traversal built on a name-only guess). `Duration` (a plain type alias
— `Duration = Annotated[float, ..., AfterValidator(_within_duration_ceiling)]`)
has no `cbm_qualified_name` at all (CBM's symbol model doesn't track type
aliases), so it resolves unresolved. The real chain
`_within_duration_ceiling -> Duration -> OpenAISpeechRequest -> create_speech`
connects correctly at every hop (verified directly against `_FactIndex`),
but the walk legitimately refuses to continue past the unresolved
`Duration` node. This means `_within_duration_ceiling` does NOT reach
Established in this round — not because of the READS_MEMBER exclusion the
original plan assumed, but because of this different, deeper, and
correctly-principled limit. Not worked around: doing so would defeat the
exact protection this gate provides.

## Promotion / canonical merge: no new code needed

`AcceptedImpact` already derives from `ImpactResult.affected`, which
already merges PROVEN deterministic entries over INFERRED ones on any
duplicate (`verify/models.py:374-386`). Layer 2 edges only add supporting
evidence into that SAME existing mechanism — no separate canonical-merge
path, no new dedup logic, nothing to write. This is what "no new graph
engine" bought: the promotion/dedup invariant already holds by
construction, not by new code added this round.

## What remains explicitly unsupported

- `READS_MEMBER` (member-access + receiver-type resolution) — not ported;
  needs its own `citation_text` schema field, larger change, next round.
- Native backend benefit — needs entrypoint bridging + a broadened branch
  condition in `analyzer.py`, separate follow-up.
- A changed symbol whose only reachability path runs through a type alias
  or other CBM-untracked construct cannot be established via this
  mechanism, for the principled reason above.
