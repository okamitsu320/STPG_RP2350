"""Host-only tests for the STPG pattern viewer (stpgview)."""

from __future__ import annotations

import http.client
import io
import json
import os
from pathlib import Path
import re
import sys
import threading
import time

import pytest

from stil.pipeline import CompileOptions, compile_stil
from stil.errors import StilError
from stpg.constants import STPG_DRIVE_VECTOR, STPG_RATE_SLOT_DIR_UPDATE, STPG_RATE_SLOT_SAMPLE
from stpg.pattern import RateDesc, RateSlot, VectorRecord, pack_rate_vector
from stpgview import session as session_module
from stpgview.load_bin import load_bin
from stpgview.load_stil import load_stil
from stpgview.model import KIND_COMPARE, KIND_DRIVE, KIND_RELEASE, Trace
from stpgview.server import ViewerServer
from stpgview.session import Session, plan_inputs
from stpgview.vcd import write_vcd

pytestmark = pytest.mark.stpg_host_only

FIXTURES = Path(__file__).parent / "fixtures" / "stil"
PIN_MAP_24 = Path(__file__).parent / "fixtures" / "vcd" / "vf_c10lp_pinmap_24.json"

# Bidirectional turnaround, DirZ release at 50 ns, P/N labels, multiple WFTs,
# mixed rates, X samples, and a pattern long enough for summary mode.
REPRESENTATIVE = [
    "vf_c10lp_f8_bidir_turnaround_200ns.stil",
    "vf_c10lp_f14_main_representative_100ns.stil",
    "pn_norm.stil",
    "multi_wft_rate_switch.stil",
    "vf_c10lp_f9_short_mixed_rate_200_240_280ns.stil",
    "compare_hlt.stil",
    "c1_macro_m4_sampled_200ns.stil",
    "loop_boundary_exit_direct_regression.stil",
]
# Their 4-tick sub-slots cannot be represented by the 6-tick raw backend.
# Test these precise rejections separately; never skip an arbitrary exception.
RAW_OVERHEAD_REJECTIONS = {"pn_norm.stil", "multi_wft_rate_switch.stil", "compare_hlt.stil"}


def compile_to(tmp_path: Path, stil_name: str, mode: str, loop_policy: str = "expand") -> Path:
    payload, map_data, _report = compile_stil(
        CompileOptions(input_path=FIXTURES / stil_name, mode=mode, loop_policy=loop_policy)
    )
    out = tmp_path / f"{Path(stil_name).stem}.{mode}.bin"
    out.write_bytes(payload)
    out.with_name(out.stem + ".map.json").write_text(json.dumps(map_data))
    return out


def timelines(trace: Trace) -> dict[str, tuple[list, list]]:
    """signal -> (collapsed drive-state changes, compare strobes), absolute ps."""

    state = {s.name: "Z" for s in trace.signals}
    levels: dict[str, list] = {s.name: [] for s in trace.signals}
    strobes: dict[str, list] = {s.name: [] for s in trace.signals}
    for _index, t, event in trace.iter_changes():
        name = trace.signals[event.signal].name
        if event.kind == KIND_COMPARE:
            strobes[name].append((t, event.value))
            continue
        new = event.value if event.kind == KIND_DRIVE else "Z"
        if new != state[name]:
            state[name] = new
            levels[name].append((t, new))
    return {name: (levels[name], strobes[name]) for name in state}


def assert_same_behaviour(stil_trace: Trace, bin_trace: Trace) -> None:
    """Drive/release timelines and H/L strobes must match exactly. X (sampled, not
    compared) may be absent from a binary: raw events with H/L and selector X do not
    carry it per pin since the 2026-10-08 compare-mask repair."""

    expected = timelines(stil_trace)
    actual = timelines(bin_trace)
    for name, (levels, strobes) in expected.items():
        bin_levels, bin_strobes = actual[name]
        assert bin_levels == levels, name
        assert [s for s in bin_strobes if s[1] != "X"] == [s for s in strobes if s[1] != "X"], name
        assert {s for s in bin_strobes if s[1] == "X"} <= {s for s in strobes if s[1] == "X"}, name


