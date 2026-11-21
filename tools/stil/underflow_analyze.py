from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json

from stpg.constants import STPG_RATE_SLOT_DIR_UPDATE, STPG_RATE_SLOT_SAMPLE, STPG_RATEVEC_MAGIC
from stpg.pattern import (
    LoopBlock,
    LoopRefRecord,
    RateDesc,
    RateSlot,
    VectorRecord,
    emitted_event_count_estimate,
    streaming_poc_timing_incompatibility,
    unpack_rate_vector_full,
)

from .json_artifacts import read_json_artifact

DEFAULT_CHUNK_EVENT_BUDGET = 4096
ANALYZER_VERSION = "v0.1"


@dataclass(frozen=True)
class MergedGroup:
    group_index: int
    time_ticks: int
    gap_ticks: int
    sample_trigger: bool
    op_count: int
    dir_update: bool
    required_ticks: int
    deficit_ticks: int


@dataclass(frozen=True)
class RateRisk:
    rate_id: int
    period_ticks: int
    merged_groups: tuple[MergedGroup, ...]
    emitted_groups_per_vector: int
    requires_compare: bool
    score: int


@dataclass(frozen=True)
class VectorSpan:
    source_kind: str
    vector_record_index: int
    rate_id: int
    repeat: int
    expanded_start: int
    expanded_end_exclusive: int
    emitted_events: int
    score: int
    global_vector_start: int | None
    global_vector_end: int | None
    stil_line_start: int | None
    stil_line_end: int | None
    label_start: str | None
    label_end: str | None
    pattern_name: str | None
    waveform_table: str | None


def _merge_adjacent_rate_runs(spans: list[VectorSpan]) -> list[VectorSpan]:
    if not spans:
        return []
    merged: list[VectorSpan] = [spans[0]]
    for span in spans[1:]:
        prev = merged[-1]
        if prev.rate_id != span.rate_id or prev.expanded_end_exclusive != span.expanded_start:
            merged.append(span)
            continue
        merged[-1] = VectorSpan(
            source_kind="rate-run",
            vector_record_index=prev.vector_record_index,
            rate_id=prev.rate_id,
            repeat=prev.repeat + span.repeat,
            expanded_start=prev.expanded_start,
            expanded_end_exclusive=span.expanded_end_exclusive,
            emitted_events=prev.emitted_events + span.emitted_events,
            score=prev.score + span.score,
            global_vector_start=prev.global_vector_start,
            global_vector_end=span.global_vector_end if span.global_vector_end is not None else prev.global_vector_end,
            stil_line_start=prev.stil_line_start,
            stil_line_end=span.stil_line_end if span.stil_line_end is not None else prev.stil_line_end,
            label_start=prev.label_start,
            label_end=span.label_end if span.label_end is not None else prev.label_end,
            pattern_name=prev.pattern_name or span.pattern_name,
            waveform_table=prev.waveform_table or span.waveform_table,
        )
    return merged


def load_optional_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    return read_json_artifact(path)


def _infer_timing_model_variant(compile_report: dict[str, Any] | None) -> tuple[str, str, dict[str, int]]:
    if not isinstance(compile_report, dict):
        return "full", "measured-safe", {}
    runtime_precheck = compile_report.get("capability_precheck", {}).get("runtime_precheck", {})
    if isinstance(runtime_precheck, dict) and bool(runtime_precheck.get("reduced_density_candidate")):
        return (
            "reduced",
            "measured-spacing-heuristic-phase",
            {
                "measured_output_half_cycle_ticks": 33,
                "measured_output_period_ticks": 66,
                "nominal_probe_half_cycle_ticks": 30,
                "nominal_probe_period_ticks": 60,
            },
        )
    return "full", "measured-safe", {}


def _required_ticks(*, sample_trigger: bool, op_count: int, dir_update: bool) -> int:
    del op_count
    del dir_update
    return 11 if sample_trigger else 10


def _slot_has_sample_trigger(slot: RateSlot) -> bool:
    return (slot.flags & STPG_RATE_SLOT_SAMPLE) != 0


def _merge_rate_groups(rate: RateDesc, slots: list[RateSlot]) -> tuple[MergedGroup, ...]:
    rate_slots = slots[rate.slot_offset : rate.slot_offset + rate.slot_count]
    groups: list[dict[str, Any]] = []
    index = 0
    while index < len(rate_slots):
        time_ticks = rate_slots[index].time_ticks
        sample_trigger = False
        op_count = 0
        dir_update = False
        while index < len(rate_slots) and rate_slots[index].time_ticks == time_ticks:
            slot = rate_slots[index]
            if _slot_has_sample_trigger(slot):
                sample_trigger = True
            if slot.drive_mask != 0:
                op_count += 1
            if (slot.flags & STPG_RATE_SLOT_DIR_UPDATE) != 0 and slot.dir_mask != 0:
                dir_update = True
            index += 1
        groups.append(
            {
                "time_ticks": time_ticks,
                "sample_trigger": sample_trigger,
                "op_count": op_count,
                "dir_update": dir_update,
            }
        )
    if not groups:
        return ()
    merged: list[MergedGroup] = []
    prev_time = groups[-1]["time_ticks"]
    for group_index, group in enumerate(groups):
        time_ticks = int(group["time_ticks"])
        if group_index == 0:
            gap_ticks = rate.period_ticks - int(prev_time) + time_ticks
        else:
            gap_ticks = time_ticks - int(prev_time)
        prev_time = time_ticks
        required_ticks = _required_ticks(
            sample_trigger=bool(group["sample_trigger"]),
            op_count=int(group["op_count"]),
            dir_update=bool(group["dir_update"]),
        )
        merged.append(
            MergedGroup(
                group_index=group_index,
                time_ticks=time_ticks,
                gap_ticks=gap_ticks,
                sample_trigger=bool(group["sample_trigger"]),
                op_count=int(group["op_count"]),
                dir_update=bool(group["dir_update"]),
                required_ticks=required_ticks,
                deficit_ticks=max(0, required_ticks - gap_ticks),
            )
        )
    return tuple(merged)


