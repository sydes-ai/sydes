"""Soft data models for graph-backed API tracing output in Sydes V1."""

from pydantic import BaseModel, Field

# Common V1 labels for low-friction interoperability across modules.
TARGET_KIND_API_ROUTE = "api_route"
TEST_KIND_INTEGRATION = "integration"
STATUS_INFERRED = "inferred"
STATUS_CONFIRMED = "confirmed"
STATUS_UNKNOWN = "unknown"


class EvidenceRef(BaseModel):
    """Source reference supporting a node, edge, or inferred conclusion."""

    file: str
    symbol: str | None = None
    label: str | None = None


class RepoRef(BaseModel):
    """Repository identity and local root path."""

    name: str
    root: str


class InventoryFile(BaseModel):
    """Single shallow file inventory item."""

    path: str
    size_bytes: int | None = None


class RepoInventory(BaseModel):
    """Shallow file inventory for a repository root."""

    repo: str
    root: str
    files: list[InventoryFile] = Field(default_factory=list)
    file_count: int = 0
    total_size_bytes: int | None = None


class RepoSenseSummary(BaseModel):
    """Heuristic, shallow repo-level sensing summary."""

    repo: str
    root: str
    top_level_files: list[str] = Field(default_factory=list)
    top_level_dirs: list[str] = Field(default_factory=list)
    manifests: list[str] = Field(default_factory=list)
    dominant_extensions: dict[str, int] = Field(default_factory=dict)
    likely_language_families: list[str] = Field(default_factory=list)
    backend_signals: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class TargetSpec(BaseModel):
    """Requested target for trace entrypoint discovery."""

    kind: str = TARGET_KIND_API_ROUTE
    method: str | None = None
    path: str


class GraphNode(BaseModel):
    """Node in the trace graph representing code, route, or external boundary."""

    id: str
    type: str
    name: str
    service: str | None = None
    repo: str | None = None
    file: str | None = None
    symbol: str | None = None
    method: str | None = None
    path: str | None = None
    metadata: dict = Field(default_factory=dict)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    confidence: float | None = None
    status: str | None = None


class GraphEdge(BaseModel):
    """Directed or inferred relationship between two graph nodes."""

    id: str
    source: str
    target: str
    type: str
    direction: str | None = None
    service: str | None = None
    repo: str | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)
    confidence: float | None = None
    status: str | None = None


class FlowStep(BaseModel):
    """Single flow step tying a step kind to a graph node identifier."""

    node_id: str
    kind: str


class Flow(BaseModel):
    """Named sequence of graph steps describing one candidate request flow."""

    id: str
    name: str
    entry_node: str
    steps: list[FlowStep] = Field(default_factory=list)
    summary: str | None = None
    confidence: float | None = None


class GeneratedTest(BaseModel):
    """Candidate generated test associated with a selected flow."""

    name: str
    flow_id: str
    kind: str = TEST_KIND_INTEGRATION
    covers: list[str] = Field(default_factory=list)
    reason: str | None = None


class Unknown(BaseModel):
    """Unresolved or ambiguous element captured during tracing."""

    id: str
    kind: str
    service: str | None = None
    repo: str | None = None
    file: str | None = None
    symbol: str | None = None
    description: str
    confidence: float | None = None


class EndpointCandidate(BaseModel):
    """Candidate API endpoint discovered from repository code."""

    method: str | None = None
    path: str
    repo: str | None = None
    file: str | None = None
    symbol: str | None = None
    confidence: float | None = None


class RoutesResult(BaseModel):
    """V1 placeholder contract for route discovery output."""

    version: str = "v1"
    repos: list[RepoRef] = Field(default_factory=list)
    routes: list[EndpointCandidate] = Field(default_factory=list)


class TraceSummary(BaseModel):
    """Top-level summary for the best-known traced flow."""

    key_flow_id: str | None = None
    confidence: float | None = None


class TraceResult(BaseModel):
    """V1 graph-backed trace result contract."""

    version: str = "v1"
    target: TargetSpec
    repos: list[RepoRef] = Field(default_factory=list)
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    flows: list[Flow] = Field(default_factory=list)
    tests: list[GeneratedTest] = Field(default_factory=list)
    unknowns: list[Unknown] = Field(default_factory=list)
    summary: TraceSummary
