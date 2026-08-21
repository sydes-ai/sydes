"""Characterization tests for route container/mount composition.

These tests describe *routing architectures*, not framework syntax. Each pattern
is expressed in more than one concrete syntax where the architecture allows it,
so a pass proves the shared composition model understands the relation rather
than one parser understanding one library.

The architectures under test:

    Pattern 1  container owns its prefix          -> /students + /{id}
    Pattern 2  parent owns the prefix at mount    -> /api      + /books
    Pattern 3  nested composition                 -> /api + /notifications + /unread
    Pattern 4  flat application, no composition   -> /health, and no mount penalty
    Pattern 5  containers exist, relation unknown -> must not read as "strong"
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from sydes.core.models import RepoRef
from sydes.discover.endpoints import discover_endpoints


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _routes(root: Path) -> set[str]:
    """Return `METHOD /path` for every route the deterministic ladder recovers."""
    result = discover_endpoints(
        [RepoRef(name="api", root=str(root))], llm_policy="never"
    )
    return {f"{(item.method or 'ANY').upper()} {item.path}" for item in result.routes}


def _coverage_payload(result) -> dict:
    """Recompute the coverage assessment from a discovery run's own facts."""
    from sydes.discover.discovery_coverage import evaluate_discovery_coverage
    from sydes.discover.route_graph import build_route_graph_facts_batch
    from sydes.discover.route_index import build_route_index_batch

    repos = [RepoRef(name=item.name, root=item.root) for item in result.repos]
    index = build_route_index_batch(repos)
    graph = build_route_graph_facts_batch(repos, route_index_batch=index)
    return evaluate_discovery_coverage(
        route_index_summary=index["repos"][0]["summary"],
        route_graph_summary=graph["repos"][0]["summary"],
        deterministic_route_count=len(result.routes),
        deterministic_scan_truncated_files=0,
    )


def _coverage(root: Path) -> tuple[str, float, list[str]]:
    """Return (label, score, reasons) from the discovery coverage assessment."""
    result = discover_endpoints(
        [RepoRef(name="api", root=str(root))], llm_policy="never"
    )
    label, score, reasons = "unknown", 0.0, []
    for note in result.notes:
        match = re.search(r"discovery_coverage=(?P<label>\w+) score=(?P<score>[\d.]+)", note)
        if match:
            label = match.group("label")
            score = float(match.group("score"))
        if "discovery_coverage_reasons=" in note:
            reasons = note.split("discovery_coverage_reasons=", 1)[1].split("; ")
    return label, score, reasons


# --------------------------------------------------------------------------
# Pattern 1 — the container carries its own prefix
# --------------------------------------------------------------------------


def test_pattern_1_container_owned_prefix_is_composed(tmp_path: Path) -> None:
    """A container declaring its own prefix contributes it to every child route."""
    root = tmp_path / "repo"
    _write(
        root,
        "routers/students.py",
        "from framework import APIRouter\n"
        "\n"
        'router = APIRouter(prefix="/students")\n'
        "\n"
        '@router.get("/{student_id}")\n'
        "def get_student(student_id):\n"
        "    return {}\n",
    )
    _write(
        root,
        "main.py",
        "from framework import App\n"
        "from routers import students\n"
        "\n"
        "app = App()\n"
        "app.include_router(students.router)\n",
    )

    assert "GET /students/{student_id}" in _routes(root)


def test_pattern_1_holds_for_an_unfamiliar_container_name(tmp_path: Path) -> None:
    """Composition keys off the construct's *shape*, not a known library name."""
    root = tmp_path / "repo"
    _write(
        root,
        "routers/orders.py",
        "from acme import HttpRouteGroup\n"
        "\n"
        'group = HttpRouteGroup(prefix="/orders")\n'
        "\n"
        '@group.post("/{order_id}/cancel")\n'
        "def cancel(order_id):\n"
        "    return {}\n",
    )
    _write(
        root,
        "main.py",
        "from acme import Application\n"
        "from routers import orders\n"
        "\n"
        "app = Application()\n"
        "app.include_router(orders.group)\n",
    )

    assert "POST /orders/{order_id}/cancel" in _routes(root)


def test_pattern_1_alternate_prefix_keyword_is_composed(tmp_path: Path) -> None:
    """A differently-named prefix keyword is still a container prefix."""
    root = tmp_path / "repo"
    _write(
        root,
        "views/reports.py",
        "from framework import Blueprint\n"
        "\n"
        'reports = Blueprint("reports", __name__, url_prefix="/reports")\n'
        "\n"
        '@reports.route("/daily", methods=["GET"])\n'
        "def daily():\n"
        "    return {}\n",
    )
    _write(
        root,
        "app.py",
        "from framework import App\n"
        "from views.reports import reports\n"
        "\n"
        "app = App()\n"
        "app.register_blueprint(reports)\n",
    )

    assert "GET /reports/daily" in _routes(root)


