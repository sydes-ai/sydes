"""Change context for model stages: priority, hunk boundaries, budgets, no sliced JSON."""

from __future__ import annotations

import json

from sydes.verify.diff_context import context_chars, file_priority, fit_prompt, select_diff, trim_list


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
    assert out.index("backend/src/app/handler.py") < out.index("backend/tests/app/test_handler.py")
    # every included hunk is complete
    for line in out.splitlines():
        if line.startswith("+x"):
            assert line.endswith(tuple(f"hunk{i}" for i in range(3)))
    assert "not shown: context budget" in out
    assert "docs/installation/configuration.md" in out.split("files changed but not shown")[-1]


def test_every_included_file_gets_a_hunk_before_any_gets_all() -> None:
    out = select_diff(DIFF, 1150)
    assert "handler.py hunk0" in out and "test_handler.py hunk0" in out
    assert "handler.py hunk2" not in out  # the source file did not starve the test file


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
    assert context_chars(20_000) == 20_000
    monkeypatch.setenv("SYDES_LLM_CONTEXT_CHARS", "240000")
    assert context_chars(20_000) == 240_000
    monkeypatch.setenv("SYDES_LLM_CONTEXT_CHARS", "nonsense")
    assert context_chars(20_000) == 20_000
