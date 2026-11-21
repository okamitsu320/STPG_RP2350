#!/usr/bin/env python3
"""Do the GUI tests catch the faults they exist for? Break one thing at a time and require a test to fail.

Each mutant changes one line of a temporary copy of ``stpgview``; the source tree is never touched. The named test
must pass on the unchanged code (the positive control) and fail on the mutant. Run from a checkout:

    python tools/tests/gui_mutation_check.py             # the mutants that need no browser
    python tools/tests/gui_mutation_check.py --browser   # the mutants checked in a real browser (selenium, Firefox)
    python tools/tests/gui_mutation_check.py --list

Exit status 0 only if every selected mutant was caught.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

CHECKOUT = Path(__file__).resolve().parents[2]
PACKAGE = CHECKOUT / "tools" / "stpgview"
S = "tools/tests/test_stpgview_settings_folder.py"
A = "tools/tests/test_stpgview_runner_accessories.py"
R = "tools/tests/test_stpgview_external_result_render.py"
B = "tools/tests/test_stpgview_browser.py"
RJ = "static/runner.js"
R8 = "tools/tests/test_stpgview_results_r8.py"
B8 = "tools/tests/test_stpgview_browser_r8.py"
R9 = "tools/tests/test_stpgview_r9.py"
B9 = "tools/tests/test_stpgview_browser_r9.py"
LW = "tools/tests/test_stpgview_long_wait.py"
BLW = "tools/tests/test_stpgview_browser_long_wait.py"
# id, what it breaks, file in the package, old text, new text, pytest target
MUTANTS = [
    ("SET-01", "--config-dir ignored", "config_location.py", "    if explicit is not None:\n",
     "    if False:\n", f"{S}::test_the_option_wins_over_the_variable_and_the_variable_over_the_default"),
    ("SET-02", "STPG_CONFIG_DIR ignored (the original mismatch)", "config_location.py",
     "    value = environ.get(ENV_NAME)\n", "    value = None\n",
     f"{S}::test_the_option_wins_over_the_variable_and_the_variable_over_the_default"),
    ("SET-03", "a blank --config-dir falls back silently", "config_location.py",
     '            raise ConfigLocationError(f"{OPTION} is blank; give a folder or leave the option out")\n',
     "            return resolve_config_dir(None, environ)\n", f"{S}::test_a_blank_option_is_an_error_and_never_falls_back"),
    ("SET-04", "relative paths stay relative", "config_location.py", "        return path.resolve()\n",
     "        return path\n", f"{S}::test_a_relative_value_is_taken_from_the_start_folder_once"),
    ("SET-05", "looking at the folder creates it", "config_location.py",
     '            return {"state": "new", "problem": None, "counts": counts}\n',
     '            path.mkdir(parents=True)\n            return {"state": "new", "problem": None, "counts": counts}\n',
     f"{S}::test_the_folder_state_is_described_without_creating_it"),
    ("SET-06", "an unreadable folder is listed as empty", "runner/store.py",
     '            raise RunnerError(f"cannot read the settings folder {folder}: {exc.strerror or exc}") from exc\n',
     "            paths = []\n", f"{S}::test_an_unreadable_folder_is_reported_not_shown_as_empty"),
    ("SET-07", "a quoted value is accepted", "config_location.py",
     '    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\\"\'":\n', "    if False:\n",
     f"{S}::test_quoted_and_control_character_values_are_refused"),
    ("SET-08", "the start line hides the reason", "config_location.py",
     "({location.source_text}; {state_text(describe_config_dir(location.path))})",
     "({state_text(describe_config_dir(location.path))})", f"{S}::test_start_line_names_folder_source_and_state"),
    ("ACC-01", "a config edited after confirmation is accepted", "runner/service.py",
     '            if _frozen(live.to_dict()) != frozen["spec_json"] or live_sha != frozen["config_sha256"]:\n',
     "            if False:\n", f"{A}::test_confirmation_freezes_the_external_accessory_selection"),
    ("ACC-02", "role pins are not named", "runner/service.py",
     'if isinstance(entry, dict) and entry.get("role", "signal") != "signal":', "if False:",
     "tools/tests/test_stpgview_runner.py::test_role_pins_of_a_bundle_are_named_in_the_confirmation"),
    ("ACC-03", "the mode of an accessory config is not checked", "runner/service.py",
     '        if spec.mode != ("simulated" if simulated else "device"):\n', "        if False:\n",
     f"{A}::test_demo_controller_run_and_manual_reset_are_simulated"),
    ("TXT-01", "the unadmitted-bytes notice claims an arrival time", RJ,
     "This can include bytes buffered before the cancel; it does not establish their arrival time. ",
     "They arrived after the cancel. ", f"{R}::test_unadmitted_bytes_are_counted_without_an_arrival_claim"),
    ("TXT-02", "the release caveat is dropped", RJ,
     "Process exit does not prove that its device or pins were released.", "",
     f"{R}::test_no_notice_without_unadmitted_bytes_and_no_release_claim"),
    ("SMP-01", "the sample command writes into a folder that is not empty", "physical/patterns.py",
     "    if args.output.exists() and (not args.output.is_dir() or any(args.output.iterdir())):\n", "    if False:\n",
     "tools/tests/test_stpgview_physical_helpers.py::test_the_sample_command_refuses_a_folder_that_is_not_empty"),
    ("HTTP-01", "Scan ports leaves its {} body on the connection", "server.py",
     "            self._json_body()  # the page sends {}: read it, so that it cannot reach the next request\n", "",
     "tools/tests/test_stpgview_http_connection.py::test_scan_then_save_on_one_connection_is_200_200_and_saves_once"),
    ("STOP-01", "a RUN reply that the SDK would reject is taken as accepted", "runner/service.py",
     "    if not accepted(upload, upload_state) or not accepted(started, c.STATE_RUNNING):\n",
     "    if not accepted(upload, upload_state):\n",
     f"{A}::test_the_first_status_notice_is_not_used_for_other_boundaries"),
    ("STOP-02", "a complete late STATUS frame is described as incomplete", "runner/service.py",
     " or len(raw) >= 2 * c.STPG_RSP_SIZE", "",
     f"{A}::test_the_first_status_notice_is_not_used_for_other_boundaries[full-frame-after-deadline]"),
    ("HTTP-02", "a refused request's unread body stays on the connection", "server.py",
     '            self.send_header("Connection", "close")  # also sets close_connection: no leftover byte is parsed\n',
     "            pass\n",
     "tools/tests/test_stpgview_http_connection.py::"
     "test_an_unread_body_closes_the_connection_and_never_becomes_the_next_request"),
    ("MARK-01", "a fail is marked although this view's vector is on another line", "session.py",
     "            elif trace.lines[vector] != line:\n", "            elif False:\n",
     f"{R8}::test_the_viewer_marks_only_the_vector_on_the_named_line"),
    ("GZ-01", "a gzip STIL is read as plain text", "session.py",
     '            if stil_path.name.lower().endswith(".gz"):  # for example a bundle\'s compressed source snapshot\n',
     "            if False:\n", f"{R8}::test_a_gzip_stil_opens_in_the_viewer_with_the_same_lines"),
    ("ACK-04", "the fail-log box of a plan with compares is not required", "runner/service.py",
     '            if any(acks.get(key) is not True for key in confirmation["acknowledgements"]):\n',
     "            if False:\n", f"{R8}::test_the_fail_log_box_is_asked_for_a_plan_with_compares_and_required"),
    ("DESC-01", "a result is described with an open bundle of another manifest", "runner/description.py",
     '            if manifest.is_file() and not manifest.is_symlink() and identity(manifest)["sha256"] == manifest_sha256:\n',
     "            if manifest.is_file():\n", f"{R8}::test_a_mismatch_is_described_against_the_stil_and_saved_as_text"),
    ("DESC-02", "the shared text names the result folder (its name can be a port, serial or private label)",
     "runner/description.py", '    lines = ["STPG run: STIL details", "",\n',
     '    lines = ["STPG run: STIL details", "", f"result: {result_name}",\n',
     f"{R8}::test_details_and_text_reuse_matching_read_result_once_and_omit_result_folder_name"),
    ("DESC-03", "the SDK reads the result folder again instead of the GUI's snapshot", "runner/service.py",
     "            description = describe_result(bundle, manifest_sha256=sha, result=report)\n",
     "            description = describe_result(bundle, manifest_sha256=sha, result=folder)\n",
     f"{R8}::test_details_and_text_reuse_matching_read_result_once_and_omit_result_folder_name"),
    ("SAVE-01", "an existing text file is not refused before writing", "runner/service.py",
     "        if target.exists():\n            raise RunnerError(f\"{target} exists; choose a new file name\")\n        if any((parent / BUNDLE_NAME)",
     "        if any((parent / BUNDLE_NAME)", f"{R8}::test_a_mismatch_is_described_against_the_stil_and_saved_as_text"),
    ("FLS-01", "a stopped fail-log read is not explained", "runner/description.py",
     '                   if isinstance(item.get("fail_log"), dict) and item["fail_log"].get("status") == "READ_ERROR"), None)\n',
     '                   if isinstance(item.get("fail_log"), dict) and item["fail_log"].get("status") == "NEVER"), None)\n',
     f"{R8}::test_fail_log_stop_only_for_a_read_error_and_names_a_rejected_answer"),
    ("EXC-01", "a fail-log page is not named in the exchanges", "runner/service.py",
     '            "request": _RECEIVE_ONLY.get(record.get("kind")) or (',
     '            "request": None or (', f"{R8}::test_exchanges_name_fail_log_records_that_write_nothing"),
    ("REB-01", "an update is started without the exact manifest", "runner/service.py",
     "        if not isinstance(manifest_sha256, str) or not SHA256.fullmatch(manifest_sha256):\n            raise RunnerError(\"updating",
     "        if False:\n            raise RunnerError(\"updating",
     f"{R8}::test_a_bundle_of_another_sdk_is_updated_only_on_request_and_logged"),
    ("WAIT-01", "a Custom length finer than 1 µs is cut instead of refused", "runner/service.py",
     '    if fraction[3:].strip("0"):\n', "    if False:\n",
     f"{R9}::test_a_custom_length_that_needs_rounding_or_is_not_a_plain_number_is_refused"),
    ("WAIT-02", "the Custom length is read as a binary float", "runner/service.py",
     '    microseconds = int(match[1]) * 1000 + int(fraction[:3].ljust(3, "0"))\n',
     "    microseconds = int(float(text) * 1000)\n", f"{R9}::test_a_custom_length_in_ms_becomes_exact_microseconds"),
    ("WAIT-03", "a length typed with another preset is dropped silently", "runner/service.py",
     '    elif request.get("chunk_wait_ms") not in (None, ""):\n', "    elif False:\n",
     f"{R9}::test_a_length_is_never_kept_or_dropped_silently_with_another_preset"),
    ("CTL-01", "the controller's identification lines are not listed", "runner/service.py",
     '                "identity_lines": identity_lines, "identity_lines_total": len(lines),\n',
     '                "identity_lines": [], "identity_lines_total": 0,\n',
     f"{R9}::test_a_refused_identification_lists_the_lines_and_the_stpg_kept_the_upload"),
    ("LOAD-01", "a run that stopped after an accepted upload is not recognised", "runner/service.py",
     '                "left_loaded": upload_without_run(report),\n', '                "left_loaded": None,\n',
     f"{R9}::test_a_refused_identification_lists_the_lines_and_the_stpg_kept_the_upload"),
    ("LOAD-02", "an upload followed by RUN or RESET still counts as kept", "runner/service.py",
     "    last = records[-1] if records else None\n",
     "    last = next((t for t in reversed(records) if t.get(\"command\") == c.CMD_UPLOAD_END), None)\n",
     f"{R9}::test_an_accepted_upload_with_nothing_after_it_is_recognised_only_as_the_last_record"),
    ("LOAD-03", "a pending upload checkpoint is treated as a finished before-RUN stop", "runner/service.py",
     '    if report.get("outcome") not in ("REJECT", "CANCELLED"):\n',
     '    if report.get("outcome") == "COMPLETED":\n',
     "tools/tests/test_stpgview_r9_record_limits.py::test_a_real_pending_upload_checkpoint_can_precede_a_later_successful_run"),
    ("LOAD-04", "the historical upload notice loses its live-state caveat", RJ,
     "This saved record is not a live device check and does not establish current state or pin levels.", "",
     "tools/tests/test_stpgview_r9_record_limits.py::test_the_rendered_notice_is_about_the_record_not_current_hardware"),
    ("ERR-01", "a device error code is shown without its firmware name", "runner/service.py",
     '"error_name": _ERR_NAMES.get(item["terminal"]["error_code"]) if item["terminal"]["error_code"]',
     '"error_name": None if item["terminal"]["error_code"]',
     f"{LW}::test_a_device_reported_timeout_is_named_and_is_not_a_missing_reply"),
]

BROWSER = [
    ("BRW-09", "the confirmation does not say that the fail log is read", RJ,
     "  if (f && s.segments.some((g) => g.compares > 0)) {", "  if (false) {",
     f"{B8}::test_the_confirmation_names_the_fail_log_read_and_what_it_holds"),
    ("BRW-10", "the fail-log read is shown as generic progress", RJ,
     '  } else if (e.stage === "fail_log") {', "  } else if (false) {",
     f"{B8}::test_the_live_view_tells_the_fail_log_read_from_run_progress"),
    ("BRW-11", "a stopped fail-log read is not explained", RJ,
     "  if (res.fail_log_stop) box.append(failLogStopNotice(res.fail_log_stop));", "",
     f"{B8}::test_a_stopped_fail_log_read_is_explained_and_only_that_row_fails"),
    ("BRW-12", "the viewer does not load the fails placed on a pattern", "static/app.js",
     "  if (S.meta && S.meta.fail_marks) loadMarks(id);", "  if (false) loadMarks(id);",
     f"{B8}::test_a_mismatch_shows_its_fail_log_stil_details_and_marks_in_the_viewer"),
    ("BRW-13", "a bundle of another SDK offers no update", RJ,
     '    if (v.status === "PASS" && B.sdk_matches === false) {', "    if (false) {",
     f"{B8}::test_a_bundle_of_another_sdk_is_updated_on_request_and_says_so"),
    ("BRW-14", "the Custom length field is never shown", RJ,
     "      waitLength.hidden = wait.value !== custom;", "      waitLength.hidden = true;",
     f"{B9}::test_custom_length_is_typed_exactly_kept_by_edit_and_copy_and_dropped_by_another_preset"),
    ("BRW-15", "Edit and Copy lose a custom length", RJ,
     '    chunk_wait_ms: typeof w.chunk_wait_us === "number" ? usAsMs(w.chunk_wait_us) : "",', '    chunk_wait_ms: "",',
     f"{B9}::test_custom_length_is_typed_exactly_kept_by_edit_and_copy_and_dropped_by_another_preset"),
    ("BRW-16", "a controller line is inserted as HTML", RJ,
     "[total - lines.length + i + 1, el(\"code\", {}, x.line), ",
     "[total - lines.length + i + 1, Object.assign(el(\"code\"), { innerHTML: x.line }), ",
     f"{B9}::test_controller_lines_are_text_and_the_stpg_kept_the_upload"),
    ("BRW-17", "a record without lines shows nothing", RJ,
     "  if (!lines.length) {\n    box.append(", "  if (!lines.length) {\n    return box;\n    box.append(",
     f"{B9}::test_a_start_check_refusal_lists_no_controller_lines_and_names_the_earlier_stop"),
    ("BRW-18", "the kept upload is not told", RJ,
     "  if (res.left_loaded) box.append(leftLoadedNotice(res.left_loaded, res.control));", "",
     f"{B9}::test_controller_lines_are_text_and_the_stpg_kept_the_upload"),
    ("BRW-19", "a refused wait plan is not explained in Build", RJ,
     '    if (failed && /wait plan rejected/.test(build.job.error || "")) body.append(waitPlanNotice(build.job.error, "build"));',
     "", f"{B9}::test_a_refused_wait_plan_says_nothing_was_adjusted"),
    ("BRW-20", "a bundle whose saved plan is refused is not explained", RJ,
     '    if (waitRejected) body.append(waitPlanNotice(waitRejected, "bundle"));', "",
     f"{B9}::test_a_bundle_with_a_rejected_saved_plan_offers_no_update"),
    ("BRW-21", "a device error is left as a bare number", RJ,
     '  const failed = segments.filter((s) => s.outcome === "DEVICE_ERROR");', "  const failed = [];",
     f"{BLW}::test_a_device_error_code_is_named_and_not_called_a_host_timeout"),
    ("BRW-22", "every device error code is tied to the stream watchdog", RJ,
     '  if (failed.some((s) => s.error_name === "ERR_TIMEOUT")) lines.push(', "  if (true) lines.push(",
     f"{BLW}::test_a_device_error_code_is_named_and_not_called_a_host_timeout"),
    ("BRW-23", "the segment table drops the error name", RJ,
     'const errorCodeText = (s) => s.terminal.error_code ? `${s.terminal.error_code}${s.error_name ? ` (${s.error_name})` : ""}` : "";',
     'const errorCodeText = (s) => s.terminal.error_code || "";',
     f"{BLW}::test_a_device_error_code_is_named_and_not_called_a_host_timeout"),
    ("BRW-06", "the accepted upload loses its result-file annotation", RJ,
     "  if (uploadStage) uploadStage.answered = stop.upload_end;",
     "  if (uploadStage) uploadStage.answered = null;",
     f"{B}::test_the_stage_list_uses_the_acceptance_events_and_keeps_the_result_file_rows"),
    ("BRW-07", "an accepted RUN is listed twice (event and result file)", RJ,
     "    if (!runStage) row(\"done\", `RUN answered ${stop.run}`, took(stop.run_took_s));",
     "    row(\"done\", `RUN answered ${stop.run}`, took(stop.run_took_s));",
     f"{B}::test_the_stage_list_uses_the_acceptance_events_and_keeps_the_result_file_rows"),
    ("BRW-08", "an accepted upload is shown as bytes still being sent", RJ,
     '  } else if (e.stage === "upload" && e.event === "done") {',
     '  } else if (false && e.stage === "upload" && e.event === "done") {',
     f"{B}::test_the_live_view_tells_bytes_sent_upload_accepted_run_accepted_and_status_apart"),
    ("BRW-01", "ticking the box does not update the state", RJ,
     '  use.addEventListener("change", () => { X.use = use.checked; renderExternal(); renderRun(); });',
     '  use.addEventListener("change", () => { X.use = use.checked; renderRun(); });',
     f"{B}::test_each_accessory_state_is_told_apart_and_nothing_starts"),
    ("BRW-02", "a re-render closes the open guide", RJ, "  guide.open = !!R.laGuideOpen;", "  guide.open = false;",
     f"{B}::test_the_logic_analyzer_guidance_says_what_is_here_and_what_to_do"),
    ("BRW-03", "a settings folder problem is not shown", RJ, "  if (c.problem) box.append(", "  if (false) box.append(",
     f"{B}::test_a_settings_folder_problem_is_shown_and_no_other_folder_is_used"),
    ("BRW-04", "the Analyzers list offers Add", RJ, '      kind === "analyzers"\n        ? el("p"',
     '      false\n        ? el("p"', f"{B}::test_analyzer_bindings_are_listed_read_only_by_usb_identity"),
    ("BRW-05", "an edited accessory config starts anyway", "runner/service.py",
     '            if _frozen(live.to_dict()) != frozen["spec_json"] or live_sha != frozen["config_sha256"]:\n',
     "            if False:\n", f"{B}::test_an_accessory_config_edited_after_confirmation_is_rejected_in_the_dialog"),
]


def pytest_run(target: str, copy: Path | None) -> int:
    paths = f"{copy} tools" if copy else "tools"
    command = [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "-o", f"pythonpath={paths}", target]
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(command, cwd=CHECKOUT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode


def check(mutant: tuple) -> tuple[str, bool]:
    ident, what, name, old, new, target = mutant
    source = (PACKAGE / name).read_text(encoding="utf-8")
    if source.count(old) != 1:
        return "STALE (text not found once)", False
    if pytest_run(target, None) != 0:
        return "BASELINE FAILED", False
    with tempfile.TemporaryDirectory(prefix="stpg-mutant-") as tmp:
        shutil.copytree(PACKAGE, Path(tmp) / "stpgview", ignore=shutil.ignore_patterns("__pycache__"))
        (Path(tmp) / "stpgview" / name).write_text(source.replace(old, new), encoding="utf-8")
        code = pytest_run(target, Path(tmp))
    return ("caught", True) if code == 1 else (f"SURVIVED (pytest exit {code})", False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--browser", action="store_true", help="the mutants checked in a real browser")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    selected = BROWSER if args.browser else MUTANTS
    if args.list:
        for mutant in selected:
            print(f"{mutant[0]}  {mutant[1]}  ->  {mutant[5]}")
        return 0
    failed = 0
    for mutant in selected:
        text, ok = check(mutant)
        failed += not ok
        print(f"{mutant[0]}  {text}: {mutant[1]}", flush=True)
    print(f"{len(selected) - failed} of {len(selected)} mutants caught")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
