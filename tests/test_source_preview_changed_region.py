"""A changed symbol's source preview must show the region that actually
changed, not the first few statements of a possibly-large symbol.

Root cause this pins (ory/kratos#4277): `Handler.patch` spans lines 900-964
and its only change is at line 919. The preview took `statements[:6]` from
the declaration, stopped before line 919, and so presented *unchanged*
neighbouring code — including a `WithAdminMetadataInJSON(...)` call — as the
evidence for what changed. The guide then inferred an "admin metadata"
behavior that the PR does not support. Reproduced across two independent
runs, so this is an evidence-supply defect, not model noise.

The complementary case is the attached-declaration-metadata one (a
Python/TypeScript decorator, a Rust outer attribute): there the change sits
*above* the declaration line, and the slicer's statements structurally begin
at the declaration, so a preview with no raw-line prefix would contain no
evidence of the change at all.

These tests drive `source_preview` against real temp files (the slicer reads
source), never a live parser or CBM session.
"""

from __future__ import annotations

from pathlib import Path

from sydes.impact.investigate import (
    _PREVIEW_ELISION,
    _PREVIEW_MAX_CHARS,
    _PREVIEW_MAX_STATEMENTS,
    source_preview,
)
from sydes.impact.models import SymbolIdentity

REPO = "app"


class _Facts:
    """The single `facts` method `_symbol_dict_for` calls."""

    def __init__(self, symbols: list[dict]) -> None:
        self._symbols = symbols

    def symbols_for_file(self, repo: str, file: str) -> list[dict]:
        return [s for s in self._symbols if s.get("file") == file]


def _identity(name: str, file: str) -> SymbolIdentity:
    return SymbolIdentity.from_fields(repo=REPO, file=file, short_name=name)


def _facts_for(name: str, file: str, start: int, end: int, language: str = "python") -> _Facts:
    return _Facts([{
        "name": name, "file": file, "start_line": start, "end_line": end,
        "language": language,
    }])


def _write_large_function(root: Path) -> Path:
    """A function starting at line 10 whose only change is around line 40."""
    lines = ["# module header"] * 9                      # lines 1-9
    lines.append("def handle(request):")                  # line 10
    for n in range(11, 40):                                # lines 11-39
        lines.append(f"    step_{n} = compute_{n}(request)")
    lines.append("    result = apply_policy(request, DENY_ALL)")  # line 40  <-- the change
    for n in range(41, 61):                                # lines 41-60
        lines.append(f"    trailing_{n} = finalize_{n}(result)")
    lines.append("    return result")                      # line 61
    path = root / "handler.py"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --- 1. Large function: change far from the declaration -------------------

def test_large_function_preview_shows_the_changed_region_not_the_head(tmp_path: Path) -> None:
    """The exact Kratos shape."""
    _write_large_function(tmp_path)
    facts = _facts_for("handle", "handler.py", 10, 61)

    preview = source_preview(
        _identity("handle", "handler.py"), facts, tmp_path,
        changed_line_ranges=[(40, 40)],
    )

    assert "apply_policy" in preview, preview
    assert "DENY_ALL" in preview, preview
    # ... and it is NOT merely the head of the body.
    assert "step_11" not in preview, preview
    assert "step_12" not in preview, preview


def test_head_of_body_is_what_the_old_behavior_would_have_shown(tmp_path: Path) -> None:
    """Pins the contrast: with no range information the preview is exactly
    the previous head-of-body read, which for this symbol contains no
    evidence of the change at all."""
    _write_large_function(tmp_path)
    facts = _facts_for("handle", "handler.py", 10, 61)

    preview = source_preview(_identity("handle", "handler.py"), facts, tmp_path)

    assert "step_11" in preview
    assert "apply_policy" not in preview


# --- 2. Multiple changed lines / regions ----------------------------------

def test_adjacent_changed_lines_are_represented_once_with_context(tmp_path: Path) -> None:
    _write_large_function(tmp_path)
    facts = _facts_for("handle", "handler.py", 10, 61)

    preview = source_preview(
        _identity("handle", "handler.py"), facts, tmp_path,
        changed_line_ranges=[(39, 41)],
    )

    assert "apply_policy" in preview
    assert preview.count("apply_policy") == 1, preview
    # Neighbouring statements from the same region provide the context.
    assert "step_39" in preview or "trailing_41" in preview, preview


def test_two_distant_changed_regions_are_both_represented(tmp_path: Path) -> None:
    """A budget shared between two far-apart changed regions must not be
    spent entirely on the first."""
    _write_large_function(tmp_path)
    facts = _facts_for("handle", "handler.py", 10, 61)

    preview = source_preview(
        _identity("handle", "handler.py"), facts, tmp_path,
        changed_line_ranges=[(12, 12), (40, 40)],
    )

    assert "step_12" in preview, preview
    assert "apply_policy" in preview, preview


# --- 3/4. Change near the beginning and near the end ----------------------

