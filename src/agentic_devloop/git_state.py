from __future__ import annotations

from pathlib import Path

from agentic_devloop.process import run_process


def git_text(repo_path: Path, args: list[str]) -> str:
    result = run_process(["git", *args], cwd=repo_path, timeout_seconds=60)
    if result.exit_code != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def changed_files(repo_path: Path) -> list[str]:
    status = git_text(repo_path, ["status", "--porcelain", "--untracked-files=all"])
    files: list[str] = []
    for line in status.splitlines():
        if not line:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", maxsplit=1)[1]
        files.append(path)
    return files


def diff_patch(repo_path: Path) -> str:
    diff = git_text(repo_path, ["diff", "--patch"])
    untracked_patches = []
    for path in changed_files(repo_path):
        if (repo_path / path).is_file() and _is_untracked(repo_path, path):
            untracked_patches.append(_untracked_file_patch(repo_path, path))

    if untracked_patches:
        return "\n".join([diff, *untracked_patches]).lstrip()
    return diff


def _is_untracked(repo_path: Path, path: str) -> bool:
    status = git_text(repo_path, ["status", "--porcelain", "--", path])
    return any(line.startswith("?? ") for line in status.splitlines())


def _untracked_file_patch(repo_path: Path, path: str) -> str:
    content = (repo_path / path).read_text(encoding="utf-8", errors="replace")
    lines = content.splitlines()
    patch_lines = [
        f"diff --git a/{path} b/{path}",
        "new file mode 100644",
        "index 0000000..0000000",
        "--- /dev/null",
        f"+++ b/{path}",
        f"@@ -0,0 +1,{len(lines)} @@",
    ]
    patch_lines.extend(f"+{line}" for line in lines)
    return "\n".join(patch_lines) + "\n"
