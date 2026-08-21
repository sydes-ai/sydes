"""Inference of the runtime dependencies needed to exercise affected behavior.

This answers "what actual system would need to exist to verify this change?".
Sydes does not provision, mock, start, or contact anything here — detection is
read-only over configuration and source, and every dependency keeps the file and
line it was detected from.

Parsing is deliberately dependency-free (line oriented) rather than pulling in a
YAML runtime for a read-only heuristic pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from sydes.core.models import EvidenceRef
from sydes.verify.models import AffectedFlow, CrossRepoImpact, RuntimeDependency
from sydes.verify.source_files import RepoFiles, SourceFile

KIND_DATABASE = "database"
KIND_CACHE = "cache"
KIND_QUEUE = "queue"
KIND_HTTP_SERVICE = "http_service"
KIND_OBJECT_STORE = "object_store"
KIND_SEARCH = "search"
KIND_MAIL = "mail"


@dataclass(slots=True)
class RuntimeHit:
    """One detection site for a runtime dependency."""

    name: str
    kind: str
    evidence: EvidenceRef


# Environment variable name fragment -> (dependency name, kind).
_ENV_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"^(?:POSTGRES|POSTGRESQL|PG)_"), "PostgreSQL", KIND_DATABASE),
    (re.compile(r"^(?:DATABASE|DB)_?(?:URL|URI|HOST|PORT|NAME|DSN|CONNECTION.*)$"), "SQL database", KIND_DATABASE),
    (re.compile(r"^MYSQL_"), "MySQL", KIND_DATABASE),
    (re.compile(r"^MONGO"), "MongoDB", KIND_DATABASE),
    (re.compile(r"^NEO4J"), "Neo4j", KIND_DATABASE),
    (re.compile(r"^REDIS"), "Redis", KIND_CACHE),
    (re.compile(r"^MEMCACHED"), "Memcached", KIND_CACHE),
    (re.compile(r"^KAFKA"), "Kafka", KIND_QUEUE),
    (re.compile(r"^(?:RABBIT|AMQP)"), "RabbitMQ", KIND_QUEUE),
    (re.compile(r"^SQS_|_SQS_|^AWS_SQS"), "AWS SQS", KIND_QUEUE),
    (re.compile(r"^SNS_|^AWS_SNS"), "AWS SNS", KIND_QUEUE),
    (re.compile(r"^(?:S3|MINIO|AWS_S3)"), "S3-compatible object store", KIND_OBJECT_STORE),
    (re.compile(r"^(?:ELASTIC|OPENSEARCH)"), "Elasticsearch/OpenSearch", KIND_SEARCH),
    (re.compile(r"^(?:SMTP|MAIL|SENDGRID)"), "Mail/SMTP service", KIND_MAIL),
]

_SERVICE_URL_RE = re.compile(r"^(?P<name>[A-Z0-9_]+?)_(?:API|SERVICE|BASE|CLIENT)?_?(?:URL|URI|ENDPOINT|HOST)$")

# Prefixes that name this application or its build tooling, not a dependency
# it must reach. `VITE_API_URL` is the frontend's view of *this* service.
_NON_DEPENDENCY_PREFIXES = {
    "APP",
    "SERVER",
    "BASE",
    "PUBLIC",
    "FRONTEND",
    "BACKEND",
    "CLIENT",
    "HOST",
    "SITE",
    "WEBSITE",
    "DOMAIN",
    "VITE",
    "REACT",
    "REACT_APP",
    "NEXT",
    "NEXT_PUBLIC",
    "NG",
    "WEBPACK",
    "STORYBOOK",
    "SOCKET_IO",
}

# Container image fragment -> (dependency name, kind).
_IMAGE_PATTERNS: list[tuple[str, str, str]] = [
    ("postgres", "PostgreSQL", KIND_DATABASE),
    ("mysql", "MySQL", KIND_DATABASE),
    ("mariadb", "MariaDB", KIND_DATABASE),
    ("mongo", "MongoDB", KIND_DATABASE),
    ("neo4j", "Neo4j", KIND_DATABASE),
    ("redis", "Redis", KIND_CACHE),
    ("memcached", "Memcached", KIND_CACHE),
    ("kafka", "Kafka", KIND_QUEUE),
    ("zookeeper", "ZooKeeper", KIND_QUEUE),
    ("rabbitmq", "RabbitMQ", KIND_QUEUE),
    ("localstack", "LocalStack (AWS emulation)", KIND_QUEUE),
    ("minio", "MinIO (S3-compatible)", KIND_OBJECT_STORE),
    ("elasticsearch", "Elasticsearch", KIND_SEARCH),
    ("opensearch", "OpenSearch", KIND_SEARCH),
    ("mailhog", "MailHog (SMTP)", KIND_MAIL),
]

# Source import fragment -> (dependency name, kind).
_IMPORT_PATTERNS: list[tuple[str, str, str]] = [
    ("psycopg", "PostgreSQL", KIND_DATABASE),
    ("asyncpg", "PostgreSQL", KIND_DATABASE),
    ("sqlalchemy", "SQL database", KIND_DATABASE),
    ("from pg ", "PostgreSQL", KIND_DATABASE),
    ("require('pg'", "PostgreSQL", KIND_DATABASE),
    ('require("pg"', "PostgreSQL", KIND_DATABASE),
    ("'pg'", "PostgreSQL", KIND_DATABASE),
    ("mysql2", "MySQL", KIND_DATABASE),
    ("mongoose", "MongoDB", KIND_DATABASE),
    ("pymongo", "MongoDB", KIND_DATABASE),
    ("@prisma/client", "SQL database (Prisma)", KIND_DATABASE),
    ("typeorm", "SQL database (TypeORM)", KIND_DATABASE),
    ("sequelize", "SQL database (Sequelize)", KIND_DATABASE),
    ("ioredis", "Redis", KIND_CACHE),
    ("redis", "Redis", KIND_CACHE),
    ("elasticsearch", "Elasticsearch", KIND_SEARCH),
    ("nodemailer", "Mail/SMTP service", KIND_MAIL),
    ("boto3", "AWS services", KIND_OBJECT_STORE),
    ("@aws-sdk/client-s3", "S3-compatible object store", KIND_OBJECT_STORE),
]

_MESSAGING_NAMES = {
    "kafka": ("Kafka", KIND_QUEUE),
    "rabbitmq": ("RabbitMQ", KIND_QUEUE),
    "sqs": ("AWS SQS", KIND_QUEUE),
    "sns": ("AWS SNS", KIND_QUEUE),
    "celery": ("Celery broker", KIND_QUEUE),
    "bull": ("Redis (Bull queue)", KIND_QUEUE),
}

_SPRING_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"spring\.datasource\.url|datasource:", re.IGNORECASE), "SQL database", KIND_DATABASE),
    (re.compile(r"bootstrap[-_]servers", re.IGNORECASE), "Kafka", KIND_QUEUE),
    (re.compile(r"spring\.rabbitmq|rabbitmq:", re.IGNORECASE), "RabbitMQ", KIND_QUEUE),
    (re.compile(r"spring\.redis|redis:", re.IGNORECASE), "Redis", KIND_CACHE),
    (re.compile(r"spring\.mail|mail:", re.IGNORECASE), "Mail/SMTP service", KIND_MAIL),
]

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?(?P<key>[A-Z][A-Z0-9_]*)\s*=")
_IMAGE_LINE = re.compile(r"^\s*image\s*:\s*['\"]?(?P<image>[^'\"\s#]+)")
_HTTP_URL = re.compile(r"https?://(?P<host>[A-Za-z0-9_.\-]+)(?::(?P<port>\d+))?")


def _titleize_service(token: str) -> str:
    """Render `PAYMENT_API_URL` style prefixes as a service name."""
    words = [part.capitalize() for part in token.split("_") if part]
    return " ".join(words) or token


def _env_hits(scanned: SourceFile) -> list[RuntimeHit]:
    """Detect dependencies from environment variable declarations."""
    hits: list[RuntimeHit] = []
    for line_no, line in enumerate(scanned.text.splitlines(), start=1):
        match = _ENV_LINE.match(line)
        if match is None:
            continue
        key = match.group("key")
        evidence = EvidenceRef(
            file=scanned.path,
            symbol=key,
            label="env_var",
            snippet=line.strip()[:160],
        )
        value = line.split("=", 1)[1].strip().strip("\"'").lower() if "=" in line else ""
        matched = False
        for pattern, name, kind in _ENV_PATTERNS:
            if pattern.search(key):
                # `DB_CONNECTION=mysql` names the engine in the value, not the key.
                if kind == KIND_DATABASE:
                    for engine, engine_name in (
                        ("postgres", "PostgreSQL"),
                        ("mysql", "MySQL"),
                        ("mariadb", "MariaDB"),
                        ("mongo", "MongoDB"),
                        ("sqlite", "SQLite"),
                    ):
                        if engine in value:
                            name = engine_name
                            break
                hits.append(RuntimeHit(name=name, kind=kind, evidence=evidence))
                matched = True
                break
        if matched:
            continue
        service_match = _SERVICE_URL_RE.match(key)
        if service_match:
            prefix = service_match.group("name")
            head = prefix.split("_", 1)[0]
            if prefix in _NON_DEPENDENCY_PREFIXES or head in _NON_DEPENDENCY_PREFIXES:
                continue
            if len(prefix) < 3:
                continue
            if any(token in key for token in ("CALLBACK", "REDIRECT", "CORS", "ORIGIN")):
                # An OAuth callback or CORS origin points back at this service.
                continue
            hits.append(
                RuntimeHit(
                    name=f"{_titleize_service(prefix)} service",
                    kind=KIND_HTTP_SERVICE,
                    evidence=evidence,
                )
            )
    return hits


def _compose_hits(scanned: SourceFile) -> list[RuntimeHit]:
    """Detect dependencies from container images in compose/k8s/helm/CI files."""
    hits: list[RuntimeHit] = []
    for line_no, line in enumerate(scanned.text.splitlines(), start=1):
        match = _IMAGE_LINE.match(line)
        if match is None:
            continue
        image = match.group("image").lower()
        for fragment, name, kind in _IMAGE_PATTERNS:
            if fragment in image:
                hits.append(
                    RuntimeHit(
                        name=name,
                        kind=kind,
                        evidence=EvidenceRef(
                            file=scanned.path,
                            symbol=match.group("image"),
                            label="container_image",
                            snippet=line.strip()[:160],
                        ),
                    )
                )
                break
    return hits


def _spring_hits(scanned: SourceFile) -> list[RuntimeHit]:
    """Detect dependencies from Spring-style application config."""
    hits: list[RuntimeHit] = []
    for line in scanned.text.splitlines():
        for pattern, name, kind in _SPRING_PATTERNS:
            if pattern.search(line):
                hits.append(
                    RuntimeHit(
                        name=name,
                        kind=kind,
                        evidence=EvidenceRef(
                            file=scanned.path,
                            label="application_config",
                            snippet=line.strip()[:160],
                        ),
                    )
                )
                break
    return hits


def _import_hits(scanned: SourceFile) -> list[RuntimeHit]:
    """Detect dependencies from client library imports in source."""
    hits: list[RuntimeHit] = []
    for line_no, line in enumerate(scanned.text.splitlines(), start=1):
        lowered = line.lower()
        if "import" not in lowered and "require" not in lowered:
            continue
        for fragment, name, kind in _IMPORT_PATTERNS:
            if fragment in lowered:
                hits.append(
                    RuntimeHit(
                        name=name,
                        kind=kind,
                        evidence=EvidenceRef(
                            file=scanned.path,
                            label="client_import",
                            snippet=line.strip()[:160],
                        ),
                    )
                )
                break
    return hits


def _is_config_file(scanned: SourceFile) -> bool:
    """True for compose/k8s/helm/CI/application config files."""
    lowered = scanned.path.lower()
    name = Path(lowered).name
    if name.startswith("docker-compose") or name == "compose.yml" or name == "compose.yaml":
        return True
    if ".github/workflows/" in lowered:
        return True
    if lowered.startswith("k8s/") or "/k8s/" in lowered or "helm" in lowered or "manifests" in lowered:
        return True
    return name.startswith("values") and scanned.extension in {".yml", ".yaml"}


def _is_env_file(scanned: SourceFile) -> bool:
    """True for `.env`-style files."""
    return Path(scanned.path).name.lower().startswith(".env")


def _is_app_config(scanned: SourceFile) -> bool:
    """True for Spring-style application config files."""
    name = Path(scanned.path).name.lower()
    return name.startswith("application") and scanned.extension in {".yml", ".yaml", ".properties"}


def infer_runtime_dependencies(
    *,
    files: RepoFiles,
    flows: list[AffectedFlow],
    changed_files: set[str],
    cross_repo_impacts: list[CrossRepoImpact] | None = None,
) -> list[RuntimeDependency]:
    """Infer runtime dependencies needed to exercise the affected flows."""
    hits: list[RuntimeHit] = []

    for scanned in files.files:
        if _is_env_file(scanned):
            hits.extend(_env_hits(scanned))
        elif _is_config_file(scanned):
            hits.extend(_compose_hits(scanned))
            hits.extend(_env_hits(scanned))
        elif _is_app_config(scanned):
            hits.extend(_spring_hits(scanned))
        elif scanned.is_app_source:
            hits.extend(_import_hits(scanned))

    flow_files: set[str] = set()
    flow_ids_by_kind: dict[str, set[str]] = {}
    external_hosts: dict[str, tuple[str, EvidenceRef]] = {}

    # The affected flows describe themselves through the shared sink taxonomy,
    # so runtime needs are read from those sinks rather than re-derived.
    for flow in flows:
        for step in flow.steps:
            if step.get("file"):
                flow_files.add(str(step["file"]))
        for sink in flow.sinks:
            if sink.get("file"):
                flow_files.add(str(sink["file"]))
            kind_token = str(sink.get("kind") or "").lower()
            name = str(sink.get("name") or "")
            if kind_token == "database":
                flow_ids_by_kind.setdefault(KIND_DATABASE, set()).add(flow.id)
            elif kind_token == "queue":
                flow_ids_by_kind.setdefault(KIND_QUEUE, set()).add(flow.id)
            elif kind_token == "external_api":
                flow_ids_by_kind.setdefault(KIND_HTTP_SERVICE, set()).add(flow.id)
                url_match = _HTTP_URL.search(name)
                if url_match:
                    external_hosts[url_match.group("host")] = (
                        flow.id,
                        EvidenceRef(
                            file=str(sink.get("file") or ""),
                            symbol=name,
                            label="affected_flow_external_call",
                        ),
                    )

    grouped: dict[tuple[str, str], list[EvidenceRef]] = {}
    for hit in hits:
        grouped.setdefault((hit.name, hit.kind), []).append(hit.evidence)

    # A generic name is noise once a concrete product was named for the same
    # kind; fold its evidence into the specific one.
    for generic_name, kind in (
        ("SQL database", KIND_DATABASE),
        ("S3-compatible object store", KIND_OBJECT_STORE),
    ):
        generic = (generic_name, kind)
        if generic not in grouped:
            continue
        specific = [
            key
            for key in grouped
            if key[1] == kind and key[0] not in {generic_name, "AWS services"}
        ]
        if len(specific) == 1:
            grouped[specific[0]].extend(grouped.pop(generic))

    dependencies: list[RuntimeDependency] = []
    for (name, kind), evidence in sorted(grouped.items()):
        deduped: list[EvidenceRef] = []
        seen: set[tuple[str | None, str | None]] = set()
        for item in evidence:
            key = (item.file, item.snippet)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)

        related = sorted(flow_ids_by_kind.get(kind, set()))
        touches_change = any(item.file in changed_files for item in deduped)
        touches_flow = any(item.file in flow_files for item in deduped)
        dependencies.append(
            RuntimeDependency(
                id=f"runtime:{kind}:{name}".replace(" ", "-").lower(),
                name=name,
                kind=kind,
                repo=files.repo,
                required_for_flow_ids=related,
                detected_from=deduped[:6],
                scope="affected_flow" if (touches_change or touches_flow or related) else "repository",
            )
        )

    priority = {
        KIND_DATABASE: 0,
        KIND_QUEUE: 1,
        KIND_HTTP_SERVICE: 2,
        KIND_CACHE: 3,
        KIND_OBJECT_STORE: 4,
        KIND_SEARCH: 5,
        KIND_MAIL: 6,
    }
    return sorted(
        dependencies,
        key=lambda item: (0 if item.scope == "affected_flow" else 1, priority.get(item.kind, 9), item.name),
    )