def test_change_near_the_symbol_beginning_behaves_sensibly(tmp_path: Path) -> None:
    _write_large_function(tmp_path)
    facts = _facts_for("handle", "handler.py", 10, 61)

    preview = source_preview(
        _identity("handle", "handler.py"), facts, tmp_path,
        changed_line_ranges=[(11, 11)],
    )

    assert "step_11" in preview, preview
    # Starting at the first statement, nothing was elided ahead of it.
    assert not preview.startswith(_PREVIEW_ELISION), preview


def test_change_near_the_symbol_end_behaves_sensibly(tmp_path: Path) -> None:
    _write_large_function(tmp_path)
    facts = _facts_for("handle", "handler.py", 10, 61)

    preview = source_preview(
        _identity("handle", "handler.py"), facts, tmp_path,
        changed_line_ranges=[(60, 61)],
    )

    assert "trailing_60" in preview or "return result" in preview, preview
    assert "step_11" not in preview, preview


def test_a_selected_window_is_marked_as_elided(tmp_path: Path) -> None:
    """Honest evidence presentation: a preview that does not start at the
    symbol's first statement says so, so neither reader nor model mistakes
    it for a head-of-body read."""
    _write_large_function(tmp_path)
    facts = _facts_for("handle", "handler.py", 10, 61)

    preview = source_preview(
        _identity("handle", "handler.py"), facts, tmp_path,
        changed_line_ranges=[(40, 40)],
    )
    assert preview.startswith(_PREVIEW_ELISION), preview


# --- 5. Attached decorator metadata ---------------------------------------

def test_changed_python_decorator_above_the_declaration_is_included(tmp_path: Path) -> None:
    """The NetBox shape: the decorator IS the change, and it sits above the
    class's own span, so the slicer's statements can never contain it."""
    path = tmp_path / "views.py"
    path.write_text(
        "@register_model_view(DataFile)\n"                     # line 1
        "@method_decorator(never_cache, name='dispatch')\n"    # line 2  <-- the change
        "class DataFileView(generic.ObjectView):\n"            # line 3
        "    queryset = DataFile.objects.all()\n"              # line 4
        "    actions = (DeleteObject,)\n",                     # line 5
        encoding="utf-8",
    )
    facts = _facts_for("DataFileView", "views.py", 3, 5)

    preview = source_preview(
        _identity("DataFileView", "views.py"), facts, tmp_path,
        changed_line_ranges=[(2, 2)],
    )

    assert "method_decorator" in preview, preview
    assert "never_cache" in preview, preview


def test_changed_typescript_decorator_above_the_declaration_is_included(tmp_path: Path) -> None:
    path = tmp_path / "resolver.ts"
    path.write_text(
        "@Resolver()\n"                             # line 1  <-- the change
        "export class ProductResolver {\n"          # line 2
        "  private readonly x = 1;\n"               # line 3
        "}\n",                                       # line 4
        encoding="utf-8",
    )
    facts = _facts_for("ProductResolver", "resolver.ts", 2, 4, language="typescript")

    preview = source_preview(
        _identity("ProductResolver", "resolver.ts"), facts, tmp_path,
        changed_line_ranges=[(1, 1)],
    )
    assert "@Resolver()" in preview, preview


# --- 6. Rust attached attribute -------------------------------------------

def test_changed_rust_outer_attribute_above_the_declaration_is_included(tmp_path: Path) -> None:
    path = tmp_path / "post.rs"
    path.write_text(
        "#[derive(Clone, Debug, Serialize)]\n"                          # line 1
        "#[cfg_attr(feature = \"full\", diesel(table_name = post))]\n"  # line 2  <-- the change
        "pub struct Post {\n"                                            # line 3
        "  pub id: PostId,\n"                                            # line 4
        "}\n",                                                            # line 5
        encoding="utf-8",
    )
    facts = _facts_for("Post", "post.rs", 3, 5, language="rust")

    preview = source_preview(
        _identity("Post", "post.rs"), facts, tmp_path,
        changed_line_ranges=[(2, 2)],
    )
    assert "cfg_attr" in preview, preview


# --- 7. Fallback when no position information exists ----------------------

def test_no_range_information_preserves_the_previous_behavior(tmp_path: Path) -> None:
    _write_large_function(tmp_path)
    facts = _facts_for("handle", "handler.py", 10, 61)
    identity = _identity("handle", "handler.py")

    assert source_preview(identity, facts, tmp_path) == source_preview(
        identity, facts, tmp_path, changed_line_ranges=None,
    )
    assert source_preview(identity, facts, tmp_path, changed_line_ranges=[]) == source_preview(
        identity, facts, tmp_path,
    )


def test_unusable_range_shapes_fall_back_rather_than_crash(tmp_path: Path) -> None:
    _write_large_function(tmp_path)
    facts = _facts_for("handle", "handler.py", 10, 61)
    identity = _identity("handle", "handler.py")
    baseline = source_preview(identity, facts, tmp_path)

    for bad in ([(0, 0)], [(-3, -1)], [("a", "b")], [None], [(None, 5)]):
        assert source_preview(identity, facts, tmp_path, changed_line_ranges=bad) == baseline


