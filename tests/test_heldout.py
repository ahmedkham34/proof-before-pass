"""heldout: seal, verify, strict summary reading, and check against a commit (never the working tree)."""
from __future__ import annotations

import hashlib
import os
import stat
import warnings
from pathlib import Path

import pytest

from pbp import heldout

CHECK = '''\
import os
import sys

sys.path.insert(0, os.environ["HELDOUT_TREE"])
from mod import double  # noqa: E402


def test_double():
    assert double(2) == 4
    assert double(-3) == -6
'''

GOOD = "def double(x):\n    return 2 * x\n"
BAD = "def double(x):\n    return x + x if x > 0 else 0\n"


@pytest.mark.parametrize("line,ok", [
    ("3 passed in 0.12s", True),
    ("============ 3 passed in 0.12s ============", True),
    ("2 passed, 1 skipped in 0.10s", True),
    ("5 passed in 61.00s (0:01:01)", True),
    ("1 failed, 2 passed in 0.20s", False),
    ("2 passed, 1 error in 0.20s", False),
    ("2 passed, 1 xpassed in 0.20s", False),
    ("no tests ran in 0.01s", False),
    ("1 skipped in 0.01s", False),
    ("3 passed", False),
    ("", False),
])
def test_summary_ok_reads_only_a_clean_final_line(line, ok):
    assert heldout.summary_ok("collected 3 items\n...\n" + line + "\n") is ok


def test_summary_ok_uses_the_last_line_only():
    assert heldout.summary_ok("3 passed in 0.1s\nsomething printed after\n") is False


def make_src(tmp_path, files=None):
    src = tmp_path / "src"
    src.mkdir()
    for rel, text in (files or {"test_double.py": CHECK}).items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return src


def seal(tmp_path, capsys, files=None):
    src = make_src(tmp_path, files)
    store = tmp_path / "store"
    assert heldout.main(["seal", str(src), str(store)]) == 0
    seal_id = capsys.readouterr().out.strip()
    return src, store, seal_id


def unlock(store):
    for root, dirs, names in os.walk(store):
        os.chmod(root, 0o755)
        for n in names:
            os.chmod(os.path.join(root, n), 0o644)


def test_seal_makes_a_read_only_store_whose_id_is_the_manifest_hash(tmp_path, capsys):
    _src, store, seal_id = seal(tmp_path, capsys, {"test_double.py": CHECK, "sub/data.txt": "x\n"})
    assert seal_id == hashlib.sha256((store / heldout.MANIFEST).read_bytes()).hexdigest()
    manifest = (store / heldout.MANIFEST).read_text().splitlines()
    assert [ln.split("  ")[1] for ln in manifest] == ["sub/data.txt", "test_double.py"]
    assert stat.S_IMODE(os.stat(store / "test_double.py").st_mode) == 0o444
    assert stat.S_IMODE(os.stat(store).st_mode) == 0o555
    assert heldout.main(["verify", str(store), "--seal", seal_id]) == 0
    unlock(store)


def test_verify_reports_changed_extra_and_wrong_seal(tmp_path, capsys):
    _src, store, seal_id = seal(tmp_path, capsys)
    unlock(store)
    (store / "test_double.py").write_text(CHECK.replace("== 4", "== 4 or True"))
    (store / "extra.py").write_text("x = 1\n")
    assert heldout.main(["verify", str(store)]) == heldout.EXIT_MISMATCH
    out = capsys.readouterr().out
    assert "changed test_double.py" in out and "extra extra.py" in out


def test_verify_with_a_malformed_seal_id_exits_5(tmp_path, capsys):
    _src, store, seal_id = seal(tmp_path, capsys)
    try:
        for bad in ("abc", seal_id.upper(), seal_id + "0", ""):
            assert heldout.main(["verify", str(store), "--seal", bad]) == heldout.EXIT_REFUSED, bad
            assert "pbp heldout: refused: --seal must be the 64-hex seal id" in capsys.readouterr().err
        assert heldout.main(["verify", str(store), "--seal", seal_id]) == heldout.EXIT_OK
    finally:
        unlock(store)


