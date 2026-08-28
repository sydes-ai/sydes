"""CBM fast-mode exclusion recovery.

CBM's `mode="fast"` index can exclude entire directories from a repository.
On spring-petclinic, `index_repository()` returned `excluded.dirs` including
`src/main/java/org/springframework/samples` — the directory containing the
changed production file — so no symbols in it were indexed at all.
GraphSlice then requested seeds for symbols CBM had never heard of and
resolved zero of them.

These tests pin the recovery: when a changed file falls under an excluded
directory, `CBMCodeIntelligence.build_or_update` retries indexing that
repository once with `mode="full"` and continues with the full result.
Nothing here is Java-specific — the mechanism only reads `excluded.dirs`
and compares paths.
"""

from __future__ import annotations

from typing import Any

import pytest

from sydes.code_intelligence.base import CodeIntelligenceError
from sydes.code_intelligence.cbm import (
    CBMCodeIntelligence,
    _changed_files_under_excluded_dirs,
    _excluded_dirs_from_index_payload,
    _is_under_excluded_dir,
)
from sydes.core.models import RepoRef

REPO = "petclinic"

# The exact regression shape from spring-petclinic PR #2589.
_CHANGED_PRODUCTION_FILE = (
    "src/main/java/org/springframework/samples/petclinic/owner/OwnerController.java"
)
_EXCLUDED_JAVA_DIR = "src/main/java/org/springframework/samples"


class FakeIndexClient:
    """A CBM client double whose `index_repository` behaves like the real
    fast/full split: fast excludes a directory, full does not."""

    def __init__(
        self,
        *,
        fast_excluded_dirs: list[str] | None = None,
        full_fails: bool = False,
        fast_project: str = "proj-fast",
        full_project: str = "proj-full",
    ) -> None:
        self._fast_excluded_dirs = fast_excluded_dirs or []
        self._full_fails = full_fails
        self._fast_project = fast_project
        self._full_project = full_project
        self.index_calls: list[str] = []  # the `mode` of each call, in order
        self.metrics: dict = {"calls": 0, "session_start_ms": 0, "mean_call_ms": 0}
        self.server_version = "fake"
        self.malformed_rows = 0

    def index_repository(self, repo_path, *, mode: str = "fast") -> dict[str, Any]:
        self.index_calls.append(mode)
        if mode == "fast":
            return {
                "project": self._fast_project,
                "nodes": 10,
                "excluded": {"dirs": list(self._fast_excluded_dirs)},
            }
        if self._full_fails:
            raise CodeIntelligenceError("CBM full-mode indexing failed")
        return {"project": self._full_project, "nodes": 500, "excluded": {"dirs": []}}

    def all_symbols(self, project: str, label: str) -> list[list[str]]:
        return []

    def all_imports(self, project: str) -> list[list[str]]:
        return []

    def decorated_symbols(self, project: str, *, page_size: int = 500) -> list[dict]:
        return []


def _build(client: FakeIndexClient, *, changed_files: list[str], repo_root="/repo"):
    backend = CBMCodeIntelligence(client=client)
    facts = backend.build_or_update(
        [RepoRef(name=REPO, root=repo_root)],
        defer_edges=True,
        changed_files_by_repo={REPO: changed_files},
    )
    return backend, facts


# --------------------------------------------------------------------------
# Pure path-matching helpers
# --------------------------------------------------------------------------


def test_a_changed_file_under_the_excluded_directory_matches() -> None:
    assert _is_under_excluded_dir(_CHANGED_PRODUCTION_FILE, _EXCLUDED_JAVA_DIR) is True


def test_prefix_collision_does_not_falsely_match() -> None:
    """`foo` must not match `foobar` — a naive string-prefix check would."""
    assert _is_under_excluded_dir("src/main/java/foobar/X.java", "src/main/java/foo") is False


def test_an_unrelated_excluded_directory_does_not_match() -> None:
    assert _is_under_excluded_dir("src/main/java/other/Y.java", _EXCLUDED_JAVA_DIR) is False


def test_backslash_separators_are_normalized() -> None:
    assert _is_under_excluded_dir(
        "src\\main\\java\\org\\springframework\\samples\\petclinic\\owner\\OwnerController.java",
        _EXCLUDED_JAVA_DIR,
    ) is True


def test_excluded_dirs_read_defensively_from_a_missing_key() -> None:
    assert _excluded_dirs_from_index_payload({"project": "p", "nodes": 1}) == []


