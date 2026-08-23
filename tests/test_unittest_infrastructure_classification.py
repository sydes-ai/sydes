"""Telling a suite that could not load from a suite that genuinely failed.

`unittest` reports both through the same channel. A module it cannot import is
replaced by a synthetic `unittest.loader._FailedTest`, which is *counted* in
"Ran N tests" and summarised as an error. Read naively, an environment with the
wrong dependencies installed is indistinguishable from code that broke — and
Sydes then reports `failed`, which forces ACTION REQUIRED on a change that may
be perfectly fine.

The rule these tests pin: infrastructure problems are `unknown` with a blocker;
a false assertion stays `failed`.
"""

from __future__ import annotations

import pytest

from sydes.verify.models import (
    BLOCKER_COLLECTION_ERROR,
    BLOCKER_MISSING_DEPENDENCY,
    BLOCKER_NO_TESTS_COLLECTED,
    VERIFICATION_FAILED,
    VERIFICATION_PASSED,
    VERIFICATION_UNKNOWN,
)
from sydes.verify.test_execution import _interpret_exit

FRAMEWORK = "unittest"


# Reduced from the real output of `python -m unittest` on psf/requests at the
# commit used in the 10-task evaluation, under a Python too new for the pinned
# urllib3. Two placeholder tests are "run"; nothing actually executed.
_LOADER_IMPORT_ERROR = """\
======================================================================
ERROR: test_requests (unittest.loader._FailedTest.test_requests)
----------------------------------------------------------------------
ImportError: Failed to import test module: test_requests
Traceback (most recent call last):
  File "/usr/lib/python3.12/unittest/loader.py", line 339, in _get_module_from_name
    __import__(name)
  File "/repo/requests/packages/urllib3/_collections.py", line 7, in <module>
    from collections import MutableMapping
ImportError: cannot import name 'MutableMapping' from 'collections'

----------------------------------------------------------------------
Ran 2 tests in 0.000s

FAILED (errors=2)
"""

_LOADER_SYNTAX_ERROR = """\
ERROR: test_broken (unittest.loader._FailedTest.test_broken)
ImportError: Failed to import test module: test_broken
Traceback (most recent call last):
  File "/usr/lib/python3.12/unittest/loader.py", line 339, in _get_module_from_name
    __import__(name)
  File "/repo/tests/test_broken.py", line 4
    def broken(:
              ^
SyntaxError: invalid syntax

----------------------------------------------------------------------
Ran 1 test in 0.000s

FAILED (errors=1)
"""

_GENUINE_ASSERTION_FAILURE = """\
======================================================================
FAIL: test_blank_name_is_rejected (tests.test_students.StudentTests.test_blank_name_is_rejected)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/repo/tests/test_students.py", line 22, in test_blank_name_is_rejected
    self.assertEqual(response.status_code, 400)
AssertionError: 201 != 400

----------------------------------------------------------------------
Ran 12 tests in 0.412s

FAILED (failures=1)
"""

_GENUINE_ERROR_IN_TEST_BODY = """\
======================================================================
ERROR: test_create_student (tests.test_students.StudentTests.test_create_student)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/repo/tests/test_students.py", line 30, in test_create_student
    student = crud.create_student(db, payload)
  File "/repo/crud.py", line 12, in create_student
    raise ValueError("boom")
ValueError: boom

----------------------------------------------------------------------
Ran 12 tests in 0.398s

FAILED (errors=1)
"""

_MIXED_LOAD_FAILURE_AND_REAL_FAILURE = """\
ERROR: test_optional (unittest.loader._FailedTest.test_optional)
ImportError: Failed to import test module: test_optional
Traceback (most recent call last):
  File "/usr/lib/python3.12/unittest/loader.py", line 339, in _get_module_from_name
    __import__(name)
ModuleNotFoundError: No module named 'optional_extra'

FAIL: test_rejects_blank (tests.test_students.StudentTests.test_rejects_blank)
AssertionError: 201 != 400

----------------------------------------------------------------------
Ran 9 tests in 0.201s

FAILED (failures=1, errors=1)
"""


