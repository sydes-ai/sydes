"""Backend selection.

Only `native` exists today. A name that is not registered raises rather than
degrading to whatever does work: falling back silently would mean a report
built on facts from a backend the operator did not ask for, which is the one
outcome that makes a verdict unreadable.
"""

from __future__ import annotations

import os

from sydes.code_intelligence.base import CodeIntelligence, CodeIntelligenceError
from sydes.code_intelligence.native import NATIVE_BACKEND, NativeCodeIntelligence

#: Overrides the default backend. A future `cbm` backend registers alongside
#: `native`; until it exists, naming it is an error rather than a no-op.
BACKEND_ENV_VAR = "SYDES_CODE_INTELLIGENCE"

DEFAULT_BACKEND = NATIVE_BACKEND

_BACKENDS = {NATIVE_BACKEND: NativeCodeIntelligence}


def available_backends() -> list[str]:
    return sorted(_BACKENDS)


def get_code_intelligence(backend: str | None = None) -> CodeIntelligence:
    """Resolve a backend by name, then `$SYDES_CODE_INTELLIGENCE`, then default."""
    requested = (backend or os.environ.get(BACKEND_ENV_VAR) or DEFAULT_BACKEND).strip()
    factory = _BACKENDS.get(requested)
    if factory is None:
        raise CodeIntelligenceError(
            f"Unknown code-intelligence backend {requested!r}. "
            f"Available: {', '.join(available_backends())}."
        )
    return factory()
