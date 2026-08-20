"""Symbol index, call graph, and affected-flow expansion tests for verify-change."""

from __future__ import annotations

from pathlib import Path

import pytest

from sydes.core.models import RepoRef
from sydes.verify.events import detect_event_signals
from sydes.verify.repo_scan import scan_repository
from sydes.verify.surface import FlowBuilder, build_system_surface, classify_symbol_role
from sydes.verify.symbol_index import build_symbol_index
from sydes.verify.test_index import build_test_index, map_existing_verification


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture()
def python_service(tmp_path: Path) -> Path:
    """A FastAPI-style repo: route -> service -> repository -> event publish."""
    root = tmp_path / "svc"
    _write(
        root,
        "app/api.py",
        "from fastapi import FastAPI\n"
        "from app.services import RefundService\n"
        "\n"
        "app = FastAPI()\n"
        "service = RefundService()\n"
        "\n"
        '@app.post("/refund")\n'
        "def create_refund(payload: dict):\n"
        "    return service.retry_refund(payload)\n",
    )
    _write(
        root,
        "app/services.py",
        "from app.ledger import LedgerRepository\n"
        "from app.bus import bus\n"
        "\n"
        "class RefundService:\n"
        "    def retry_refund(self, payload):\n"
        "        LedgerRepository.reverse(payload)\n"
        '        bus.publish("refund.created", payload)\n'
        "        return {'ok': True}\n",
    )
    _write(
        root,
        "app/ledger.py",
        "class LedgerRepository:\n"
        "    @staticmethod\n"
        "    def reverse(payload):\n"
        '        session.execute("UPDATE ledger SET reversed = 1 WHERE id = ?", [payload])\n',
    )
    _write(
        root,
        "app/consumers.py",
        "from app.bus import bus\n"
        "\n"
        "def register():\n"
        '    bus.subscribe("refund.created", handle_refund_created)\n'
        "\n"
        "def handle_refund_created(event):\n"
        "    return event\n",
    )
    _write(root, "app/bus.py", "class Bus:\n    pass\n\nbus = Bus()\n")
    _write(
        root,
        "tests/test_refund_api.py",
        "def test_refund_endpoint(client):\n"
        '    response = client.post("/refund", json={"id": 1})\n'
        "    assert response.status_code == 200\n",
    )
    _write(root, ".env.example", "DATABASE_URL=postgres://localhost/app\nKAFKA_BROKERS=localhost:9092\n")
    return root


def _analyze(root: Path, changed_symbol_names: set[str]):
    repo = RepoRef(name="svc", root=str(root))
    scan = scan_repository("svc", root)
    index = build_symbol_index(scan)
    events = detect_event_signals(scan)
    surface = build_system_surface(repo=repo, scan=scan, index=index, events=events)
    changed_ids = [
        symbol.id
        for symbol in index.symbols.values()
        if symbol.name in changed_symbol_names or (symbol.qualified_name or "") in changed_symbol_names
    ]
    builder = FlowBuilder(index=index, surface=surface, scan=scan, events=events)
    flows = builder.build(changed_ids, set(), {})
    return scan, index, surface, flows


def test_python_route_binds_to_its_decorated_handler(python_service: Path) -> None:
    """A FastAPI decorator binds the route to the exact handler symbol."""
    _, index, surface, _ = _analyze(python_service, set())

    labels = {binding.label for binding in surface.routes}
    assert "POST /refund" in labels
    binding = next(item for item in surface.routes if item.label == "POST /refund")
    assert binding.handler_symbol_id is not None
    assert index.symbols[binding.handler_symbol_id].name == "create_refund"


def test_change_deep_in_the_call_graph_reaches_the_route(python_service: Path) -> None:
    """A change two hops below the handler still surfaces the entry route."""
    _, _, _, flows = _analyze(python_service, {"RefundService.retry_refund"})

    assert [flow.entry_label for flow in flows] == ["POST /refund"]
    names = {node.name for node in flows[0].nodes}
    assert "create_refund" in names
    assert "RefundService.retry_refund" in names


def test_flow_reaches_database_and_published_event_with_consumer(python_service: Path) -> None:
    """Downstream expansion names the database, the event, and its consumer."""
    _, _, _, flows = _analyze(python_service, {"RefundService.retry_refund"})
    flow = flows[0]

    kinds = {node.kind for node in flow.nodes}
    assert "database" in kinds
    assert "event" in kinds
    assert "consumer" in kinds
    assert any(node.name == "refund.created" for node in flow.nodes)
    consumers = [node.name for node in flow.nodes if node.kind == "consumer"]
    assert consumers == ["handle_refund_created"]


