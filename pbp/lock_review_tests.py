#!/usr/bin/env python3
"""lock_review_tests - turn a review's text tests into locked tests, keeping only the ones that really fail.

What it checks: which of a reviewer's claimed findings are backed by a test that is red on the exact code under
review. A finding whose test is green (or cannot even be collected) is a claim, not a proof.

usage: python -m pbp lock-review-tests REVIEW.md TREE OUT [--pytest-root REL] [--timeout S] [--preamble CODE]
  Takes every ```python block in REVIEW.md that defines a top-level `def test_`, writes it to a temp probe file in
  TREE/--pytest-root, and runs each top-level test_ function of the block on its own with pytest (cwd TREE). Prints
  one line per test function:
    RED     failed at run time: locked
    GREEN   passed: a claim with no proof
    BROKEN  a collection, import, syntax or fixture error, a NameError, or any pytest exit other than 0 or 1:
            a claim the author must check, never locked
    TIMEOUT ran past --timeout: never locked, a hang is not a proven failure
  An AttributeError or TypeError inside a test counts as RED: a test may rightly fail because the code lacks an
  attribute or takes other arguments. That also means a test that asks for a name the fix merely adds (with no real
  change) is red before the fix, so pair this with redgreen.
  The RED functions are written to OUT, each block kept with its finding number and with its non-red test
  functions removed. A ```python block with no test is put before every test only when its first line is the
  comment `# helper`; other code blocks (for example a quote of the code under review) are ignored.
  OUT and --pytest-root are paths relative to TREE and must stay inside it; OUT must not exist yet.
  Exit 0 when at least one test was RED and locked; 1 when no test was red (or the review has no test blocks);
  2 on a usage error (bad option, unreadable REVIEW.md, TREE not a folder, OUT or --pytest-root outside TREE,
  OUT already exists).
Rule: a finding counts only when its test is red on the exact candidate; the rest are claims. Red is necessary, not
sufficient: `assert False` is red too.
This runs the review's code with your user rights and writes probe files into TREE: use a throwaway copy.
"""
import argparse
import ast
import os
import re
import sys
import tempfile
import textwrap
from pathlib import Path

from pbp._common import exit_on_signals, git_env, is_within, positive_seconds, run_cmd, safe_rel, shown_path

BLOCK = re.compile(r"```python\n(.*?)```", re.S)
HELPER_MARK = "# helper"
TEST_DEF = re.compile(r"^def (test_\w+)", re.M)
NOT_PROVEN = re.compile(r"^E\s+(ModuleNotFoundError|ImportError|NameError|SyntaxError|FixtureLookupError)\b", re.M)


class UsageError(Exception):
    pass


def _lf(text):
    return text.replace("\r\n", "\n")


def parse_review(text):
    """One pass over the review: (test blocks as [(tag, code)], helper blocks as [code])."""
    text = _lf(text)
    tests, helpers = [], []
    for m in BLOCK.finditer(text):
        code = textwrap.dedent(m.group(1))   # blocks inside a markdown list are indented
        if test_names(code):
            head = text[:m.start()]
            nums = re.findall(r"(?m)^\s*(?:#+\s*)?(\d+)\.\s+\**\s*(BLOCKER|MAJOR|MINOR)", head)
            tests.append((f"finding {nums[-1][0]} {nums[-1][1]}" if nums else "finding ?", code))
        elif code.lstrip("\n").split("\n", 1)[0].strip() == HELPER_MARK:
            helpers.append(code)
    return tests, helpers


def blocks(text):
    return parse_review(text)[0]


def helper_blocks(text):
    return parse_review(text)[1]


