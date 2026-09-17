"""CBM already models top-level/module-scope value declarations (`const
strategySchema = joi.object()...`) under a `Variable` label -- confirmed
live against a real indexed repository (`sydes-examples/unleash`, the
PR #12632 case): a real `file_path`/`start_line`/`end_line`/`qualified_name`
came back, the same shape `Function`/`Method`/`Class` rows already have.
`_KIND_BY_LABEL` previously never queried CBM for this label at all, so a
changed top-level const/exported schema expression yielded zero symbols
even when the CBM backend was selected (`SYDES_CODE_INTELLIGENCE=cbm`, what
the real GitHub Actions workflows use) -- the native-extractor fix for the
same underlying gap (`trace/handler_symbols/js_ts.py`) does not apply to
this backend at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sydes.code_intelligence.cbm import CBMCodeIntelligence
from sydes.core.models import RepoRef

REPO = "app"


class FakeClient:
    def __init__(self, rows_by_label: dict[str, list[list[str]]] | None = None) -> None:
        self._rows_by_label = rows_by_label or {}
        self.metrics: dict = {"calls": 0, "session_start_ms": 0, "mean_call_ms": 0}
        self.server_version = "fake"
        self.malformed_rows = 0

    def index_repository(self, repo_path, *, mode: str = "fast") -> dict[str, Any]:
        return {"project": "proj", "nodes": 1, "excluded": {"dirs": []}}

    def all_symbols(self, project: str, label: str) -> list[list[str]]:
        return self._rows_by_label.get(label, [])

    def all_imports(self, project: str) -> list[list[str]]:
        return []

    def decorated_symbols(self, project: str, *, page_size: int = 500) -> list[dict]:
        return []


def _build(client: FakeClient, repo_root: Path) -> Any:
    backend = CBMCodeIntelligence(client=client)
    return backend.build_or_update([RepoRef(name=REPO, root=str(repo_root))], defer_edges=True)


def test_a_top_level_const_variable_becomes_a_symbol(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "strategy-schema.ts").write_text(
        "const strategySchema = joi.object({});\n", encoding="utf-8",
    )
    client = FakeClient(rows_by_label={
        "Variable": [[
            "strategySchema", "src/strategy-schema.ts", "1", "1", None, "true",
            "app.src.strategy-schema.strategySchema",
        ]],
    })

    facts = _build(client, tmp_path)

    symbols = facts.symbol_index["repos"][0]["files"]
    file_entry = next(f for f in symbols if f["path"] == "src/strategy-schema.ts")
    names = {s["name"]: s for s in file_entry["symbols"]}
    assert "strategySchema" in names
    assert names["strategySchema"]["kind"] == "variable"
    assert names["strategySchema"]["exported"] is True


def test_no_variable_rows_still_works_exactly_as_before(tmp_path: Path) -> None:
    """No regression for repos/backends with nothing under this label."""
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    facts = _build(FakeClient(), tmp_path)
    assert facts.symbol_index["repos"][0]["files"] == []
