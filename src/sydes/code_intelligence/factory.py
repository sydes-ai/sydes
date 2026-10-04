"""Backend selection.

`cbm` (the `codebase-memory-mcp` code-graph backend, installed and pinned with
Sydes: the default) and `native` (Sydes' own lightweight parser, chosen explicitly
with `$SYDES_CODE_INTELLIGENCE=native`) are the two registered backends. A name that is not registered raises rather than
degrading to whatever does work: falling back silently would mean a report
built on facts from a backend the operator did not ask for, which is the one
outcome that makes a verdict unreadable.
"""

from __future__ import annotations

import os

from sydes.code_intelligence.base import CodeIntelligence, CodeIntelligenceError
from sydes.code_intelligence.cbm import CBM_BACKEND, CBMCodeIntelligence
from sydes.code_intelligence.native import NATIVE_BACKEND, NativeCodeIntelligence

#: Overrides the default backend (see module docstring for what's registered).
BACKEND_ENV_VAR = "SYDES_CODE_INTELLIGENCE"

DEFAULT_BACKEND = CBM_BACKEND

_BACKENDS = {NATIVE_BACKEND: NativeCodeIntelligence, CBM_BACKEND: CBMCodeIntelligence}


def available_backends() -> list[str]:
    return sorted(_BACKENDS)


def resolve_backend(backend: str | None = None) -> tuple[str, str]:
    """(backend name, how it was chosen: "argument", the env var name, or "default")."""
    if backend:
        return backend.strip(), "argument"
    if os.environ.get(BACKEND_ENV_VAR, "").strip():
        return os.environ[BACKEND_ENV_VAR].strip(), BACKEND_ENV_VAR
    return DEFAULT_BACKEND, "default"


def get_code_intelligence(backend: str | None = None) -> CodeIntelligence:
    """Resolve a backend by name, then `$SYDES_CODE_INTELLIGENCE`, then default."""
    requested, _source = resolve_backend(backend)
    factory = _BACKENDS.get(requested)
    if factory is None:
        raise CodeIntelligenceError(
            f"Unknown code-intelligence backend {requested!r}. "
            f"Available: {', '.join(available_backends())}."
        )
    return factory()
