"""Deterministic detection of queue/event producers and consumers.

Sydes' existing sink taxonomy classifies a step as `queue`, but does not name
the topic or connect a producer to its consumer. Change verification needs both:
"this change publishes `refund.created`" is only interesting when the consumer
on the other side can be named too.

Detection is literal-only. A topic is recorded when its name appears as a string
literal at the call site; dynamic topic names are reported as unnamed signals
rather than guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from sydes.verify.repo_scan import RepoScan, ScannedFile

PUBLISH = "publish"
CONSUME = "consume"


@dataclass(slots=True)
class EventSignal:
    """One producer or consumer site found in source."""

    repo: str
    file: str
    line: int
    action: str
    technology: str
    topic: str | None
    snippet: str
    symbol: str | None = None

    @property
    def label(self) -> str:
        """Human-facing name for the event/topic."""
        return self.topic or f"{self.technology} {self.action}"


_LITERAL = r"['\"](?P<topic>[A-Za-z0-9_.:\-/{}]+)['\"]"

# (technology, action, regex). Each pattern optionally captures a `topic` group.
_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    # Kafka (kafkajs / node-rdkafka / kafka-python / confluent)
    ("kafka", PUBLISH, re.compile(rf"\btopic\s*[:=]\s*{_LITERAL}")),
    ("kafka", PUBLISH, re.compile(rf"producer\s*\.\s*(?:send|produce)\s*\(\s*{_LITERAL}")),
    ("kafka", CONSUME, re.compile(rf"consumer\s*\.\s*subscribe\s*\(\s*(?:\{{\s*topics?\s*:\s*\[?\s*)?{_LITERAL}")),
    ("kafka", CONSUME, re.compile(rf"KafkaConsumer\s*\(\s*{_LITERAL}")),
    ("kafka", CONSUME, re.compile(rf"@KafkaListener\s*\(\s*topics\s*=\s*\{{?\s*{_LITERAL}")),
    # RabbitMQ (amqplib / pika)
    ("rabbitmq", PUBLISH, re.compile(rf"(?:publish|sendToQueue)\s*\(\s*{_LITERAL}")),
    ("rabbitmq", PUBLISH, re.compile(rf"basic_publish\s*\([^)]*routing_key\s*=\s*{_LITERAL}")),
    ("rabbitmq", CONSUME, re.compile(rf"(?:consume|basic_consume)\s*\(\s*(?:queue\s*=\s*)?{_LITERAL}")),
    # AWS SQS / SNS / EventBridge
    ("sqs", PUBLISH, re.compile(r"\b(?:sendMessage|send_message)\s*\(")),
    ("sqs", CONSUME, re.compile(r"\b(?:receiveMessage|receive_message)\s*\(")),
    ("sns", PUBLISH, re.compile(r"TopicArn\s*[:=]")),
    ("eventbridge", PUBLISH, re.compile(r"\b(?:putEvents|put_events)\s*\(")),
    # Redis pub/sub and streams
    ("redis", PUBLISH, re.compile(rf"\b(?:redis|client|pub)\s*\.\s*publish\s*\(\s*{_LITERAL}")),
    ("redis", CONSUME, re.compile(rf"\b(?:redis|client|sub)\s*\.\s*subscribe\s*\(\s*{_LITERAL}")),
    # NestJS microservices / generic event emitters / domain event buses
    ("nest", CONSUME, re.compile(rf"@(?:EventPattern|MessagePattern)\s*\(\s*{_LITERAL}")),
    ("event_bus", PUBLISH, re.compile(rf"\b(?:eventBus|events|emitter|eventEmitter|bus)\s*\.\s*(?:emit|publish|dispatch)\s*\(\s*{_LITERAL}")),
    ("event_bus", CONSUME, re.compile(rf"\b(?:eventBus|events|emitter|eventEmitter|bus)\s*\.\s*(?:on|subscribe|handle)\s*\(\s*{_LITERAL}")),
    ("event_bus", PUBLISH, re.compile(rf"\bpublish(?:Event|Domain\w*)?\s*\(\s*{_LITERAL}")),
    # Celery / background task queues
    ("celery", PUBLISH, re.compile(r"\.\s*(?:delay|apply_async)\s*\(")),
    ("celery", CONSUME, re.compile(r"@(?:app|celery|shared_task)[\w.]*\.?task\b")),
]

_IMPORT_TECHNOLOGY = {
    "kafkajs": "kafka",
    "kafka-python": "kafka",
    "node-rdkafka": "kafka",
    "amqplib": "rabbitmq",
    "pika": "rabbitmq",
    "@aws-sdk/client-sqs": "sqs",
    "@aws-sdk/client-sns": "sns",
    "celery": "celery",
    "bullmq": "bull",
    "bull": "bull",
}


def _looks_like_topic(value: str) -> bool:
    """Filter obvious non-topic literals out of generic emitter matches."""
    if len(value) < 3 or len(value) > 120:
        return False
    if value.startswith("/") or value.startswith("http"):
        return False
    return True


def detect_event_signals(scan: RepoScan) -> list[EventSignal]:
    """Scan a repository for queue/event producer and consumer sites."""
    signals: list[EventSignal] = []
    for scanned in scan.files:
        if not scanned.is_source:
            continue
        signals.extend(_detect_in_file(scanned))
    return signals


def _detect_in_file(scanned: ScannedFile) -> list[EventSignal]:
    """Detect producer/consumer signals inside one source file."""
    found: list[EventSignal] = []
    for line_no, line in enumerate(scanned.text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("//") or stripped.startswith("#"):
            continue
        for technology, action, pattern in _PATTERNS:
            match = pattern.search(line)
            if match is None:
                continue
            topic = None
            if "topic" in pattern.groupindex:
                candidate = match.groupdict().get("topic")
                if isinstance(candidate, str) and _looks_like_topic(candidate):
                    topic = candidate
            found.append(
                EventSignal(
                    repo=scanned.repo,
                    file=scanned.path,
                    line=line_no,
                    action=action,
                    technology=technology,
                    topic=topic,
                    snippet=stripped[:220],
                )
            )
    return _dedupe(found)


def _dedupe(signals: list[EventSignal]) -> list[EventSignal]:
    """Collapse multiple pattern hits on the same line to the most specific one."""
    best: dict[tuple[str, int], EventSignal] = {}
    for signal in signals:
        key = (signal.file, signal.line)
        current = best.get(key)
        if current is None:
            best[key] = signal
            continue
        # Prefer a named topic; then prefer a specific technology over generic.
        if current.topic is None and signal.topic is not None:
            best[key] = signal
        elif current.technology == "event_bus" and signal.technology != "event_bus":
            best[key] = signal
    return sorted(best.values(), key=lambda item: (item.file, item.line))


def detect_technologies_from_imports(scan: RepoScan) -> dict[str, list[tuple[str, int, str]]]:
    """Map messaging technology -> import sites, used for runtime inference."""
    hits: dict[str, list[tuple[str, int, str]]] = {}
    for scanned in scan.files:
        if not scanned.is_source:
            continue
        for line_no, line in enumerate(scanned.text.splitlines(), start=1):
            if "import" not in line and "require" not in line:
                continue
            for module, technology in _IMPORT_TECHNOLOGY.items():
                if module in line:
                    hits.setdefault(technology, []).append((scanned.path, line_no, line.strip()[:220]))
    return hits
