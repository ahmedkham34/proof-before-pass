"""Shared helpers: the neutral git identity, the process-group runner, path checks and signal handling."""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import tempfile

# Every git commit the tools, the demo and the tests make uses this identity and never reads the user's git config,
# so a clean machine with no git identity works and nothing personal ends up in a commit.
GIT_ENV = {
    "GIT_AUTHOR_NAME": "pbp", "GIT_AUTHOR_EMAIL": "pbp@example.invalid",
    "GIT_COMMITTER_NAME": "pbp", "GIT_COMMITTER_EMAIL": "pbp@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
}
_GIT_LOCATION_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                      "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CEILING_DIRECTORIES")


def git_env(base: dict | None = None) -> dict:
    """A copy of BASE (default os.environ) with the neutral identity and no inherited repository location."""
    env = dict(os.environ if base is None else base)
    for k in _GIT_LOCATION_VARS:
        env.pop(k, None)
    env.update(GIT_ENV)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def kill_group(proc: subprocess.Popen) -> None:
    """Kill PROC's whole process group (it was started in its own session) and reap PROC."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def run_cmd(cmd, cwd, timeout, log_path=None, env=None):
    """Run CMD in its own process group with stdin from /dev/null and stdout+stderr to LOG_PATH.

    Returns (returncode, output bytes); returncode is None when TIMEOUT ran out. The whole group is killed
    afterwards in every case (timeout, an exception or a signal in the caller, or a normal end), so no grandchild
    the command started is left running. PYTHONDONTWRITEBYTECODE is set and PYTEST_ADDOPTS is removed.
    """
    env = dict(os.environ if env is None else env)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTEST_ADDOPTS", None)
    own_log = log_path is None
    if own_log:
        fd, log_path = tempfile.mkstemp(prefix="pbp-run-", suffix=".log")
        os.close(fd)
    try:
        with open(log_path, "wb") as log, open(os.devnull, "rb") as devnull:
            proc = subprocess.Popen(cmd, cwd=cwd, stdin=devnull, stdout=log, stderr=subprocess.STDOUT,
                                    env=env, start_new_session=True)
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                rc = None
            except BaseException:
                kill_group(proc)
                raise
            kill_group(proc)
        with open(log_path, "rb") as fh:
            out = fh.read()
    finally:
        if own_log:
            try:
                os.unlink(log_path)
            except OSError:
                pass
    return rc, out


def is_within(child, parent) -> bool:
    """True when CHILD's real path is PARENT's real path or below it (symlinks resolved)."""
    child = os.path.realpath(child)
    parent = os.path.realpath(parent)
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def safe_rel(path: str) -> str:
    """PATH normalised; ValueError if it is absolute or climbs out with `..`."""
    norm = os.path.normpath(path)
    if not path or os.path.isabs(norm) or norm == ".." or norm.startswith(".." + os.sep):
        raise ValueError(f"path must be relative and stay inside the folder: {path}")
    return norm


def shown_path(path) -> str:
    """PATH relative to the current folder when it lies inside it, else as given."""
    path = os.path.abspath(path)
    cwd = os.getcwd()
    return os.path.relpath(path, cwd) if is_within(path, cwd) else path


def positive_seconds(v: str) -> float:
    """argparse type for --timeout: a finite number of seconds above 0."""
    f = float(v)
    if not (f > 0) or f == float("inf"):
        raise argparse.ArgumentTypeError("timeout must be a positive number")
    return f


def _on_signal(signum, _frame):
    raise SystemExit(128 + signum)


def exit_on_signals() -> None:
    """Turn SIGTERM and SIGHUP into SystemExit(128+N), so `finally` blocks kill children and remove temp folders."""
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _on_signal)
