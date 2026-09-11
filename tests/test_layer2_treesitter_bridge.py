"""Focused tests for the TS/Java/Go Layer 2 declaration-reference bridge
(`discover/layer2_treesitter_bridge.py`) -- the production port of
`experiments/layer2_generic_edges/treesitter_extractors.py`.

Skipped entirely if the optional `tree_sitter`/`tree_sitter_language_pack`
extra isn't installed -- the bridge itself must degrade to a clean no-op
in that case, which `test_returns_empty_when_treesitter_unavailable`
checks without actually uninstalling anything.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sydes.discover.layer2_shared import LAYER2_ENV_VAR
from sydes.discover.layer2_treesitter_bridge import bridge_layer2_treesitter_edges

pytest.importorskip("tree_sitter")
pytest.importorskip("tree_sitter_language_pack")


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _symbol_index(entries: dict[str, list[dict]]) -> dict:
    return {"repos": [{"files": [{"path": p, "symbols": s, "imports": []} for p, s in entries.items()]}]}


def test_returns_empty_when_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv(LAYER2_ENV_VAR, raising=False)
    _write(tmp_path, "a.java", "class Foo { Bar bar; }\nclass Bar {}\n")
    edges = bridge_layer2_treesitter_edges(
        repo="app", repo_root=tmp_path, changed_files=["a.java"], symbol_index={},
    )
    assert edges == []


def test_returns_empty_for_unsupported_extension(tmp_path, monkeypatch):
    monkeypatch.setenv(LAYER2_ENV_VAR, "1")
    edges = bridge_layer2_treesitter_edges(
        repo="app", repo_root=tmp_path, changed_files=["a.py", "a.md"], symbol_index={},
    )
    assert edges == []


def test_java_field_type_reference_resolved_and_citation_verified(tmp_path, monkeypatch):
    monkeypatch.setenv(LAYER2_ENV_VAR, "1")
    _write(
        tmp_path, "Foo.java",
        "class Foo {\n    Bar bar;\n}\n\nclass Bar {\n}\n",
    )
    symbol_index = _symbol_index({
        "Foo.java": [
            {"name": "Foo", "kind": "class", "cbm_qualified_name": "app.Foo"},
            {"name": "Bar", "kind": "class", "cbm_qualified_name": "app.Bar"},
        ],
    })
    edges = bridge_layer2_treesitter_edges(
        repo="app", repo_root=tmp_path, changed_files=["Foo.java"], symbol_index=symbol_index,
    )
    match = next((e for e in edges if e["used_symbol"] == "Bar"), None)
    assert match is not None
    assert match["user_symbol"] == "Foo"
    assert match["used_qualified_name"] == "app.Bar"
    assert match["user_qualified_name"] == "app.Foo"


def test_unknown_name_not_admitted_noise_filter(tmp_path, monkeypatch):
    """A local variable name that is NOT also a real symbol somewhere in
    the (already-indexed) repo must not produce an edge -- the mandatory
    noise filter a real false positive (`isEmpty -> item`) forced during
    the research round this is ported from."""
    monkeypatch.setenv(LAYER2_ENV_VAR, "1")
    _write(
        tmp_path, "Foo.java",
        "class Foo {\n    void run() {\n        doSomething(neverDefinedAnywhere);\n    }\n}\n",
    )
    symbol_index = _symbol_index({
        "Foo.java": [{"name": "Foo", "kind": "class", "cbm_qualified_name": "app.Foo"}],
    })
    edges = bridge_layer2_treesitter_edges(
        repo="app", repo_root=tmp_path, changed_files=["Foo.java"], symbol_index=symbol_index,
    )
    assert edges == []


def test_java_call_argument_reference(tmp_path, monkeypatch):
    monkeypatch.setenv(LAYER2_ENV_VAR, "1")
    _write(
        tmp_path, "Foo.java",
        "class Foo {\n"
        "    void register() {\n"
        "        registry.add(Validator.class);\n"
        "    }\n"
        "}\n\n"
        "class Validator {\n}\n",
    )
    symbol_index = _symbol_index({
        "Foo.java": [
            {"name": "Foo", "kind": "class", "cbm_qualified_name": "app.Foo"},
            {"name": "Validator", "kind": "class", "cbm_qualified_name": "app.Validator"},
        ],
    })
    edges = bridge_layer2_treesitter_edges(
        repo="app", repo_root=tmp_path, changed_files=["Foo.java"], symbol_index=symbol_index,
    )
    assert any(e["used_symbol"] == "Validator" and e["user_symbol"] == "register" for e in edges)


def test_typescript_field_type_reference(tmp_path, monkeypatch):
    monkeypatch.setenv(LAYER2_ENV_VAR, "1")
    _write(
        tmp_path, "foo.ts",
        "class Foo {\n    bar: Bar;\n}\n\nclass Bar {\n}\n",
    )
    symbol_index = _symbol_index({
        "foo.ts": [
            {"name": "Foo", "kind": "class", "cbm_qualified_name": "app.Foo"},
            {"name": "Bar", "kind": "class", "cbm_qualified_name": "app.Bar"},
        ],
    })
    edges = bridge_layer2_treesitter_edges(
        repo="app", repo_root=tmp_path, changed_files=["foo.ts"], symbol_index=symbol_index,
    )
    assert any(e["used_symbol"] == "Bar" and e["user_symbol"] == "Foo" for e in edges)


def test_go_field_type_reference(tmp_path, monkeypatch):
    monkeypatch.setenv(LAYER2_ENV_VAR, "1")
    _write(
        tmp_path, "foo.go",
        "package app\n\ntype Foo struct {\n\tBar Bar\n}\n\ntype Bar struct {\n}\n",
    )
    symbol_index = _symbol_index({
        "foo.go": [
            {"name": "Foo", "kind": "class", "cbm_qualified_name": "app.Foo"},
            {"name": "Bar", "kind": "class", "cbm_qualified_name": "app.Bar"},
        ],
    })
    edges = bridge_layer2_treesitter_edges(
        repo="app", repo_root=tmp_path, changed_files=["foo.go"], symbol_index=symbol_index,
    )
    assert any(e["used_symbol"] == "Bar" and e["user_symbol"] == "Foo" for e in edges)


def test_fabricated_citation_rejected_treesitter(tmp_path, monkeypatch):
    """Mirrors the Python bridge's own fabricated-citation regression
    test, via the shared verifier both bridges call."""
    from sydes.discover.layer2_shared import citation_verified

    _write(tmp_path, "Foo.java", "class Foo {\n    Bar bar;\n}\n")
    real_edge = {"user_file": "Foo.java", "used_symbol": "Bar", "line": 2}
    fabricated_edge = {"user_file": "Foo.java", "used_symbol": "NeverThere", "line": 2}
    cache: dict = {}
    assert citation_verified(real_edge, tmp_path, cache) is True
    assert citation_verified(fabricated_edge, tmp_path, cache) is False
