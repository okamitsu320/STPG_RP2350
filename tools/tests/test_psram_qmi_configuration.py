from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIRMWARE_CMAKE = ROOT / "firmware" / "CMakeLists.txt"
PSRAM_SOURCE = ROOT / "firmware" / "src" / "psram.c"


def test_psram_qmi_default_is_explicit_and_safe_by_default():
    cmake = FIRMWARE_CMAKE.read_text()

    assert "set(STPG_PSRAM_QMI_CLKDIV 2" in cmake
    assert "set(STPG_PSRAM_QMI_RXDELAY 2" in cmake
    assert "set(STPG_PSRAM_QMI_PAGEBREAK 1024" in cmake
    assert "STPG_PSRAM_ALLOW_UNSAFE_CLKDIV1 OFF" in cmake
    assert "STPG_PSRAM_QMI_CLKDIV EQUAL 1 AND NOT STPG_PSRAM_ALLOW_UNSAFE_CLKDIV1" in cmake
    assert "STPG_PSRAM_QMI_CLKDIV=${STPG_PSRAM_QMI_CLKDIV}" in cmake
    assert "STPG_PSRAM_QMI_RXDELAY=${STPG_PSRAM_QMI_RXDELAY}" in cmake
    assert "STPG_PSRAM_QMI_PAGEBREAK=${STPG_PSRAM_QMI_PAGEBREAK}" in cmake


def test_psram_qmi_uses_quad_read_write_and_sdk_timing_fields():
    source = PSRAM_SOURCE.read_text()

    assert "#define STPG_PSRAM_QUAD_ENABLE_CMD 0x35u" in source
    assert "#define STPG_PSRAM_QUAD_READ_CMD 0xEBu" in source
    assert "#define STPG_PSRAM_QUAD_WRITE_CMD 0x38u" in source
    assert "QMI_M1_TIMING_PAGEBREAK_VALUE_256" in source
    assert "QMI_M1_TIMING_PAGEBREAK_VALUE_1024" in source
    assert "QMI_M1_TIMING_PAGEBREAK_VALUE_4096" in source
    assert "QMI_M1_TIMING_MAX_SELECT_LSB" in source
    assert "QMI_M1_TIMING_MIN_DESELECT_LSB" in source
    assert "QMI_M1_RFMT_DATA_WIDTH_VALUE_Q" in source
    assert "QMI_M1_WFMT_DATA_WIDTH_VALUE_Q" in source
    assert "__no_inline_not_in_flash_func(stpg_psram_configure_qmi)" in source
    assert "stpg_psram_configure_qmi(max_select, min_deselect);" in source
    assert "stpg_psram_qmi_configuration_matches" in source
    assert "stpg_psram_reapply_qmi" in source
    assert "stpg_psram_prepare_access" in source
    assert "stpg_board_psram_configure_cs1(g_psram_cs1_gpio);" not in source
    assert "stpg_board_psram_restore_cs1_mux(g_psram_cs1_gpio);" in source
    assert source.index("stpg_psram_configure_qmi(max_select, min_deselect);") < source.index("stpg_psram_self_test()")


def test_psram_qmi_access_contract_checks_read_write_format_and_reentry_path():
    source = PSRAM_SOURCE.read_text()
    board = (PSRAM_SOURCE.parent / "board_rp2350a_16pin.c").read_text()
    pattern_buffer = (PSRAM_SOURCE.parent / "pattern_buffer.c").read_text()
    samples = (PSRAM_SOURCE.parent / "samples.c").read_text()

    assert "QMI_M1_TIMING_BITS" in source
    assert "QMI_M1_RFMT_BITS" in source
    assert "QMI_M1_WFMT_BITS" in source
    assert "qmi_hw->m[1].rcmd" in source
    assert "qmi_hw->m[1].wcmd" in source
    assert "static uint32_t g_psram_cs1_gpio;" in source
    assert "stpg_board_psram_restore_cs1_mux(g_psram_cs1_gpio);" in source
    assert "void stpg_board_psram_restore_cs1_mux(uint32_t gpio)" in board
    restore = board.split("void stpg_board_psram_restore_cs1_mux", 1)[1]
    restore = restore.split("const stpg_board_flash_profile_t", 1)[0]
    assert "gpio_set_function(gpio, GPIO_FUNC_XIP_CS1);" in restore
    assert "flash_devinfo_set_cs_" not in restore
    assert "stpg_psram_prepare_access()" in pattern_buffer
    assert "stpg_psram_prepare_access()" in samples


