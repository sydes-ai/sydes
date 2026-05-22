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
