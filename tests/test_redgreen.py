"""redgreen: a real fix passes; every cheap way to fake RED, GREEN or KEPT is rejected or written down."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from pbp import redgreen

BUGGY = "def parse_qty(text):\n    return int(text)\n"
FIXED = ("def parse_qty(text):\n    value = int(text)\n    if value < 0:\n"
         "        raise ValueError('negative')\n    return value\n")
BASE_TESTS = ("from mod import parse_qty\n\n\ndef test_reads():\n    assert parse_qty('7') == 7\n"
              "    assert parse_qty('0') == 0\n")
NEW_TEST = "\n\ndef test_negative_refused():\n    with pytest.raises(ValueError):\n        parse_qty('-1')\n"
REAL_PROOF = "import pytest\n\n" + BASE_TESTS + NEW_TEST
DEMO = Path(__file__).resolve().parent.parent / "demo" / "redgreen"


@pytest.fixture()
def repo(make_repo):
    r = make_repo()
    r.pre = r.commit({"mod.py": BUGGY, "tests/test_mod.py": BASE_TESTS}, "pre")
    return r


def run(repo, post, *extra, capsys=None):
    rc = redgreen.main([str(repo.path), repo.pre, post, *extra])
    out = capsys.readouterr().out if capsys else ""
    return rc, out


def fix_with(repo, files, remove=()):
    """POST = the real fix and its red test, plus FILES on top."""
    return repo.commit({"mod.py": FIXED, "tests/test_mod.py": REAL_PROOF, **files}, "fix", remove=remove)


# ------------------------------------------------------------------ the positive control
def test_real_fix_with_a_red_proof_passes(repo, capsys):
    post = repo.commit({"mod.py": FIXED, "tests/test_mod.py": REAL_PROOF}, "fix")
    rc, out = run(repo, post, capsys=capsys)
    assert rc == 0, out
    assert out == "PASS new=1 red_new=1 green=suite\n"


def test_bundled_demo_fake_fails_and_real_passes(make_repo, capsys):
    def tree(name):
        root = DEMO / name
        return {str(p.relative_to(root)): p.read_text() for p in root.rglob("*.py")}
    r = make_repo()
    r.pre = r.commit(tree("pre"), "pre")
    fake = r.commit(tree("fake"), "fake")
    r.git("checkout", "-q", r.pre)
    real = r.commit(tree("real"), "real")
    rc, out = run(r, fake, capsys=capsys)
    assert rc == 1, out
    assert " - KEPT: tests/test_inventory.py::test_parse_qty_reads_a_number changed" in out
    assert " - RED: no new test fails with an assertion on the unfixed code" in out
    rc, out = run(r, real, capsys=capsys)
    assert rc == 0, out


# ------------------------------------------------------------------ 1 KEPT: existing tests by AST
@pytest.mark.parametrize("name,edit", [
    ("early return", ("def test_reads():\n", "def test_reads():\n    return\n")),
    ("weakened assert", ("assert parse_qty('7') == 7", "assert parse_qty('7') >= 0")),
    ("removed assert", ("    assert parse_qty('0') == 0\n", "")),
    ("expected from the code", ("def test_reads():\n    assert parse_qty('7') == 7\n",
                                "def test_reads():\n    expected = parse_qty('7')\n    assert parse_qty('7') == expected\n")),
    ("skip decorator", ("def test_reads():", "@pytest.mark.skip\ndef test_reads():")),
    ("xfail decorator", ("def test_reads():", "@pytest.mark.xfail\ndef test_reads():")),
    ("skip call", ("def test_reads():\n", "def test_reads():\n    pytest.skip('later')\n")),
    ("xfail call", ("def test_reads():\n", "def test_reads():\n    pytest.xfail('later')\n")),
    ("importorskip", ("def test_reads():\n", "def test_reads():\n    pytest.importorskip('no_such_module_x')\n")),
    ("docstring", ("def test_reads():\n", "def test_reads():\n    '''Reads.'''\n")),
    ("shadowed by a second def", (NEW_TEST, NEW_TEST + "\n\ndef test_reads():\n    pass\n")),
])
def test_any_change_to_an_existing_test_is_flagged(repo, capsys, name, edit):
    post = fix_with(repo, {"tests/test_mod.py": REAL_PROOF.replace(*edit)})
    rc, out = run(repo, post, capsys=capsys)
    assert rc == 1, (name, out)
    assert " - KEPT: tests/test_mod.py::test_reads changed\n" in out, name


EXP_PRE = ("from mod import parse_qty\n\n\ndef test_reads():\n    expected = 7\n"
           "    assert parse_qty('7') == expected\n")


def test_expected_literal_replaced_by_a_call_above_the_assert_is_flagged(make_repo, capsys):
    r = make_repo()
    r.pre = r.commit({"mod.py": BUGGY, "tests/test_mod.py": EXP_PRE}, "pre")
    post = r.commit({"mod.py": FIXED, "tests/test_mod.py": "import pytest\n\n"
                     + EXP_PRE.replace("expected = 7", "expected = parse_qty('7')") + NEW_TEST}, "fix")
    rc, out = run(r, post, capsys=capsys)
    assert rc == 1, out
    assert " - KEPT: tests/test_mod.py::test_reads changed\n" in out


def test_removed_test_is_flagged(repo, capsys):
    post = fix_with(repo, {"tests/test_mod.py": "import pytest\n\nfrom mod import parse_qty\n" + NEW_TEST})
    rc, out = run(repo, post, capsys=capsys)
    assert rc == 1, out
    assert " - KEPT: tests/test_mod.py::test_reads removed\n" in out


@pytest.mark.parametrize("line", [
    "import mod\nmod.parse_qty = lambda text: abs(int(text))\n",
    "pytest_plugins = ['tests.plug']\n",
    "pytestmark = pytest.mark.skip\n",
    "pytest.importorskip('no_such_module_x')\n",
    "def helper():\n    return 1\n",
    "CASES = [1, 2]\n",
])
def test_module_level_change_in_an_existing_test_file_is_flagged(repo, capsys, line):
    post = fix_with(repo, {"tests/test_mod.py": REAL_PROOF + "\n" + line})
    rc, out = run(repo, post, capsys=capsys)
    assert rc == 1, out
    assert " - KEPT: tests/test_mod.py: module-level code added or changed: " in out


def test_new_import_that_rebinds_the_code_under_test_is_flagged(repo, capsys):
    post = fix_with(repo, {"tests/test_mod.py": REAL_PROOF.replace(
        "from mod import parse_qty\n", "from mod import parse_qty\nfrom stub import parse_qty\n"),
        "stub.py": FIXED})
    rc, out = run(repo, post, capsys=capsys)
    assert rc == 1, out
    assert " - KEPT: tests/test_mod.py: new import rebinds parse_qty: from stub import parse_qty" in out


def test_removed_import_is_flagged(repo, capsys):
    post = fix_with(repo, {"tests/test_mod.py": REAL_PROOF.replace("from mod import parse_qty\n",
                                                                   "from mod import parse_qty as parse_qty\n")})
    rc, out = run(repo, post, capsys=capsys)
    assert rc == 1, out
    assert " - KEPT: tests/test_mod.py: import removed or changed: from mod import parse_qty" in out


def test_a_test_file_that_does_not_parse_at_post_is_flagged(repo, capsys):
    post = fix_with(repo, {"tests/test_mod.py": REAL_PROOF + "\ndef (:\n"})
    rc, out = run(repo, post, capsys=capsys)
    assert rc == 1, out
    assert " - KEPT: tests/test_mod.py at POST does not parse: " in out


UT_PRE = ("import unittest\n\nfrom mod import parse_qty\n\n\nclass TestRead(unittest.TestCase):\n"
          "    def test_seven(self):\n        self.assertEqual(parse_qty('7'), 7)\n")
UT_NEW = ("\n    def test_negative(self):\n        with self.assertRaises(ValueError):\n"
          "            parse_qty('-1')\n")


@pytest.fixture()
def ut_repo(make_repo):
    r = make_repo()
    r.pre = r.commit({"mod.py": BUGGY, "tests/test_ut.py": UT_PRE}, "pre")
    return r


def test_new_unittest_method_in_an_existing_class_is_a_real_proof(ut_repo, capsys):
    post = ut_repo.commit({"mod.py": FIXED, "tests/test_ut.py": UT_PRE + UT_NEW}, "fix")
    rc, out = run(ut_repo, post, capsys=capsys)
    assert rc == 0, out
    assert out.startswith("PASS new=1 red_new=1")


@pytest.mark.parametrize("edit,line", [
    (("    def test_seven(self):", "    @unittest.skip('x')\n    def test_seven(self):"), "::TestRead::test_seven changed"),
    (("        self.assertEqual(parse_qty('7'), 7)\n", "        self.skipTest('x')\n"), "::TestRead::test_seven changed"),
    (("        self.assertEqual(parse_qty('7'), 7)", "        self.assertTrue(True)"), "::TestRead::test_seven changed"),
    (("class TestRead", "@unittest.skip('x')\nclass TestRead"), ": module-level code added or changed: "),
    (("    def test_seven", "    def setUp(self):\n        pass\n\n    def test_seven"),
     ": module-level code added or changed: "),
])
def test_unittest_weakening_is_flagged(ut_repo, capsys, edit, line):
    post = ut_repo.commit({"mod.py": FIXED, "tests/test_ut.py": UT_PRE.replace(*edit) + UT_NEW}, "fix")
    rc, out = run(ut_repo, post, capsys=capsys)
    assert rc == 1, out
    assert f" - KEPT: tests/test_ut.py{line}" in out


def test_new_test_class_in_an_existing_file_is_allowed(repo, capsys):
    cls = "\n\nclass TestNeg:\n    def test_neg(self):\n        with pytest.raises(ValueError):\n            parse_qty('-1')\n"
    post = repo.commit({"mod.py": FIXED, "tests/test_mod.py": "import pytest\n\n" + BASE_TESTS + cls}, "fix")
    rc, out = run(repo, post, capsys=capsys)
    assert rc == 0, out


def test_an_assert_removed_from_the_code_not_a_test_file_is_fine(make_repo, capsys):
    r = make_repo()
    r.pre = r.commit({"mod.py": "def parse_qty(text):\n    assert isinstance(text, str)\n    return int(text)\n",
                      "tests/test_mod.py": BASE_TESTS}, "pre")
    post = r.commit({"mod.py": FIXED, "tests/test_mod.py": REAL_PROOF}, "fix")
    rc, out = run(r, post, capsys=capsys)
    assert rc == 0, out


# ------------------------------------------------------------------ 2 non-test Python under the test roots
@pytest.mark.parametrize("path,verb,pre_has", [
    ("tests/helpers.py", "changed", True), ("tests/helpers.py", "added", False),
    ("tests/sub/plugin.py", "added", False), ("conftest.py", "added", False),
    ("tests/conftest.py", "added", False), ("pkg/tests/data/conftest.py", "added", False),
])
def test_helper_conftest_or_plugin_change_is_flagged(make_repo, capsys, path, verb, pre_has):
    r = make_repo()
    r.pre = r.commit({"mod.py": BUGGY, "tests/test_mod.py": BASE_TESTS,
                      **({path: "X = 1\n"} if pre_has else {})}, "pre")
    post = r.commit({"mod.py": FIXED, "tests/test_mod.py": REAL_PROOF, path: "X = 2\n"}, "fix")
    rc, out = run(r, post, capsys=capsys)
    assert rc == 1, out
    assert f" - KEPT: {path} {verb} (a conftest, helper or plugin can skip, patch or hide tests)" in out


def test_deleted_helper_is_flagged(make_repo, capsys):
    r = make_repo()
    r.pre = r.commit({"mod.py": BUGGY, "tests/test_mod.py": BASE_TESTS, "tests/helpers.py": "X = 1\n"}, "pre")
    post = r.commit({"mod.py": FIXED, "tests/test_mod.py": REAL_PROOF}, "fix", remove=("tests/helpers.py",))
    rc, out = run(r, post, capsys=capsys)
    assert rc == 1, out
    assert " - KEPT: tests/helpers.py deleted (a conftest, helper or plugin can skip, patch or hide tests)" in out


def test_deleted_test_file_is_flagged(make_repo, capsys):
    r = make_repo()
    r.pre = r.commit({"mod.py": BUGGY, "tests/test_mod.py": BASE_TESTS,
                      "tests/test_other.py": "from mod import parse_qty\n\n\ndef test_big():\n"
                                             "    assert parse_qty('99') == 99\n"}, "pre")
    post = r.commit({"mod.py": FIXED, "tests/test_mod.py": REAL_PROOF}, "fix", remove=("tests/test_other.py",))
    rc, out = run(r, post, capsys=capsys)
    assert rc == 1, out
    assert " - KEPT: tests/test_other.py deleted (a test file that existed at PRE)" in out


@pytest.mark.parametrize("name", ["tests/test_été.py", "tests/test_with space.py"])
def test_deleted_test_file_with_an_unusual_name_is_flagged(make_repo, capsys, name):
    r = make_repo()
    r.pre = r.commit({"mod.py": BUGGY, "tests/test_mod.py": BASE_TESTS,
                      name: "def test_big():\n    assert 99 == 99\n"}, "pre")
    post = r.commit({"mod.py": FIXED, "tests/test_mod.py": REAL_PROOF}, "fix", remove=(name,))
    rc, out = run(r, post, capsys=capsys)
    assert rc == 1, out
    assert f" - KEPT: {name} deleted (a test file that existed at PRE)" in out


# ------------------------------------------------------------------ 3 pytest configuration
@pytest.mark.parametrize("path,before,after", [
    ("pytest.ini", "[pytest]\naddopts = -q\n", "[pytest]\naddopts = -q -p tests.plug\n"),
    ("pytest.ini", "[pytest]\n", "[pytest]\npython_files = check_*.py\n"),
    ("pyproject.toml", "[tool.pytest.ini_options]\naddopts = \"-q\"\n",
     "[tool.pytest.ini_options]\naddopts = \"-q\"\npython_functions = \"check_*\"\n"),
    ("pyproject.toml", "[project]\nname = \"x\"\n", "[project]\nname = \"x\"\n[tool.pytest.ini_options]\ntestpaths = [\"x\"]\n"),
    ("setup.cfg", "[tool:pytest]\naddopts = -q\n", "[tool:pytest]\naddopts = -kfoo\n"),
    ("tox.ini", "[pytest]\naddopts = -q\n[tox]\nx = 1\n", "[pytest]\naddopts = -q\nnorecursedirs = tests\n[tox]\nx = 1\n"),
    ("sub/pytest.ini", None, "[pytest]\n"),
    ("pytest.toml", None, "[pytest]\naddopts = [\"-q\"]\n"),
])
def test_any_change_to_the_pytest_config_is_flagged(make_repo, capsys, path, before, after):
    r = make_repo()
    r.pre = r.commit({"mod.py": BUGGY, "tests/test_mod.py": BASE_TESTS, **({path: before} if before else {})}, "pre")
    post = r.commit({"mod.py": FIXED, "tests/test_mod.py": REAL_PROOF, path: after}, "fix")
    rc, out = run(r, post, capsys=capsys)
    assert rc == 1, out
    assert f" - KEPT: {path}: pytest configuration " in out


@pytest.mark.parametrize("path,before,after", [
    ("pyproject.toml", "[tool.other]\nargs = \"-q\"\n", "[tool.other]\nargs = \"-k x --ignore y\"\n"),
    ("tox.ini", "[pytest]\naddopts = -q\n[tox]\nx = 1\n", "[pytest]\naddopts = -q\n[tox]\nx = 2\n"),
])
def test_changes_outside_the_pytest_section_are_not_flagged(make_repo, capsys, path, before, after):
    r = make_repo()
    r.pre = r.commit({"mod.py": BUGGY, "tests/test_mod.py": BASE_TESTS, path: before}, "pre")
    post = r.commit({"mod.py": FIXED, "tests/test_mod.py": REAL_PROOF, path: after}, "fix")
    rc, out = run(r, post, capsys=capsys)
    assert rc == 0, out


def test_ini_section_reads_only_the_named_section():
    text = "[a]\nk = 1\n[pytest]\naddopts = -q\n\n  -x\n[b]\nk = 2\n"
    assert redgreen.ini_section(text, "pytest") == ["addopts = -q", "  -x"]
    assert redgreen.ini_section(text, "c") is None
    assert redgreen.ini_section("[pytest]\n", "pytest") == []


# ------------------------------------------------------------------ 4 new test files must not patch at import
@pytest.mark.parametrize("code", [
    "import mod\nmod.parse_qty = lambda text: abs(int(text))\n",
    "import mod\nsetattr(mod, 'parse_qty', abs)\n",
    "import sys\nsys.modules['mod'] = object()\n",
    "from unittest import mock\nmock.patch('mod.parse_qty', abs).start()\n",
    "pytest_plugins = ['tests.plug']\n",
    "import mod\nclass Holder:\n    mod.parse_qty = abs\n",
    "import mod\nif True:\n    del mod.parse_qty\n",
    "import mod\n\n\ndef test_x(_=setattr(mod, 'parse_qty', abs)):\n    assert True\n",
    "import mod\n\n\ndef helper(*, _=setattr(mod, 'parse_qty', abs)):\n    return 1\n",
    "import mod\n\n\n@setattr(mod, 'parse_qty', abs) or (lambda f: f)\ndef test_y():\n    assert True\n",
    "import mod\n\n\n@(lambda f: setattr(mod, 'parse_qty', abs) or f)\ndef test_y():\n    assert True\n",
    "import mod\n\n\ndef test_z(x: setattr(mod, 'parse_qty', abs) = 1):\n    assert True\n",
    "import mod\nf = lambda _=setattr(mod, 'parse_qty', abs): 0\n",
    "import mod\nvars(mod)['parse_qty'] = abs\n",
    "import mod\ndel vars(mod)['parse_qty']\n",
    "import mod\nmod.__dict__['parse_qty'] = abs\n",
    "import mod\nmod.__dict__.update(parse_qty=abs)\n",
    "import mod\nvars(mod).update(parse_qty=abs)\n",
    "exec('import mod; mod.parse_qty = abs')\n",
    "import mod\nfor mod.parse_qty in [abs]:\n    pass\n",
    "import mod\n\n\nclass TestK(setattr(mod, 'parse_qty', abs) or object):\n    def test_k(self):\n"
    "        assert True\n",
    "import mod\n\n\n@(lambda c: setattr(mod, 'parse_qty', abs) or c)\nclass TestK:\n    def test_k(self):\n"
    "        assert True\n",
])
def test_new_test_file_that_patches_at_import_time_is_flagged(repo, capsys, code):
    new = code + "\n\ndef test_extra():\n    assert True\n"
    post = fix_with(repo, {"tests/test_new.py": new})
    rc, out = run(repo, post, capsys=capsys)
    assert rc == 1, out
    assert " - KEPT: tests/test_new.py: new test file patches at import time: " in out


DOUBLE_PRE = BUGGY + "\n\ndef double(x):\n    return 2 * x\n"
DOUBLE_TEST = "from mod import double\n\n\ndef test_double():\n    assert double(2) == 4\n"


def test_a_new_test_whose_default_argument_patches_the_code_does_not_pass(make_repo, capsys):
    """The fix breaks double(); a new test's default argument repairs it at import, so the whole suite passes."""
    r = make_repo()
    r.pre = r.commit({"mod.py": DOUBLE_PRE, "tests/test_mod.py": BASE_TESTS, "tests/test_other.py": DOUBLE_TEST},
                     "pre")
    proof = REAL_PROOF.replace("import pytest\n", "import pytest\n\nimport mod\n") + (
        "\n\ndef test_x(_=setattr(mod, 'double', lambda x: 2 * x)):\n    assert True\n")
    post = r.commit({"mod.py": FIXED + "\n\ndef double(x):\n    return 5\n", "tests/test_mod.py": proof}, "fix")
    rc, out = run(r, post, capsys=capsys)
    assert rc == 1, out
    assert (" - KEPT: tests/test_mod.py: new test code patches at import time: "
            "setattr(mod, 'double', lambda x: 2 * x)") in out


