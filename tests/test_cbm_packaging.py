"""Minimal production integration for Codebase Memory: packaging + startup.

Sydes depends on the official `codebase-memory-mcp` PyPI package (a small
pure-Python wrapper that downloads, verifies, caches, and launches the
correct native CBM runtime on first use) and continues to launch its public
`codebase-memory-mcp` console script as a persistent stdio MCP subprocess,
exactly as before. Nothing here vendors CBM, bundles a native binary,
implements a second downloader, or invokes `codebase-memory-mcp install`.

Some tests here spawn the real, installed `codebase-memory-mcp` executable
(no live daemon is mocked — this is exactly the "packaging actually works"
question) and are skipped when it is not on PATH, so the suite stays green
in an environment without the dependency installed. Others use a small fake
executable script to simulate startup failure without touching the real
CBM downloader/runtime.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import sys
import tomllib

import pytest

from sydes.code_intelligence.base import CodeIntelligenceError
from sydes.code_intelligence.cbm import CBMCodeIntelligence
from sydes.code_intelligence.cbm_client import CBMClient, resolve_executable

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REAL_CBM = shutil.which("codebase-memory-mcp")


def _write_executable(path: Path, script: str) -> str:
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


_FAKE_CBM_OK = """#!/usr/bin/env python3
import json
import sys

