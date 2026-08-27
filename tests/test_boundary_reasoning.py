"""Increment D: evidence-backed boundary inference.

When deterministic discovery cannot ESTABLISH a boundary, one bounded LLM
call reasons over a compact packet of real evidence and may propose
INFERRED boundaries. These tests pin the epistemic separation that makes
that safe: an inferred boundary is evidence-backed inference, never
structural proof, and it can never reach verdict math, obligations, or
`AffectedFlow`.
"""

from __future__ import annotations

import json
from pathlib import Path

from sydes.code_intelligence.base import StructuralFacts
from sydes.impact.models import (
    AffectedEntrypoint,
    ImpactResult,
    UnresolvedImpact,
)
from sydes.llm.client import LLMClientError, LLMRequest, LLMResponse, TracingLLMClient
from sydes.report.verify_terminal import render_verify_change_terminal
from sydes.verify.boundary_reasoning import (
    build_reasoning_packet,
    has_reasonable_evidence,
    infer_boundaries,
)
from sydes.verify.models import (
    AffectedBoundary,
    ChangedSymbol,
    ChangeSet,
    ChangeVerificationResult,
)

REPO = "app"


def facts(files: list[dict] | None = None) -> StructuralFacts:
    return StructuralFacts(
        symbol_index={"repos": [{"repo": REPO, "files": files or []}]},
        provides_call_graph=True,
        backend="cbm",
    )


def symbol_file(path: str, names: list[str]) -> dict:
    return {
        "path": path,
        "symbols": [
            {"name": name, "kind": "function", "start_line": 1, "end_line": 5}
            for name in names
        ],
    }


def changed_symbol(name: str, *, file: str = "app/svc.py") -> ChangedSymbol:
    return ChangedSymbol(id=f"{REPO}:{file}:{name}", repo=REPO, file=file, name=name)


def change_set(*names: str, file: str = "app/svc.py") -> ChangeSet:
    return ChangeSet(
        base="main", head="abc", files=[],
        symbols=[changed_symbol(name, file=file) for name in names],
    )


def entrypoint_result(*symbols: tuple[str, str]) -> ImpactResult:
    """An `ImpactResult` whose accepted entrypoints supply the candidates."""
    return ImpactResult(
        affected=[
            AffectedEntrypoint(
                repo=REPO, symbol=symbol, qualified_name=f"app.{symbol}", file=file,
            )
            for symbol, file in symbols
        ],
    )


class CountingClient:
    """Returns one scripted response per call; records every call made."""

    def __init__(self, payload: dict | str) -> None:
        self._text = payload if isinstance(payload, str) else json.dumps(payload)
        self.calls = 0
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        self.requests.append(request)
        return LLMResponse(text=self._text)


class FailingClient:
    def generate(self, request: LLMRequest) -> LLMResponse:
        raise LLMClientError("provider unreachable")


def _response(*boundaries: dict) -> dict:
    return {"inferred_boundaries": list(boundaries)}


def _boundary(kind: str, **overrides) -> dict:
    payload = {
        "kind": kind, "subtype": None, "label": f"a {kind} behavior",
        "symbol": "handler", "file": "app/svc.py", "changed_symbols": ["helper"],
        "reason": "the supplied source shows this surface is affected",
        "supporting_evidence": ["candidate symbol handler"],
        "uncertainty": "complete caller coverage not established",
        "confidence": 0.6,
    }
    payload.update(overrides)
    return payload


def run(client, *, impact_result=None, change=None, deterministic=None,
        semantic_analysis=None, structural=None):
    return infer_boundaries(
        change=change or change_set("helper"),
        impact_result=impact_result or entrypoint_result(("handler", "app/svc.py")),
        deterministic_boundaries=deterministic or [],
        semantic_analysis=semantic_analysis,
        facts=structural or facts(),
        repo=REPO,
        llm_client=client,
    )


# --------------------------------------------------------------------------
# 1. One call per change
# --------------------------------------------------------------------------

