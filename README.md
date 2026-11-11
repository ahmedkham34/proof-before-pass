[![ci](https://github.com/ahmedkham34/proof-before-pass/actions/workflows/ci.yml/badge.svg)](https://github.com/ahmedkham34/proof-before-pass/actions/workflows/ci.yml)

# pbp

pbp (proof-before-pass) checks whether a passing test suite could fail: it mutates a module and counts the mutants
the tests miss, re-runs new tests on the commit before a fix, re-runs a review's tests and keeps the ones that fail,
and runs sealed tests the author never saw. Python standard library only; Linux and macOS.

Before and after: this test passes whatever list `low_stock` returns.

```
def test_low_stock():
    result = low_stock([1, 9], 5)
    assert all(x or True for x in result)   # shape 3: `or True` makes every item pass
```
```
$ python3 -m pbp mutate --repo . --module flags.py --tests tests/test_or_true.py
mutants: total=2 killed=1 survived=1 timeouts=0 score=50.0% score_without_timeouts=50.0%
survivors:
2:12 cmp_flip  return [level < threshold for level in levels] -> return [level >= threshold for level in levels]
```

(Run inside `demo/shapes` committed as a git repository of its own; stage 5 of `python3 -m pbp demo` runs the same
check and prints only the counts. The killed mutant is `return None`: iterating `None` fails the test.)

## Quickstart

```
git clone https://github.com/ahmedkham34/proof-before-pass.git pbp && cd pbp
python3 -m venv .venv
. .venv/bin/activate
pip install pytest      # the code under test runs with pytest; the tools use no packages
python3 -m pbp demo
```

A virtual environment is needed because a plain `pip install` fails on stock Debian, Ubuntu and Homebrew Pythons
(`externally-managed-environment`). Debian and Ubuntu may first need `sudo apt install python3-venv`.

The demo needs a clone: its material is in the `demo/` folder, which an installed package does not have. Last
lines of a real run:

```
[5/5] cannot-fail shapes: does mutate report survivors for each?
  tests/test_same_call.py (expected value from the same call): exit 0, 7 of 7 mutants survived
  tests/test_expected_from_code.py (expected value computed by the code): exit 0, 5 of 7 mutants survived
  tests/test_or_true.py (an `or True` in the assertion): exit 0, 1 of 2 mutants survived
  tests/test_cell_count.py (only the number of cells is checked): exit 0, 2 of 3 mutants survived
  RESULT: as expected - 4 of 4 cannot-fail tests let mutants survive

demo: 5/5 stages as expected in 14.5s
```

(The time varies by machine.)

Warning: every tool runs the code under test (and a review's test code) with your user rights. Use a throwaway
copy or a container for code you do not trust.

Where to start: run `mutate` on the module you care most about. It needs no second commit, no review and no
sealed tests. Requirements: Python 3.11 or newer, `git` and `pytest`. CI runs 3.11 to 3.14.

## On your own repository

Clone pbp once, then run it from your repository with `PYTHONPATH` pointing at the clone. Nothing of pbp is installed.

```
git clone https://github.com/ahmedkham34/proof-before-pass "$HOME/proof-before-pass"
cd your-repo
python3 -m venv .venv
. .venv/bin/activate
pip install pytest
PYTHONPATH="$HOME/proof-before-pass" python3 -m pbp mutate --repo . --module yourmodule.py --tests tests/test_yourmodule.py
```

- Run pbp with the Python that has pytest and your project's own dependencies (use your project's venv if it has
  one): `mutate` runs your tests with that same Python.
- Keep `.venv` out of git (put `.venv/` in `.gitignore`): `mutate` stops on a tracked virtual environment.
- Commit the module and the test file first. `mutate` reads git-tracked files only; an uncommitted file stops with
  `pbp mutate: test is not a tracked file: ...` (exit 2).
- `pip install "$HOME/proof-before-pass"` also gives a `pbp` command, but `pbp demo` then fails (an installed package
  has no `demo/` folder). For the demo, use `pip install -e "$HOME/proof-before-pass"` or run it from the clone.
- `mutate` scores a test file, not one test. A test that cannot fail is hidden when an honest test in the same file
  kills the mutants. To judge one test, put it in its own file. `--tests` takes a file path, not a pytest node id
  (`tests/test_x.py::test_name` stops with `test is not a tracked file`).

## mutate: can the tests fail?

Copies the git-tracked files to a temp folder, checks the tests pass, then changes one thing in one module at a time
(a flipped comparison, `and`/`or` swapped, a dropped `not`, a flipped boolean return, `return None`, an integer + 1,
`raise` replaced by `pass`, an `if` forced true or false) and re-runs the tests. A mutant the tests still pass on is
a survivor: a wrong program the suite accepts.

```
python3 -m pbp mutate --repo . --module inventory.py --tests tests/test_inventory.py
```

On the demo's vacuous suite (`demo/toy`), as printed (indented) by stage 1 of `python3 -m pbp demo`:

```
mutants: total=10 killed=4 survived=6 timeouts=0 score=40.0% score_without_timeouts=40.0%
survivors:
6:4 return_none  return stock < threshold -> return None
6:11 cmp_flip  return stock < threshold -> return stock >= threshold
12:7 if_false  if value < 0: -> if False:
12:15 int_inc  if value < 0: -> if value < 1:
13:8 raise_pass  raise ValueError("negative quantity") -> pass
19:43 cmp_flip  return [name for name, qty in items if qty < threshold] -> return [name for name, qty in items if qty >= threshold]
note: no --fail-under given, so the exit code says nothing about the score; pass --fail-under PCT in CI
```

With `demo/fixed_tests/test_inventory.py` in place and `--fail-under 100` (stage 1 runs it without the flag):

```
mutants: total=10 killed=10 survived=0 timeouts=0 score=100.0% score_without_timeouts=100.0%
```

Options: `--max N` (default 150, evenly spread; the report says "sampled N of M" when it cut), `--timeout S`
(per run, default 60), `--json FILE`, `--fail-under PCT`. `--fail-under` is judged on `score_without_timeouts`.
Without it the run exits 0 whatever the score, and a `note:` line on stderr says so; pass it in CI.

Exit codes: 0 baseline green and `score_without_timeouts` at or above `--fail-under` (or no `--fail-under`); 1 below `--fail-under`, or no mutants at
all when `--fail-under` is given (even `--fail-under 0`); 2 usage or setup error; 3 the tests fail before any
mutation (including on the module as rewritten by `ast.unparse`).

Limits: a small fixed set of operators, one module per run. A mutant that runs past `--timeout` counts as killed
in `killed` and `score`, so a slow machine or a short timeout can raise them. `score_without_timeouts` leaves those
mutants out of the kills; read it, and the `timeouts=` count. A test that checks the
module's source text (a hash, a line count) kills every mutant, so the score means nothing for such a suite.
Survivors can be equivalent mutants (a change that does not alter behaviour); read each one.

## redgreen: do the new tests fail before the change and pass after it, with the existing tests untouched?

Runs the new tests on the commit before the change and the whole suite on the commit after it, and compares every
test that already existed. PASS only when all three hold:

- RED: at least one new test fails on the old code with an assertion failure. One that fails there only with another
  error (`AttributeError`, `NameError`, an import or collection error) is RED_UNPROVEN, and any RED_UNPROVEN test
  fails the verdict by default, even when another new test is properly red. So a real fix that adds a new function
  and a test for it (the test fails at PRE with `NameError` or `ImportError`, because the name does not exist yet)
  fails unless you pass `--allow-error-red`.
- GREEN: the whole suite passes on the new commit, and every new test passes (a skip is not a pass).
- KEPT: no existing test, helper, conftest or pytest setting changed (tests are compared by their AST). A new
  module-level helper or constant in an existing test file (for example `CASES = [...]` for a new parametrized
  test) is flagged too; put it inside the test or in a new test file. And no new test code patches other code at
  import time in a form the AST check knows: an assignment, deletion, `for` or `with` target on another object's
  attribute or item (`m.x`, `vars(m)[k]`, `m.__dict__[k]`), a method call on `vars(...)` or `.__dict__`, a call to
  `setattr`, `delattr`, `exec`, `eval`, anything named `*patch*` or `*.start()`, or a `pytest_plugins` assignment.
  This covers a whole new test file and, in an existing test file, the decorators, default values and annotations
  of each new test and the decorators, bases and keywords of each new test class.

On `demo/redgreen` (usage: `python3 -m pbp redgreen TREE PRE POST`). Stage 2 of `python3 -m pbp demo` commits the
folders `demo/redgreen/pre`, then `fake` and `real` each on top of `pre`, and prints these verdict lines indented;
`pre`, `fake` and `real` below stand for those three commits. The fake fix leaves the code as it was, weakens an
existing test and adds a new test that passes either way:

```
$ python3 -m pbp redgreen . pre fake
FAIL new=1 red_new=0 green=suite
 - KEPT: tests/test_inventory.py::test_parse_qty_reads_a_number changed
 - RED: no new test fails with an assertion on the unfixed code; the proof proves nothing
$ python3 -m pbp redgreen . pre real
PASS new=1 red_new=1 green=suite
```

Options:

- `--tests FILES`: test files to run for RED, relative to TREE (default: the test files that hold new tests)
- `--out FILE.json`: write a JSON report to this path, relative to TREE
- `--refuse-under DIR`: refuse to run when TREE lies under DIR (repeatable)
- `--allow-error-red`: count a new test that fails at PRE with a non-assertion error or a collection error as red
- `--green-only-changed`: at POST run only the test files that hold new tests; the summary then says `green=changed`
- `--timeout S`: seconds per pytest run, after which the whole process group is killed (default 1800)

Exit codes: 0 PASS; 1 a check failed (a timeout included); 2 usage error (bad arguments, an unknown commit, TREE not
a git repository, a path outside TREE, TREE under a `--refuse-under` folder, a symlink in PRE that would take a POST
test file out of the PRE worktree) or an unexpected error; 128+N stopped by signal N (the pytest process group is
killed and the worktrees are removed first).

What it does not check:

- whether the change is a correct or complete fix: it only shows the new tests went from red to green;
- patching or stubbing inside a new test body or fixture (monkeypatch, mock.patch as a context manager or decorator,
  assigning to the module under test);
- import-time patching in any form other than the direct ones listed above. The check reads syntax and does not
  follow values, so these are known to get through: a helper function the new test file defines and calls at import
  (also as a decorator), a metaclass, an alias of the module's namespace (`ns = mod.__dict__; ns["f"] = ...`),
  `object.__setattr__`, `dict.__setitem__` or operator.setitem, `map` over `__setitem__`,
  getattr(builtins, "setattr"), importlib, or code imported from another module. Read a PASS as "none of the
  common shortcuts was found", not as proof that no test cheats;
- a new test that asserts something trivially true only after the change (for example a new constant the change
  adds, or `try: ...; except AttributeError: assert False`);
- changes to non-Python fixtures or data files, and to plugins installed in the environment or imported from outside
  the test roots;
- tests generated at run time (pytest_generate_tests output, tests built in loops or by exec); only test functions and
  methods written as `def` are compared;
- it runs the code under test and the tests at PRE and POST, so use it only on code you would run anyway.

## lock-review-tests: which review findings have a red test?

Takes every ```` ```python ```` block in a review that defines a top-level `test_` function, and runs each test
function on its own against the code. Red tests are written to a new file; green and broken ones stay claims.

```
python3 -m pbp lock-review-tests REVIEW.md app tests/test_locked.py
```

On `demo/lock_review/REVIEW.md` and the commit before the fix. Stage 3 of `python3 -m pbp demo` runs this on a
checkout of `demo/redgreen/pre` and prints that folder as `<tree>`; here the checkout is called `app` and the
command runs from its parent folder:

```
RED     finding 1 BLOCKER    test_negative_refused  (1 failed in 0.01s)
GREEN   finding 2 MAJOR      test_zero  (1 passed in 0.00s)
wrote 1 red test(s) to app/tests/test_locked.py
```

Run again, it refuses to overwrite the file (exit 2):
`pbp lock-review-tests: OUT already exists, refusing to overwrite it: tests/test_locked.py`

What counts: RED is a failure at run time. An `AttributeError` or `TypeError` inside a test counts as RED (the code
may lack an attribute or take other arguments). A `NameError`, an import or syntax error, a missing fixture, a
collection error or any pytest exit other than 0 or 1 is BROKEN and never locked. A test that runs past `--timeout`
is TIMEOUT and never locked. Every test function in a block is judged; only the red ones are written out.

A code block with no test is put before every test only when its first line is `# helper`. Other blocks (for
example a quote of the code under review) are ignored, so a review cannot replace the code its tests run against.
OUT and `--pytest-root` are paths relative to the tree and must stay inside it.

Exit codes: 0 at least one red test locked; 1 no red test, or no test blocks; 2 usage error (unreadable review,
tree not a folder, OUT or `--pytest-root` outside the tree, OUT already exists).

Limits: red is necessary, not sufficient. `assert False` is red too, and so is a test that asks for a name a later
commit merely adds; pair this with redgreen. Probe files are written into the tree while the tests run.

## heldout: does the change pass tests its author never saw?

`seal` copies a folder of tests into a read-only store with a sha256 manifest and prints the seal id; keep the seal
id where the author cannot write. `verify` checks the store still matches. `check` re-verifies, extracts the commit
with `git archive` (never the working tree), runs the sealed tests from outside the extracted tree, and passes only
when pytest exits 0, the final summary line is clean, and a junit result file lists every sealed test as passed.

```
python3 -m pbp heldout seal held_tests store
python3 -m pbp heldout check store app <commit> out_real --seal <seal id>
```

Sealed test files find the code through the `HELDOUT_TREE` environment variable, which `check` sets to the extracted
tree. A test written like a normal test (`from inventory import parse_qty`) fails with `ModuleNotFoundError`. The
demo's held test (`demo/heldout/held_tests/test_parse_qty.py`) reads the tree path like this:

```
sys.path.insert(0, os.environ["HELDOUT_TREE"])
from inventory import parse_qty  # noqa: E402
```

`seal` printed `fe123c69526b21617be4f4fa9ca3a1671f7d60242972553f0f7e7977c3d4ed5f` for `demo/heldout/held_tests`
(stage 4 of `python3 -m pbp demo` seals the same folder and checks the real fix and the forged candidate).

`check` prints nothing on success; it writes `heldout.rc`, `heldout.log`, `heldout.commit` and `heldout.seal` to
OUT. The real fix exits 0 and its log ends with `2 passed in 0.01s`. The demo's forged candidate appends that same
line to the log and exits 0 at import; it exits 1 and its log ends with:

```
[heldout] junit check: no junit result (FileNotFoundError): pytest did not finish its run
```

Options: `--timeout S` (default 900), `--inject FILE` and `--inject-as REL` (one extra file put in the extracted
tree; if a symlinked folder in the commit would take it outside the tree, the check is refused with exit 5).

Exit codes: 0 passed; 1 failed; 2 usage error, unreadable input folder, unknown commit, a TREE that `git archive`
cannot extract, or an unexpected error (a crash during `check` also writes 2 to `heldout.rc`, never a pass); 4 the
store does not match its seal; 5 refused: a malformed `--seal` id (`verify` and `check`), or candidate content that
would put the injected file outside the extracted tree.

Limits: this is not a sandbox, and the result can be forged. Any code running in the test process can forge the
result, for example by finding the junit file and rewriting it at exit. The seal protects against the tests being
read or tuned to by the author; it does not protect against a hostile candidate. Run untrusted code in a throwaway
container or VM. The read-only store stops mistakes, not the same user, who can change the permissions back.

## Exit codes

| code | meaning |
|---|---|
| 0 | the check ran and passed (`--help` exits 0 for every command) |
| 1 | the check ran and failed: `score_without_timeouts` under `--fail-under` or no mutants with it, a redgreen FAIL, no red test locked, a failed held-out check, a demo stage not as expected |
| 2 | usage error, unreadable input, unknown commit, a path outside the tree, an existing OUT file, or an unexpected error (one line, no traceback) |
| 3 | `mutate`: the tests fail before any mutation |
| 4 | `heldout verify` and `heldout check`: the store does not match its seal |
| 5 | `heldout`: a malformed `--seal` id, or candidate content that would put the injected file outside the extracted tree |
| 128+N | `mutate`, `redgreen`, `heldout`, `lock-review-tests`: stopped by signal N (SIGTERM, SIGHUP; Ctrl-C gives 130); children and temp folders are removed first |

## Limits

- Each tool runs the code under test with your user rights. None is a sandbox.
- A score or a red test is evidence about one suite on one machine, not a proof that the code is right.
- Each tool's own limits are listed in its section above.
- Python only, pytest only, git required. CI runs the tests, the demo and the falsifiers on Linux, and the tests and the demo on macOS.

## Evidence

Everything below can be re-run from a clone.

1. mutate on four of its own modules, each against its own test file plus `tests/test_cli.py`, with
`--timeout 60` and `--max 200` (`--timeout 180` and `--max 120` for redgreen, whose tests are the slowest)
(`make selftest`):

| module | tests | mutants | killed | survived | timeouts | score | sampled |
|---|---|---|---|---|---|---|---|
| pbp/mutate.py | tests/test_mutate.py, tests/test_cli.py | 200 | 179 | 21 | 0 | 89.5% | 200 of 226 |
| pbp/heldout.py | tests/test_heldout.py, tests/test_cli.py | 200 | 176 | 24 | 0 | 88.0% | 200 of 318 |
| pbp/lock_review_tests.py | tests/test_lock_review_tests.py, tests/test_cli.py | 101 | 89 | 12 | 0 | 88.1% | all 101 |
| pbp/redgreen.py | tests/test_redgreen.py, tests/test_cli.py | 120 | 110 | 10 | 1 | 91.7% | 120 of 361 |

The mutate.py and heldout.py rows were re-run for this version (no timeouts, so `score_without_timeouts` equals
`score`); the other two rows were not re-run.

2. Four shapes of a test that cannot fail (`demo/shapes/`; also stage 5 of the demo and
`tests/test_cannot_fail_shapes.py`). Each test passes; mutate shows why that means little:

| shape | the test | mutants | killed | survived | score |
|---|---|---|---|---|---|
| 1 both sides run the code | `assert discount(150) == discount(150)` | 7 | 0 | 7 | 0.0% |
| 2 the expected value comes from the code | `expected = line_total(12, 3) + line_total(1, 4)` | 7 | 2 | 5 | 28.6% |
| 3 `or True` in the assertion | `assert all(x or True for x in result)` | 2 | 1 | 1 | 50.0% |
| 4 only the number of cells is checked | `assert sum(len(r) for r in rows) == 4` | 3 | 1 | 2 | 33.3% |

3. `tools/falsify.py` breaks each guard on purpose and checks that a named test goes red; the table from one full
run is in `docs/FALSIFIERS.md`.

## Related work

mutmut, cosmic-ray and mutatest are fuller mutation testing tools, with more operators, caching and reports. Use
them for real mutation testing. `pbp mutate` is a small dependency-free check that a suite can fail at all.

## Development

```
python3 -m venv ../pbp-venv               # outside the clone, so the privacy gate does not scan it
. ../pbp-venv/bin/activate
pip install pytest
python3 -m pytest -q                       # the test suite (make test)
python3 -m pbp demo                        # the demo (make demo)
python3 tools/falsify.py                   # every falsifier; --markdown prints the docs table (make falsify)
python3 tools/privacy_gate.py . --git-log  # personal or machine data in files and history (make privacy)
make selftest                              # the Evidence table above (slow)
```

Every git commit made by the tests and the demo sets its own author and committer, so a machine with no git
identity works. `pbp/_common.py` holds the shared process-group runner and git identity.

Licence: MIT, see LICENSE.
