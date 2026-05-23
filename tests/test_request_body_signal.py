"""Tests for generic request-body route input signal inference."""

from sydes.core.models import EvidenceRef, Flow, FlowStep, GraphNode, RepoRef, TraceResult, TraceSummary, TargetSpec
from sydes.generate.tests import infer_request_body_signal


def _trace_with_step(name: str, *, metadata: dict | None = None, snippet: str | None = None, method: str = "POST") -> TraceResult:
    """Build a minimal trace containing one internal step for signal checks."""
    step = GraphNode(
        id="step",
        type="internal_step",
        name=name,
        metadata=metadata or {},
        evidence=[EvidenceRef(file="app/routes.py", symbol="handler", snippet=snippet)] if snippet else [],
        repo="api",
    )
    return TraceResult(
        target=TargetSpec(path="/items", method=method),
        nodes=[GraphNode(id="ep", type="api_endpoint", name="/items", method=method, path="/items", repo="api"), step],
        flows=[Flow(id="f1", name=f"{method} /items", entry_node="ep", steps=[FlowStep(node_id="ep", kind="endpoint"), FlowStep(node_id="step", kind="input")])],
        summary=TraceSummary(confidence=0.7),
    )


def test_request_body_signal_detects_python_get_json_without_fields() -> None:
    """request.get_json() should mark body consumed and use generic fallback shape when fields unknown."""
    trace = _trace_with_step(
        "read JSON request body",
        metadata={"step_kind": "input", "expression": "request.get_json()"},
        snippet="data = request.get_json()",
    )
    signal = infer_request_body_signal(trace)
    assert signal.consumed is True
    assert signal.shape == {"type": "object", "description": "valid JSON payload"}
    assert signal.required is True


def test_request_body_signal_detects_req_body_token() -> None:
    """req.body evidence should trigger generic body-consumption signal."""
    trace = _trace_with_step(
        "parse input",
        metadata={"step_kind": "transform", "expression": "req.body"},
        snippet="const payload = req.body",
    )
    signal = infer_request_body_signal(trace)
    assert signal.consumed is True


def test_request_body_signal_detects_request_body_annotation() -> None:
    """@RequestBody annotation evidence should trigger body-consumption signal."""
    trace = _trace_with_step(
        "handle request",
        metadata={"step_kind": "unknown"},
        snippet="@RequestBody UserCreate body",
    )
    signal = infer_request_body_signal(trace)
    assert signal.consumed is True


def test_request_body_signal_uses_input_model_and_inferred_fields(tmp_path) -> None:
    """Input model evidence should infer body shape fields when model class is available."""
    repo_root = tmp_path / "api"
    repo_root.mkdir()
    (repo_root / "models.py").write_text(
        "\n".join(
            [
                "from pydantic import BaseModel",
                "class UserCreate(BaseModel):",
                "    name: str",
                "    email: str",
            ]
        ),
        encoding="utf-8",
    )
    trace = TraceResult(
        target=TargetSpec(path="/users", method="POST"),
        repos=[RepoRef(name="api", root=str(repo_root))],
        nodes=[
            GraphNode(id="ep", type="api_endpoint", name="/users", method="POST", path="/users", repo="api"),
            GraphNode(
                id="step",
                type="internal_step",
                name="input model: UserCreate",
                metadata={"step_kind": "input_model", "expression": "UserCreate"},
                repo="api",
            ),
        ],
        flows=[Flow(id="f1", name="POST /users", entry_node="ep", steps=[FlowStep(node_id="ep", kind="endpoint"), FlowStep(node_id="step", kind="input_model")])],
        summary=TraceSummary(confidence=0.7),
    )
    signal = infer_request_body_signal(trace)
    assert signal.consumed is True
    assert signal.shape == {"name": "string", "email": "string"}


def test_request_body_signal_absent_for_get_without_body_evidence() -> None:
    """GET traces with no body evidence should not emit a request-body signal."""
    trace = TraceResult(
        target=TargetSpec(path="/users", method="GET"),
        nodes=[GraphNode(id="ep", type="api_endpoint", name="/users", method="GET", path="/users", repo="api")],
        flows=[Flow(id="f1", name="GET /users", entry_node="ep", steps=[FlowStep(node_id="ep", kind="endpoint")])],
        summary=TraceSummary(confidence=0.7),
    )
    signal = infer_request_body_signal(trace)
    assert signal.consumed is False
    assert signal.shape is None
