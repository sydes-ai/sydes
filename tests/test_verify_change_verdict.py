"""End-to-end V2 tests: execution evidence reaching the result and the verdict."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest
from typer.testing import CliRunner

from sydes.cli.main import app
from sydes.verify.models import (
    VERDICT_ACTION_REQUIRED,
    VERDICT_INCOMPLETE,
    VERDICT_VERIFIED,
    VERIFICATION_FAILED,
    VERIFICATION_PASSED,
    VERIFICATION_UNKNOWN,
    VERIFICATION_UNVERIFIED,
    ChangeVerificationResult,
)

runner = CliRunner()


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, text=True)


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


_HANDLER = '''from app.services import ItemService

service = ItemService()


@app.post("/items")
def add_item(payload):
    return service.create(payload)
'''

_SERVICE_OK = '''class ItemService:
    def create(self, payload):
        return {"message": "Item added successfully"}
'''

_SERVICE_CHANGED = '''class ItemService:
    def create(self, payload):
        return {"id": 1, "name": payload.get("name")}
'''

_TEST = '''import requests_stub


def test_add_item_route(client):
    response = client.post("/items", json={"name": "item1"})
    assert response.get_json() == {"message": "Item added successfully"}
'''


@pytest.fixture()
def service_repo(tmp_path: Path) -> Path:
    """A committed pytest repo whose single test asserts the old response shape."""
    root = tmp_path / "svc"
    _write(root, "pyproject.toml", '[project]\nname = "svc"\ndependencies = ["pytest"]\n')
    _write(root, "app/api.py", _HANDLER)
    _write(root, "app/services.py", _SERVICE_OK)
    # A self-contained test double, so the suite needs no external service.
    _write(
        root,
        "conftest.py",
        "import pytest\n"
        "from app.services import ItemService\n"
        "\n"
        "class _Response:\n"
        "    def __init__(self, payload):\n"
        "        self._payload = payload\n"
        "    def get_json(self):\n"
        "        return self._payload\n"
        "\n"
        "class _Client:\n"
        "    def post(self, path, json):\n"
        "        return _Response(ItemService().create(json))\n"
        "\n"
        "@pytest.fixture\n"
        "def client():\n"
        "    return _Client()\n",
    )
    _write(root, "requests_stub.py", "")
    _write(root, "tests/test_app.py", _TEST)

    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "initial")
    return root


def _invoke(root: Path, *extra: str):
    return runner.invoke(
        app,
        [
            "verify-change",
            "--base",
            "main",
            "--llm-policy",
            "never",
            "--repo",
            f"svc={root}",
            *extra,
        ],
    )


def _result(root: Path, tmp_path: Path, *extra: str) -> ChangeVerificationResult:
    out = tmp_path / "result.json"
    outcome = _invoke(root, "--json", str(out), *extra)
    assert outcome.exit_code == 0, outcome.output
    return ChangeVerificationResult.model_validate(json.loads(out.read_text(encoding="utf-8")))


def test_breaking_change_executes_the_mapped_test_and_reports_failure(
    service_repo: Path, tmp_path: Path
) -> None:
    """The mapped test is run, fails, and the behavior is `failed` — not `passed`."""
    _write(service_repo, "app/services.py", _SERVICE_CHANGED)

    result = _result(service_repo, tmp_path)

    behavior = next(item for item in result.verification if item.name == "POST /items")
    assert behavior.status == VERIFICATION_FAILED
    assert behavior.executions
    execution = behavior.executions[0]
    assert execution.exit_code == 1
    assert execution.status == VERIFICATION_FAILED
    assert execution.failure_summary
    assert result.summary.verdict == VERDICT_ACTION_REQUIRED
    assert result.summary.counts.behaviors_failed == 1
    assert result.summary.counts.tests_executed == 1


def test_non_breaking_change_yields_a_verified_verdict(
    service_repo: Path, tmp_path: Path
) -> None:
    """When every affected behavior's test passes, the verdict is VERIFIED."""
    _write(
        service_repo,
        "app/services.py",
        "class ItemService:\n"
        "    def create(self, payload):\n"
        "        message = \"Item added successfully\"\n"
        "        return {\"message\": message}\n",
    )

    result = _result(service_repo, tmp_path)

    behavior = next(item for item in result.verification if item.name == "POST /items")
    assert behavior.status == VERIFICATION_PASSED
    assert behavior.executions[0].duration_ms is not None
    assert result.summary.verdict == VERDICT_VERIFIED
    assert result.summary.counts.behaviors_passed == 1


