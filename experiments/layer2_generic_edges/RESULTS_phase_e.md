# Phase E — Layer 2 for TypeScript, Java, Go (Rust deferred)

Extends Phase D's production integration (`SYDES_LAYER2_GENERIC_EDGES`,
still default off) with the same two relation kinds
(`field_or_param_type_reference`, `call_argument_reference`) generalized
over tree-sitter, ported from
`experiments/layer2_generic_edges/treesitter_extractors.py`. Same-file
resolution only — Python's cross-file one-hop import resolution has no
validated equivalent here yet; a scoped follow-up, not invented in this
pass.

## Integration result

**Mechanism works, safely, across all three languages. Real evidence-value
NOT yet demonstrated for any of them** — unlike Python, where the
mechanism visibly promoted a real impact. Reported honestly rather than
papered over: this is a materially different (weaker) result than Phase D,
and the difference is worth taking at face value, not talking around.

## What shipped

- `src/sydes/discover/layer2_shared.py` — citation verification and
  `SymbolIdentity` qualified-name bridging factored out of the Python
  bridge so both bridges share one implementation, no drift risk.
- `src/sydes/discover/layer2_treesitter_bridge.py` — TS/Java/Go extraction.
  `known_symbol_names` sourced from the already-computed `symbol_index`
  (no full-repo re-parse); citation verification mandatory; canonical
  qualified-name bridging, same discipline as Python.
- `tree-sitter`/`tree-sitter-language-pack` added as an optional extra
  (`sydes[treesitter]`), imported lazily — a Python-only install is
  unaffected by their absence; the bridge returns `[]` cleanly, not an
  ImportError, when the extra isn't installed.
- Wired into `verify/analyzer.py` alongside the Python bridge, same call
  site, same additive-only contract.

## Real-fixture validation (real `verify-change` CLI runs, CBM backend)

| Language | Fixture | Edges extracted | Accepted impacts, flag ON | Same result flag OFF? |
|---|---|---:|---|---|
| Java | spring-boot-demo `java-s-01-reject-blank-username` | 27 | `save` / `POST /user` — both proven | **Yes, identical** — CBM's own call graph already covered this direct-call case |
| Java | spring-boot-demo `java-m-01-filter-blank-duplicate-kickout-names` | 12 | `kickout` / `DELETE /api/monitor/online/user/kickout` — proven | Not compared (single real impact, no false candidates present) |
| TypeScript | domain-driven-hexagon `ts-s-01-admin-delete-protection` | 7 | `deleteUser` / `DELETE /` — proven | **Yes, identical** |
| Go | simplebank `go-s-01-insufficient-balance` | **0** | `createTransfer` / `POST /transfers` — proven (pre-existing) | N/A — no edges extracted at all |

**Why Go extracted zero edges — checked directly, not assumed**: the real
diff adds one function taking `db.Account`/`*gin.Context` parameters —
both externally-package-qualified types, not bare local-type references,
and the function itself introduces no new locally-defined type the
extractor's noise filter would recognize. Zero edges is the *honest*
output for this diff, not a bug — it matches the original cross-language
experiment's own finding that Go's real-world code shape (heavy use of
package-qualified selectors) under-serves this class of extractor more
than TS/Java do.

**Why Java/TS showed no outcome change**: both real diffs happened to be
direct, single-hop call chains CBM's own call graph already resolves
natively — Layer 2's value (as demonstrated in Python) is specifically for
chains CBM's call graph *cannot* see (a type alias, a Pydantic validator
referenced only via decorator argument). Neither Java nor TS fixture
tested here happened to contain that shape of gap. This is a genuine
difference from Python's validation, not a flaw in the port: it means the
right showcase case for these languages hasn't been found yet, the same
way Python's took a targeted search (`_within_duration_ceiling`) rather
than showing up in the first fixture tried.

## Safety property (the part that DID fully hold)

Zero false positives, zero crashes, zero framework-specific literals
anywhere in the extraction logic, across three real repos in three
languages. Every extracted edge was citation-verified against the real
file it cites before being admitted. This is the same bar Phase D was held
to, and it holds identically here.

## Tests

- `tests/test_layer2_treesitter_bridge.py` — 8 new tests: flag on/off,
  unsupported extensions, Java/TypeScript/Go field-type-reference
  extraction, the mandatory known-name noise filter (an unrelated free
  variable must not be admitted), a Java call-argument-reference case, and
  fabricated-citation rejection via the shared verifier.
- Full suite: 1462 passed, 2 failed — the same 2 pre-existing, unrelated
  failures confirmed present on `main` before this change.

## Recommendation

**C (one narrow gap to close before claiming real value): find and
validate an actual value-add case per language**, the same way Python's
`_within_duration_ceiling`/`Settings.max_output_duration_s` search worked
— a real diff whose impact genuinely depends on a declaration-reference
relation CBM's call graph doesn't already cover. Until then, this ships as
infrastructure proven safe but not yet proven valuable for TS/Java/Go
specifically (Go additionally needs cross-file/package-qualified-type
resolution before it will fire often in real Go code at all).

Merging now is still reasonable — the flag stays off by default, the
mechanism is safe and well-tested, and shipping the infrastructure ungates
finding real showcase cases without another integration pass. But the
honest claim is "safe, not yet proven valuable here" — not "another
Python-shaped win."

## Production decision

**Merge to `main`: YES**, flag stays default-off, same zero-behavior-change
guarantee as Phase D. The value-add search is real follow-up work, not a
blocker to landing safe, tested infrastructure.
