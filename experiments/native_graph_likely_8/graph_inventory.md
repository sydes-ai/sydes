# Forensic inventory: Sydes' native/legacy structural graph machinery

Read-only research note. No production code changed. Findings below are
grounded in the actual source at the branch point of
`experiment/native-graph-likely-8` (based on `main`).

## 1. What are the existing node identities?

Two identity systems, not one:

- **`SymbolIdentity`** (`impact/interpreter.py`) — the identity used by the
  reachability engine (`_FactIndex`/`ImpactInterpreter`). Built from
  `(repo, file, qualified_name, short_name, line)`. `.key` is what adjacency
  is actually keyed on. Critically, a symbol resolved only by bare short name
  (no qualified name, no line) is `resolved=False` — an explicit "this
  identity is not trustworthy enough to keep walking past" flag, added
  specifically after a real same-name-collision bug (`JAVA-S-01`: two
  unrelated `UserServiceImpl.save` methods in different modules collided on
  a bare-name key).
- **Route-index/route-graph identities** (`discover/route_index.py`,
  `discover/route_graph.py`) — containers, declarations, and composed routes,
  identified by `(file, symbol, kind)` plus a computed `own_prefix`/mount
  chain. This is a *separate* identity space, reconciled into
  `StructuralFacts.entrypoints` only via the `route_entrypoints.py` bridge
  (see §6).

## 2. What edge types exist?

Two, both carried on `StructuralFacts` (`code_intelligence/base.py`), and
both consumed by the SAME reachability walk:

- **`call_edges`** — normalized caller→callee, `(caller_file, caller_symbol,
  caller_qualified_name, caller_line, callee_*, source)`. "Empty means not
  provided, never no calls exist" is stated explicitly in the field
  docstring.
- **`usage_edges`** — "symbol named inside another symbol's body... a symbol
  can be referenced without being invoked, which is how a dependency
  declared in a decorator argument or composed inside another definition
  reaches the code that uses it." This is the field whose own docstring
  already anticipates exactly the `_within_duration_ceiling` question (see
  Phase 3/§9 below) — decorator-argument-style reference, not a call.

Both are backend-supplied. Neither is computed by parsing source text at
interpret time; `_FactIndex` only indexes whatever the backend handed it.

## 3 & 4. Repository-wide vs flow-local; built before vs after route tracing

- **Repository-wide, built before any flow is known**: `route_index`,
  `route_graph`/composed routes, `symbol_index`, and (when a backend
  supplies them) `call_edges`/`usage_edges`. These exist independent of any
  specific change or entrypoint.
- **Flow-local, built only after a specific handler is already resolved**:
  `trace/function_body_slicer.py` (slices ONE resolved symbol's body) and
  `trace/call_follower.py`'s `build_layered_trace_expansion` (expands
  forward from that one slice). Both take an already-matched
  `endpoint`/`resolution` as input — they cannot run without one.
- **`core/graph.py`** (`build_graph_from_inferred_flow`): confirmed by
  direct read to be exactly what the task's warning anticipated — its own
  docstring says "a first coarse graph from **matched endpoint + expansion
  output**." It is a presentation/normalization step over a
  `FlowExpansionResult` that `trace/expand.py` already produced for one
  already-established flow. **It is not a repository call graph and cannot
  answer "is X reachable from Y" for an unestablished pair** — it has
  nothing to build from until that pair is already known by other means.

## 5. Confirms #4 directly

`core/graph.py` is downstream of flow inference, not an input to it. Calling
it "the native graph" would have been a real mistake; it never had the
information this experiment needs.

## 6. How does route composition work?

