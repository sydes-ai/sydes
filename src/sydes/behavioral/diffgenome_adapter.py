"""Obtain a `diffgenome-change/1` artifact: from a path, or by invoking DiffGenome.

Sydes never imports DiffGenome's collectors, graph or composer. The only contract
is the artifact. Invocation is a subprocess so a DiffGenome failure (unsupported
runtime, no tests, a collector crash, a timeout) is isolated and reported, never
raised into the verification pipeline.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ARTIFACT_FORMAT = "diffgenome-change/1"
ARTIFACT_FILENAME = "diffgenome-change.json"
STATUS_FILENAME = "diffgenome-status.json"  # written by DiffGenome when it produced no artifact
COMMAND_ENV_VAR = "SYDES_DIFFGENOME_COMMAND"
DEFAULT_COMMAND = "diffgenome"


class BehavioralUnavailable(Exception):
    """DiffGenome evidence could not be obtained; carries the reason for the report."""


@dataclass
class DiffGenomeRequest:
    repo_root: Path
    diff: str  # git revspec DiffGenome understands: base..head or a commit
    out_dir: Path
    runtime_args: list[str] = field(default_factory=list)  # forwarded opaquely
    probe_budget: int = 0  # 0: existing tests only, no LLM call
    attempts: int = 1
    writer: str = "none"
    timeout_seconds: float = 900.0
    command: list[str] | None = None


def resolve_command(explicit: list[str] | None = None) -> list[str]:
    if explicit:
        return list(explicit)
    spec = os.environ.get(COMMAND_ENV_VAR, "").strip()
    if spec:
        return shlex.split(spec)
    if shutil.which(DEFAULT_COMMAND) is None:
        # the `diffgenome` dependency installed next to Sydes, when its bin dir is not on PATH
        beside = Path(sys.executable).parent / DEFAULT_COMMAND
        if beside.exists():
            return [str(beside)]
    return [DEFAULT_COMMAND]


def load_artifact(path: Path) -> dict[str, Any]:
    if not Path(path).exists():
        status = Path(path).with_name(STATUS_FILENAME)
        if status.is_file():
            try:
                doc = json.loads(status.read_text(encoding="utf-8"))
                if isinstance(doc, dict) and doc.get("reason"):
                    raise BehavioralUnavailable(f"DiffGenome {doc.get('status', 'failed')}: {doc['reason']}")
            except ValueError:
                pass
        raise BehavioralUnavailable(
            f"behavioral artifact not found at {path} (the DiffGenome step produced none)"
        )
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BehavioralUnavailable(f"could not read behavioral artifact {path}: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("format") != ARTIFACT_FORMAT:
        raise BehavioralUnavailable(
            f"{path} is not a {ARTIFACT_FORMAT} artifact (format={doc.get('format') if isinstance(doc, dict) else '?'!r})"
        )
    return doc


def run_diffgenome(request: DiffGenomeRequest) -> tuple[dict[str, Any], list[str]]:
    """Invoke `diffgenome change` and load its artifact. Returns (artifact, log lines)."""
    if "--runtime" not in request.runtime_args:
        raise BehavioralUnavailable(
            "no DiffGenome runtime configuration given (--behavioral-args must name --runtime "
            "and the runtime's test location)"
        )
    command = resolve_command(request.command)
    if shutil.which(command[0]) is None and not Path(command[0]).exists():
        raise BehavioralUnavailable(
            f"DiffGenome is not installed: `{command[0]}` not found (set {COMMAND_ENV_VAR})"
        )
    argv = [
        *command, "change",
        "--repo", str(request.repo_root),
        "--diff", request.diff,
        "--out", str(request.out_dir),
        "--writer", request.writer if request.probe_budget > 0 else "none",
        "--probes", str(request.probe_budget),
        "--attempts", str(request.attempts),
        *request.runtime_args,
    ]
    env = {k: v for k, v in os.environ.items() if not k.startswith("SYDES_")}
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=request.timeout_seconds, env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BehavioralUnavailable(
            f"DiffGenome timed out after {request.timeout_seconds:.0f}s"
        ) from exc
    except OSError as exc:
        raise BehavioralUnavailable(f"DiffGenome could not be started: {exc}") from exc
    log = [line for line in proc.stderr.splitlines() if line.startswith("[diffgenome")]
    artifact_path = request.out_dir / ARTIFACT_FILENAME
    if proc.returncode != 0 or not artifact_path.is_file():
        tail = (proc.stderr.strip().splitlines() or proc.stdout.strip().splitlines() or ["(no output)"])[-1]
        tail = re.sub(r"^\[diffgenome [0-9:]+\]\s*", "", tail)
        raise BehavioralUnavailable(f"DiffGenome exited {proc.returncode}: {tail}")
    return load_artifact(artifact_path), log
