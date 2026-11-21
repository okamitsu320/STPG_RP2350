# Testing the GUI

This page says how to test `stpg-view` and how to read the results. The tests are host only: the STPG, the GPIO
controller and the analyzer are models, and all settings are temporary folders. In the test process a guard blocks
real serial opens, USB enumeration and starting a debugger or flashing tool, and the programs the tests start run with
device runs switched off (`--no-device-run`). The guard replaces Python calls in the test process; it is not an OS
sandbox ([Host-only testing](development/testing.md) says what it does not cover). Do not run suites in parallel,
because they share simulated device identities.

## Levels

Each level proves something different. A lower level does not replace a higher one.

| Level | What it proves | Tool |
| --- | --- | --- |
| 1. Service | Rules, refusals and records, without a page | pytest |
| 2. Rendered text | What a result view says, from the real page script in Node. This is **not** a browser test | pytest + Node |
| 3. Browser | Clicks and page states in a real browser | pytest + selenium + Firefox + geckodriver |
| 4. Installed | The wheel in a clean environment, away from the source | pytest + built wheels |
| 5. Mutation | That the tests fail when the behaviour is broken | `gui_mutation_check.py` |

A skipped test is not a pass. Levels 3 and 4 skip when their tools or wheels are missing, so report them as NOT RUN.
Results on one system say nothing about another: running on Linux is not a Windows or macOS result.

## Set up

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev,browser]'   # browser: selenium for level 3; Firefox and geckodriver must be installed too
```

`STPG_BROWSER_FIREFOX` and `STPG_BROWSER_GECKODRIVER` select the browser binaries, and `STPG_BROWSER_SHOTS` a folder
for screenshots. With a snap Firefox, set `TMPDIR` to a folder that Firefox may read, such as `~/snap/firefox/common/tmp`.

## Run

Levels 1 and 2:

```sh
python -m pytest -q tools/tests/test_stpgview_settings_folder.py tools/tests/test_stpgview_core_boundary.py \
  tools/tests/test_stpgview_external_result_render.py tools/tests/test_manual_pages.py \
  tools/tests/test_stpgview_runner.py tools/tests/test_stpgview_runner_accessories.py \
  tools/tests/test_stpgview_runner_acceptance.py tools/tests/test_stpgview_runner_edit.py \
  tools/tests/test_stpgview_runner_timing.py tools/tests/test_stpgview_runner_inputs.py \
  tools/tests/test_stpgview_controller_identify.py tools/tests/test_stpgview_physical_helpers.py \
  tools/tests/test_stpgview_results_r8.py tools/tests/test_stpgview_r9.py \
  tools/tests/test_stpgview_r9_record_limits.py tools/tests/test_stpgview_long_wait.py
```

Level 3, alone, with nothing else running:

```sh
python -m pytest -q tools/tests/test_stpgview_browser.py tools/tests/test_stpgview_viewer_browser.py \
  tools/tests/test_getting_started_browser.py tools/tests/test_stpgview_browser_r8.py \
  tools/tests/test_stpgview_browser_r9.py tools/tests/test_stpgview_browser_long_wait.py
