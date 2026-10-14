"""Case definitions for the hostile-repo benchmark round.

Every case is resolved through `ensure_case_worktree`
(`experiments/bench_common/git_worktree.py`) — one dedicated `git worktree`
per (source repo, commit), never a shared checkout re-pointed between
cases. That module's docstring has the full story: an earlier version of
this file pointed H2 and H3 (both from the strapi repo) at the same
physical checkout and `git checkout`-ed between them, which silently fed
several model runs the wrong commit's tree because runs execute
asynchronously. `ensure_case_worktree` makes that structurally impossible —
two different shas from the same source repo always get two different
directories — rather than relying on this file's author remembering to set
up worktrees by hand each time, which is what caused the bug in the first
place.

Ground truth for each case was hand-verified by direct repo inspection (see
the conversation for the full trace) before any model was run — never
derived from a model's own output.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "bench_common"))
from git_worktree import ensure_case_worktree  # noqa: E402

WORKTREES_ROOT = Path("/Users/ksnaik/sample_repos/.bench_worktrees")

# (source repo, commit, subdirectory to hand the agentic tools, label).
# `subdirectory` is relative to the worktree root: `""` for the whole repo
# (immich, where `repo_root` is the `server/` package directly), or a
# monorepo package path scoped narrowly enough for the bounded agentic
# tool loop to search in reasonable time.
_CASE_SPECS = {
    "hostile-h1-immich": {
        "source_repo": "/Users/ksnaik/sample_repos/immich",
        "sha": "25d44078ad8f05467d50bd5156e6fb49a5b8233e",
        "subdir": "server",
        "label": "monorepo + generated OpenAPI SDK + async worker lifecycle via NestJS event bus, not HTTP routing",
    },
    "hostile-h2-strapi": {
        "source_repo": "/Users/ksnaik/sample_repos/strapi",
        "sha": "d31768cb7e9f0f70025816e811316808d11e1079",
        # Scoped to packages/core (not the whole monorepo, which is
        # impractically large for the agentic tool loop) -- still spans
        # all 11 plugins that share the "bootstrap.ts" filename, which is
        # the point of this case.
        "subdir": "packages/core",
        "label": "same-name symbol collisions (bootstrap.ts x11 plugins) + newly-dead code + Koa framework",
    },
    "hostile-h3-strapi": {
        "source_repo": "/Users/ksnaik/sample_repos/strapi",
        "sha": "95a42d596260b27d52fef59860f49462a27db163",
        # The diff spans two packages (content-manager and utils); scoped
        # to their shared parent so both are visible to the agentic tools.
        "subdir": "packages/core",
        "label": "genuinely ambiguous: behavior is content-type-schema-dependent, not statically determinable",
    },
}


def _resolve_cases() -> dict[str, dict[str, str]]:
    resolved = {}
    for name, spec in _CASE_SPECS.items():
        worktree = ensure_case_worktree(
            source_repo=Path(spec["source_repo"]), sha=spec["sha"], worktrees_root=WORKTREES_ROOT,
        )
        resolved[name] = {
            "repo_root": str(worktree / spec["subdir"]) if spec["subdir"] else str(worktree),
            "git_root": spec["source_repo"],
            "sha": spec["sha"],
            "label": spec["label"],
        }
    return resolved


CASES = _resolve_cases()


def get_diff(git_root: str, sha: str) -> str:
    return subprocess.run(
        ["git", "show", sha],
        cwd=git_root, capture_output=True, text=True, check=True,
    ).stdout
