"""Detect how a Python repository's tests run, so runtime evidence needs no hand-written flags.

Deterministic; no model. `detect_python_project` reads packaging and pytest metadata and the
layout, picks the project the change touches, and validates an interpreter by running it once
(version, pytest, declared dependencies, which local packages it can import). Every field
records why it was chosen and where the value came from (`inferred`, `.sydes.yml`, `cli`);
anything ambiguous becomes a warning instead of a guess. `diffgenome_args` turns the result
into DiffGenome's existing flags. Test relevance is not decided here (see `test_selection`).

Precedence: inference < `.sydes.yml` (`runtime:`) < CLI (explicit `--behavioral-args`
bypasses detection entirely).
"""

from __future__ import annotations

import configparser
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MIN_PYTHON = (3, 12)  # DiffGenome's tracer is built on sys.monitoring
_PROJECT_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini")
_NOT_PACKAGES = {"tests", "test", "docs", "doc", "scripts", "examples", "build", "dist", "venv", ".venv"}
OVERRIDE_FILE = ".sydes.yml"


class DetectionError(Exception):
    """No trustworthy way to run this repository's tests; the message says what to do."""


@dataclass
class PythonProject:
    repo_root: str
    project_root: str = "."  # repo-relative
    source_roots: list[str] = field(default_factory=list)
    test_roots: list[str] = field(default_factory=list)
    pytest_config: str | None = None
    pytest_pythonpath: list[str] = field(default_factory=list)  # managed by pytest itself
    import_roots: list[str] = field(default_factory=list)  # passed as --pythonpath
    pytest_args: list[str] = field(default_factory=list)  # e.g. pytest-django for a Django project
    requires: list[str] = field(default_factory=list)  # test-runner packages the env must have
    python: str | None = None
    python_version: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    allow_loopback: bool = False
    confidence: str = "high"
    reasons: dict[str, str] = field(default_factory=dict)
    origin: dict[str, str] = field(default_factory=dict)  # field -> inferred | .sydes.yml | cli
    rejected_interpreters: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def note(self, name: str, reason: str, origin: str = "inferred") -> None:
        self.reasons[name] = reason
        self.origin[name] = origin


# -- metadata readers --------------------------------------------------------------------------


def _toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _ini(path: Path) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read(path, encoding="utf-8")
    except (OSError, configparser.Error):
        pass
    return cp


def _ini_list(value: str | None) -> list[str]:
    return [v for v in re.split(r"[\s,]+", value or "") if v]


def _git_files(repo: Path) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    return out.splitlines()


def pytest_config(project: Path, repo: Path) -> tuple[Path | None, dict[str, list[str]]]:
    """The config file pytest itself would use, searching from the project up to the repo root,
    with pytest's precedence: pytest.ini, pyproject.toml ([tool.pytest.ini_options]), tox.ini
    ([pytest]), setup.cfg ([tool:pytest]). Returns it and its testpaths/pythonpath."""
    d = project
    while True:
        ini = d / "pytest.ini"
        if ini.is_file():
            cp = _ini(ini)
            sec = cp["pytest"] if cp.has_section("pytest") else {}
            return ini, {k: _ini_list(sec.get(k)) for k in ("testpaths", "pythonpath")}
        pp = d / "pyproject.toml"
        opts = _toml(pp).get("tool", {}).get("pytest", {}).get("ini_options") if pp.is_file() else None
        if isinstance(opts, dict):
            def as_list(v: Any) -> list[str]:
                return [str(x) for x in v] if isinstance(v, list) else _ini_list(str(v or ""))
            return pp, {k: as_list(opts.get(k)) for k in ("testpaths", "pythonpath")}
        for name, section in (("tox.ini", "pytest"), ("setup.cfg", "tool:pytest")):
            f = d / name
            if f.is_file():
                cp = _ini(f)
                if cp.has_section(section):
                    return f, {k: _ini_list(cp[section].get(k)) for k in ("testpaths", "pythonpath")}
        if d.resolve() == repo.resolve() or d.parent == d:
            return None, {"testpaths": [], "pythonpath": []}
        d = d.parent


