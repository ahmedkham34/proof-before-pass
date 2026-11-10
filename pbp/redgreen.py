#!/usr/bin/env python3
"""redgreen: do the new tests fail before the change and pass after it, with the existing tests untouched?

Usage: python -m pbp redgreen TREE PRE POST [options]
  TREE  a git repository (a trial copy: two temporary worktrees are added to it and removed again)
  PRE   the commit before the change
  POST  the commit with the change and its new tests

What it checks (all of it from git, the Python AST and pytest's own junit report):
  RED    The new tests (test functions or methods that exist at POST and not at PRE) are run on the PRE
         code, with the POST test files laid over it. At least one new test must fail with an assertion
         failure: an AssertionError, or a pytest.raises / pytest.fail outcome, read from the exception
         type in pytest's junit report. A new test that fails at PRE only with another error
         (AttributeError, NameError, ImportError, TypeError, ...) or a collection error is RED_UNPROVEN:
         a missing name is not a proof. Any RED_UNPROVEN test fails the verdict unless --allow-error-red is
         given, even when another new test is properly red; so a real fix that adds a new function, whose
         test fails at PRE with NameError or ImportError, needs the flag.
  GREEN  The whole suite runs at POST (plain `pytest` in the POST worktree). Any failure or error
         anywhere fails the verdict, with the count, and every new test must pass (not skip).
         --green-only-changed runs only the test files that hold new tests (for slow suites);
         the summary line says which mode ran (green=suite or green=changed).
  KEPT   Nothing that existed at PRE was weakened. Test files are test_*.py and *_test.py anywhere;
         test roots are folders named tests, test or testing, at any depth.
         - In every test file that existed at PRE, each test function and test method must have the
           same AST at POST (ast.dump of the whole def: decorators, docstring and body). An early
           return, a changed assert, an expected value replaced by a call, an added skip or xfail
           (decorator or call) or a removed test each fail. A test file deleted at POST fails.
         - In those files, every other module-level statement (helpers, fixtures, assignments,
           pytestmark, pytest_plugins, class bodies apart from their test methods) must be unchanged,
           so a new module-level helper or constant (for example CASES = [...] for a new parametrized
           test) fails. New imports are allowed unless they rebind a name the file already bound; new test
           functions, new test methods and new classes that hold only test methods are allowed.
         - A file that does not parse at PRE or POST fails.
         - Any non-test Python file under a test root (helpers, plugins) and any conftest.py anywhere
           that was added, changed or deleted fails.
         - Any change to the pytest section of pytest.ini, .pytest.ini, tox.ini, setup.cfg or
           pyproject.toml, or to pytest.toml, anywhere in the tree, fails (the whole section is compared).
         - New test code must not patch other code at import time. This covers a whole new test file
           and, in an existing test file, the decorators, default values and annotations of each new test
           function or method and the decorators, bases and keywords of each new test class. Flagged: an
           assignment, deletion, for or with target on another object's attribute or item (m.x,
           vars(m)[k], m.__dict__[k]), a method call on vars(...) or .__dict__, a call to setattr,
           delattr, exec, eval, anything named *patch* or *.start() (a lambda's body counts too), and a
           pytest_plugins assignment. Patching inside a test body or a fixture body is NOT detected.

What it does not check:
  - whether the change is a correct or complete fix: it only shows the new tests went from red to green;
  - patching or stubbing inside a new test body or fixture (monkeypatch, mock.patch as a context
    manager or decorator, assigning to the module under test);
  - import-time patching in any form other than the direct ones listed above. The check reads syntax and
    does not follow values, so these are known to get through: a helper function the new test file defines
    and calls at import (also as a decorator), a metaclass, an alias of the module's namespace
    (`ns = mod.__dict__; ns["f"] = ...`), `object.__setattr__`, `dict.__setitem__` or operator.setitem,
    `map` over `__setitem__`, getattr(builtins, "setattr"), importlib, or code imported from another
    module. Read a PASS as "none of the common shortcuts was found", not as proof that no test cheats;
  - a new test that asserts something trivially true only after the change (for example a new
    constant the change adds, or `try: ...; except AttributeError: assert False`);
  - changes to non-Python fixtures or data files, and to plugins installed in the environment or
    imported from outside the test roots;
  - tests generated at run time (pytest_generate_tests output, tests built in loops or by exec);
    only test functions and methods written as `def` are compared;
  - it runs the code under test and the tests at PRE and POST, so use it only on code you would run anyway.

Output: a summary line "PASS new=N red_new=M green=suite" (or FAIL; " red_unproven=K" is added when
K > 0), then one " - REASON" line per failed check. Exit 0 PASS; 1 a check failed; 2 usage error
(bad arguments, an unknown commit, TREE not a git repository, a path outside TREE, a symlink in PRE that
would take a POST test file out of the PRE worktree) or an unexpected error (one line on stderr);
128+N stopped by signal N (SIGTERM, SIGHUP: the pytest process group is killed and the worktrees are
removed first). --help exits 0.

Optional, off by default (environment):
  PBP_PYTHON  interpreter for pytest (default: the running interpreter)
  PBP_LOCK    a lock file; when set, each pytest run holds an exclusive flock on it
"""
from __future__ import annotations

