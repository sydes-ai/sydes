"""Zero-config Python runtime detection: layouts, pytest precedence, interpreter choice,
import roots, `.sydes.yml` overrides. Interpreters are fakes answering the probe."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from sydes.behavioral.python_project import (
    DetectionError,
    detect_python_project,
    diffgenome_args,
    load_overrides,
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "repo"
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x")
    return root


def _fake_python(path: Path, *, version=(3, 12, 4), pytest_ok=True, missing=(), prefix=None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": list(version), "prefix": prefix or str(path.parent.parent), "pytest": pytest_ok,
               "missing": list(missing), "origins": {}}
    path.write_text(f"#!/bin/sh\necho '{json.dumps(payload)}'\n")
    path.chmod(0o755)
    return path


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """PATH holds only git: no ambient `python3` unless a test adds one."""
    for var in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"):
        monkeypatch.delenv(var, raising=False)
    gitbin = tmp_path / "gitbin"
    gitbin.mkdir()
    (gitbin / "git").symlink_to(shutil.which("git"))
    monkeypatch.setenv("PATH", str(gitbin))
    return gitbin


def test_src_layout_from_setuptools_metadata(tmp_path: Path) -> None:
    root = _repo(tmp_path, {
        "pyproject.toml": '[project]\nname="p"\ndependencies=["requests>=2"]\n'
                          '[tool.setuptools.packages.find]\nwhere=["src"]\n',
        "src/pkg/__init__.py": "", "src/pkg/core.py": "x=1\n", "tests/test_core.py": "",
    })
    _fake_python(root / ".venv" / "bin" / "python")
    p = detect_python_project(root, ["src/pkg/core.py"], overrides={})
    assert p.source_roots == ["src"] and p.test_roots == ["tests"] and p.import_roots == ["src"]
    assert p.python == str(root / ".venv" / "bin" / "python") and p.confidence == "high"
    assert "setuptools.packages.find" in p.reasons["source_roots"]
    assert diffgenome_args(p, ["src/pkg/core.py"])[:8] == [
        "--runtime", "python", "--python", p.python, "--source-root", "src", "--test-root", "tests",
    ]


def test_package_at_the_root_is_medium_confidence(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"pyproject.toml": '[project]\nname="p"\n', "app/__init__.py": "",
                            "app/main.py": "", "tests/test_main.py": ""})
    _fake_python(root / ".venv" / "bin" / "python")
    p = detect_python_project(root, ["app/main.py"], overrides={})
    assert p.source_roots == ["."] and p.import_roots == ["."] and p.confidence == "medium"


def test_pytest_ini_wins_over_pyproject_like_pytest_itself(tmp_path: Path) -> None:
    """Baserow: backend/pytest.ini silently overrides [tool.pytest.ini_options] (whose
    pythonpath pytest never applies)."""
    root = _repo(tmp_path, {
        "backend/pyproject.toml": '[project]\nname="b"\n[tool.hatch.build.targets.wheel]\npackages=["src/b"]\n'
                                  '[tool.pytest.ini_options]\npythonpath=["tests"]\n',
        "backend/pytest.ini": "[pytest]\ntestpaths =\n    tests\n    ../plugin/tests\n",
        "backend/src/b/__init__.py": "", "backend/src/b/m.py": "",
        "backend/tests/__init__.py": "", "backend/tests/test_m.py": "",
        "plugin/tests/plugin_helpers/__init__.py": "", "plugin/tests/test_p.py": "",
    })
    _fake_python(root / "backend" / ".venv" / "bin" / "python")
    p = detect_python_project(root, ["backend/src/b/m.py"], overrides={})
    assert p.project_root == "backend" and p.pytest_config == "backend/pytest.ini"
    assert p.test_roots == ["backend/tests", "plugin/tests"] and p.pytest_pythonpath == []
    # source root always; a non-package test root holding helper packages too (pytest only adds
    # it when one of its tests is collected); a package test root (backend/tests) not
    assert p.import_roots == ["backend/src", "plugin/tests"]


def test_monorepo_picks_the_project_holding_most_changed_files_plus_uv_members(tmp_path: Path) -> None:
    root = _repo(tmp_path, {
        "backend/pyproject.toml": '[project]\nname="b"\n[tool.uv.workspace]\nmembers=["../ext"]\n',
        "backend/src/b/__init__.py": "", "backend/src/b/x.py": "", "backend/src/b/y.py": "",
        "ext/pyproject.toml": '[project]\nname="e"\n', "ext/src/e/__init__.py": "", "ext/src/e/z.py": "",
        "tools/pyproject.toml": '[project]\nname="t"\n', "tools/t.py": "",
    })
    _fake_python(root / "backend" / ".venv" / "bin" / "python")
    changed = ["backend/src/b/x.py", "backend/src/b/y.py", "ext/src/e/z.py"]
    p = detect_python_project(root, changed, overrides={})
    assert p.project_root == "backend" and p.source_roots == ["backend/src", "ext/src"]
    assert "backend: 2" in p.reasons["project_root"]
    assert diffgenome_args(p, changed)[5] == "backend/src"  # indexes the root with most changes


def test_interpreters_are_validated_and_rejections_explained(tmp_path: Path, monkeypatch) -> None:
    root = _repo(tmp_path, {"pyproject.toml": '[project]\nname="p"\ndependencies=["fastapi"]\n',
                            "app/__init__.py": "", "tests/test_a.py": ""})
    _fake_python(root / ".venv" / "bin" / "python", version=(3, 11, 9))
    _fake_python(root / "venv" / "bin" / "python", missing=["fastapi"])
    good = _fake_python(tmp_path / "ci" / "bin" / "python3")
    monkeypatch.setenv("PATH", f"{good.parent}:{tmp_path / 'gitbin'}")
    p = detect_python_project(root, ["app/__init__.py"], overrides={})
    assert p.python == str(good) and "on PATH" in p.reasons["python"]
    assert any("< 3.12" in r for r in p.rejected_interpreters)
    assert any("missing declared dependencies: fastapi" in r for r in p.rejected_interpreters)


def test_never_sydes_own_environment_and_a_clear_error_when_nothing_fits(tmp_path: Path, monkeypatch) -> None:
    import sys

    root = _repo(tmp_path, {"pyproject.toml": '[project]\nname="p"\n', "tests/test_a.py": ""})
    _fake_python(root / ".venv" / "bin" / "python", prefix=sys.prefix)
    with pytest.raises(DetectionError, match="is Sydes' own environment.*runtime.python"):
        detect_python_project(root, [], overrides={})


def test_sydes_yml_overrides_inference(tmp_path: Path) -> None:
    root = _repo(tmp_path, {
        "pyproject.toml": '[project]\nname="p"\n', "src/p/__init__.py": "", "tests/test_a.py": "",
        ".sydes.yml": "runtime:\n  python: py/bin/python\n  pythonpath: [src, extra]\n"
                      "  env: {DATABASE_HOST: 127.0.0.1}\n  allow_loopback: true\n",
    })
    _fake_python(root / "py" / "bin" / "python")
    p = detect_python_project(root, ["src/p/__init__.py"])
    assert p.python == str(root / "py" / "bin" / "python") and p.origin["python"] == ".sydes.yml"
    assert p.import_roots == ["src", "extra"] and p.origin["import_roots"] == ".sydes.yml"
    assert p.source_roots == ["src"] and p.origin["source_roots"] == "inferred"
    args = diffgenome_args(p, [])
    assert args[-3:] == ["--test-env", "DATABASE_HOST=127.0.0.1", "--allow-loopback"]
    assert p.origin["allow_loopback"] == ".sydes.yml"


def test_loopback_is_allowed_by_default_and_can_be_denied(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"pyproject.toml": '[project]\nname="p"\n', "app/__init__.py": "", "tests/test_a.py": ""})
    _fake_python(root / ".venv" / "bin" / "python")
    p = detect_python_project(root, [], overrides={})
    assert p.allow_loopback and p.origin["allow_loopback"] == "inferred" and "--allow-loopback" in diffgenome_args(p, [])
    p = detect_python_project(root, [], overrides={"allow_loopback": False})
    assert not p.allow_loopback and "--allow-loopback" not in diffgenome_args(p, [])


def test_sydes_yml_rejects_unknown_keys(tmp_path: Path) -> None:
    root = tmp_path / "r"
    root.mkdir()
    (root / ".sydes.yml").write_text("runtime:\n  pythn: x\n")
    with pytest.raises(DetectionError, match="unknown runtime key"):
        load_overrides(root)
    assert load_overrides(tmp_path) == {}


def test_changed_files_outside_known_roots_are_a_warning(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"pyproject.toml": '[project]\nname="p"\n[tool.setuptools.packages.find]\nwhere=["src"]\n',
                            "src/p/__init__.py": "", "scripts/tool.py": "", "tests/test_a.py": ""})
    _fake_python(root / ".venv" / "bin" / "python")
    p = detect_python_project(root, ["scripts/tool.py"], overrides={})
    assert any("outside the source and test roots: scripts/tool.py" in w for w in p.warnings)
    assert p.confidence == "medium"
    assert os.path.isabs(p.python)


def test_django_project_with_per_app_tests(tmp_path: Path) -> None:
    """healthchecks: no pytest config, tests per app, Django's own runner. Each app's tests/ is
    a test root (DiffGenome 0.1.4 takes several), and the tests run under pytest-django with
    manage.py's settings; an environment without pytest-django is rejected."""
    root = _repo(tmp_path, {
        "manage.py": 'import os\nos.environ.setdefault("DJANGO_SETTINGS_MODULE", "hc.settings")\n',
        "requirements.txt": "Django==5.1\n",
        "hc/__init__.py": "", "hc/settings.py": 'TEST_RUNNER = "hc.api.tests.CustomRunner"\n',
        "hc/accounts/__init__.py": "", "hc/accounts/models.py": "",
        "hc/accounts/tests/__init__.py": "", "hc/accounts/tests/test_models.py": "",
        "hc/api/__init__.py": "", "hc/api/tests/__init__.py": "", "hc/api/tests/test_views.py": "",
    })
    _fake_python(root / "nodjango" / "bin" / "python", missing=["pytest-django"])
    p = detect_python_project(root, ["hc/accounts/models.py"], overrides={"python": "nodjango/bin/python"})
    assert any("pytest-django" in w for w in p.warnings)  # an explicit python is kept, with a warning
    _fake_python(root / ".venv" / "bin" / "python")
    p = detect_python_project(root, ["hc/accounts/models.py"], overrides={})
    assert p.test_roots == ["hc/accounts/tests", "hc/api/tests"]
    assert p.pytest_args == ["-p", "pytest_django", "--ds=hc.settings"] and p.requires == ["pytest-django"]
    args = diffgenome_args(p, ["hc/accounts/models.py"])
    assert args.count("--test-root") == 2 and "--pytest-arg=-p pytest_django --ds=hc.settings" in args
    assert any("custom Django TEST_RUNNER hc.api.tests.CustomRunner" in w for w in p.warnings)