def _django_settings(project: Path, cfg: Path | None) -> tuple[str | None, str]:
    """(settings module, reason) for a Django project whose tests pytest cannot run on its own:
    `manage.py` names the settings and no pytest config sets them."""
    if cfg is not None:
        text = cfg.read_text("utf-8", "replace")
        if "DJANGO_SETTINGS_MODULE" in text or "--ds" in text:
            return None, f"{cfg.name} already configures Django for pytest"
    manage = project / "manage.py"
    if not manage.is_file():
        return None, ""
    m = re.search(r"DJANGO_SETTINGS_MODULE['\"]\s*,\s*['\"]([\w.]+)['\"]", manage.read_text("utf-8", "replace"))
    if not m:
        return None, "manage.py does not name a settings module"
    return m.group(1), f"Django project (manage.py sets DJANGO_SETTINGS_MODULE={m.group(1)}); its tests run under pytest-django"


def _metadata_source_dirs(project: Path) -> tuple[list[str], str] | None:
    """Source directories (project-relative) declared by packaging metadata, with the reason."""
    data = _toml(project / "pyproject.toml")
    tool = data.get("tool", {})
    hatch = tool.get("hatch", {}).get("build", {}).get("targets", {}).get("wheel", {}).get("packages")
    if isinstance(hatch, list) and hatch:
        dirs = sorted({str(Path(p).parent) for p in hatch})
        return dirs, f"pyproject [tool.hatch.build.targets.wheel] packages = {hatch}"
    st = tool.get("setuptools", {})
    where = st.get("packages", {}).get("find", {}).get("where") if isinstance(st.get("packages"), dict) else None
    if isinstance(where, list) and where:
        return [str(w) for w in where], f"pyproject [tool.setuptools.packages.find] where = {where}"
    pkg_dir = st.get("package-dir")
    if isinstance(pkg_dir, dict) and pkg_dir.get(""):
        return [str(pkg_dir[""])], f'pyproject [tool.setuptools] package-dir = {{"": "{pkg_dir[""]}"}}'
    poetry = tool.get("poetry", {}).get("packages")
    if isinstance(poetry, list):
        froms = sorted({str(p.get("from")) for p in poetry if isinstance(p, dict) and p.get("from")})
        if froms:
            return froms, f"pyproject [tool.poetry] packages from = {froms}"
    cfg = _ini(project / "setup.cfg")
    if cfg.has_section("options"):
        pd = cfg["options"].get("package_dir", "")
        m = re.search(r"=\s*([\w./-]+)", pd)
        if m:
            return [m.group(1)], f"setup.cfg [options] package_dir = {pd.strip()}"
    if cfg.has_section("options.packages.find") and cfg["options.packages.find"].get("where"):
        w = cfg["options.packages.find"]["where"].strip()
        return [w], f"setup.cfg [options.packages.find] where = {w}"
    setup_py = project / "setup.py"
    if setup_py.is_file():
        m = re.search(r"package_dir\s*=\s*\{\s*['\"]{2}\s*:\s*['\"]([\w./-]+)['\"]", setup_py.read_text("utf-8", "replace"))
        if m:
            return [m.group(1)], f"setup.py package_dir = {{'': '{m.group(1)}'}}"
    return None


def _packages_in(d: Path) -> list[str]:
    if not d.is_dir():
        return []
    return sorted(
        p.name for p in d.iterdir()
        if p.is_dir() and (p / "__init__.py").is_file() and p.name not in _NOT_PACKAGES
        and not p.name.startswith(".")
    )


def _source_dirs(project: Path) -> tuple[list[str], str, str]:
    """(project-relative source dirs, reason, confidence)."""
    meta = _metadata_source_dirs(project)
    if meta:
        return meta[0], meta[1], "high"
    if _packages_in(project / "src"):
        return ["src"], f"layout: src/ holds packages {_packages_in(project / 'src')}", "high"
    pkgs = _packages_in(project)
    if pkgs:
        return ["."], f"layout: packages at the project root {pkgs}", "medium"
    return ["."], "no packaging metadata or package directories: the project root", "low"


_TEST_FILE = re.compile(r"^(test_[^/]*|[^/]*_test)\.py$")
_NOT_MODULES = {"setup.py", "conftest.py", "noxfile.py", "tasks.py", "fabfile.py", "manage.py"}


