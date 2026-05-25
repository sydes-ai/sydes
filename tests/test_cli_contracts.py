"""Contract tests for the current Sydes CLI surface."""

import json
from pathlib import Path

import pytest
from sydes.llm.client import LLMValidationResult
from sydes.core.models import (
    ConfidenceSummary,
    EndpointCandidate,
    FlowExpansionResult,
    RepoRef,
    RoutesResult,
    TargetSpec,
    TargetMatchResult,
    TraceResult,
    TraceSummary,
)
from typer.testing import CliRunner

from sydes.cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _mock_llm_preflight_success(monkeypatch):
    """Default CLI contract tests should not depend on live LLM availability."""
    ok = LLMValidationResult(ok=True, provider="ollama", model="llama3.1:latest", base_url="http://localhost:11434")
    monkeypatch.setattr("sydes.cli.routes.validate_llm_available", lambda model_spec=None: ok)
    monkeypatch.setattr("sydes.cli.trace.validate_llm_available", lambda model_spec=None: ok)


def test_trace_terminal_output_contains_target_and_repos(tmp_path: Path, monkeypatch) -> None:
    """Trace terminal mode should include target and selected repos."""
    gateway_dir = tmp_path / "gateway"
    api_dir = tmp_path / "api"
    gateway_dir.mkdir()
    api_dir.mkdir()
    (api_dir / "src").mkdir()
    (api_dir / "src" / "routes.py").write_text("router.post('/checkout', checkout)\n")

    monkeypatch.setattr(
        "sydes.cli.trace._build_trace_result",
        lambda **_kwargs: (
            TraceResult(
                target=TargetSpec(path="/checkout", method="POST"),
                repos=[
                    RepoRef(name="gateway", root=str(gateway_dir)),
                    RepoRef(name="api", root=str(api_dir)),
                ],
                summary=TraceSummary(confidence=0.0),
            ),
            None,
        ),
    )
    monkeypatch.setattr("sydes.cli.trace.compute_workspace_id", lambda repos: "ws-test")
    monkeypatch.setattr("sydes.cli.trace.create_run_id", lambda: "run-test")
    monkeypatch.setattr(
        "sydes.cli.trace.save_run_artifact",
        lambda **kwargs: Path(f"/tmp/{kwargs['artifact_name']}.json"),
    )

    result = runner.invoke(
        app,
        [
            "trace",
            "/checkout",
            "--method",
            "POST",
            "--allow-partial",
            "--repo",
            f"gateway={gateway_dir}",
            "--repo",
            f"api={api_dir}",
        ],
    )

    assert result.exit_code == 0
    assert "Sydes API Flow Trace" in result.stdout
    assert "Target: POST /checkout" in result.stdout
    assert "gateway:" in result.stdout
    assert "api:" in result.stdout
    assert (
        "Trace is inferred from static code context and may miss runtime configuration or dynamic behavior."
        in result.stdout
    )


