"""Tests for V1 sink taxonomy normalization helpers."""

from sydes.core.models import SinkCandidate
from sydes.trace.sinks import (
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