def _tests_dirs_under(files: list[str], prefix: str) -> list[str]:
    """tests/ or test/ directories (holding test files) below `prefix` (repo-relative, '' = all)."""
    dirs = {
        re.sub(r"(/tests?)/.*$", r"\1", str(Path(f).parent))
        for f in files
        if f.startswith(prefix) and re.search(r"(^|/)tests?/(.*/)?(test_[^/]*|[^/]*_test)\.py$", f)
    }
    return sorted(d for d in dirs if re.search(r"(^|/)tests?$", d))


def _modules_in(d: Path) -> list[str]:
    """Top-level project modules (single-module projects such as six.py)."""
    if not d.is_dir():
        return []
    return sorted(
        p.stem for p in d.glob("*.py")
        if p.name not in _NOT_MODULES and not _TEST_FILE.match(p.name)
    )


def _declared_dependencies(project: Path) -> list[str]:
    data = _toml(project / "pyproject.toml")
    deps = data.get("project", {}).get("dependencies")
    if not isinstance(deps, list):
        deps = []
        req = project / "requirements.txt"
        if req.is_file():
            deps = [line for line in req.read_text("utf-8", "replace").splitlines()
                    if line.strip() and not line.lstrip().startswith(("#", "-"))]
    names = []
    for d in deps:
        if ";" in d:  # environment markers: not reliably checkable here
            continue
        m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", str(d))
        if m:
            names.append(m.group(1))
    return names


# -- interpreter ---------------------------------------------------------------------------------

_PROBE = r"""
import importlib.metadata as md, importlib.util as iu, json, sys
deps, pkgs = json.loads(sys.argv[1]), json.loads(sys.argv[2])
def installed(name):
    try:
        md.distribution(name); return True
    except md.PackageNotFoundError:
        return False
print(json.dumps({
    "version": list(sys.version_info[:3]), "prefix": sys.prefix,
    "pytest": iu.find_spec("pytest") is not None,
    "missing": [d for d in deps if not installed(d)],
    "origins": {p: (getattr(iu.find_spec(p), "origin", None) if iu.find_spec(p) else None) for p in pkgs},
}))
"""


def _candidates(repo: Path, project: Path) -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    if os.environ.get("VIRTUAL_ENV"):
        out.append((Path(os.environ["VIRTUAL_ENV"]) / "bin" / "python", "active virtualenv (VIRTUAL_ENV)"))
    for base in dict.fromkeys([project, repo]):
        for name in (".venv", "venv", "env"):
            out.append((base / name / "bin" / "python", f"{os.path.relpath(base / name, repo)}/ virtualenv"))
    if os.environ.get("UV_PROJECT_ENVIRONMENT"):
        out.append((Path(os.environ["UV_PROJECT_ENVIRONMENT"]) / "bin" / "python", "UV_PROJECT_ENVIRONMENT"))
    if (project / "poetry.lock").is_file() and shutil.which("poetry"):
        try:
            p = subprocess.run(["poetry", "env", "info", "-p"], cwd=project, capture_output=True,
                               text=True, timeout=30).stdout.strip()
            if p:
                out.append((Path(p) / "bin" / "python", "poetry environment"))
        except (OSError, subprocess.TimeoutExpired):
            pass
    for name in ("python3", "python"):
        exe = shutil.which(name)
        if exe:
            out.append((Path(exe), f"`{name}` on PATH (the job's prepared Python)"))
    return out


def _probe(python: Path, deps: list[str], pkgs: list[str]) -> dict[str, Any] | None:
    try:
        r = subprocess.run(
            [str(python), "-I", "-c", _PROBE, json.dumps(deps), json.dumps(pkgs)],
            capture_output=True, text=True, timeout=60, cwd="/",
        )
        return json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def _choose_interpreter(proj: PythonProject, repo: Path, project: Path, pkgs: list[str]) -> dict[str, Any]:
    deps = _declared_dependencies(project) + proj.requires
    own = os.path.realpath(sys.prefix)
    seen: set[str] = set()
    for path, why in _candidates(repo, project):
        if not path.exists() or str(path) in seen:
            continue
        seen.add(str(path))
        info = _probe(path, deps, pkgs)
        if info is None:
            proj.rejected_interpreters.append(f"{path} ({why}): does not run")
            continue
        version = ".".join(str(x) for x in info["version"])
        problems = []
        if os.path.realpath(info["prefix"]) == own:
            problems.append("is Sydes' own environment")
        if tuple(info["version"][:2]) < MIN_PYTHON:
            problems.append(f"Python {version} < 3.12 (DiffGenome needs sys.monitoring)")
        if not info["pytest"]:
            problems.append("pytest not installed")
        if info["missing"]:
            problems.append("missing declared dependencies: " + ", ".join(info["missing"][:5]))
        if problems:
            proj.rejected_interpreters.append(f"{path} ({why}): " + "; ".join(problems))
            continue
        proj.python, proj.python_version = str(path), version
        proj.note("python", f"{why}: Python {version}, pytest and the {len(deps)} declared/required packages present")
        return info
    raise DetectionError(
        "no usable Python environment for this repository's tests: "
        + (" | ".join(proj.rejected_interpreters) or "none found")
        + ". Prepare one (e.g. `.venv` with the project and pytest installed, Python 3.12+) "
        "or set `runtime.python` in .sydes.yml."
    )


