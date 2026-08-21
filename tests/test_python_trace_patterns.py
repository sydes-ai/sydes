"""Characterization tests for downstream Python flow tracing.

These describe what the shared trace stack must recover once a route resolves to
its handler, independent of any web framework:

    Pattern 1  handler in a short file      -> handler -> service -> database
    Pattern 2  handler in a long file       -> same result past the read budget
    Pattern 3  construct spanning the budget-> a source budget must not break parsing
    Pattern 4  imported service call        -> handler -> imported symbol
    Pattern 5  same-file helper             -> handler -> helper
    Pattern 6  async handler                -> `async def` behaves like `def`

Every assertion is on deterministic machinery; no test calls a model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sydes.core.models import EndpointCandidate, RepoRef
from sydes.trace.call_follower import CallFollowBudgets, build_layered_trace_expansion
from sydes.trace.expand import _extract_python_handler_baseline, prepare_flow_expansion_context
from sydes.trace.function_body_slicer import slice_resolved_handler_body
from sydes.trace.handler_resolver import resolve_handler_reference
from sydes.trace.handler_symbol_index import build_handler_symbol_index

_PADDING = "\n".join(f"# filler line {index} " + "x" * 70 for index in range(1, 140))


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _endpoint(root: Path, file: str, handler: str) -> EndpointCandidate:
    return EndpointCandidate(
        method="POST",
        path="/students",
        handler=handler,
        file=file,
        repo="api",
        confidence=1.0,
        status="deterministic_composed",
    )


def _baseline(root: Path, file: str, handler: str):
    """Run the deterministic Python flow baseline the trace stack relies on."""
    endpoint = _endpoint(root, file, handler)
    context = prepare_flow_expansion_context(
        matched_endpoint=endpoint,
        repos=[RepoRef(name="api", root=str(root))],
        max_related_files=0,
    )
    return _extract_python_handler_baseline(endpoint, context, repo_root=str(root))


_SHORT_HANDLER = '''from app.repository import save_student


def create_student(payload):
    """Create a student."""
    if not payload.get("name"):
        raise ValueError("name required")
    record = save_student(payload)
    return record
'''

_REPOSITORY = '''def save_student(payload):
    session.execute("INSERT INTO students (name) VALUES (?)", [payload["name"]])
    session.commit()
    return {"id": 1}
'''


# --------------------------------------------------------------------------
# Patterns 1-3 — the handler body must survive the source-read budget
#
# Two separable requirements, asserted separately:
#   (a) the deterministic baseline still parses and yields in-handler evidence;
#   (b) the shared call-following machinery still reaches the downstream call.
# --------------------------------------------------------------------------


def test_pattern_1_short_file_handler_yields_downstream_flow(tmp_path: Path) -> None:
    """Baseline: a small file yields handler evidence and a followed service call."""
    root = tmp_path / "repo"
    _write(root, "app/routes.py", _SHORT_HANDLER)
    _write(root, "app/repository.py", _REPOSITORY)

    steps, _sinks, notes = _baseline(root, "app/routes.py", "create_student")
    assert steps, f"expected deterministic steps from the handler body; notes={notes}"

    _index, _body, expansion = _follow(root, "app/routes.py", "create_student")
    followed = {item["call"]: item for item in expansion["followed_calls"]}
    assert "save_student" in followed
    assert followed["save_student"]["file"] == "app/repository.py"


def test_pattern_2_long_file_handler_still_yields_downstream_flow(tmp_path: Path) -> None:
    """A source-read budget must not erase the handler's evidence or calls."""
    root = tmp_path / "repo"
    _write(root, "app/routes.py", _SHORT_HANDLER + "\n\n" + _PADDING + "\n")
    _write(root, "app/repository.py", _REPOSITORY)

    steps, _sinks, notes = _baseline(root, "app/routes.py", "create_student")
    assert steps, f"long file produced no steps; notes={notes}"

    _index, _body, expansion = _follow(root, "app/routes.py", "create_student")
    assert "save_student" in {item["call"] for item in expansion["followed_calls"]}


