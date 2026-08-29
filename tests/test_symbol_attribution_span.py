"""Symbol attribution span: a decorator (Python/TypeScript) or an outer
attribute (Rust) sitting immediately above a declaration is semantically
part of it, even though CBM's own `[start_line, end_line]` excludes it (Java
already includes annotations; Go's leading comments are deliberately out of
scope here — see `symbol_attribution_span`'s module docstring).

Two levels: unit tests against `symbol_attribution_span`/
`language_for_attribution` directly (full control over `file_lines`, no
parser or repo needed), and integration tests against
`attribute_changed_symbols` itself, using hand-built `ChangeSet`/`ChangedFile`
/`Hunk` and CBM-shaped symbol dicts (exactly the shape CBM was empirically
observed to return) plus a real temp file for the `repo_root` source read —
never a live parser or CBM session.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sydes.ingest.file_roles import FILE_ROLE_TEST_USAGE_CANDIDATE, classify_candidate_file_role
from sydes.verify.analyzer import attribute_changed_symbols
from sydes.verify.models import CHANGE_MODIFIED, ChangeSet, ChangedFile, Hunk
from sydes.verify.symbol_attribution_span import language_for_attribution, symbol_attribution_span

REPO = "app"


# --- unit tests: symbol_attribution_span / language_for_attribution --------


def test_language_for_attribution_maps_known_suffixes() -> None:
    assert language_for_attribution("app/views.py") == "python"
    assert language_for_attribution("app/resolver.ts") == "typescript"
    assert language_for_attribution("app/resolver.tsx") == "typescript"
    assert language_for_attribution("app/widget.js") == "typescript"
    assert language_for_attribution("app/lib.rs") == "rust"


def test_language_for_attribution_reports_unknown_for_java_and_go() -> None:
    """Java and Go are deliberate no-ops here: Java already includes
    annotations in CBM's own span; Go's leading comments are out of scope."""
    assert language_for_attribution("app/Controller.java") == "unknown"
    assert language_for_attribution("app/handler.go") == "unknown"


# 1. Python: changed class decorator => class widened to include it.
def test_python_class_decorator_widens_span() -> None:
    lines = [
        "@method_decorator(never_cache, name='dispatch')",  # line 1
        "class DataFileView(generic.ObjectView):",          # line 2
        "    queryset = DataFile.objects.all()",            # line 3
    ]
    start, end = symbol_attribution_span(
        start_line=2, end_line=3, file_lines=lines, language="python",
    )
    assert (start, end) == (1, 3)


# 2. Python: changed function decorator => function widened.
def test_python_function_decorator_widens_span() -> None:
    lines = ["@lru_cache", "def compute():", "    return 1"]
    start, end = symbol_attribution_span(
        start_line=2, end_line=3, file_lines=lines, language="python",
    )
    assert (start, end) == (1, 3)


# 3. Python: stacked decorators => all consumed.
def test_python_stacked_decorators_all_widen() -> None:
    lines = [
        "@register_model_view(DataFile)",   # line 1
        "@method_decorator(never_cache, name='dispatch')",  # line 2
        "class DataFileView(generic.ObjectView):",  # line 3
        "    pass",  # line 4
    ]
    start, end = symbol_attribution_span(
        start_line=3, end_line=4, file_lines=lines, language="python",
    )
    assert (start, end) == (1, 4)


# 4. Python: multiline decorator => NOT widened (documented limitation).
def test_python_multiline_decorator_is_not_widened() -> None:
    """A decorator whose argument list continues onto further lines is
    exactly the construct `_is_attached_metadata_line` deliberately does not
    recognize (see its docstring) — the line directly above the declaration
    is a bare continuation (`)`), which does not match the single-line
    decorator pattern, so the scan stops immediately and the original span
    is retained rather than guessed at."""
    lines = [
        "@some_decorator(",       # line 1 -- the true start, unreachable
        "    arg1,",              # line 2
        "    arg2,",              # line 3
        ")",                      # line 4 -- does not look like "@..."
        "class Example:",         # line 5
        "    pass",                # line 6
    ]
    start, end = symbol_attribution_span(
        start_line=5, end_line=6, file_lines=lines, language="python",
    )
    assert (start, end) == (5, 6)  # unchanged: no false widening


# 5. Python: unrelated preceding statement, separated by a blank line.
def test_python_unrelated_preceding_statement_is_not_absorbed() -> None:
    lines = [
        "do_something()",   # line 1
        "",                  # line 2 -- blank line terminates attachment
        "@decorator",       # line 3
        "class Foo:",        # line 4
        "    pass",           # line 5
    ]
    start, end = symbol_attribution_span(
        start_line=4, end_line=5, file_lines=lines, language="python",
    )
    assert (start, end) == (3, 5)  # only the decorator, never do_something()


# 6. Python: decorator belonging to another declaration => no cross-attribution.
def test_python_decorator_of_a_different_declaration_is_not_absorbed() -> None:
    lines = [
        "@decorator_for_bar",   # line 1
        "def bar():",            # line 2
        "    pass",               # line 3
        "",                       # line 4
        "@decorator_for_foo",    # line 5
        "def foo():",             # line 6
        "    pass",                # line 7
    ]
    start, end = symbol_attribution_span(
        start_line=6, end_line=7, file_lines=lines, language="python",
    )
    assert (start, end) == (5, 7)  # foo's own decorator only, never bar's


# 7. Ordinary body-line change: ordinary declaration with nothing above it.
def test_python_undecorated_declaration_is_unaffected() -> None:
    lines = ["x = 1", "def plain():", "    return 1"]
    start, end = symbol_attribution_span(
        start_line=2, end_line=3, file_lines=lines, language="python",
    )
    assert (start, end) == (2, 3)


# --- TypeScript ---------------------------------------------------------


# 8. TS: changed class decorator => widened.
def test_typescript_class_decorator_widens_span() -> None:
    lines = ["@Resolver()", "export class ProductResolver {", "  x = 1;", "}"]
    start, end = symbol_attribution_span(
        start_line=2, end_line=4, file_lines=lines, language="typescript",
    )
    assert (start, end) == (1, 4)


# 9. TS: changed method decorator => widened.
def test_typescript_method_decorator_widens_span() -> None:
    lines = [
        "  @Query()",                     # line 1
        "  @Allow(Permission.ReadProduct)",  # line 2
        "  async products() {",             # line 3
        "  }",                               # line 4
    ]
    start, end = symbol_attribution_span(
        start_line=3, end_line=4, file_lines=lines, language="typescript",
    )
    assert (start, end) == (1, 4)


# 10. TS: stacked decorators => all consumed (same fixture as 9).
def test_typescript_stacked_decorators_all_widen() -> None:
    lines = ["@Query()", "@Allow('x')", "async products() {", "}"]
    start, end = symbol_attribution_span(
        start_line=3, end_line=4, file_lines=lines, language="typescript",
    )
    assert (start, end) == (1, 4)


# 11. TS: unrelated prior expression => no false attribution.
def test_typescript_unrelated_prior_expression_is_not_absorbed() -> None:
    lines = ["console.log('hi');", "", "@Injectable()", "export class Foo {}"]
    start, end = symbol_attribution_span(
        start_line=4, end_line=4, file_lines=lines, language="typescript",
    )
    assert (start, end) == (3, 4)


# --- Rust -----------------------------------------------------------------


# 12. Rust: changed #[derive(...)] => widened.
def test_rust_derive_attribute_widens_span() -> None:
    lines = ["#[derive(Clone, Debug, Serialize, Deserialize)]", "pub struct Post {", "}"]
    start, end = symbol_attribution_span(
        start_line=2, end_line=3, file_lines=lines, language="rust",
    )
    assert (start, end) == (1, 3)


# 13. Rust: changed #[cfg_attr(...)] => widened.
def test_rust_cfg_attr_widens_span() -> None:
    lines = ["#[cfg_attr(feature = \"full\", derive(Queryable))]", "pub struct Post {", "}"]
    start, end = symbol_attribution_span(
        start_line=2, end_line=3, file_lines=lines, language="rust",
    )
    assert (start, end) == (1, 3)


# 14. Rust: stacked attributes => all consumed.
def test_rust_stacked_attributes_all_widen() -> None:
    lines = [
        "#[skip_serializing_none]",                                      # line 1
        "#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]",    # line 2
        "#[cfg_attr(feature = \"full\", diesel(table_name = post))]",   # line 3
        "pub struct Post {",                                              # line 4
        "}",                                                                # line 5
    ]
    start, end = symbol_attribution_span(
        start_line=4, end_line=5, file_lines=lines, language="rust",
    )
    assert (start, end) == (1, 5)


# 15. Rust: unrelated previous declaration/statement => no false attribution.
def test_rust_unrelated_previous_declaration_is_not_absorbed() -> None:
    lines = [
        "pub struct Other { pub x: i32 }",  # line 1
        "",                                   # line 2 -- terminates
        "#[derive(Debug)]",                  # line 3
        "pub struct Post {",                   # line 4
        "}",                                    # line 5
    ]
    start, end = symbol_attribution_span(
        start_line=4, end_line=5, file_lines=lines, language="rust",
    )
    assert (start, end) == (3, 5)


# 15b. Rust: a doc comment sandwiched between attributes and the item is
# looked past, not treated as a stop — the real, common Rust idiom
# (confirmed against `lemmy`'s own `Post` struct: attributes, then `///`,
# then `pub struct`, no blank line) that a naive "any non-match stops"
# rule would otherwise defeat entirely.
def test_rust_doc_comment_between_attributes_and_item_is_looked_past() -> None:
    lines = [
        "#[derive(Clone, Debug)]",   # line 1
        "#[cfg_attr(feature = \"full\", diesel(table_name = post))]",  # line 2
        "/// A post.",                # line 3 -- doc comment, not a stop
        "pub struct Post {",            # line 4
        "}",                              # line 5
    ]
    start, end = symbol_attribution_span(
        start_line=4, end_line=5, file_lines=lines, language="rust",
    )
    assert (start, end) == (1, 5)


# 15c. Rust: the comment itself is never absorbed into the span -- only
# looked past. A blank line beyond it still terminates normally.
def test_rust_doc_comment_alone_with_no_attributes_above_is_not_absorbed() -> None:
    lines = [
        "do_something();",   # line 1
        "",                    # line 2 -- terminates
        "/// A post.",          # line 3 -- comment, looked past, finds nothing
        "pub struct Post {",      # line 4
        "}",                        # line 5
    ]
    start, end = symbol_attribution_span(
        start_line=4, end_line=5, file_lines=lines, language="rust",
    )
    assert (start, end) == (4, 5)  # unchanged: comment alone widens nothing


# 15d. Python deliberately does NOT get comment pass-through: a `#` comment
# between a decorator and the declaration remains a plain stop.
def test_python_comment_between_decorator_and_declaration_still_stops() -> None:
    lines = ["@decorator", "# a plain comment", "class Foo:", "    pass"]
    start, end = symbol_attribution_span(
        start_line=3, end_line=4, file_lines=lines, language="python",
    )
    assert (start, end) == (3, 4)  # unchanged: the comment is a stop, not skipped


# --- Java: preserve current behavior (deliberate no-op) --------------------


# 16. Java: language is unrecognized here on purpose -- no widening is ever
# attempted, since CBM's own span already includes the annotation.
def test_java_annotation_attribution_is_unaffected() -> None:
    lines = ["@Controller", "class WelcomeController {", "}"]
    start, end = symbol_attribution_span(
        start_line=2, end_line=3, file_lines=lines, language="java",
    )
    assert (start, end) == (2, 3)  # unchanged -- CBM's span (2, 3) already correct


# --- Go: preserve current behavior; comments are explicitly out of scope --


# 17. Go: an ordinary leading doc comment must never be treated as attached
# metadata -- language_for_attribution reports "unknown" for .go, and even if
# it did not, comment lines never match the decorator/attribute predicate.
def test_go_leading_comment_is_never_treated_as_attached_metadata() -> None:
    lines = ["// MergePullRequest merges a PR given an index", "func MergePullRequest() {", "}"]
    assert language_for_attribution("routers/api/v1/repo/pull.go") == "unknown"
    start, end = symbol_attribution_span(
        start_line=2, end_line=3, file_lines=lines, language="go",
    )
    assert (start, end) == (2, 3)


# --- integration: attribute_changed_symbols end to end ---------------------


def _symbol(
    name: str, *, kind: str = "class", start_line: int, end_line: int,
    language: str = "", qualified_name: str | None = None, parent: str | None = None,
) -> dict:
    entry = {
        "name": name, "kind": kind, "language": language,
        "start_line": start_line, "end_line": end_line,
        "qualified_name": qualified_name or name,
    }
    if parent:
        entry["parent"] = parent
    return entry


def _handler_index(file_path: str, symbols: list[dict]) -> dict:
    return {"repos": [{"files": [{"path": file_path, "symbols": symbols}]}]}


def _change(
    *, path: str, hunks: list[tuple[int, int]], change_type: str = CHANGE_MODIFIED,
) -> ChangeSet:
    return ChangeSet(
        base="base", head="head",
        files=[ChangedFile(
            repo=REPO, path=path, change_type=change_type,
            hunks=[Hunk(start_line=s, end_line=e) for s, e in hunks],
        )],
    )


def test_decorator_only_hunk_now_attributes_the_class_via_repo_root(tmp_path: Path) -> None:
    """The exact Gitea/NetBox shape: a `-U0` hunk touching only the newly
    added decorator line, one line above the class keyword CBM reports as
    `start_line`. Without `repo_root`, this must reproduce today's original
    miss; with it, the class must now be attributed."""
    src = tmp_path / "views.py"
    src.write_text(
        "@register_model_view(DataFile)\n"      # line 1
        "@method_decorator(never_cache, name='dispatch')\n"  # line 2 -- the diff hunk
        "class DataFileView(generic.ObjectView):\n"           # line 3 -- CBM start_line
        "    queryset = DataFile.objects.all()\n",            # line 4
        encoding="utf-8",
    )
    handler_index = _handler_index("views.py", [
        _symbol("DataFileView", kind="class", start_line=3, end_line=4, language="python"),
    ])
    change = _change(path="views.py", hunks=[(2, 2)])

    without_repo_root = attribute_changed_symbols(change, handler_index)
    assert without_repo_root == []  # reproduces the original miss

    with_repo_root = attribute_changed_symbols(change, handler_index, repo_root=tmp_path)
    assert len(with_repo_root) == 1
    assert with_repo_root[0].name == "DataFileView"
    assert with_repo_root[0].start_line == 3  # CBM's own span is still reported as-is
    assert with_repo_root[0].end_line == 4


def test_decorator_hunk_does_not_cross_attribute_to_an_unrelated_earlier_class(
    tmp_path: Path,
) -> None:
    src = tmp_path / "views.py"
    src.write_text(
        "class Unrelated:\n"                         # line 1
        "    pass\n"                                   # line 2
        "\n"                                             # line 3 -- blank, terminates
        "@method_decorator(never_cache, name='dispatch')\n"  # line 4 -- the diff hunk
        "class DataFileView(generic.ObjectView):\n"           # line 5
        "    pass\n",                                            # line 6
        encoding="utf-8",
    )
    handler_index = _handler_index("views.py", [
        _symbol("Unrelated", kind="class", start_line=1, end_line=2, language="python"),
        _symbol("DataFileView", kind="class", start_line=5, end_line=6, language="python"),
    ])
    change = _change(path="views.py", hunks=[(4, 4)])

    result = attribute_changed_symbols(change, handler_index, repo_root=tmp_path)
    assert [item.name for item in result] == ["DataFileView"]


def test_ordinary_body_line_change_is_unaffected_by_this_feature(tmp_path: Path) -> None:
    """Regression pin: a hunk squarely inside a symbol's existing span must
    keep attributing exactly as before, decorator widening or not."""
    src = tmp_path / "views.py"
    src.write_text("class Foo:\n    def bar(self):\n        return 1\n", encoding="utf-8")
    handler_index = _handler_index("views.py", [
        _symbol("Foo", kind="class", start_line=1, end_line=3, language="python"),
    ])
    change = _change(path="views.py", hunks=[(3, 3)])

    result = attribute_changed_symbols(change, handler_index, repo_root=tmp_path)
    assert [item.name for item in result] == ["Foo"]


# 18. Cross-cutting: a decorated TEST symbol is still attributed as changed,
# and downstream test classification is untouched by this feature.
def test_test_only_decorated_symbol_is_attributed_and_still_classified_as_test(
    tmp_path: Path,
) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    src = tests_dir / "test_views.py"
    src.write_text(
        "@pytest.mark.django_db\n"      # line 1 -- the diff hunk
        "def test_content_is_not_cacheable():\n"  # line 2
        "    pass\n",                                 # line 3
        encoding="utf-8",
    )
    handler_index = _handler_index("tests/test_views.py", [
        _symbol(
            "test_content_is_not_cacheable", kind="function",
            start_line=2, end_line=3, language="python",
        ),
    ])
    change = _change(path="tests/test_views.py", hunks=[(1, 1)])

    result = attribute_changed_symbols(change, handler_index, repo_root=tmp_path)
    assert [item.name for item in result] == ["test_content_is_not_cacheable"]
    # attribution finding it "changed" says nothing about production
    # eligibility -- that classification is untouched, and still correct.
    assert classify_candidate_file_role("tests/test_views.py") == FILE_ROLE_TEST_USAGE_CANDIDATE


# 19. Changed-symbol ordering/dedup remains stable.
def test_ordering_and_dedup_are_stable_with_widening_in_play(tmp_path: Path) -> None:
    src = tmp_path / "views.py"
    src.write_text(
        "@decorator_a\n"                  # line 1
        "class Alpha:\n"                    # line 2
        "    pass\n"                          # line 3
        "\n"                                    # line 4
        "@decorator_b\n"                        # line 5
        "class Beta:\n"                           # line 6
        "    pass\n",                              # line 7
        encoding="utf-8",
    )
    handler_index = _handler_index("views.py", [
        _symbol("Beta", kind="class", start_line=6, end_line=7, language="python"),
        _symbol("Alpha", kind="class", start_line=2, end_line=3, language="python"),
    ])
    # Two hunks touching Beta's decorator -- must not produce two records.
    change = _change(path="views.py", hunks=[(1, 1), (5, 5)])

    result = attribute_changed_symbols(change, handler_index, repo_root=tmp_path)
    assert [item.name for item in result] == ["Alpha", "Beta"]  # file order by start_line
    assert len(result) == 2  # no duplicate for Beta despite two overlapping hunks


# 20. Architectural proof: no new CBM/network call is introduced -- widening
# reads only a plain local file via `Path.read_text`, nothing else.
def test_widening_requires_only_a_plain_file_read_no_cbm_client(tmp_path: Path) -> None:
    src = tmp_path / "views.py"
    src.write_text("@deco\nclass Foo:\n    pass\n", encoding="utf-8")
    handler_index = _handler_index("views.py", [
        _symbol("Foo", kind="class", start_line=2, end_line=3, language="python"),
    ])
    change = _change(path="views.py", hunks=[(1, 1)])
    # `handler_index` is a plain, already-materialized dict -- no client
    # object, no query method, reachable anywhere in this call.
    result = attribute_changed_symbols(change, handler_index, repo_root=tmp_path)
    assert [item.name for item in result] == ["Foo"]


# 21. Other languages retain old behavior when no attached metadata is
# involved -- Go/Java files with an ordinary, undecorated declaration change
# must attribute exactly as before (no accidental new widening anywhere).
def test_go_ordinary_declaration_change_is_unaffected(tmp_path: Path) -> None:
    src = tmp_path / "pull.go"
    src.write_text(
        "// MergePullRequest merges a PR given an index\n"  # line 1
        "func MergePullRequest() {\n"                          # line 2
        "\tdoSomething()\n"                                       # line 3 -- the diff hunk
        "}\n",                                                      # line 4
        encoding="utf-8",
    )
    handler_index = _handler_index("pull.go", [
        _symbol("MergePullRequest", kind="function", start_line=2, end_line=4, language="unknown"),
    ])
    change = _change(path="pull.go", hunks=[(3, 3)])

    result = attribute_changed_symbols(change, handler_index, repo_root=tmp_path)
    assert [item.name for item in result] == ["MergePullRequest"]
    assert result[0].start_line == 2  # unchanged, exactly as CBM reported


# --- second validation: real files from existing launch-gate worktrees -----
#
# Not synthetic fixtures -- these lock in behavior against real source that
# was directly inspected (via codebase-memory-mcp) during the audit. Skipped
# gracefully if a worktree isn't present locally (these live outside this
# repo, in the sibling sydes-evals checkout) rather than failing the suite
# on an environment difference.

_VENDURE_RESOLVER = Path(
    "/Users/ksnaik/StudioProjects/sydes-evals/_eval_worktrees/vendurehq__vendure-4930"
    "/packages/core/src/api/resolvers/admin/product.resolver.ts"
)
_LEMMY_POST = Path(
    "/Users/ksnaik/StudioProjects/sydes-evals/_eval_worktrees/LemmyNet__lemmy-6442"
    "/crates/db_schema/src/source/post.rs"
)


@pytest.mark.skipif(not _VENDURE_RESOLVER.exists(), reason="vendure worktree not present locally")
def test_real_vendure_resolver_class_and_method_decorators_widen() -> None:
    """`ProductResolver`: CBM's own span is `(44, 275)`, excluding `@Resolver()`
    at line 43; `products()`: CBM's span is `(53, 59)`, excluding the stacked
    `@Query()`/`@Allow(...)` at lines 51-52 — both empirically confirmed
    against CBM's index during the audit."""
    lines = _VENDURE_RESOLVER.read_text(encoding="utf-8").splitlines()
    assert symbol_attribution_span(
        start_line=44, end_line=275, file_lines=lines, language="typescript",
    ) == (43, 275)
    assert symbol_attribution_span(
        start_line=53, end_line=59, file_lines=lines, language="typescript",
    ) == (51, 59)


@pytest.mark.skipif(not _LEMMY_POST.exists(), reason="lemmy worktree not present locally")
def test_real_lemmy_post_struct_widens_past_its_doc_comment() -> None:
    """`Post`: CBM's own span is `(25, 91)`. The real file has attributes on
    lines 13-14 and 19-23, a *multiline* `#[cfg_attr(...)]` on lines 15-18,
    and a `///` doc comment on line 24 directly touching the struct. The
    doc comment must be looked past (reaching line 19), but the multiline
    attribute's own limitation (documented, not this test's concern) still
    stops the scan there rather than reaching lines 13-14."""
    lines = _LEMMY_POST.read_text(encoding="utf-8").splitlines()
    assert symbol_attribution_span(
        start_line=25, end_line=91, file_lines=lines, language="rust",
    ) == (19, 91)