@pytest.mark.parametrize("new", [
    "\n\n" "@pytest.mark.parametrize('v', [setattr(mod, 'parse_qty', abs)])\ndef test_x(v):\n    assert True\n",
    "\n\ndef test_x(*, _=vars(mod).update(parse_qty=abs)):\n    assert True\n",
    "\n\nclass TestK(setattr(mod, 'parse_qty', abs) or object):\n    def test_k(self):\n        assert True\n",
    "\n\n@(lambda c: setattr(mod, 'parse_qty', abs) or c)\nclass TestK:\n    def test_k(self):\n"
    "        assert True\n",
])
def test_new_test_code_in_an_existing_file_that_patches_at_import_time_is_flagged(repo, capsys, new):
    proof = REAL_PROOF.replace("import pytest\n", "import pytest\n\nimport mod\n") + new
    post = fix_with(repo, {"tests/test_mod.py": proof})
    rc, out = run(repo, post, capsys=capsys)
    assert rc == 1, out
    assert " - KEPT: tests/test_mod.py: new test code patches at import time: " in out


def test_new_test_method_in_an_existing_class_that_patches_at_import_time_is_flagged(ut_repo, capsys):
    new = "\n    def test_neg2(self, _=setattr(parse_qty, 'x', 1)):\n        assert True\n"
    post = ut_repo.commit({"mod.py": FIXED, "tests/test_ut.py": UT_PRE + UT_NEW + new}, "fix")
    rc, out = run(ut_repo, post, capsys=capsys)
    assert rc == 1, out
    assert " - KEPT: tests/test_ut.py: new test code patches at import time: setattr(parse_qty, 'x', 1)" in out