import argparse
import ast
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import xml.etree.ElementTree as ET

from pbp._common import exit_on_signals, git_env, is_within, run_cmd, safe_rel

PY = os.environ.get("PBP_PYTHON") or sys.executable
LOCK = os.environ.get("PBP_LOCK") or None
TIMEOUT = 1800.0

TEST_FILE = re.compile(r"(^|/)(test_[^/]*|[^/]*_test)\.py$")
TEST_ROOTS = {"tests", "test", "testing"}
INI_SECTION = {"pytest.ini": "pytest", ".pytest.ini": "pytest", "tox.ini": "pytest", "setup.cfg": "tool:pytest"}
WHOLE_FILE = {"pytest.toml", ".pytest.toml"}
RED_TYPES = {"AssertionError", "Failed"}
STATUS_WORD = {"A": "added", "M": "changed", "D": "deleted", "T": "changed"}


class UsageError(Exception):
    pass


class GitError(Exception):
    pass


def git(tree, *a, check=True):
    return git_bytes(tree, *a, check=check).decode("utf-8", "replace")


def git_bytes(tree, *a, check=True):
    r = subprocess.run(["git", "-C", tree, *a], capture_output=True, stdin=subprocess.DEVNULL, env=git_env())
    if check and r.returncode != 0:
        raise GitError(f"git {a[0]} failed: {r.stderr.decode('utf-8', 'replace').strip()[-200:]}")
    return r.stdout


def show(tree, rev, path):
    """The bytes of PATH at REV, or None when it does not exist there."""
    r = subprocess.run(["git", "-C", tree, "show", f"{rev}:{path}"], capture_output=True, stdin=subprocess.DEVNULL,
                       env=git_env())
    return r.stdout if r.returncode == 0 else None


def changed_files(tree, pre, post):
    """[(status, path)] for PRE..POST, read with -z so any file name survives."""
    raw = git_bytes(tree, "diff", "--name-status", "--no-renames", "-z", pre, post).split(b"\0")
    out = []
    for i in range(0, len(raw) - 1, 2):
        out.append((raw[i].decode("ascii", "replace")[:1], os.fsdecode(raw[i + 1])))
    return out


def is_test_file(path):
    return bool(TEST_FILE.search(path))


def under_test_root(path):
    return any(part in TEST_ROOTS for part in path.split("/")[:-1])


