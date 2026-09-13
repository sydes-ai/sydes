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


def _join_open_calls(text: str) -> str:
    """Pull continuation lines up into their opening call.

    Container constructions and mount calls are frequently written across
    several lines. Content is moved up rather than removed, so every reported
    line number still points at the construct's first line.

    Shared by every line-based scanner in this module and in
    `route_index.py` (which imports it from here) -- a decorator or call
    argument spanning multiple physical lines (e.g. NestJS's
    `@Controller({\\n  path: 'auth',\\n  version: '1',\\n})`) must look like
    one line to any scanner that only ever inspects one line at a time,
    or its argument is silently lost mid-way through.
    """
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        depth = line.count("(") - line.count(")")
        if depth <= 0 or "(" not in line:
            index += 1
            continue
        cursor = index + 1
        while cursor < len(lines) and depth > 0 and cursor - index <= 8:
            line = line.rstrip() + " " + lines[cursor].strip()
            depth += lines[cursor].count("(") - lines[cursor].count(")")
            lines[cursor] = ""
            cursor += 1
        lines[index] = line
        index = cursor if cursor > index else index + 1
    return "\n".join(lines)


def _split_args(expr: str) -> list[str]:
    """Split a top-level, comma-separated argument list, respecting nested
    parens and quoted strings so a comma inside either is never mistaken
    for an argument separator. Shared with `route_index.py`."""
    args: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    escape = False
    for ch in expr:
        if quote is not None:
            buf.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = None
            continue
        if ch in {"'", '"', "`"}:
            quote = ch
            buf.append(ch)
            continue
        if ch == "(":
            depth += 1
            buf.append(ch)
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
            continue
        if ch == "," and depth == 0:
            part = "".join(buf).strip()
            if part:
                args.append(part)
            buf = []
            continue
        buf.append(ch)
    part = "".join(buf).strip()
    if part:
        args.append(part)
    return args


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


#: Class-level decorators that carry an optional path prefix for the routes
#: declared inside them. Shared across TypeScript decorator-routing
#: frameworks (NestJS's `@Controller`, `routing-controllers`' `@Controller`/
#: `@JsonController`, tsoa's `@Route`) rather than tied to any single library
#: — they all use the same "class decorator names a prefix, method decorator
#: names a verb" shape.
_TS_CONTAINER_DECORATORS = {"Controller", "JsonController", "RestController", "Route"}

#: Method-level decorators naming an HTTP verb, same shape across those
#: frameworks.
_TS_VERB_DECORATORS = {
    "Get": "GET", "Post": "POST", "Put": "PUT", "Patch": "PATCH",
    "Delete": "DELETE", "Head": "HEAD", "Options": "OPTIONS", "All": "ALL",
}

_TS_DECORATOR_NAME_RE = re.compile(r"^@(?P<name>[A-Za-z_$][\w$]*)")
_TS_DECORATOR_STRING_ARG_RE = re.compile(r"^@[A-Za-z_$][\w$]*\s*\(\s*['\"]([^'\"]*)['\"]")
#: NestJS also accepts an object-literal decorator argument --
#: `@Controller({ path: 'auth', version: '1' })` -- most often specifically
#: to attach a per-controller URI version, which a bare string argument
#: cannot express at all. `_TS_DECORATOR_STRING_ARG_RE` alone silently
#: treats this shape as "no prefix declared" (confirmed the dominant cause
#: of real route paths missing their controller segment entirely).
_TS_DECORATOR_OBJECT_ARG_RE = re.compile(r"^@[A-Za-z_$][\w$]*\s*\(\s*\{(?P<body>.*)\}")
_TS_OBJECT_PATH_KEY_RE = re.compile(r"\bpath\s*:\s*['\"]([^'\"]*)['\"]")
_TS_OBJECT_VERSION_KEY_RE = re.compile(r"\bversion\s*:\s*['\"]([^'\"]*)['\"]")
_TS_CLASS_RE = re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+[A-Za-z_$]")
_TS_METHOD_RE = re.compile(
    r"^\s*(?:(?:public|private|protected|static|abstract|readonly|async)\s+)*"
    r"(?P<name>[A-Za-z_$][\w$]*)\s*\("
)
_TS_STATEMENT_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "constructor", "return", "new", "else", "throw", "super",
}