def test_patching_inside_a_new_test_body_is_not_detected(repo, capsys):
    """A documented limit: a monkeypatch inside a test body is not seen."""
    new = ("import mod\n\n\ndef test_patch(monkeypatch):\n    monkeypatch.setattr(mod, 'X', 1, raising=False)\n"
           "    assert mod.X == 1\n")
    post = fix_with(repo, {"tests/test_new.py": new})
    rc, out = run(repo, post, capsys=capsys)
    assert rc == 0, out


def test_new_test_file_that_does_not_parse_is_flagged(repo, capsys):
    post = fix_with(repo, {"tests/test_new.py": "def (:\n"})
    rc, out = run(repo, post, capsys=capsys)
    assert rc == 1, out
    assert " - KEPT: tests/test_new.py at POST does not parse: SyntaxError" in out


# ------------------------------------------------------------------ 5 RED must be an assertion failure
def test_new_test_that_passes_on_the_unfixed_code_proves_nothing(repo, capsys):
    vacuous = BASE_TESTS + "\n\ndef test_negative_refused():\n    assert int('3') == 3\n"
    post = repo.commit({"tests/test_mod.py": vacuous}, "fake fix")
    rc, out = run(repo, post, capsys=capsys)
    assert rc == 1
    assert " - RED: no new test fails with an assertion on the unfixed code; the proof proves nothing" in out
    assert "KEPT" not in out


