"""The four shapes of a test that cannot fail (demo/shapes/): mutate must report surviving mutants for each."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pbp import mutate
from pbp._common import git_env

SHAPES = Path(__file__).resolve().parents[1] / "demo" / "shapes"


@pytest.fixture(scope="module")
def shapes_repo(tmp_path_factory):
    import subprocess
    repo = tmp_path_factory.mktemp("shapes")
    for src in SHAPES.rglob("*.py"):
        dst = repo / src.relative_to(SHAPES)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "--no-verify", "-m", "shapes"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=git_env())
    return repo


@pytest.mark.parametrize("module,tests", [
    ("discount.py", "tests/test_same_call.py"),
    ("totals.py", "tests/test_expected_from_code.py"),
    ("flags.py", "tests/test_or_true.py"),
    ("table.py", "tests/test_cell_count.py"),
])
def test_mutate_reports_survivors_for_each_shape(shapes_repo, tmp_path, capsys, module, tests):
    out = tmp_path / "result.json"
    rc = mutate.main(["--repo", str(shapes_repo), "--module", module, "--tests", tests, "--timeout", "60",
                      "--json", str(out)])
    assert rc == 0, capsys.readouterr()
    result = json.loads(out.read_text())
    assert result["total"] > 0
    assert result["survived"] > 0, f"{module}: no mutant survived the cannot-fail test {tests}"