def _rate_score(groups: tuple[MergedGroup, ...]) -> int:
    score = 0
    for group in groups:
        deficit_weight = group.deficit_ticks * 16
        tight_gap_weight = 6 if group.gap_ticks <= group.required_ticks + 1 else 0
        sample_weight = 3 if group.sample_trigger else 0
        drive_weight = max(0, group.op_count - 1) * 2
        dir_weight = 2 if group.dir_update else 0
        cycle_wrap_weight = 4 if group.group_index == 0 else 0
        score += deficit_weight + tight_gap_weight + sample_weight + drive_weight + dir_weight + cycle_wrap_weight
    return score


def _build_rate_risks(rates: list[RateDesc], slots: list[RateSlot], vectors: list[VectorRecord | LoopRefRecord]) -> dict[int, RateRisk]:
    compare_rates = {vector.rate_id for vector in vectors if isinstance(vector, VectorRecord) and vector.expect_mask != 0}
    risks: dict[int, RateRisk] = {}
    for rate_id, rate in enumerate(rates):
        groups = _merge_rate_groups(rate, slots)
        risks[rate_id] = RateRisk(
            rate_id=rate_id,
            period_ticks=rate.period_ticks,
            merged_groups=groups,
            emitted_groups_per_vector=len(groups),
            requires_compare=rate_id in compare_rates,
            score=_rate_score(groups),
        )
    return risks


def _source_metadata(
    map_data: dict[str, Any] | None,
    record_index: int,
) -> dict[str, Any]:
    if map_data is None:
        return {}
    vectors = map_data.get("vectors", {})
    payload = vectors.get(str(record_index))
    if not payload:
        return {}
    source_map = payload.get("source_map", [])
    if not source_map:
        return {}
    return {
        "global_vector_start": source_map[0].get("global_vector_index"),
        "global_vector_end": source_map[-1].get("global_vector_index"),
        "stil_line_start": source_map[0].get("stil_line"),
        "stil_line_end": source_map[-1].get("stil_line"),
        "label_start": source_map[0].get("label"),
        "label_end": source_map[-1].get("label"),
        "pattern_name": source_map[0].get("pattern_name"),
        "waveform_table": source_map[0].get("waveform_table"),
    }


def _vector_base_score(rate_risk: RateRisk, vector: VectorRecord) -> int:
    compare_weight = 6 if vector.expect_mask != 0 else 0
    sample_weight = 3 if any(group.sample_trigger for group in rate_risk.merged_groups) else 0
    return rate_risk.score + compare_weight + sample_weight


def _build_vector_spans(
    vectors: list[VectorRecord | LoopRefRecord],
    loop_blocks: list[LoopBlock],
    rate_risks: dict[int, RateRisk],
    map_data: dict[str, Any] | None,
) -> tuple[list[VectorSpan], str]:
    spans: list[VectorSpan] = []
    expanded_cursor = 0
    confidence = "full"
    for record_index, entry in enumerate(vectors):
        if isinstance(entry, VectorRecord):
            rate_risk = rate_risks[entry.rate_id]
            metadata = _source_metadata(map_data, record_index)
            spans.append(
                VectorSpan(
                    source_kind="vector",
                    vector_record_index=record_index,
                    rate_id=entry.rate_id,
                    repeat=entry.repeat,
                    expanded_start=expanded_cursor,
                    expanded_end_exclusive=expanded_cursor + entry.repeat,
                    emitted_events=entry.repeat * rate_risk.emitted_groups_per_vector,
                    score=_vector_base_score(rate_risk, entry) * entry.repeat,
                    global_vector_start=metadata.get("global_vector_start"),
                    global_vector_end=metadata.get("global_vector_end"),
                    stil_line_start=metadata.get("stil_line_start"),
                    stil_line_end=metadata.get("stil_line_end"),
                    label_start=metadata.get("label_start"),
                    label_end=metadata.get("label_end"),
                    pattern_name=metadata.get("pattern_name"),
                    waveform_table=metadata.get("waveform_table"),
                )
            )
            expanded_cursor += entry.repeat
            continue
        confidence = "reduced"
        loop_block = loop_blocks[entry.loop_id]
        for body_index, body_record in enumerate(loop_block.body_records):
            rate_risk = rate_risks[body_record.rate_id]
            collapsed_repeat = loop_block.loop_count * body_record.repeat
            spans.append(
                VectorSpan(
                    source_kind=f"loop[{entry.loop_id}].body[{body_index}]",
                    vector_record_index=record_index,
                    rate_id=body_record.rate_id,
                    repeat=collapsed_repeat,
                    expanded_start=expanded_cursor,
                    expanded_end_exclusive=expanded_cursor + collapsed_repeat,
                    emitted_events=collapsed_repeat * rate_risk.emitted_groups_per_vector,
                    score=_vector_base_score(rate_risk, body_record) * collapsed_repeat,
                    global_vector_start=None,
                    global_vector_end=None,
                    stil_line_start=None,
                    stil_line_end=None,
                    label_start=None,
                    label_end=None,
                    pattern_name=None,
                    waveform_table=None,
                )
            )
            expanded_cursor += collapsed_repeat
    return spans, confidence


