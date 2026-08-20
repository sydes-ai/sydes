"""LLM output validation and runtime-dependency inference tests for verify-change."""

from __future__ import annotations

import json
from pathlib import Path

from sydes.llm.client import LLMRequest, LLMResponse
from sydes.verify.llm_findings import (
    build_change_context,
    generate_code_findings,
    generate_verification_gaps,
)
from sydes.verify.models import AffectedFlow, ChangedFile, ChangeSet, FlowNode
from sydes.verify.repo_scan import scan_repository
from sydes.verify.runtime import infer_runtime_dependencies


class _StubLLM:
    """LLM client stub returning a fixed payload."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.prompts: list[str] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.prompts.append(request.prompt)
        return LLMResponse(text=json.dumps(self.payload))


def _context() -> dict:
    change = ChangeSet(
        base="main",
        files=[ChangedFile(repo="api", path="src/refund/service.py", role="source_route_candidate")],
    )
    flow = AffectedFlow(
        id="flow:POST:/refund",
        entry_label="POST /refund",
        nodes=[
            FlowNode(id="route:POST /refund", kind="route", name="POST /refund"),
            FlowNode(id="sym:retry", kind="service", name="RefundService.retryRefund", changed=True),
        ],
    )
    return build_change_context(change=change, flows=[flow], verification=[], diff_text="diff --git ...")


def test_code_findings_reject_files_outside_the_change() -> None:
    """A finding pointing at a file that is not in the diff is discarded."""
    stub = _StubLLM(
        {
            "findings": [
                {"severity": "P1", "title": "Real one", "file": "src/refund/service.py", "line": 142},
                {"severity": "P0", "title": "Hallucinated", "file": "src/does/not/exist.py", "line": 3},
            ]
        }
    )

    findings, warnings = generate_code_findings(context=_context(), llm_client=stub)

    assert [item.title for item in findings] == ["Real one"]
    assert any("unknown file" in item for item in warnings)


def test_code_findings_normalize_unknown_severity() -> None:
    """An out-of-range severity falls back to P2 rather than being trusted."""
    stub = _StubLLM(
        {"findings": [{"severity": "CRITICAL", "title": "x", "file": "src/refund/service.py"}]}
    )

    findings, _ = generate_code_findings(context=_context(), llm_client=stub)

    assert findings[0].severity == "P2"


def test_verification_gaps_require_a_known_graph_node() -> None:
    """A gap must anchor to a node Sydes actually put in the context."""
    stub = _StubLLM(
        {
            "gaps": [
                {"behavior": "refund.created is emitted exactly once", "related_node_ids": ["sym:retry"]},
                {"behavior": "something invented", "related_node_ids": ["sym:nope"]},
                {"behavior": "no anchor at all"},
            ]
        }
    )

    gaps, warnings = generate_verification_gaps(context=_context(), covered_flow_ids=set(), llm_client=stub)

    assert [item.behavior for item in gaps] == ["refund.created is emitted exactly once"]
    assert sum("without a known graph node" in item for item in warnings) == 2


def test_verification_gaps_reject_generic_suggestions() -> None:
    """Generic advice is filtered out; only system behaviors survive."""
    stub = _StubLLM(
        {
            "gaps": [
                {"behavior": "Add more tests for the service", "related_node_ids": ["sym:retry"]},
                {"behavior": "Handle edge cases in retry", "related_node_ids": ["sym:retry"]},
            ]
        }
    )

    gaps, warnings = generate_verification_gaps(context=_context(), covered_flow_ids=set(), llm_client=stub)

    assert gaps == []
    assert sum("generic verification gap" in item for item in warnings) == 2


def test_llm_output_wrapped_in_markdown_fences_is_parsed() -> None:
    """Models that wrap JSON in code fences are handled."""

    class _FencedLLM:
        def generate(self, request: LLMRequest) -> LLMResponse:
            body = json.dumps({"findings": [{"title": "t", "file": "src/refund/service.py"}]})
            return LLMResponse(text=f"```json\n{body}\n```")

    findings, _ = generate_code_findings(context=_context(), llm_client=_FencedLLM())

    assert [item.title for item in findings] == ["t"]


def test_runtime_dependencies_detected_from_env_compose_and_imports(tmp_path: Path) -> None:
    """Env vars, compose images, and client imports each become dependencies."""
    root = tmp_path / "svc"
    (root / "src").mkdir(parents=True)
    (root / ".env.example").write_text(
        "DATABASE_URL=postgres://localhost/app\n"
        "KAFKA_BROKERS=localhost:9092\n"
        "PAYMENT_API_URL=https://payments.internal\n"
        "PORT=3000\n",
        encoding="utf-8",
    )
    (root / "docker-compose.yml").write_text(
        "services:\n"
        "  db:\n"
        "    image: postgres:15\n"
        "  cache:\n"
        "    image: redis:7\n",
        encoding="utf-8",
    )
    (root / "src" / "client.py").write_text("import psycopg\n", encoding="utf-8")

    dependencies = infer_runtime_dependencies(
        scan=scan_repository("svc", root), flows=[], changed_files=set()
    )
    names = {item.name for item in dependencies}
    kinds = {item.kind for item in dependencies}

    assert "PostgreSQL" in names
    assert "Kafka" in names
    assert "Redis" in names
    assert "Payment service" in names
    assert "http_service" in kinds
    assert all(item.detected_from for item in dependencies)


def test_runtime_dependency_ignores_generic_app_urls(tmp_path: Path) -> None:
    """`APP_URL`-style variables are not reported as external services."""
    root = tmp_path / "svc"
    root.mkdir()
    (root / ".env").write_text("APP_URL=http://localhost:3000\nBASE_URL=http://localhost\n", encoding="utf-8")

    dependencies = infer_runtime_dependencies(
        scan=scan_repository("svc", root), flows=[], changed_files=set()
    )

    assert dependencies == []


def test_env_value_refines_generic_database_name(tmp_path: Path) -> None:
    """`DB_CONNECTION=mysql` names MySQL, not a generic SQL database."""
    root = tmp_path / "svc"
    root.mkdir()
    (root / ".env.example").write_text("DB_CONNECTION=mysql\n", encoding="utf-8")

    dependencies = infer_runtime_dependencies(
        scan=scan_repository("svc", root), flows=[], changed_files=set()
    )

    assert [item.name for item in dependencies] == ["MySQL"]
