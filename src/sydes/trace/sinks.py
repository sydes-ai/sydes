"""Sink normalization helpers for V1 flow expansion output."""

from __future__ import annotations

import re

from sydes.core.models import (
    EvidenceRef,
    SINK_ACTION_CONSUME,
    SINK_ACTION_PUBLISH,
    SINK_ACTION_READ,
    SINK_ACTION_WRITE,
    SINK_KIND_DATABASE,
    SINK_KIND_EXTERNAL_API,
    SINK_KIND_FILE_SINK,
    SINK_KIND_QUEUE,
    SinkCandidate,
    TraceStep,
)

V1_SINK_KINDS = {
    SINK_KIND_DATABASE,
    SINK_KIND_EXTERNAL_API,
    SINK_KIND_QUEUE,
    SINK_KIND_FILE_SINK,
}
V1_SINK_ACTIONS = {
    SINK_ACTION_READ,
    SINK_ACTION_WRITE,
    SINK_ACTION_PUBLISH,
    SINK_ACTION_CONSUME,
}


def _normalize_token(value: str | None) -> str:
    """Normalize freeform tokens into lower snake-like text."""
    if value is None:
        return ""
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def normalize_sink_kind(raw_kind: str | None, *, name: str | None = None) -> str:
    """Map freeform sink labels into the small V1 sink-kind taxonomy."""
    token = _normalize_token(raw_kind)
    name_token = _normalize_token(name)

    if token in V1_SINK_KINDS:
        return token
    if any(marker in token for marker in ("db", "database", "sql", "postgres", "mysql", "mongo", "redis")):
        return SINK_KIND_DATABASE
    if any(marker in token for marker in ("queue", "kafka", "rabbit", "sqs", "pubsub", "topic")):
        return SINK_KIND_QUEUE
    if any(marker in token for marker in ("file", "fs", "storage", "s3", "blob", "bucket")):
        return SINK_KIND_FILE_SINK
    if any(marker in token for marker in ("api", "http", "https", "webhook", "grpc", "external")):
        return SINK_KIND_EXTERNAL_API

    if any(marker in name_token for marker in ("db", "database", "sql", "table", "collection")):
        return SINK_KIND_DATABASE
    if any(marker in name_token for marker in ("queue", "topic", "stream", "kafka", "rabbit", "sqs", "pubsub")):
        return SINK_KIND_QUEUE
    if any(marker in name_token for marker in ("file", "storage", "bucket", "s3")):
        return SINK_KIND_FILE_SINK
    if any(marker in name_token for marker in ("api", "http", "webhook", "service", "client")):
        return SINK_KIND_EXTERNAL_API
    return "unknown"


def normalize_sink_action(raw_action: str | None, *, kind: str | None = None) -> str | None:
    """Map freeform sink actions into V1 read/write/publish/consume actions."""
    token = _normalize_token(raw_action)
    kind_token = _normalize_token(kind)

    if token in V1_SINK_ACTIONS:
        return token
    if any(marker in token for marker in ("write", "insert", "update", "delete", "save", "upsert", "create", "upload")):
        return SINK_ACTION_WRITE
    if any(marker in token for marker in ("read", "select", "query", "fetch", "get", "load")):
        return SINK_ACTION_READ
    if any(marker in token for marker in ("publish", "enqueue", "push", "produce", "send", "emit")):
        return SINK_ACTION_PUBLISH
    if any(marker in token for marker in ("consume", "dequeue", "poll", "read_message", "subscribe")):
        return SINK_ACTION_CONSUME

    if kind_token == SINK_KIND_QUEUE:
        return None
    if kind_token in {SINK_KIND_DATABASE, SINK_KIND_FILE_SINK, SINK_KIND_EXTERNAL_API}:
        return None
    return None


def normalize_sink_candidate(candidate: SinkCandidate) -> SinkCandidate:
    """Normalize one sink candidate while preserving original target naming."""
    normalized_kind = normalize_sink_kind(candidate.kind, name=candidate.name)
    normalized_action = normalize_sink_action(candidate.action, kind=normalized_kind)
    return SinkCandidate(
        kind=normalized_kind,
        name=candidate.name,
        repo=candidate.repo,
        service=candidate.service,
        file=candidate.file,
        symbol=candidate.symbol,
        action=normalized_action,
        evidence=candidate.evidence,
        confidence=candidate.confidence,
        status=candidate.status,
    )