# -- detection -----------------------------------------------------------------------------------


def _project_root(repo: Path, changed: list[str], files: list[str]) -> tuple[Path, str]:
    roots = sorted(
        {str(Path(f).parent) for f in files if Path(f).name in _PROJECT_MARKERS},
        key=lambda r: (r.count("/"), r),
    )
    roots = [r for r in roots if "node_modules" not in r and "/site-packages/" not in r]
    if not roots:
        return repo, "no Python project metadata; the repository root"
    changed_py = [c for c in changed if c.endswith(".py")]

    def owner(path: str) -> str | None:
        hits = [r for r in roots if r == "." or path == r or path.startswith(r + "/")]
        return max(hits, key=len) if hits else None

    counts: dict[str, int] = {}
    for c in changed_py:
        o = owner(c)
        if o:
            counts[o] = counts.get(o, 0) + 1
    if counts:
        best = max(counts, key=lambda r: (counts[r], -r.count("/")))
        detail = ", ".join(f"{r}: {n}" for r, n in sorted(counts.items(), key=lambda kv: -kv[1]))
        return repo / best, f"holds most changed Python files ({detail})"
    top = roots[0]
    return repo / top, f"the top-most Python project ({top}); no changed Python files"


def load_overrides(repo: Path) -> dict[str, Any]:
    path = repo / OVERRIDE_FILE
    if not path.is_file():
        return {}
    import yaml

    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise DetectionError(f"{OVERRIDE_FILE} is not valid YAML: {exc}") from exc
    runtime = doc.get("runtime") if isinstance(doc, dict) else None
    if runtime is None:
        return {}
    if not isinstance(runtime, dict):
        raise DetectionError(f"{OVERRIDE_FILE}: `runtime` must be a mapping")
    known = {"python", "project_root", "source_roots", "test_roots", "pythonpath", "env", "allow_loopback"}
    unknown = sorted(set(runtime) - known)
    if unknown:
        raise DetectionError(f"{OVERRIDE_FILE}: unknown runtime key(s) {unknown}; known: {sorted(known)}")
    return runtime


