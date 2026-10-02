# Zero-config Python runtime evidence

## 1. Manual settings used so far

| setting | Baserow #5507 | demo-orders-api #6 | class |
|---|---|---|---|
| runtime | `--runtime python` | `--runtime python` | **A** language detection |
| interpreter / venv | `--python /tmp/bw-venv/bin/python` (made by `runtime_setup`) | `--python /tmp/target/bin/python` (made by `runtime_setup`) | **A** when an environment exists (repo `.venv`, uv/poetry env, the CI job's Python); **B** for an unusual location |
| dependency environment | `uv sync --frozen` in `backend/` (uv workspace with premium, enterprise) | `pip install fastapi uvicorn pytest httpx` | **A** reuse what the repo's CI or developer prepared; preparing it is an explicit mode, never silent |
| source root | `backend/src` | `.` | **A** from packaging metadata (`hatch` packages, setuptools `package-dir`/`where`, poetry `from`) or layout (`src/<pkg>`, `<pkg>/` at the root) |
| test root | `backend/tests` | `tests` | **A** from pytest `testpaths`, else `tests/`/`test/` |
| import roots | 6× `--pythonpath` (3 src, 3 tests) | `--pythonpath .` | **A** only what the environment cannot import; nothing when the effective pytest config already sets `pythonpath` |
| pytest config | `backend/pytest.ini` (silently overrides `pyproject` `[tool.pytest.ini_options]`, including its `pythonpath`) | `pyproject` `[tool.pytest.ini_options]` | **A** pytest's own precedence: `pytest.ini` > `pyproject.toml` > `tox.ini` > `setup.cfg` |
| tests to run | automatic selection (Sydes) | automatic selection | **A** (unchanged) |
| `-p no:cacheprovider` | passed | – | **A** DiffGenome always adds it (redundant) |
| environment variables | `DATABASE_HOST/PORT/USER/PASSWORD/NAME` | – | **C** service credentials; `.sydes.yml` `env` |
| working directory | repo root (pytest finds `backend/pytest.ini` from the test paths) | repo root | **A** |
| services | PostgreSQL (`brew install`, `initdb`, role, db) | – | **C** the repo's CI must start them |
| loopback | `--allow-loopback` (database) | – | **B** `.sydes.yml` `allow_loopback` |

## 2. Detector (`sydes.behavioral.python_project`)

Deterministic, no model. Input: repository root plus the change's Python files. Output
(`PythonProject`): project root, source roots, test roots, pytest config, import roots,
interpreter, confidence, and for every field the reason it was chosen (`reasons`) and its
origin (`inferred`, `.sydes.yml`, `cli`). Ambiguity is recorded as a warning, never resolved by
guessing.

- **Project root**: directories with `pyproject.toml`, `setup.py`, `setup.cfg`, `pytest.ini`
  or `tox.ini`. In a monorepo it is the one that contains most of the changed Python files.
- **Source roots**: packaging metadata first, then the `src/` layout, then root-level
  packages. Changed files outside every source root are reported.
- **Test roots**: the effective pytest config's `testpaths` (resolved against its directory),
  then `tests/`, then `test/`.
- **Pytest config**: pytest's own precedence. That decides the working rootdir and whether
  pytest already manages `pythonpath`.
- **Interpreter**, first that is usable: `VIRTUAL_ENV`; `.venv`, `venv` or `env` under the
  project root or the repo root; `UV_PROJECT_ENVIRONMENT`; poetry's env (`poetry env info -p`);
  `python3`/`python` on PATH. Sydes' own interpreter is never chosen. A candidate is usable
  only if it is Python 3.12+ (DiffGenome traces with `sys.monitoring`), can import pytest, and
  has the project's declared dependencies installed (checked by distribution name). Otherwise
  it is rejected, and the diagnostic says why.
- **Import roots**: none when the pytest config sets `pythonpath`. Otherwise each source root
  whose packages the chosen interpreter cannot import. Never the whole repository unless the
  packages live at its root and are not installed.

## 3. Precedence

`inference < .sydes.yml < CLI`.

`.sydes.yml` at the repo root:

```yaml
runtime:
  python: backend/.venv/bin/python
  source_roots: [backend/src]
  test_roots: [backend/tests]
  pythonpath: [backend/tests]
  env: {DATABASE_HOST: 127.0.0.1}
  allow_loopback: true
```

The CLI wins: an explicit `--behavioral-args` replaces detection entirely, as it always has.
Provenance records each value's origin.

## 4. Success and failure criteria (set before the study)

Success, if all of these hold across 3–5 real PRs in 2–3 repositories:

- no hand-written interpreter, source root, pythonpath or test path on at least all but one
  repository; any override is a `.sydes.yml` naming a service or credential (class C);
- auto-selected tests run (exit status from the tests themselves, not import or collection
  errors) on every PR whose environment is prepared;
- runtime evidence is correct on manual check: the functions marked executed really are
  called by the named tests, and the paths exist in the code;
- every failure ends in a one-line actionable diagnostic (which interpreter was rejected and
  why, which file is outside the source roots);
- runtime evidence adds at most about 2× the PR's selected-test time, plus setup.

Failure if any of these:

- an override is needed on most repositories;
- a wrong environment is used silently (tests run against packages other than the PR's);
- import-path failures in the selected tests;
- irrelevant tests dominate the selection;
- misleading evidence (an executed function that was not, a path that does not exist);
- an unsafe fallback (running without the sandbox, or with Sydes' own interpreter).

Tuning for an evaluation repository is allowed only when the change generalizes and gets a
regression test.

## 5. Evaluation (5 PRs, 3 repositories; no hand-picked tests, no runtime flags)

PRs were chosen mechanically: the most recent non-merge commits changing both source and test
Python files with 30–400 changed lines, two per repository, plus demo-orders-api #5. The
environment was prepared with `sydes runtime-detect --prepare`. Runs on macOS (Seatbelt),
deterministic (no model), DiffGenome 0.1.4. The demo also ran zero-config on ubuntu-latest CI
(bubblewrap), with the same evidence.

| PR | detection | override | selected | passed | changed fns run | gaps | paths | pytest (plain → traced) | Sydes total |
|---|---|---|---|---|---|---|---|---|---|
| healthchecks 29c759d1 | Django, 34 per-app test roots, `.venv`-style env, pytest-django | `env: EMAIL_HOST` (custom TEST_RUNNER) | 1 file, 18 tests | 18/18 | 1/1 (`Profile.send_report`, 14 tests) | 1: new `count := …` branch never false | 0 | 10.1 s → 9.9 s | 17 s |
| healthchecks dd068b91 | same | same | 2 files (both changed), 33 | 33/33 | 2/2 | 0 | 1 (`send_report → day_boundaries`) | – | 17 s |
| requests 6f66281a | src layout, pytest testpaths | none | 1 file (changed), 237 | 335/340 | 4/4 (incl. `_encode_files`, raises observed) | 8 | 3 | 38.6 s → 42.0 s | 49 s |
| requests f8bec2f7 | same | none | 1 file of 3 changed (budget), 233 | 326/331 | 2/4 | 2 | 3 | – | 48 s |
| demo-orders-api #5 | root package, pyproject pytest config, `[dev]` extra | none | 1 file, 7 | 7/7 | 2/2 (`InsufficientStockError`, `HTTPException`) | 0 | 1 | 0.1 s → 0.4 s | 5 s (99 s in CI with review) |

Failures, all expected and explained: requests' 4 `TestTimeout` tests connect to an external
blackhole address (10.255.255.1), which the sandbox refuses instead of timing out.

Problems found and fixed generally (each with a regression test): Django tests need
pytest-django plus the settings (Sydes); per-app test roots (DiffGenome 0.1.4 `--test-root`
repeatable); unittest failures recorded as passed (DiffGenome 0.1.4); the egress guard
refused loopback even when allowed (DiffGenome 0.1.4); pytest ids outside the repository
(DiffGenome 0.1.4 `--rootdir .`); unknown extras installed nothing and one native dependency
failed a whole requirements file (Sydes prepare); editable installs pointing at another
checkout (source roots always prepended).

Still misleading or incomplete:
- import-time code is reported as not executed (`requests._check_cryptography` runs when the
  package is imported, during collection, which DiffGenome does not trace);
- a large changed test file can use the whole test budget, so another changed test file is
  dropped (requests f8bec2f7: `test_help.py`, which would have run `help._implementation`).
