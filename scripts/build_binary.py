"""Build a standalone Sydes CLI executable for the current platform.

Usage:
  uv run python scripts/build_binary.py
"""

from __future__ import annotations

from pathlib import Path
import platform
import shutil
import subprocess
import sys


def _platform_key() -> str:
    """Return VS Code extension-compatible platform key for this machine."""
    system = sys.platform
    machine = platform.machine().lower()

    if system == "darwin":
        if machine in {"arm64", "aarch64"}:
            return "darwin-arm64"
        if machine in {"x86_64", "amd64"}:
            return "darwin-x64"
    elif system.startswith("linux"):
        if machine in {"x86_64", "amd64"}:
            return "linux-x64"
    elif system == "win32":
        if machine in {"x86_64", "amd64"}:
            return "win32-x64"

    raise RuntimeError(
        f"Unsupported platform/arch for Sydes standalone binary: "
        f"sys.platform={system!r}, machine={machine!r}."
    )


def main() -> int:
    """Build Sydes one-file executable and place it in dist/binaries/<platform-key>/."""
    repo_root = Path(__file__).resolve().parents[1]
    platform_key = _platform_key()
    exe_name = "sydes.exe" if sys.platform == "win32" else "sydes"

    # Keep all PyInstaller internals out of repo root.
    pyinstaller_root = repo_root / "build" / "pyinstaller" / platform_key
    pyinstaller_dist = pyinstaller_root / "dist"
    pyinstaller_work = pyinstaller_root / "work"
    pyinstaller_spec = pyinstaller_root / "spec"

    final_dist_dir = repo_root / "dist" / "binaries" / platform_key
    final_binary = final_dist_dir / exe_name

    # Clean stale per-platform build outputs before rebuilding.
    if pyinstaller_root.exists():
        shutil.rmtree(pyinstaller_root)
    final_dist_dir.mkdir(parents=True, exist_ok=True)
    if final_binary.exists():
        final_binary.unlink()

    entry_script = repo_root / "src" / "sydes" / "cli" / "main.py"

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        "sydes",
        "--distpath",
        str(pyinstaller_dist),
        "--workpath",
        str(pyinstaller_work),
        "--specpath",
        str(pyinstaller_spec),
        "--paths",
        str(repo_root / "src"),
        str(entry_script),
    ]

    print(f"[sydes] Building standalone binary for {platform_key}...")
    subprocess.run(cmd, check=True, cwd=repo_root)

    built_binary = pyinstaller_dist / exe_name
    if not built_binary.exists():
        raise RuntimeError(f"PyInstaller did not produce expected binary: {built_binary}")

    shutil.copy2(built_binary, final_binary)
    if sys.platform != "win32":
        final_binary.chmod(0o755)

    print(f"[sydes] Binary created: {final_binary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