# ------------------------------------------------------------------ pytest runs
def pytest(cwd, files, junit, base, timeout):
    """Run pytest in its own process group (killed on timeout); return (rc or None on timeout,
    {(classname, name): (outcome, exception type)}, output tail)."""
    cmd = [PY, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=long", "--continue-on-collection-errors",
           f"--junitxml={junit}", f"--basetemp={base}", *files]
    env = dict(os.environ)
    log = junit + ".log"
    if LOCK:
        with open(LOCK, "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            rc, out = run_cmd(cmd, cwd, timeout, log, env)
    else:
        rc, out = run_cmd(cmd, cwd, timeout, log, env)
    return rc, read_junit(junit), out.decode("utf-8", "replace")[-1500:]


def read_junit(path):
    res = {}
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return res
    for tc in root.iter("testcase"):
        outcome, etype = "pass", ""
        for c in tc:
            if c.tag in ("failure", "error"):
                outcome = c.tag
                lines = [ln for ln in (c.text or "").splitlines() if ln.strip()]
                m = re.search(r":\d+: ([\w.]+)$", lines[-1]) if lines else None
                etype = m.group(1) if m else ""
                if c.tag == "error":
                    etype = etype or "collection error"
                break
            if c.tag == "skipped":
                outcome = "skip"
        res[(tc.get("classname") or "", tc.get("name") or "")] = (outcome, etype)
    return res


# ------------------------------------------------------------------ AST: tests and module-level code
def parse(src, label):
    try:
        return ast.parse(src)
    except (SyntaxError, ValueError) as exc:
        raise SyntaxError(f"{label} does not parse: {exc.__class__.__name__}: {exc}") from None


def is_test_def(node):
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test")


def test_defs(tree):
    """{qualname: [ast.dump, ...]} for every test function and test method (classes at any depth)."""
    out = {}

    def walk(body, prefix):
        for n in body:
            if is_test_def(n):
                out.setdefault(prefix + n.name, []).append(ast.dump(n))
            elif isinstance(n, ast.ClassDef):
                walk(n.body, f"{prefix}{n.name}::")
    walk(tree.body, "")
    return out


def shell(node):
    """A class with its test methods removed (recursively), so its other members can be compared."""
    if not isinstance(node, ast.ClassDef):
        return node
    body = [shell(n) for n in node.body if not is_test_def(n)]
    return ast.ClassDef(name=node.name, bases=node.bases, keywords=node.keywords, body=body,
                        decorator_list=node.decorator_list, type_params=getattr(node, "type_params", []))


def is_docstring(n):
    return isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)


def module_parts(tree):
    """(imports, other statements, top-level bound names) of a module; test functions are left out."""
    imports, other, names = [], [], set()
    for i, n in enumerate(tree.body):
        if i == 0 and is_docstring(n):
            continue
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            imports.append(n)
            names.update((a.asname or a.name).split(".")[0] for a in n.names)
        elif not is_test_def(n):
            other.append(shell(n))
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(n.name)
            for t in getattr(n, "targets", []) + [getattr(n, "target", None)]:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        else:
            names.add(n.name)
    return imports, other, names


def is_new_test_class(node, pre_names):
    """A class new at POST whose body, apart from test methods, is only a docstring or pass. Its decorators,
    bases and keywords, and the heads of its test methods, are checked for import-time patches in
    compare_test_file (new_test_nodes)."""
    if not isinstance(node, ast.ClassDef) or node.name in pre_names:
        return False
    return all(is_docstring(n) or isinstance(n, ast.Pass) for n in node.body)


def new_test_nodes(tree, pre_tests, pre_names):
    """The test defs (functions and methods) whose qualname is new at POST, and the new test classes."""
    out = []

    def walk(body, prefix, in_new_class):
        for n in body:
            if is_test_def(n):
                if in_new_class or prefix + n.name not in pre_tests:
                    out.append(n)
            elif isinstance(n, ast.ClassDef):
                new_class = not prefix and n.name not in pre_names
                if new_class:
                    out.append(n)
                walk(n.body, f"{prefix}{n.name}::", in_new_class or new_class)
    walk(tree.body, "", False)
    return out


def first_line(node):
    return ast.unparse(node).splitlines()[0][:100]


