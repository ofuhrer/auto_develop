from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agentic_devloop import __version__
from agentic_devloop.config import ProjectConfigError, load_project_config
from agentic_devloop.orchestrator import run_task
from agentic_devloop.planning import plan_release_contracts
from agentic_devloop.release import run_release
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

    run_release_parser = subparsers.add_parser(
        "run-release",
        help="Run an ordered release task queue from contracts.",
    )
    run_release_parser.add_argument("--project", required=True, help="Project identifier.")
    run_release_parser.add_argument("--release", required=True, help="Release identifier.")
    run_release_parser.add_argument(
        "--contract",
        action="append",
        dest="contracts",
        help="Contract path to run, in order. May be passed multiple times.",
    )
    run_release_parser.add_argument(
        "--config-dir",
        default="configs",
        help="Directory containing project config YAML files.",
    )
    run_release_parser.add_argument(
        "--contracts-dir",
        default="contracts",
        help="Directory containing task contract YAML files.",
    )
    run_release_parser.add_argument(
        "--runs-dir",
        default="runs",
        help="Directory where run evidence should be written.",
    )
    run_release_parser.add_argument(
        "--verification-timeout-seconds",
        type=int,
        default=600,
        help="Timeout for each verification command.",
    )
    run_release_parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow creating worktrees when the base repository has uncommitted changes.",
    )
    run_release_parser.add_argument(
        "--commit-on-accept",
        action="store_true",
        help="Commit accepted task changes in task worktrees.",
    )
    run_release_parser.add_argument(
        "--merge-on-accept",
        action="store_true",
        help="Commit accepted task changes and merge task branches into the base branch.",
    )
    run_release_parser.add_argument(
        "--push-on-accept",
        action="store_true",
        help="Commit, merge, and push accepted task changes to origin.",
    )
    run_release_parser.add_argument(
        "--continue-on-failure",
        action="store_true",
        help="Continue running remaining contracts after a task is not accepted.",
    )
    run_release_parser.add_argument(
        "--execution-mode",
        choices=["sequential", "parallel"],
        default="sequential",
        help="Execution scheduling mode. Parallel mode rejects broad overlaps.",
    )

    plan_release_parser = subparsers.add_parser(
        "plan-release",
        help="Create a conservative release contract plan from an objective.",
    )
    plan_release_parser.add_argument("--objective", required=True, help="Release objective YAML file.")
    plan_release_parser.add_argument(
        "--mode",
        choices=["deterministic", "strong-model"],
        default="deterministic",
        help="Planning mode.",
    )
    plan_release_parser.add_argument(
        "--project",
        help="Project identifier. Required for strong-model mode.",
    )
    plan_release_parser.add_argument(
        "--config-dir",
        default="configs",
        help="Directory containing project config YAML files.",
    )
    plan_release_parser.add_argument(
        "--contracts-dir",
        default="contracts",
        help="Directory containing task contract YAML files.",
    )
    plan_release_parser.add_argument(
        "--runs-dir",
        default="runs",
        help="Directory where planning output should be written.",
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

    if args.command == "run-release":
        try:
            result = run_release(
                project_id=args.project,
                release_id=args.release,
                contract_paths=[Path(path) for path in args.contracts] if args.contracts else None,
                config_dir=Path(args.config_dir),
                contracts_dir=Path(args.contracts_dir),
                runs_dir=Path(args.runs_dir),
                verification_timeout_seconds=args.verification_timeout_seconds,
                allow_dirty=args.allow_dirty,
                commit_on_accept=args.commit_on_accept,
                merge_on_accept=args.merge_on_accept,
                push_on_accept=args.push_on_accept,
                stop_on_failure=not args.continue_on_failure,
                execution_mode=args.execution_mode,
                progress=_print_progress,
            )
        except KeyboardInterrupt:
            parser.exit(130, "\ninterrupted: run-release stopped before completion\n")
        except Exception as error:
            parser.exit(2, f"error: {error}\n")

        print(json.dumps(_release_run_result(result), indent=2))
        return 0

    if args.command == "plan-release":
        try:
            result = plan_release_contracts(
                objective_path=Path(args.objective),
                contracts_dir=Path(args.contracts_dir),
                runs_dir=Path(args.runs_dir),
                mode=args.mode,
                project_id=args.project,
                config_dir=Path(args.config_dir),
            )
        except Exception as error:
            parser.exit(2, f"error: {error}\n")

        print(
            json.dumps(
                {
                    "release_id": result.release_id,
                    "plan_path": str(result.plan_path),
                    "generated_contracts": len(result.plan.generated_contracts),
                    "warnings": result.plan.warnings,
                },
                indent=2,
            )
        )
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


def _release_run_result(result) -> dict[str, object]:
    return {
        "release_id": result.release_id,
        "run_id": result.run_id,
        "summary_path": str(result.summary_path),
        "decision": result.decision,
        "tasks": [_task_run_result(task_result) for task_result in result.task_results],
    }


def _print_progress(message: str) -> None:
    print(f"[agent-loop] {message}", file=sys.stderr, flush=True)