@pytest.mark.parametrize("mode,stil_name", [(mode, name) for mode in ("rate-vector", "raw-event")
                                          for name in REPRESENTATIVE
                                          if mode != "raw-event" or name not in RAW_OVERHEAD_REJECTIONS])
def test_binary_decoding_matches_stil_semantics(tmp_path, stil_name, mode):
    binary = compile_to(tmp_path, stil_name, mode)
    stil_trace = load_stil(FIXTURES / stil_name)
    bin_trace = load_bin(binary)
    assert bin_trace.total_ps == stil_trace.total_ps
    assert [s.name for s in bin_trace.signals] == sorted(
        (s.name for s in stil_trace.signals), key=lambda n: next(x.bit for x in bin_trace.signals if x.name == n)
    )
    assert_same_behaviour(stil_trace, bin_trace)
    if mode == "rate-vector":
        assert bin_trace.vector_count == stil_trace.vector_count
        assert bin_trace.lines.tolist() == stil_trace.lines.tolist()


@pytest.mark.parametrize("stil_name", sorted(RAW_OVERHEAD_REJECTIONS))
def test_raw_fixture_rejects_its_exact_unrepresentable_subslot(tmp_path, stil_name):
    with pytest.raises(StilError) as caught:
        compile_to(tmp_path, stil_name, "raw-event")
    assert caught.value.code == "ERR_BAD_RAW_TIMING"
    assert str(caught.value) == "raw-event sub-slot 4 ticks is smaller than event overhead 6"


def test_preserved_loop_binary_expands_like_the_source(tmp_path):
    name = "loop_boundary_exit_direct_regression.stil"
    binary = compile_to(tmp_path, name, "rate-vector", loop_policy="preserve")
    stil_trace = load_stil(FIXTURES / name)
    bin_trace = load_bin(binary)
    assert bin_trace.vector_count == stil_trace.vector_count
    assert_same_behaviour(stil_trace, bin_trace)


NEW_SELECTORS_STIL = open(Path(__file__).with_name("fixtures") / "stil" / "stpgview_new_selectors.stil").read()     if (Path(__file__).with_name("fixtures") / "stil" / "stpgview_new_selectors.stil").exists() else None


@pytest.mark.parametrize("mode", ["rate-vector", "raw-event"])
def test_case_sensitive_selectors_and_character_repetition(tmp_path, mode):
    """U/D/h/l/u/d/p/n take their own WaveformChar definitions; \rN repeats one group."""

    source = Path(__file__).parent / "fixtures" / "stpgview" / "new_selectors.stil"
    trace = load_stil(source)
    assert trace.chars == ["01HL", "UDhl", "udlh", "11Hl", "p0XH", "d1TX", "0ZTT"]
    a = _signal(trace, "A")
    assert [(e.t, e.value) for e in trace.events(1) if e.signal == a] == [(200_000, "1")]   # U
    assert [(e.t, e.value) for e in trace.events(2) if e.signal == a] == [(400_000, "1")]   # u
    assert [(e.t, e.value) for e in trace.events(4) if e.signal == a] == [(0, "0"), (300_000, "1"), (600_000, "0")]
    c = _signal(trace, "C")
    assert [(e.t, e.kind, e.value) for e in trace.events(1) if e.signal == c] == [(700_000, KIND_COMPARE, "H")]  # h
    payload, map_data, _report = compile_stil(CompileOptions(input_path=source, mode=mode))
    binary = tmp_path / "p.bin"
    binary.write_bytes(payload)
    binary.with_name("p.map.json").write_text(json.dumps(map_data))
    assert_same_behaviour(trace, load_bin(binary))


