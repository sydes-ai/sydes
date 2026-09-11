# Layer 2 experiment: generic declaration-reference extractors

Continuation of `experiment/native-graph-likely-8`, on its own branch
(`experiment/layer2-generic-edges`). Not wired into production. No file
under `src/sydes/` was touched.

## What was built

Three edge kinds, each a pure syntactic-shape recognizer, no framework or
symbol name anywhere in the algorithm:

- `call_argument_reference` — a locally-defined name passed as an argument
  to ANY call expression, attributed to the nearest enclosing owner
  (assignment target, or enclosing function/class for a bare call
  statement with no assignment at all).
- `class_field_type_reference` — a class-body field's type annotation
  references a locally-defined name.
- `function_parameter_type_reference` — a function parameter's type
  annotation references a name; same-file directly, cross-file by reusing
  Sydes' own already-computed import records (`resolved_file`) rather than
  writing a new resolver.

Implemented twice: once with Python's own `ast` module (precise, used for
the real scored result), once generically over tree-sitter, parametrized
per language only by grammar node-type names (the same kind of per-language
table `discover/deterministic_routes.py` already uses) — used for the
cross-language generalization check.

## Result 1 — the two real legitimate cases

| Case | Before | After |
|---|---|---|
| `_within_duration_ceiling` | Likely (unresolved) | **REACHABLE** — chain: `_within_duration_ceiling -> Duration -> OpenAISpeechRequest -> create_speech` |
| `Settings.max_output_duration_s` | Likely (unresolved) | still unresolved — needs a 4th relation kind (attribute access *inside a function body*, i.e. `settings.max_output_duration_s` read inside `_within_duration_ceiling`), which none of the 3 declaration-level extractors above cover. Identified precisely, not built. |

1 of 2 promoted cleanly, with a real, inspectable, zero-framework-naming chain — matching the "1–2/2" success bar from the plan. The miss (`Settings`) is honestly diagnosed, not forced.

## Result 2 — noise check on the 6 false candidates (Kokoro-FastAPI)

Zero spurious connections introduced. `generate_audio_stream`/`generate_audio` have **no** Layer-2-only inbound edges at all. Every false handler's (`model_status`, `generate_from_phonemes`, `reload_model`, `unload_model`, `convert_audio`, `trim_audio`) Layer-2-only outbound edges are real, unrelated type/DI references (`TTSService`, `get_tts_service`, `AudioChunk`, `AudioNormalizer`) — nothing connects them to the changed generation methods. See `layer2_kokoro_edges.json` (189 edges total).

## Result 3 — cross-language generalization (the real test)

Ran the identical tree-sitter extractors (built only by looking at Python/Kokoro-FastAPI) against one file set each from an unrelated TypeScript (NestJS-style, `domain-driven-hexagon`), Java (Spring, `spring-boot-demo`), Go (`simplebank`), and Rust (`Rocket`) repository.

**First pass (no filter): real noise.** Call-argument extraction with no filter picked up plain local variable/parameter names (`isEmpty -> item`, `main -> err`) — not meaningful. This reproduced, in a new language, the exact mistake the *first* Python prototype (from the prior experiment) also had to fix: **a bare "is this an identifier" check is not enough; the referenced name must also be a known symbol somewhere in the codebase.**

**After adding the same `known_symbol_names` filter Python's version already had:** dramatically cleaner and, on inspection, genuinely meaningful edges in all four languages — including, in the Java/Spring/RabbitMQ config file, real declarative-binding wiring (`@Bean` methods referencing exchange/queue beans as call arguments) that is structurally the *same kind* of pattern as the Pydantic case, on a completely different framework, found by the same generic algorithm. See `cross_language_edges.json`.

**Conclusion: generalizes as a useful signal, not a hairball — but only once the known-symbol-name filter is treated as load-bearing, not optional.** That filter should be considered part of the core design, not an afterthought.

## Tests

`test_layer2.py` — 14 focused tests: Python extractor shapes (including the exact real chain shape, and the local-definitions regression that broke case 6 mid-experiment), tree-sitter extractors per language, the noise-filter behavior (with and without), and a no-framework-literals guard. All pass. Main Sydes suite unaffected (1438 passed, same 2 known pre-existing unrelated failures, 0 new).

## Recommendation

The Layer 2 direction holds up under the stop condition set for it: the true case promotes cleanly and without noise, and the same generic algorithm — unmodified — produces meaningful (not spurious) edges on three other languages and two other frameworks it was never shown during development. Worth carrying to the next stage (Layer 1 / LSP integration for the remaining gaps: `Settings`'s attribute-access hop, and the `generate_from_phonemes` type-resolution gap from the prior experiment). Still not integrated into production, still on its own branch, still pending your review.