def test_streaming_pio_mux_preserves_psram_cs1_gap_and_rechecks_access():
    runner = (PSRAM_SOURCE.parent / "stpg_runner.c").read_text()
    board = (PSRAM_SOURCE.parent / "board_rp2350a_16pin.c").read_text()

    set_pins_start = runner.index("static void stpg_runner_set_pio_pins(void)")
    set_pins_end = runner.index("static void stpg_runner_reset_psram_mux_evidence", set_pins_start)
    set_pins = runner[set_pins_start:set_pins_end]
    assert "stpg_board_gpio_for_dut_pin(logical_bit)" in set_pins
    assert "stpg_board_out_pin_base" not in set_pins
    assert "stpg_board_out_pin_span" not in set_pins

    assert "bool stpg_board_gpio_is_dut_pin" in board
    assert "stpg_runner_admit_streaming_psram_mux" in runner
    assert "stpg_runner_recheck_streaming_psram_mux" in runner
    assert runner.count(
        "stpg_buffer_active_source() != STPG_PATTERN_SOURCE_PSRAM"
    ) >= 2
    assert "stpg_psram_selected_cs1_mux_matches()" in runner
    assert runner.index("stpg_runner_prepare_output_sm(preserve_gpio_state);") < runner.index(
        "stpg_runner_recheck_streaming_psram_mux();"
    )


def test_psram_qmi_reconfiguration_helper_executes_from_sram():
    source = PSRAM_SOURCE.read_text()

    helper_start = source.index("__no_inline_not_in_flash_func(stpg_psram_configure_qmi)")
    helper_end = source.index("static bool stpg_psram_self_test", helper_start)
    helper = source[helper_start:helper_end]

    assert "qmi_hw->direct_csr" in helper
    assert "qmi_hw->m[1].timing" in helper
    assert "qmi_hw->m[1].rfmt" in helper
    assert "qmi_hw->m[1].wfmt" in helper
    assert "stpg_psram_qmi_select_timing" not in helper


def test_psram_device_only_qmi_headers_do_not_break_host_builds():
    source = PSRAM_SOURCE.read_text()
    device_guard = source.index("#ifdef PICO_ON_DEVICE")

    assert source.index('#include "pico/stdlib.h"') > device_guard
    assert source.index('#include "hardware/regs/qmi.h"') > device_guard


def test_psram_reads_use_page_bounded_xip_stream_dma_with_small_read_fallbacks():
    source = PSRAM_SOURCE.read_text()
    read_start = source.index("stpg_err_t STPG_PSRAM_READ_FUNC(stpg_psram_read)(uint32_t offset, void *dst, uint32_t size)")
    read_end = source.index("stpg_err_t stpg_psram_write", read_start)
    read_body = source[read_start:read_end]

    assert "STPG_PSRAM_QMI_PAGEBREAK_BYTES STPG_PSRAM_QMI_PAGEBREAK" in source
    assert "STPG_PSRAM_XIP_STREAM_DMA_MIN_BYTES 256u" in source
    assert "const volatile uint8_t *page_src = stpg_psram_ptr(absolute);" in read_body
    assert "uint8_t *const page_dst = out + cursor;" in read_body
    assert "chunk >= STPG_PSRAM_XIP_STREAM_DMA_MIN_BYTES" in read_body
    assert "stpg_psram_read_page_dma(page_src, page_dst, chunk)" in read_body
    assert "const volatile uint32_t *const page_words" in read_body
    assert "const uint32_t word = page_words[index];" in read_body
    assert "memcpy(page_dst + (index * sizeof(uint32_t)), &word, sizeof(word));" in read_body
    assert "page_dst[index] = page_src[index];" in read_body
    assert "(uintptr_t)page_src | (uintptr_t)page_dst | (uintptr_t)chunk" in read_body
    assert "absolute % STPG_PSRAM_QMI_PAGEBREAK_BYTES" in read_body
    assert "STPG_PSRAM_READ_FUNC(stpg_psram_read)" in read_body
    assert "__dsb();" in read_body
    assert "__compiler_memory_barrier();" in read_body
    assert "memcpy(dst, stpg_psram_read_ptr(offset), size);" not in read_body


