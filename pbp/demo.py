"""demo - run the checks on the bundled demo material, where the right answer is known in advance.

What it checks: that each check catches the thing it is for.
  1 mutate             the vacuous demo suite lets mutants survive; the corrected suite kills every one
  2 redgreen           a fake "fix" that only edits tests is rejected; a real fix is accepted (judged by exit code)
  3 lock-review-tests  of a review's two test blocks, only the one that is red on the unfixed code is locked
  4 heldout            a real fix passes sealed tests it never saw; a candidate that forges a pass line is refused
  5 cannot-fail shapes four tests that cannot fail, and mutate reports surviving mutants for each

The material lives in the repo's demo/ folder, so the demo needs a clone of the repo (an installed package has no
demo/ folder). Each stage runs in its own temporary folder; git repos there use a neutral identity. Exit 0 only if
every stage gives the expected result, 1 if any does not, 2 on a usage error.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from pbp import heldout, lock_review_tests, mutate, redgreen
from pbp._common import git_env

DEMO_DIR = Path(__file__).resolve().parent.parent / "demo"
USAGE = "usage: python -m pbp demo   (takes no arguments; run it from a clone of the repo)"
SHAPES = (("discount.py", "tests/test_same_call.py", "expected value from the same call"),
          ("totals.py", "tests/test_expected_from_code.py", "expected value computed by the code"),
          ("flags.py", "tests/test_or_true.py", "an `or True` in the assertion"),
          ("table.py", "tests/test_cell_count.py", "only the number of cells is checked"))


class DemoError(Exception):
    pass


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=git_env(),
                       stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise DemoError(f"git {' '.join(args)} failed: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    """Stage everything and commit; a stage whose input changes nothing gets a clear error, not an empty one."""
    _git(repo, "add", "-A")
    if subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--quiet"], env=git_env(),
                      stdin=subprocess.DEVNULL).returncode == 0:
        raise DemoError(f"nothing to commit for {message!r}: the stage's input files are the same as the "
                        "previous commit (was a demo file overwritten?)")
    _git(repo, "commit", "-q", "--no-verify", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _commit_tree(repo: Path, src: Path, message: str) -> str:
    """Make repo's tracked content exactly src's files, commit, return the full sha."""
    for item in repo.iterdir():
        if item.name == ".git":
            continue
        shutil.rmtree(item) if item.is_dir() else item.unlink()
    shutil.copytree(src, repo, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    return _commit(repo, message)


def _new_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "commit.gpgsign", "false")
    return path


def _capture(fn, *args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        rc = fn(*args)
    return rc, out.getvalue()


def _indent(text: str) -> str:
    return "\n".join("    " + line for line in text.rstrip().splitlines())


# ---------------------------------------------------------------- stages

def stage_mutate(work: Path) -> tuple[bool, str]:
    repo = _new_repo(work / "toy")
    _commit_tree(repo, DEMO_DIR / "toy", "toy repo with a vacuous suite")
    args = ["--repo", str(repo), "--module", "inventory.py", "--tests", "tests/test_inventory.py",
            "--timeout", "60"]
    rc1, out1 = _capture(mutate.main, args + ["--json", str(work / "vacuous.json")])
    print("  vacuous suite (demo/toy/tests/test_inventory.py):")
    print(_indent(out1))
    shutil.copyfile(DEMO_DIR / "fixed_tests" / "test_inventory.py", repo / "tests" / "test_inventory.py")
    _commit(repo, "corrected suite")
    rc2, out2 = _capture(mutate.main, args + ["--json", str(work / "fixed.json")])
    print("  corrected suite (demo/fixed_tests/test_inventory.py):")
    print(_indent(out2))
    if rc1 != 0 or rc2 != 0:
        return False, f"mutate exited {rc1} and {rc2} (expected 0 and 0)"
    vac = json.loads((work / "vacuous.json").read_text())
    fix = json.loads((work / "fixed.json").read_text())
    ok = vac["survived"] > 0 and fix["survived"] == 0 and fix["total"] == vac["total"]
    return ok, (f"vacuous suite: {vac['survived']} of {vac['total']} mutants survived; "
                f"corrected suite: {fix['survived']} of {fix['total']} survived")


def stage_redgreen(work: Path) -> tuple[bool, str]:
    base = DEMO_DIR / "redgreen"
    repo = _new_repo(work / "rg")
    pre = _commit_tree(repo, base / "pre", "PRE: the commit the reviewer failed")
    fake = _commit_tree(repo, base / "fake", "FAKE FIX: tests edited until green")
    _git(repo, "checkout", "-q", "--detach", pre)
    real = _commit_tree(repo, base / "real", "REAL FIX: negative quantities refused")
    _git(repo, "checkout", "-q", "--detach", pre)
    rc_fake, out_fake = _capture(redgreen.main, [str(repo), pre, fake])
    print("  fake fix (code unchanged, a test weakened, a vacuous new test):")
    print(_indent(out_fake.replace(str(repo), "<repo>")))
    rc_real, out_real = _capture(redgreen.main, [str(repo), pre, real])
    print("  real fix (positive control):")
    print(_indent(out_real.replace(str(repo), "<repo>")))
    ok = rc_fake == 1 and rc_real == 0
    return ok, f"fake fix -> exit {rc_fake} (FAIL expected); real fix -> exit {rc_real} (PASS expected)"


def _redgreen_repo(work: Path) -> tuple[Path, str, str]:
    base = DEMO_DIR / "redgreen"
    repo = _new_repo(work / "rg")
    pre = _commit_tree(repo, base / "pre", "PRE: the commit the reviewer failed")
    real = _commit_tree(repo, base / "real", "REAL FIX: negative quantities refused")
    return repo, pre, real


def stage_lock_review(work: Path) -> tuple[bool, str]:
    repo, pre, _real = _redgreen_repo(work)
    _git(repo, "checkout", "-q", "--detach", pre)
    review = DEMO_DIR / "lock_review" / "REVIEW.md"
    rc, out = _capture(lock_review_tests.main, [str(review), str(repo), "tests/test_locked.py", "--timeout", "60"])
    print(_indent(out.replace(str(repo), "<tree>")))
    locked_path = repo / "tests" / "test_locked.py"
    locked = locked_path.read_text() if locked_path.exists() else ""
    ok = (rc == 0 and "test_negative_refused" in locked and "test_zero" not in locked
          and out.count("RED ") == 1 and out.count("GREEN ") == 1)
    return ok, f"exit {rc}; locked: {'test_negative_refused only' if ok else 'unexpected set'}"


def _unlock(path: Path) -> None:
    for root, _dirs, names in os.walk(path):
        os.chmod(root, 0o755)
        for n in names:
            os.chmod(os.path.join(root, n), 0o644)


def stage_heldout(work: Path) -> tuple[bool, str]:
    repo, pre, real = _redgreen_repo(work)
    _git(repo, "checkout", "-q", "--detach", pre)
    forged_dir = work / "forged_tree"
    shutil.copytree(DEMO_DIR / "redgreen" / "pre", forged_dir)
    shutil.copyfile(DEMO_DIR / "heldout" / "forged" / "inventory.py", forged_dir / "inventory.py")
    forged = _commit_tree(repo, forged_dir, "FORGED: no fix; fakes a pytest summary and exits 0")
    store = work / "store"
    rc_seal, out_seal = _capture(heldout.main, ["seal", str(DEMO_DIR / "heldout" / "held_tests"), str(store)])
    seal_id = out_seal.strip().splitlines()[-1] if out_seal.strip() else ""
    print(f"  sealed demo/heldout/held_tests -> exit {rc_seal}")
    try:
        results = {}
        for name, commit in (("real fix", real), ("forged pass", forged)):
            out = work / f"out_{name.split()[0]}"
            rc, _text = _capture(heldout.main, ["check", str(store), str(repo), commit, str(out),
                                                "--seal", seal_id, "--timeout", "120"])
            results[name] = rc
            log = (out / "heldout.log").read_text() if (out / "heldout.log").exists() else ""
            why = [ln for ln in log.splitlines() if ln.startswith("[heldout] junit check")]
            print(f"  {name}: check -> exit {rc}" + (f"  ({why[0][len('[heldout] '):]})" if why else ""))
    finally:
        if store.exists():
            _unlock(store)
    ok = rc_seal == 0 and results.get("real fix") == 0 and results.get("forged pass") == 1
    return ok, (f"real fix -> exit {results.get('real fix')} (PASS expected); "
                f"forged pass -> exit {results.get('forged pass')} (FAIL expected)")


def stage_shapes(work: Path) -> tuple[bool, str]:
    repo = _new_repo(work / "shapes")
    _commit_tree(repo, DEMO_DIR / "shapes", "four tests that cannot fail")
    found = []
    for module, tests, shape in SHAPES:
        rc, out = _capture(mutate.main, ["--repo", str(repo), "--module", module, "--tests", tests,
                                         "--timeout", "60", "--json", str(work / f"{module}.json")])
        res = json.loads((work / f"{module}.json").read_text()) if rc == 0 else {"survived": 0, "total": 0}
        print(f"  {tests} ({shape}): exit {rc}, {res['survived']} of {res['total']} mutants survived")
        found.append(rc == 0 and res["survived"] > 0)
    return all(found), f"{sum(found)} of {len(SHAPES)} cannot-fail tests let mutants survive"


STAGES = (
    ("mutate: does the green suite fail on wrong code?", stage_mutate),
    ("redgreen: is a fake fix rejected and a real fix accepted?", stage_redgreen),
    ("lock-review-tests: which review findings have a red test?", stage_lock_review),
    ("heldout: does the fix pass sealed tests, and is a forged pass refused?", stage_heldout),
    ("cannot-fail shapes: does mutate report survivors for each?", stage_shapes),
)


def main(argv: list[str] | None = None) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        print(__doc__.split("\n\n", 2)[1])
        return 0
    if argv:
        print(f"pbp demo: takes no arguments\n{USAGE}", file=sys.stderr)
        return 2
    if not DEMO_DIR.is_dir():
        print(f"pbp demo: demo material not found at {DEMO_DIR}; the demo needs a clone of the repo",
              file=sys.stderr)
        return 2
    if shutil.which("git") is None:
        print("pbp demo: the demo needs git on PATH", file=sys.stderr)
        return 2
    started = time.monotonic()
    results = []
    old = os.environ.get("PYTHONDONTWRITEBYTECODE")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        with tempfile.TemporaryDirectory(prefix="pbp-demo-") as tmp:
            for i, (title, fn) in enumerate(STAGES, 1):
                print(f"[{i}/{len(STAGES)}] {title}")
                work = Path(tmp) / f"stage{i}"
                work.mkdir()
                try:
                    ok, summary = fn(work)
                except (DemoError, OSError, ValueError, KeyError) as exc:
                    ok, summary = False, f"stage crashed: {exc}"
                results.append(ok)
                print(f"  RESULT: {'as expected' if ok else 'NOT as expected'} - {summary}\n")
    finally:
        if old is None:
            os.environ.pop("PYTHONDONTWRITEBYTECODE", None)
        else:
            os.environ["PYTHONDONTWRITEBYTECODE"] = old
    took = time.monotonic() - started
    print(f"demo: {sum(results)}/{len(results)} stages as expected in {took:.1f}s")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
