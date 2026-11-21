"""Common source-map lookup helpers for compressed and expanded entries."""

from __future__ import annotations

from copy import deepcopy
from types import MappingProxyType
from typing import Any, Mapping, cast

COMPRESSED_LOOP_KINDS = {"loop_vector_repeat_v1", "loop_vector_repeat_v2"}


def validate_source_map_compression(payload: dict[str, Any], *, repeat: int | None = None) -> str | None:
    """Validate bounded compression metadata before using computed provenance."""
    compression = payload.get("source_map_compression")
    if compression is None:
        return None
    if (not isinstance(compression, dict) or not isinstance(compression.get("kind"), str)
            or compression["kind"] not in COMPRESSED_LOOP_KINDS):
        raise ValueError("unknown source_map_compression kind")
    kind = compression["kind"]
    if kind == "loop_vector_repeat_v2":
        for key in ("global_vector_base", "loop_id", "loop_iteration_base", "loop_body_vector_offset"):
            if type(compression.get(key)) is not int or compression[key] < 0:
                raise ValueError(f"invalid compressed loop {key}")
        count = compression.get("loop_body_vector_count")
        if type(count) is not int or count < 2 or compression["loop_body_vector_offset"] >= count:
            raise ValueError("invalid compressed loop body count/offset")
        if type(compression.get("vector_stride")) is not int or compression["vector_stride"] != 1:
            raise ValueError("invalid compressed loop vector stride")
        body = payload.get("body_source_map")
        entries = payload.get("source_map")
        if (not isinstance(body, list) or len(body) != count or any(not isinstance(i, dict) for i in body)
                or not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict)):
            raise ValueError("invalid compressed loop source/body map")
        if type(payload.get("repeat")) is not int or not 1 <= payload["repeat"] <= 0xFFFF:
            raise ValueError("invalid compressed loop repeat")
        if repeat is not None and payload["repeat"] != repeat:
            raise ValueError("compressed loop repeat differs from binary")
    return kind


def _source_entries(payload: dict[str, Any], *, loop: bool) -> list[Any]:
    value = payload.get("body_source_map" if loop else "source_map")
    if not isinstance(value, list) or not value:
        fallback = payload.get("source_map")
        value = fallback if isinstance(fallback, list) else []
    return value


def source_map_entry_for_occurrence(
    payload: dict[str, Any],
    *,
    repeat_index: int = 0,
    loop_iteration: int | None = None,
    body_record_index: int | None = None,
    body_repeat_index: int | None = None,
    logical_vector_index: int | None = None,
) -> dict[str, Any]:
    """Resolve one runtime occurrence without requiring a full expanded map."""

    validate_source_map_compression(payload)
    return _source_map_entry_for_occurrence(
        payload, repeat_index=repeat_index, loop_iteration=loop_iteration,
        body_record_index=body_record_index, body_repeat_index=body_repeat_index,
        logical_vector_index=logical_vector_index,
    )


