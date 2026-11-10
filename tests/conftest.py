"""Shared helpers: throwaway git repos with a neutral identity, never the host's git config."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from pbp._common import GIT_ENV


@pytest.fixture(scope="session", autouse=True)
def _neutral_git_identity():
    """Every git call in the tests (ours and the tools') uses the neutral identity and no global config."""
    old = {k: os.environ.get(k) for k in GIT_ENV}
    os.environ.update(GIT_ENV)
    os.environ.pop("PYTEST_ADDOPTS", None)
    yield
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


class Repo:
    def __init__(self, path: Path):
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args: str) -> str:
        r = subprocess.run(["git", "-C", str(self.path), *args], capture_output=True, text=True,
                           env={**os.environ, **GIT_ENV}, stdin=subprocess.DEVNULL)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    def write(self, files: dict[str, str]) -> None:
        for rel, text in files.items():
            p = self.path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)

    def commit(self, files: dict[str, str], message: str = "c", remove: tuple[str, ...] = ()) -> str:
        self.write(files)
        for rel in remove:
            (self.path / rel).unlink()
        self.git("add", "-A")
        self.git("commit", "-q", "--no-verify", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")


@pytest.fixture()
def make_repo(tmp_path):
    def make(name: str = "repo") -> Repo:
        return Repo(tmp_path / name)
    return make