def test_every_flow_edge_carries_evidence(python_service: Path) -> None:
    """No edge is added to a flow without a concrete source reference."""
    _, _, _, flows = _analyze(python_service, {"RefundService.retry_refund"})

    call_edges = [edge for flow in flows for edge in flow.edges if edge.kind != "routes_to"]
    assert call_edges
    assert all(edge.evidence and edge.evidence[0].file for edge in call_edges)


def test_unrelated_symbols_do_not_enter_the_flow(python_service: Path) -> None:
    """Graph expansion stays on resolved edges instead of pulling in the repo."""
    _, _, _, flows = _analyze(python_service, {"LedgerRepository.reverse"})

    names = {node.name for node in flows[0].nodes}
    assert "register" not in names


def test_existing_tests_map_to_the_affected_route(python_service: Path) -> None:
    """A test issuing the affected request is located as existing verification."""
    scan, index, _, flows = _analyze(python_service, {"RefundService.retry_refund"})
    verification = map_existing_verification(
        flows=flows,
        test_index=build_test_index(scan),
        symbol_index=index,
        changed_symbol_names={"retry_refund"},
        changed_files=set(),
    )

    verified = [item for item in verification if item.status == "verified"]
    assert [item.name for item in verified] == ["test_refund_endpoint"]
    assert verified[0].evidence[0].label == "test_requests_route"


def test_flow_without_any_test_is_reported_unverified(python_service: Path) -> None:
    """Flows with no located test are explicitly marked, not silently omitted."""
    (python_service / "tests" / "test_refund_api.py").unlink()
    scan, index, _, flows = _analyze(python_service, {"RefundService.retry_refund"})
    verification = map_existing_verification(
        flows=flows,
        test_index=build_test_index(scan),
        symbol_index=index,
        changed_symbol_names={"retry_refund"},
        changed_files=set(),
    )

    assert any(item.status == "unverified" and item.name == "POST /refund" for item in verification)


def test_express_mount_prefix_and_commonjs_handler_resolve(tmp_path: Path) -> None:
    """A CommonJS Express app resolves both its mount prefix and its handler."""
    root = tmp_path / "api"
    _write(
        root,
        "app.js",
        "const express = require('express');\n"
        "const app = express();\n"
        "const apiRoutes = require('./src/routes/api');\n"
        "app.use('/v1', apiRoutes);\n",
    )
    _write(
        root,
        "src/routes/api.js",
        'const express = require("express");\n'
        "const router = express.Router();\n"
        'const authController = require("../controllers/AuthController");\n'
        "\n"
        "router.post(\n"
        '  "/login",\n'
        "  validate(),\n"
        "  authController.login\n"
        ");\n"
        "\n"
        "module.exports = router;\n",
    )
    _write(
        root,
        "src/controllers/AuthController.js",
        'const User = require("../models/user");\n'
        "\n"
        "const authController = {\n"
        "  login: async (req, res) => {\n"
        "    const rows = await User.findByEmail(req.body.email);\n"
        "    res.json(rows);\n"
        "  },\n"
        "};\n"
        "\n"
        "module.exports = authController;\n",
    )
    _write(
        root,
        "src/models/user.js",
        'const db = require("../config/database");\n'
        "\n"
        "const User = {\n"
        "  findByEmail: (email) => {\n"
        '    return db.promise().query("SELECT * FROM users WHERE email = ?", [email]);\n'
        "  },\n"
        "};\n"
        "\n"
        "module.exports = User;\n",
    )

    repo = RepoRef(name="api", root=str(root))
    scan = scan_repository("api", root)
    index = build_symbol_index(scan)
    surface = build_system_surface(repo=repo, scan=scan, index=index, events=[])

    assert [binding.label for binding in surface.routes] == ["POST /v1/login"]
    binding = surface.routes[0]
    assert binding.handler_symbol_id is not None
    assert index.symbols[binding.handler_symbol_id].qualified_name == "authController.login"


def test_symbol_role_classification_uses_path_and_name() -> None:
    """Layer classification reads the file path and class name."""
    from sydes.verify.symbol_index import Symbol

    def make(file: str, name: str, class_name: str | None = None) -> Symbol:
        return Symbol(
            id="x",
            repo="r",
            file=file,
            name=name,
            kind="function",
            language="python",
            start_line=1,
            end_line=2,
            class_name=class_name,
        )

    assert classify_symbol_role(make("src/controllers/auth.py", "login")) == "handler"
    assert classify_symbol_role(make("src/repositories/ledger.py", "reverse")) == "repository"
    assert classify_symbol_role(make("src/clients/payment.py", "refund")) == "client"
    assert classify_symbol_role(make("src/services/refund.py", "retry")) == "service"
    assert classify_symbol_role(make("src/util.py", "helper")) == "function"