def test_pattern_2_handler_after_the_budget_still_resolves(tmp_path: Path) -> None:
    """A handler positioned beyond the read budget is still reachable."""
    root = tmp_path / "repo"
    _write(root, "app/routes.py", _PADDING + "\n\n" + _SHORT_HANDLER)
    _write(root, "app/repository.py", _REPOSITORY)

    steps, _sinks, notes = _baseline(root, "app/routes.py", "create_student")
    assert steps, f"handler beyond the budget produced no steps; notes={notes}"

    _index, _body, expansion = _follow(root, "app/routes.py", "create_student")
    assert "save_student" in {item["call"] for item in expansion["followed_calls"]}


def test_pattern_3_construct_spanning_the_budget_does_not_break_parsing(tmp_path: Path) -> None:
    """Truncating mid-construct must not silently disable deterministic tracing.

    The trailing collection is a single syntactic unit that any positional cut
    would leave unclosed. The file is valid Python as a whole.
    """
    root = tmp_path / "repo"
    filler = "y" * 60
    entries = "\n".join(
        f'    "key_{index}": "value_{index}" + "{filler}",' for index in range(1, 120)
    )
    source = _SHORT_HANDLER + "\n\nLOOKUP = {\n" + entries + "\n}\n"
    _write(root, "app/routes.py", source)
    _write(root, "app/repository.py", _REPOSITORY)

    steps, _sinks, notes = _baseline(root, "app/routes.py", "create_student")

    assert steps, f"construct spanning the budget produced no steps; notes={notes}"
    assert not any("python_parse_failed" in note for note in notes)


def test_pattern_3_parse_failure_is_reported_not_swallowed(tmp_path: Path) -> None:
    """Genuinely invalid source is diagnosed, never returned as an empty trace."""
    root = tmp_path / "repo"
    _write(root, "app/routes.py", "def create_student(payload):\n    return save(\n")

    steps, sinks, notes = _baseline(root, "app/routes.py", "create_student")

    assert steps == [] and sinks == []
    assert any("python_parse_failed" in note for note in notes)


def test_pattern_3_missing_handler_is_reported_not_swallowed(tmp_path: Path) -> None:
    """A handler absent from the anchor file is diagnosed explicitly."""
    root = tmp_path / "repo"
    _write(root, "app/routes.py", "def other_function():\n    return 1\n")

    steps, _sinks, notes = _baseline(root, "app/routes.py", "create_student")

    assert steps == []
    assert any("handler_body_unavailable" in note for note in notes)


# --------------------------------------------------------------------------
# Patterns 4 & 5 — call following through the shared symbol machinery
# --------------------------------------------------------------------------


def _follow(root: Path, file: str, handler: str):
    """Run the shared handler-symbol + call-following path for one handler."""
    index = build_handler_symbol_index(RepoRef(name="api", root=str(root)))
    resolution = resolve_handler_reference(_endpoint(root, file, handler), index)
    primary = resolution.get("primary_handler") or {}
    symbol = primary.get("symbol")
    assert symbol is not None, f"handler symbol not resolved from index: {resolution}"
    body = slice_resolved_handler_body(
        repo_root=root,
        handler_name=handler,
        symbol=symbol,
        language=str(symbol.get("language") or "python"),
    )
    assert body is not None, "handler body slice unavailable"
    expansion = build_layered_trace_expansion(
        repo_root=root,
        matched_endpoint={"method": "POST", "path": "/students", "file": file},
        resolution=resolution,
        primary_slice=body,
        repo_index=index,
        budgets=CallFollowBudgets(),
    )
    return index, body, expansion


def test_pattern_4_imported_service_call_is_followed(tmp_path: Path) -> None:
    """A handler calling an imported symbol resolves into that symbol's file."""
    root = tmp_path / "repo"
    _write(
        root,
        "app/routes.py",
        "from app.service import create_student_record\n"
        "\n"
        "async def create_student(payload):\n"
        "    return await create_student_record(payload)\n",
    )
    _write(
        root,
        "app/service.py",
        "def create_student_record(payload):\n"
        '    session.execute("INSERT INTO students (name) VALUES (?)", [payload])\n'
        "    return {}\n",
    )

    _index, _body, expansion = _follow(root, "app/routes.py", "create_student")
    followed = {item["call"]: item for item in expansion["followed_calls"]}

    assert "create_student_record" in followed
    assert followed["create_student_record"]["file"] == "app/service.py"