def test_fixed_constant_with_attribute_error_at_pre_is_red_unproven(repo, capsys):
    tests = "import mod\n" + BASE_TESTS + "\n\ndef test_fixed():\n    assert mod.FIXED\n"
    post = repo.commit({"mod.py": BUGGY + "\nFIXED = True\n", "tests/test_mod.py": tests}, "fake fix")
    rc, out = run(repo, post, capsys=capsys)
    assert rc == 1, out
    assert out.startswith("FAIL new=1 red_new=0 red_unproven=1 green=suite\n")
    assert (" - RED_UNPROVEN: 1 new test(s) fail at PRE with an error, not an assertion failure, which proves "
            "nothing about the fix: ['tests.test_mod::test_fixed (AttributeError)'] (accept with --allow-error-red)"
            ) in out
    rc, out = run(repo, post, "--allow-error-red", capsys=capsys)
    assert rc == 0, out


@pytest.mark.parametrize("body,kind", [
    ("    assert missing_name() == 1\n", "NameError"),
    ("    assert parse_qty('1', strict=True) == 1\n", "TypeError"),
    ("    import no_such_module_x\n    assert no_such_module_x\n", "ModuleNotFoundError"),
])
def test_non_assertion_errors_at_pre_are_red_unproven(repo, capsys, body, kind):
    tests = REAL_PROOF + "\n\ndef test_err():\n" + body
    post = repo.commit({"mod.py": FIXED + "\n\ndef missing_name():\n    return 1\n", "tests/test_mod.py": tests}, "f")
    rc, out = run(repo, post, capsys=capsys)
    assert rc == 1, out
    assert f"tests.test_mod::test_err ({kind})" in out


