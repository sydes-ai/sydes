# Phase D — production integration (Layer 2 generic declaration-reference edges)

Branch: `feature/layer2-evidence-integration` (off `main`, which already
includes Phase A/B/C and the route-graph fix). Design note:
`PRODUCTION_INTEGRATION_DESIGN.md` in this directory.

## Integration result

**READY, flag-gated (default off).** The safety objective is fully met —
zero false-Established across every known-false candidate tested,
citation-verified, additive-only, existing traversal/promotion machinery
unchanged. The originally-hoped headline outcome (`_within_duration_ceiling`
reaching Established) does NOT materialize, for a real, principled
architectural reason found during validation (see below) — not a defect in
this integration.

## What shipped

`src/sydes/discover/layer2_declaration_bridge.py` — three declaration-
reference relation kinds (`call_argument_reference`,
`class_field_type_reference`, `function_parameter_type_reference`), ported
from `experiments/layer2_generic_edges/python_extractors.py` and
`experiments/claim_verifier/verifier.py`. Gated behind
`SYDES_LAYER2_GENERIC_EDGES` (default off). Wired into `verify/analyzer.py`
alongside the existing `bridge_interface_call_edges`/
`bridge_member_call_edges` bridges. `READS_MEMBER` deliberately not
included this round.

## Production edge model

- Every edge carries the backend's own canonical qualified name
  (`cbm_qualified_name`, looked up from `symbol_index`) for both endpoints
  — without this, an edge resolves at a different `SymbolIdentity` tier
  than the same real symbol's own changed-symbol identity, and the two
  never connect. Found and fixed empirically (see design note).
- Every candidate edge is citation-verified (re-read against the real
  file/line it cites) before being admitted; a failed citation is dropped,
  never partially trusted.
- Cross-file resolution reuses `symbol_index`'s already-computed
  `imports[].resolved_file` (no second full-repo scan), with a one-hop
  re-export correction so a barrel-file import doesn't produce a different
  identity than the symbol's own same-file declaration.
- Feeds directly into `structural.usage_edges` in the existing shape — zero
  changes to `_FactIndex`/`_reachability`/`ImpactInterpreter`.
- Scoped to CBM backend only this round (native backend's production path
  doesn't consult `usage_edges` for reachability at all — a separate,
  larger change; see design note). CBM is what both real fork PR workflows
  already run.

## Original 8 cases (Kokoro-FastAPI, real `verify-change` run, CBM backend)

| Case | Before | With Layer 2 (3 relations, no READS_MEMBER) | Correct? |
|---|---|---|---|
| 1–4, 7–8 (known false: `model_status`, `generate_from_phonemes`, `reload`, `unload`, `convert_audio`, `trim_audio`) | Likely/inferred | **Unchanged — still inferred, never falsely Established** | ✅ Correct (safety property holds) |
| 5. `Settings.max_output_duration_s` | Likely | Unchanged — remains unresolved | ✅ Expected (needs READS_MEMBER, deliberately excluded) |
| 6. `_within_duration_ceiling` | Likely | **Unchanged — remains unresolved** | ⚠️ NOT as originally hoped, but for a real reason (see below), not a bug |

`OpenAISpeechRequest` (the class field/parameter chain the 3 relations
*do* cover) gained additional real, verified connections in both PR6 and
PR7 runs (e.g. `POST /dev/captioned_speech` newly includes
`OpenAISpeechRequest` in its proven changed-symbols) — genuine, if smaller
than hoped, positive signal.

**Why case 6 doesn't resolve**: traced directly against `_FactIndex` — the
real chain `_within_duration_ceiling -> Duration -> OpenAISpeechRequest ->
create_speech` connects correctly at every hop (verified with matching
identity keys end-to-end). But `_reachability` deliberately refuses to
traverse PAST `Duration`, because `Duration` (a plain type alias, not a
class/function) has no CBM-tracked qualified name and resolves only via
the unresolved tier-3 fallback — exactly the safety gate this engagement's
own `SymbolIdentity` work exists to enforce. Not worked around: doing so
would defeat the exact protection that gate provides.

## Cross-repo validation (2 repos beyond Kokoro, per reduced scope)

Ran the extractor+citation-verifier directly against real, non-Kokoro
Python code (no fabricated fixtures):

| Repo | Framework | Python files | Layer2 edges | Notes |
|---|---|---:|---:|---|
| SimpleFastPyAPI | FastAPI | 5 | 7 | Real cross-file resolution (`create_user -> UserCreate` in a separate schema.py) |
| school-portal-api | FastAPI | 17 | 51 | Same-file call-argument + class-field references, no crashes |
| flask-sample-app | Flask | 5 | 3 | Confirms no FastAPI-specific assumption anywhere in the algorithm |

No crashes, no obviously-wrong edges, no framework-specific literal
anywhere in the extraction logic across three different real repos and two
different frameworks.

## Canonical invariants

No new canonical-merge code was needed: `AcceptedImpact` already derives
from `ImpactResult.affected`, which already merges PROVEN over INFERRED on
any duplicate. Layer 2 edges only add supporting evidence into that
existing mechanism — the "PROVEN never downgrades, wins over stale
INFERRED" invariant holds by construction, verified by the false-candidate
checks above (nothing was ever wrongly promoted).

## Behavioral coverage

Not re-run this round (Phase C's mutation oracle targets the Duration
ceiling check specifically, which doesn't newly establish here) — no
change in scope to report.

## Tests

- `tests/test_layer2_declaration_bridge.py` — 14 focused tests: flag on/off,
  each relation kind, arbitrary-local-variable rejection, cross-file
  resolution, one-hop re-export correction, fabricated-citation rejection,
  multi-line statement citation, canonical-qualified-name bridging
  (regression-pinned after being found and fixed empirically), and a scope
  check confirming no second full-repo scan happens.
- Full suite: 1454 passed, 2 failed — the same 2 pre-existing, unrelated
  failures confirmed present on `main` before this change.

## Files changed

- `src/sydes/discover/layer2_declaration_bridge.py` (new)
- `src/sydes/verify/analyzer.py` (+16 lines: one new import, one new
  bridge call after `_attach_bounded_graph_edges`)
- `tests/test_layer2_declaration_bridge.py` (new)
- `experiments/layer2_generic_edges/PRODUCTION_INTEGRATION_DESIGN.md`,
  `RESULTS_phase_d.md` (this file)

## Recommendation

**B (safe behind flag, needs more real-repo validation)**, with the safety
objective already fully demonstrated. Merge now with the flag default off
— zero behavior change for any existing user, real evidence value proven
for the relation kinds that don't require an unresolved intermediate node,
and an honest, well-understood limit documented rather than hidden for the
one case that doesn't yet resolve.

## Production decision

**Merge to `main`: YES**, flag stays default-off. Nothing changes for any
current user until `SYDES_LAYER2_GENERIC_EDGES=1` is explicitly set.
