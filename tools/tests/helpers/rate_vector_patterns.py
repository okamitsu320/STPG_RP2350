"""Synthetic host-test encodings only; no serial transport or physical runner."""
from dataclasses import dataclass
from stpg.client import FailLogEntry
from stpg.constants import CAP_EXPECT_COMPARE, CAP_RATE_LOOP_LIMITED, CAP_PSRAM_STREAMING, CAP_RATE_REPEAT, CAP_RATE_VECTOR_MODE, CAP_SAMPLE_CAPTURE, CAP_UPLOAD_PSRAM, ERR_BAD_SIZE, ERR_RING_UNDERFLOW, ERR_RATE_PERIOD_MISMATCH, ERR_UNSUPPORTED, RUN_FLAG_STREAMING_POC, STATE_DONE, STATE_ERROR, STPG_MAX_EVENT_COUNT, STPG_PSRAM_PATTERN_BASE, STPG_RESULT_FLAG_COMPARE_MISMATCH, STORAGE_PSRAM, STPG_DRIVE_COMPLEMENT, STPG_DRIVE_ONE, STPG_DRIVE_VECTOR, STPG_DRIVE_ZERO, STPG_RATE_SLOT_DIR_UPDATE, STPG_RATE_SLOT_SAMPLE
from stpg.pattern import LoopBlock, LoopRefRecord, RateVectorEntry, RateDesc, RateSlot, VectorRecord, expanded_vector_count_estimate, pack_rate_vector

@dataclass(frozen=True)
class ExpectedRateInfo:
    vector_count: int
    rate_count: int
    slot_count: int
    loop_block_count: int
    expanded_vector_count_estimate: int
    active_pins: int

@dataclass(frozen=True)
class CaseExpectation:
    should_run: bool
    expected_error: int = 0
    expected_runtime_error: int = 0
    sample_count: int = 0
    fail_count: int = 0
    result_flags: int = 0
    sample_mask: int = 0
    expected_samples: tuple[int, ...] = ()
    fail_log_count: int = 0
    expected_first_fail: FailLogEntry | None = None

@dataclass(frozen=True)
class RegressionCase:
    case_id: str
    description: str
    rates: tuple[RateDesc, ...]
    slots: tuple[RateSlot, ...]
    vectors: tuple[RateVectorEntry, ...]
    expectation: CaseExpectation
    loop_blocks: tuple[LoopBlock, ...] = ()
    run_flags: int = 0

    @property
    def rate_info(self) -> ExpectedRateInfo:
        return ExpectedRateInfo(vector_count=len(self.vectors), rate_count=len(self.rates), slot_count=len(self.slots), loop_block_count=len(self.loop_blocks), expanded_vector_count_estimate=expanded_vector_count_estimate(self.vectors, self.loop_blocks), active_pins=16)

    def build_pattern(self, *, tick_hz: int) -> bytes:
        return pack_rate_vector(list(self.rates), list(self.slots), list(self.vectors), tick_hz=tick_hz, loop_blocks=list(self.loop_blocks))

def _rate(period_ticks: int, slot_count: int, slot_offset: int) -> RateDesc:
    return RateDesc(period_ticks=period_ticks, slot_count=slot_count, flags=0, slot_offset=slot_offset)

def _slot(time_ticks: int, flags: int, *, drive_mask: int=0, dir_mask: int=0, sample_mask: int=0) -> RateSlot:
    return RateSlot(time_ticks=time_ticks, flags=flags, drive_mask=drive_mask, dir_mask=dir_mask, sample_mask=sample_mask)

def _vector(rate_id: int, repeat: int, *, drive_value: int, drive_enable: int, expect_value: int, expect_mask: int) -> VectorRecord:
    return VectorRecord(rate_id=rate_id, flags=0, repeat=repeat, drive_value=drive_value, drive_enable=drive_enable, expect_value=expect_value, expect_mask=expect_mask)

def _loop_ref(loop_id: int) -> LoopRefRecord:
    return LoopRefRecord(loop_id=loop_id)

