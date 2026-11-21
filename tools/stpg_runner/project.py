"""Strict v1 portable project data, separate from local device configuration."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any

from stil.runtime_capacity import DEFAULT_RUNTIME_PROFILE, get_runtime_capacity_profile
from .artifacts import RunnerError, confined_path, portable_path, read_json

PROJECT_KIND = "stpg_runner_project"
PROJECT_SCHEMA_VERSION = 1
WAIT_PRESETS = {"none": 0, "chunk-4ms": 4000, "chunk-12ms": 12000}
# Keep the numeric presets stable for older GUI clients. Custom input is a
# separate explicit mode; a GUI must supply its duration before offering it.
CUSTOM_WAIT_PRESET = "chunk-custom"
WAIT_MODES = (*WAIT_PRESETS, CUSTOM_WAIT_PRESET)


class ProjectError(RunnerError):
    pass


def _object(value: Any, required: set[str], optional: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProjectError(f"{label} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ProjectError(f"{label}: object keys must be strings")
    if required - value.keys() or value.keys() - required - optional:
        raise ProjectError(f"{label}: missing={sorted(required - value.keys())}, "
                           f"unknown={sorted(value.keys() - required - optional)}")
    return value


def _choice(value: Any, choices: tuple[str, ...], label: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ProjectError(f"{label} must be one of {choices}")
    return value


def _integer(value: Any, minimum: int, maximum: int, label: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProjectError(f"{label} must be an integer in {minimum}..{maximum}")
    return value


def _number(value: Any, label: str) -> float:
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        valid = False
    if not valid:
        raise ProjectError(f"{label} must be a finite positive number")
    return float(value)


@dataclass(frozen=True)
class Project:
    """Validated settings. ``to_dict`` returns a copy, not mutable SDK state."""

    _settings: dict[str, Any]

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Project":
        value = deepcopy(value)
        _object(value, {"kind", "schema_version", "input", "target", "compile",
                        "wait", "execution"}, {"segmentation", "capture"}, "project")
        if value["kind"] != PROJECT_KIND or type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ProjectError("unsupported project kind/schema_version")
        source = _object(value["input"], {"stil"}, {"pin_map", "pattern", "pattern_burst"}, "input")
        try:
            source["stil"] = portable_path(source["stil"])
            source.setdefault("pin_map", None)
            if source["pin_map"] is not None:
                source["pin_map"] = portable_path(source["pin_map"])
        except RunnerError as exc:
            raise ProjectError(str(exc)) from exc
        for field in ("pattern", "pattern_burst"):
            source.setdefault(field, None)
            if source[field] is not None and (not isinstance(source[field], str) or not source[field].strip()):
                raise ProjectError(f"input.{field} must be a non-empty string or null")
        target = _object(value["target"], {"runtime_profile", "tick_hz", "max_rates"}, set(), "target")
        if not isinstance(target["runtime_profile"], str):
            raise ProjectError("target.runtime_profile must be a string")
        try:
            get_runtime_capacity_profile(target["runtime_profile"])
        except ValueError as exc:
            raise ProjectError(str(exc)) from exc
        _integer(target["tick_hz"], 1, 0xFFFFFFFF, "target.tick_hz")
        _integer(target["max_rates"], 1, 64, "target.max_rates")
        compile_ = _object(value["compile"], {"mode", "loop_policy"}, {"allow_approx"}, "compile")
        _choice(compile_["mode"], ("auto", "rate-vector", "raw-event", "macro-stream"), "compile.mode")
        _choice(compile_["loop_policy"], ("expand", "preserve"), "compile.loop_policy")
        # Missing in older schema-1 projects means exact. Do not alter their
        # normalized bytes/binding just by loading them with a newer SDK.
        if "allow_approx" in compile_ and type(compile_["allow_approx"]) is not bool:
            raise ProjectError("compile.allow_approx must be a boolean")
        if compile_["loop_policy"] == "preserve" and compile_["mode"] in {"raw-event", "macro-stream"}:
            raise ProjectError("raw-event/macro-stream require explicit loop_policy=expand")
        wait = _object(value["wait"], {"preset"}, {"service_us", "service_source", "service_scope", "max_total_wait_us",
                                                 "forbidden_stil_rates", "chunk_wait_us"}, "wait")
        _choice(wait["preset"], WAIT_MODES, "wait.preset")
        if wait["preset"] == CUSTOM_WAIT_PRESET:
            duration = _integer(wait.get("chunk_wait_us"), 1, 0xFFFFFFFF, "wait.chunk_wait_us")
            numerator = duration * target["tick_hz"]
            if numerator % 1_000_000 or not 1 <= numerator // 1_000_000 <= 0xFFFFFFFF:
                raise ProjectError("wait.chunk_wait_us must fit positive whole uint32 target ticks")
        elif "chunk_wait_us" in wait:
            raise ProjectError("wait.chunk_wait_us is only allowed with chunk-custom")
        wait.setdefault("service_us", None)
        wait.setdefault("service_source", "")
        wait.setdefault("service_scope", None)
        wait.setdefault("max_total_wait_us", None)
        wait.setdefault("forbidden_stil_rates", [])
        forbidden = wait["forbidden_stil_rates"]
        if not isinstance(forbidden, list) or any(not isinstance(item, str) or not item.strip() for item in forbidden):
            raise ProjectError("wait.forbidden_stil_rates must be a list of non-empty names")
        if len(set(forbidden)) != len(forbidden):
            raise ProjectError("duplicate forbidden STIL rate")
        if wait["preset"] == "none":
            if wait["service_us"] is not None or wait["service_source"] or wait["service_scope"] is not None or wait["max_total_wait_us"] is not None or forbidden:
                raise ProjectError("wait=none must not carry ignored service/limit/forbidden settings")
        else:
            _number(wait["service_us"], "wait.service_us")
            _number(wait["max_total_wait_us"], "wait.max_total_wait_us")
            if not isinstance(wait["service_source"], str) or not wait["service_source"].strip():
                raise ProjectError("wait.service_source is required; budgets are not measurements")
            _choice(wait["service_scope"], ("non-loop", "loop-generic"), "wait.service_scope")
            if compile_["mode"] not in {"auto", "rate-vector"}:
                raise ProjectError("chunk waits require rate-vector/auto mode")
        execution = _object(value["execution"], {"storage", "completion"}, set(), "execution")
        _choice(execution["storage"], ("sram", "psram"), "execution.storage")
        _choice(execution["completion"], ("hold", "release"), "execution.completion")
        segment = _object(value.setdefault("segmentation", {"max_vectors": None}),
                          {"max_vectors"}, {"mode"}, "segmentation")
        # Do not normalize a new default into old schema-1 projects: their
        # canonical binding and old cap-only meaning must remain unchanged.
        if "mode" in segment:
            _choice(segment["mode"], ("auto", "single"), "segmentation.mode")
            if segment["mode"] == "single" and segment["max_vectors"] is not None:
                raise ProjectError("segmentation.mode=single requires max_vectors=null")
        if segment["max_vectors"] is not None:
            _integer(segment["max_vectors"], 1, 0xFFFFFFFF, "segmentation.max_vectors")
        capture = _object(value.setdefault("capture", {"enabled": False}), {"enabled"}, set(), "capture")
        if capture["enabled"] is not False:
            raise ProjectError("capture is reserved for M4; enabled must currently be false")
        return cls(value)

    def to_dict(self) -> dict[str, Any]:
        return deepcopy(self._settings)

    def input_paths(self, root: Path) -> dict[str, Path]:
        result = {"stil": confined_path(root, self._settings["input"]["stil"])}
        if self._settings["input"]["pin_map"] is not None:
            result["pin_map"] = confined_path(root, self._settings["input"]["pin_map"])
        for path in result.values():
            if not path.is_file():
                raise ProjectError(f"project input is missing/not a file: {path}")
        return result


def load_project(path: Path) -> Project:
    return Project.from_dict(read_json(Path(path)))


def chunk_wait_microseconds(wait: dict[str, Any]) -> int:
    """Effective length of a validated wait setting, not its service budget."""
    return wait["chunk_wait_us"] if wait["preset"] == CUSTOM_WAIT_PRESET else WAIT_PRESETS[wait["preset"]]


def project_template(*, stil: str, pin_map: str | None = None, wait_preset: str,
                     completion: str, service_us: float | None = None,
                     chunk_wait_us: int | None = None,
                     service_source: str = "", service_scope: str | None = None,
                     max_total_wait_us: float | None = None,
                     forbidden_stil_rates: tuple[str, ...] = (),
                     max_segment_vectors: int | None = None,
                     segmentation_mode: str | None = "auto",
                     storage: str = "psram", compile_mode: str = "auto",
                     loop_policy: str = "preserve", allow_approx: bool = False) -> Project:
    return Project.from_dict({
        "kind": PROJECT_KIND, "schema_version": 1,
        "input": {"stil": stil, "pin_map": pin_map},
        "target": {"runtime_profile": DEFAULT_RUNTIME_PROFILE, "tick_hz": 200_000_000, "max_rates": 64},
        "compile": {"mode": compile_mode, "loop_policy": loop_policy, "allow_approx": allow_approx},
        "wait": {"preset": wait_preset, "service_us": service_us,
                 **({"chunk_wait_us": chunk_wait_us} if chunk_wait_us is not None else {}),
                 "service_source": service_source, "service_scope": service_scope,
                 "max_total_wait_us": max_total_wait_us,
                 "forbidden_stil_rates": list(forbidden_stil_rates)},
        "segmentation": {"max_vectors": max_segment_vectors,
                         **({"mode": segmentation_mode} if segmentation_mode is not None else {})},
        "execution": {"storage": storage, "completion": completion},
        "capture": {"enabled": False},
    })
