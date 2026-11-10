"""python -m pbp: the single entry point dispatches every command."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from pbp import __main__ as cli
from pbp import mutate

ROOT = Path(__file__).resolve().parents[1]
COMMANDS = ("mutate", "redgreen", "lock-review-tests", "heldout", "demo")


def test_help_lists_every_command(capsys):
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    for name in COMMANDS:
        assert f"  {name} " in out


def test_no_command_and_unknown_command_are_usage_errors(capsys):
    assert cli.main([]) == 2
    assert cli.main(["nonsense"]) == 2
    assert "pbp: unknown command" in capsys.readouterr().err


def test_module_entry_runs_as_a_program():
    r = subprocess.run([sys.executable, "-B", "-m", "pbp", "--version"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "0.1.0"


def _run(*args, cwd=ROOT):
    return subprocess.run([sys.executable, "-B", "-m", "pbp", *args], cwd=cwd, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL)


def test_every_command_exits_0_on_help_and_2_on_missing_arguments():
    for name in COMMANDS:
        assert _run(name, "--help").returncode == 0, name
        if name != "demo":                           # demo takes no arguments
            assert _run(name).returncode == 2, name


def test_unreadable_inputs_are_usage_errors(tmp_path):
    missing = str(tmp_path / "missing")
    assert _run("lock-review-tests", missing, str(tmp_path), "tests/x.py").returncode == 2
    assert _run("lock-review-tests", missing, missing, "tests/x.py").returncode == 2
    assert _run("heldout", "seal", missing, str(tmp_path / "store")).returncode == 2
    assert _run("heldout", "verify", missing).returncode == 2
    assert _run("demo", "extra-argument").returncode == 2


def test_an_unexpected_error_exits_2_with_one_line(monkeypatch, capsys):
    def boom(argv):
        raise RuntimeError("boom\nsecond line")
    monkeypatch.setattr(mutate, "main", boom)
    assert cli.main(["mutate"]) == 2
    err = capsys.readouterr().err
    assert err == "pbp mutate: unexpected error: RuntimeError: boom\n"


def test_errors_use_the_pbp_command_prefix(tmp_path):
    r = _run("mutate", "--repo", str(tmp_path / "missing"), "--module", "m.py", "--tests", "t.py")
    assert r.returncode == 2 and r.stderr.startswith("pbp mutate: ")
    r = _run("heldout", "verify", str(tmp_path / "missing"))
    assert r.returncode == 2 and r.stderr.startswith("pbp heldout: ")
    r = _run("lock-review-tests", str(tmp_path / "missing"), str(tmp_path), "x.py")
    assert r.returncode == 2 and r.stderr.startswith("pbp lock-review-tests: ")


def test_timeout_is_a_float_everywhere():
    for args in (("mutate", "--repo", ".", "--module", "m.py", "--tests", "t.py"),
                 ("lock-review-tests", "R.md", "T", "o.py"),
                 ("heldout", "check", "S", "T", "C", "O", "--seal", "0" * 64)):
        r = _run(*args, "--timeout", "abc")
        assert r.returncode == 2 and "timeout" in r.stderr, args
        r = _run(*args, "--timeout", "0.5")            # accepted; the run then fails on the missing input
        assert r.returncode == 2 and "invalid" not in r.stderr and "timeout" not in r.stderr, args


def test_the_exit_code_table_is_in_the_top_level_help(capsys):
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    for part in ("0 ok", "1 check failed", "2 usage error or unexpected error", "3 mutate", "4 heldout",
                 "5 heldout"):
        assert part in out