def test_behavior_without_a_mapped_test_is_unverified(
    service_repo: Path, tmp_path: Path
) -> None:
    """A behavior with no applicable test stays `unverified`, verdict INCOMPLETE."""
    (service_repo / "tests" / "test_app.py").unlink()
    _write(service_repo, "app/services.py", _SERVICE_CHANGED)

    result = _result(service_repo, tmp_path)

    behavior = next(item for item in result.verification if item.name == "POST /items")
    assert behavior.status == VERIFICATION_UNVERIFIED
    assert behavior.tests == []
    assert result.summary.verdict == VERDICT_INCOMPLETE
    assert result.summary.counts.behaviors_unverified == 1


def test_no_run_tests_marks_behaviors_unknown_and_runs_nothing(
    service_repo: Path, tmp_path: Path
) -> None:
    """`--no-run-tests` never claims a pass; it reports the tests as unexecuted."""
    _write(service_repo, "app/services.py", _SERVICE_CHANGED)

    result = _result(service_repo, tmp_path, "--no-run-tests")

    behavior = next(item for item in result.verification if item.name == "POST /items")
    assert behavior.tests
    assert behavior.status == VERIFICATION_UNKNOWN
    assert result.test_executions == []
    assert result.summary.counts.tests_executed == 0
    assert result.summary.verdict == VERDICT_INCOMPLETE


def test_execution_evidence_is_serialized_in_the_json_artifact(
    service_repo: Path, tmp_path: Path
) -> None:
    """The artifact carries the exact command, exit code, duration, and output."""
    _write(service_repo, "app/services.py", _SERVICE_CHANGED)
    out = tmp_path / "result.json"

    outcome = _invoke(service_repo, "--json", str(out))
    assert outcome.exit_code == 0, outcome.output
    payload = json.loads(out.read_text(encoding="utf-8"))

    assert payload["version"] == "v2"
    assert payload["test_executions"], payload
    execution = payload["test_executions"][0]
    assert set(execution) >= {
        "test_id",
        "framework",
        "command",
        "granularity",
        "status",
        "exit_code",
        "duration_ms",
        "stdout_excerpt",
        "evidence",
    }
    assert execution["framework"] == "pytest"
    assert isinstance(execution["command"], list)
    assert execution["evidence"]["file"] == "tests/test_app.py"


def test_terminal_output_distinguishes_evidence_from_existence(
    service_repo: Path,
) -> None:
    """A located test is never shown as a tick without an executed result."""
    _write(service_repo, "app/services.py", _SERVICE_CHANGED)

    outcome = _invoke(service_repo)

    assert outcome.exit_code == 0, outcome.output
    assert "VERIFICATION" in outcome.output
    assert "FAIL  test_add_item_route" in outcome.output
    assert "✓ POST /items" not in outcome.output
    assert "Affected behaviors:" in outcome.output


def test_no_code_review_still_works_with_execution(service_repo: Path, monkeypatch) -> None:
    """`--no-code-review` remains functional alongside test execution."""
    _write(service_repo, "app/services.py", _SERVICE_CHANGED)

    def _fail(*_args, **_kwargs):
        raise AssertionError("code findings pass should not run")

    monkeypatch.setattr("sydes.verify.analyzer.generate_code_findings", _fail)
    monkeypatch.setattr(
        "sydes.verify.analyzer.generate_verification_gaps", lambda **_kwargs: ([], [])
    )

    outcome = runner.invoke(
        app,
        ["verify-change", "--base", "main", "--no-code-review", "--repo", f"svc={service_repo}"],
    )

    assert outcome.exit_code == 0, outcome.output
    assert "code_findings=skipped" in outcome.output
    assert "FAIL  test_add_item_route" in outcome.output