def test_collection_error_at_pre_is_red_unproven_and_fails_by_default(repo, capsys):
    files = {"mod.py": FIXED + "\n\ndef is_valid(text):\n    return int(text) >= 0\n",
             "tests/test_valid.py": "from mod import is_valid\n\n\ndef test_valid():\n"
                                    "    assert is_valid('3') and not is_valid('-3')\n"}
    post = repo.commit(files, "fix")
    rc, out = run(repo, post, capsys=capsys)
    assert rc == 1, out
    assert out.startswith("FAIL new=1 red_new=0 red_unproven=1")
    assert "tests.test_valid::test_valid (not collected)" in out
    rc, out = run(repo, post, "--allow-error-red", capsys=capsys)
    assert rc == 0, out
    assert out.startswith("PASS new=1 red_new=1 red_unproven=1")


def test_pytest_fail_and_unittest_asserts_count_as_red(repo, capsys):
    tests = ("import unittest\n\nimport pytest\n\n" + BASE_TESTS
             + "\n\ndef test_fail():\n    try:\n        parse_qty('-1')\n    except ValueError:\n        return\n"
               "    pytest.fail('accepted a negative')\n"
             + "\n\nclass TestNeg(unittest.TestCase):\n    def test_neg(self):\n"
               "        self.assertRaises(ValueError, parse_qty, '-1')\n")
    post = repo.commit({"mod.py": FIXED, "tests/test_mod.py": tests}, "fix")
    rc, out = run(repo, post, capsys=capsys)
    assert rc == 0, out
    assert out.startswith("PASS new=2 red_new=2")


def test_read_junit_takes_the_exception_type_from_the_report(tmp_path):
    xml = tmp_path / "j.xml"
    xml.write_text('<testsuites><testsuite><testcase classname="a" name="t1"><failure message="m">x\n'
                   'a.py:3: AssertionError\n</failure></testcase><testcase classname="a" name="t2">'
                   '<failure message="AssertionError: m">a.py:4: AttributeError</failure></testcase>'
                   '<testcase classname="" name="b"><error message="collection failure">E boom</error></testcase>'
                   '<testcase classname="a" name="t3"><skipped/></testcase><testcase classname="a" name="t4"/>'
                   '<testcase classname="a" name="t5"><failure message="m">no location line</failure></testcase>'
                   '</testsuite></testsuites>')
    assert redgreen.read_junit(str(xml)) == {
        ("a", "t1"): ("failure", "AssertionError"), ("a", "t2"): ("failure", "AttributeError"),
        ("", "b"): ("error", "collection error"), ("a", "t3"): ("skip", ""), ("a", "t4"): ("pass", ""),
        ("a", "t5"): ("failure", "")}
    assert redgreen.read_junit(str(tmp_path / "missing.xml")) == {}