Deterministically and framework-agnostically, in `deterministic_routes.py`
(per-file decorator/annotation recognizers for Python/TS/Java/Go-style route
declarations) → `route_index.py` (per-file structural signals) →
`route_graph.py` (container/mount composition, e.g. Express-style
`app.use(prefix, router)` chains) → `route_entrypoints.py` (bridges the
composed routes into `StructuralFacts.entrypoints`, merging with whatever a
backend's own decorator-derived entrypoints report, deduped). This path is
**shared by both backends** — it is not CBM-only and not dormant.

## 7. How are handler→callee edges discovered?

Two independent mechanisms, not one pipeline:

- **Backend-supplied**: CBM's own `call_edges`/`usage_edges`, either from a
  full-repository `query_graph` sweep or (newer, not yet wired into the
  default path — see `graph_slice.py`'s own docstring) a bounded
  hop-batched neighborhood around specific seed symbols.
- **Text-based fallback**: `call_follower.py`'s `_extract_calls_from_statement_text`
  (a regex over `name(` call-expression shapes, string/comment-stripped)
  plus `_resolve_call` (import-aware symbol-table lookup: resolved import →
  target file → exact-name match, falling back to same-file symbol table).
  This is genuinely backend-independent — it works from nothing but the
  resolved handler's own sliced source text and the repository's symbol
  index. **This is the only mechanism that produces call information when
  the native backend is selected**, since native supplies no `call_edges`
  at all (§9).

Both feed the SAME `build_layered_trace_expansion`, which follows
backend-supplied edges when given (`call_edges is not None`) and falls back
to text-based extraction otherwise — one function, two edge sources,
identical downstream traversal policy (budgets, importance scoring, skip
rules). This is a real, already-existing hybrid mechanism, not something
this experiment has to invent (directly relevant to Phase 2).

## 8. What does native traversal understand?

| Relation | Understood? | Evidence |
|---|---|---|
| Function calls | Yes (either edge source) | §7 |
| Member calls (`this.x.y()`) | Only via `member_call_bridge.py`'s synthetic edge, and only when the declared type resolves to exactly one class with exactly one matching method name — else left unresolved, explicitly not guessed |
| Imports | Yes | `_resolve_call`'s import-aware resolution in `call_follower.py` |
| Interface dispatch | Only via `interface_bridge.py`, same "exactly one implementation or don't guess" discipline |
| DI | No | No mechanism reads a DI container/registration and produces an edge |
| Decorators (as *route* wiring) | Yes | `deterministic_routes.py`/`route_index.py` — but this is route discovery, not a general decorator→wiring relation |
| Decorators (as a *dependency* wiring, e.g. a validator/callback named in a decorator argument) | **Modeled by the schema (`usage_edges`), not populated by any extractor found in this repository.** No code path that scans a type-alias/`Annotated[...]` argument list and emits a `usage_edge` was found. This is the concrete gap behind case 6 — see Phase 3. |
| Route mounts | Yes | `route_graph.py`'s composition |
| Type usage (a field's declared type) | No general mechanism | Same gap as above |
| Field↔type relationships | No | Same gap |
| Validators | No | Same gap |
| Queues | No | No queue/topic edge kind exists anywhere in this code |
| Registrations (event/handler registries) | No | Same |

## 9. Which components currently consume CBM call/usage edges?

`impact/interpreter.py`'s `_FactIndex` (§ below) is the *only* consumer of
`call_edges`/`usage_edges` for reachability purposes. It builds one combined
inbound-adjacency index over both edge kinds and walks it with
`ImpactInterpreter._reachability` — already exactly the "CALLS and USES in
one mixed backward walk" the task hints at wanting, not something to build
from scratch (see next section and Phase 1/2 design).

`trace/call_follower.py` also *accepts* `call_edges` (optionally) for its
own, separate, flow-local expansion (§7), but that expansion is invoked from
`verify/analyzer.py` only *after* a route/handler is already matched — it
builds obligations/step detail for an established flow, it does not decide
whether a flow is established in the first place.

## 10. Native/CBM backend split — which components are live, which are legacy

Confirmed via `code_intelligence/factory.py`: `native` (Sydes' own parser,
`NativeCodeIntelligence`) is the actual default when `$SYDES_CODE_INTELLIGENCE`
is unset; `cbm` is opt-in. **Both of our real fork PR runs explicitly set
`SYDES_CODE_INTELLIGENCE: cbm`** in the reusable workflow — the 8 known
Likely cases were produced entirely under the CBM backend. The native
backend has never been exercised against these two PRs at all.

`NativeCodeIntelligence.build_or_update`'s own docstring states plainly:
**"the native backend supplies no call graph."** It returns `repo_map`,
`route_index`, `symbol_index`, `route_graph` only — `call_edges` and
`usage_edges` are empty, `provides_call_graph=False`. Concretely: if
`SYDES_CODE_INTELLIGENCE=native` had been used for these two PRs,
`ImpactInterpreter.interpret()` would have had *zero* edges to traverse and
would have resolved nothing beyond exact route/entrypoint matches — every
one of the 6 false candidates AND both legitimate candidates would have
landed as unresolved, not as "Likely, not fully established" impacts at
all (no impact record would have been synthesized for them in the first
place, since the interpreter's own guide/inference layer that produces
"Likely" entries is itself an *increment on top of* this reachability
result, not independent of it).

The genuinely load-bearing "native/legacy" pieces for THIS experiment are
therefore:

- **Live, shared by both backends**: route discovery/composition
  (`route_index`/`route_graph`/`route_entrypoints`), `member_call_bridge`,
  `interface_bridge`, and the `_FactIndex`/`ImpactInterpreter._reachability`
  traversal engine itself (backend-agnostic — it only cares what's in
  `StructuralFacts`, not who produced it).
- **Live but flow-local, not entrypoint-discovery**: `call_follower.py` +
  `expand.py`'s `build_layered_trace_expansion` + `function_body_slicer.py`
  — real, working, backend-independent call-following, but architecturally
  scoped to expand ONE already-resolved handler forward by a hard two-level
  cap (see Phase 1 finding below), not a general two-symbol reachability
  query.
- **Legacy relative to `verify-change`**: `core/graph.py` — real, wired into
  `analyzer.py`, but consumes an already-produced expansion; not usable as
  reachability evidence on its own (§4/§5).

## Summary going into Phase 1/2

The right experiment is not "old graph vs CBM." It is: **run the exact
same, already-generic, already-shipping `ImpactInterpreter._reachability`
walk (with `guide_policy=GUIDE_OFF`, so no LLM at all) once with the edges
the native backend actually supplies (none) and once with real CBM
`call_edges`/`usage_edges` fetched for the real repository**, and see where
each one honestly lands for the 8 frozen cases.