def detect_python_project(
    repo: Path, changed_files: list[str], overrides: dict[str, Any] | None = None,
    prepared_python: Path | None = None,
) -> PythonProject:
    repo = repo.resolve()
    over = load_overrides(repo) if overrides is None else overrides
    proj = PythonProject(repo_root=str(repo))
    files = _git_files(repo)

    if over.get("project_root"):
        project = repo / str(over["project_root"])
        proj.note("project_root", "set in .sydes.yml", OVERRIDE_FILE)
    else:
        project, why = _project_root(repo, changed_files, files)
        proj.note("project_root", why)
    proj.project_root = os.path.relpath(project, repo)

    def rel(p: Path) -> str:
        return os.path.relpath(p, repo)

    cfg, ini = pytest_config(project, repo)
    if cfg is not None:
        proj.pytest_config = rel(cfg)
        proj.pytest_pythonpath = [rel(cfg.parent / p) for p in ini["pythonpath"]]
        proj.note("pytest_config", "the file pytest uses (pytest.ini > pyproject > tox.ini > setup.cfg)")

    settings, why = _django_settings(project, cfg)
    if settings:
        proj.pytest_args = ["-p", "pytest_django", f"--ds={settings}"]
        proj.requires.append("pytest-django")
        proj.note("pytest_args", why)
        base = project / settings.replace(".", "/")
        for f in (base.with_suffix(".py"), base / "__init__.py"):
            m = re.search(r"^TEST_RUNNER\s*=\s*['\"]([\w.]+)['\"]", f.read_text("utf-8", "replace"), re.M) if f.is_file() else None
            if m and m.group(1) != "django.test.runner.DiscoverRunner":
                proj.warnings.append(
                    f"custom Django TEST_RUNNER {m.group(1)} is not used under pytest-django: settings it "
                    "applies for tests are missing (set them as env in .sydes.yml if the settings read env)"
                )

    if over.get("source_roots"):
        proj.source_roots = [str(s) for s in over["source_roots"]]
        proj.note("source_roots", "set in .sydes.yml", OVERRIDE_FILE)
    else:
        dirs, why, conf = _source_dirs(project)
        proj.source_roots = [rel(project / d) for d in dirs]
        proj.note("source_roots", why)
        if conf != "high":
            proj.confidence = conf
        members = _toml(project / "pyproject.toml").get("tool", {}).get("uv", {}).get("workspace", {}).get("members")
        for m in members if isinstance(members, list) else []:
            member = (project / str(m)).resolve()
            if (member / "pyproject.toml").is_file():
                extra, _, _ = _source_dirs(member)
                proj.source_roots += [rel(member / d) for d in extra]
        if isinstance(members, list) and members:
            proj.reasons["source_roots"] += f"; plus uv workspace members {members}"

    if over.get("test_roots"):
        proj.test_roots = [str(t) for t in over["test_roots"]]
        proj.note("test_roots", "set in .sydes.yml", OVERRIDE_FILE)
    else:
        base = cfg.parent if cfg is not None else project
        tp = [rel(base / t) for t in ini["testpaths"] if (base / t).is_dir()]
        if tp:
            # a testpath that is a source package (toolz: testpaths = toolz) would make the
            # package itself test code: use the tests/ directories inside it
            refined: list[str] = []
            for t in tp:
                is_package = (repo / t / "__init__.py").is_file()
                inner = _tests_dirs_under(files, t.rstrip("/") + "/") if is_package else []
                refined += inner or [t]
            proj.test_roots = refined
            proj.note("test_roots", f"pytest testpaths in {proj.pytest_config}" + (
                " (tests/ directories inside the package it names)" if refined != tp else ""))
        else:
            found = [rel(project / d) for d in ("tests", "test") if (project / d).is_dir()]
            if found:
                proj.test_roots = found
                proj.note("test_roots", "layout: tests/ or test/ under the project root")
            else:
                # tests kept per package (Django apps: hc/accounts/tests, hc/api/tests, ...)
                prefix = "" if proj.project_root == "." else proj.project_root.rstrip("/") + "/"
                per_pkg = _tests_dirs_under(files, prefix)
                root_files = sorted(
                    f for f in files
                    if str(Path(f).parent) == (proj.project_root if proj.project_root != "." else ".")
                    and _TEST_FILE.match(Path(f).name)
                )
                if per_pkg:
                    proj.test_roots = per_pkg
                    proj.note("test_roots", f"layout: {len(per_pkg)} tests/ directories inside packages")
                elif root_files:
                    proj.test_roots = root_files
                    proj.note("test_roots", "layout: test files at the project root (" + ", ".join(root_files[:3]) + ")")
                else:
                    proj.test_roots = [proj.project_root]
                    proj.note("test_roots", "no test directory found; the project root")
                    proj.warnings.append("no tests/ or test/ directory and no pytest testpaths")

    outside = [
        c for c in changed_files
        if c.endswith(".py") and not any(c == t or c.startswith(t.rstrip("/") + "/") for t in proj.test_roots)
        and not any(s == "." or c.startswith(s.rstrip("/") + "/") for s in proj.source_roots)
        and Path(c).name not in ("setup.py", "conftest.py")
    ]
    if outside:
        proj.warnings.append(
            f"{len(outside)} changed Python file(s) outside the source and test roots: "
            + ", ".join(outside[:4])
        )

    top_pkgs = {s: _packages_in(repo / s) for s in proj.source_roots}
    all_pkgs = sorted({p for ps in top_pkgs.values() for p in ps})
    if prepared_python is not None or over.get("python"):
        py = Path(str(prepared_python or over["python"]))
        py = py if py.is_absolute() else repo / py
        info = _probe(py, _declared_dependencies(project) + proj.requires, all_pkgs)
        if info is None:
            raise DetectionError(f"{OVERRIDE_FILE} runtime.python {py} does not run")
        proj.python = str(py)
        proj.python_version = ".".join(str(x) for x in info["version"])
        if prepared_python is not None:
            proj.note("python", "environment prepared by Sydes from the project's declared dependencies", "prepared")
        else:
            proj.note("python", "set in .sydes.yml", OVERRIDE_FILE)
        if info["missing"]:
            proj.warnings.append("declared/required packages not installed: " + ", ".join(info["missing"][:5]))
    else:
        info = _choose_interpreter(proj, repo, project, all_pkgs)

    if over.get("pythonpath") is not None:
        proj.import_roots = [str(p) for p in over["pythonpath"]]
        proj.note("import_roots", "set in .sydes.yml", OVERRIDE_FILE)
    else:
        # Always the source roots that hold packages: prepended inside DiffGenome's workspace,
        # so the PR's code is what runs and is traced, never an installed or editable copy
        # (which points at the original checkout, or another one entirely).
        proj.import_roots = [s for s, ps in top_pkgs.items() if ps or _modules_in(repo / s)]
        # Test roots that are not packages themselves but hold packages (shared test helpers,
        # e.g. Baserow's baserow_premium_tests): pytest's prepend import mode only puts them
        # on sys.path when it collects a test from them, so a selected subset can miss them.
        helper_roots = [
            t for t in proj.test_roots
            if not (repo / t / "__init__.py").is_file() and _packages_in(repo / t)
        ]
        proj.import_roots += [t for t in helper_roots if t not in proj.import_roots]
        elsewhere = sorted(
            p for p, origin in (info.get("origins") or {}).items()
            if origin and not os.path.realpath(origin).startswith(str(repo) + os.sep)
        )
        proj.note("import_roots", (
            "the source roots, so the workspace copy of the PR's code is imported"
            + (f"; test roots holding shared test packages: {', '.join(helper_roots)}" if helper_roots else "")
            + (f" (the interpreter's own copy of {', '.join(elsewhere)} is elsewhere and is shadowed)"
               if elsewhere else "")
        ))

    proj.env = {str(k): str(v) for k, v in (over.get("env") or {}).items()}
    if proj.env:
        proj.note("env", "set in .sydes.yml", OVERRIDE_FILE)
    if proj.warnings and proj.confidence == "high":
        proj.confidence = "medium"
    # after confidence: a platform note, not doubt about the detection
    if "allow_loopback" in over:
        proj.allow_loopback = bool(over["allow_loopback"])
        proj.note("allow_loopback", "set in .sydes.yml", OVERRIDE_FILE)
    else:
        # test servers bind localhost (pytest-httpbin, live servers); DiffGenome's Go and Node
        # collectors always allow it. Linux keeps it private to the sandbox; macOS shares the
        # host's loopback, so say so.
        proj.allow_loopback = True
        proj.note("allow_loopback", "default: tests may start localhost servers (as for Go and Node)")
        if sys.platform == "darwin":
            proj.warnings.append(
                "loopback on macOS includes services running on this host; set "
                "runtime.allow_loopback: false in .sydes.yml to deny it"
            )
    return proj


