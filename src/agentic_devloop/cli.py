from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from agentic_devloop import __version__
from agentic_devloop.backlog import CodexBacklogPlannerBackend, plan_backlog
from agentic_devloop.cleanup import cleanup_release_artifacts
from agentic_devloop.config import ProjectConfigError, load_project_config
from agentic_devloop.doctor import run_doctor
from agentic_devloop.objective import run_objective
from agentic_devloop.orchestrator import run_task
from agentic_devloop.planning import plan_release_contracts
from agentic_devloop.planner_backend import CodexPlannerBackend
from agentic_devloop.governor_log import GovernorEventType, build_governor_event_log_writer
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
        live_cycle_events: set[str] = set()

        def governor_progress(message: str) -> None:
            _print_progress(message)
            if "event=governor_cycle_started" in message:
                governor_writer.write(
                    event_type=GovernorEventType.EPIC_SELECTED,
                    message=message,
                )
            elif "event=release_started" in message:
                governor_writer.write(
                    event_type=GovernorEventType.RELEASE_STARTED,
                    message=message,
                )
            elif (
                "event=repair_" in message
                or "event=task_resumed" in message
                or "event=conflict_repair_started" in message
            ):
                event_key = f"repair:{message}"
                if event_key not in live_cycle_events:
                    live_cycle_events.add(event_key)
                    governor_writer.write(
                        event_type=GovernorEventType.REPAIR_DECISION,
                        message=message,
                    )
            elif "event=governor_cycle_completed" in message:
                governor_writer.write(
                    event_type=GovernorEventType.RELEASE_COMPLETED,
                    message=message,
                )

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
            parser.exit(2, f"error: {error}\n")

        for index, cycle in enumerate(result.cycles, start=1):
            governor_writer.write(
                event_type=GovernorEventType.EPIC_SELECTED,
                message=f"cycle={index} selected_epic_id={cycle.selected_epic_id}",
                artifacts=[cycle.plan_path],
            )
            governor_writer.write(
                event_type=GovernorEventType.OBJECTIVE_READY,
                message=f"cycle={index} objective_path={cycle.objective_path}",
                artifacts=[cycle.objective_path],
            )
            if cycle.contract_plan_path is not None:
                governor_writer.write(
                    event_type=GovernorEventType.CONTRACT_PLAN_COMPLETED,
                    message=f"cycle={index} contract_plan_path={cycle.contract_plan_path}",
                    artifacts=[cycle.contract_plan_path],
                )
            if cycle.release is not None:
                governor_writer.write(
                    event_type=GovernorEventType.RELEASE_COMPLETED,
                    message=f"cycle={index} release_id={cycle.release.release_id} decision={cycle.release.decision}",
                    artifacts=_release_artifact_links(cycle.release),
                )
                if args.release_finalize != "none":
                    governor_writer.write(
                        event_type=GovernorEventType.FINALIZATION_COMPLETED,
                        message=f"cycle={index} release_id={cycle.release.release_id} mode={args.release_finalize}",
                        artifacts=[cycle.release.summary_path],
                    )
        governor_writer.write(
            event_type=GovernorEventType.GOVERNOR_COMPLETED,
            message=(
                f"attempted_epic_count={result.attempted_epic_count} "
                f"accepted_epic_count={result.accepted_epic_count} "
                f"requested_epic_count={result.requested_epic_count} stop_reason={result.stop_reason}"
            ),
            artifacts=[cycle.plan_path for cycle in result.cycles],
        )

        output = _backlog_multi_run_result(result)
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