def _source_map_entry_for_occurrence(
    payload: dict[str, Any],
    *,
    repeat_index: int = 0,
    loop_iteration: int | None = None,
    body_record_index: int | None = None,
    body_repeat_index: int | None = None,
    logical_vector_index: int | None = None,
    _copy_nested: bool = True,
) -> dict[str, Any]:
    # Read-only consumers inspect scalar provenance; do not duplicate signals,
    # expansion traces or body maps for every emitted runtime group. Always
    # copy the outer entry before adding computed occurrence indices.
    copy_entry = deepcopy if _copy_nested else dict
    is_loop = loop_iteration is not None or body_record_index is not None
    entries = _source_entries(payload, loop=is_loop)
    if not entries:
        return {}
    selected: dict[str, Any] | None = None
    if is_loop:
        loop_body_vector_count = int(payload.get("body_logical_vector_count", 0) or 0)
        if loop_body_vector_count <= 0:
            loop_body_vector_count = sum(
                int(item.get("body_repeat_count", 1))
                for item in entries
                if isinstance(item, dict)
            )
        selected = next(
            (
                item
                for item in entries
                if isinstance(item, dict) and item.get("body_record_index") == body_record_index
            ),
            None,
        )
        if selected is None and isinstance(entries[0], dict):
            selected = entries[0]
        if selected is None:
            return {}
        body_repeat = int(body_repeat_index or 0)
        detailed = selected.get("body_source_entries")
        if isinstance(detailed, list) and 0 <= body_repeat < len(detailed):
            if isinstance(detailed[body_repeat], dict):
                selected = detailed[body_repeat]
        elif body_repeat:
            selected = {
                **selected,
                "global_vector_index": int(selected.get("global_vector_index", 0)) + body_repeat,
            }
        result = copy_entry(selected)
        if loop_iteration is not None:
            source_iteration = int(payload.get("iteration_base", 0) or 0) + int(loop_iteration)
            result["loop_iteration"] = source_iteration
            result["loop_iteration_index"] = source_iteration
            result["global_vector_index"] = (
                int(result.get("global_vector_index", 0))
                + int(loop_iteration) * loop_body_vector_count
            )
            result["expanded_logical_vector_index"] = int(result.get("global_vector_index", 0))
        if body_record_index is not None:
            result["body_record_index"] = int(body_record_index)
            result["loop_body_record_index"] = int(body_record_index)
        if body_repeat_index is not None:
            result["body_repeat_index"] = int(body_repeat_index)
            result["loop_body_repeat_index"] = int(body_repeat_index)
        if logical_vector_index is not None:
            result["expanded_logical_vector_index"] = int(logical_vector_index)
        return result

    compression = payload.get("source_map_compression")
    if isinstance(compression, dict) and compression.get("kind") in COMPRESSED_LOOP_KINDS:
        v2 = compression["kind"] == "loop_vector_repeat_v2"
        offset = int(compression.get("loop_body_vector_offset", 0)) + repeat_index
        body_count = int(compression.get("loop_body_vector_count", 1)) if v2 else 1
        selected = payload["body_source_map"][offset % body_count] if v2 else entries[0]
        selected = selected if isinstance(selected, dict) else None
        if selected is None:
            return {}
        stride = int(compression.get("vector_stride", 1))
        result = copy_entry(selected)
        result["global_vector_index"] = int(compression["global_vector_base"]) + repeat_index * stride
        if "loop_id" in compression:
            result["loop_id"] = int(compression["loop_id"])
        if "loop_iteration_base" in compression:
            result["loop_iteration"] = int(compression["loop_iteration_base"]) + (offset // body_count if v2 else repeat_index)
            result["loop_iteration_index"] = result["loop_iteration"]
        if v2:
            result["loop_body_index"] = offset % body_count
            result["expanded_logical_vector_index"] = result["global_vector_index"]
            result["top_level_repeat_index"] = int(repeat_index)
        result["repeat_iteration"] = int(repeat_index)
        if logical_vector_index is not None:
            result["expanded_logical_vector_index"] = int(logical_vector_index)
        return result

    selected = next(
        (
            item
            for item in entries
            if isinstance(item, dict) and item.get("repeat_iteration") == repeat_index
        ),
        entries[0],
    )
    return copy_entry(selected) if isinstance(selected, dict) else {}


class _PayloadSourceIndex:
    """Indexes one source-map payload and narrows lookup to one entry."""

    def __init__(self, payload: dict[str, Any], *, loop: bool) -> None:
        validate_source_map_compression(payload)
        self.payload = payload
        self.loop = loop
        self.entries = _source_entries(payload, loop=loop)
        self.loop_body_vector_count = 0
        if loop:
            self.loop_body_vector_count = int(payload.get("body_logical_vector_count", 0) or 0)
            if self.loop_body_vector_count <= 0:
                self.loop_body_vector_count = sum(
                    int(item.get("body_repeat_count", 1))
                    for item in self.entries
                    if isinstance(item, dict)
                )
        self.by_occurrence: dict[Any, dict[str, Any]] = {}
        occurrence_key = "body_record_index" if loop else "repeat_iteration"
        for item in self.entries:
            if isinstance(item, dict):
                try:
                    self.by_occurrence.setdefault(item.get(occurrence_key), item)
                except TypeError:
                    # Unhashable metadata cannot match the integer runtime index.
                    continue

    def narrowed_payload(
        self,
        *,
        repeat_index: int,
        body_record_index: int | None,
    ) -> dict[str, Any]:
        entries = self.entries
        if self.loop:
            selected = self.by_occurrence.get(body_record_index)
        elif (
            isinstance(self.payload.get("source_map_compression"), dict)
            and self.payload["source_map_compression"].get("kind") in COMPRESSED_LOOP_KINDS
        ):
            selected = entries[0] if entries else None
        else:
            selected = self.by_occurrence.get(repeat_index)
        if selected is None and entries:
            selected = entries[0]

        narrowed = dict(self.payload)
        if self.loop:
            # The legacy fallback derives the iteration stride from every
            # body entry, not just the selected entry in this narrowed view.
            narrowed["body_logical_vector_count"] = self.loop_body_vector_count
        narrowed["body_source_map" if self.loop else "source_map"] = [selected] if entries else []
        return narrowed


class SourceMapOccurrenceResolver:
    """Scope-local indexed resolver for the ordered events of one pattern.

    Runtime groups belonging to the same vector request identical source
    metadata. This keeps only the latest payload index and occurrence, avoiding
    repeated linear scans/deepcopies without retaining one cache entry per
    vector. The map input is expected to remain unchanged for this resolver's
    lifetime; returned values are safe to treat as immutable.
    """

    def __init__(self, map_data: dict[str, Any] | None) -> None:
        vectors = map_data.get("vectors") if isinstance(map_data, dict) else None
        self._vectors = vectors if isinstance(vectors, dict) else None
        self._last_source_record_index: int | None = None
        self._last_payload: dict[str, Any] | None = None
        self._last_loop_mode: bool | None = None
        self._last_payload_index: _PayloadSourceIndex | None = None
        self._last_occurrence_key: tuple[Any, ...] | None = None
        self._last_result: Mapping[str, Any] = {}

    def resolve(
        self,
        source_record_index: int,
        *,
        repeat_index: int = 0,
        loop_iteration: int | None = None,
        body_record_index: int | None = None,
        body_repeat_index: int | None = None,
        logical_vector_index: int | None = None,
    ) -> dict[str, Any]:
        return cast(dict[str, Any], self._resolve(
            source_record_index, repeat_index=repeat_index, loop_iteration=loop_iteration,
            body_record_index=body_record_index, body_repeat_index=body_repeat_index,
            logical_vector_index=logical_vector_index,
        ))

    def resolve_readonly(self, source_record_index: int, **occurrence: Any) -> Mapping[str, Any]:
        """Read scalar provenance without deep-copying unused nested metadata.

        The outer mapping is read-only. Nested containers share the input's
        storage and are NOT recursively frozen. Do not append, remove or
        assign anything inside this result, including nested lists/dicts.
        This API is for scalar reads only; use ``resolve`` or
        ``source_map_entry_for_occurrence`` for owned nested data. Consecutive
        identical ``resolve`` calls may reuse the same owned result too, so
        copy that result yourself if it must be mutated and retained.
        """

        return self._resolve(source_record_index, _copy_nested=False, **occurrence)

    def _resolve(
        self,
        source_record_index: int,
        *,
        repeat_index: int = 0,
        loop_iteration: int | None = None,
        body_record_index: int | None = None,
        body_repeat_index: int | None = None,
        logical_vector_index: int | None = None,
        _copy_nested: bool = True,
    ) -> Mapping[str, Any]:
        if self._vectors is None:
            return {}
        payload = self._vectors.get(str(source_record_index))
        if not isinstance(payload, dict):
            return {}
        is_loop = loop_iteration is not None or body_record_index is not None
        occurrence_key = (
            int(source_record_index),
            int(repeat_index),
            loop_iteration,
            body_record_index,
            body_repeat_index,
            logical_vector_index,
            _copy_nested,
        )
        if (
            source_record_index == self._last_source_record_index
            and payload is self._last_payload
            and occurrence_key == self._last_occurrence_key
        ):
            return self._last_result
        if (
            source_record_index != self._last_source_record_index
            or payload is not self._last_payload
            or is_loop != self._last_loop_mode
        ):
            self._last_source_record_index = source_record_index
            self._last_payload = payload
            self._last_loop_mode = is_loop
            self._last_payload_index = _PayloadSourceIndex(payload, loop=is_loop)
        assert self._last_payload_index is not None
        narrowed = self._last_payload_index.narrowed_payload(
            repeat_index=repeat_index,
            body_record_index=body_record_index,
        )
        result = _source_map_entry_for_occurrence(
            narrowed,
            repeat_index=repeat_index,
            loop_iteration=loop_iteration,
            body_record_index=body_record_index,
            body_repeat_index=body_repeat_index,
            logical_vector_index=logical_vector_index,
            _copy_nested=_copy_nested,
        )
        self._last_occurrence_key = occurrence_key
        self._last_result = result if _copy_nested else MappingProxyType(result)
        return self._last_result


def source_map_slice_for_repeats(
    payload: dict[str, Any],
    *,
    start: int,
    end: int,
) -> list[dict[str, Any]]:
    """Materialize only the requested direct-repeat map slice."""

    if start < 0 or end < start:
        return []
    index = _PayloadSourceIndex(payload, loop=False)
    return [
        _source_map_entry_for_occurrence(
            index.narrowed_payload(repeat_index=repeat, body_record_index=None),
            repeat_index=repeat,
        )
        for repeat in range(start, end + 1)
    ]
