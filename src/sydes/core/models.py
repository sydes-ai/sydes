"""Soft data models for graph-backed API tracing output in Sydes V1."""

from typing import Any

from pydantic import BaseModel, Field

# Common V1 labels for low-friction interoperability across modules.
TARGET_KIND_API_ROUTE = "api_route"
TEST_KIND_INTEGRATION = "integration"
TEST_MATRIX_CATEGORY_HAPPY_PATH = "happy_path"
TEST_MATRIX_CATEGORY_VALIDATION = "validation"
TEST_MATRIX_CATEGORY_SIDE_EFFECTS = "side_effects"
TEST_MATRIX_CATEGORY_STATE_CONSISTENCY = "state_consistency"
TEST_MATRIX_CATEGORY_EDGE_CASES = "edge_cases"
STATUS_INFERRED = "inferred"
STATUS_CONFIRMED = "confirmed"
STATUS_UNKNOWN = "unknown"
SINK_KIND_DATABASE = "database"
SINK_KIND_EXTERNAL_API = "external_api"
SINK_KIND_QUEUE = "queue"
SINK_KIND_FILE_SINK = "file_sink"
SINK_ACTION_READ = "read"
SINK_ACTION_WRITE = "write"
SINK_ACTION_PUBLISH = "publish"
SINK_ACTION_CONSUME = "consume"


class EvidenceRef(BaseModel):
    """Source reference supporting a node, edge, or inferred conclusion."""

    file: str
    symbol: str | None = None
    label: str | None = None
    snippet: str | None = None


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


class RankedFileCandidate(BaseModel):
    """Ranked file candidate for downstream endpoint discovery exploration."""

    file: str
    score: float
    reasons: list[str] = Field(default_factory=list)
    role: str | None = None
    repo: str | None = None
    service: str | None = None


class ReadFileSnippet(BaseModel):
    """Bounded text snippet loaded from a repository file."""

    repo: str
    relative_path: str
    truncated: bool = False
    text: str = ""
    line_count: int = 0
    char_count: int = 0


class CandidateFileRead(BaseModel):
    """Read attempt result for a ranked candidate file."""

    repo: str
    relative_path: str
    role: str | None = None
    snippet: ReadFileSnippet | None = None
    skipped: bool = False
    skip_reason: str | None = None


class ExpansionContextFile(BaseModel):
    """Selected file for flow expansion context with grounding metadata."""

    repo: str
    file: str
    selection_reasons: list[str] = Field(default_factory=list)
    read: CandidateFileRead | None = None
    truncated: bool | None = None


class FlowExpansionContext(BaseModel):
    """Bounded contextual file set prepared for downstream flow expansion."""

    anchor_repo: str
    anchor_file: str
    files: list[ExpansionContextFile] = Field(default_factory=list)
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


class TraceStep(BaseModel):
    """Ordered step in the inferred execution path from an entry endpoint."""

    kind: str
    name: str
    repo: str | None = None
    service: str | None = None
    file: str | None = None
    symbol: str | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)
    confidence: float | None = None
    status: str | None = None


class SinkCandidate(BaseModel):
    """Side-effect target candidate using V1 sink taxonomy when mappable."""

    kind: str
    name: str
    repo: str | None = None
    service: str | None = None
    file: str | None = None
    symbol: str | None = None
    action: str | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)
    confidence: float | None = None
    status: str | None = None


class FlowExpansionResult(BaseModel):
    """Selective likely flow expansion output, not a full call graph."""

    entry_endpoint_id: str | None = None
    steps: list[TraceStep] = Field(default_factory=list)
    sinks: list[SinkCandidate] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    confidence: float | None = None


class Flow(BaseModel):
    """Named sequence of graph steps describing one candidate request flow."""

    id: str
    name: str
    entry_node: str
    steps: list[FlowStep] = Field(default_factory=list)
    summary: str | None = None
    confidence: float | None = None


class TestInputHint(BaseModel):
    """Soft hint for likely integration-test input values."""

    kind: str
    name: str | None = None
    value_hint: Any | None = None
    required: bool | None = None


class TestExpectation(BaseModel):
    """Expected observable behavior for a suggested integration test."""

    kind: str
    description: str
    target: str | None = None


