from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Optional, Tuple


def run_git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout


def current_sha(root: Path) -> str:
    try:
        return run_git(root, "rev-parse", "HEAD").strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def current_branch(root: Path) -> Optional[str]:
    try:
        value = run_git(root, "branch", "--show-current").strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return value or None


def tracked_paths(root: Path, suffix: Optional[str] = None, ref: Optional[str] = None) -> List[str]:
    try:
        if ref:
            output = run_git(root, "ls-tree", "-r", "--name-only", ref)
        else:
            output = run_git(root, "ls-files", "--cached")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    paths = [line for line in output.splitlines() if line]
    if suffix:
        paths = [path for path in paths if path.endswith(suffix)]
    return sorted(set(paths))


def untracked_paths(root: Path) -> List[str]:
    try:
        output = run_git(root, "ls-files", "--others", "--exclude-standard")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return sorted({line for line in output.splitlines() if line})


def bytes_at_ref(root: Path, ref: str, relative_path: str) -> Optional[bytes]:
    try:
        result = subprocess.run(
            ["git", "show", f"{ref}:{relative_path}"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return result.stdout


def content_at_ref(root: Path, ref: str, relative_path: str) -> Optional[str]:
    content = bytes_at_ref(root, ref, relative_path)
    if content is None:
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.hex()


def changed_paths(root: Path, base: str | None, head: str | None) -> Tuple[List[str], List[str]]:
    if not base or not head:
        try:
            output = run_git(root, "status", "--porcelain=v1", "--untracked-files=all")
        except (subprocess.CalledProcessError, FileNotFoundError):
            return [], []
        changed: List[str] = []
        added: List[str] = []
        for line in output.splitlines():
            if len(line) < 4:
                continue
            status = line[:2]
            path = line[3:]
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            changed.append(path)
            if status == "??" or "A" in status:
                added.append(path)
        return sorted(set(changed)), sorted(set(added))
    output = run_git(root, "diff", "--name-status", base, head)
    changed: List[str] = []
    added: List[str] = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status, path = parts[0], parts[-1]
        changed.append(path)
        if status.startswith("A"):
            added.append(path)
    return changed, added


def deleted_paths(root: Path, base: str | None, head: str | None) -> List[str]:
    if not base or not head:
        return []
    output = run_git(root, "diff", "--name-status", base, head)
    return [
        parts[-1]
        for line in output.splitlines()
        if len(parts := line.split("\t")) >= 2 and parts[0].startswith("D")
    ]


def changed_content(root: Path, base: str | None, head: str | None) -> str:
    if not base or not head:
        return ""
    return run_git(root, "diff", "--unified=0", base, head)