def _decorator_name(annotation: str) -> str:
    match = _TS_DECORATOR_NAME_RE.match(annotation)
    return match.group("name") if match else ""


def _decorator_object_body(annotation: str) -> str | None:
    """The raw text between `{` and the matching `}` of an object-literal
    decorator argument, or `None` if the argument isn't object-literal
    shaped. Requires the decorator's whole argument list to already be on
    one logical line (see `_join_open_calls`)."""
    match = _TS_DECORATOR_OBJECT_ARG_RE.match(annotation)
    return match.group("body") if match else None


def _decorator_path_arg(annotation: str) -> str | None:
    """A decorator's path/prefix argument, from either a bare string
    (`@Controller('auth')`) or an object literal's `path` key
    (`@Controller({ path: 'auth', version: '1' })`)."""
    match = _TS_DECORATOR_STRING_ARG_RE.match(annotation)
    if match:
        return match.group(1)
    body = _decorator_object_body(annotation)
    if body is not None:
        object_match = _TS_OBJECT_PATH_KEY_RE.search(body)
        if object_match:
            return object_match.group(1)
    return None


def _decorator_version_arg(annotation: str) -> str | None:
    """A decorator's `version` key, only from the object-literal form --
    there is no equivalent in the bare-string form. Only ever a plain
    quoted string: an array (`version: ['1', '2']`) or the `VERSION_NEUTRAL`
    constant both fail to match here, which is the correct, honest
    "not resolved" outcome rather than a guess at which version to use.
    """
    body = _decorator_object_body(annotation)
    if body is None:
        return None
    match = _TS_OBJECT_VERSION_KEY_RE.search(body)
    return match.group(1) if match else None


def _compose_ts_path(class_prefix: str, route_path: str) -> str:
    if class_prefix:
        combined = f"{class_prefix.rstrip('/')}/{route_path.lstrip('/')}" if route_path else class_prefix
    else:
        combined = route_path
    return _normalize_express_path(combined)


def _compose_container_prefix(path_prefix: str, version: str | None) -> str:
    """A container's own composed prefix: its declared `version` (URI
    versioning inserts this as its own path segment, prefixed `v` -- NestJS's
    own default convention for `VersioningType.URI`) ahead of its declared
    `path`. With no version, `path_prefix` passes through completely
    unchanged -- this must be a strict no-op for every container that
    doesn't use per-controller versioning, the overwhelming majority of
    existing callers.
    """
    if not version:
        return path_prefix
    segments = [f"v{version}"]
    if path_prefix:
        segments.append(path_prefix.strip("/"))
    return "/".join(segments)


#: `app.setGlobalPrefix(...)` applies to every controller in a NestJS
#: application at once -- not to one container, the way `@Controller(...)`
#: does -- so it needs its own, separate detector rather than reusing the
#: per-container decorator-argument parsing above.
_TS_SET_GLOBAL_PREFIX_RE = re.compile(r"(?<![\w.])[A-Za-z_$][\w$]*\.setGlobalPrefix\s*\(")
_LITERAL_STRING_ONLY_RE = re.compile(r"^['\"]([^'\"]*)['\"]$")


def extract_set_global_prefix(line: str) -> tuple[str | None, bool]:
    """Detect `app.setGlobalPrefix(...)` on one (already call-joined) line.

    Returns `(literal_value, dynamic)`. `literal_value` is set only when the
    call's first argument is a plain quoted string -- the common case for a
    hard-coded API prefix. Any other first argument (a config lookup, a
    variable, a function call -- the actual shape in real code at least as
    often as a literal) makes `dynamic=True` instead: the call was found,
    but Sydes cannot statically know what it resolves to, and must say so
    rather than guess or silently proceed as if no prefix were set at all.
    """
    match = _TS_SET_GLOBAL_PREFIX_RE.search(line)
    if match is None:
        return None, False
    open_paren = match.end() - 1
    depth = 0
    close_paren = None
    for pos in range(open_paren, len(line)):
        if line[pos] == "(":
            depth += 1
        elif line[pos] == ")":
            depth -= 1
            if depth == 0:
                close_paren = pos
                break
    if close_paren is None:
        # The call's closing paren never appeared within this (already
        # call-joined) line -- an unusually long or deeply nested argument
        # list. Nothing to guess at safely.
        return None, True
    args = _split_args(line[open_paren + 1 : close_paren])
    if not args:
        return None, True
    literal_match = _LITERAL_STRING_ONLY_RE.match(args[0].strip())
    if literal_match:
        return literal_match.group(1), False
    return None, True