def compare_test_file(f, pre_src, post_src):
    """KEPT reasons for a test file that exists at PRE and POST."""
    try:
        a, b = parse(pre_src, f"{f} at PRE"), parse(post_src, f"{f} at POST")
    except SyntaxError as exc:
        return [f"KEPT: {exc}"]
    reasons = []
    pre_tests, post_tests = test_defs(a), test_defs(b)
    for q, dumps in pre_tests.items():
        if q not in post_tests:
            reasons.append(f"KEPT: {f}::{q} removed")
        elif post_tests[q] != dumps:
            reasons.append(f"KEPT: {f}::{q} changed")
    pre_imp, pre_other, pre_names = module_parts(a)
    post_imp, post_other, _ = module_parts(b)
    post_imp_dumps = [ast.dump(n) for n in post_imp]
    pre_imp_dumps = [ast.dump(n) for n in pre_imp]
    for n in pre_imp:
        if ast.dump(n) not in post_imp_dumps:
            reasons.append(f"KEPT: {f}: import removed or changed: {first_line(n)}")
    for n in post_imp:
        if ast.dump(n) in pre_imp_dumps:
            continue
        clash = sorted({(x.asname or x.name).split(".")[0] for x in n.names} & pre_names)
        if clash:
            reasons.append(f"KEPT: {f}: new import rebinds {', '.join(clash)}: {first_line(n)}")
    for node in new_test_nodes(b, pre_tests, pre_names):
        heads = node.decorator_list + node.bases + node.keywords if isinstance(node, ast.ClassDef) else [node]
        for n in patches_in(heads):
            reasons.append(f"KEPT: {f}: new test code patches at import time: {first_line(n)}")
    kept_other = [n for n in post_other if not is_new_test_class(n, pre_names)]
    pre_d, post_d = [ast.dump(n) for n in pre_other], [ast.dump(n) for n in kept_other]
    if pre_d != post_d:
        diff = next((n for n, d in zip(kept_other, post_d) if d not in pre_d), None)
        gone = next((n for n, d in zip(pre_other, pre_d) if d not in post_d), None)
        what = (f"added or changed: {first_line(diff)}" if diff is not None else
                f"removed: {first_line(gone)}" if gone is not None else "reordered")
        reasons.append(f"KEPT: {f}: module-level code {what}")
    return reasons


PATCH_CALLS = {"setattr", "delattr", "start", "exec", "eval"}


def dotted(fn):
    parts = []
    while isinstance(fn, ast.Attribute):
        parts.append(fn.attr)
        fn = fn.value
    if isinstance(fn, ast.Name):
        parts.append(fn.id)
    return ".".join(reversed(parts))


def is_namespace(node):
    """`X.__dict__` or `vars(...)`: another object's namespace, writable like a dict."""
    return ((isinstance(node, ast.Attribute) and node.attr == "__dict__")
            or (isinstance(node, ast.Call) and dotted(node.func) == "vars"))


def writes_elsewhere(target):
    """An assignment or deletion target that changes another object: `a.b`, `a.b[k]`, `vars(a)[k]`, `f()[k]`."""
    if isinstance(target, ast.Attribute):
        return True
    if isinstance(target, ast.Subscript):
        base = target.value
        while isinstance(base, ast.Subscript):
            base = base.value
        return isinstance(base, (ast.Attribute, ast.Call))
    return False


def patches_in(nodes):
    """The parts of NODES that run at import time and patch other code. A def body is not visited (it runs
    later), but its decorators, default values and annotations are, since they run when the def runs; a class
    body, bases, keywords and decorators are, and so is a lambda (defaults and body: a lambda used as a
    decorator or called at once runs at import)."""
    found = []

    def visit(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            heads = [*node.decorator_list, *a.defaults, *(d for d in a.kw_defaults if d is not None)]
            heads += [x.annotation for x in (*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg)
                      if x is not None and x.annotation is not None]
            heads += [node.returns] if node.returns is not None else []
            for h in heads:
                visit(h)
            return
        hit = False
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete, ast.For, ast.AsyncFor,
                             ast.withitem)):
            targets = (node.targets if isinstance(node, (ast.Assign, ast.Delete)) else
                       [node.optional_vars] if isinstance(node, ast.withitem) else [node.target])
            targets = [t for t in targets if t is not None]
            for t in targets:
                for e in (t.elts if isinstance(t, (ast.Tuple, ast.List)) else [t]):
                    if writes_elsewhere(e):
                        hit = True
                    if isinstance(e, ast.Name) and e.id == "pytest_plugins":
                        hit = True
        elif isinstance(node, ast.Call):
            name = dotted(node.func)
            last = name.rsplit(".", 1)[-1]
            if last in PATCH_CALLS or "patch" in name.lower():
                hit = True
            elif isinstance(node.func, ast.Attribute) and is_namespace(node.func.value):
                hit = True       # vars(m).update(...), m.__dict__.setdefault(...), ...
        if hit:
            found.append(node)
            return
        for child in ast.iter_child_nodes(node):
            visit(child)

    for n in nodes:
        visit(n)
    return found


