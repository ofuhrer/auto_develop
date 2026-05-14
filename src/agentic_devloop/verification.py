from __future__ import annotations

from pathlib import Path
import shlex

from agentic_devloop.models import CommandResult
from agentic_devloop.process import run_process


MAX_LOG_EXCERPT_CHARS = 4000
_VERIFICATION_ENV_VALUE_ALLOWLIST: set[str] = {"SHARED_RT"}
_WORKTREE_PYTHON = {".venv/bin/python", "./.venv/bin/python"}
_UNSAFE_SHELL_OPERATORS = {"|", "||", "&", "&&", ";", "<", "<<", ">", ">>"}
_ALLOWED_ENV_PREFIX_KEYS = {"PYTHONPATH"}


class VerificationRunner:
    def __init__(self, *, timeout_seconds: int = 600) -> None:
        self.timeout_seconds = timeout_seconds

    def run(
        self,
        *,
        commands: list[str],
        worktree_path: Path,
        output_dir: Path,
        runtime_python_path: str | None = None,
        runtime_env: dict[str, str] | None = None,
        stop_on_failure: bool = True,
    ) -> list[CommandResult]:
        output_dir.mkdir(parents=True, exist_ok=True)
        results: list[CommandResult] = []
        log_lines: list[str] = []

        for index, command in enumerate(commands, start=1):
            resolved_command = rewrite_worktree_local_verification_command(
                command,
                safe_runtime=runtime_python_path,
            )
            env_additions = runtime_env or {}
            result = run_process(
                resolved_command,
                cwd=worktree_path,
                timeout_seconds=self.timeout_seconds,
                shell=True,
                env_additions=env_additions,
            )
            stdout_path = output_dir / f"verification_{index}_stdout.log"
            stderr_path = output_dir / f"verification_{index}_stderr.log"
            stdout_path.write_text(result.stdout, encoding="utf-8")
            stderr_path.write_text(result.stderr, encoding="utf-8")
            failure_reason = _failure_reason(result.exit_code, result.timed_out)

            command_result = CommandResult(
                command=resolved_command,
                exit_code=result.exit_code,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                duration_seconds=result.duration_seconds,
                timed_out=result.timed_out,
            )
            results.append(command_result)
            log_lines.append(
                f"[{index}] {resolved_command}\n"
                f"original_command={command}\n"
                f"resolved_command={resolved_command}\n"
                f"cwd={worktree_path}\n"
                f"timeout_seconds={self.timeout_seconds}\n"
                f"env_additions={_render_env_additions(env_additions)}\n"
                f"exit_code={result.exit_code}\n"
                f"timed_out={result.timed_out}\n"
                f"failure_reason={failure_reason}\n"
                f"duration_seconds={result.duration_seconds:.3f}\n"
                f"stdout_path={stdout_path}\n"
                f"stderr_path={stderr_path}\n"
                f"stdout_excerpt:\n{_excerpt(result.stdout)}\n"
                f"stderr_excerpt:\n{_excerpt(result.stderr)}\n"
            )

            if stop_on_failure and result.exit_code != 0:
                break

        (output_dir / "verification.log").write_text("\n".join(log_lines), encoding="utf-8")
        return results


def _excerpt(text: str) -> str:
    if not text:
        return "<empty>"
    if len(text) <= MAX_LOG_EXCERPT_CHARS:
        return text.rstrip("\n")
    omitted = len(text) - MAX_LOG_EXCERPT_CHARS
    return text[:MAX_LOG_EXCERPT_CHARS].rstrip("\n") + f"\n... <truncated {omitted} chars>"


def bounded_verification_excerpt(text: str) -> str:
    return _excerpt(text)


def rewrite_worktree_local_verification_command(command: str, *, safe_runtime: str | None) -> str:
    if not safe_runtime:
        return command
    if ".venv/bin/python" not in command:
        return command
    if not is_safe_worktree_python_rewrite_command(command):
        return command
    tokens = shlex.split(command)
    rewritten = False
    updated_tokens: list[str] = []
    for token in tokens:
        if token in _WORKTREE_PYTHON:
            updated_tokens.append(safe_runtime)
            rewritten = True
            continue
        updated_tokens.append(token)
    if not rewritten:
        return command
    return shlex.join(updated_tokens)


def is_safe_worktree_python_rewrite_command(command: str) -> bool:
    if "$(" in command or "`" in command:
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens:
        return False
    if any(token in _UNSAFE_SHELL_OPERATORS for token in tokens):
        return False
    python_token_index = 0
    while python_token_index < len(tokens) and _is_allowed_env_assignment_token(tokens[python_token_index]):
        python_token_index += 1
    if python_token_index >= len(tokens):
        return False
    return tokens[python_token_index] in _WORKTREE_PYTHON


def _is_allowed_env_assignment_token(token: str) -> bool:
    if "=" not in token:
        return False
    key, value = token.split("=", 1)
    if not key or value == "":
        return False
    if key not in _ALLOWED_ENV_PREFIX_KEYS:
        return False
    return key.replace("_", "").isalnum() and key[0].isalpha()


def _render_env_additions(env_additions: dict[str, str]) -> str:
    if not env_additions:
        return "<none>"
    items = []
    for key, value in sorted(env_additions.items()):
        if key in _VERIFICATION_ENV_VALUE_ALLOWLIST:
            items.append(f"{key}={value}")
            continue
        items.append(f"{key}=<redacted>")
    return ", ".join(items)


def _failure_reason(exit_code: int, timed_out: bool) -> str:
    if timed_out:
        return "timeout"
    if exit_code != 0:
        return f"nonzero_exit_{exit_code}"
    return "<none>"


def apply_pythonpath_prefix_repair(
    *,
    runtime_env: dict[str, str] | None,
    pythonpath_prefix: str,
) -> dict[str, str]:
    """Apply a bounded PYTHONPATH repair from a policy-declared env-assignment token."""
    if not _is_allowed_env_assignment_token(pythonpath_prefix):
        raise ValueError("pythonpath_prefix must be a safe KEY=value token for an allowed env prefix")
    key, value = pythonpath_prefix.split("=", 1)
    updated = dict(runtime_env or {})
    updated[key] = value
    return updated


def verification_environment_capture_commands(*, safe_runtime: str | None, failed_command: str) -> list[str]:
    """Return bounded diagnostics commands safe to run under shell=True."""
    python = shlex.quote(safe_runtime) if safe_runtime else "python3"
    commands: list[str] = [
        f"{python} -V",
        f"{python} -c {shlex.quote('import sys; print(sys.executable); print(\"\\\\n\".join(sys.path))')}",
        f"{python} -m pip --version",
    ]
    failed = failed_command.strip()
    if failed:
        try:
            tokens = shlex.split(failed)
        except ValueError:
            tokens = []
        if tokens:
            candidate = tokens[0]
            if candidate and all(op not in candidate for op in _UNSAFE_SHELL_OPERATORS):
                commands.append(f"command -v {shlex.quote(candidate)}")
    return commands
