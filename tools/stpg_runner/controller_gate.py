"""Pure-file GPIO regression check, not a physical authorization/security sandbox."""
import ast
from collections import Counter
from pathlib import Path


def check_controller_contract(source: str | None = None, accessories_source: str | None = None) -> dict:
    source = source if source is not None else Path(__file__).with_name("controller.py").read_text(encoding="utf-8")
    accessories_source = (accessories_source if accessories_source is not None else
                          Path(__file__).with_name("accessories.py").read_text(encoding="utf-8"))
    errors = []
    try:
        tree = ast.parse(source)
        calls = Counter(ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call))
        for name, count in (("self.stream.write", 1), ("self.stream.read", 1), ("self.stream.close", 1),
                            ("serial.Serial", 1), ("session.set", 2), ("session.open", 2), ("session.close", 2)):
            if calls[name] != count:
                errors.append(f"exact controller call-site inventory differs: {name}")
        functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        identify_calls = Counter(ast.unparse(node.func) for node in ast.walk(functions["identify_controller"])
                                 if isinstance(node, ast.Call))
        if (identify_calls["session.open"] != 1 or identify_calls["session.close"] != 1
                or identify_calls["session.set"] or identify_calls["session._write"]):
            errors.append("identify must open/close once and never issue a GPIO/direct command")
        manual = functions["manual_control"]
        sets = [node for node in ast.walk(manual) if isinstance(node, ast.Call)
                and ast.unparse(node.func) == "session.set"]
        if [ast.unparse(node) for node in sets] != [
            "session.set(pin, requested_state, allow_run_high=allow_run_high)", "session.set(pin, 'Hi-Z')"]:
            errors.append("explicit manual set/pulse command selection changed")
        requested = [ast.unparse(node) for node in manual.body if isinstance(node, ast.Assign)
                     and any(isinstance(item, ast.Name) and item.id == "requested_state" for item in node.targets)]
        if requested != ["requested_state = 'LOW' if pulse_ms is not None else state"]:
            errors.append("manual GPIO state must not silently default to LOW")
        target_guard = next(node for node in manual.body if isinstance(node, ast.If)
                            and ast.unparse(node.test) == "pin in (0, 1)")
        if [ast.unparse(node) for node in target_guard.body] != [
            "from .external_binding import target_binding", "target = target_binding(target.to_dict())",
            "target_kinds = ('stpg_runner_device',) if pin == 0 else ('stpg_capture_analyzer', 'stpg_external_device')",
            "if target.to_dict().get('kind') not in target_kinds:\n    raise RunnerError('RUN pin and target binding kind differ')",
        ]:
            errors.append("exact manual RUN target validation/role kinds changed")
        for node in ast.walk(manual):
            if isinstance(node, (ast.For, ast.While)):
                errors.append("manual control may not retry/loop")
            if isinstance(node, ast.Try):
                for part in [*node.handlers, *node.finalbody]:
                    if any(isinstance(child, ast.Call) and ast.unparse(child.func) == "session.set"
                           for child in ast.walk(part)):
                        errors.append("GPIO write in failure/finally path")
        classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
        methods = {node.name: node for node in classes["ControllerSession"].body if isinstance(node, ast.FunctionDef)}
        if any(isinstance(node, ast.Call) and ast.unparse(node.func) in ("self.set", "self._write")
               for node in ast.walk(methods["close"])):
            errors.append("controller close must not change pins")
        config_methods = {node.name: node for node in classes["ControllerConfig"].body if isinstance(node, ast.FunctionDef)}
        guard = next(node for node in config_methods["validate_action"].body
                     if isinstance(node, ast.If) and ast.unparse(node.test) == "run_attached and pin in (0, 1)")
        if ast.unparse(guard.test) != "run_attached and pin in (0, 1)" or not isinstance(guard.body[0], ast.Raise):
            errors.append("RUN pins must be excluded from pattern-run actions")
        loop = next(node for node in methods["_line"].body if isinstance(node, ast.While))
        if (ast.unparse(loop.body[0]) != "self.check()" or not isinstance(loop.body[1], ast.If)
                or ast.unparse(loop.body[1].test) != "self.clock() >= deadline"):
            errors.append("controller cancellation/deadline check order differs")
        accessory_tree = ast.parse(accessories_source)
        validations = [ast.unparse(node) for node in ast.walk(accessory_tree) if isinstance(node, ast.Call)
                       and isinstance(node.func, ast.Attribute) and node.func.attr == "validate_action"]
        if validations != ["controller.validate_action(action['pin'], action['state'], run_attached=True)"]:
            errors.append("exact run-attached pin admission changed")
        accessory_calls = Counter(ast.unparse(node.func) for node in ast.walk(accessory_tree) if isinstance(node, ast.Call))
        if accessory_calls["self.session.set"] != 1 or accessory_calls["self.session.open"] != 1:
            errors.append("accessory GPIO call-site counts changed")
        adapter = next(node for node in accessory_tree.body if isinstance(node, ast.ClassDef) and node.name == "_ControllerRun")
        adapter_methods = {node.name: node for node in adapter.body if isinstance(node, ast.FunctionDef)}
        for name, phase in (("before", "before_run"), ("after", "after_terminal")):
            if [ast.unparse(node) for node in adapter_methods[name].body] != [f"self.apply(context, '{phase}')"]:
                errors.append("accessory hook boundary changed")
        for node in ast.walk(adapter):
            if isinstance(node, ast.Try):
                for part in [*node.handlers, *node.finalbody]:
                    if any(isinstance(child, ast.Call) and ast.unparse(child.func) in ("self.session.set", "self.session.open")
                           for child in ast.walk(part)):
                        errors.append("accessory GPIO action in failure/finally path")
    except (SyntaxError, KeyError, IndexError, StopIteration, ValueError, AttributeError) as exc:
        errors.append(f"controller structure rejected: {type(exc).__name__}: {exc}")
    return {"status": "REJECT" if errors else "PASS", "errors": errors,
            "device_io_performed": False, "physical_execution_authorized": False}