# --------------------------------------------------------------------------
# Pattern 2 — the parent supplies the prefix at the mount site
# --------------------------------------------------------------------------


def test_pattern_2_parent_owned_prefix_is_composed(tmp_path: Path) -> None:
    """A prefix supplied at the mount site is prepended to the child's routes."""
    root = tmp_path / "repo"
    _write(
        root,
        "routes/books.js",
        'const express = require("express");\n'
        "const router = express.Router();\n"
        '\nrouter.get("/books", listBooks);\n'
        "\nmodule.exports = router;\n",
    )
    _write(
        root,
        "app.js",
        'const express = require("express");\n'
        "const app = express();\n"
        'const books = require("./routes/books");\n'
        '\napp.use("/api", books);\n',
    )

    assert "GET /api/books" in _routes(root)


def test_pattern_2_holds_when_the_parent_mounts_a_prefixed_container(tmp_path: Path) -> None:
    """Parent-supplied and container-owned prefixes compose, without duplication."""
    root = tmp_path / "repo"
    _write(
        root,
        "routers/books.py",
        "from framework import APIRouter\n"
        "\n"
        'router = APIRouter(prefix="/books")\n'
        "\n"
        '@router.get("/{book_id}")\n'
        "def get_book(book_id):\n"
        "    return {}\n",
    )
    _write(
        root,
        "main.py",
        "from framework import App\n"
        "from routers import books\n"
        "\n"
        "app = App()\n"
        'app.include_router(books.router, prefix="/api")\n',
    )

    routes = _routes(root)
    assert "GET /api/books/{book_id}" in routes
    assert "GET /books/{book_id}" not in routes


# --------------------------------------------------------------------------
# Pattern 3 — nested composition
# --------------------------------------------------------------------------


def test_pattern_3_nested_composition_accumulates_every_prefix(tmp_path: Path) -> None:
    """Prefixes accumulate across an arbitrary depth of mounts."""
    root = tmp_path / "repo"
    _write(
        root,
        "routes/notifications.js",
        'const express = require("express");\n'
        "const router = express.Router();\n"
        '\nrouter.get("/unread", getUnread);\n'
        "\nmodule.exports = router;\n",
    )
    _write(
        root,
        "routes/api.js",
        'const express = require("express");\n'
        "const api = express.Router();\n"
        'const notifications = require("./notifications");\n'
        '\napi.use("/notifications", notifications);\n'
        "\nmodule.exports = api;\n",
    )
    _write(
        root,
        "app.js",
        'const express = require("express");\n'
        "const app = express();\n"
        'const api = require("./routes/api");\n'
        '\napp.use("/api", api);\n',
    )

    assert "GET /api/notifications/unread" in _routes(root)


def test_pattern_3_nested_composition_with_container_owned_prefixes(tmp_path: Path) -> None:
    """Nesting works when each level declares its own prefix instead."""
    root = tmp_path / "repo"
    _write(
        root,
        "routers/notifications.py",
        "from framework import APIRouter\n"
        "\n"
        'router = APIRouter(prefix="/notifications")\n'
        "\n"
        '@router.get("/unread")\n'
        "def unread():\n"
        "    return {}\n",
    )
    _write(
        root,
        "routers/api.py",
        "from framework import APIRouter\n"
        "from routers import notifications\n"
        "\n"
        'router = APIRouter(prefix="/api")\n'
        "router.include_router(notifications.router)\n",
    )
    _write(
        root,
        "main.py",
        "from framework import App\n"
        "from routers import api\n"
        "\n"
        "app = App()\n"
        "app.include_router(api.router)\n",
    )

    assert "GET /api/notifications/unread" in _routes(root)


# --------------------------------------------------------------------------
# Pattern 4 — flat application, no composition at all
# --------------------------------------------------------------------------


def test_pattern_4_flat_application_routes_are_unprefixed(tmp_path: Path) -> None:
    """Routes declared straight on the application keep their literal paths."""
    root = tmp_path / "repo"
    _write(
        root,
        "main.py",
        "from framework import App\n"
        "\n"
        "app = App()\n"
        "\n"
        '@app.get("/health")\n'
        "def health():\n"
        "    return {}\n"
        "\n"
        '@app.post("/items")\n'
        "def add_item():\n"
        "    return {}\n",
    )

    routes = _routes(root)
    assert "GET /health" in routes
    assert "POST /items" in routes


