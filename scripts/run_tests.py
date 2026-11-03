from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VENV = ROOT / ".venv"
SOURCE_ROOTS = (
    ROOT / "packages" / "fabric-domain" / "src",
    ROOT / "packages" / "fabric-sim" / "src",
    ROOT / "packages" / "event-contracts" / "src",
    ROOT / "services" / "gateway" / "src",
    ROOT / "services" / "planner" / "src",
    ROOT / "services" / "aggregator" / "src",
    ROOT / "services" / "evaluator" / "src",
)
MINIMUM_PYTHON = (3, 12)
MAXIMUM_PYTHON_EXCLUSIVE = (3, 14)


def active_venv_matches_project() -> bool:
    if sys.prefix == sys.base_prefix:
        return False
    return Path(sys.prefix).resolve() == EXPECTED_VENV.resolve()


def main() -> int:
    if not MINIMUM_PYTHON <= sys.version_info[:2] < MAXIMUM_PYTHON_EXCLUSIVE:
        print(
            "FabricSandbox requires Python 3.12 or 3.13; "
            f"current interpreter is {sys.version.split()[0]}."
        )
        return 2

    if not active_venv_matches_project():
        print("The project virtual environment is not active.")
        print(r"Run: .\.venv\Scripts\python.exe scripts\run_tests.py")
        return 2

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    pythonpath = [str(path) for path in SOURCE_ROOTS]
    existing_pythonpath = env.get("PYTHONPATH")
    if existing_pythonpath:
        pythonpath.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)

    command = [
        sys.executable,
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
        "-p",
        "test_*.py",
        "-v",
    ]
    completed = subprocess.run(command, cwd=ROOT, env=env, check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())

