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
from member_access_extractor import extract_member_access_edges, edges_only
from member_access_treesitter import extract_member_access, known_type_names


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


# ---------------------------------------------------------------------------
# Member-access extractor (Python) -- bounded receiver-type resolution
# ---------------------------------------------------------------------------


def test_receiver_via_parameter_annotation():
    src = """
class Payload:
    pass

def handler(payload: Payload):
    return payload.value
"""
    entries = extract_member_access_edges(src, file="x.py", repo_root=Path("/tmp"))
    hit = next(e for e in entries if e["function"] == "handler")
    assert hit["status"] == "resolved" and hit["resolved_type"] == "Payload"


def test_receiver_via_local_annotation():
    src = """
class Config:
    pass

def handler():
    cfg: Config
    return cfg.value
"""
    entries = extract_member_access_edges(src, file="x.py", repo_root=Path("/tmp"))
    hit = next(e for e in entries if e["function"] == "handler")
    assert hit["status"] == "resolved" and hit["resolved_type"] == "Config"


def test_receiver_via_constructor_assignment():
    src = """
class Config:
    pass

def handler():
    cfg = Config()
    return cfg.value
"""
    entries = extract_member_access_edges(src, file="x.py", repo_root=Path("/tmp"))
    hit = next(e for e in entries if e["function"] == "handler")
    assert hit["status"] == "resolved" and hit["resolved_type"] == "Config"


def test_ambiguous_receiver_is_unknown_not_guessed():
    """Two different classes could both plausibly be `cfg`'s type -- must
    be reported ambiguous, never resolved to either by a coin flip."""
    src = """
class ConfigA:
    pass

class ConfigB:
    pass

def handler(flag):
    if flag:
        cfg = ConfigA()
    else:
        cfg = ConfigB()
    return cfg.value
"""
    entries = extract_member_access_edges(src, file="x.py", repo_root=Path("/tmp"))
    hit = next(e for e in entries if e["function"] == "handler")
    assert hit["status"] == "ambiguous"
    assert hit["resolved_type"] is None


def test_unresolved_receiver_produces_no_entity_edge():
    src = """
def handler(payload):  # no annotation at all
    return payload.value
"""
    entries = extract_member_access_edges(src, file="x.py", repo_root=Path("/tmp"))
    hit = next(e for e in entries if e["function"] == "handler")
    assert hit["status"] == "unknown"
    assert edges_only(entries) == []


def test_no_string_name_coincidence_resolution():
    """A local variable named `Config` (matching a REAL class's name only
    by string coincidence, never actually bound to an instance of it) must
    not resolve -- only a real constructor call/annotation counts."""
    src = """
class Config:
    pass

def handler():
    Config = "just a string, not an instance"
    return Config.upper()
"""
    entries = extract_member_access_edges(src, file="x.py", repo_root=Path("/tmp"))
    hit = next(e for e in entries if e["function"] == "handler")
    # `Config = "..."` is a plain string assignment, not `Config = Config()`
    # -- the RHS is not a Call at all, so no constructor-assignment
    # candidate is ever produced, regardless of the variable's name.
    assert hit["status"] == "unknown"


def test_cross_file_import_resolution_one_hop(tmp_path: Path):
    """The exact real Settings-case shape: a function-local RELATIVE
    import whose ORIGINAL binding (in a different file) is a direct
    constructor assignment -- resolved via exactly one hop, not chased
    further."""
    (tmp_path / "config.py").write_text("""
class Settings:
    pass

settings = Settings()
""")
    (tmp_path / "schemas.py").write_text("""
def validator(value):
    from .config import settings
    if value > settings.max_output_duration_s:
        raise ValueError("too big")
    return value
""")
    entries = extract_member_access_edges(
        (tmp_path / "schemas.py").read_text(), file="schemas.py", repo_root=tmp_path,
    )
    hit = next(e for e in entries if e["function"] == "validator")
    assert hit["status"] == "resolved"
    assert hit["resolved_type"] == "Settings"
    assert hit["provenance"] == "python_ast_one_hop_import"


def test_known_class_filter_still_applied():
    """A receiver assigned the result of calling something that is NOT a
    known class in this file (a plain function call) must not resolve --
    only a call to a locally-defined CLASS counts as a constructor."""
    src = """
def make_thing():
    return object()

def handler():
    cfg = make_thing()
    return cfg.value
"""
    entries = extract_member_access_edges(src, file="x.py", repo_root=Path("/tmp"))
    hit = next(e for e in entries if e["function"] == "handler")
    assert hit["status"] == "unknown"


def test_no_framework_specific_literals_in_member_access_extractor():
    """The module explains its OWN motivation in prose (mentioning the real
    "Settings" case by name in docstrings is legitimate documentation, not
    a special case) -- what must never appear is a literal CONDITIONAL
    comparison against a specific symbol/framework name, e.g.
    `== "Settings"` or `== "pydantic"`, which would be an actual algorithm
    special-case rather than documentation."""
    source = Path(__file__).parent.joinpath("member_access_extractor.py").read_text()
    banned_comparisons = (
        '== "Settings"', '== "settings"', '== "pydantic"', '== "FastAPI"',
        "== 'Settings'", "== 'settings'", "== 'pydantic'", "== 'FastAPI'",
    )
    for term in banned_comparisons:
        assert term not in source, f"found a literal special-case comparison {term!r}"