for line in sys.stdin:
    try:
        msg = json.loads(line)
    except Exception:
        continue
    method = msg.get("method")
    if method == "initialize":
        reply = {
            "jsonrpc": "2.0", "id": msg["id"],
            "result": {"serverInfo": {"name": "codebase-memory-mcp", "version": "0.10.8"}},
        }
        print(json.dumps(reply)); sys.stdout.flush()
    elif method == "notifications/initialized":
        continue
    elif msg.get("id") is not None:
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {}})); sys.stdout.flush()
"""

_FAKE_CBM_BOOTSTRAP_FAILURE = """#!/usr/bin/env python3
import sys
print("codebase-memory-mcp: downloading v0.10.8 for darwin/arm64...", file=sys.stderr)
print("codebase-memory-mcp: download failed: connection refused", file=sys.stderr)
sys.exit(1)
"""


# --------------------------------------------------------------------------
# 1. Packaging declares the exact pinned dependency
# --------------------------------------------------------------------------


def test_pyproject_declares_the_exact_cbm_pin() -> None:
    data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]
    assert "codebase-memory-mcp==0.10.8" in deps
    # An exact pin, never a floor — CBM changes rapidly and upgrades must be
    # intentional, not silently picked up by a `>=` resolver.
    assert not any(dep.startswith("codebase-memory-mcp>=") for dep in deps)
    assert not any(dep.startswith("codebase-memory-mcp~=") for dep in deps)


def test_lockfile_pins_the_same_exact_version_if_present() -> None:
    lock_path = _REPO_ROOT / "uv.lock"
    if not lock_path.exists():
        pytest.skip("no uv.lock in this checkout")
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    entry = next((p for p in lock["package"] if p["name"] == "codebase-memory-mcp"), None)
    assert entry is not None, "codebase-memory-mcp must be a locked package"
    assert entry["version"] == "0.10.8"


# --------------------------------------------------------------------------
# 2. Executable available -> existing MCP startup path works
# --------------------------------------------------------------------------


@pytest.mark.skipif(_REAL_CBM is None, reason="codebase-memory-mcp not installed in this environment")
def test_real_cbm_executable_resolves_and_a_session_starts_cleanly() -> None:
    """The dependency-provided console script, launched exactly as before —
    no private CLI module, no subcommand. Exercised through
    `CBMCodeIntelligence` (the real adapter seam) in a single session: CBM
    itself allows only one running instance at a time, so this intentionally
    does not also spawn a second, separate real session in the same test run.
    """
    resolved = resolve_executable()
    assert resolved == _REAL_CBM

    adapter = CBMCodeIntelligence()
    try:
        client = adapter._ensure_client()  # noqa: SLF001 - the one seam this test exercises
        assert client.server_version == "0.10.8"
    finally:
        adapter.close()


# --------------------------------------------------------------------------
# 3. Executable missing / bootstrap failure -> clear error, no fallback
# --------------------------------------------------------------------------


def test_missing_executable_fails_clearly_with_no_native_fallback(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SYDES_CBM_EXECUTABLE", raising=False)
    with pytest.raises(CodeIntelligenceError, match="not found"):
        CBMCodeIntelligence(executable=str(tmp_path / "does-not-exist"))


def test_bootstrap_failure_surfaces_the_underlying_stderr_at_the_sydes_boundary(tmp_path: Path) -> None:
    """A native-runtime download/verification failure inside the wrapper
    must reach the operator with Sydes' own framing plus the real reason —
    never a bare 'connection closed', and never a switch to native."""
    fake = _write_executable(tmp_path / "fake-cbm", _FAKE_CBM_BOOTSTRAP_FAILURE)

    with pytest.raises(CodeIntelligenceError) as excinfo:
        CBMClient.spawn(fake)

    message = str(excinfo.value)
    assert "Sydes could not initialize code intelligence (Codebase Memory)." in message
    assert "download failed: connection refused" in message


def test_bootstrap_failure_through_the_adapter_never_falls_back_to_native(tmp_path: Path) -> None:
    fake = _write_executable(tmp_path / "fake-cbm", _FAKE_CBM_BOOTSTRAP_FAILURE)
    adapter = CBMCodeIntelligence(executable=fake)

    with pytest.raises(CodeIntelligenceError):
        adapter._ensure_client()  # noqa: SLF001

    # No native adapter was ever constructed as a substitute — the failure
    # is the only thing that happened.
    assert adapter.name == "cbm"


# --------------------------------------------------------------------------
# 4. Sydes never invokes `codebase-memory-mcp install`
# --------------------------------------------------------------------------


def test_sydes_never_invokes_the_cbm_install_subcommand() -> None:
    """CBM's own `install` subcommand edits coding-agent configuration —
    Sydes must never call it. `StdioMCPSession` launches the executable with
    no arguments at all (`[self._executable]`), so this is also a literal
    source scan for the quoted string `"install"`/`'install'` (an argv
    literal, not prose mentioning the word) as a second, cheap regression
    guard.
    """
    import re

    from sydes.code_intelligence import cbm, cbm_client

    quoted_install = re.compile(r"""["']install["']""")
    for module in (cbm, cbm_client):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert not quoted_install.search(source), module.__name__


def test_cbm_process_is_launched_with_no_extra_arguments(tmp_path: Path) -> None:
    """Guards the exact argv Popen receives — a single token, never a
    subcommand such as `install`."""
    captured: dict = {}
    real_popen = __import__("subprocess").Popen

    def spy(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        return real_popen(cmd, *args, **kwargs)

    fake = _write_executable(tmp_path / "fake-cbm", _FAKE_CBM_OK)
    import sydes.code_intelligence.cbm_client as cbm_client_module
    original = cbm_client_module.subprocess.Popen
    cbm_client_module.subprocess.Popen = spy
    try:
        client = CBMClient.spawn(fake)
        client.close()
    finally:
        cbm_client_module.subprocess.Popen = original

    assert captured["cmd"] == [fake]


# --------------------------------------------------------------------------
# 5. Persistent MCP-session behavior is unchanged
# --------------------------------------------------------------------------


def test_one_session_still_serves_multiple_tool_calls_with_the_real_argv_shape(tmp_path: Path) -> None:
    """The whole point of the persistent session (one process, many
    queries) must survive this change — spawn once, call several times."""
    fake = _write_executable(tmp_path / "fake-cbm", _FAKE_CBM_OK)
    client = CBMClient.spawn(fake)
    try:
        client._session.call_tool("index_status", {"project": "p"})  # noqa: SLF001
        client._session.call_tool("get_graph_schema", {"project": "p"})  # noqa: SLF001
        client._session.call_tool("get_architecture", {"project": "p"})  # noqa: SLF001
        assert client.metrics["calls"] == 3
    finally:
        client.close()
