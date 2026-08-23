"""The seam between source-language intelligence and Sydes system reasoning."""

from sydes.code_intelligence.base import (
    CodeIntelligence,
    CodeIntelligenceError,
    StructuralFacts,
)
from sydes.code_intelligence.factory import (
    BACKEND_ENV_VAR,
    DEFAULT_BACKEND,
    available_backends,
    get_code_intelligence,
)
from sydes.code_intelligence.native import NATIVE_BACKEND, NativeCodeIntelligence

__all__ = [
    "BACKEND_ENV_VAR",
    "DEFAULT_BACKEND",
    "NATIVE_BACKEND",
    "CodeIntelligence",
    "CodeIntelligenceError",
    "NativeCodeIntelligence",
    "StructuralFacts",
    "available_backends",
    "get_code_intelligence",
]
