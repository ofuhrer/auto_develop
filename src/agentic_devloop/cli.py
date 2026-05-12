from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agentic_devloop import __version__
from agentic_devloop.config import ProjectConfigError, load_project_config
from agentic_devloop.objective import run_objective
from agentic_devloop.orchestrator import run_task
from agentic_devloop.planning import plan_release_contracts
from agentic_devloop.planner_backend import CodexPlannerBackend
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
    _add_config_dir_argument(config_parser)
    config_parser.add_argument(
        "--validate-repo",
        action="store_true",
        help="Fail if the configured repository path does not exist.",
    )

    run_task_parser = subparsers.add_parser("run-task", help="Run one bounded task contract.")
    run_task_parser.add_argument("--project", required=True, help="Project identifier.")
    run_task_parser.add_argument("--contract", required=True, help="Path to a task contract YAML file.")
    _add_config_dir_argument(run_task_parser)
    _add_runs_dir_argument(run_task_parser, purpose="run evidence")
    _add_verification_timeout_argument(run_task_parser)
    _add_allow_dirty_argument(run_task_parser, subject="a worktree")
    _add_finalization_arguments(run_task_parser, task_scope="the task worktree")
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
    _add_release_execution_arguments(run_release_parser)

    plan_release_parser = subparsers.add_parser(
        "plan-release",
        help="Create a conservative release contract plan from an objective.",
    )
    plan_release_parser.add_argument("--objective", required=True, help="Release objective YAML file.")
    _add_planning_mode_arguments(plan_release_parser)
    plan_release_parser.add_argument(
        "--project",
        help="Project identifier. Required for strong-model mode.",
    )
    _add_config_dir_argument(plan_release_parser)
    _add_contracts_dir_argument(plan_release_parser)
    _add_runs_dir_argument(plan_release_parser, purpose="planning output")
    plan_release_parser.add_argument(
        "--inspect-proposed-contracts",
        action="store_true",
        help="Include proposed contract details in the CLI output.",
    )
    plan_release_parser.add_argument(
        "--write-contracts-dir",
        help="Write validated contract drafts to this directory without running them.",
    )
    _add_execute_planner_argument(plan_release_parser, help_text="Execute the configured planner backend instead of only writing the planner prompt.")

    run_objective_parser = subparsers.add_parser(
        "run-objective",
        help="Plan contracts from an objective, write them, then run the resulting release.",
    )
    run_objective_parser.add_argument("--project", required=True, help="Project identifier.")
    run_objective_parser.add_argument("--objective", required=True, help="Release objective YAML file.")
    _add_planning_mode_arguments(run_objective_parser)
    _add_execute_planner_argument(run_objective_parser, help_text="Execute the configured planner backend when using strong-model mode.")
    _add_release_execution_arguments(run_objective_parser)

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


def _add_config_dir_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config-dir",
        default="configs",
        help="Directory containing project config YAML files.",
    )


def _add_contracts_dir_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--contracts-dir",
        default="contracts",
        help="Directory containing task contract YAML files.",
    )


def _add_runs_dir_argument(parser: argparse.ArgumentParser, *, purpose: str) -> None:
    parser.add_argument(
        "--runs-dir",
        default="runs",
        help=f"Directory where {purpose} should be written.",
    )


def _add_verification_timeout_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--verification-timeout-seconds",
        type=int,
        default=600,
        help="Timeout for each verification command.",
    )


def _add_allow_dirty_argument(parser: argparse.ArgumentParser, *, subject: str) -> None:
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help=f"Allow creating {subject} when the base repository has uncommitted changes.",
    )


def _add_finalization_arguments(parser: argparse.ArgumentParser, *, task_scope: str) -> None:
    parser.add_argument(
        "--commit-on-accept",
        action="store_true",
        help=f"Commit accepted task changes in {task_scope}.",
    )
    parser.add_argument(
        "--merge-on-accept",
        action="store_true",
        help="Commit accepted task changes and merge task branches into the base branch.",
    )
    parser.add_argument(
        "--push-on-accept",
        action="store_true",
        help="Commit, merge, and push accepted task changes to origin.",
    )


def _add_release_execution_arguments(parser: argparse.ArgumentParser) -> None:
    _add_config_dir_argument(parser)
    _add_contracts_dir_argument(parser)
    _add_runs_dir_argument(parser, purpose="run evidence")
    _add_verification_timeout_argument(parser)
    _add_allow_dirty_argument(parser, subject="worktrees")
    _add_finalization_arguments(parser, task_scope="task worktrees")
    parser.add_argument(
        "--continue-on-failure",
        action="store_true",
        help="Continue running remaining contracts after a task is not accepted.",
    )
    parser.add_argument(
        "--execution-mode",
        choices=["sequential", "parallel"],
        default="sequential",
        help="Execution scheduling mode. Parallel mode rejects broad overlaps.",
    )
    parser.add_argument(
        "--debug-keep-artifacts",
        action="store_true",
        help="Keep task worktrees and branches after each task for debugging.",
    )


