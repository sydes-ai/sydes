"""`sydes.recovery.context.build_context` -- specifically the
`declarative_entrypoints` wiring: a small, targeted, deterministic scan
of the files next to what changed, added because discovery never once
proposed the real HTTP entrypoint across 5 live TS reliability-experiment
runs (its controller was a sibling of the changed file, never itself part
of the diff)."""

from __future__ import annotations

from pathlib import Path

from sydes.core.models import EndpointCandidate
from sydes.recovery.context import build_context
from sydes.recovery.trigger import RecoveryTrigger
from sydes.verify.models import (
    AffectedBoundary,
    ChangedSymbol,
    ChangeSet,
    ChangeSummary,
    ChangeVerificationResult,
    VERDICT_INCOMPLETE,
)

REPO = "app"


def _result(changed_file: str, known_routes: list[EndpointCandidate] | None = None) -> ChangeVerificationResult:
    return ChangeVerificationResult(
        change=ChangeSet(
            base="main", head="abc",
            symbols=[ChangedSymbol(id=f"app:{changed_file}:handler", repo=REPO, file=changed_file, name="handler")],
        ),
        summary=ChangeSummary(verdict=VERDICT_INCOMPLETE),
        known_routes=known_routes or [],
    )


def _trigger() -> RecoveryTrigger:
    return RecoveryTrigger(gap_kinds=("no_established_flow",), reason="test")


def test_without_repo_root_declarative_entrypoints_is_empty(tmp_path: Path):
    context = build_context(_result("src/user/handler.ts"), _trigger())
    assert context.declarative_entrypoints == ()


def test_with_repo_root_finds_a_sibling_controller_not_itself_changed(tmp_path: Path):
    module_dir = tmp_path / "src" / "user"
    module_dir.mkdir(parents=True)
    (module_dir / "handler.ts").write_text("export function handler() {}\n")
    (module_dir / "controller.ts").write_text(
        "@Controller('users')\nexport class UserController {\n"
        "  @Get('/users')\n  async list() {}\n}\n"
    )

    context = build_context(_result("src/user/handler.ts"), _trigger(), repo_root=tmp_path)
    assert len(context.declarative_entrypoints) == 1
    assert "list" in context.declarative_entrypoints[0]
    assert "controller.ts" in context.declarative_entrypoints[0]


def test_with_repo_root_but_no_entrypoint_nearby_is_empty(tmp_path: Path):
    module_dir = tmp_path / "src" / "user"
    module_dir.mkdir(parents=True)
    (module_dir / "handler.ts").write_text("export function handler() {}\n")

    context = build_context(_result("src/user/handler.ts"), _trigger(), repo_root=tmp_path)
    assert context.declarative_entrypoints == ()


def test_nonexistent_repo_root_does_not_raise(tmp_path: Path):
    missing = tmp_path / "does-not-exist"
    context = build_context(_result("src/user/handler.ts"), _trigger(), repo_root=missing)
    assert context.declarative_entrypoints == ()


# ---------------------------------------------------------------------------
# repo_known_routes: opt-in, deterministic, entrypoint-anchoring context for
# discovery when the changed file is far (in file-tree distance) from the
# route that reaches it -- declarative_entrypoints only scans siblings of the
# changed file, so it finds nothing when the real controller lives several
# directories away (a controller -> service -> entity -> value-object chain
# routinely does). This is the same "hand discovery the fact instead of
# hoping it stumbles onto it" fix, sourced from the run's own already-
# computed route discovery instead of a directory scan.
# ---------------------------------------------------------------------------

_ROUTE = EndpointCandidate(
    method="POST", path="/v1/users", handler="UserController.createUser",
    file="src/modules/user/infra/http/user.controller.ts", repo=REPO,
)


def test_repo_known_routes_is_empty_by_default_even_with_known_routes_present():
    context = build_context(_result("src/deep/value.ts", known_routes=[_ROUTE]), _trigger())
    assert context.repo_known_routes == ()


