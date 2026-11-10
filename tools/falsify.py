#!/usr/bin/env python3
"""falsify - prove that this repo's own tests can fail.

What it checks: for each guard, at least one named test goes red when the code is broken in a specific, realistic
way, and is green on the unbroken code. Each break is applied to a throwaway copy of the repo (the working tree is
never edited); the old text must occur exactly once, so a stale break fails loudly instead of silently testing
nothing.

usage: python3 tools/falsify.py [--markdown] [ID ...]
  no ID: run all; exit 0 only if every break turned its test red. --markdown also prints the table used in
  docs/FALSIFIERS.md.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pbp._common import run_cmd  # noqa: E402

# id, file, old text, new text, test node, what the break means
FALSIFIERS = (
    ("mutate-status", "pbp/mutate.py",
     '''else "survived" if rc == 0 else "killed")''',
     '''else "survived" if rc in (0, 1) else "killed")''',
     "tests/test_mutate.py::test_weak_suite_has_survivors_strong_suite_kills_all",
     "a mutant that makes the tests fail is counted as a survivor"),
    ("mutate-cmp-table", "pbp/mutate.py",
     "ast.Lt: ast.GtE, ast.GtE: ast.Lt,",
     "ast.Lt: ast.Gt, ast.GtE: ast.Lt,",
     "tests/test_mutate.py::test_apply_mutant_makes_exactly_the_named_change",
     "`<` is flipped to `>` instead of its negation `>=`"),
    ("mutate-fail-under", "pbp/mutate.py",
     'result["score"] < args.fail_under)',
     'result["score"] < args.fail_under - 100)',
     "tests/test_mutate.py::test_fail_under_gates_the_score",
     "a score below --fail-under still exits 0"),
    ("mutate-zero-mutants", "pbp/mutate.py",
     '(result["score"] is None or result["score"] < args.fail_under)',
     '((result["score"] or 0.0) < args.fail_under)',
     "tests/test_mutate.py::test_zero_mutants_fail_any_fail_under_even_0",
     "a run with no mutants passes --fail-under 0"),
    ("mutate-unparse-baseline", "pbp/mutate.py",
     '            fh.write((ast.unparse(tree) + "\\n").encode("utf-8"))\n',
     '            fh.write(original)\n',
     "tests/test_mutate.py::test_unparse_only_change_is_a_baseline_failure_not_kills",
     "the baseline runs on the original source, so unparse-only changes count as kills"),
    ("mutate-sampled", "pbp/mutate.py",
     '"sampled": total < len(all_muts),',
     '"sampled": False,',
     "tests/test_mutate.py::test_text_report_says_when_mutants_were_sampled",
     "the report hides that only a sample of the mutants ran"),
    ("mutate-timeouts", "pbp/mutate.py",
     'status = ("timeout" if rc is None',
     'status = ("killed" if rc is None',
     "tests/test_mutate.py::test_timeouts_are_counted_and_shown",
     "timeouts are folded into kills and not counted"),
    ("mutate-symlink", "pbp/mutate.py",
     "            shutil.copyfile(src, dst)\n",
     "            shutil.copy2(src, dst, follow_symlinks=False)\n",
     "tests/test_mutate.py::test_a_tracked_symlink_is_copied_by_content_never_written_through",
     "a tracked symlink is recreated, so mutants are written through it into the original file"),
    ("lock-nameerror", "pbp/lock_review_tests.py",
     "(ModuleNotFoundError|ImportError|NameError|SyntaxError|FixtureLookupError)",
     "(ModuleNotFoundError|ImportError|SyntaxError|FixtureLookupError)",
     "tests/test_lock_review_tests.py::test_a_failure_caused_by_a_missing_name_is_broken_not_red",
     "a test that fails only on a NameError is locked as a real red test"),
    ("lock-no-red-exit", "pbp/lock_review_tests.py",
     '        print("no red test: nothing locked")\n        return 1\n',
     '        print("no red test: nothing locked")\n        return 0\n',
     "tests/test_lock_review_tests.py::test_no_red_test_exits_1",
     "a run with no red test exits 0"),
    ("lock-timeout", "pbp/lock_review_tests.py",
     '        return "TIMEOUT", ""',
     '        return "RED", ""',
     "tests/test_lock_review_tests.py::test_a_timeout_is_its_own_label_and_is_never_locked",
     "a hanging test is locked as a proven red"),
    ("lock-crlf", "pbp/lock_review_tests.py",
     '    return text.replace("\\r\\n", "\\n")\n',
     "    return text\n",
     "tests/test_lock_review_tests.py::test_a_crlf_review_finds_the_same_blocks",
     "a review with CRLF line ends finds no test blocks"),
    ("lock-rc", "pbp/lock_review_tests.py",
     "if rc != 1 or",
     "if rc in (2, 3, 4, 5) or",
     "tests/test_lock_review_tests.py::test_pytest_return_codes_map_to_labels",
     "a pytest killed by a signal is read as RED"),
    ("lock-out-inside-tree", "pbp/lock_review_tests.py",
     "    try:\n        norm = safe_rel(rel)\n    except ValueError:\n"
     '        raise UsageError(f"{what} must be a relative path inside TREE: {rel}") from None\n',
     "    norm = rel\n",
     "tests/test_lock_review_tests.py::test_out_outside_tree_is_refused",
     "OUT may be an absolute path or climb out of TREE with .."),
    ("lock-out-symlink", "pbp/lock_review_tests.py",
     "    if not is_within(path, tree):\n",
     "    if False:\n",
     "tests/test_lock_review_tests.py::test_out_through_a_symlink_and_a_bad_pytest_root_are_refused",
     "OUT may leave TREE through a symlinked folder"),
    ("lock-no-overwrite", "pbp/lock_review_tests.py",
     "    if os.path.lexists(out):\n",
     "    if False:\n",
     "tests/test_lock_review_tests.py::test_an_existing_out_file_is_never_overwritten",
     "an existing OUT file is not refused up front"),
    ("lock-helper-marker", "pbp/lock_review_tests.py",
     '        elif code.lstrip("\\n").split("\\n", 1)[0].strip() == HELPER_MARK:\n',
     '        elif re.search(r"^(def|class) \\w+", code, re.M):\n',
     "tests/test_lock_review_tests.py::test_an_unmarked_code_quote_is_not_put_before_tests",
     "any code block with a def is put before every test (a quote of the buggy code shadows the import)"),
    ("lock-every-test", "pbp/lock_review_tests.py",
     "        for name in test_names(code):\n",
     "        for name in test_names(code)[-1:]:\n",
     "tests/test_lock_review_tests.py::test_every_test_in_a_block_is_judged_and_only_red_ones_are_locked",
     "only the last test in a block is judged"),
    ("lock-relative-path", "pbp/lock_review_tests.py",
     "to {shown_path(out)}",
     "to {out}",
     "tests/test_lock_review_tests.py::test_the_output_path_is_shown_relative_to_the_current_folder",
     "the output path is printed absolute even inside the current folder"),
    ("heldout-stale-pass", "pbp/heldout.py",
     '        os.unlink(os.path.join(out, "heldout.rc"))',
     "        pass",
     "tests/test_heldout.py::test_check_refuses_to_run_a_tampered_store_and_leaves_no_pass",
     "an earlier pass survives a later refused run"),
    ("heldout-summary", "pbp/heldout.py",
     "    return not any(k in counts for k in _BAD_KEYS)",
     "    return True",
     "tests/test_heldout.py::test_summary_ok_reads_only_a_clean_final_line",
     "a summary with failures, errors or xpassed reads as a pass"),
    ("heldout-junit", "pbp/heldout.py",
     "        problems = [] if timed_out else junit_problems(junit, expected)\n",
     "        problems = []\n",
     "tests/test_heldout.py::test_a_candidate_that_prints_a_fake_summary_and_exits_0_is_not_a_pass",
     "a forged log line plus exit 0 reads as a pass (junit result ignored)"),
    ("heldout-seal-required", "pbp/heldout.py",
     'c.add_argument("--seal", required=True)',
     'c.add_argument("--seal")',
     "tests/test_heldout.py::test_check_requires_the_seal_id",
     "check runs without the seal id"),
    ("heldout-every-test", "pbp/heldout.py",
     'elif any(st != "passed" for st in states):',
     'elif any(st == "failed" for st in states):',
     "tests/test_heldout.py::test_every_sealed_test_must_be_present_and_passed",
     "a skipped sealed test counts as passed"),
    ("heldout-inject-symlink", "pbp/heldout.py",
     "            if not is_within(os.path.dirname(ldest), tree_dir):\n",
     "            if False:\n",
     "tests/test_heldout.py::test_inject_never_follows_a_symlinked_folder_out_of_the_tree",
     "--inject-as follows a symlinked folder in the commit and writes outside the extracted tree"),
    ("heldout-sigterm", "pbp/heldout.py",
     "    exit_on_signals()\n",
     "",
     "tests/test_heldout.py::test_sigterm_kills_the_pytest_child_and_removes_the_temp_folders",
     "SIGTERM leaves the pytest child running and the temp folders behind"),
    ("heldout-unknown-commit-exit", "pbp/heldout.py",
     '        raise Usage(f"COMMIT does not resolve: {commit}")',
     '        raise Refused(f"COMMIT does not resolve: {commit}")',
     "tests/test_heldout.py::test_check_refuses_an_unknown_commit",
     "an unknown commit exits 5 (refused) instead of 2 (usage)"),
    ("heldout-crash-exit", "pbp/heldout.py",
     '        _err(f"crashed: {exc!r}")\n        return EXIT_USAGE\n',
     '        _err(f"crashed: {exc!r}")\n        return EXIT_FAIL\n',
     "tests/test_heldout.py::test_a_crash_inside_check_is_recorded_and_exits_2",
     "a crash inside check exits 1, like a failed check, instead of 2"),
    ("heldout-archive-exit", "pbp/heldout.py",
     '            raise Usage(f"cannot extract COMMIT from TREE',
     '            raise RuntimeError(f"cannot extract COMMIT from TREE',
     "tests/test_heldout.py::test_a_tree_that_git_archive_cannot_extract_exits_2",
     "a TREE that git archive cannot extract is reported as a crash, not as unreadable input"),
    ("heldout-verify-malformed-seal", "pbp/heldout.py",
     "    if seal is not None and not _HEX64.match(seal):\n",
     "    if False:\n",
     "tests/test_heldout.py::test_verify_with_a_malformed_seal_id_exits_5",
     "verify with a malformed --seal exits 4 (mismatch) instead of 5 (refused)"),
    ('redgreen-test-changed', 'pbp/redgreen.py',
     'elif post_tests[q] != dumps:',
     'elif False:',
     'tests/test_redgreen.py::test_any_change_to_an_existing_test_is_flagged[early return-edit0]',
     'an existing test given an early return is not flagged'),
    ('redgreen-module-level', 'pbp/redgreen.py',
     'if pre_d != post_d:',
     'if False:',
     'tests/test_redgreen.py::test_module_level_change_in_an_existing_test_file_is_flagged[import mod\\nmod.parse_qty = lambda text: abs(int(text))\\n]',
     'module-level code added to an existing test file is not flagged'),
    ('redgreen-import-rebind', 'pbp/redgreen.py',
     '        if clash:',
     '        if False:',
     'tests/test_redgreen.py::test_new_import_that_rebinds_the_code_under_test_is_flagged',
     'a new import that rebinds the code under test is not flagged'),
    ('redgreen-helper-file', 'pbp/redgreen.py',
     'elif base == "conftest.py" or (under_test_root(f) and not is_test_file(f)):',
     'elif base == "conftest.py":',
     'tests/test_redgreen.py::test_helper_conftest_or_plugin_change_is_flagged[tests/helpers.py-changed-True]',
     'a changed helper file under a test root is not flagged'),
    ('redgreen-pytest-config', 'pbp/redgreen.py',
     'if pytest_config(base, show(tree, pre, f)) != pytest_config(base, show(tree, post, f)):',
     'if False:',
     'tests/test_redgreen.py::test_any_change_to_the_pytest_config_is_flagged[pytest.ini-[pytest]\\naddopts = -q\\n-[pytest]\\naddopts = -q -p tests.plug\\n]',
     'a plugin added through the pytest config is not flagged'),
    ('redgreen-import-time-patch', 'pbp/redgreen.py',
     'if writes_elsewhere(e):',
     'if False:',
     'tests/test_redgreen.py::test_new_test_file_that_patches_at_import_time_is_flagged[import mod\\nmod.parse_qty = lambda text: abs(int(text))\\n]',
     'a new test file that patches the code at import time is not flagged'),
    ('redgreen-patch-defaults', 'pbp/redgreen.py',
     'heads = [*node.decorator_list, *a.defaults, *(d for d in a.kw_defaults if d is not None)]',
     'heads = [*node.decorator_list]',
     'tests/test_redgreen.py::test_a_new_test_whose_default_argument_patches_the_code_does_not_pass',
     'a default argument that patches the code under test at import is not seen'),
    ('redgreen-new-test-code', 'pbp/redgreen.py',
     'for node in new_test_nodes(b, pre_tests, pre_names):',
     'for node in []:',
     'tests/test_redgreen.py::test_new_test_method_in_an_existing_class_that_patches_at_import_time_is_flagged',
     'new tests added to an existing test file are not checked for import-time patches'),
    ('redgreen-namespace-write', 'pbp/redgreen.py',
     'elif isinstance(node.func, ast.Attribute) and is_namespace(node.func.value):',
     'elif False:',
     'tests/test_redgreen.py::test_new_test_file_that_patches_at_import_time_is_flagged[import mod\\nvars(mod).update(parse_qty=abs)\\n]',
     'a vars(m).update(...) at import is not flagged'),
    ('redgreen-exec', 'pbp/redgreen.py',
     'PATCH_CALLS = {"setattr", "delattr", "start", "exec", "eval"}',
     'PATCH_CALLS = {"setattr", "delattr", "start", "eval"}',
     "tests/test_redgreen.py::test_new_test_file_that_patches_at_import_time_is_flagged[exec('import mod; mod.parse_qty = abs')\\n]",
     'an exec(...) at import is not flagged'),
    ('redgreen-pre-symlink-folder', 'pbp/redgreen.py',
     '    if not is_within(probe, wt):\n',
     '    if False:\n',
     'tests/test_redgreen.py::test_a_symlinked_test_folder_at_pre_is_never_written_through',
     'a POST test file is written through a symlinked folder at PRE, outside the worktree'),
    ('redgreen-pre-symlink-file', 'pbp/redgreen.py',
     '        os.unlink(dest)\n',
     '        pass\n',
     'tests/test_redgreen.py::test_a_symlinked_test_file_at_pre_is_replaced_not_written_through',
     'a symlinked test file at PRE is not replaced, so the run crashes'),
    ('redgreen-sigterm', 'pbp/redgreen.py',
     '    exit_on_signals()\n',
     '',
     'tests/test_redgreen.py::test_sigterm_kills_the_pytest_group_and_removes_the_worktrees',
     'SIGTERM leaves the pytest group running and the worktrees registered'),
    ('redgreen-tests-symlink', 'pbp/redgreen.py',
     'if norm is None or norm == "." or not is_within(os.path.join(tree, norm), tree):',
     'if norm is None or norm == ".":',
     'tests/test_redgreen.py::test_tests_through_a_symlink_out_of_the_tree_is_refused',
     'a --tests or --out path through a symlink out of TREE is accepted'),
    ('redgreen-red-types', 'pbp/redgreen.py',
     'RED_TYPES = {"AssertionError", "Failed"}',
     'RED_TYPES = {"AssertionError", "Failed", "AttributeError"}',
     'tests/test_redgreen.py::test_fixed_constant_with_attribute_error_at_pre_is_red_unproven',
     'an AttributeError at PRE counts as a red proof'),
    ('redgreen-green-suite', 'pbp/redgreen.py',
     'green_files = files if args.green_only_changed else []',
     'green_files = files',
     'tests/test_redgreen.py::test_a_fix_that_breaks_an_unrelated_test_fails_green',
     'GREEN runs only the changed test files, so a broken unrelated test passes'),
    ('redgreen-tests-path', 'pbp/redgreen.py',
     'explicit = [safe_path(tree, f, "--tests") for spec in args.tests for f in spec.split()]',
     'explicit = [f for spec in args.tests for f in spec.split()]',
     'tests/test_redgreen.py::test_tests_path_outside_the_tree_is_refused_before_anything_opens',
     'a --tests path outside TREE is accepted'),
    ('redgreen-diff-z', 'pbp/redgreen.py',
     '"--no-renames", "-z", pre, post',
     '"--no-renames", pre, post',
     'tests/test_redgreen.py::test_deleted_test_file_with_an_unusual_name_is_flagged[tests/test_with space.py]',
     'git names are read without -z, so a deleted test file with a space in its name is missed'),
    ('redgreen-timeout-group', 'pbp/_common.py',
     'os.killpg(proc.pid, signal.SIGKILL)',
     'proc.kill()',
     'tests/test_redgreen.py::test_timeout_kills_the_whole_process_group',
     'on timeout only pytest is killed, its children keep running'),
    ('redgreen-crash-exit', 'pbp/redgreen.py',
     'except Exception as exc:',
     'except UsageError as exc:',
     'tests/test_redgreen.py::test_an_unexpected_error_exits_2_with_one_line',
     'an unexpected error escapes as a traceback instead of exit 2'),
    ('redgreen-code-file-tests', 'pbp/redgreen.py',
     'if s != "D" and is_test_file(f)]',
     'if s != "D" or is_test_file(f)]',
     'tests/test_redgreen.py::test_a_test_named_function_in_a_code_file_is_not_a_new_test',
     'a test-named function in a code file counts as a new test'),
    ('redgreen-green-rc', 'pbp/redgreen.py',
     'elif grc not in (0, None):',
     'elif False:',
     'tests/test_redgreen.py::test_pytest_that_exits_nonzero_without_a_failed_test_fails_green',
     'pytest exiting non-zero with no failed test passes GREEN'),
    ('redgreen-patch-name', 'pbp/redgreen.py',
     'or "patch" in name.lower():',
     'or "patch" not in name.lower():',
     'tests/test_redgreen.py::test_harmless_module_level_code_in_a_new_test_file_is_allowed',
     'harmless module-level calls in a new test file are flagged as patches'),
    ('redgreen-module-docstring', 'pbp/redgreen.py',
     'if i == 0 and is_docstring(n):',
     'if False:',
     'tests/test_redgreen.py::test_module_docstring_change_in_an_existing_test_file_is_allowed',
     'a changed module docstring in a test file is flagged'),
    ('redgreen-config-word', 'pbp/redgreen.py',
     "{word if base in WHOLE_FILE else 'changed'}",
     "{word if base not in WHOLE_FILE else 'changed'}",
     'tests/test_redgreen.py::test_config_reason_names_what_happened[pytest.toml-None-added]',
     'an added pytest.toml is reported as changed'),
    ('redgreen-junit-error', 'pbp/redgreen.py',
     'if c.tag == "error":',
     'if True:',
     'tests/test_redgreen.py::test_read_junit_takes_the_exception_type_from_the_report',
     'a failure without a readable type is reported as a collection error'),
    ('redgreen-tree-folder', 'pbp/redgreen.py',
     'if not os.path.isdir(tree):',
     'if False:',
     'tests/test_redgreen.py::test_an_unknown_commit_or_a_non_repo_is_a_usage_error',
     'a TREE that is not a folder gives a git error instead of a clear message'),
    ("cli-crash-exit", "pbp/__main__.py",
     "        return 2\n    return 0 if rc is None else int(rc)\n",
     "        raise\n    return 0 if rc is None else int(rc)\n",
     "tests/test_cli.py::test_an_unexpected_error_exits_2_with_one_line",
     "an unexpected exception escapes with a traceback and exit 1"),
    ("demo-help", "pbp/demo.py",
     "        print(__doc__.split(\"\\n\\n\", 2)[1])\n        return 0\n",
     "        print(__doc__.split(\"\\n\\n\", 2)[1])\n        return 2\n",
     "tests/test_cli.py::test_every_command_exits_0_on_help_and_2_on_missing_arguments",
     "demo --help exits 2"),
    ("privacy-mac-home", "tools/privacy_gate.py",
     're.escape(_MAC_HOME) + r"[A-Za-z0-9._-]+"',
     'r"(?!x)x"',
     "tests/test_privacy_gate.py::test_each_pattern_is_a_hit[mac-home]",
     "a home path in the /Users style is not reported"),
    ("privacy-win-home", "tools/privacy_gate.py",
     're.compile(r"(?<![A-Za-z])[A-Za-z]:',
     're.compile(r"(?!x)x(?<![A-Za-z])[A-Za-z]:',
     "tests/test_privacy_gate.py::test_each_pattern_is_a_hit[win-home]",
     "a home path in the drive-letter Users style is not reported"),
    ("privacy-tilde", "tools/privacy_gate.py",
     r're.compile(r"(?<![\w~])~',
     r're.compile(r"(?!x)x(?<![\w~])~',
     "tests/test_privacy_gate.py::test_each_pattern_is_a_hit[tilde-user]",
     "a tilde path that names a user is not reported"),
    ("privacy-secret", "tools/privacy_gate.py",
     '("secret", re.compile(_SECRET)),',
     '("secret", re.compile(r"(?!x)x")),',
     "tests/test_privacy_gate.py::test_each_pattern_is_a_hit[key-sk-ant]",
     "secret-shaped key strings are not reported"),
    ("privacy-secret-shortened", "tools/privacy_gate.py",
     "                    if kind in SECRET_KINDS:",
     "                    if False:",
     "tests/test_privacy_gate.py::test_a_secret_is_reported_shortened_never_in_full",
     "a found secret is printed in full"),
    ("privacy-pem", "tools/privacy_gate.py",
     're.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")',
     're.compile(r"(?!x)x")',
     "tests/test_privacy_gate.py::test_each_pattern_is_a_hit[pem-rsa]",
     "a PEM private-key header is not reported"),
    ("privacy-localhost", "tools/privacy_gate.py",
     'ALLOWED_DOMAINS = ("example.com",',
     'ALLOWED_DOMAINS = ("localhost", "example.com",',
     "tests/test_privacy_gate.py::test_each_pattern_is_a_hit[email-localhost]",
     "an address at localhost is allowed again"),
    ("privacy-email", "tools/privacy_gate.py",
     "        return no_reply_ok and is_no_reply(match)",
     "        return True",
     "tests/test_privacy_gate.py::test_each_pattern_is_a_hit",
     "every email address is allowed"),
    ("privacy-no-reply", "tools/privacy_gate.py",
     "    return local in NO_REPLY_LOCALS or domain.startswith(NO_REPLY_DOMAIN_PREFIX)",
     "    return local.endswith(NO_REPLY_LOCALS) or domain.startswith(NO_REPLY_DOMAIN_PREFIX)",
     "tests/test_privacy_gate.py::test_only_addresses_that_cannot_receive_mail_are_let_through",
     "an author address that merely ends in noreply is let through"),
    ("privacy-no-reply-file-text", "tools/privacy_gate.py",
     "        return no_reply_ok and is_no_reply(match)",
     "        return is_no_reply(match)",
     "tests/test_privacy_gate.py::test_only_addresses_that_cannot_receive_mail_are_let_through",
     "a no-reply address in file text is let through"),
    ("privacy-no-reply-message", "tools/privacy_gate.py",
     "no_reply_ok=line.lower().startswith(CO_AUTHOR_TRAILER))",
     "no_reply_ok=True)",
     "tests/test_privacy_gate.py::test_only_addresses_that_cannot_receive_mail_are_let_through",
     "a no-reply address inside a commit message is let through"),
    ("privacy-co-author-trailer", "tools/privacy_gate.py",
     "line.lower().startswith(CO_AUTHOR_TRAILER)",
     "CO_AUTHOR_TRAILER in line.lower()",
     "tests/test_privacy_gate.py::test_only_addresses_that_cannot_receive_mail_are_let_through",
     "a no-reply address after the words co-authored-by in the middle of a message line is let through"),
    ("privacy-cache-folders", "tools/privacy_gate.py",
     'SKIP_DIRS = {".git"}',
     'SKIP_DIRS = {".git", "__pycache__", ".pytest_cache"}',
     "tests/test_privacy_gate.py::test_text_files_in_cache_folders_are_scanned_but_bytecode_is_not",
     "text files inside cache folders are skipped"),
    ("privacy-symlink", "tools/privacy_gate.py",
     '            hits.append(f"{rel}: symlink (not followed) -> {target}")\n',
     "",
     "tests/test_privacy_gate.py::test_a_symlink_is_reported_not_skipped",
     "a symlink is skipped silently"),
    ("privacy-git-log", "tools/privacy_gate.py",
     "    return log_hits\n",
     "    return []\n",
     "tests/test_privacy_gate.py::test_git_log_scans_author_emails_and_messages",
     "--git-log finds nothing in commit e-mails and messages"),
)


def _drop_bytecode(copy: Path) -> None:
    for cache in list(copy.rglob("__pycache__")):
        shutil.rmtree(cache, ignore_errors=True)
    for pyc in list(copy.rglob("*.pyc")):
        pyc.unlink(missing_ok=True)


def run_test(copy: Path, node: str) -> tuple[int, str]:
    rc, out = run_cmd([sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", node], cwd=copy,
                      timeout=600, log_path=str(copy) + ".log")
    return (-1 if rc is None else rc), out.decode("utf-8", "replace")


def red_line(output: str) -> str:
    lines = [ln for ln in output.splitlines() if ln.startswith("FAILED ")]
    summary = [ln for ln in output.splitlines() if " failed" in ln and " in " in ln]
    return (lines[0] if lines else "(no FAILED line)") + " | " + (summary[-1].strip("= ") if summary else "")


def prove(fid: str, rel: str, old: str, new: str, node: str, tmp: Path) -> tuple[bool, str]:
    copy = tmp / fid
    shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "*.pyc"))
    rc, out = run_test(copy, node)
    if rc != 0:
        return False, f"test is not green on the unbroken code (exit {rc})"
    target = copy / rel
    text = target.read_text()
    count = text.count(old)
    if count != 1:
        return False, f"break text occurs {count} times in {rel} (must be exactly 1)"
    target.write_text(text.replace(old, new))
    _drop_bytecode(copy)   # a stale .pyc with the same size and mtime second would hide the break
    rc, out = run_test(copy, node)
    if rc == 0:
        return False, "test stayed GREEN on the broken code: it cannot catch this break"
    if rc != 1:
        return False, f"pytest exit {rc} on the broken code (an error or a timeout, not a failing assertion)"
    return True, red_line(out)


def main(argv: list[str]) -> int:
    markdown = "--markdown" in argv
    argv = [a for a in argv if a != "--markdown"]
    unknown = set(argv) - {f[0] for f in FALSIFIERS}
    if unknown:
        print(f"falsify: unknown id(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2
    chosen = [f for f in FALSIFIERS if not argv or f[0] in argv]
    results = []
    with tempfile.TemporaryDirectory(prefix="pbp-falsify-") as tmp:
        for fid, rel, old, new, node, meaning in chosen:
            ok, detail = prove(fid, rel, old, new, node, Path(tmp))
            results.append((fid, rel, meaning, node, ok, detail))
            print(f"{'RED ' if ok else 'MISS'} {fid}: {rel} ({meaning})")
            print(f"     green before, then: {detail}" if ok else f"     {detail}", flush=True)
    if markdown:
        print("\n| id | file | break | test | result |\n|---|---|---|---|---|")
        for fid, rel, meaning, node, ok, _detail in results:
            print(f"| {fid} | {rel} | {meaning.replace('|', '/')} | `{node.split('::', 1)[1]}` | "
                  f"{'red' if ok else 'MISS'} |")
    good = sum(1 for r in results if r[4])
    print(f"falsify: {good}/{len(results)} breaks turned their test red")
    return 0 if good == len(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