def _add_planning_mode_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--mode",
        choices=["deterministic", "strong-model"],
        default="deterministic",
        help="Planning mode.",
    )
    parser.add_argument(
        "--strong-model",
        dest="mode",
        action="store_const",
        const="strong-model",
        help="Shortcut for --mode strong-model.",
    )


def _add_execute_planner_argument(parser: argparse.ArgumentParser, *, help_text: str) -> None:
    parser.add_argument(
        "--execute-planner",
        action="store_true",
        help=help_text,
    )


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
                debug_keep_artifacts=args.debug_keep_artifacts,
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
            if args.execute_planner and args.mode != "strong-model":
                raise ValueError("--execute-planner requires --mode strong-model")
            planner_backend = _codex_planner_backend(
                project_id=args.project,
                config_dir=Path(args.config_dir),
                runs_dir=Path(args.runs_dir),
            ) if args.execute_planner else None
            result = plan_release_contracts(
                objective_path=Path(args.objective),
                contracts_dir=Path(args.contracts_dir),
                runs_dir=Path(args.runs_dir),
                write_contracts_dir=Path(args.write_contracts_dir) if args.write_contracts_dir else None,
                mode=args.mode,
                project_id=args.project,
                config_dir=Path(args.config_dir),
                planner_backend=planner_backend,
            )
        except Exception as error:
            parser.exit(2, f"error: {error}\n")

        print(
            json.dumps(
                _plan_release_result(
                    result,
                    inspect_proposed_contracts=args.inspect_proposed_contracts,
                ),
                indent=2,
            )
        )
        return 0

    if args.command == "run-objective":
        try:
            if args.execute_planner and args.mode != "strong-model":
                raise ValueError("--execute-planner requires --mode strong-model")
            if args.mode == "strong-model" and not args.execute_planner:
                raise ValueError("run-objective --mode strong-model requires --execute-planner")
            planner_backend = _codex_planner_backend(
                project_id=args.project,
                config_dir=Path(args.config_dir),
                runs_dir=Path(args.runs_dir),
            ) if args.execute_planner else None
            result = run_objective(
                project_id=args.project,
                objective_path=Path(args.objective),
                config_dir=Path(args.config_dir),
                contracts_dir=Path(args.contracts_dir),
                runs_dir=Path(args.runs_dir),
                planning_mode=args.mode,
                planner_backend=planner_backend,
                verification_timeout_seconds=args.verification_timeout_seconds,
                allow_dirty=args.allow_dirty,
                commit_on_accept=args.commit_on_accept,
                merge_on_accept=args.merge_on_accept,
                push_on_accept=args.push_on_accept,
                stop_on_failure=not args.continue_on_failure,
                execution_mode=args.execution_mode,
                debug_keep_artifacts=args.debug_keep_artifacts,
                progress=_print_progress,
            )
        except KeyboardInterrupt:
            parser.exit(130, "\ninterrupted: run-objective stopped before completion\n")
        except Exception as error:
            parser.exit(2, f"error: {error}\n")

        print(json.dumps(_objective_run_result(result), indent=2))
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
        "log_path": str(result.log_path),
        "decision": result.decision,
        "tasks": [_task_run_result(task_result) for task_result in result.task_results],
    }


def _plan_release_result(result, *, inspect_proposed_contracts: bool) -> dict[str, object]:
    output: dict[str, object] = {
        "release_id": result.release_id,
        "plan_path": str(result.plan_path),
        "generated_contracts": len(result.plan.generated_contracts),
        "warnings": result.plan.warnings,
    }
    if result.written_contract_paths:
        output["written_contract_paths"] = [str(path) for path in result.written_contract_paths]
    if inspect_proposed_contracts:
        output["proposed_contracts"] = [
            {
                "task_id": generated_contract.task_id,
                "title": generated_contract.title,
                "objective": generated_contract.objective,
                "rationale": generated_contract.rationale,
                "suggested_contract": generated_contract.suggested_contract.model_dump(mode="json"),
            }
            for generated_contract in result.plan.generated_contracts
        ]
    return output


def _objective_run_result(result) -> dict[str, object]:
    return {
        "release_id": result.release_id,
        "plan_path": str(result.planning.plan_path),
        "written_contract_paths": [str(path) for path in result.planning.written_contract_paths],
        "release": _release_run_result(result.release),
    }


def _codex_planner_backend(*, project_id: str | None, config_dir: Path, runs_dir: Path) -> CodexPlannerBackend:
    if project_id is None:
        raise ValueError("--execute-planner requires --project")
    config = load_project_config(project_id, config_dir, validate_repo=True)
    planner = config.model_roles.get("planner", config.executor)
    return CodexPlannerBackend(
        config=planner,
        repo_path=config.repo_path,
    )


def _print_progress(message: str) -> None:
    print(f"[agent-loop] {message}", file=sys.stderr, flush=True)
