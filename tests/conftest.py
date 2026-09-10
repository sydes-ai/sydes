"""Shared test fixtures.

The important one is `isolate_sydes_store`, which is autouse: without it, any
test that runs a CLI command writes into the developer's real `~/.sydes`.

That is not merely untidy. Workspace directories are keyed by a hash of the
repository path, and pytest recycles `tmp_path` directory names across runs, so
a discovery cache written by one run can be read by a *later* run's unrelated
test against a same-named temporary repository. The routes command then takes
its cache-hit early return, never calls discovery, and the test fails with no
local explanation. This produced a suite whose failing set moved between runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sydes.store.workspace import STORE_ROOT_ENV_VAR


@pytest.fixture(autouse=True)
def isolate_sydes_store(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory, monkeypatch
) -> Path | None:
    """Point the Sydes store at a per-test temporary root.

    Every store path resolves through `workspace.resolve_store_root`, so setting
    `$SYDES_HOME` relocates artifacts, the discovery cache, and the file-fact
    index together. A fresh directory per test also means no test can observe
    another's cache, whatever order they run in.

    A test that needs to exercise real default-path behavior opts out with
    `@pytest.mark.uses_real_sydes_home`. That marker is deliberately awkward to
    type: opting out reintroduces cross-run coupling, so it should be rare and
    such a test must not write to the store.
    """
    if request.node.get_closest_marker("uses_real_sydes_home"):
        monkeypatch.delenv(STORE_ROOT_ENV_VAR, raising=False)
        return None

    store_root = tmp_path_factory.mktemp("sydes-store")
    monkeypatch.setenv(STORE_ROOT_ENV_VAR, str(store_root))
    return store_root


@pytest.fixture(autouse=True)
def no_real_ai_recovery_by_default(monkeypatch) -> None:
    """AI recovery now runs by default on every `verify-change` invocation
    (see `--no-ai-recovery`), which means any test that drives the CLI
    command (`CliRunner`/`subprocess`) without that flag would otherwise
    try to build a REAL LLM client and make a REAL network call the moment
    its fixture repo happens to leave any high-value gap -- silently slow,
    costly, and flaky, for tests that were never written to exercise this.

    This makes the CLI's AI-recovery hook a no-op by default for the whole
    suite. A test that specifically wants to exercise it (e.g. asserting
    it's invoked/skipped correctly) overrides this within its own body via
    `monkeypatch.setattr("sydes.cli.verify_change._run_ai_recovery", ...)`
    — that later call wins over this one since both share the same
    per-test `monkeypatch` instance. A test that calls `_run_ai_recovery`
    directly (not through the CLI command) is unaffected either way, since
    that is a separate, explicit call this fixture never touches.
    """
    monkeypatch.setattr("sydes.cli.verify_change._run_ai_recovery", lambda *a, **k: None)


def pytest_configure(config: pytest.Config) -> None:
    """Register the opt-out marker so `--strict-markers` stays usable."""
    config.addinivalue_line(
        "markers",
        "uses_real_sydes_home: run against the default store root instead of an "
        "isolated temporary one; only for tests that assert default-path behavior "
        "and never write to the store.",
    )