def test_hunks_touching_only_another_symbol_in_the_file_fall_back(tmp_path: Path) -> None:
    """Per-file hunks are supplied, so a symbol whose own lines are untouched
    must fall back rather than render an empty or arbitrary window."""
    _write_large_function(tmp_path)
    facts = _facts_for("handle", "handler.py", 10, 61)
    identity = _identity("handle", "handler.py")

    # Line 5 is module header, far outside `handle`'s span.
    assert source_preview(
        identity, facts, tmp_path, changed_line_ranges=[(5, 5)],
    ) == source_preview(identity, facts, tmp_path)


def test_hunk_objects_with_start_line_attributes_are_accepted(tmp_path: Path) -> None:
    """Tolerant of the `Hunk`-like shape the change layer already holds."""
    class _Hunk:
        def __init__(self, start: int, end: int) -> None:
            self.start_line, self.end_line = start, end

    _write_large_function(tmp_path)
    facts = _facts_for("handle", "handler.py", 10, 61)

    preview = source_preview(
        _identity("handle", "handler.py"), facts, tmp_path,
        changed_line_ranges=[_Hunk(40, 40)],
    )
    assert "apply_policy" in preview, preview


# --- 8. Bounded -----------------------------------------------------------

def test_preview_stays_bounded_in_every_path(tmp_path: Path) -> None:
    _write_large_function(tmp_path)
    facts = _facts_for("handle", "handler.py", 10, 61)
    identity = _identity("handle", "handler.py")

    everything = [(n, n) for n in range(10, 62)]  # every line changed
    for ranges in (None, [(40, 40)], [(12, 12), (40, 40)], everything):
        preview = source_preview(identity, facts, tmp_path, changed_line_ranges=ranges)
        assert len(preview) <= _PREVIEW_MAX_CHARS


def test_selection_never_exceeds_the_line_budget(tmp_path: Path) -> None:
    """Even with every line changed, the window respects the line cap —
    verified structurally, not just by the character bound."""
    from sydes.impact.investigate import _PREVIEW_MAX_LINES, changed_region_source

    _write_large_function(tmp_path)
    span = {
        "file": "handler.py", "start_line": 10, "end_line": 61,
        "language": "python", "name": "handle",
    }
    preview = changed_region_source(
        span, [(n, n) for n in range(10, 62)], repo_root=tmp_path,
    )
    # Elision markers are not source lines; count only real content.
    content = [p for p in preview.split(" ") if p != _PREVIEW_ELISION]
    assert content
    assert preview.count("=") <= _PREVIEW_MAX_LINES


def test_a_change_inside_a_slicer_merged_block_is_still_shown(tmp_path: Path) -> None:
    """The case that forced raw-line selection over statement selection: a
    brace-delimited language whose statement splitter merges many raw lines
    into one block. Statement granularity would select that whole block and
    the change — at its far end — would fall past the character budget.

    Modeled on the real Go handler: one `if` block spanning ~19 lines whose
    only changed line is the `if` itself, preceded by unchanged code that
    previously dominated the preview.
    """
    path = tmp_path / "handler.go"
    body = ["package main", "", "func patch(w, r) {"]           # lines 1-3
    for n in range(4, 20):                                       # lines 4-19
        body.append(f"\tunchanged_{n} := setup_{n}(r)")
    body.append('\tpatchedIdentity := WithAdminMetadata(*identity)')   # line 20
    body.append('\tif err := ApplyJSONPatch(body, "/credentials/oidc/**"); err != nil {')  # 21 <- change
    body.append("\t\twriteError(w, r, err)")                     # line 22
    body.append("\t\treturn")                                     # line 23
    body.append("\t}")                                             # line 24
    body.append("}")                                                # line 25
    path.write_text("\n".join(body) + "\n", encoding="utf-8")

    facts = _facts_for("patch", "handler.go", 3, 25, language="go")
    preview = source_preview(
        _identity("patch", "handler.go"), facts, tmp_path,
        changed_line_ranges=[(21, 21)],
    )

    assert "/credentials/oidc/**" in preview, preview
    assert "unchanged_4" not in preview, preview


# --- 10. Existing callers keep working ------------------------------------

def test_existing_positional_call_signature_still_works(tmp_path: Path) -> None:
    """`boundary_reasoning` calls `source_preview(identity, facts, root)`
    positionally and passes no ranges — that call must be unchanged."""
    _write_large_function(tmp_path)
    facts = _facts_for("handle", "handler.py", 10, 61)

    preview = source_preview(_identity("handle", "handler.py"), facts, tmp_path)
    assert preview
    assert "step_11" in preview


def test_missing_repo_root_and_unknown_symbol_still_return_empty(tmp_path: Path) -> None:
    facts = _facts_for("handle", "handler.py", 10, 61)
    assert source_preview(_identity("handle", "handler.py"), facts, None) == ""
    assert source_preview(
        _identity("nope", "handler.py"), facts, tmp_path, changed_line_ranges=[(40, 40)],
    ) == ""