def test_names(code):
    """The block's top-level test functions, read with ast (a def inside a string is not a test)."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return TEST_DEF.findall(code)   # pytest will report it BROKEN
    return [n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")]


def keep_only(code, names):
    """CODE with every top-level test_ function not in NAMES (decorators included) removed."""
    tree = ast.parse(code)
    lines = code.splitlines(keepends=True)
    drop = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_") \
                and n.name not in names:
            first = min([n.lineno] + [d.lineno for d in n.decorator_list])
            drop.update(range(first - 1, n.end_lineno))
    return "".join(ln for i, ln in enumerate(lines) if i not in drop)


def classify(rc, out):
    """(status, detail) for one pytest run of one test; rc None means it timed out."""
    if rc is None:
        return "TIMEOUT", ""
    last = out.strip().splitlines()[-1] if out.strip() else ""
    if rc == 0:
        return "GREEN", last
    # pytest exit 1 means "tests failed"; 2-5 are interrupted, internal error, usage, no tests; anything else
    # (a signal, a crash) is no result at all
    if rc != 1 or "error" in last and "failed" not in last:
        return "BROKEN", last
    # a test that fails only because it cannot import the code or a name proves nothing about the code
    m = NOT_PROVEN.search(out)
    if m:
        return "BROKEN", out[m.start():].split("\n", 1)[0]
    return "RED", last


def run_one(tree, code, timeout, root, name):
    fd, path = tempfile.mkstemp(suffix=".py", prefix="test_lockprobe_", dir=Path(tree) / root)
    with os.fdopen(fd, "w") as f:
        f.write(code)
    try:
        rc, raw = run_cmd([sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                           f"{path}::{name}"],   # only this test, never a star-imported one
                          cwd=tree, timeout=timeout, env=git_env())
    finally:
        Path(path).unlink(missing_ok=True)
    status, last = classify(rc, raw.decode("utf-8", "replace"))
    if status == "TIMEOUT":
        last = f"no result after {timeout:g}s"
    return status, last


def inside_tree(tree, rel, what):
    try:
        norm = safe_rel(rel)
    except ValueError:
        raise UsageError(f"{what} must be a relative path inside TREE: {rel}") from None
    path = tree / norm
    if not is_within(path, tree):
        raise UsageError(f"{what} leads outside TREE (through a symlink): {rel}")
    return path


def run(a):
    tree = Path(a.tree).resolve()
    if not tree.is_dir():
        raise UsageError(f"TREE is not a folder: {a.tree}")
    out = inside_tree(tree, a.out, "OUT")
    if os.path.lexists(out):
        raise UsageError(f"OUT already exists, refusing to overwrite it: {a.out}")
    probe_dir = inside_tree(tree, a.pytest_root, "--pytest-root")
    if not probe_dir.is_dir():
        raise UsageError(f"--pytest-root is not a folder in TREE: {a.pytest_root}")
    try:
        text = Path(a.review).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise UsageError(f"cannot read REVIEW.md: {exc}") from None
    found, helpers = parse_review(text)
    preamble = "\n".join(p for p in [a.preamble, *helpers] if p)
    if helpers:
        print(f"helpers: {len(helpers)} block(s) marked {HELPER_MARK!r} added before every test")
    if not found:
        print("no test blocks in the review")
        return 1
    keep = []
    for tag, code in found:
        full = (preamble + "\n" + code) if preamble else code
        red = []
        for name in test_names(code):
            status, last = run_one(tree, full, a.timeout, probe_dir.relative_to(tree), name)
            print(f"{status:7} {tag:20} {name}  ({last[:120]})")
            if status == "RED":
                red.append(name)
        if red:
            kept = keep_only(code, set(red))
            body = (preamble + "\n" + kept) if preamble else kept
            keep.append((len(red), f"# {tag}: red when locked\n{body.rstrip()}\n"))
    if not keep:
        print("no red test: nothing locked")
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    hdr = f'"""Locked review tests from {Path(a.review).name} (red when locked; do not weaken)."""\n\n'
    with open(out, "x", encoding="utf-8") as fh:   # "x": never overwrite
        fh.write(hdr + "\n\n".join(text for _n, text in keep))
    print(f"wrote {sum(n for n, _t in keep)} red test(s) to {shown_path(out)}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pbp lock-review-tests",
                                 description="keep only the review tests that are red on the tree")
    ap.add_argument("review")
    ap.add_argument("tree")
    ap.add_argument("out", help="file to write, relative to TREE; must not exist")
    ap.add_argument("--pytest-root", default=".", help="folder (relative to TREE) to place probe files in")
    ap.add_argument("--timeout", type=positive_seconds, default=300.0)
    ap.add_argument("--preamble", default="", help="code put before every block (imports the review assumed)")
    a = ap.parse_args(argv)
    exit_on_signals()
    try:
        return run(a)
    except UsageError as exc:
        print(f"pbp lock-review-tests: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