def test_excluded_dirs_read_defensively_from_a_malformed_shape() -> None:
    assert _excluded_dirs_from_index_payload({"excluded": "not-a-dict"}) == []
    assert _excluded_dirs_from_index_payload({"excluded": {"dirs": "not-a-list"}}) == []
    assert _excluded_dirs_from_index_payload({"excluded": {"dirs": [1, None, "ok/dir"]}}) == ["ok/dir"]


def test_changed_files_under_excluded_dirs_is_empty_with_no_overlap() -> None:
    assert _changed_files_under_excluded_dirs(
        ["src/main/java/foobar/X.java"], [_EXCLUDED_JAVA_DIR],
    ) == []


# --------------------------------------------------------------------------
# 1-2. Retry triggers for production and test files
# --------------------------------------------------------------------------


def test_changed_production_file_under_excluded_dir_retries_full_exactly_once() -> None:
    client = FakeIndexClient(fast_excluded_dirs=[_EXCLUDED_JAVA_DIR])

    _, facts = _build(client, changed_files=[_CHANGED_PRODUCTION_FILE])

    assert client.index_calls == ["fast", "full"]
    assert facts.metrics.get("session", {}) is not None  # facts still produced


def test_changed_test_file_under_excluded_dir_retries_full_exactly_once() -> None:
    test_excluded_dir = "src/test/java/org/springframework/samples"
    client = FakeIndexClient(fast_excluded_dirs=[test_excluded_dir])
    changed_test_file = (
        "src/test/java/org/springframework/samples/petclinic/owner/OwnerControllerTests.java"
    )

    _build(client, changed_files=[changed_test_file])

    assert client.index_calls == ["fast", "full"]


# --------------------------------------------------------------------------
# 3-5. No false-positive retries
# --------------------------------------------------------------------------


def test_an_unrelated_excluded_directory_does_not_trigger_a_retry() -> None:
    client = FakeIndexClient(fast_excluded_dirs=["src/main/java/some/other/pkg"])

    _build(client, changed_files=[_CHANGED_PRODUCTION_FILE])

    assert client.index_calls == ["fast"]


def test_a_prefix_collision_does_not_trigger_a_false_retry() -> None:
    client = FakeIndexClient(fast_excluded_dirs=["src/main/java/foo"])

    _build(client, changed_files=["src/main/java/foobar/X.java"])

    assert client.index_calls == ["fast"]


def test_missing_excluded_metadata_behaves_exactly_as_before() -> None:
    client = FakeIndexClient(fast_excluded_dirs=[])  # no exclusion at all

    _build(client, changed_files=[_CHANGED_PRODUCTION_FILE])

    assert client.index_calls == ["fast"]


# --------------------------------------------------------------------------
# 6. Multiple changed files, only one excluded
# --------------------------------------------------------------------------


def test_one_excluded_file_among_several_changed_files_still_retries() -> None:
    client = FakeIndexClient(fast_excluded_dirs=[_EXCLUDED_JAVA_DIR])
    changed = [
        "pom.xml",
        "src/main/resources/application.properties",
        _CHANGED_PRODUCTION_FILE,
    ]

    _build(client, changed_files=changed)

    assert client.index_calls == ["fast", "full"]


# --------------------------------------------------------------------------
# 7. Full-retry failure preserves existing failure semantics
# --------------------------------------------------------------------------


def test_a_failed_full_retry_raises_rather_than_using_the_incomplete_fast_index() -> None:
    client = FakeIndexClient(fast_excluded_dirs=[_EXCLUDED_JAVA_DIR], full_fails=True)

    with pytest.raises(CodeIntelligenceError, match="full-mode indexing failed"):
        _build(client, changed_files=[_CHANGED_PRODUCTION_FILE])

    # The retry was attempted — this is a real failure, not a silent skip.
    assert client.index_calls == ["fast", "full"]


# --------------------------------------------------------------------------
# 8. No regression to the normal path
# --------------------------------------------------------------------------


def test_no_changed_files_at_all_never_retries() -> None:
    client = FakeIndexClient(fast_excluded_dirs=[_EXCLUDED_JAVA_DIR])

    backend = CBMCodeIntelligence(client=client)
    backend.build_or_update([RepoRef(name=REPO, root="/repo")], defer_edges=True)

    assert client.index_calls == ["fast"]


def test_omitting_changed_files_by_repo_entirely_never_retries() -> None:
    """Callers that do not pass the new parameter keep exactly today's
    behavior — no `TypeError`, no retry."""
    client = FakeIndexClient(fast_excluded_dirs=[_EXCLUDED_JAVA_DIR])

    backend = CBMCodeIntelligence(client=client)
    backend.build_or_update([RepoRef(name=REPO, root="/repo")], defer_edges=True)

    assert client.index_calls == ["fast"]