```

Level 4. Build the wheel, then point the test at it. `STPG_WHEELHOUSE` is a folder with the dependencies, for a run
without network access.

```sh
python -m build --wheel --outdir dist .
STPG_CORE_WHEEL=dist/stpg_rp2350-0.1.0.dev7-py3-none-any.whl python -m pytest -q tools/tests/test_gui_installed_core.py
```

Level 5:

```sh
python tools/tests/gui_mutation_check.py             # no browser
python tools/tests/gui_mutation_check.py --browser   # real browser
python tools/tests/gui_mutation_check.py --list      # what is broken, and which test must catch it
```

Each mutant breaks one line in a temporary copy, never in the source tree. The named test must pass on the unchanged
code and fail on the mutant. `STALE` means the source changed and the mutant must be updated; `BASELINE FAILED` means
the test does not pass even unchanged. Neither counts as caught.

## What is covered, and where

`…::` means the same file as the name before it in that row.

| ID | Requirement | Test (`tools/tests/`) | Level |
| --- | --- | --- | --- |
| S1 | Order: `--config-dir`, then a non-blank `STPG_CONFIG_DIR`, then the default | `test_stpgview_settings_folder.py::test_the_option_wins_over_the_variable_and_the_variable_over_the_default` | 1 |
| S2 | Blank variable, blank option | `test_stpgview_settings_folder.py::test_a_blank_variable_counts_as_not_set_and_a_non_empty_one_is_reported`, `…::test_a_blank_option_is_an_error_and_never_falls_back`, `…::test_the_command_stops_on_a_blank_option_before_anything_is_served` | 1 |
| S3 | `~`, unknown `~user`, spaces, relative paths, quotes, symbolic links | `test_stpgview_settings_folder.py::test_the_home_folder_is_expanded_the_same_from_option_and_variable`, `…::test_an_unknown_user_home_is_an_error_not_a_folder_named_tilde`, `…::test_spaces_and_other_characters_are_kept_as_typed`, `…::test_a_relative_value_is_taken_from_the_start_folder_once`, `…::test_quoted_and_control_character_values_are_refused`, `…::test_a_symbolic_link_is_followed_at_start_so_a_later_change_cannot_move_the_settings` | 1 |
| S4 | Default folder of each system | `test_stpgview_settings_folder.py::test_the_default_folder_of_each_platform`, `…::test_a_relative_xdg_value_is_ignored_and_reported` (a model of the rule; not a native run) | 1 |
| S5 | An unusable folder is reported and nothing else is used | `test_stpgview_settings_folder.py::test_a_file_in_place_of_the_folder_is_a_problem_and_no_other_folder_is_used`, `…::test_an_unreadable_folder_is_reported_not_shown_as_empty`, `test_stpgview_browser.py::test_a_settings_folder_problem_is_shown_and_no_other_folder_is_used` | 1, 3 |
| S6 | Existing files stay exactly as they are; old or broken files are explained | `test_stpgview_settings_folder.py::test_starting_and_listing_leave_existing_files_exactly_as_they_are`, `…::test_a_name_that_exists_is_not_overwritten_without_an_explicit_replace` | 1 |
| S7 | The folder, its source and state are shown at start and on the page | `test_stpgview_settings_folder.py::test_start_line_names_folder_source_and_state`, `…::test_the_runner_uses_and_reports_the_resolved_folder`, `…::test_the_real_command_prints_the_folder_and_creates_nothing`, `test_stpgview_browser.py::test_a_new_user_sees_the_settings_folder_and_how_to_use_an_earlier_one`, `…::test_existing_settings_are_counted_and_the_earlier_setup_hint_goes_away` | 1, 3 |
| G1 | The analyzer tool's state is told from its package metadata, without importing or starting it; guidance | `test_stpgview_core_boundary.py::test_an_installed_analyzer_tool_is_reported_from_its_metadata_without_importing_or_starting_it`, `test_stpgview_browser.py::test_the_logic_analyzer_guidance_says_what_is_here_and_what_to_do`, `…::test_the_analyzer_list_points_to_the_tool_that_manages_it`, `…::test_analyzer_bindings_are_listed_read_only_by_usb_identity` | 1, 3 |
| G2 | Not selected, not checked, failed, checked and used are told apart; Check starts nothing | `test_stpgview_browser.py::test_each_accessory_state_is_told_apart_and_nothing_starts`, `test_stpgview_runner_accessories.py::test_external_inspect_checks_files_only` | 1, 3 |
| G3 | The phase limits are shown, and a selection changed after confirmation is refused | `test_stpgview_runner_accessories.py::test_confirmation_freezes_the_external_accessory_selection`, `…::test_confirmation_displays_and_freezes_phase_limits`, `test_stpgview_browser.py::test_an_accessory_config_edited_after_confirmation_is_rejected_in_the_dialog` | 1, 3 |
| G4 | Accessory results: separate axis, INCOMPLETE, cancel and cleanup told apart, unadmitted bytes | `test_stpgview_external_result_render.py`, `test_stpgview_runner_accessories.py::test_the_view_names_unadmitted_bytes_of_a_cancelled_exchange`, `…::test_an_accessory_that_is_not_ready_stops_before_the_stpg_run`, `test_stpgview_browser.py::test_accessory_failures_render_as_separate_axes_without_relabelling_the_stpg_result`, `…::test_an_accessory_that_is_not_ready_says_the_stpg_was_not_started` | 1, 2, 3 |
| G5 | Unselected accessories are not contacted | `test_stpgview_runner_accessories.py::test_stpg_only_run_never_contacts_an_accessory`, `…::test_four_accessory_combinations_match_the_cli` | 1 |
| G6 | GPIO role, HIGH and lease refusals | `test_stpgview_runner_accessories.py::test_run_attached_pin_phase_and_high_restrictions`, `…::test_manual_set_restrictions`, `…::test_run_line_high_needs_allow_list_and_explicit_override`, `…::test_manual_reset_rejects_while_another_process_owns_a_device`, `…::test_controller_aliasing_stpg_or_analyzer_rejects` | 1 |
| G7 | Role pins are named | `test_stpgview_runner.py::test_role_pins_of_a_bundle_are_named_in_the_confirmation` | 1 |
| G8 | The GPIO sketch's boot state, pin order and refusals, compiled natively with stubs (not Arduino, not hardware) | `test_gpio_controller_sketch.py` | 1 |
| G9 | A cancel at each receive boundary leaves the received reply unadmitted | `test_external_accessory_round3.py::test_received_reply_is_unadmitted_at_every_cancel_boundary` | 1 |
| G10 | `stpg-examples` writes only into a new or empty folder; when it refuses, it writes nothing | `test_stpgview_physical_helpers.py::test_the_sample_command_refuses_a_folder_that_is_not_empty`, `…::test_the_sample_command_refuses_a_file_and_accepts_an_empty_folder`, `test_gui_installed_core.py::test_the_example_command_writes_only_the_public_offline_inputs` | 1, 4 |
| G11 | The getting-started page works as written: the installed commands and its labels, in a browser | `test_getting_started_browser.py` | 3 |
| G12 | A run the STPG refuses at its start check says what the STPG reported and what a run needs, and GPIO actions it never reached are "not started", apart from a real controller failure; a finished job is not shown as a successful run | `test_stpgview_runner_accessories.py::test_a_run_refused_at_the_start_check_says_so_and_no_gpio_action_was_started`, `…::test_a_controller_that_fails_after_the_start_check_is_not_called_not_started`, `…::test_the_start_check_is_read_only_from_what_was_recorded`, `test_stpgview_browser.py::test_a_run_the_stpg_refuses_at_its_start_check_says_why_and_that_gpio_was_not_started` | 1, 3 |
| G13 | A firmware version or capability that differs from the binding is shown side by side (expected and reported), before any upload, and never copied into the binding; the form explains both values | `test_stpgview_runner_accessories.py::test_an_identity_difference_shows_both_values_and_the_binding_is_never_changed`, `…::test_an_identity_failure_with_the_same_values_is_not_called_a_different_firmware`, `test_stpgview_browser.py::test_a_different_firmware_shows_both_identities_and_the_form_explains_the_values` | 1, 3 |
| G14 | The file and folder pickers offer every Windows drive that exists (a model on this host, not a native run) or "/", open a root with one press, and refuse a new folder name that exists in other upper or lower case | `test_stpgview.py::test_browse_roots_are_every_windows_drive_or_the_root_folder`, `test_stpgview_viewer_browser.py::test_the_open_dialog_shows_the_roots_and_opens_one`, `test_stpgview_browser.py::test_new_project_defaults_new_folder_picker_and_wait_suggestions` | 1, 3 |
| G15 | The result file distinguishes accepted upload and RUN from a missing or partial first STATUS reply, even after an earlier segment finished. The upload is not marked failed, and completion is not inferred. Later polls, writes, invalid replies and release failures are not given this diagnosis | `test_stpgview_runner_accessories.py::test_a_run_whose_first_status_gets_no_reply_says_what_was_answered`, `…::test_the_stop_point_names_only_answered_steps`, `…::test_the_first_status_notice_is_not_used_for_other_boundaries`, `test_stpgview_browser.py::test_a_run_without_a_status_reply_shows_the_answered_steps_not_a_failed_upload`, `…::test_first_status_failure_rows_do_not_depend_on_the_upload_being_the_last_progress_stage` | 1, 3 |
| G16 | The SDK's acceptance notifications are told apart: bytes sent, UPLOAD_END accepted, RUN accepted and waiting for the first STATUS (no progress claimed), a STATUS count (no RUNNING or RUN_DRAINING inferred); RUN is listed once, and a run loaded without those events keeps the result-file rows; the STATUS budget is shown and the 5-second note is information | `test_stpgview_browser.py::test_the_live_view_tells_bytes_sent_upload_accepted_run_accepted_and_status_apart`, `…::test_the_stage_list_uses_the_acceptance_events_and_keeps_the_result_file_rows`, `…::test_a_slow_first_status_is_shown_as_waiting_after_an_accepted_run` | 3 |
| G17 | A later segment does not inherit an earlier upload's acceptance, RUN rows stay with their own upload, and an earlier segment's events do not suppress a later journal's acceptance facts | `test_stpgview_stage_segments.py::test_later_upload_never_inherits_previous_segment_acceptance`, `…::test_later_accepted_run_remains_after_its_own_upload_in_the_stage_list`, `…::test_later_journal_fallback_is_not_suppressed_by_an_earlier_acceptance`, `…::test_legacy_earlier_upload_is_not_annotated_as_the_later_segment` | 2 |
| G18 | Before a run with compares, the confirmation and its own box say that a mismatching segment's retained fail log is read before the next segment or release, its limits, and that HOLD levels stay driven meanwhile; a plan without compares does not ask it. The live view calls the read a log transfer, not RUN progress | `test_stpgview_results_r8.py::test_the_fail_log_box_is_asked_for_a_plan_with_compares_and_required`, `…::test_a_plan_without_compares_does_not_ask_the_fail_log_box`, `test_stpgview_browser_r8.py::test_the_confirmation_names_the_fail_log_read_and_what_it_holds`, `…::test_the_live_view_tells_the_fail_log_read_from_run_progress` | 1, 3 |
| G19 | A stopped fail-log read names the request and keeps MISMATCH and the entries read; only its row fails, and its journal records are named, not called a missing STATUS | `test_stpgview_results_r8.py::test_a_stopped_fail_log_read_is_named_and_the_mismatch_stands`, `…::test_fail_log_stop_only_for_a_read_error_and_names_a_rejected_answer`, `…::test_exchanges_name_fail_log_records_that_write_nothing`, `test_stpgview_browser_r8.py::test_a_stopped_fail_log_read_is_explained_and_only_that_row_fails` | 1, 3 |
| G20 | STIL details come from the SDK with the bundle of the exact manifest; the text names no folder, port or USB identity and is written only to a new .txt; the viewer marks a fail only on the vector of the named line and lists the rest; a gzip STIL opens | `test_stpgview_results_r8.py::test_a_mismatch_is_described_against_the_stil_and_saved_as_text`, `…::test_the_viewer_marks_only_the_vector_on_the_named_line`, `…::test_a_gzip_stil_opens_in_the_viewer_with_the_same_lines`, `…::test_describe_text_lists_the_rebind_and_limits_the_entries`, `test_stpgview_browser_r8.py::test_a_mismatch_shows_its_fail_log_stil_details_and_marks_in_the_viewer` | 1, 3 |
| G21 | A bundle of another SDK is updated only on request, into a new bundle beside the unchanged old one; the page says it was not recompiled and the project log records the compile SDK | `test_stpgview_results_r8.py::test_a_bundle_of_another_sdk_is_updated_only_on_request_and_logged`, `test_stpgview_browser_r8.py::test_a_bundle_of_another_sdk_is_updated_on_request_and_says_so` | 1, 3 |
| G22 | A Custom chunk-wait length is read exactly in ms (1 µs steps; a finer, zero or non-plain value is refused, nothing is rounded); New, Edit and Copy keep it and another preset removes it; the project equals the CLI's and builds the same single and segmented patterns, and a length equal to 4 or 12 ms builds the preset's pattern | `test_stpgview_r9.py::test_a_custom_length_in_ms_becomes_exact_microseconds`, `…::test_a_custom_length_that_needs_rounding_or_is_not_a_plain_number_is_refused`, `…::test_a_length_is_never_kept_or_dropped_silently_with_another_preset`, `…::test_a_custom_project_is_the_cli_project_and_builds_the_same_single_and_segmented_patterns`, `…::test_a_custom_length_equal_to_a_preset_builds_the_same_pattern`, `…::test_edit_and_reopen_keep_the_exact_length_and_another_preset_removes_it`, `…::test_an_older_preset_project_is_not_given_the_new_key`, `test_stpgview_browser_r9.py::test_custom_length_is_typed_exactly_kept_by_edit_and_copy_and_dropped_by_another_preset` | 1, 3 |
| G23 | A refused wait plan is shown and nothing is adjusted; a bundle whose saved plan the SDK refuses is REJECT and offers no update | `test_stpgview_r9.py::test_a_plan_over_the_total_limit_is_refused_and_nothing_is_adjusted`, `…::test_a_bundle_whose_saved_plan_is_rejected_is_refused_and_not_updated`, `test_stpgview_browser_r9.py::test_a_refused_wait_plan_says_nothing_was_adjusted`, `…::test_a_bundle_with_a_rejected_saved_plan_offers_no_update` | 1, 3 |
| G24 | A refused controller identification lists the received lines as text, never as markup, with the SDK's reading of each; an older record says it has none; a start-check refusal lists none | `test_stpgview_r9.py::test_a_refused_identification_lists_the_lines_and_the_stpg_kept_the_upload`, `…::test_a_matched_identification_is_listed_too_and_a_completed_run_left_nothing_loaded`, `…::test_an_old_record_has_no_lines_and_long_or_many_lines_are_bounded`, `…::test_a_start_check_refusal_never_lists_controller_lines`, `test_stpgview_browser_r9.py::test_controller_lines_are_text_and_the_stpg_kept_the_upload`, `…::test_a_start_check_refusal_lists_no_controller_lines_and_names_the_earlier_stop` | 1, 3 |
| G25 | A finished rejected/cancelled record ending in an accepted upload names that reply and the lack of a later recorded command; PENDING prefixes do not prove later commands were not sent, and no record establishes current device/pin state | `test_stpgview_r9.py::test_an_accepted_upload_with_nothing_after_it_is_recognised_only_as_the_last_record`, `test_stpgview_r9_record_limits.py`, `test_stpgview_browser_r9.py::test_controller_lines_are_text_and_the_stpg_kept_the_upload` | 1, 3 |
| G26 | A long WFT period builds into narrow and wide segments without a wait-plan notice; the viewer and the analyzer timeline decode both, and fails in the wide segment map to its STIL lines. A streamed gap of 250 ms or more on an STPG without `CAP_STREAM_WAIT_WATCHDOG` is refused after INFO, CLOCK and STATUS: no RESET, upload, RUN or GPIO request, no start-check, kept-upload or link notice; with the capability, or at ordinary rates, or unstreamed, it runs; an updated STPG with an old binding names the new capability | `test_stpgview_long_wait.py`, `test_stpgview_browser_long_wait.py::test_a_long_period_builds_without_a_wait_plan_notice`, `…::test_old_firmware_refusal_reads_as_a_check_before_reset` | 1, 3 |
| G27 | A segment that ends in DEVICE_ERROR names the firmware's error code (`12 (ERR_TIMEOUT)`), says the answer arrived (not a host response deadline) and does not name a cause; the stream-watchdog sentence is only for ERR_TIMEOUT | `test_stpgview_long_wait.py::test_a_device_reported_timeout_is_named_and_is_not_a_missing_reply`, `…::test_every_firmware_error_code_has_one_name`, `test_stpgview_browser_long_wait.py::test_a_device_error_code_is_named_and_not_called_a_host_timeout` | 1, 3 |
| W1 | Requests on one kept-alive connection: each body is read once or the connection closes, a refused request's body never runs as the next request, and the first STPG binding saves on one press | `test_stpgview_http_connection.py`, `test_stpgview_browser.py::test_the_first_stpg_binding_is_saved_by_one_press_after_a_scan` | 1, 3 |
| B1 | The core has no logic-analyzer code and starts without it | `test_stpgview_core_boundary.py` | 1 |
| B2 | The installed wheel works alone, with no analyzer code, command or GPL text | `test_gui_installed_core.py` | 4 |
| B3 | Host-only guard; independent fake lower layers for its negative controls | the autouse guard of `conftest.py` in every test, and `stpg_hardware_guard.py` also in the Runner modules; `test_stpg_hardware_guard.py` | 1–3 |
| M1 | The manual's links, options and labels are real; no internal material | `test_manual_pages.py` | 1 |
| T1 | The tests can fail | `gui_mutation_check.py` | 5 |

The logic-analyzer tool has its own tests, including the run with the real child process: see `manual/testing.md` in
its repository, `STPG_RP2350_LA`.

## Not covered here

These need their own run and their own approval, and are reported as NOT RUN unless done:

- Native Windows and macOS. The folder rule is modelled for them, not run on them.
- Real STPG, GPIO controller or analyzer hardware.
- Any browser other than the one you ran. The tests use Firefox.
