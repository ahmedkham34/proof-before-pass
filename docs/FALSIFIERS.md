# Falsifiers: proof that this repo's own tests can fail

Each row is a deliberate, realistic break of one guard. `tools/falsify.py` copies the repo to a throwaway folder,
runs the named test on the unbroken code (it must pass), applies the break (the old text must occur exactly once),
runs the test again (it must fail with pytest exit 1), and never edits the working tree. Each run uses `python -B`,
`PYTHONDONTWRITEBYTECODE=1`, and every `__pycache__` and `.pyc` is removed after the edit.

```
python3 tools/falsify.py                  # all of them
python3 tools/falsify.py lock-every-test  # one, by id
python3 tools/falsify.py --markdown       # also print the table below
```

The table below is the `--markdown` output of one full run.

| id | file | break | test | result |
|---|---|---|---|---|
| mutate-status | pbp/mutate.py | a mutant that makes the tests fail is counted as a survivor | `test_weak_suite_has_survivors_strong_suite_kills_all` | red |
| mutate-cmp-table | pbp/mutate.py | `<` is flipped to `>` instead of its negation `>=` | `test_apply_mutant_makes_exactly_the_named_change` | red |
| mutate-fail-under | pbp/mutate.py | a score below --fail-under still exits 0 | `test_fail_under_gates_the_score` | red |
| mutate-zero-mutants | pbp/mutate.py | a run with no mutants passes --fail-under 0 | `test_zero_mutants_fail_any_fail_under_even_0` | red |
| mutate-fail-under-lenient | pbp/mutate.py | --fail-under is judged on the score that counts timeouts as kills | `test_score_without_timeouts_is_reported_and_gates_fail_under` | red |
| mutate-no-fail-under-note | pbp/mutate.py | the note that the exit code ignores the score is not printed | `test_note_when_no_fail_under_is_given` | red |
| mutate-unparse-baseline | pbp/mutate.py | the baseline runs on the original source, so unparse-only changes count as kills | `test_unparse_only_change_is_a_baseline_failure_not_kills` | red |
| mutate-sampled | pbp/mutate.py | the report hides that only a sample of the mutants ran | `test_text_report_says_when_mutants_were_sampled` | red |
| mutate-timeouts | pbp/mutate.py | timeouts are folded into kills and not counted | `test_timeouts_are_counted_and_shown` | red |
| mutate-symlink | pbp/mutate.py | a tracked symlink is recreated, so mutants are written through it into the original file | `test_a_tracked_symlink_is_copied_by_content_never_written_through` | red |
| lock-nameerror | pbp/lock_review_tests.py | a test that fails only on a NameError is locked as a real red test | `test_a_failure_caused_by_a_missing_name_is_broken_not_red` | red |
| lock-no-red-exit | pbp/lock_review_tests.py | a run with no red test exits 0 | `test_no_red_test_exits_1` | red |
| lock-timeout | pbp/lock_review_tests.py | a hanging test is locked as a proven red | `test_a_timeout_is_its_own_label_and_is_never_locked` | red |
| lock-crlf | pbp/lock_review_tests.py | a review with CRLF line ends finds no test blocks | `test_a_crlf_review_finds_the_same_blocks` | red |
| lock-rc | pbp/lock_review_tests.py | a pytest killed by a signal is read as RED | `test_pytest_return_codes_map_to_labels` | red |
| lock-out-inside-tree | pbp/lock_review_tests.py | OUT may be an absolute path or climb out of TREE with .. | `test_out_outside_tree_is_refused` | red |
| lock-out-symlink | pbp/lock_review_tests.py | OUT may leave TREE through a symlinked folder | `test_out_through_a_symlink_and_a_bad_pytest_root_are_refused` | red |
| lock-no-overwrite | pbp/lock_review_tests.py | an existing OUT file is not refused up front | `test_an_existing_out_file_is_never_overwritten` | red |
| lock-helper-marker | pbp/lock_review_tests.py | any code block with a def is put before every test (a quote of the buggy code shadows the import) | `test_an_unmarked_code_quote_is_not_put_before_tests` | red |
| lock-every-test | pbp/lock_review_tests.py | only the last test in a block is judged | `test_every_test_in_a_block_is_judged_and_only_red_ones_are_locked` | red |
| lock-relative-path | pbp/lock_review_tests.py | the output path is printed absolute even inside the current folder | `test_the_output_path_is_shown_relative_to_the_current_folder` | red |
| heldout-stale-pass | pbp/heldout.py | an earlier pass survives a later refused run | `test_check_refuses_to_run_a_tampered_store_and_leaves_no_pass` | red |
| heldout-summary | pbp/heldout.py | a summary with failures, errors or xpassed reads as a pass | `test_summary_ok_reads_only_a_clean_final_line` | red |
| heldout-junit | pbp/heldout.py | a forged log line plus exit 0 reads as a pass (junit result ignored) | `test_a_candidate_that_prints_a_fake_summary_and_exits_0_is_not_a_pass` | red |
| heldout-seal-required | pbp/heldout.py | check runs without the seal id | `test_check_requires_the_seal_id` | red |
| heldout-every-test | pbp/heldout.py | a skipped sealed test counts as passed | `test_every_sealed_test_must_be_present_and_passed` | red |
| heldout-inject-symlink | pbp/heldout.py | --inject-as follows a symlinked folder in the commit and writes outside the extracted tree | `test_inject_never_follows_a_symlinked_folder_out_of_the_tree` | red |
| heldout-sigterm | pbp/heldout.py | SIGTERM leaves the pytest child running and the temp folders behind | `test_sigterm_kills_the_pytest_child_and_removes_the_temp_folders` | red |
| heldout-unknown-commit-exit | pbp/heldout.py | an unknown commit exits 5 (refused) instead of 2 (usage) | `test_check_refuses_an_unknown_commit` | red |
| heldout-crash-exit | pbp/heldout.py | a crash inside check exits 1, like a failed check, instead of 2 | `test_a_crash_inside_check_is_recorded_and_exits_2` | red |
| heldout-archive-exit | pbp/heldout.py | a TREE that git archive cannot extract is reported as a crash, not as unreadable input | `test_a_tree_that_git_archive_cannot_extract_exits_2` | red |
| heldout-verify-malformed-seal | pbp/heldout.py | verify with a malformed --seal exits 4 (mismatch) instead of 5 (refused) | `test_verify_with_a_malformed_seal_id_exits_5` | red |
| redgreen-test-changed | pbp/redgreen.py | an existing test given an early return is not flagged | `test_any_change_to_an_existing_test_is_flagged[early return-edit0]` | red |
| redgreen-module-level | pbp/redgreen.py | module-level code added to an existing test file is not flagged | `test_module_level_change_in_an_existing_test_file_is_flagged[import mod\nmod.parse_qty = lambda text: abs(int(text))\n]` | red |
| redgreen-import-rebind | pbp/redgreen.py | a new import that rebinds the code under test is not flagged | `test_new_import_that_rebinds_the_code_under_test_is_flagged` | red |
| redgreen-helper-file | pbp/redgreen.py | a changed helper file under a test root is not flagged | `test_helper_conftest_or_plugin_change_is_flagged[tests/helpers.py-changed-True]` | red |
| redgreen-pytest-config | pbp/redgreen.py | a plugin added through the pytest config is not flagged | `test_any_change_to_the_pytest_config_is_flagged[pytest.ini-[pytest]\naddopts = -q\n-[pytest]\naddopts = -q -p tests.plug\n]` | red |
| redgreen-import-time-patch | pbp/redgreen.py | a new test file that patches the code at import time is not flagged | `test_new_test_file_that_patches_at_import_time_is_flagged[import mod\nmod.parse_qty = lambda text: abs(int(text))\n]` | red |
| redgreen-patch-defaults | pbp/redgreen.py | a default argument that patches the code under test at import is not seen | `test_a_new_test_whose_default_argument_patches_the_code_does_not_pass` | red |
| redgreen-new-test-code | pbp/redgreen.py | new tests added to an existing test file are not checked for import-time patches | `test_new_test_method_in_an_existing_class_that_patches_at_import_time_is_flagged` | red |
| redgreen-namespace-write | pbp/redgreen.py | a vars(m).update(...) at import is not flagged | `test_new_test_file_that_patches_at_import_time_is_flagged[import mod\nvars(mod).update(parse_qty=abs)\n]` | red |
| redgreen-exec | pbp/redgreen.py | an exec(...) at import is not flagged | `test_new_test_file_that_patches_at_import_time_is_flagged[exec('import mod; mod.parse_qty = abs')\n]` | red |
| redgreen-pre-symlink-folder | pbp/redgreen.py | a POST test file is written through a symlinked folder at PRE, outside the worktree | `test_a_symlinked_test_folder_at_pre_is_never_written_through` | red |
| redgreen-pre-symlink-file | pbp/redgreen.py | a symlinked test file at PRE is not replaced, so the run crashes | `test_a_symlinked_test_file_at_pre_is_replaced_not_written_through` | red |
| redgreen-sigterm | pbp/redgreen.py | SIGTERM leaves the pytest group running and the worktrees registered | `test_sigterm_kills_the_pytest_group_and_removes_the_worktrees` | red |
| redgreen-tests-symlink | pbp/redgreen.py | a --tests or --out path through a symlink out of TREE is accepted | `test_tests_through_a_symlink_out_of_the_tree_is_refused` | red |
| redgreen-red-types | pbp/redgreen.py | an AttributeError at PRE counts as a red proof | `test_fixed_constant_with_attribute_error_at_pre_is_red_unproven` | red |
| redgreen-green-suite | pbp/redgreen.py | GREEN runs only the changed test files, so a broken unrelated test passes | `test_a_fix_that_breaks_an_unrelated_test_fails_green` | red |
| redgreen-tests-path | pbp/redgreen.py | a --tests path outside TREE is accepted | `test_tests_path_outside_the_tree_is_refused_before_anything_opens` | red |
| redgreen-diff-z | pbp/redgreen.py | git names are read without -z, so a deleted test file with a space in its name is missed | `test_deleted_test_file_with_an_unusual_name_is_flagged[tests/test_with space.py]` | red |
| redgreen-timeout-group | pbp/_common.py | on timeout only pytest is killed, its children keep running | `test_timeout_kills_the_whole_process_group` | red |
| redgreen-crash-exit | pbp/redgreen.py | an unexpected error escapes as a traceback instead of exit 2 | `test_an_unexpected_error_exits_2_with_one_line` | red |
| redgreen-code-file-tests | pbp/redgreen.py | a test-named function in a code file counts as a new test | `test_a_test_named_function_in_a_code_file_is_not_a_new_test` | red |
| redgreen-green-rc | pbp/redgreen.py | pytest exiting non-zero with no failed test passes GREEN | `test_pytest_that_exits_nonzero_without_a_failed_test_fails_green` | red |
| redgreen-patch-name | pbp/redgreen.py | harmless module-level calls in a new test file are flagged as patches | `test_harmless_module_level_code_in_a_new_test_file_is_allowed` | red |
| redgreen-module-docstring | pbp/redgreen.py | a changed module docstring in a test file is flagged | `test_module_docstring_change_in_an_existing_test_file_is_allowed` | red |
| redgreen-config-word | pbp/redgreen.py | an added pytest.toml is reported as changed | `test_config_reason_names_what_happened[pytest.toml-None-added]` | red |
| redgreen-junit-error | pbp/redgreen.py | a failure without a readable type is reported as a collection error | `test_read_junit_takes_the_exception_type_from_the_report` | red |
| redgreen-tree-folder | pbp/redgreen.py | a TREE that is not a folder gives a git error instead of a clear message | `test_an_unknown_commit_or_a_non_repo_is_a_usage_error` | red |
| cli-crash-exit | pbp/__main__.py | an unexpected exception escapes with a traceback and exit 1 | `test_an_unexpected_error_exits_2_with_one_line` | red |
| demo-help | pbp/demo.py | demo --help exits 2 | `test_every_command_exits_0_on_help_and_2_on_missing_arguments` | red |
| privacy-mac-home | tools/privacy_gate.py | a home path in the /Users style is not reported | `test_each_pattern_is_a_hit[mac-home]` | red |
| privacy-win-home | tools/privacy_gate.py | a home path in the drive-letter Users style is not reported | `test_each_pattern_is_a_hit[win-home]` | red |
| privacy-tilde | tools/privacy_gate.py | a tilde path that names a user is not reported | `test_each_pattern_is_a_hit[tilde-user]` | red |
| privacy-secret | tools/privacy_gate.py | secret-shaped key strings are not reported | `test_each_pattern_is_a_hit[key-sk-ant]` | red |
| privacy-secret-shortened | tools/privacy_gate.py | a found secret is printed in full | `test_a_secret_is_reported_shortened_never_in_full` | red |
| privacy-pem | tools/privacy_gate.py | a PEM private-key header is not reported | `test_each_pattern_is_a_hit[pem-rsa]` | red |
| privacy-localhost | tools/privacy_gate.py | an address at localhost is allowed again | `test_each_pattern_is_a_hit[email-localhost]` | red |
| privacy-email | tools/privacy_gate.py | every email address is allowed | `test_each_pattern_is_a_hit` | red |
| privacy-no-reply | tools/privacy_gate.py | an author address that merely ends in noreply is let through | `test_only_addresses_that_cannot_receive_mail_are_let_through` | red |
| privacy-no-reply-file-text | tools/privacy_gate.py | a no-reply address in file text is let through | `test_only_addresses_that_cannot_receive_mail_are_let_through` | red |
| privacy-no-reply-message | tools/privacy_gate.py | a no-reply address inside a commit message is let through | `test_only_addresses_that_cannot_receive_mail_are_let_through` | red |
| privacy-co-author-trailer | tools/privacy_gate.py | a no-reply address after the words co-authored-by in the middle of a message line is let through | `test_only_addresses_that_cannot_receive_mail_are_let_through` | red |
| privacy-cache-folders | tools/privacy_gate.py | text files inside cache folders are skipped | `test_text_files_in_cache_folders_are_scanned_but_bytecode_is_not` | red |
| privacy-symlink | tools/privacy_gate.py | a symlink is skipped silently | `test_a_symlink_is_reported_not_skipped` | red |
| privacy-git-log | tools/privacy_gate.py | --git-log finds nothing in commit e-mails and messages | `test_git_log_scans_author_emails_and_messages` | red |

falsify: 76/76 breaks turned their test red (exit 0)
