"""lock_review_tests: only tests that are red on the code get locked; green and broken ones stay claims."""
from __future__ import annotations

import os

import pytest

from pbp import lock_review_tests as lrt

FENCE = "`" * 3

MOD = "def parse_qty(text):\n    return int(text)\n"


def block(code: str) -> str:
    return f"{FENCE}python\n{code}{FENCE}\n"


REVIEW = (
    "# Review\n\n"
    "1. **BLOCKER - negative quantities accepted**\n\n"
    + block("from mod import parse_qty\nimport pytest\n\n\ndef test_negative_refused():\n"
            "    with pytest.raises(ValueError):\n        parse_qty('-1')\n")
    + "\n2. **MAJOR - zero handling**\n\n"
    + block("from mod import parse_qty\n\n\ndef test_zero_reads():\n    assert parse_qty('0') == 0\n")
    + "\n3. **MAJOR - imports a module that does not exist**\n\n"
    + block("from nowhere import thing\n\n\ndef test_thing():\n    assert thing() == 1\n")
    + "\nSome prose with a non-test block:\n\n"
    + block("x = 1\n")
)


def test_blocks_tags_each_test_with_its_finding():
    found = lrt.blocks(REVIEW)
    assert [tag for tag, _ in found] == ["finding 1 BLOCKER", "finding 2 MAJOR", "finding 3 MAJOR"]
    assert all("def test_" in code for _, code in found)


def test_indented_blocks_in_a_list_are_dedented():
    text = "1. **MINOR - x**\n\n   " + FENCE + "python\n   def test_a():\n       assert 1\n   " + FENCE + "\n"
    (tag, code), = lrt.blocks(text)
    assert tag == "finding 1 MINOR"
    assert code.startswith("def test_a():")


def test_only_blocks_marked_helper_are_helpers():
    marked = "# helper\ndef _helper():\n    return 3\n"
    text = (block(marked) + block("def parse_qty(t):\n    return int(t)\n")
            + block("def test_x():\n    assert _helper() == 3\n"))
    assert lrt.helper_blocks(text) == [marked]


def test_top_test_ignores_a_def_inside_a_string():
    code = 'ATTACK = """\ndef test_early():\n    pass\n"""\n\n\ndef test_real():\n    assert ATTACK\n'
    assert lrt.test_names(code) == ["test_real"]


@pytest.fixture()
def tree(tmp_path):
    t = tmp_path / "tree"
    t.mkdir()
    (t / "mod.py").write_text(MOD)
    return t


def test_only_red_tests_are_locked(tree, tmp_path, capsys):
    review = tmp_path / "REVIEW.md"
    review.write_text(REVIEW)
    rc = lrt.main([str(review), str(tree), "tests/test_locked.py", "--timeout", "120"])
    out = capsys.readouterr().out
    assert rc == 0
    # each line: STATUS finding N SEVERITY test_name  (last pytest line)
    status = {parts[4]: parts[0] for parts in (ln.split() for ln in out.splitlines())
              if parts and parts[0] in ("RED", "GREEN", "BROKEN")}
    assert status == {"test_negative_refused": "RED", "test_zero_reads": "GREEN", "test_thing": "BROKEN"}
    locked = (tree / "tests" / "test_locked.py").read_text()
    assert "def test_negative_refused" in locked
    assert "test_zero_reads" not in locked and "test_thing" not in locked
    assert "# finding 1 BLOCKER: red when locked" in locked
    assert "wrote 1 red test(s)" in out
    assert not list(tree.glob("test_lockprobe_*"))      # probe files are cleaned up


def test_a_failure_caused_by_a_missing_name_is_broken_not_red(tree, tmp_path, capsys):
    review = tmp_path / "REVIEW.md"
    review.write_text("1. **BLOCKER - x**\n\n" + block("def test_uses_undefined():\n    assert undefined_name() == 1\n"))
    rc = lrt.main([str(review), str(tree), "out/test_locked.py"])
    out = capsys.readouterr().out
    assert rc == 1                                        # nothing red: nothing was proven
    assert out.startswith("BROKEN")
    assert not (tree / "out" / "test_locked.py").exists()


