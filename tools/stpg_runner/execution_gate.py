"""Pure-file regression gate for the preview executor, never physical approval."""

from __future__ import annotations

import ast
from collections import Counter
import hashlib
from pathlib import Path
from typing import Any


def _name(node: ast.AST) -> str:
    return ast.unparse(node)


def check_execution_contract(*, execution_source: str | None = None,
                             device_source: str | None = None) -> dict[str, Any]:
    execution_source = (execution_source if execution_source is not None else
                        Path(__file__).with_name("execution.py").read_text(encoding="utf-8"))
    device_source = (device_source if device_source is not None else
                     Path(__file__).with_name("device.py").read_text(encoding="utf-8"))
    errors: list[str] = []
    counts: Counter[str] = Counter()
    try:
        tree, device_tree = ast.parse(execution_source), ast.parse(device_source)
        classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
        methods = {node.name: node for node in classes["_Session"].body if isinstance(node, ast.FunctionDef)}
        expected = {
            "_reset": ["c.CMD_RESET", "c.CMD_GET_STATUS"],
            "_upload": ["c.CMD_UPLOAD_BEGIN", "c.CMD_UPLOAD_DATA", "c.CMD_UPLOAD_END"],
            "_start": ["c.CMD_RUN"], "_observe": ["c.CMD_GET_STATUS"],
            "_fail_log": ["c.CMD_READ_FAIL_LOG"],
            "execute": ["c.CMD_INFO", "c.CMD_GET_CLOCK_INFO", "c.CMD_GET_STATUS", "c.CMD_INFO"],
        }

        def exchange_sites(node: ast.AST, depth: int = 0) -> list[tuple[str, int]]:
            found: list[tuple[str, int]] = []
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "exchange":
                found.append((_name(node.args[0]), depth))
            for child in ast.iter_child_nodes(node):
                found.extend(exchange_sites(child, depth + int(isinstance(node, (ast.For, ast.While, ast.AsyncFor)))))
            return found

        all_sites = exchange_sites(tree)
        for command, _ in all_sites:
            counts[command] += 1
        if counts != Counter(command for commands in expected.values() for command in commands):
            errors.append("exact command call-site inventory changed")

        def helper_sites(node: ast.AST, names: set[str], depth: int = 0) -> list[tuple[str, int]]:
            found: list[tuple[str, int]] = []
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in names:
                found.append((node.func.attr, depth))
            for child in ast.iter_child_nodes(node):
                found.extend(helper_sites(child, names, depth + int(isinstance(node, (ast.For, ast.While, ast.AsyncFor)))))
            return found

        helpers = {"_reset", "_upload", "_start", "_observe", "execute", "payload", "_write", "_read", "_invoke_hook", "_fail_log", "fail_data"}
        if Counter(name for name, _ in helper_sites(tree, helpers)) != Counter(
            {"_reset": 2, "_upload": 1, "_start": 1, "_observe": 1, "execute": 1,
             "payload": 1, "_write": 2, "_read": 4, "_invoke_hook": 2, "_fail_log": 1, "fail_data": 1}
        ):
            errors.append("exact protocol-helper call-site inventory changed")
        if helper_sites(methods["execute"], {"_reset", "_upload", "_start", "_observe"}) != [
            ("_reset", 0), ("_upload", 1), ("_start", 1), ("_observe", 1), ("_reset", 0),
        ]:
            errors.append("protocol-helper ordering/loop nesting changed")
        for name, commands in expected.items():
            sites = exchange_sites(methods[name])
            # ast field order, unlike ast.walk, preserves source statement order.
            if [command for command, _ in sites] != commands:
                errors.append(f"{name}: command order/count changed")
            for command, depth in sites:
                wanted = int((name == "_upload" and command == "c.CMD_UPLOAD_DATA") or name in {"_observe", "_fail_log"})
                if depth != wanted:
                    errors.append(f"{name}: command loop nesting changed")
        functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        prepare, run = functions["prepare_run"], functions["run_bundle"]
        reuse = functions["_reuse_invocation_plan"]
        if [_name(node) for node in run.decorator_list] != ["_invocation_scope"]:
            errors.append("executor must own a private invocation scope")
        # The optimized path is not an unchecked plan parameter: exact private
        # owner lookup, plan/manifest/tool seals and full inventory are required.
        reuse_source = _name(reuse)
        required_reuse = [
            "state = _current_invocation()",
            "record = state.plans.get((root, manifest_sha256)) if state is not None else None",
            "if record is None:\n        return None",
            "if type(plan) is not RunPlan or _plan_seal(plan) != seal:",
            "manifest = read_json(confined_path(root, BUNDLE_NAME), expected_sha256=manifest_sha256)",
            "canonical_json(manifest) != manifest_bytes or tool_fingerprint() != fingerprint",
            "manifest['binding']['tool_sha256'] != fingerprint",
            "_verify_inventory(root, manifest, manifest_sha256=manifest_sha256, progress=progress, stage='shared_files')",
            "if _plan_seal(plan) != seal or tool_fingerprint() != fingerprint:",
        ]
        if any(text not in reuse_source for text in required_reuse):
            errors.append("private snapshot ownership/seal/manifest/tool/inventory guard changed")
        reuse_calls = [node for node in ast.walk(reuse) if isinstance(node, ast.Call)]
        if sum(_name(node.func) == "_verify_inventory" for node in reuse_calls) != 1:
            errors.append("shared snapshot must hash the exact inventory once")
        if _name(functions["_plan_seal"].body[0]) != (
                "return canonical_json({'root': str(plan.bundle_dir), 'plan': plan.to_dict()})"):
            errors.append("execution plan seal must bind every field/payload and root")
        prepare_calls = [_name(node.func) for node in ast.walk(prepare) if isinstance(node, ast.Call)]
        if prepare_calls.count("verify_bundle") != 1 or prepare_calls.count("_verify_inventory") != 1:
            errors.append("exact initial verification/final inventory guard sites changed")
        verify_stmt = next(node for node in prepare.body if isinstance(node, ast.Assign)
                           and isinstance(node.value, ast.Call) and _name(node.value.func) == "verify_bundle")
        snapshot_loop = next(node for node in prepare.body if isinstance(node, ast.For))
        inventory_stmt = next(node for node in prepare.body if isinstance(node, ast.Expr)
                              and isinstance(node.value, ast.Call)
                              and _name(node.value.func) == "progress.perform"
                              and _name(node.value.args[0]) == "'snapshot_verify'")
        returned = next(node for node in prepare.body if isinstance(node, ast.Return))
        if not (prepare.body.index(verify_stmt) < prepare.body.index(snapshot_loop)
                < prepare.body.index(inventory_stmt) < prepare.body.index(returned)):
            errors.append("semantic verification/snapshot/inventory/return ordering changed")
        pinned_manifest = next(node for node in prepare.body if isinstance(node, ast.Assign)
                               and _name(node.targets[0]) == "manifest")
        if _name(pinned_manifest.value) != "read_json(root / BUNDLE_NAME, expected_sha256=manifest_sha256)":
            errors.append("execution snapshot manifest must be read with its exact hash")
        run_calls = [node for node in ast.walk(run) if isinstance(node, ast.Call)]
        if sum(_name(node.func) == "prepare_run" for node in run_calls) != 1:
            errors.append("executor must prepare exactly one authoritative snapshot")
        lease = next(node for node in ast.walk(run) if isinstance(node, ast.With)
                     and any(_name(item.context_expr) == "DeviceLease(device)" for item in node.items))
        phases = [node for node in lease.body if isinstance(node, ast.Expr)
                  and isinstance(node.value, ast.Call) and _name(node.value.func) == "check_port"]
        if (len(phases) != 2 or [_name(node.value) for node in phases]
                != ["check_port('before_bundle_verify')", "check_port('before_open')"]
                or sum(_name(node.func) == "check_port" for node in run_calls) != 2):
            errors.append("exact early/pre-open metadata call sites changed")
        prepared_stmt = next(node for node in lease.body if isinstance(node, ast.Assign)
                             and isinstance(node.value, ast.Call) and _name(node.value.func) == "prepare_run")
        open_stmt = next(node for node in lease.body if isinstance(node, ast.Try))
        if not (lease.body.index(phases[0]) < lease.body.index(prepared_stmt)
                < lease.body.index(phases[1]) < lease.body.index(open_stmt)):
            errors.append("leased metadata/prepare/recheck/open ordering changed")
        final_guard = next(node for node in lease.body if isinstance(node, ast.If)
                           and "_reuse_invocation_plan" in _name(node.test))
        if (_name(final_guard.test) != "_reuse_invocation_plan(root, manifest_sha256, Reporter(None, token, clock=clock)) is not plan"
                or lease.body.index(final_guard) >= lease.body.index(open_stmt)
                or lease.body.index(final_guard) <= lease.body.index(phases[1])
                or not any(isinstance(node, ast.Raise) for node in final_guard.body)):
            errors.append("final silent pre-open snapshot/inventory guard changed")
        if (prepare_calls.count("_reuse_invocation_plan") != 1
                or "reuse=True" not in _name(verify_stmt.value)
                or "state.plans[root, manifest_sha256] = (plan, _plan_seal(plan), canonical_json(manifest), fingerprint)"
                not in _name(prepare)):
            errors.append("initial snapshot registration/reuse path changed")
        constants = {node.targets[0].id: ast.literal_eval(node.value) for node in tree.body
                     if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                     and isinstance(node.value, ast.Constant)}
        required = {"MAX_SEGMENTS": 256, "MAX_POLLS": 300, "POLL_INTERVAL_S": 0.2,
                    "OBSERVATION_TIMEOUT_S": 60.0, "RESPONSE_TIMEOUT_S": 2.0,
                    "ADMISSION_TIMEOUT_S": 20.0, "STATUS_RESPONSE_TIMEOUT_S": 20.0}
        if any(constants.get(key) != value for key, value in required.items()):
            errors.append("fixed execution bounds changed")
        snapshot_bound = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                              and _name(node.targets[0]) == "MAX_SNAPSHOT_BYTES")
        if _name(snapshot_bound) != "128 * 1024 * 1024":
            errors.append("snapshot bound changed")
        observe_loop = next(node for node in methods["_observe"].body if isinstance(node, ast.For))
        if _name(observe_loop.iter) != "range(MAX_POLLS)":
            errors.append("observation loop bound changed")
        prefix = [_name(node) for node in observe_loop.body[:6]]
        wanted_prefix = [
            "self.wire.check_cancel()",
            "if self.clock() >= deadline:\n    raise RunnerError('terminal observation deadline exhausted')",
            "self.sleep(POLL_INTERVAL_S)",
            "if self.clock() >= deadline:\n    raise RunnerError('terminal observation deadline exhausted after sleep')",
            "response = self.wire.exchange(c.CMD_GET_STATUS, timeout=STATUS_RESPONSE_TIMEOUT_S)",
            "outcome = judge_status(response, segment.expected, terminal_state=segment.terminal_state)",
        ]
        if prefix != wanted_prefix:
            errors.append("sleep/deadline/STATUS/strict-judge ordering changed")
        # The extended budget belongs only to post-RUN observation. Initial and
        # RESET-check STATUS keep the ordinary default, as do payload ACKs.
        for name in ("_reset", "execute"):
            status_calls = [node for node in ast.walk(methods[name]) if isinstance(node, ast.Call)
                            and _name(node.func) == "self.wire.exchange"
                            and node.args and _name(node.args[0]) == "c.CMD_GET_STATUS"]
            if len(status_calls) != 1 or len(status_calls[0].args) != 1 or status_calls[0].keywords:
                errors.append(f"{name}: non-observation STATUS budget changed")
        wire_methods = {node.name: node for node in classes["_Wire"].body if isinstance(node, ast.FunctionDef)}
        exchange = wire_methods["exchange"]
        if (len(exchange.args.kwonlyargs) != 1 or exchange.args.kwonlyargs[0].arg != "timeout"
                or [_name(value) for value in exchange.args.kw_defaults] != ["RESPONSE_TIMEOUT_S"]):
            errors.append("ordinary response default changed")
        if _name(wire_methods["payload"].body[-1]) != "return self._read(record, seq, RESPONSE_TIMEOUT_S)":
            errors.append("payload ACK budget changed")
        # Acceptance notifications are facts only after strict ACK validation,
        # never a terminal/completion claim or a gap between before_run and RUN.
        tails = {
            "_upload": """self._expect(response, c.RSP_OK, (c.STATE_HOLD_AWAITING_NEXT if segment.index else c.STATE_READY,),
                **self._tags(segment), run_progress=0, sample_count=0, fail_count=0, result_flags=0)
self.emit("upload", "done", completed=len(payload), total=len(payload),
          detail=f"segment={segment.index}; UPLOAD_END accepted")""",
            "_start": """self._expect(response, c.RSP_OK, (c.STATE_RUNNING,), **self._tags(segment),
                run_progress=0, sample_count=0, fail_count=0, result_flags=0)
self.emit("run", "accepted", detail=f"segment={segment.index}; RUN accepted; waiting for STATUS; "
          "no waveform or terminal completion observed")""",
        }
        for name, expected_tail in tails.items():
            if [_name(node) for node in methods[name].body[-2:]] != [
                    _name(node) for node in ast.parse(expected_tail).body]:
                errors.append(f"{name}: acceptance notification/validation ordering changed")
        emitted = Counter((_name(node.args[0]), _name(node.args[1])) for node in ast.walk(classes["_Session"])
                          if isinstance(node, ast.Call) and _name(node.func) == "self.emit")
        if emitted != Counter({("'upload'", "'start'"): 1, ("'upload'", "'progress'"): 1,
                               ("'upload'", "'done'"): 1, ("'run'", "'accepted'"): 1,
                               ("'fail_log'", "'start'"): 1, ("'fail_log'", "'progress'"): 1,
                               ("'fail_log'", "'done'"): 1,
                               ("'observe'", "'progress'"): 1, ("'segment'", "'done'"): 1}):
            errors.append("exact session progress notification inventory changed")
        upload_loop = next(node for node in methods["_upload"].body if isinstance(node, ast.For))
        if _name(upload_loop.iter) != "range(0, len(payload), c.STPG_UPLOAD_CHUNK_SIZE)":
            errors.append("upload chunk loop bound changed")
        upload_calls = [node.func.attr for node in ast.walk(upload_loop)
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in {"exchange", "payload", "_expect"}]
        if upload_calls != ["exchange", "_expect", "payload", "_expect"]:
            errors.append("DATA/READY/payload/ACK validation ordering changed")
        execute = methods["execute"]
        execute_calls = [node for node in ast.walk(execute) if isinstance(node, ast.Call)]
        resets = [node for node in execute_calls if isinstance(node.func, ast.Attribute) and node.func.attr == "_reset"]
        if len(resets) != 2 or [_name(node.args[0]) for node in resets] != ["False", "True"]:
            errors.append("planned reset/release sites changed")
        run_loop = next(node for node in execute.body if isinstance(node, ast.For)
                        and any(isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                                and child.func.attr == "_upload" for child in ast.walk(node)))
        actions = [node.func.attr for node in ast.walk(run_loop) if isinstance(node, ast.Call)
                   and isinstance(node.func, ast.Attribute) and node.func.attr in {"_reset", "_upload", "_start", "_observe"}]
        if actions != ["_upload", "_start", "_observe"] or _name(run_loop.iter) != "self.plan.segments":
            errors.append("segment loop upload/RUN/observation ordering changed")
        fail_method = methods["_fail_log"]
        fail_text = _name(fail_method)
        required_fail = [
            "if terminal.fail_count == 0:\n        return",
            "count = min(terminal.fail_count, c.STPG_FAIL_LOG_MAX)",
            "for offset in range(0, count, c.STPG_FAIL_LOG_READ_PAGE_ENTRIES):",
            "page_count = min(count - offset, c.STPG_FAIL_LOG_READ_PAGE_ENTRIES)",
            "length = page_count * c.STPG_FAIL_LOG_ENTRY_SIZE",
            "self._expect(ready, c.RSP_READY_FOR_READ, (terminal.state,), event_count=page_count, **tags)",
            "payload, trailer = self.wire.fail_data(ready.seq, length)",
            "self._expect(trailer, c.RSP_DATA_END, (terminal.state,), event_count=crc32(payload), **tags)",
            "log['status'] = 'READ_ERROR'",
        ]
        if any(text not in fail_text for text in required_fail):
            errors.append("FAIL_LOG mismatch-only bounded paging/validation/stop guard changed")
        fail_call = next(node for node in run_loop.body if isinstance(node, ast.Expr)
                         and isinstance(node.value, ast.Call) and _name(node.value.func) == "self._fail_log")
        hook_after = next(node for node in run_loop.body if isinstance(node, ast.Expr)
                          and isinstance(node.value, ast.Call)
                          and _name(node.value) == "self._invoke_hook('after_terminal', segment, terminal)")
        if run_loop.body.index(fail_call) <= run_loop.body.index(hook_after):
            errors.append("FAIL_LOG must follow persisted non-ERROR terminal and the terminal hook")
        if helper_sites(execute, {"_invoke_hook"}) != [("_invoke_hook", 1), ("_invoke_hook", 1)]:
            errors.append("hook loop nesting/count changed")
        # Keep hooks at the precise accepted-upload / persisted-terminal boundaries.
        # In particular no checkpoint/progress/sleep may slip between arm and RUN.
        hook_positions = [index for index, node in enumerate(run_loop.body)
                          if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                          and _name(node.value.func) == "self._invoke_hook"]
        before, after = hook_positions
        if ([_name(node) for node in run_loop.body[before - 2:before + 2]] != [
                "self._upload(segment)", "self.checkpoint()",
                "self._invoke_hook('before_run', segment)", "self._start(segment)"]
                or [_name(node) for node in run_loop.body[after - 2:after + 2]] != [
                    "self.checkpoint()",
                    "if terminal.state == c.STATE_ERROR:\n    raise _DeviceError(f'terminal device error {terminal.error_code}; no continuation')",
                    "self._invoke_hook('after_terminal', segment, terminal)",
                    "if self.hooks is not None and self.hooks.after_terminal is not None:\n    self.checkpoint()"]):
            errors.append("hook admission/checkpoint/ERROR/RUN ordering changed")
        callback_calls = [_name(node) for node in ast.walk(methods["_invoke_hook"])
                          if isinstance(node, ast.Call) and _name(node.func) == "callback"]
        if callback_calls != ["callback(context)", "callback(context, asdict(terminal))"]:
            errors.append("exact callback dispatch sites changed")
        release = next(node for node in execute.body if isinstance(node, ast.If)
                       and "completion" in _name(node.test))
        if _name(release.test) != "self.plan.completion == 'release'" or execute.body.index(release) <= execute.body.index(run_loop):
            errors.append("release admission must follow the complete segment loop")
        reset_stmt = next(node for node in execute.body if isinstance(node, ast.Expr)
                          and isinstance(node.value, ast.Call) and _name(node.value.func) == "self._reset")
        if execute.body.index(reset_stmt) >= execute.body.index(run_loop):
            errors.append("initial reset must precede uploads")
        io = Counter(_name(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Attribute) and node.func.attr in {"read", "write", "close"})
        if io != Counter({"self.stream.read": 1, "self.stream.write": 1, "stream.close": 1}):
            errors.append("exact read/write/close sites changed")
        opens = [node for node in ast.walk(device_tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == "open"]
        if Counter(_name(node.func) for node in opens) != Counter({"stream.open": 1, "os.open": 1}):
            errors.append("device open sites changed")
        lease = next(node for node in device_tree.body if isinstance(node, ast.ClassDef) and node.name == "DeviceLease")
        lease_init = next(node for node in lease.body if isinstance(node, ast.FunctionDef) and node.name == "__init__")
        key = next(node for node in lease_init.body if isinstance(node, ast.Assign) and _name(node.targets[0]) == "self.key")
        lock_open = next(node for node in opens if _name(node.func) == "os.open")
        if _name(key.value) != "physical_identity_key(config)" or _name(lock_open.args[0]) != "root / f'{self.key}.lock'":
            errors.append("physical identity lease namespace changed")
        serial = next(node for node in device_tree.body if isinstance(node, ast.FunctionDef) and node.name == "open_serial")
        serial_calls = [node for node in ast.walk(serial) if isinstance(node, ast.Call)]
        constructors = [node for node in serial_calls if _name(node.func) == "serial.Serial"]
        if len(constructors) != 1:
            errors.append("serial constructor count changed")
        else:
            settings = {kw.arg: _name(kw.value) for kw in constructors[0].keywords}
            if settings != {"port": "None", "baudrate": "115200", "timeout": "SERIAL_READ_TIMEOUT_S",
                            "write_timeout": "2.0", "dsrdtr": "sys.platform != 'win32'",
                            "rtscts": "False", "xonxoff": "False"}:
                errors.append("serial open/line-control contract changed")
        if Counter(_name(node.func) for node in serial_calls) != Counter({"serial.Serial": 1, "stream.open": 1}):
            errors.append("extra operation in serial opener")
        assignments = [_name(node.targets[0]) for node in serial.body if isinstance(node, ast.Assign)]
        if assignments != ["stream", "stream.rts", "stream.port"]:
            errors.append("serial setup assignment contract changed")
        serial_order = [_name(node) for node in serial.body]
        rts = serial_order.index("stream.rts = True")
        opened = serial_order.index("stream.open()")
        if rts >= opened or any("stream." in text and "=" in text for text in serial_order[opened + 1:]):
            errors.append("line control after open")
        read_timeout = next(node.value for node in device_tree.body if isinstance(node, ast.Assign)
                            and _name(node.targets[0]) == "SERIAL_READ_TIMEOUT_S")
        if _name(read_timeout) != "0.05":
            errors.append("fixed serial read slice changed")
        read = next(node for node in classes["_Wire"].body
                    if isinstance(node, ast.FunctionDef) and node.name == "_read")
        read_loop = next(node for node in ast.walk(read) if isinstance(node, ast.For))
        if _name(read_loop.iter) != "range(max_reads)":
            errors.append("bounded response read loop changed")
        max_reads = next(node for node in read.body if isinstance(node, ast.Assign)
                         and _name(node.targets[0]) == "max_reads")
        if _name(max_reads.value) != "math.ceil(timeout / SERIAL_READ_TIMEOUT_S) + c.STPG_RSP_SIZE":
            errors.append("response read-call cap changed")
        if [_name(node) for node in read_loop.body[:4]] != [
            "stage = 'cancel'", "self.check_cancel()", "stage = 'deadline'",
            "if self.clock() >= deadline:\n    raise RunnerError('response deadline exhausted; partial bytes retained')"]:
            errors.append("read cancellation/deadline ordering changed")
        if not any(isinstance(node, ast.If) and _name(node.test) == "call['end_s'] > deadline"
                   for node in read_loop.body):
            errors.append("post-read deadline check missing")
        for node in ast.walk(read):
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(target, ast.Attribute) and _name(target).startswith("self.stream.")
                       for target in targets):
                    errors.append("serial setting written during response read")
        for parsed in (tree, device_tree):
            for node in ast.walk(parsed):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                if any(name.split(".")[0] in {"subprocess", "socket", "ctypes"} for name in names):
                    errors.append("unexpected process/socket/native-I/O dependency")
        # Exception/finally paths may only close, never issue protocol commands.
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                for part in [*node.handlers, *node.finalbody]:
                    if exchange_sites(part) or helper_sites(part, helpers):
                        errors.append("protocol command in exception/finally path")
    except (SyntaxError, KeyError, StopIteration, ValueError, IndexError, AttributeError) as exc:
        errors.append(f"structural parse rejected: {type(exc).__name__}: {exc}")
    return {"kind": "stpg_runner_execution_gate", "schema_version": 1,
            "status": "REJECT" if errors else "PASS", "errors": errors,
            "command_call_sites": dict(sorted(counts.items())),
            "execution_source_sha256": hashlib.sha256(execution_source.encode("utf-8")).hexdigest(),
            "device_source_sha256": hashlib.sha256(device_source.encode("utf-8")).hexdigest(),
            "physical_execution_authorized": False, "device_io_performed": False}