def test_multiple_candidates_produce_exactly_one_llm_call() -> None:
    impact_result = entrypoint_result(
        ("handler", "app/a.py"), ("service", "app/b.py"), ("worker", "app/c.py"),
    )
    impact_result.unresolved = [
        UnresolvedImpact(repo=REPO, symbol="helper", reason="no_entrypoint_reached"),
    ]
    client = CountingClient(_response(_boundary("api")))

    boundaries, _notes = run(client, impact_result=impact_result,
                             change=change_set("helper", "other"))

    assert client.calls == 1
    assert len(boundaries) == 1


# --------------------------------------------------------------------------
# 2-4. API / callable / async inference
# --------------------------------------------------------------------------

def test_api_boundary_can_be_inferred_from_partial_evidence() -> None:
    boundaries, _notes = run(CountingClient(_response(
        _boundary("api", subtype="route_registration", label="public self-service flow"),
    )))

    assert len(boundaries) == 1
    assert boundaries[0].kind == "api"
    assert boundaries[0].subtype == "route_registration"
    assert boundaries[0].status == "inferred"


def test_callable_boundary_can_be_inferred_without_any_http() -> None:
    boundaries, _notes = run(CountingClient(_response(
        _boundary("callable", subtype="domain", label="order expiration calculation"),
    )))

    assert len(boundaries) == 1
    assert boundaries[0].kind == "callable"
    assert boundaries[0].subtype == "domain"
    assert boundaries[0].status == "inferred"


def test_async_boundary_can_be_inferred_from_specific_event_evidence() -> None:
    boundaries, _notes = run(CountingClient(_response(
        _boundary("async", subtype="event_handler", label="order-created reaction"),
    )))

    assert len(boundaries) == 1
    assert boundaries[0].kind == "async"
    assert boundaries[0].subtype == "event_handler"


# --------------------------------------------------------------------------
# 5. Prompt-contract safety rules are actually stated
# --------------------------------------------------------------------------

def test_prompt_forbids_inventing_edges_routes_and_generic_decorator_async() -> None:
    """These rules cannot be enforced deterministically after the fact — an
    inference is only as safe as the contract that produced it — so the
    contract itself is pinned here."""
    from sydes.verify.boundary_reasoning import _SYSTEM_PROMPT

    assert "NEVER invent a structural edge" in _SYSTEM_PROMPT
    assert "NEVER claim a concrete route method/path" in _SYSTEM_PROMPT
    assert "NEVER treat test-only code as a production boundary" in _SYSTEM_PROMPT
    assert "Do not infer `async` merely because a symbol is decorated" in _SYSTEM_PROMPT
    assert "return an EMPTY list" in _SYSTEM_PROMPT


def test_an_unknown_kind_is_dropped_rather_than_coerced() -> None:
    boundaries, _notes = run(CountingClient(_response(_boundary("database"))))
    assert boundaries == []


def test_a_boundary_with_no_stated_reason_is_dropped() -> None:
    """Rule 7 in practice: an inference with no stated basis is a guess."""
    boundaries, _notes = run(CountingClient(_response(_boundary("api", reason=""))))
    assert boundaries == []


# --------------------------------------------------------------------------
# Increment D.1: calibration — boundary-crossing discipline, fixed subtype
# vocabulary, and a real production-evidence requirement.
# --------------------------------------------------------------------------

def test_1_ui_surface_alone_does_not_survive_as_an_api_boundary() -> None:
    """The exact real-PR failure shape: a UI/template rendering change, with
    no candidate/source/structural evidence behind it, must not survive —
    regardless of what confident label or subtype the model attaches."""
    boundaries, _notes = run(CountingClient(_response({
        "kind": "api", "subtype": "http_handler_ui", "label": "UI form rendering",
        "symbol": "", "file": "", "changed_symbols": [],
        "reason": "the template now renders new fields",
        "supporting_evidence": ["change_summary mentions the UI now renders new fields"],
        "uncertainty": "", "confidence": 0.8,
    })))

    assert boundaries == []


def test_2_unsupported_subtype_is_normalized_not_invented(monkeypatch) -> None:
    """`callable/form_validation` is not in the fixed vocabulary. With real
    grounding evidence present, the KIND survives but the subtype is
    normalized to `None` rather than passed through verbatim."""
    boundaries, _notes = run(CountingClient(_response(
        _boundary("callable", subtype="form_validation"),
    )))

    assert len(boundaries) == 1
    assert boundaries[0].kind == "callable"
    assert boundaries[0].subtype is None


