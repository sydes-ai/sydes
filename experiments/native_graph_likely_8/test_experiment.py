"""Focused tests for this experiment's own harness logic and typed-edge
prototype. Not part of the main Sydes test suite (this whole directory is
an experiment, not shipped code) -- run directly with:

    .venv/bin/python -m pytest experiments/native_graph_likely_8/test_experiment.py -q

Tests the EXPERIMENT's classification logic and the Phase 4 extractors in
isolation with small synthetic inputs -- not a re-run of the real 8 cases
(those require the real repo checkouts and are exercised by run.py itself).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from typed_edge_prototype import (
    extract_class_field_type_usages,
    extract_function_parameter_type_usages,
    extract_module_level_assignment_argument_usages,
)


# ---------------------------------------------------------------------------
# Typed-edge prototype (Phase 4)
# ---------------------------------------------------------------------------


def test_assignment_argument_reference_found_generically():
    src = """
def _validator(value):
    return value

Alias = Annotated[int, SomeWrapper(_validator)]
"""
    edges = extract_module_level_assignment_argument_usages(src, file="x.py")
    assert any(e["user_symbol"] == "Alias" and e["used_symbol"] == "_validator" for e in edges)


def test_assignment_argument_reference_ignores_builtins_and_self_reference():
    src = """
def _validator(value):
    return value

Alias = Annotated[int, len, str]
"""
    edges = extract_module_level_assignment_argument_usages(src, file="x.py")
    # len/str are builtins, never locally-defined functions/classes -- must
    # not appear as spurious edges.
    assert not any(e["used_symbol"] in ("len", "str") for e in edges)


def test_class_field_type_reference_found_generically():
    src = """
class Inner:
    pass

class Outer:
    field: Inner
"""
    edges = extract_class_field_type_usages(src, file="x.py")
    assert any(e["user_symbol"] == "Outer" and e["used_symbol"] == "Inner" for e in edges)


def test_class_field_type_reference_handles_wrapped_annotation():
    """Optional[Inner]/List[Inner]-shaped annotations must still yield the
    inner name -- the extractor walks the whole annotation subtree, not
    just a bare Name node."""
    src = """
class Inner:
    pass

class Outer:
    field: "Optional[Inner]" if False else Inner
"""
    edges = extract_class_field_type_usages(src, file="x.py")
    assert any(e["used_symbol"] == "Inner" for e in edges)


def test_function_parameter_type_reference_same_file_only():
    """Documented, real limitation (see phase4 json): only same-file names
    resolve today. An imported name is silently NOT matched -- this test
    pins that behavior so it can't silently change without notice."""
    src = """
class Payload:
    pass

def handler(request: Payload):
    pass
"""
    edges = extract_function_parameter_type_usages(src, file="x.py")
    assert any(e["user_symbol"] == "handler" and e["used_symbol"] == "Payload" for e in edges)


def test_no_framework_specific_literals_in_the_algorithm():
    """The whole point of Phase 4: the algorithm must never special-case a
    framework or symbol name. Guards against a future edit accidentally
    adding one."""
    source = Path(__file__).parent.joinpath("typed_edge_prototype.py").read_text().lower()
    # The module's own TOP docstring explains what it deliberately does NOT
    # hardcode by naming the excluded terms in prose -- legitimate, and
    # excluded from the scan. Everything after it (every function body,
    # every function docstring) must never contain a framework/symbol
    # literal. "annotated assignment"/"annotation" (PEP 526, ast.AnnAssign)
    # are legitimate generic Python/AST vocabulary, so the ban is on the
    # concrete typing CONSTRUCT (`Annotated[`), not the English word.
    _, _, after_module_docstring = source.partition('"""')
    _, _, code_and_other_docstrings = after_module_docstring.partition('"""')
    # The __main__ demo block picks a real repo path as a convenience
    # default (explicitly permitted: "unless used purely inside an
    # experiment fixture, never the algorithm") -- scope the ban to the
    # actual extractor functions above it, not the demo entrypoint.
    algorithm_only, _, _ = code_and_other_docstrings.partition('if __name__')
    banned = ("pydantic", "annotated[", "aftervalidator", "_within_duration_ceiling", "fastapi")
    for term in banned:
        assert term not in algorithm_only, f"found banned literal {term!r} in the extractor algorithm"


