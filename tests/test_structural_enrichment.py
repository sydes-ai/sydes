"""Targeted structural enrichment after ordinary traversal stalls (Track A).

Three shapes the CBM call graph cannot finish on its own, from real cases:
domain-driven-hexagon (a CQRS message handler nothing calls directly), spring-boot-demo
(a security filter the framework invokes after `addFilterBefore` registers it) and
Kokoro-FastAPI (a route assembled from a router decorator and an `include_router` prefix).
Enrichment asks the CBM fact layer for the relation families each case is missing --
typed relations with their properties, HANDLES / HTTP_CALLS routes, DECORATES, CONFIGURES,
INHERITS, direct-relation and bounded-path checks, CBM source search -- and falls back to a
local read only after CBM had no answer. It leaves a framework-boundary candidate, never an
edge. Fixtures use neutral names.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_cbm_client import FakeSession

from sydes.code_intelligence.cbm import CBMCodeIntelligence
from sydes.code_intelligence.cbm_client import CBMClient
from sydes.code_intelligence.cbm_facts import (
    MAX_SEARCHES,
    TRACK_A_RELATIONS,
    CBMFacts,
    Relation,
)
from sydes.discover.structural_enrichment import (
    PROVENANCE_CBM_CALL,
    PROVENANCE_CBM_CALL_REFERENCE,
    PROVENANCE_CBM_CONFIGURES,
    PROVENANCE_CBM_DECORATOR,
    PROVENANCE_CBM_HANDLES,
    PROVENANCE_CBM_HTTP_CALLS,
    PROVENANCE_CBM_INHERITS,
    PROVENANCE_CBM_PATH,
    PROVENANCE_CBM_REGISTRATION,
    PROVENANCE_CBM_SOURCE,
    PROVENANCE_CBM_USAGE,
    PROVENANCE_SOURCE_FALLBACK,
    EnrichmentInput,
    EnrichmentResult,
    enrich,
    numbered,
)

P = "proj"
ALL_TYPES = set(TRACK_A_RELATIONS)


def _sym(name: str, kind: str, start: int, end: int, parent: str | None = None) -> dict[str, Any]:
    return {"name": name, "kind": kind, "start_line": start, "end_line": end, "parent": parent,
            "cbm_qualified_name": f"{P}.{parent + '.' if parent else ''}{name}"}


def _index(files: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {"repos": [{"repo": "app", "files": [{"path": p, **v} for p, v in files.items()]}]}


def _rel(source: str, label: str, file: str, kind: str, target: str, tlabel: str, tfile: str,
         **props: Any) -> list[str]:
    return [source, label, file, kind, target, tlabel, tfile, json.dumps(props)]


class FakeClient:
    """The CBM client surface the fact layer uses, answering from canned facts and
    recording each request."""

    def __init__(self, *, relations: list[list[str]] | None = None, edge_types: set[str] | None = None,
                 direct: list[list[str]] | None = None, paths: list[list[str]] | None = None,
                 searches: dict[tuple[str, str | None, str], dict[str, Any]] | None = None) -> None:
        self.rows = relations or []
        self.edge_types = ALL_TYPES if edge_types is None else edge_types
        self.direct_rows, self.path_rows, self.searches = direct or [], paths or [], searches or {}
        self.calls: list[tuple[str, Any]] = []

    def graph_capabilities(self, project: str) -> dict[str, dict[str, int]]:
        self.calls.append(("schema", None))
        return {"labels": {}, "edge_types": {t: 1 for t in self.edge_types}}

    def relations(self, project: str, seeds: list[str], types: list[str]) -> list[list[str]]:
        self.calls.append(("relations", (list(seeds), list(types))))
        return [r for r in self.rows if r[3] in types and (r[0] in seeds or r[4] in seeds)]

    def direct_relations(self, project: str, left: list[str], right: list[str]) -> list[list[str]]:
        self.calls.append(("direct", (left, right)))
        return [r for r in self.direct_rows if {r[0], r[2]} & set(left) and {r[0], r[2]} & set(right)]

    def calls_paths(self, project: str, sources: list[str], targets: list[str], *, max_hops: int) -> list[list[str]]:
        self.calls.append(("calls_path", (sources, targets, max_hops)))
        return [r for r in self.path_rows if r[0] in sources and r[1] in targets]

    def search_code(self, project: str, pattern: str, *, path_filter: str | None, mode: str) -> dict[str, Any]:
        self.calls.append(("search", (pattern, path_filter, mode)))
        return self.searches.get((pattern, path_filter, mode), {"rows": [], "files": [], "truncated": False})

    def kinds(self) -> list[str]:
        return [k for k, _ in self.calls]


def _row(qn: str, label: str, file: str, lines: str, matches: list[int], source: str) -> dict[str, Any]:
    return {"qn": qn, "label": label, "file": file, "lines": lines, "matches": matches, "source": source}


def _facts(client: FakeClient) -> CBMFacts:
    return CBMFacts(client, P)


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
REMOVE, COMMAND = f"{P}.RemoveItemController.remove", f"{P}.RemoveItemCommand"
SERVICE, EXECUTE, MODULE_QN = f"{P}.RemoveItemService", f"{P}.RemoveItemService.execute", f"{P}.ItemModule"


def _dispatch_relations(reference: str = "CALLS") -> list[list[str]]:
    return [
        _rel(SERVICE, "Class", "src/service.ts", "DECORATES", f"{P}.<decorator:CommandHandler>", "Decorator", "",
             decorator="@CommandHandler(RemoveItemCommand)"),
        _rel(REMOVE, "Method", "src/controller.ts", reference, COMMAND, "Class", "src/service.ts",
             line=5, args=[{"i": 0, "e": "{ itemId: id }"}], strategy="import_map", confidence=0.95),
        _rel(f"{P}.spec.it", "Method", "src/service.spec.ts", "CALLS", COMMAND, "Class", "src/service.ts"),
        _rel(MODULE_QN, "Class", "src/module.ts", "DECORATES", f"{P}.<decorator:Module>", "Decorator", "",
             decorator="@Module({ imports: [CqrsModule], providers: [...handlers] })"),
    ]


def _dispatch_searches() -> dict[tuple[str, str | None, str], dict[str, Any]]:
    return {
        ("RemoveItemService", None, "files"): {"rows": [], "files": ["src/service.ts", "src/module.ts",
                                                                     "src/service.spec.ts"]},
        ("new RemoveItemCommand", "src/controller.ts", "full"): {"rows": [_row(
            REMOVE, "Method", "src/controller.ts", "4-7", [5], "\n".join(PRODUCER.splitlines()[3:7]))]},
        ("commandBus", "src/controller.ts", "full"): {"rows": [_row(
            f"{P}.RemoveItemController.constructor", "Method", "src/controller.ts", "2-2", [2],
            PRODUCER.splitlines()[1])]},
    }


def _dispatch_case(tmp_path: Path, *, composed: list[dict[str, Any]] | None = None, unresolved: bool = True,
                   write_files: bool = True) -> EnrichmentInput:
    if write_files:
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
    return EnrichmentInput(
        repo="app", repo_root=tmp_path,
        unresolved=[{"name": "execute", "file": "src/service.ts"}] if unresolved else [],
        reached_entrypoints=[], route_flows=[], decorated=decorated, symbol_index=index,
        route_index={"repos": []}, composed=composed or [],
    )


def test_no_trigger_means_no_cbm_request(tmp_path: Path) -> None:
    client = FakeClient()
    result = enrich(_dispatch_case(tmp_path, unresolved=False), _facts(client))
    assert client.calls == [] and result.candidates == [] and result.triggered == []


def test_an_unresolved_handler_yields_an_information_rich_candidate_not_an_edge(tmp_path: Path) -> None:
    client = FakeClient(relations=_dispatch_relations(), searches=_dispatch_searches())
    result = enrich(_dispatch_case(tmp_path), _facts(client))
    [candidate] = result.candidates
    assert candidate.category == "message_dispatch" and candidate.status == "unresolved"
    assert candidate.source_symbol == "RemoveItemController.remove"
    assert candidate.via == "CommandBus.execute(RemoveItemCommand)"
    assert candidate.targets == ["RemoveItemService.execute"]
    facts = {(f.provenance, f.fact) for f in candidate.facts}
    assert (PROVENANCE_CBM_DECORATOR,
            "RemoveItemService is decorated @CommandHandler(RemoveItemCommand) (DECORATES)") in facts
    # the CALLS relation with its own properties; the test-file caller is not a producer
    assert (PROVENANCE_CBM_CALL, ("RemoveItemController.remove calls RemoveItemCommand "
                                  "[line 5; args ({ itemId: id }); resolved by import_map at 0.95]")) in facts
    assert not any("spec" in f for _, f in facts)
    # the hand-off and the receiver's type come from CBM's source search, not a local read
    handoff = next(f for f in candidate.facts if "commandBus.execute" in f.fact)
    assert handoff.provenance == PROVENANCE_CBM_SOURCE and handoff.line == 6
    assert "`commandBus` is declared CommandBus" in handoff.fact
    assert any(p == PROVENANCE_CBM_REGISTRATION and "ItemModule is decorated @Module" in f for p, f in facts)
    assert candidate.checks == {"direct_relation": "none", "calls_path": "none within 5 hops"}
    assert sum(f.provenance == PROVENANCE_CBM_PATH for f in candidate.facts) == 2
    assert candidate.runtime_needed and "through_external" in candidate.runtime_needed
    assert not any(f.provenance == PROVENANCE_SOURCE_FALLBACK for f in candidate.facts)
    assert not hasattr(result, "edges")


def test_requests_are_batched_per_wave_and_counted(tmp_path: Path) -> None:
    client = FakeClient(relations=_dispatch_relations(), searches=_dispatch_searches())
    facts = _facts(client)
    result = enrich(_dispatch_case(tmp_path), facts)
    assert client.kinds().count("schema") == 1
    assert client.kinds().count("relations") == 2  # wave 1 (trigger symbols) + wave 2 (producers, wiring)
    assert client.kinds().count("direct") == 1 and client.kinds().count("calls_path") == 1
    assert result.stats["total_requests"] == len(client.calls)
    assert result.candidates[0].queries == len(client.calls)
    # a second pass over the same session asks CBM nothing new
    before = len(client.calls)
    enrich(_dispatch_case(tmp_path), facts)
    assert len(client.calls) == before and facts.stats()["cache_hits"]["relations"] >= 1


def test_a_matching_composed_dispatch_resolves_it_and_a_mismatch_does_not(tmp_path: Path) -> None:
    rule = {"from": "RemoveItemController.remove", "to": "RemoveItemService.execute",
            "rule": "nestjs_cqrs_command_handler"}
    client = FakeClient(relations=_dispatch_relations(), searches=_dispatch_searches())
    [resolved] = enrich(_dispatch_case(tmp_path, composed=[rule]), _facts(client)).candidates
    assert resolved.status == "resolved" and "composed dispatch" in (resolved.resolved_by or "")
    assert resolved.runtime_needed is None
    mismatch = _dispatch_case(tmp_path, composed=[{**rule, "to": "OtherService.execute"}])
    [still] = enrich(mismatch, _facts(client)).candidates
    assert still.status == "unresolved" and still.resolved_by is None


def test_missing_cbm_references_stay_an_explicit_gap(tmp_path: Path) -> None:
    client = FakeClient(relations=[r for r in _dispatch_relations() if r[3] == "DECORATES"])
    [candidate] = enrich(_dispatch_case(tmp_path), _facts(client)).candidates
    assert candidate.status == "unresolved" and candidate.source_symbol == "?"
    assert candidate.missing[0].startswith("no repository code that constructs or references RemoveItemCommand")


@pytest.mark.parametrize(("kind", "provenance", "verb"), [
    ("CALL_REFERENCE", PROVENANCE_CBM_CALL_REFERENCE, "passes/stores a reference to"),
    ("USAGE", PROVENANCE_CBM_USAGE, "uses"),
])
def test_call_reference_and_usage_stay_distinct_from_calls(tmp_path: Path, kind: str, provenance: str,
                                                           verb: str) -> None:
    client = FakeClient(relations=_dispatch_relations(kind), searches=_dispatch_searches())
    [candidate] = enrich(_dispatch_case(tmp_path), _facts(client)).candidates
    [reference] = [f for f in candidate.facts if f.fact.startswith("RemoveItemController.remove ")
                   and f.fact.endswith("]") and "RemoveItemCommand" in f.fact]
    assert reference.provenance == provenance and f" {verb} RemoveItemCommand" in reference.fact
    assert not any(f.provenance == PROVENANCE_CBM_CALL for f in candidate.facts)


def test_source_comes_from_cbm_and_the_local_read_only_after_a_cbm_miss(tmp_path: Path) -> None:
    # CBM answers: the repository files need not even exist
    client = FakeClient(relations=_dispatch_relations(), searches=_dispatch_searches())
    [cbm] = enrich(_dispatch_case(tmp_path, write_files=False), _facts(client)).candidates
    assert any(f.provenance == PROVENANCE_CBM_SOURCE and "commandBus.execute" in f.fact for f in cbm.facts)
    # CBM's search finds nothing: the local read, saying so
    client = FakeClient(relations=_dispatch_relations())
    [local] = enrich(_dispatch_case(tmp_path), _facts(client)).candidates
    handoff = next(f for f in local.facts if "commandBus.execute" in f.fact)
    assert handoff.provenance == PROVENANCE_SOURCE_FALLBACK
    assert "CBM source search found no `new RemoveItemCommand`" in handoff.fact


def test_without_a_cbm_session_nothing_is_invented(tmp_path: Path) -> None:
    [candidate] = enrich(_dispatch_case(tmp_path), None).candidates
    assert candidate.source_symbol == "?"  # no CBM relations: no producer is invented
    assert {f.provenance for f in candidate.facts} <= {PROVENANCE_CBM_DECORATOR, PROVENANCE_CBM_REGISTRATION,
                                                        PROVENANCE_SOURCE_FALLBACK}
    assert any("local import index" in f.fact for f in candidate.facts)
    assert candidate.checks == {}


def test_a_found_direct_relation_or_path_is_reported_never_turned_into_an_edge(tmp_path: Path) -> None:
    client = FakeClient(relations=_dispatch_relations(), searches=_dispatch_searches(),
                        direct=[[REMOVE, "USAGE", SERVICE, "{}"]], paths=[[REMOVE, EXECUTE]])
    [candidate] = enrich(_dispatch_case(tmp_path), _facts(client)).candidates
    assert candidate.checks == {"direct_relation": "RemoveItemController.remove -USAGE-> proj.RemoveItemService",
                                "calls_path": "RemoveItemController.remove ->…-> RemoveItemService.execute"}
    assert candidate.status == "unresolved" and candidate.targets == ["RemoveItemService.execute"]


def test_decorates_is_preferred_and_the_sweep_keeps_repeated_decorators(tmp_path: Path) -> None:
    relations = _dispatch_relations() + [
        _rel(REMOVE, "Method", "src/controller.ts", "DECORATES", f"{P}.<decorator:ApiResponse>", "Decorator", "",
             decorator="@ApiResponse({ status: 404 })"),
    ]
    ctx = _dispatch_case(tmp_path)
    ctx.decorated.append({"symbol": "remove", "file": "src/controller.ts",
                          "decorators": "@ApiResponse({ status: 404 })\n@ApiResponse({ status: 200 })"})
    [candidate] = enrich(ctx, _facts(FakeClient(relations=relations, searches=_dispatch_searches()))).candidates
    texts = [f.fact for f in candidate.facts if f.fact.startswith("RemoveItemController.remove is decorated")]
    assert texts == ["RemoveItemController.remove is decorated @ApiResponse({ status: 404 }) (DECORATES)",
                     "RemoveItemController.remove is decorated @ApiResponse({ status: 200 }) (decorator sweep)"]


def test_a_graph_without_handles_says_so_and_is_not_asked_for_it(tmp_path: Path) -> None:
    client = FakeClient(relations=_dispatch_relations(), searches=_dispatch_searches(),
                        edge_types=ALL_TYPES - {"HANDLES"})
    [candidate] = enrich(_dispatch_case(tmp_path), _facts(client)).candidates
    assert any(f.provenance == PROVENANCE_CBM_HANDLES and "no HANDLES anywhere in this graph" in f.fact
               for f in candidate.facts)
    assert all("HANDLES" not in types for kind, (_seeds, types) in
               ((k, a) for k, a in client.calls if k == "relations"))


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
FILTER, WEB = f"{P}.TokenFilter", f"{P}.WebConfig"


def _callback_case(tmp_path: Path) -> EnrichmentInput:
    (tmp_path / "WebConfig.java").write_text(CONFIG)
    (tmp_path / "TokenFilter.java").write_text("@Component\npublic class TokenFilter extends OncePerRequest {}\n")
    index = _index({
        # CBM keeps one symbol per qualified name: the overload holding the call has no span
        "WebConfig.java": {"symbols": [_sym("WebConfig", "class", 1, 13),
                                       _sym("configure", "class_method", 6, 8, "WebConfig")]},
        "TokenFilter.java": {"symbols": [_sym("TokenFilter", "class", 1, 2),
                                         _sym("doFilterInternal", "class_method", 2, 2, "TokenFilter")]},
    })
    return EnrichmentInput(
        repo="app", repo_root=tmp_path, unresolved=[], route_flows=[], route_index={"repos": []},
        reached_entrypoints=[{"symbol": "doFilterInternal", "qualified_name": f"{FILTER}.doFilterInternal",
                              "file": "TokenFilter.java", "kind": "decorated", "route_method": None}],
        decorated=[{"symbol": "TokenFilter", "file": "TokenFilter.java", "decorators": "@Component"}],
        symbol_index=index,
    )


def _callback_relations(*, configures: bool = False) -> list[list[str]]:
    rows = [
        _rel(f"{P}.java.__file__", "File", "WebConfig.java", "USAGE", FILTER, "Class", "TokenFilter.java"),
        _rel(FILTER, "Class", "TokenFilter.java", "INHERITS", f"{P}.res.logback.filter", "Class", "res/logback.xml"),
        _rel(FILTER, "Class", "TokenFilter.java", "INHERITS", f"{P}.OncePerRequest", "Class", "OncePerRequest.java"),
        _rel(WEB, "Class", "WebConfig.java", "DECORATES", f"{P}.<decorator:Configuration>", "Decorator", "",
             decorator="@Configuration"),
    ]
    if configures:
        rows.append(_rel(FILTER, "Class", "TokenFilter.java", "CONFIGURES", f"{P}.jwt.key", "Variable",
                         "app.properties", strategy="key_symbol", config_key="jwt.key"))
    return rows


FIELD_ROW = _row(f"{WEB}.tokenFilter", "Field", "WebConfig.java", "3-4", [4],
                 "    @Autowired\n    private TokenFilter tokenFilter;\n")


def _callback_searches() -> dict[tuple[str, str | None, str], dict[str, Any]]:
    return {
        ("TokenFilter", "WebConfig.java", "full"): {"rows": [FIELD_ROW]},
        # CBM places the call only in the class (a window around the match, not the whole span)
        ("tokenFilter", "WebConfig.java", "full"): {"rows": [FIELD_ROW, _row(
            WEB, "Class", "WebConfig.java", "1-13", [11],
            "    protected void configure(Http http) throws Exception {\n"
            "        http.addFilterBefore(tokenFilter, Other.class);\n")]},
    }


def test_a_callback_registration_is_found_through_cbm_and_the_lifecycle_stays_unresolved(tmp_path: Path) -> None:
    client = FakeClient(relations=_callback_relations(), searches=_callback_searches())
    [candidate] = enrich(_callback_case(tmp_path), _facts(client)).candidates
    assert candidate.category == "callback_registration" and candidate.status == "unresolved"
    assert candidate.source_symbol == "WebConfig.configure"
    assert candidate.via == "addFilterBefore(tokenFilter, …)"
    assert candidate.targets == ["TokenFilter.doFilterInternal"]
    registration = next(f for f in candidate.facts if "addFilterBefore" in f.fact)
    assert registration.line == 11
    # CBM found the line; only the overloaded method's name needed a local signature scan, and it says so
    assert registration.provenance == PROVENANCE_SOURCE_FALLBACK
    assert "overloads share one symbol" in registration.fact
    assert any(f.provenance == PROVENANCE_CBM_USAGE and f.fact == "WebConfig.java uses TokenFilter"
               for f in candidate.facts)
    assert candidate.checks and candidate.runtime_needed and "lifecycle" in candidate.missing[0]


def test_inherits_into_a_configuration_file_is_reported_and_ignored(tmp_path: Path) -> None:
    client = FakeClient(relations=_callback_relations(), searches=_callback_searches())
    [candidate] = enrich(_callback_case(tmp_path), _facts(client)).candidates
    inherits = [f.fact for f in candidate.facts if f.provenance == PROVENANCE_CBM_INHERITS]
    assert "TokenFilter inherits OncePerRequest" in inherits
    assert any(f.startswith("ignored: CBM resolves TokenFilter INHERITS to `filter` from res/logback.xml")
               for f in inherits)


def test_configures_is_used_when_present_and_its_absence_explained(tmp_path: Path) -> None:
    absent = FakeClient(relations=_callback_relations(), searches=_callback_searches())
    [candidate] = enrich(_callback_case(tmp_path), _facts(absent)).candidates
    [note] = [f.fact for f in candidate.facts if f.provenance == PROVENANCE_CBM_CONFIGURES]
    assert "configuration keys and environment access, not registrations" in note
    present = FakeClient(relations=_callback_relations(configures=True), searches=_callback_searches())
    [candidate] = enrich(_callback_case(tmp_path), _facts(present)).candidates
    assert [f.fact for f in candidate.facts if f.provenance == PROVENANCE_CBM_CONFIGURES] == [
        "proj.TokenFilter configures jwt.key (key_symbol)"]


def test_a_callback_without_cbm_source_falls_back_to_the_local_read(tmp_path: Path) -> None:
    client = FakeClient(relations=_callback_relations())  # no search answers
    [candidate] = enrich(_callback_case(tmp_path), _facts(client)).candidates
    registration = next(f for f in candidate.facts if "addFilterBefore" in f.fact)
    assert registration.provenance == PROVENANCE_SOURCE_FALLBACK and registration.line == 11
    assert "CBM source search found no `TokenFilter`" in registration.fact
    assert candidate.source_symbol == "WebConfig.configure"


# ----------------------------------------------------------------------------- route registration

ROUTER, SPEAK = f"{P}.router", f"{P}.speak"


def _route_case(tmp_path: Path, *, decl: str, mount: str, handles: str | None = None, usage: bool = True,
                http_arg: str | None = None, mount_search: bool = True) -> EnrichmentResult:
    (tmp_path / "app").mkdir(exist_ok=True)
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
    relations = []
    if usage:
        relations.append(_rel(f"{P}.main", "Module", "app/main.py", "USAGE", ROUTER, "Variable", "app/routes.py",
                              callee="api_router"))
    if handles:
        route = f"__route__POST__{handles}"
        relations.append(_rel(SPEAK, "Function", "app/routes.py", "HANDLES", route, "Route", "", handler=SPEAK))
        if http_arg:
            relations.append(_rel(f"{P}.routes", "Module", "app/routes.py", "HTTP_CALLS", route, "Route", "",
                                  callee="router.post", url_path=handles, method="POST",
                                  args=[{"i": 0, "e": http_arg}]))
    searches = {}
    if mount_search:
        searches[("api_router", "app/main.py", "full")] = {"rows": [_row(
            f"{P}.main", "Module", "app/main.py", "1-2", [1, 2], f"from .routes import router as api_router\n{mount}\n")]}
    ctx = EnrichmentInput(
        repo="app", repo_root=tmp_path, unresolved=[], reached_entrypoints=[],
        route_flows=[{"method": "POST", "path": f"{prefix}/speak", "handler": "speak", "handler_file": "app/routes.py"}],
        decorated=[{"symbol": "speak", "file": "app/routes.py", "decorators": decl}],
        symbol_index=index, route_index=route_index,
    )
    return enrich(ctx, _facts(FakeClient(relations=relations, searches=searches)))


def test_a_literal_route_and_prefix_compose_from_cbm_facts(tmp_path: Path) -> None:
    [route] = _route_case(tmp_path, decl='@router.post("/speak")', mount='app.include_router(api_router, prefix="/v1")',
                          handles="/v1/speak", http_arg="'/speak'").candidates
    assert route.category == "route_registration" and route.status == "resolved" and route.missing == []
    facts = {(f.provenance, f.fact) for f in route.facts}
    assert (PROVENANCE_CBM_HTTP_CALLS, "speak is registered on `router` with path '/speak' (HTTP_CALLS)") in facts
    assert (PROVENANCE_CBM_USAGE, "CBM: `router` is used as `api_router` in app/main.py") in facts
    assert (PROVENANCE_CBM_SOURCE, "`api_router` is mounted with prefix '/v1'") in facts
    assert (PROVENANCE_CBM_HANDLES, "CBM: speak HANDLES POST /v1/speak") in facts
    assert not any(p == PROVENANCE_SOURCE_FALLBACK for p, _ in facts)
    assert not any("discrepancy" in f for _, f in facts)


def test_a_cbm_route_that_disagrees_with_sydes_is_kept_and_flagged(tmp_path: Path) -> None:
    [route] = _route_case(tmp_path, decl='@router.post("/speak")', mount='app.include_router(api_router, prefix="/v1")',
                          handles="/speak", http_arg="'/speak'").candidates
    facts = [f.fact for f in route.facts if f.provenance == PROVENANCE_CBM_HANDLES]
    assert facts == ["CBM: speak HANDLES POST /speak",
                     ("discrepancy: CBM POST /speak vs Sydes POST /v1/speak "
                      "(Sydes includes a mount/container prefix CBM does not compose)")]
    assert route.via == "POST /v1/speak"  # Sydes' route stays; CBM's is preserved beside it


def test_a_symbolic_route_path_is_not_composed(tmp_path: Path) -> None:
    [route] = _route_case(tmp_path, decl="@router.post(routes.audio.speech)",
                          mount='app.include_router(api_router, prefix="/v1")').candidates
    assert route.status == "unresolved" and route.resolved_by is None
    assert route.missing == ["route path `routes.audio.speech` is not a literal"]
    declaration = next(f for f in route.facts if " is registered on " in f.fact)
    assert declaration.provenance == PROVENANCE_CBM_DECORATOR  # CBM's decorator, not the route index


def test_a_symbolic_mount_prefix_is_flagged_instead_of_dropped(tmp_path: Path) -> None:
    [route] = _route_case(tmp_path, decl='@router.post("/speak")',
                          mount="app.include_router(api_router, prefix=settings.api_prefix)").candidates
    assert route.status == "unresolved"
    assert route.missing[0].startswith("mount prefix `settings.api_prefix` is not a literal")


def test_without_cbm_usage_the_mount_comes_from_the_route_index_and_says_so(tmp_path: Path) -> None:
    [route] = _route_case(tmp_path, decl='@router.post("/speak")', mount='app.include_router(api_router, prefix="/v1")',
                          usage=False, mount_search=False).candidates
    mount = next(f for f in route.facts if "is mounted" in f.fact)
    assert mount.provenance == PROVENANCE_SOURCE_FALLBACK and "CBM has no USAGE of the router" in mount.fact
    assert route.status == "resolved"


# ----------------------------------------------------------------------------- the fact layer


def test_relations_are_cached_per_seed_and_only_new_seeds_are_fetched() -> None:
    client = FakeClient(relations=[_rel("a", "Method", "a.ts", "CALLS", "b", "Method", "b.ts", line=1),
                                   _rel("c", "Method", "c.ts", "USAGE", "b", "Method", "b.ts")])
    facts = _facts(client)
    first = facts.relations(["a"])
    assert [r.target for r in first["a"]] == ["b"] and first["a"][0].props == {"line": 1}
    facts.relations(["a", "b"])
    [(_, (seeds, _types))] = [c for c in client.calls if c[0] == "relations"][1:]
    assert seeds == ["b"] and facts.stats()["cache_hits"]["relations"] == 1
    assert {r.source for r in facts.relations(["b"])["b"]} == {"a", "c"}


def test_an_edge_between_two_seeds_is_held_once_per_end_and_distinct_properties_survive() -> None:
    client = FakeClient(relations=[
        _rel("a", "Method", "a.ts", "DECORATES", "d", "Decorator", "", decorator="@X(1)"),
        _rel("a", "Method", "a.ts", "DECORATES", "d", "Decorator", "", decorator="@X(2)"),
        _rel("a", "Method", "a.ts", "CALLS", "b", "Method", "b.ts"),
    ])
    out = _facts(client).relations(["a", "b"])
    assert [r.props.get("decorator") for r in out["a"] if r.type == "DECORATES"] == ["@X(1)", "@X(2)"]
    assert len([r for r in out["b"] if r.type == "CALLS"]) == 1


def test_paths_and_searches_are_cached_and_the_search_budget_is_explicit() -> None:
    client = FakeClient()
    facts = _facts(client)
    facts.direct(["a"], ["b"])
    facts.direct(["a"], ["b"])
    facts.calls_path(["a"], ["b"])
    facts.calls_path(["a"], ["b"])
    assert client.kinds() == ["direct", "calls_path"]
    for i in range(MAX_SEARCHES):
        assert facts.search(f"p{i}") is not None
    assert facts.search("p0") is not None  # cached, no budget spent
    assert facts.search("one-more") is None and "budget" in facts.notes[-1]
    assert facts.stats()["requests"]["search"] == MAX_SEARCHES


def test_a_cbm_error_costs_the_fact_never_the_analysis() -> None:
    class Broken(FakeClient):
        def relations(self, *args: Any) -> list[list[str]]:
            raise RuntimeError("query was not fully parsed")

    facts = _facts(Broken())
    assert facts.relations(["a"]) == {"a": []}
    assert facts.stats()["errors"] == {"relations": 1} and "not fully parsed" in facts.notes[0]


def test_partial_cbm_answers_are_counted_in_the_stats() -> None:
    client = FakeClient()
    client.truncated_responses = 2  # before the fact layer existed: not its own
    facts = _facts(client)
    facts.relations(["a"])
    client.truncated_responses += 1
    assert facts.stats()["truncated"] == 1


def test_capabilities_come_from_the_schema_once() -> None:
    client = FakeClient(edge_types={"CALLS", "DECORATES"})
    facts = _facts(client)
    assert facts.supports("CALLS") and not facts.supports("HANDLES")
    assert facts.capabilities() == {"CALLS", "DECORATES"}
    facts.relations(["a"])
    assert client.kinds().count("schema") == 1
    assert [c[1][1] for c in client.calls if c[0] == "relations"] == [["CALLS", "DECORATES"]]


def test_the_adapter_hands_out_one_fact_layer_per_indexed_repository() -> None:
    adapter = CBMCodeIntelligence(client=CBMClient(FakeSession({})))
    adapter._projects["app"] = P
    assert adapter.facts("app") is adapter.facts("app")
    assert adapter.facts("other") is None


def test_search_rows_are_numbered_from_their_matches() -> None:
    whole = _row("q", "Method", "f", "10-12", [11], "a\nneedle\nc")
    assert numbered(whole, "needle") == [(10, "a"), (11, "needle"), (12, "c")]
    window = _row("q", "Class", "f", "1-200", [92], "x\ny\nneedle(z)\n")
    assert numbered(window, "needle") == [(90, "x"), (91, "y"), (92, "needle(z)")]


# ----------------------------------------------------------------------------- the CBM client


def _json(columns: list[str], rows: list[list[Any]], **extra: Any) -> dict[str, Any]:
    return {"columns": columns, "rows": rows, **extra}


def test_client_relations_query_is_one_batched_request_with_properties() -> None:
    session = FakeSession({"query_graph": _json(list("abcdefgh"), [
        ["s", "Method", "s.ts", "CALLS", "t", "Class", "t.ts", '{"line": 3}']])})
    rows = CBMClient(session).relations(P, ["s", "t"], ["CALLS", "USAGE", "bad type"])
    assert rows == [["s", "Method", "s.ts", "CALLS", "t", "Class", "t.ts", '{"line": 3}']]
    [(_, arguments)] = session.calls
    assert "[r:CALLS|USAGE]" in arguments["query"] and "properties(r)" in arguments["query"]
    # CBM 0.11 cannot order by type(r): the query must not try
    assert "ORDER BY a.qualified_name, b.qualified_name" in arguments["query"]
    assert Relation.from_row(rows[0]).props == {"line": 3}


def test_client_relation_pages_are_followed_and_a_cut_page_is_counted() -> None:
    row = ["s", "Method", "s.ts", "CALLS", "t", "Class", "t.ts", "{}"]
    pages = {None: _json(list("abcdefgh"), [row], has_more=True, next_offset=1),
             1: _json(list("abcdefgh"), [[*row[:4], "u", *row[5:]]], has_more=False)}
    client = CBMClient(FakeSession({"query_graph": lambda arguments: pages[arguments.get("offset")]}))
    assert [r[4] for r in client.relations(P, ["s"], ["CALLS"])] == ["t", "u"]
    cut = CBMClient(FakeSession({"query_graph": _json(list("abcdefgh"), [row], has_more=True)}))
    cut.relations(P, ["s"], ["CALLS"])
    assert cut.truncated_responses == 1  # a partial answer, surfaced as such


def test_client_direct_and_path_queries() -> None:
    def reply(arguments: dict[str, Any]) -> dict[str, Any]:
        if "type(r)" in arguments["query"]:
            return _json(["a", "b", "c", "d"], [["x", "USAGE", "y", "{}"]])
        return _json(["a", "b"], [["x", "y"]])

    session = FakeSession({"query_graph": reply})
    client = CBMClient(session)
    assert client.direct_relations(P, ["x"], ["y"]) == [["x", "USAGE", "y", "{}"]]
    assert client.calls_paths(P, ["x"], ["y"], max_hops=20) == [["x", "y"]]
    direct, path = (arguments["query"] for _, arguments in session.calls)
    assert "MATCH (a)-[r]->(b)" in direct and "OR (a.qualified_name IN" in direct
    assert "[:CALLS*1..8]" in path  # bounded, whatever was asked


def test_client_search_code_rows_and_truncation() -> None:
    payload = {"cols": ["qn", "label", "file", "lines", "matches", "source"],
               "rows": [["q", "Method", "f.ts", "1-3", [2], {"source": "a\nb\nc"}]], "has_more": True}
    session = FakeSession({"search_code": payload})
    client = CBMClient(session)
    result = client.search_code(P, "b", path_filter="f.ts")
    assert result["rows"] == [{"qn": "q", "label": "Method", "file": "f.ts", "lines": "1-3", "matches": [2],
                               "source": "a\nb\nc"}]
    assert result["truncated"] and client.truncated_responses == 1
    assert session.calls[0][1]["path_filter"] == "f.ts" and session.calls[0][1]["format"] == "json"


def test_client_graph_capabilities_from_text_and_json() -> None:
    text = {"_text": "node_labels:\n  Class 3\n  Route 2\nedge_types:\n  CALLS 10\n  DECORATES 4\nother:\n  x 1\n"}
    assert CBMClient(FakeSession({"get_graph_schema": text})).graph_capabilities(P) == {
        "labels": {"Class": 3, "Route": 2}, "edge_types": {"CALLS": 10, "DECORATES": 4}}
    structured = {"node_labels": [{"label": "Class", "count": 1}], "edge_types": [{"type": "HANDLES", "count": 2}]}
    assert CBMClient(FakeSession({"get_graph_schema": structured})).graph_capabilities(P) == {
        "labels": {"Class": 1}, "edge_types": {"HANDLES": 2}}


# ----------------------------------------------------------------------------- report


def test_the_report_marks_framework_hops_unresolved_never_as_calls() -> None:
    from sydes.report.verify_terminal import _framework_boundary_blocks

    blocks = _framework_boundary_blocks([
        {"category": "message_dispatch", "status": "unresolved", "source_symbol": "C.remove",
         "via": "CommandBus.execute(T)", "targets": ["S.execute"],
         "facts": [{"provenance": "cbm_decorator", "fact": "S is decorated @CommandHandler(T) (DECORATES)"}],
         "checks": {"direct_relation": "none", "calls_path": "none within 5 hops"}},
        {"category": "callback_registration", "status": "unresolved", "source_symbol": "W.configure",
         "via": "addFilterBefore(f, …)", "targets": ["F.doFilterInternal"], "facts": []},
        {"category": "message_dispatch", "status": "resolved", "source_symbol": "X", "via": "v", "targets": ["Y"]},
    ])
    assert blocks == [
        ["C.remove", "  → CommandBus.execute(T)", "  → [framework dispatch unresolved]",
         "  → candidate: S.execute (@CommandHandler(T))", "  · CBM: no direct relation, no CALLS path (within 5 hops)"],
        ["W.configure", "  → addFilterBefore(f, …): registers F", "  → [framework lifecycle unresolved]",
         "  → candidate: F.doFilterInternal"],
    ]


def test_the_report_shows_a_cbm_route_discrepancy() -> None:
    from types import SimpleNamespace

    from sydes.report.verify_terminal import _route_registration_line

    flow = SimpleNamespace(method="POST", path="/v1/speak", entry_label="POST /v1/speak")
    line = _route_registration_line(flow, [{
        "category": "route_registration", "status": "resolved", "via": "POST /v1/speak", "missing": [],
        "facts": [{"fact": "speak is registered on `router` with path '/speak' (HTTP_CALLS)"},
                  {"fact": "`api_router` is mounted with prefix '/v1'", "file": "app/main.py", "line": 2},
                  {"fact": "discrepancy: CBM POST /speak vs Sydes POST /v1/speak"}]}])
    assert line == ("  registered on `router`, mounted with prefix '/v1' (app/main.py:2) · path composed from literals\n"
                    "  CBM route differs: CBM POST /speak vs Sydes POST /v1/speak")
