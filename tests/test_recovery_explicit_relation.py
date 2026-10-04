"""AI recovery reuses explicit relations and never contradicts them.

Shape (neutral names): a route handler assigns the result of calling a module-level helper
(`generator = make_chunks(...)`). The helper changed. A code-graph CALLS edge (or the literal
call in the handler's body) establishes handler -> helper deterministically; a model that
would answer "no relationship found" is never asked, and the verification pass cannot
downgrade the edge.
"""

from __future__ import annotations

import json
from pathlib import Path

from sydes.llm.client import LLMResponse
from sydes.recovery.agent import (
    RecoveryBudget,
    RecoveryRunStats,
    _prove_hop_with_recursion,
    explicit_call_edge,
)
from sydes.recovery.schema import (
    PROVENANCE_EXPLICIT_RELATION,
    STATUS_ESTABLISHED,
    EntityRef,
    RecoveredEdge,
    RecoveredEvidence,
    RecoveredPath,
)
from sydes.recovery.tools import RepoTools
from sydes.recovery.verify import verify_paths

ROUTES = '''\
async def make_chunks(service, request):
    async for chunk in service.stream(request):
        yield chunk


@router.post("/speak")
async def speak(request, client_request):
    service = await get_service()
    if request.stream:
        generator = make_chunks(service, request)

        async def single_output():
            try:
                async for chunk in generator:
                    yield chunk
            finally:
                await generator.aclose()

        return StreamingResponse(single_output())
    return Response(await service.render(request))


async def unrelated(request):
    return request
'''


class _NoRelationshipLLM:
    """A model that would deny every relationship -- recovery must not let it win."""

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        verdicts = [{"index": i, "accept": False, "reason": "no relationship found"} for i in range(5)]
        return LLMResponse(text=json.dumps({"verdicts": verdicts, "relationship": "no relationship found"}))


class _Graph:
    def __init__(self, line: int | None) -> None:
        self.line, self.calls = line, []

    def call_site(self, caller, caller_file, callee, callee_file):
        self.calls.append((caller, caller_file, callee, callee_file))
        return self.line


def _repo(tmp_path: Path, text: str = ROUTES) -> Path:
    (tmp_path / "app").mkdir(exist_ok=True)
    (tmp_path / "app" / "routes.py").write_text(text)
    return tmp_path


HANDLER = EntityRef(symbol="speak", file="app/routes.py")
HELPER = EntityRef(symbol="make_chunks", file="app/routes.py")


def test_an_explicit_calls_edge_wins_before_any_model_is_asked(tmp_path: Path) -> None:
    graph = _Graph(line=10)
    llm = _NoRelationshipLLM()
    stats = RecoveryRunStats()
    [edge] = _prove_hop_with_recursion(HANDLER, HELPER, "ctx", tools=RepoTools(_repo(tmp_path), graph=graph),
                                       client=llm, budget=RecoveryBudget(), stats=stats)
    assert llm.calls == 0 and stats.explicit_relation_edges == 1
    assert edge.provenance == PROVENANCE_EXPLICIT_RELATION and edge.status == STATUS_ESTABLISHED
    assert edge.evidence[0].line_start == 10 and "code graph CALLS edge" in edge.relationship


def test_without_a_graph_the_literal_call_in_the_callers_body_is_used(tmp_path: Path) -> None:
    edge = explicit_call_edge(HANDLER, HELPER, tools=RepoTools(_repo(tmp_path)))
    assert edge is not None and edge.evidence[0].line_start == 10
    assert "caller's own source" in edge.relationship


def test_no_call_means_no_edge(tmp_path: Path) -> None:
    other = EntityRef(symbol="unrelated", file="app/routes.py")
    assert explicit_call_edge(other, HELPER, tools=RepoTools(_repo(tmp_path), graph=_Graph(line=None))) is None


def test_verification_cannot_downgrade_an_explicit_relation(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    explicit = explicit_call_edge(HANDLER, HELPER, tools=RepoTools(repo))
    llm_proposed = RecoveredEdge(
        **{"from": HANDLER, "to": EntityRef(symbol="unrelated", file="app/routes.py")},
        relationship="speak may reach unrelated",
        evidence=[RecoveredEvidence(file="app/routes.py", line_start=24, line_end=25, fact="unrelated returns request")],
    )
    paths = [
        RecoveredPath(entrypoint="POST /speak", target_node="make_chunks", nodes=[HANDLER, HELPER], edges=[explicit]),
        RecoveredPath(entrypoint="POST /speak", target_node="unrelated",
                      nodes=[HANDLER, EntityRef(symbol="unrelated", file="app/routes.py")], edges=[llm_proposed]),
    ]
    llm = _NoRelationshipLLM()
    result = verify_paths(paths, changed_files=frozenset({"app/routes.py"}), tools=RepoTools(repo),
                          client=llm, stats=RecoveryRunStats())
    by_target = {p.target_node: p for p in result.paths}
    assert by_target["make_chunks"].status == STATUS_ESTABLISHED
    assert by_target["make_chunks"].edges[0].provenance == PROVENANCE_EXPLICIT_RELATION
    assert by_target["unrelated"].status != STATUS_ESTABLISHED  # model-proposed edges are still judged
    assert llm.calls == 1
