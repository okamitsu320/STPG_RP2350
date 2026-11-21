from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SourceLocation:
    source_file: str
    line: int
    column: int = 1
    label: str | None = None
    expansion_trace: tuple[str, ...] = ()


@dataclass(frozen=True)
class Diagnostic:
    severity: str
    code: str
    message: str
    line: int
    column: int
    context: str | None = None


@dataclass(frozen=True)
class SignalDecl:
    name: str
    direction: str
    location: SourceLocation


@dataclass(frozen=True)
class SignalGroupDecl:
    name: str
    signals: tuple[str, ...]
    location: SourceLocation


@dataclass(frozen=True)
class EventDecl:
    time_text: str
    action_name: str
    action_arg: str | None
    location: SourceLocation


@dataclass(frozen=True)
class WaveformCharDecl:
    char: str
    events: tuple[EventDecl, ...]
    location: SourceLocation


@dataclass(frozen=True)
class WaveformDecl:
    signal_or_group: str
    chars: tuple[WaveformCharDecl, ...]
    location: SourceLocation


@dataclass(frozen=True)
class WaveformTableDecl:
    name: str
    period_text: str
    waveforms: tuple[WaveformDecl, ...]
    location: SourceLocation


@dataclass(frozen=True)
class PatternBurstDecl:
    name: str
    patterns: tuple[str, ...]
    location: SourceLocation
    macro_domains: tuple[str, ...] = ()
    procedure_domains: tuple[str, ...] = ()


@dataclass(frozen=True)
class VectorDecl:
    target: str
    chars: str
    waveform_table_name: str
    location: SourceLocation


@dataclass(frozen=True)
class LoopDecl:
    """A source STIL Loop retained without host-side expansion."""

    loop_count: int
    items: tuple["PatternItem", ...]
    location: SourceLocation


PatternItem = VectorDecl | LoopDecl


@dataclass(frozen=True)
class WaveformSelectStmt:
    name: str
    location: SourceLocation


@dataclass(frozen=True)
class VectorStmt:
    target: str
    chars: str
    location: SourceLocation


@dataclass(frozen=True)
class LoopStmt:
    loop_count: int
    statements: tuple["PatternStatement", ...]
    location: SourceLocation


@dataclass(frozen=True)
class MacroCallStmt:
    name: str
    location: SourceLocation


@dataclass(frozen=True)
class ProcedureCallStmt:
    name: str
    location: SourceLocation


@dataclass(frozen=True)
class GotoStmt:
    target: str
    location: SourceLocation


@dataclass(frozen=True)
class StopStmt:
    location: SourceLocation


PatternStatement = (
    WaveformSelectStmt
    | VectorStmt
    | LoopStmt
    | MacroCallStmt
    | ProcedureCallStmt
    | GotoStmt
    | StopStmt
)


@dataclass(frozen=True)
class MacroDecl:
    name: str
    domain: str
    statements: tuple[PatternStatement, ...]
    location: SourceLocation


@dataclass(frozen=True)
class ProcedureDecl:
    name: str
    domain: str
    statements: tuple[PatternStatement, ...]
    location: SourceLocation


@dataclass(frozen=True)
class PatternDecl:
    name: str
    vectors: tuple[VectorDecl, ...]
    location: SourceLocation
    # ``vectors`` remains the legacy expanded representation. ``items`` is
    # populated only when the parser is explicitly asked to preserve loops.
    items: tuple[PatternItem, ...] = ()
    statements: tuple[PatternStatement, ...] = ()


@dataclass(frozen=True)
class Document:
    source_file: str
    signals: tuple[SignalDecl, ...] = ()
    signal_groups: tuple[SignalGroupDecl, ...] = ()
    waveform_tables: tuple[WaveformTableDecl, ...] = ()
    pattern_bursts: tuple[PatternBurstDecl, ...] = ()
    patterns: tuple[PatternDecl, ...] = ()
    macros: tuple[MacroDecl, ...] = ()
    procedures: tuple[ProcedureDecl, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True)
class PinAssignment:
    bit: int
    logical_pin: str
    gpio: int | None = None
    direction: str = "inout"
    role: str = "signal"
    allow_shared: bool = False