def test_3_api_http_subtype_survives() -> None:
    boundaries, _notes = run(CountingClient(_response(_boundary("api", subtype="http"))))
    assert len(boundaries) == 1
    assert boundaries[0].subtype == "http"


def test_4_api_route_registration_subtype_survives() -> None:
    boundaries, _notes = run(CountingClient(_response(
        _boundary("api", subtype="route_registration"),
    )))
    assert len(boundaries) == 1
    assert boundaries[0].subtype == "route_registration"


def test_5_callable_service_subtype_survives() -> None:
    boundaries, _notes = run(CountingClient(_response(_boundary("callable", subtype="service"))))
    assert len(boundaries) == 1
    assert boundaries[0].subtype == "service"


def test_6_callable_domain_subtype_survives() -> None:
    boundaries, _notes = run(CountingClient(_response(_boundary("callable", subtype="domain"))))
    assert len(boundaries) == 1
    assert boundaries[0].subtype == "domain"


def test_7_async_event_handler_survives_only_with_specific_evidence() -> None:
    boundaries, _notes = run(CountingClient(_response(_boundary(
        "async", subtype="event_handler",
        supporting_evidence=["candidate symbol handler registered as a signal receiver"],
    ))))
    assert len(boundaries) == 1
    assert boundaries[0].kind == "async"
    assert boundaries[0].subtype == "event_handler"


def test_8_kind_subtype_mismatch_is_normalized_conservatively() -> None:
    """`api/service` — `service` belongs to `callable`'s vocabulary, not
    `api`'s. The centralized validator drops it rather than passing through
    a subtype that would misdescribe the kind."""
    boundaries, _notes = run(CountingClient(_response(_boundary("api", subtype="service"))))
    assert len(boundaries) == 1
    assert boundaries[0].kind == "api"
    assert boundaries[0].subtype is None


def test_9_repo_context_and_semantic_summary_alone_cannot_produce_a_boundary() -> None:
    """A candidate exists (so the call happens), but this specific proposed
    boundary's own supporting_evidence cites only repo_context/semantic
    prose — no concrete production fact. It must not survive even though
    the call was made and other, well-grounded boundaries still could."""
    boundaries, _notes = run(CountingClient(_response({
        "kind": "callable", "subtype": "service", "label": "backend service change",
        "symbol": "", "file": "", "changed_symbols": [],
        "reason": "the change touches backend code",
        "supporting_evidence": [
            "repo_context: packages/core is a backend package",
            "change_summary: the PR affects backend behavior",
        ],
        "uncertainty": "", "confidence": 0.7,
    })))

    assert boundaries == []


def test_10_vague_evidence_rejected_concrete_evidence_survives() -> None:
    """Two boundaries in the same response: one backed only by vague prose,
    one citing the real supplied candidate symbol. Only the second must
    survive — this is the evidence-quality gate operating per-boundary."""
    boundaries, _notes = run(CountingClient(_response(
        {
            "kind": "callable", "subtype": "service", "label": "vague claim",
            "symbol": "", "file": "", "changed_symbols": [],
            "reason": "seems important", "supporting_evidence": ["the change feels significant"],
            "uncertainty": "", "confidence": 0.5,
        },
        _boundary("callable", subtype="service", label="a real grounded surface"),
    )))

    assert len(boundaries) == 1
    assert boundaries[0].label == "a real grounded surface"


def test_11_multiple_related_symbols_may_form_one_grouped_boundary() -> None:
    """No clustering logic — this is a parser/report support test: a single
    inferred boundary may legitimately list several `changed_symbols`."""
    boundaries, _notes = run(CountingClient(_response(_boundary(
        "callable", subtype="service",
        changed_symbols=["helper", "helper_two", "helper_three"],
    ))))

    assert len(boundaries) == 1
    assert boundaries[0].changed_symbols == ["helper", "helper_two", "helper_three"]


