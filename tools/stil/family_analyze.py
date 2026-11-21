from __future__ import annotations

from collections import Counter
from typing import Any


DRIVE_ACTIONS = {"Drive", "DirZ", "DirDrive", "DirInput", "DirOutput"}
SAMPLE_ACTIONS = {"Compare"}
SUPPORTED_FAMILIES = (
    "M1_DRIVE_THEN_SAMPLE",
    "M2_SAMPLE_THEN_DRIVE",
    "M3_DRIVE_ONLY",
    "M4_SAMPLE_ONLY",
    "M5_SIMULTANEOUS_SAMPLE_DRIVE",
)


def _event_times(vector) -> tuple[set[int], set[int], set[int]]:  # type: ignore[no-untyped-def]
    drive_times: set[int] = set()
    sample_times: set[int] = set()
    dir_times: set[int] = set()
    for signal_events in vector.signal_events.values():
        for event in signal_events:
            if event.action_name in SAMPLE_ACTIONS:
                sample_times.add(int(event.time_ticks))
            elif event.action_name == "Drive":
                drive_times.add(int(event.time_ticks))
            elif event.action_name in DRIVE_ACTIONS:
                dir_times.add(int(event.time_ticks))
    return drive_times, sample_times, dir_times


def classify_micro_op_family(vector) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    drive_times, sample_times, dir_times = _event_times(vector)
    effective_drive_times = sorted(drive_times | dir_times)
    effective_sample_times = sorted(sample_times)

    if not effective_drive_times and not effective_sample_times:
        family = "UNSUPPORTED_EMPTY"
        reason = "vector resolved to no drive/sample events"
    elif effective_drive_times and not effective_sample_times:
        family = "M3_DRIVE_ONLY"
        reason = None
    elif effective_sample_times and not effective_drive_times:
        family = "M4_SAMPLE_ONLY"
        reason = None
    else:
        same_time = sorted(set(effective_drive_times) & set(effective_sample_times))
        if same_time:
            family = "M5_SIMULTANEOUS_SAMPLE_DRIVE"
            reason = None
        else:
            last_drive = effective_drive_times[-1]
            first_drive = effective_drive_times[0]
            last_sample = effective_sample_times[-1]
            first_sample = effective_sample_times[0]
            if last_drive < first_sample:
                family = "M1_DRIVE_THEN_SAMPLE"
                reason = None
            elif last_sample < first_drive:
                family = "M2_SAMPLE_THEN_DRIVE"
                reason = None
            else:
                family = "UNSUPPORTED_INTERLEAVED_PHASES"
                reason = "drive/sample phases interleave across more than one ordered boundary"

    return {
        "family": family,
        "vector_index": int(vector.vector_index),
        "global_vector_index": int(vector.top_level_index),
        "stil_line": int(vector.stil_line),
        "label": vector.label,
        "pattern_name": vector.pattern_name,
        "waveform_table_name": vector.waveform_table_name,
        "drive_times": effective_drive_times,
        "sample_times": effective_sample_times,
        "drive_signal_count": len(vector.drive_chars),
        "compare_signal_count": len(vector.compare_chars),
        "reason": reason,
        "supported": family in SUPPORTED_FAMILIES,
        "optimization_eligible": family in SUPPORTED_FAMILIES,
        "rate_vector_generic_fallback": family != "UNSUPPORTED_EMPTY",
    }


def analyze_micro_op_families(ir) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    # The report retains only bounded examples. Do not build a diagnostic
    # dictionary (plus several filtering lists) for every source vector.
    family_counts: Counter[str] = Counter()
    family_examples: dict[str, list[dict[str, Any]]] = {name: [] for name in SUPPORTED_FAMILIES}
    unsupported_examples: list[dict[str, Any]] = []
    total = supported_count = 0
    for vector in ir.vectors:
        item = classify_micro_op_family(vector)
        total += 1
        family = item["family"]
        family_counts[family] += 1
        if item["supported"]:
            supported_count += 1
            if len(family_examples[family]) < 8:
                family_examples[family].append(item)
        elif len(unsupported_examples) < 16:
            unsupported_examples.append(item)

    return {
        "summary": {
            "total_vectors": total,
            "supported_vectors": supported_count,
            "unsupported_vectors": total - supported_count,
            "micro_op_candidate_ratio": 0.0 if not total else supported_count / total,
            "family_histogram": dict(sorted(family_counts.items())),
            "supported_families": list(SUPPORTED_FAMILIES),
            "optimization_scope": "canonical-micro-op-families-only",
            "generic_rate_vector_fallback": True,
        },
        "unsupported_examples": unsupported_examples,
        "family_examples": family_examples,
    }


def build_micro_op_diagnostics(family_report: dict[str, Any]) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    summary = family_report["summary"]
    if summary["unsupported_vectors"] > 0:
        first = family_report["unsupported_examples"][0]
        diagnostics.append(
            {
                "severity": "warning",
                "code": "WARN_MICRO_OP_OPTIMIZATION_INELIGIBLE",
                "message": (
                    f"{summary['unsupported_vectors']} vector(s) are outside the canonical micro-op "
                    "optimization families; the rate-vector generic fallback remains available "
                    f"when timing/admission checks pass (first line {first['stil_line']}, "
                    f"family={first['family']})"
                ),
                "line": first["stil_line"],
                "column": 1 if first["stil_line"] is not None else None,
                "context": {
                    "unsupported_vectors": summary["unsupported_vectors"],
                    "family_histogram": summary["family_histogram"],
                    "first_unsupported": first,
                    "generic_rate_vector_fallback": True,
                },
            }
        )
    return diagnostics