@dataclass(frozen=True)
class PinMap:
    active_pins: int
    signals: dict[str, PinAssignment]


@dataclass(frozen=True)
class ResolvedVector:
    vector_index: int
    top_level_index: int
    location: SourceLocation
    pattern_name: str
    waveform_table_name: str
    signal_chars: dict[str, str]
    signal_bits: dict[str, int]
    source_path: tuple[int, ...] = ()
    # A vector lowered from a cut boundary iteration remains a plain
    # VectorRecord in the binary, but must retain enough provenance for wait
    # profile selection and source-map consumers to recognize its Loop origin.
    loop_source_path: tuple[int, ...] | None = None
    loop_iteration_index: int | None = None
    loop_body_index: int | None = None


@dataclass(frozen=True)
class ResolvedLoop:
    loop_count: int
    items: tuple["ResolvedItem", ...]
    location: SourceLocation
    source_path: tuple[int, ...] = ()
    # Logical index of the first vector in the first retained iteration.  A
    # sliced preserved loop may start in the middle of the source loop, so it
    # cannot be reconstructed from the first body's source vector index.
    logical_vector_base: int = 0
    iteration_base: int = 0


ResolvedItem = ResolvedVector | ResolvedLoop


@dataclass(frozen=True)
class ResolvedPattern:
    document: Document
    selected_pattern_burst: str | None
    selected_pattern_name: str
    waveform_table: WaveformTableDecl
    vectors: tuple[ResolvedVector, ...]
    pin_map: PinMap
    normalized_transition_count: int = 0
    normalized_transition_summary: tuple[str, ...] = ()
    items: tuple[ResolvedItem, ...] = ()
    loop_preserved: bool = False


@dataclass(frozen=True)
class RateKey:
    waveform_table_name: str
    period_ticks: int
    normalized_edge_topology: tuple[str, ...]


@dataclass(frozen=True)
class TimedSignalEvent:
    time_ticks: int
    action_name: str
    action_arg: str | None


@dataclass(frozen=True)
class ApproximationRecord:
    kind: str
    original_time: str
    emitted_time_ticks: int
    emitted_time_ns: str
    delta_ticks: int
    delta_ns: str
    affected_signal: str | None
    affected_vector_index: int | None
    stil_line: int | None
    waveform_table_name: str


@dataclass(frozen=True)
class IRVector:
    vector_index: int
    top_level_index: int
    stil_line: int
    label: str | None
    pattern_name: str
    waveform_table_name: str
    rate_key: RateKey
    signal_chars: dict[str, str]
    drive_chars: dict[str, str]
    compare_chars: dict[str, str]
    signal_bit_assignments: dict[str, int]
    signal_events: dict[str, tuple[TimedSignalEvent, ...]]
    expansion_trace: tuple[str, ...] = ()
    source_path: tuple[int, ...] = ()
    loop_source_path: tuple[int, ...] | None = None
    loop_iteration_index: int | None = None
    loop_body_index: int | None = None


@dataclass(frozen=True)
class IRLoop:
    loop_id: int
    loop_count: int
    items: tuple["IRSequenceItem", ...]
    source_path: tuple[int, ...] = ()
    stil_line: int | None = None
    logical_vector_base: int = 0
    iteration_base: int = 0


IRSequenceItem = IRVector | IRLoop


@dataclass(frozen=True)
class CompilerIR:
    resolved_pattern: ResolvedPattern
    vectors: tuple[IRVector, ...]
    exact: bool = True
    approximate_reasons: tuple[str, ...] = ()
    approximations: tuple[ApproximationRecord, ...] = ()
    warnings: tuple[str, ...] = ()
    markers: tuple[dict, ...] = ()
    terminal_drive_value: int = 0
    terminal_drive_enable: int = 0
    sequence_items: tuple[IRSequenceItem, ...] = ()
    logical_vector_count: int = 0
    loop_preserved: bool = False
    tick_hz: int = 0


@dataclass(frozen=True)
class CompileArtifacts:
    pattern_bytes: bytes
    mode_emitted: str
    map_data: dict
    report_data: dict
