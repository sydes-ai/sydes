"""Tests for V1 sink taxonomy normalization helpers."""

from sydes.core.models import SinkCandidate, TraceStep
from sydes.trace.sinks import (
    derive_sink_candidates_from_steps,
    merge_and_dedupe_sinks,
    normalize_sink_action,
    normalize_sink_candidate,
    normalize_sink_kind,
)


def test_normalize_sink_kind_maps_representative_variants() -> None:
    """Common sink-like labels should map into the V1 sink taxonomy."""
    assert normalize_sink_kind("sql-db") == "database"
    assert normalize_sink_kind("webhook") == "external_api"
    assert normalize_sink_kind("kafka-topic") == "queue"
    assert normalize_sink_kind("s3") == "file_sink"
    assert normalize_sink_kind("mystery-target") == "unknown"


def test_normalize_sink_action_maps_common_action_variants() -> None:
    """Action labels should normalize to read/write/publish/consume when inferable."""
    assert normalize_sink_action("select") == "read"
    assert normalize_sink_action("upsert") == "write"
    assert normalize_sink_action("enqueue") == "publish"
    assert normalize_sink_action("dequeue") == "consume"
    assert normalize_sink_action("mutate") is None


def test_normalize_sink_candidate_preserves_name_and_maps_kind_action() -> None:
    """Sink candidate normalization should keep original target name while standardizing taxonomy."""
    candidate = SinkCandidate(
        kind="postgres",
        name="orders_db",
        action="insert",
    )

    normalized = normalize_sink_candidate(candidate)

    assert normalized.name == "orders_db"
    assert normalized.kind == "database"
    assert normalized.action == "write"


def test_normalize_sink_candidate_maps_queue_external_and_file_variants() -> None:
    """Additional sink families should normalize into V1 taxonomy consistently."""
    queue_candidate = SinkCandidate(kind="kafka-topic", name="order-events", action="enqueue")
    external_candidate = SinkCandidate(kind="http-client", name="payments-service", action="fetch")
    file_candidate = SinkCandidate(kind="s3", name="invoice-bucket", action="upload")

    normalized_queue = normalize_sink_candidate(queue_candidate)
    normalized_external = normalize_sink_candidate(external_candidate)
    normalized_file = normalize_sink_candidate(file_candidate)

    assert normalized_queue.kind == "queue"
    assert normalized_queue.action == "publish"
    assert normalized_external.kind == "external_api"
    assert normalized_external.action == "read"
    assert normalized_file.kind == "file_sink"
    assert normalized_file.action == "write"


def test_derive_sink_candidates_from_steps_recovers_database_write() -> None:
    """Step-derived sink recovery should detect obvious db write operations."""
    steps = [
        TraceStep(kind="internal_step", name="db.add", repo="api", file="src/routes.py"),
        TraceStep(kind="internal_step", name="db.commit", repo="api", file="src/routes.py"),
        TraceStep(kind="internal_step", name="db.refresh", repo="api", file="src/routes.py"),
    ]

    sinks = derive_sink_candidates_from_steps(steps)

    assert sinks
    assert all(sink.kind == "database" for sink in sinks)
    assert all(sink.action == "write" for sink in sinks)


def test_merge_and_dedupe_sinks_preserves_explicit_and_dedupes_derived() -> None:
    """Explicit sinks should be preserved while equivalent derived sinks are deduped."""
    explicit = [
        SinkCandidate(
            kind="database",
            action="write",
            name="database",
            file="src/routes.py",
            status="confirmed",
        )
    ]
    derived = [
        SinkCandidate(
            kind="database",
            action="write",
            name="database",
            file="src/routes.py",
            status="inferred",
        ),
        SinkCandidate(
            kind="queue",
            action="publish",
            name="queue",
            file="src/routes.py",
            status="inferred",
        ),
    ]

    merged = merge_and_dedupe_sinks(explicit, derived)

    assert len(merged) == 2
    database = next(item for item in merged if item.kind == "database")
    assert database.status == "confirmed"
    queue = next(item for item in merged if item.kind == "queue")
    assert queue.action == "publish"


def test_derive_sink_candidates_from_steps_ignores_route_declaration_like_steps() -> None:
    """Route declaration syntax should never produce recovered sink candidates."""
    steps = [
        TraceStep(kind="internal_step", name="@app.get('/users/')", repo="api", file="src/main.py"),
        TraceStep(kind="internal_step", name="@router.post('/users/')", repo="api", file="src/main.py"),
        TraceStep(kind="internal_step", name="@GetMapping('/db/books')", repo="api", file="src/BooksController.java"),
    ]

    sinks = derive_sink_candidates_from_steps(steps)

    assert sinks == []


def test_merge_and_dedupe_sinks_uses_kind_action_file_repo_key() -> None:
    """Derived sink dedupe should collapse same kind/action/file/repo even with different names."""
    derived = [
        SinkCandidate(kind="database", action="write", name="database", file="src/routes.py", repo="api"),
        SinkCandidate(kind="database", action="write", name="orders_db", file="src/routes.py", repo="api"),
    ]

    merged = merge_and_dedupe_sinks([], derived)

    assert len(merged) == 1
    assert merged[0].kind == "database"
    assert merged[0].action == "write"
    assert merged[0].file == "src/routes.py"
    assert merged[0].repo == "api"