def test_stil_view_keeps_source_p_and_n_labels():
    trace = load_stil(FIXTURES / "pn_norm.stil")
    assert [trace.chars[i] for i in range(trace.vector_count)] == ["P", "N"]
    events = trace.events(0)
    assert [(e.t, e.value) for e in events] == [(0, "0"), (20_000, "1")]


def test_dirz_release_time_follows_the_waveform_table():
    trace = load_stil(FIXTURES / "vf_c10lp_f14_main_representative_100ns.stil")
    releases = {e.t for i in range(trace.vector_count) for e in trace.events(i) if e.kind == KIND_RELEASE}
    assert releases == {50_000}


def test_binary_without_map_falls_back_to_pin_map_or_dut_names(tmp_path):
    binary = compile_to(tmp_path, "vf_c10lp_f8_bidir_turnaround_200ns.stil", "rate-vector")
    binary.with_name(binary.stem + ".map.json").unlink()
    plain = load_bin(binary)
    assert plain.signals[0].name == "DUT0"
    assert any("no map.json" in note for note in plain.notes)
    named = load_bin(binary, pin_map=PIN_MAP_24)
    assert named.signals[0].name == "clk"


def _synthetic_waited_binary(path: Path) -> Path:
    slot = RateSlot(time_ticks=0, flags=STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE, drive_mask=1, dir_mask=1,
                    sample_mask=0)
    sample = RateSlot(time_ticks=20, flags=STPG_RATE_SLOT_SAMPLE, drive_mask=0, dir_mask=0, sample_mask=2)
    rates = [RateDesc(period_ticks=40, slot_count=2, flags=0, slot_offset=0),
             RateDesc(period_ticks=1040, slot_count=2, flags=0, slot_offset=2)]
    records = [VectorRecord(rate_id=0, flags=0, repeat=2, drive_value=1, drive_enable=1, expect_value=2,
                            expect_mask=2),
               VectorRecord(rate_id=1, flags=0, repeat=1, drive_value=0, drive_enable=1, expect_value=0,
                            expect_mask=0)]
    path.write_bytes(pack_rate_vector(rates, [slot, sample, slot, sample], records, tick_hz=200_000_000))
    return path


def test_cloned_rate_with_longer_period_is_reported_as_wait(tmp_path):
    trace = load_bin(_synthetic_waited_binary(tmp_path / "waited.bin"))
    assert trace.vector_count == 3
    assert list(trace.extra_ps) == [0, 0, 5_000_000]
    assert trace.wait_vectors(0, 2) == [2]
    assert list(trace.wait_at) == [0, 0, 200_000]  # same group times: the added time is at the end
    assert trace.rates[1].name.startswith("rate1 = rate0 +")
    assert trace.wait_source == "inferred"  # no pattern.wait.patch.json next to this binary
    assert len(trace.signals) == 16  # no map: one DUT<n> column per active pin
    assert [row[:2] for row in trace.chars] == ["1H", "1H", "0X"]


def test_vcd_export_is_well_formed():
    trace = load_stil(FIXTURES / "vf_c10lp_f8_bidir_turnaround_200ns.stil")
    buffer = io.StringIO()
    write_vcd(trace, buffer)
    text = buffer.getvalue()
    header, _, body = text.partition("$enddefinitions $end")
    variables = re.findall(r"\$var (\w+) (\d+) (\S+) (\S+) \$end", header)
    assert len(variables) == 2 * len(trace.signals) + 3
    ids = {v[2] for v in variables}
    assert len(ids) == len(variables)
    times = [int(t) for t in re.findall(r"^#(\d+)$", body, flags=re.M)]
    assert times == sorted(times) and len(set(times)) == len(times)
    assert times[-1] == trace.total_ps
    for line in body.splitlines():
        if not line or line.startswith(("#", "$")):
            continue
        if line.startswith("b"):
            value, ident = line[1:].split()
            assert set(value) <= {"0", "1"} and ident in ids
        else:
            assert line[0] in "01xz" and line[1:] in ids


