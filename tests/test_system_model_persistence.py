"""Persistent system model MVP (`sydes.store.system_model` /
`sydes.verify.system_model_reconcile`): canonical Flow/Symbol entities, a
REACHES relation, and VerificationRecord history, gated behind
`--persist-system-model` (off by default; nothing here changes existing
behavior unless explicitly opted into).

v1 safety rule under test throughout: the test-suite-skip optimization may
ONLY fire when the current run's `head` commit is EXACTLY the commit a
record was established under (plus an unchanged tracked fingerprint) —
never across a different commit, even when the fingerprint still matches.
Test outcomes can depend on things outside the tracked fingerprint
(fixtures, config, lockfiles, migrations, environment), so cross-commit
reuse is deliberately not attempted in v1: a different commit always
re-runs the suite, and the prior record is retained purely as history.

Reuses the exact repo/fixture shape already established in
`test_verify_change_semantics.py` (a hand-rolled Python "framework" with a
POST /students route gaining input validation) rather than inventing a new
one.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sydes.cli.main import app
from sydes.store.system_model import (
    SystemModelStore,
    VerificationRecord,
    compute_fingerprint,
    verification_record_id,
)
from sydes.verify.test_execution import run_ci_suite as _real_run_ci_suite
from sydes.verify.models import VERIFICATION_PASSED, ChangeVerificationResult

runner = CliRunner()

_FRAMEWORK_STUB = '''class APIRouter:
    def __init__(self, prefix=""):
        self.prefix = prefix

    def post(self, path):
        def _wrap(fn):
            return fn
        return _wrap


class App:
    def include_router(self, router):
        return router


class HttpError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
'''

_DB_STUB = '''class _Db:
    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(row)

    def commit(self):
        return True


db = _Db()
'''

_CRUD = '''def create_student(payload):
    student = {"name": payload["name"]}
    db.add(student)
    db.commit()
    return student
'''

_MAIN = '''from framework import App
from routers import students

app = App()
app.include_router(students.router)
'''

_CONFTEST = '''import pytest

import crud
from routers import students


class _Client:
    def post(self, path, json):
        try:
            return _Response(200, students.create_student(json))
        except Exception as exc:  # noqa: BLE001
            return _Response(getattr(exc, "code", 500), {"error": str(exc)})


class _Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def get_json(self):
        return self._body


@pytest.fixture
def client():
    return _Client()
'''

_ROUTES_BASE = '''from framework import APIRouter

import crud

router = APIRouter(prefix="/students")


@router.post("")
def create_student(payload):
    """Create a student."""
    return crud.create_student(payload)
'''

_ROUTES_WITH_VALIDATION = '''from framework import APIRouter

import crud

router = APIRouter(prefix="/students")


@router.post("")
def create_student(payload):
    """Create a student."""
    if not payload.get("name", "").strip():
        raise HttpError(400, "Student name cannot be blank")
    return crud.create_student(payload)
'''

_STUDENTS_TEST = (
    "def test_create_student_succeeds(client):\n"
    '    response = client.post("/students", json={"name": "Ada"})\n'
    "    assert response.status_code == 200\n"
    "\n"
    "def test_blank_name_is_rejected(client):\n"
    '    response = client.post("/students", json={"name": "   "})\n'
    "    assert response.status_code == 400\n"
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, text=True)


def _head_sha(root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(root), check=True, capture_output=True, text=True,
    ).stdout.strip()


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A committed Python repo whose route composes through a shared prefix
    -- identical shape to `test_verify_change_semantics.py`'s `repo`
    fixture, duplicated here (not imported) so this test file stays
    self-contained per the established per-file-fixture convention."""
    root = tmp_path / "svc"
    _write(root, "pyproject.toml", '[project]\nname = "svc"\ndependencies = ["pytest"]\n')
    _write(root, "framework.py", _FRAMEWORK_STUB)
    _write(root, "database.py", _DB_STUB)
    _write(root, "crud.py", "from database import db\n\n\n" + _CRUD)
    _write(root, "routers/__init__.py", "")
    _write(root, "routers/students.py", "from framework import HttpError\n" + _ROUTES_BASE)
    _write(root, "main.py", _MAIN)
    _write(root, "conftest.py", _CONFTEST)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "initial")
    return root


def _apply_validation_and_test(root: Path) -> None:
    _write(root, "routers/students.py", "from framework import HttpError\n" + _ROUTES_WITH_VALIDATION)
    _write(root, "tests/test_students.py", _STUDENTS_TEST)


def _run(root: Path, tmp_path: Path, base: str, out_name: str) -> ChangeVerificationResult:
    out = tmp_path / out_name
    outcome = runner.invoke(
        app,
        [
            "verify-change", "--base", base, "--llm-policy", "never",
            "--repo", f"svc={root}", "--json", str(out), "--persist-system-model",
        ],
    )
    assert outcome.exit_code == 0, outcome.output
    return ChangeVerificationResult.model_validate(json.loads(out.read_text(encoding="utf-8")))


