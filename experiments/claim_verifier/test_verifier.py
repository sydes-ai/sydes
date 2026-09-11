"""Unit tests for the Phase B claim verifier, using hermetic tmp_path
fixtures rather than the real Kokoro-FastAPI checkout (fast, no git
branch-switching needed to run these)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from verifier import verify_edge_citation, verify_chain  # noqa: E402


def test_citation_verified_when_symbol_literally_present(tmp_path):
    (tmp_path / "a.py").write_text("class Foo:\n    bar: Baz\n", encoding="utf-8")
    edge = {"kind": "class_field_type_reference", "user_file": "a.py", "used_symbol": "Baz", "line": 2}
    result = verify_edge_citation(edge, tmp_path)
    assert result.verified


def test_citation_rejected_when_fabricated():
    edge_file_content = "class Foo:\n    bar: Baz\n"
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "a.py").write_text(edge_file_content, encoding="utf-8")
        edge = {"kind": "class_field_type_reference", "user_file": "a.py", "used_symbol": "NeverExisted", "line": 2}
        result = verify_edge_citation(edge, root)
        assert not result.verified


def test_citation_rejects_substring_match_not_whole_word(tmp_path):
    """`Duration` must not match inside `DurationLimitError` -- exactly the
    string-coincidence failure class this whole engagement has repeatedly
    flagged and fixed elsewhere (e.g. the `info` collision in
    member_access_treesitter.py)."""
    (tmp_path / "a.py").write_text("raise DurationLimitError()\n", encoding="utf-8")
    edge = {"kind": "call_argument_reference", "user_file": "a.py", "used_symbol": "Duration", "line": 1}
    result = verify_edge_citation(edge, tmp_path)
    assert not result.verified


def test_citation_spans_a_multiline_call_statement(tmp_path):
    """Reproduces the real false-rejection found against Kokoro-FastAPI:
    a citation on a call's opening line, with the actual argument several
    lines further down inside the same (still-open) call."""
    source = (
        "def f():\n"
        "    result = some_call(\n"
        "        a=1,\n"
        "        b=2,\n"
        "        writer=writer,\n"
        "    )\n"
    )
    (tmp_path / "a.py").write_text(source, encoding="utf-8")
    edge = {"kind": "call_argument_reference", "user_file": "a.py", "used_symbol": "writer", "line": 2}
    result = verify_edge_citation(edge, tmp_path)
    assert result.verified


def test_reads_member_requires_citation_text_not_used_symbol(tmp_path):
    """A reads_member edge's used_symbol is a RESOLVED TYPE (never literal
    text at the site) -- checking it directly must fail with an explicit
    reason, not silently pass or silently fail for the wrong reason."""
    (tmp_path / "a.py").write_text("if value > settings.max_output_duration_s:\n    pass\n", encoding="utf-8")
    edge = {
        "kind": "reads_member", "user_file": "a.py", "used_symbol": "Settings", "line": 1,
    }
    result = verify_edge_citation(edge, tmp_path)
    assert not result.verified
    assert "citation_text" in result.reason


def test_reads_member_verified_with_citation_text(tmp_path):
    (tmp_path / "a.py").write_text("if value > settings.max_output_duration_s:\n    pass\n", encoding="utf-8")
    edge = {
        "kind": "reads_member", "user_file": "a.py", "used_symbol": "Settings", "line": 1,
        "citation_text": "settings.max_output_duration_s",
    }
    result = verify_edge_citation(edge, tmp_path)
    assert result.verified


def test_chain_rejects_broken_adjacency(tmp_path):
    """Two individually-real citations strung together with no actual
    connection (edge0.user_symbol != edge1.used_symbol) must fail the
    chain check even though each edge verifies alone."""
    (tmp_path / "a.py").write_text("class A:\n    x: B\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("class Unrelated:\n    y: Z\n", encoding="utf-8")
    edge0 = {"kind": "class_field_type_reference", "user_file": "a.py", "user_symbol": "A", "used_file": None, "used_symbol": "B", "line": 2}
    edge1 = {"kind": "class_field_type_reference", "user_file": "b.py", "user_symbol": "Unrelated", "used_file": None, "used_symbol": "Z", "line": 2}
    result = verify_chain([edge0, edge1], tmp_path)
    assert not result.verified
    assert "does not connect" in result.reason


def test_chain_rejects_symbol_identity_collision_across_files(tmp_path):
    """The real risk this whole engagement has flagged before
    (impact/interpreter.py's _FactIndex is keyed by full identity, not bare
    name, for exactly this reason): two DIFFERENT symbols that happen to
    share a bare name in different files must not be silently treated as
    the same node just because the names match. `helper` here is a REAL,
    distinct function in each file -- edge1 claims it found real.py's
    `helper` used by `consumer`, but its own used_file says unrelated.py,
    a different `helper` entirely."""
    (tmp_path / "real.py").write_text("class Widget:\n    pass\n\ndef helper():\n    pass\n", encoding="utf-8")
    (tmp_path / "unrelated.py").write_text("def helper():\n    pass\n\ndef consumer():\n    helper()\n", encoding="utf-8")
    edge0 = {"kind": "call_argument_reference", "user_file": "real.py", "user_symbol": "helper", "used_file": None, "used_symbol": "Widget", "line": 2}
    edge1 = {"kind": "call_argument_reference", "user_file": "unrelated.py", "user_symbol": "consumer", "used_file": "unrelated.py", "used_symbol": "helper", "line": 5}
    result = verify_chain([edge0, edge1], tmp_path)
    assert not result.verified
    assert "collision" in result.reason


def test_reexport_resolution_one_hop_only(tmp_path):
    """Mirrors the real Kokoro-FastAPI case: `structures/__init__.py`
    re-exports `OpenAISpeechRequest` from `.schemas` -- a chain hop citing
    the barrel file as `used_file` should resolve back to the real
    definition file, but only for the actual re-exported name/target."""
    from verifier import _resolves_via_one_hop_reexport
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "schemas.py").write_text("class Foo:\n    pass\n", encoding="utf-8")
    (tmp_path / "pkg" / "__init__.py").write_text("from .schemas import Foo\n", encoding="utf-8")

    assert _resolves_via_one_hop_reexport("pkg/__init__.py", "Foo", "pkg/schemas.py", tmp_path)
    assert not _resolves_via_one_hop_reexport("pkg/__init__.py", "Foo", "somewhere/else.py", tmp_path)
    assert not _resolves_via_one_hop_reexport("pkg/__init__.py", "NotImported", "pkg/schemas.py", tmp_path)


def test_chain_verifies_end_to_end_with_reexport_hop(tmp_path):
    """edge0: bar (defined in schemas.py) uses Foo. edge1: handler uses
    bar, but only ever saw it imported through the package barrel file
    (pkg/__init__.py re-exporting `bar` from `.schemas`) -- the chain must
    still verify, with that hop explicitly marked confirmed_via_reexport
    rather than a plain file-equality match."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "schemas.py").write_text("class Foo:\n    pass\n\ndef bar(v: Foo):\n    pass\n", encoding="utf-8")
    (tmp_path / "pkg" / "__init__.py").write_text("from .schemas import bar\n", encoding="utf-8")
    (tmp_path / "consumer.py").write_text("def handler():\n    bar()\n", encoding="utf-8")

    edge0 = {"kind": "call_argument_reference", "user_file": "pkg/schemas.py", "user_symbol": "bar", "used_file": "pkg/schemas.py", "used_symbol": "Foo", "line": 3}
    edge1 = {"kind": "call_argument_reference", "user_file": "consumer.py", "user_symbol": "handler", "used_file": "pkg/__init__.py", "used_symbol": "bar", "line": 2}
    result = verify_chain([edge0, edge1], tmp_path)
    assert result.verified
    assert result.hops[1].file_identity == "confirmed_via_reexport"