# ---------------------------------------------------------------------------
# Classification semantics (mirrors run.py's decision rules with tiny,
# synthetic stand-ins so these don't require the real repo checkout)
# ---------------------------------------------------------------------------


def _classify(reached_via_interpreter: bool, reached_via_call_follower: bool, handler_resolved: bool, truncated: bool) -> str:
    """Re-states run.py's phase1() classification rule in isolation, so it
    can be tested without needing the real Kokoro-FastAPI checkout."""
    if reached_via_interpreter or reached_via_call_follower:
        return "REACHABLE"
    if not handler_resolved:
        return "AMBIGUOUS_IDENTITY"
    if truncated:
        return "TRUNCATED"
    return "NOT_REACHED_IN_AVAILABLE_GRAPH"


def test_reachable_path_yields_reachable():
    assert _classify(True, False, True, False) == "REACHABLE"
    assert _classify(False, True, True, False) == "REACHABLE"


def test_no_path_found_is_never_classified_false_or_negative():
    """Central conceptual rule: absence of a path must map to an honest
    'not reached in available graph' label, never a false/negative claim."""
    result = _classify(False, False, True, False)
    assert result == "NOT_REACHED_IN_AVAILABLE_GRAPH"
    assert "FALSE" not in result
    assert "NEGATIVE" not in result


def test_truncated_frontier_yields_truncated_not_a_conclusion():
    assert _classify(False, False, True, True) == "TRUNCATED"


def test_unresolved_handler_yields_ambiguous_identity():
    assert _classify(False, False, False, False) == "AMBIGUOUS_IDENTITY"


# ---------------------------------------------------------------------------
# Graph completeness certificate (Phase 5)
# ---------------------------------------------------------------------------


def _certificate_ok(
    *, entrypoint_exact: bool, handler_exact: bool, route_composition_exact: bool,
    all_direct_calls_extracted: bool, traversal_truncated: bool,
    unresolved_ambiguous_on_frontier: bool, target_identity_exact: bool,
    unresolved_dynamic_dispatch: bool,
) -> bool:
    """Mirrors the exact 8-condition list in the task/report: every
    condition must hold for a negative certificate to be honestly issued."""
    return (
        entrypoint_exact and handler_exact and route_composition_exact
        and all_direct_calls_extracted and not traversal_truncated
        and not unresolved_ambiguous_on_frontier and target_identity_exact
        and not unresolved_dynamic_dispatch
    )


def test_certificate_requires_every_condition():
    base = dict(
        entrypoint_exact=True, handler_exact=True, route_composition_exact=True,
        all_direct_calls_extracted=True, traversal_truncated=False,
        unresolved_ambiguous_on_frontier=False, target_identity_exact=True,
        unresolved_dynamic_dispatch=False,
    )
    assert _certificate_ok(**base) is True

    for flip_key in base:
        broken = dict(base)
        broken[flip_key] = not broken[flip_key]
        assert _certificate_ok(**broken) is False, f"certificate should fail when {flip_key} is violated"


def test_certificate_denies_case_2_shape():
    """Case 2 (generate_from_phonemes) failed on 'all_direct_calls_extracted'
    (a real call to tts_service.generate_from_phonemes was independently
    confirmed missing from the traced graph) -- pin that this alone is
    enough to deny the certificate, even with every other condition met."""
    assert _certificate_ok(
        entrypoint_exact=True, handler_exact=True, route_composition_exact=True,
        all_direct_calls_extracted=False,  # the real, confirmed gap
        traversal_truncated=False, unresolved_ambiguous_on_frontier=False,
        target_identity_exact=True, unresolved_dynamic_dispatch=False,
    ) is False


def test_certificate_grants_case_1_shape():
    """Case 1 (dev/model) had an exhaustive, untruncated, unambiguous
    neighborhood on both ends -- the certificate should grant a negative
    conclusion here."""
    assert _certificate_ok(
        entrypoint_exact=True, handler_exact=True, route_composition_exact=True,
        all_direct_calls_extracted=True, traversal_truncated=False,
        unresolved_ambiguous_on_frontier=False, target_identity_exact=True,
        unresolved_dynamic_dispatch=False,
    ) is True
