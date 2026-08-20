"""Serializable result model for `sydes verify-change`.

The model is the product; CLI/JSON/GitHub output are renderers over it. Keep
field names stable and renderer-neutral so GitHub Actions, PR comments, VS Code
and a future web UI can all consume the same artifact.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from sydes.core.models import EvidenceRef, RepoRef

# Change classification.
CHANGE_ADDED = "added"
CHANGE_MODIFIED = "modified"
CHANGE_DELETED = "deleted"
CHANGE_RENAMED = "renamed"

# Verification evidence states. Deliberately coarse: no invented confidence.
VERIFICATION_VERIFIED = "verified"
VERIFICATION_FAILED = "failed"
VERIFICATION_UNVERIFIED = "unverified"
VERIFICATION_UNKNOWN = "unknown"

RISK_LOW = "LOW"
RISK_MEDIUM = "MEDIUM"
RISK_HIGH = "HIGH"

VERDICT_OK = "OK"
VERDICT_REVIEW = "REVIEW RECOMMENDED"
VERDICT_ACTION_REQUIRED = "ACTION REQUIRED"

# Node kinds used in affected system flows.
NODE_ROUTE = "route"
NODE_HANDLER = "handler"
NODE_SERVICE = "service"
NODE_REPOSITORY = "repository"
NODE_CLIENT = "client"
NODE_DATABASE = "database"
NODE_EVENT = "event"
NODE_CONSUMER = "consumer"
NODE_EXTERNAL = "external"
NODE_FUNCTION = "function"


class SourceRef(BaseModel):
    """Location reference used by findings, flows, and evidence."""

    repo: str | None = None
    file: str | None = None
    symbol: str | None = None
    line: int | None = None


class Hunk(BaseModel):
    """Single changed line range in a file, from `git diff -U0`."""

    start_line: int
    end_line: int


class ChangedSymbol(BaseModel):
    """Symbol whose body overlaps a diff hunk."""

    id: str
    repo: str
    file: str
    name: str
    qualified_name: str | None = None
    kind: str = "function"
    language: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    change_type: str = CHANGE_MODIFIED
    changed_lines: int = 0
    decorators: list[str] = Field(default_factory=list)


class ChangedFile(BaseModel):
    """One file in the diff, with role classification and symbol overlap."""

    repo: str
    path: str
    old_path: str | None = None
    change_type: str = CHANGE_MODIFIED
    role: str | None = None
    added_lines: int = 0
    removed_lines: int = 0
    hunks: list[Hunk] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    binary: bool = False


class ChangeSet(BaseModel):
    """Resolved git change against a base revision."""

    base: str
    head: str | None = None
    merge_base: str | None = None
    repos: list[RepoRef] = Field(default_factory=list)
    includes_working_tree: bool = False
    files: list[ChangedFile] = Field(default_factory=list)
    symbols: list[ChangedSymbol] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class VerificationCounts(BaseModel):
    """Concrete counts backing the summary. No synthetic confidence scores."""

    changed_files: int = 0
    changed_source_files: int = 0
    changed_test_files: int = 0
    changed_symbols: int = 0
    affected_flows: int = 0
    code_findings: int = 0
    existing_verification: int = 0
    verification_gaps: int = 0
    runtime_dependencies: int = 0
    cross_repo_impacts: int = 0


class ChangeSummary(BaseModel):
    """Top-level verdict derived deterministically from counts and evidence."""

    risk: str = RISK_LOW
    verdict: str = VERDICT_OK
    headline: str | None = None
    counts: VerificationCounts = Field(default_factory=VerificationCounts)
    risk_reasons: list[str] = Field(default_factory=list)


class CodeFinding(BaseModel):
    """Code-level semantic finding about the diff itself."""

    id: str
    severity: Literal["P0", "P1", "P2", "P3"] = "P2"
    title: str
    file: str | None = None
    line: int | None = None
    repo: str | None = None
    explanation: str | None = None
    impact: str | None = None
    suggested_fix: str | None = None
    source: str = "llm"
    evidence: list[EvidenceRef] = Field(default_factory=list)


class FlowNode(BaseModel):
    """Node on an affected system flow path."""

    id: str
    kind: str
    name: str
    repo: str | None = None
    file: str | None = None
    symbol: str | None = None
    method: str | None = None
    path: str | None = None
    changed: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class FlowEdge(BaseModel):
    """Directed relation between two flow nodes, with why-we-believe-it evidence."""

    source: str
    target: str
    kind: str
    reason: str | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)


class AffectedFlow(BaseModel):
    """One backend behavior path touched by the change."""

    id: str
    entry_kind: str = NODE_ROUTE
    entry_label: str
    repo: str | None = None
    nodes: list[FlowNode] = Field(default_factory=list)
    edges: list[FlowEdge] = Field(default_factory=list)
    changed_node_ids: list[str] = Field(default_factory=list)
    reason: str | None = None
    notes: list[str] = Field(default_factory=list)


class VerificationItem(BaseModel):
    """Existing verification evidence mapped to affected behavior."""

    id: str
    name: str
    kind: str = "test"
    repo: str | None = None
    file: str | None = None
    line: int | None = None
    status: str = VERIFICATION_UNKNOWN
    covers: list[str] = Field(default_factory=list)
    related_flow_ids: list[str] = Field(default_factory=list)
    related_symbols: list[str] = Field(default_factory=list)
    changed_in_diff: bool = False
    evidence: list[EvidenceRef] = Field(default_factory=list)


class VerificationGap(BaseModel):
    """Externally meaningful behavior with no located verification evidence."""

    id: str
    behavior: str
    why: str | None = None
    related_node_ids: list[str] = Field(default_factory=list)
    related_flow_ids: list[str] = Field(default_factory=list)
    existing_evidence_found: bool = False
    status: str = VERIFICATION_UNVERIFIED
    source: str = "llm"
    evidence: list[EvidenceRef] = Field(default_factory=list)


class RuntimeDependency(BaseModel):
    """Dependency that must be running/reachable to exercise affected behavior.

    Sydes does not provision, mock, or contact any of these.
    """

    id: str
    name: str
    kind: str
    repo: str | None = None
    required_for_flow_ids: list[str] = Field(default_factory=list)
    detected_from: list[EvidenceRef] = Field(default_factory=list)
    scope: str = "repository"


class CrossRepoImpact(BaseModel):
    """Relationship crossing a repository/service boundary."""

    id: str
    target_repo: str | None = None
    target_label: str
    kind: str = "http_call"
    status: str = VERIFICATION_UNKNOWN
    reason: str | None = None
    related_flow_ids: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)


class ChangeVerificationResult(BaseModel):
    """Top-level `verify-change` artifact."""

    version: str = "v1"
    kind: str = "sydes_change_verification"
    generated_at: str | None = None
    change: ChangeSet
    summary: ChangeSummary = Field(default_factory=ChangeSummary)
    code_findings: list[CodeFinding] = Field(default_factory=list)
    affected_flows: list[AffectedFlow] = Field(default_factory=list)
    verification: list[VerificationItem] = Field(default_factory=list)
    verification_gaps: list[VerificationGap] = Field(default_factory=list)
    runtime_dependencies: list[RuntimeDependency] = Field(default_factory=list)
    cross_repo_impacts: list[CrossRepoImpact] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