def test_pattern_5_same_file_helper_is_followed(tmp_path: Path) -> None:
    """A handler calling a helper in its own file resolves to that helper."""
    root = tmp_path / "repo"
    _write(
        root,
        "app/routes.py",
        "async def create_student(payload):\n"
        "    return persist_student(payload)\n"
        "\n"
        "def persist_student(payload):\n"
        '    session.execute("INSERT INTO students VALUES (?)", [payload])\n'
        "    return {}\n",
    )

    _index, _body, expansion = _follow(root, "app/routes.py", "create_student")
    followed = {item["call"] for item in expansion["followed_calls"]}

    assert "persist_student" in followed


def test_pattern_5_unresolved_call_is_recorded_not_dropped(tmp_path: Path) -> None:
    """A call that cannot be resolved is reported, not silently omitted."""
    root = tmp_path / "repo"
    _write(
        root,
        "app/routes.py",
        "async def create_student(payload):\n    return external_thing(payload)\n",
    )

    _index, _body, expansion = _follow(root, "app/routes.py", "create_student")
    unresolved = {item["call"] for item in expansion["unresolved_calls"]}
    skipped = {item["call"] for item in expansion["skipped_calls"]}

    assert "external_thing" in (unresolved | skipped)


# --------------------------------------------------------------------------
# Pattern 6 — async handlers
# --------------------------------------------------------------------------


@pytest.mark.parametrize("declaration", ["def", "async def"])
def test_pattern_6_async_and_sync_handlers_behave_identically(
    tmp_path: Path, declaration: str
) -> None:
    """`async def` is indexed and sliced exactly like `def`."""
    root = tmp_path / "repo"
    _write(
        root,
        "app/routes.py",
        f"{declaration} create_student(payload):\n"
        "    return persist_student(payload)\n"
        "\n"
        "def persist_student(payload):\n"
        '    session.execute("INSERT INTO students VALUES (?)", [payload])\n'
        "    return {}\n",
    )

    index = build_handler_symbol_index(RepoRef(name="api", root=str(root)))
    names = {
        symbol["name"]
        for file_item in index["files"]
        for symbol in file_item["symbols"]
    }

    assert "create_student" in names
    assert "persist_student" in names


def test_python_files_participate_in_the_shared_symbol_index(tmp_path: Path) -> None:
    """Python source is indexed by the shared handler-symbol registry."""
    root = tmp_path / "repo"
    _write(root, "app/routes.py", _SHORT_HANDLER)
    _write(root, "app/repository.py", _REPOSITORY)

    index = build_handler_symbol_index(RepoRef(name="api", root=str(root)))

    assert index["summary"]["files_indexed"] >= 2
    by_path = {item["path"]: item for item in index["files"]}
    assert "app/routes.py" in by_path
    handler = next(
        item for item in by_path["app/routes.py"]["symbols"] if item["name"] == "create_student"
    )
    assert handler["language"] == "python"
    assert isinstance(handler["start_line"], int)
    assert isinstance(handler["end_line"], int)
    assert handler["end_line"] > handler["start_line"]
    assert any(item["imported"] == "save_student" for item in by_path["app/routes.py"]["imports"])


def test_flow_expansion_wires_the_complete_source_into_parsing(tmp_path: Path) -> None:
    """The production entry point supplies the parse-complete source itself."""
    from sydes.llm.client import LLMResponse
    from sydes.trace.expand import run_flow_expansion

    class _EmptyLLM:
        timeout_seconds = 1.0

        def generate(self, request):
            return LLMResponse(text='{"steps": [], "sinks": []}')

    root = tmp_path / "repo"
    _write(root, "app/routes.py", _SHORT_HANDLER + "\n\n" + _PADDING + "\n")
    _write(root, "app/repository.py", _REPOSITORY)

    result = run_flow_expansion(
        _endpoint(root, "app/routes.py", "create_student"),
        [RepoRef(name="api", root=str(root))],
        llm_client=_EmptyLLM(),
    )

    assert result.steps, f"no deterministic steps reached the result; notes={result.notes}"
    assert not any("python_parse_failed" in note for note in result.notes)


# --------------------------------------------------------------------------
# Phase 5 — existing sink machinery becomes visible once traversal works
# --------------------------------------------------------------------------


