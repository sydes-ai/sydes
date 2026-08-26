"""Passive, opt-in evaluation tracing.

See `sydes.observability.trace` for the writer. Disabled unless
`SYDES_TRACE_DIR` is set; see that module's docstring for the privacy note
and the exact artifact shapes an external harness can consume.
"""

from sydes.observability.trace import (
    TRACE_DIR_ENV_VAR,
    is_enabled,
    new_call_id,
    record_cbm_call,
    record_final_decision,
    record_impact_decision,
    record_llm_call,
    record_test_decision,
    record_verification_decision,
    start_run,
)

__all__ = [
    "TRACE_DIR_ENV_VAR",
    "is_enabled",
    "new_call_id",
    "record_cbm_call",
    "record_final_decision",
    "record_impact_decision",
    "record_llm_call",
    "record_test_decision",
    "record_verification_decision",
    "start_run",
]