def _obligations(result: ChangeVerificationResult):
    return [item for flow in result.affected_flows for item in flow.obligations]


def _system_model_path(sydes_home: Path, result: ChangeVerificationResult) -> Path:
    """Workspace ids are content-addressed from repo inputs, so any run
    against the same `svc=<root>` repo lands in the same workspace file --
    locate it by scanning rather than recomputing the hash ourselves,
    keeping this test decoupled from `compute_workspace_id`'s internals."""
    matches = list((sydes_home / "workspaces").glob("*/system_model.json"))
    assert len(matches) == 1, f"expected exactly one system_model.json, found {matches}"
    return matches[0]


# --------------------------------------------------------------------------
# 1. Identical head rerun: skip the suite, restore the record
# --------------------------------------------------------------------------

def test_identical_head_rerun_skips_the_suite_and_restores_the_record(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_sha = _head_sha(repo)
    _apply_validation_and_test(repo)
    _git(repo, "add", "."), _git(repo, "commit", "-qm", "add validation + test")

    first = _run(repo, tmp_path, base_sha, "run1.json")
    passed = [item for item in _obligations(first) if item.status == VERIFICATION_PASSED]
    assert passed, [(item.kind, item.status) for item in _obligations(first)]
    assert not any("restored" in note for note in first.notes)

    call_count = {"n": 0}

    def _spy(*args, **kwargs):
        call_count["n"] += 1
        return _real_run_ci_suite(*args, **kwargs)

    monkeypatch.setattr("sydes.verify.analyzer.run_ci_suite", _spy)

    second = _run(repo, tmp_path, base_sha, "run2.json")
    assert call_count["n"] == 0, "run_ci_suite must not be called on an identical-head rerun"
    passed_again = [item for item in _obligations(second) if item.status == VERIFICATION_PASSED]
    assert passed_again
    assert any("restored" in note and "not re-executed" in note for note in second.notes)


# --------------------------------------------------------------------------
# 2. A later commit touching the same handler: rerun normally, append history
# --------------------------------------------------------------------------

def test_new_commit_touching_the_same_dependency_reruns_and_appends_history(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_sha = _head_sha(repo)
    _apply_validation_and_test(repo)
    _git(repo, "add", "."), _git(repo, "commit", "-qm", "add validation + test")
    first = _run(repo, tmp_path, base_sha, "run1.json")
    passed = [item for item in _obligations(first) if item.status == VERIFICATION_PASSED]
    assert passed

    # A second commit modifying the SAME handler file (a different error message).
    _write(
        repo, "routers/students.py",
        "from framework import HttpError\n" + _ROUTES_WITH_VALIDATION.replace(
            "Student name cannot be blank", "Name is required",
        ),
    )
    _git(repo, "add", "."), _git(repo, "commit", "-qm", "tweak validation message")

    call_count = {"n": 0}

    def _spy(*args, **kwargs):
        call_count["n"] += 1
        return _real_run_ci_suite(*args, **kwargs)

    monkeypatch.setattr("sydes.verify.analyzer.run_ci_suite", _spy)

    second = _run(repo, tmp_path, base_sha, "run2.json")
    assert call_count["n"] == 1, "a different head commit must always re-run the suite"
    assert not any("restored" in note for note in second.notes)

    passed_obligation = next(item for item in _obligations(first) if item.status == VERIFICATION_PASSED)
    flow = next(f for f in first.affected_flows if passed_obligation in f.obligations)
    record_id = verification_record_id(flow.id, passed_obligation.kind, passed_obligation.statement)

    import os
    system_model_path = _system_model_path(Path(os.environ["SYDES_HOME"]).expanduser(), first)
    store = SystemModelStore(system_model_path)
    store.load()
    history = store.history_for(record_id)
    assert len(history) == 2, "the first run's record plus the second commit's fresh one"
    assert history[0].established_at_commit != history[1].established_at_commit


# --------------------------------------------------------------------------
# 3. An unrelated commit: prior record preserved, flow not reconstructed
# --------------------------------------------------------------------------

def test_unrelated_commit_preserves_prior_history_without_cross_commit_skip(
    repo: Path, tmp_path: Path,
) -> None:
    import os

    base_sha = _head_sha(repo)
    _apply_validation_and_test(repo)
    _git(repo, "add", "."), _git(repo, "commit", "-qm", "add validation + test")
    established_sha = _head_sha(repo)
    first = _run(repo, tmp_path, base_sha, "run1.json")
    passed_obligation = next(item for item in _obligations(first) if item.status == VERIFICATION_PASSED)
    flow = next(f for f in first.affected_flows if passed_obligation in f.obligations)
    record_id = verification_record_id(flow.id, passed_obligation.kind, passed_obligation.statement)

    # A wholly unrelated file, unrelated to the students route/handler.
    _write(repo, "unrelated.py", "VALUE = 1\n")
    _git(repo, "add", "."), _git(repo, "commit", "-qm", "unrelated change")

    second = _run(repo, tmp_path, established_sha, "run2.json")
    # The unrelated diff doesn't reach the students flow at all.
    assert not any(
        passed_obligation.kind == item.kind and passed_obligation.statement == item.statement
        for item in _obligations(second)
    )

    system_model_path = _system_model_path(Path(os.environ["SYDES_HOME"]).expanduser(), second)
    store = SystemModelStore(system_model_path)
    store.load()
    latest = store.latest_for(record_id)
    assert latest is not None
    assert latest.established_at_commit == established_sha
    assert latest.status == VERIFICATION_PASSED


# --------------------------------------------------------------------------
# 4. SystemModelStore unit tests (no repo/CLI needed)
# --------------------------------------------------------------------------

def test_two_distinct_same_kind_obligations_on_one_flow_get_distinct_ids() -> None:
    id_a = verification_record_id("flow:POST:/students", "validation", "rejects a blank name")
    id_b = verification_record_id("flow:POST:/students", "validation", "rejects a malformed email")
    assert id_a != id_b


def test_append_verification_record_retains_history_not_overwrite(tmp_path: Path) -> None:
    store = SystemModelStore(tmp_path / "system_model.json")
    record_id = verification_record_id("flow:x", "validation", "a claim")
    first = VerificationRecord(
        id=record_id, flow_id="flow:x", kind="validation", statement="a claim",
        status=VERIFICATION_PASSED, evidence=[], established_at_commit="sha1",
        input_files=[["svc", "a.py"]], input_fingerprint="fp1", run_id="run1", confirmed_at="t1",
    )
    second = VerificationRecord(
        id=record_id, flow_id="flow:x", kind="validation", statement="a claim",
        status=VERIFICATION_PASSED, evidence=[], established_at_commit="sha2",
        input_files=[["svc", "a.py"]], input_fingerprint="fp2", run_id="run2", confirmed_at="t2",
    )
    store.append_verification_record(first)
    store.append_verification_record(second)
    store.save()

    reloaded = SystemModelStore(store.path)
    reloaded.load()
    history = reloaded.history_for(record_id)
    assert [item.established_at_commit for item in history] == ["sha1", "sha2"]
    assert reloaded.latest_for(record_id).established_at_commit == "sha2"


# --------------------------------------------------------------------------
# 5. CLI artifact and analyzer-internal state must agree on the workspace id
# --------------------------------------------------------------------------

def test_relative_repo_path_lands_cli_artifact_and_system_model_in_same_workspace(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for a real bug: the CLI computed `workspace_id` from the
    raw `--repo` path while `analyzer.py` computed its own internal
    `workspace_id` from the `.resolve()`d path, so a relative `--repo` (a
    very common invocation, e.g. `--repo api=.`) could save
    `change_verification.json` in a different workspace directory than the
    one `SystemModelStore`/`FileFactStore` actually wrote to. Fixed by
    canonicalizing inside `compute_workspace_id` itself, so every caller
    agrees regardless of whether it pre-normalizes."""
    import os

    base_sha = _head_sha(repo)
    _apply_validation_and_test(repo)
    _git(repo, "add", "."), _git(repo, "commit", "-qm", "add validation + test")

    monkeypatch.chdir(tmp_path)
    out = tmp_path / "run.json"
    outcome = runner.invoke(
        app,
        [
            "verify-change", "--base", base_sha, "--llm-policy", "never",
            "--repo", "svc=svc", "--json", str(out), "--persist-system-model",
        ],
    )
    assert outcome.exit_code == 0, outcome.output
    result = ChangeVerificationResult.model_validate(json.loads(out.read_text(encoding="utf-8")))

    artifact_note = next(note for note in result.notes if note.startswith("Saved change verification artifact:"))
    artifact_path = Path(artifact_note.split(": ", 1)[1])
    # workspace_dir/artifacts/<run_id>/change_verification.json -> workspace_dir
    cli_workspace_dir = artifact_path.parent.parent.parent

    sydes_home = Path(os.environ["SYDES_HOME"]).expanduser()
    system_model_path = _system_model_path(sydes_home, result)

    assert system_model_path.parent == cli_workspace_dir, (
        f"CLI artifact workspace {cli_workspace_dir} != analyzer-internal "
        f"system_model workspace {system_model_path.parent} for the same relative --repo"
    )


def test_compute_fingerprint_treats_a_missing_dependency_file_as_not_present(tmp_path: Path) -> None:
    repo_root = tmp_path / "svc"
    repo_root.mkdir()
    (repo_root / "a.py").write_text("x = 1\n", encoding="utf-8")
    fingerprint_present, all_present = compute_fingerprint(
        [("svc", "a.py")], {"svc": repo_root},
    )
    assert all_present is True

    (repo_root / "a.py").unlink()
    fingerprint_missing, all_present_after_delete = compute_fingerprint(
        [("svc", "a.py")], {"svc": repo_root},
    )
    assert all_present_after_delete is False
    assert fingerprint_missing != fingerprint_present
