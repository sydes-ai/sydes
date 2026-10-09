"""A signature reference is a resolved repository identity, not a spelling coincidence.

The impact interpreter links a changed symbol to an entrypoint whose signature names it (or
the type that owns it). Ownership comes only from the symbol model, never from a qualified
name's position (a CBM qualified name for a field can end `...<package>.<field>`), and the
name in the signature must resolve to the changed symbol's own definition. A resolved
reference is deterministic evidence -- PROVEN, never the guide's inference. No model call.
"""

from __future__ import annotations

from sydes.code_intelligence.base import StructuralFacts
from sydes.impact import (
    IMPACT_STATUS_PROVEN,
    STRATEGY_SIGNATURE_REFERENCE,
    ImpactInterpreter,
)

REPO = "app"


def _entry(symbol: str, file: str, signature: str, path: str) -> dict:
    return {"repo": REPO, "qualified_name": f"proj.{symbol}", "symbol": symbol, "file": file, "line": 3,
            "route_method": "GET", "route_path": path, "decorators": "", "signature": signature}


def _index(files: dict[str, list[dict]], imports: dict[str, list[dict]] | None = None) -> dict:
    imports = imports or {}
    return {"repos": [{"repo": REPO, "files": [
        {"path": path, "symbols": symbols, "imports": imports.get(path, [])} for path, symbols in files.items()]}]}


def _facts(entrypoints: list[dict], symbol_index: dict, usage_edges: list[dict] | None = None) -> StructuralFacts:
    return StructuralFacts(entrypoints=entrypoints, usage_edges=usage_edges or [], provides_call_graph=True,
                           backend="cbm", symbol_index=symbol_index)


ACCOUNT_FILES = {
    "app/account.py": [{"name": "Account", "kind": "class"},
                       {"name": "rename", "kind": "class_method", "parent": "Account"}],
    "app/views.py": [{"name": "show", "kind": "function"}, {"name": "list_all", "kind": "function"}],
}


def test_a_package_segment_colliding_with_a_parameter_name_is_not_a_reference() -> None:
    # the field's only qualified name is a graph name whose second-to-last segment is a package
    learned = {"repo": REPO, "user_file": "app/user/service.py", "user_symbol": "encoder",
               "user_qualified_name": "proj.app.user.encoder", "used_file": "app/codec.py",
               "used_symbol": "Codec", "used_qualified_name": "proj.app.codec.Codec"}
    result = ImpactInterpreter().interpret(
        [{"name": "encoder", "file": "app/user/service.py", "repo": REPO}],
        _facts([_entry("list_all", "app/views.py", "(user: Account, limit: int)", "/articles")],
               _index({"app/user/service.py": [{"name": "encoder", "kind": "variable"}], **ACCOUNT_FILES}),
               usage_edges=[learned]),
    )
    assert result.affected == []


def test_a_resolved_owner_type_in_a_signature_is_proven_deterministic_evidence() -> None:
    result = ImpactInterpreter().interpret(
        [{"name": "rename", "file": "app/account.py", "qualified_name": "Account.rename", "repo": REPO}],
        _facts([_entry("show", "app/views.py", "(item: Account)", "/accounts/{id}")],
               _index(ACCOUNT_FILES, {"app/views.py": [
                   {"local": "Account", "imported": "Account", "resolved_file": "app/account.py"}]})),
    )
    [affected] = result.affected
    assert affected.label == "GET /accounts/{id}"
    assert affected.status == IMPACT_STATUS_PROVEN  # never labelled as the guide's inference
    assert affected.paths[0].strategy == STRATEGY_SIGNATURE_REFERENCE


def test_ownership_from_the_symbol_index_is_honoured_for_a_graph_qualified_name() -> None:
    result = ImpactInterpreter().interpret(
        [{"name": "rename", "file": "app/account.py", "qualified_name": "proj.app.account.Account.rename",
          "repo": REPO}],
        _facts([_entry("show", "app/views.py", "(item: Account)", "/accounts/{id}")], _index(ACCOUNT_FILES)),
    )
    assert [a.label for a in result.affected] == ["GET /accounts/{id}"]


def test_the_same_spelling_with_another_identity_does_not_match() -> None:
    files = {**ACCOUNT_FILES, "app/legacy/account.py": [{"name": "Account", "kind": "class"}]}
    changed = [{"name": "rename", "file": "app/account.py", "qualified_name": "Account.rename", "repo": REPO}]
    signature = [_entry("show", "app/views.py", "(item: Account)", "/accounts/{id}")]
    # the handler's `Account` is imported from the other definition
    legacy = _index(files, {"app/views.py": [
        {"local": "Account", "imported": "Account", "resolved_file": "app/legacy/account.py"}]})
    assert ImpactInterpreter().interpret(changed, _facts(signature, legacy)).affected == []
    # two definitions and no import saying which: not resolvable, so no impact
    assert ImpactInterpreter().interpret(changed, _facts(signature, _index(files))).affected == []


def test_without_a_symbol_model_nothing_is_inferred_from_text() -> None:
    result = ImpactInterpreter().interpret(
        [{"name": "rename", "file": "app/account.py", "qualified_name": "Account.rename", "repo": REPO}],
        _facts([_entry("show", "app/views.py", "(item: Account)", "/accounts/{id}")], {}),
    )
    assert result.affected == []