def diffgenome_args(proj: PythonProject, changed_files: list[str]) -> list[str]:
    """DiffGenome's existing flags for this project. DiffGenome indexes one source root: the one
    holding most changed files; the rest stay importable through the environment or
    --pythonpath."""
    def hits(root: str) -> int:
        return sum(1 for c in changed_files if root == "." or c.startswith(root.rstrip("/") + "/"))

    source = max(proj.source_roots, key=hits) if proj.source_roots else "."
    args = ["--runtime", "python", "--python", str(proj.python), "--source-root", source]
    for t in proj.test_roots or ["."]:
        args += ["--test-root", t]
    if proj.pytest_args:
        args.append("--pytest-arg=" + " ".join(proj.pytest_args))
    for p in proj.import_roots:
        args += ["--pythonpath", p]
    for k, v in sorted(proj.env.items()):
        args += ["--test-env", f"{k}={v}"]
    if proj.allow_loopback:
        args.append("--allow-loopback")
    return args


def changed_files(repo: Path, base: str, head: str = "HEAD") -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=AMR", base, head],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout
    return out.splitlines()


def provenance(proj: PythonProject) -> dict[str, Any]:
    """What a report keeps about the environment runtime evidence ran in."""
    return {
        k: getattr(proj, k)
        for k in (
            "project_root", "python", "python_version", "source_roots", "test_roots",
            "pytest_config", "import_roots", "allow_loopback", "confidence", "origin", "warnings",
        )
    } | {"env_keys": sorted(proj.env)}


