"""Tests for bounded selective file-reading helpers."""

from pathlib import Path

from sydes.core.models import RankedFileCandidate
from sydes.ingest.readers import read_ranked_candidate_files, read_text_file_safely


def test_read_text_file_safely_returns_snippet_metadata(tmp_path: Path) -> None:
    """Reader should return text plus basic line/char metadata."""
    repo_root = tmp_path / "api"
    repo_root.mkdir()
    file_path = repo_root / "src" / "routes.py"
    file_path.parent.mkdir(parents=True)
    file_path.write_text("line1\nline2\n", encoding="utf-8")

    result = read_text_file_safely("api", repo_root, "src/routes.py")

    assert not result.skipped
    assert result.snippet is not None
    assert result.snippet.line_count == 2
    assert result.snippet.char_count >= 10


def test_read_text_file_safely_truncates_by_line_cap(tmp_path: Path) -> None:
    """Reader should mark truncated output when line cap is exceeded."""
    repo_root = tmp_path / "api"
    repo_root.mkdir()
    file_path = repo_root / "server.py"
    file_path.write_text("a\nb\nc\nd\n", encoding="utf-8")

    result = read_text_file_safely(
        "api",
        repo_root,
        "server.py",
        max_read_lines=2,
        max_read_chars=10_000,
        max_read_bytes=10_000,
    )

    assert not result.skipped
    assert result.snippet is not None
    assert result.snippet.truncated
    assert result.snippet.line_count == 2


def test_read_text_file_safely_skips_binary_and_huge_files(tmp_path: Path) -> None:
    """Reader should gracefully skip binary-like and oversized files."""
    repo_root = tmp_path / "api"
    repo_root.mkdir()

    binary_path = repo_root / "asset.bin"
    binary_path.write_bytes(b"\x00\x01\x02")
    binary_result = read_text_file_safely("api", repo_root, "asset.bin")
    assert binary_result.skipped
    assert binary_result.skip_reason == "binary_file"

    huge_path = repo_root / "notes.txt"
    huge_path.write_text("x" * 500, encoding="utf-8")
    huge_result = read_text_file_safely(
        "api",
        repo_root,
        "notes.txt",
        max_file_size_bytes=100,
    )
    assert huge_result.skipped
    assert huge_result.skip_reason == "file_too_large"


def test_read_ranked_candidate_files_reads_top_n_in_order(tmp_path: Path) -> None:
    """Batch reader should read only top-N ranked candidates in input order."""
    repo_root = tmp_path / "api"
    repo_root.mkdir()
    (repo_root / "a.py").write_text("print('a')\n", encoding="utf-8")
    (repo_root / "b.py").write_text("print('b')\n", encoding="utf-8")
    (repo_root / "c.py").write_text("print('c')\n", encoding="utf-8")

    ranked = [
        RankedFileCandidate(file="a.py", score=10.0, repo="api"),
        RankedFileCandidate(file="b.py", score=9.0, repo="api"),
        RankedFileCandidate(file="c.py", score=8.0, repo="api"),
    ]

    results = read_ranked_candidate_files("api", repo_root, ranked, top_n=2)

    assert len(results) == 2
    assert results[0].relative_path == "a.py"
    assert results[1].relative_path == "b.py"
    assert all(not item.skipped for item in results)

