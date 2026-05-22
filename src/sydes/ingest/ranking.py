"""Heuristics for ranking candidate files for endpoint discovery."""

from __future__ import annotations

from pathlib import Path

from sydes.core.models import RankedFileCandidate, RepoInventory, RepoSenseSummary
from sydes.ingest.file_roles import (
    FILE_ROLE_DOCS_CANDIDATE,
    FILE_ROLE_SOURCE_ROUTE_CANDIDATE,
    FILE_ROLE_TEST_USAGE_CANDIDATE,
    classify_candidate_file_role,
)

PATH_SIGNAL_WEIGHTS = {
    "route": 2.0,
    "routes": 2.0,
    "router": 2.0,
    "controller": 1.8,
    "controllers": 1.8,
    "handler": 1.6,
    "handlers": 1.6,
    "api": 1.6,
    "view": 1.0,
    "views": 1.0,
    "endpoint": 1.6,
    "endpoints": 1.6,
    "server": 1.4,
    "main": 1.2,
    "app": 1.2,
}

BOOTSTRAP_FILENAME_WEIGHTS = {
    "main.py": 2.6,
    "app.py": 2.4,
    "server.py": 2.4,
    "server.js": 2.4,
    "server.ts": 2.4,
    "main.go": 2.6,
    "main.rs": 2.6,
    "index.js": 1.8,
    "index.ts": 1.8,
}

SOURCE_DIR_NAMES = {"src", "app", "api", "server", "backend", "services"}
EXTENSION_HINT_WEIGHTS = {
    ".py": 1.0,
    ".ts": 1.0,
    ".tsx": 0.8,
    ".js": 0.8,
    ".go": 1.0,
    ".rs": 1.0,
    ".java": 0.8,
    ".kt": 0.8,
}

EXTENSION_BY_LANGUAGE_FAMILY = {
    "python": {".py"},
    "typescript": {".ts", ".tsx"},
    "javascript": {".js", ".mjs", ".cjs"},
    "go": {".go"},
    "rust": {".rs"},
    "java": {".java"},
    "kotlin": {".kt"},
    "ruby": {".rb"},
    "php": {".php"},
    "dotnet": {".cs"},
    "scala": {".scala"},
}

ROLE_SCORE_ADJUSTMENTS = {
    FILE_ROLE_SOURCE_ROUTE_CANDIDATE: 0.5,
    FILE_ROLE_TEST_USAGE_CANDIDATE: -8.0,
    FILE_ROLE_DOCS_CANDIDATE: -8.0,
    "unknown": -0.3,
}

TEST_PATH_NEGATIVE_MARKERS = (
    "tests/",
    "test/",
    "__tests__/",
    "spec/",
)
DOC_PATH_NEGATIVE_MARKERS = (
    "docs/",
    "readme.md",
)
TEST_FILENAME_SUFFIXES = (
    ".test.ts",
    ".spec.ts",
    ".test.js",
    ".spec.js",
    ".test.jsx",
    ".spec.jsx",
    ".test.tsx",
    ".spec.tsx",
)


def _path_negative_weight(path: str) -> tuple[float, list[str]]:
    """Apply path-only negative heuristics for test/doc files."""
    lowered = path.replace("\\", "/").lower()
    filename = Path(lowered).name
    penalty = 0.0
    reasons: list[str] = []

    if any(marker in lowered for marker in TEST_PATH_NEGATIVE_MARKERS):
        penalty -= 5.0
        reasons.append("negative:test_path")
    if filename.startswith("test_") and filename.endswith(".py"):
        penalty -= 5.0
        reasons.append("negative:test_filename")
    if filename.endswith("_test.py"):
        penalty -= 5.0
        reasons.append("negative:test_filename")
    if any(filename.endswith(suffix) for suffix in TEST_FILENAME_SUFFIXES):
        penalty -= 5.0
        reasons.append("negative:test_suffix")

    if any(marker in lowered for marker in DOC_PATH_NEGATIVE_MARKERS):
        penalty -= 5.0
        reasons.append("negative:docs_path")
    if filename.endswith((".md", ".rst", ".adoc")):
        penalty -= 5.0
        reasons.append("negative:docs_suffix")

    return penalty, reasons


def _score_file(path: str, sense: RepoSenseSummary) -> tuple[float, list[str]]:
    """Score one file path and return score plus explainable reason labels."""
    score = 0.0
    reasons: list[str] = []
    p = Path(path)
    filename = p.name.lower()
    parts = [part.lower() for part in p.parts]
    suffix = p.suffix.lower()

    for token, weight in PATH_SIGNAL_WEIGHTS.items():
        if token in parts or token in filename:
            score += weight
            reasons.append(f"path:{token}")

    if filename in BOOTSTRAP_FILENAME_WEIGHTS:
        score += BOOTSTRAP_FILENAME_WEIGHTS[filename]
        reasons.append("bootstrap:filename")

    if any(part in SOURCE_DIR_NAMES for part in parts):
        score += 0.7
        reasons.append("location:source_dir")

    if len(parts) <= 2:
        score += 0.6
        reasons.append("location:top_level-ish")

    if suffix in EXTENSION_HINT_WEIGHTS:
        score += EXTENSION_HINT_WEIGHTS[suffix]
        reasons.append(f"extension:{suffix}")

    dominant_extensions = set(sense.dominant_extensions.keys())
    if suffix and suffix in dominant_extensions:
        score += 0.4
        reasons.append("extension:repo_dominant")

    preferred_extensions = {
        ext
        for family in sense.likely_language_families
        for ext in EXTENSION_BY_LANGUAGE_FAMILY.get(family, set())
    }
    if suffix and suffix in preferred_extensions:
        score += 0.6
        reasons.append("language:family_match")

    matched_backend_signals = [
        signal for signal in sense.backend_signals if signal in parts or signal in filename
    ]
    if matched_backend_signals:
        score += 0.8
        reasons.append("signal:backend_hint")

    role = classify_candidate_file_role(path)
    role_adjustment = ROLE_SCORE_ADJUSTMENTS.get(role, ROLE_SCORE_ADJUSTMENTS["unknown"])
    if role_adjustment:
        score += role_adjustment
        reasons.append(f"role:{role}")

    path_penalty, penalty_reasons = _path_negative_weight(path)
    if path_penalty:
        score += path_penalty
        reasons.extend(penalty_reasons)

    return score, sorted(set(reasons))


def rank_candidate_files(
    inventory: RepoInventory,
    sense: RepoSenseSummary,
    *,
    top_k: int = 60,
    min_score: float = 0.1,
) -> list[RankedFileCandidate]:
    """Rank inventory files for likely endpoint-entry discovery usefulness."""
    candidates: list[RankedFileCandidate] = []
    for item in inventory.files:
        score, reasons = _score_file(item.path, sense)
        if score < min_score:
            continue
        candidates.append(
            RankedFileCandidate(
                file=item.path,
                score=round(score, 3),
                reasons=reasons,
                role=classify_candidate_file_role(item.path),
                repo=inventory.repo,
            )
        )

    candidates.sort(key=lambda candidate: (-candidate.score, candidate.file))
    return candidates[:top_k]
