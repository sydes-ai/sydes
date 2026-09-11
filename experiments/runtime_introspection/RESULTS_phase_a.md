# Phase A — runtime route introspection vs. static route composition

Experiment only. `main` untouched. No file under `src/sydes/` modified.
Branch: `experiment/runtime-introspection` (off `experiment/layer2-generic-edges`).

## Question

Instead of growing `deterministic_routes.py`'s pattern table to cover every
syntax variant of "register a route" (which explodes across frameworks and
styles), can we ask the framework what it actually resolved its own routes
to, and use any mismatch against Sydes' static composition as a measured
bug, not a hypothesis?

## Method

1. Built Sydes' own static, deterministic route composition for
   Kokoro-FastAPI exactly as production does: `build_route_index_batch` →
   `build_route_graph_facts_batch` → `entrypoints_from_route_graph`
   (`src/sydes/discover/route_index.py`, `route_graph.py`,
   `route_entrypoints.py` — unmodified, called as-is).
2. In a subprocess, under the *target repo's own venv* (never Sydes'),
   imported `api.src.main` (module-level import only — a FastAPI `lifespan`
   context manager body does not execute on import, so no model
   loading/network calls happen) and read back `app.routes`.
3. Compared by `(HTTP method, normalized path)`.

## Real finding #1 — framework introspection itself is not stable across its own versions

The installed FastAPI (0.141.1) no longer flattens `include_router()` calls
into plain `APIRoute` objects inside `app.routes`. It stores an opaque
`_IncludedRouter` wrapper instead; the real sub-routes and the prefix used
at include time live on `wrapper.include_context.prefix` /
`wrapper.include_context.included_router.routes`. Naively walking
`app.routes` silently produced only 6 routes (should be 30). Fixed the
worker (`_introspect_worker.py`, `_flatten_routes`) to recurse through this
wrapper; verified backward-compatible with the plain-`APIRoute` shape too.
Worth keeping honest: **"ask the framework" is more robust than parsing 50
syntaxes, but the framework's own internal representation is itself not a
stable contract** — this recursion is the one piece of per-framework-version
adaptation this approach still needs.

## Real finding #2 — a real, previously-unknown bug in Sydes' own static route composition

After fixing the introspection side:

| | count |
|---|---:|
| Static route-derived entrypoints | 22 |
| Live (method, path) pairs | 30 |
| Matched exactly | 14 |
| Static-only (guessed, wrong) | 8 |
| Live-only, framework built-in (`/docs`, `/openapi.json`, `/redoc`, `/docs/oauth2-redirect`) — correctly out of scope | 8 |
| Live-only, same handler as a static-only row (see below) | 8 |

The 8 "static-only" and 8 of the "live-only" rows are **the same 8 real
routes** — same file, same handler — reported with two different paths:

| handler | static (wrong) | live (real) |
|---|---|---|
| `create_speech` | `/audio/speech` | `/v1/audio/speech` |
| `download_audio_file` | `/download/{id}` | `/v1/download/{id}` |
| `list_models` | `/models` | `/v1/models` |
| `retrieve_model` | `/models/{id}` | `/v1/models/{id}` |
| `list_voices` | `/audio/voices` | `/v1/audio/voices` |
| `combine_voices` | `/audio/voices/combine` | `/v1/audio/voices/combine` |
| `get_web_config` | `/config` | `/web/config` |
| `serve_web_file` | `/{id}` | `/web/{id}` |

Every one of these is reported with **`confidence: 1.0`** — silently wrong,
not flagged uncertain. The other 14 routes (declared with no mount prefix
at all — `dev_router`, `ssml_router`, `debug_router`) matched exactly.

**Root cause, confirmed by direct code reading** (not inferred): both routers
above are obtained via a Python relative import —
`from .routers.openai_compatible import router as openai_router` — then
mounted with a prefix: `app.include_router(openai_router, prefix="/v1")`
(`api/src/main.py`). `route_graph.py`'s mount resolver
(`_import_target_candidates`, [route_graph.py:102](../../src/sydes/discover/route_graph.py#L102))
only knows JS/TS import shapes (`_EXTS = (".ts", ".tsx", ".js", ".jsx")`,
[route_graph.py:12](../../src/sydes/discover/route_graph.py#L12)); its
`_module_path_candidates` helper explicitly returns `[]` for any import
source starting with `.`
([route_graph.py:93](../../src/sydes/discover/route_graph.py#L93)) — which
is every Python relative import. `resolve_local_symbol` therefore can never
find the child container for `openai_router`/`web_router`, the mount edge
carrying the `/v1`/`/web` prefix is silently dropped
([route_graph.py:297](../../src/sydes/discover/route_graph.py#L297): `if
m.child_container_id is None: continue`), and `prefixes_for` falls back to
`""` — the container's own (non-existent) prefix — instead of the mount's.

This is a real, reproducible defect, not a hypothetical: **Python — the
language calibration data called "strongest" — has a live, silent,
high-confidence path-composition bug whenever a router is mounted through
its own native relative-import syntax**, which is the normal, idiomatic way
every FastAPI app of this shape is written. It was invisible to prior
testing because nothing had cross-checked the composed output against a
live route table before.

## What this validates about the Phase A approach itself

- Zero framework-specific literals were needed in the *comparison* logic —
  only `(method, normalized-path)` matching.
- The live introspection found the bug; static inspection alone did not
  surface it despite three prior rounds of experiments already touching this
  fixture.
- Framework-version fragility (finding #1) is real but bounded — one small,
  documented adapter function per framework major-version shape, not a
  combinatorial explosion of route *syntax* variants.

## Recommendation

Do **not** fix `route_graph.py` in this round (per the standing experiment
discipline — no file under `src/sydes/` touched). Feed this exact,
evidence-backed defect into Phase D (`feature/layer2-evidence-integration`):
extend `_import_target_candidates`/`_module_path_candidates` to resolve
Python's relative and absolute dotted-module import syntax, mirroring the
one-hop resolution already proven in Layer 2's
`resolve_cross_file_parameter_types`. This is now a concrete, reproducible,
already-diagnosed bug fix, not a speculative gap.

**merge experiment? NO.** Branch `experiment/runtime-introspection` pushed
to `sydes-ai/sydes`, no PR opened, `main` untouched.