# ------------------------------------------------------------------ 6 GREEN runs the whole suite
OTHER = {"tests/test_other.py": "from mod import double\n\n\ndef test_double():\n    assert double(2) == 4\n"}


def test_a_fix_that_breaks_an_unrelated_test_fails_green(make_repo, capsys):
    r = make_repo()
    r.pre = r.commit({"mod.py": BUGGY + "\n\ndef double(x):\n    return 2 * x\n",
                      "tests/test_mod.py": BASE_TESTS, **OTHER}, "pre")
    post = r.commit({"mod.py": FIXED + "\n\ndef double(x):\n    return 5\n", "tests/test_mod.py": REAL_PROOF}, "fix")
    rc, out = run(r, post, capsys=capsys)
    assert rc == 1, out
    assert " - GREEN: 1 test(s) fail at POST (suite): ['tests.test_other::test_double']" in out
    rc, out = run(r, post, "--green-only-changed", capsys=capsys)
    assert rc == 0, out
    assert out == "PASS new=1 red_new=1 green=changed\n"


def test_tests_red_at_post_fail_green(repo, capsys):
    post = repo.commit({"tests/test_mod.py": REAL_PROOF}, "proof only, no fix")
    rc, out = run(repo, post, capsys=capsys)
    assert rc == 1
    assert " - GREEN: 1 test(s) fail at POST (suite): ['tests.test_mod::test_negative_refused']" in out
    assert " - GREEN: new test(s) not passing at POST: ['tests.test_mod::test_negative_refused']" in out


def test_a_new_test_skipped_at_post_fails_green(repo, capsys):
    skipping = REAL_PROOF + "\n\ndef test_later():\n    import unittest\n    raise unittest.SkipTest('later')\n"
    post = repo.commit({"mod.py": FIXED, "tests/test_mod.py": skipping}, "fix")
    rc, out = run(repo, post, capsys=capsys)
    assert rc == 1, out
    assert " - GREEN: new test(s) not passing at POST: ['tests.test_mod::test_later']" in out


def test_no_new_tests_fails(repo, capsys):
    post = repo.commit({"mod.py": FIXED}, "code only")
    rc, out = run(repo, post, capsys=capsys)
    assert rc == 1
    assert out == "FAIL new=0 red_new=0 green=suite\n - RED: no new test functions in PRE..POST; nothing was shown red\n"


# ------------------------------------------------------------------ 7 arguments, paths, processes, errors
def test_tests_path_outside_the_tree_is_refused_before_anything_opens(repo, tmp_path, capsys):
    canary = tmp_path / "canary.py"
    canary.write_text("keep\n")
    post = fix_with(repo, {})
    for bad in ["../canary.py", "../../x.py", str(canary), "tests/../../canary.py"]:
        assert redgreen.main([str(repo.path), repo.pre, post, "--tests", bad]) == 2, bad
        assert "pbp redgreen: --tests path must be relative and inside TREE: " in capsys.readouterr().err
    assert canary.read_text() == "keep\n"


def test_tests_through_a_symlink_out_of_the_tree_is_refused(repo, tmp_path, capsys):
    (tmp_path / "outside").mkdir()
    os.symlink(tmp_path / "outside", repo.path / "link")
    post = fix_with(repo, {})
    capsys.readouterr()
    assert redgreen.main([str(repo.path), repo.pre, post, "--tests", "link/test_x.py"]) == 2
    assert capsys.readouterr().err == "pbp redgreen: --tests path must be relative and inside TREE: link/test_x.py\n"
    assert redgreen.main([str(repo.path), repo.pre, post, "--out", "link/r.json"]) == 2
    assert capsys.readouterr().err == "pbp redgreen: --out path must be relative and inside TREE: link/r.json\n"
    assert not list((tmp_path / "outside").iterdir())


def test_a_symlinked_test_folder_at_pre_is_never_written_through(repo, tmp_path, capsys):
    outside = tmp_path / "outside"
    outside.mkdir()
    r = repo
    (r.path / "tests" / "test_mod.py").unlink()
    (r.path / "tests").rmdir()
    os.symlink(outside, r.path / "tests")                      # PRE commits `tests` as a link out of the tree
    r.pre = r.commit({}, "pre with a symlinked tests folder")
    (r.path / "tests").unlink()
    post = r.commit({"mod.py": FIXED, "tests/test_mod.py": REAL_PROOF}, "fix")
    capsys.readouterr()
    assert redgreen.main([str(r.path), r.pre, post]) == 2
    assert ("pbp redgreen: refusing to write tests/test_mod.py at PRE: a symlink in the PRE commit leads outside "
            "the worktree") in capsys.readouterr().err
    assert list(outside.iterdir()) == []
    assert len(r.git("worktree", "list").splitlines()) == 1


def test_a_symlinked_test_file_at_pre_is_replaced_not_written_through(repo, tmp_path, capsys):
    target = tmp_path / "outside.py"
    target.write_text("keep\n")
    (repo.path / "tests" / "test_mod.py").unlink()
    os.symlink(target, repo.path / "tests" / "test_mod.py")   # PRE commits the test file as a link out
    repo.pre = repo.commit({}, "pre with a symlinked test file")
    (repo.path / "tests" / "test_mod.py").unlink()
    post = repo.commit({"mod.py": FIXED, "tests/test_mod.py": REAL_PROOF}, "fix")
    rc, out = run(repo, post, capsys=capsys)
    assert rc == 1, out                    # a verdict (the link at PRE does not parse as a test file), no crash
    assert " - KEPT: tests/test_mod.py at PRE does not parse: " in out
    assert target.read_text() == "keep\n"


