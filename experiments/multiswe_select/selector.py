"""Find Multi-SWE-bench instances that fall inside Sydes' verification scope.

Sydes verifies behavior reachable from a system entrypoint — an HTTP route, an
RPC method, a queue consumer. It has nothing to say about a pure library. So an
instance is applicable only when all of these hold:

  1. the language has a Sydes symbol extractor (python, js/ts today);
  2. the repository actually exposes an externally reachable entrypoint;
  3. the production patch changes implementation code, not only tests/docs/build;
  4. the changed files sit inside the served dependency graph.

Applicability is decided from repository structure, the production patch, and
detected entrypoints. `test_patch` is never read here — it is the answer sheet
for the evaluation that follows, and consulting it would turn selection into
cherry-picking.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess

# Languages with a Sydes handler-symbol extractor. Everything else is reported
# as unsupported rather than silently dropped.
SUPPORTED_LANGUAGES = {"python", "js", "ts"}

_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec|e2e)/|\.(test|spec)\.[jt]sx?$|(^|/)test_[^/]+\.py$|_test\.py$")
_DOC_PATH = re.compile(r"(^|/)(docs?|examples?|website|\.github)/|\.(md|rst|txt|png|svg|jpg|gif)$")
_BUILD_PATH = re.compile(
    r"(^|/)(package(-lock)?\.json|yarn\.lock|pnpm-lock\.yaml|tsconfig[^/]*\.json|"
    r"\.eslintrc[^/]*|\.prettierrc[^/]*|Dockerfile|Makefile|setup\.py|setup\.cfg|"
    r"pyproject\.toml|requirements[^/]*\.txt|\.gitignore|rollup\.config\.[jt]s|"
    r"vite\.config\.[jt]s|babel\.config\.[jt]s|jest\.config\.[jt]s)$"
)

_CODE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}

# Entrypoint markers. Each must indicate a surface the *shipped service*
# exposes. Earlier, looser versions of these matched reactive `.subscribe(`
# calls in svelte stores, ANTLR's `.consume(` in sympy's generated parsers, and
# demo servers under `examples/` — inflating the applicable population roughly
# fifteen-fold with repositories that serve nothing.
_ENTRYPOINT_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    # A route registration carrying a concrete path literal. Generic routing
    # machinery inside a framework has no literal and is correctly skipped.
    ("http_route", re.compile(
        r"""\b(?:app|router|server|api)\.(?:get|post|put|patch|delete|all)\s*\(\s*['"`](?P<path>/[^'"`]*)['"`]"""
        r"""|@(?:app|router|bp|blueprint)\.(?:get|post|put|patch|delete|route)\s*\(\s*['"](?P<p2>/[^'"]*)['"]"""
    )),
    # Serverless HTTP handlers: the platform supplies the path, so the handler
    # signature is the surface.
    ("serverless_http", re.compile(
        r"module\.exports\s*=\s*(?:async\s*)?\(\s*req\s*,\s*res\s*\)"
        r"|export\s+default\s+(?:async\s+)?function\s+\w*\s*\(\s*req\s*,\s*res"
        r"|addEventListener\s*\(\s*['\"]fetch['\"]"
    )),
    # An RPC service definition, not merely the word "grpc" in a comment.
    ("rpc", re.compile(
        r"^\s*service\s+\w+\s*\{|\bregisterService\s*\(|\bgrpc\.(?:Server|createServer)\b", re.MULTILINE
    )),
    # A broker client that is actually imported, not any mention of a broker.
    ("queue_event", re.compile(
        r"""(?:^|\n)\s*(?:import\s[^\n]*|from\s+)['"]?(?:kafkajs|amqplib|bullmq|celery|"""
        r"""@aws-sdk/client-sqs|google-cloud/pubsub|pika|kombu)\b"""
        r"""|require\s*\(\s*['"](?:kafkajs|amqplib|bullmq|@aws-sdk/client-sqs)['"]"""
    )),
]

#: Directories whose contents are not the shipped service: demos, docs,
#: benchmarks, tooling, and generated parsers.
_NON_PRODUCT_DIR = re.compile(
    r"(^|/)(examples?|sandbox|demo|benchmarks?|scripts?|website|docs?|fixtures?|"
    r"__fixtures__|_antlr|generated|vendor|third_party)/"
    # Mock servers that exist to drive a test suite are test infrastructure, not
    # a served surface. Insomnia's `insomnia-smoke-test/server/*` matched here.
    r"|(^|/)[\w.-]*(smoke-test|smoke_test|e2e-test|test-server|testing)[\w.-]*/"
)

#: Triple-quoted regions in Python. Framework documentation is full of example
#: route decorators — flask's own `ctx.py` and `helpers.py` qualified on
#: docstrings alone, which describes the framework's users, not the framework.
_PY_DOCSTRING = re.compile(r"(\"\"\"|\'\'\').*?\1", re.DOTALL)

#: A single route literal can appear in a fixture or a docstring. Requiring a
#: couple of distinct ones keeps one stray match from qualifying a repository.
_MIN_HTTP_ROUTES = 2

_SCAN_MAX_FILES = 4000
_SCAN_MAX_BYTES = 200_000


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True, timeout=timeout)


