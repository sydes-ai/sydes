"""Helpers for parsing repository references from CLI input."""

from pathlib import Path

from sydes.core.models import RepoRef


def parse_repo_spec(spec: str) -> RepoRef:
    """Parse a single repository spec in the form ``name=path``."""
    if "=" not in spec:
        raise ValueError(
            f"Invalid --repo value '{spec}'. Expected format: name=path."
        )

    name, root = spec.split("=", 1)
    name = name.strip()
    root = root.strip()
    if not name or not root:
        raise ValueError(
            f"Invalid --repo value '{spec}'. Both name and path are required."
        )

    return RepoRef(name=name, root=root)


def parse_repo_specs(specs: list[str]) -> list[RepoRef]:
    """Parse multiple repository specs from repeated ``--repo`` flags."""
    return [parse_repo_spec(spec) for spec in specs]


def validate_repo_roots(repos: list[RepoRef]) -> list[RepoRef]:
    """Validate that repository roots exist and normalize them to absolute paths."""
    normalized: list[RepoRef] = []
    for repo in repos:
        root = Path(repo.root).expanduser().resolve()
        if not root.exists():
            raise ValueError(f"Repository root does not exist for '{repo.name}': {repo.root}")
        if not root.is_dir():
            raise ValueError(f"Repository root is not a directory for '{repo.name}': {repo.root}")
        normalized.append(RepoRef(name=repo.name, root=str(root)))
    return normalized


def summarize_repo_roots(repos: list[RepoRef]) -> list[str]:
    """Return concise repo root summary lines after validation."""
    validated = validate_repo_roots(repos)
    return [f"{repo.name}: {repo.root}" for repo in validated]
