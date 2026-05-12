from __future__ import annotations

import argparse
import json
from pathlib import Path

from agentic_devloop import __version__
from agentic_devloop.config import ProjectConfigError, load_project_config


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

    config_parser = subparsers.add_parser("config", help="Load and print project configuration.")
    config_parser.add_argument("--project", required=True, help="Project identifier.")
    config_parser.add_argument(
        "--config-dir",
        default="configs",
        help="Directory containing project config YAML files.",
    )
    config_parser.add_argument(
        "--validate-repo",
        action="store_true",
        help="Fail if the configured repository path does not exist.",
    )

    subparsers.add_parser("status", help="Show orchestrator status.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        print(f"project={args.project}")
        print(f"repo={args.repo}")
        return 0

    if args.command == "config":
        try:
            config = load_project_config(
                args.project,
                Path(args.config_dir),
                validate_repo=args.validate_repo,
            )
        except ProjectConfigError as error:
            parser.exit(2, f"error: {error}\n")

        print(json.dumps(config.model_dump(mode="json"), indent=2))
        return 0

    if args.command == "status":
        print("No runs found.")
        return 0

    parser.print_help()
    return 0