def test_verify_with_a_different_seal_id_is_a_mismatch(tmp_path, capsys):
    _src, store, _seal_id = seal(tmp_path, capsys)
    assert heldout.main(["verify", str(store), "--seal", "0" * 64]) == heldout.EXIT_MISMATCH
    assert capsys.readouterr().out.startswith("seal ")
    unlock(store)


def test_seal_refuses_bad_inputs(tmp_path, capsys):
    src = make_src(tmp_path)
    assert heldout.main(["seal", str(src), str(src / "inner")]) == heldout.EXIT_USAGE
    existing = tmp_path / "exists"
    existing.mkdir()
    assert heldout.main(["seal", str(src), str(existing)]) == heldout.EXIT_USAGE
    assert heldout.main(["seal", str(tmp_path / "missing"), str(tmp_path / "s3")]) == heldout.EXIT_USAGE
    assert heldout.main(["verify", str(tmp_path / "missing")]) == heldout.EXIT_USAGE
    os.symlink(src / "test_double.py", src / "link.py")
    assert heldout.main(["seal", str(src), str(tmp_path / "s2")]) == heldout.EXIT_USAGE
    assert not (tmp_path / "s2").exists()


@pytest.fixture()
def sealed(tmp_path, capsys):
    _src, store, seal_id = seal(tmp_path, capsys)
    yield store, seal_id
    unlock(store)


def check(store, seal_id, repo, commit, out, *extra):
    return heldout.main(["check", str(store), str(repo.path), commit, str(out), "--seal", seal_id,
                         "--timeout", "120", *extra])


def test_check_passes_a_good_commit_and_fails_a_bad_one(sealed, make_repo, tmp_path):
    store, seal_id = sealed
    repo = make_repo()
    good = repo.commit({"mod.py": GOOD})
    bad = repo.commit({"mod.py": BAD})
    out = tmp_path / "out"
    assert check(store, seal_id, repo, good, out) == heldout.EXIT_OK
    assert (out / "heldout.rc").read_text() == "0\n"
    assert (out / "heldout.commit").read_text() == good + "\n"
    assert (out / "heldout.seal").read_text() == seal_id + "\n"
    assert check(store, seal_id, repo, bad, out) == heldout.EXIT_FAIL
    assert (out / "heldout.rc").read_text() == "1\n"
    assert "failed" in (out / "heldout.log").read_text()


def test_check_uses_the_commit_not_the_working_tree(sealed, make_repo, tmp_path):
    store, seal_id = sealed
    repo = make_repo()
    bad = repo.commit({"mod.py": BAD})
    (repo.path / "mod.py").write_text(GOOD)                 # uncommitted "fix" must not count
    assert check(store, seal_id, repo, bad, tmp_path / "out") == heldout.EXIT_FAIL
    assert (repo.path / "mod.py").read_text() == GOOD       # and TREE is never modified


def test_check_refuses_to_run_a_tampered_store_and_leaves_no_pass(sealed, make_repo, tmp_path, capsys):
    store, seal_id = sealed
    repo = make_repo()
    good = repo.commit({"mod.py": GOOD})
    out = tmp_path / "out"
    assert check(store, seal_id, repo, good, out) == heldout.EXIT_OK
    unlock(store)
    (store / "test_double.py").write_text("def test_double():\n    assert True\n")
    capsys.readouterr()
    assert check(store, seal_id, repo, good, out) == heldout.EXIT_MISMATCH
    assert "changed test_double.py" in capsys.readouterr().out
    assert not (out / "heldout.rc").exists()                # the earlier pass was removed first


def test_check_refuses_an_unknown_commit(sealed, make_repo, tmp_path):
    store, seal_id = sealed
    repo = make_repo()
    repo.commit({"mod.py": GOOD})
    assert check(store, seal_id, repo, "no-such-ref", tmp_path / "out") == heldout.EXIT_USAGE
    with pytest.raises(heldout.Usage):                      # an option-looking commit is never passed to git
        heldout.cmd_check(str(store), str(repo.path), "--output=x", str(tmp_path / "out"), seal_id, None, 60.0)