def test_psram_bulk_read_uses_xip_aux_stream_fifo_not_direct_mapped_dma():
    source = PSRAM_SOURCE.read_text()
    drain_start = source.index(
        "static bool __no_inline_not_in_flash_func(stpg_psram_drain_stream_fifo)"
    )
    helper_start = source.index(
        "static stpg_err_t __no_inline_not_in_flash_func(stpg_psram_read_page_dma)"
    )
    drain = source[drain_start:helper_start]
    helper_end = source.index("static void stpg_psram_sync_range", helper_start)
    helper = source[helper_start:helper_end]

    assert '#include "hardware/dma.h"' in source
    assert '#include "hardware/regs/dreq.h"' in source
    assert "dma_claim_unused_channel(false)" in source
    assert "dma_channel_set_irq0_enabled((uint)channel, false);" in source
    assert "dma_channel_set_irq1_enabled((uint)channel, false);" in source
    assert "channel_config_set_transfer_data_size(&config, DMA_SIZE_32);" in helper
    assert "channel_config_set_read_increment(&config, false);" in helper
    assert "channel_config_set_write_increment(&config, true);" in helper
    assert "channel_config_set_dreq(&config, DREQ_XIP_STREAM);" in helper
    assert "channel_config_set_high_priority(&config, false);" in helper
    assert "(const volatile void *)(uintptr_t)XIP_AUX_BASE" in helper
    assert "xip_ctrl_hw->stream_addr = (uint32_t)(uintptr_t)page_src;" in helper
    assert "xip_ctrl_hw->stream_ctr = word_count;" in helper
    assert "dma_encode_transfer_count(word_count)" in helper
    assert "STPG_PSRAM_XIP_STREAM_DMA_TIMEOUT_US" in helper
    assert "stpg_psram_stop_read_dma(channel);" in helper
    assert "index < 2u" in drain
    assert "while (" not in drain
    assert "if (!stpg_psram_drain_stream_fifo())" in helper
    dma_args = helper.split("dma_channel_configure(", 1)[1].split(");", 1)[0]
    assert "page_src," not in dma_args


def test_psram_read_path_exclusively_owns_the_xip_stream_engine():
    stream_users = []
    for path in sorted(PSRAM_SOURCE.parent.glob("*.c")):
        text = path.read_text()
        if "xip_ctrl_hw->stream_addr" in text or "xip_ctrl_hw->stream_ctr" in text:
            stream_users.append(path.name)

    assert stream_users == ["psram.c"]


def test_psram_stream_dma_threshold_is_pinned_in_the_firmware_build_contract():
    cmake = FIRMWARE_CMAKE.read_text()

    assert "set(STPG_PSRAM_XIP_STREAM_DMA_MIN_BYTES 256 CACHE STRING" in cmake
    assert "STPG_PSRAM_XIP_STREAM_DMA_MIN_BYTES must be 32-bit aligned" in cmake
    assert "STPG_PSRAM_XIP_STREAM_DMA_MIN_BYTES=${STPG_PSRAM_XIP_STREAM_DMA_MIN_BYTES}" in cmake


def test_stream_cache_validates_records_before_publishing_the_window():
    source = (PSRAM_SOURCE.parent / "pattern_buffer.c").read_text()
    prefetch_start = source.index("stpg_buffer_stream_prefetch_vectors_into")
    prefetch_end = source.index("stpg_buffer_stream_prefetch_vectors_limited", prefetch_start)
    prefetch_body = source[prefetch_start:prefetch_end]

    assert "STPG_STREAM_VECTOR_CACHE_READ_RETRY_COUNT 2u" in prefetch_body
    assert "stpg_buffer_validate_vector_record_fields" in prefetch_body
    assert "*out_count = 0u;" in prefetch_body
    assert prefetch_body.index("stpg_buffer_validate_vector_record_fields") < prefetch_body.index(
        "*out_count = fetch_count;"
    )
