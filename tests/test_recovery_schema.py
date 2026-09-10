"""`sydes.recovery.schema` — strict parsing and the deterministic evidence
gate (an `established` path with no cited evidence is always downgraded,
independent of `sydes.recovery.verify`'s LLM-based check)."""

from __future__ import annotations

import json

import pytest

from sydes.recovery.schema import (
    PROVENANCE_AI_RECOVERY,
    PROVENANCE_AI_RECOVERY_EXHAUSTED,
    RecoveryError,
    STATUS_ESTABLISHED,
    STATUS_UNRESOLVED,
    parse_recovery_result,
)


def _payload(**overrides) -> dict:
    base = {
        "recovered_paths": [],
        "recovered_tests": [],
        "corrected_first_pass_claims": [],
        "unresolved": [],
    }
    base.update(overrides)
    return base


def test_parses_a_well_formed_established_path_with_evidence():
    payload = _payload(
        recovered_paths=[
            {
                "entrypoint": "GET /users",
                "steps": [{"symbol": "handler", "file": "a.ts", "relationship": "constructs and dispatches"}],
                "evidence": [{"file": "a.ts", "line_start": 10, "line_end": 12, "fact": "constructs FindUsersQuery"}],
                "status": "established",
            }
        ],
    )
    result = parse_recovery_result(json.dumps(payload))
    assert result.status == STATUS_ESTABLISHED
    assert len(result.recovered_paths) == 1
    assert result.recovered_paths[0].provenance == PROVENANCE_AI_RECOVERY


def test_established_path_with_no_evidence_is_downgraded_to_unresolved():
    payload = _payload(
        recovered_paths=[
            {
                "entrypoint": "GET /users",
                "steps": [{"symbol": "handler", "file": "a.ts", "relationship": "calls"}],
                "evidence": [],
                "status": "established",
            }
        ],
    )
    result = parse_recovery_result(json.dumps(payload))
    assert result.status == STATUS_UNRESOLVED
    path = result.recovered_paths[0]
    assert path.status == STATUS_UNRESOLVED
    assert path.provenance == PROVENANCE_AI_RECOVERY_EXHAUSTED
    assert path.rejection_reason is not None


def test_unresolved_path_needs_no_evidence():
    payload = _payload(
        recovered_paths=[{"entrypoint": "GET /users", "status": "unresolved"}],
        unresolved=[{"question": "does anything call this?", "missing_evidence": "no registration found"}],
    )
    result = parse_recovery_result(json.dumps(payload))
    assert result.status == STATUS_UNRESOLVED
    assert len(result.unresolved) == 1


def test_non_json_output_raises_recovery_error():
    with pytest.raises(RecoveryError):
        parse_recovery_result("this is not JSON at all")


def test_missing_entrypoint_field_raises_recovery_error():
    payload = _payload(recovered_paths=[{"steps": [], "evidence": [], "status": "established"}])
    with pytest.raises(RecoveryError):
        parse_recovery_result(json.dumps(payload))


def test_evidence_entry_missing_file_is_rejected():
    payload = _payload(
        recovered_paths=[
            {
                "entrypoint": "GET /users",
                "evidence": [{"fact": "some claim with no file"}],
                "status": "established",
            }
        ],
    )
    with pytest.raises(RecoveryError):
        parse_recovery_result(json.dumps(payload))


def test_evidence_entry_missing_fact_is_rejected():
    payload = _payload(
        recovered_paths=[
            {
                "entrypoint": "GET /users",
                "evidence": [{"file": "a.ts", "line_start": 1, "line_end": 2}],
                "status": "established",
            }
        ],
    )
    with pytest.raises(RecoveryError):
        parse_recovery_result(json.dumps(payload))


def test_unsupported_status_value_raises_recovery_error():
    payload = _payload(recovered_paths=[{"entrypoint": "GET /users", "status": "probably"}])
    with pytest.raises(RecoveryError):
        parse_recovery_result(json.dumps(payload))


def test_recovered_test_requires_file_test_and_covers():
    payload = _payload(recovered_tests=[{"file": "a.spec.ts", "test": "rejects too-large limit"}])
    with pytest.raises(RecoveryError):
        parse_recovery_result(json.dumps(payload))


def test_response_wrapped_in_markdown_fence_still_parses():
    payload = _payload()
    text = f"```json\n{json.dumps(payload)}\n```"
    result = parse_recovery_result(text)
    assert result.status == STATUS_UNRESOLVED
