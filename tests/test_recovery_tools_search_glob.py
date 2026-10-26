"""Regression test for silent false negatives in `RepoTools.search_text`:
`pathlib.Path.glob` supports neither `{a,b}` braces nor `|` alternatives, so
a model-written `path_glob` such as `**/*.{ts,tsx}` matched no files and the
tool answered "(no matches)" -- indistinguishable from a real absence. Found
in an AACR Investigator run, where 9 of 11 empty searches had real matches."""

from __future__ import annotations

from pathlib import Path

import pytest

from sydes.recovery.tools import RepoTools, expand_glob


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "web" / "src").mkdir(parents=True)
    (tmp_path / "web" / "src" / "worker.ts").write_text("caches.open('v1')\n", encoding="utf-8")
    (tmp_path / "web" / "src" / "view.svelte").write_text("<img src={thumbnailUrl}>\n", encoding="utf-8")
    (tmp_path / "web" / "src" / "notes.md").write_text("thumbnailUrl caches.open\n", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}\n', encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("glob, expected", [
    ("src/**/*.py", ["src/**/*.py"]),
    ("**/*.{ts,svelte}", ["**/*.ts", "**/*.svelte"]),
    ("{web,app}/**/*.{ts,js}", ["web/**/*.ts", "web/**/*.js", "app/**/*.ts", "app/**/*.js"]),
    ("**/*.{ts,{svelte,js}}", ["**/*.ts", "**/*.svelte", "**/*.js"]),
    ("package.json|scripts/**/*", ["package.json", "scripts/**/*"]),
    ("{a,b}/x|{a,c}/x", ["a/x", "b/x", "c/x"]),
    ("**/*.{ts", ["**/*.{ts"]),  # unbalanced: literal
    ("**/{x}.ts", ["**/{x}.ts"]),  # one option: not an alternative
])
def test_expand_glob(glob: str, expected: list[str]) -> None:
    assert expand_glob(glob) == expected


def test_a_brace_glob_searches_every_alternative(repo: Path) -> None:
    out = RepoTools(repo).search_text(r"caches\.open|thumbnailUrl", path_glob="web/src/**/*.{ts,svelte}")
    assert out.splitlines() == ["web/src/view.svelte:1: <img src={thumbnailUrl}>", "web/src/worker.ts:1: caches.open('v1')"]


def test_pipe_alternatives_are_searched_and_files_are_not_duplicated(repo: Path) -> None:
    out = RepoTools(repo).search_text("jest|caches", path_glob="package.json|web/**/*.ts|**/*.ts")
    assert out.splitlines() == ['package.json:1: {"scripts": {"test": "jest"}}', "web/src/worker.ts:1: caches.open('v1')"]


def test_a_glob_matching_no_files_is_distinguishable_from_no_matches(repo: Path) -> None:
    tools = RepoTools(repo)
    assert tools.search_text("caches", path_glob="server/**/*.go") == (
        "(no files match path_glob 'server/**/*.go'; nothing was searched)")
    assert tools.search_text("no-such-text", path_glob="web/**/*.ts") == "(no matches)"
    assert tools.search_text("no-such-text") == "(no matches)"
