"""The test suite must not read or write the developer's real Sydes store.

The failure this guards against is indirect, which is why it earns a dedicated
test. Workspace directories are keyed by a hash of the repository path, and
pytest reuses `tmp_path` names across runs. A discovery cache written by an
earlier run therefore sits exactly where a later run's unrelated test will look
for one, and `sydes routes` short-circuits on a cache hit before it ever calls
discovery. The symptom is a passing exit code with nothing discovered.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sydes.cli.main import app
from sydes.core.models import RepoRef
from sydes.discover.discovery_cache import load_cache_bundle, save_cache_bundle
from sydes.store.workspace import (
    STORE_ROOT_ENV_VAR,
    DEFAULT_STORE_ROOT,
    compute_workspace_id,
    ensure_workspace,
    resolve_store_root,
    save_run_artifact,
)

runner = CliRunner()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "api"
    root.mkdir()
    (root / "main.py").write_text(
        'from fastapi import FastAPI\n\napp = FastAPI()\n\n\n@app.get("/health")\ndef health():\n    return {}\n',
        encoding="utf-8",
    )
    return root


# --------------------------------------------------------------------------
# The seam
# --------------------------------------------------------------------------


def test_store_root_follows_the_environment_override(tmp_path: Path, monkeypatch) -> None:
    """Every store path resolves through one function, so one variable moves all."""
    monkeypatch.setenv(STORE_ROOT_ENV_VAR, str(tmp_path / "elsewhere"))

    assert resolve_store_root() == tmp_path / "elsewhere"
    assert ensure_workspace("ws").workspace_dir.is_relative_to(tmp_path / "elsewhere")


def test_explicit_root_argument_outranks_the_environment(tmp_path: Path, monkeypatch) -> None:
    """A caller passing a root explicitly still wins."""
    monkeypatch.setenv(STORE_ROOT_ENV_VAR, str(tmp_path / "env"))

    assert resolve_store_root(tmp_path / "explicit") == tmp_path / "explicit"


@pytest.mark.uses_real_sydes_home
def test_production_default_is_unchanged() -> None:
    """With no override, the store root is still `~/.sydes`.

    Opted out of isolation on purpose: this asserts the default and must not be
    handed a temporary root. It only reads a path and never touches the store.
    """
    assert resolve_store_root() == DEFAULT_STORE_ROOT.expanduser()
    assert resolve_store_root() == Path.home() / ".sydes"


# --------------------------------------------------------------------------
# The regression this exists to prevent
# --------------------------------------------------------------------------


def test_stale_cache_at_the_default_root_cannot_reach_an_isolated_run(
    repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """A poisoned cache where the real store lives must not be consulted.

    A stale bundle is planted at the *default* location for this exact
    repository — the situation recycled `tmp_path` names create — and then the
    routes command runs under isolation. If isolation leaked, the command would
    hit that cache and echo its fabricated route.
    """
    repos = [RepoRef(name="api", root=str(repo))]
    workspace_id = compute_workspace_id(repos)

    # Plant the poisoned bundle at the default root, with isolation lifted so
    # `save_cache_bundle` resolves exactly where a real run would have put it.
    fake_home = tmp_path / "pretend-home"
    monkeypatch.setenv(STORE_ROOT_ENV_VAR, str(fake_home))
    save_cache_bundle(
        workspace_id=workspace_id,
        repos=repos,
        llm_policy="never",
        model_fingerprint=None,
        artifacts={
            "routes_discovery": {
                "result": {
                    "repos": [item.model_dump() for item in repos],
                    "routes": [
                        {
                            "method": "GET",
                            "path": "/poisoned-from-a-previous-run",
                            "repo": "api",
                            "confidence": 1.0,
                            "status": "deterministic",
                        }
                    ],
                    "candidate_files": 0,
                    "files_examined": 0,
                    "notes": [],
                }
            },
            "repo_map": {},
            "route_index": {},
            "route_graph_facts": {},
            "discovery_coverage": {},
            "routing_pattern_plan": {},
        },
    )
    planted = fake_home / "workspaces" / workspace_id
    assert planted.exists(), "the fixture must actually plant a cache to be meaningful"

    # Now run isolated, as the autouse fixture arranges for every other test.
    isolated_root = tmp_path / "isolated"
    monkeypatch.setenv(STORE_ROOT_ENV_VAR, str(isolated_root))
    result = runner.invoke(
        app, ["routes", "--repo", f"api={repo}", "--llm-policy", "never"]
    )

    assert result.exit_code == 0, result.output
    assert "/poisoned-from-a-previous-run" not in result.stdout
    assert "discovery_cache=hit" not in result.stdout


def test_the_planted_cache_would_otherwise_be_hit(repo: Path, tmp_path: Path, monkeypatch) -> None:
    """Prove the poison is live, so the test above is not vacuously passing.

    Reading the bundle back from the root it was written to must find it. If
    this ever stops holding, the guard above proves nothing.
    """
    repos = [RepoRef(name="api", root=str(repo))]
    workspace_id = compute_workspace_id(repos)
    home = tmp_path / "home"
    monkeypatch.setenv(STORE_ROOT_ENV_VAR, str(home))

    save_cache_bundle(
        workspace_id=workspace_id,
        repos=repos,
        llm_policy="never",
        model_fingerprint=None,
        artifacts={name: {} for name in ("repo_map", "route_index", "route_graph_facts",
                                         "discovery_coverage", "routing_pattern_plan",
                                         "routes_discovery")},
    )
    status, bundle = load_cache_bundle(
        workspace_id=workspace_id, repos=repos, llm_policy="never", model_fingerprint=None
    )

    assert status.hit is True, status.reason
    assert bundle is not None


# --------------------------------------------------------------------------
# Nothing escapes to the real store
# --------------------------------------------------------------------------


def test_command_run_writes_only_under_the_isolated_root(repo: Path) -> None:
    """The autouse fixture is in force here; artifacts must land inside it."""
    isolated = resolve_store_root()
    assert isolated != Path.home() / ".sydes"

    result = runner.invoke(
        app, ["routes", "--repo", f"api={repo}", "--llm-policy", "never"]
    )

    assert result.exit_code == 0, result.output
    written = list(isolated.rglob("*.json"))
    assert written, "the command should have written artifacts somewhere"
    for path in written:
        assert path.is_relative_to(isolated)


def test_file_fact_index_also_follows_the_override(repo: Path) -> None:
    """The incremental index shares the seam rather than having its own root."""
    from sydes.discover.file_facts import build_structural_index

    repos = [RepoRef(name="api", root=str(repo))]
    workspace_id = compute_workspace_id(repos)
    build_structural_index(repos, workspace_id=workspace_id)

    isolated = resolve_store_root()
    facts = list(isolated.rglob("facts.json"))
    assert facts, "file-fact index did not persist under the isolated root"
    for path in facts:
        assert path.is_relative_to(isolated)


def test_artifact_saving_follows_the_override(repo: Path) -> None:
    """Run artifacts are stored through the same root resolution."""
    path = save_run_artifact(
        workspace_id="ws-isolation",
        run_id="run-1",
        artifact_name="probe",
        payload={"ok": True},
    )

    assert path.is_relative_to(resolve_store_root())
    assert json.loads(path.read_text(encoding="utf-8"))["ok"] is True
