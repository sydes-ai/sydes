# Semantic-source check (Phase 0/7, done first per the revised plan)

Question asked: can an existing semantic engine (CBM, or a real language
server) supply the exact receiver/member identity our own syntax extractor
would otherwise have to derive?

## What was checked

Queried the REAL, already-indexed CBM graph for `Users-ksnaik-sample_repos-Kokoro-FastAPI`
directly (`get_graph_schema`, `search_graph`, `query_graph`/Cypher) — not
theorized from documentation.

## Findings

1. **CBM's schema has no member/attribute-access edge type at all.**
   `get_graph_schema` lists `CALLS`, `USAGE`, `WRITES`, `IMPORTS`, `DEFINES`,
   `INHERITS`, `TESTS`, `HANDLES`, `CONFIGURES`, `DECORATES`, `THROWS`,
   `OVERRIDE`, `CALL_REFERENCE`, `HTTP_CALLS` — every one is at whole-symbol
   granularity (function/class/variable/module), never sub-symbol
   ("this specific field of that instance"). There is nothing to query for
   "does F read `x.member`" regardless of receiver resolution.

2. **CBM DOES track the raw ingredients for receiver-type resolution — but
   never correlates them.** For this exact case:
   - `Variable {name: "settings"}` exists (in=65, out=0).
   - `Class {name: "Settings"}` exists (in=3 — including one directly
     relevant edge).
   - `Module(api/src/core/config.py) --CALLS--> Class(Settings)`
     (`line=115`, `confidence=0.90`, `strategy="same_module"`) — CBM
     correctly recognized the constructor call `Settings()`, and even
     scored its confidence — but attributes the edge to the MODULE as
     caller, not to the `settings` variable as "has type." There is no
     `settings --INSTANCE_OF--> Settings` (or equivalent) edge anywhere.
   - This means: for a function CBM already has a node for, a
     `Module --CALLS--> Class` edge landing on the same line as a variable
     assignment IS a usable, ALREADY-COMPUTED signal a future integration
     could join against, cheaply, via one Cypher query — no new extraction
     needed for that specific resolution source. Worth remembering as a
     concrete integration idea for later, even though it doesn't help this
     particular case (next point).

3. **Decisive finding: `_within_duration_ceiling` has NO graph node in CBM
   at all.** `search_graph(name_pattern=".*_within_duration_ceiling.*")`
   and a direct Cypher query for a `USAGE` edge from any node matching
   "duration"/"ceiling" to `settings` both returned zero rows. Whatever
   CBM's Python extractor does with a module-level function used only as
   a decorator/validator-callback argument (never directly called), it did
   not create a node for it. No amount of querying CBM's existing data can
   resolve this case, because the reading function is invisible to it
   independent of the receiver-resolution question.

## Conclusion

For this specific case, an existing semantic source (CBM) cannot supply the
needed fact, on two independent counts: it has no member-access relation at
all, and it never indexed the reading function in the first place. The
extractor for this experiment is therefore built entirely from our own AST
walk, with no CBM/LSP dependency for the Settings case specifically.

The correlatable-but-unlinked `Module --CALLS--> Class` signal (point 2) is
recorded as a genuine, reusable idea for a *future*, broader Layer 1/CBM
integration — not pursued further here, since it would not change the
outcome for the one case this experiment is scored against, and pursuing it
now would be solving a problem this experiment doesn't have.

No full LSP (pyright/basedpyright/tsserver/etc.) integration was attempted
— out of scope per the plan ("do not build a full LSP framework in this
experiment"), and unnecessary here since the CBM check alone already
answered the question for this case.
