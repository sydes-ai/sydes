"""Tests for terminal rendering of trace suggested test output."""

from sydes.core.models import (
    IntegrationTestSuggestion,
    TestExpectation as ExpectationModel,
    TraceResult,
    TraceSummary,
    TargetSpec,
)
from sydes.report.terminal import render_terminal


def test_render_terminal_includes_suggested_tests_section_when_present() -> None:
    """Trace terminal renderer should include Suggested Tests section when tests exist."""
    result = TraceResult(
        target=TargetSpec(path="/users", method="POST"),
        tests=[
            IntegrationTestSuggestion(
                name="post_users_creates_record",
                route="/users",
                method="POST",
                summary="validate primary route behavior",
                expectations=[
                    ExpectationModel(kind="http_response", description="request succeeds with expected response"),
                    ExpectationModel(kind="side_effect", description="created data is persisted", target="database"),
                ],
            )
        ],
        summary=TraceSummary(confidence=0.7),
    )

    rendered = render_terminal(result)

    assert "Suggested Tests:" in rendered
    assert "post_users_creates_record" in rendered
    assert "expects: request succeeds with expected response" in rendered
    assert "expects: created data is persisted" in rendered
