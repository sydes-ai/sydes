"""Tests for heuristic candidate-file ranking."""

from sydes.core.models import InventoryFile, RepoInventory, RepoSenseSummary
from sydes.ingest.ranking import rank_candidate_files


def test_rank_candidate_files_prefers_api_and_bootstrap_paths() -> None:
    """Ranking should prioritize route/controller/bootstrap files."""
    inventory = RepoInventory(
        repo="api",
        root="/tmp/api",
        files=[
            InventoryFile(path="src/routes/orders.py"),
            InventoryFile(path="src/controllers/order_controller.py"),
            InventoryFile(path="README.md"),
            InventoryFile(path="main.py"),
        ],
        file_count=4,
    )
    sense = RepoSenseSummary(
        repo="api",
        root="/tmp/api",
        dominant_extensions={".py": 3, ".md": 1},
        likely_language_families=["python"],
        backend_signals=["api", "routes", "controllers", "main.py"],
    )

    ranked = rank_candidate_files(inventory, sense, top_k=4, min_score=-100.0)

    assert len(ranked) == 4
    top_files = [item.file for item in ranked[:3]]
    assert "README.md" not in top_files
    assert ranked[0].score >= ranked[-1].score


def test_rank_candidate_files_emits_reason_labels_and_limits_results() -> None:
    """Ranking output should include reason labels and respect top_k."""
    inventory = RepoInventory(
        repo="gateway",
        root="/tmp/gateway",
        files=[
            InventoryFile(path="src/api/router.ts"),
            InventoryFile(path="src/lib/http.ts"),
            InventoryFile(path="docs/notes.md"),
        ],
        file_count=3,
    )
    sense = RepoSenseSummary(
        repo="gateway",
        root="/tmp/gateway",
        dominant_extensions={".ts": 2},
        likely_language_families=["typescript"],
        backend_signals=["api", "router"],
    )

    ranked = rank_candidate_files(inventory, sense, top_k=2)

    assert len(ranked) == 2
    assert ranked[0].repo == "gateway"
    assert ranked[0].role is not None
    assert ranked[0].reasons
    assert any(reason.startswith("path:") for reason in ranked[0].reasons)


def test_rank_candidate_files_prioritizes_source_over_tests_and_docs() -> None:
    """Role-aware ranking should strongly downrank test/docs for route discovery."""
    inventory = RepoInventory(
        repo="flask-sample-app",
        root="/tmp/flask-sample-app",
        files=[
            InventoryFile(path="app/routes.py"),
            InventoryFile(path="tests/test_app.py"),
            InventoryFile(path="src/routes/users.ts"),
            InventoryFile(path="src/users.test.ts"),
            InventoryFile(path="main.py"),
            InventoryFile(path="README.md"),
        ],
        file_count=6,
    )
    sense = RepoSenseSummary(
        repo="flask-sample-app",
        root="/tmp/flask-sample-app",
        dominant_extensions={".py": 3, ".ts": 2, ".md": 1},
        likely_language_families=["python", "typescript"],
        backend_signals=["routes", "api", "app"],
    )

    ranked = rank_candidate_files(inventory, sense, top_k=6, min_score=-100.0)
    by_file = {item.file: item for item in ranked}
    assert by_file["app/routes.py"].score > by_file["tests/test_app.py"].score
    assert by_file["src/routes/users.ts"].score > by_file["src/users.test.ts"].score
    assert by_file["main.py"].score > by_file["README.md"].score

    top_files = [item.file for item in ranked[:3]]
    assert "README.md" not in top_files
    assert "tests/test_app.py" not in top_files


