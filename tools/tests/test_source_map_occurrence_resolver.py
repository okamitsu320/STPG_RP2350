from __future__ import annotations

import pytest

from stil.map_lookup import SourceMapOccurrenceResolver, source_map_entry_for_occurrence


def _assert_resolver_matches(payload, **occurrence):
    resolver = SourceMapOccurrenceResolver({"vectors": {"7": payload}})
    expected = source_map_entry_for_occurrence(payload, **occurrence)
    assert resolver.resolve(7, **occurrence) == expected
    assert resolver.resolve_readonly(7, **occurrence) == expected
    # Repeated runtime groups for one logical occurrence reuse the same result.
    assert resolver.resolve(7, **occurrence) is resolver.resolve(7, **occurrence)


def test_readonly_lookup_shares_unused_nested_metadata_but_owned_lookup_does_not():
    entry = {"repeat_iteration": 0, "global_vector_index": 7,
             "signals": ["CLK", "Q"], "waveform_table": "T"}
    resolver = SourceMapOccurrenceResolver({"vectors": {"7": {"source_map": [entry]}}})
    readonly = resolver.resolve_readonly(7)
    assert readonly["signals"] is entry["signals"]
    with pytest.raises(TypeError):
        readonly["global_vector_index"] = 99
    owned = resolver.resolve(7)
    assert owned == readonly
    assert owned["signals"] is not readonly["signals"]
    owned["signals"].append("MARK")
    assert entry["signals"] == ["CLK", "Q"]
    assert resolver.resolve_readonly(7)["signals"] == ["CLK", "Q"]


def test_readonly_lookup_never_deepcopies_unrelated_source_fields(monkeypatch):
    import stil.map_lookup as lookup

    def forbidden_copy(value):
        raise AssertionError("read-only provenance must not deep-copy nested metadata")

    monkeypatch.setattr(lookup, "deepcopy", forbidden_copy)
    resolver = SourceMapOccurrenceResolver({"vectors": {"0": {"source_map": [
        {"global_vector_index": 4, "signals": list(range(1000)), "waveform_table": "T"},
    ]}}})
    assert resolver.resolve_readonly(0)["waveform_table"] == "T"


def test_indexed_direct_repeat_lookup_preserves_first_match_and_fallback() -> None:
    payload = {
        "source_map": [
            {"repeat_iteration": 0, "global_vector_index": 10, "waveform_table": "W0"},
            {"repeat_iteration": 1, "global_vector_index": 11, "waveform_table": "W1"},
            {"repeat_iteration": 1, "global_vector_index": 99, "waveform_table": "DUPLICATE"},
        ]
    }
    _assert_resolver_matches(payload, repeat_index=1, logical_vector_index=101)
    _assert_resolver_matches(payload, repeat_index=8, logical_vector_index=108)


def test_indexed_compressed_repeat_lookup_preserves_global_indices() -> None:
    payload = {
        "source_map": [{"global_vector_index": 20, "waveform_table": "WFT_LOOP"}],
        "source_map_compression": {
            "kind": "loop_vector_repeat_v1",
            "global_vector_base": 20,
            "vector_stride": 3,
            "loop_id": 4,
            "loop_iteration_base": 9,
        },
    }
    _assert_resolver_matches(payload, repeat_index=5, logical_vector_index=35)


def test_indexed_loop_lookup_preserves_body_repeat_and_iteration_mapping() -> None:
    payload = {
        "body_logical_vector_count": 3,
        "iteration_base": 7,
        "body_source_map": [
            {
                "body_record_index": 0,
                "global_vector_index": 40,
                "body_repeat_count": 2,
                "body_source_entries": [
                    {"global_vector_index": 40, "waveform_table": "WFT_A"},
                    {"global_vector_index": 41, "waveform_table": "WFT_B"},
                ],
            },
            {
                "body_record_index": 1,
                "global_vector_index": 42,
                "body_repeat_count": 1,
                "waveform_table": "WFT_C",
            },
        ],
    }
    _assert_resolver_matches(
        payload,
        repeat_index=0,
        loop_iteration=2,
        body_record_index=0,
        body_repeat_index=1,
        logical_vector_index=47,
    )
    _assert_resolver_matches(
        payload,
        repeat_index=0,
        loop_iteration=3,
        body_record_index=1,
        body_repeat_index=0,
        logical_vector_index=52,
    )
    # Missing body records retain the legacy first-entry fallback.
    _assert_resolver_matches(
        payload,
        repeat_index=0,
        loop_iteration=0,
        body_record_index=8,
        body_repeat_index=0,
        logical_vector_index=0,
    )


def test_resolver_does_not_reuse_an_entry_after_payload_replacement() -> None:
    vectors = {
        "7": {"source_map": [{"global_vector_index": 1, "waveform_table": "OLD"}]}
    }
    resolver = SourceMapOccurrenceResolver({"vectors": vectors})
    assert resolver.resolve(7, repeat_index=0)["waveform_table"] == "OLD"

    replacement = {
        "source_map": [{"global_vector_index": 2, "waveform_table": "NEW"}]
    }
    vectors["7"] = replacement
    assert resolver.resolve(7, repeat_index=0) == source_map_entry_for_occurrence(
        replacement, repeat_index=0
    )


def test_loop_stride_fallback_uses_the_entire_body_before_narrowing() -> None:
    for declared_count in (None, 0, -1):
        payload = {"source_map": [
            {"body_record_index": 0, "body_repeat_count": 2, "global_vector_index": 10},
            {"body_record_index": 1, "body_repeat_count": 3, "global_vector_index": 12},
        ]}
        if declared_count is not None:
            payload["body_logical_vector_count"] = declared_count
        _assert_resolver_matches(payload, loop_iteration=4, body_record_index=1, body_repeat_index=2)
