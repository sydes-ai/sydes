"""End-to-end CLI tests for `sydes verify-change`."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest
from typer.testing import CliRunner

from sydes.cli.main import app
from sydes.cli.verify_change import _run_ai_recovery
from sydes.llm.client import LLMRequest, LLMResponse
from sydes.recovery.trigger import RecoveryTrigger
from sydes.verify.models import ChangeSet, ChangeVerificationResult

runner = CliRunner()


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, text=True)


@pytest.fixture()
def service_repo(tmp_path: Path) -> Path:
    """A committed FastAPI-style repo with a route, a service, a test, and config."""
    root = tmp_path / "svc"
    (root / "app").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "app" / "api.py").write_text(
        "from app.services import RefundService\n"
        "\n"
        "service = RefundService()\n"
        "\n"
        '@app.post("/refund")\n'
        "def create_refund(payload):\n"
        "    return service.retry_refund(payload)\n",
        encoding="utf-8",
    )
    (root / "app" / "services.py").write_text(
        "class RefundService:\n"
        "    def retry_refund(self, payload):\n"
        '        session.execute("UPDATE ledger SET reversed = 1")\n'
        "        return {'ok': True}\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_refund.py").write_text(
        "def test_refund(client):\n"
        '    assert client.post("/refund").status_code == 200\n',
        encoding="utf-8",
    )
    (root / ".env.example").write_text("DATABASE_URL=postgres://localhost/app\n", encoding="utf-8")

    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "initial")
    return root


def _apply_service_change(root: Path) -> None:
    (root / "app" / "services.py").write_text(
        "class RefundService:\n"
        "    def retry_refund(self, payload):\n"
        '        session.execute("UPDATE ledger SET reversed = 1")\n'
        "        return {'ok': True, 'retried': True}\n",
        encoding="utf-8",
    )


def test_ai_recovery_runs_by_default(service_repo: Path, monkeypatch) -> None:
    """AI recovery is on by default -- the CLI must invoke it without any
    flag, not require an explicit opt-in."""
    _apply_service_change(service_repo)
    calls: list[bool] = []
    monkeypatch.setattr("sydes.cli.verify_change._run_ai_recovery", lambda *a, **k: calls.append(True))

    result = runner.invoke(
        app,
        ["verify-change", "--base", "main", "--llm-policy", "never", "--repo", f"svc={service_repo}"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [True]


def test_no_ai_recovery_flag_opts_out(service_repo: Path, monkeypatch) -> None:
    """`--no-ai-recovery` is the escape hatch -- it must skip the hook entirely."""
    _apply_service_change(service_repo)
    calls: list[bool] = []
    monkeypatch.setattr("sydes.cli.verify_change._run_ai_recovery", lambda *a, **k: calls.append(True))

    result = runner.invoke(
        app,
        [
            "verify-change", "--base", "main", "--llm-policy", "never",
            "--repo", f"svc={service_repo}", "--no-ai-recovery",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == []


def test_no_ai_recovery_reports_disabled_not_failed(service_repo: Path, tmp_path: Path) -> None:
    """With recovery disabled, terminal and result say so; neither reads as a run that failed."""
    _apply_service_change(service_repo)
    out = tmp_path / "result.json"
    result = runner.invoke(
        app,
        [
            "verify-change", "--base", "main", "--llm-policy", "never",
            "--repo", f"svc={service_repo}", "--no-ai-recovery", "--json", str(out),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "AI recovery (experimental): disabled (--no-ai-recovery)." in result.output
    assert "AI recovery (experimental): triggered" not in result.output
    assert "AI recovery (experimental): failed" not in result.output
    notes = json.loads(out.read_text())["notes"]
    assert "AI recovery: disabled (--no-ai-recovery); not run." in notes


def test_verify_change_reports_flow_verification_and_runtime(service_repo: Path) -> None:
    """The deterministic run connects a service change to its route and needs."""
    _apply_service_change(service_repo)

    result = runner.invoke(
        app,
        ["verify-change", "--base", "main", "--llm-policy", "never", "--repo", f"svc={service_repo}"],
    )

    assert result.exit_code == 0, result.output
    assert "SYDES VERIFICATION" in result.output
    assert "POST /refund" in result.output
    assert "RefundService.retry_refund" in result.output
    assert "CI" in result.output
    assert "PostgreSQL" in result.output


def test_verify_change_writes_json_artifact(service_repo: Path, tmp_path: Path) -> None:
    """`--json` writes a schema-valid artifact usable by non-terminal consumers."""
    _apply_service_change(service_repo)
    out = tmp_path / "result.json"

    result = runner.invoke(
        app,
        [
            "verify-change",
            "--base",
            "main",
            "--llm-policy",
            "never",
            "--repo",
            f"svc={service_repo}",
            "--json",
            str(out),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert set(payload) >= {
        "change",
        "summary",
        "code_findings",
        "affected_flows",
        "analysis_status",
        "test_executions",
        "runtime_dependencies",
        "cross_repo_impacts",
    }
    parsed = ChangeVerificationResult.model_validate(payload)
    assert parsed.affected_flows[0].entry_label.endswith("/refund")
    assert parsed.summary.counts.changed_symbols == 1


def test_verify_change_handles_no_changes(service_repo: Path) -> None:
    """A clean tree produces an OK verdict instead of an error."""
    result = runner.invoke(
        app,
        ["verify-change", "--base", "main", "--llm-policy", "never", "--repo", f"svc={service_repo}"],
    )

    assert result.exit_code == 0, result.output
    assert "No changes against main." in result.output
    assert "Verdict" in result.output
    assert result.output.rstrip().endswith("OK")


def test_verify_change_reports_git_errors_cleanly(service_repo: Path) -> None:
    """An unknown base exits non-zero with a readable message."""
    result = runner.invoke(
        app,
        ["verify-change", "--base", "nope", "--llm-policy", "never", "--repo", f"svc={service_repo}"],
    )

    assert result.exit_code == 1
    assert "Git error:" in result.output


def test_verify_change_rejects_non_repository(tmp_path: Path) -> None:
    """Running outside a git repository fails with a clear message."""
    plain = tmp_path / "plain"
    plain.mkdir()

    result = runner.invoke(
        app,
        ["verify-change", "--base", "main", "--llm-policy", "never", "--repo", f"x={plain}"],
    )

    assert result.exit_code == 1
    assert "Not a git repository" in result.output


def test_code_findings_are_opt_in_and_advisory(service_repo: Path, monkeypatch) -> None:
    """Findings do not run by default and never enter the verdict."""

    def _fail(*_args, **_kwargs):
        raise AssertionError("code findings pass should not run by default")

    monkeypatch.setattr("sydes.verify.llm_findings.generate_code_findings", _fail)

    result = runner.invoke(
        app,
        ["verify-change", "--base", "main", "--llm-policy", "never", "--repo", f"svc={service_repo}"],
    )

    assert result.exit_code == 0, result.output
    assert "CODE FINDINGS" not in result.output


def test_ai_recovery_builds_its_client_with_no_pinned_temperature(tmp_path: Path, monkeypatch) -> None:
    """Regression test for a real observed failure: AI recovery hardcoded
    no explicit temperature override at the client-construction call, which
    let the client's own settings-derived default (0.0) reach the provider
    -- some models reject any non-default value outright ("'temperature'
    does not support 0.0 with this model. Only the default (1) value is
    supported."). Matches the same fix already applied to code review,
    route discovery, and the impact guide: build the client with
    `temperature=None` explicitly."""
    captured: dict[str, object] = {}

    class _StubClient:
        def generate(self, request: LLMRequest):
            return LLMResponse(text="{}")

    def _fake_create_default_llm_client(*args, **kwargs):
        captured.update(kwargs)
        return _StubClient()

    monkeypatch.setattr(
        "sydes.cli.verify_change.evaluate_trigger",
        lambda result: RecoveryTrigger(gap_kinds=("test-gap",), reason="test-trigger"),
    )
    monkeypatch.setattr(
        "sydes.cli.verify_change.create_default_llm_client", _fake_create_default_llm_client,
    )

    result = ChangeVerificationResult(change=ChangeSet(base="main", head="abc123", files=[], symbols=[]))
    _run_ai_recovery(result, repo_root=tmp_path, model_spec=None, json_output=None)

    assert "temperature" in captured
    assert captured["temperature"] is None


def _capture_options(monkeypatch) -> list:
    import sydes.cli.verify_change as cli

    seen: list = []
    real = cli.analyze_change

    def spy(*, repos, options):
        seen.append(options)
        return real(repos=repos, options=options)

    monkeypatch.setattr(cli, "analyze_change", spy)
    monkeypatch.setattr("sydes.cli.verify_change._run_ai_recovery", lambda *a, **k: None)
    return seen


def test_behavioral_review_context_is_off_by_default(service_repo: Path, monkeypatch) -> None:
    _apply_service_change(service_repo)
    seen = _capture_options(monkeypatch)
    result = runner.invoke(
        app, ["verify-change", "--base", "main", "--llm-policy", "never", "--repo", f"svc={service_repo}"]
    )
    assert result.exit_code == 0, result.output
    assert seen[0].checked_behavior_preamble == ""


def test_behavioral_review_context_without_genome_falls_back(service_repo: Path, tmp_path: Path, monkeypatch) -> None:
    _apply_service_change(service_repo)
    seen = _capture_options(monkeypatch)
    art = tmp_path / "a.json"
    art.write_text(json.dumps({"format": "diffgenome-change/1"}))
    out = tmp_path / "r.json"
    result = runner.invoke(
        app,
        [
            "verify-change", "--base", "main", "--llm-policy", "never", "--repo", f"svc={service_repo}",
            "--behavioral-review-context", "on", "--behavioral-artifact", str(art), "--json", str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen[0].checked_behavior_preamble == ""
    diags = json.loads(out.read_text())["diagnostics"]
    assert any(d.startswith("behavioral_review_context unavailable") for d in diags)


def test_behavioral_review_context_supplies_checked_rules(service_repo: Path, tmp_path: Path, monkeypatch) -> None:
    _apply_service_change(service_repo)
    seen = _capture_options(monkeypatch)
    art = tmp_path / "a.json"
    art.write_text(json.dumps({"format": "diffgenome-change/1", "genome": {
        "format": "diffgenome-genome-summary/1",
        "decision_rules": [{"status": "verified", "entity": "RefundService.retry_refund",
                            "file": "app/services.py", "line": 4, "source": "x", "agreeing_tests": 2}],
    }}))
    result = runner.invoke(
        app,
        [
            "verify-change", "--base", "main", "--llm-policy", "never", "--repo", f"svc={service_repo}",
            "--behavioral-review-context", "on", "--behavioral-artifact", str(art),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "app/services.py:4" in seen[0].checked_behavior_preamble