class IntegrationTestSuggestion(BaseModel):
    """Structured integration-test suggestion, not a runnable test artifact."""

    name: str
    route: str
    method: str | None = None
    summary: str | None = None
    inputs: list[TestInputHint] = Field(default_factory=list)
    expectations: list[TestExpectation] = Field(default_factory=list)
    derived_from_flow_id: str | None = None
    confidence: float | None = None
    notes: list[str] = Field(default_factory=list)
    category: str | None = None
    priority: str | None = None
    purpose: str | None = None
    request: dict[str, Any] | None = None
    expected: dict[str, Any] | None = None
    side_effects: list[str] = Field(default_factory=list)
    related_steps: list[str] = Field(default_factory=list)
    related_sinks: list[str] = Field(default_factory=list)
    contract_refs: list[str] = Field(default_factory=list)
    requires_mocking: bool | None = None
    notes_text: str | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class TestMatrixGroup(BaseModel):
    """Category-grouped integration test suggestions for matrix-style planning."""

    category: str
    title: str | None = None
    tests: list[IntegrationTestSuggestion] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class TestMatrix(BaseModel):
    """Structured test matrix grouped by deterministic test categories."""

    groups: list[TestMatrixGroup] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    coverage: float | None = None
    confidence: float | None = None
    endpoint: dict[str, Any] | None = None


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
    """Soft candidate API endpoint discovered from repository code."""

    method: str | None = None
    path: str | None = None
    handler: str | None = None
    file: str
    repo: str
    service: str | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)
    confidence: float | None = None
    status: str | None = None


class CrossRepoCallCandidate(BaseModel):
    """Soft inferred cross-repo API call candidate from traced code context."""

    source_repo: str
    source_file: str | None = None
    source_symbol: str | None = None
    target_path: str | None = None
    target_method: str | None = None
    normalized_target_path: str | None = None
    normalized_target_method: str | None = None
    target_service_hint: str | None = None
    raw_call_text: str | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)
    confidence: float | None = None
    status: str | None = None


class CrossRepoLinkResult(BaseModel):
    """Soft cross-repo endpoint link result preserving uncertainty and notes."""

    source_endpoint_id: str | None = None
    matched_target_endpoint_id: str | None = None
    link_type: str | None = None
    normalized_target_path: str | None = None
    normalized_target_method: str | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)
    confidence: float | None = None
    notes: list[str] = Field(default_factory=list)


class EndpointDiscoveryBatch(BaseModel):
    """LLM-facing discovery request payload over bounded candidate file reads."""

    candidates: list[CandidateFileRead] = Field(default_factory=list)
    target_hint: str | None = None
    method_hint: str | None = None


class EndpointDiscoveryResult(BaseModel):
    """LLM-facing discovery response payload with soft endpoint candidates."""

    endpoints: list[EndpointCandidate] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    confidence: float | None = None
    files_sent_to_llm: int = 0
    prompt_chars: int = 0
    timeout_seconds: float | None = None
    truncated_files: int = 0


class TargetMatchResult(BaseModel):
    """Target-to-endpoint resolution outcome for trace grounding."""

    selected: EndpointCandidate | None = None
    alternatives: list[EndpointCandidate] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    confidence: float | None = None


class ConfidenceSummary(BaseModel):
    """Soft confidence aggregate for endpoint discovery output."""

    average: float | None = None
    minimum: float | None = None
    maximum: float | None = None


class RoutesResult(BaseModel):
    """V1 placeholder contract for route discovery output."""

    version: str = "v1"
    repos: list[RepoRef] = Field(default_factory=list)
    routes: list[EndpointCandidate] = Field(default_factory=list)
    candidate_files: int = 0
    files_examined: int = 0
    files_sent_to_llm: int = 0
    prompt_chars: int = 0
    timeout_seconds: float | None = None
    truncated_files: int = 0
    notes: list[str] = Field(default_factory=list)
    confidence_summary: ConfidenceSummary | None = None


class ApiContractEvidence(BaseModel):
    """Grounding metadata for API contract fields."""

    kind: str
    file: str | None = None
    symbol: str | None = None
    line: int | None = None
    source: str | None = None
    confidence: str | None = None
    notes: list[str] = Field(default_factory=list)


class ApiSchemaProperty(BaseModel):
    """Single contract schema property."""

    type: str | None = None
    format: str | None = None
    required: bool | None = None
    description: str | None = None
    example: Any | None = None
    enum: list[Any] | None = None
    nullable: bool | None = None


class ApiSchema(BaseModel):
    """Request/response schema shape."""

    type: str
    required: list[str] = Field(default_factory=list)
    properties: dict[str, ApiSchemaProperty] = Field(default_factory=dict)
    items: "ApiSchema | None" = None
    description: str | None = None
    example: Any | None = None
    additional_properties: bool | None = None


