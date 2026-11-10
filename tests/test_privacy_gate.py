"""privacy_gate: each pattern is caught, reserved examples are allowed, the deny-list stays outside the repo."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[1] / "tools" / "privacy_gate.py"
HOME = "/" + "home" + "/"
VARTMP = "/" + "var" + "/" + "tmp" + "/"
# Secret-shaped fixtures are joined at run time so that no file in this repository holds one.
FAKE = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0"         # 40 characters, not a real key


def j(*parts):
    return "".join(parts)


@pytest.fixture()
def gate():
    spec = importlib.util.spec_from_file_location("privacy_gate_under_test", GATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def root(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    (r / "clean.py").write_text("x = 1  # nothing here\n")
    return r


def test_clean_tree_passes(gate, root, capsys):
    assert gate.main([str(root)]) == 0
    assert "0 hit(s)" in capsys.readouterr().out


@pytest.mark.parametrize("text,kind", [
    pytest.param("contact: someone" + "@" + "mail-provider.com\n", "email", id="email"),
    pytest.param("cd " + HOME + "someone/project\n", "home-path", id="home-path"),
    pytest.param("scratch in " + VARTMP + "job\n", "var-tmp-path", id="var-tmp-path"),
    pytest.param("server at 203.0.113." + "7 and more\n", "ip-address", id="ip-public"),
    pytest.param("gateway 10.1.2." + "3.\n", "ip-address", id="ip-private-end-of-sentence"),
    pytest.param(j("mail ", "someone", "@", "localhost\n"), "email", id="email-localhost"),
    pytest.param(j("open /", "Users", "/someone/project\n"), "mac-home-path", id="mac-home"),
    pytest.param(j("open C:", "\\", "Users", "\\", "someone\\Desktop\n"), "windows-home-path", id="win-home"),
    pytest.param(j('"C:', "\\\\", "Users", "\\\\", 'someone"\n'), "windows-home-path", id="win-home-json-escaped"),
    pytest.param(j("open d:/", "users", "/someone/x\n"), "windows-home-path", id="win-home-forward-slash"),
    pytest.param(j("see ", "~", "someone/notes\n"), "tilde-path", id="tilde-user"),
    pytest.param(j("see ", "~", "/private/notes\n"), "tilde-path", id="tilde-plain-folder"),
    pytest.param(j("key = ", "sk-", "ant-", "api03-", FAKE, "\n"), "secret", id="key-sk-ant"),
    pytest.param(j("key = ", "sk-", "proj-", FAKE, "\n"), "secret", id="key-sk-proj"),
    pytest.param(j("token: ", "gh", "p_", FAKE[:36], "\n"), "secret", id="token-ghp"),
    pytest.param(j("token: ", "github", "_pat_", FAKE, "\n"), "secret", id="token-pat"),
    pytest.param(j("token: ", "gl", "pat-", FAKE[:20], "\n"), "secret", id="token-glpat"),
    pytest.param(j("id=", "AK", "IA", "ABCDEFGHIJ234567", "\n"), "secret", id="key-akia"),
    pytest.param(j("key=", "AI", "za", FAKE[:35], "\n"), "secret", id="key-aiza"),
    pytest.param(j("hook ", "xo", "xb-", "1234567890-abcdef\n"), "secret", id="token-xox"),
    pytest.param(j("token ", "h", "f_", FAKE[:34], "\n"), "secret", id="token-hf"),
    pytest.param(j("key ", "sk", "_live_", FAKE[:24], "\n"), "secret", id="key-sk-live"),
    pytest.param(j("-----BEGIN ", "RSA ", "PRIVATE", " KEY-----\n"), "private-key", id="pem-rsa"),
    pytest.param(j("-----BEGIN ", "OPENSSH ", "PRIVATE", " KEY-----\n"), "private-key", id="pem-openssh"),
    pytest.param(j("-----BEGIN ", "PRIVATE", " KEY-----\n"), "private-key", id="pem-pkcs8"),
])
def test_each_pattern_is_a_hit(gate, root, capsys, text, kind):
    (root / "notes.md").write_text("line one\n" + text)
    assert gate.main([str(root)]) == 1
    out = capsys.readouterr().out
    assert f"notes.md:2: {kind}:" in out


def test_a_secret_is_reported_shortened_never_in_full(gate, root, capsys):
    secret = j("sk-", "ant-", "api03-", FAKE)
    (root / "conf.py").write_text(f"KEY = '{secret}'\n")
    assert gate.main([str(root)]) == 1
    out = capsys.readouterr().out
    assert "conf.py:1: secret:" in out
    assert secret not in out and FAKE[:10] not in out


@pytest.mark.parametrize("text", [
    "demo" + "@" + "example.invalid\n",
    "a" + "@" + "example.com\n",
    "@pytest.fixture\n",
    "bind 127.0.0.1 or 0.0.0.0\n",
    "version 1.2.3\n",
    "not an address 999.1.1.1\n",
    j("git reads ", "~", "/.gitconfig and ", "~", "/.config/git/config\n"),
    j("a lone ", "~", "/ and a ", "~", " sign\n"),
    j("/", "Users", " alone, and C:", "\\", "Program Files\n"),
    j("pip install scikit-learn; ", "sk-", "short\n"),
    j("-----BEGIN ", "PUBLIC", " KEY-----\n"),
    j("ghp", "_ too short ", "AKIA", "short\n"),
])
def test_reserved_and_non_addresses_are_allowed(gate, root, text):
    (root / "notes.md").write_text(text)
    assert gate.main([str(root)]) == 0


def test_deny_words_match_any_case_and_are_not_echoed(gate, root, tmp_path, capsys):
    deny = tmp_path / "deny.txt"
    deny.write_text("# private words\n\nSecretCo\n")
    (root / "about.md").write_text("built for SECRETCO staff\n")
    (root / "secretco_notes.txt").write_text("fine\n")
    assert gate.main([str(root), "--deny", str(deny)]) == 1
    out = capsys.readouterr().out
    assert "about.md:1: deny-list word on line 3" in out
    assert "secretco_notes.txt: file-name: deny-list word on line 3" in out
    assert "built for" not in out


def test_deny_list_inside_the_repo_is_refused(gate, root, capsys):
    deny = root / "deny.txt"
    deny.write_text("word\n")
    assert gate.main([str(root), "--deny", str(deny)]) == 2


def test_empty_deny_list_is_refused(gate, root, tmp_path):
    deny = tmp_path / "deny.txt"
    deny.write_text("# only a comment\n")
    assert gate.main([str(root), "--deny", str(deny)]) == 2


def test_git_dir_is_skipped(gate, root):
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("email = someone" + "@" + "mail-provider.com\n")
    assert gate.main([str(root)]) == 0


def test_text_files_in_cache_folders_are_scanned_but_bytecode_is_not(gate, root, capsys):
    cache = root / "docs" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "mod.cpython-311.pyc").write_bytes(j("x", "@", "mail-provider.com").encode())
    assert gate.main([str(root)]) == 0
    (cache / "notes.md").write_text(j("contact: someone", "@", "mail-provider.com\n"))
    assert gate.main([str(root)]) == 1
    assert "notes.md:1: email" in capsys.readouterr().out


def test_a_symlink_is_reported_not_skipped(gate, root, tmp_path, capsys):
    (tmp_path / "outside.txt").write_text("x\n")
    os.symlink(tmp_path / "outside.txt", root / "link.txt")
    assert gate.main([str(root)]) == 1
    assert "link.txt: symlink (not followed) -> " in capsys.readouterr().out


def test_git_log_scans_author_emails_and_messages(gate, root, capsys, monkeypatch):
    import subprocess
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": j("someone", "@", "mail-provider.com"),
           "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@example.invalid"}
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "--no-verify", "-m", "first"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)
    assert gate.main([str(root)]) == 0                      # the files are clean
    assert gate.main([str(root), "--git-log"]) == 1
    assert ": email: someone" in capsys.readouterr().out
    monkeypatch.chdir(root.parent)                          # a relative ROOT finds the same history
    assert gate.main([root.name, "--git-log"]) == 1


def git_repo(path, author_email, committer_email, message):
    """A fresh one-commit repository at PATH with the given e-mail fields and message."""
    import subprocess
    path.mkdir()
    (path / "clean.py").write_text("x = 1\n")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": author_email,
           "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": committer_email}
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "--no-verify", "-m", message]):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, env=env)
    return path


@pytest.mark.parametrize("address,no_reply", [
    (j("noreply", "@", "mail-provider.com"), True),
    (j("no-reply", "@", "mail-provider.com"), True),
    (j("1234+someone", "@", "users.noreply.", "code-host.com"), True),
    (j("someone.noreply", "@", "mail-provider.com"), False),
    (j("someone", "@", "noreply.mail-provider.com"), False),
])
def test_only_addresses_that_cannot_receive_mail_are_let_through(gate, root, tmp_path, capsys, address, no_reply):
    neutral = "a" + "@" + "example.invalid"
    # in file text every one of them is a hit (a no-reply address can still name a company or an account)
    (root / "notes.md").write_text(f"contact {address} now\n")
    assert gate.main([str(root)]) == 1
    assert f"notes.md:1: email: {address}" in capsys.readouterr().out
    # in the author or committer e-mail field of a commit, only a no-reply address is let through
    for field, emails in (("author", (address, neutral)), ("committer", (neutral, address))):
        repo = git_repo(tmp_path / f"by_{field}", *emails, "first")
        assert gate.main([str(repo), "--git-log"]) == (0 if no_reply else 1), field
        assert (f": email: {address}" in capsys.readouterr().out) is not no_reply, field
    # the same address inside a commit message is a hit
    repo = git_repo(tmp_path / "in_message", neutral, neutral, f"thanks {address}")
    assert gate.main([str(repo), "--git-log"]) == 1
    assert f":5: email: {address}" in capsys.readouterr().out
    # a co-author trailer line is one more author e-mail field: only a no-reply address is let through there,
    # and only when the line starts with the trailer key
    repo = git_repo(tmp_path / "in_trailer", neutral, neutral, f"first\n\nCo-Authored-By: A Tool <{address}>")
    assert gate.main([str(repo), "--git-log"]) == (0 if no_reply else 1)
    assert (f":7: email: {address}" in capsys.readouterr().out) is not no_reply
    # a message cannot fake a new record (an e-mail field) with a separator byte
    fake = f"subject\n\n\x1edeadbeef\nn\n{address}\nn\n{address}\nbody"
    repo = git_repo(tmp_path / "fake_record", neutral, neutral, fake)
    assert gate.main([str(repo), "--git-log"]) == 1
    assert f": email: {address}" in capsys.readouterr().out
    repo = git_repo(tmp_path / "mid_line", neutral, neutral, f"see co-authored-by: {address}")
    assert gate.main([str(repo), "--git-log"]) == 1
    capsys.readouterr()


def test_git_log_outside_a_repo_is_a_usage_error(gate, root):
    assert gate.main([str(root), "--git-log"]) == 2