def test_trace_json_output_contains_expected_fields(tmp_path: Path, monkeypatch) -> None:
    """Trace JSON mode should emit stable structured fields."""
    gateway_dir = tmp_path / "gateway"
    gateway_dir.mkdir()

    monkeypatch.setattr(
        "sydes.cli.trace._build_trace_result",
        lambda **_kwargs: (
            TraceResult(
                target=TargetSpec(path="/checkout", method="POST"),
                repos=[RepoRef(name="gateway", root=str(gateway_dir))],
                summary=TraceSummary(confidence=0.0),
            ),
            None,
        ),
    )
    monkeypatch.setattr("sydes.cli.trace.compute_workspace_id", lambda repos: "ws-test")
    monkeypatch.setattr("sydes.cli.trace.create_run_id", lambda: "run-test")
    monkeypatch.setattr(
        "sydes.cli.trace.save_run_artifact",
        lambda **kwargs: Path(f"/tmp/{kwargs['artifact_name']}.json"),
    )

    result = runner.invoke(
        app,
        [
            "trace",
            "/checkout",
            "--method",
            "POST",
            "--allow-partial",
            "--repo",
            f"gateway={gateway_dir}",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["version"] == "v1"
    assert payload["target"]["path"] == "/checkout"
    assert payload["target"]["method"] == "POST"
    assert payload["repos"][0]["name"] == "gateway"
    assert "notes" in payload


def test_routes_terminal_output_runs_successfully(tmp_path: Path, monkeypatch) -> None:
    """Routes command should run and report discovery status."""
    gateway_dir = tmp_path / "gateway"
    api_dir = tmp_path / "api"
    gateway_dir.mkdir()
    api_dir.mkdir()
    (api_dir / "app.py").write_text("print('ok')\n", encoding="utf-8")

    monkeypatch.setattr(
        "sydes.cli.routes.discover_endpoints",
        lambda repos, **_kwargs: RoutesResult(
            repos=repos,
            routes=[],
            candidate_files=1,
            files_examined=1,
            notes=[],
            confidence_summary=ConfidenceSummary(average=0.0, minimum=0.0, maximum=0.0),
        ),
    )
    monkeypatch.setattr("sydes.cli.routes.compute_workspace_id", lambda repos: "ws-test")
    monkeypatch.setattr("sydes.cli.routes.create_run_id", lambda: "run-test")
    monkeypatch.setattr(
        "sydes.cli.routes.save_run_artifact",
        lambda **kwargs: Path(f"/tmp/{kwargs['artifact_name']}.json"),
    )

    result = runner.invoke(
        app,
        [
            "routes",
            "--allow-partial",
            "--repo",
            f"gateway={gateway_dir}",
            "--repo",
            f"api={api_dir}",
        ],
    )

    assert result.exit_code == 0
    assert "Sydes Routes Discovery" in result.stdout
    assert "Routes discovered:" in result.stdout
    assert "Files examined:" in result.stdout


def test_routes_model_option_is_passed_to_discovery(monkeypatch, tmp_path: Path) -> None:
    """Routes command should forward --model to LLM-backed discovery."""
    api_dir = tmp_path / "api"
    api_dir.mkdir()
    captured: dict[str, object] = {}

    def _fake_discover_endpoints(repos, *, model_spec=None, **_kwargs):
        captured["model_spec"] = model_spec
        return RoutesResult(
            repos=repos,
            routes=[],
            candidate_files=0,
            files_examined=0,
            notes=[],
            confidence_summary=ConfidenceSummary(average=0.0, minimum=0.0, maximum=0.0),
        )

    monkeypatch.setattr("sydes.cli.routes.discover_endpoints", _fake_discover_endpoints)

    result = runner.invoke(
        app,
        [
            "routes",
            "--repo",
            f"api={api_dir}",
            "--model",
            "openai:gpt-4.1-mini",
        ],
    )

    assert result.exit_code == 0
    assert captured["model_spec"] == "openai:gpt-4.1-mini"


def test_routes_model_timeout_option_is_passed_to_discovery(monkeypatch, tmp_path: Path) -> None:
    """Routes command should forward --model-timeout to discovery."""
    api_dir = tmp_path / "api"
    api_dir.mkdir()
    captured: dict[str, object] = {}

    def _fake_discover_endpoints(repos, *, model_timeout_seconds=None, **_kwargs):
        captured["model_timeout_seconds"] = model_timeout_seconds
        return RoutesResult(
            repos=repos,
            routes=[],
            candidate_files=0,
            files_examined=0,
            notes=[],
            confidence_summary=ConfidenceSummary(average=0.0, minimum=0.0, maximum=0.0),
        )

    monkeypatch.setattr("sydes.cli.routes.discover_endpoints", _fake_discover_endpoints)

    result = runner.invoke(
        app,
        [
            "routes",
            "--repo",
            f"api={api_dir}",
            "--llm-policy",
            "always",
            "--model-timeout",
            "300",
        ],
    )

    assert result.exit_code == 0
    assert captured["model_timeout_seconds"] == 300.0


def test_routes_llm_policy_never_skips_preflight(monkeypatch, tmp_path: Path) -> None:
    """Routes with --llm-policy never should not call LLM preflight validation."""
    api_dir = tmp_path / "api"
    api_dir.mkdir()
    called = {"preflight": False}

    def _fake_validate(model_spec=None):  # noqa: ANN001
        called["preflight"] = True
        return LLMValidationResult(ok=False, provider="ollama", model="missing", reason="should not be used")

    monkeypatch.setattr("sydes.cli.routes.validate_llm_available", _fake_validate)
    monkeypatch.setattr(
        "sydes.cli.routes.discover_endpoints",
        lambda repos, **_kwargs: RoutesResult(
            repos=repos,
            routes=[],
            candidate_files=0,
            files_examined=0,
            notes=[],
            confidence_summary=ConfidenceSummary(average=0.0, minimum=0.0, maximum=0.0),
        ),
    )

    result = runner.invoke(
        app,
        ["routes", "--repo", f"api={api_dir}", "--llm-policy", "never"],
    )

    assert result.exit_code == 0
    assert called["preflight"] is False


def test_trace_model_option_is_passed_to_discovery_and_expansion(monkeypatch, tmp_path: Path) -> None:
    """Trace command should forward --model through discovery and flow expansion."""
    api_dir = tmp_path / "api"
    api_dir.mkdir()
    captured: dict[str, object] = {}

    endpoint = EndpointCandidate(
        method="POST",
        path="/checkout",
        handler="checkout",
        file="app.py",
        repo="api",
    )

    def _fake_discover_endpoints(repos, *, model_spec=None, **_kwargs):
        captured["discovery_model_spec"] = model_spec
        return RoutesResult(
            repos=[RepoRef(name="api", root=str(api_dir))],
            routes=[endpoint],
            candidate_files=1,
            files_examined=1,
            notes=[],
            confidence_summary=ConfidenceSummary(average=0.6, minimum=0.6, maximum=0.6),
        )

    def _fake_resolve_trace_target(_routes, *, path, method):
        return TargetMatchResult(
            selected=endpoint,
            alternatives=[],
            notes=[],
            confidence=0.8,
        )

    def _fake_run_flow_expansion(_endpoint, _repos, *, model_spec=None, **_kwargs):
        captured["expansion_model_spec"] = model_spec
        return FlowExpansionResult(steps=[], sinks=[], notes=[], confidence=0.6)

    monkeypatch.setattr("sydes.cli.trace.discover_endpoints", _fake_discover_endpoints)
    monkeypatch.setattr("sydes.cli.trace.resolve_trace_target", _fake_resolve_trace_target)
    monkeypatch.setattr("sydes.cli.trace.run_flow_expansion", _fake_run_flow_expansion)
    monkeypatch.setattr(
        "sydes.cli.trace.build_graph_from_inferred_flow",
        lambda _selected, _expansion: ([], [], []),
    )
    monkeypatch.setattr("sydes.cli.trace.prepare_flow_expansion_context", lambda **_kwargs: None)
    monkeypatch.setattr("sydes.cli.trace.detect_cross_repo_call_candidates", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("sydes.cli.trace.generate_test_suggestions", lambda _result: [])
    monkeypatch.setattr("sydes.cli.trace.generate_test_matrix", lambda _result: None)

    result = runner.invoke(
        app,
        [
            "trace",
            "/checkout",
            "--method",
            "POST",
            "--repo",
            f"api={api_dir}",
            "--model",
            "anthropic:claude-3-5-sonnet-latest",
        ],
    )

    assert result.exit_code == 0
    assert captured["discovery_model_spec"] == "anthropic:claude-3-5-sonnet-latest"
    assert captured["expansion_model_spec"] == "anthropic:claude-3-5-sonnet-latest"


def test_routes_fails_fast_on_llm_preflight_failure(monkeypatch, tmp_path: Path) -> None:
    """Routes should fail before discovery/artifact writes when LLM preflight fails."""
    api_dir = tmp_path / "api"
    api_dir.mkdir()
    called: dict[str, bool] = {"discover": False, "artifact": False}

    monkeypatch.setattr(
        "sydes.cli.routes.validate_llm_available",
        lambda model_spec=None: LLMValidationResult(
            ok=False,
            provider="ollama",
            model="missing-model",
            base_url="http://localhost:11434",
            reason="LLM model not available: missing-model.",
            available_models=("llama3.1:latest",),
        ),
    )

    def _should_not_discover(*_args, **_kwargs):
        called["discover"] = True
        raise AssertionError("discover_endpoints should not be called when preflight fails")

    def _should_not_save(*_args, **_kwargs):
        called["artifact"] = True
        raise AssertionError("save_run_artifact should not be called when preflight fails")

    monkeypatch.setattr("sydes.cli.routes.discover_endpoints", _should_not_discover)
    monkeypatch.setattr("sydes.cli.routes.save_run_artifact", _should_not_save)

    result = runner.invoke(
        app,
        [
            "routes",
            "--repo",
            f"api={api_dir}",
            "--model",
            "ollama:missing-model",
            "--llm-policy",
            "always",
        ],
    )
    assert result.exit_code != 0
    assert "LLM validation failed: LLM model not available: missing-model." in result.stdout
    assert called["discover"] is False
    assert called["artifact"] is False


def test_trace_fails_fast_on_llm_preflight_failure_json_mode(monkeypatch, tmp_path: Path) -> None:
    """Trace should return structured JSON error and non-zero exit on preflight failure."""
    api_dir = tmp_path / "api"
    api_dir.mkdir()
    called: dict[str, bool] = {"build": False, "artifact": False}

    monkeypatch.setattr(
        "sydes.cli.trace.validate_llm_available",
        lambda model_spec=None: LLMValidationResult(
            ok=False,
            provider="openai",
            model="gpt-4.1-mini",
            base_url="https://api.openai.com/v1",
            reason="OpenAI API key is not configured.",
        ),
    )

    def _should_not_build(*_args, **_kwargs):
        called["build"] = True
        raise AssertionError("_build_trace_result should not be called when preflight fails")

    def _should_not_save(*_args, **_kwargs):
        called["artifact"] = True
        raise AssertionError("save_run_artifact should not be called when preflight fails")

    monkeypatch.setattr("sydes.cli.trace._build_trace_result", _should_not_build)
    monkeypatch.setattr("sydes.cli.trace.save_run_artifact", _should_not_save)

    result = runner.invoke(
        app,
        [
            "trace",
            "/users",
            "--method",
            "GET",
            "--repo",
            f"api={api_dir}",
            "--model",
            "openai:gpt-4.1-mini",
            "--format",
            "json",
        ],
    )
    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["provider"] == "openai"
    assert payload["error"]["model"] == "gpt-4.1-mini"
    assert payload["error"]["message"] == "OpenAI API key is not configured."
    assert called["build"] is False
    assert called["artifact"] is False


def test_routes_fails_on_runtime_llm_error_in_strict_mode(monkeypatch, tmp_path: Path) -> None:
    """Routes should exit non-zero when discovery raises LLM client errors in strict mode."""
    api_dir = tmp_path / "api"
    api_dir.mkdir()
    called: dict[str, bool] = {"artifact": False}

    monkeypatch.setattr(
        "sydes.cli.routes.validate_llm_available",
        lambda model_spec=None: LLMValidationResult(
            ok=True,
            provider="ollama",
            model="llama3.1:latest",
            base_url="http://localhost:11434",
        ),
    )

    def _raise_runtime_llm_error(*_args, **_kwargs):
        from sydes.llm.client import LLMClientError

        raise LLMClientError("network/connectivity failure: Ollama request timed out.")

    def _should_not_save(*_args, **_kwargs):
        called["artifact"] = True
        raise AssertionError("save_run_artifact should not be called on strict LLM failure")

    monkeypatch.setattr("sydes.cli.routes.discover_endpoints", _raise_runtime_llm_error)
    monkeypatch.setattr("sydes.cli.routes.save_run_artifact", _should_not_save)

    result = runner.invoke(
        app,
        ["routes", "--repo", f"api={api_dir}"],
    )
    assert result.exit_code != 0
    assert "LLM discovery failed: network/connectivity failure" in result.stdout
    assert called["artifact"] is False


def test_trace_fails_on_runtime_llm_error_in_strict_mode_json(
    monkeypatch, tmp_path: Path
) -> None:
    """Trace should return structured non-zero error when runtime LLM failure occurs."""
    api_dir = tmp_path / "api"
    api_dir.mkdir()
    called: dict[str, bool] = {"artifact": False}

    monkeypatch.setattr(
        "sydes.cli.trace.validate_llm_available",
        lambda model_spec=None: LLMValidationResult(
            ok=True,
            provider="openai",
            model="gpt-4.1-mini",
            base_url="https://api.openai.com/v1",
        ),
    )

    def _raise_runtime_llm_error(*_args, **_kwargs):
        from sydes.llm.client import LLMClientError

        raise LLMClientError("model output parse failure: malformed JSON")

    def _should_not_save(*_args, **_kwargs):
        called["artifact"] = True
        raise AssertionError("save_run_artifact should not be called on strict LLM failure")

    monkeypatch.setattr("sydes.cli.trace._build_trace_result", _raise_runtime_llm_error)
    monkeypatch.setattr("sydes.cli.trace.save_run_artifact", _should_not_save)

    result = runner.invoke(
        app,
        [
            "trace",
            "/users",
            "--method",
            "GET",
            "--repo",
            f"api={api_dir}",
            "--model",
            "openai:gpt-4.1-mini",
            "--format",
            "json",
        ],
    )
    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["message"] == "model output parse failure: malformed JSON"
    assert called["artifact"] is False


def test_routes_allow_partial_disables_strict_llm(monkeypatch, tmp_path: Path) -> None:
    """Routes should pass strict_llm=False when --allow-partial is set."""
    api_dir = tmp_path / "api"
    api_dir.mkdir()
    captured: dict[str, bool] = {}

    def _fake_discover(repos, *, strict_llm=False, **_kwargs):
        captured["strict_llm"] = strict_llm
        return RoutesResult(repos=repos, routes=[], candidate_files=0, files_examined=0)

    monkeypatch.setattr("sydes.cli.routes.discover_endpoints", _fake_discover)

    result = runner.invoke(
        app,
        ["routes", "--repo", f"api={api_dir}", "--allow-partial"],
    )
    assert result.exit_code == 0
    assert captured["strict_llm"] is False


def test_trace_allow_partial_disables_strict_llm(monkeypatch, tmp_path: Path) -> None:
    """Trace should pass strict_llm=False into build path when --allow-partial is set."""
    api_dir = tmp_path / "api"
    api_dir.mkdir()
    captured: dict[str, bool] = {}

    def _fake_build(*, strict_llm=False, **_kwargs):
        captured["strict_llm"] = strict_llm
        result = TraceResult(
            target=TargetSpec(path="/users", method="GET"),
            repos=[RepoRef(name="api", root=str(api_dir))],
            summary=TraceSummary(confidence=0.0),
        )
        return result, None

    monkeypatch.setattr("sydes.cli.trace._build_trace_result", _fake_build)
    monkeypatch.setattr("sydes.cli.trace.compute_workspace_id", lambda repos: "ws-test")
    monkeypatch.setattr("sydes.cli.trace.create_run_id", lambda: "run-test")
    monkeypatch.setattr(
        "sydes.cli.trace.save_run_artifact",
        lambda **kwargs: Path(f"/tmp/{kwargs['artifact_name']}.json"),
    )

    result = runner.invoke(
        app,
        ["trace", "/users", "--method", "GET", "--repo", f"api={api_dir}", "--allow-partial"],
    )
    assert result.exit_code == 0
    assert captured["strict_llm"] is False


def test_trace_malformed_llm_output_fails_strict_but_allows_partial(
    monkeypatch, tmp_path: Path
) -> None:
    """Strict trace mode should fail on malformed LLM output, while --allow-partial can proceed."""
    api_dir = tmp_path / "api"
    api_dir.mkdir()

    def _fake_build(*, strict_llm=False, **_kwargs):
        from sydes.llm.client import LLMClientError

        if strict_llm:
            raise LLMClientError("model output parse failure: malformed JSON")
        return (
            TraceResult(
                target=TargetSpec(path="/users", method="GET"),
                repos=[RepoRef(name="api", root=str(api_dir))],
                summary=TraceSummary(confidence=0.0),
                notes=["Flow expansion unavailable: malformed JSON."],
            ),
            None,
        )

    monkeypatch.setattr("sydes.cli.trace._build_trace_result", _fake_build)
    monkeypatch.setattr("sydes.cli.trace.compute_workspace_id", lambda repos: "ws-test")
    monkeypatch.setattr("sydes.cli.trace.create_run_id", lambda: "run-test")
    monkeypatch.setattr(
        "sydes.cli.trace.save_run_artifact",
        lambda **kwargs: Path(f"/tmp/{kwargs['artifact_name']}.json"),
    )

    strict_result = runner.invoke(
        app,
        ["trace", "/users", "--method", "GET", "--repo", f"api={api_dir}"],
    )
    assert strict_result.exit_code != 0
    assert "LLM trace failed: model output parse failure: malformed JSON" in strict_result.stdout

    partial_result = runner.invoke(
        app,
        ["trace", "/users", "--method", "GET", "--repo", f"api={api_dir}", "--allow-partial"],
    )
    assert partial_result.exit_code == 0
    assert "Flow expansion unavailable: malformed JSON." in partial_result.stdout
