from __future__ import annotations

from sydes.core.models import TargetSpec, TraceResult, TraceSummary
from sydes.report.terminal import render_terminal


def test_terminal_layered_flow_truncates_long_evidence_and_shows_summary() -> None:
    long_snippet = "const q = `INSERT INTO task_attachments (...) VALUES (...) RETURNING id, name, size, type, created_at, updated_at`;" * 3
    result = TraceResult(
        target=TargetSpec(path="/api/v1/attachments/tasks", method="POST"),
        summary=TraceSummary(confidence=0.8, text="Uploads metadata and returns response."),
        flow={
            "steps": [
                {
                    "id": "step:1",
                    "kind": "request_input",
                    "name": "request body input",
                    "detail": "const data = req.body",
                    "repo": "worklenz",
                    "file": "src/controller.ts",
                    "symbol": "AttachmentController.createTaskAttachment",
                    "evidence": [{"file": "src/controller.ts", "label": "assignment", "snippet": long_snippet}],
                    "confidence": 0.9,
                    "status": "grounded",
                }
            ]
        },
        sinks=[{"kind": "database", "operation": "write", "name": "task_attachments", "evidence": []}],
    )

    rendered = render_terminal(result)

    assert "Summary:" in rendered
    assert "Flow:" in rendered
    assert "request input" in rendered.lower()
    assert "evidence:" in rendered
    assert "..." in rendered