def prepare_environment(repo: Path, changed: list[str], dest: Path) -> tuple[Path, list[str]]:
    """Explicit mode (never implicit): create a virtualenv at `dest` with the project, its
    test/dev extras and pytest, the way the project declares them. uv.lock: `uv sync --frozen`;
    otherwise pip: the project with [test] or [dev] extras, then requirements*.txt. Runs the
    project's own build and install steps unsandboxed, as the repository's CI would.
    Returns the interpreter and the commands run."""
    repo = repo.resolve()
    project, _ = _project_root(repo, changed, _git_files(repo))
    dest = dest.resolve()
    python = dest / "bin" / "python"
    log: list[str] = []

    def run(argv: list[str], cwd: Path, env: dict[str, str] | None = None, check: bool = True) -> bool:
        log.append(" ".join(argv))
        r = subprocess.run(argv, cwd=cwd, env={**os.environ, **(env or {})}, capture_output=True, text=True)
        if check and r.returncode != 0:
            raise DetectionError(f"preparing the environment failed: {' '.join(argv)}: "
                                 + (r.stderr.strip().splitlines() or ["?"])[-1])
        return r.returncode == 0

    if (project / "uv.lock").is_file() and shutil.which("uv"):
        run(["uv", "sync", "--frozen", "--all-extras", "--python", f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}"],
            project, {"UV_PROJECT_ENVIRONMENT": str(dest)})
        if not _probe(python, [], []) or not _probe(python, [], []).get("pytest"):
            run(["uv", "pip", "install", "--python", str(python), "pytest"], project)
        return python, log
    base_python = shutil.which(f"python{MIN_PYTHON[0]}.{MIN_PYTHON[1]}") or shutil.which("python3")
    if base_python is None:
        raise DetectionError("no python3 to create the environment with")
    own = os.path.realpath(sys.prefix)
    info = _probe(Path(base_python), [], [])
    if info and os.path.realpath(info["prefix"]) == own:
        base_python = sys.executable  # Sydes' interpreter is fine as a *base*: a new venv is created
    run([base_python, "-m", "venv", str(dest)], project)
    pip = [str(python), "-m", "pip", "install", "-q"]
    if any((project / f).is_file() for f in ("pyproject.toml", "setup.py", "setup.cfg")):
        # the test extras the project declares (pip only warns about an unknown extra, so
        # guessing names silently installed none)
        data = _toml(project / "pyproject.toml")
        declared = data.get("project", {}).get("optional-dependencies") or {}
        extras = [e for e in ("test", "tests", "testing", "dev") if e in declared]
        target = f".[{','.join(extras)}]" if extras else "."
        if not run([*pip, "-e", target], project, check=False):
            run([*pip, "-e", "."], project, check=False)
        groups = [g for g in ("test", "tests", "dev") if g in (data.get("dependency-groups") or {})]
        for g in groups:  # PEP 735 (pip 25.1+)
            run([*pip, "--group", g], project, check=False)
    skipped: list[str] = []
    reqs = sorted(project.glob("requirements*.txt")) + sorted(
        f for f in project.glob("requirements/*.txt") if re.search(r"test|dev", f.name)
    )  # requirements/tests.txt (Pallets) as well as requirements-dev.txt
    for req in reqs:
        if run([*pip, "-r", str(req)], project, check=False):
            continue
        # one uninstallable entry (a native library missing on this host) fails the whole
        # file: install the rest one by one and say which were skipped
        for line in req.read_text("utf-8", "replace").splitlines():
            spec = line.split("#", 1)[0].strip()
            if spec and not spec.startswith("-") and not run([*pip, spec], project, check=False):
                skipped.append(spec)
    cfg, _ = pytest_config(project, repo)
    runner = ["pytest"] + (["pytest-django"] if _django_settings(project, cfg)[0] else [])
    run([*pip, *runner], project)
    if skipped:
        log.append("skipped (could not install): " + ", ".join(skipped))
    return python, log
