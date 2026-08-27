"""Persistent repository architecture memory — Increment B, v1.

Increment D's boundary reasoning works, but it repeatedly re-derives the
same repository facts from path conventions and local source clues on every
run: which package is backend vs frontend, which roots are tests or
tooling, which package is a publishable library. Those facts change rarely
and cost nothing to determine from manifests Sydes already walks past.

This module captures a small set of them once, persists them per workspace,
and — critically — hands back only the *few* facts relevant to the current
change:

    repository -> small persisted RepoProfile -> lookup(files, symbols,
    concepts) -> 3-8 facts injected into the boundary-reasoning packet

It is retrieval memory, not permanent prompt context. The full profile is
never injected anywhere; `RepoProfile.lookup()` is the only path by which a
fact reaches an LLM prompt.

Deterministic by construction. Every fact here comes from a manifest, a
build/CI config, or directory structure Sydes' own `repo_map` already
recorded — there is no LLM call in this module, no embedding, no vector
search, no documentation ingestion, and no repository re-walk.

Soundness. A repo fact is not a graph edge:

    repo fact                       != graph edge
    semantic hypothesis             != graph edge
    repo fact + semantic hypothesis != ESTABLISHED boundary

Facts here improve INFERRED reasoning and candidate context. They never
create an `AffectedFlow`, a `VerificationObligation`, an `AcceptedImpact`,
or a deterministic boundary — nothing in this module writes to any of them,
and `verify.analyzer` only ever routes the retrieved facts into
`boundary_reasoning`'s packet.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sydes.ingest.file_roles import FILE_ROLE_TEST_USAGE_CANDIDATE, classify_candidate_file_role
from sydes.store.workspace import ensure_workspace

SCHEMA_VERSION = "v2"
PROFILE_FILENAME = "repo_profile.json"

#: The complete role vocabulary. Deliberately tiny — a package carries only
#: the small amount of role information investigation actually needs.
ROLE_BACKEND = "backend"
ROLE_FRONTEND = "frontend"
ROLE_LIBRARY = "library"
ROLE_APPLICATION = "application"
ROLE_TESTS = "tests"
ROLE_TOOLING = "tooling"
ROLE_UNKNOWN = "unknown"
ROLES = frozenset({
    ROLE_BACKEND, ROLE_FRONTEND, ROLE_LIBRARY, ROLE_APPLICATION,
    ROLE_TESTS, ROLE_TOOLING, ROLE_UNKNOWN,
})

#: Fact provenance. Ordered by trust: a manifest states intent, a directory
#: name merely suggests it. Deliberately coarse — no probabilistic engine.
SOURCE_MANIFEST = "manifest"
SOURCE_BUILD_CONFIG = "build_config"
SOURCE_CI_CONFIG = "ci_config"
SOURCE_DIRECTORY_STRUCTURE = "directory_structure"
SOURCE_SOURCE_LAYOUT = "source_layout"
SOURCE_DOCUMENTATION = "documentation"

CONFIDENCE_STRONG = "strong"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_WEAK = "weak"

_SOURCE_CONFIDENCE = {
    SOURCE_MANIFEST: CONFIDENCE_STRONG,
    SOURCE_BUILD_CONFIG: CONFIDENCE_STRONG,
    SOURCE_CI_CONFIG: CONFIDENCE_MEDIUM,
    SOURCE_DIRECTORY_STRUCTURE: CONFIDENCE_MEDIUM,
    SOURCE_SOURCE_LAYOUT: CONFIDENCE_MEDIUM,
    SOURCE_DOCUMENTATION: CONFIDENCE_WEAK,
}

#: Small, deliberately incomplete dependency->framework tables. Not a
#: framework catalog: just enough recognizable names to classify a package
#: role and give boundary reasoning a runtime identity. An unrecognized
#: dependency simply yields no framework fact.
_FRONTEND_DEPENDENCIES = {
    "react": "React", "react-dom": "React", "vue": "Vue", "svelte": "Svelte",
    "@angular/core": "Angular", "next": "Next.js", "nuxt": "Nuxt",
    "@sveltejs/kit": "SvelteKit", "vite": "Vite",
}
_BACKEND_DEPENDENCIES = {
    "express": "Express", "@nestjs/core": "NestJS", "fastify": "Fastify",
    "koa": "Koa", "django": "Django", "fastapi": "FastAPI", "flask": "Flask",
    "starlette": "Starlette", "actix-web": "Actix", "axum": "Axum",
    "rocket": "Rocket", "gin-gonic/gin": "Gin", "echo": "Echo",
    "spring-boot-starter-web": "Spring",
}

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")

#: Directory names that identify tooling roots by convention alone.
_TOOLING_DIR_NAMES = frozenset({"tools", "scripts", "build", "ci", "hack", "devtools"})


@dataclass(frozen=True)
class RepoFact:
    """One repository architecture fact, with where it came from.

    Every non-trivial fact carries provenance: a reader (or a later
    increment) must always be able to ask "why does Sydes believe this?"
    """

    key: str
    value: str
    source: str
    confidence: str
    evidence: tuple[str, ...] = ()
    #: The path this fact is about, when it is about one — the primary
    #: retrieval key for `lookup(files=...)`.
    path: str = ""
    observed_commit: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "value": self.value, "source": self.source,
            "confidence": self.confidence, "evidence": list(self.evidence),
            "path": self.path, "observed_commit": self.observed_commit,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RepoFact":
        return cls(
            key=str(raw.get("key") or ""), value=str(raw.get("value") or ""),
            source=str(raw.get("source") or SOURCE_DIRECTORY_STRUCTURE),
            confidence=str(raw.get("confidence") or CONFIDENCE_MEDIUM),
            evidence=tuple(str(item) for item in raw.get("evidence") or ()),
            path=str(raw.get("path") or ""),
            observed_commit=raw.get("observed_commit"),
        )

    def describe(self) -> str:
        """The one-line rendering injected into a reasoning packet."""
        where = f"{self.path} " if self.path else ""
        return f"{where}{self.value} ({self.source})".strip()


@dataclass(frozen=True)
class RepoPackage:
    """One discovered package/workspace root."""

    path: str
    role: str = ROLE_UNKNOWN
    name: str | None = None
    manifest: str | None = None
    kind: str | None = None
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path, "name": self.name, "role": self.role,
            "kind": self.kind, "manifest": self.manifest,
            "evidence": list(self.evidence),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RepoPackage":
        return cls(
            path=str(raw.get("path") or ""), name=raw.get("name"),
            role=str(raw.get("role") or ROLE_UNKNOWN), kind=raw.get("kind"),
            manifest=raw.get("manifest"),
            evidence=tuple(str(item) for item in raw.get("evidence") or ()),
        )


@dataclass
class RepoProfile:
    """A small, persisted set of repository architecture facts."""

    schema_version: str = SCHEMA_VERSION
    repo_identity: str = ""
    observed_commit: str | None = None
    languages: list[str] = field(default_factory=list)
    packages: list[RepoPackage] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    test_roots: list[str] = field(default_factory=list)
    tooling_roots: list[str] = field(default_factory=list)
    public_surface_hints: list[RepoFact] = field(default_factory=list)
    internal_surface_hints: list[RepoFact] = field(default_factory=list)
    architecture_facts: list[RepoFact] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "repo_identity": self.repo_identity,
            "observed_commit": self.observed_commit,
            "languages": list(self.languages),
            "packages": [item.to_dict() for item in self.packages],
            "frameworks": list(self.frameworks),
            "test_roots": list(self.test_roots),
            "tooling_roots": list(self.tooling_roots),
            "public_surface_hints": [item.to_dict() for item in self.public_surface_hints],
            "internal_surface_hints": [item.to_dict() for item in self.internal_surface_hints],
            "architecture_facts": [item.to_dict() for item in self.architecture_facts],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RepoProfile":
        return cls(
            schema_version=str(raw.get("schema_version") or ""),
            repo_identity=str(raw.get("repo_identity") or ""),
            observed_commit=raw.get("observed_commit"),
            languages=[str(item) for item in raw.get("languages") or []],
            packages=[RepoPackage.from_dict(item) for item in raw.get("packages") or []],
            frameworks=[str(item) for item in raw.get("frameworks") or []],
            test_roots=[str(item) for item in raw.get("test_roots") or []],
            tooling_roots=[str(item) for item in raw.get("tooling_roots") or []],
            public_surface_hints=[
                RepoFact.from_dict(item) for item in raw.get("public_surface_hints") or []
            ],
            internal_surface_hints=[
                RepoFact.from_dict(item) for item in raw.get("internal_surface_hints") or []
            ],
            architecture_facts=[
                RepoFact.from_dict(item) for item in raw.get("architecture_facts") or []
            ],
        )

    # -- retrieval --------------------------------------------------------

    def all_facts(self) -> list[RepoFact]:
        """Every fact, including one synthesized per package so a package's
        role is retrievable by the same path-matching as any other fact."""
        facts: list[RepoFact] = []
        for package in self.packages:
            if package.role == ROLE_UNKNOWN:
                continue  # an unknown role tells a reader nothing
            facts.append(RepoFact(
                key=f"package_role:{package.path}",
                value=f"is a {package.role} package"
                      + (f" ({package.kind})" if package.kind else ""),
                source=SOURCE_MANIFEST if package.manifest else SOURCE_DIRECTORY_STRUCTURE,
                confidence=CONFIDENCE_STRONG if package.manifest else CONFIDENCE_MEDIUM,
                evidence=package.evidence, path=package.path,
                observed_commit=self.observed_commit,
            ))
        facts.extend(self.public_surface_hints)
        facts.extend(self.internal_surface_hints)
        facts.extend(self.architecture_facts)
        return facts

    def lookup(
        self,
        *,
        files: list[str] | None = None,
        symbols: list[str] | None = None,
        concepts: list[str] | None = None,
        limit: int = 5,
    ) -> list[RepoFact]:
        """The few facts relevant to this change, best first.

        Deterministic lexical scoring — no embeddings, no LLM, no network.
        Ranked by: exact path match, containing package/prefix match,
        symbol/path token overlap, then concept overlap. Facts scoring
        nothing at all are omitted rather than padding the result, and the
        result is de-duplicated by `key` so one package cannot contribute
        the same fact twice.
        """
        files = [item for item in (files or []) if item]
        query_tokens = _tokens(" ".join((symbols or []) + (concepts or [])))

        scored: list[tuple[float, int, RepoFact]] = []
        for position, fact in enumerate(self.all_facts()):
            score = 0.0
            # A path-scoped fact earns relevance only through real path
            # containment. Incidental token overlap deliberately does not
            # count here: sibling packages share their parent directory's
            # name (`packages/core` vs `packages/admin-ui`), so token
            # overlap on a path would make every sibling look relevant to
            # every change.
            if fact.path:
                for file in files:
                    if file == fact.path:
                        score += 10.0  # exact file match
                    elif file.startswith(fact.path.rstrip("/") + "/"):
                        score += 6.0  # containing package/workspace
            # Symbol/concept overlap against the fact's own text — this is
            # what lets a non-path fact (a framework) match at all.
            score += 1.5 * len(_tokens(f"{fact.value} {fact.key}") & query_tokens)
            if score > 0:
                # A tiebreaker among facts that already matched, never a
                # reason for an unmatched fact to be returned.
                if fact.confidence == CONFIDENCE_STRONG:
                    score += 0.5
                scored.append((score, position, fact))

        scored.sort(key=lambda item: (-item[0], item[1]))
        out: list[RepoFact] = []
        seen: set[str] = set()
        for _score, _position, fact in scored:
            if fact.key in seen:
                continue
            seen.add(fact.key)
            out.append(fact)
            if len(out) >= limit:
                break
        return out


def _tokens(text: str) -> frozenset[str]:
    out: set[str] = set()
    for match in _TOKEN_RE.finditer(text or ""):
        word = match.group(0).lower()
        if len(word) > 1:
            out.add(word)
    return frozenset(out)


# --------------------------------------------------------------------------
# Deterministic extraction
# --------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_toml(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError, ValueError):
        return None


def _classify_node_package(payload: dict[str, Any]) -> tuple[str, str | None, list[str], list[str]]:
    """`(role, kind, frameworks, evidence)` for a `package.json`.

    Dependency evidence decides the role; a directory name never does here.
    An unrecognized package stays `unknown` rather than being guessed.
    """
    deps: dict[str, Any] = {}
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        value = payload.get(section)
        if isinstance(value, dict):
            deps.update(value)

    frameworks = sorted({
        name for dep, name in _FRONTEND_DEPENDENCIES.items() if dep in deps
    } | {
        name for dep, name in _BACKEND_DEPENDENCIES.items() if dep in deps
    })
    frontend_hits = sorted({_FRONTEND_DEPENDENCIES[d] for d in deps if d in _FRONTEND_DEPENDENCIES})
    backend_hits = sorted({_BACKEND_DEPENDENCIES[d] for d in deps if d in _BACKEND_DEPENDENCIES})

    evidence: list[str] = []
    if frontend_hits and not backend_hits:
        evidence.append(f"package.json depends on {', '.join(frontend_hits)}")
        return ROLE_FRONTEND, None, frameworks, evidence
    if backend_hits and not frontend_hits:
        evidence.append(f"package.json depends on {', '.join(backend_hits)}")
        return ROLE_BACKEND, None, frameworks, evidence

    # No framework signal: a package declaring public entry points and not
    # marked private is a publishable library — an explicit statement of
    # intent, not an inference.
    if payload.get("private") is not True and (payload.get("exports") or payload.get("main")):
        evidence.append("package.json is publishable with declared entry points")
        return ROLE_LIBRARY, "publishable", frameworks, evidence
    return ROLE_UNKNOWN, None, frameworks, evidence


def _classify_python_package(payload: dict[str, Any]) -> tuple[str, str | None, list[str], list[str]]:
    project = payload.get("project") if isinstance(payload.get("project"), dict) else {}
    deps = [
        str(item).lower() for item in (project.get("dependencies") or [])
        if isinstance(item, str)
    ]
    frameworks = sorted({
        name for dep, name in _BACKEND_DEPENDENCIES.items()
        if any(item.startswith(dep) for item in deps)
    })
    evidence: list[str] = []
    if frameworks:
        evidence.append(f"pyproject.toml depends on {', '.join(frameworks)}")
        return ROLE_BACKEND, None, frameworks, evidence
    if project.get("scripts") or project.get("entry-points"):
        evidence.append("pyproject.toml declares console entry points")
        return ROLE_APPLICATION, None, frameworks, evidence
    if project.get("name"):
        evidence.append("pyproject.toml declares a distributable project")
        return ROLE_LIBRARY, "distributable", frameworks, evidence
    return ROLE_UNKNOWN, None, frameworks, evidence


def _classify_rust_package(payload: dict[str, Any]) -> tuple[str, str | None, list[str], list[str]]:
    deps = payload.get("dependencies") if isinstance(payload.get("dependencies"), dict) else {}
    frameworks = sorted({
        name for dep, name in _BACKEND_DEPENDENCIES.items() if dep in deps
    })
    evidence: list[str] = []
    if frameworks:
        evidence.append(f"Cargo.toml depends on {', '.join(frameworks)}")
        return ROLE_BACKEND, None, frameworks, evidence
    # Cargo states crate type explicitly: a `[[bin]]` target is an
    # application, a `[lib]` target is a library.
    if payload.get("bin"):
        evidence.append("Cargo.toml declares a [[bin]] target")
        return ROLE_APPLICATION, "binary", frameworks, evidence
    if payload.get("lib"):
        evidence.append("Cargo.toml declares a [lib] target")
        return ROLE_LIBRARY, "crate", frameworks, evidence
    return ROLE_UNKNOWN, None, frameworks, evidence


def _package_for_manifest(root: Path, manifest_rel: str) -> tuple[RepoPackage, list[str]] | None:
    """One package from one manifest path, or `None` when the manifest is
    not one this v1 understands. Returns `(package, frameworks)`."""
    name_lower = Path(manifest_rel).name.lower()
    package_path = str(Path(manifest_rel).parent.as_posix())
    if package_path == ".":
        package_path = ""
    full = root / manifest_rel

    if name_lower == "package.json":
        payload = _read_json(full)
        if payload is None:
            return None
        role, kind, frameworks, evidence = _classify_node_package(payload)
        name = payload.get("name") if isinstance(payload.get("name"), str) else None
    elif name_lower == "pyproject.toml":
        payload = _read_toml(full)
        if payload is None:
            return None
        role, kind, frameworks, evidence = _classify_python_package(payload)
        project = payload.get("project") if isinstance(payload.get("project"), dict) else {}
        name = project.get("name") if isinstance(project.get("name"), str) else None
    elif name_lower == "cargo.toml":
        payload = _read_toml(full)
        if payload is None:
            return None
        role, kind, frameworks, evidence = _classify_rust_package(payload)
        package_section = payload.get("package") if isinstance(payload.get("package"), dict) else {}
        name = package_section.get("name") if isinstance(package_section.get("name"), str) else None
    elif name_lower == "go.mod":
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        match = re.search(r"^module\s+(\S+)", text, re.MULTILINE)
        name = match.group(1) if match else None
        role, kind, frameworks, evidence = ROLE_UNKNOWN, None, [], []
        for dep, framework in _BACKEND_DEPENDENCIES.items():
            if dep in text:
                role, frameworks = ROLE_BACKEND, [framework]
                evidence = [f"go.mod requires {framework}"]
                break
        if role == ROLE_UNKNOWN:
            evidence = ["go.mod declares a module"]
    else:
        return None

    return (
        RepoPackage(
            path=package_path, name=name, role=role, kind=kind,
            manifest=manifest_rel, evidence=tuple(evidence),
        ),
        frameworks,
    )


def _surface_hints(root: Path, packages: list[RepoPackage]) -> tuple[list[RepoFact], list[RepoFact]]:
    """Public/internal surface hints, deliberately modest and grounded.

    Only two v1 sources, both explicit statements of intent rather than
    inferences: a package manifest that declares itself publishable, and
    Go's `internal/` convention, which the language itself enforces.
    """
    public: list[RepoFact] = []
    internal: list[RepoFact] = []
    for package in packages:
        if package.role == ROLE_LIBRARY and package.kind in {"publishable", "distributable", "crate"}:
            public.append(RepoFact(
                key=f"public_surface:{package.path}",
                value="is a publishable library package (its exports are a public surface)",
                source=SOURCE_MANIFEST, confidence=CONFIDENCE_STRONG,
                evidence=package.evidence, path=package.path,
            ))
    try:
        for candidate in sorted(root.glob("**/internal")):
            if not candidate.is_dir():
                continue
            relative = candidate.relative_to(root).as_posix()
            if any(part in {"node_modules", ".git", "vendor"} for part in Path(relative).parts):
                continue
            internal.append(RepoFact(
                key=f"internal_surface:{relative}",
                value="is internal-only by Go package convention (not importable outside its parent)",
                source=SOURCE_SOURCE_LAYOUT, confidence=CONFIDENCE_STRONG,
                evidence=(f"{relative} follows the Go internal/ convention",), path=relative,
            ))
            if len(internal) >= 10:
                break
    except OSError:
        pass
    return public, internal


def _normalize_repo_map_for_repo(
    repo_map: dict[str, Any] | None, repo_identity: str,
) -> dict[str, Any]:
    """The one place `repo_map`'s two possible shapes are told apart.

    `build_repo_map()` (single-repo) returns `{"repo": ..., "manifests": [...],
    "extension_counts": {...}, "folders": [...], ...}` directly.
    `build_repo_map_batch()` — what `StructuralFacts.repo_map` actually holds
    on the normal `analyze_change` path — wraps one of those per repository
    under `{"repos": [...]}` instead. `build_repo_profile` only ever reads
    `manifests`/`extension_counts`/`folders`, which exist on the single-repo
    shape; called with the batch shape unnormalized, every one of those
    lookups silently sees `[]`/`{}` and the profile ends up looking empty
    (`packages=[]`, `frameworks=[]`) even though the facts were right there.

    Batch shape narrows to the entry whose own `"repo"` field matches
    `repo_identity` exactly — never the first entry, which would silently
    profile the wrong repository in a multi-repo run. No match found (or an
    empty/malformed payload) returns `{}`, the same conservative empty map
    `build_repo_profile` already treats as "nothing to extract from".
    """
    if not isinstance(repo_map, dict):
        return {}
    repos = repo_map.get("repos")
    if not isinstance(repos, list):
        return repo_map  # already single-repo shaped; unchanged
    for entry in repos:
        if isinstance(entry, dict) and entry.get("repo") == repo_identity:
            return entry
    return {}


def build_repo_profile(
    *,
    repo_root: Path,
    repo_identity: str,
    observed_commit: str | None = None,
    repo_map: dict[str, Any] | None = None,
) -> RepoProfile:
    """Build the profile deterministically from manifests and structure.

    Reuses `repo_map`'s already-computed manifest list and extension counts
    when supplied (the normal path — `StructuralFacts.repo_map`), so no
    second repository walk happens. Reads only manifest files, never source.
    No LLM call, no CBM call.
    """
    root = Path(repo_root).expanduser().resolve()
    repo_map = _normalize_repo_map_for_repo(repo_map, repo_identity)
    manifests = [str(item) for item in repo_map.get("manifests") or []]

    packages: list[RepoPackage] = []
    frameworks: set[str] = set()
    for manifest_rel in manifests:
        found = _package_for_manifest(root, manifest_rel)
        if found is None:
            continue
        package, package_frameworks = found
        packages.append(package)
        frameworks.update(package_frameworks)
    packages.sort(key=lambda item: item.path)

    from sydes.discover.route_index import LANGUAGE_BY_EXT

    languages = sorted({
        LANGUAGE_BY_EXT[ext]
        for ext in (repo_map.get("extension_counts") or {})
        if ext in LANGUAGE_BY_EXT
    })

    # Test and tooling roots, from the existing shared classifier and from
    # directory convention — never a second test-path detector.
    test_roots: set[str] = set()
    tooling_roots: set[str] = set()
    for folder in repo_map.get("folders") or []:
        path = str(folder.get("path") or "")
        if not path:
            continue
        if classify_candidate_file_role(f"{path}/x.py") == FILE_ROLE_TEST_USAGE_CANDIDATE:
            test_roots.add(path)
        elif Path(path).name.lower() in _TOOLING_DIR_NAMES and len(Path(path).parts) <= 2:
            tooling_roots.add(path)

    # A package rooted at a test/tooling root takes that role; a manifest
    # rarely says "this workspace is tests", but the layout does.
    adjusted: list[RepoPackage] = []
    for package in packages:
        if package.role == ROLE_UNKNOWN and package.path in test_roots:
            package = RepoPackage(
                path=package.path, name=package.name, role=ROLE_TESTS, kind=package.kind,
                manifest=package.manifest,
                evidence=package.evidence + (f"{package.path} is a test root",),
            )
        elif package.role == ROLE_UNKNOWN and package.path in tooling_roots:
            package = RepoPackage(
                path=package.path, name=package.name, role=ROLE_TOOLING, kind=package.kind,
                manifest=package.manifest,
                evidence=package.evidence + (f"{package.path} is a tooling root",),
            )
        adjusted.append(package)

    public_hints, internal_hints = _surface_hints(root, adjusted)
    architecture_facts: list[RepoFact] = []
    for framework in sorted(frameworks):
        architecture_facts.append(RepoFact(
            key=f"framework:{framework}", value=f"uses {framework}",
            source=SOURCE_MANIFEST, confidence=CONFIDENCE_STRONG,
            evidence=(f"a manifest declares a {framework} dependency",),
            observed_commit=observed_commit,
        ))

    return RepoProfile(
        schema_version=SCHEMA_VERSION,
        repo_identity=repo_identity,
        observed_commit=observed_commit,
        languages=languages,
        packages=adjusted,
        frameworks=sorted(frameworks),
        test_roots=sorted(test_roots),
        tooling_roots=sorted(tooling_roots),
        public_surface_hints=public_hints,
        internal_surface_hints=internal_hints,
        architecture_facts=architecture_facts,
    )


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def profile_path(workspace_id: str, root: Path | None = None) -> Path:
    """Where a workspace's profile lives — beside the workspace's runs and
    artifacts, not inside any single run, because it outlives all of them."""
    return ensure_workspace(workspace_id, root).workspace_dir / PROFILE_FILENAME


def save_repo_profile(profile: RepoProfile, workspace_id: str, root: Path | None = None) -> Path | None:
    """Persist the profile. Returns `None` on failure rather than raising —
    a profile Sydes could not save must never break a verification run."""
    try:
        target = profile_path(workspace_id, root)
        target.write_text(json.dumps(profile.to_dict(), indent=2) + "\n", encoding="utf-8")
        return target
    except OSError:
        return None


def load_repo_profile(workspace_id: str, root: Path | None = None) -> RepoProfile | None:
    """Load a persisted profile, or `None` if absent/unreadable/stale-schema."""
    try:
        target = profile_path(workspace_id, root)
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        # A profile written by a different schema is not silently trusted.
        return None
    return RepoProfile.from_dict(raw)


def get_or_build_repo_profile(
    *,
    repo_root: Path,
    repo_identity: str,
    workspace_id: str,
    observed_commit: str | None = None,
    repo_map: dict[str, Any] | None = None,
    root: Path | None = None,
) -> tuple[RepoProfile | None, list[str]]:
    """Load a current profile, or rebuild and persist one.

    v1 invalidation is deliberately simple: reuse a stored profile only when
    its `observed_commit` matches this run's, otherwise do a full
    deterministic rebuild. Correctness over clever caching — a rebuild is
    cheap (manifests only) and stale facts must never be silently
    authoritative.

    Never raises: any failure yields `(None, notes)` and the caller
    continues exactly as it did before profiles existed.
    """
    try:
        existing = load_repo_profile(workspace_id, root)
        if (
            existing is not None
            and existing.repo_identity == repo_identity
            and existing.observed_commit == observed_commit
        ):
            return existing, ["repo_profile: reused persisted profile"]

        profile = build_repo_profile(
            repo_root=repo_root, repo_identity=repo_identity,
            observed_commit=observed_commit, repo_map=repo_map,
        )
        saved = save_repo_profile(profile, workspace_id, root)
        note = (
            f"repo_profile: rebuilt ({len(profile.packages)} package(s), "
            f"{len(profile.frameworks)} framework(s))"
        )
        if saved is None:
            note += " — could not persist"
        return profile, [note]
    except Exception as exc:  # noqa: BLE001 - a profile must never break a run
        return None, [f"repo_profile unavailable: {exc}"]
