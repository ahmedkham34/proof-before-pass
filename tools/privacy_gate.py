#!/usr/bin/env python3
"""privacy_gate - fail when a repository holds personal or machine-specific data.

What it checks: that no file in the tree carries an email address, a path inside a user's home folder (the
/home, /Users and drive-letter Users styles, or a tilde path that names a user or a plain folder in a home), a path in the system's persistent
temp folder, a public IP address, a secret-shaped string (common API key and token prefixes, PEM private-key
headers), or any word from a private deny-list. It is a floor, not a proof of privacy: names, places and secrets
in other shapes are only caught through the deny-list.

usage: python3 tools/privacy_gate.py ROOT [--deny FILE] [--git-log]
  ROOT         folder to scan (every file, recursively). The .git folder is skipped; inside __pycache__ and
               .pytest_cache only bytecode (.pyc, .pyo) is skipped. A symlink is never followed and is reported
               as a hit with its target, so a link out of the tree cannot hide.
  --git-log    also scan the author and committer names and e-mails and the messages of every commit
               (`git log --all`); ROOT must be the top of a git repository
  --deny FILE  one word or phrase per line (blank lines and lines starting with # are ignored), matched as a
               case-insensitive substring. The file must live OUTSIDE ROOT, so it is never committed. Hits are
               reported by the word's line number in FILE, never by the word itself.

Allowed: addresses at reserved example domains (example.com/.org/.net, *.example, *.invalid, *.test); loopback and
unspecified IP addresses (127.x.x.x, 0.0.0.0); tilde paths into a dot-folder (a tool's config, such as git's).
Only in the author e-mail and committer e-mail fields of `--git-log`, and on a message line that starts with
`Co-authored-by:` (one more author e-mail field): an address that cannot receive mail, that
is a `noreply`/`no-reply` mailbox or a code host's per-account `users.noreply.` domain (the form commit authors use
to keep their real address private). In file text and in the rest of a commit message these are hits like any other address,
since they can still name a company domain or an account.
Secret-shaped hits are printed shortened, never in full.
Exit 0 clean, 1 at least one hit, 2 usage error.
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pbp._common import git_env, is_within, run_cmd  # noqa: E402

SKIP_DIRS = {".git"}
BYTECODE = (".pyc", ".pyo")
# Built from pieces so that this file does not match its own patterns.
_HOME = "/" + "home" + "/"
_VARTMP = "/" + "var" + "/" + "tmp" + "/"
_MAC_HOME = "/" + "Users" + "/"
_SECRET = (r"(?<![A-Za-z0-9])(?:"
           r"sk-ant-[A-Za-z0-9_-]{20,}"                     # sk-ant- keys
           r"|sk-(?:proj-)?[A-Za-z0-9_-]{20,}"              # sk- and sk-proj- keys
           r"|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"   # gh?_ and github_pat_ tokens
           r"|glpat-[A-Za-z0-9_-]{20,}"                     # glpat- tokens
           r"|(?:AKIA|ASIA)[0-9A-Z]{16}"                    # AKIA/ASIA access key ids
           r"|AIza[0-9A-Za-z_-]{35}"                        # AIza keys
           r"|xox[abprs]-[A-Za-z0-9-]{10,}"                 # xox?- tokens
           r"|hf_[A-Za-z0-9]{30,}"                          # hf_ tokens
           r"|[sr]k_live_[A-Za-z0-9]{20,}"                  # sk_live_/rk_live_ keys
           r")")
PATTERNS = (
    ("email", re.compile(r"[A-Za-z0-9._%+-]+" + "@" + r"([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*)")),
    ("home-path", re.compile(re.escape(_HOME) + r"[A-Za-z0-9._-]+")),
    ("var-tmp-path", re.compile(re.escape(_VARTMP))),
    ("ip-address", re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\w|\.\d)")),
    ("mac-home-path", re.compile(re.escape(_MAC_HOME) + r"[A-Za-z0-9._-]+")),
    ("windows-home-path", re.compile(r"(?<![A-Za-z])[A-Za-z]:(?:\\|/)+(?i:users)(?:\\|/)+[A-Za-z0-9._ -]*[A-Za-z0-9_-]")),
    # a tilde then a user name then a slash, or a tilde-slash path whose first folder is not a dot-folder
    ("tilde-path", re.compile(r"(?<![\w~])~(?:[A-Za-z_][A-Za-z0-9._-]*/|/[A-Za-z0-9_][^\s/\"'`)]*)")),
    ("secret", re.compile(_SECRET)),
    ("private-key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
)
SECRET_KINDS = {"secret", "private-key"}
ALLOWED_DOMAINS = ("example.com", "example.org", "example.net")
ALLOWED_SUFFIXES = (".example", ".invalid", ".test", ".example.com", ".example.org", ".example.net")
NO_REPLY_LOCALS = ("noreply", "no-reply")
NO_REPLY_DOMAIN_PREFIX = "users.noreply."
CO_AUTHOR_TRAILER = "co-authored-by:"


def is_no_reply(match: re.Match) -> bool:
    """A "noreply" mailbox, or a code host's per-account no-reply domain."""
    local, domain = match.group(0).rsplit("@", 1)[0].lower(), match.group(1).lower()
    return local in NO_REPLY_LOCALS or domain.startswith(NO_REPLY_DOMAIN_PREFIX)


