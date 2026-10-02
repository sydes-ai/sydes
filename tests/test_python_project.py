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
