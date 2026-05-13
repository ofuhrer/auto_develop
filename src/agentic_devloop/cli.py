from __future__ import annotations

import argparse
import json
import sys
from dataclasses import is_dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from agentic_devloop import __version__
from agentic_devloop.backlog import CodexBacklogPlannerBackend, plan_backlog
from agentic_devloop.cleanup import cleanup_release_artifacts
from agentic_devloop.config import ProjectConfigError, load_project_config
from agentic_devloop.doctor import run_doctor
from agentic_devloop.objective import run_objective
from agentic_devloop.orchestrator import run_task
from agentic_devloop.models import GovernorStopCategory, GovernorStopContext, GovernorStopReason
from agentic_devloop.planning import plan_release_contracts
from agentic_devloop.planner_backend import CodexPlannerBackend
from agentic_devloop.governor_log import GovernorEventContext, GovernorEventType, build_governor_event_log_writer
from agentic_devloop.governor import governor_stop_category_for_reason
from agentic_devloop.release import run_release
from agentic_devloop.status import load_run_summaries

try:
    from agentic_devloop.backlog import run_backlog, run_governor
except ImportError:
    def run_backlog(**_kwargs):
        raise NotImplementedError("run-backlog orchestrator is not available in this build")

    def run_governor(**_kwargs):
        raise NotImplementedError("run-governor orchestrator is not available in this build")


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

    doctor_parser = subparsers.add_parser("doctor", help="Inspect project preflight diagnostics.")
    doctor_parser.add_argument("--project", required=True, help="Project identifier.")
    _add_config_dir_argument(doctor_parser)
    doctor_parser.add_argument(
        "--release",
        help="Release identifier to inspect stale release and task branches.",
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

    plan_backlog_parser = subparsers.add_parser(
        "plan-backlog",
        help="Analyze roadmap and repo state into prioritized development epics.",
    )
    plan_backlog_parser.add_argument("--project", required=True, help="Project identifier.")
    plan_backlog_parser.add_argument("--goal", required=True, help="Repository goal used to prioritize epics.")
    plan_backlog_parser.add_argument(
        "--roadmap",
        default="docs/design/ROADMAP_AND_BACKLOG.md",
        help="Roadmap Markdown file to analyze.",
    )
    _add_config_dir_argument(plan_backlog_parser)
    _add_runs_dir_argument(plan_backlog_parser, purpose="backlog planning output")
    _add_planning_mode_arguments(plan_backlog_parser)
    _add_execute_planner_argument(plan_backlog_parser, help_text="Execute the configured planner agent instead of only writing the backlog governor prompt.")
    plan_backlog_parser.add_argument(
        "--objectives-dir",
        default="objectives",
        help="Directory where selected objective YAML should be written.",
    )
    plan_backlog_parser.add_argument(
        "--write-objective",
        action="store_true",
        help="Write an objective YAML for the highest-priority epic.",
    )

    run_backlog_parser = subparsers.add_parser(
        "run-backlog",
        help="Select one backlog epic and execute it through objective orchestration.",
    )
    run_backlog_parser.add_argument("--project", required=True, help="Project identifier.")
    run_backlog_parser.add_argument(
        "--epic-id",
        help="Backlog epic identifier to execute. Omit to use the planner-selected epic.",
    )
    run_backlog_parser.add_argument("--goal", required=True, help="Repository goal used to prioritize epics.")
    run_backlog_parser.add_argument(
        "--roadmap",
        default="docs/design/ROADMAP_AND_BACKLOG.md",
        help="Roadmap Markdown file to analyze.",
    )
    run_backlog_parser.add_argument(
        "--mode",
        choices=["strong-model"],
        default="strong-model",
        help="Backlog execution mode. Only strong-model execution is supported.",
    )
    _add_execute_planner_argument(run_backlog_parser, help_text="Execute the configured planner backend.")
    _add_release_execution_arguments(run_backlog_parser)
    run_backlog_parser.add_argument(
        "--objectives-dir",
        default="objectives",
        help="Directory where selected objective YAML should be written.",
    )

    run_governor_parser = subparsers.add_parser(
        "run-governor",
        help="Run the autonomous governor for the next N backlog epics.",
    )
    run_governor_parser.add_argument("--project", required=True, help="Project identifier.")
    run_governor_parser.add_argument(
        "--epic-count",
        type=int,
        required=True,
        help="Maximum number of epic cycles to attempt before stopping.",
    )
    run_governor_parser.add_argument(
        "--epic-id",
        help="Optional first epic identifier to execute. Later cycles use planner selection.",
    )
    run_governor_parser.add_argument("--goal", required=True, help="Repository goal used to prioritize epics.")
    run_governor_parser.add_argument(
        "--roadmap",
        default="docs/design/ROADMAP_AND_BACKLOG.md",
        help="Roadmap Markdown file to analyze.",
    )
    run_governor_parser.add_argument(
        "--mode",
        choices=["strong-model"],
        default="strong-model",
        help="Governor execution mode. Only strong-model execution is supported.",
    )
    _add_execute_planner_argument(run_governor_parser, help_text="Execute the configured planner backend.")
    _add_release_execution_arguments(run_governor_parser)
    run_governor_parser.add_argument(
        "--objectives-dir",
        default="objectives",
        help="Directory where selected objective YAML should be written.",
    )
    run_governor_parser.add_argument(
        "--cleanup-accepted-cycles-dry-run",
        action="store_true",
        help=(
            "Generate per-cycle cleanup dry-run reports for accepted releases. "
            "Also runs implicitly when --release-finalize is not none."
        ),
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

    cleanup_parser = subparsers.add_parser(
        "cleanup",
        help="Clean stale release worktrees and task branches.",
    )
    cleanup_parser.add_argument("--project", required=True, help="Project identifier.")
    cleanup_parser.add_argument("--release", required=True, help="Release identifier.")
    _add_config_dir_argument(cleanup_parser)
    cleanup_parser.add_argument(
        "--force",
        action="store_true",
        help="Actually remove artifacts. Without this flag cleanup only reports candidates.",
    )
    cleanup_parser.add_argument(
        "--include-integration-branch",
        action="store_true",
        help="Also delete feature/<release> when it exists and is not checked out.",
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
        "--integration-branch",
        help="Feature branch owned by the release orchestrator. Defaults to feature/<release>.",
    )
    parser.add_argument(
        "--release-finalize",
        choices=["none", "merge-main", "push-feature", "push-main"],
        default="none",
        help="Final action for the release integration branch after all tasks are accepted.",
    )
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

    if args.command == "doctor":
        try:
            result = run_doctor(
                project_id=args.project,
                config_dir=Path(args.config_dir),
                release_id=args.release,
            )
        except Exception as error:
            parser.exit(2, f"error: {error}\n")

        print(json.dumps(result.to_dict(), indent=2))
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
                release_finalize=args.release_finalize,
                integration_branch=args.integration_branch,
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
                release_finalize=args.release_finalize,
                integration_branch=args.integration_branch,
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

    if args.command == "plan-backlog":
        try:
            if args.execute_planner and args.mode != "strong-model":
                raise ValueError("--execute-planner requires --mode strong-model")
            planner_backend = _codex_backlog_planner_backend(
                project_id=args.project,
                config_dir=Path(args.config_dir),
            ) if args.execute_planner else None
            result = plan_backlog(
                project_id=args.project,
                goal=args.goal,
                roadmap_path=Path(args.roadmap),
                config_dir=Path(args.config_dir),
                runs_dir=Path(args.runs_dir),
                objectives_dir=Path(args.objectives_dir),
                write_objective=args.write_objective,
                mode=args.mode,
                planner_backend=planner_backend,
            )
        except Exception as error:
            parser.exit(2, f"error: {error}\n")

        print(json.dumps(_backlog_plan_result(result), indent=2))
        return 0

    if args.command == "run-backlog":
        governor_run_id = _make_governor_run_id(project_id=args.project)
        governor_writer = build_governor_event_log_writer(
            runs_dir=Path(args.runs_dir),
            run_id=governor_run_id,
        )
        observed: dict[str, bool] = {
            "release_started": False,
            "repair_or_resume": False,
            "normalization": False,
            "finalized": False,
        }

        def backlog_progress(message: str) -> None:
            _print_progress(message)
            if "event=release_started" in message:
                observed["release_started"] = True
            if (
                "event=repair_" in message
                or "event=task_resumed" in message
                or "event=conflict_repair_started" in message
            ):
                observed["repair_or_resume"] = True
            if "planner_contract_normalization" in message or "event=normalized_contract_plan" in message:
                observed["normalization"] = True
            if "event=release_merged" in message or "event=release_pushed" in message:
                observed["finalized"] = True

        governor_writer.write(
            event_type=GovernorEventType.GOVERNOR_STARTED,
            message=f"project={args.project} run-backlog started goal={json.dumps(args.goal)}",
        )
        try:
            if not args.execute_planner:
                raise ValueError("run-backlog requires --execute-planner")
            planner_backend = _codex_backlog_planner_backend(
                project_id=args.project,
                config_dir=Path(args.config_dir),
            )
            result = run_backlog(
                project_id=args.project,
                goal=args.goal,
                roadmap_path=Path(args.roadmap),
                selected_epic_id=args.epic_id,
                config_dir=Path(args.config_dir),
                contracts_dir=Path(args.contracts_dir),
                runs_dir=Path(args.runs_dir),
                objectives_dir=Path(args.objectives_dir),
                mode=args.mode,
                planner_backend=planner_backend,
                verification_timeout_seconds=args.verification_timeout_seconds,
                allow_dirty=args.allow_dirty,
                commit_on_accept=args.commit_on_accept,
                merge_on_accept=args.merge_on_accept,
                push_on_accept=args.push_on_accept,
                release_finalize=args.release_finalize,
                integration_branch=args.integration_branch,
                stop_on_failure=not args.continue_on_failure,
                execution_mode=args.execution_mode,
                debug_keep_artifacts=args.debug_keep_artifacts,
                progress=backlog_progress,
            )
        except KeyboardInterrupt:
            parser.exit(130, "\ninterrupted: run-backlog stopped before completion\n")
        except Exception as error:
            parser.exit(2, f"error: {error}\n")

        governor_writer.write(
            event_type=GovernorEventType.BACKLOG_PLANNING_COMPLETED,
            message=f"selected_epic_id={result.selected_epic_id}",
            artifacts=[result.plan_path],
        )
        governor_writer.write(
            event_type=GovernorEventType.EPIC_SELECTED,
            message=f"selected_epic_id={result.selected_epic_id}",
            artifacts=[result.plan_path],
        )
        governor_writer.write(
            event_type=GovernorEventType.OBJECTIVE_READY,
            message=f"objective_path={result.objective_path}",
            artifacts=[result.objective_path],
        )
        governor_writer.write(
            event_type=GovernorEventType.CONTRACT_PLAN_COMPLETED,
            message=f"contract_plan_path={result.contract_plan_path}",
            artifacts=[result.contract_plan_path],
        )
        if observed["release_started"] and getattr(result, "release", None) is not None:
            governor_writer.write(
                event_type=GovernorEventType.RELEASE_STARTED,
                message=f"release_id={result.release.release_id}",
                artifacts=[result.release.log_path],
            )
        if observed["normalization"] and getattr(result, "release", None) is not None:
            governor_writer.write(
                event_type=GovernorEventType.CONTRACT_NORMALIZATION,
                message=f"release_id={result.release.release_id} planner contract normalization applied",
                artifacts=[result.contract_plan_path, result.release.log_path],
            )
        if observed["repair_or_resume"] and getattr(result, "release", None) is not None:
            governor_writer.write(
                event_type=GovernorEventType.REPAIR_DECISION,
                message=f"release_id={result.release.release_id} repair or resume observed",
                artifacts=[result.release.summary_path, result.release.log_path],
            )
        if getattr(result, "release", None) is not None:
            governor_writer.write(
                event_type=GovernorEventType.RELEASE_COMPLETED,
                message=f"release_id={result.release.release_id} decision={result.release.decision}",
                artifacts=_release_artifact_links(result.release),
            )
            if observed["finalized"] or args.release_finalize != "none":
                governor_writer.write(
                    event_type=GovernorEventType.FINALIZATION_COMPLETED,
                    message=f"release_id={result.release.release_id} mode={args.release_finalize}",
                    artifacts=[result.release.summary_path],
                )
        plan_warnings = getattr(result.plan, "repo_state_updates", None) or getattr(result.plan, "roadmap_updates", None)
        if plan_warnings:
            governor_writer.write(
                event_type=GovernorEventType.STATE_REFRESHED,
                message="repo-state refresh proposal captured in backlog plan",
                artifacts=[result.plan_path],
            )
        release_id = result.release.release_id if getattr(result, "release", None) is not None else result.release_id
        completion_artifacts = [result.plan_path]
        if getattr(result, "release", None) is not None:
            completion_artifacts.append(result.release.summary_path)
        governor_writer.write(
            event_type=GovernorEventType.GOVERNOR_COMPLETED,
            message=f"selected_epic_id={result.selected_epic_id} release_id={release_id}",
            artifacts=completion_artifacts,
        )

        print(json.dumps(_backlog_run_result(result), indent=2))
        return 0

    if args.command == "run-governor":
        governor_run_id = _make_governor_run_id(project_id=args.project)
        governor_writer = build_governor_event_log_writer(
            runs_dir=Path(args.runs_dir),
            run_id=governor_run_id,
        )

        def governor_progress(message: str) -> None:
            _print_progress(message)

        governor_writer.write(
            event_type=GovernorEventType.GOVERNOR_STARTED,
            message=(
                f"project={args.project} run-governor started "
                f"epic_count={args.epic_count} goal={json.dumps(args.goal)}"
            ),
        )
        try:
            if not args.execute_planner:
                raise ValueError("run-governor requires --execute-planner")
            planner_backend = _codex_backlog_planner_backend(
                project_id=args.project,
                config_dir=Path(args.config_dir),
            )
            result = run_governor(
                project_id=args.project,
                goal=args.goal,
                epic_count=args.epic_count,
                roadmap_path=Path(args.roadmap),
                selected_epic_id=args.epic_id,
                config_dir=Path(args.config_dir),
                contracts_dir=Path(args.contracts_dir),
                runs_dir=Path(args.runs_dir),
                objectives_dir=Path(args.objectives_dir),
                mode=args.mode,
                planner_backend=planner_backend,
                verification_timeout_seconds=args.verification_timeout_seconds,
                allow_dirty=args.allow_dirty,
                commit_on_accept=args.commit_on_accept,
                merge_on_accept=args.merge_on_accept,
                push_on_accept=args.push_on_accept,
                release_finalize=args.release_finalize,
                integration_branch=args.integration_branch,
                stop_on_failure=not args.continue_on_failure,
                execution_mode=args.execution_mode,
                debug_keep_artifacts=args.debug_keep_artifacts,
                progress=governor_progress,
            )
        except KeyboardInterrupt:
            parser.exit(130, "\ninterrupted: run-governor stopped before completion\n")
        except Exception as error:
            stop_context = _governor_failure_stop_context(error)
            governor_writer.write(
                event_type=GovernorEventType.STOP_REASON_RECORDED,
                message=(
                    f"governor stop context category={stop_context.category.value} "
                    f"reason={stop_context.reason}"
                ),
                artifacts=stop_context.evidence_artifact_paths,
                context=GovernorEventContext(
                    phase=GovernorEventType.STOP_REASON_RECORDED.value,
                    stop_reason=stop_context.reason,
                    artifact_count=len(stop_context.evidence_artifact_paths),
                    details={"stop_category": stop_context.category.value},
                ),
            )
            parser.exit(2, f"error: {error}\n")

        def write_cycle_event(
            *,
            cycle_index: int,
            event_type: GovernorEventType,
            message: str,
            artifacts: list[Path],
            epic_id: str | None = None,
            release_id: str | None = None,
        ) -> None:
            governor_writer.write(
                event_type=event_type,
                message=message,
                artifacts=artifacts,
                context=GovernorEventContext(
                    phase=event_type.value,
                    cycle_index=cycle_index,
                    epic_id=epic_id,
                    release_id=release_id,
                    artifact_count=len(artifacts),
                ),
            )

        cleanup_reports_dir = governor_writer.paths.run_root / "cleanup"
        cleanup_reports_dir.mkdir(parents=True, exist_ok=True)
        cycles_for_output = []

        for index, cycle in enumerate(result.cycles, start=1):
            cycle_for_output = cycle
            cycle_release_id = cycle.release.release_id if cycle.release is not None else cycle.release_id
            cleanup_path_for_cycle: Path | None = None
            cycle_artifacts = _cycle_artifact_graph(
                cycle=cycle,
                cleanup_path=cleanup_path_for_cycle,
            )
            manifest = getattr(cycle, "evidence_manifest", None)

            state_review_snapshot_path = getattr(manifest, "state_review_snapshot_path", None) if manifest is not None else None
            state_refresh_summary_path = getattr(manifest, "state_refresh_summary_path", None) if manifest is not None else None
            state_refresh_error_path = getattr(manifest, "state_refresh_error_path", None) if manifest is not None else None
            backlog_plan_path = getattr(manifest, "backlog_plan_path", None) if manifest is not None else None

            if state_review_snapshot_path is not None and state_review_snapshot_path.exists():
                write_cycle_event(
                    cycle_index=index,
                    event_type=GovernorEventType.STATE_REVIEW_COMPLETED,
                    message=f"cycle={index} epic_id={cycle.selected_epic_id} state_review_snapshot_path={state_review_snapshot_path}",
                    artifacts=[state_review_snapshot_path],
                    epic_id=cycle.selected_epic_id,
                    release_id=cycle_release_id,
                )
            if state_refresh_error_path is not None and state_refresh_error_path.exists():
                write_cycle_event(
                    cycle_index=index,
                    event_type=GovernorEventType.STATE_REFRESH_ERROR,
                    message=f"cycle={index} epic_id={cycle.selected_epic_id} state_refresh_error_path={state_refresh_error_path}",
                    artifacts=[state_refresh_error_path],
                    epic_id=cycle.selected_epic_id,
                    release_id=cycle_release_id,
                )
            elif state_refresh_summary_path is not None and state_refresh_summary_path.exists():
                write_cycle_event(
                    cycle_index=index,
                    event_type=GovernorEventType.STATE_REFRESH_SUMMARY,
                    message=f"cycle={index} epic_id={cycle.selected_epic_id} state_refresh_summary_path={state_refresh_summary_path}",
                    artifacts=[state_refresh_summary_path],
                    epic_id=cycle.selected_epic_id,
                    release_id=cycle_release_id,
                )

            backlog_planning_artifacts = cycle_artifacts["planning"] + cycle_artifacts["repo_state_proposal"]
            write_cycle_event(
                cycle_index=index,
                event_type=GovernorEventType.BACKLOG_PLANNING_COMPLETED,
                message=f"cycle={index} epic_id={cycle.selected_epic_id} backlog_plan_path={backlog_plan_path or cycle.plan_path}",
                artifacts=backlog_planning_artifacts,
                epic_id=cycle.selected_epic_id,
                release_id=cycle_release_id,
            )
            write_cycle_event(
                cycle_index=index,
                event_type=GovernorEventType.BACKLOG_SELECTION_COMPLETED,
                message=f"cycle={index} selected_epic_id={cycle.selected_epic_id}",
                artifacts=_existing_paths([backlog_plan_path or getattr(cycle, "plan_path", None)]),
                epic_id=cycle.selected_epic_id,
                release_id=cycle_release_id,
            )
            governor_writer.write(
                event_type=GovernorEventType.EPIC_SELECTED,
                message=f"cycle={index} selected_epic_id={cycle.selected_epic_id}",
                artifacts=cycle_artifacts["planning"] + cycle_artifacts["repo_state_proposal"],
            )
            objective_artifacts = cycle_artifacts["objective"]
            if objective_artifacts:
                write_cycle_event(
                    cycle_index=index,
                    event_type=GovernorEventType.OBJECTIVE_GENERATION_COMPLETED,
                    message=f"cycle={index} objective_path={cycle.objective_path}",
                    artifacts=objective_artifacts,
                    epic_id=cycle.selected_epic_id,
                    release_id=cycle_release_id,
                )
            governor_writer.write(
                event_type=GovernorEventType.OBJECTIVE_READY,
                message=f"cycle={index} objective_path={cycle.objective_path}",
                artifacts=cycle_artifacts["objective"],
            )
            if cycle.contract_plan_path is not None:
                contract_generation_artifacts = _existing_paths(
                    [
                        getattr(manifest, "execution_strategy_selection_path", None) if manifest is not None else None,
                        getattr(manifest, "one_shot_execution_input_path", None) if manifest is not None else None,
                        getattr(manifest, "supervisor_decision_path", None) if manifest is not None else None,
                        getattr(manifest, "contract_plan_path", None) if manifest is not None else None,
                    ]
                )
                if contract_generation_artifacts:
                    write_cycle_event(
                        cycle_index=index,
                        event_type=GovernorEventType.CONTRACT_GENERATION_COMPLETED,
                        message=f"cycle={index} contract_plan_path={cycle.contract_plan_path}",
                        artifacts=contract_generation_artifacts,
                        epic_id=cycle.selected_epic_id,
                        release_id=cycle_release_id,
                    )
                governor_writer.write(
                    event_type=GovernorEventType.CONTRACT_PLAN_COMPLETED,
                    message=f"cycle={index} contract_plan_path={cycle.contract_plan_path}",
                    artifacts=cycle_artifacts["planning"],
                )
            if cycle.release is not None:
                cleanup_handoff_note = ""
                child_release_artifacts = _existing_paths(
                    [
                        getattr(manifest, "release_summary_path", None) if manifest is not None else None,
                        getattr(manifest, "release_log_path", None) if manifest is not None else None,
                        getattr(manifest, "release_review_path", None) if manifest is not None else None,
                    ]
                )
                if child_release_artifacts:
                    write_cycle_event(
                        cycle_index=index,
                        event_type=GovernorEventType.CHILD_RELEASE_COMPLETED,
                        message=f"cycle={index} release_id={cycle.release.release_id} decision={cycle.release.decision}",
                        artifacts=child_release_artifacts,
                        epic_id=cycle.selected_epic_id,
                        release_id=cycle.release.release_id,
                    )
                governor_writer.write(
                    event_type=GovernorEventType.RELEASE_COMPLETED,
                    message=f"cycle={index} release_id={cycle.release.release_id} decision={cycle.release.decision}",
                    artifacts=cycle_artifacts["release"] + cycle_artifacts["review"] + cycle_artifacts["decision"],
                )
                feature_review_artifacts = _existing_paths(
                    [
                        getattr(manifest, "feature_review_path", None) if manifest is not None else None,
                        getattr(manifest, "feature_review_recheck_path", None) if manifest is not None else None,
                        *(
                            list(getattr(manifest, "feature_review_proposal_paths", []))
                            if manifest is not None
                            else []
                        ),
                    ]
                )
                if feature_review_artifacts:
                    write_cycle_event(
                        cycle_index=index,
                        event_type=GovernorEventType.FEATURE_REVIEW_COMPLETED,
                        message=f"cycle={index} release_id={cycle.release.release_id} feature_review_artifacts={len(feature_review_artifacts)}",
                        artifacts=feature_review_artifacts,
                        epic_id=cycle.selected_epic_id,
                        release_id=cycle.release.release_id,
                    )
                final_verification_path = getattr(cycle.release, "final_integration_verification_path", None)
                if final_verification_path is not None and final_verification_path.exists():
                    write_cycle_event(
                        cycle_index=index,
                        event_type=GovernorEventType.FINAL_VERIFICATION_COMPLETED,
                        message=f"cycle={index} release_id={cycle.release.release_id} final_integration_verification_path={final_verification_path}",
                        artifacts=[final_verification_path],
                        epic_id=cycle.selected_epic_id,
                        release_id=cycle.release.release_id,
                    )
                decision_artifacts = cycle_artifacts["decision"]
                if decision_artifacts:
                    write_cycle_event(
                        cycle_index=index,
                        event_type=GovernorEventType.REPAIR_DECISION,
                        message=f"cycle={index} release_id={cycle.release.release_id} decision_artifacts={len(decision_artifacts)}",
                        artifacts=decision_artifacts,
                        epic_id=cycle.selected_epic_id,
                        release_id=cycle.release.release_id,
                    )
                cleanup_result = None
                should_collect_cleanup_dry_run = (
                    args.cleanup_accepted_cycles_dry_run or args.release_finalize != "none"
                )
                if str(cycle.release.decision) == "accepted" and should_collect_cleanup_dry_run:
                    cleanup_result = cleanup_release_artifacts(
                        project_id=args.project,
                        release_id=cycle.release.release_id,
                        config_dir=Path(args.config_dir),
                        force=False,
                        include_integration_branch=False,
                        runs_dir=Path(args.runs_dir),
                    )
                    cleanup_payload = _cleanup_result(cleanup_result)
                    cleanup_path = cleanup_reports_dir / f"cycle_{index:03d}_{cycle.release.release_id}_cleanup.json"
                    cleanup_path.write_text(json.dumps(cleanup_payload, indent=2) + "\n", encoding="utf-8")
                    cleanup_path_for_cycle = cleanup_path
                    evidence_manifest = getattr(cycle, "evidence_manifest", None)
                    if evidence_manifest is not None:
                        evidence_manifest = evidence_manifest.model_copy(
                            update={"cleanup_report_path": cleanup_path}
                        )
                    cycle_for_output = _with_updates(
                        cycle_for_output,
                        {
                            "cleanup_result": cleanup_payload,
                            "evidence_manifest": evidence_manifest,
                        },
                    )
                    cycle_artifacts = _cycle_artifact_graph(
                        cycle=cycle_for_output,
                        cleanup_path=cleanup_path_for_cycle,
                    )
                    cleanup_handoff_note = f" cleanup_handoff dry_run={cleanup_payload['dry_run']}"
                    if cleanup_path_for_cycle is not None and cleanup_path_for_cycle.exists():
                        write_cycle_event(
                            cycle_index=index,
                            event_type=GovernorEventType.CLEANUP_ELIGIBILITY_EVALUATED,
                            message=f"cycle={index} release_id={cycle.release.release_id} cleanup_report_path={cleanup_path_for_cycle}",
                            artifacts=[cleanup_path_for_cycle],
                            epic_id=cycle.selected_epic_id,
                            release_id=cycle.release.release_id,
                        )
                if args.release_finalize != "none" or getattr(cycle, "finalization_result", None) is not None:
                    governor_writer.write(
                        event_type=GovernorEventType.FINALIZATION_COMPLETED,
                        message=(
                            f"cycle={index} release_id={cycle.release.release_id} "
                            f"mode={args.release_finalize} blocked="
                            f"{getattr(cycle, 'blocked_finalization', None) is not None}"
                            f"{cleanup_handoff_note}"
                        ),
                        artifacts=cycle_artifacts["finalization"] + cycle_artifacts["cleanup"],
                    )
            cycles_for_output.append(cycle_for_output)

            if index < len(result.cycles):
                next_cycle = result.cycles[index]
                next_manifest = getattr(next_cycle, "evidence_manifest", None)
                next_backlog_plan_path = getattr(next_manifest, "backlog_plan_path", None) if next_manifest is not None else None
                next_artifacts = _existing_paths([next_backlog_plan_path or getattr(next_cycle, "plan_path", None)])
                write_cycle_event(
                    cycle_index=index,
                    event_type=GovernorEventType.NEXT_EPIC_SELECTED,
                    message=f"cycle={index} next_selected_epic_id={next_cycle.selected_epic_id}",
                    artifacts=next_artifacts,
                    epic_id=next_cycle.selected_epic_id,
                    release_id=cycle_release_id,
                )
        stop_context = _governor_stop_context(result)
        governor_writer.write(
            event_type=GovernorEventType.STOP_REASON_RECORDED,
            message=(
                f"governor stop context category={stop_context.category.value} "
                f"reason={stop_context.reason}"
            ),
            artifacts=stop_context.evidence_artifact_paths,
            context=GovernorEventContext(
                phase=GovernorEventType.STOP_REASON_RECORDED.value,
                stop_reason=stop_context.reason,
                cycle_index=stop_context.cycle_index,
                epic_id=stop_context.epic_id,
                release_id=stop_context.release_id,
                artifact_count=len(stop_context.evidence_artifact_paths),
                details={"stop_category": stop_context.category.value},
            ),
        )
        governor_writer.write(
            event_type=GovernorEventType.GOVERNOR_COMPLETED,
            message=(
                f"attempted_epic_count={result.attempted_epic_count} "
                f"accepted_epic_count={result.accepted_epic_count} "
                f"requested_epic_count={result.requested_epic_count} stop_reason={result.stop_reason}"
            ),
            artifacts=[cycle.plan_path for cycle in result.cycles],
            context=GovernorEventContext(
                phase=GovernorEventType.GOVERNOR_COMPLETED.value,
                stop_reason=getattr(result.stop_reason, "value", str(result.stop_reason)),
                artifact_count=len(result.cycles),
                details={"stop_category": stop_context.category.value},
            ),
        )

        result_for_output = _with_updates(result, {"cycles": cycles_for_output})
        output = _backlog_multi_run_result(result_for_output)
        output["stop_context"] = stop_context.model_dump(mode="json")
        output["governor_log_path"] = str(governor_writer.paths.log_path)
        output["governor_events_path"] = str(governor_writer.paths.events_path)
        print(json.dumps(output, indent=2))
        return 0

    if args.command == "status":
        summaries = load_run_summaries(Path(args.runs_dir), limit=args.limit)
        if not summaries:
            print("No runs found.")
            return 0
        print(json.dumps([summary.__dict__ | {"bundle_path": str(summary.bundle_path)} for summary in summaries], indent=2))
        return 0

    if args.command == "cleanup":
        try:
            result = cleanup_release_artifacts(
                project_id=args.project,
                release_id=args.release,
                config_dir=Path(args.config_dir),
                force=args.force,
                include_integration_branch=args.include_integration_branch,
                runs_dir=Path(getattr(args, "runs_dir", "runs")),
            )
        except Exception as error:
            parser.exit(2, f"error: {error}\n")

        print(json.dumps(_cleanup_result(result), indent=2))
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
    output = {
        "release_id": result.release_id,
        "run_id": result.run_id,
        "summary_path": str(result.summary_path),
        "log_path": str(result.log_path),
        "review_path": str(result.review_path) if getattr(result, "review_path", None) else None,
        "metrics_path": str(result.metrics_path) if getattr(result, "metrics_path", None) else None,
        "budget_path": str(result.budget_path) if getattr(result, "budget_path", None) else None,
        "tuning_path": str(result.tuning_path) if getattr(result, "tuning_path", None) else None,
        "integration_branch": getattr(result, "integration_branch", None),
        "feature_review_path": str(result.feature_review_path) if getattr(result, "feature_review_path", None) else None,
        "feature_review_recheck_path": (
            str(result.feature_review_recheck_path) if getattr(result, "feature_review_recheck_path", None) else None
        ),
        "final_review_continuation_decision_path": (
            str(result.final_review_continuation_decision_path)
            if getattr(result, "final_review_continuation_decision_path", None)
            else None
        ),
        "final_integration_verification_path": (
            str(result.final_integration_verification_path)
            if getattr(result, "final_integration_verification_path", None)
            else None
        ),
        "feature_review_prompt_path": (
            str(result.feature_review_prompt_path) if getattr(result, "feature_review_prompt_path", None) else None
        ),
        "feature_review_stdout_path": (
            str(result.feature_review_stdout_path) if getattr(result, "feature_review_stdout_path", None) else None
        ),
        "feature_review_stderr_path": (
            str(result.feature_review_stderr_path) if getattr(result, "feature_review_stderr_path", None) else None
        ),
        "feature_review_metadata_path": (
            str(result.feature_review_metadata_path) if getattr(result, "feature_review_metadata_path", None) else None
        ),
        "feature_review_output_normalization_decision_path": (
            str(result.feature_review_output_normalization_decision_path)
            if getattr(result, "feature_review_output_normalization_decision_path", None)
            else None
        ),
        "feature_review_normalized_artifact_path": (
            str(result.feature_review_normalized_artifact_path)
            if getattr(result, "feature_review_normalized_artifact_path", None)
            else None
        ),
        "feature_review_proposals": [
            proposal.model_dump(mode="json") for proposal in getattr(result, "feature_review_proposals", [])
        ],
        "decision": result.decision,
        "tasks": [_task_run_result(task_result) for task_result in result.task_results],
    }
    if getattr(result, "finalization_gate", None) is not None:
        output["finalization_gate"] = result.finalization_gate
    return output


def _plan_release_result(result, *, inspect_proposed_contracts: bool) -> dict[str, object]:
    output: dict[str, object] = {
        "release_id": result.release_id,
        "plan_path": str(result.plan_path),
        "generated_contracts": len(result.plan.generated_contracts),
        "warnings": result.plan.warnings,
        "execution_strategy_selection_path": (
            str(result.execution_strategy_selection_path)
            if getattr(result, "execution_strategy_selection_path", None) is not None
            else None
        ),
        "supervisor_decision_path": (
            str(result.supervisor_decision_path) if getattr(result, "supervisor_decision_path", None) is not None else None
        ),
        "one_shot_execution_input_path": (
            str(result.one_shot_execution_input_path)
            if getattr(result, "one_shot_execution_input_path", None) is not None
            else None
        ),
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
        "execution_strategy_selection_path": (
            str(result.planning.execution_strategy_selection_path)
            if getattr(result.planning, "execution_strategy_selection_path", None) is not None
            else None
        ),
        "supervisor_decision_path": (
            str(result.planning.supervisor_decision_path)
            if getattr(result.planning, "supervisor_decision_path", None) is not None
            else None
        ),
        "one_shot_execution_input_path": (
            str(result.planning.one_shot_execution_input_path)
            if getattr(result.planning, "one_shot_execution_input_path", None) is not None
            else None
        ),
        "release": _release_run_result(result.release) if result.release is not None else None,
    }


def _backlog_plan_result(result) -> dict[str, object]:
    selected = next(
        (epic for epic in result.plan.epics if epic.epic_id == result.plan.selected_epic_id),
        None,
    )
    return {
        "plan_path": str(result.plan_path),
        "objective_path": str(result.objective_path) if result.objective_path else None,
        "selected_epic_id": result.plan.selected_epic_id,
        "selected_title": selected.title if selected else None,
        "epics": [
            {
                "epic_id": epic.epic_id,
                "priority": epic.priority,
                "title": epic.title,
                "suggested_release_id": epic.suggested_release_id,
            }
            for epic in result.plan.epics
        ],
        "warnings": result.plan.warnings,
    }


def _cleanup_result(result) -> dict[str, object]:
    return {
        "project_id": result.project_id,
        "release_id": result.release_id,
        "dry_run": result.dry_run,
        "worktree_paths": [str(path) for path in result.worktree_paths],
        "task_branches": result.task_branches,
        "integration_branch": result.integration_branch,
        "eligible_worktree_paths": [str(path) for path in getattr(result, "eligible_worktree_paths", [])],
        "skipped_worktree_paths": getattr(result, "skipped_worktree_paths", []),
        "eligible_branches": getattr(result, "eligible_branches", []),
        "skipped_branches": getattr(result, "skipped_branches", []),
        "finalization_evidence_path": (
            str(getattr(result, "finalization_evidence_path"))
            if getattr(result, "finalization_evidence_path", None) is not None
            else None
        ),
        "removed_worktrees": [str(path) for path in result.removed_worktrees],
        "deleted_branches": result.deleted_branches,
        "errors": result.errors,
    }


def _backlog_run_result(result) -> dict[str, object]:
    output: dict[str, object] = {}
    if getattr(result, "selected_epic_id", None) is not None:
        output["selected_epic_id"] = result.selected_epic_id
    if getattr(result, "objective_path", None) is not None:
        output["objective_path"] = str(result.objective_path)
    if getattr(result, "plan_path", None) is not None:
        output["plan_path"] = str(result.plan_path)
    if getattr(result, "execution_strategy_selection_path", None) is not None:
        output["execution_strategy_selection_path"] = str(result.execution_strategy_selection_path)
    if getattr(result, "supervisor_decision_path", None) is not None:
        output["supervisor_decision_path"] = str(result.supervisor_decision_path)
    if getattr(result, "one_shot_execution_input_path", None) is not None:
        output["one_shot_execution_input_path"] = str(result.one_shot_execution_input_path)
    if getattr(result, "release", None) is not None:
        output["release"] = _release_run_result(result.release)
    if getattr(result, "finalization_policy", None) is not None:
        output["finalization_policy"] = result.finalization_policy
    if getattr(result, "finalization_result", None) is not None:
        output["finalization_result"] = result.finalization_result
    if getattr(result, "cleanup_result", None) is not None:
        output["cleanup_result"] = result.cleanup_result
    if getattr(result, "blocked_finalization", None) is not None:
        output["blocked_finalization"] = result.blocked_finalization
    if getattr(result, "evidence_manifest", None) is not None:
        output["evidence_manifest"] = result.evidence_manifest.model_dump(mode="json")
    return output


def _backlog_multi_run_result(result) -> dict[str, object]:
    return {
        "project_id": result.project_id,
        "requested_epic_count": result.requested_epic_count,
        "attempted_epic_count": result.attempted_epic_count,
        "accepted_epic_count": result.accepted_epic_count,
        "stop_reason": result.stop_reason,
        "cycles": [_backlog_run_result(cycle) for cycle in result.cycles],
    }


def _governor_stop_context(result) -> GovernorStopContext:
    stop_reason = result.stop_reason
    category = governor_stop_category_for_reason(stop_reason)
    cycle_index: int | None = None
    epic_id: str | None = None
    release_id: str | None = None
    evidence_paths: list[Path] = []
    if result.cycles:
        cycle_index = len(result.cycles)
        last_cycle = result.cycles[-1]
        epic_id = getattr(last_cycle, "selected_epic_id", None)
        cycle_release = getattr(last_cycle, "release", None)
        release_id = (
            getattr(cycle_release, "release_id", None)
            if cycle_release is not None
            else getattr(last_cycle, "release_id", None)
        )
        evidence_paths = _cycle_stop_artifacts(last_cycle)
    return GovernorStopContext(
        reason=getattr(stop_reason, "value", str(stop_reason)),
        category=category,
        cycle_index=cycle_index,
        epic_id=epic_id,
        release_id=release_id,
        evidence_artifact_paths=evidence_paths,
    )


def _cycle_stop_artifacts(cycle) -> list[Path]:
    manifest = getattr(cycle, "evidence_manifest", None)
    if manifest is None:
        return _existing_paths([getattr(cycle, "plan_path", None), getattr(cycle, "objective_path", None)])
    return _existing_paths(
        [
            manifest.backlog_plan_path,
            manifest.contract_plan_path,
            manifest.release_summary_path,
            manifest.finalization_decision_path,
            manifest.final_review_continuation_decision_path,
            manifest.state_refresh_error_path,
            manifest.state_refresh_summary_path,
            manifest.feature_review_path,
            manifest.feature_review_recheck_path,
        ]
    )


def _governor_failure_stop_context(error: Exception) -> GovernorStopContext:
    reason = str(error).strip() or error.__class__.__name__
    normalized = reason.lower()
    category = GovernorStopCategory.UNKNOWN
    if "credential" in normalized or "auth" in normalized:
        category = GovernorStopCategory.MISSING_PLANNER_CREDENTIALS
    elif "hard policy" in normalized or "hard gate" in normalized or "forbidden" in normalized:
        category = GovernorStopCategory.HARD_POLICY_STOP
    return GovernorStopContext(
        reason=reason,
        category=category,
    )


def _codex_planner_backend(*, project_id: str | None, config_dir: Path, runs_dir: Path) -> CodexPlannerBackend:
    if project_id is None:
        raise ValueError("--execute-planner requires --project")
    config = load_project_config(project_id, config_dir, validate_repo=True)
    planner = config.model_roles.get("planner", config.executor)
    return CodexPlannerBackend(
        config=planner,
        repo_path=config.repo_path,
    )


def _codex_backlog_planner_backend(*, project_id: str, config_dir: Path) -> CodexBacklogPlannerBackend:
    config = load_project_config(project_id, config_dir, validate_repo=True)
    planner = config.model_roles.get("planner", config.executor)
    return CodexBacklogPlannerBackend(
        config=planner,
        repo_path=config.repo_path,
    )


def _print_progress(message: str) -> None:
    print(f"[agent-loop] {message}", file=sys.stderr, flush=True)


def _make_governor_run_id(*, project_id: str, now: datetime | None = None) -> str:
    timestamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}_{project_id}_governor"


def _release_artifact_links(release_result) -> list[Path]:
    artifacts: list[Path] = [release_result.summary_path, release_result.log_path]
    for attr in ("review_path", "metrics_path", "budget_path", "tuning_path"):
        value = getattr(release_result, attr, None)
        if value is not None:
            artifacts.append(value)
    return artifacts


def _cycle_artifact_graph(*, cycle, cleanup_path: Path | None) -> dict[str, list[Path]]:
    manifest = getattr(cycle, "evidence_manifest", None)
    planning: list[Path] = []
    objective: list[Path] = []
    release: list[Path] = []
    review: list[Path] = []
    decision: list[Path] = []
    finalization: list[Path] = []
    cleanup: list[Path] = []
    repo_state_proposal: list[Path] = []

    if manifest is not None:
        planning = _existing_paths(
            [
                manifest.backlog_plan_path,
                manifest.contract_plan_path,
                manifest.execution_strategy_selection_path,
                manifest.one_shot_execution_input_path,
                manifest.state_review_snapshot_path,
                manifest.state_refresh_summary_path,
                manifest.state_refresh_error_path,
            ]
        )
        objective = _existing_paths([manifest.generated_objective_path or cycle.objective_path])
        release = _existing_paths(
            [
                manifest.release_summary_path,
                manifest.release_log_path,
                manifest.release_metrics_path,
                manifest.release_budget_path,
                manifest.release_tuning_path,
            ]
        )
        review = _existing_paths(
            [
                manifest.release_review_path,
                manifest.feature_review_path,
                manifest.feature_review_recheck_path,
                *manifest.feature_review_proposal_paths,
            ]
        )
        decision = _existing_paths(
            [
                manifest.supervisor_decision_path,
                manifest.release_soft_gate_decision_path,
            ]
        )
        finalization = _existing_paths([manifest.finalization_summary_path or manifest.release_summary_path])
        repo_state_proposal = _existing_paths(
            [manifest.repo_state_proposal_plan_path, manifest.roadmap_proposal_plan_path]
        )
    else:
        planning = _existing_paths([getattr(cycle, "plan_path", None), getattr(cycle, "contract_plan_path", None)])
        objective = _existing_paths([getattr(cycle, "objective_path", None)])
        if getattr(cycle, "release", None) is not None:
            release = _existing_paths(_release_artifact_links(cycle.release))
            finalization = _existing_paths([getattr(cycle.release, "summary_path", None)])

    if cleanup_path is not None:
        cleanup = _existing_paths([cleanup_path])
    elif manifest is not None:
        cleanup = _existing_paths([manifest.cleanup_report_path])

    return {
        "planning": planning,
        "objective": objective,
        "release": release,
        "review": review,
        "decision": decision,
        "finalization": finalization,
        "cleanup": cleanup,
        "repo_state_proposal": repo_state_proposal,
    }


def _existing_paths(paths: list[Path | None]) -> list[Path]:
    result: list[Path] = []
    for path in paths:
        if path is None:
            continue
        if path.exists():
            result.append(path)
    return result


def _with_updates(value, updates: dict[str, object]):
    if is_dataclass(value):
        return replace(value, **updates)
    model_copy = getattr(value, "model_copy", None)
    if callable(model_copy):
        return model_copy(update=updates)
    if isinstance(value, SimpleNamespace):
        merged = vars(value) | updates
        return SimpleNamespace(**merged)
    raise TypeError(
        "_with_updates supports dataclasses, pydantic models (model_copy), and SimpleNamespace; "
        f"received {type(value).__name__}"
    )
