"""Targeted structural enrichment after ordinary traversal stalls (Track A).

Three shapes the CBM call graph cannot finish on its own, from real cases:
domain-driven-hexagon (a CQRS message handler nothing calls directly), spring-boot-demo
(a security filter the framework invokes after `addFilterBefore` registers it) and
Kokoro-FastAPI (a route assembled from a router decorator and an `include_router` prefix).
Enrichment gathers the structural facts with provenance and leaves a framework-boundary
candidate -- never an edge -- unless the facts compose deterministically or an existing rule
(composed dispatch) closed it. Fixtures use neutral names.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from test_cbm_client import FakeSession

from sydes.code_intelligence.cbm import CBMCodeIntelligence
from sydes.code_intelligence.cbm_client import CBMClient
from sydes.discover.structural_enrichment import (
    PROVENANCE_CBM_CALL,
    PROVENANCE_CBM_DECORATOR,
    PROVENANCE_CBM_REGISTRATION,
    PROVENANCE_SOURCE_FALLBACK,
    EnrichmentInput,
    EnrichmentResult,
    enrich,
)

P = "proj"


def _sym(name: str, kind: str, start: int, end: int, parent: str | None = None) -> dict[str, Any]:
    return {"name": name, "kind": kind, "start_line": start, "end_line": end, "parent": parent,
            "cbm_qualified_name": f"{P}.{parent + '.' if parent else ''}{name}"}


def _index(files: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {"repos": [{"repo": "app", "files": [{"path": p, **v} for p, v in files.items()]}]}


class _Refs:
    """A reference fetcher that counts requests, as the CBM adapter does."""

    def __init__(self, rows: list[list[str]]) -> None:
        self.rows, self.calls = rows, []

    def __call__(self, names: list[str]) -> list[list[str]]:
        self.calls.append(list(names))
        return [r for r in self.rows if r[0] in names]


# ----------------------------------------------------------------------------- message dispatch

PRODUCER = """\
export class RemoveItemController {
  constructor(private readonly commandBus: CommandBus) {}

  async remove(id: string): Promise<void> {
    const command = new RemoveItemCommand({ itemId: id });
    await this.commandBus.execute(command);
  }
}
"""
HANDLER = """\
export class RemoveItemCommand { constructor(props: any) {} }