def test_centralized_validator_covers_every_kind() -> None:
    from sydes.verify.boundary_reasoning import _normalize_subtype

    assert _normalize_subtype("api", "http") == "http"
    assert _normalize_subtype("api", "route_registration") == "route_registration"
    assert _normalize_subtype("api", "made_up") is None
    assert _normalize_subtype("callable", "service") == "service"
    assert _normalize_subtype("callable", "domain") == "domain"
    assert _normalize_subtype("callable", "public_callable") == "public_callable"
    assert _normalize_subtype("callable", "http") is None
    assert _normalize_subtype("async", "event_handler") == "event_handler"
    assert _normalize_subtype("async", "scheduled_job") == "scheduled_job"
    assert _normalize_subtype("async", "queue_consumer") == "queue_consumer"
    assert _normalize_subtype("unknown", "anything") is None
    assert _normalize_subtype("api", None) is None


def test_prompt_states_the_boundary_crossing_criterion_and_fixed_vocabulary() -> None:
    from sydes.verify.boundary_reasoning import _SYSTEM_PROMPT

    assert "What crosses this boundary?" in _SYSTEM_PROMPT
    assert "template renders new form fields" in _SYSTEM_PROMPT
    assert "NOT automatically" in _SYSTEM_PROMPT
    assert "http | route_registration" in _SYSTEM_PROMPT
    assert "service | domain | public_callable" in _SYSTEM_PROMPT
    assert "event_handler | scheduled_job | queue_consumer" in _SYSTEM_PROMPT


# --------------------------------------------------------------------------
# 6-7. Test / main candidate filtering
# --------------------------------------------------------------------------

def test_test_symbols_are_never_offered_as_production_candidates() -> None:
    impact_result = entrypoint_result(
        ("test_get_thing", "app/tests/thing_test.py"),
        ("test_inline", "app/svc.py"),
    )
    packet = build_reasoning_packet(
        change=change_set("helper"), impact_result=impact_result,
        deterministic_boundaries=[], semantic_analysis=None,
        facts=facts(), repo=REPO,
    )

    assert packet["boundary_candidates"] == []
    assert not has_reasonable_evidence(packet)


def test_main_is_never_offered_as_a_production_candidate() -> None:
    impact_result = entrypoint_result(("main", "app/cmd/bin.py"))
    packet = build_reasoning_packet(
        change=change_set("helper"), impact_result=impact_result,
        deterministic_boundaries=[], semantic_analysis=None,
        facts=facts(), repo=REPO,
    )

    assert packet["boundary_candidates"] == []


def test_production_candidates_survive_alongside_filtered_ones() -> None:
    impact_result = entrypoint_result(
        ("test_thing", "app/tests/thing_test.py"),
        ("main", "app/cmd/bin.py"),
        ("real_handler", "app/svc.py"),
    )
    packet = build_reasoning_packet(
        change=change_set("helper"), impact_result=impact_result,
        deterministic_boundaries=[], semantic_analysis=None,
        facts=facts(), repo=REPO,
    )

    assert [item["symbol"] for item in packet["boundary_candidates"]] == ["real_handler"]


# --------------------------------------------------------------------------
# 8-9. Insufficient evidence / semantic-only isolation
# --------------------------------------------------------------------------

def test_no_candidates_means_no_llm_call_at_all() -> None:
    client = CountingClient(_response(_boundary("api")))
    boundaries, notes = run(client, impact_result=ImpactResult())

    assert client.calls == 0
    assert boundaries == []
    assert any("skipped" in note for note in notes)


def test_semantic_hypothesis_alone_cannot_create_an_inferred_boundary() -> None:
    """Increment D is evidence-BACKED, not semantic boundary guessing: a
    confident semantic reading with no structural or source candidate
    behind it must not even reach the model."""
    from sydes.verify.models import ChangeSemanticAnalysis, SemanticBehaviorChange

    analysis = ChangeSemanticAnalysis(
        change_summary="Order expiration now varies by sales channel.",
        behavior_changes=[
            SemanticBehaviorChange(description="channel-specific expiry", confidence=0.9),
        ],
        likely_boundary_types=["api", "callable"],
    )
    client = CountingClient(_response(_boundary("api")))
    boundaries, notes = run(client, impact_result=ImpactResult(), semantic_analysis=analysis)

    assert client.calls == 0
    assert boundaries == []
    assert any("skipped" in note for note in notes)