def test_downstream_database_effect_is_reachable_through_the_followed_layer(
    tmp_path: Path,
) -> None:
    """A database effect in a called function appears in the expansion layers."""
    root = tmp_path / "repo"
    _write(
        root,
        "app/routes.py",
        "from app.repository import save_student\n"
        "\n"
        "async def create_student(payload):\n"
        "    return save_student(payload)\n",
    )
    _write(root, "app/repository.py", _REPOSITORY)

    _index, _body, expansion = _follow(root, "app/routes.py", "create_student")
    followed_layer = next(
        layer for layer in expansion["layers"] if layer.get("handler") == "save_student"
    )
    signals = {
        signal
        for step in followed_layer["steps"]
        for signal in step.get("signals", [])
    }

    assert "possible_db_call" in signals or "sql_literal" in signals


def test_downstream_outbound_call_is_reachable_through_the_followed_layer(
    tmp_path: Path,
) -> None:
    """An outbound HTTP effect in a called function is visible downstream."""
    root = tmp_path / "repo"
    _write(
        root,
        "app/routes.py",
        "from app.client import notify_registry\n"
        "\n"
        "async def create_student(payload):\n"
        "    return notify_registry(payload)\n",
    )
    _write(
        root,
        "app/client.py",
        "def notify_registry(payload):\n"
        '    return requests.post("https://registry.internal/students", json=payload)\n',
    )

    _index, _body, expansion = _follow(root, "app/routes.py", "create_student")
    followed_layer = next(
        layer for layer in expansion["layers"] if layer.get("handler") == "notify_registry"
    )
    signals = {
        signal
        for step in followed_layer["steps"]
        for signal in step.get("signals", [])
    }

    assert "possible_external_call" in signals


def test_multi_hop_flow_reaches_repository_through_a_service(tmp_path: Path) -> None:
    """handler -> service -> repository resolves across three files."""
    root = tmp_path / "repo"
    _write(
        root,
        "app/routes.py",
        "from app.service import register_student\n"
        "\n"
        "async def create_student(payload):\n"
        "    return await register_student(payload)\n",
    )
    _write(
        root,
        "app/service.py",
        "from app.repository import save_student\n"
        "\n"
        "async def register_student(payload):\n"
        "    validated = dict(payload)\n"
        "    return save_student(validated)\n",
    )
    _write(root, "app/repository.py", _REPOSITORY)

    _index, _body, expansion = _follow(root, "app/routes.py", "create_student")
    followed = {item["call"]: item["file"] for item in expansion["followed_calls"]}

    assert followed.get("register_student") == "app/service.py"


def test_every_followed_edge_carries_inspectable_evidence(tmp_path: Path) -> None:
    """A followed call records the file, symbol, and calling statement."""
    root = tmp_path / "repo"
    _write(
        root,
        "app/routes.py",
        "from app.repository import save_student\n"
        "\n"
        "async def create_student(payload):\n"
        "    return save_student(payload)\n",
    )
    _write(root, "app/repository.py", _REPOSITORY)

    _index, _body, expansion = _follow(root, "app/routes.py", "create_student")
    edge = next(item for item in expansion["followed_calls"] if item["call"] == "save_student")

    assert edge["file"] == "app/repository.py"
    assert edge["resolved_to"] == "save_student"
    assert "save_student" in (edge["called_from_statement"] or "")


def test_multiline_signature_is_not_treated_as_body(tmp_path: Path) -> None:
    """A signature spanning several lines is not mistaken for handler statements."""
    root = tmp_path / "repo"
    _write(
        root,
        "app/routes.py",
        "from app.repository import save_student\n"
        "\n"
        "async def create_student(\n"
        "    payload,\n"
        "    db=None,\n"
        "    current_user=None,\n"
        "):\n"
        "    return save_student(payload)\n",
    )
    _write(root, "app/repository.py", _REPOSITORY)

    _index, body, _expansion = _follow(root, "app/routes.py", "create_student")
    texts = [step["text"] for step in body["statements"]]

    assert not any("current_user=None" in text for text in texts), texts
    assert any("save_student" in text for text in texts)


@pytest.mark.parametrize(
    "statement",
    [
        "db.add(student)",
        "db.commit()",
        "session.flush()",
        "session.execute(query)",
    ],
)
def test_python_session_idioms_register_as_database_access(statement: str) -> None:
    """Session-style persistence reaches the existing database signal."""
    from sydes.trace.function_body_slicer import split_statements

    statements = split_statements([f"    {statement}"], 1, language="python")

    assert statements, statement
    assert "possible_db_call" in statements[0]["signals"], statements[0]