# --------------------------------------------------------------------------
# Infrastructure must never read as failure
# --------------------------------------------------------------------------


def test_import_error_during_loading_is_infrastructure() -> None:
    """The exact psf/requests case: nothing ran, so nothing is known."""
    status, blocker, detail = _interpret_exit(FRAMEWORK, 1, _LOADER_IMPORT_ERROR)

    assert status == VERIFICATION_UNKNOWN
    assert blocker == BLOCKER_MISSING_DEPENDENCY
    assert detail == "MutableMapping"


def test_ran_n_tests_does_not_by_itself_mean_tests_ran() -> None:
    """`Ran 2 tests` counts loader placeholders, so it cannot decide the verdict."""
    assert "Ran 2 tests" in _LOADER_IMPORT_ERROR

    status, _blocker, _detail = _interpret_exit(FRAMEWORK, 1, _LOADER_IMPORT_ERROR)

    assert status != VERIFICATION_FAILED


def test_syntax_error_in_a_test_module_is_a_collection_error() -> None:
    """A module that cannot be parsed is a setup problem, not a broken change."""
    status, blocker, _detail = _interpret_exit(FRAMEWORK, 1, _LOADER_SYNTAX_ERROR)

    assert status == VERIFICATION_UNKNOWN
    assert blocker == BLOCKER_COLLECTION_ERROR


# --------------------------------------------------------------------------
# Genuine failures must survive
# --------------------------------------------------------------------------


def test_failed_assertion_remains_a_failure() -> None:
    """The signal Sydes exists to surface must not be softened into `unknown`."""
    status, blocker, _detail = _interpret_exit(FRAMEWORK, 1, _GENUINE_ASSERTION_FAILURE)

    assert status == VERIFICATION_FAILED
    assert blocker is None


def test_exception_raised_inside_a_test_remains_a_failure() -> None:
    """An error from the code under test is evidence, even though it is an `error`."""
    status, blocker, _detail = _interpret_exit(FRAMEWORK, 1, _GENUINE_ERROR_IN_TEST_BODY)

    assert status == VERIFICATION_FAILED
    assert blocker is None


def test_real_failure_alongside_a_load_failure_stays_a_failure() -> None:
    """A false assertion is hard evidence and outranks an unrelated bad import."""
    status, blocker, _detail = _interpret_exit(
        FRAMEWORK, 1, _MIXED_LOAD_FAILURE_AND_REAL_FAILURE
    )

    assert status == VERIFICATION_FAILED
    assert blocker is None


# --------------------------------------------------------------------------
# Unchanged behaviour
# --------------------------------------------------------------------------


def test_clean_run_still_passes() -> None:
    status, blocker, _detail = _interpret_exit(FRAMEWORK, 0, "Ran 12 tests in 0.4s\n\nOK\n")

    assert status == VERIFICATION_PASSED
    assert blocker is None


def test_empty_suite_still_reports_nothing_collected() -> None:
    status, blocker, _detail = _interpret_exit(FRAMEWORK, 1, "Ran 0 tests in 0.000s\n\nOK\n")

    assert status == VERIFICATION_UNKNOWN
    assert blocker == BLOCKER_NO_TESTS_COLLECTED


@pytest.mark.parametrize(
    "output",
    [
        "ModuleNotFoundError: No module named 'psycopg2'\n",
        "ImportError: No module named 'redis'\n",
    ],
)
def test_missing_module_detection_is_unchanged(output: str) -> None:
    """The pre-existing detector keeps working for plainly absent modules."""
    status, blocker, _detail = _interpret_exit(FRAMEWORK, 1, output)

    assert status == VERIFICATION_UNKNOWN
    assert blocker == BLOCKER_MISSING_DEPENDENCY
