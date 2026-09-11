# Settings/member-access experiment — final iteration on the 8-case fixture

Continuation of `experiment/layer2-generic-edges`. Not wired into
production. No file under `src/sydes/` touched.

## Result

**DID recover `Settings.max_output_duration_s`** with a generic,
cross-language member-access relation and strictly-bounded receiver-type
resolution — no framework or symbol name anywhere in the algorithm, no
oracle leakage, real chain, zero new false candidates.

| Case | Before | After | Exact chain / reason |
|---|---|---|---|
| `_within_duration_ceiling` | Likely | REACHABLE (unchanged from prior experiment) | `_within_duration_ceiling → Duration → OpenAISpeechRequest → create_speech` |
| `Settings.max_output_duration_s` | Likely | **REACHABLE** | `Settings ←[READS_MEMBER]← _within_duration_ceiling ←[call_argument_reference]← Duration ←[class_field_type_reference]← OpenAISpeechRequest ←[function_parameter_type_reference, cross-file]← create_speech` |

Both legitimate cases now resolve. (A shorter, equally real second path also exists — `_within_input_limit`, a sibling validator, independently reads `settings.max_input_length` — confirming this isn't a fluke of one function.)

## New generic relation

- **Edge kind**: `READS_MEMBER` — function F reads `receiver.member`, receiver resolved to type T → `F --READS_MEMBER--> T`.
- **Receiver-resolution method, strictly bounded to direct, same-scope evidence only** (per the explicit constraint): parameter annotation, local variable annotation, local direct constructor-call assignment (`x = ClassName(...)`), and exactly **one hop** of cross-file resolution for an imported name (including a Python function-*local* deferred import — the real case's own shape) whose *original* binding is itself one of the first three. No alias chasing beyond that one hop, no interprocedural data-flow, no reassignment tracking, no branch-sensitive reasoning, no points-to analysis.
- **Provenance** carried on every resolved edge (`python_ast_same_scope` / `python_ast_one_hop_import`, or per-language equivalents), and every unresolved/ambiguous case is recorded with that status rather than silently dropped.
- **Framework-agnostic**: the algorithm has no notion of Pydantic, FastAPI, Spring, or any other framework — it recognizes "a function reads an attribute of a variable whose type came from a direct declaration or one-hop import," a plain language-level fact true in any object-oriented (or struct-based) language.

## Noise / safety

Zero new false Established paths. All 6 known-false candidates re-checked:

- PR6 (`model_status`, `generate_from_phonemes`, `reload_model`, `unload_model`): **zero** member-access edges produced at all for any of them (none of these handlers happen to do object member access on a resolvable receiver in a way that reaches anything).
- PR7 (`convert_audio`, `trim_audio`): member-access edges exist (`AudioNormalizer`, from real, unrelated local variables) but **none** connect to the changed normalizer symbols.
- Explicit check: zero member-access edges from *any* scanned function into `generate_audio`/`generate_audio_stream`.

**False Established introduced = 0.**

One real false-positive risk *was* found and fixed during this experiment, not swept under the rug: an early version resolved a receiver name against the full "known symbol names" set (which includes function/method names), and a local variable named `info` in one file matched an *unrelated* method also named `info` in a different scanned file — exactly the string-coincidence failure this experiment was explicitly told to avoid. Fixed by restricting the "receiver is already a known name" shortcut (and the declaration-based resolvers, which only ever look for a type anyway) to a type-only name set (classes/structs/interfaces/enums/traits — explicitly excluding functions/methods). Regression-tested (`test_treesitter_known_type_names_excludes_function_names`).

## Cross-language result

| Language | Member accesses found | Receiver resolved | Meaningful | Noisy/Ambiguous |
|---|---:|---:|---:|---:|
| TypeScript | 25 | 4 | 4 (real: `UserRepository`/`Guard`/`UserEntity` references) | 0 |
| Java | 11 | 11 | 11 (real: `RabbitConsts.*` static-constant access, a Spring/RabbitMQ config file) | 0 |
| Go | 93 | 0 | — | 0 (none of the sampled files' member accesses happened to be on a locally-declared-with-a-known-type receiver — an honest, no-signal-invented result, not a bug; see limitations) |
| Rust | 22 | 0 (after the `info` fix; 1 before it, which was the false positive) | — | 0 |

No noise anywhere — every resolved edge, inspected, was real. Go and Rust's zero-resolved result is a real limitation of this bounded pass (see below), not a hairball.

## Semantic-engine findings

Checked directly against the real, already-indexed CBM graph before writing any extractor code (`semantic_source_notes.md`). Findings:

- CBM's schema has **no member/attribute-access edge type at all** — nothing to query for this relation regardless of receiver resolution.
- CBM **does** carry the raw ingredients for receiver-type resolution in some cases (a `Variable` node, a `Class` node, and a `Module --CALLS--> Class` edge from a constructor call, complete with a confidence score) — but never correlates them into an explicit "variable has type" fact. Worth remembering as a cheap, already-computed signal for a *future*, broader integration.
- Decisive: CBM has **no graph node whatsoever** for `_within_duration_ceiling`. No amount of querying its existing data could resolve this case — the reading function itself is invisible to it, independent of the receiver question.
- Conclusion: a full LSP integration was correctly out of scope and not needed to answer this experiment's question; the CBM check alone was sufficient, and it pointed to exactly the same gap (member-access modeling) that source analysis had already identified.

## Performance

- Python extraction (3 files, PR6): well under 100ms.
- Cross-file resolution (repo-wide native index build + AST scan): a few hundred ms, dominated by the existing `build_structural_index` call already used in the prior experiment.
- Tree-sitter parsing (4 languages, ~15 files total): well under 1s combined.
- CBM queries for the semantic check: 4 real tool calls, each sub-second, no pagination/truncation encountered.

## Recommendation

**A. LAYER 2 NOW HAS ENOUGH SIGNAL TO PLAN PRODUCTION INTEGRATION** — with an honest caveat: what's proven is the *Python* path end-to-end (real chain, real repo, zero noise) and the *design discipline* (bounded resolution, provenance, type-only name-matching) generalizing cleanly to 3 more languages structurally. What is **not** yet proven at production scale is receiver resolution's *hit rate* on a much larger, more varied codebase — Go and Rust returned zero resolved edges on the sampled files specifically because the one direct-declaration pattern implemented per language didn't happen to occur there (most access was on parameters or `self`/`this`-equivalent receivers, which the Python version already handles via parameter annotations but the tree-sitter version does not yet implement per-language). That is a concrete, scoped, low-risk next increment — not a sign the approach doesn't generalize.

Per the explicit instruction for this iteration: this is the stopping point on the 8-case fixture. No further relation types were invented to chase remaining cases (none remain — both legitimate cases now resolve), and this result was **not** achieved by hard-coding `Settings`'s name anywhere — confirmed by the no-framework-literal test suite and by the fact that the exact same code independently found `_within_input_limit`'s use of a different `Settings` attribute without being asked to.

## Production decision

**merge experiment? NO**

Still evaluating architecture, per instruction. Branch `experiment/layer2-generic-edges` pushed to `sydes-ai/sydes`, no PR opened, `main` untouched.