def import_time_patches(src):
    """Statements of a new test file that patch other code when the file is imported."""
    return patches_in(ast.parse(src).body)


# ------------------------------------------------------------------ config sections
def ini_section(text, want):
    """The non-blank lines of section [WANT] in ini TEXT, or None when there is no such section."""
    lines, inside, found = [], False, False
    for ln in text.splitlines():
        m = re.match(r"\s*\[\s*([^\]]+?)\s*\]\s*$", ln)
        if m:
            inside = m.group(1) == want
            found = found or inside
            continue
        if inside and ln.strip():
            lines.append(ln.rstrip())
    return lines if found else None


def pytest_config(base, data):
    """The pytest settings a config file holds, in a comparable form (None when absent)."""
    if data is None:
        return None
    text = data.decode("utf-8", "replace")
    if base in WHOLE_FILE:
        return text
    if base == "pyproject.toml":
        try:
            section = tomllib.loads(text).get("tool", {}).get("pytest")
        except tomllib.TOMLDecodeError as exc:
            return f"unparsable: {exc}"
        return None if section is None else repr(section)
    return ini_section(text, INI_SECTION[base])


def kept_reasons(tree, pre, post, changes):
    """Every way PRE..POST could have bought GREEN by weakening the tests (KEPT)."""
    reasons = []
    for status, f in changes:
        base = f.rsplit("/", 1)[-1]
        word = STATUS_WORD.get(status, "changed")
        if base in INI_SECTION or base in WHOLE_FILE or base == "pyproject.toml":
            if pytest_config(base, show(tree, pre, f)) != pytest_config(base, show(tree, post, f)):
                reasons.append(f"KEPT: {f}: pytest configuration {word if base in WHOLE_FILE else 'changed'}")
        elif not f.endswith(".py"):
            continue
        elif base == "conftest.py" or (under_test_root(f) and not is_test_file(f)):
            reasons.append(f"KEPT: {f} {word} (a conftest, helper or plugin can skip, patch or hide tests)")
        elif is_test_file(f):
            if status == "D":
                reasons.append(f"KEPT: {f} deleted (a test file that existed at PRE)")
            elif status == "A":
                src = show(tree, post, f)
                try:
                    patches = import_time_patches(src)
                except (SyntaxError, ValueError) as exc:
                    reasons.append(f"KEPT: {f} at POST does not parse: {exc.__class__.__name__}: {exc}")
                    continue
                for n in patches:
                    reasons.append(f"KEPT: {f}: new test file patches at import time: {first_line(n)}")
            else:
                reasons.extend(compare_test_file(f, show(tree, pre, f), show(tree, post, f)))
    return reasons


# ------------------------------------------------------------------ new tests
def junit_prefix(f):
    return f[:-3].replace("/", ".")


def new_tests(tree, pre, post, files):
    """[(junit classname, function name)] of the test defs in FILES that exist at POST and not at PRE."""
    out = []
    for f in files:
        post_src = show(tree, post, f)
        if post_src is None:
            continue
        try:
            post_t = test_defs(ast.parse(post_src))
        except (SyntaxError, ValueError):
            continue
        pre_src = show(tree, pre, f)
        try:
            pre_t = test_defs(ast.parse(pre_src)) if pre_src is not None else {}
        except (SyntaxError, ValueError):
            pre_t = {}
        for q in post_t:
            if q not in pre_t:
                parts = q.split("::")
                out.append((".".join([junit_prefix(f), *parts[:-1]]), parts[-1]))
    return out


