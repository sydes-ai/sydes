"""Discovery of *existing* verification (tests) and mapping to affected behavior.

Sydes previously only generated test *suggestions*; nothing located the tests a
repository already has. This module indexes real test files, extracts the case
names and the routes/symbols each case touches, and maps them onto affected
flows.

This module only *locates* tests. Deciding which obligation a test can verify
lives in `test_mapping`, and deciding whether it passes lives in
`test_execution`; nothing here ever assigns a verification state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

from sydes.verify.source_files import RepoFiles, SourceFile

_PY_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(?P<name>test_\w+)\s*\(")
_PY_TEST_CLASS = re.compile(r"^\s*class\s+(?P<name>Test\w+)\s*[\(:]")
_JS_TEST_CASE = re.compile(r"^\s*(?:it|test)\s*(?:\.\w+)?\s*\(\s*[`'\"](?P<name>[^`'\"]+)[`'\"]")
_JS_SUITE = re.compile(r"^\s*describe\s*(?:\.\w+)?\s*\(\s*[`'\"](?P<name>[^`'\"]+)[`'\"]")
_JAVA_TEST = re.compile(r"^\s*(?:public\s+)?void\s+(?P<name>\w*[Tt]est\w*)\s*\(")

_ROUTE_LITERAL = re.compile(r"[`'\"](?P<path>/[A-Za-z0-9_\-{}:./$]*)[`'\"]")
_HTTP_VERB = re.compile(
    r"\.\s*(?P<verb>get|post|put|patch|delete|head|options)\s*\(", re.IGNORECASE
)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")

_MAX_CASE_LINES = 120

# `conftest.py` holds fixtures, not test cases, and a fixture may legitimately be
# named `test_*`. Treating either as a runnable case yields a target the runner
# cannot collect, which would wrongly read as an execution blocker.
_FIXTURE_DECORATOR_RE = re.compile(r"^\s*@(?:pytest\.)?fixture\b")
_NON_CASE_BASENAMES = {"conftest.py"}


@dataclass(slots=True)
class LocatedTest:
    """One located test case with the behavior signals it references."""

    repo: str
    file: str
    name: str
    line: int
    suite: str | None = None
    body: str = ""
    route_paths: set[str] = field(default_factory=set)
    methods: set[str] = field(default_factory=set)
    identifiers: set[str] = field(default_factory=set)
    snippet: str = ""

    @property
    def display_name(self) -> str:
        """Readable test identity for output."""
        if self.suite:
            return f"{self.suite} :: {self.name}"
        return self.name


@dataclass(slots=True)
class ExistingTestIndex:
    """All located test cases for a repository, plus per-file import context."""

    repo: str
    cases: list[LocatedTest] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _humanize(name: str) -> str:
    """Turn a test identifier into a readable covered-behavior phrase."""
    text = name.strip()
    text = re.sub(r"^(?:test_|test|it_|should_)", "", text, flags=re.IGNORECASE)
    text = text.replace("_", " ").strip()
    text = re.sub(r"(?<!^)(?=[A-Z][a-z])", " ", text).strip()
    return text[:1].upper() + text[1:] if text else name


def _extract_cases_from_file(scanned: SourceFile) -> list[LocatedTest]:
    """Extract test cases and their referenced routes/identifiers from one file."""
    lines = scanned.text.splitlines()
    starts: list[tuple[int, str, str | None]] = []
    current_suite: str | None = None
    pending_fixture = False

    for line_no, line in enumerate(lines, start=1):
        if _FIXTURE_DECORATOR_RE.match(line):
            pending_fixture = True
            continue
        suite_match = _JS_SUITE.match(line)
        if suite_match:
            current_suite = suite_match.group("name")
            continue
        for pattern in (_PY_TEST_DEF, _JS_TEST_CASE, _JAVA_TEST):
            match = pattern.match(line)
            if match:
                if not pending_fixture:
                    starts.append((line_no, match.group("name"), current_suite))
                pending_fixture = False
                break
        else:
            class_match = _PY_TEST_CLASS.match(line)
            if class_match:
                current_suite = class_match.group("name")

    cases: list[LocatedTest] = []
    for position, (line_no, name, suite) in enumerate(starts):
        end = starts[position + 1][0] - 1 if position + 1 < len(starts) else len(lines)
        end = min(end, line_no + _MAX_CASE_LINES)
        body = "\n".join(lines[line_no - 1 : end])
        case = LocatedTest(
            repo=scanned.repo,
            file=scanned.path,
            name=name,
            line=line_no,
            suite=suite,
            route_paths={
                match.group("path")
                for match in _ROUTE_LITERAL.finditer(body)
                if len(match.group("path")) > 1
            },
            methods={match.group("verb").upper() for match in _HTTP_VERB.finditer(body)},
            identifiers=set(_IDENTIFIER.findall(body)),
            body=body,
            snippet=lines[line_no - 1].strip()[:220],
        )
        cases.append(case)

    if not cases and scanned.is_test:
        # A test file with no recognizable case names still counts as evidence.
        cases.append(
            LocatedTest(
                repo=scanned.repo,
                file=scanned.path,
                name=Path(scanned.path).stem,
                line=1,
                route_paths={
                    match.group("path")
                    for match in _ROUTE_LITERAL.finditer(scanned.text)
                    if len(match.group("path")) > 1
                },
                methods={match.group("verb").upper() for match in _HTTP_VERB.finditer(scanned.text)},
                identifiers=set(_IDENTIFIER.findall(scanned.text)),
                body=scanned.text,
                snippet="",
            )
        )
    return cases


def build_test_index(files: RepoFiles) -> ExistingTestIndex:
    """Index every test file in a repository."""
    index = ExistingTestIndex(repo=files.repo)
    for scanned in files.tests():
        if Path(scanned.path).name in _NON_CASE_BASENAMES:
            continue
        index.files.append(scanned.path)
        index.cases.extend(_extract_cases_from_file(scanned))
    index.notes.append(f"test_files_found={len(index.files)}")
    index.notes.append(f"test_cases_found={len(index.cases)}")
    return index