def allowed(kind: str, match: re.Match, no_reply_ok: bool = False) -> bool:
    """NO_REPLY_OK is True only for a commit's author and committer e-mail fields."""
    if kind == "email":
        domain = match.group(1).lower()
        # no dot: not an address at all (a decorator, or a version pin like x@1); a bare localhost still counts
        if "." not in domain and domain != "localhost":
            return True
        if domain in ALLOWED_DOMAINS or domain.endswith(ALLOWED_SUFFIXES):
            return True
        return no_reply_ok and is_no_reply(match)
    if kind == "ip-address":
        try:
            ip = ipaddress.ip_address(match.group(0))
        except ValueError:
            return True          # 999.1.1.1 and the like are not addresses
        return ip.is_loopback or ip.is_unspecified
    return False


def load_deny(path: str) -> list[tuple[int, str]]:
    words = []
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            word = line.strip()
            if word and not word.startswith("#"):
                words.append((n, word.lower()))
    return words


def iter_entries(root: str):
    """Yield (path, is_symlink) for every file and every symlink under ROOT, never following a link."""
    for dirpath, dirs, names in os.walk(root):
        for d in sorted(dirs):
            if os.path.islink(os.path.join(dirpath, d)):
                yield os.path.join(dirpath, d), True
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d)))
        for name in sorted(names):
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                yield path, True
            elif name.endswith(BYTECODE) and os.path.basename(dirpath) in ("__pycache__", ".pytest_cache"):
                continue
            elif os.path.isfile(path):
                yield path, False


def scan_text(where: str, text: str, deny: list[tuple[int, str]], no_reply_ok: bool = False,
              first: int = 1) -> list[str]:
    hits = []
    for lineno, line in enumerate(text.splitlines(), first):
        for kind, pattern in PATTERNS:
            for m in pattern.finditer(line):
                if not allowed(kind, m, no_reply_ok):
                    shown = m.group(0)
                    if kind in SECRET_KINDS:
                        shown = shown[:6] + "...(" + str(len(shown)) + " chars, not shown)"
                    hits.append(f"{where}:{lineno}: {kind}: {shown}")
        low = line.lower()
        for n, bad in deny:
            if bad in low:
                hits.append(f"{where}:{lineno}: deny-list word on line {n}")
    return hits


