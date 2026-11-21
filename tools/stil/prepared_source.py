"""Hash-bound, conversion-local reuse of one source range and its encoding.

No process-global or disk cache is used. Only the latest range/IR and
unshaped encoding are retained. Repeated preserved-range conversions may
opt into retaining one complete resolution; admission checks stay with callers.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path
from typing import Callable

from .ast import CompilerIR, Document, PinMap, ResolvedLoop, ResolvedPattern
from .backend_rate import RateBuildResult, ShapingOptions
from .errors import StilError
from .json_artifacts import resolve_json_artifact
from .parser import parse_stil_file
from .resolve import (_resolved_logical_vector_count, build_ir_with_scale, load_pin_map,
                      resolve_document, slice_resolved_pattern)


def _content_identity(path: Path | None) -> tuple[str, str] | None:
    if path is None:
        return None
    actual = resolve_json_artifact(path)
    digest = hashlib.sha256()
    with actual.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return str(actual), digest.hexdigest()


class PreparedStilSource:
    """A lazy preparation owned by one package/segmented conversion.

    Paths remain part of the binding because source-file names are provenance.
    Nested resolved/IR dictionaries are internal read-only data; compiler
    stages copy before adding markers. Do not mutate returned preparation.
    Clear the one-range cache after compilation so disk verification need not
    retain IR or an earlier encoding. With retain_full_resolution, clear drops
    only the range/IR/encoding; release also drops the complete preserved source.
    """

    def __init__(self, input_path: Path, *, pin_map_path: Path | None = None,
                 pattern_burst: str | None = None, pattern: str | None = None,
                 loop_policy: str = "expand", retain_full_resolution: bool = False) -> None:
        self._binding = (Path(input_path), Path(pin_map_path) if pin_map_path is not None else None,
                         pattern_burst, pattern, loop_policy)
        self._identity = None
        self._document: Document | None = None
        self._pin_map: PinMap | None = None
        self._resolved: ResolvedPattern | None = None
        # Opt-in for repeated preserved-range probes. Never expand a huge Loop
        # just to populate a cache. This is one source, not a multi-range cache.
        self._retain_full_resolution = retain_full_resolution and loop_policy == "preserve"
        self._full_resolved: ResolvedPattern | None = None
        self._range: tuple[int, int] | None = None
        self._invalid = False
        self._source_count: int | None = None
        self._ir: CompilerIR | None = None
        self._ir_options: tuple | None = None
        self._rate_result: RateBuildResult | None = None
        self._rate_options: tuple | None = None

    def validate_binding(self, input_path: Path, *, pin_map_path: Path | None,
                         pattern_burst: str | None, pattern: str | None, loop_policy: str) -> None:
        binding = (Path(input_path), Path(pin_map_path) if pin_map_path is not None else None,
                   pattern_burst, pattern, loop_policy)
        if binding != self._binding:
            raise StilError("ERR_PREPARED_SOURCE_BINDING", "prepared source paths/selection/loop policy do not match")

    def _current_identity(self):
        return _content_identity(self._binding[0]), _content_identity(self._binding[1])

    def _check_identity(self) -> None:
        if self._current_identity() != self._identity:
            self.clear_resolution()
            self._full_resolved = None
            self._invalid = True
            raise StilError("ERR_PREPARED_SOURCE_CHANGED", "source or pin map changed during conversion; prepare again")

    def document(self) -> Document:
        if self._invalid:
            raise StilError("ERR_PREPARED_SOURCE_CHANGED", "changed prepared source cannot be reused; prepare again")
        if self._document is None:
            identity = self._current_identity()
            if self._identity is not None and identity != self._identity:
                self._invalid = True
                raise StilError("ERR_PREPARED_SOURCE_CHANGED", "source or pin map changed after preparation was released")
            document = parse_stil_file(self._binding[0], loop_policy=self._binding[4])
            if self._current_identity() != identity:
                self._invalid = True
                raise StilError("ERR_PREPARED_SOURCE_CHANGED", "source or pin map changed while parsing")
            self._identity = identity
            self._document = document
        else:
            self._check_identity()
        return self._document

    def resolve(self, vector_range: tuple[int, int] | None = None) -> ResolvedPattern:
        document = self.document()  # hash checks run even on a cache hit
        if self._resolved is not None and self._range == vector_range:
            return self._resolved
        self.clear_resolution()
        if self._pin_map is None:
            pin_map = load_pin_map(self._binding[1], document=document)
            self._check_identity()
            self._pin_map = pin_map
        if self._retain_full_resolution:
            if self._full_resolved is None:
                full = resolve_document(document, pin_map=self._pin_map,
                                        pattern_burst_name=self._binding[2], pattern_name=self._binding[3],
                                        loop_policy=self._binding[4])
                self._check_identity()
                self._full_resolved = full
            result = (self._full_resolved if vector_range is None else slice_resolved_pattern(
                self._full_resolved, start=vector_range[0], end=vector_range[1]))
        else:
            result = resolve_document(document, pin_map=self._pin_map,
                                      pattern_burst_name=self._binding[2], pattern_name=self._binding[3],
                                      loop_policy=self._binding[4], vector_range=vector_range)
        self._check_identity()
        self._range, self._resolved = vector_range, result
        return result

    def _flat_preserved_source(self) -> bool:
        """Conservative proof for byte-size interpolation, not admission.

        Loop clipping can make encoded size non-monotone. Unknown/uncached
        inputs and expanded/auto-fallback sources keep the old bisection.
        """
        full = self._full_resolved
        return (full is not None and full.loop_preserved
                and not any(isinstance(item, ResolvedLoop) for item in full.items))

    def logical_vector_count(self) -> int:
        """Validate/count without retaining an expanded resolution.

        The opt-in preserved source may remain compressed in Loop structure.
        """

        document = self.document()
        if self._source_count is not None:
            return self._source_count
        if self._binding[4] == "preserve":
            self._source_count = _resolved_logical_vector_count(self.resolve())
            self.clear_resolution()
            return self._source_count
        if self._pin_map is None:
            pin_map = load_pin_map(self._binding[1], document=document)
            self._check_identity()
            self._pin_map = pin_map
        count: list[int] = []
        resolve_document(document, pin_map=self._pin_map, pattern_burst_name=self._binding[2],
                         pattern_name=self._binding[3], loop_policy=self._binding[4],
                         vector_range=(0, 0), _source_count=count)
        self._check_identity()
        self._source_count = count[0]
        return self._source_count

    def clear_resolution(self) -> None:
        self._resolved = None
        self._range = None
        self._ir = None
        self._ir_options = None
        self._release_rate_encoding()

    def _release_rate_encoding(self) -> None:
        """Drop the consumed analysis handoff before constructing reports."""

        self._rate_result = None
        self._rate_options = None

    def _compiler_ir(self, resolved: ResolvedPattern, *, tick_hz: int, time_scale: Decimal) -> CompilerIR:
        """Internal read-only IR handoff; timings are part of its identity."""

        self._check_identity()
        if resolved is not self._resolved:
            raise StilError("ERR_PREPARED_SOURCE_BINDING", "IR must belong to the current prepared range")
        options = (tick_hz, time_scale)
        if self._ir is None or self._ir_options != options:
            self._ir = None
            self._rate_result = None
            self._rate_options = None
            self._ir = build_ir_with_scale(resolved, tick_hz=tick_hz, time_scale=time_scale)
            self._ir_options = options
        return self._ir

    def _rate_vector(
        self, ir: CompilerIR, *, compiler: Callable[..., RateBuildResult], tick_hz: int,
        allow_approx: bool, max_rates: int | None, shaping: ShapingOptions | None = None,
    ) -> RateBuildResult:
        """Reuse only an unchanged analysis encoding, never a marker/shaped IR.

        Empty marker injection only enriches terminal metadata and shares the
        vectors/sequence. These are the exact read-only inputs of the backend.
        Approximation permission remains distinct when the IR needs it, so an
        analysis probe with allow_approx=True cannot bypass a compile refusal.
        """

        self._check_identity()
        eligible = (
            self._ir is not None
            and ir.resolved_pattern is self._ir.resolved_pattern
            and ir.vectors is self._ir.vectors
            and ir.sequence_items is self._ir.sequence_items
            and ir.tick_hz == self._ir.tick_hz == tick_hz
            and ir.loop_preserved == self._ir.loop_preserved
            and ir.logical_vector_count == self._ir.logical_vector_count
            and ir.exact == self._ir.exact
            and ir.approximate_reasons == self._ir.approximate_reasons
            and (shaping is None or not shaping.enabled)
        )
        options = (compiler, tick_hz, max_rates, allow_approx if ir.approximate_reasons else None)
        if eligible and self._rate_result is not None and self._rate_options == options:
            return self._rate_result
        built = compiler(ir, tick_hz=tick_hz, allow_approx=allow_approx, max_rates=max_rates, shaping=shaping)
        if eligible:
            self._rate_result, self._rate_options = built, options
        return built

    def release(self) -> None:
        """Drop parsed data too once planning ends; retain the identity binding."""

        self.clear_resolution()
        self._full_resolved = None
        self._document = None
        self._pin_map = None
