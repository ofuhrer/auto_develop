from __future__ import annotations

import argparse

from agentic_devloop import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-loop",
        description="Run bounded AI development tasks with deterministic evidence.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="Initialize a project configuration.")
    init_parser.add_argument("--project", required=True, help="Project identifier.")
    init_parser.add_argument("--repo", required=True, help="Path to the target repository.")

    subparsers.add_parser("status", help="Show orchestrator status.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        print(f"project={args.project}")
        print(f"repo={args.repo}")
        return 0

    if args.command == "status":
        print("No runs found.")
        return 0

    parser.print_help()
    return 0
