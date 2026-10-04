# Structural reasoning order (Track A)

How Sydes decides what a change reaches, in order. Each step runs only for what the previous one left open.

1. **Normal CBM traversal.** The CBM call graph (CALLS / USAGE) through the bounded graph slice.
2. **Comprehensive CBM fact planner** (`discover/structural_enrichment.py` over `code_intelligence/cbm_facts.py`). Where traversal stalls at a framework boundary, the planner asks CBM only for what is missing:
   - typed relations with properties: CALLS, CALL_REFERENCE, USAGE, DECORATES, HANDLES, HTTP_CALLS, CONFIGURES, INHERITS / IMPLEMENTS / OVERRIDE;
   - a direct-relation check and a bounded CALLS path;
   - CBM source search;
   - capabilities from the graph schema.

   Order per fact: native CBM graph fact → CBM search/source → bounded local source read (labelled `source_fallback`, with the reason) → unresolved candidate.
3. **Deterministic composition.** Existing rules that close a hop from matching static facts, e.g. composed dispatch (static facts plus a `through_external` runtime observation), and literal route composition.
4. **`FrameworkBoundaryCandidate`.** What is still open: every fact with its provenance, the path checks run, candidate targets, what is missing and which runtime edge would close it.
5. **Selective DiffGenome runtime evidence**, for the candidates that need it.
6. **AI recovery last.**

## Evidence rule

No structural fact from CBM, source or runtime may be promoted to a direct CALLS edge unless its semantics actually support that claim.

- A decorator, a registration call, a USAGE / CALL_REFERENCE, a HANDLES route or a runtime observation inside framework code is evidence of its own kind, and stays labelled as such.
- Composed edges carry their own relation (`composed_dispatch`), never `calls`.
- Explicit edge, deterministic composition, candidate, runtime and AI remain distinct evidence classes.

The structural parser (tree-sitter) is a required dependency. If it is missing, composition is not evaluated and the analysis is reported as partial, never as "no composition".
