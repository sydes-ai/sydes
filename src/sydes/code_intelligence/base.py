"""The boundary between source-language intelligence and system reasoning.

Sydes answers questions about a *system*: which route serves this behavior,
what does it write to, is that behavior demonstrated by a test. Answering them
needs facts about *source language*: which files exist, what symbols they
define, where those symbols start and end, what they import and export.

Those are different problems. The second is language-general and open-ended —
every framework, dialect and syntax revision widens it — and Sydes should not
own it indefinitely. This module names the seam so a language-general engine
can supply those facts later without the verification layers noticing.

What crosses the seam is deliberately narrow: structural facts only. Route
composition, sink interpretation, obligations, evidence and verdicts stay on
the Sydes side, because they are claims about a system rather than readings of
a file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from sydes.core.models import RepoRef


class CodeIntelligenceError(RuntimeError):
    """A backend could not be selected or could not produce facts.

    Raised rather than degraded: a caller that silently fell back to a
    different backend would report facts from a source the operator did not
    choose, and no verdict built on them could be trusted.
    """


@dataclass
class StructuralFacts:
    """Structural facts for one repository set, from whichever backend.

    The payloads keep the shapes Sydes already produces and consumes. A second
    graph schema would buy nothing here and would have to be kept in step with
    the first one forever.
    """

    #: Repository/file inventory and candidate-directory signals.
    repo_map: dict[str, Any] = field(default_factory=dict)
    #: Per-file route declaration signals (framework-agnostic; composition is
    #: Sydes' own step and is not part of this payload).
    route_index: dict[str, Any] = field(default_factory=dict)
    #: Per-file symbols, spans, imports and exports.
    symbol_index: dict[str, Any] = field(default_factory=dict)
    #: Composed routes derived from `route_index`. Present here because today's
    #: native backend computes it; see the fact map in the task report for why
    #: it is classified as needing Sydes enrichment rather than generic.
    route_graph: dict[str, Any] = field(default_factory=dict)
    #: Backend-reported index metrics, for diagnostics only.
    metrics: dict[str, Any] = field(default_factory=dict)
    #: Human-readable metric lines for a diagnostics section.
    diagnostics: list[str] = field(default_factory=list)
    #: Which backend produced these facts.
    backend: str = "native"

    def symbols_for_file(self, repo: str, path: str) -> list[dict[str, Any]]:
        """Symbols defined in one file, or an empty list if it is not indexed.

        A file that is absent from the index is not a file without symbols, and
        callers that need to tell those apart should consult `indexed_files`.
        """
        for repo_index in self.symbol_index.get("repos", []) or []:
            if repo_index.get("repo") != repo:
                continue
            for item in repo_index.get("files", []) or []:
                if item.get("path") == path:
                    return list(item.get("symbols", []) or [])
        return []

    def indexed_files(self, repo: str | None = None) -> set[str]:
        """Paths the symbol index actually covers."""
        paths: set[str] = set()
        for repo_index in self.symbol_index.get("repos", []) or []:
            if repo is not None and repo_index.get("repo") != repo:
                continue
            for item in repo_index.get("files", []) or []:
                path = item.get("path")
                if isinstance(path, str):
                    paths.add(path)
        return paths


@runtime_checkable
class CodeIntelligence(Protocol):
    """A source of structural facts about a repository set."""

    name: str

    def build_or_update(
        self,
        repos: list[RepoRef],
        *,
        workspace_id: str | None = None,
        root: Path | None = None,
    ) -> StructuralFacts:
        """Produce current structural facts, reusing prior work where possible."""
        ...
