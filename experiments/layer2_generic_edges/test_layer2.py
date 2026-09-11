"""Focused tests for Layer 2's Python and tree-sitter extractors.

Run directly:
    .venv/bin/python -m pytest experiments/layer2_generic_edges/test_layer2.py -q

Not part of the main Sydes test suite -- this whole directory is an
experiment, not shipped code.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from python_extractors import (
    CALL_ARGUMENT_REFERENCE,
    CLASS_FIELD_TYPE_REFERENCE,
    extract_all_same_file,
    extract_call_argument_references,
    extract_class_field_type_references,
    extract_function_parameter_type_references,
)
from treesitter_extractors import extract_all, known_symbol_names


# ---------------------------------------------------------------------------
# Python extractors
# ---------------------------------------------------------------------------


def test_type_alias_assignment_argument_reference_found():
    """The exact real-world shape that motivated this experiment: a
    module-level type-alias assignment whose expression names another
    locally-defined function as an argument."""
    src = """
def _validator(value):
    return value

Alias = Annotated[float, SomeWrapper(_validator)]
"""
    edges = extract_call_argument_references(src, file="x.py")
    assert any(e["user_symbol"] == "Alias" and e["used_symbol"] == "_validator" for e in edges)


def test_bare_call_statement_argument_reference_found():
    """Generalization over the first prototype: a registration-style call
    with NO assignment at all, inside a function body, must still produce
    an edge attributed to the enclosing function."""
    src = """
class SomeMiddleware:
    pass

def create_app():
    app = object()
    app.add_middleware(SomeMiddleware)
"""
    edges = extract_call_argument_references(src, file="x.py")
    assert any(e["user_symbol"] == "create_app" and e["used_symbol"] == "SomeMiddleware" for e in edges)


def test_call_argument_reference_ignores_builtins():
    src = """
def f():
    print(len)
"""
    edges = extract_call_argument_references(src, file="x.py")
    assert edges == []


def test_class_field_type_reference_found():
    src = """
class Inner:
    pass

class Outer:
    field: Inner
"""
    edges = extract_class_field_type_references(src, file="x.py")
    assert any(e["kind"] == CLASS_FIELD_TYPE_REFERENCE and e["used_symbol"] == "Inner" for e in edges)


def test_function_parameter_type_reference_same_file():
    src = """
class Payload:
    pass

def handler(request: Payload):
    pass
"""
    edges = extract_function_parameter_type_references(src, file="x.py")
    assert any(e["user_symbol"] == "handler" and e["used_symbol"] == "Payload" for e in edges)


def test_local_definitions_includes_plain_assignment_targets():
    """Regression pin: a type alias (`Alias = Annotated[...]`) is a plain
    ast.Assign, not a def -- if local_definitions ever regresses to only
    tracking FunctionDef/ClassDef again, this whole experiment's core
    finding (the Duration/_within_duration_ceiling chain) silently breaks."""
    src = """
def _validator(value):
    return value

Alias = Annotated[float, SomeWrapper(_validator)]

class UsesAlias:
    field: Alias
"""
    edges = extract_all_same_file(src, file="x.py")
    used = {e["used_symbol"] for e in edges}
    assert "_validator" in used  # via Alias's assignment
    assert "Alias" in used  # via UsesAlias's field annotation


def test_end_to_end_chain_assembles_from_three_edges():
    """The exact abstract shape of the real Kokoro-FastAPI chain, as a
    small synthetic repro: validator -> type alias -> class field ->
    (parameter, checked separately since it's cross-file in the real
    case) -- confirms the three same-file edges chain correctly."""
    src = """
def _within_ceiling(value):
    return value

Duration = Annotated[float, AfterValidator(_within_ceiling)]

class OpenAISpeechRequest:
    max_duration_seconds: Optional[Duration]
"""
    edges = extract_all_same_file(src, file="x.py")
    by_used: dict[str, list[str]] = {}
    for e in edges:
        by_used.setdefault(e["used_symbol"], []).append(e["user_symbol"])
    assert "Duration" in by_used["_within_ceiling"]
    assert "OpenAISpeechRequest" in by_used["Duration"]


# ---------------------------------------------------------------------------
# Tree-sitter extractors (cross-language)
# ---------------------------------------------------------------------------


def test_typescript_field_and_call_argument_reference():
    src = """
class Foo {
  bar: SomeType;
}
class SomeMiddleware {}
function setup() {
  app.use(SomeMiddleware);
}
"""
    known = known_symbol_names({"x.ts": src}, language="typescript")
    edges = extract_all(src, file="x.ts", language="typescript", known_names=known)
    kinds_used = {(e["user_symbol"], e["used_symbol"]) for e in edges}
    assert ("Foo", "SomeType") not in kinds_used  # SomeType is never DEFINED anywhere in this snippet
    assert ("setup", "SomeMiddleware") in kinds_used


def test_java_field_type_reference():
    src = """
class SomeType {}
class Foo {
  private SomeType bar;
}
"""
    known = known_symbol_names({"x.java": src}, language="java")
    edges = extract_all(src, file="x.java", language="java", known_names=known)
    assert any(e["user_symbol"] == "Foo" and e["used_symbol"] == "SomeType" for e in edges)


def test_go_field_type_reference():
    src = """
package main
type SomeType struct {}
type Foo struct {
  Bar SomeType
}
"""
    known = known_symbol_names({"x.go": src}, language="go")
    edges = extract_all(src, file="x.go", language="go", known_names=known)
    assert any(e["user_symbol"] == "Foo" and e["used_symbol"] == "SomeType" for e in edges)


def test_rust_field_type_reference():
    src = """
struct SomeType {}
struct Foo {
    bar: SomeType,
}
"""
    known = known_symbol_names({"x.rs": src}, language="rust")
    edges = extract_all(src, file="x.rs", language="rust", known_names=known)
    assert any(e["user_symbol"] == "Foo" and e["used_symbol"] == "SomeType" for e in edges)


def test_known_names_filter_drops_plain_variable_arguments():
    """The exact noise pattern found in the first, unfiltered cross-language
    run: a call argument that is just a local variable/parameter name (not
    a symbol defined anywhere) must be dropped once a known_names set is
    supplied."""
    src = """
function isEmpty(value) {
  return check(value);
}
"""
    known = known_symbol_names({"x.ts": src}, language="typescript")
    edges = extract_all(src, file="x.ts", language="typescript", known_names=known)
    assert not any(e["used_symbol"] == "value" for e in edges)


def test_known_names_none_disables_the_filter():
    """Sanity check the filter is opt-in, not silently mandatory -- callers
    who want the raw (noisier) signal can still get it."""
    src = """
function isEmpty(value) {
  return check(value);
}
"""
    edges = extract_all(src, file="x.ts", language="typescript", known_names=None)
    assert any(e["used_symbol"] == "value" for e in edges)


def test_no_framework_specific_literals_in_treesitter_extractors():
    source = Path(__file__).parent.joinpath("treesitter_extractors.py").read_text().lower()
    _, _, after_module_docstring = source.partition('"""')
    _, _, code_and_other_docstrings = after_module_docstring.partition('"""')
    banned = ("pydantic", "spring", "nestjs", "fastapi", "django", "@bean", "rabbitmq")
    for term in banned:
        assert term not in code_and_other_docstrings, f"found banned literal {term!r} in treesitter_extractors.py"
