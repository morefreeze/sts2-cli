#!/usr/bin/env python3
"""Install the run progress viewer as a macOS LaunchAgent."""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.sts2.run-progress-viewer"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def default_launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def launchctl_domain() -> str:
    return f"gui/{os.getuid()}"


def launchctl_service_target() -> str:
    return f"{launchctl_domain()}/{LABEL}"


def build_launch_agent_plist(repo_root: Path, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    log_dir = repo_root / "logs"
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(repo_root / ".venv" / "bin" / "python"),
            str(repo_root / "agent" / "run_progress_viewer.py"),
            "--host",
            host,
            "--port",
            str(port),
        ],
        "WorkingDirectory": str(repo_root),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(log_dir / "run_progress_viewer.launchd.out.log"),
        "StandardErrorPath": str(log_dir / "run_progress_viewer.launchd.err.log"),
    }


def write_launch_agent_plist(path: Path, plist: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        plistlib.dump(plist, handle, sort_keys=False)


def _run_launchctl(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["launchctl", *args], check=check, text=True)


def _validate_install_inputs(repo_root: Path) -> None:
    python_path = repo_root / ".venv" / "bin" / "python"
    viewer_path = repo_root / "agent" / "run_progress_viewer.py"
    if not python_path.exists():
        raise FileNotFoundError(f"missing Python interpreter: {python_path}")
    if not viewer_path.exists():
        raise FileNotFoundError(f"missing viewer script: {viewer_path}")


def install_launch_agent(
    repo_root: Path,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    plist_path: Path | None = None,
) -> Path:
    repo_root = repo_root.resolve()
    _validate_install_inputs(repo_root)
    (repo_root / "logs").mkdir(parents=True, exist_ok=True)
    path = plist_path or default_launch_agent_path()
    write_launch_agent_plist(path, build_launch_agent_plist(repo_root, host=host, port=port))

    _run_launchctl(["bootout", launchctl_service_target()], check=False)
    _run_launchctl(["bootstrap", launchctl_domain(), str(path)])
    _run_launchctl(["kickstart", "-k", launchctl_service_target()])
    return path


def uninstall_launch_agent(*, plist_path: Path | None = None) -> Path:
    path = plist_path or default_launch_agent_path()
    _run_launchctl(["bootout", launchctl_service_target()], check=False)
    if path.exists():
        path.unlink()
    return path


def print_status() -> int:
    completed = _run_launchctl(["print", launchctl_service_target()], check=False)
    return completed.returncode


def _add_repo_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--plist-path", type=Path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    install_parser = subparsers.add_parser("install", help="write, load, and start the LaunchAgent")
    _add_repo_options(install_parser)

    print_parser = subparsers.add_parser("print-plist", help="print the LaunchAgent plist XML")
    _add_repo_options(print_parser)

    uninstall_parser = subparsers.add_parser("uninstall", help="stop and remove the LaunchAgent")
    uninstall_parser.add_argument("--plist-path", type=Path)

    subparsers.add_parser("status", help="print launchd status for the LaunchAgent")

    args = parser.parse_args(argv)
    if args.command == "print-plist":
        plist = build_launch_agent_plist(args.repo_root, host=args.host, port=args.port)
        sys.stdout.write(plistlib.dumps(plist, sort_keys=False).decode("utf-8"))
        return 0
    if args.command == "install":
        path = install_launch_agent(args.repo_root, host=args.host, port=args.port, plist_path=args.plist_path)
        print(f"Installed {LABEL}: {path}")
        print(f"Viewer URL: http://{args.host}:{args.port}")
        return 0
    if args.command == "uninstall":
        path = uninstall_launch_agent(plist_path=args.plist_path)
        print(f"Uninstalled {LABEL}: {path}")
        return 0
    if args.command == "status":
        return print_status()
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