def test_out_path_outside_the_tree_is_refused(repo, tmp_path, capsys):
    post = fix_with(repo, {})
    assert redgreen.main([str(repo.path), repo.pre, post, "--out", str(tmp_path / "r.json")]) == 2
    assert redgreen.main([str(repo.path), repo.pre, post, "--out", "../r.json"]) == 2
    assert not (tmp_path / "r.json").exists()
    assert "--out path must be relative and inside TREE" in capsys.readouterr().err


@pytest.mark.parametrize("tail", [["--tests"], ["--out"], ["--bogus"], []])
def test_bad_arguments_exit_2(repo, capsys, tail):
    args = [str(repo.path), repo.pre, *tail] if not tail else [str(repo.path), repo.pre, "HEAD", *tail]
    assert redgreen.main(args) == 2


def test_options_may_come_before_the_positionals(repo, capsys):
    post = fix_with(repo, {})
    rc = redgreen.main(["--tests", "tests/test_mod.py", str(repo.path), repo.pre, post])
    assert rc == 0, capsys.readouterr().out


def test_out_writes_the_report_inside_the_tree_and_leaves_no_worktrees(repo, capsys):
    post = fix_with(repo, {})
    rc, _ = run(repo, post, "--out", "report.json", capsys=capsys)
    assert rc == 0
    data = json.loads((repo.path / "report.json").read_text())
    assert data["verdict"] == "PASS" and data["reasons"] == []
    assert data["files"] == ["tests/test_mod.py"] and data["green_mode"] == "suite"
    assert data["red_failing_new"] == ["tests.test_mod::test_negative_refused"]
    assert len(repo.git("worktree", "list").splitlines()) == 1


def test_explicit_tests_still_check_every_changed_test_file(make_repo, capsys):
    r = make_repo()
    r.pre = r.commit({"mod.py": BUGGY, "tests/test_mod.py": BASE_TESTS, "tests/test_ut.py": UT_PRE}, "pre")
    weak = UT_PRE.replace("self.assertEqual(parse_qty('7'), 7)", "self.assertTrue(True)")
    post = r.commit({"mod.py": FIXED, "tests/test_mod.py": REAL_PROOF, "tests/test_ut.py": weak}, "fix")
    rc, out = run(r, post, "--tests", "tests/test_mod.py", capsys=capsys)
    assert rc == 1, out
    assert " - KEPT: tests/test_ut.py::TestRead::test_seven changed" in out


def test_a_test_file_outside_the_test_roots_counts(repo, capsys):
    post = repo.commit({"mod.py": FIXED, "test_root.py": REAL_PROOF.replace("test_reads", "test_reads_root")}, "fix")
    rc, out = run(repo, post, capsys=capsys)
    assert rc == 0, out
    assert out.startswith("PASS new=2 red_new=1")


def test_timeout_kills_the_whole_process_group(repo, tmp_path, capsys):
    pidfile = tmp_path / "child.pid"
    hang = ("import subprocess\nimport time\n\nfrom mod import parse_qty\n\n\ndef test_hang():\n"
            f"    p = subprocess.Popen(['sleep', '300'])\n    open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
            "    time.sleep(300)\n    assert parse_qty('-1') == 1\n")
    post = repo.commit({"mod.py": FIXED, "tests/test_hang.py": hang}, "hang")
    start = time.monotonic()
    rc, out = run(repo, post, "--timeout", "3", capsys=capsys)
    assert rc == 1, out
    assert time.monotonic() - start < 60
    assert " - TIMEOUT: pytest at PRE ran past 3s and was killed" in out
    pid = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail(f"grandchild {pid} survived the timeout")


def test_sigterm_kills_the_pytest_group_and_removes_the_worktrees(repo, tmp_path):
    import signal
    import subprocess
    import sys
    probe = tmp_path / "pid.txt"
    wait = ("import os\nimport time\n\nfrom mod import parse_qty\n\n\ndef test_wait():\n"
            "    open(os.environ['PBP_PROBE'], 'w').write(str(os.getpid()))\n    time.sleep(120)\n"
            "    assert parse_qty('-1') == 1\n")
    post = repo.commit({"mod.py": FIXED, "tests/test_wait.py": wait}, "wait")
    tmpdir = tmp_path / "tmpdir"
    tmpdir.mkdir()
    env = {**os.environ, "PBP_PROBE": str(probe), "TMPDIR": str(tmpdir)}
    proc = subprocess.Popen([sys.executable, "-B", "-m", "pbp", "redgreen", str(repo.path), repo.pre, post,
                             "--timeout", "300"], env=env, cwd=Path(redgreen.__file__).resolve().parents[1],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pid = None
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not (probe.exists() and probe.read_text()):
            time.sleep(0.1)
        pid = int(probe.read_text())
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=60) == 128 + signal.SIGTERM
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            pytest.fail("the pytest child outlived redgreen")
        assert sorted(p.name for p in tmpdir.iterdir()) == []
        assert len(repo.git("worktree", "list").splitlines()) == 1
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_an_unexpected_error_exits_2_with_one_line(repo, capsys, monkeypatch):
    post = fix_with(repo, {})

    def boom(*a):
        raise RuntimeError("surprise\nsecond line")
    monkeypatch.setattr(redgreen, "changed_files", boom)
    assert redgreen.main([str(repo.path), repo.pre, post]) == 2
    err = capsys.readouterr().err
    assert err == "pbp redgreen: unexpected error: RuntimeError: surprise\n"


