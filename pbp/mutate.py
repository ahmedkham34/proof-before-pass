#!/usr/bin/env python3
"""mutate - a small deterministic mutation checker (stdlib only).

What it checks: whether a test suite can fail. It copies the repo's git-tracked
files to a temp folder (a tracked symlink to a file is copied as a plain file
with its content; any other symlink is refused), checks the tests pass on the
module as rewritten by ast.unparse (the same form every mutant is written in),
then applies one AST
mutant at a time to one module (flipped comparison, and/or swap, dropped `not`,
flipped boolean return, `return None`, int + 1, `raise` -> `pass`, if-test
forced true/false) and re-runs the tests. A mutant the tests still pass on is
a "survivor": a wrong program the suite accepts.

Usage:
  python -m pbp mutate --repo DIR --module REL/PATH.py --tests REL/TEST.py [...]
                       [--max N] [--timeout S] [--json OUT] [--fail-under PCT]

  --max N          run at most N mutants, evenly spread (default 150); the
                   report says "sampled N of M" when it cut
  --fail-under PCT exit 1 when the score is below PCT (0-100)

A mutant that runs past --timeout counts as killed (the suite noticed a hang)
and is shown in its own "timeouts=" count.

Limits:
  - timeouts count as kills, so a slow machine (or a short --timeout) can
    raise the score; read the "timeouts=" count next to the score;
  - a test that checks the module's source text (a hash, a line count, a
    grep) kills every mutant, so the score says nothing for such a suite;
  - a run with zero mutants has no score and fails any --fail-under,
    including --fail-under 0.

Exit codes: 0 after a baseline-green run (score at or above --fail-under, if
given), 1 score below --fail-under (or no mutants with --fail-under), 2
usage/setup error, 3 baseline red.
"""
from __future__ import annotations

import argparse
import ast
import copy
import json
import os
import re
import shutil
import sys
import tempfile

from pbp._common import exit_on_signals, run_cmd
from pbp._common import safe_rel as _safe_rel

EXIT_OK = 0
EXIT_UNDER = 1
EXIT_USAGE = 2
EXIT_BASELINE_RED = 3
TRIM = 100

CMP_FLIP = {
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
    ast.Lt: ast.GtE, ast.GtE: ast.Lt,
    ast.Gt: ast.LtE, ast.LtE: ast.Gt,
    ast.In: ast.NotIn, ast.NotIn: ast.In,
    ast.Is: ast.IsNot, ast.IsNot: ast.Is,
}


class SetupError(Exception):
    pass


# --------------------------------------------------------------------------
# mutant generation
# --------------------------------------------------------------------------
def split_lines(source):
    return re.split(r"\r\n|\r|\n", source)


def gen_mutants(tree):
    """Return a sorted list of mutant specs (dicts) for tree."""
    muts = []

    def add(idx, node, kind, op=0):
        muts.append({"idx": idx, "line": node.lineno, "col": node.col_offset,
                     "kind": kind, "op": op})

    for idx, node in enumerate(ast.walk(tree)):
        if isinstance(node, ast.Compare):
            for i, op in enumerate(node.ops):
                if type(op) in CMP_FLIP:
                    add(idx, node, "cmp_flip", i)
        elif isinstance(node, ast.BoolOp):
            add(idx, node, "bool_flip")
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            add(idx, node, "drop_not")
        elif isinstance(node, ast.Return):
            v = node.value
            if isinstance(v, ast.Constant) and isinstance(v.value, bool):
                add(idx, node, "return_bool")
            if v is not None and not (isinstance(v, ast.Constant) and v.value is None):
                add(idx, node, "return_none")
        elif isinstance(node, ast.Constant) and type(node.value) is int:
            add(idx, node, "int_inc")
        elif isinstance(node, ast.Raise):
            add(idx, node, "raise_pass")
        elif isinstance(node, ast.If):
            add(idx, node.test, "if_true")
            muts[-1]["idx"] = idx
            add(idx, node.test, "if_false")
            muts[-1]["idx"] = idx
    muts.sort(key=lambda m: (m["line"], m["col"], m["kind"], m["op"], m["idx"]))
    return muts