def changed_files(patch: str) -> list[str]:
    """Paths touched by a diff, as repo-relative strings."""
    return [line.split(" b/", 1)[-1].strip()
            for line in patch.splitlines() if line.startswith("diff --git")]


def changed_symbols(patch: str) -> list[str]:
    """Symbol names visible in diff hunk headers and added definitions.

    A cheap structural read of the patch, not a parse: enough to say whether the
    change touches implementation rather than data or configuration.
    """
    names: list[str] = []
    for line in patch.splitlines():
        if line.startswith("@@"):
            tail = line.split("@@")[-1].strip()
            match = re.search(r"\b(?:def|function|class|const|export)\s+(\w+)", tail)
            if match:
                names.append(match.group(1))
        elif line.startswith("+"):
            match = re.match(r"\+\s*(?:async\s+)?(?:def|function|class)\s+(\w+)", line)
            if match:
                names.append(match.group(1))
            else:
                match = re.match(r"\+\s*(?:export\s+)?(?:const|let)\s+(\w+)\s*=\s*(?:async\s*)?\(", line)
                if match:
                    names.append(match.group(1))
    seen: list[str] = []
    for name in names:
        if name not in seen:
            seen.append(name)
    return seen[:12]


def classify_paths(paths: list[str]) -> dict[str, list[str]]:
    """Split touched paths into production / test / doc / build."""
    buckets: dict[str, list[str]] = {"production": [], "test": [], "doc": [], "build": []}
    for path in paths:
        if _TEST_PATH.search(path):
            buckets["test"].append(path)
        elif _DOC_PATH.search(path):
            buckets["doc"].append(path)
        elif _BUILD_PATH.search(path):
            buckets["build"].append(path)
        elif Path(path).suffix.lower() in _CODE_SUFFIXES:
            buckets["production"].append(path)
        else:
            buckets["build"].append(path)
    return buckets


def detect_entrypoints(root: Path) -> dict[str, list[str]]:
    """Scan a checkout for externally reachable entrypoints.

    Repository-level and task-independent: what surfaces does this service
    expose at all? A repo with none of them cannot produce a Sydes flow no
    matter which file a patch touches.
    """
    found: dict[str, list[str]] = {}
    route_paths: set[str] = set()
    scanned = 0
    for path in sorted(root.rglob("*")):
        if scanned >= _SCAN_MAX_FILES:
            break
        if not path.is_file() or path.suffix.lower() not in _CODE_SUFFIXES:
            continue
        parts = {p.lower() for p in path.parts}
        if parts & {".git", "node_modules", "dist", "build", "__pycache__", ".venv"}:
            continue
        rel_check = path.relative_to(root).as_posix()
        if _TEST_PATH.search(rel_check) or _NON_PRODUCT_DIR.search(rel_check):
            continue
        try:
            if path.stat().st_size > _SCAN_MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        if path.suffix.lower() == ".py":
            text = _PY_DOCSTRING.sub("", text)
        rel = path.relative_to(root).as_posix()
        for kind, pattern in _ENTRYPOINT_MARKERS:
            matches = list(pattern.finditer(text))
            if not matches:
                continue
            if kind == "http_route":
                paths = {m.group("path") or m.group("p2") for m in matches}
                route_paths.update(p for p in paths if p)
            found.setdefault(kind, [])
            if len(found[kind]) < 6:
                found[kind].append(rel)

    # A lone route literal proves little; a handful is a served surface.
    if "http_route" in found and len(route_paths) < _MIN_HTTP_ROUTES:
        del found["http_route"]
    return found


