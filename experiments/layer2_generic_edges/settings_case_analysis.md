# Forensic trace: `Settings.max_output_duration_s`

Recorded before writing any extractor code. Source: `py-real-01-max-output-duration`
branch, `/Users/ksnaik/sample_repos/Kokoro-FastAPI`.

## Exact source chain

```
api/src/core/config.py:22   class Settings(BaseSettings): ...
api/src/core/config.py:59       max_output_duration_s: float = 600.0
api/src/core/config.py:116  settings = Settings()          # module-level, direct constructor call

api/src/structures/schemas.py:68  def _within_duration_ceiling(value: float) -> float:
api/src/structures/schemas.py:71      from ..core.config import settings   # FUNCTION-LOCAL import, not module-level
api/src/structures/schemas.py:73      if value > settings.max_output_duration_s:
```

## Canonical identities

| Entity | Identity |
|---|---|
| `Settings` | class, `api/src/core/config.py`, qualified name `Settings` |
| `settings` | module-level variable in `api/src/core/config.py`, value bound by a direct constructor call `Settings()` at line 116 |
| `Settings.max_output_duration_s` | annotated class attribute, `api/src/core/config.py:59` |
| `_within_duration_ceiling` | function, `api/src/structures/schemas.py:68` |

## Is the receiver type discoverable, and how?

- **Syntactically (direct constructor assignment)**: YES, cleanly. `settings = Settings()` is an unambiguous `ast.Assign` whose RHS is a bare `ast.Call` to a locally-defined class with no arguments — the least ambiguous shape possible. This is the resolution source used.
- **Through a variable annotation**: not present here (`settings` has no `: Settings` annotation — the type comes only from the constructor call), but would be an equally valid, equally cheap source for other cases.
- **Through an import**: YES, and notably the import in `_within_duration_ceiling` is **function-local** (`from ..core.config import settings` inside the function body, "deferred import, keeps torch out of every schema import" per the adjacent comment), not a module-level import. Any resolver that only scans module-level imports (as the prior experiment's `resolve_cross_file_parameter_types` does) would miss this. The extractor built for this experiment walks a function's own body for local `ImportFrom` nodes as well.
- **Through a function parameter annotation**: not applicable here (`settings` isn't a parameter).
- **Through existing CBM/LSP data**: **checked directly against the real, already-indexed graph — see `semantic_source_notes.md`.** CBM has a `Variable:settings` node, a `Class:Settings` node, and a `Module(config.py) --CALLS--> Class:Settings` edge (from the constructor call) — but does **not** correlate these into an explicit "variable has type" fact, has no member/attribute-access edge type in its schema at all, and — the decisive finding — has **no graph node whatsoever for `_within_duration_ceiling`**. CBM cannot help resolve this specific case regardless of what it knows about `settings`, because the reading function itself is invisible to it.
- **Only dynamically**: no — nothing here requires runtime information; it's fully determinable from source text alone. This is a pure Layer 2/static-syntax problem, not a Layer 4 one.

## What's missing that this experiment needs to add

Two things, both absent from every extractor built so far:

1. A **member/attribute-access** extraction: recognize `receiver.member` accessed inside a function body (not a call, not a declaration — a plain read expression).
2. A **receiver-type resolution** step, bounded to direct, same-scope evidence only: here, "a same-file variable directly assigned a bare constructor call to a known class."
