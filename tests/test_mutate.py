"""mutate: mutant generation and application, and the end-to-end verdict on a weak and a strong suite."""
from __future__ import annotations

import ast
import json
import os

import pytest

from pbp import mutate

SRC = '''\
def is_adult(age):
    if age >= 18:
        return True
    return False


def clamp(x, lo, hi):
    if not lo <= hi:
        raise ValueError("bad range")
    return max(lo, min(x, hi)) and x
'''

MODULE = '''\
def is_adult(age):
    return age >= 18
'''

WEAK = '''\
from mod import is_adult


def test_is_adult():
    assert is_adult(30) or True
'''

STRONG = '''\
from mod import is_adult


def test_is_adult():
    assert is_adult(18) is True
    assert is_adult(17) is False
'''


def kinds(src):
    return [m["kind"] for m in mutate.gen_mutants(ast.parse(src))]


def test_gen_mutants_covers_every_operator_kind():
    found = set(kinds(SRC))
    assert found == {"cmp_flip", "bool_flip", "drop_not", "return_bool", "return_none", "int_inc",
                     "raise_pass", "if_true", "if_false"}


def test_gen_mutants_is_deterministic_and_sorted_by_position():
    a = mutate.gen_mutants(ast.parse(SRC))
    b = mutate.gen_mutants(ast.parse(SRC))
    assert a == b
    keys = [(m["line"], m["col"]) for m in a]
    assert keys == sorted(keys)


def test_none_return_and_bare_return_get_no_return_none_mutant():
    assert kinds("def f():\n    return None\n") == []
    assert kinds("def f():\n    return\n") == []


@pytest.mark.parametrize("src,kind,expected", [
    ("x = a < b\n", "cmp_flip", "x = a >= b"),
    ("x = a == b\n", "cmp_flip", "x = a != b"),
    ("x = a in b\n", "cmp_flip", "x = a not in b"),
    ("x = a and b\n", "bool_flip", "x = a or b"),
    ("x = not a\n", "drop_not", "x = a"),
    ("def f():\n    return True\n", "return_bool", "def f():\n    return False"),
    ("x = 41\n", "int_inc", "x = 42"),
    ("def f():\n    raise E\n", "raise_pass", "def f():\n    pass"),
    ("if a:\n    b()\n", "if_true", "if True:\n    b()"),
    ("if a:\n    b()\n", "if_false", "if False:\n    b()"),
])
def test_apply_mutant_makes_exactly_the_named_change(src, kind, expected):
    tree = ast.parse(src)
    mut = next(m for m in mutate.gen_mutants(tree) if m["kind"] == kind)
    mtree, _old, _new = mutate.apply_mutant(tree, mut)
    assert ast.unparse(ast.fix_missing_locations(mtree)) == expected
    assert ast.unparse(tree) == ast.unparse(ast.parse(src))      # the original tree is untouched


def test_bool_constant_is_not_an_int_mutant():
    assert kinds("x = True\n") == []


def test_spread_is_even_and_bounded():
    items = list(range(10))
    assert mutate.spread(items, 3) == [0, 3, 6]
    assert mutate.spread(items, 10) == items
    assert mutate.spread(items, 50) == items
    assert mutate.spread(items, 0) == []


def test_describe_shows_the_mutated_line():
    src = "def f(a, b):\n    return a < b\n"
    tree = ast.parse(src)
    mut = next(m for m in mutate.gen_mutants(tree) if m["kind"] == "cmp_flip")
    _t, old, new = mutate.apply_mutant(tree, mut)
    assert mutate.describe(mutate.split_lines(src), old, new) == ("return a < b", "return a >= b")


def test_safe_rel_refuses_escapes():
    with pytest.raises(mutate.SetupError):
        mutate.safe_rel("../outside.py")
    with pytest.raises(mutate.SetupError):
        mutate.safe_rel("/abs/path.py")
    assert mutate.safe_rel("pkg/./mod.py") == "pkg/mod.py"


def run_mutate(repo, tmp_path, tests="test_mod.py", extra=()):
    out = tmp_path / "result.json"
    rc = mutate.main(["--repo", str(repo.path), "--module", "mod.py", "--tests", tests,
                      "--timeout", "60", "--json", str(out), *extra])
    return rc, (json.loads(out.read_text()) if out.exists() else None)