def test_django_already_configured_for_pytest_is_left_alone(tmp_path: Path) -> None:
    root = _repo(tmp_path, {
        "manage.py": 'import os\nos.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.settings")\n',
        "pytest.ini": "[pytest]\nDJANGO_SETTINGS_MODULE = app.settings\n",
        "app/__init__.py": "", "tests/test_a.py": "",
    })
    _fake_python(root / ".venv" / "bin" / "python")
    p = detect_python_project(root, [], overrides={})
    assert p.pytest_args == [] and "already configures Django" not in p.reasons.get("pytest_args", "")


def test_a_prepared_environment_is_labelled_prepared_not_an_override(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"pyproject.toml": '[project]\nname="p"\n', "app/__init__.py": "", "tests/test_a.py": ""})
    py = _fake_python(tmp_path / "prepared" / "bin" / "python")
    p = detect_python_project(root, [], overrides={}, prepared_python=py)
    assert p.python == str(py) and p.origin["python"] == "prepared"


def test_a_testpath_that_is_the_package_is_narrowed_to_its_tests(tmp_path: Path) -> None:
    """toolz: testpaths = toolz (tests live in toolz/tests); the package must stay code."""
    root = _repo(tmp_path, {
        "pyproject.toml": '[project]\nname="toolz"\n[tool.pytest.ini_options]\ntestpaths=["toolz"]\n',
        "toolz/__init__.py": "", "toolz/functoolz.py": "", "toolz/tests/test_functoolz.py": "",
        "toolz/curried/__init__.py": "", "toolz/curried/tests/test_curried.py": "",
    })
    _fake_python(root / ".venv" / "bin" / "python")
    p = detect_python_project(root, ["toolz/functoolz.py"], overrides={})
    assert p.test_roots == ["toolz/curried/tests", "toolz/tests"]
    assert "inside the package" in p.reasons["test_roots"] and p.import_roots == ["."]