def test_pattern_4_flat_application_is_not_penalized_for_absent_mounts(tmp_path: Path) -> None:
    """An app with no composition is fully understood, so coverage stays strong."""
    root = tmp_path / "repo"
    _write(
        root,
        "main.py",
        "from framework import App\n"
        "\n"
        "app = App()\n"
        "\n"
        '@app.get("/health")\n'
        "def health():\n"
        "    return {}\n"
        "\n"
        '@app.post("/items")\n'
        "def add_item():\n"
        "    return {}\n",
    )

    label, _score, reasons = _coverage(root)

    assert label == "strong"
    assert not any("unresolved" in reason for reason in reasons)


# --------------------------------------------------------------------------
# Pattern 5 — containers exist but their composition cannot be resolved
# --------------------------------------------------------------------------


def _unresolved_composition_repo(root: Path) -> None:
    """Two containers mounted through a construct the deterministic layer cannot read."""
    _write(
        root,
        "routes/students.js",
        'const express = require("express");\n'
        "const router = express.Router();\n"
        '\nrouter.get("/{student_id}", getStudent);\n'
        "\nmodule.exports = router;\n",
    )
    _write(
        root,
        "routes/subjects.js",
        'const express = require("express");\n'
        "const router = express.Router();\n"
        '\nrouter.get("/{subject_id}", getSubject);\n'
        "\nmodule.exports = router;\n",
    )
    _write(
        root,
        "app.js",
        'const express = require("express");\n'
        "const app = express();\n"
        "\nconst registry = [\n"
        '  { prefix: "/students", module: require("./routes/students") },\n'
        '  { prefix: "/subjects", module: require("./routes/subjects") },\n'
        "];\n"
        "\nregistry.forEach((entry) => app.use(entry.prefix, entry.module));\n",
    )


def test_pattern_5_unresolved_composition_is_not_reported_as_strong(tmp_path: Path) -> None:
    """Containers with no resolvable mount must not read as full understanding."""
    root = tmp_path / "repo"
    _unresolved_composition_repo(root)

    label, _score, _reasons = _coverage(root)

    assert label != "strong"


def test_pattern_5_unresolved_composition_is_stated_in_diagnostics(tmp_path: Path) -> None:
    """The uncertainty is named, not silently absorbed into the score."""
    root = tmp_path / "repo"
    _unresolved_composition_repo(root)

    _label, _score, reasons = _coverage(root)

    assert any("composition" in reason and "unresolved" in reason for reason in reasons)


def test_pattern_5_remains_eligible_for_the_fallback_ladder(tmp_path: Path) -> None:
    """Unresolved composition must leave the routing-pattern planner eligible."""
    from sydes.discover.discovery_coverage import auto_policy_should_skip_llm

    root = tmp_path / "repo"
    _unresolved_composition_repo(root)
    result = discover_endpoints([RepoRef(name="api", root=str(root))], llm_policy="never")
    coverage = _coverage_payload(result)

    assert coverage["signals"]["composition_unresolved"] is True
    assert auto_policy_should_skip_llm(coverage) is False


@pytest.mark.parametrize(
    ("containers", "resolved_mounts"),
    [(2, 0), (5, 0), (3, 1)],
)
def test_multiple_containers_with_unresolved_composition_never_score_strong(
    containers: int, resolved_mounts: int
) -> None:
    """Guards the false-confidence bug directly at the scoring function."""
    from sydes.discover.discovery_coverage import evaluate_discovery_coverage

    coverage = evaluate_discovery_coverage(
        route_index_summary={
            "files_indexed": containers + 1,
            "files_with_route_calls": containers,
            "route_call_count": containers * 2,
            "mount_call_count": 0,
            "router_symbol_count": containers,
        },
        route_graph_summary={
            "containers": containers,
            "declarations": containers * 2,
            "mount_edges": resolved_mounts,
            "composed_routes": containers * 2,
            "unresolved_mounts": 0,
        },
        deterministic_route_count=containers * 2,
        deterministic_scan_truncated_files=0,
    )

    assert coverage["label"] != "strong"


def test_flat_application_keeps_the_planner_skipped(tmp_path: Path) -> None:
    """A fully understood flat app must not trigger the fallback ladder."""
    from sydes.discover.discovery_coverage import (
        auto_policy_should_skip_llm,
        composition_is_unresolved,
    )

    root = tmp_path / "repo"
    _write(
        root,
        "main.py",
        "from framework import App\n"
        "\n"
        "app = App()\n"
        "\n"
        '@app.get("/health")\n'
        "def health():\n"
        "    return {}\n",
    )
    result = discover_endpoints([RepoRef(name="api", root=str(root))], llm_policy="never")
    coverage = _coverage_payload(result)

    assert composition_is_unresolved(coverage) is False
    assert auto_policy_should_skip_llm(coverage) is True