def test_exact_real_settings_chain_shape_end_to_end(tmp_path: Path):
    """Faithful synthetic repro of the real chain this experiment was
    built to recover -- including the function-local RELATIVE import,
    the exact shape the real case uses (see settings_case_analysis.md):
    Settings.max_output_duration_s -> _within_duration_ceiling (member
    access, one-hop cross-file) -> Duration (call-argument reference) ->
    OpenAISpeechRequest (class-field type reference)."""
    from python_extractors import extract_all_same_file

    (tmp_path / "config.py").write_text("""
class Settings:
    pass

settings = Settings()
""")
    schemas_src = """
def _within_duration_ceiling(value):
    from .config import settings
    if value > settings.max_output_duration_s:
        raise ValueError("too big")
    return value

Duration = Annotated[float, AfterValidator(_within_duration_ceiling)]

class OpenAISpeechRequest:
    max_duration_seconds: Optional[Duration]
"""
    (tmp_path / "schemas.py").write_text(schemas_src)

    decl_edges = extract_all_same_file(schemas_src, file="schemas.py")
    member_edges = edges_only(extract_member_access_edges(schemas_src, file="schemas.py", repo_root=tmp_path))
    all_edges = decl_edges + member_edges

    by_used: dict[str, list[str]] = {}
    for e in all_edges:
        by_used.setdefault(e["used_symbol"], []).append(e["user_symbol"])

    assert "_within_duration_ceiling" in by_used["Settings"]
    assert "Duration" in by_used["_within_duration_ceiling"]
    assert "OpenAISpeechRequest" in by_used["Duration"]


# ---------------------------------------------------------------------------
# Member-access extractor (tree-sitter, cross-language)
# ---------------------------------------------------------------------------


def test_treesitter_member_access_resolves_local_constructor_receiver():
    src = """
class SomeType {}
class Foo {
  method() {
    const x: SomeType = new SomeType();
    x.value;
  }
}
"""
    known = known_type_names({"x.ts": src}, language="typescript")
    entries = extract_member_access(src, file="x.ts", language="typescript", known_names=known)
    hit = next(e for e in entries if e["receiver"] == "x")
    assert hit["status"] == "resolved" and hit["resolved_type"] == "SomeType"


def test_treesitter_member_access_leaves_unresolved_receiver_unknown():
    src = """
class Foo {
  method(other) {
    other.value;
  }
}
"""
    known = known_type_names({"x.ts": src}, language="typescript")
    entries = extract_member_access(src, file="x.ts", language="typescript", known_names=known)
    hit = next(e for e in entries if e["receiver"] == "other")
    assert hit["status"] == "unknown"


def test_treesitter_static_access_resolves_when_receiver_is_a_known_type():
    """The RabbitConsts.SOME_CONST shape: the receiver IS ALREADY a known
    type name -- zero inference needed, and safe by construction."""
    src = """
interface RabbitConsts {
    String QUEUE_ONE = "queue.one";
}
class Foo {
  void method() {
    String q = RabbitConsts.QUEUE_ONE;
  }
}
"""
    known = known_type_names({"x.java": src}, language="java")
    entries = extract_member_access(src, file="x.java", language="java", known_names=known)
    hit = next(e for e in entries if e["receiver"] == "RabbitConsts")
    assert hit["status"] == "resolved" and hit["resolved_type"] == "RabbitConsts"


def test_treesitter_known_type_names_excludes_function_names():
    """Regression pin for a REAL false positive found during this
    experiment: a local variable named `info` matched an unrelated
    method also named `info` in a different scanned file, purely by
    string coincidence, when the (broader) known_symbol_names set --
    which includes function/method names -- was used for the "receiver is
    already known" shortcut. known_type_names must exclude them."""
    method_file = """
class Fairing {
  info() {
    return 1;
  }
}
"""
    usage_file = """
class Foo {
  finalize() {
    let info = something();
    info.data_type;
  }
}
"""
    known = known_type_names({"a.ts": method_file, "b.ts": usage_file}, language="typescript")
    assert "info" not in known  # it's a METHOD name, not a type name

    entries = extract_member_access(usage_file, file="b.ts", language="typescript", known_names=known)
    hit = next(e for e in entries if e["receiver"] == "info")
    assert hit["status"] == "unknown"


def test_no_framework_specific_literals_in_member_access_treesitter():
    source = Path(__file__).parent.joinpath("member_access_treesitter.py").read_text()
    banned_comparisons = (
        '== "Settings"', '== "pydantic"', '== "Spring"', '== "RabbitConsts"',
        "== 'Settings'", "== 'pydantic'", "== 'Spring'", "== 'RabbitConsts'",
    )
    for term in banned_comparisons:
        assert term not in source, f"found a literal special-case comparison {term!r}"
