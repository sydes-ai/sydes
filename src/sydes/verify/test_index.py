"""Discovery of *existing* verification (tests) and mapping to affected behavior.

Sydes previously only generated test *suggestions*; nothing located the tests a
repository already has. This module indexes real test files, extracts the case
names and the routes/symbols each case touches, and maps them onto affected
flows.

Sydes does not execute tests. A `verified` status here means "existing
verification was located with direct evidence", never "this passed".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

from sydes.core.models import EvidenceRef
from sydes.verify.models import (
    VERIFICATION_UNKNOWN,
    VERIFICATION_UNVERIFIED,
    VERIFICATION_VERIFIED,
    AffectedFlow,
    VerificationItem,
)
from sydes.verify.repo_scan import RepoScan, ScannedFile
from sydes.verify.symbol_index import SymbolIndex

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


@dataclass(slots=True)
class LocatedTest:
    """One located test case with the behavior signals it references."""

    repo: str
    file: str
    name: str
    line: int
    suite: str | None = None
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


def _extract_cases_from_file(scanned: ScannedFile) -> list[LocatedTest]:
    """Extract test cases and their referenced routes/identifiers from one file."""
    lines = scanned.text.splitlines()
    starts: list[tuple[int, str, str | None]] = []
    current_suite: str | None = None

    for line_no, line in enumerate(lines, start=1):
        suite_match = _JS_SUITE.match(line)
        if suite_match:
            current_suite = suite_match.group("name")
            continue
        for pattern in (_PY_TEST_DEF, _JS_TEST_CASE, _JAVA_TEST):
            match = pattern.match(line)
            if match:
                starts.append((line_no, match.group("name"), current_suite))
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
                snippet="",
            )
        )
    return cases


def build_test_index(scan: RepoScan) -> ExistingTestIndex:
    """Index every test file in a repository."""
    index = ExistingTestIndex(repo=scan.repo)
    for scanned in scan.test_files():
        index.files.append(scanned.path)
        index.cases.extend(_extract_cases_from_file(scanned))
    index.notes.append(f"test_files_found={len(index.files)}")
    index.notes.append(f"test_cases_found={len(index.cases)}")
    return index


def _normalize_route(path: str | None) -> str:
    """Normalize a route path for comparison across param styles."""
    if not path:
        return ""
    value = path.strip()
    if not value.startswith("/"):
        value = "/" + value
    value = re.sub(r"\{[^}]*\}", "{}", value)
    value = re.sub(r":[A-Za-z_]\w*", "{}", value)
    value = re.sub(r"<[^>]*>", "{}", value)
    value = re.sub(r"/+", "/", value)
    if len(value) > 1 and value.endswith("/"):
        value = value[:-1]
    return value.lower()


def _route_matches(test_paths: set[str], route_path: str | None) -> str | None:
    """Return the matching literal when a test exercises a route path."""
    target = _normalize_route(route_path)
    if not target or target == "/":
        return None
    for literal in test_paths:
        normalized = _normalize_route(literal)
        if normalized == target:
            return literal
        if target.count("{}") and normalized.count("/") == target.count("/"):
            pattern = "^" + re.escape(target).replace(r"\{\}", r"[^/]+") + "$"
            if re.match(pattern, normalized):
                return literal
    return None


def map_existing_verification(
    *,
    flows: list[AffectedFlow],
    test_index: ExistingTestIndex,
    symbol_index: SymbolIndex,
    changed_symbol_names: set[str],
    changed_files: set[str],
) -> list[VerificationItem]:
    """Map located tests onto affected flows, emitting evidence-backed statuses."""
    items: dict[str, VerificationItem] = {}
    covered_flow_ids: set[str] = set()

    flow_symbol_names: dict[str, set[str]] = {}
    flow_routes: dict[str, list[tuple[str | None, str | None]]] = {}
    for flow in flows:
        names: set[str] = set()
        routes: list[tuple[str | None, str | None]] = []
        for node in flow.nodes:
            if node.symbol:
                names.add(node.symbol.rsplit(".", 1)[-1])
                names.add(node.symbol)
            if node.kind == "route":
                routes.append((node.method, node.path))
        flow_symbol_names[flow.id] = names
        flow_routes[flow.id] = routes

    for case in test_index.cases:
        for flow in flows:
            reasons: list[str] = []
            evidence: list[EvidenceRef] = []

            for method, path in flow_routes.get(flow.id, []):
                literal = _route_matches(case.route_paths, path)
                if literal is None:
                    continue
                if method and method != "ANY" and case.methods and method not in case.methods:
                    continue
                reasons.append(f"requests `{literal}`")
                evidence.append(
                    EvidenceRef(
                        file=case.file,
                        symbol=case.display_name,
                        label="test_requests_route",
                        snippet=literal,
                    )
                )
                break

            direct_symbols = (flow_symbol_names.get(flow.id, set()) & case.identifiers) & (
                changed_symbol_names or flow_symbol_names.get(flow.id, set())
            )
            changed_hits = changed_symbol_names & case.identifiers
            if changed_hits:
                reasons.append("references changed symbol " + ", ".join(sorted(changed_hits)[:3]))
                evidence.append(
                    EvidenceRef(
                        file=case.file,
                        symbol=case.display_name,
                        label="test_references_changed_symbol",
                        snippet=", ".join(sorted(changed_hits)[:5]),
                    )
                )
            elif direct_symbols:
                reasons.append("references " + ", ".join(sorted(direct_symbols)[:3]))
                evidence.append(
                    EvidenceRef(
                        file=case.file,
                        symbol=case.display_name,
                        label="test_references_flow_symbol",
                        snippet=", ".join(sorted(direct_symbols)[:5]),
                    )
                )

            if not reasons:
                continue

            covered_flow_ids.add(flow.id)
            item_id = f"test:{case.file}:{case.name}"
            existing = items.get(item_id)
            if existing is None:
                items[item_id] = VerificationItem(
                    id=item_id,
                    name=case.display_name,
                    kind="test",
                    repo=case.repo,
                    file=case.file,
                    line=case.line,
                    status=VERIFICATION_VERIFIED,
                    covers=[_humanize(case.name)],
                    related_flow_ids=[flow.id],
                    related_symbols=sorted(changed_hits)[:5],
                    changed_in_diff=case.file in changed_files,
                    evidence=evidence,
                )
            else:
                if flow.id not in existing.related_flow_ids:
                    existing.related_flow_ids.append(flow.id)
                existing.evidence.extend(
                    item for item in evidence if item not in existing.evidence
                )

    for flow in flows:
        if flow.id in covered_flow_ids:
            continue
        items[f"unverified:{flow.id}"] = VerificationItem(
            id=f"unverified:{flow.id}",
            name=flow.entry_label,
            kind="flow",
            repo=flow.repo,
            status=VERIFICATION_UNVERIFIED,
            covers=[],
            related_flow_ids=[flow.id],
            evidence=[],
        )

    if not test_index.files:
        items["unknown:no-tests"] = VerificationItem(
            id="unknown:no-tests",
            name="No test files located in this repository",
            kind="repository",
            repo=test_index.repo,
            status=VERIFICATION_UNKNOWN,
        )

    order = {
        VERIFICATION_VERIFIED: 0,
        "failed": 1,
        VERIFICATION_UNVERIFIED: 2,
        VERIFICATION_UNKNOWN: 3,
    }
    return sorted(items.values(), key=lambda item: (order.get(item.status, 9), item.name))