def test_plan_inputs_classifies_json_and_package_dirs(tmp_path):
    binary = compile_to(tmp_path, "vf_c10lp_f8_bidir_turnaround_200ns.stil", "rate-vector")
    map_path = binary.with_name(binary.stem + ".map.json")
    request = plan_inputs([binary, map_path, PIN_MAP_24])
    assert request.binaries == [binary]
    assert request.map_path == map_path
    assert request.pin_map == PIN_MAP_24
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "pattern.bin").write_bytes(binary.read_bytes())
    assert plan_inputs([package]).binaries == [package / "pattern.bin"]
    with pytest.raises(ValueError):
        plan_inputs([tmp_path / "missing.stil"])


def test_wave_query_switches_to_summary_for_wide_windows(monkeypatch):
    session = Session()
    (trace_id,) = session.load([FIXTURES / "loop_boundary_exit_direct_regression.stil"])
    trace = session.get(trace_id)
    detailed = session.wave(trace, 0, trace.starts[10], 500)
    assert detailed["mode"] == "events" and detailed["vectors"][0][0] == 0
    monkeypatch.setattr(session_module, "EVENT_WINDOW_LIMIT", 100)
    summary = session.wave(trace, 0, trace.total_ps, 300)
    assert summary["mode"] == "summary"
    assert len(summary["cells"]) == 300 and len(summary["cells"][0]) == len(trace.signals)
    found = session.find(trace, line=trace.lines[5])["index"]
    assert found <= 5 and trace.lines[found] == trace.lines[5]
    assert all(trace.lines[i] != trace.lines[5] for i in range(found))


@pytest.fixture()
def server():
    session = Session()
    session.load([FIXTURES / "vf_c10lp_f8_bidir_turnaround_200ns.stil"])
    srv = ViewerServer(("127.0.0.1", 0), session, "secret-token")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _get(srv: ViewerServer, path: str, headers: dict | None = None) -> tuple[int, bytes]:
    host, port = srv.server_address[:2]
    connection = http.client.HTTPConnection(host, port, timeout=10)
    try:
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def test_server_requires_token_and_loopback_host(server):
    assert _get(server, "/")[0] == 200
    assert _get(server, "/api/state")[0] == 401
    assert _get(server, "/api/state", {"X-STPG-Token": "wrong"})[0] == 401
    status, body = _get(server, "/api/state", {"X-STPG-Token": "secret-token"})
    assert status == 200 and json.loads(body)["traces"][0]["vector_count"] == 20
    assert _get(server, "/api/state", {"X-STPG-Token": "secret-token", "Host": "attacker.example"})[0] == 403
    assert _get(server, "/static/../server.py")[0] == 404