def test_a_second_call_with_a_full_index_project_id_does_not_double_retry() -> None:
    """A full-mode payload reporting its OWN (empty) exclusion list must not
    trigger a second retry — one retry, never more."""
    client = FakeIndexClient(fast_excluded_dirs=[_EXCLUDED_JAVA_DIR])

    _build(client, changed_files=[_CHANGED_PRODUCTION_FILE])

    assert client.index_calls.count("full") == 1


# --------------------------------------------------------------------------
# 9. GraphSlice fallback semantics are untouched
# --------------------------------------------------------------------------


def test_attach_bounded_edges_fallback_semantics_are_unaffected(tmp_path) -> None:
    """The index-mode retry and GraphSlice's own CodeIntelligenceError
    fallback are independent mechanisms; this one must not change the
    other's behavior."""
    from sydes.code_intelligence.graph_slice import GraphSliceLimits

    class _SeedFailingClient(FakeIndexClient):
        def all_symbols(self, project: str, label: str) -> list[list[str]]:
            # A resolvable symbol, so `attach_bounded_edges` actually reaches
            # the seed-scoped query below rather than short-circuiting on
            # "nothing resolved" (a distinct, already-tested condition).
            if label == "Function":
                return [["whatever", "src/main/java/Whatever.java", "1", "2", "", "true", "mod.whatever"]]
            return []

        def call_edges_for_seeds(self, project, seeds, *, limit=1000):
            raise CodeIntelligenceError("CBM transport failed")

        def usage_edges_for_seeds(self, project, seeds, *, limit=1000):
            raise CodeIntelligenceError("CBM transport failed")

        def all_call_edges(self, project):
            return []

        def all_usage_edges(self, project):
            return []

    client = _SeedFailingClient(fast_excluded_dirs=[_EXCLUDED_JAVA_DIR])
    backend, facts = _build(client, changed_files=[_CHANGED_PRODUCTION_FILE])

    # The index-mode retry still happened...
    assert client.index_calls == ["fast", "full"]
    # ...and GraphSlice's own, separate fallback-on-error behavior is intact.
    outcome = backend.attach_bounded_edges(
        facts, seed_symbols=["whatever"], limits=GraphSliceLimits(),
    )
    assert outcome.fell_back is True


# --------------------------------------------------------------------------
# Diagnostics / tracing surface
# --------------------------------------------------------------------------


def test_a_retry_is_reported_in_diagnostics() -> None:
    client = FakeIndexClient(fast_excluded_dirs=[_EXCLUDED_JAVA_DIR])

    _, facts = _build(client, changed_files=[_CHANGED_PRODUCTION_FILE])

    assert any("cbm_index_mode" in line and "full" in line for line in facts.diagnostics)


def test_no_retry_adds_no_index_mode_diagnostic_noise() -> None:
    client = FakeIndexClient(fast_excluded_dirs=[])

    _, facts = _build(client, changed_files=[_CHANGED_PRODUCTION_FILE])

    assert not any("cbm_index_mode" in line for line in facts.diagnostics)


def test_index_mode_decision_is_traced(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sydes.observability import trace as _trace

    monkeypatch.setenv(_trace.TRACE_DIR_ENV_VAR, str(tmp_path))
    client = FakeIndexClient(fast_excluded_dirs=[_EXCLUDED_JAVA_DIR])

    _build(client, changed_files=[_CHANGED_PRODUCTION_FILE])

    events = [
        __import__("json").loads(line)
        for line in (tmp_path / "index_decisions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(events) == 1
    event = events[0]
    assert event["initial_mode"] == "fast"
    assert event["retried"] is True
    assert event["retry_reason"] == "changed_file_under_excluded_dir"
    assert event["decided_mode"] == "full"
    assert _CHANGED_PRODUCTION_FILE in event["triggering_changed_files"]


def test_index_mode_decision_traces_the_no_retry_case_too(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sydes.observability import trace as _trace

    monkeypatch.setenv(_trace.TRACE_DIR_ENV_VAR, str(tmp_path))
    client = FakeIndexClient(fast_excluded_dirs=[])

    _build(client, changed_files=[_CHANGED_PRODUCTION_FILE])

    events = [
        __import__("json").loads(line)
        for line in (tmp_path / "index_decisions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(events) == 1
    assert events[0]["retried"] is False
    assert events[0]["decided_mode"] == "fast"