def spread(items, n):
    """Pick n items evenly spread over items (deterministic)."""
    total = len(items)
    if n is None or n >= total:
        return list(items)
    if n <= 0:
        return []
    return [items[(i * total) // n] for i in range(n)]


def _replace_child(parent, old, new):
    for field, value in ast.iter_fields(parent):
        if value is old:
            setattr(parent, field, new)
            return
        if isinstance(value, list):
            for i, item in enumerate(value):
                if item is old:
                    value[i] = new
                    return
    raise RuntimeError("child not found")


def apply_mutant(tree, mut):
    """Return (mutated tree copy, original node, replacement node)."""
    t = copy.deepcopy(tree)
    nodes = list(ast.walk(t))
    parents = {}
    for p in nodes:
        for c in ast.iter_child_nodes(p):
            parents[id(c)] = p
    node = nodes[mut["idx"]]
    kind = mut["kind"]
    shown_old = node
    if kind == "cmp_flip":
        old = copy.deepcopy(node)
        node.ops[mut["op"]] = CMP_FLIP[type(node.ops[mut["op"]])]()
        return t, old, node
    if kind == "bool_flip":
        old = copy.deepcopy(node)
        node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
        return t, old, node
    if kind == "drop_not":
        _replace_child(parents[id(node)], node, node.operand)
        return t, shown_old, node.operand
    if kind == "return_bool":
        old = copy.deepcopy(node)
        node.value = ast.Constant(value=not node.value.value)
        return t, old, node
    if kind == "return_none":
        old = copy.deepcopy(node)
        node.value = ast.Constant(value=None)
        return t, old, node
    if kind == "int_inc":
        old = copy.deepcopy(node)
        node.value = node.value + 1
        return t, old, node
    if kind == "raise_pass":
        new = ast.copy_location(ast.Pass(), node)
        _replace_child(parents[id(node)], node, new)
        return t, shown_old, new
    if kind in ("if_true", "if_false"):
        old = node.test
        new = ast.copy_location(ast.Constant(value=(kind == "if_true")), old)
        node.test = new
        return t, old, new
    raise ValueError(kind)


def _trim(text):
    text = text.strip()
    return text if len(text) <= TRIM else text[:TRIM - 3] + "..."


def describe(lines, old, new):
    """Return (original line text, mutated line text), both trimmed."""
    lineno = old.lineno
    line = lines[lineno - 1] if 0 < lineno <= len(lines) else ""
    repl = ast.unparse(new)
    end_line = getattr(old, "end_lineno", None)
    if end_line == lineno and old.end_col_offset is not None:
        raw = line.encode("utf-8")
        mutated = (raw[:old.col_offset].decode("utf-8", "replace") + repl
                   + raw[old.end_col_offset:].decode("utf-8", "replace"))
    else:
        raw = line.encode("utf-8")
        first = repl.split("\n", 1)[0]
        mutated = raw[:old.col_offset].decode("utf-8", "replace") + first + " ..."
    return _trim(line), _trim(mutated)


# --------------------------------------------------------------------------
# repo copy
# --------------------------------------------------------------------------
def copy_tracked(repo, dest, workdir):
    rc, out = run_cmd(["git", "-C", repo, "ls-files", "-z"], cwd=workdir,
                      timeout=60, log_path=os.path.join(workdir, "git.log"))
    if rc != 0:
        raise SetupError("git ls-files failed in %s: %s"
                         % (repo, out.decode("utf-8", "replace").strip()))
    names = sorted(n for n in out.decode("utf-8", "surrogateescape").split("\0") if n)
    for name in names:
        src = os.path.join(repo, name)
        dst = os.path.join(dest, name)
        if not os.path.lexists(src):
            continue  # tracked but deleted in the work tree
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.islink(src):
            # never recreate a link: a mutant written to it would go through into the original file
            if not os.path.isfile(src):
                raise SetupError("tracked symlink does not point to a regular file: %s" % name)
            shutil.copyfile(src, dst)
        elif os.path.isfile(src):
            shutil.copy2(src, dst)
    return names


def safe_rel(path):
    try:
        return _safe_rel(path)
    except ValueError:
        raise SetupError("path must be relative to the repo: %s" % path) from None


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def parse_args(argv):
    ap = argparse.ArgumentParser(prog="pbp mutate", description="Deterministic mutation checker")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--module", required=True)
    ap.add_argument("--tests", required=True, nargs="+")
    ap.add_argument("--max", type=int, default=150)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--json", dest="json_out")
    ap.add_argument("--fail-under", type=float, metavar="PCT")
    args = ap.parse_args(argv)
    if args.fail_under is not None and not 0 <= args.fail_under <= 100:
        ap.error("--fail-under must be between 0 and 100")
    if args.max < 0:
        ap.error("--max must be >= 0")
    if args.timeout <= 0:
        ap.error("--timeout must be > 0")
    return args


def format_report(result):
    lines = ["mutants: total=%d killed=%d survived=%d timeouts=%d score=%s"
             % (result["total"], result["killed"], result["survived"], result["timeouts"],
                "n/a" if result["score"] is None else "%.1f%%" % result["score"])]
    if result["sampled"]:
        lines.append("sampled %d of %d generated mutants (--max %d)"
                     % (result["total"], result["generated"], result["total"]))
    if result["survivors"]:
        lines.append("survivors:")
        for s in result["survivors"]:
            lines.append("%d:%d %s  %s -> %s"
                         % (s["line"], s["col"], s["kind"], s["original"], s["mutated"]))
    return "\n".join(lines)


def run(args):
    repo = os.path.abspath(args.repo)
    if not os.path.isdir(repo):
        raise SetupError("not a directory: %s" % args.repo)
    module_rel = safe_rel(args.module)
    tests_rel = [safe_rel(t) for t in args.tests]

    workdir = tempfile.mkdtemp(prefix="mutate-")
    try:
        copy_dir = os.path.join(workdir, "repo")
        os.mkdir(copy_dir)
        log = os.path.join(workdir, "run.log")
        copy_tracked(repo, copy_dir, workdir)
        module_path = os.path.join(copy_dir, module_rel)
        if not os.path.isfile(module_path):
            raise SetupError("module is not a tracked file: %s" % args.module)
        for t in tests_rel:
            if not os.path.exists(os.path.join(copy_dir, t)):
                raise SetupError("test is not a tracked file: %s" % t)

        pytest_cmd = [sys.executable, "-B", "-m", "pytest", "-q", "-x",
                      "-p", "no:cacheprovider"] + tests_rel

        with open(module_path, "rb") as fh:
            original = fh.read()
        try:
            source = original.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SetupError("module is not UTF-8: %s" % exc) from None
        try:
            tree = ast.parse(source, filename=module_rel)
        except SyntaxError as exc:
            raise SetupError("cannot parse module: %s" % exc)
        lines = split_lines(source)

        # Baseline on the unparsed module: every mutant is written by ast.unparse, so a behaviour change that comes
        # from unparse alone (comments, line numbers, getsource) must show here, not be counted as kills.
        with open(module_path, "wb") as fh:
            fh.write((ast.unparse(tree) + "\n").encode("utf-8"))
        rc, out = run_cmd(pytest_cmd, copy_dir, args.timeout, log)
        if rc != 0:
            why = "timeout" if rc is None else "exit %d" % rc
            with open(module_path, "wb") as fh:
                fh.write(original)
            orc, _ = run_cmd(pytest_cmd, copy_dir, args.timeout, log + ".orig")
            if orc == 0:
                sys.stderr.write("baseline red (%s): the tests pass on the original module but fail on its "
                                 "ast.unparse form, so mutants cannot be judged\n" % why)
            else:
                sys.stderr.write("baseline red (%s)\n" % why)
            tail = out.decode("utf-8", "replace").strip().splitlines()[-20:]
            if tail:
                sys.stderr.write("\n".join(tail) + "\n")
            return EXIT_BASELINE_RED

        all_muts = gen_mutants(tree)
        chosen = spread(all_muts, args.max)
        results = []
        killed = 0
        for mut in chosen:   # each mutant overwrites the whole module in the throwaway copy
            mtree, old, new = apply_mutant(tree, mut)
            code = ast.unparse(ast.fix_missing_locations(mtree)) + "\n"
            with open(module_path, "wb") as fh:
                fh.write(code.encode("utf-8"))
            rc, _ = run_cmd(pytest_cmd, copy_dir, args.timeout, log)
            status = ("timeout" if rc is None
                      else "survived" if rc == 0 else "killed")
            if status != "survived":
                killed += 1
            orig_txt, mut_txt = describe(lines, old, new)
            results.append({"line": mut["line"], "col": mut["col"],
                            "kind": mut["kind"], "original": orig_txt,
                            "mutated": mut_txt, "status": status})

        total = len(results)
        survivors = [{k: r[k] for k in ("line", "col", "kind", "original", "mutated")}
                     for r in results if r["status"] == "survived"]
        result = {
            "module": module_rel.replace(os.sep, "/"),
            "generated": len(all_muts),
            "sampled": total < len(all_muts),
            "total": total,
            "killed": killed,
            "survived": total - killed,
            "timeouts": sum(1 for r in results if r["status"] == "timeout"),
            "score": None if total == 0 else round(100.0 * killed / total, 1),
            "survivors": survivors,
            "mutants": results,
        }
        print(format_report(result))
        if args.json_out:
            with open(args.json_out, "w", encoding="utf-8") as fh:
                json.dump(result, fh, indent=2, sort_keys=True)
                fh.write("\n")
        if args.fail_under is not None and (result["score"] is None or result["score"] < args.fail_under):
            sys.stderr.write("score %s is below --fail-under %g\n"
                             % ("n/a" if result["score"] is None else "%.1f%%" % result["score"], args.fail_under))
            return EXIT_UNDER
        return EXIT_OK
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main(argv=None):
    args = parse_args(argv)
    exit_on_signals()
    try:
        return run(args)
    except SetupError as exc:
        sys.stderr.write("pbp mutate: %s\n" % exc)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
