from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VENV = ROOT / ".venv"
MINIMUM_PYTHON = (3, 12)
MAXIMUM_PYTHON_EXCLUSIVE = (3, 14)


def command_version(name: str, command: list[str]) -> tuple[bool, str]:
    executable = shutil.which(command[0])
    if executable is None:
        return False, "not found"

    try:
        completed = subprocess.run(
            [executable, *command[1:]],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"cannot run: {exc}"

    output = (completed.stdout or completed.stderr).strip().splitlines()
    detail = output[0] if output else "installed"
    return completed.returncode == 0, detail


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check the FabricSandbox environment.")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Also check tools needed by the completed full-stack project.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    checks: list[tuple[bool, str, str, str]] = []

    python_ok = MINIMUM_PYTHON <= sys.version_info[:2] < MAXIMUM_PYTHON_EXCLUSIVE
    checks.append(
        (
            python_ok,
            "Python",
            sys.version.split()[0],
            "S0",
        )
    )

    in_venv = sys.prefix != sys.base_prefix
    venv_ok = in_venv and Path(sys.prefix).resolve() == EXPECTED_VENV.resolve()
    checks.append(
        (
            venv_ok,
            "Project venv",
            str(EXPECTED_VENV) if venv_ok else f"active interpreter: {sys.prefix}",
            "S0",
        )
    )

    pip_ok, pip_detail = command_version("pip", [sys.executable, "-m", "pip", "--version"])
    checks.append((pip_ok, "pip", pip_detail, "S0"))

    env_file = ROOT / ".env"
    checks.append((env_file.is_file(), "Local .env", str(env_file), "S0"))

    if args.full:
        checks.extend(
            [
                (*command_version("Docker", ["docker", "--version"]), "full stack"),
                (*command_version("Docker Compose", ["docker", "compose", "version"]), "full stack"),
                (*command_version("Node.js", ["node", "--version"]), "full stack"),
                (*command_version("pnpm", ["pnpm", "--version"]), "full stack"),
            ]
        )

    print("FabricSandbox environment check")
    for ok, name, detail, required_for in checks:
        print(f"[{'OK' if ok else 'MISSING':7}] {name:16} {detail} ({required_for})")

    if any(not ok for ok, _, _, _ in checks):
        print("\nEnvironment is not ready.")
        print("See docs/environment-setup.md for setup instructions.")
        return 1

    print(f"\n{'Full stack' if args.full else 'S0'} environment is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

