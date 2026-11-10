"""pbp: checks whether a passing test suite could fail.

mutate counts the mutants a suite misses, redgreen re-runs new tests on the commit before a change,
lock_review_tests keeps the review tests that fail, and heldout runs sealed tests the author never saw.
"""
__version__ = "0.1.0"
