#!/usr/bin/env python3
"""heldout - sealed held-out check runner.

What it checks: that a candidate commit passes tests its builder never saw, and that those tests were not edited
after sealing. `seal` copies a folder of tests into a read-only store with a sha256 manifest and prints the seal id
(the manifest's sha256); keep the seal id somewhere the builder cannot write. `verify` checks the store still
matches. `check` (which requires the seal id) re-verifies, extracts COMMIT from TREE with `git archive` (never the
working tree), copies the verified tests OUTSIDE the extracted tree, runs pytest on them under a timeout, and passes
only when all of these agree: pytest exit 0, a clean final summary line, and a junit-xml result file - written to a
randomly named path outside both the tree and the run folder - in which every test id found in the sealed sources
is present and passed. The checks find the extracted tree through the HELDOUT_TREE environment variable.

This is NOT a sandbox, and the result can be forged. The candidate's code runs inside the pytest process with
your user rights, so any code running in the test process can forge the result: for example by finding the junit
file and rewriting it at exit, by patching pytest, or by printing a summary and exiting 0 after the file is
written. The junit cross-check only stops the crudest forgery (a fake summary line and an early exit). The seal
protects against the tests being read or tuned to by the author; it does not protect against a hostile candidate.
Run untrusted candidates in a throwaway container or VM. The read-only store only stops mistakes by code that plays
fair: the same user can chmod it back.

Commands (exit 0 ok; 1 checks failed; 2 usage error, unreadable input, unknown commit, a TREE that `git archive`
cannot extract, or an unexpected error; 4 store does not match its seal; 5 refused: a malformed --seal id (verify
and check), or candidate content that would put the injected file outside the extracted tree; 128+N killed by
signal N):

  python -m pbp heldout seal SRC STORE
  python -m pbp heldout verify STORE [--seal SEAL_ID]
  python -m pbp heldout check STORE TREE COMMIT OUT --seal SEAL_ID [--inject FILE [--inject-as REL]]
                              [--timeout 900]

  --inject FILE   copy one extra file (for example a helper script the checks call) into the extracted tree
  --inject-as REL where to put it, relative to the tree root (default: the file's base name)

Python 3.11+ standard library only (pytest and git are run as programs).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from pbp._common import exit_on_signals, git_env, is_within, positive_seconds, run_cmd

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_MISMATCH = 4
EXIT_REFUSED = 5

MANIFEST = "MANIFEST.sha256"
SKIP_DIRS = {"__pycache__", ".pytest_cache"}
VERIFY_IGNORED_DIRS = {"__pycache__"}
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class Refused(Exception):
    """A malformed seal id or refused candidate content (exit 5)."""


class Usage(Exception):
    """A usage error, unreadable input or an unknown commit (exit 2)."""


# ---------------------------------------------------------------- helpers

def _err(msg: str) -> None:
    print(f"pbp heldout: {msg}", file=sys.stderr)


def sha256_file(path: str | os.PathLike) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _rmtree_force(path: str) -> None:
    """Remove a tree even if parts of it were made read-only."""
    def onerror(func, p, _exc):
        try:
            parent = os.path.dirname(p)
            os.chmod(parent, stat.S_IRWXU)
            if os.path.isdir(p) and not os.path.islink(p):
                os.chmod(p, stat.S_IRWXU)
            func(p)
        except OSError:
            pass
    if os.path.lexists(path):
        if sys.version_info >= (3, 12):   # onerror is deprecated from 3.12; onexc has the same retry role
            shutil.rmtree(path, onexc=onerror)
        else:
            shutil.rmtree(path, onerror=onerror)


def _atomic_write(directory: str, name: str, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, os.path.join(directory, name))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- seal

def _collect_src(src: str) -> list[str]:
    """Return sorted posix relative paths of regular files to copy; refuse odd entries."""
    files: list[str] = []
    for root, dirs, names in os.walk(src, followlinks=False):
        keep = []
        for d in dirs:
            full = os.path.join(root, d)
            if os.path.islink(full):
                raise Usage(f"symlink in source: {full}")
            if d in SKIP_DIRS:
                continue
            keep.append(d)
        dirs[:] = keep
        for n in names:
            full = os.path.join(root, n)
            st = os.lstat(full)
            if stat.S_ISLNK(st.st_mode):
                raise Usage(f"symlink in source: {full}")
            if not stat.S_ISREG(st.st_mode):
                raise Usage(f"special file in source: {full}")
            if n.endswith(".pyc"):
                continue
            rel = Path(os.path.relpath(full, src)).as_posix()
            if "\n" in rel or "\r" in rel:
                raise Usage(f"file name with newline refused: {full!r}")
            if rel == MANIFEST:
                raise Usage(f"source already contains {MANIFEST}")
            files.append(rel)
    files.sort()
    return files


def cmd_seal(src: str, store: str) -> int:
    if os.path.islink(src) or not os.path.isdir(src):
        raise Usage(f"SRC is not a directory: {src}")
    if os.path.lexists(store):
        raise Usage(f"STORE already exists: {store}")
    if is_within(store, src):
        raise Usage("STORE must not be inside SRC")
    files = _collect_src(src)
    lines = []
    try:
        os.makedirs(store)
        for rel in files:
            s = os.path.join(src, *rel.split("/"))
            d = os.path.join(store, *rel.split("/"))
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copyfile(s, d, follow_symlinks=False)
            lines.append(f"{sha256_file(d)}  {rel}\n")
        mpath = os.path.join(store, MANIFEST)
        with open(mpath, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("".join(lines))
        seal_id = sha256_file(mpath)
        # read-only: files 0444, dirs 0555 (dirs last, deepest first)
        for root, dirs, names in os.walk(store, topdown=False):
            for n in names:
                os.chmod(os.path.join(root, n), 0o444)
            for d in dirs:
                os.chmod(os.path.join(root, d), 0o555)
        os.chmod(store, 0o555)
    except BaseException:
        _rmtree_force(store)
        raise
    print(seal_id)
    return EXIT_OK


# ---------------------------------------------------------------- verify

def _read_manifest(store: str) -> tuple[dict[str, str] | None, str | None, list[str]]:
    mpath = os.path.join(store, MANIFEST)
    if os.path.islink(mpath) or not os.path.isfile(mpath):
        return None, None, [f"missing {MANIFEST}"]
    seal_id = sha256_file(mpath)
    entries: dict[str, str] = {}
    problems: list[str] = []
    with open(mpath, "rb") as fh:
        raw = fh.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, seal_id, [f"changed {MANIFEST}"]
    for line in text.split("\n"):
        if line == "":
            continue
        parts = line.split("  ", 1)
        if len(parts) != 2 or not _HEX64.match(parts[0]) or not parts[1] or parts[1] in entries:
            return None, seal_id, [f"changed {MANIFEST}"]
        rel = parts[1]
        if rel.startswith("/") or ".." in rel.split("/") or rel == MANIFEST:
            return None, seal_id, [f"changed {MANIFEST}"]
        entries[rel] = parts[0]
    return entries, seal_id, problems


def _scan_store(store: str) -> dict[str, str]:
    """Return {rel: kind} where kind is 'file' or 'bad' (symlink/special) for everything in STORE."""
    found: dict[str, str] = {}
    for root, dirs, names in os.walk(store, followlinks=False):
        keep = []
        for d in dirs:
            full = os.path.join(root, d)
            rel = Path(os.path.relpath(full, store)).as_posix()
            if os.path.islink(full):
                found[rel] = "bad"
                continue
            if d in VERIFY_IGNORED_DIRS:
                continue
            keep.append(d)
        dirs[:] = keep
        for n in names:
            full = os.path.join(root, n)
            rel = Path(os.path.relpath(full, store)).as_posix()
            if rel == MANIFEST:
                continue
            st = os.lstat(full)
            found[rel] = "file" if stat.S_ISREG(st.st_mode) else "bad"
    return found


def verify_store(store: str, seal: str | None) -> tuple[list[str], str | None]:
    """Return (differences, manifest sha256). Empty differences means the store matches."""
    if os.path.islink(store) or not os.path.isdir(store):
        return [f"missing {store}"], None
    entries, seal_id, problems = _read_manifest(store)
    if entries is None:
        diffs = list(problems)
        if seal is not None and seal_id != seal:
            diffs.append(f"seal {seal_id}")
        return diffs, seal_id
    found = _scan_store(store)
    diffs: list[str] = []
    for rel in sorted(set(found) | set(entries)):
        if rel not in entries:
            diffs.append(f"extra {rel}")
        elif rel not in found:
            diffs.append(f"missing {rel}")
        elif found[rel] != "file":
            diffs.append(f"changed {rel}")
        else:
            try:
                actual = sha256_file(os.path.join(store, *rel.split("/")))
            except OSError:
                actual = None
            if actual != entries[rel]:
                diffs.append(f"changed {rel}")
    if seal is not None and seal_id != seal:
        diffs.append(f"seal {seal_id}")
    return diffs, seal_id


def _need_store(store: str) -> None:
    if os.path.islink(store) or not os.path.isdir(store):
        raise Usage(f"STORE is not a directory: {store}")


def cmd_verify(store: str, seal: str | None) -> int:
    if seal is not None and not _HEX64.match(seal):
        raise Refused(f"--seal must be the 64-hex seal id printed by `seal`: {seal!r}")
    _need_store(store)
    diffs, seal_id = verify_store(store, seal)
    if diffs:
        for d in diffs:
            print(d)
        return EXIT_MISMATCH
    print(seal_id)
    return EXIT_OK


# ---------------------------------------------------------------- check

_SUMMARY_LINE = re.compile(r"^=*\s*(?P<body>.*?)\s+in\s+[0-9.]+s(?:\s*\([^)]*\))?\s*=*$")
_COUNT = re.compile(r"(\d+)\s+([a-z]+)")
_BAD_KEYS = {"failed", "error", "errors", "xpassed"}


def summary_ok(output: str) -> bool:
    """True only if the final summary line has N passed (N >= 1) and no failed/error(s)/xpassed count."""
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    if not lines:
        return False
    m = _SUMMARY_LINE.match(lines[-1])
    if not m:
        return False
    counts: dict[str, int] = {}
    for num, key in _COUNT.findall(m.group("body")):
        counts[key] = counts.get(key, 0) + int(num)
    if counts.get("passed", 0) < 1:
        return False
    return not any(k in counts for k in _BAD_KEYS)


_TEST_FILE = re.compile(r"^(test_.*|.*_test)\.py$")   # pytest's default python_files


def expected_ids(checks: str) -> list[str]:
    """Test ids ("rel/file.py::[Class::]test_name") pytest would collect from the sealed sources, read with ast."""
    import ast
    ids: list[str] = []
    for root, dirs, names in os.walk(checks):
        dirs[:] = sorted(d for d in dirs if d not in VERIFY_IGNORED_DIRS)
        for n in names:
            if not _TEST_FILE.match(n):
                continue
            rel = Path(os.path.relpath(os.path.join(root, n), checks)).as_posix()
            with open(os.path.join(root, n), "rb") as fh:
                try:
                    tree = ast.parse(fh.read())
                except SyntaxError:
                    ids.append(f"{rel}::<syntax error>")   # never matches: the check fails
                    continue
            funcs = (ast.FunctionDef, ast.AsyncFunctionDef)
            for node in tree.body:
                if isinstance(node, funcs) and node.name.startswith("test"):
                    ids.append(f"{rel}::{node.name}")
                elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                    ids.extend(f"{rel}::{node.name}::{m.name}" for m in node.body
                               if isinstance(m, funcs) and m.name.startswith("test"))
    return sorted(ids)


def junit_problems(junit: str, expected: list[str]) -> list[str]:
    """Empty list only if JUNIT exists, every EXPECTED id is in it and passed, and no test case failed."""
    if not expected:
        return ["no test ids found in the sealed sources"]
    try:
        cases = list(ET.parse(junit).getroot().iter("testcase"))
    except (OSError, ET.ParseError) as exc:
        return [f"no junit result ({type(exc).__name__}): pytest did not finish its run"]
    seen: dict[tuple[str, str], list[str]] = {}
    problems = []
    for tc in cases:
        tags = {c.tag for c in tc}
        state = ("failed" if "failure" in tags else "error" if "error" in tags
                 else "skipped" if "skipped" in tags else "passed")
        key = (tc.get("classname") or "", (tc.get("name") or "").split("[", 1)[0])
        seen.setdefault(key, []).append(state)
        if state in ("failed", "error"):
            problems.append(f"{state}: {key[0]}::{tc.get('name')}")
    for tid in expected:
        rel, *rest = tid.split("::")
        mod = rel[:-3].replace("/", ".")
        key = (".".join([mod, *rest[:-1]]), rest[-1])
        states = seen.get(key)
        if not states:
            problems.append(f"missing: {tid}")
        elif any(st != "passed" for st in states):
            problems.append(f"not passed: {tid} ({','.join(sorted(set(states) - {'passed'}))})")
    return problems


def _copy_verified(store: str, dest: str) -> None:
    """Copy STORE to DEST (files 0444, dirs 0755); __pycache__ and odd entries are never copied."""
    os.makedirs(dest)
    for root, dirs, names in os.walk(store, followlinks=False):
        dirs[:] = [d for d in dirs
                   if d not in VERIFY_IGNORED_DIRS and not os.path.islink(os.path.join(root, d))]
        rel_root = os.path.relpath(root, store)
        out_root = dest if rel_root == "." else os.path.join(dest, rel_root)
        os.makedirs(out_root, exist_ok=True)
        os.chmod(out_root, 0o755)
        for n in names:
            s = os.path.join(root, n)
            if not stat.S_ISREG(os.lstat(s).st_mode):
                continue
            d = os.path.join(out_root, n)
            shutil.copyfile(s, d, follow_symlinks=False)
            os.chmod(d, 0o444)


def _run_pytest(tmp: str, timeout: float, junit: str) -> tuple[int | None, bytes, bool]:
    checks = os.path.join(tmp, "checks")
    cmd = [sys.executable, "-m", "pytest", checks, "-q", "-p", "no:cacheprovider", f"--junitxml={junit}",
           f"--basetemp={os.path.join(tmp, 'bt')}", f"--confcutdir={checks}", f"--rootdir={checks}"]
    env = dict(os.environ)
    env.pop("PYTEST_PLUGINS", None)   # no outside injection of pytest plugins (run_cmd drops PYTEST_ADDOPTS)
    env["HELDOUT_TREE"] = os.path.join(tmp, "tree")
    rc, data = run_cmd(cmd, tmp, timeout, os.path.join(tmp, "pytest.log"), env=env)
    timed_out = rc is None
    if timed_out:
        data += f"\n[heldout] TIMEOUT after {timeout:g}s: process group killed\n".encode()
    return rc, data, timed_out


def _inject_rel(inject: str, inject_as: str | None) -> str:
    rel = inject_as if inject_as is not None else os.path.basename(inject)
    norm = os.path.normpath(rel)
    if not rel or os.path.isabs(norm) or norm == ".." or norm.startswith(".." + os.sep) or norm == ".":
        raise Usage(f"--inject-as must be a relative path inside the tree: {rel!r}")
    return norm


def cmd_check(store: str, tree: str, commit: str, out: str, seal: str,
              inject: str | None, timeout: float, inject_as: str | None = None) -> int:
    base = tempfile.gettempdir()
    if not isinstance(seal, str) or not _HEX64.match(seal):
        raise Refused(f"--seal must be the 64-hex seal id printed by `seal`: {seal!r}")
    _need_store(store)
    if os.path.lexists(out) and not os.path.isdir(out):
        raise Usage(f"OUT is not a directory: {out}")
    if os.path.isdir(tree) and is_within(out, tree):
        raise Usage("OUT must not be inside TREE (TREE is never modified)")
    os.makedirs(out, exist_ok=True)
    # Before step 1: a stale pass must never survive.
    try:
        os.unlink(os.path.join(out, "heldout.rc"))
    except FileNotFoundError:
        pass

    # 1. verify
    diffs, seal_id = verify_store(store, seal)
    if diffs:
        for d in diffs:
            print(d)
        return EXIT_MISMATCH

    # 2. resolve commit
    if not commit or commit.startswith("-") or not os.path.isdir(tree):
        raise Usage(f"bad COMMIT or TREE: {commit!r} {tree!r}")
    if inject is not None and (os.path.islink(inject) or not os.path.isfile(inject)):
        raise Usage(f"inject is not a regular file: {inject}")
    inject_rel = _inject_rel(inject, inject_as) if inject is not None else None
    genv = git_env()
    r = subprocess.run(["git", "-C", tree, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"],
                       env=genv, stdin=subprocess.DEVNULL, capture_output=True)
    sha = r.stdout.decode().strip()
    if r.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        raise Usage(f"COMMIT does not resolve: {commit}")

    tmp = tempfile.mkdtemp(prefix="heldout.", dir=base)
    jdir = tempfile.mkdtemp(prefix=f"hj{secrets.token_hex(8)}.", dir=base)   # result channel, outside tmp and tree
    try:
        # 3. extract the commit (never the working tree or index)
        tree_dir = os.path.join(tmp, "tree")
        os.makedirs(tree_dir)
        arch = subprocess.Popen(["git", "-C", tree, "archive", sha], env=genv,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
        tar = subprocess.run(["tar", "-x", "-C", tree_dir], stdin=arch.stdout,
                             capture_output=True)
        arch.stdout.close()
        arc_rc = arch.wait()
        if arc_rc != 0 or tar.returncode != 0:
            raise Usage(f"cannot extract COMMIT from TREE: git archive rc={arc_rc} tar rc={tar.returncode} "
                        f"{tar.stderr.decode(errors='replace').strip()[-200:]}")
        if inject is not None:
            ldest = os.path.join(tree_dir, inject_rel)
            # the commit may hold a symlinked folder on the way: never follow it out of the extracted tree
            if not is_within(os.path.dirname(ldest), tree_dir):
                raise Refused(f"--inject-as {inject_rel} leads outside the extracted tree (a symlink in the commit)")
            os.makedirs(os.path.dirname(ldest), exist_ok=True)
            if os.path.lexists(ldest):
                os.unlink(ldest)
            shutil.copyfile(inject, ldest)
            os.chmod(ldest, 0o700)

        # 4. copy store outside the tree, then re-verify the copy against the same seal
        checks = os.path.join(tmp, "checks")
        _copy_verified(store, checks)
        cdiffs, _ = verify_store(checks, seal_id)
        if cdiffs:
            for d in cdiffs:
                print(d)
            return EXIT_MISMATCH

        # 5. run
        junit = os.path.join(jdir, secrets.token_hex(16) + ".xml")
        expected = expected_ids(checks)
        rc, log, timed_out = _run_pytest(tmp, timeout, junit)
        problems = [] if timed_out else junit_problems(junit, expected)
        for line in problems:
            log += f"\n[heldout] junit check: {line}".encode()
        if problems:
            log += b"\n"
        passed = (not timed_out) and rc == 0 and summary_ok(log.decode("utf-8", errors="replace")) and not problems
        result = EXIT_OK if passed else EXIT_FAIL

        # 6. results, rc last
        _atomic_write(out, "heldout.log", log)
        _atomic_write(out, "heldout.commit", (sha + "\n").encode())
        _atomic_write(out, "heldout.seal", (seal_id + "\n").encode())
        _atomic_write(out, "heldout.rc", f"{result}\n".encode())
        return result
    except (Refused, Usage):
        raise
    except Exception as exc:  # crashed run: an error of the tool (exit 2), recorded, never a pass
        try:
            _atomic_write(out, "heldout.log", f"[heldout] crashed: {exc!r}\n".encode())
            _atomic_write(out, "heldout.commit", (sha + "\n").encode())
            _atomic_write(out, "heldout.seal", (str(seal_id) + "\n").encode())
            _atomic_write(out, "heldout.rc", f"{EXIT_USAGE}\n".encode())
        except OSError:
            pass
        _err(f"crashed: {exc!r}")
        return EXIT_USAGE
    finally:
        # 7. always remove TMP and the result channel
        _rmtree_force(tmp)
        _rmtree_force(jdir)


# ---------------------------------------------------------------- cli

class _Parser(argparse.ArgumentParser):
    def error(self, message):  # usage errors -> exit 2
        self.print_usage(sys.stderr)
        _err(message)
        sys.exit(EXIT_USAGE)


def main(argv: list[str] | None = None) -> int:
    p = _Parser(prog="pbp heldout", description="Sealed held-out check runner")
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=_Parser)
    s = sub.add_parser("seal")
    s.add_argument("src")
    s.add_argument("store")
    v = sub.add_parser("verify")
    v.add_argument("store")
    v.add_argument("--seal")
    c = sub.add_parser("check")
    c.add_argument("store")
    c.add_argument("tree")
    c.add_argument("commit")
    c.add_argument("out")
    c.add_argument("--seal", required=True)
    c.add_argument("--inject", metavar="FILE")
    c.add_argument("--inject-as", metavar="REL")
    c.add_argument("--timeout", type=positive_seconds, default=900.0)
    a = p.parse_args(argv)
    exit_on_signals()
    try:
        if a.cmd == "seal":
            return cmd_seal(a.src, a.store)
        if a.cmd == "verify":
            return cmd_verify(a.store, a.seal)
        return cmd_check(a.store, a.tree, a.commit, a.out, a.seal, a.inject, a.timeout, a.inject_as)
    except Usage as exc:
        _err(str(exc))
        return EXIT_USAGE
    except Refused as exc:
        _err(f"refused: {exc}")
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
