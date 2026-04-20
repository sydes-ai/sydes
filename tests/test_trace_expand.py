"""Tests for bounded contextual file selection for flow expansion."""

from pathlib import Path

from sydes.core.models import EndpointCandidate, EvidenceRef, RepoRef
from sydes.trace.expand import prepare_flow_expansion_context


def test_prepare_flow_expansion_context_selects_anchor_and_related_files(tmp_path: Path) -> None:
    """Expansion context should include anchor file plus bounded nearby candidates."""
    repo_root = tmp_path / "api"
    (repo_root / "src").mkdir(parents=True)
    (repo_root / "src" / "routes.py").write_text(
        "from user_service import create_user\nrouter.post('/users', create_user)\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "user_service.py").write_text(
        "def create_user(payload):\n    return payload\n",
        encoding="utf-8",
    )
    (repo_root / "src" / "db_client.py").write_text("def write_user(payload):\n    pass\n", encoding="utf-8")
    (repo_root / "src" / "models.py").write_text("class User: ...\n", encoding="utf-8")
    (repo_root / "scripts").mkdir()
    (repo_root / "scripts" / "tool.py").write_text("print('x')\n", encoding="utf-8")

    endpoint = EndpointCandidate(
        method="POST",
        path="/users",
        handler="create_user",
        file="src/routes.py",
        repo="api",
        evidence=[EvidenceRef(file="src/routes.py", symbol="create_user", label="route")],
    )
    repos = [RepoRef(name="api", root=str(repo_root))]

    context = prepare_flow_expansion_context(endpoint, repos, max_related_files=3)

    assert context.anchor_file == "src/routes.py"
    assert context.files
    assert context.files[0].file == "src/routes.py"
    assert context.files[0].selection_reasons == ["anchor_endpoint_file"]
    selected_files = [item.file for item in context.files]
    assert "src/user_service.py" in selected_files
    assert len(context.files) <= 4
    non_anchor_reasons = [reason for item in context.files[1:] for reason in item.selection_reasons]
    assert any(reason in {"same_directory", "related_filename_keyword", "name_matches_symbol"} for reason in non_anchor_reasons)


def test_prepare_flow_expansion_context_handles_missing_repo_root() -> None:
    """Missing repo mapping should return empty file list with an actionable note."""
    endpoint = EndpointCandidate(file="src/routes.py", repo="api")

    context = prepare_flow_expansion_context(endpoint, repos=[])

    assert context.files == []
    assert context.notes
    assert "Repo root" in context.notes[0]


def test_prepare_flow_expansion_context_preserves_truncation_metadata(tmp_path: Path) -> None:
    """Context entries should surface reader truncation metadata for large files."""
    repo_root = tmp_path / "api"
    (repo_root / "src").mkdir(parents=True)
    (repo_root / "src" / "routes.py").write_text(
        "\n".join(f"line {index}" for index in range(500)),
        encoding="utf-8",
    )

    endpoint = EndpointCandidate(
        method="GET",
        path="/status",
        file="src/routes.py",
        repo="api",
    )
    repos = [RepoRef(name="api", root=str(repo_root))]

    context = prepare_flow_expansion_context(endpoint, repos, max_related_files=0)

    assert len(context.files) == 1
    anchor_entry = context.files[0]
    assert anchor_entry.truncated is True
    assert any("truncated" in note.lower() for note in context.notes)