def test_an_unknown_commit_or_a_non_repo_is_a_usage_error(repo, tmp_path, capsys):
    assert redgreen.main([str(repo.path), "HEAD", "no-such-commit"]) == 2
    assert "pbp redgreen: cannot resolve PRE/POST in TREE" in capsys.readouterr().err
    assert redgreen.main([str(tmp_path / "nowhere"), "a", "b"]) == 2
    assert "pbp redgreen: TREE is not a folder: " in capsys.readouterr().err


def test_explicit_tests_file_missing_at_post_is_a_usage_error(repo, capsys):
    post = fix_with(repo, {})
    assert redgreen.main([str(repo.path), repo.pre, post, "--tests", "tests/test_nope.py"]) == 2
    assert "pbp redgreen: --tests file not in POST: tests/test_nope.py" in capsys.readouterr().err


def test_module_docstring_change_in_an_existing_test_file_is_allowed(repo, capsys):
    post = repo.commit({"mod.py": FIXED, "tests/test_mod.py": '"""Quantity tests."""\n' + REAL_PROOF}, "fix")
    rc, out = run(repo, post, capsys=capsys)
    assert rc == 0, out


def test_harmless_module_level_code_in_a_new_test_file_is_allowed(repo, capsys):
    new = "CASES = list(range(3))\n\n\ndef test_cases():\n    assert len(CASES) == 3\n"
    rc, out = run(repo, fix_with(repo, {"tests/test_new.py": new}), capsys=capsys)
    assert rc == 0, out


@pytest.mark.parametrize("path,before,word", [("pytest.toml", None, "added"), ("pytest.toml", "x = 1\n", "changed"),
                                              ("pytest.ini", "[pytest]\n", "changed")])
def test_config_reason_names_what_happened(make_repo, capsys, path, before, word):
    r = make_repo()
    r.pre = r.commit({"mod.py": BUGGY, "tests/test_mod.py": BASE_TESTS, **({path: before} if before else {})}, "pre")
    post = r.commit({"mod.py": FIXED, "tests/test_mod.py": REAL_PROOF, path: "[pytest]\naddopts = -x\n"}, "fix")
    rc, out = run(r, post, capsys=capsys)
    assert rc == 1, out
    assert f" - KEPT: {path}: pytest configuration {word}\n" in out


def test_a_test_named_function_in_a_code_file_is_not_a_new_test(repo, capsys):
    post = repo.commit({"mod.py": FIXED + "\n\ndef test_mode():\n    return 1\n", "tests/test_mod.py": REAL_PROOF}, "fix")
    rc, out = run(repo, post, capsys=capsys)
    assert rc == 0, out
    assert out.startswith("PASS new=1 red_new=1")


def test_pytest_that_exits_nonzero_without_a_failed_test_fails_green(repo, capsys):
    post = fix_with(repo, {"conftest.py": "raise RuntimeError('broken setup')\n"})
    rc, out = run(repo, post, capsys=capsys)
    assert rc == 1, out
    assert " - GREEN: pytest exited rc=" in out and "at POST (suite)" in out


def test_refuse_under_blocks_a_protected_tree(repo, capsys):
    post = fix_with(repo, {})
    rc, out = run(repo, post, "--refuse-under", str(repo.path.parent), capsys=capsys)
    assert rc == 2
    assert out.startswith("refusing: TREE is under ")


def test_refuse_under_an_unrelated_dir_still_runs(repo, tmp_path, capsys):
    post = fix_with(repo, {})
    other = tmp_path / "elsewhere"
    other.mkdir()
    rc, out = run(repo, post, "--refuse-under", str(other), capsys=capsys)
    assert rc == 0, out


def test_pbp_lock_is_held_during_each_pytest_run(repo, tmp_path, capsys, monkeypatch):
    lock = tmp_path / "pbp.lock"
    monkeypatch.setattr(redgreen, "LOCK", str(lock))
    seen = []
    real = redgreen.run_cmd

    def spy(cmd, cwd, timeout, log_path, env):
        import fcntl
        with open(lock, "a") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                seen.append("free")
            except BlockingIOError:
                seen.append("held")
        return real(cmd, cwd, timeout, log_path, env)
    monkeypatch.setattr(redgreen, "run_cmd", spy)
    rc, out = run(repo, fix_with(repo, {}), capsys=capsys)
    assert rc == 0, out
    assert seen == ["held", "held"]


# ------------------------------------------------------------------ 8 exit codes and 9 the docstring
def test_help_exits_0_and_shows_the_title(capsys):
    assert redgreen.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: python -m pbp redgreen ")
    assert ("redgreen: do the new tests fail before the change and pass after it, with the existing tests "
            "untouched?") in out
    assert redgreen.main(["only-one-arg"]) == 2
    assert "usage:" in capsys.readouterr().err


def test_docstring_names_what_it_does_not_check():
    doc = redgreen.__doc__
    assert doc.splitlines()[0] == ("redgreen: do the new tests fail before the change and pass after it, "
                                   "with the existing tests untouched?")
    head, limits = doc.split("What it does not check:")
    assert "What it checks" in head
    for phrase in ["correct or complete fix", "patching or stubbing inside a new test body",
                   "trivially true only after the change", "non-Python fixtures or data files",
                   "tests generated at run time", "use it only on code you would run anyway",
                   "Patching inside a test body or a fixture"]:
        assert phrase in doc, phrase


def test_no_names_or_host_paths_in_the_module():
    src = Path(redgreen.__file__).read_text()
    for host_path in ("/" + "home/", "/" + "var/tmp/"):
        assert host_path not in src
