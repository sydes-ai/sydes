"""Execution of the existing tests Sydes mapped to affected behavior.

This is the step that turns "a relevant test exists" into "that test passes /
fails". It runs only the tests the mapping stage already selected — it performs
no test selection of its own and never runs a whole suite unless the mapped test
cannot be targeted more narrowly.

Two rules shape everything here:

- A framework/command is only used when a repository file proves it exists.
  Nothing is invented; an unidentifiable setup is reported as `unknown`.
- An infrastructure problem is never reported as a product failure. A missing
  dependency, an absent runner, a collection error, or a timeout yields
  `unknown` with a `blocker`, never `failed`.

Sydes does not install anything, does not load `.env` files, and does not modify
the repository under analysis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import subprocess
import time

from sydes.core.models import EvidenceRef
from sydes.verify.models import (
    BLOCKER_COLLECTION_ERROR,
    BLOCKER_FRAMEWORK_UNSUPPORTED,
    BLOCKER_MISSING_DEPENDENCY,
    BLOCKER_NO_TESTS_COLLECTED,
    BLOCKER_PROCESS_ERROR,
    BLOCKER_RUNNER_MISSING,
    BLOCKER_TIMEOUT,
    GRANULARITY_CASE,
    GRANULARITY_FILE,
    VERIFICATION_FAILED,
    VERIFICATION_PASSED,
    VERIFICATION_UNKNOWN,
    CiSuiteRun,
    MappedTest,
    SourceRef,
    TestExecution,
)
from sydes.verify.source_files import RepoFiles

FRAMEWORK_PYTEST = "pytest"
FRAMEWORK_UNITTEST = "unittest"
FRAMEWORK_JEST = "jest"
FRAMEWORK_MOCHA = "mocha"
FRAMEWORK_NODE_TEST = "node:test"
FRAMEWORK_UNKNOWN = "unknown"

DEFAULT_TIMEOUT_SECONDS = 120.0
MAX_EXECUTIONS = 20
MAX_OUTPUT_CHARS = 4_000

_PYTHON_EXTENSIONS = {".py"}
_NODE_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}

# Interpreter locations checked, in order, before falling back to `python3`.
_VENV_PYTHON_PATHS = (
    ".venv/bin/python",
    "venv/bin/python",
    ".venv/Scripts/python.exe",
    "venv/Scripts/python.exe",
    "env/bin/python",
)

_MISSING_MODULE_RE = re.compile(
    r"(?:ModuleNotFoundError|ImportError):[^\n]*?No module named ['\"](?P<module>[\w.\-]+)['\"]"
)
_NODE_MISSING_MODULE_RE = re.compile(
    r"Cannot find module ['\"](?P<module>[^'\"]+)['\"]"
)
_CONNECTION_ERROR_RE = re.compile(
    r"ECONNREFUSED|Connection refused|could not connect to server|"
    r"OperationalError|ServerSelectionTimeoutError|NoBrokersAvailable|getaddrinfo",
    re.IGNORECASE,
)
# Matches a pytest dependency declaration in any of the shapes a manifest uses:
# `pytest`, `pytest==8.0`, `"pytest>=8"`, `pytest [extra]`.
_PYTEST_DEPENDENCY_RE = re.compile(
    r"""(?:^|["'\s\[,])pytest(?:[<>=!~\[\s"',\]]|$)""", re.MULTILINE
)
_ASSERTION_LINE_RE = re.compile(
    r"^(?:E\s+|\s*)(?P<text>(?:\w*(?:Error|Exception|Failure)|assert|AssertionError)\b.*)$"
)


@dataclass(slots=True)
class FrameworkDetection:
    """A test framework proven to be configured for a repository."""

    framework: str
    language: str
    evidence: EvidenceRef
    runner_argv: list[str] = field(default_factory=list)
    runner_available: bool = True
    unavailable_reason: str | None = None
    # Directory the manifest lives in, relative to the repo root. Monorepos keep
    # their runner config next to the package, not at the top level.
    working_dir: str = "."


@dataclass(slots=True)
class ExecutionSettings:
    """Controls for one execution pass."""

    enabled: bool = True
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_executions: int = MAX_EXECUTIONS


def _read(files: RepoFiles, path: str) -> str | None:
    """Return the text of a scanned file, if it was read."""
    for scanned in files.files:
        if scanned.path == path:
            return scanned.text
    return None


def _manifest_dirs(files: RepoFiles, basenames: tuple[str, ...]) -> list[str]:
    """Directories holding one of the given manifest files, root-first."""
    found: list[str] = []
    for scanned in files.files:
        if Path(scanned.path).name in basenames:
            parent = str(Path(scanned.path).parent)
            if parent not in found:
                found.append(parent)
    return sorted(found, key=lambda item: (item != ".", item.count("/"), item))


def _in_dir(working_dir: str, relative: str) -> str:
    """Join a working directory with a repo-relative path."""
    return relative if working_dir == "." else f"{working_dir}/{relative}"


def _relative_to_working_dir(working_dir: str, repo_relative_path: str) -> str:
    """Re-express a repo-relative path against the runner's working directory."""
    if working_dir == ".":
        return repo_relative_path
    prefix = working_dir.rstrip("/") + "/"
    if repo_relative_path.startswith(prefix):
        return repo_relative_path[len(prefix) :]
    return repo_relative_path


def _repo_python(root: Path, working_dir: str = ".") -> tuple[list[str], bool]:
    """Pick the interpreter to run Python tests with, preferring a repo venv."""
    bases = [root / working_dir, root] if working_dir != "." else [root]
    for base in bases:
        for relative in _VENV_PYTHON_PATHS:
            candidate = base / relative
            if candidate.is_file():
                return [str(candidate)], True
    return ["python3"], False


def _node_binary(root: Path, working_dir: str, name: str) -> Path | None:
    """Return an installed `node_modules/.bin` executable, if present."""
    candidate = root / working_dir / "node_modules" / ".bin" / name
    return candidate if candidate.exists() else None


def _detect_python(files: RepoFiles) -> list[FrameworkDetection]:
    """Detect a Python test runner from configuration files only."""
    detections: list[FrameworkDetection] = []
    manifest_names = ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini", "requirements.txt")

    for working_dir in _manifest_dirs(files, manifest_names):
        argv, from_venv = _repo_python(files.root, working_dir)
        source: tuple[str, str] | None = None
        for name in ("pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml"):
            path = _in_dir(working_dir, name)
            text = _read(files, path)
            if text is None:
                continue
            if name == "pytest.ini":
                source = (path, "pytest.ini present")
            elif "[tool:pytest]" in text or "[tool.pytest.ini_options]" in text:
                source = (path, "pytest configuration section")
            elif _PYTEST_DEPENDENCY_RE.search(text):
                source = (path, "pytest declared as a dependency")
            if source:
                break
        if source is None:
            for name in ("requirements.txt", "requirements-dev.txt", "test-requirements.txt"):
                path = _in_dir(working_dir, name)
                text = _read(files, path)
                if text and _PYTEST_DEPENDENCY_RE.search(text):
                    source = (path, "pytest declared as a dependency")
                    break
        if source is None:
            continue

        path, why = source
        if not from_venv:
            why = f"{why}; no repo virtualenv found, using `python3`"
        detections.append(
            FrameworkDetection(
                framework=FRAMEWORK_PYTEST,
                language="python",
                runner_argv=[*argv, "-m", "pytest"],
                working_dir=working_dir,
                evidence=EvidenceRef(file=path, label="test_framework", snippet=why),
            )
        )

    unittest_files = [
        scanned
        for scanned in files.files
        if scanned.is_test
        and scanned.extension in _PYTHON_EXTENSIONS
        and ("unittest.TestCase" in scanned.text or "import unittest" in scanned.text)
    ]
    if unittest_files:
        source_file = unittest_files[0]
        # unittest resolves dotted module paths from the directory it runs in,
        # so anchor it at the repository root.
        argv, from_venv = _repo_python(files.root)
        why = "test module builds on unittest.TestCase"
        if not from_venv:
            why = f"{why}; no repo virtualenv found, using `python3`"
        detections.append(
            FrameworkDetection(
                framework=FRAMEWORK_UNITTEST,
                language="python",
                runner_argv=[*argv, "-m", "unittest"],
                evidence=EvidenceRef(file=source_file.path, label="test_framework", snippet=why),
            )
        )
    return detections


def _detect_node(files: RepoFiles) -> list[FrameworkDetection]:
    """Detect a Node test runner from package.json and installed binaries."""
    detections: list[FrameworkDetection] = []

    for working_dir in _manifest_dirs(files, ("package.json",)):
        manifest_path = _in_dir(working_dir, "package.json")
        package_text = _read(files, manifest_path)
        if package_text is None:
            continue
        try:
            package = json.loads(package_text)
        except json.JSONDecodeError:
            continue
        if not isinstance(package, dict):
            continue

        dependencies: dict[str, str] = {}
        for key in ("devDependencies", "dependencies"):
            section = package.get(key)
            if isinstance(section, dict):
                dependencies.update(section)
        scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
        test_script = str(scripts.get("test") or "")

        for name in (FRAMEWORK_JEST, FRAMEWORK_MOCHA):
            if name not in dependencies and name not in test_script:
                continue
            binary = _node_binary(files.root, working_dir, name)
            detections.append(
                FrameworkDetection(
                    framework=name,
                    language="node",
                    runner_argv=[str(binary)] if binary else [],
                    runner_available=binary is not None,
                    working_dir=working_dir,
                    unavailable_reason=(
                        None
                        if binary
                        else f"`{name}` is declared in {manifest_path} but not installed in node_modules"
                    ),
                    evidence=EvidenceRef(
                        file=manifest_path,
                        label="test_framework",
                        snippet=f"`{name}` declared in {manifest_path}",
                    ),
                )
            )

        if "node --test" in test_script:
            detections.append(
                FrameworkDetection(
                    framework=FRAMEWORK_NODE_TEST,
                    language="node",
                    runner_argv=["node", "--test"],
                    working_dir=working_dir,
                    evidence=EvidenceRef(
                        file=manifest_path,
                        label="test_framework",
                        snippet="`node --test` declared as the test script",
                    ),
                )
            )
    return detections


def detect_frameworks(files: RepoFiles) -> list[FrameworkDetection]:
    """Detect every test framework a repository demonstrably configures."""
    return [*_detect_python(files), *_detect_node(files)]


def _select_detection(
    detections: list[FrameworkDetection], test_file: str
) -> FrameworkDetection | None:
    """Pick the framework that can run a given test file."""
    suffix = Path(test_file).suffix.lower()
    if suffix in _PYTHON_EXTENSIONS:
        language = "python"
    elif suffix in _NODE_EXTENSIONS:
        language = "node"
    else:
        return None
    matching = [item for item in detections if item.language == language]
    if not matching:
        return None

    def _rank(detection: FrameworkDetection) -> tuple[int, int]:
        # Prefer the manifest closest to the test file, then a runner that is
        # actually installed.
        owns = detection.working_dir == "." or test_file.startswith(
            detection.working_dir.rstrip("/") + "/"
        )
        return (0 if owns else 1, 0 if detection.runner_available else 1)

    return sorted(matching, key=_rank)[0]


def _module_path(test_file: str) -> str:
    """Convert `tests/test_app.py` into `tests.test_app`."""
    return Path(test_file).with_suffix("").as_posix().replace("/", ".")


def build_command(
    detection: FrameworkDetection, test: MappedTest
) -> tuple[list[str], str] | None:
    """Build the narrowest command that runs one mapped test.

    Returns the argv plus the granularity it achieves, or None when the
    framework cannot target the test.
    """
    repo_relative = test.file or ""
    if not repo_relative:
        return None
    test_file = _relative_to_working_dir(detection.working_dir, repo_relative)
    case_name = test.case_name or test.name
    # A test file with no recognizable case names is recorded under the file
    # stem; that is a file-granularity target, not a case.
    has_case = bool(case_name) and case_name != Path(test_file).stem

    if detection.framework == FRAMEWORK_PYTEST:
        if has_case:
            node_id = (
                f"{test_file}::{test.suite}::{case_name}" if test.suite else f"{test_file}::{case_name}"
            )
            return [*detection.runner_argv, node_id, "-q", "--no-header"], GRANULARITY_CASE
        return [*detection.runner_argv, test_file, "-q", "--no-header"], GRANULARITY_FILE

    if detection.framework == FRAMEWORK_UNITTEST:
        module = _module_path(test_file)
        if has_case:
            target = f"{module}.{test.suite}.{case_name}" if test.suite else f"{module}.{case_name}"
            return [*detection.runner_argv, target, "-v"], GRANULARITY_CASE
        return [*detection.runner_argv, module, "-v"], GRANULARITY_FILE

    if detection.framework == FRAMEWORK_JEST:
        base = [*detection.runner_argv, "--ci", "--runTestsByPath", test_file]
        if has_case:
            return [*base, "-t", case_name], GRANULARITY_CASE
        return base, GRANULARITY_FILE

    if detection.framework == FRAMEWORK_MOCHA:
        if has_case:
            return [*detection.runner_argv, test_file, "--grep", case_name], GRANULARITY_CASE
        return [*detection.runner_argv, test_file], GRANULARITY_FILE

    if detection.framework == FRAMEWORK_NODE_TEST:
        if has_case:
            return [
                *detection.runner_argv,
                "--test-name-pattern",
                case_name,
                test_file,
            ], GRANULARITY_CASE
        return [*detection.runner_argv, test_file], GRANULARITY_FILE

    return None


def _excerpt(text: str) -> tuple[str, bool]:
    """Trim output to a bounded tail, where runners print their failure summary."""
    cleaned = text.replace("\r\n", "\n").strip()
    if len(cleaned) <= MAX_OUTPUT_CHARS:
        return cleaned, False
    return "... [truncated by Sydes]\n" + cleaned[-MAX_OUTPUT_CHARS:], True


def _failure_summary(output: str) -> str | None:
    """Pull the first assertion/error line out of runner output."""
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = _ASSERTION_LINE_RE.match(stripped)
        if match:
            return match.group("text").strip()[:300]
    return None


def _classify_output(combined: str) -> tuple[str | None, str | None]:
    """Detect an infrastructure blocker in runner output.

    Returns (blocker, detail). A dependency that cannot be imported and a
    service that cannot be reached are both environment problems, not failures
    of the code under review.
    """
    match = _MISSING_MODULE_RE.search(combined) or _NODE_MISSING_MODULE_RE.search(combined)
    if match:
        return BLOCKER_MISSING_DEPENDENCY, match.group("module")
    if _CONNECTION_ERROR_RE.search(combined):
        return BLOCKER_MISSING_DEPENDENCY, None
    return None, None


def _interpret_exit(
    framework: str, exit_code: int, combined: str
) -> tuple[str, str | None, str | None]:
    """Map a runner exit code to a verification status plus any blocker."""
    blocker, detail = _classify_output(combined)
    if blocker is not None:
        return VERIFICATION_UNKNOWN, blocker, detail

    if exit_code == 0:
        return VERIFICATION_PASSED, None, None

    if framework == FRAMEWORK_PYTEST:
        # pytest: 1 = tests failed, 2/3/4 = runner problems, 5 = nothing collected.
        # A node id that does not exist exits 4, so read the message too.
        if exit_code == 1:
            return VERIFICATION_FAILED, None, None
        if exit_code == 5 or re.search(r"ERROR: not found:|no tests ran", combined):
            return VERIFICATION_UNKNOWN, BLOCKER_NO_TESTS_COLLECTED, None
        return VERIFICATION_UNKNOWN, BLOCKER_COLLECTION_ERROR, None

    if framework == FRAMEWORK_UNITTEST:
        if "Ran 0 tests" in combined:
            return VERIFICATION_UNKNOWN, BLOCKER_NO_TESTS_COLLECTED, None
        if re.search(r"^Ran \d+ test", combined, re.MULTILINE):
            # The runner got as far as executing tests, so this is a real result.
            return VERIFICATION_FAILED, None, None
        return VERIFICATION_UNKNOWN, BLOCKER_COLLECTION_ERROR, None

    if framework in {FRAMEWORK_JEST, FRAMEWORK_MOCHA, FRAMEWORK_NODE_TEST}:
        if re.search(r"No tests found|0 passing|matched 0 test", combined, re.IGNORECASE):
            return VERIFICATION_UNKNOWN, BLOCKER_NO_TESTS_COLLECTED, None
        return VERIFICATION_FAILED, None, None

    return VERIFICATION_UNKNOWN, BLOCKER_COLLECTION_ERROR, None


def _blocked(
    test: MappedTest,
    framework: str,
    blocker: str,
    reason: str,
    *,
    command: list[str] | None = None,
    granularity: str = GRANULARITY_CASE,
) -> TestExecution:
    """Build an execution record for a test that could not be run."""
    return TestExecution(
        test_id=test.id,
        framework=framework,
        command=command or [],
        granularity=granularity,
        status=VERIFICATION_UNKNOWN,
        blocker=blocker,
        reason=reason,
        evidence=SourceRef(repo=test.repo, file=test.file, line=test.line),
    )


def _run_once(
    *,
    test: MappedTest,
    detection: FrameworkDetection,
    command: list[str],
    granularity: str,
    repo_root: Path,
    settings: ExecutionSettings,
) -> TestExecution:
    """Run one command and interpret its outcome."""
    started = time.monotonic()
    try:
        completed = subprocess.run(  # noqa: S603 - argv list, never a shell string
            command,
            cwd=str(repo_root / detection.working_dir),
            capture_output=True,
            text=True,
            timeout=settings.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _blocked(
            test,
            detection.framework,
            BLOCKER_TIMEOUT,
            f"Test process exceeded the {settings.timeout_seconds:g}s timeout",
            command=command,
            granularity=granularity,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        return _blocked(
            test,
            detection.framework,
            BLOCKER_PROCESS_ERROR,
            f"Could not start the test process: {exc}",
            command=command,
            granularity=granularity,
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    stdout, stdout_truncated = _excerpt(completed.stdout or "")
    stderr, stderr_truncated = _excerpt(completed.stderr or "")
    combined = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    status, blocker, detail = _interpret_exit(detection.framework, completed.returncode, combined)

    reason = None
    if blocker == BLOCKER_MISSING_DEPENDENCY:
        reason = (
            f"Test requires `{detail}`, which is not available in this environment"
            if detail
            else "Test could not reach a required service"
        )
    elif blocker == BLOCKER_NO_TESTS_COLLECTED:
        reason = "The runner collected no tests for this target"
    elif blocker == BLOCKER_COLLECTION_ERROR:
        reason = f"The runner exited with code {completed.returncode} without reporting a test result"

    return TestExecution(
        test_id=test.id,
        framework=detection.framework,
        command=command,
        granularity=granularity,
        status=status,
        blocker=blocker,
        reason=reason,
        exit_code=completed.returncode,
        duration_ms=duration_ms,
        stdout_excerpt=stdout or None,
        stderr_excerpt=stderr or None,
        output_truncated=stdout_truncated or stderr_truncated,
        failure_summary=_failure_summary(combined) if status == VERIFICATION_FAILED else None,
        missing_dependency=detail,
        evidence=SourceRef(repo=test.repo, file=test.file, line=test.line),
    )


def execute_mapped_test(
    *,
    test: MappedTest,
    detections: list[FrameworkDetection],
    repo_root: Path,
    settings: ExecutionSettings,
) -> TestExecution:
    """Run one mapped test and interpret the result."""
    detection = _select_detection(detections, test.file or "")
    if detection is None:
        return _blocked(
            test,
            FRAMEWORK_UNKNOWN,
            BLOCKER_FRAMEWORK_UNSUPPORTED,
            f"No test framework was detected that can run {test.file}",
        )
    if not detection.runner_available:
        return _blocked(
            test,
            detection.framework,
            BLOCKER_RUNNER_MISSING,
            detection.unavailable_reason or "test runner is not installed",
        )

    built = build_command(detection, test)
    if built is None:
        return _blocked(
            test,
            detection.framework,
            BLOCKER_FRAMEWORK_UNSUPPORTED,
            f"Cannot build a {detection.framework} command for this test",
        )
    command, granularity = built

    execution = _run_once(
        test=test,
        detection=detection,
        command=command,
        granularity=granularity,
        repo_root=repo_root,
        settings=settings,
    )

    # A case-level target that collects nothing usually means the case name or
    # its enclosing class was mis-read. Widening to the file is honest and still
    # far narrower than the suite, so retry once and record the widening.
    if execution.blocker == BLOCKER_NO_TESTS_COLLECTED and granularity == GRANULARITY_CASE:
        file_target = MappedTest(
            id=test.id,
            name=Path(test.file or "").stem,
            case_name=Path(test.file or "").stem,
            repo=test.repo,
            file=test.file,
            line=test.line,
        )
        widened = build_command(detection, file_target)
        if widened is not None:
            retried = _run_once(
                test=test,
                detection=detection,
                command=widened[0],
                granularity=widened[1],
                repo_root=repo_root,
                settings=settings,
            )
            note = "Case-level run collected no tests; widened to the whole file."
            retried.reason = f"{note} {retried.reason}" if retried.reason else note
            return retried

    return execution


def execute_mapped_tests(
    *,
    tests: list[MappedTest],
    files: RepoFiles,
    repo_root: Path,
    settings: ExecutionSettings,
) -> tuple[list[TestExecution], list[str]]:
    """Execute every mapped test once, bounded, reusing results across behaviors."""
    # Detect first, so the report can say what the repository is configured for
    # even when nothing was mapped and nothing will run.
    detections = detect_frameworks(files)
    notes: list[str] = [
        "test_frameworks_detected="
        + (",".join(sorted({item.framework for item in detections})) if detections else "none")
    ]
    for detection in detections:
        if not detection.runner_available:
            notes.append(
                f"test_runner_unavailable={detection.framework} "
                f"reason={detection.unavailable_reason}"
            )
    if not settings.enabled:
        return [], [*notes, "test_execution=disabled"]
    if not tests:
        return [], [*notes, "test_execution=no_mapped_tests"]

    executions: list[TestExecution] = []
    seen: set[str] = set()
    for test in tests:
        if test.id in seen:
            continue
        seen.add(test.id)
        if len(executions) >= settings.max_executions:
            notes.append(f"test_execution_truncated_at={settings.max_executions}")
            break
        executions.append(
            execute_mapped_test(
                test=test,
                detections=detections,
                repo_root=repo_root,
                settings=settings,
            )
        )
    notes.append(f"tests_executed={len(executions)}")
    return executions, notes


# --------------------------------------------------------------------------
# Repository CI suite: one run of the project's own test command
# --------------------------------------------------------------------------

_WORKFLOW_RUN_RE = re.compile(r"^\s*(?:-\s*)?run:\s*(?P<command>.+?)\s*$")
_TEST_COMMAND_RE = re.compile(r"\b(?:pytest|jest|mocha|vitest|npm\s+test|node\s+--test)\b")
# Shell constructs Sydes will not interpret; such a line is skipped rather than
# split naively into argv.
_SHELL_METACHARACTERS = ("&&", "||", "|", ";", ">", "<", "$(", "`")


def _workflow_test_command(files: RepoFiles) -> tuple[list[str], str] | None:
    """Find a test command declared in a CI workflow, if one is unambiguous."""
    for scanned in files.files:
        if ".github/workflows/" not in scanned.path:
            continue
        for line in scanned.text.splitlines():
            match = _WORKFLOW_RUN_RE.match(line)
            if match is None:
                continue
            command = match.group("command").strip().strip("\"'")
            if not _TEST_COMMAND_RE.search(command):
                continue
            if any(token in command for token in _SHELL_METACHARACTERS):
                continue
            return command.split(), scanned.path
    return None


def _package_test_script(files: RepoFiles) -> tuple[list[str], str] | None:
    """Find a `npm test` script that actually runs a test runner."""
    for working_dir in _manifest_dirs(files, ("package.json",)):
        path = _in_dir(working_dir, "package.json")
        text = _read(files, path)
        if text is None:
            continue
        try:
            package = json.loads(text)
        except json.JSONDecodeError:
            continue
        scripts = package.get("scripts") if isinstance(package, dict) else None
        script = str((scripts or {}).get("test") or "")
        if not script or not _TEST_COMMAND_RE.search(script):
            continue
        return ["npm", "test", "--silent"], path
    return None


def resolve_ci_test_command(
    files: RepoFiles, detections: list[FrameworkDetection]
) -> tuple[list[str], str, FrameworkDetection] | None:
    """Resolve the repository's own test command, preferring declared signals.

    The workflow file is the strongest statement of "how this project runs its
    tests". A bare runner name from it is re-pointed at the environment the
    existing runner detection already resolved, so the repo's virtualenv or
    `node_modules` binary is used rather than whatever is on PATH.
    """
    if not detections:
        return None
    available = [item for item in detections if item.runner_available]
    if not available:
        return None

    declared = _workflow_test_command(files) or _package_test_script(files)
    if declared is not None:
        command, source = declared
        head = command[0]
        detection = next(
            (item for item in available if item.framework == head),
            available[0],
        )
        # `pytest -v` becomes `<repo python> -m pytest -v`; anything already
        # naming an interpreter or npm is left as written.
        if head in {FRAMEWORK_PYTEST, FRAMEWORK_JEST, FRAMEWORK_MOCHA}:
            return [*detection.runner_argv, *command[1:]], source, detection
        return command, source, detection

    # No declared command: fall back to the detected runner's conventional form.
    detection = available[0]
    return list(detection.runner_argv), f"detected:{detection.framework}", detection


_PYTEST_SUMMARY_RE = re.compile(
    r"^=+\s*(?P<body>.*?\b\d+\s+(?:passed|failed|error).*?)\s*=+\s*$", re.MULTILINE
)
_PYTEST_COUNT_RE = re.compile(r"(?P<count>\d+)\s+(?P<state>passed|failed|errors?|skipped)")
_PYTEST_FAILED_RE = re.compile(r"^(?:FAILED|ERROR)\s+(?P<node>\S+)", re.MULTILINE)
_JEST_COUNT_RE = re.compile(r"Tests:\s+(?:(?P<failed>\d+)\s+failed,\s*)?(?:\d+\s+skipped,\s*)?(?P<passed>\d+)\s+passed")


def _parse_suite_output(combined: str) -> tuple[int | None, int | None, str | None, list[str]]:
    """Extract pass/fail counts and failing test ids from runner output."""
    passed = failed = None
    summary = None

    match = _PYTEST_SUMMARY_RE.search(combined)
    if match:
        summary = match.group("body").strip()
        for count_match in _PYTEST_COUNT_RE.finditer(summary):
            value = int(count_match.group("count"))
            state = count_match.group("state")
            if state == "passed":
                passed = value
            elif state in {"failed", "error", "errors"}:
                failed = (failed or 0) + value

    jest_match = _JEST_COUNT_RE.search(combined)
    if jest_match:
        passed = int(jest_match.group("passed"))
        failed = int(jest_match.group("failed") or 0)
        summary = summary or jest_match.group(0)

    failed_ids = [item.group("node") for item in _PYTEST_FAILED_RE.finditer(combined)]
    return passed, failed, summary, failed_ids


def run_ci_suite(
    *, files: RepoFiles, repo_root: Path, settings: ExecutionSettings
) -> tuple[CiSuiteRun | None, list[str]]:
    """Run the repository's own test command once and interpret the result."""
    detections = detect_frameworks(files)
    notes: list[str] = [
        "test_frameworks_detected="
        + (",".join(sorted({item.framework for item in detections})) if detections else "none")
    ]
    if not settings.enabled:
        return None, [*notes, "ci_suite=disabled"]

    resolved = resolve_ci_test_command(files, detections)
    if resolved is None:
        unavailable = next(
            (item for item in detections if not item.runner_available), None
        )
        blocker = BLOCKER_RUNNER_MISSING if unavailable else BLOCKER_FRAMEWORK_UNSUPPORTED
        reason = (
            unavailable.unavailable_reason
            if unavailable and unavailable.unavailable_reason
            else "No repository test command could be resolved"
        )
        notes.append(f"ci_suite=unresolved reason={reason}")
        return (
            CiSuiteRun(status=VERIFICATION_UNKNOWN, blocker=blocker, reason=reason),
            notes,
        )

    command, source, detection = resolved
    started = time.monotonic()
    try:
        completed = subprocess.run(  # noqa: S603 - argv list, never a shell string
            command,
            cwd=str(repo_root / detection.working_dir),
            capture_output=True,
            text=True,
            timeout=settings.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return (
            CiSuiteRun(
                command=command,
                source=source,
                working_dir=detection.working_dir,
                framework=detection.framework,
                status=VERIFICATION_UNKNOWN,
                blocker=BLOCKER_TIMEOUT,
                reason=f"Test suite exceeded the {settings.timeout_seconds:g}s timeout",
            ),
            [*notes, "ci_suite=timeout"],
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        return (
            CiSuiteRun(
                command=command,
                source=source,
                working_dir=detection.working_dir,
                framework=detection.framework,
                status=VERIFICATION_UNKNOWN,
                blocker=BLOCKER_PROCESS_ERROR,
                reason=f"Could not start the test suite: {exc}",
            ),
            [*notes, "ci_suite=process_error"],
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    combined = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    stdout, stdout_truncated = _excerpt(completed.stdout or "")
    stderr, stderr_truncated = _excerpt(completed.stderr or "")
    status, blocker, detail = _interpret_exit(detection.framework, completed.returncode, combined)
    passed, failed, summary, failed_ids = _parse_suite_output(combined)

    reason = None
    if blocker == BLOCKER_MISSING_DEPENDENCY:
        reason = (
            f"Suite requires `{detail}`, which is not available in this environment"
            if detail
            else "Suite could not reach a required service"
        )
    elif blocker is not None:
        reason = f"The runner exited with code {completed.returncode} without a usable result"

    notes.append(f"ci_suite={status} exit={completed.returncode} source={source}")
    return (
        CiSuiteRun(
            command=command,
            source=source,
            working_dir=detection.working_dir,
            framework=detection.framework,
            status=status,
            blocker=blocker,
            reason=reason,
            exit_code=completed.returncode,
            duration_ms=duration_ms,
            tests_passed=passed,
            tests_failed=failed,
            summary_line=summary,
            failed_test_ids=failed_ids,
            stdout_excerpt=stdout or None,
            stderr_excerpt=stderr or None,
            output_truncated=stdout_truncated or stderr_truncated,
            evidence=[EvidenceRef(file=source, label="ci_test_command", snippet=" ".join(command))],
        ),
        notes,
    )
