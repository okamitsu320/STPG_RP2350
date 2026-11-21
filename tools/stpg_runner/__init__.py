"""Shared STPG runner foundation. Importing this package performs no device I/O."""

__version__ = "0.1.0"

from .project import Project, ProjectError, load_project
from .build import BuildResult, build_project, init_project, verify_bundle
from .diagnostics import sdk_info
from .progress import BuildCancelled, CancellationToken, ProgressEvent
from .device import DeviceConfig, DeviceLease, load_device_config, physical_identity_key
from .execution import (CaptureMetadata, RunHooks, RunPlan, SegmentRunContext,
                        prepare_run, public_summary, run_bundle)
from .controller import ControllerConfig, load_controller_config, manual_control, identify_controller
from .accessories import run_with_accessories, accessory_exit
from .external_accessory import (ExternalAccessorySpec, PreparedExternalAccessory, load_external_accessory,
                                 prepare_external_accessory, run_with_external_accessory)
from .external_binding import ExternalDeviceBinding
from .results import describe_result, read_run_result, run_result_path
from .rebinding import rebind_bundle

__all__ = [
    "BuildCancelled", "BuildResult", "CancellationToken", "ProgressEvent",
    "Project", "ProjectError", "build_project", "init_project", "load_project", "verify_bundle",
    "DeviceConfig", "DeviceLease", "RunPlan", "load_device_config", "prepare_run", "public_summary", "run_bundle",
    "CaptureMetadata", "RunHooks", "SegmentRunContext", "physical_identity_key",
    "ControllerConfig", "load_controller_config", "manual_control", "run_with_accessories", "accessory_exit",
    "identify_controller",
    "sdk_info",
    "ExternalAccessorySpec", "PreparedExternalAccessory", "ExternalDeviceBinding",
    "load_external_accessory", "prepare_external_accessory", "run_with_external_accessory",
    "read_run_result", "run_result_path", "describe_result",
    "rebind_bundle",
]