def test_weak_suite_has_survivors_strong_suite_kills_all(make_repo, tmp_path, capsys):
    repo = make_repo()
    repo.commit({"mod.py": MODULE, "test_mod.py": WEAK})
    rc, weak = run_mutate(repo, tmp_path)
    assert rc == 0
    assert weak["total"] == 3                       # cmp_flip, return_none and 18 -> 19 on `return age >= 18`
    assert weak["survived"] == 3 and weak["score"] == 0.0
    assert "survivors:" in capsys.readouterr().out
    repo.commit({"test_mod.py": STRONG})
    rc, strong = run_mutate(repo, tmp_path)
    assert rc == 0
    assert strong["survived"] == 0 and strong["killed"] == 3 and strong["score"] == 100.0
    assert [m["status"] for m in strong["mutants"]] == ["killed", "killed", "killed"]


def test_the_source_repo_is_never_modified(make_repo, tmp_path):
    repo = make_repo()
    repo.commit({"mod.py": MODULE, "test_mod.py": WEAK})
    before = (repo.path / "mod.py").read_bytes()
    run_mutate(repo, tmp_path)
    assert (repo.path / "mod.py").read_bytes() == before
    assert repo.git("status", "--porcelain") == ""


def test_baseline_red_exits_3_and_runs_no_mutants(make_repo, tmp_path, capsys):
    repo = make_repo()
    repo.commit({"mod.py": MODULE, "test_mod.py": "from mod import is_adult\n\n\ndef test_x():\n"
                                                  "    assert is_adult(1)\n"})
    rc, result = run_mutate(repo, tmp_path)
    assert rc == mutate.EXIT_BASELINE_RED
    assert result is None
    assert "baseline red" in capsys.readouterr().err


def test_untracked_module_is_a_setup_error(make_repo, tmp_path):
    repo = make_repo()
    repo.commit({"test_mod.py": WEAK})
    (repo.path / "mod.py").write_text(MODULE)                 # present, but not tracked
    rc, _ = run_mutate(repo, tmp_path)
    assert rc == mutate.EXIT_USAGE


def test_max_limits_the_number_of_mutants_run(make_repo, tmp_path):
    repo = make_repo()
    repo.commit({"mod.py": MODULE, "test_mod.py": STRONG})
    rc, result = run_mutate(repo, tmp_path, extra=("--max", "1"))
    assert rc == 0
    assert result["generated"] == 3 and result["total"] == 1


def test_fail_under_gates_the_score(make_repo, tmp_path, capsys):
    repo = make_repo()
    repo.commit({"mod.py": MODULE, "test_mod.py": WEAK})
    rc, weak = run_mutate(repo, tmp_path, extra=("--fail-under", "50"))
    assert rc == mutate.EXIT_UNDER and rc == 1
    assert weak["score"] == 0.0
    assert "score 0.0% is below --fail-under 50" in capsys.readouterr().err
    repo.commit({"test_mod.py": STRONG})
    rc, _ = run_mutate(repo, tmp_path, extra=("--fail-under", "100"))
    assert rc == 0


@pytest.mark.parametrize("bad", ["-1", "101", "nan"])
def test_fail_under_out_of_range_is_usage(bad):
    with pytest.raises(SystemExit) as exc:
        mutate.parse_args(["--repo", ".", "--module", "m.py", "--tests", "t.py", "--fail-under", bad])
    assert exc.value.code == 2


MARKED = ('''def is_adult(age):\n    return age >= 18\n\n\n'''
          '''def marked():\n    return ("#" + " keep") in open(__file__).read()  # keep\n''')
MARKED_TEST = ("from mod import is_adult, marked\n\n\ndef test_x():\n    assert marked()\n"
               "    assert is_adult(18) is True\n")


def test_unparse_only_change_is_a_baseline_failure_not_kills(make_repo, tmp_path, capsys):
    repo = make_repo()
    repo.commit({"mod.py": MARKED, "test_mod.py": MARKED_TEST})
    rc, result = run_mutate(repo, tmp_path)
    assert rc == mutate.EXIT_BASELINE_RED
    assert result is None
    assert "fail on its ast.unparse form" in capsys.readouterr().err


def test_text_report_says_when_mutants_were_sampled(make_repo, tmp_path, capsys):
    repo = make_repo()
    repo.commit({"mod.py": MODULE, "test_mod.py": STRONG})
    rc, result = run_mutate(repo, tmp_path, extra=("--max", "1"))
    assert rc == 0
    assert result["sampled"] is True
    out = capsys.readouterr().out
    assert "mutants: total=1 killed=1 survived=0 timeouts=0 score=100.0%" in out
    assert "sampled 1 of 3 generated mutants (--max 1)" in out
    rc, _ = run_mutate(repo, tmp_path)
    assert "sampled" not in capsys.readouterr().out