class ApiRequestContract(BaseModel):
    """Request-side contract details for a route."""

    path_params: dict[str, ApiSchemaProperty] = Field(default_factory=dict)
    query_params: dict[str, ApiSchemaProperty] = Field(default_factory=dict)
    headers: dict[str, ApiSchemaProperty] = Field(default_factory=dict)
    body: ApiSchema | None = None
    examples: list[dict[str, Any]] = Field(default_factory=list)


class ApiResponseContract(BaseModel):
    """Response-side contract details for a route/status."""

    status: int | str
    description: str | None = None
    body: ApiSchema | None = None
    examples: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[ApiContractEvidence] = Field(default_factory=list)
    confidence: str | None = None


class ApiRouteContract(BaseModel):
    """Contract details for one API route candidate."""

    method: str | None = None
    path: str | None = None
    repo: str | None = None
    service: str | None = None
    handler: str | None = None
    file: str | None = None
    request: ApiRequestContract = Field(default_factory=ApiRequestContract)
    responses: dict[str, ApiResponseContract] = Field(default_factory=dict)
    evidence: list[ApiContractEvidence] = Field(default_factory=list)
    confidence: str | None = None
    notes: list[str] = Field(default_factory=list)


class ApiContractArtifact(BaseModel):
    """Top-level API contract artifact."""

    version: str = "v1"
    routes: list[ApiRouteContract] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    confidence: float | None = None


class EvidenceEndpoint(BaseModel):
    """Endpoint identity for graph-grounded evidence packets."""

    method: str
    path: str
    repo: str | None = None
    handler: str | None = None
    file: str | None = None


class EvidenceSourceWindow(BaseModel):
    """Bounded source excerpt used as grounded extraction context."""

    repo: str | None = None
    file: str
    symbol: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    code: str
    truncated: bool = False


class EvidenceTraceNode(BaseModel):
    """Compact trace node for evidence packet context."""

    id: str
    type: str | None = None
    name: str | None = None
    kind: str | None = None
    repo: str | None = None
    file: str | None = None
    symbol: str | None = None
    snippet: str | None = None
    confidence: float | None = None
    status: str | None = None


class EvidenceTraceEdge(BaseModel):
    """Compact trace edge for evidence packet context."""

    id: str | None = None
    source: str
    target: str
    type: str | None = None
    snippet: str | None = None
    confidence: float | None = None


class EvidenceSink(BaseModel):
    """Compact side-effect or boundary candidate for evidence packet context."""

    name: str
    kind: str | None = None
    repo: str | None = None
    file: str | None = None
    symbol: str | None = None
    snippet: str | None = None
    confidence: float | None = None


class EvidencePacketLimits(BaseModel):
    """Limits applied while building an evidence packet."""

    max_source_chars: int = 8000
    max_nodes: int = 40
    max_edges: int = 60
    max_test_scenarios: int = 12


class EvidencePacket(BaseModel):
    """Graph-grounded compact evidence packet for later contract/test extraction."""

    version: str = "v1"
    endpoint: EvidenceEndpoint
    source_windows: list[EvidenceSourceWindow] = Field(default_factory=list)
    trace_nodes: list[EvidenceTraceNode] = Field(default_factory=list)
    trace_edges: list[EvidenceTraceEdge] = Field(default_factory=list)
    sinks: list[EvidenceSink] = Field(default_factory=list)
    current_contract: dict[str, Any] | None = None
    current_test_matrix_summary: dict[str, Any] | None = None
    notes: list[str] = Field(default_factory=list)
    limits: EvidencePacketLimits | None = None


class TraceSummary(BaseModel):
    """Top-level summary for the best-known traced flow."""

    key_flow_id: str | None = None
    trace_confidence: float | None = None
    test_matrix_coverage: float | None = None
    test_matrix_confidence: float | None = None
    confidence: float | None = None
    text: str | None = None


class TraceResult(BaseModel):
    """V1 graph-backed trace result contract."""

    version: str = "v1"
    target: TargetSpec
    repos: list[RepoRef] = Field(default_factory=list)
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    flows: list[Flow] = Field(default_factory=list)
    tests: list[IntegrationTestSuggestion] = Field(default_factory=list)
    test_matrix: TestMatrix | None = None
    unknowns: list[Unknown] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    summary: TraceSummary
    matched_endpoint: EndpointCandidate | None = None
    flow: dict[str, Any] | None = None
    layers: list[dict[str, Any]] = Field(default_factory=list)
    sinks: list[dict[str, Any]] = Field(default_factory=list)
    resolved_handlers: list[dict[str, Any]] = Field(default_factory=list)
    budgets: dict[str, Any] | None = None
    diagnostics: list[str] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)
