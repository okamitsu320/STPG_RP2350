"""Optional GPIO composition and trusted MIT hooks; no analyzer implementation.

No controller/analyzer selection means no accessory import, enumeration, open
or query. Explicit selection is strict: no missing-device fallback or recovery.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import time

from .artifacts import RunnerError, write_json
from .controller import ControllerConfig, ControllerSession, ensure_separate
from .device import DeviceConfig, enumerate_ports, validate_port
from .execution import RunCancelled, RunHooks, prepare_run, public_summary, run_bundle
from .progress import CancellationToken
from .preparation import _invocation_scope
from .results import RUN_RESULT_NAME


def validate_control_plan(value: dict, controller: ControllerConfig, run_plan) -> dict:
    if (not isinstance(value, dict) or set(value) != {"kind", "schema_version", "manifest_sha256", "actions"}
            or value["kind"] != "stpg_gpio_action_plan" or type(value["schema_version"]) is not int
            or value["schema_version"] != 1 or value["manifest_sha256"] != run_plan.manifest_sha256):
        raise RunnerError("invalid or differently bound GPIO action plan")
    actions = value["actions"]
    if not isinstance(actions, list) or not 1 <= len(actions) <= 128:
        raise RunnerError("GPIO action plan requires 1..128 explicit actions")
    previous = (-1, -1)
    for action in actions:
        if not isinstance(action, dict) or set(action) != {"phase", "segment", "pin", "state"}:
            raise RunnerError("GPIO action has missing/unknown fields")
        if (action["phase"] not in ("before_run", "after_terminal") or type(action["segment"]) is not int
                or not 0 <= action["segment"] < len(run_plan.segments)):
            raise RunnerError("invalid GPIO segment boundary")
        controller.validate_action(action["pin"], action["state"], run_attached=True)
        order = (action["segment"], int(action["phase"] == "after_terminal"))
        if order < previous:
            raise RunnerError("GPIO actions must follow segment/boundary order")
        previous = order
    return deepcopy(value)


class _ControllerRun:
    def __init__(self, config, actions, output, *, token, factory, ports, clock):
        self.actions, self.output = actions, output
        self.owned_output = False
        self.session = ControllerSession(config, stream_factory=factory, port_provider=ports, cancel=token, clock=clock)
        self.report = {"kind": "stpg_gpio_run_result", "schema_version": 1, "outcome": "PENDING",
                       "config": config.to_dict(), "plan": actions, "actions": [], "errors": [],
                       "result_persisted": False, "electrical_release_verified": False}

    def checkpoint(self):
        if not self.owned_output:
            raise RunnerError("controller has no owned result workspace")
        self.report["transport"] = self.session.evidence()
        write_json(self.output / "control.json", self.report)

    def apply(self, context, phase):
        for action in self.actions["actions"]:
            if (action["segment"], action["phase"]) != (context.segment_index, phase):
                continue
            try:
                self.checkpoint()  # durable intent before an accessory action
                if self.session.stream is None:
                    self.session.open()
                self.session.set(action["pin"], action["state"])
                self.report["actions"].append({**action, "acknowledged": True})
                self.checkpoint()  # any failure seals later GPIO/STPG requests
            except BaseException as exc:
                self.report["outcome"] = "CANCELLED" if isinstance(exc, (RunCancelled, KeyboardInterrupt)) else "REJECT"
                self.report["errors"].append(f"{type(exc).__name__}: {exc}")
                raise

    def before(self, context):
        self.apply(context, "before_run")

    def after(self, context, terminal):
        self.apply(context, "after_terminal")

    def finish(self, run):
        try:
            self.session.close()  # no corrective pin command, including after a failure
        except BaseException as exc:
            self.report["outcome"] = "REJECT"
            self.report["errors"].append(f"close: {type(exc).__name__}: {exc}")
        if self.report["outcome"] == "PENDING":
            self.report["outcome"] = ("COMPLETED" if len(self.report["actions"]) == len(self.actions["actions"])
                                      else "CANCELLED" if run and run.get("outcome") == "CANCELLED" else "INCOMPLETE")
        if self.owned_output:
            try:
                self.report["result_persisted"] = True
                self.checkpoint()
            except BaseException as exc:
                self.report["outcome"] = "REJECT"; self.report["result_persisted"] = False
                self.report["errors"].append(f"checkpoint: {type(exc).__name__}: {exc}")
        return self.report


@_invocation_scope
def run_with_accessories(bundle_dir: Path, *, manifest_sha256: str, device, output_dir: Path,
                         confirm_run: bool = False, controller: ControllerConfig | None = None,
                         control_plan: dict | None = None, analyzer=None, capture_plan: dict | None = None,
                         marker_derivation: dict | None = None, on_progress=None, cancel=None,
                         capture_mode: str = "device",
                         run_hooks: RunHooks | None = None,
                         stream_factory=None, port_provider=None, controller_stream_factory=None,
                         controller_port_provider=None, analyzer_stream_factory=None, analyzer_port_provider=None,
                         clock=time.monotonic, sleep=time.sleep) -> dict:
    """One invocation, with independently reported core/capture/controller outcomes.

    RUN GPIO0/1 are never touched here; manual HW reset is a separate command.
    The returned run is a public summary; raw evidence stays in local run.json.gz.
    """
    if confirm_run is not True:
        raise RunnerError("explicit confirm_run=True is required")
    if (analyzer is not None or capture_plan is not None or marker_derivation is not None
            or analyzer_stream_factory is not None or analyzer_port_provider is not None or capture_mode != "device"):
        raise RunnerError("in-process analyzer capture is no longer supported; select a separate external accessory process")
    if (controller is None) != (control_plan is None):
        raise RunnerError("each selected controller requires both binding and plan")
    if run_hooks is not None and (type(run_hooks) is not RunHooks
                                  or not any((run_hooks.before_run, run_hooks.after_terminal))
                                  or any(callback is not None and not callable(callback)
                                         for callback in (run_hooks.before_run, run_hooks.after_terminal))):
        raise RunnerError("run_hooks requires trusted callable RunHooks")
    token = cancel or CancellationToken()
    started = clock()

    def relay(event):
        if on_progress is not None:
            on_progress(replace(event, elapsed_s=clock() - started))

    # The ordinary executor owns all admission when no accessory needs an
    # offline plan. Do not duplicate its expensive semantic verification.
    root = Path(bundle_dir).resolve()
    output = Path(output_dir).resolve()
    if output == root or root in output.parents or output.exists():
        raise RunnerError("accessory results require a new directory outside the bundle")
    if token.cancelled:
        # Preserve the ordinary executor's durable CANCELLED result without
        # preparing or contacting any selected accessory (not a run fallback).
        report = run_bundle(bundle_dir, manifest_sha256=manifest_sha256, device=device,
                            output_dir=output, confirm_run=True, on_progress=relay, cancel=token,
                            stream_factory=stream_factory, port_provider=port_provider, clock=clock, sleep=sleep)
        return {"run": public_summary(report), "capture": None, "control": None,
                "run_result_file": str(output / RUN_RESULT_NAME)}
    if controller is not None:
        token.check()
        device = DeviceConfig.from_dict(device.to_dict())
        validate_port(device, (port_provider or enumerate_ports)())
    controlled = None
    if controller is not None:
        plan = prepare_run(bundle_dir, manifest_sha256=manifest_sha256, on_progress=relay,
                           cancel=token, clock=clock)
        from .controller_gate import check_controller_contract
        gate = check_controller_contract()
        if gate["status"] != "PASS":
            raise RunnerError(f"controller structural gate rejected: {gate['errors']}")
        controller = ControllerConfig.from_dict(controller.to_dict())
        ensure_separate(controller, [device])
        actions = validate_control_plan(control_plan, controller, plan)
        controlled = _ControllerRun(controller, actions, output, token=token,
                                    factory=controller_stream_factory, ports=controller_port_provider, clock=clock)
    hooks = RunHooks(controlled.before, controlled.after) if controlled else None
    if run_hooks is not None:
        if hooks is None:
            hooks = run_hooks
        else:
            def before(context):
                controlled.before(context)
                if run_hooks.before_run is not None:
                    return run_hooks.before_run(context)

            def after(context, terminal):
                controlled.after(context, terminal)
                if run_hooks.after_terminal is not None:
                    return run_hooks.after_terminal(context, terminal)

            hooks = RunHooks(before, after)
    result = {"run": None, "capture": None, "control": None, "run_result_file": None}

    def progress(event):
        # Host validation may now report before reservation. Only the explicit
        # owned-workspace event admits controller evidence writes on finalization.
        if controlled is not None and event.stage == "workspace" and event.event == "reserved":
            controlled.owned_output = True
        if on_progress is not None:
            relay(event)

    options = dict(confirm_run=True, on_progress=progress, cancel=token,
                   stream_factory=stream_factory, port_provider=port_provider, clock=clock, sleep=sleep)
    try:
        report = run_bundle(bundle_dir, manifest_sha256=manifest_sha256, device=device,
                            output_dir=output, capture_hook=hooks, **options)
        result["run"], result["run_result_file"] = public_summary(report), str(output / RUN_RESULT_NAME)
    finally:
        if controlled is not None:
            result["control"] = controlled.finish(result["run"])
    return result


def accessory_exit(result: dict) -> int:
    run, control, capture = result.get("run") or {}, result.get("control"), result.get("capture")
    external = result.get("external")
    if (run.get("outcome") == "CANCELLED" or control and control.get("outcome") == "CANCELLED"
            or capture and capture.get("outcome") == "CANCELLED"
            or external and (external.get("outcome") == "CANCELLED" or external.get("cancel_requested") is True)):
        return 130
    if control and (control.get("outcome") != "COMPLETED" or control.get("result_persisted") is not True):
        return 2
    if run.get("outcome") != "COMPLETED" or run.get("result_persisted") is not True:
        return 2
    if capture is not None:
        return 2  # Never accept an old in-process report via an implicit import.
    if external is not None:
        if external.get("outcome") != "COMPLETED" or external.get("result_persisted") is not True:
            return 2
        if external.get("adopted") is not True:
            return 4
    return 3 if run.get("comparison") == "MISMATCH" else 0