def scan(root: str, deny: list[tuple[int, str]]) -> list[str]:
    hits = []
    for path, is_link in iter_entries(root):
        rel = os.path.relpath(path, root)
        if is_link:
            target = os.readlink(path)
            hits.append(f"{rel}: symlink (not followed) -> {target}")
            hits += scan_text(f"{rel} (link target)", target, deny)
            continue
        try:
            with open(path, "rb") as fh:
                text = fh.read().decode("utf-8", errors="replace")
        except OSError as exc:
            hits.append(f"{rel}: unreadable: {exc.strerror}")
            continue
        low_rel = rel.lower()
        for n, bad in deny:
            if bad in low_rel:
                hits.append(f"{rel}: file-name: deny-list word on line {n}")
        hits += scan_text(rel, text, deny)
    return hits


def scan_git_log(root: str, deny: list[tuple[int, str]]) -> list[str]:
    """Hits in the names, e-mails and messages of every commit; None when ROOT is not a git repository."""
    root = os.path.abspath(root)                             # git runs with cwd=ROOT, so a relative ROOT would be applied twice
    rc, top = run_cmd(["git", "-C", root, "rev-parse", "--show-toplevel"], cwd=root, timeout=60, env=git_env())
    if rc != 0 or os.path.realpath(top.decode("utf-8", "replace").strip()) != os.path.realpath(root):
        return None
    # -z ends each commit with a NUL byte, which no name or message can hold, so a message cannot fake a record
    fmt = "--format=%H%n%an%n%ae%n%cn%n%ce%n%B"
    rc, out = run_cmd(["git", "-C", root, "log", "--all", "-z", fmt], cwd=root, timeout=300, env=git_env())
    if rc != 0:
        return None
    log_hits = []
    for record in out.decode("utf-8", "replace").split("\x00"):
        if record.strip():
            sha, _, body = record.partition("\n")
            where = f"git-log:{sha[:12]}"
            author, author_email, committer, committer_email, message = (body.split("\n", 4) + [""] * 5)[:5]
            # the no-reply allowance holds only in the two e-mail fields and in a co-author trailer line
            # (one more author e-mail field); never in names or in the rest of a message
            for lineno, (field, email_field) in enumerate(((author, False), (author_email, True),
                                                           (committer, False), (committer_email, True)), 1):
                log_hits += scan_text(where, field, deny, no_reply_ok=email_field, first=lineno)
            for lineno, line in enumerate(message.splitlines(), 5):
                log_hits += scan_text(where, line, deny, first=lineno,
                                      no_reply_ok=line.lower().startswith(CO_AUTHOR_TRAILER))
    return log_hits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="privacy_gate.py", description="fail on personal or machine data in a repo")
    ap.add_argument("root")
    ap.add_argument("--deny", metavar="FILE", help="private word list, kept outside ROOT")
    ap.add_argument("--git-log", action="store_true", help="also scan commit names, e-mails and messages")
    a = ap.parse_args(argv)
    if not os.path.isdir(a.root):
        print(f"privacy_gate: not a directory: {a.root}", file=sys.stderr)
        return 2
    deny: list[tuple[int, str]] = []
    if a.deny:
        if is_within(a.deny, a.root):
            print("privacy_gate: the deny-list must live outside the scanned folder", file=sys.stderr)
            return 2
        try:
            deny = load_deny(a.deny)
        except (OSError, UnicodeDecodeError) as exc:
            print(f"privacy_gate: cannot read the deny-list: {exc}", file=sys.stderr)
            return 2
        if not deny:
            print("privacy_gate: the deny-list is empty", file=sys.stderr)
            return 2
    hits = scan(a.root, deny)
    if a.git_log:
        log_hits = scan_git_log(a.root, deny)
        if log_hits is None:
            print("privacy_gate: --git-log needs ROOT to be the top of a git repository", file=sys.stderr)
            return 2
        hits += log_hits
    files = sum(1 for _path, link in iter_entries(a.root) if not link)
    for h in hits:
        print(h)
    checks = "patterns" + (f" + {len(deny)} deny-list words" if deny else "") + (" + git log" if a.git_log else "")
    print(f"privacy_gate: {files} files scanned ({checks}): {len(hits)} hit(s)")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