# --------------------------------------------------------------------------
# 10. Deterministic precedence
# --------------------------------------------------------------------------

def test_an_established_boundary_is_never_duplicated_as_inferred() -> None:
    established = AffectedBoundary(
        id="boundary:api:app:handler:app/svc.py", kind="api", subtype="http",
        repo=REPO, file="app/svc.py", symbol="handler", label="GET /x", status="proven",
    )
    client = CountingClient(_response(
        _boundary("api", symbol="handler", label="the same routing surface"),
        _boundary("callable", symbol="other_service", label="a genuinely new surface"),
    ))

    boundaries, _notes = run(client, deterministic=[established])

    assert len(boundaries) == 1
    assert boundaries[0].symbol == "other_service"
    assert boundaries[0].kind == "callable"


# --------------------------------------------------------------------------
# 11. Failure behavior
# --------------------------------------------------------------------------

def test_provider_failure_yields_no_boundary_and_never_raises() -> None:
    boundaries, notes = run(FailingClient())

    assert boundaries == []
    assert any("unavailable" in note for note in notes)


def test_malformed_json_yields_no_boundary_and_never_raises() -> None:
    boundaries, notes = run(CountingClient("not json at all"))

    assert boundaries == []
    assert any("unavailable" in note for note in notes)


# --------------------------------------------------------------------------
# 12. Verification isolation — the critical soundness test
# --------------------------------------------------------------------------

def test_inferred_boundaries_cannot_reach_flows_obligations_impacts_or_verdict() -> None:
    """An inferred boundary must be structurally incapable of influencing
    verification. Built directly onto a result and run through the real
    summary computation."""
    from sydes.verify.analyzer import _compute_summary

    result = ChangeVerificationResult(change=change_set("helper"))
    result.affected_boundaries = [
        AffectedBoundary(
            id="boundary:inferred:api:app:handler:0", kind="api", subtype="http",
            repo=REPO, file="app/svc.py", symbol="handler",
            label="a confidently inferred routing surface",
            status="inferred", reason="looks like routing", llm_confidence=0.99,
        ),
    ]
    result.summary = _compute_summary(result)

    assert result.affected_flows == []
    assert result.accepted_impacts == []
    assert result.summary.counts.obligations == 0
    assert result.summary.counts.impacts_proven == 0
    assert result.summary.counts.impacts_inferred == 0
    assert result.summary.verdict != "VERIFIED"


def test_an_inferred_boundary_does_not_resolve_an_unresolved_changed_symbol() -> None:
    result = ChangeVerificationResult(change=change_set("helper"))
    result.unresolved_changed_symbols = 1
    result.affected_boundaries = [
        AffectedBoundary(
            id="boundary:inferred:callable:app:helper:0", kind="callable",
            repo=REPO, symbol="helper", label="inferred surface", status="inferred",
        ),
    ]

    assert result.unresolved_changed_symbols == 1


# --------------------------------------------------------------------------
# 13. Tracing
# --------------------------------------------------------------------------

