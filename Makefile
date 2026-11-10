PYTHON ?= python3

.PHONY: test demo privacy falsify selftest all

test:
	$(PYTHON) -m pytest -q

demo:
	$(PYTHON) -m pbp demo

privacy:
	$(PYTHON) tools/privacy_gate.py . --git-log

falsify:
	$(PYTHON) tools/falsify.py

# The Evidence table in the README: mutate run on four of its own modules (needs a git clone; slow).
selftest:
	$(PYTHON) -m pbp mutate --repo . --module pbp/mutate.py --tests tests/test_mutate.py tests/test_cli.py --max 200 --timeout 60
	$(PYTHON) -m pbp mutate --repo . --module pbp/heldout.py --tests tests/test_heldout.py tests/test_cli.py --max 200 --timeout 60
	$(PYTHON) -m pbp mutate --repo . --module pbp/lock_review_tests.py --tests tests/test_lock_review_tests.py tests/test_cli.py --max 200 --timeout 60
	$(PYTHON) -m pbp mutate --repo . --module pbp/redgreen.py --tests tests/test_redgreen.py tests/test_cli.py --max 120 --timeout 180

all: test demo privacy falsify
