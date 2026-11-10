"""Single entry point: python -m pbp <command> [args]."""
from __future__ import annotations

import importlib
import sys

COMMANDS = {
    "mutate": ("pbp.mutate", "can the tests fail? apply AST mutants to one module and list survivors"),
    "redgreen": ("pbp.redgreen", "do new tests fail before a change and pass after it?"),
    "lock-review-tests": ("pbp.lock_review_tests", "keep only the review tests that are red on the code"),
    "heldout": ("pbp.heldout", "seal, verify and run held-out tests the author never sees"),
    "demo": ("pbp.demo", "run every check on the bundled demo material (needs a clone of the repo)"),
}


def usage_text() -> str:
    width = max(len(c) for c in COMMANDS)
    lines = ["usage: python -m pbp <command> [args]", "", "commands:"]
    lines += [f"  {name:<{width}}  {help_}" for name, (_mod, help_) in COMMANDS.items()]
    lines += ["", "python -m pbp <command> --help shows each command's options.", "",
              "exit codes (all commands): 0 ok; 1 check failed; 2 usage error or unexpected error;",
              "3 mutate: tests red before any mutation; 4 heldout: store does not match its seal;",
              "5 heldout: malformed seal id or refused candidate content; 128+N killed by signal N"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(usage_text())
        return 0 if argv else 2
    if argv[0] in ("-V", "--version"):
        from pbp import __version__
        print(__version__)
        return 0
    name = argv[0]
    entry = COMMANDS.get(name)
    if entry is None:
        print(f"pbp: unknown command: {name}\n", file=sys.stderr)
        print(usage_text(), file=sys.stderr)
        return 2
    try:
        module = importlib.import_module(entry[0])
        rc = module.main(argv[1:])
    except KeyboardInterrupt:
        print(f"pbp {name}: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:   # a crash is never a check result: exit 2, one line, no traceback
        text = str(exc).splitlines()[0] if str(exc) else ""
        print(f"pbp {name}: unexpected error: {type(exc).__name__}: {text}", file=sys.stderr)
        return 2
    return 0 if rc is None else int(rc)


if __name__ == "__main__":
    sys.exit(main())