def test_exactly_one_boundary_reasoning_stage_call_is_traced(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("SYDES_TRACE_DIR", str(tmp_path / "traces"))
    inner = CountingClient(_response(_boundary("api")))
    traced = TracingLLMClient(
        inner, stage="boundary_reasoning", provider="fake", model="fake-model",
    )
    impact_result = entrypoint_result(("handler", "app/a.py"), ("service", "app/b.py"))

    boundaries, _notes = run(traced, impact_result=impact_result)

    assert boundaries
    assert inner.calls == 1
    records = [
        json.loads(line)
        for line in (tmp_path / "traces" / "llm_calls.jsonl").read_text().splitlines()
    ]
    stage_calls = [item for item in records if item["stage"] == "boundary_reasoning"]
    assert len(stage_calls) == 1
    assert stage_calls[0]["success"] is True


def test_the_call_site_requests_the_boundary_reasoning_stage(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Stub:
        def generate(self, request: LLMRequest) -> LLMResponse:
            return LLMResponse(text=json.dumps(_response()))

    def fake_factory(**kwargs):
        captured.update(kwargs)
        return Stub()

    monkeypatch.setattr(
        "sydes.verify.boundary_reasoning.create_default_llm_client", fake_factory,
    )
    infer_boundaries(
        change=change_set("helper"),
        impact_result=entrypoint_result(("handler", "app/svc.py")),
        deterministic_boundaries=[], semantic_analysis=None,
        facts=facts(), repo=REPO,
    )

    assert captured.get("stage") == "boundary_reasoning"
    assert captured.get("temperature") is None


# --------------------------------------------------------------------------
# 14. Serialization / reporting
# --------------------------------------------------------------------------

def test_established_and_inferred_boundaries_render_and_serialize_distinctly() -> None:
    result = ChangeVerificationResult(change=change_set("helper"))
    result.affected_boundaries = [
        AffectedBoundary(
            id="boundary:async:app:cleanup:app/jobs.py", kind="async",
            subtype="scheduled_job", repo=REPO, file="app/jobs.py", symbol="cleanup",
            label="app.cleanup", evidence=["calls:helper -> calls:cleanup"],
            evidence_strength="strong", status="proven",
        ),
        AffectedBoundary(
            id="boundary:inferred:callable:app:set_expires:0", kind="callable",
            subtype="domain", repo=REPO, file="app/orders.py", symbol="set_expires",
            label="order expiration calculation", status="inferred",
            reason="changed Order.set_expires with related callers",
            evidence=["candidate symbol set_expires"],
            uncertainty="complete production caller coverage",
            llm_confidence=0.6,
        ),
    ]

    dumped = result.model_dump()
    statuses = [item["status"] for item in dumped["affected_boundaries"]]
    assert statuses == ["proven", "inferred"]
    assert dumped["affected_boundaries"][1]["reason"]
    assert dumped["affected_boundaries"][1]["llm_confidence"] == 0.6

    report = render_verify_change_terminal(result)
    # The established one renders in System impact; the inferred one only in
    # its own clearly-labelled section, never in the same visual language.
    assert "async · app.cleanup" in report
    assert "Inferred boundaries" in report
    assert "INFERRED · callable/domain · order expiration calculation" in report
    assert "Uncertain: complete production caller coverage" in report
    assert report.index("System impact") < report.index("Inferred boundaries")

    verbose = render_verify_change_terminal(result, verbose=True)
    assert "ESTABLISHED" in verbose
    assert "INFERRED" in verbose


# --------------------------------------------------------------------------
# Packet shape / budget
# --------------------------------------------------------------------------

def test_packet_carries_the_documented_evidence_fields_and_stays_bounded() -> None:
    impact_result = entrypoint_result(*[(f"handler_{i}", "app/svc.py") for i in range(40)])
    impact_result.unresolved = [
        UnresolvedImpact(repo=REPO, symbol="helper", reason="no_entrypoint_reached"),
    ]
    packet = build_reasoning_packet(
        change=change_set(*[f"sym_{i}" for i in range(40)]),
        impact_result=impact_result, deterministic_boundaries=[],
        semantic_analysis=None, facts=facts(), repo=REPO,
    )

    assert set(packet) == {
        "version", "change_summary", "behavior_changes", "changed_symbols",
        "deterministic_boundaries", "boundary_candidates", "accepted_impacts",
        "unresolved_changed_symbols", "relevant_source_snippets", "repo_context",
        "uncertainties",
    }
    # No profile supplied — the field exists but stays empty, so boundary
    # reasoning behaves exactly as it did before Increment B.
    assert packet["repo_context"] == []
    assert len(packet["boundary_candidates"]) <= 12
    assert len(packet["changed_symbols"]) <= 20
    assert len(packet["accepted_impacts"]) <= 12


def test_packet_supplies_already_established_boundaries_to_avoid_reproposal() -> None:
    established = AffectedBoundary(
        id="b1", kind="api", subtype="http", repo=REPO, label="GET /x", status="proven",
    )
    packet = build_reasoning_packet(
        change=change_set("helper"),
        impact_result=entrypoint_result(("handler", "app/svc.py")),
        deterministic_boundaries=[established], semantic_analysis=None,
        facts=facts(), repo=REPO,
    )

    assert packet["deterministic_boundaries"] == [
        {"kind": "api", "subtype": "http", "label": "GET /x"},
    ]