def test_rank_candidate_files_boosts_a_changed_file_with_a_neutral_name() -> None:
    """A file the diff actually touches should outrank an unrelated
    main/server/router file, even when its own path/filename carries none of
    the static route-signal keywords -- this is the diff-awareness fix, and
    it must not depend on any framework-specific vocabulary."""
    inventory = RepoInventory(
        repo="realworld-axum-sqlx",
        root="/tmp/realworld-axum-sqlx",
        files=[
            InventoryFile(path="src/http/articles/mod.rs"),
            InventoryFile(path="src/main.rs"),
        ],
        file_count=2,
    )
    sense = RepoSenseSummary(
        repo="realworld-axum-sqlx",
        root="/tmp/realworld-axum-sqlx",
        dominant_extensions={".rs": 2},
        likely_language_families=["rust"],
        backend_signals=[],
    )

    ranked = rank_candidate_files(
        inventory,
        sense,
        top_k=2,
        min_score=-100.0,
        changed_paths={"src/http/articles/mod.rs"},
    )

    assert ranked[0].file == "src/http/articles/mod.rs"
    assert "change:touched" in ranked[0].reasons
    assert ranked[0].score > ranked[1].score


def test_rank_candidate_files_gives_adjacent_files_a_smaller_boost_than_changed() -> None:
    """A structurally-adjacent file (importer/caller of a changed file) is a
    real but weaker signal than the diff itself, and must never outrank a
    directly-changed file."""
    inventory = RepoInventory(
        repo="api",
        root="/tmp/api",
        files=[
            InventoryFile(path="src/widgets/handler.py"),
            InventoryFile(path="src/widgets/router.py"),
        ],
        file_count=2,
    )
    sense = RepoSenseSummary(
        repo="api",
        root="/tmp/api",
        dominant_extensions={".py": 2},
        likely_language_families=["python"],
        backend_signals=[],
    )

    ranked = rank_candidate_files(
        inventory,
        sense,
        top_k=2,
        min_score=-100.0,
        changed_paths={"src/widgets/handler.py"},
        adjacent_paths={"src/widgets/router.py"},
    )
    by_file = {item.file: item for item in ranked}
    assert by_file["src/widgets/handler.py"].score > by_file["src/widgets/router.py"].score
    assert "change:adjacent" in by_file["src/widgets/router.py"].reasons


def test_rank_candidate_files_ordering_is_deterministic_with_a_tie() -> None:
    """Two files with identical final scores must sort in a stable,
    reproducible order (alphabetical by path), both with and without any
    change signal -- ranking must never depend on set/dict iteration order."""
    inventory = RepoInventory(
        repo="api",
        root="/tmp/api",
        files=[
            InventoryFile(path="src/http/articles/comments.rs"),
            InventoryFile(path="src/http/articles/listing.rs"),
            InventoryFile(path="src/http/articles/mod.rs"),
        ],
        file_count=3,
    )
    sense = RepoSenseSummary(
        repo="api",
        root="/tmp/api",
        dominant_extensions={".rs": 3},
        likely_language_families=["rust"],
        backend_signals=[],
    )

    first = rank_candidate_files(inventory, sense, top_k=3, min_score=-100.0)
    second = rank_candidate_files(inventory, sense, top_k=3, min_score=-100.0)
    assert [item.file for item in first] == [item.file for item in second]
    assert [item.file for item in first] == [
        "src/http/articles/comments.rs",
        "src/http/articles/listing.rs",
        "src/http/articles/mod.rs",
    ]


def test_rank_candidate_files_with_no_change_signal_matches_prior_behavior() -> None:
    """When no change/adjacency information is available (e.g. a standalone
    `sydes routes` scan with no diff), ranking must be identical to before
    this feature existed -- diff-awareness is additive, never a regression
    for change-agnostic callers."""
    inventory = RepoInventory(
        repo="api",
        root="/tmp/api",
        files=[
            InventoryFile(path="src/routes/orders.py"),
            InventoryFile(path="README.md"),
            InventoryFile(path="main.py"),
        ],
        file_count=3,
    )
    sense = RepoSenseSummary(
        repo="api",
        root="/tmp/api",
        dominant_extensions={".py": 2, ".md": 1},
        likely_language_families=["python"],
        backend_signals=["routes", "main.py"],
    )

    with_none = rank_candidate_files(inventory, sense, top_k=3, min_score=-100.0)
    with_empty = rank_candidate_files(
        inventory, sense, top_k=3, min_score=-100.0, changed_paths=set(), adjacent_paths=set()
    )
    assert [(item.file, item.score, item.reasons) for item in with_none] == [
        (item.file, item.score, item.reasons) for item in with_empty
    ]
