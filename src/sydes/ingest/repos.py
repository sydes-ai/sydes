"""Helpers for parsing repository references from CLI input."""

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
