"""`sydes.cli.verify_change`'s `--ai-recovery` integration point.

`_run_ai_recovery` is the entire touch point this prototype has with the
canonical `ChangeVerificationResult` (see its own docstring) — these tests
pin that a broken recovery pass can never corrupt or crash a normal
`verify-change` run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sydes.cli.verify_change import _run_ai_recovery
from sydes.llm.client import LLMClientError
from sydes.verify.models import ChangeSet, ChangeSummary, ChangeVerificationResult, VERDICT_INCOMPLETE


def _result() -> ChangeVerificationResult:
    return ChangeVerificationResult(
        change=ChangeSet(base="main", head="abc"),
        summary=ChangeSummary(verdict=VERDICT_INCOMPLETE),
    )


def test_no_trigger_leaves_notes_untouched(tmp_path: Path):
    result = _result()
    original_notes = list(result.notes)
    _run_ai_recovery(result, repo_root=tmp_path, model_spec=None, json_output=None)
    assert result.notes == original_notes


def test_llm_client_construction_failure_leaves_result_intact(tmp_path: Path, monkeypatch):
    from sydes.verify.models import AcceptedImpact

    result = _result()
    result.accepted_impacts = [AcceptedImpact(id="i1", label="something", status="proven")]
    original_notes = list(result.notes)

    def _boom(*_args, **_kwargs):
        raise LLMClientError("no provider configured")

    monkeypatch.setattr("sydes.cli.verify_change.create_default_llm_client", _boom)
    _run_ai_recovery(result, repo_root=tmp_path, model_spec=None, json_output=None)

    assert result.notes == original_notes  # untouched -- no misleading trace left behind


def test_recovery_error_during_the_loop_leaves_result_intact(tmp_path: Path, monkeypatch):
    from sydes.recovery.schema import RecoveryError
    from sydes.verify.models import AcceptedImpact

    result = _result()
    result.accepted_impacts = [AcceptedImpact(id="i1", label="something", status="proven")]
    original_notes = list(result.notes)

    class _FakeClient:
        def generate(self, request):  # pragma: no cover - never actually called
            raise AssertionError("should not be reached")

    def _fake_client_factory(*_args, **_kwargs):
        return _FakeClient()

    def _boom_recover(*_args, **_kwargs):
        raise RecoveryError("agent turn was not valid JSON")

    monkeypatch.setattr("sydes.cli.verify_change.create_default_llm_client", _fake_client_factory)
    monkeypatch.setattr("sydes.cli.verify_change.recover", _boom_recover)
    _run_ai_recovery(result, repo_root=tmp_path, model_spec=None, json_output=None)

    assert result.notes == original_notes