@CommandHandler(RemoveItemCommand)
export class RemoveItemService {
  async execute(command: RemoveItemCommand): Promise<void> {}
}
"""
MODULE = """\
import { RemoveItemService } from './remove-item.service';
const handlers = [RemoveItemService];
@Module({ imports: [CqrsModule], providers: [...handlers] })
export class ItemModule {}
"""


def _dispatch_case(tmp_path: Path, *, composed: list[dict[str, Any]] | None = None,
                   unresolved: bool = True) -> tuple[EnrichmentInput, _Refs]:
    for name, text in {"src/controller.ts": PRODUCER, "src/service.ts": HANDLER, "src/module.ts": MODULE}.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    index = _index({
        "src/controller.ts": {"symbols": [_sym("RemoveItemController", "class", 1, 8),
                                          _sym("remove", "class_method", 4, 7, "RemoveItemController")]},
        "src/service.ts": {"symbols": [_sym("RemoveItemCommand", "class", 1, 1),
                                       _sym("RemoveItemService", "class", 3, 6),
                                       _sym("execute", "class_method", 5, 5, "RemoveItemService")]},
        "src/module.ts": {"symbols": [_sym("ItemModule", "class", 3, 4)],
                          "imports": [{"local": "RemoveItemService", "imported": "RemoveItemService",
                                       "source": "./remove-item.service", "resolved_file": "src/service.ts"}]},
    })
    decorated = [
        {"symbol": "RemoveItemService", "file": "src/service.ts", "decorators": "@CommandHandler(RemoveItemCommand)"},
        {"symbol": "ItemModule", "file": "src/module.ts",
         "decorators": "@Module({ imports: [CqrsModule], providers: [...handlers] })"},
    ]
    refs = _Refs([[f"{P}.RemoveItemCommand", "CALLS", f"{P}.RemoveItemController.remove", "Method", "src/controller.ts", "4"]])
    ctx = EnrichmentInput(
        repo="app", repo_root=tmp_path,
        unresolved=[{"name": "execute", "file": "src/service.ts"}] if unresolved else [],
        reached_entrypoints=[], route_flows=[], decorated=decorated, symbol_index=index,
        route_index={"repos": []}, composed=composed or [],
    )
    return ctx, refs


def test_no_trigger_means_no_cbm_request(tmp_path: Path) -> None:
    ctx, refs = _dispatch_case(tmp_path, unresolved=False)
    result = enrich(ctx, refs)
    assert refs.calls == [] and result.candidates == [] and result.triggered == []


def test_an_unresolved_handler_yields_a_dispatch_candidate_not_an_edge(tmp_path: Path) -> None:
    ctx, refs = _dispatch_case(tmp_path)
    result = enrich(ctx, refs)
    assert len(refs.calls) == 1  # one batched reference request for the whole run
    [candidate] = result.candidates
    assert candidate.category == "message_dispatch" and candidate.status == "unresolved"
    assert candidate.source_symbol == "RemoveItemController.remove"
    assert candidate.via == "CommandBus.execute(RemoveItemCommand)"
    assert candidate.targets == ["RemoveItemService.execute"]
    by = {f.provenance: f.fact for f in candidate.facts}
    assert by[PROVENANCE_CBM_DECORATOR] == "RemoveItemService is decorated @CommandHandler(RemoveItemCommand)"
    assert "RemoveItemController.remove constructs/references RemoveItemCommand" in by[PROVENANCE_CBM_CALL]
    assert any("`commandBus` is declared CommandBus" in f.fact for f in candidate.facts
               if f.provenance == PROVENANCE_SOURCE_FALLBACK)
    assert "ItemModule" in by[PROVENANCE_CBM_REGISTRATION]
    assert candidate.missing and "runtime" in candidate.missing[0]
    assert not hasattr(result, "edges")


def test_a_matching_composed_dispatch_resolves_it_and_a_mismatch_does_not(tmp_path: Path) -> None:
    rule = {"from": "RemoveItemController.remove", "to": "RemoveItemService.execute", "rule": "nestjs_cqrs_command_handler"}
    ctx, refs = _dispatch_case(tmp_path, composed=[rule])
    [resolved] = enrich(ctx, refs).candidates
    assert resolved.status == "resolved" and "composed dispatch" in (resolved.resolved_by or "")
    ctx, refs = _dispatch_case(tmp_path, composed=[{**rule, "to": "OtherService.execute"}])
    [still] = enrich(ctx, refs).candidates
    assert still.status == "unresolved" and still.resolved_by is None


def test_missing_cbm_references_stay_an_explicit_gap(tmp_path: Path) -> None:
    ctx, _ = _dispatch_case(tmp_path)
    [candidate] = enrich(ctx, _Refs([])).candidates
    assert candidate.status == "unresolved"
    assert candidate.missing == ["no repository code that constructs or references RemoveItemCommand was found"]


# ----------------------------------------------------------------------------- callback registration

CONFIG = """\
@Configuration
public class WebConfig extends Base {
    @Autowired
    private TokenFilter tokenFilter;

    protected void configure(Builder auth) throws Exception {
        auth.noop();
    }