def test_resolved_composition_keeps_the_planner_skipped(tmp_path: Path) -> None:
    """Successfully composed mounts are full understanding, not uncertainty."""
    from sydes.discover.discovery_coverage import composition_is_unresolved

    root = tmp_path / "repo"
    _write(
        root,
        "routes/books.js",
        'const express = require("express");\n'
        "const router = express.Router();\n"
        '\nrouter.get("/books", listBooks);\n'
        "\nmodule.exports = router;\n",
    )
    _write(
        root,
        "app.js",
        'const express = require("express");\n'
        "const app = express();\n"
        'const books = require("./routes/books");\n'
        '\napp.use("/api", books);\n',
    )
    result = discover_endpoints([RepoRef(name="api", root=str(root))], llm_policy="never")

    assert composition_is_unresolved(_coverage_payload(result)) is False


def test_route_declaring_an_empty_path_resolves_to_the_container_prefix(tmp_path: Path) -> None:
    """A declaration may contribute no segment, leaving the container prefix."""
    root = tmp_path / "repo"
    _write(
        root,
        "routers/subjects.py",
        "from framework import APIRouter\n"
        "\n"
        'router = APIRouter(prefix="/subjects")\n'
        "\n"
        '@router.get("")\n'
        "def list_subjects():\n"
        "    return []\n",
    )
    _write(
        root,
        "main.py",
        "from framework import App\n"
        "from routers import subjects\n"
        "\n"
        "app = App()\n"
        "app.include_router(subjects.router)\n",
    )

    assert "GET /subjects" in _routes(root)


def test_multiline_declaration_is_composed(tmp_path: Path) -> None:
    """A declaration split across lines composes like a single-line one."""
    root = tmp_path / "repo"
    _write(
        root,
        "routers/subjects.py",
        "from framework import APIRouter\n"
        "\n"
        'router = APIRouter(prefix="/subjects")\n'
        "\n"
        "@router.post(\n"
        '    "/{subject_id}/archive",\n'
        "    status_code=201,\n"
        ")\n"
        "def archive(subject_id):\n"
        "    return {}\n",
    )
    _write(
        root,
        "main.py",
        "from framework import App\n"
        "from routers import subjects\n"
        "\n"
        "app = App()\n"
        "app.include_router(subjects.router)\n",
    )

    assert "POST /subjects/{subject_id}/archive" in _routes(root)


def test_composed_route_binds_the_declaration_it_decorates(tmp_path: Path) -> None:
    """A decorator-style declaration binds the callable it precedes.

    The handler is the next declaration below the route, not an argument to it.
    Without this binding a composed route reaches trace with no entry symbol.
    """
    root = tmp_path / "repo"
    _write(
        root,
        "routers/students.py",
        "from framework import APIRouter\n"
        "\n"
        'router = APIRouter(prefix="/students")\n'
        "\n"
        "@router.post(\n"
        '    "",\n'
        "    status_code=201,\n"
        ")\n"
        "async def create_student(payload):\n"
        "    return {}\n",
    )
    _write(
        root,
        "main.py",
        "from framework import App\n"
        "from routers import students\n"
        "\n"
        "app = App()\n"
        "app.include_router(students.router)\n",
    )

    result = discover_endpoints([RepoRef(name="api", root=str(root))], llm_policy="never")
    created = next(
        item for item in result.routes if item.path == "/students" and item.method == "POST"
    )

    assert created.handler == "create_student"


def test_composition_does_not_leave_an_unprefixed_duplicate(tmp_path: Path) -> None:
    """When a prefix is resolved, the un-composed twin must not also survive.

    The same declaration is seen twice: once raw by the deterministic adapter,
    and once composed with its prefix. Only the composed one is a real route.
    """
    root = tmp_path / "repo"
    _write(
        root,
        "routes/auth.js",
        'const express = require("express");\n'
        "const router = express.Router();\n"
        '\nrouter.post("/login", validate(), authController.login);\n'
        "\nmodule.exports = router;\n",
    )
    _write(
        root,
        "app.js",
        'const express = require("express");\n'
        "const app = express();\n"
        'const auth = require("./routes/auth");\n'
        '\napp.use("/v1", auth);\n',
    )

    routes = _routes(root)

    assert "POST /v1/login" in routes
    assert "POST /login" not in routes


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("/items/<int:item_id>", "/items/{item_id}"),
        ("/items/<item_id>", "/items/{item_id}"),
        ("/items/:item_id", "/items/{item_id}"),
        ("/items/{item_id}", "/items/{item_id}"),
    ],
)
def test_path_parameters_normalize_to_one_form(tmp_path: Path, declared: str, expected: str) -> None:
    """Every path-parameter syntax composes to the same canonical form."""
    root = tmp_path / "repo"
    _write(
        root,
        "main.py",
        "from framework import App\n"
        "\n"
        "app = App()\n"
        "\n"
        f'@app.get("{declared}")\n'
        "def get_item(item_id):\n"
        "    return {}\n",
    )

    assert _routes(root) == {f"GET {expected}"}