def test_single_module_project_with_a_root_test_file(tmp_path: Path) -> None:
    """six: six.py and test_six.py at the repository root, no tests/ directory."""
    root = _repo(tmp_path, {"setup.py": "", "six.py": "x = 1\n", "test_six.py": "", "documentation/conf.py": ""})
    _fake_python(root / ".venv" / "bin" / "python")
    p = detect_python_project(root, ["six.py", "test_six.py"], overrides={})
    assert p.test_roots == ["test_six.py"] and p.import_roots == ["."]
    assert not p.warnings or all("outside" not in w for w in p.warnings)


def test_diffgenome_floor_excludes_the_broken_releases() -> None:
    """0.1.4 skipped subdirectory pytest configs and 0.1.5 broke filterwarnings=error
    projects; Sydes must never resolve to either (0.1.7 is the minimum supported)."""
    import re
    import tomllib

    root = Path(__file__).resolve().parents[1]
    deps = tomllib.loads((root / "pyproject.toml").read_text())["project"]["dependencies"]
    [spec] = [d for d in deps if d.startswith("diffgenome")]
    floor = tuple(int(x) for x in re.search(r">=\s*([\d.]+)", spec).group(1).split("."))
    assert floor >= (0, 1, 7)
    lock = (root / "uv.lock").read_text()
    locked = re.search(r'name = "diffgenome"\nversion = "([\d.]+)"', lock).group(1)
    assert tuple(int(x) for x in locked.split(".")) >= (0, 1, 7)
