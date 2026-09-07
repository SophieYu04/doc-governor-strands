"""Repair staged documents in isolation, then publish only verified outputs.

The executor and verifier are trusted local commands, never model-provided shell.
No model output is executed as code and no repaired prose is self-certified.
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .engine import build_snapshot
from .repair import build_repair_prompt, repair_candidates
from .model_errors import model_error_code


class RepairBlocked(RuntimeError):
    pass


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["DOCGOV_REPAIR_ACTIVE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(root: Path, *args: str, data: bytes | None = None,
         env: dict[str, str] | None = None) -> bytes:
    return subprocess.run(["git", *args], cwd=root, env=env or _env(), input=data,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout


def _files(root: Path) -> dict[str, tuple[int, bytes]]:
    result = {}
    for p in root.rglob("*"):
        relative = p.relative_to(root)
        if relative.parts[0] == ".git":
            continue
        if p.is_symlink():
            raise RepairBlocked("symlink_output")
        if p.is_file():
            result[relative.as_posix()] = (p.stat().st_mode & 0o777, p.read_bytes())
    return result


def _index_entries(root: Path) -> dict[str, tuple[str, str]]:
    result = {}
    for row in _git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if not row:
            continue
        info, raw_path = row.split(b"\t", 1)
        mode, oid, stage = info.decode().split()
        path = raw_path.decode("utf-8")
        if stage != "0" or mode not in {"100644", "100755"}:
            raise RepairBlocked("unsupported_index_entry")
        if Path(path).is_absolute() or ".." in Path(path).parts or ".git" in Path(path).parts:
            raise RepairBlocked("unsafe_index_path")
        result[path] = mode, oid
    return result


def _working_signature(root: Path, paths: set[str]) -> dict[str, Any]:
    result = {}
    for name in sorted(paths):
        p = root / name
        if p.is_symlink() or any(parent.is_symlink() for parent in p.parents if parent != root.parent):
            raise RepairBlocked("symlink_input")
        result[name] = ((p.stat().st_mode & 0o777, hashlib.sha256(p.read_bytes()).hexdigest())
                        if p.exists() else None)
    return result


def _metadata(text: bytes) -> list[str]:
    # A prose repair must not refresh a status or verification date on its own.
    normalized = text.decode("utf-8").replace("*", "").replace("`", "")
    return re.findall(r"(?im)^\s*(?:狀態|最後驗證|最後更新|status|approval|last_verified_at|last verified)\s*[:：=].*$",
                      normalized)



def _run(command: str, root: Path, prompt: str | None, timeout: int) -> None:
    argv = shlex.split(command)
    if not argv:
        raise RepairBlocked("empty_command")
    try:
        completed = subprocess.run(argv, cwd=root, env=_env(), input=prompt,
                                   text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RepairBlocked("command_timeout") from exc
    if completed.returncode:
        # Child output may contain private source or prose; never echo it publicly.
        raise RepairBlocked("executor_failed" if prompt is not None else "verification_failed")


def repair_staged(root: Path, *, catalog: str = ".docgov/catalog.yaml",
                  enable_model: bool = False, model_id: str | None = None,
                  executor_command: str | None = None, verify_command: str | None = None,
                  planner_runner: Any = None, timeout: int = 900) -> dict[str, Any]:
    result: dict[str, Any] = dict(result="blocked", changed=False, model_requested=enable_model,
                                  model_used=False, model_trace=[], modified_paths=[],
                                  verification="not_run")
    root = root.resolve()
    try:
        if os.environ.get("DOCGOV_REPAIR_ACTIVE") == "1":
            raise RepairBlocked("recursive_repair")
        entries = _index_entries(root)
        head = _git(root, "rev-parse", "HEAD").strip().decode()
        tree = _git(root, "write-tree").strip().decode()
        staged = {x.decode() for x in _git(root, "diff", "--cached", "--name-only", "-z", "HEAD").split(b"\0") if x}
        result.update(source_head=head, staged_tree=tree)
        if not staged:
            result.update(result="pass", verification="not_required")
            return result
        if catalog not in entries:
            raise RepairBlocked("catalog_not_tracked")
        original_index = Path(_git(root, "rev-parse", "--path-format=absolute", "--git-path", "index").decode().strip())
        patch = _git(root, "diff", "--cached", "--binary", "--full-index", "HEAD")
        with tempfile.TemporaryDirectory(prefix="docgov-repair-") as directory:
            isolated = Path(directory) / "repo"
            isolated.mkdir()
            _git(isolated, "init", "-q")
            _git(isolated, "-c", "protocol.file.allow=always", "fetch", "--quiet", "--no-tags", str(root), head)
            _git(isolated, "checkout", "--quiet", "--detach", "FETCH_HEAD")
            _git(isolated, "apply", "--index", "--binary", data=patch)
            if _git(isolated, "write-tree").strip().decode() != tree:
                raise RepairBlocked("snapshot_mismatch")
            snapshot = build_snapshot(isolated, isolated / catalog)
            candidates = repair_candidates(snapshot)
            if not candidates:
                result.update(result="pass", verification="not_required")
                return result
            targets = {d.path for d in candidates}
            result["documents"] = sorted(targets)
            for target in targets:
                if target not in entries or (root / target).read_bytes() != (isolated / target).read_bytes():
                    raise RepairBlocked("target_has_unstaged_changes")
            inputs = set(entries) | staged
            signature = _working_signature(root, inputs)
            if not enable_model:
                raise RepairBlocked("model_required")
            verifier = verify_command or os.environ.get("DOCGOV_VERIFY_COMMAND")
            if not verifier:
                raise RepairBlocked("verification_command_required")
            trace: list[dict] = []
            try:
                prompt = build_repair_prompt(snapshot, enable_model=True, model_id=model_id,
                                             runner=planner_runner, trace=trace)
            except Exception as exc:
                result.update(model_used=any(t.get("event") == "agent_complete" for t in trace), model_trace=trace)
                raise RepairBlocked(model_error_code(exc)) from exc
            if not prompt or not any(t.get("event") == "agent_complete" for t in trace):
                raise RepairBlocked("model_completed_no_plan")
            result.update(model_used=True, model_trace=trace,
                          repair_plan_sha256=hashlib.sha256(prompt.encode()).hexdigest())
            before = _files(isolated)
            prompt += ("\nOnly modify these existing documents: " + ", ".join(sorted(targets)) +
                       ". Do not run git commit, stage files, change governance controls, or refresh verification metadata.\n")
            executor = executor_command or os.environ.get("DOCGOV_REPAIR_COMMAND") or "codex exec --approve-for-me -C . -"
            _run(executor, isolated, prompt, timeout)
            after = _files(isolated)
            changes = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
            if changes - targets or any(p not in before or p not in after for p in changes):
                raise RepairBlocked("executor_modified_outside_targets")
            if _git(isolated, "rev-parse", "HEAD").strip().decode() != head or _git(isolated, "write-tree").strip().decode() != tree:
                raise RepairBlocked("executor_modified_git_state")
            for path in changes:
                if before[path][0] != after[path][0] or _metadata(before[path][1]) != _metadata(after[path][1]):
                    raise RepairBlocked("verification_metadata_modified")
            _run(verifier, isolated, None, timeout)
            if _git(isolated, "rev-parse", "HEAD").strip().decode() != head or _git(isolated, "write-tree").strip().decode() != tree:
                raise RepairBlocked("verification_modified_git_state")
            verified = _files(isolated)
            # Verification may create ignored build outputs, but cannot alter inputs or repairs.
            if any(verified.get(p) != after[p] for p in after):
                raise RepairBlocked("verification_modified_files")
            extras = set(verified) - set(after)
            for path in extras:
                if subprocess.run(["git", "check-ignore", "--quiet", "--", path], cwd=isolated, env=_env()).returncode:
                    raise RepairBlocked("verification_created_untracked_file")
            result["verification"] = "passed"
            result["document_hashes"] = {
                path: {"before_sha256": hashlib.sha256(before[path][1]).hexdigest(),
                       "after_sha256": hashlib.sha256(after[path][1]).hexdigest()}
                for path in sorted(changes)
            }
            if (_git(root, "rev-parse", "HEAD").strip().decode() != head or
                _git(root, "write-tree").strip().decode() != tree or
                _working_signature(root, inputs) != signature):
                raise RepairBlocked("source_changed_during_repair")
            if not changes:
                result.update(result="pass")
                return result
            lock = original_index.with_name(original_index.name + ".lock")
            lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(lock_fd)
            temp_index = Path(directory) / "index"
            written: list[str] = []
            try:
                # Holding the real index lock prevents another Git writer during publication.
                shutil.copyfile(original_index, temp_index)
                env = _env() | {"GIT_INDEX_FILE": str(temp_index)}
                if (_git(root, "rev-parse", "HEAD").strip().decode() != head or
                    _git(root, "write-tree", env=env).strip().decode() != tree or
                    _working_signature(root, inputs) != signature):
                    raise RepairBlocked("source_changed_during_repair")
                for path in sorted(changes):
                    blob = _git(root, "hash-object", "-w", "--stdin", data=after[path][1]).strip().decode()
                    _git(root, "update-index", "--add", "--cacheinfo", entries[path][0], blob, path, env=env)
                for path in sorted(changes):
                    (root / path).write_bytes(after[path][1])
                    written.append(path)
                shutil.copyfile(temp_index, lock)
                os.replace(lock, original_index)
            except Exception:
                for path in written:
                    if (root / path).read_bytes() == after[path][1]:
                        (root / path).write_bytes(before[path][1])
                raise
            finally:
                lock.unlink(missing_ok=True)
            result.update(result="changed", changed=True, modified_paths=sorted(changes))
            return result
    except RepairBlocked as exc:
        result["error_code"] = str(exc)
    except FileExistsError:
        result["error_code"] = "index_locked"
    except (OSError, ValueError, subprocess.SubprocessError):
        result["error_code"] = "repair_execution_failed"
    return result
