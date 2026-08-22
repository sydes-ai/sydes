"""Semantic tests for the rebuilt verifier.

These characterize *verification meaning*, not framework parsing: which
obligations exist, which tests may satisfy them, and how a verdict aggregates.
System understanding comes from the shared stack; nothing here parses routes.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest
from typer.testing import CliRunner

from sydes.cli.main import app
from sydes.verify.analyzer import _analysis_status_from, _contextual_limit_notes
from sydes.verify.models import (
    ANALYSIS_COMPLETE,
    ANALYSIS_PARTIAL,
    ORIGIN_TRACE_SINK,
    OBLIGATION_VALIDATION,
    VERDICT_ACTION_REQUIRED,
    VERDICT_INCOMPLETE,
    VERDICT_VERIFIED,
    VERIFICATION_FAILED,
    VERIFICATION_PASSED,
    VERIFICATION_UNKNOWN,
    VERIFICATION_UNVERIFIED,
    ChangeVerificationResult,
)

runner = CliRunner()


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, text=True)


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


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


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A committed Python repo whose route composes through a shared prefix."""
    root = tmp_path / "svc"
    _write(root, "pyproject.toml", '[project]\nname = "svc"\ndependencies = ["pytest"]\n')
    _write(root, "framework.py", _FRAMEWORK_STUB)
    _write(root, "database.py", _DB_STUB)
    _write(root, "crud.py", "from database import db\n\n\n" + _CRUD)
    _write(root, "routers/__init__.py", "")
    _write(root, "routers/students.py", "from framework import HttpError\n" + _ROUTES_BASE)
    _write(root, "main.py", _MAIN)
    _write(root, "conftest.py", _CONFTEST)
    _write(root, ".env.example", "DATABASE_URL=postgres://localhost/app\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "initial")
    return root


def _apply_validation_change(root: Path) -> None:
    _write(root, "routers/students.py", "from framework import HttpError\n" + _ROUTES_WITH_VALIDATION)


def _run(root: Path, tmp_path: Path, *extra: str) -> ChangeVerificationResult:
    out = tmp_path / "result.json"
    outcome = runner.invoke(
        app,
        [
            "verify-change",
            "--base",
            "main",
            "--llm-policy",
            "never",
            "--repo",
            f"svc={root}",
            "--json",
            str(out),
            *extra,
        ],
    )
    assert outcome.exit_code == 0, outcome.output
    return ChangeVerificationResult.model_validate(json.loads(out.read_text(encoding="utf-8")))


def _obligations(result: ChangeVerificationResult):
    return [item for flow in result.affected_flows for item in flow.obligations]


def _validation_obligations(result: ChangeVerificationResult):
    return [item for item in _obligations(result) if item.kind == OBLIGATION_VALIDATION]


# --------------------------------------------------------------------------
# Case A — changed validation behavior, no matching test
# --------------------------------------------------------------------------


def test_case_a_changed_validation_without_a_test_is_unverified(repo: Path, tmp_path: Path) -> None:
    """A happy-path test passing must not satisfy a new validation obligation."""
    _write(
        repo,
        "tests/test_students.py",
        "def test_create_student_succeeds(client):\n"
        '    response = client.post("/students", json={"name": "Ada"})\n'
        "    assert response.status_code == 200\n",
    )
    _git(repo, "add", "."), _git(repo, "commit", "-qm", "tests")
    _apply_validation_change(repo)

    result = _run(repo, tmp_path)
    validation = _validation_obligations(result)

    assert validation, "expected a validation obligation from the changed branch"
    assert any(item.status == VERIFICATION_UNVERIFIED for item in validation)
    assert result.summary.verdict != VERDICT_VERIFIED


def test_case_a_reports_the_passing_test_as_supporting_only(repo: Path, tmp_path: Path) -> None:
    """The happy-path test is recorded as context, never as the missing evidence."""
    _write(
        repo,
        "tests/test_students.py",
        "def test_create_student_succeeds(client):\n"
        '    response = client.post("/students", json={"name": "Ada"})\n'
        "    assert response.status_code == 200\n",
    )
    _git(repo, "add", "."), _git(repo, "commit", "-qm", "tests")
    _apply_validation_change(repo)

    result = _run(repo, tmp_path)
    unverified = [
        item for item in _validation_obligations(result) if item.status == VERIFICATION_UNVERIFIED
    ]

    assert unverified
    assert any(item.supporting_tests for item in unverified)
    assert all(not item.mapped_tests for item in unverified)


# --------------------------------------------------------------------------
# Case B / C — a matching test exists
# --------------------------------------------------------------------------


def _write_blank_name_test(root: Path, expected_status: int) -> None:
    _write(
        root,
        "tests/test_students.py",
        "def test_create_student_succeeds(client):\n"
        '    response = client.post("/students", json={"name": "Ada"})\n'
        "    assert response.status_code == 200\n"
        "\n"
        "def test_blank_name_is_rejected(client):\n"
        '    response = client.post("/students", json={"name": "   "})\n'
        f"    assert response.status_code == {expected_status}\n",
    )


def test_case_b_matching_test_passing_marks_the_obligation_passed(
    repo: Path, tmp_path: Path
) -> None:
    """A test asserting the changed behavior's status satisfies that obligation."""
    _write_blank_name_test(repo, 400)
    _git(repo, "add", "."), _git(repo, "commit", "-qm", "tests")
    _apply_validation_change(repo)

    result = _run(repo, tmp_path)
    passed = [item for item in _obligations(result) if item.status == VERIFICATION_PASSED]

    assert passed, [
        (item.kind, item.status, item.statement) for item in _obligations(result)
    ]
    assert all(item.mapped_tests for item in passed)
    assert all(item.mapped_tests[0].match_rule for item in passed)


def test_case_c_matching_test_failing_marks_the_obligation_failed(
    repo: Path, tmp_path: Path
) -> None:
    """A failing mapped test fails its obligation and forces ACTION REQUIRED."""
    _write_blank_name_test(repo, 422)  # the code returns 400, so this fails
    _git(repo, "add", "."), _git(repo, "commit", "-qm", "tests")
    _apply_validation_change(repo)

    result = _run(repo, tmp_path)
    failed = [item for item in _obligations(result) if item.status == VERIFICATION_FAILED]

    assert failed
    assert result.summary.verdict == VERDICT_ACTION_REQUIRED


# --------------------------------------------------------------------------
# Case D — related but irrelevant tests
# --------------------------------------------------------------------------


def test_case_d_irrelevant_tests_never_satisfy_an_obligation(
    repo: Path, tmp_path: Path
) -> None:
    """Lexical overlap with flow symbols is not evidence for anything."""
    _write(
        repo,
        "tests/test_unrelated.py",
        "import crud\n"
        "\n"
        "def test_crud_module_has_create_student():\n"
        "    assert hasattr(crud, 'create_student')\n"
        "\n"
        "def test_data_client_session_words():\n"
        "    data = {'client': 1, 'session': 2, 'create': 3}\n"
        "    assert data['create'] == 3\n",
    )
    _git(repo, "add", "."), _git(repo, "commit", "-qm", "tests")
    _apply_validation_change(repo)

    result = _run(repo, tmp_path)

    assert result.summary.verdict != VERDICT_VERIFIED
    for obligation in _obligations(result):
        for test in obligation.mapped_tests:
            assert test.evidence_tier != "rejected"
            assert test.match_rule, "an accepted mapping must state its reason"


# --------------------------------------------------------------------------
# Case E — execution blocked by the environment
# --------------------------------------------------------------------------


def test_case_e_unexecutable_test_yields_unknown_with_a_blocker(
    repo: Path, tmp_path: Path
) -> None:
    """A missing dependency is `unknown` with a blocker, never `failed`."""
    _write_blank_name_test(repo, 400)
    _write(
        repo,
        "tests/test_students.py",
        "import a_module_that_does_not_exist\n\n"
        + (repo / "tests/test_students.py").read_text(encoding="utf-8"),
    )
    _git(repo, "add", "."), _git(repo, "commit", "-qm", "tests")
    _apply_validation_change(repo)

    result = _run(repo, tmp_path)
    unknown = [item for item in _obligations(result) if item.status == VERIFICATION_UNKNOWN]

    assert unknown, [(item.status, item.statement) for item in _obligations(result)]
    assert result.summary.verdict == VERDICT_INCOMPLETE
    assert result.ci_suite is not None
    assert result.ci_suite.blocker == "missing_dependency"
    assert result.ci_suite.status != VERIFICATION_FAILED


def test_no_run_tests_yields_unknown_not_verified(repo: Path, tmp_path: Path) -> None:
    """Disabling execution can never produce a VERIFIED verdict."""
    _write_blank_name_test(repo, 400)
    _git(repo, "add", "."), _git(repo, "commit", "-qm", "tests")
    _apply_validation_change(repo)

    result = _run(repo, tmp_path, "--no-run-tests")

    assert result.summary.verdict != VERDICT_VERIFIED
    assert result.ci_suite is None


# --------------------------------------------------------------------------
# Case F — partial analysis
# --------------------------------------------------------------------------


def test_case_f_partial_analysis_is_reported_and_blocks_verified(
    repo: Path, tmp_path: Path
) -> None:
    """Unparseable downstream source is incompleteness, not absence of effects."""
    _write_blank_name_test(repo, 400)
    _git(repo, "add", "."), _git(repo, "commit", "-qm", "tests")
    # Break the handler's own file so the shared trace cannot analyze the body.
    _write(
        repo,
        "routers/students.py",
        "from framework import HttpError\n" + _ROUTES_WITH_VALIDATION + "\ndef broken(\n",
    )

    result = _run(repo, tmp_path)

    assert result.analysis_status != ANALYSIS_COMPLETE or all(
        flow.analysis_status != ANALYSIS_COMPLETE for flow in result.affected_flows
    )
    assert result.summary.verdict != VERDICT_VERIFIED


def test_partial_analysis_does_not_assert_absence_of_side_effects(
    repo: Path, tmp_path: Path
) -> None:
    """A flow Sydes could not fully analyze must say so rather than look clean."""
    _write(
        repo,
        "routers/students.py",
        "from framework import HttpError\n" + _ROUTES_WITH_VALIDATION + "\ndef broken(\n",
    )

    result = _run(repo, tmp_path)

    partial = [
        flow for flow in result.affected_flows if flow.analysis_status != ANALYSIS_COMPLETE
    ]
    assert partial or result.analysis_status != ANALYSIS_COMPLETE
    for flow in partial:
        assert flow.analysis_notes, "incomplete analysis must explain itself"


# --------------------------------------------------------------------------
# Case G — mixed obligation outcomes
# --------------------------------------------------------------------------


def test_case_g_mixed_outcomes_yield_verification_incomplete(
    repo: Path, tmp_path: Path
) -> None:
    """Any unresolved obligation prevents VERIFIED, whatever else passes."""
    _write(
        repo,
        "tests/test_students.py",
        "def test_create_student_succeeds(client):\n"
        '    response = client.post("/students", json={"name": "Ada"})\n'
        "    assert response.status_code == 200\n",
    )
    _git(repo, "add", "."), _git(repo, "commit", "-qm", "tests")
    _apply_validation_change(repo)

    result = _run(repo, tmp_path)
    statuses = {item.status for item in _obligations(result)}

    assert VERIFICATION_UNVERIFIED in statuses
    assert result.summary.verdict == VERDICT_INCOMPLETE
    assert result.summary.counts.obligations_unverified >= 1


def test_verified_requires_every_obligation_resolved(repo: Path, tmp_path: Path) -> None:
    """VERIFIED is structurally impossible while an obligation is unresolved."""
    _write_blank_name_test(repo, 400)
    _git(repo, "add", "."), _git(repo, "commit", "-qm", "tests")
    _apply_validation_change(repo)

    result = _run(repo, tmp_path)
    unresolved = [
        item
        for item in _obligations(result)
        if item.required and item.status != VERIFICATION_PASSED
    ]

    if result.summary.verdict == VERDICT_VERIFIED:
        assert not unresolved
    else:
        assert unresolved


# --------------------------------------------------------------------------
# Structural guarantees
# --------------------------------------------------------------------------


def test_flow_status_never_exceeds_its_worst_obligation(repo: Path, tmp_path: Path) -> None:
    """A flow cannot report better than the weakest obligation it carries."""
    _write(
        repo,
        "tests/test_students.py",
        "def test_create_student_succeeds(client):\n"
        '    response = client.post("/students", json={"name": "Ada"})\n'
        "    assert response.status_code == 200\n",
    )
    _git(repo, "add", "."), _git(repo, "commit", "-qm", "tests")
    _apply_validation_change(repo)

    result = _run(repo, tmp_path)
    rank = {
        VERIFICATION_FAILED: 0,
        VERIFICATION_UNKNOWN: 1,
        VERIFICATION_UNVERIFIED: 2,
        VERIFICATION_PASSED: 3,
    }
    for flow in result.affected_flows:
        required = [item for item in flow.obligations if item.required]
        if required:
            assert rank[flow.status] <= min(rank[item.status] for item in required)


def test_artifact_references_shared_sources_rather_than_copying_topology(
    repo: Path, tmp_path: Path
) -> None:
    """Flows point at shared artifacts; obligations cite the refs they came from."""
    _apply_validation_change(repo)

    result = _run(repo, tmp_path)

    assert result.version == "v3"
    assert result.affected_flows
    flow = result.affected_flows[0]
    assert flow.artifact_refs.get("route_file")
    assert all(item.source_refs for item in flow.obligations)
    assert all(item.origin != "llm_hypothesis" for item in flow.obligations)


def test_code_findings_are_off_by_default(repo: Path, tmp_path: Path) -> None:
    """Advisory findings neither run nor influence the verdict unless requested."""
    _apply_validation_change(repo)

    result = _run(repo, tmp_path)

    assert result.code_findings == []
    assert result.summary.counts.code_findings == 0


# --------------------------------------------------------------------------
# Case H — sink material-impact classification
# --------------------------------------------------------------------------


def _sink_obligations(result: ChangeVerificationResult):
    return [item for item in _obligations(result) if item.origin == ORIGIN_TRACE_SINK]


def test_guard_only_change_leaves_downstream_sinks_contextual(
    repo: Path, tmp_path: Path
) -> None:
    """An upstream guard does not modify the persistence path it precedes."""
    _write_blank_name_test(repo, 400)
    _git(repo, "add", "."), _git(repo, "commit", "-qm", "tests")
    _apply_validation_change(repo)

    result = _run(repo, tmp_path)
    sinks = _sink_obligations(result)

    assert sinks, "the flow must still surface its downstream database effects"
    assert all(not item.required for item in sinks)
    assert all(item.reason for item in sinks), "a demoted sink must say why"


def test_satisfied_change_critical_obligations_can_reach_verified(
    repo: Path, tmp_path: Path
) -> None:
    """Contextual sinks must not block a change whose own behavior is proven."""
    # This flow's every required obligation must be demonstrable, so the stub
    # client answers with the contract's declared success status.
    _write(repo, "conftest.py", _CONFTEST.replace("_Response(200,", "_Response(201,"))
    _write(
        repo,
        "tests/test_students.py",
        "def test_create_student_succeeds(client):\n"
        '    response = client.post("/students", json={"name": "Ada"})\n'
        "    assert response.status_code == 201\n"
        "\n"
        "def test_blank_name_is_rejected(client):\n"
        '    response = client.post("/students", json={"name": "   "})\n'
        "    assert response.status_code == 400\n",
    )
    _git(repo, "add", "."), _git(repo, "commit", "-qm", "tests")
    _apply_validation_change(repo)

    result = _run(repo, tmp_path)
    required = [item for item in _obligations(result) if item.required]

    assert all(item.status == VERIFICATION_PASSED for item in required), [
        (item.status, item.statement) for item in required
    ]
    assert result.analysis_status == ANALYSIS_COMPLETE
    assert result.summary.verdict == VERDICT_VERIFIED


def test_change_to_the_sink_symbol_keeps_its_obligation_required(
    repo: Path, tmp_path: Path
) -> None:
    """Editing the function that produces a sink puts that sink in scope."""
    _write(
        repo,
        "crud.py",
        "from database import db\n\n\n" + _CRUD.replace(
            "db.add(student)", "db.add(student)\n    db.flush()"
        ),
    )

    result = _run(repo, tmp_path)
    sinks = _sink_obligations(result)

    assert sinks, "changing the data layer must still resolve its sinks"
    assert all(item.required for item in sinks), [
        (item.required, item.statement) for item in sinks
    ]


def test_unverified_required_sink_prevents_verified(repo: Path, tmp_path: Path) -> None:
    """No test asserts the changed persistence effect, so VERIFIED is unavailable."""
    _write(
        repo,
        "crud.py",
        "from database import db\n\n\n" + _CRUD.replace(
            "db.add(student)", "db.add(student)\n    db.flush()"
        ),
    )

    result = _run(repo, tmp_path)
    required_sinks = [item for item in _sink_obligations(result) if item.required]

    assert any(item.status == VERIFICATION_UNVERIFIED for item in required_sinks)
    assert result.summary.verdict != VERDICT_VERIFIED


# --------------------------------------------------------------------------
# Case I — what counts as incomplete analysis
# --------------------------------------------------------------------------


_TRUNCATION_NOTE = "1 contextual files were truncated by bounded read caps."


def test_bounded_context_truncation_alone_keeps_analysis_complete() -> None:
    """Optional context is truncated *after* deterministic parsing succeeded."""
    status, notes = _analysis_status_from([_TRUNCATION_NOTE])

    assert status == ANALYSIS_COMPLETE
    assert notes == []
    assert _contextual_limit_notes([_TRUNCATION_NOTE]) == [_TRUNCATION_NOTE]


@pytest.mark.parametrize(
    "note",
    [
        "python_parse_failed: unexpected EOF",
        "unresolved_call: crud.create_student",
        "route composition is unresolved",
        "import of crud could not be resolved",
    ],
)
def test_limitations_that_can_hide_topology_still_mark_analysis_partial(note: str) -> None:
    """Anything that could conceal a route, call, or sink remains PARTIAL."""
    status, notes = _analysis_status_from([note])

    assert status == ANALYSIS_PARTIAL
    assert notes == [note]
    assert _contextual_limit_notes([note]) == []


def test_truncation_does_not_mask_a_real_limitation() -> None:
    """A genuine marker still wins when both kinds of note are present."""
    status, notes = _analysis_status_from([_TRUNCATION_NOTE, "python_parse_failed: x"])

    assert status == ANALYSIS_PARTIAL
    assert notes == ["python_parse_failed: x"]