def _extract_typescript_decorator_routes(
    repo: str,
    relative_path: str,
    text: str,
) -> list[EndpointCandidate]:
    # A decorator argument (string or object-literal) commonly spans several
    # physical lines; without joining them first, an intervening line such
    # as `  path: 'auth',` reads as ordinary code and wipes `pending_
    # decorators` below before the class line is ever reached -- silently
    # losing the whole container declaration, not just its prefix.
    lines = _join_open_calls(text).splitlines()
    endpoints: list[EndpointCandidate] = []
    pending_decorators: list[str] = []
    class_prefix = ""

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # A JSDoc/block comment line between a decorator and the class or
        # method it documents must not reset `pending_decorators` below --
        # the same failure mode as an ordinary intervening code line, just
        # triggered by documentation instead.
        if stripped.startswith("/*") or stripped.startswith("*") or stripped.startswith("//"):
            continue
        if stripped.startswith("@"):
            pending_decorators.append(stripped)
            continue

        if pending_decorators and _TS_CLASS_RE.match(stripped):
            container_ann = next(
                (ann for ann in pending_decorators if _decorator_name(ann) in _TS_CONTAINER_DECORATORS),
                None,
            )
            if container_ann:
                prefix = _decorator_path_arg(container_ann) or ""
                version = _decorator_version_arg(container_ann)
                composed = _compose_container_prefix(prefix, version)
                class_prefix = _normalize_basic_path(composed) if composed else ""
                if class_prefix == "/":
                    class_prefix = ""
            pending_decorators = []
            continue

        method_match = _TS_METHOD_RE.match(stripped)
        if method_match and method_match.group("name") in _TS_STATEMENT_KEYWORDS:
            method_match = None
        if pending_decorators and method_match:
            handler = method_match.group("name")
            handler_signature = stripped
            for ann in pending_decorators:
                verb = _TS_VERB_DECORATORS.get(_decorator_name(ann))
                if not verb:
                    continue
                route_path = _decorator_path_arg(ann) or ""
                full_path = _compose_ts_path(class_prefix, route_path)
                endpoints.append(
                    EndpointCandidate(
                        method=verb,
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
            pending_decorators = []
            continue

        if stripped:
            pending_decorators = []

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


#: Access modifiers are optional: package-private handlers are legal Java and
#: common in single-file Spring samples. A return type is still required, so
#: statements such as `return foo(` do not look like declarations. Shared with
#: `route_index.py`'s Java route-declaration recognizer, which needs the same
#: shape to tell a real method declaration from a statement.
_SPRING_METHOD_RE = re.compile(
    r"^\s*(?:(?:public|private|protected|static|final|abstract|synchronized|default)\s+)*"
    r"(?P<type>[A-Za-z_][\w.]*(?:\s*<[^>]*>)?(?:\s*\[\s*\])?)\s+"
    r"(?P<name>[A-Za-z_]\w*)\s*\("
)
_SPRING_STATEMENT_KEYWORDS = {"return", "new", "if", "while", "for", "switch", "catch", "throw", "else", "assert"}


def _extract_spring_routes(repo: str, relative_path: str, text: str) -> list[EndpointCandidate]:
    lines = text.splitlines()
    endpoints: list[EndpointCandidate] = []
    pending_annotations: list[str] = []
    class_prefix = ""

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

        method_match = _SPRING_METHOD_RE.match(stripped)
        if method_match and method_match.group("type") in _SPRING_STATEMENT_KEYWORDS:
            method_match = None
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

        ts_decorator_routes = _extract_typescript_decorator_routes(candidate.repo, file_path, text)
        if ts_decorator_routes:
            frameworks.add("ts_decorators")
            endpoints.extend(ts_decorator_routes)

    return endpoints, frameworks
