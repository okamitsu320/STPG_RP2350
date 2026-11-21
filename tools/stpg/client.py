"""High-level STPG protocol client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .constants import (
    CMD_GET_CLOCK_INFO,
    CMD_ERASE_FLASH_SLOT,
    CMD_GET_FLASH_SLOT_INFO,
    CMD_SET_BOOT_SLOT,
    CMD_GET_FAIL_SUMMARY,
    CMD_GET_RATE_INFO,
    CMD_GET_STREAM_TELEMETRY,
    CMD_GET_SAMPLE_INFO,
    CMD_GET_STATUS,
    CMD_HELLO,
    CMD_INFO,
    CMD_LIST_FLASH_SLOTS,
    CMD_PSRAM_CHECKSUM,
    CMD_PSRAM_ERASE,
    CMD_PSRAM_READ,
    CMD_READ_FAIL_LOG,
    CMD_READ_SAMPLES,
    CMD_RESET,
    RESET_FLAG_RELEASE_OUTPUTS,
    CMD_RUN,
    CMD_SELECT_FLASH_SLOT,
    CMD_STOP,
    CMD_UPLOAD_BEGIN,
    CMD_UPLOAD_DATA,
    CMD_UPLOAD_END,
    ERR_NONE,
    RSP_CLOCK_INFO,
    RSP_DATA_END,
    RSP_DATA_ACK,
    RSP_ERROR,
    RSP_FLASH_SLOT_INFO,
    RSP_INFO,
    RSP_OK,
    RSP_READY_FOR_READ,
    RSP_READY_FOR_PAYLOAD,
    RSP_STATUS,
    STPG_FAIL_LOG_ENTRY_SIZE,
    STPG_FAIL_LOG_READ_PAGE_ENTRIES,
    STPG_RSP_SIZE,
    STPG_UPLOAD_CHUNK_SIZE,
    STORAGE_FLASH,
    STORAGE_PSRAM,
    STORAGE_SRAM,
    RUN_FLAG_PRESERVE_GPIO_STATE,
    RUN_FLAG_SEGMENT_END_HOLD,
    RUN_FLAG_STREAMING_POC,
    RUN_FLAG_FAIL_STOP,
    BOOTCFG_READ_CURRENT,
    BOOTCFG_SET_SLOT,
    BOOTCFG_DISABLE,
)
from .crc32 import crc32
from .protocol import StpgCommand, StpgStatusResponse
from .transport import Transport


class StpgClientError(RuntimeError):
    def __init__(self, message: str, response: StpgStatusResponse | None = None):
        super().__init__(message)
        self.response = response


@dataclass(frozen=True)
class FailLogEntry:
    event_index: int
    actual: int
    expected: int
    mask: int

    @classmethod
    def unpack_many(cls, payload: bytes) -> list["FailLogEntry"]:
        if len(payload) % STPG_FAIL_LOG_ENTRY_SIZE != 0:
            raise ValueError(f"fail log payload size must be a multiple of {STPG_FAIL_LOG_ENTRY_SIZE}")
        entries: list[FailLogEntry] = []
        for offset in range(0, len(payload), STPG_FAIL_LOG_ENTRY_SIZE):
            chunk = payload[offset:offset + STPG_FAIL_LOG_ENTRY_SIZE]
            event_index = int.from_bytes(chunk[0:4], "little")
            actual = int.from_bytes(chunk[4:8], "little")
            expected = int.from_bytes(chunk[8:12], "little")
            mask = int.from_bytes(chunk[12:16], "little")
            entries.append(cls(event_index, actual, expected, mask))
        return entries


@dataclass
class StpgClient:
    transport: Transport
    seq: int = 1

    def next_seq(self) -> int:
        value = self.seq
        self.seq += 1
        if self.seq == 0:
            self.seq = 1
        return value

    def send_command(self, cmd: int, arg0: int = 0, arg1: int = 0,
                     arg2: int = 0, arg3: int = 0, *,
                     response_timeout: float | None = None) -> StpgStatusResponse:
        command = StpgCommand(cmd, self.next_seq(), arg0, arg1, arg2, arg3)
        self.transport.write(command.pack())
        if response_timeout is None:
            raw_response = self.transport.read_exactly(STPG_RSP_SIZE)
        else:
            try:
                raw_response = self.transport.read_exactly_with_timeout(STPG_RSP_SIZE, response_timeout)
            except AttributeError as exc:
                raise StpgClientError("transport lacks bounded response-timeout API") from exc
        response = StpgStatusResponse.unpack(raw_response)
        if response.seq != command.seq:
            raise StpgClientError(f"response seq mismatch: expected {command.seq}, got {response.seq}", response)
        return response

    def expect(self, response: StpgStatusResponse, rsp: int) -> StpgStatusResponse:
        if response.rsp == RSP_ERROR:
            raise StpgClientError(f"device error: {response.error_code}", response)
        if response.rsp != rsp:
            raise StpgClientError(f"unexpected response: expected 0x{rsp:04X}, got 0x{response.rsp:04X}", response)
        return response

    def hello(self) -> StpgStatusResponse:
        return self.expect(self.send_command(CMD_HELLO), RSP_OK)

    def info(self) -> StpgStatusResponse:
        return self.expect(self.send_command(CMD_INFO), RSP_INFO)

    def status(self, *, response_timeout: float | None = None) -> StpgStatusResponse:
        return self.expect(
            self.send_command(CMD_GET_STATUS, response_timeout=response_timeout),
            RSP_STATUS,
        )

    def clock_info(self) -> StpgStatusResponse:
        return self.expect(self.send_command(CMD_GET_CLOCK_INFO), RSP_CLOCK_INFO)

    def rate_info(self) -> StpgStatusResponse:
        return self.expect(self.send_command(CMD_GET_RATE_INFO), RSP_INFO)

    def stream_telemetry(self, page: int = 0, selector: int = 0) -> StpgStatusResponse:
        return self.expect(self.send_command(CMD_GET_STREAM_TELEMETRY, page, selector, 0, 0), RSP_INFO)

    def sample_info(self) -> StpgStatusResponse:
        return self.expect(self.send_command(CMD_GET_SAMPLE_INFO), RSP_STATUS)

    def fail_summary(self) -> StpgStatusResponse:
        return self.expect(self.send_command(CMD_GET_FAIL_SUMMARY), RSP_STATUS)

    def run(self, run_flags: int = 0, start_event_index: int = 0,
            max_events: int = 0, aux_arg: int = 0,
            *, response_timeout: float | None = None) -> StpgStatusResponse:
        return self.expect(
            self.send_command(
                CMD_RUN,
                run_flags,
                start_event_index,
                max_events,
                aux_arg,
                response_timeout=response_timeout,
            ),
            RSP_OK,
        )

    def run_with_write_boundary(self, on_pre_write_intent: Callable[[bytes, int], None],
                                on_host_write_complete: Callable[[bytes, int], None], *, run_flags: int = 0,
                                start_event_index: int = 0, max_events: int = 0, aux_arg: int = 0) -> StpgStatusResponse:
        """Dedicated RUN path; callback occurs after full write+flush and before response read."""
        command = StpgCommand(CMD_RUN, self.next_seq(), run_flags, start_event_index, max_events, aux_arg)
        packet = command.pack()
        writer = getattr(self.transport, "write_run_packet_with_boundary", None)
        if writer is None:
            raise StpgClientError("transport lacks dedicated RUN write-boundary API")
        on_pre_write_intent(packet, command.seq)
        writer(packet, lambda complete: on_host_write_complete(complete, command.seq))
        response = StpgStatusResponse.unpack(self.transport.read_exactly(STPG_RSP_SIZE))
        if response.seq != command.seq:
            raise StpgClientError(f"response seq mismatch: expected {command.seq}, got {response.seq}", response)
        return self.expect(response, RSP_OK)

    def run_preserve_gpio_state(self) -> StpgStatusResponse:
        return self.run(run_flags=RUN_FLAG_PRESERVE_GPIO_STATE)

    def run_segment_hold(self) -> StpgStatusResponse:
        return self.run(run_flags=RUN_FLAG_SEGMENT_END_HOLD)

    def run_stream_segment_hold(
        self,
        *,
        fail_stop_limit: int | None = None,
        response_timeout: float | None = None,
    ) -> StpgStatusResponse:
        """Start a Rate Vector segment and park GPIO at its terminal state."""
        flags = RUN_FLAG_STREAMING_POC | RUN_FLAG_SEGMENT_END_HOLD
        if fail_stop_limit is not None:
            flags |= RUN_FLAG_FAIL_STOP
        return self.run(
            run_flags=flags,
            aux_arg=0 if fail_stop_limit is None else fail_stop_limit,
            response_timeout=response_timeout,
        )

    def run_resume_from_hold(self, expected_drive_value: int, expected_drive_enable: int, *, hold_after: bool = False) -> StpgStatusResponse:
        flags = RUN_FLAG_PRESERVE_GPIO_STATE | (RUN_FLAG_SEGMENT_END_HOLD if hold_after else 0)
        return self.run(run_flags=flags, start_event_index=expected_drive_value, max_events=expected_drive_enable)

    def run_stream_resume_from_hold(
        self,
        expected_drive_value: int,
        expected_drive_enable: int,
        *,
        hold_after: bool = False,
        fail_stop_limit: int | None = None,
        response_timeout: float | None = None,
    ) -> StpgStatusResponse:
        """Resume a Rate Vector segment from the previously parked GPIO state."""
        flags = (
            RUN_FLAG_STREAMING_POC
            | RUN_FLAG_PRESERVE_GPIO_STATE
            | (RUN_FLAG_SEGMENT_END_HOLD if hold_after else 0)
        )
        if fail_stop_limit is not None:
            flags |= RUN_FLAG_FAIL_STOP
        return self.run(
            run_flags=flags,
            start_event_index=expected_drive_value,
            max_events=expected_drive_enable,
            aux_arg=0 if fail_stop_limit is None else fail_stop_limit,
            response_timeout=response_timeout,
        )

    def reset(self, *, release_outputs: bool = False) -> StpgStatusResponse:
        """Clear results; optionally release PIO outputs (old firmware rejects)."""
        return self.expect(self.send_command(
            CMD_RESET, RESET_FLAG_RELEASE_OUTPUTS if release_outputs else 0,
        ), RSP_OK)

    def stop(self) -> StpgStatusResponse:
        return self.expect(self.send_command(CMD_STOP), RSP_OK)

    def read_samples(self, offset: int, length: int) -> bytes:
        ready = self.expect(self.send_command(CMD_READ_SAMPLES, offset, length, 0, 0), RSP_READY_FOR_READ)
        payload = self.transport.read_exactly(ready.pattern_size)
        trailer = StpgStatusResponse.unpack(self.transport.read_exactly(STPG_RSP_SIZE))
        if trailer.seq != ready.seq:
            raise StpgClientError(f"read trailer seq mismatch: expected {ready.seq}, got {trailer.seq}", trailer)
        self.expect(trailer, RSP_DATA_END)
        if trailer.pattern_size != ready.pattern_size or trailer.run_progress != ready.run_progress:
            raise StpgClientError("read trailer metadata mismatch", trailer)
        return payload

    def _read_fail_log_page(self, offset_entries: int, count: int) -> list[FailLogEntry]:
        ready = self.expect(
            self.send_command(CMD_READ_FAIL_LOG, offset_entries, count, 0, 0),
            RSP_READY_FOR_READ,
        )
        if ready.event_count != count or ready.run_progress != offset_entries:
            raise StpgClientError("fail-log page metadata mismatch", ready)
        payload = self.transport.read_exactly(ready.pattern_size)
        trailer = StpgStatusResponse.unpack(self.transport.read_exactly(STPG_RSP_SIZE))
        if trailer.seq != ready.seq:
            raise StpgClientError(f"read trailer seq mismatch: expected {ready.seq}, got {trailer.seq}", trailer)
        self.expect(trailer, RSP_DATA_END)
        if trailer.pattern_size != ready.pattern_size or trailer.run_progress != ready.run_progress:
            raise StpgClientError("read trailer metadata mismatch", trailer)
        entries = FailLogEntry.unpack_many(payload)
        if len(entries) != count:
            raise StpgClientError(
                f"fail-log page entry count mismatch: expected {count}, got {len(entries)}",
                trailer,
            )
        return entries

    def read_fail_log(self, offset_entries: int, count: int) -> list[FailLogEntry]:
        """Read FAIL_LOG entries in conservative wire-size pages.

        The firmware storage capacity and the requested range are separate
        contracts. Keep each request at two entries as a conservative host
        compatibility policy; this is not a firmware wire-size limit.
        """
        if offset_entries < 0 or count < 0:
            raise ValueError("FAIL_LOG offset and count must be non-negative")

        if count == 0:
            return self._read_fail_log_page(offset_entries, 0)

        entries: list[FailLogEntry] = []
        offset = offset_entries
        remaining = count
        while remaining:
            page_count = min(remaining, STPG_FAIL_LOG_READ_PAGE_ENTRIES)
            entries.extend(self._read_fail_log_page(offset, page_count))
            offset += page_count
            remaining -= page_count
        return entries

    def psram_checksum(self, offset: int, size: int) -> StpgStatusResponse:
        return self.expect(self.send_command(CMD_PSRAM_CHECKSUM, offset, size, 0, 0), RSP_OK)

    def psram_read(self, offset: int, size: int) -> bytes:
        ready = self.expect(self.send_command(CMD_PSRAM_READ, offset, size, 0, 0), RSP_READY_FOR_READ)
        payload = self.transport.read_exactly(ready.pattern_size)
        trailer = StpgStatusResponse.unpack(self.transport.read_exactly(STPG_RSP_SIZE))
        if trailer.seq != ready.seq:
            raise StpgClientError(f"read trailer seq mismatch: expected {ready.seq}, got {trailer.seq}", trailer)
        self.expect(trailer, RSP_DATA_END)
        if trailer.pattern_size != ready.pattern_size or trailer.run_progress != ready.run_progress:
            raise StpgClientError("read trailer metadata mismatch", trailer)
        return payload

    def psram_erase(self, offset: int = 0, size: int = 0) -> StpgStatusResponse:
        return self.expect(self.send_command(CMD_PSRAM_ERASE, offset, size, 0, 0), RSP_OK)

    def flash_list_slots(self) -> StpgStatusResponse:
        return self.expect(self.send_command(CMD_LIST_FLASH_SLOTS), RSP_OK)

    def flash_get_slot_info(self, slot_id: int) -> StpgStatusResponse:
        return self.expect(self.send_command(CMD_GET_FLASH_SLOT_INFO, slot_id, 0, 0, 0), RSP_FLASH_SLOT_INFO)

    def flash_select_slot(self, slot_id: int, mode: int = 0) -> StpgStatusResponse:
        return self.expect(self.send_command(CMD_SELECT_FLASH_SLOT, slot_id, mode, 0, 0), RSP_OK)

    def flash_erase_slot(self, slot_id: int) -> StpgStatusResponse:
        return self.expect(self.send_command(CMD_ERASE_FLASH_SLOT, slot_id, 0, 0, 0), RSP_OK)

    def boot_slot_read(self) -> StpgStatusResponse:
        return self.expect(self.send_command(CMD_SET_BOOT_SLOT, BOOTCFG_READ_CURRENT, 0, 0, 0), RSP_INFO)

    def boot_slot_set(self, slot_id: int, boot_flags: int) -> StpgStatusResponse:
        return self.expect(self.send_command(CMD_SET_BOOT_SLOT, BOOTCFG_SET_SLOT, slot_id, boot_flags, 0), RSP_OK)

    def boot_slot_disable(self) -> StpgStatusResponse:
        return self.expect(self.send_command(CMD_SET_BOOT_SLOT, BOOTCFG_DISABLE, 0, 0, 0), RSP_OK)

    def upload(self, pattern: bytes, *, storage_target: int = STORAGE_SRAM,
               chunk_size: int = STPG_UPLOAD_CHUNK_SIZE,
               end_response_timeout: float | None = None) -> StpgStatusResponse:
        return self._upload(pattern, storage_target, chunk_size, end_response_timeout)

    def upload_with_end_write_boundary(
        self,
        pattern: bytes,
        on_pre_write_intent: Callable[[bytes, int], None],
        on_host_write_complete: Callable[[bytes, int], None],
        *,
        storage_target: int = STORAGE_SRAM,
        chunk_size: int = STPG_UPLOAD_CHUNK_SIZE,
        end_response_timeout: float | None = None,
    ) -> StpgStatusResponse:
        """Upload once with a durable boundary around only the final UPLOAD_END write.

        The callbacks surround exactly the 32-byte final command: intent is
        before the serial write; host-write completion is after a full write
        and flush but before the one final response read.  No callback asserts
        device receipt or successful finalization.
        """
        return self._upload(
            pattern,
            storage_target,
            chunk_size,
            end_response_timeout,
            on_pre_write_intent,
            on_host_write_complete,
        )

    def _upload(
        self,
        pattern: bytes,
        storage_target: int,
        chunk_size: int,
        end_response_timeout: float | None,
        on_upload_end_pre_write: Callable[[bytes, int], None] | None = None,
        on_upload_end_host_write_complete: Callable[[bytes, int], None] | None = None,
    ) -> StpgStatusResponse:
        if (on_upload_end_pre_write is None) != (on_upload_end_host_write_complete is None):
            raise ValueError("UPLOAD_END boundary callbacks must be supplied together")
        if chunk_size < 1 or chunk_size > STPG_UPLOAD_CHUNK_SIZE:
            raise ValueError(f"chunk_size must be in range 1..{STPG_UPLOAD_CHUNK_SIZE}")
        if storage_target not in (STORAGE_SRAM, STORAGE_PSRAM, STORAGE_FLASH):
            raise ValueError("storage_target must be STORAGE_SRAM, STORAGE_PSRAM, or STORAGE_FLASH")

        total_crc = crc32(pattern)
        try:
            self.expect(
                self.send_command(CMD_UPLOAD_BEGIN, storage_target, len(pattern), total_crc, 0),
                RSP_OK,
            )
        except TimeoutError as exc:
            raise TimeoutError(
                f"upload begin timeout: target={storage_target} size={len(pattern)}: {exc}"
            ) from exc

        offset = 0
        while offset < len(pattern):
            chunk = pattern[offset:offset + chunk_size]
            try:
                ready = self.send_command(CMD_UPLOAD_DATA, offset, len(chunk), crc32(chunk), 0)
            except TimeoutError as exc:
                raise TimeoutError(
                    f"upload ready timeout at offset={offset} size={len(chunk)}: {exc}"
                ) from exc
            try:
                self.expect(ready, RSP_READY_FOR_PAYLOAD)
            except StpgClientError as exc:
                raise StpgClientError(
                    f"upload ready failed at offset={offset} size={len(chunk)}: {exc}",
                    exc.response,
                ) from exc
            self.transport.write(chunk)
            try:
                ack = StpgStatusResponse.unpack(self.transport.read_exactly(STPG_RSP_SIZE))
            except TimeoutError as exc:
                raise TimeoutError(
                    f"upload ack timeout at offset={offset} size={len(chunk)}: {exc}"
                ) from exc
            if ack.seq != ready.seq:
                raise StpgClientError(f"payload ack seq mismatch: expected {ready.seq}, got {ack.seq}", ack)
            try:
                self.expect(ack, RSP_DATA_ACK)
            except StpgClientError as exc:
                raise StpgClientError(
                    f"upload ack failed at offset={offset} size={len(chunk)}: {exc}",
                    exc.response,
                ) from exc
            offset += len(chunk)

        try:
            if on_upload_end_pre_write is None:
                return self.expect(
                    self.send_command(
                        CMD_UPLOAD_END, len(pattern), total_crc, 0, 0,
                        response_timeout=end_response_timeout,
                    ),
                    RSP_OK,
                )

            command = StpgCommand(CMD_UPLOAD_END, self.next_seq(), len(pattern), total_crc, 0, 0)
            packet = command.pack()
            writer = getattr(self.transport, "write_upload_end_packet_with_boundary", None)
            if writer is None:
                raise StpgClientError("transport lacks dedicated UPLOAD_END write-boundary API")
            on_upload_end_pre_write(packet, command.seq)
            writer(packet, lambda complete: on_upload_end_host_write_complete(complete, command.seq))
            if end_response_timeout is None:
                raw_response = self.transport.read_exactly(STPG_RSP_SIZE)
            else:
                try:
                    raw_response = self.transport.read_exactly_with_timeout(
                        STPG_RSP_SIZE, end_response_timeout
                    )
                except AttributeError as exc:
                    raise StpgClientError("transport lacks bounded response-timeout API") from exc
            response = StpgStatusResponse.unpack(raw_response)
            if response.seq != command.seq:
                raise StpgClientError(
                    f"response seq mismatch: expected {command.seq}, got {response.seq}", response
                )
            return self.expect(response, RSP_OK)
        except TimeoutError as exc:
            raise TimeoutError(
                f"upload end timeout: size={len(pattern)}: {exc}"
            ) from exc

    @staticmethod
    def is_ok(response: StpgStatusResponse) -> bool:
        return response.rsp != RSP_ERROR and response.error_code == ERR_NONE