def normalize_sink_candidates(candidates: list[SinkCandidate]) -> list[SinkCandidate]:
    """Normalize sink candidates into V1 kinds/actions when possible."""
    return [normalize_sink_candidate(candidate) for candidate in candidates]


def _step_text(step: TraceStep) -> str:
    """Build normalized step text for lightweight sink-intent matching."""
    parts = [step.name or "", step.symbol or ""]
    return " ".join(parts).strip().lower()


def _is_route_declaration_step_text(text: str) -> bool:
    """Detect route declaration-like step text that should not drive sink recovery."""
    route_patterns = (
        r"^\s*@\s*[a-z_][a-z0-9_\.]*\s*\.\s*(get|post|put|patch|delete|route)\s*\(",
        r"^\s*@\s*(getmapping|postmapping|putmapping|deletemapping|patchmapping|requestmapping)\b",
        r"^\s*(app|router|blueprint|bp)\s*\.\s*(get|post|put|patch|delete|route)\s*\(",
    )
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in route_patterns)


def _is_literal_database_write_step(text: str) -> bool:
    """Detect obvious literal DB write operations from retained flow steps."""
    literal_signals = (
        "db.add",
        "db.commit",
        "db.execute",
        "db.refresh",
        "repository.save",
    )
    if any(signal in text for signal in literal_signals):
        return True
    if re.search(r"\b(insert|update|delete)\b", text):
        return True
    return False


def _derive_sink_from_step(step: TraceStep) -> SinkCandidate | None:
    """Derive a conservative database sink candidate from one literal flow step."""
    text = _step_text(step)
    if not text:
        return None
    if _is_route_declaration_step_text(text):
        return None
    if not _is_literal_database_write_step(text):
        return None

    evidence: list[EvidenceRef] = []
    if step.file:
        evidence.append(
            EvidenceRef(
                file=step.file,
                symbol=step.symbol,
                label=f"derived-from-step:{step.name}",
            )
        )
    return normalize_sink_candidate(
        SinkCandidate(
            kind=SINK_KIND_DATABASE,
            name=SINK_KIND_DATABASE,
            repo=step.repo,
            service=step.service,
            file=step.file,
            symbol=step.symbol,
            action=SINK_ACTION_WRITE,
            evidence=evidence,
            confidence=step.confidence,
            status="inferred",
        )
    )


def derive_sink_candidates_from_steps(steps: list[TraceStep]) -> list[SinkCandidate]:
    """Derive sink candidates from retained flow steps using simple pattern matching."""
    derived: list[SinkCandidate] = []
    for step in steps:
        candidate = _derive_sink_from_step(step)
        if candidate is not None:
            derived.append(candidate)
    return derived


def _sink_dedupe_key(sink: SinkCandidate) -> tuple[str, str, str, str]:
    """Create a stable key for coarse sink deduplication."""
    return (
        sink.kind or "unknown",
        sink.action or "",
        sink.file or "",
        sink.repo or "",
    )


def merge_and_dedupe_sinks(
    explicit_sinks: list[SinkCandidate],
    derived_sinks: list[SinkCandidate],
) -> list[SinkCandidate]:
    """Merge explicit and derived sinks, preserving explicit candidates on duplicates."""
    merged: list[SinkCandidate] = []
    by_key: dict[tuple[str, str, str, str], int] = {}

    for sink in explicit_sinks:
        normalized = normalize_sink_candidate(sink)
        key = _sink_dedupe_key(normalized)
        by_key[key] = len(merged)
        merged.append(normalized)

    for sink in derived_sinks:
        normalized = normalize_sink_candidate(sink)
        key = _sink_dedupe_key(normalized)
        existing_index = by_key.get(key)
        if existing_index is None:
            by_key[key] = len(merged)
            merged.append(normalized)
            continue
        existing = merged[existing_index]
        if existing.status == "inferred" and normalized.status != "inferred":
            merged[existing_index] = normalized

    return merged