def test_check_refuses_out_inside_tree(sealed, make_repo):
    store, seal_id = sealed
    repo = make_repo()
    good = repo.commit({"mod.py": GOOD})
    assert check(store, seal_id, repo, good, repo.path / "out") == heldout.EXIT_USAGE


def test_inject_puts_a_file_in_the_extracted_tree(tmp_path, capsys, make_repo):
    probe = ("import os\n\n\ndef test_injected():\n"
             "    p = os.path.join(os.environ['HELDOUT_TREE'], 'helpers', 'h.txt')\n"
             "    assert open(p).read() == 'hi\\n'\n")
    _src, store, sid = seal(tmp_path, capsys, {"test_inject.py": probe})
    repo = make_repo()
    c = repo.commit({"mod.py": GOOD})
    extra = tmp_path / "h.txt"
    extra.write_text("hi\n")
    try:
        assert check(store, sid, repo, c, tmp_path / "o1") == heldout.EXIT_FAIL
        assert check(store, sid, repo, c, tmp_path / "o2", "--inject", str(extra),
                     "--inject-as", "helpers/h.txt") == heldout.EXIT_OK
        assert check(store, sid, repo, c, tmp_path / "o3", "--inject", str(extra),
                     "--inject-as", "../escape.txt") == heldout.EXIT_USAGE
    finally:
        unlock(store)


def test_a_hanging_check_times_out_as_a_failure(tmp_path, capsys, make_repo):
    _src, store, sid = seal(tmp_path, capsys, {"test_hang.py": "import time\n\n\ndef test_hang():\n"
                                                             "    time.sleep(60)\n"})
    repo = make_repo()
    c = repo.commit({"mod.py": GOOD})
    out = tmp_path / "out"
    try:
        rc = heldout.main(["check", str(store), str(repo.path), c, str(out), "--seal", sid, "--timeout", "3"])
        assert rc == heldout.EXIT_FAIL
        assert "TIMEOUT after 3s" in (out / "heldout.log").read_text()
    finally:
        unlock(store)


def test_bad_timeout_is_usage(tmp_path):
    with pytest.raises(SystemExit) as exc:
        heldout.main(["check", "s", "t", "c", "o", "--timeout", "0"])
    assert exc.value.code == heldout.EXIT_USAGE


