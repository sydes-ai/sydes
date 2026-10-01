"""Change context for model stages: priority, hunk boundaries, budgets, no sliced JSON."""

from __future__ import annotations

import json

from sydes.verify.diff_context import (
    HOSTED_CONTEXT_CHARS, context_chars, file_priority, fit_prompt, select_diff, trim_list,
)


def _file(path: str, hunks: int, body: str = "x") -> str:
    out = f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
    for h in range(hunks):
        out += f"@@ -{h * 10 + 1},2 +{h * 10 + 1},3 @@\n context\n+{body * 60} {path} hunk{h}\n-old\n"
    return out


DIFF = (
    _file("docs/installation/configuration.md", 2)
    + _file("backend/src/app/handler.py", 3)
    + _file("backend/tests/app/test_handler.py", 2)
    + _file("web-frontend/modules/x/Comp.vue", 1)
)


def test_priorities() -> None:
    assert file_priority("backend/src/app/handler.py") == 0
    assert file_priority("backend/tests/app/test_handler.py") == 1
    assert file_priority("web-frontend/modules/x/Comp.vue") == 2
    assert file_priority("docs/installation/configuration.md") == 3
    assert file_priority("backend/src/app/migrations/0001_init.py") == 3
    assert file_priority("go.sum") == 3


def test_whole_diff_when_it_fits() -> None:
    assert select_diff(DIFF, 10**6) == DIFF


def test_source_first_whole_hunks_and_explicit_omissions() -> None:
    out = select_diff(DIFF, 900)
    # every hunk of changed source fits, so all of it is in; the test file and lower tiers are
    # named as omitted rather than partly shown
    assert all(f"handler.py hunk{i}" in out for i in range(3))
    for line in out.splitlines():
        if line.startswith("+x"):
            assert line.endswith(tuple(f"hunk{i}" for i in range(3)))
    omitted = out.split("files changed but not shown")[-1]
    assert "backend/tests/app/test_handler.py" in omitted
    assert "docs/installation/configuration.md" in omitted


def test_lower_tiers_never_displace_changed_source() -> None:
    """Baserow #5507 (gpt-6.1-sol): with a per-file first-hunk pass, the 16 KB test file's first
    hunk and a changelog went in before source hunks 2..8, and the model reported the row
    action / handler / trash changes as omitted. Source now completes before any test hunk."""
    out = select_diff(DIFF, 650)
    assert "handler.py hunk0" in out and "handler.py hunk1" in out
    assert "test_handler.py hunk0" not in out
    assert "more hunk(s) of backend/src/app/handler.py not shown: context budget" in out


def test_within_a_tier_every_file_gets_a_hunk_before_any_gets_all() -> None:
    two_sources = _file("backend/src/app/handler.py", 3) + _file("backend/src/app/models.py", 2)
    out = select_diff(two_sources, 800)
    assert "handler.py hunk0" in out and "models.py hunk0" in out
    assert "handler.py hunk2" not in out  # one source file did not starve the other


def test_fit_prompt_trims_metadata_first_and_never_slices_json() -> None:
    ctx = {"symbols": [{"name": f"s{i}", "file": "backend/src/app/handler.py" * 3} for i in range(200)],
           "files": [], "diff": DIFF}
    prompt, sent = fit_prompt("HEADER", ctx, limit=6000, full_diff=DIFF, trims=[trim_list("symbols", 20)])
    assert len(prompt) <= 6000
    payload = json.loads(prompt.split("\nContext:\n", 1)[1])  # valid JSON
    assert len(payload["symbols"]) == 20 and payload["symbols_not_shown"] == 180
    assert "handler.py hunk0" in payload["diff"]


def test_fit_prompt_with_everything_fitting_is_the_plain_serialization() -> None:
    ctx = {"files": [], "diff": DIFF}
    prompt, _ = fit_prompt("H", ctx, limit=10**6, full_diff=DIFF, trims=[])
    assert prompt == "H\nContext:\n" + json.dumps(ctx, ensure_ascii=True, separators=(",", ":"))


def test_context_chars(monkeypatch) -> None:
    monkeypatch.delenv("SYDES_LLM_CONTEXT_CHARS", raising=False)
    monkeypatch.delenv("SYDES_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("SYDES_LLM_MODEL", raising=False)
    assert context_chars(20_000) == 20_000
    assert context_chars(20_000, "ollama:qwen2.5-coder:3b") == 20_000
    assert context_chars(20_000, "openai:gpt-6.1-sol") == HOSTED_CONTEXT_CHARS
    assert context_chars(20_000, "anthropic:claude-x") == HOSTED_CONTEXT_CHARS
    monkeypatch.setenv("SYDES_LLM_CONTEXT_CHARS", "240000")
    assert context_chars(20_000) == 240_000
    assert context_chars(20_000, "openai:gpt-6.1-sol") == 240_000  # an explicit size wins
    monkeypatch.setenv("SYDES_LLM_CONTEXT_CHARS", "nonsense")
    assert context_chars(20_000) == 20_000


def _sized(path: str, hunks: int, total: int) -> str:
    return _file(path, hunks, body="y" * max(1, total // (hunks * 60)))


#: the shape of Baserow #5507 (sizes per file, hunk counts): 22k chars of changed source,
#: a 16k test file, a workflow and a changelog
BASEROW_5507_SHAPE = (
    _sized("backend/src/baserow/contrib/database/apps.py", 2, 1200)
    + _sized("backend/src/baserow/contrib/database/rows/actions.py", 8, 7400)
    + _sized("backend/src/baserow/contrib/database/rows/handler.py", 7, 5200)
    + _sized("backend/src/baserow/contrib/database/views/handler.py", 1, 950)
    + _sized("backend/src/baserow/core/trash/handler.py", 2, 4300)
    + _sized("premium/backend/src/baserow_premium/permission_manager.py", 2, 1000)
    + _sized("enterprise/backend/tests/baserow_enterprise_tests/views/test_restricted_view.py", 3, 16000)
    + _sized(".github/workflows/sydes.yml", 1, 2600)
    + _sized("changelog/entries/unreleased/bug/5506_fix.json", 1, 700)
)


def test_semantic_prompt_for_a_hosted_model_carries_every_changed_source_hunk(monkeypatch) -> None:
    """Baserow #5507: gpt-6.1-sol got 10 of 24 source hunks (20k-char budget, tests' and
    changelog's first hunks taken before source hunks) and reported the row action / handler /
    trash changes as omitted. A hosted model now gets them all."""
    from sydes.verify.models import ChangeSet
    from sydes.verify.pr_semantic_analysis import _bounded_prompt, _build_semantic_context

    monkeypatch.delenv("SYDES_LLM_CONTEXT_CHARS", raising=False)
    ctx = _build_semantic_context(change=ChangeSet(base="main"), diff_text=BASEROW_5507_SHAPE)
    hosted = _bounded_prompt(ctx, "", "openai:gpt-6.1-sol")
    for path, n in (("rows/actions.py", 8), ("rows/handler.py", 7), ("core/trash/handler.py", 2)):
        assert all(f"{path} hunk{i}" in hosted for i in range(n)), path
    assert "not shown" not in hosted.split("Context:", 1)[1]
    # a small local model keeps the small budget, but source still comes before tests
    local = _bounded_prompt(ctx, "", "ollama:qwen2.5-coder:3b")
    assert len(local) <= 20_000 and "test_restricted_view.py hunk0" not in local
