"""Interpreting structural facts as affected system entrypoints.

Sits between code intelligence and verification: it answers "which entrypoints
could this change reach", and stops there. Obligations, evidence and verdicts
remain downstream and unchanged.
"""

from sydes.impact.interpreter import ImpactInterpreter, TraversalBudget
from sydes.impact.reconcile import (
    build_route_lookup,
    reconcile_entrypoint,
    reconcile_entrypoints,
)
from sydes.impact.models import (
    COMPLETENESS_COMPLETE,
    COMPLETENESS_TRUNCATED,
    COMPLETENESS_UNRESOLVED,
    ENTRYPOINT_DECORATED,
    ENTRYPOINT_HTTP,
    ENTRYPOINT_UNKNOWN,
    RELATION_CALLS,
    RELATION_DECORATOR_REFERENCE,
    RELATION_DIRECT,
    RELATION_SIGNATURE_REFERENCE,
    RELATION_USAGE,
    STRATEGY_CALL_REACHABILITY,
    STRATEGY_DECORATOR_REFERENCE,
    STRATEGY_DIRECT_ENTRYPOINT,
    STRATEGY_SIGNATURE_REFERENCE,
    STRATEGY_USAGE_REACHABILITY,
    AffectedEntrypoint,
    ImpactPath,
    ImpactResult,
    ImpactStep,
    SymbolIdentity,
    UnresolvedImpact,
)

__all__ = [
    "COMPLETENESS_COMPLETE", "COMPLETENESS_TRUNCATED", "COMPLETENESS_UNRESOLVED",
    "ENTRYPOINT_DECORATED", "ENTRYPOINT_HTTP", "ENTRYPOINT_UNKNOWN",
    "RELATION_CALLS", "RELATION_DECORATOR_REFERENCE", "RELATION_DIRECT",
    "RELATION_SIGNATURE_REFERENCE", "RELATION_USAGE",
    "STRATEGY_CALL_REACHABILITY", "STRATEGY_DECORATOR_REFERENCE",
    "STRATEGY_DIRECT_ENTRYPOINT", "STRATEGY_SIGNATURE_REFERENCE",
    "STRATEGY_USAGE_REACHABILITY",
    "AffectedEntrypoint", "ImpactInterpreter", "ImpactPath", "ImpactResult",
    "ImpactStep", "SymbolIdentity", "TraversalBudget", "UnresolvedImpact",
    "build_route_lookup", "reconcile_entrypoint", "reconcile_entrypoints",
]
