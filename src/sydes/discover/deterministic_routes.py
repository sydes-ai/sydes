"""Small deterministic route declaration recognizers for common frameworks."""

from __future__ import annotations

import re

from sydes.core.models import CandidateFileRead, EndpointCandidate, EvidenceRef
from sydes.ingest.file_roles import FILE_ROLE_SOURCE_ROUTE_CANDIDATE, classify_candidate_file_role

_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "ALL"}


def _normalize_basic_path(path: str) -> str:
    path = path.strip()
    if not path.startswith("/"):
        path = f"/{path}"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return path


def _normalize_flask_path(path: str) -> str:
    normalized = re.sub(r"<(?:[^:>]+:)?([^>]+)>", r"{\1}", path)
    return _normalize_basic_path(normalized)


def _normalize_express_path(path: str) -> str:
    normalized = re.sub(r":([A-Za-z_]\w*)", r"{\1}", path)
    return _normalize_basic_path(normalized)


def _extract_python_decorator_routes(
    repo: str,
    relative_path: str,
    text: str,
) -> list[EndpointCandidate]:
    lines = text.splitlines()
    endpoints: list[EndpointCandidate] = []
    decorator_re = re.compile(
        r"^\s*@(?P<object>[A-Za-z_][\w\.]*)\.(?P<verb>route|get|post|put|patch|delete|options|head)\("
        r"\s*['\"](?P<path>[^'\"]+)['\"](?P<rest>.*)\)\s*$",
        re.IGNORECASE,
    )
    def_re = re.compile(r"^\s*def\s+(?P<name>[A-Za-z_]\w*)\s*\(")

    pending: list[tuple[int, str, str, str]] = []
    for idx, line in enumerate(lines):
        match = decorator_re.match(line)
        if match:
            pending.append(
                (
                    idx,
                    match.group("verb").lower(),
                    match.group("path"),
                    match.group("rest") or "",
                )
            )
            continue
        def_match = def_re.match(line)
        if pending and def_match:
            handler = def_match.group("name")
            handler_signature = line.strip()
            for decorator_idx, verb, raw_path, rest in pending:
                methods: list[str] = []
                if verb == "route":
                    methods_match = re.search(r"methods\s*=\s*\[([^\]]+)\]", rest, flags=re.IGNORECASE)
                    if methods_match:
                        methods = [
                            token.strip(" '\"").upper()
                            for token in methods_match.group(1).split(",")
                            if token.strip(" '\"")
                        ]
                    if not methods:
                        methods = ["GET"]
                else:
                    methods = [verb.upper()]
                path = _normalize_flask_path(raw_path)
                evidence_line = lines[decorator_idx].strip()
                for method in methods:
                    if method not in _HTTP_METHODS:
                        continue
                    endpoints.append(
                        EndpointCandidate(
                            method=method,
                            path=path,
                            handler=handler,
                            file=relative_path,
                            repo=repo,
                            evidence=[
                                EvidenceRef(
                                    file=relative_path,
                                    symbol=handler,
                                    label=evidence_line,
                                    snippet=f"{evidence_line}\n{handler_signature}",
                                )
                            ],
                            confidence=1.0,
                            status="deterministic",
                        )
                    )
            pending = []
        elif pending and line.strip() and not line.strip().startswith("@"):
            pending = []
    return endpoints


def _extract_express_routes(repo: str, relative_path: str, text: str) -> list[EndpointCandidate]:
    line_re = re.compile(
        r"(?<![\w.])(?P<obj>app|router)\.(?P<method>get|post|put|patch|delete|options|head|all)\s*"
        r"\(\s*['\"`](?P<path>[^'\"`]+)['\"`]\s*(?:,\s*(?P<handler>[^)\n]+))?",
        re.IGNORECASE,
    )
    endpoints: list[EndpointCandidate] = []
    for line in text.splitlines():
        if line.lstrip().startswith("@"):
            continue
        match = line_re.search(line)
        if not match:
            continue
        method = match.group("method").upper()
        if method not in _HTTP_METHODS:
            continue
        path = _normalize_express_path(match.group("path"))
        raw_handler = (match.group("handler") or "").strip()
        handler = None
        if raw_handler:
            if "=>" in raw_handler or raw_handler.startswith("(") or raw_handler.startswith("async"):
                handler = "<inline>"
            else:
                handler_match = re.match(r"([A-Za-z_]\w*)", raw_handler)
                handler = handler_match.group(1) if handler_match else "<inline>"
        endpoints.append(
            EndpointCandidate(
                method=method,
                path=path,
                handler=handler,
                file=relative_path,
                repo=repo,
                evidence=[
                    EvidenceRef(
                        file=relative_path,
                        symbol=handler,
                        label=line.strip(),
                        snippet=line.strip(),
                    )
                ],
                confidence=1.0,
                status="deterministic",
            )
        )
    return endpoints