def test_server_serves_rows_vectors_and_vcd(server):
    headers = {"X-STPG-Token": "secret-token"}
    status, body = _get(server, "/api/rows?start=5&count=2", headers)
    rows = json.loads(body)["rows"]
    assert status == 200 and [r["i"] for r in rows] == [5, 6] and rows[0]["line"] == 69
    status, body = _get(server, "/api/vector?i=5", headers)
    assert status == 200 and json.loads(body)["source_text"].startswith("V { ALL =")
    assert _get(server, "/api/vector?i=999", headers)[0] == 400
    status, body = _get(server, "/api/export/vcd?token=secret-token")
    assert status == 200 and body.startswith(b"$date")


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX shell script as a fake viewer")
def test_external_viewer_receives_an_exported_vcd(server, tmp_path, monkeypatch):
    record = tmp_path / "args.txt"
    fake = tmp_path / "surfer"
    fake.write_text(f"#!/bin/sh\necho \"$1\" > {record}\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    host, port = server.server_address[:2]
    connection = http.client.HTTPConnection(host, port, timeout=10)
    connection.request("POST", "/api/external", headers={"X-STPG-Token": "secret-token", "Content-Length": "0"})
    response = connection.getresponse()
    payload = json.loads(response.read())
    connection.close()
    assert response.status == 200 and payload["viewer"] == str(fake)
    for _ in range(50):
        if record.exists():
            break
        time.sleep(0.1)
    vcd = Path(record.read_text().strip())
    assert vcd == Path(payload["vcd"]) and vcd.read_text().startswith("$date")


# ---------------------------------------------------------------------------
# editing
# ---------------------------------------------------------------------------
from stpgview.edit import EditError, StilEditor  # noqa: E402
from stil.errors import StilError  # noqa: E402

F8 = FIXTURES / "vf_c10lp_f8_bidir_turnaround_200ns.stil"


def _signal(trace: Trace, name: str) -> int:
    return [s.name for s in trace.signals].index(name)


def test_edits_produce_a_loadable_copy_with_the_requested_timing(tmp_path):
    source = load_stil(F8)
    editor = StilEditor(F8)
    result = editor.apply([
        {"op": "insert_wait", "after": 5, "duration": "1us"},
        {"op": "set_period", "first": 8, "last": 10, "period": "300ns"},
        {"op": "edge_time", "signal": "clk", "char": "1", "event": 0, "time": "60ns", "first": 12, "last": 13},
        {"op": "edge_time", "signal": "io_0", "char": "H", "event": 0, "time": "120ns", "table": "WFT1"},
    ], source)
    edited_path = tmp_path / "f8.edited.stil"
    edited_path.write_text(result.text)
    edited = load_stil(edited_path)
    assert F8.read_text() == editor.text  # the source is untouched
    assert edited.vector_count == source.vector_count + 1
    assert edited.total_ps == source.total_ps + 1_000_000 + 3 * 100_000
    # the wait vector holds the outputs present at the end of vector 5 and compares nothing
    assert edited.extra_ps[6] == 1_000_000 and edited.periods[6] == 1_000_000
    value, enable = edited.start_values[6], edited.start_enables[6]
    assert all(e.kind != KIND_COMPARE for e in edited.events(6))
    after_wait_value = value
    for e in edited.events(6):
        if e.kind == KIND_DRIVE:
            assert ((after_wait_value >> e.signal) & 1) == int(e.value) and (enable >> e.signal) & 1
    # periods 300 ns on original vectors 8..10 (shifted by the inserted wait)
    assert [edited.periods[i] for i in range(8, 13)] == [200_000, 300_000, 300_000, 300_000, 200_000]
    clk = _signal(edited, "clk")
    assert [e.t for e in edited.events(14) if e.signal == clk] == [60_000]  # original 13 has clk '1'
    io0 = _signal(edited, "io_0")
    h_times = {e.t for i in range(edited.vector_count) for e in edited.events(i) if e.signal == io0 and e.value == "H"}
    assert h_times == {120_000}
    # one vector was inserted, right after original vector 5
    assert result.inserted_after == {5: 1}


def test_edited_stil_compiles_and_matches_its_binary(tmp_path):
    source = load_stil(F8)
    result = StilEditor(F8).apply([{"op": "insert_wait", "after": 2, "duration": "4ms"},
                                   {"op": "set_period", "first": 4, "last": 6, "period": "250ns"}], source)
    edited_path = tmp_path / "edited.stil"
    edited_path.write_text(result.text)
    payload, map_data, _report = compile_stil(CompileOptions(input_path=edited_path, mode="rate-vector"))
    binary = tmp_path / "edited.bin"
    binary.write_bytes(payload)
    binary.with_name("edited.map.json").write_text(json.dumps(map_data))
    stil_trace, bin_trace = load_stil(edited_path), load_bin(binary)
    assert timelines(bin_trace) == timelines(stil_trace)
    assert bin_trace.wait_vectors(0, bin_trace.vector_count) == [3]  # the STPGV_WAIT hold vector


@pytest.mark.parametrize("edit, message", [
    ({"op": "set_period", "first": 3, "last": 4, "period": "50ns"}, "not inside the new period"),
    ({"op": "edge_time", "signal": "clk", "char": "1", "event": 0, "time": "250ns", "first": 1}, "not inside"),
    ({"op": "edge_time", "signal": "clk", "char": "Q", "event": 0, "time": "10ns", "first": 1}, "no character"),
    ({"op": "insert_wait", "after": 99, "duration": "1us"}, "out of range"),
    ({"op": "insert_wait", "after": 1, "duration": "soon"}, "bad time"),
])
def test_invalid_edits_are_rejected(edit, message):
    with pytest.raises(EditError, match=message):
        StilEditor(F8).apply([edit], load_stil(F8))


def test_vectors_inside_loops_are_not_editable():
    name = FIXTURES / "loop_boundary_exit_direct_regression.stil"
    trace = load_stil(name)
    with pytest.raises(EditError, match="Loop"):
        StilEditor(name).apply([{"op": "insert_wait", "after": trace.vector_count - 1, "duration": "1us"}], trace)


def test_session_edit_flow_preview_save_and_reopen(tmp_path):
    source = tmp_path / "f8.stil"
    source.write_text(F8.read_text())
    session = Session(work_dir=tmp_path / "work")
    (source_id,) = session.load([source])
    info = session.edits.add(source_id, {"op": "insert_wait", "after": 5, "duration": "2us"})
    preview_id = info["preview_id"]
    assert session.order == [source_id, preview_id]
    assert session.get(preview_id).vector_count == 21
    # editing from the preview refers back to original vectors (preview #7 is original #6)
    mapping = session.edits.preview_mapping(preview_id)
    assert mapping[7] == 6 and 6 not in mapping.values() or mapping[7] == 6
    with pytest.raises(EditError, match="inserted"):
        session.edits.add(preview_id, {"op": "insert_wait", "after": 6, "duration": "1us"})
    session.edits.add(preview_id, {"op": "set_period", "first": 7, "last": 7, "period": "300ns"})
    assert session.edits.info(source_id)["edits"][1] == {"op": "set_period", "first": 6, "last": 6, "period": "300ns"}
    saved = session.edits.save(source_id)["saved"]
    assert Path(saved["stil"]).name == "f8.edited.stil" and source.read_text() == F8.read_text()
    with pytest.raises(EditError, match="overwrite"):
        session.edits.save(source_id, str(source))
    # the edit list reopens on a fresh session and reproduces the same edited text
    fresh = Session(work_dir=tmp_path / "work2")
    reopened = fresh.load([saved["edits"]])
    assert fresh.get(reopened[-1]).vector_count == 21
    removed = session.edits.remove(source_id, 0)
    assert len(removed["edits"]) == 1 and session.get(preview_id).vector_count == 20
    cleared = session.edits.replace_all(source_id, [])
    assert cleared["preview_id"] is None and preview_id not in session.order


# ---------------------------------------------------------------------------
# which STIL forms open (toolchain form, ATE-generator input, rejections)
# ---------------------------------------------------------------------------
ATE_PROJECT = """STIL 1.0;
Signals {
  A In;
  B Out;
}
SignalGroups {
  ALL = 'A + B';
}
Timing {
  WaveformTable W1 {
    Period '100ns';
    Waveforms {
      A { 0 { '0ns' D; } }
      A { 1 { '0ns' U; } }
      B { L { '50ns' L; } }
      B { H { '50ns' H; } }
    }
  }
}
"""
ATE_PATTERN = """STIL 1.0;
Pattern P {
  W W1;
  V { ALL = 0L; }
  V { ALL = 1H; }
  V { ALL = \\r2 1; }
}
"""


def test_ate_generator_project_and_pattern_are_preprocessed(tmp_path):
    (tmp_path / "proj.stil").write_text(ATE_PROJECT)
    (tmp_path / "pat.stil").write_text(ATE_PATTERN.replace("\\r2 1", "1H"))
    session = Session(work_dir=tmp_path / "work")
    (trace_id,) = session.load([tmp_path / "pat.stil", tmp_path / "proj.stil"])  # either order
    trace = session.get(trace_id)
    assert trace.chars == ["0L", "1H", "1H"]
    assert [s["kind"] for s in trace.sources] == ["stil", "ategen_project", "ategen_pattern"]
    assert trace.preprocessed_from == {"project": str(tmp_path / "proj.stil"), "pattern": str(tmp_path / "pat.stil")}
    b = _signal(trace, "B")
    assert [(e.t, e.value) for e in trace.events(1) if e.signal == b] == [(50_000, "H")]
    with pytest.raises(StilError, match="together with its pattern file"):
        session.load([tmp_path / "proj.stil"])
    with pytest.raises(StilError, match="together with its project file"):
        session.load([tmp_path / "pat.stil"])


def test_single_file_with_tester_event_codes_is_preprocessed(tmp_path):
    single = tmp_path / "single.stil"
    single.write_text(ATE_PROJECT + ATE_PATTERN.replace("STIL 1.0;\n", "").replace("\\r2 1", "1H"))
    session = Session(work_dir=tmp_path / "work")
    trace = session.get(session.load([single])[0])
    assert trace.chars == ["0L", "1H", "1H"] and "tester event codes" in trace.notes[0]


@pytest.mark.parametrize("text, hint", [
    ("Signals { A In; B Out; }\nSignalGroups { ALL = 'A + B'; }\n"
     "Timing { WaveformTable W1 { Period '100ns'; Waveforms { A { 0: 0ns Drive(0); } } } }\n"
     "Pattern P { W W1; V { ALL = 0L; } }\n", "one declaration per line"),
    (ATE_PROJECT.replace("      A { 0 { '0ns' D; } }\n      A { 1 { '0ns' U; } }\n",
                         "      A { 01 { '0ns' D/U; } }\n") + ATE_PATTERN.replace("STIL 1.0;\n", ""),
     "tester event codes"),
    ('Signals {\n  "DQ[0]" InOut;\n}\n', "quoted signal names"),
])
def test_unsupported_stil_is_rejected_with_a_hint(tmp_path, text, hint):
    path = tmp_path / "x.stil"
    path.write_text(text)
    with pytest.raises(StilError, match=hint):
        Session(work_dir=tmp_path / "work").load([path])


def test_editing_preprocessed_input_saves_next_to_the_pattern_and_reopens(tmp_path):
    (tmp_path / "proj.stil").write_text(ATE_PROJECT)
    (tmp_path / "pat.stil").write_text(ATE_PATTERN.replace("\\r2 1", "1H"))
    session = Session(work_dir=tmp_path / "work")
    (trace_id,) = session.load([tmp_path / "proj.stil", tmp_path / "pat.stil"])
    session.edits.add(trace_id, {"op": "insert_wait", "after": 0, "duration": "1us"})
    saved = session.edits.save(trace_id)["saved"]
    assert Path(saved["stil"]) == tmp_path / "pat.pre.edited.stil"
    assert json.loads(Path(saved["edits"]).read_text())["preprocess"]["pattern"] == str(tmp_path / "pat.stil")
    fresh = Session(work_dir=tmp_path / "work2")
    ids = fresh.load([saved["edits"]])
    assert fresh.get(ids[-1]).vector_count == 4


def test_browse_roots_are_every_windows_drive_or_the_root_folder():
    # A model of the drive list: a native Windows run is a separate check.
    from stpgview.server import path_roots

    assert path_roots("nt", ["D:\\", "A:\\", "Z:\\", "C:\\", "K:\\"]) == ["A:\\", "C:\\", "D:\\", "K:\\", "Z:\\"]
    assert path_roots("posix") == ["/"]
    assert path_roots() == (["/"] if os.name != "nt" else path_roots())