def test_check_requires_the_seal_id(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        heldout.main(["check", "s", "t", "c", "o"])
    assert exc.value.code == heldout.EXIT_USAGE
    assert "--seal" in capsys.readouterr().err


def test_a_malformed_seal_id_is_refused(sealed, make_repo, tmp_path):
    store, _ = sealed
    repo = make_repo()
    good = repo.commit({"mod.py": GOOD})
    assert check(store, "abc", repo, good, tmp_path / "out") == heldout.EXIT_REFUSED


# Candidate code that runs at import inside the pytest process: it appends a fake summary to the run log
# (found next to HELDOUT_TREE) and exits 0 before pytest can report anything.
FORGER = ('import os\n'
          'log = os.path.join(os.path.dirname(os.environ["HELDOUT_TREE"]), "pytest.log")\n'
          'with open(log, "a") as fh:\n'
          '    fh.write("\\n1 passed in 0.01s\\n")\n'
          'os._exit(0)\n')


def test_a_candidate_that_prints_a_fake_summary_and_exits_0_is_not_a_pass(sealed, make_repo, tmp_path):
    store, seal_id = sealed
    repo = make_repo()
    forged = repo.commit({"mod.py": FORGER})
    out = tmp_path / "out"
    assert check(store, seal_id, repo, forged, out) == heldout.EXIT_FAIL
    assert (out / "heldout.rc").read_text() == "1\n"
    assert "no junit result" in (out / "heldout.log").read_text()


def test_every_sealed_test_must_be_present_and_passed(tmp_path, capsys, make_repo):
    two = CHECK + "\n\nclass TestMore:\n    def test_triple(self):\n        import pytest\n" \
                  "        pytest.skip('skipped is not passed')\n"
    _src, store, sid = seal(tmp_path, capsys, {"test_double.py": two, "data.txt": "x\n"})
    repo = make_repo()
    c = repo.commit({"mod.py": GOOD})
    out = tmp_path / "out"
    try:
        assert check(store, sid, repo, c, out) == heldout.EXIT_FAIL
        assert "not passed: test_double.py::TestMore::test_triple (skipped)" in (out / "heldout.log").read_text()
    finally:
        unlock(store)


def test_expected_ids_come_from_the_sealed_sources(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "test_a.py").write_text("def test_one():\n    pass\n\n\ndef helper():\n    pass\n\n\n"
                                        "class TestK:\n    def test_two(self):\n        pass\n\n\n"
                                        "class Other:\n    def test_no(self):\n        pass\n")
    (tmp_path / "sub" / "b_test.py").write_text("async def test_three():\n    pass\n")
    (tmp_path / "notes.py").write_text("def test_not_collected():\n    pass\n")
    assert heldout.expected_ids(str(tmp_path)) == ["sub/b_test.py::test_three", "test_a.py::TestK::test_two",
                                                   "test_a.py::test_one"]


def test_inject_never_follows_a_symlinked_folder_out_of_the_tree(tmp_path, capsys, make_repo):
    _src, store, sid = seal(tmp_path, capsys)
    outside = tmp_path / "outside"
    outside.mkdir()
    repo = make_repo()
    repo.write({"mod.py": GOOD})
    os.symlink(outside, repo.path / "sub")                    # the candidate commits a symlinked folder
    c = repo.commit({})
    extra = tmp_path / "h.txt"
    extra.write_text("hi\n")
    try:
        assert check(store, sid, repo, c, tmp_path / "o", "--inject", str(extra),
                     "--inject-as", "sub/h.txt") == heldout.EXIT_REFUSED
    finally:
        unlock(store)
    assert list(outside.iterdir()) == []


def test_sigterm_kills_the_pytest_child_and_removes_the_temp_folders(tmp_path, capsys, make_repo):
    import signal
    import subprocess
    import sys
    import time
    probe = tmp_path / "pid.txt"
    _src, store, sid = seal(tmp_path, capsys, {"test_wait.py": "import os\nimport time\n\n\ndef test_wait():\n"
                                                "    open(os.environ['PBP_PROBE'], 'w').write(str(os.getpid()))\n"
                                                "    time.sleep(120)\n"})
    repo = make_repo()
    c = repo.commit({"mod.py": GOOD})
    tmpdir = tmp_path / "tmpdir"
    tmpdir.mkdir()
    env = {**os.environ, "PBP_PROBE": str(probe), "TMPDIR": str(tmpdir)}
    proc = subprocess.Popen([sys.executable, "-B", "-m", "pbp", "heldout", "check", str(store), str(repo.path), c,
                             str(tmp_path / "out"), "--seal", sid, "--timeout", "300"], env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pid = None
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not (probe.exists() and probe.read_text()):
            time.sleep(0.1)
        pid = int(probe.read_text())
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=60) == 128 + signal.SIGTERM
        time.sleep(0.2)
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        assert not alive, "the pytest child outlived heldout"
        assert sorted(p.name for p in tmpdir.iterdir()) == []
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        unlock(store)


# ---------------------------------------------------------------- seal: refused sources
def _bad_source(kind, src, tmp_path):
    if kind == "symlinked folder":
        (tmp_path / "elsewhere").mkdir()
        os.symlink(tmp_path / "elsewhere", src / "linked")
        return "symlink in source"
    if kind == "symlinked file":
        os.symlink(src / "test_double.py", src / "link.py")
        return "symlink in source"
    if kind == "fifo":
        os.mkfifo(src / "pipe")
        return "special file in source"
    if kind == "newline in a name":
        (src / "a\nb.txt").write_text("x\n")
        return "file name with newline refused"
    (src / heldout.MANIFEST).write_text("x\n")
    return f"source already contains {heldout.MANIFEST}"


@pytest.mark.parametrize("kind", ["symlinked folder", "symlinked file", "fifo", "newline in a name", "manifest"])
def test_seal_refuses_odd_sources_with_a_clear_reason(tmp_path, capsys, kind):
    src = make_src(tmp_path)
    reason = _bad_source(kind, src, tmp_path)
    assert heldout.main(["seal", str(src), str(tmp_path / "store")]) == 2
    assert reason in capsys.readouterr().err
    assert not (tmp_path / "store").exists()


def test_seal_skips_cache_folders_and_bytecode(tmp_path, capsys):
    _src, store, _sid = seal(tmp_path, capsys, {"test_double.py": CHECK, "__pycache__/m.cpython.pyc": "x",
                                                "top.pyc": "x", ".pytest_cache/v": "x"})
    try:
        names = [ln.split("  ")[1] for ln in (store / heldout.MANIFEST).read_text().splitlines()]
        assert names == ["test_double.py"]
        assert sorted(p.name for p in store.iterdir()) == [heldout.MANIFEST, "test_double.py"]
    finally:
        unlock(store)


def test_a_failed_seal_removes_the_half_made_store_and_raises(tmp_path):
    src = make_src(tmp_path)
    secret = src / "unreadable.txt"
    secret.write_text("x\n")
    os.chmod(secret, 0)
    try:
        with pytest.raises(PermissionError):
            heldout.main(["seal", str(src), str(tmp_path / "store")])
    finally:
        os.chmod(secret, 0o644)
    assert not (tmp_path / "store").exists()


# ---------------------------------------------------------------- verify: every kind of difference
def _tamper(store, how):
    unlock(store)
    manifest = store / heldout.MANIFEST
    if how == "manifest removed":
        manifest.unlink()
        return "missing MANIFEST.sha256"
    if how == "manifest not utf-8":
        manifest.write_bytes(b"\xff\xfe\n")
        return "changed MANIFEST.sha256"
    if how == "manifest line malformed":
        manifest.write_text("zz  test_double.py\n")
        return "changed MANIFEST.sha256"
    if how == "manifest path climbs out":
        manifest.write_text("0" * 64 + "  ../test_double.py\n")
        return "changed MANIFEST.sha256"
    if how == "file removed":
        (store / "test_double.py").unlink()
        return "missing test_double.py"
    (store / "test_double.py").rename(store / "real.txt")
    os.symlink(store / "real.txt", store / "test_double.py")
    return "changed test_double.py"


@pytest.mark.parametrize("how", ["manifest removed", "manifest not utf-8", "manifest line malformed",
                                 "manifest path climbs out", "file removed", "file replaced by a symlink"])
def test_verify_names_each_difference_and_exits_4(tmp_path, capsys, how):
    _src, store, _sid = seal(tmp_path, capsys)
    line = _tamper(store, how)
    try:
        assert heldout.main(["verify", str(store)]) == 4          # the documented exit code
        out = capsys.readouterr().out.splitlines()
        assert line in out
        assert not any(o.startswith("seal ") for o in out)        # no --seal given: no seal line
    finally:
        unlock(store)


def test_a_broken_manifest_also_reports_the_seal_mismatch(tmp_path, capsys):
    _src, store, sid = seal(tmp_path, capsys)
    _tamper(store, "manifest line malformed")
    try:
        assert heldout.main(["verify", str(store), "--seal", sid]) == 4
        out = capsys.readouterr().out.splitlines()
        assert out[0] == "changed MANIFEST.sha256" and out[1].startswith("seal ") and out[1] != f"seal {sid}"
    finally:
        unlock(store)


def test_verify_ignores_a_pycache_folder_in_the_store(tmp_path, capsys):
    _src, store, sid = seal(tmp_path, capsys)
    unlock(store)
    (store / "__pycache__").mkdir()
    (store / "__pycache__" / "test_double.cpython.pyc").write_bytes(b"x")
    try:
        assert heldout.main(["verify", str(store), "--seal", sid]) == 0
    finally:
        unlock(store)


def test_verify_store_reports_a_missing_store(tmp_path):
    assert heldout.verify_store(str(tmp_path / "nope"), None) == ([f"missing {tmp_path / 'nope'}"], None)


# ---------------------------------------------------------------- check: refusals and failure paths
def test_check_with_a_well_formed_but_wrong_seal_id_is_a_mismatch(sealed, make_repo, tmp_path, capsys):
    store, _sid = sealed
    repo = make_repo()
    good = repo.commit({"mod.py": GOOD})
    capsys.readouterr()
    assert check(store, "0" * 64, repo, good, tmp_path / "out") == 4
    assert capsys.readouterr().out.startswith("seal ")
    assert not (tmp_path / "out" / "heldout.rc").exists()


def test_a_malformed_seal_id_exits_5(sealed, make_repo, tmp_path):
    store, _ = sealed
    repo = make_repo()
    assert check(store, "abc", repo, repo.commit({"mod.py": GOOD}), tmp_path / "out") == 5


def test_check_refuses_out_that_is_a_file_a_missing_tree_and_an_inject_that_is_no_file(
        sealed, make_repo, tmp_path, capsys):
    store, sid = sealed
    repo = make_repo()
    good = repo.commit({"mod.py": GOOD})
    a_file = tmp_path / "a_file"
    a_file.write_text("x\n")
    assert check(store, sid, repo, good, a_file) == 2
    assert "OUT is not a directory" in capsys.readouterr().err
    assert heldout.main(["check", str(store), str(tmp_path / "no_tree"), good, str(tmp_path / "o1"),
                         "--seal", sid]) == 2
    assert "bad COMMIT or TREE" in capsys.readouterr().err
    assert check(store, sid, repo, good, tmp_path / "o2", "--inject", str(tmp_path)) == 2
    assert "inject is not a regular file" in capsys.readouterr().err


def test_inject_replaces_a_committed_symlink_and_never_writes_through_it(sealed, make_repo, tmp_path):
    store, sid = sealed
    target = tmp_path / "target.txt"
    target.write_text("orig\n")
    repo = make_repo()
    repo.write({"mod.py": GOOD})
    os.symlink(target, repo.path / "h.txt")                    # the candidate commits a symlink to a file
    c = repo.commit({})
    extra = tmp_path / "extra.txt"
    extra.write_text("injected\n")
    assert check(store, sid, repo, c, tmp_path / "o", "--inject", str(extra), "--inject-as", "h.txt") == 0
    assert target.read_text() == "orig\n"


def test_sealed_tests_in_a_subfolder_are_copied_to_the_same_place(tmp_path, capsys, make_repo):
    _src, store, sid = seal(tmp_path, capsys, {"sub/test_sub.py": CHECK})
    repo = make_repo()
    try:
        assert check(store, sid, repo, repo.commit({"mod.py": GOOD}), tmp_path / "o") == 0
    finally:
        unlock(store)


def test_a_store_with_no_tests_fails_and_says_why(tmp_path, capsys, make_repo):
    _src, store, sid = seal(tmp_path, capsys, {"data.txt": "x\n"})
    repo = make_repo()
    try:
        assert check(store, sid, repo, repo.commit({"mod.py": GOOD}), tmp_path / "o") == 1
        assert "no test ids found in the sealed sources" in (tmp_path / "o" / "heldout.log").read_text()
    finally:
        unlock(store)


def test_a_crash_inside_check_is_recorded_and_exits_2(sealed, make_repo, tmp_path, capsys, monkeypatch):
    store, sid = sealed
    repo = make_repo()
    good = repo.commit({"mod.py": GOOD})

    def crash(*_a, **_k):
        raise RuntimeError("boom")
    monkeypatch.setattr(heldout, "_run_pytest", crash)
    assert check(store, sid, repo, good, tmp_path / "out") == 2
    assert (tmp_path / "out" / "heldout.rc").read_text() == "2\n"
    assert "crashed: RuntimeError('boom')" in capsys.readouterr().err


def test_a_tree_that_git_archive_cannot_extract_exits_2(sealed, make_repo, tmp_path, capsys):
    store, sid = sealed
    repo = make_repo()
    good = repo.commit({"mod.py": GOOD})
    blob = repo.git("rev-parse", f"{good}:mod.py")
    (repo.path / ".git" / "objects" / blob[:2] / blob[2:]).unlink()   # the commit resolves, its file is gone
    out = tmp_path / "out"
    out.mkdir()
    (out / "heldout.rc").write_text("0\n")                              # a stale pass must not survive
    capsys.readouterr()
    assert check(store, sid, repo, good, out) == 2
    assert capsys.readouterr().err.startswith("pbp heldout: cannot extract COMMIT from TREE: git archive rc=")
    assert not (out / "heldout.rc").exists()


@pytest.mark.parametrize("value", ["0", "-1", "inf", "nan"])
def test_a_timeout_that_is_not_a_positive_number_is_a_usage_error(value, capsys):
    with pytest.raises(SystemExit) as exc:
        heldout.main(["check", "s", "t", "c", "o", "--seal", "0" * 64, "--timeout", value])
    assert exc.value.code == 2
    assert "timeout must be a positive number" in capsys.readouterr().err


# ---------------------------------------------------------------- small parts
def test_summary_ok_on_empty_output_is_false():
    assert heldout.summary_ok("") is False
    assert heldout.summary_ok("\n  \n") is False


def test_junit_problems_lists_a_failure_outside_the_expected_ids(tmp_path):
    junit = tmp_path / "j.xml"
    junit.write_text('<testsuites><testsuite><testcase classname="test_a" name="test_one"/>'
                     '<testcase classname="test_a" name="test_gen"><failure message="x">boom</failure>'
                     '</testcase></testsuite></testsuites>')
    assert heldout.junit_problems(str(junit), ["test_a.py::test_one"]) == ["failed: test_a::test_gen"]
    assert heldout.junit_problems(str(junit), []) == ["no test ids found in the sealed sources"]


def test_copy_verified_copies_regular_files_read_only_and_nothing_else(tmp_path):
    store = tmp_path / "store"
    (store / "sub").mkdir(parents=True)
    (store / "__pycache__").mkdir()
    (store / "a.txt").write_text("a\n")
    (store / "sub" / "b.txt").write_text("b\n")
    (store / "__pycache__" / "c.pyc").write_bytes(b"c")
    os.symlink(store / "a.txt", store / "link.txt")
    dest = tmp_path / "dest"
    heldout._copy_verified(str(store), str(dest))
    got = sorted(Path(r, n).relative_to(dest).as_posix() for r, _d, ns in os.walk(dest) for n in ns)
    assert got == ["a.txt", "sub/b.txt"]
    assert stat.S_IMODE(os.stat(dest / "a.txt").st_mode) == 0o444
    assert (dest / "sub" / "b.txt").read_text() == "b\n"


def test_rmtree_force_removes_read_only_folders_without_a_deprecation_warning(tmp_path):
    top = tmp_path / "t"
    (top / "a" / "b").mkdir(parents=True)
    (top / "a" / "b" / "f.txt").write_text("x\n")
    os.chmod(top / "a" / "b", 0o555)
    os.chmod(top / "a", 0o555)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        heldout._rmtree_force(str(top))
    assert not top.exists()


def test_atomic_write_removes_its_temp_file_and_raises_when_the_rename_fails(tmp_path, monkeypatch):
    def no_rename(_a, _b):
        raise OSError("rename refused")
    monkeypatch.setattr(heldout.os, "replace", no_rename)
    with pytest.raises(OSError, match="rename refused"):
        heldout._atomic_write(str(tmp_path), "x.txt", b"data")
    monkeypatch.undo()
    assert list(tmp_path.iterdir()) == []


def test_the_module_runs_as_a_script(tmp_path):
    import subprocess
    import sys
    root = Path(heldout.__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, "-B", "-m", "pbp.heldout", "verify", str(tmp_path / "missing")],
                       cwd=root, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert r.returncode == 2
    assert "STORE is not a directory" in r.stderr