def build_cases() -> dict[str, RegressionCase]:
    rv01 = RegressionCase(case_id='RV-01', description='baseline compare-pass closed 2-slot pattern', rates=(_rate(16, 2, 0),), slots=(_slot(0, STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE, drive_mask=63, dir_mask=63), _slot(8, STPG_DRIVE_ZERO | STPG_RATE_SLOT_SAMPLE, drive_mask=63, sample_mask=63)), vectors=(_vector(0, 16, drive_value=21, drive_enable=63, expect_value=21, expect_mask=63),), expectation=CaseExpectation(should_run=True, sample_count=16, fail_count=0, result_flags=0, sample_mask=63, expected_samples=(21,) * 16))
    rv02 = RegressionCase(case_id='RV-02', description='repeat closure over many cycles', rates=rv01.rates, slots=rv01.slots, vectors=(_vector(0, 64, drive_value=42, drive_enable=63, expect_value=42, expect_mask=63),), expectation=CaseExpectation(should_run=True, sample_count=64, fail_count=0, result_flags=0, sample_mask=63, expected_samples=(42,) * 64))
    rv03 = RegressionCase(case_id='RV-03', description='same-time merge with dir update and drive', rates=(_rate(16, 3, 0),), slots=(_slot(0, STPG_RATE_SLOT_DIR_UPDATE, dir_mask=3), _slot(0, STPG_DRIVE_VECTOR, drive_mask=3), _slot(8, STPG_RATE_SLOT_SAMPLE, sample_mask=3)), vectors=(_vector(0, 8, drive_value=3, drive_enable=3, expect_value=3, expect_mask=3),), expectation=CaseExpectation(should_run=True, sample_count=8, fail_count=0, result_flags=0, sample_mask=3, expected_samples=(3,) * 8))
    rv04a = RegressionCase(case_id='RV-04A', description='T_min floor exact boundary passes', rates=(_rate(12, 2, 0),), slots=(_slot(0, STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE, drive_mask=1, dir_mask=1), _slot(6, STPG_RATE_SLOT_SAMPLE, sample_mask=1)), vectors=(_vector(0, 8, drive_value=1, drive_enable=1, expect_value=1, expect_mask=1),), expectation=CaseExpectation(should_run=True, sample_count=8, fail_count=0, result_flags=0, sample_mask=1, expected_samples=(1,) * 8))
    rv04b = RegressionCase(case_id='RV-04B', description='delta below T_min rejects during expansion', rates=(_rate(12, 2, 0),), slots=(_slot(0, STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE, drive_mask=1, dir_mask=1), _slot(5, STPG_RATE_SLOT_SAMPLE, sample_mask=1)), vectors=rv04a.vectors, expectation=CaseExpectation(should_run=False, expected_error=ERR_RATE_PERIOD_MISMATCH))
    rv04c = RegressionCase(case_id='RV-04C', description='wrap delta below T_min rejects during expansion', rates=(_rate(16, 2, 0),), slots=(_slot(0, STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE, drive_mask=1, dir_mask=1), _slot(12, STPG_RATE_SLOT_SAMPLE, sample_mask=1)), vectors=(_vector(0, 8, drive_value=1, drive_enable=1, expect_value=1, expect_mask=1),), expectation=CaseExpectation(should_run=False, expected_error=ERR_RATE_PERIOD_MISMATCH))
    rv05_rz = RegressionCase(case_id='RV-05-RZ', description='RZ waveform with compare-pass sampling', rates=(_rate(64, 2, 0),), slots=(_slot(0, STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE, drive_mask=1, dir_mask=1), _slot(32, STPG_DRIVE_ZERO | STPG_RATE_SLOT_SAMPLE, drive_mask=1, sample_mask=1)), vectors=(_vector(0, 16, drive_value=1, drive_enable=1, expect_value=1, expect_mask=1),), expectation=CaseExpectation(should_run=True, sample_count=16, fail_count=0, result_flags=0, sample_mask=1, expected_samples=(1,) * 16))
    rv05_r1 = RegressionCase(case_id='RV-05-R1', description='R1 waveform with compare-pass sampling', rates=(_rate(64, 2, 0),), slots=(_slot(0, STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE, drive_mask=1, dir_mask=1), _slot(32, STPG_DRIVE_ONE | STPG_RATE_SLOT_SAMPLE, drive_mask=1, sample_mask=1)), vectors=(_vector(0, 16, drive_value=0, drive_enable=1, expect_value=0, expect_mask=1),), expectation=CaseExpectation(should_run=True, sample_count=16, fail_count=0, result_flags=0, sample_mask=1, expected_samples=(0,) * 16))
    rv06 = RegressionCase(case_id='RV-06', description='SBC waveform with compare-pass sampling', rates=(_rate(96, 3, 0),), slots=(_slot(0, STPG_DRIVE_COMPLEMENT | STPG_RATE_SLOT_DIR_UPDATE, drive_mask=1, dir_mask=1), _slot(32, STPG_DRIVE_VECTOR, drive_mask=1), _slot(64, STPG_DRIVE_COMPLEMENT | STPG_RATE_SLOT_SAMPLE, drive_mask=1, sample_mask=1)), vectors=(_vector(0, 16, drive_value=0, drive_enable=1, expect_value=0, expect_mask=1),), expectation=CaseExpectation(should_run=True, sample_count=16, fail_count=0, result_flags=0, sample_mask=1, expected_samples=(0,) * 16))
    rv07 = RegressionCase(case_id='RV-07', description='multi-rate vector dispatch', rates=(_rate(16, 2, 0), _rate(24, 2, 2)), slots=(_slot(0, STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE, drive_mask=3, dir_mask=3), _slot(8, STPG_DRIVE_ZERO | STPG_RATE_SLOT_SAMPLE, drive_mask=3, sample_mask=3), _slot(0, STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE, drive_mask=3, dir_mask=3), _slot(12, STPG_DRIVE_ONE | STPG_RATE_SLOT_SAMPLE, drive_mask=3, sample_mask=3)), vectors=(_vector(0, 4, drive_value=1, drive_enable=3, expect_value=1, expect_mask=3), _vector(1, 4, drive_value=2, drive_enable=3, expect_value=2, expect_mask=3)), expectation=CaseExpectation(should_run=True, sample_count=8, fail_count=0, result_flags=0, sample_mask=3, expected_samples=(1,) * 4 + (2,) * 4))
    rv08_repeat = STPG_MAX_EVENT_COUNT // 2 + 1
    rv08 = RegressionCase(case_id='RV-08', description='uploadable but expanded run exceeds Stage-A event capacity', rates=(_rate(16, 2, 0),), slots=(_slot(0, STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE, drive_mask=1, dir_mask=1), _slot(8, STPG_RATE_SLOT_SAMPLE, sample_mask=1)), vectors=(_vector(0, rv08_repeat, drive_value=1, drive_enable=1, expect_value=1, expect_mask=1),), expectation=CaseExpectation(should_run=False, expected_error=ERR_BAD_SIZE))
    rv09 = RegressionCase(case_id='RV-09', description='intentional compare failure for fail-log verification', rates=rv01.rates, slots=rv01.slots, vectors=(_vector(0, 16, drive_value=21, drive_enable=63, expect_value=20, expect_mask=1),), expectation=CaseExpectation(should_run=True, sample_count=16, fail_count=16, result_flags=4, sample_mask=1, expected_samples=(21,) * 16, fail_log_count=16, expected_first_fail=FailLogEntry(event_index=1, actual=21, expected=20, mask=1)))
    lf01_loop = LoopBlock(loop_count=4, body_records=(_vector(0, 1, drive_value=1, drive_enable=3, expect_value=0, expect_mask=0), _vector(0, 1, drive_value=2, drive_enable=3, expect_value=0, expect_mask=0)))
    lf01 = RegressionCase(case_id='LF-01', description='basic loop full-expansion pass', rates=(_rate(16, 2, 0),), slots=(_slot(0, STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE, drive_mask=3, dir_mask=3), _slot(8, STPG_DRIVE_ZERO, drive_mask=3)), vectors=(_loop_ref(0),), loop_blocks=(lf01_loop,), expectation=CaseExpectation(should_run=True, sample_count=0, fail_count=0, result_flags=0))
    lf02 = RegressionCase(case_id='LF-02', description='loop plus vector repeat pass', rates=(_rate(16, 2, 0),), slots=(_slot(0, STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE, drive_mask=3, dir_mask=3), _slot(8, STPG_DRIVE_ZERO, drive_mask=3)), vectors=(_vector(0, 2, drive_value=3, drive_enable=3, expect_value=0, expect_mask=0), _loop_ref(0)), loop_blocks=(LoopBlock(loop_count=2, body_records=(_vector(0, 2, drive_value=1, drive_enable=3, expect_value=0, expect_mask=0),)),), expectation=CaseExpectation(should_run=True, sample_count=0, fail_count=0, result_flags=0))
    lf03 = RegressionCase(case_id='LF-03', description='multi-rate loop body pass', rates=(_rate(16, 1, 0), _rate(24, 1, 1)), slots=(_slot(0, STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE, drive_mask=3, dir_mask=3), _slot(0, STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE, drive_mask=3, dir_mask=3)), vectors=(_loop_ref(0),), loop_blocks=(LoopBlock(loop_count=2, body_records=(_vector(0, 1, drive_value=1, drive_enable=3, expect_value=0, expect_mask=0), _vector(1, 1, drive_value=2, drive_enable=3, expect_value=0, expect_mask=0))),), expectation=CaseExpectation(should_run=True, sample_count=0, fail_count=0, result_flags=0))
    lf05 = RegressionCase(case_id='LF-05', description='loop expands past Stage-A event limit', rates=(_rate(16, 2, 0),), slots=(_slot(0, STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE, drive_mask=1, dir_mask=1), _slot(8, STPG_DRIVE_ZERO, drive_mask=1)), vectors=(_loop_ref(0),), loop_blocks=(LoopBlock(loop_count=STPG_MAX_EVENT_COUNT // 2 + 1, body_records=(_vector(0, 1, drive_value=1, drive_enable=1, expect_value=0, expect_mask=0),)),), expectation=CaseExpectation(should_run=False, expected_error=ERR_BAD_SIZE))
    sv01 = RegressionCase(case_id='SV-01', description='small compare-free streaming pass', rates=(_rate(16, 2, 0),), slots=(_slot(0, STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE, drive_mask=3, dir_mask=3), _slot(8, STPG_DRIVE_ZERO, drive_mask=3)), vectors=(_vector(0, 64, drive_value=3, drive_enable=3, expect_value=0, expect_mask=0),), expectation=CaseExpectation(should_run=True, sample_count=0, fail_count=0, result_flags=0), run_flags=RUN_FLAG_STREAMING_POC)
    sv02 = RegressionCase(case_id='SV-02', description='small compare-aware streaming pass', rates=rv01.rates, slots=rv01.slots, vectors=(_vector(0, 16, drive_value=21, drive_enable=63, expect_value=21, expect_mask=63),), expectation=CaseExpectation(should_run=True, sample_count=16, fail_count=0, result_flags=0, sample_mask=63, expected_samples=(21,) * 16), run_flags=RUN_FLAG_STREAMING_POC)
    sv03 = RegressionCase(case_id='SV-03', description='streaming compare failure surfaces fail log', rates=rv01.rates, slots=rv01.slots, vectors=(_vector(0, 16, drive_value=21, drive_enable=63, expect_value=0, expect_mask=63),), expectation=CaseExpectation(should_run=True, sample_count=16, fail_count=16, fail_log_count=16, result_flags=STPG_RESULT_FLAG_COMPARE_MISMATCH, sample_mask=63, expected_samples=(21,) * 16, expected_first_fail=FailLogEntry(event_index=1, actual=21, expected=0, mask=63)), run_flags=RUN_FLAG_STREAMING_POC)
    sv04 = RegressionCase(case_id='SV-04', description='multi-buffer compare-free streaming pass', rates=(_rate(16, 1, 0),), slots=(_slot(0, STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE, drive_mask=3, dir_mask=3),), vectors=(_vector(0, 384, drive_value=3, drive_enable=3, expect_value=0, expect_mask=0),), expectation=CaseExpectation(should_run=True, sample_count=0, fail_count=0, result_flags=0), run_flags=RUN_FLAG_STREAMING_POC)
    sv05 = RegressionCase(case_id='SV-05', description='large compare-free pattern surfaces underflow instead of silent failure', rates=sv04.rates, slots=sv04.slots, vectors=(_vector(0, STPG_MAX_EVENT_COUNT + 128, drive_value=3, drive_enable=3, expect_value=0, expect_mask=0),), expectation=CaseExpectation(should_run=True, expected_runtime_error=ERR_RING_UNDERFLOW), run_flags=RUN_FLAG_STREAMING_POC)
    sv06 = RegressionCase(case_id='SV-06-300NS', description='large compare-aware streaming pass at 300ns-equivalent period', rates=(_rate(45, 2, 0),), slots=(_slot(0, STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE, drive_mask=63, dir_mask=63), _slot(22, STPG_RATE_SLOT_SAMPLE, sample_mask=63)), vectors=tuple((_vector(0, 1, drive_value=21 if index & 1 else 42, drive_enable=63, expect_value=21 if index & 1 else 42, expect_mask=63) for index in range(4000))), expectation=CaseExpectation(should_run=True, sample_count=4000, fail_count=0, result_flags=0, sample_mask=63, expected_samples=tuple((21 if index & 1 else 42 for index in range(4000)))), run_flags=RUN_FLAG_STREAMING_POC)
    sv07 = RegressionCase(case_id='SV-07-200NS', description='large compare-aware streaming pass at 200ns-equivalent period', rates=(_rate(30, 2, 0),), slots=(_slot(0, STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE, drive_mask=63, dir_mask=63), _slot(15, STPG_RATE_SLOT_SAMPLE, sample_mask=63)), vectors=tuple((_vector(0, 1, drive_value=21 if index & 1 else 42, drive_enable=63, expect_value=21 if index & 1 else 42, expect_mask=63) for index in range(4000))), expectation=CaseExpectation(should_run=True, sample_count=4000, fail_count=0, result_flags=0, sample_mask=63, expected_samples=tuple((21 if index & 1 else 42 for index in range(4000)))), run_flags=RUN_FLAG_STREAMING_POC)
    lf08 = RegressionCase(case_id='LF-08', description='streaming loop compare-free pass', rates=sv01.rates, slots=sv01.slots, vectors=(_loop_ref(0),), loop_blocks=(LoopBlock(loop_count=32, body_records=(_vector(0, 1, drive_value=3, drive_enable=3, expect_value=0, expect_mask=0), _vector(0, 1, drive_value=1, drive_enable=3, expect_value=0, expect_mask=0))),), expectation=CaseExpectation(should_run=True, sample_count=0, fail_count=0, result_flags=0), run_flags=RUN_FLAG_STREAMING_POC)
    lf09 = RegressionCase(case_id='LF-09', description='streaming loop rejects compare metadata', rates=sv01.rates, slots=sv01.slots, vectors=(_loop_ref(0),), loop_blocks=(LoopBlock(loop_count=4, body_records=(_vector(0, 1, drive_value=3, drive_enable=3, expect_value=3, expect_mask=3),)),), expectation=CaseExpectation(should_run=False, expected_error=ERR_UNSUPPORTED), run_flags=RUN_FLAG_STREAMING_POC)
    lf10 = RegressionCase(case_id='LF-10', description='streaming loop pass with chunk boundary on loop-iteration edge', rates=(_rate(16, 1, 0),), slots=(_slot(0, STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE, drive_mask=3, dir_mask=3),), vectors=(_loop_ref(0),), loop_blocks=(LoopBlock(loop_count=80, body_records=(_vector(0, 1, drive_value=0, drive_enable=3, expect_value=0, expect_mask=0), _vector(0, 1, drive_value=1, drive_enable=3, expect_value=0, expect_mask=0), _vector(0, 1, drive_value=2, drive_enable=3, expect_value=0, expect_mask=0), _vector(0, 1, drive_value=3, drive_enable=3, expect_value=0, expect_mask=0))),), expectation=CaseExpectation(should_run=True, sample_count=0, fail_count=0, result_flags=0), run_flags=RUN_FLAG_STREAMING_POC)
    lf11 = RegressionCase(case_id='LF-11', description='streaming loop pass with chunk boundary inside loop body', rates=(_rate(16, 1, 0),), slots=(_slot(0, STPG_DRIVE_VECTOR | STPG_RATE_SLOT_DIR_UPDATE, drive_mask=3, dir_mask=3),), vectors=(_loop_ref(0),), loop_blocks=(LoopBlock(loop_count=100, body_records=(_vector(0, 1, drive_value=1, drive_enable=3, expect_value=0, expect_mask=0), _vector(0, 1, drive_value=2, drive_enable=3, expect_value=0, expect_mask=0), _vector(0, 1, drive_value=3, drive_enable=3, expect_value=0, expect_mask=0))),), expectation=CaseExpectation(should_run=True, sample_count=0, fail_count=0, result_flags=0), run_flags=RUN_FLAG_STREAMING_POC)
    return {case.case_id: case for case in (rv01, rv02, rv03, rv04a, rv04b, rv04c, rv05_rz, rv05_r1, rv06, rv07, rv08, rv09, lf01, lf02, lf03, lf05, sv01, sv02, sv03, sv04, sv05, sv06, sv07, lf08, lf09, lf10, lf11)}