def outcomes(results, test):
    cls, name = test
    return [v for (c, n), v in results.items() if c == cls and (n == name or n.startswith(name + "["))]


def tid(test):
    return f"{test[0]}::{test[1]}"


def safe_path(tree, path, flag):
    """PATH relative to TREE, normalised; UsageError when it is absolute, climbs out or leads out by a symlink."""
    try:
        norm = safe_rel(path)
    except ValueError:
        norm = None
    if norm is None or norm == "." or not is_within(os.path.join(tree, norm), tree):
        raise UsageError(f"{flag} path must be relative and inside TREE: {path}")
    return norm.replace(os.sep, "/")


def write_into(wt, rel, data):
    """Write DATA to WT/REL, never through a symlink and never outside WT (PRE may commit a symlinked folder
    or a symlinked test file where POST has a real one)."""
    dest = os.path.join(wt, rel)
    parent = os.path.dirname(dest)
    probe = parent
    while not os.path.lexists(probe):
        probe = os.path.dirname(probe)
    if not is_within(probe, wt):
        raise UsageError(f"refusing to write {rel} at PRE: a symlink in the PRE commit leads outside the worktree")
    os.makedirs(parent, exist_ok=True)
    if os.path.lexists(dest):
        os.unlink(dest)
    with open(dest, "xb") as fh:
        fh.write(data)


