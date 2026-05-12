from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agentic_devloop import __version__
from agentic_devloop.config import ProjectConfigError, load_project_config
from agentic_devloop.orchestrator import run_task
from agentic_devloop.status import load_run_summaries


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

    run_task_parser = subparsers.add_parser("run-task", help="Run one bounded task contract.")
    run_task_parser.add_argument("--project", required=True, help="Project identifier.")
    run_task_parser.add_argument("--contract", required=True, help="Path to a task contract YAML file.")
    run_task_parser.add_argument(
        "--config-dir",
        default="configs",
        help="Directory containing project config YAML files.",
    )
    run_task_parser.add_argument(
        "--runs-dir",
        default="runs",
        help="Directory where run evidence should be written.",
    )
    run_task_parser.add_argument(
        "--verification-timeout-seconds",
        type=int,
        default=600,
        help="Timeout for each verification command.",
    )
    run_task_parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow creating a worktree when the base repository has uncommitted changes.",
    )
    run_task_parser.add_argument(
        "--commit-on-accept",
        action="store_true",
        help="Commit accepted task changes in the task worktree.",
    )
    run_task_parser.add_argument(
        "--merge-on-accept",
        action="store_true",
        help="Commit accepted task changes and merge the task branch into the base branch.",
    )
    run_task_parser.add_argument(
        "--push-on-accept",
        action="store_true",
        help="Commit, merge, and push accepted task changes to origin.",
    )
    run_task_parser.add_argument(
        "--commit-message",
        help="Commit message to use when committing accepted task changes.",
    )

    status_parser = subparsers.add_parser("status", help="Show orchestrator status.")
    status_parser.add_argument(
        "--runs-dir",
        default="runs",
        help="Directory containing run evidence.",
    )
    status_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of runs to show.",
    )

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

    if args.command == "run-task":
        try:
            result = run_task(
                project_id=args.project,
                contract_path=Path(args.contract),
                config_dir=Path(args.config_dir),
                runs_dir=Path(args.runs_dir),
                verification_timeout_seconds=args.verification_timeout_seconds,
                allow_dirty=args.allow_dirty,
                commit_on_accept=args.commit_on_accept,
                merge_on_accept=args.merge_on_accept,
                push_on_accept=args.push_on_accept,
                commit_message=args.commit_message,
                progress=_print_progress,
            )
        except KeyboardInterrupt:
            parser.exit(130, "\ninterrupted: run-task stopped before final evidence collection\n")
        except Exception as error:
            parser.exit(2, f"error: {error}\n")

        print(json.dumps(_task_run_result(result), indent=2))
        return 0

    if args.command == "status":
        summaries = load_run_summaries(Path(args.runs_dir), limit=args.limit)
        if not summaries:
            print("No runs found.")
            return 0
        print(json.dumps([summary.__dict__ | {"bundle_path": str(summary.bundle_path)} for summary in summaries], indent=2))
        return 0

    parser.print_help()
    return 0


def _task_run_result(result) -> dict[str, object]:
    output = {
        "run_id": result.run_id,
        "worktree_path": str(result.worktree_path),
        "bundle_path": str(result.bundle_path),
        "decision": result.decision.decision,
        "rationale": result.decision.rationale,
    }
    if result.finalize is not None:
        output["commit_hash"] = result.finalize.commit_hash
        output["merged"] = result.finalize.merged
        output["pushed"] = result.finalize.pushed
    return output


def _print_progress(message: str) -> None:
    print(f"[agent-loop] {message}", file=sys.stderr, flush=True)