    protected void configure(Http http) throws Exception {
        http.addFilterBefore(tokenFilter, Other.class);
    }
}
"""


def test_a_callback_registration_is_found_and_the_lifecycle_stays_unresolved(tmp_path: Path) -> None:
    (tmp_path / "WebConfig.java").write_text(CONFIG)
    (tmp_path / "TokenFilter.java").write_text("@Component\npublic class TokenFilter {}\n")
    index = _index({
        # CBM keeps one symbol per qualified name: the overload holding the call has no span
        "WebConfig.java": {"symbols": [_sym("WebConfig", "class", 1, 13),
                                       _sym("configure", "class_method", 6, 8, "WebConfig")]},
        "TokenFilter.java": {"symbols": [_sym("TokenFilter", "class", 1, 2),
                                         _sym("doFilterInternal", "class_method", 2, 2, "TokenFilter")]},
    })
    refs = _Refs([[f"{P}.TokenFilter", "USAGE", f"{P}.java.__file__", "File", "WebConfig.java", "1"]])
    ctx = EnrichmentInput(
        repo="app", repo_root=tmp_path, unresolved=[], route_flows=[], route_index={"repos": []},
        reached_entrypoints=[{"symbol": "doFilterInternal", "qualified_name": f"{P}.TokenFilter.doFilterInternal",
                              "file": "TokenFilter.java", "kind": "decorated", "route_method": None}],
        decorated=[{"symbol": "TokenFilter", "file": "TokenFilter.java", "decorators": "@Component"}],
        symbol_index=index,
    )
    result = enrich(ctx, refs)
    [candidate] = result.candidates
    assert candidate.category == "callback_registration" and candidate.status == "unresolved"
    assert candidate.source_symbol == "WebConfig.configure"  # found by source, not CBM's overload span
    assert candidate.via == "addFilterBefore(tokenFilter, …)"
    assert candidate.targets == ["TokenFilter.doFilterInternal"]
    registration = [f for f in candidate.facts if f.provenance == PROVENANCE_SOURCE_FALLBACK]
    assert registration and registration[0].line == 11 and "addFilterBefore" in registration[0].fact
    assert "lifecycle" in candidate.missing[0]


# ----------------------------------------------------------------------------- route registration


def _route_case(tmp_path: Path, *, decl: str, mount: str) -> EnrichmentResult:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "routes.py").write_text(f"router = APIRouter()\n\n{decl}\nasync def speak():\n    pass\n")
    (tmp_path / "app" / "main.py").write_text(f"from .routes import router as api_router\n{mount}\n")
    index = _index({
        "app/routes.py": {"symbols": [_sym("router", "variable", 1, 1), _sym("speak", "function", 4, 5)]},
        "app/main.py": {"symbols": [], "imports": [{"local": "api_router", "imported": "api_router",
                                                     "source": "router", "resolved_file": "app/routes.py"}]},
    })
    prefix = "/v1" if 'prefix="/v1"' in mount else ""
    route_index = {"repos": [{"repo": "app", "files": [
        {"path": "app/routes.py", "route_calls": [{"receiver": "router", "method": "post", "handler_hint": "speak",
                                                  "path": "/speak" if '"/speak"' in decl else "", "line": 3,
                                                  "snippet": decl}], "mount_calls": []},
        {"path": "app/main.py", "route_calls": [], "mount_calls": [
            {"receiver": "app", "prefix": prefix, "child": "api_router", "line": 2, "snippet": mount}]},
    ]}]}
    refs = _Refs([[f"{P}.router", "USAGE", f"{P}.main", "Module", "app/main.py", "2"]])
    ctx = EnrichmentInput(
        repo="app", repo_root=tmp_path, unresolved=[], reached_entrypoints=[],
        route_flows=[{"method": "POST", "path": f"{prefix}/speak", "handler": "speak", "handler_file": "app/routes.py"}],
        decorated=[{"symbol": "speak", "file": "app/routes.py", "decorators": decl}],
        symbol_index=index, route_index=route_index,
    )
    return enrich(ctx, refs)



def test_a_literal_route_and_prefix_compose(tmp_path: Path) -> None:
    [route] = _route_case(tmp_path, decl='@router.post("/speak")',
                          mount='app.include_router(api_router, prefix="/v1")').candidates
    assert route.category == "route_registration" and route.status == "resolved" and route.missing == []
    facts = [f.fact for f in route.facts]
    assert 'speak is decorated @router.post("/speak")' in facts
    assert "`api_router` is mounted with prefix '/v1'" in facts
    assert any(f.startswith("CBM: `router` is used in app/main.py") for f in facts)


def test_a_symbolic_route_path_is_not_composed(tmp_path: Path) -> None:
    [route] = _route_case(tmp_path, decl="@router.post(routes.audio.speech)",
                          mount='app.include_router(api_router, prefix="/v1")').candidates
    assert route.status == "unresolved" and route.resolved_by is None
    assert route.missing == ["route path `routes.audio.speech` is not a literal"]


def test_a_symbolic_mount_prefix_is_flagged_instead_of_dropped(tmp_path: Path) -> None:
    [route] = _route_case(tmp_path, decl='@router.post("/speak")',
                          mount="app.include_router(api_router, prefix=settings.api_prefix)").candidates
    assert route.status == "unresolved"
    assert route.missing[0].startswith("mount prefix `settings.api_prefix` is not a literal")


# ----------------------------------------------------------------------------- CBM requests


def test_reference_lookups_are_cached_and_counted_per_run() -> None:
    rows = [[f"{P}.T", "CALLS", f"{P}.A.run", "Method", "a.ts", "3"]]
    session = FakeSession({"query_graph": {"columns": ["b", "r", "a", "l", "f", "s"], "rows": rows,
                                           "has_more": False}})
    adapter = CBMCodeIntelligence(client=CBMClient(session))
    adapter._projects["app"] = P
    assert adapter.inbound_references("app", [f"{P}.T"]) == rows
    assert adapter.inbound_references("app", [f"{P}.T"]) == rows
    assert adapter.enrichment_queries == 1 and len(session.calls) == 1
    assert adapter.inbound_references("other", [f"{P}.T"]) == []  # not indexed here: no request


def test_reference_pages_are_followed_and_a_cut_page_is_counted() -> None:
    pages = {
        None: {"columns": ["b", "r", "a", "l", "f", "s"], "rows": [["t", "CALLS", "a", "Method", "a.ts", "1"]],
               "has_more": True, "next_offset": 1},
        1: {"columns": ["b", "r", "a", "l", "f", "s"], "rows": [["t", "USAGE", "b", "File", "b.ts", "2"]],
            "has_more": False},
    }
    client = CBMClient(FakeSession({"query_graph": lambda arguments: pages[arguments.get("offset")]}))
    assert [r[2] for r in client.inbound_references(P, ["t"])] == ["a", "b"]
    cut = CBMClient(FakeSession({"query_graph": {"columns": ["b", "r", "a", "l", "f", "s"],
                                                 "rows": [["t", "CALLS", "a", "Method", "a.ts", "1"]],
                                                 "has_more": True}}))
    cut.inbound_references(P, ["t"])
    assert cut.truncated_responses == 1  # surfaces as a partial-analysis gap, not a complete answer


def test_the_report_marks_framework_hops_unresolved_never_as_calls() -> None:
    from sydes.report.verify_terminal import _framework_boundary_blocks

    blocks = _framework_boundary_blocks([
        {"category": "message_dispatch", "status": "unresolved", "source_symbol": "C.remove",
         "via": "CommandBus.execute(T)", "targets": ["S.execute"],
         "facts": [{"provenance": "cbm_decorator", "fact": "S is decorated @CommandHandler(T)"}]},
        {"category": "callback_registration", "status": "unresolved", "source_symbol": "W.configure",
         "via": "addFilterBefore(f, …)", "targets": ["F.doFilterInternal"], "facts": []},
        {"category": "message_dispatch", "status": "resolved", "source_symbol": "X", "via": "v", "targets": ["Y"]},
    ])
    assert blocks == [
        ["C.remove", "  → CommandBus.execute(T)", "  → [framework dispatch unresolved]",
         "  → candidate: S.execute (@CommandHandler(T))"],
        ["W.configure", "  → addFilterBefore(f, …): registers F", "  → [framework lifecycle unresolved]",
         "  → candidate: F.doFilterInternal"],
    ]
