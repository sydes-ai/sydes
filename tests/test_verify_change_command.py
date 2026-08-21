"""End-to-end CLI tests for `sydes verify-change`."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest
from typer.testing import CliRunner

from sydes.cli.main import app
from sydes.verify.models import ChangeVerificationResult

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


def test_verify_change_reports_flow_verification_and_runtime(service_repo: Path) -> None:
    """The deterministic run connects a service change to its route and needs."""
    _apply_service_change(service_repo)

    result = runner.invoke(
        app,
        ["verify-change", "--base", "main", "--llm-policy", "never", "--repo", f"svc={service_repo}"],
    )

    assert result.exit_code == 0, result.output
    assert "SYDES CHANGE VERIFICATION" in result.output
    assert "POST /refund" in result.output
    assert "RefundService.retry_refund" in result.output
    assert "test_refund" in result.output
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
    assert "Verdict:  OK" in result.output


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
