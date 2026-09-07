"""End-to-end coverage, through the real `CBMCodeIntelligence.build_or_update`,
of the entrypoint bridge: CBM's decorator-derived entrypoints and Sydes' own
deterministic route-derived entrypoints must both end up in
`StructuralFacts.entrypoints`, deduplicated, with neither silently replacing
the other. This is the exact mechanism that closes the GO-S-01 gap where
`known_entrypoints=0` despite `router.POST("/transfers", server.createTransfer)`
being a real, structurally discoverable Gin route.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sydes.code_intelligence.cbm import CBMCodeIntelligence, _LANGUAGE_BY_SUFFIX, _language_for
from sydes.core.models import RepoRef

REPO = "app"

_GIN_SERVER_GO = '''package api

func (server *Server) setupRouter() {
\trouter := gin.Default()
\trouter.POST("/transfers", server.createTransfer)
}
'''


class FakeClient:
    """A CBM client double reporting one decorated (Python-style) entrypoint,
    used to prove it survives the merge unchanged alongside anything the
    route-index bridge adds from real files on disk."""

    def __init__(self, decorated: list[dict] | None = None) -> None:
        self._decorated = decorated or []
        self.metrics: dict = {"calls": 0, "session_start_ms": 0, "mean_call_ms": 0}
        self.server_version = "fake"
        self.malformed_rows = 0

    def index_repository(self, repo_path, *, mode: str = "fast") -> dict[str, Any]:
        return {"project": "proj", "nodes": 1, "excluded": {"dirs": []}}

    def all_symbols(self, project: str, label: str) -> list[list[str]]:
        return []

    def all_imports(self, project: str) -> list[list[str]]:
        return []

    def decorated_symbols(self, project: str, *, page_size: int = 500) -> list[dict]:
        return self._decorated


def _build(client: FakeClient, repo_root: Path) -> Any:
    backend = CBMCodeIntelligence(client=client)
    return backend.build_or_update(
        [RepoRef(name=REPO, root=str(repo_root))],
        defer_edges=True,
    )


def test_a_gin_route_becomes_a_structural_entrypoint_with_no_cbm_decorated_symbols(
    tmp_path: Path,
) -> None:
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "server.go").write_text(_GIN_SERVER_GO, encoding="utf-8")

    facts = _build(FakeClient(decorated=[]), tmp_path)

    by_symbol = {entry["symbol"]: entry for entry in facts.entrypoints}
    assert "createTransfer" in by_symbol
    entry = by_symbol["createTransfer"]
    assert entry["route_method"] == "POST"
    assert entry["route_path"] == "/transfers"
    assert entry["source"] == "route_index"


def test_an_existing_cbm_decorated_entrypoint_survives_alongside_the_bridged_one(
    tmp_path: Path,
) -> None:
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "server.go").write_text(_GIN_SERVER_GO, encoding="utf-8")

    decorated = [{
        "file": "app/views.py",
        "qualified_name": "views.index",
        "name": "index",
        "lines": "10-12",
        "route_method": "GET",
        "route_path": "/",
        "decorators": "@app.route('/')",
        "signature": "def index():",
    }]
    facts = _build(FakeClient(decorated=decorated), tmp_path)

    symbols = {entry["symbol"] for entry in facts.entrypoints}
    assert "index" in symbols  # CBM's own entry, untouched
    assert "createTransfer" in symbols  # the bridged one, added alongside it

    index_entry = next(e for e in facts.entrypoints if e["symbol"] == "index")
    assert index_entry["source"] == "cbm"  # unchanged provenance


def test_no_go_files_at_all_adds_no_route_derived_entrypoints(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    facts = _build(FakeClient(decorated=[]), tmp_path)
    assert facts.entrypoints == []


def test_route_derived_entrypoints_are_counted_in_diagnostics(tmp_path: Path) -> None:
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "server.go").write_text(_GIN_SERVER_GO, encoding="utf-8")
    facts = _build(FakeClient(decorated=[]), tmp_path)
    assert any(line.startswith("route_derived_entrypoints_added=1") for line in facts.diagnostics)


# --------------------------------------------------------------------------
# Language tagging (Phase G)
# --------------------------------------------------------------------------


def test_go_files_are_no_longer_tagged_unknown() -> None:
    assert _language_for("api/transfer.go") == "go"


def test_java_and_rust_are_also_tagged() -> None:
    assert _language_for("Main.java") == "java"
    assert _language_for("main.rs") == "rust"


def test_existing_python_js_ts_tagging_is_unchanged() -> None:
    assert _language_for("app.py") == "python"
    assert _language_for("app.js") == "javascript"
    assert _language_for("app.ts") == "typescript"
    assert _language_for("app.tsx") == "typescript"


def test_a_genuinely_unknown_extension_still_reports_unknown() -> None:
    assert _language_for("README.md") == "unknown"


def test_language_by_suffix_table_has_exactly_the_expected_entries() -> None:
    assert _LANGUAGE_BY_SUFFIX[".go"] == "go"
    assert _LANGUAGE_BY_SUFFIX[".java"] == "java"
    assert _LANGUAGE_BY_SUFFIX[".rs"] == "rust"