def build_parser():
    p = argparse.ArgumentParser(prog="python -m pbp redgreen", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tree", metavar="TREE", help="a git repository (a trial copy)")
    p.add_argument("pre", metavar="PRE", help="the commit before the change")
    p.add_argument("post", metavar="POST", help="the commit with the change and its new tests")
    p.add_argument("--tests", action="append", default=[], metavar="FILES",
                   help="test files to run for RED, relative to TREE (space separated, repeatable); "
                        "default: the test files that hold new tests")
    p.add_argument("--out", metavar="FILE.json", help="write a JSON report to this path, relative to TREE")
    p.add_argument("--refuse-under", action="append", default=[], metavar="DIR",
                   help="refuse to run when TREE lies under DIR (repeatable)")
    p.add_argument("--allow-error-red", action="store_true",
                   help="count a new test that fails at PRE with a non-assertion error or a collection error as red")
    p.add_argument("--green-only-changed", action="store_true",
                   help="at POST run only the test files that hold new tests, not the whole suite")
    p.add_argument("--timeout", type=float, default=TIMEOUT, metavar="S",
                   help=f"seconds per pytest run; the whole process group is killed after it (default {TIMEOUT:g})")
    return p


def parse_args(argv):
    return build_parser().parse_args(argv)


def check(args):
    tree = os.path.realpath(args.tree)
    for refused in args.refuse_under:
        if is_within(tree, refused):
            print(f"refusing: TREE is under {refused}")
            return 2
    explicit = [safe_path(tree, f, "--tests") for spec in args.tests for f in spec.split()]
    out_path = safe_path(tree, args.out, "--out") if args.out else None
    if not os.path.isdir(tree):
        raise UsageError(f"TREE is not a folder: {args.tree}")
    try:
        pre = git(tree, "rev-parse", "--verify", args.pre + "^{commit}").strip()
        post = git(tree, "rev-parse", "--verify", args.post + "^{commit}").strip()
    except GitError as exc:
        raise UsageError(f"cannot resolve PRE/POST in TREE: {exc}") from None
    for f in explicit:
        if show(tree, post, f) is None:
            raise UsageError(f"--tests file not in POST: {f}")
    changes = changed_files(tree, pre, post)
    if explicit:
        files = explicit
    else:
        cand = [f for s, f in changes if s != "D" and is_test_file(f)]
        files = [f for f in cand if new_tests(tree, pre, post, [f])]
    report = {"tree": tree, "pre": pre, "post": post, "files": files,
              "green_mode": "changed" if args.green_only_changed else "suite"}
    reasons = kept_reasons(tree, pre, post, changes)
    new = new_tests(tree, pre, post, files)
    report["new_tests"] = [tid(t) for t in new]
    red_new, unproven = [], []
    if not files or not new:
        reasons.append("RED: no new test functions in PRE..POST; nothing was shown red")
    else:
        work = tempfile.mkdtemp(prefix="redgreen_")
        try:
            wt = os.path.join(work, "pre")
            git(tree, "worktree", "add", "-q", "--detach", wt, pre)
            try:
                for f in files:
                    write_into(wt, f, show(tree, post, f))
                rrc, red, red_tail = pytest(wt, files, os.path.join(work, "red.xml"), os.path.join(work, "bt_red"),
                                            args.timeout)
            finally:
                git(tree, "worktree", "remove", "--force", wt, check=False)
            wt2 = os.path.join(work, "post")
            git(tree, "worktree", "add", "-q", "--detach", wt2, post)
            try:
                green_files = files if args.green_only_changed else []
                grc, green, green_tail = pytest(wt2, green_files, os.path.join(work, "green.xml"),
                                                os.path.join(work, "bt_green"), args.timeout)
            finally:
                git(tree, "worktree", "remove", "--force", wt2, check=False)
            git(tree, "worktree", "prune", check=False)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if rrc is None:
            reasons.append(f"TIMEOUT: pytest at PRE ran past {args.timeout:g}s and was killed")
        for t in new:
            got = outcomes(red, t)
            if any(o == "failure" and e in RED_TYPES for o, e in got):
                red_new.append(tid(t))
            elif not got or any(o in ("failure", "error") for o, _e in got):
                kinds = sorted({e or "not collected" for o, e in got if o != "pass"} or {"not collected"})
                unproven.append(f"{tid(t)} ({', '.join(kinds)})")
        report["red_tail"] = red_tail[-400:] if unproven else ""
        if unproven:
            if args.allow_error_red:
                red_new += unproven
            else:
                reasons.append(f"RED_UNPROVEN: {len(unproven)} new test(s) fail at PRE with an error, not an "
                               f"assertion failure, which proves nothing about the fix: {unproven[:5]} "
                               f"(accept with --allow-error-red)")
        if not red_new:
            reasons.append("RED: no new test fails with an assertion on the unfixed code; the proof proves nothing")
        if grc is None:
            reasons.append(f"TIMEOUT: pytest at POST ran past {args.timeout:g}s and was killed")
        bad = sorted(f"{c}::{n}" for (c, n), (o, _e) in green.items() if o in ("failure", "error"))
        if bad:
            reasons.append(f"GREEN: {len(bad)} test(s) fail at POST ({report['green_mode']}): {bad[:5]}")
        elif grc not in (0, None):
            reasons.append(f"GREEN: pytest exited rc={grc} at POST ({report['green_mode']}): {green_tail[-300:]}")
        not_passing = [tid(t) for t in new if not outcomes(green, t)
                       or any(o != "pass" for o, _e in outcomes(green, t))]
        if not_passing:
            reasons.append(f"GREEN: new test(s) not passing at POST: {not_passing[:5]}")
        report["green_fail"] = bad
    report.update(red_failing_new=red_new, red_unproven=unproven, verdict="FAIL" if reasons else "PASS",
                  reasons=reasons)
    if out_path:
        with open(os.path.join(tree, out_path), "w") as fh:
            json.dump(report, fh, indent=1)
    print(report["verdict"], f"new={len(new)} red_new={len(red_new)}"
          + (f" red_unproven={len(unproven)}" if unproven else "") + f" green={report['green_mode']}")
    for r in reasons:
        print(" -", r)
    return 0 if not reasons else 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    exit_on_signals()
    try:
        return check(args)
    except UsageError as exc:
        print(f"pbp redgreen: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # any surprise is an error of the tool, never a FAIL verdict
        print(f"pbp redgreen: unexpected error: {exc.__class__.__name__}: "
              f"{str(exc).splitlines()[0] if str(exc) else ''}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