def _parse_spring_mapping(annotation: str) -> tuple[list[str], str | None]:
    ann = annotation.strip()
    path_value: str | None = None
    methods: list[str] = []
    direct_map = {
        "GetMapping": "GET",
        "PostMapping": "POST",
        "PutMapping": "PUT",
        "DeleteMapping": "DELETE",
        "PatchMapping": "PATCH",
    }
    for ann_name, method in direct_map.items():
        if ann.startswith(f"@{ann_name}"):
            methods = [method]
            break
    if ann.startswith("@RequestMapping") and not methods:
        method_match = re.search(r"RequestMethod\.(GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)", ann)
        if method_match:
            methods = [method_match.group(1)]
    if not methods:
        methods = ["GET"]

    path_match = re.search(r'["\']([^"\']+)["\']', ann)
    if path_match:
        path_value = path_match.group(1)
    else:
        named_match = re.search(r"(?:value|path)\s*=\s*['\"]([^'\"]+)['\"]", ann)
        if named_match:
            path_value = named_match.group(1)
    return methods, path_value


def _extract_spring_routes(repo: str, relative_path: str, text: str) -> list[EndpointCandidate]:
    lines = text.splitlines()
    endpoints: list[EndpointCandidate] = []
    pending_annotations: list[str] = []
    class_prefix = ""
    method_re = re.compile(r"^\s*(?:public|private|protected)\s+[^\(]*\s+(?P<name>[A-Za-z_]\w*)\s*\(")

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("@"):
            pending_annotations.append(stripped)
            continue

        if pending_annotations and " class " in f" {stripped} ":
            class_ann = next((ann for ann in pending_annotations if ann.startswith("@RequestMapping")), None)
            if class_ann:
                _, prefix = _parse_spring_mapping(class_ann)
                class_prefix = _normalize_basic_path(prefix or "")
                if class_prefix == "/":
                    class_prefix = ""
            pending_annotations = []
            continue

        method_match = method_re.match(stripped)
        if pending_annotations and method_match:
            handler = method_match.group("name")
            handler_signature = stripped
            for ann in pending_annotations:
                if not ann.startswith(
                    ("@GetMapping", "@PostMapping", "@PutMapping", "@DeleteMapping", "@PatchMapping", "@RequestMapping")
                ):
                    continue
                methods, route_path = _parse_spring_mapping(ann)
                if route_path is None:
                    route_path = ""
                full_path = _normalize_basic_path(f"{class_prefix.rstrip('/')}/{route_path.lstrip('/')}" if class_prefix else route_path)
                for method in methods:
                    if method not in _HTTP_METHODS:
                        continue
                    endpoints.append(
                        EndpointCandidate(
                            method=method,
                            path=full_path,
                            handler=handler,
                            file=relative_path,
                            repo=repo,
                            evidence=[
                                EvidenceRef(
                                    file=relative_path,
                                    symbol=handler,
                                    label=ann,
                                    snippet=f"{ann}\n{handler_signature}",
                                )
                            ],
                            confidence=1.0,
                            status="deterministic",
                        )
                    )
            pending_annotations = []
            continue

        if stripped:
            pending_annotations = []

    return endpoints


def extract_deterministic_routes(
    candidates: list[CandidateFileRead],
) -> tuple[list[EndpointCandidate], set[str]]:
    """Extract obvious route declarations from source candidates only."""
    endpoints: list[EndpointCandidate] = []
    frameworks: set[str] = set()

    for candidate in candidates:
        role = candidate.role or classify_candidate_file_role(candidate.relative_path)
        if role != FILE_ROLE_SOURCE_ROUTE_CANDIDATE:
            continue
        if candidate.skipped or candidate.snippet is None:
            continue
        text = candidate.snippet.text
        file_path = candidate.relative_path

        python_routes = _extract_python_decorator_routes(candidate.repo, file_path, text)
        if python_routes:
            frameworks.add("flask_fastapi")
            endpoints.extend(python_routes)

        express_routes = _extract_express_routes(candidate.repo, file_path, text)
        if express_routes:
            frameworks.add("express")
            endpoints.extend(express_routes)

        spring_routes = _extract_spring_routes(candidate.repo, file_path, text)
        if spring_routes:
            frameworks.add("spring")
            endpoints.extend(spring_routes)

    return endpoints, frameworks