def _chunk_windows(
    spans: list[VectorSpan],
    rate_risks: dict[int, RateRisk],
    *,
    chunk_event_budget: int,
) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    chunk_id = 0
    remaining = chunk_event_budget
    window: dict[str, Any] = {
        "chunk_id": chunk_id,
        "event_start": 0,
        "event_end_exclusive": 0,
        "score": 0,
        "expanded_vector_start": None,
        "expanded_vector_end_exclusive": None,
        "global_vector_start": None,
        "global_vector_end": None,
        "dominant_rate_histogram": Counter(),
        "hot_span_count": 0,
    }
    event_cursor = 0
    for span in spans:
        rate_risk = rate_risks[span.rate_id]
        unit_events = max(1, rate_risk.emitted_groups_per_vector)
        consumed_vectors = 0
        while consumed_vectors < span.repeat:
            vectors_fit = max(1, remaining // unit_events)
            take_vectors = min(span.repeat - consumed_vectors, vectors_fit)
            take_events = take_vectors * unit_events
            take_score = (_vector_base_score(rate_risk, VectorRecord(span.rate_id, 0, 1, 0, 0, 0, 0)) * take_vectors)
            if window["expanded_vector_start"] is None:
                window["expanded_vector_start"] = span.expanded_start + consumed_vectors
            window["expanded_vector_end_exclusive"] = span.expanded_start + consumed_vectors + take_vectors
            if window["global_vector_start"] is None and span.global_vector_start is not None:
                window["global_vector_start"] = span.global_vector_start + consumed_vectors
            if span.global_vector_start is not None:
                window["global_vector_end"] = span.global_vector_start + consumed_vectors + take_vectors - 1
            window["event_end_exclusive"] = event_cursor + take_events
            window["score"] += take_score
            window["dominant_rate_histogram"][span.rate_id] += take_vectors
            if rate_risk.score > 0:
                window["hot_span_count"] += 1
            consumed_vectors += take_vectors
            remaining -= take_events
            event_cursor += take_events
            if remaining <= 0:
                histogram = window.pop("dominant_rate_histogram")
                dominant_rate_id, dominant_count = histogram.most_common(1)[0]
                window["dominant_rate_id"] = dominant_rate_id
                window["dominant_rate_vectors"] = dominant_count
                windows.append(window)
                chunk_id += 1
                remaining = chunk_event_budget
                window = {
                    "chunk_id": chunk_id,
                    "event_start": event_cursor,
                    "event_end_exclusive": event_cursor,
                    "score": 0,
                    "expanded_vector_start": None,
                    "expanded_vector_end_exclusive": None,
                    "global_vector_start": None,
                    "global_vector_end": None,
                    "dominant_rate_histogram": Counter(),
                    "hot_span_count": 0,
                }
    if window["expanded_vector_start"] is not None:
        histogram = window.pop("dominant_rate_histogram")
        if histogram:
            dominant_rate_id, dominant_count = histogram.most_common(1)[0]
            window["dominant_rate_id"] = dominant_rate_id
            window["dominant_rate_vectors"] = dominant_count
        else:
            window["dominant_rate_id"] = None
            window["dominant_rate_vectors"] = 0
        windows.append(window)
    return windows


def _generator_avoidance_candidates(
    spans: list[VectorSpan],
    rate_risks: dict[int, RateRisk],
    *,
    top_n: int,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for span in _merge_adjacent_rate_runs(spans):
        rate_risk = rate_risks[span.rate_id]
        if rate_risk.score <= 0:
            continue
        candidates.append(
            {
                "kind": "long_heavy_rate_run",
                "rate_id": span.rate_id,
                "score": span.score,
                "repeat": span.repeat,
                "expanded_vector_start": span.expanded_start,
                "expanded_vector_end_exclusive": span.expanded_end_exclusive,
                "global_vector_start": span.global_vector_start,
                "global_vector_end": span.global_vector_end,
                "stil_line_start": span.stil_line_start,
                "stil_line_end": span.stil_line_end,
                "pattern_name": span.pattern_name,
                "waveform_table": span.waveform_table,
                "message": (
                    f"rate[{span.rate_id}] repeats {span.repeat} vectors with score {rate_risk.score}; "
                    "split this run or move part of it behind a relaxed local window"
                ),
            }
        )
    candidates.sort(key=lambda item: (-int(item["score"]), -int(item["repeat"]), int(item["rate_id"])))
    return candidates[:top_n]


def _local_wait_candidates(
    spans: list[VectorSpan],
    rate_risks: dict[int, RateRisk],
    *,
    tick_hz: int,
    top_n: int | None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for span in _merge_adjacent_rate_runs(spans):
        rate_risk = rate_risks[span.rate_id]
        if rate_risk.score <= 0:
            continue
        for group in rate_risk.merged_groups:
            if group.deficit_ticks <= 0:
                continue
            candidates.append(
                {
                    "kind": "local_wait",
                    "rate_id": span.rate_id,
                    "group_index": group.group_index,
                    "minimum_extra_ticks": group.deficit_ticks,
                    "minimum_extra_ns": round(group.deficit_ticks * 1_000_000_000 / tick_hz, 3),
                "expanded_vector_start": span.expanded_start,
                "expanded_vector_end_exclusive": span.expanded_end_exclusive,
                "global_vector_start": span.global_vector_start,
                "global_vector_end": span.global_vector_end,
                "stil_line_start": span.stil_line_start,
                "stil_line_end": span.stil_line_end,
                "pattern_name": span.pattern_name,
                "waveform_table": span.waveform_table,
                "message": (
                    f"rate[{span.rate_id}] group[{group.group_index}] gap {group.gap_ticks} < required "
                    f"{group.required_ticks}; add at least {group.deficit_ticks} tick(s)"
                ),
            }
            )

    deduped: dict[tuple[int, int], dict[str, Any]] = {}
    for item in candidates:
        key = (int(item["rate_id"]), int(item["group_index"]))
        current = deduped.get(key)
        if current is None:
            deduped[key] = item
            continue
        current_span = int(current["expanded_vector_end_exclusive"]) - int(current["expanded_vector_start"])
        item_span = int(item["expanded_vector_end_exclusive"]) - int(item["expanded_vector_start"])
        if (
            item_span > current_span
            or (
                item_span == current_span
                and int(item["minimum_extra_ticks"]) > int(current["minimum_extra_ticks"])
            )
        ):
            deduped[key] = item

    reduced = list(deduped.values())
    reduced.sort(
        key=lambda item: (
            -(int(item["expanded_vector_end_exclusive"]) - int(item["expanded_vector_start"])),
            int(item["rate_id"]),
            int(item["group_index"]),
            int(item["expanded_vector_start"]),
            -int(item["minimum_extra_ticks"]),
        )
    )
    if top_n is None:
        return reduced
    return reduced[:top_n]


def _unwrap_runtime_report(runtime_report: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(runtime_report, dict):
        return None
    if "post_run" in runtime_report:
        return runtime_report
    runs = runtime_report.get("runs")
    if isinstance(runs, list):
        classified_runs = [item for item in runs if isinstance(item, dict) and "post_run" in item]
        if classified_runs:
            counts: dict[str, int] = {}
            for item in classified_runs:
                classification = str(item.get("classification", "unknown"))
                counts[classification] = counts.get(classification, 0) + 1
            majority = max(
                counts.items(),
                key=lambda pair: (pair[1], pair[0]),
            )[0]
            for item in classified_runs:
                if str(item.get("classification", "unknown")) == majority:
                    return item
            return classified_runs[0]
    return runtime_report


def _decode_runtime_page30(runtime_report: dict[str, Any] | None) -> dict[str, int] | None:
    runtime_report = _unwrap_runtime_report(runtime_report)
    if runtime_report is None:
        return None
    page30 = runtime_report.get("post_run", {}).get("page30")
    if not isinstance(page30, dict) or not page30.get("ok"):
        return None
    packed_index_period = int(page30.get("run_progress", 0))
    packed_prev_time = int(page30.get("sample_count", 0))
    return {
        "mismatch_stage": int(page30.get("pattern_size", 0)),
        "mismatch_rate_id": int(page30.get("event_count", 0)),
        "mismatch_index": packed_index_period & 0xFFFF,
        "mismatch_period_ticks": (packed_index_period >> 16) & 0xFFFF,
        "mismatch_desired_delta_ticks": int(page30.get("result_flags", 0)),
        "mismatch_required_ticks": int(page30.get("fail_count", 0)),
        "mismatch_prev_time_ticks": packed_prev_time & 0xFFFF,
        "mismatch_time_ticks": (packed_prev_time >> 16) & 0xFFFF,
    }


def _page30_local_wait_candidate(
    spans: list[VectorSpan],
    rate_risks: dict[int, RateRisk],
    *,
    tick_hz: int,
    runtime_report: dict[str, Any] | None,
) -> dict[str, Any] | None:
    decoded = _decode_runtime_page30(runtime_report)
    if decoded is None:
        return None
    mismatch_stage = int(decoded["mismatch_stage"])
    rate_id = int(decoded["mismatch_rate_id"])
    group_index = int(decoded["mismatch_index"])
    desired = int(decoded["mismatch_desired_delta_ticks"])
    required = int(decoded["mismatch_required_ticks"])
    if mismatch_stage == 0 or required <= desired:
        return None
    rate = rate_risks.get(rate_id)
    if rate is None or group_index < 0 or group_index >= len(rate.merged_groups):
        return None
    matching_spans = [
        span for span in spans
        if span.rate_id == rate_id
        and span.global_vector_start is not None
        and span.global_vector_end is not None
    ]
    if not matching_spans:
        return None
    best_span = max(
        matching_spans,
        key=lambda span: (
            span.score,
            span.expanded_end_exclusive - span.expanded_start,
            -(span.global_vector_start or 0),
        ),
    )
    minimum_extra_ticks = required - desired
    minimum_extra_ns = round((minimum_extra_ticks * 1_000_000_000) / tick_hz, 3)
    return {
        "kind": "local_wait",
        "candidate_source": "runtime_page30",
        "rate_id": rate_id,
        "group_index": group_index,
        "minimum_extra_ticks": minimum_extra_ticks,
        "minimum_extra_ns": minimum_extra_ns,
        "expanded_vector_start": best_span.expanded_start,
        "expanded_vector_end_exclusive": best_span.expanded_end_exclusive,
        "global_vector_start": best_span.global_vector_start,
        "global_vector_end": best_span.global_vector_end,
        "stil_line_start": best_span.stil_line_start,
        "stil_line_end": best_span.stil_line_end,
        "pattern_name": best_span.pattern_name,
        "waveform_table": best_span.waveform_table,
        "message": (
            f"rate[{rate_id}] group[{group_index}] gap {desired} < required {required}; "
            f"add at least {minimum_extra_ticks} tick(s)"
        ),
    }


def analyze_rate_vector_underflow(
    pattern_bytes: bytes,
    *,
    map_data: dict[str, Any] | None = None,
    compile_report: dict[str, Any] | None = None,
    runtime_report: dict[str, Any] | None = None,
    chunk_event_budget: int = DEFAULT_CHUNK_EVENT_BUDGET,
    top_n: int = 10,
) -> dict[str, Any]:
    magic = int.from_bytes(pattern_bytes[:4], "little")
    if magic != STPG_RATEVEC_MAGIC:
        raise ValueError("underflow analyzer currently supports rate-vector patterns only")
    header, rates, slots, vectors, loop_blocks = unpack_rate_vector_full(pattern_bytes)
    rate_risks = _build_rate_risks(rates, slots, vectors)
    spans, confidence = _build_vector_spans(vectors, loop_blocks, rate_risks, map_data)
    merged_spans = _merge_adjacent_rate_runs(spans)
    windows = _chunk_windows(spans, rate_risks, chunk_event_budget=chunk_event_budget)
    top_rates = sorted(rate_risks.values(), key=lambda item: (-item.score, item.rate_id))
    top_spans = sorted(merged_spans, key=lambda item: (-item.score, item.expanded_start))
    top_windows = sorted(windows, key=lambda item: (-int(item["score"]), int(item["chunk_id"])))
    page30_candidate = _page30_local_wait_candidate(
        merged_spans,
        rate_risks,
        tick_hz=header.tick_hz,
        runtime_report=runtime_report,
    )
    timing_model_variant, model_confidence, timing_model_notes = _infer_timing_model_variant(compile_report)
    all_local_wait_candidates = _local_wait_candidates(spans, rate_risks, tick_hz=header.tick_hz, top_n=None)
    local_wait_candidates = all_local_wait_candidates[:top_n]
    if page30_candidate is not None:
        all_filtered = [
            item for item in all_local_wait_candidates
            if not (
                int(item["rate_id"]) == int(page30_candidate["rate_id"])
                and int(item["group_index"]) == int(page30_candidate["group_index"])
                and int(item["minimum_extra_ticks"]) == int(page30_candidate["minimum_extra_ticks"])
                and int(item["expanded_vector_start"]) == int(page30_candidate["expanded_vector_start"])
                and int(item["expanded_vector_end_exclusive"]) == int(page30_candidate["expanded_vector_end_exclusive"])
            )
        ]
        all_local_wait_candidates = [page30_candidate, *all_filtered]
        filtered = [
            item for item in local_wait_candidates
            if not (
                int(item["rate_id"]) == int(page30_candidate["rate_id"])
                and int(item["group_index"]) == int(page30_candidate["group_index"])
                and int(item["minimum_extra_ticks"]) == int(page30_candidate["minimum_extra_ticks"])
                and int(item["expanded_vector_start"]) == int(page30_candidate["expanded_vector_start"])
                and int(item["expanded_vector_end_exclusive"]) == int(page30_candidate["expanded_vector_end_exclusive"])
            )
        ]
        local_wait_candidates = [page30_candidate, *filtered]
    all_rates = [
        {
            "rate_id": rate.rate_id,
            "period_ticks": rate.period_ticks,
            "emitted_groups_per_vector": rate.emitted_groups_per_vector,
            "requires_compare": rate.requires_compare,
            "score": rate.score,
            "groups": [
                {
                    "group_index": group.group_index,
                    "time_ticks": group.time_ticks,
                    "gap_ticks": group.gap_ticks,
                    "sample_trigger": group.sample_trigger,
                    "op_count": group.op_count,
                    "dir_update": group.dir_update,
                    "required_ticks": group.required_ticks,
                    "deficit_ticks": group.deficit_ticks,
                }
                for group in rate.merged_groups
            ],
        }
        for rate in top_rates
    ]
    result = {
        "input": {
            "mode": "rate-vector",
            "tick_hz": header.tick_hz,
            "active_pins": header.active_pins,
            "pattern_size_bytes": len(pattern_bytes),
            "vector_record_count": header.vector_count,
            "rate_count": header.rate_count,
            "slot_count": header.slot_count,
            "loop_block_count": len(loop_blocks),
            "chunk_event_budget": chunk_event_budget,
        },
        "summary": {
            "analyzer_version": ANALYZER_VERSION,
            "confidence": confidence,
            "timing_model_variant": timing_model_variant,
            "model_confidence": model_confidence,
            "timing_model_notes": timing_model_notes,
            "streaming_poc_compatibility": streaming_poc_timing_incompatibility(rates, slots) is None,
            "streaming_poc_incompatibility": streaming_poc_timing_incompatibility(rates, slots),
            "total_emitted_events_estimate": emitted_event_count_estimate(rates, slots, vectors, loop_blocks),
            "total_expanded_vectors_estimate": spans[-1].expanded_end_exclusive if spans else 0,
            "nonzero_deficit_rate_count": sum(1 for rate in rate_risks.values() if any(group.deficit_ticks > 0 for group in rate.merged_groups)),
            "top_rate_score": top_rates[0].score if top_rates else 0,
            "top_span_score": top_spans[0].score if top_spans else 0,
            "top_chunk_score": int(top_windows[0]["score"]) if top_windows else 0,
        },
        "rates": all_rates[:top_n],
        "all_rates": all_rates,
        "vector_spans": [
            {
                "source_kind": span.source_kind,
                "vector_record_index": span.vector_record_index,
                "rate_id": span.rate_id,
                "repeat": span.repeat,
                "score": span.score,
                "expanded_vector_start": span.expanded_start,
                "expanded_vector_end_exclusive": span.expanded_end_exclusive,
                "emitted_events": span.emitted_events,
                "global_vector_start": span.global_vector_start,
                "global_vector_end": span.global_vector_end,
                "stil_line_start": span.stil_line_start,
                "stil_line_end": span.stil_line_end,
                "label_start": span.label_start,
                "label_end": span.label_end,
                "pattern_name": span.pattern_name,
                "waveform_table": span.waveform_table,
            }
            for span in top_spans[:top_n]
        ],
        "chunk_windows": top_windows[:top_n],
        "generator_avoidance_candidates": _generator_avoidance_candidates(spans, rate_risks, top_n=top_n),
        "local_wait_candidates": local_wait_candidates[:top_n],
        "all_local_wait_candidates": all_local_wait_candidates,
        "page30_local_wait_candidate": page30_candidate,
        "assumptions": [
            "rate-vector only",
            "chunk windows are approximate and event-budget based",
            "loop blocks are reduced-confidence and collapse body repeats",
            "analysis is proposal-only and does not modify STIL or binary",
            "required_ticks for the relevant batch wait path follows firmware base overhead only: 10(no-sample) / 11(sample)",
            "timing_model_variant=full uses the measured streaming-poc base heuristic",
            "timing_model_variant=reduced uses measured external spacing from R16_MID200/R16_EARLY200/R16_LATE200 scope data",
            "timing_model_variant=reduced still treats internal sample-phase placement as heuristic",
        ],
    }
    if compile_report is not None:
        result["compile_report_runtime_precheck"] = compile_report.get("capability_precheck", {}).get("runtime_precheck", {})
    return result


def build_practical_runtime_hints(report: dict[str, Any], *, top_chunk_n: int = 2, top_candidate_n: int = 3) -> dict[str, Any]:
    rate_groups = {
        int(item["rate_id"]): int(item["emitted_groups_per_vector"])
        for item in report.get("all_rates", report.get("rates", []))
    }
    chunk_budget = int(report.get("input", {}).get("chunk_event_budget", DEFAULT_CHUNK_EVENT_BUDGET))
    windows = report.get("chunk_windows", [])[:top_chunk_n]
    candidates = report.get("generator_avoidance_candidates", [])[:top_candidate_n]
    all_waits = list(report.get("all_local_wait_candidates", report.get("local_wait_candidates", [])))
    waits = all_waits[:top_candidate_n]
    practical_chunk_warnings: list[dict[str, Any]] = []
    practical_run_warnings: list[dict[str, Any]] = []
    practical_hard_errors: list[dict[str, Any]] = []

    for window in windows:
        global_start = window.get("global_vector_start")
        if window.get("chunk_id", 0) <= 0 or global_start is None:
            continue
        dominant_rate_id = int(window.get("dominant_rate_id", -1))
        covering = next(
            (
                item for item in candidates
                if int(item.get("rate_id", -1)) == dominant_rate_id
                and item.get("global_vector_start") is not None
                and item.get("global_vector_end") is not None
                and int(item["global_vector_start"]) <= int(global_start) <= int(item["global_vector_end"])
            ),
            None,
        )
        if covering is None:
            continue
        practical_chunk_warnings.append(
            {
                "code": "WARN_PRACTICAL_PREDICTED_CHUNK_BOUNDARY_CROSSING",
                "message": (
                    f"predicted chunk[{window['chunk_id']}] starts inside long exact compare-aware run "
                    f"rate[{dominant_rate_id}] at global vector {global_start}"
                ),
                "line": covering.get("stil_line_start"),
                "column": 1 if covering.get("stil_line_start") is not None else None,
                "context": {
                    "chunk_id": int(window["chunk_id"]),
                    "chunk_global_vector_start": int(window["global_vector_start"]),
                    "chunk_global_vector_end": int(window["global_vector_end"]) if window.get("global_vector_end") is not None else None,
                    "chunk_event_start": int(window["event_start"]),
                    "chunk_event_end_exclusive": int(window["event_end_exclusive"]),
                    "dominant_rate_id": dominant_rate_id,
                    "dominant_rate_vectors": int(window.get("dominant_rate_vectors", 0)),
                    "covering_run_global_vector_start": int(covering["global_vector_start"]),
                    "covering_run_global_vector_end": int(covering["global_vector_end"]),
                    "covering_run_repeat": int(covering["repeat"]),
                },
            }
        )

    for candidate in candidates:
        rate_id = int(candidate["rate_id"])
        groups_per_vector = rate_groups.get(rate_id, 1)
        vectors_per_chunk = max(1, chunk_budget // max(1, groups_per_vector))
        if int(candidate["repeat"]) < vectors_per_chunk:
            continue
        practical_run_warnings.append(
            {
                "code": "WARN_PRACTICAL_LONG_HOMOGENEOUS_COMPARE_RUN",
                "message": (
                    f"rate[{rate_id}] run spans {candidate['repeat']} vectors, exceeding predicted chunk window "
                    f"capacity {vectors_per_chunk} vectors"
                ),
                "line": candidate.get("stil_line_start"),
                "column": 1 if candidate.get("stil_line_start") is not None else None,
                "context": {
                    "rate_id": rate_id,
                    "repeat": int(candidate["repeat"]),
                    "predicted_chunk_vector_capacity": vectors_per_chunk,
                    "global_vector_start": candidate.get("global_vector_start"),
                    "global_vector_end": candidate.get("global_vector_end"),
                    "score": int(candidate["score"]),
                },
            }
        )

    wait_by_rate_group = {
        (int(item["rate_id"]), int(item["group_index"])): item
        for item in all_waits
    }
    for rate in report.get("all_rates", report.get("rates", [])):
        if not bool(rate.get("requires_compare")):
            continue
        if int(report.get("input", {}).get("active_pins", 0)) != 24:
            continue
        groups_per_vector = rate_groups.get(int(rate["rate_id"]), 1)
        vectors_per_chunk = max(1, chunk_budget // max(1, groups_per_vector))
        groups = rate.get("groups", [])
        if int(rate.get("emitted_groups_per_vector", 0)) != 2 or len(groups) != 2:
            continue
        first_group = groups[0]
        second_group = groups[1]
        if int(first_group.get("time_ticks", -1)) != 0:
            continue
        if bool(first_group.get("sample_trigger")):
            continue
        if not bool(second_group.get("sample_trigger")):
            continue
        for group in groups:
            deficit = int(group.get("deficit_ticks", 0))
            if deficit <= 0:
                continue
            candidate = wait_by_rate_group.get((int(rate["rate_id"]), int(group["group_index"])))
            if candidate is None:
                continue
            span_vectors = int(candidate["expanded_vector_end_exclusive"]) - int(candidate["expanded_vector_start"])
            if span_vectors < vectors_per_chunk:
                continue
            context: dict[str, Any] = {
                "rate_id": int(rate["rate_id"]),
                "group_index": int(group["group_index"]),
                "gap_ticks": int(group["gap_ticks"]),
                "required_ticks": int(group["required_ticks"]),
                "deficit_ticks": deficit,
                "shape": "compare24_practical_two_group",
                "predicted_chunk_vector_capacity": vectors_per_chunk,
                "span_vectors": span_vectors,
            }
            context.update(
                {
                    "global_vector_start": candidate.get("global_vector_start"),
                    "global_vector_end": candidate.get("global_vector_end"),
                    "stil_line_start": candidate.get("stil_line_start"),
                    "stil_line_end": candidate.get("stil_line_end"),
                }
            )
            practical_hard_errors.append(
                {
                    "code": "ERR_PRACTICAL_COMPARE24_TIMING",
                    "message": (
                        f"rate[{rate['rate_id']}] group[{group['group_index']}] gap "
                        f"{group['gap_ticks']} < required {group['required_ticks']} in compare24 practical "
                        f"two-group path; add at least {deficit} tick(s)"
                    ),
                    "line": None if candidate is None else candidate.get("stil_line_start"),
                    "column": 1 if candidate is not None and candidate.get("stil_line_start") is not None else None,
                    "context": context,
                }
            )

    if practical_hard_errors:
        preferred_waits: list[dict[str, Any]] = []
        preferred_keys: set[tuple[int, int, int | None, int | None]] = set()
        for error in practical_hard_errors:
            context = error.get("context", {})
            key = (
                int(context.get("rate_id", -1)),
                int(context.get("group_index", -1)),
                None if context.get("global_vector_start") is None else int(context["global_vector_start"]),
                None if context.get("global_vector_end") is None else int(context["global_vector_end"]),
            )
            for wait in all_waits:
                wait_key = (
                    int(wait.get("rate_id", -1)),
                    int(wait.get("group_index", -1)),
                    None if wait.get("global_vector_start") is None else int(wait["global_vector_start"]),
                    None if wait.get("global_vector_end") is None else int(wait["global_vector_end"]),
                )
                if wait_key != key or wait_key in preferred_keys:
                    continue
                preferred_waits.append(wait)
                preferred_keys.add(wait_key)
                break
        if preferred_waits:
            remaining_waits = []
            for wait in all_waits:
                wait_key = (
                    int(wait.get("rate_id", -1)),
                    int(wait.get("group_index", -1)),
                    None if wait.get("global_vector_start") is None else int(wait["global_vector_start"]),
                    None if wait.get("global_vector_end") is None else int(wait["global_vector_end"]),
                )
                if wait_key in preferred_keys:
                    continue
                remaining_waits.append(wait)
            all_waits = [*preferred_waits, *remaining_waits]
            waits = all_waits[:top_candidate_n]

    return {
        "top_chunk_windows": windows,
        "generator_avoidance_candidates": candidates,
        "local_wait_candidates": waits,
        "practical_chunk_warnings": practical_chunk_warnings,
        "practical_run_warnings": practical_run_warnings,
        "practical_hard_errors": practical_hard_errors,
    }


def _fmt_optional_range(start: int | None, end: int | None) -> str:
    if start is None and end is None:
        return "-"
    if start == end:
        return str(start)
    if start is None:
        return f"?:{end}"
    if end is None:
        return f"{start}:?"
    return f"{start}:{end}"


def render_underflow_analysis_text(report: dict[str, Any]) -> str:
    lines: list[str] = []
    summary = report["summary"]
    info = report["input"]
    lines.append("Summary")
    lines.append(
        "  "
        f"mode={info['mode']} tick_hz={info['tick_hz']} active_pins={info['active_pins']} "
        f"pattern_size={info['pattern_size_bytes']} chunk_event_budget={info['chunk_event_budget']}"
    )
    lines.append(
        "  "
        f"confidence={summary['confidence']} emitted_events={summary['total_emitted_events_estimate']} "
        f"expanded_vectors={summary['total_expanded_vectors_estimate']} "
        f"streaming_poc_compatible={'yes' if summary['streaming_poc_compatibility'] else 'no'}"
    )
    lines.append(
        "  "
        f"timing_model_variant={summary['timing_model_variant']} "
        f"model_confidence={summary['model_confidence']}"
    )
    timing_model_notes = summary.get("timing_model_notes", {})
    if timing_model_notes:
        rendered = " ".join(f"{key}={value}" for key, value in timing_model_notes.items())
        lines.append(f"  timing_model_notes={rendered}")
    if summary["streaming_poc_incompatibility"]:
        lines.append(f"  streaming_poc_reason={summary['streaming_poc_incompatibility']}")
    lines.append("")
    lines.append("Top Risky Rates")
    for rate in report["rates"]:
        lines.append(
            f"  rate[{rate['rate_id']}] score={rate['score']} period_ticks={rate['period_ticks']} "
            f"groups={rate['emitted_groups_per_vector']} compare={'yes' if rate['requires_compare'] else 'no'}"
        )
        for group in rate["groups"]:
            lines.append(
                "    "
                f"group[{group['group_index']}] t={group['time_ticks']} gap={group['gap_ticks']} "
                f"required={group['required_ticks']} deficit={group['deficit_ticks']} "
                f"sample={'yes' if group['sample_trigger'] else 'no'} ops={group['op_count']} "
                f"dir_update={'yes' if group['dir_update'] else 'no'}"
            )
    lines.append("")
    lines.append("Top Risky Vector Spans")
    for span in report["vector_spans"]:
        lines.append(
            "  "
            f"rate[{span['rate_id']}] score={span['score']} repeat={span['repeat']} "
            f"expanded={_fmt_optional_range(span['expanded_vector_start'], span['expanded_vector_end_exclusive'] - 1)} "
            f"global={_fmt_optional_range(span['global_vector_start'], span['global_vector_end'])} "
            f"stil_line={_fmt_optional_range(span['stil_line_start'], span['stil_line_end'])} "
            f"source={span['source_kind']}"
        )
    lines.append("")
    lines.append("Top Risky Chunk Windows")
    for window in report["chunk_windows"]:
        lines.append(
            "  "
            f"chunk[{window['chunk_id']}] score={window['score']} "
            f"events={_fmt_optional_range(window['event_start'], window['event_end_exclusive'] - 1)} "
            f"expanded={_fmt_optional_range(window['expanded_vector_start'], None if window['expanded_vector_end_exclusive'] is None else window['expanded_vector_end_exclusive'] - 1)} "
            f"global={_fmt_optional_range(window['global_vector_start'], window['global_vector_end'])} "
            f"dominant_rate={window['dominant_rate_id']} dominant_vectors={window['dominant_rate_vectors']}"
        )
    lines.append("")
    lines.append("Generator Avoidance Candidates")
    for item in report["generator_avoidance_candidates"]:
        lines.append(
            "  "
            f"rate[{item['rate_id']}] repeat={item['repeat']} score={item['score']} "
            f"global={_fmt_optional_range(item['global_vector_start'], item['global_vector_end'])} "
            f"{item['message']}"
        )
    lines.append("")
    lines.append("Local Wait Candidates")
    for item in report["local_wait_candidates"]:
        lines.append(
            "  "
            f"rate[{item['rate_id']}] group[{item['group_index']}] extra>={item['minimum_extra_ticks']} tick "
            f"({item['minimum_extra_ns']}ns) global={_fmt_optional_range(item['global_vector_start'], item['global_vector_end'])} "
            f"{item['message']}"
        )
    return "\n".join(lines)
