"""Tests for terminal rendering of trace test matrix output."""

from sydes.core.models import (
    IntegrationTestSuggestion,
    TestMatrix as MatrixModel,
    TestMatrixGroup as MatrixGroupModel,
    TestExpectation as ExpectationModel,
    TraceResult,
    TraceSummary,
    TargetSpec,
)
from sydes.report.terminal import render_terminal


def test_render_terminal_includes_test_matrix_section_when_present() -> None:
    """Trace terminal renderer should include grouped Test Matrix output when present."""
    result = TraceResult(
        target=TargetSpec(path="/users", method="POST"),
        test_matrix=MatrixModel(
            groups=[
                MatrixGroupModel(
                    category="happy_path",
                    tests=[
                        IntegrationTestSuggestion(
                            name="post_users_creates_resource",
                            route="/users",
                            method="POST",
                            summary="verifies POST /users creates a new user and returns success",
                            expectations=[
                                ExpectationModel(kind="http_response", description="request succeeds with expected response")
                            ],
                        )
                    ],
                ),
                MatrixGroupModel(
                    category="validation",
                    tests=[
                        IntegrationTestSuggestion(name="post_users_rejects_missing_required_field", route="/users", method="POST"),
                        IntegrationTestSuggestion(name="post_users_rejects_invalid_payload", route="/users", method="POST"),
                        IntegrationTestSuggestion(name="post_users_rejects_extra_payload_case", route="/users", method="POST"),
                    ],
                ),
            ]
        ),
        summary=TraceSummary(confidence=0.7),
    )

    rendered = render_terminal(result)

    assert "Test Matrix:" in rendered
    assert "Happy Path:" in rendered
    assert "Validation:" in rendered
    assert "post_users_creates_resource" in rendered
    assert "verifies POST /users creates a new user and returns success" in rendered
    assert "post_users_rejects_missing_required_field" in rendered
    assert "post_users_rejects_invalid_payload" in rendered
    assert "post_users_rejects_extra_payload_case" not in rendered