def test_helpers_are_put_before_every_test(tree, tmp_path, capsys):
    review = tmp_path / "REVIEW.md"
    review.write_text("1. **MAJOR - x**\n\n" + block("# helper\ndef _neg():\n    return '-1'\n")
                      + block("import pytest\nfrom mod import parse_qty\n\n\ndef test_neg():\n"
                              "    with pytest.raises(ValueError):\n        parse_qty(_neg())\n"))
    rc = lrt.main([str(review), str(tree), "locked.py"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "helpers: 1 block(s)" in out
    assert "\nRED" in "\n" + out
    assert "def _neg" in (tree / "locked.py").read_text()


def test_no_test_blocks_returns_1(tree, tmp_path, capsys):
    review = tmp_path / "REVIEW.md"
    review.write_text("Looks fine to me.\n")
    assert lrt.main([str(review), str(tree), "locked.py"]) == 1
    assert "no test blocks" in capsys.readouterr().out


def test_no_red_test_exits_1(tree, tmp_path, capsys):
    review = tmp_path / "REVIEW.md"
    review.write_text("1. **MAJOR - x**\n\n" + block("from mod import parse_qty\n\n\ndef test_ok():\n"
                                                      "    assert parse_qty('3') == 3\n"))
    rc = lrt.main([str(review), str(tree), "locked.py"])
    out = capsys.readouterr().out
    assert rc == 1
    assert out.startswith("GREEN")
    assert "no red test: nothing locked" in out
    assert not (tree / "locked.py").exists()


def test_a_timeout_is_its_own_label_and_is_never_locked(tree, tmp_path, capsys):
    review = tmp_path / "REVIEW.md"
    review.write_text("1. **BLOCKER - hangs**\n\n" + block("def test_hang():\n    while True:\n        pass\n"))
    rc = lrt.main([str(review), str(tree), "locked.py", "--timeout", "2"])
    out = capsys.readouterr().out
    assert rc == 1
    assert out.startswith("TIMEOUT finding 1 BLOCKER")
    assert not (tree / "locked.py").exists()


def test_a_crlf_review_finds_the_same_blocks():
    crlf = REVIEW.replace("\n", "\r\n")
    found = lrt.blocks(crlf)
    assert [tag for tag, _ in found] == ["finding 1 BLOCKER", "finding 2 MAJOR", "finding 3 MAJOR"]
    assert all("\r" not in code for _, code in found)
    marked = "# helper\ndef _h():\n    return 1\n"
    assert lrt.helper_blocks(block(marked).replace("\n", "\r\n")) == [marked]


@pytest.mark.parametrize("rc,label", [(0, "GREEN"), (1, "RED"), (2, "BROKEN"), (3, "BROKEN"), (4, "BROKEN"),
                                      (5, "BROKEN"), (-9, "BROKEN"), (137, "BROKEN")])
def test_pytest_return_codes_map_to_labels(tree, monkeypatch, rc, label):
    monkeypatch.setattr(lrt, "run_cmd", lambda cmd, **kw: (rc, b"F\n1 failed in 0.01s\n"))
    status, last = lrt.run_one(tree, "def test_x():\n    assert 0\n", 5, ".", "test_x")
    assert status == label
    assert last == "1 failed in 0.01s"


def test_an_error_summary_is_broken_even_at_exit_1(tree, monkeypatch):
    monkeypatch.setattr(lrt, "run_cmd", lambda cmd, **kw: (1, b"E\n1 error in 0.01s\n"))
    assert lrt.run_one(tree, "def test_x():\n    pass\n", 5, ".", "test_x")[0] == "BROKEN"


def test_test_names_reads_top_level_tests_and_falls_back_to_text():
    assert lrt.test_names("def test_a(:\n    pass\n") == ["test_a"]
    assert lrt.test_names("def test_one():\n    pass\n\n\ndef test_two():\n    pass\n") == ["test_one", "test_two"]
    assert lrt.test_names("class T:\n    def test_m(self):\n        pass\n") == []   # no top-level test


def test_a_real_failure_next_to_an_error_is_still_red(tree, monkeypatch):
    monkeypatch.setattr(lrt, "run_cmd", lambda cmd, **kw: (1, b"FE\n1 failed, 1 error in 0.01s\n"))
    assert lrt.run_one(tree, "def test_x():\n    assert 0\n", 5, ".", "test_x")[0] == "RED"


def test_an_unmarked_code_quote_is_not_put_before_tests(tree, tmp_path, capsys):
    # the review quotes the buggy code; a test that uses parse_qty without importing it must not run on the quote
    review = tmp_path / "REVIEW.md"
    review.write_text("1. **BLOCKER - x**\n\n" + block("def parse_qty(t):\n    return int(t)\n")
                      + block("import pytest\n\n\ndef test_neg():\n    with pytest.raises(ValueError):\n"
                              "        parse_qty('-1')\n"))
    rc = lrt.main([str(review), str(tree), "locked.py"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "helpers:" not in out
    assert out.startswith("BROKEN")                      # NameError: parse_qty was never imported
    assert not (tree / "locked.py").exists()


def test_every_test_in_a_block_is_judged_and_only_red_ones_are_locked(tree, tmp_path, capsys):
    review = tmp_path / "REVIEW.md"
    review.write_text("1. **MAJOR - x**\n\n" + block(
        "import pytest\nfrom mod import parse_qty\n\n\ndef test_neg():\n    with pytest.raises(ValueError):\n"
        "        parse_qty('-1')\n\n\n" + "@" + "pytest.mark.parametrize('t', ['3'])\ndef test_ok(t):\n"
        "    assert parse_qty(t) == 3\n\n\ndef test_last_is_green():\n    assert parse_qty('4') == 4\n"))
    rc = lrt.main([str(review), str(tree), "locked.py"])
    out = capsys.readouterr().out
    assert rc == 0
    status = {p[4]: p[0] for p in (ln.split() for ln in out.splitlines()) if p and p[0] in ("RED", "GREEN")}
    assert status == {"test_neg": "RED", "test_ok": "GREEN", "test_last_is_green": "GREEN"}
    locked = (tree / "locked.py").read_text()
    assert "def test_neg" in locked
    assert "test_ok" not in locked and "parametrize" not in locked and "test_last_is_green" not in locked
    assert "wrote 1 red test(s)" in out


@pytest.mark.parametrize("out", ["../escape.py", "/abs/escape.py", "sub/../../escape.py"])
def test_out_outside_tree_is_refused(tree, tmp_path, capsys, out):
    review = tmp_path / "REVIEW.md"
    review.write_text(REVIEW)
    assert lrt.main([str(review), str(tree), out]) == 2
    assert "pbp lock-review-tests: OUT must be a relative path inside TREE" in capsys.readouterr().err
    assert not (tmp_path / "escape.py").exists()


def test_out_through_a_symlink_and_a_bad_pytest_root_are_refused(tree, tmp_path, capsys):
    review = tmp_path / "REVIEW.md"
    review.write_text(REVIEW)
    os.symlink(tmp_path, tree / "link")
    assert lrt.main([str(review), str(tree), "link/escape.py"]) == 2
    assert lrt.main([str(review), str(tree), "locked.py", "--pytest-root", ".."]) == 2
    assert not (tmp_path / "escape.py").exists() and not (tree / "locked.py").exists()


def test_an_existing_out_file_is_never_overwritten(tree, tmp_path, capsys):
    review = tmp_path / "REVIEW.md"
    review.write_text(REVIEW)
    (tree / "keep.py").write_text("ORIGINAL\n")
    assert lrt.main([str(review), str(tree), "keep.py"]) == 2
    assert "already exists" in capsys.readouterr().err
    assert (tree / "keep.py").read_text() == "ORIGINAL\n"


def test_the_output_path_is_shown_relative_to_the_current_folder(tree, tmp_path, capsys, monkeypatch):
    review = tmp_path / "REVIEW.md"
    review.write_text(REVIEW)
    monkeypatch.chdir(tmp_path)
    assert lrt.main([str(review), "tree", "tests/test_locked.py"]) == 0
    assert "wrote 1 red test(s) to tree/tests/test_locked.py" in capsys.readouterr().out
