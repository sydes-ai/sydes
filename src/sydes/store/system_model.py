"""Persistent system model — the minimal MVP slice: canonical Flow/Symbol
entities, one REACHES relation, and VerificationRecord history.

Plain JSON, versioned, no database — the same local-first convention as
`workspace.py` and `discover/file_facts.py`, and deliberately narrow: this
module holds only what `system_model_reconcile.py` needs to demonstrate
persisted, cross-run verification knowledge, not a general graph store.

v1 safety rule (binding — see `VerificationRecord` and
`system_model_reconcile.should_skip_test_execution`): a persisted PASSED/
FAILED result is never treated as reusable across a *different* `head`
commit, even when its tracked input fingerprint still matches. Test
outcomes can depend on shared fixtures, config, lockfiles/dependency
versions, migrations, and environment — none of which are in that
fingerprint. Across commits, a record is retained purely as history and as
the basis for a staleness/revalidation-required signal; it is never used to
skip re-running the suite.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sydes.discover.file_facts import sha256_file
from sydes.store.workspace import ensure_workspace

#: Bumped on any incompatible change to the persisted shape below.
STORE_VERSION = "v1"
SYSTEM_MODEL_FILE = "system_model.json"


def normalize_statement(statement: str) -> str:
    """Same normalization `obligations._dedupe` already uses for its own
    `(kind, statement)` dedup key — reused here so this module's identity
    scheme never drifts from the one the rest of the codebase already
    trusts for "is this the same claim."""
    return re.sub(r"\s+", " ", statement.lower()).strip()


def verification_record_id(flow_id: str, kind: str, statement: str) -> str:
    """Stable identity for one (flow, obligation-kind, obligation-statement)
    triple. Deliberately NOT `(flow_id, kind)` alone: `derive_obligations`'s
    own dedup key proves two distinct obligations of the same kind can
    coexist on one flow (e.g. two different validation claims) — collapsing
    them here would silently merge unrelated verification history."""
    payload = f"{flow_id}\x1f{kind}\x1f{normalize_statement(statement)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def symbol_entity_id(repo: str, qualified_name: str) -> str:
    return f"{repo}:{qualified_name}"


@dataclass
class VerificationRecord:
    """One immutable, evidence-backed verification conclusion for one
    (flow, obligation) pair, as of one commit.

    Immutable by convention: a record is never edited in place once
    written. A later run either restores it unchanged (identical-head
    replay) or appends a NEW record for the same `id` reflecting the new
    commit's fresh result — see `SystemModelStore.history_for`. Staleness
    is never a field stored here; it is always computed on demand by
    comparing `input_fingerprint` against the current content of
    `input_files`, so a historical record stays a plain fact ("this was
    PASSED as of commit X") rather than something the code goes back and
    mutates.
    """

    id: str
    flow_id: str
    kind: str
    statement: str
    #: `VERIFICATION_PASSED` or `VERIFICATION_FAILED` only — an obligation
    #: that resolved UNVERIFIED/UNKNOWN has nothing durable to cache and is
    #: never persisted here (see `system_model_reconcile.reconcile`).
    status: str
    #: `[{"file": ..., "symbol": ..., "label": ..., "snippet": ...}, ...]`
    #: — `EvidenceRef.model_dump()` shape, stored as plain dicts so this
    #: module stays independent of the pydantic model layer.
    evidence: list[dict[str, Any]]
    #: `result.change.head` at the moment this record was written — the
    #: ONLY thing the v1 skip rule is allowed to compare for equality.
    established_at_commit: str
    #: `(repo, path)` pairs this record's validity depends on: the flow's
    #: route/handler files, every changed-symbol file the flow's trace
    #: reached, and every mapped test's own file.
    input_files: list[list[str]]
    #: SHA-256 over the sorted `"repo:path:content_sha256"` triples for
    #: every entry in `input_files`, computed at write time.
    input_fingerprint: str
    run_id: str
    confirmed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "flow_id": self.flow_id, "kind": self.kind,
            "statement": self.statement, "status": self.status,
            "evidence": self.evidence, "established_at_commit": self.established_at_commit,
            "input_files": self.input_files, "input_fingerprint": self.input_fingerprint,
            "run_id": self.run_id, "confirmed_at": self.confirmed_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> VerificationRecord:
        return cls(
            id=raw["id"], flow_id=raw["flow_id"], kind=raw["kind"],
            statement=raw["statement"], status=raw["status"],
            evidence=list(raw.get("evidence") or []),
            established_at_commit=raw.get("established_at_commit") or "",
            input_files=[list(item) for item in raw.get("input_files") or []],
            input_fingerprint=raw.get("input_fingerprint") or "",
            run_id=raw.get("run_id") or "", confirmed_at=raw.get("confirmed_at") or "",
        )