SPIN = "def spin(stop):\n    while not stop:\n        pass\n    return 1\n"
SPIN_TEST = "from mod import spin\n\n\ndef test_spin():\n    assert spin(True) == 1\n"


def test_timeouts_are_counted_and_shown(make_repo, tmp_path, capsys):
    repo = make_repo()
    repo.commit({"mod.py": SPIN, "test_mod.py": SPIN_TEST})
    out = tmp_path / "r.json"
    rc = mutate.main(["--repo", str(repo.path), "--module", "mod.py", "--tests", "test_mod.py",
                      "--timeout", "3", "--json", str(out)])
    assert rc == 0
    result = json.loads(out.read_text())
    assert result["timeouts"] == 1 and result["killed"] == 3 and result["survived"] == 0
    assert "timeouts=1" in capsys.readouterr().out


def test_a_tracked_symlink_is_copied_by_content_never_written_through(make_repo, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    real = outside / "real_mod.py"
    real.write_text(MODULE)
    os.chmod(real, 0o644)
    before = os.stat(real)
    repo = make_repo()
    os.symlink(real, repo.path / "mod.py")                     # absolute target outside the repo
    repo.commit({"test_mod.py": STRONG})
    rc, result = run_mutate(repo, tmp_path)
    assert rc == 0 and result["killed"] == 3
    after = os.stat(real)
    assert (after.st_mtime_ns, after.st_ino) == (before.st_mtime_ns, before.st_ino)
    assert real.read_text() == MODULE


def test_a_tracked_symlink_to_a_directory_is_refused(make_repo, tmp_path, capsys):
    target = tmp_path / "somedir"
    target.mkdir()
    repo = make_repo()
    os.symlink(target, repo.path / "linkdir")
    repo.commit({"mod.py": MODULE, "test_mod.py": STRONG})
    rc, _ = run_mutate(repo, tmp_path)
    assert rc == mutate.EXIT_USAGE
    assert "tracked symlink" in capsys.readouterr().err


def test_zero_mutants_fail_any_fail_under_even_0(make_repo, tmp_path, capsys):
    repo = make_repo()
    repo.commit({"mod.py": 'NAME = "pbp"\n', "test_mod.py": "from mod import NAME\n\n\ndef test_name():\n"
                                                             "    assert NAME\n"})
    rc, result = run_mutate(repo, tmp_path, extra=("--fail-under", "0"))
    assert result["total"] == 0 and result["score"] is None
    assert rc == mutate.EXIT_UNDER
    rc, _ = run_mutate(repo, tmp_path)
    assert rc == mutate.EXIT_OK


PARTIAL = '''\
from mod import is_adult


def test_is_adult():
    assert is_adult(30)
'''


def test_a_partly_killing_suite_scores_to_one_decimal_and_the_json_file_is_optional(make_repo, tmp_path, capsys):
    repo = make_repo()
    repo.commit({"mod.py": MODULE, "test_mod.py": PARTIAL})
    rc, result = run_mutate(repo, tmp_path)
    assert rc == 0
    assert (result["killed"], result["survived"], result["score"]) == (2, 1, 66.7)
    capsys.readouterr()
    assert mutate.main(["--repo", str(repo.path), "--module", "mod.py", "--tests", "test_mod.py",
                        "--timeout", "60"]) == 0
    assert capsys.readouterr().out.startswith("mutants: total=3 killed=2 survived=1 timeouts=0 score=66.7%")


def test_baseline_red_exits_3_and_shows_why_and_the_pytest_tail(make_repo, tmp_path, capsys):
    repo = make_repo()
    repo.commit({"mod.py": MODULE, "test_mod.py": "from mod import is_adult\n\n\ndef test_x():\n"
                                                  "    assert is_adult(1)\n"})
    rc, _ = run_mutate(repo, tmp_path)
    assert rc == 3                                       # the documented exit code
    err = capsys.readouterr().err
    assert "baseline red (exit 1)" in err
    assert "1 failed" in err


def _setup_case(case, repo, tmp_path):
    if case == "repo is not a folder":
        return ["--repo", str(tmp_path / "nope"), "--module", "mod.py", "--tests", "test_mod.py"], "not a directory"
    if case == "repo is not a git repository":
        plain = tmp_path / "plain"
        plain.mkdir()
        return ["--repo", str(plain), "--module", "mod.py", "--tests", "test_mod.py"], "git ls-files failed"
    if case == "test file not tracked":
        repo.commit({"mod.py": MODULE})
        return ["--repo", str(repo.path), "--module", "mod.py", "--tests", "test_mod.py"], "test is not a tracked file"
    if case == "module not utf-8":
        (repo.path / "mod.py").write_bytes(b"x = '\xff'\n")
        repo.commit({"test_mod.py": STRONG})
        return ["--repo", str(repo.path), "--module", "mod.py", "--tests", "test_mod.py"], "module is not UTF-8"
    repo.commit({"mod.py": "def is_adult(age:\n", "test_mod.py": STRONG})
    return ["--repo", str(repo.path), "--module", "mod.py", "--tests", "test_mod.py"], "cannot parse module"


@pytest.mark.parametrize("case", ["repo is not a folder", "repo is not a git repository", "test file not tracked",
                                  "module not utf-8", "module does not parse"])
def test_setup_problems_exit_2_with_the_reason(make_repo, tmp_path, capsys, case):
    args, reason = _setup_case(case, make_repo(), tmp_path)
    assert mutate.main(args + ["--timeout", "60"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("pbp mutate: ") and reason in err


@pytest.mark.parametrize("extra", [["--max", "-1"], ["--timeout", "0"], ["--timeout", "-5"]])
def test_a_negative_max_or_a_non_positive_timeout_is_usage(extra):
    with pytest.raises(SystemExit) as exc:
        mutate.parse_args(["--repo", ".", "--module", "m.py", "--tests", "t.py", *extra])
    assert exc.value.code == 2


def test_max_0_is_allowed():
    assert mutate.parse_args(["--repo", ".", "--module", "m.py", "--tests", "t.py", "--max", "0"]).max == 0


def test_a_report_without_survivors_has_no_survivors_heading():
    text = mutate.format_report({"total": 2, "killed": 2, "survived": 0, "timeouts": 0, "score": 100.0,
                                 "sampled": False, "generated": 2, "survivors": []})
    assert text == "mutants: total=2 killed=2 survived=0 timeouts=0 score=100.0%"


def test_replacing_a_child_that_is_not_there_raises():
    tree = ast.parse("x = 1\n")
    with pytest.raises(RuntimeError, match="child not found"):
        mutate._replace_child(tree.body[0], ast.Constant(2), ast.Constant(3))


def test_the_module_runs_as_a_script():
    import subprocess
    import sys
    from pathlib import Path
    root = Path(mutate.__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, "-B", "-m", "pbp.mutate"], cwd=root, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)
    assert r.returncode == 2
    assert "--repo" in r.stderr


def test_score_without_timeouts_is_reported_and_gates_fail_under(make_repo, tmp_path, capsys):
    spin = "def spin(stop):\n    while not stop:\n        pass\n    return 1\n"
    spin_test = "from mod import spin\n\n\ndef test_spin():\n    assert spin(True) == 1\n"
    repo = make_repo()
    repo.commit({"mod.py": spin, "test_mod.py": spin_test})
    out = tmp_path / "r.json"
    args = ["--repo", str(repo.path), "--module", "mod.py", "--tests", "test_mod.py",
            "--timeout", "3", "--json", str(out)]
    assert mutate.main(args) == 0
    result = json.loads(out.read_text())
    assert "score_without_timeouts" in result
    assert result["score_without_timeouts"] == 66.7
    assert (result["total"], result["killed"], result["survived"], result["timeouts"]) == (3, 3, 0, 1)
    assert result["score"] == 100.0
    assert "score=100.0% score_without_timeouts=66.7%" in capsys.readouterr().out
    rc = mutate.main(args + ["--fail-under", "80"])          # lenient 100.0 would pass, strict 66.7 must not
    assert rc == 1
    assert "score_without_timeouts 66.7% is below --fail-under 80" in capsys.readouterr().err
    assert mutate.main(args + ["--fail-under", "60"]) == 0


def test_note_when_no_fail_under_is_given(make_repo, tmp_path, capsys):
    repo = make_repo()
    repo.commit({"mod.py": MODULE, "test_mod.py": STRONG})
    note = "note: no --fail-under given"
    rc, _ = run_mutate(repo, tmp_path)
    err = capsys.readouterr().err
    assert rc == 0
    assert err.count(note) == 1 and "pass --fail-under PCT in CI" in err
    rc, _ = run_mutate(repo, tmp_path, extra=("--fail-under", "50"))
    assert rc == 0
    assert note not in capsys.readouterr().err
