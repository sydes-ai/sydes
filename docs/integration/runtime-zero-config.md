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