def load_instances(directory: Path) -> list[dict]:
    """Read every downloaded Multi-SWE-bench shard."""
    rows: list[dict] = []
    for shard in sorted(directory.glob("*.jsonl")):
        stem = shard.stem
        language = stem.split("_", 1)[0] if "_" in stem else "python"
        if shard.name == "python_multi.jsonl":
            language = "python"
        for line in shard.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            row["_language"] = language
            row["_shard"] = shard.name
            rows.append(row)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shards", required=True, type=Path)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    options = parser.parse_args()

    rows = load_instances(options.shards)
    repos: dict[str, list[dict]] = {}
    for row in rows:
        repos.setdefault(f"{row.get('org')}/{row.get('repo')}", []).append(row)

    print(f"instances loaded : {len(rows)}")
    print(f"repositories     : {len(repos)}")
    languages = sorted({r["_language"] for r in rows})
    print(f"languages         : {', '.join(languages)}")

    # Entrypoint detection runs once per repository, on the newest base commit
    # available, because it is a property of the repository not the task.
    entrypoints: dict[str, dict[str, list[str]]] = {}
    for repo_name, instances in sorted(repos.items()):
        language = instances[0]["_language"]
        if language not in SUPPORTED_LANGUAGES:
            entrypoints[repo_name] = {"_unsupported_language": [language]}
            continue
        options.cache.mkdir(parents=True, exist_ok=True)
        clone = options.cache / repo_name.replace("/", "__")
        if not clone.exists():
            result = run(["git", "clone", "--depth", "1",
                          f"https://github.com/{repo_name}.git", str(clone)], timeout=900)
            if result.returncode != 0:
                entrypoints[repo_name] = {"_clone_failed": [result.stderr.strip()[:120]]}
                continue
        entrypoints[repo_name] = detect_entrypoints(clone)
        kinds = [k for k in entrypoints[repo_name] if not k.startswith("_")]
        print(f"  {repo_name:44} {language:7} entrypoints={kinds or 'none'}")

    candidates = []
    for row in rows:
        repo_name = f"{row.get('org')}/{row.get('repo')}"
        language = row["_language"]
        patch = row.get("fix_patch") or ""
        buckets = classify_paths(changed_files(patch))
        surface = entrypoints.get(repo_name, {})
        kinds = [k for k in surface if not k.startswith("_")]
        reasons = []
        if language not in SUPPORTED_LANGUAGES:
            reasons.append(f"language_unsupported:{language}")
        if not kinds:
            reasons.append("no_system_entrypoint")
        if not buckets["production"]:
            reasons.append("no_production_code_change")
        candidates.append({
            "instance_id": row.get("instance_id"),
            "repo": repo_name,
            "language": language,
            "base_commit": (row.get("base") or {}).get("sha"),
            "changed_files": buckets["production"],
            "other_files": buckets["test"] + buckets["doc"] + buckets["build"],
            "changed_symbols": changed_symbols(patch),
            "entrypoint_types": kinds,
            "entrypoint_evidence": {k: v[:3] for k, v in surface.items() if not k.startswith("_")},
            "applicable": not reasons,
            "exclusion_reasons": reasons,
        })

    applicable = [c for c in candidates if c["applicable"]]
    options.out.parent.mkdir(parents=True, exist_ok=True)
    options.out.write_text(json.dumps(candidates, indent=2), encoding="utf-8")
    print(f"\napplicable instances: {len(applicable)} of {len(candidates)}")
    by_repo: dict[str, int] = {}
    for item in applicable:
        by_repo[item["repo"]] = by_repo.get(item["repo"], 0) + 1
    for repo_name, count in sorted(by_repo.items(), key=lambda kv: -kv[1]):
        print(f"   {repo_name:44} {count}")


if __name__ == "__main__":
    main()
