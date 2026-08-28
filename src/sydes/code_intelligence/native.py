"""Sydes' own parsers, behind the code-intelligence boundary.

A thin adapter and nothing more. It moves no parser, duplicates no extraction,
and changes no behavior: `build_structural_index` still does the work, and this
class only presents the result in the vocabulary of the boundary.

Keeping it thin is the point. When an external engine arrives, the difference
between backends should be visible here — in what each can supply — and not
spread through the verification layers.
"""

from __future__ import annotations

from pathlib import Path

from sydes.code_intelligence.base import StructuralFacts
from sydes.core.models import RepoRef
from sydes.discover.file_facts import build_structural_index, structural_index_diagnostics

NATIVE_BACKEND = "native"


class NativeCodeIntelligence:
    """Structural facts from Sydes' existing incremental index."""

    name = NATIVE_BACKEND

    def build_or_update(
        self,
        repos: list[RepoRef],
        *,
        workspace_id: str | None = None,
        root: Path | None = None,
        defer_edges: bool = False,
        changed_files_by_repo: dict[str, list[str]] | None = None,
    ) -> StructuralFacts:
        """Build or incrementally update the native structural index.

        `defer_edges` and `changed_files_by_repo` are accepted for interface
        parity and ignored: the native backend supplies no call graph and no
        fast/full indexing mode split, so neither applies here.
        """
        index = build_structural_index(repos, workspace_id=workspace_id, root=root)
        return StructuralFacts(
            repo_map=index.repo_map_batch,
            route_index=index.route_index_batch,
            symbol_index=index.handler_symbol_batch,
            route_graph=index.route_graph_facts,
            metrics=index.metrics.to_dict(),
            diagnostics=structural_index_diagnostics(index),
            backend=self.name,
        )