def compute_fingerprint(
    dependency_files: list[tuple[str, str]], repo_roots: dict[str, Path],
) -> tuple[str, bool]:
    """Hash every `(repo, path)` dependency file and fold them into one
    fingerprint. Returns `(fingerprint, all_present)` — `all_present` is
    `False` the instant any dependency file is missing (deleted, or its
    repo root unknown), which the caller must treat as unconditionally
    stale rather than erroring."""
    parts: list[str] = []
    all_present = True
    for repo, rel_path in dependency_files:
        root = repo_roots.get(repo)
        if root is None:
            all_present = False
            continue
        full_path = root / rel_path
        if not full_path.is_file():
            all_present = False
            continue
        parts.append(f"{repo}:{rel_path}:{sha256_file(full_path)}")
    fingerprint = hashlib.sha256("\n".join(sorted(parts)).encode("utf-8")).hexdigest()
    return fingerprint, all_present


class SystemModelStore:
    """Plain-JSON store for the persisted system model, one file per
    workspace (`~/.sydes/workspaces/<id>/system_model.json`, sibling to
    `runs/`/`artifacts/`/`facts/`)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, Any] = {}
        self.loaded = False

    @classmethod
    def for_workspace(cls, workspace_id: str, *, root: Path | None = None) -> SystemModelStore:
        paths = ensure_workspace(workspace_id=workspace_id, root=root)
        return cls(paths.workspace_dir / SYSTEM_MODEL_FILE)

    def load(self) -> None:
        raw: Any = {}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = {}
        if not isinstance(raw, dict):
            raw = {}
        raw.setdefault("store_version", STORE_VERSION)
        raw.setdefault("flows", {})
        raw.setdefault("symbols", {})
        raw.setdefault("relations", [])
        raw.setdefault("verification_records", {})
        self._data = raw
        self.loaded = True

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )

    def _ensure_loaded(self) -> None:
        if not self.loaded:
            self.load()

    # -- entities/relations (topology only, no verification meaning) ------

    def upsert_flow(self, flow_id: str, *, repo: str | None, label: str) -> None:
        self._ensure_loaded()
        self._data["flows"][flow_id] = {"id": flow_id, "repo": repo, "label": label}

    def upsert_symbol(self, symbol_id: str, *, repo: str, qualified_name: str) -> None:
        self._ensure_loaded()
        self._data["symbols"][symbol_id] = {
            "id": symbol_id, "repo": repo, "qualified_name": qualified_name,
        }

    def add_reaches_relation(self, flow_id: str, symbol_id: str) -> None:
        self._ensure_loaded()
        relations: list[dict[str, str]] = self._data["relations"]
        entry = {"kind": "REACHES", "from": flow_id, "to": symbol_id}
        if entry not in relations:
            relations.append(entry)

    # -- verification record history ---------------------------------------

    def history_for(self, record_id: str) -> list[VerificationRecord]:
        """Every record ever written for this id, oldest first."""
        self._ensure_loaded()
        raw_history = self._data["verification_records"].get(record_id, [])
        return [VerificationRecord.from_dict(item) for item in raw_history]

    def latest_for(self, record_id: str) -> VerificationRecord | None:
        history = self.history_for(record_id)
        return history[-1] if history else None

    def append_verification_record(self, record: VerificationRecord) -> None:
        """Always appends — never overwrites a prior entry. This is the
        entire mechanism behind "retain the old VerificationRecord as
        history": nothing in this module ever deletes or edits a prior
        entry for a given id."""
        self._ensure_loaded()
        history: list[dict[str, Any]] = self._data["verification_records"].setdefault(
            record.id, []
        )
        history.append(record.to_dict())


def new_run_timestamp() -> str:
    return datetime.now(tz=UTC).isoformat()