def test_repo_known_routes_populated_when_opted_in():
    context = build_context(
        _result("src/deep/value.ts", known_routes=[_ROUTE]), _trigger(), include_repo_routes=True,
    )
    assert len(context.repo_known_routes) == 1
    entry = context.repo_known_routes[0]
    assert "POST" in entry and "/v1/users" in entry
    assert "UserController.createUser" in entry
    assert "user.controller.ts" in entry


def test_repo_known_routes_drops_incomplete_candidates_and_dedupes():
    incomplete = EndpointCandidate(method="GET", path=None, handler="X.y", file="a.ts", repo=REPO)
    duplicate = _ROUTE.model_copy()
    context = build_context(
        _result("src/deep/value.ts", known_routes=[_ROUTE, duplicate, incomplete]),
        _trigger(), include_repo_routes=True,
    )
    assert len(context.repo_known_routes) == 1


def test_repo_known_routes_empty_when_no_routes_discovered():
    context = build_context(_result("src/deep/value.ts"), _trigger(), include_repo_routes=True)
    assert context.repo_known_routes == ()


# ---------------------------------------------------------------------------
# entrypoint_entities: `propose_graph_path`'s only source of candidate
# entrypoints (see `sydes.recovery.graph_path`). A background/queue-triggered
# change (a task consumer, a servlet filter -- no HTTP route, no established
# `AffectedFlow`) previously had NOTHING feed a real entrypoint into this
# list at all: `affected_flows` requires a handler already resolved (exactly
# what's unresolved when recovery triggers), and `known_routes` is HTTP-only.
# `affected_boundaries` (api/callable/async/external) is the only place a
# non-HTTP entrypoint-like handler is recorded.
# ---------------------------------------------------------------------------


def _result_with_boundaries(changed_file: str, boundaries: list[AffectedBoundary]) -> ChangeVerificationResult:
    result = _result(changed_file)
    result.affected_boundaries = boundaries
    return result


def test_entrypoint_entities_includes_an_affected_boundarys_symbol():
    boundary = AffectedBoundary(
        id="boundary:1", kind="async", subtype="queue_consumer",
        symbol="RedisTaskProcessor.ProcessTask", file="worker/processor.go", status="inferred",
    )
    context = build_context(_result_with_boundaries("worker/task.go", [boundary]), _trigger())
    assert any(
        e.symbol == "RedisTaskProcessor.ProcessTask" and e.file == "worker/processor.go"
        for e in context.entrypoint_entities
    )


def test_entrypoint_entities_excludes_a_self_referential_boundary():
    """Regression: a boundary whose own `.symbol` IS one of its
    `.changed_symbols` (the changed method itself, flagged as its own
    entrypoint-like handler) must never become a candidate seed --
    otherwise `propose_graph_path`'s `target in sources` shortcut fires
    immediately, silently pre-empting a real chain to an actual upstream
    entrypoint from ever being searched for. This exact case broke a
    previously-working real path in a live repo."""
    boundary = AffectedBoundary(
        id="boundary:1", kind="async", subtype="queue_consumer",
        symbol="RedisTaskProcessor.ProcessTask", file="worker/processor.go",
        changed_symbols=["RedisTaskProcessor.ProcessTask"], status="inferred",
    )
    context = build_context(_result_with_boundaries("worker/task.go", [boundary]), _trigger())
    assert context.entrypoint_entities == ()


def test_entrypoint_entities_skips_a_boundary_missing_symbol_or_file():
    boundary = AffectedBoundary(id="boundary:1", kind="async", symbol=None, file=None)
    context = build_context(_result_with_boundaries("worker/task.go", [boundary]), _trigger())
    assert context.entrypoint_entities == ()


def test_entrypoint_entities_dedupes_a_boundary_matching_an_existing_route():
    route = EndpointCandidate(method="POST", path="/logout", handler="AuthController.logout", file="auth.ts", repo=REPO)
    boundary = AffectedBoundary(id="boundary:1", kind="api", symbol="AuthController.logout", file="auth.ts")
    result = _result("auth.ts", known_routes=[route])
    result.affected_boundaries = [boundary]
    context = build_context(result, _trigger())
    matches = [e for e in context.entrypoint_entities if e.symbol == "AuthController.logout" and e.file == "auth.ts"]
    assert len(matches) == 1
