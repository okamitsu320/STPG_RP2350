#include "stpg_psram.h"

#include <string.h>

#include "stpg_app.h"
#include "stpg_crc32.h"

#ifdef PICO_ON_DEVICE
#include "pico/stdlib.h"
#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/flash.h"
#include "hardware/address_mapped.h"
#include "hardware/regs/addressmap.h"
#include "hardware/regs/dreq.h"
#include "hardware/regs/dma.h"
#include "hardware/regs/qmi.h"
#include "hardware/structs/qmi.h"
#include "hardware/structs/pads_bank0.h"
#include "hardware/regs/xip.h"
#include "hardware/structs/xip.h"
#include "hardware/sync.h"
#include "hardware/xip_cache.h"

#define STPG_PSRAM_XIP_NOCACHE_BASE (XIP_NOCACHE_NOALLOC_BASE + 0x01000000u)

static bool g_psram_initialized;
static stpg_psram_write_diag_t g_psram_write_diag;
static uint32_t g_psram_cs1_gpio;
static uint32_t g_psram_max_select;
static uint32_t g_psram_min_deselect;
static int g_psram_read_dma_channel = -1;

/* Bounded production evidence for the selected PSRAM read path.  These are
 * intentionally simple cumulative counters so a read-only GDB snapshot can
 * prove that a candidate used XIP stream DMA rather than a CPU bulk-copy
 * fallback. */
volatile uint32_t g_psram_read_dma_channel_raw = UINT32_MAX;
volatile uint32_t g_psram_read_dma_chunk_count_raw;
volatile uint32_t g_psram_read_dma_byte_count_raw;
volatile uint32_t g_psram_read_dma_error_raw;
volatile uint32_t g_psram_read_cpu_word_chunk_count_raw;
volatile uint32_t g_psram_read_cpu_byte_chunk_count_raw;

#define STPG_PSRAM_VERIFY_RETRY_COUNT 3u
#define STPG_PSRAM_QMI_PAGEBREAK_BYTES STPG_PSRAM_QMI_PAGEBREAK
#define STPG_PSRAM_QUAD_ENABLE_CMD 0x35u
#define STPG_PSRAM_QUAD_READ_CMD 0xEBu
#define STPG_PSRAM_QUAD_WRITE_CMD 0x38u
#define STPG_PSRAM_DIRECT_CLKDIV 10u
#define STPG_PSRAM_MAX_SELECT_NS 8000u
#define STPG_PSRAM_MIN_DESELECT_NS 18u

#ifndef STPG_PSRAM_QMI_CLKDIV
#define STPG_PSRAM_QMI_CLKDIV 2u
#endif

#ifndef STPG_PSRAM_QMI_RXDELAY
#define STPG_PSRAM_QMI_RXDELAY 2u
#endif

#ifndef STPG_PSRAM_QMI_PAGEBREAK
#define STPG_PSRAM_QMI_PAGEBREAK 1024u
#endif

#ifndef STPG_PSRAM_XIP_STREAM_DMA_MIN_BYTES
#define STPG_PSRAM_XIP_STREAM_DMA_MIN_BYTES 256u
#endif

#define STPG_PSRAM_XIP_STREAM_DMA_TIMEOUT_US 100000u

#if STPG_PSRAM_XIP_STREAM_DMA_MIN_BYTES < 4u || \
    STPG_PSRAM_XIP_STREAM_DMA_MIN_BYTES > STPG_PSRAM_QMI_PAGEBREAK || \
    (STPG_PSRAM_XIP_STREAM_DMA_MIN_BYTES & 3u) != 0u
#error "STPG_PSRAM_XIP_STREAM_DMA_MIN_BYTES must be aligned and fit one QMI page"
#endif

#if STPG_PSRAM_QMI_PAGEBREAK == 256u
#define STPG_PSRAM_QMI_PAGEBREAK_VALUE QMI_M1_TIMING_PAGEBREAK_VALUE_256
#elif STPG_PSRAM_QMI_PAGEBREAK == 1024u
#define STPG_PSRAM_QMI_PAGEBREAK_VALUE QMI_M1_TIMING_PAGEBREAK_VALUE_1024
#elif STPG_PSRAM_QMI_PAGEBREAK == 4096u
#define STPG_PSRAM_QMI_PAGEBREAK_VALUE QMI_M1_TIMING_PAGEBREAK_VALUE_4096
#else
#error "STPG_PSRAM_QMI_PAGEBREAK must be 256, 1024, or 4096"
#endif

/* The streaming producer runs from SRAM while QMI M1 is serving PSRAM
 * reads.  Keep the read wrapper in SRAM too; otherwise the producer enters
 * FLASH/XIP for the wrapper itself while the QMI transaction is active. */
#define STPG_PSRAM_READ_FUNC(name) __no_inline_not_in_flash_func(name)

static volatile uint8_t *stpg_psram_ptr(uint32_t offset) {
    return (volatile uint8_t *)(uintptr_t)(STPG_PSRAM_XIP_NOCACHE_BASE + offset);
}

/* GPIO19 is the board-owned QMI CS1 pin, not a DUT pin.  R382 reasserts its
 * mux/pad policy around QMI setup, but the R382 physical snapshot still found
 * PDE set after real PSRAM traffic.  Clear only the two pull-enable bits once
 * a transaction has completed; do not remux the pin or touch any DUT GPIO.
 * Keep this helper in SRAM because the read path itself runs there while QMI
 * streaming is active, and use the atomic register alias rather than calling
 * the flash-resident gpio_set_pulls() implementation. */
static void __no_inline_not_in_flash_func(stpg_psram_clear_cs1_pulls_after_access)(void) {
    if (g_psram_initialized) {
        hw_clear_bits(
            &pads_bank0_hw->io[g_psram_cs1_gpio],
            PADS_BANK0_GPIO0_PUE_BITS | PADS_BANK0_GPIO0_PDE_BITS);
        __dsb();
        __compiler_memory_barrier();
    }
}

static bool __no_inline_not_in_flash_func(stpg_psram_claim_read_dma_channel)(void) {
    if (g_psram_read_dma_channel >= 0) {
        return true;
    }
    const int channel = dma_claim_unused_channel(false);
    if (channel < 0) {
        g_psram_read_dma_error_raw = (uint32_t)ERR_DMA_ERROR;
        return false;
    }
    g_psram_read_dma_channel = channel;
    g_psram_read_dma_channel_raw = (uint32_t)channel;
    dma_channel_set_irq0_enabled((uint)channel, false);
    dma_channel_set_irq1_enabled((uint)channel, false);
    return true;
}

static bool __no_inline_not_in_flash_func(stpg_psram_drain_stream_fifo)(void) {
    /* The hardware FIFO is exactly two words deep.  Once STREAM_CTR is zero,
     * an in-flight read is discarded, so more than two reads would indicate
     * an invalid/stuck state and must not become an unbounded cleanup loop. */
    for (uint32_t index = 0u; index < 2u; ++index) {
        if (xip_ctrl_hw->stat & XIP_STAT_FIFO_EMPTY_BITS) {
            return true;
        }
        (void)xip_ctrl_hw->stream_fifo;
    }
    return (xip_ctrl_hw->stat & XIP_STAT_FIFO_EMPTY_BITS) != 0u;
}

static bool __no_inline_not_in_flash_func(stpg_psram_stop_read_dma)(uint channel) {
    xip_ctrl_hw->stream_ctr = 0u;
    hw_clear_bits(&dma_hw->ch[channel].ctrl_trig, DMA_CH0_CTRL_TRIG_EN_BITS);
    __dmb();
    dma_channel_abort(channel);
    dma_channel_acknowledge_irq0(channel);
    dma_channel_acknowledge_irq1(channel);
    return stpg_psram_drain_stream_fifo();
}

static stpg_err_t __no_inline_not_in_flash_func(stpg_psram_read_page_dma)(
    const volatile uint8_t *page_src,
    uint8_t *page_dst,
    uint32_t chunk) {
    if ((((uintptr_t)page_src | (uintptr_t)page_dst | (uintptr_t)chunk) &
         (sizeof(uint32_t) - 1u)) != 0u || chunk == 0u ||
        chunk > STPG_PSRAM_QMI_PAGEBREAK_BYTES) {
        return ERR_BAD_SIZE;
    }
    if (!stpg_psram_claim_read_dma_channel()) {
        return ERR_DMA_ERROR;
    }

    const uint channel = (uint)g_psram_read_dma_channel;
    if (dma_channel_is_busy(channel)) {
        g_psram_read_dma_error_raw = (uint32_t)ERR_DMA_ERROR;
        return ERR_DMA_ERROR;
    }

    xip_ctrl_hw->stream_ctr = 0u;
    __dmb();
    if (!stpg_psram_drain_stream_fifo()) {
        g_psram_read_dma_error_raw = (uint32_t)ERR_DMA_ERROR;
        return ERR_DMA_ERROR;
    }

    const uint32_t word_count = chunk / sizeof(uint32_t);
    dma_channel_config config = dma_channel_get_default_config(channel);
    channel_config_set_transfer_data_size(&config, DMA_SIZE_32);
    channel_config_set_read_increment(&config, false);
    channel_config_set_write_increment(&config, true);
    channel_config_set_dreq(&config, DREQ_XIP_STREAM);
    channel_config_set_irq_quiet(&config, true);
    channel_config_set_high_priority(&config, false);
    dma_channel_configure(
        channel,
        &config,
        page_dst,
        (const volatile void *)(uintptr_t)XIP_AUX_BASE,
        dma_encode_transfer_count(word_count),
        false);

    /* STREAM_ADDR accepts the full XIP alias, including the translated M1
     * chip-select window.  Start the background producer before the DMA: the
     * two-word FIFO may fill, then DREQ resumes it as DMA drains each word. */
    xip_ctrl_hw->stream_addr = (uint32_t)(uintptr_t)page_src;
    xip_ctrl_hw->stream_ctr = word_count;
    __dmb();
    const uint32_t start_us = time_us_32();
    dma_start_channel_mask(1u << channel);
    while (dma_channel_is_busy(channel)) {
        if ((uint32_t)(time_us_32() - start_us) >= STPG_PSRAM_XIP_STREAM_DMA_TIMEOUT_US) {
            stpg_psram_stop_read_dma(channel);
            g_psram_read_dma_error_raw = (uint32_t)ERR_TIMEOUT;
            return ERR_TIMEOUT;
        }
        tight_loop_contents();
    }

    __dsb();
    __compiler_memory_barrier();
    const uint32_t remaining =
        dma_hw->ch[channel].transfer_count & DMA_CH0_TRANS_COUNT_COUNT_BITS;
    if (remaining != 0u || xip_ctrl_hw->stream_ctr != 0u ||
        !(xip_ctrl_hw->stat & XIP_STAT_FIFO_EMPTY_BITS)) {
        stpg_psram_stop_read_dma(channel);
        g_psram_read_dma_error_raw = (uint32_t)ERR_DMA_ERROR;
        return ERR_DMA_ERROR;
    }

    g_psram_read_dma_chunk_count_raw += 1u;
    g_psram_read_dma_byte_count_raw += chunk;
    g_psram_read_dma_error_raw = (uint32_t)ERR_NONE;
    return ERR_NONE;
}

static void stpg_psram_sync_range(uint32_t offset, uint32_t size) {
    (void)offset;
    (void)size;
    xip_cache_clean_all();
}

static void stpg_psram_qmi_select_timing(uint32_t *max_select, uint32_t *min_deselect) {
    const uint32_t clock_hz = clock_get_hz(clk_sys);
    const uint32_t clock_period_fs = 1000000000000000ull / clock_hz;
    *max_select = (uint32_t)(((uint64_t)STPG_PSRAM_MAX_SELECT_NS * 1000000ull) /
        (64ull * (uint64_t)clock_period_fs));
    *min_deselect = (STPG_PSRAM_MIN_DESELECT_NS * 1000000u + (clock_period_fs - 1u)) /
        clock_period_fs - (STPG_PSRAM_QMI_CLKDIV + 1u) / 2u;
}

static void __no_inline_not_in_flash_func(stpg_psram_configure_qmi)(
    uint32_t max_select, uint32_t min_deselect) {
    // APS6404 enters quad mode through a single-line 35h command.
    qmi_hw->direct_csr = (STPG_PSRAM_DIRECT_CLKDIV << QMI_DIRECT_CSR_CLKDIV_LSB) |
        QMI_DIRECT_CSR_EN_BITS |
        QMI_DIRECT_CSR_AUTO_CS1N_BITS;
    while (qmi_hw->direct_csr & QMI_DIRECT_CSR_BUSY_BITS) {
        tight_loop_contents();
    }
    qmi_hw->direct_tx = STPG_PSRAM_QUAD_ENABLE_CMD;
    while (qmi_hw->direct_csr & QMI_DIRECT_CSR_BUSY_BITS) {
        tight_loop_contents();
    }
    qmi_hw->direct_csr = 0u;
    __compiler_memory_barrier();

    // Match the Pico SDK APS6404 configuration, with an explicit compile-time
    // clock divisor.  With the default CLKDIV=2, QMI SCK is clk_sys / 2:
    // 75 MHz at 150 MHz and 100 MHz in the experimental 200 MHz profile.
    // M1 is independent from the Flash M0 timing register.
    qmi_hw->m[1].timing =
        1u << QMI_M1_TIMING_COOLDOWN_LSB |
        STPG_PSRAM_QMI_PAGEBREAK_VALUE << QMI_M1_TIMING_PAGEBREAK_LSB |
        max_select << QMI_M1_TIMING_MAX_SELECT_LSB |
        min_deselect << QMI_M1_TIMING_MIN_DESELECT_LSB |
        STPG_PSRAM_QMI_RXDELAY << QMI_M1_TIMING_RXDELAY_LSB |
        STPG_PSRAM_QMI_CLKDIV << QMI_M1_TIMING_CLKDIV_LSB;

    qmi_hw->m[1].rfmt =
        QMI_M1_RFMT_PREFIX_WIDTH_VALUE_Q << QMI_M1_RFMT_PREFIX_WIDTH_LSB |
        QMI_M1_RFMT_ADDR_WIDTH_VALUE_Q << QMI_M1_RFMT_ADDR_WIDTH_LSB |
        QMI_M1_RFMT_SUFFIX_WIDTH_VALUE_Q << QMI_M1_RFMT_SUFFIX_WIDTH_LSB |
        QMI_M1_RFMT_DUMMY_WIDTH_VALUE_Q << QMI_M1_RFMT_DUMMY_WIDTH_LSB |
        QMI_M1_RFMT_DATA_WIDTH_VALUE_Q << QMI_M1_RFMT_DATA_WIDTH_LSB |
        QMI_M1_RFMT_PREFIX_LEN_VALUE_8 << QMI_M1_RFMT_PREFIX_LEN_LSB |
        QMI_M1_RFMT_DUMMY_LEN_VALUE_24 << QMI_M1_RFMT_DUMMY_LEN_LSB |
        QMI_M1_RFMT_SUFFIX_LEN_VALUE_NONE << QMI_M1_RFMT_SUFFIX_LEN_LSB;
    qmi_hw->m[1].rcmd = STPG_PSRAM_QUAD_READ_CMD << QMI_M1_RCMD_PREFIX_LSB;

    qmi_hw->m[1].wfmt =
        QMI_M1_WFMT_PREFIX_WIDTH_VALUE_Q << QMI_M1_WFMT_PREFIX_WIDTH_LSB |
        QMI_M1_WFMT_ADDR_WIDTH_VALUE_Q << QMI_M1_WFMT_ADDR_WIDTH_LSB |
        QMI_M1_WFMT_SUFFIX_WIDTH_VALUE_Q << QMI_M1_WFMT_SUFFIX_WIDTH_LSB |
        QMI_M1_WFMT_DUMMY_WIDTH_VALUE_Q << QMI_M1_WFMT_DUMMY_WIDTH_LSB |
        QMI_M1_WFMT_DATA_WIDTH_VALUE_Q << QMI_M1_WFMT_DATA_WIDTH_LSB |
        QMI_M1_WFMT_PREFIX_LEN_VALUE_8 << QMI_M1_WFMT_PREFIX_LEN_LSB |
        QMI_M1_WFMT_DUMMY_LEN_VALUE_NONE << QMI_M1_WFMT_DUMMY_LEN_LSB |
        QMI_M1_WFMT_SUFFIX_LEN_VALUE_NONE << QMI_M1_WFMT_SUFFIX_LEN_LSB;
    qmi_hw->m[1].wcmd = STPG_PSRAM_QUAD_WRITE_CMD << QMI_M1_WCMD_PREFIX_LSB;
    __compiler_memory_barrier();
}

static bool __no_inline_not_in_flash_func(stpg_psram_qmi_configuration_matches)(
    uint32_t max_select, uint32_t min_deselect) {
    const uint32_t expected_timing =
        (1u << QMI_M1_TIMING_COOLDOWN_LSB) |
        (STPG_PSRAM_QMI_PAGEBREAK_VALUE << QMI_M1_TIMING_PAGEBREAK_LSB) |
        ((max_select << QMI_M1_TIMING_MAX_SELECT_LSB) & QMI_M1_TIMING_MAX_SELECT_BITS) |
        ((min_deselect << QMI_M1_TIMING_MIN_DESELECT_LSB) & QMI_M1_TIMING_MIN_DESELECT_BITS) |
        (STPG_PSRAM_QMI_RXDELAY << QMI_M1_TIMING_RXDELAY_LSB) |
        STPG_PSRAM_QMI_CLKDIV;
    const uint32_t expected_rfmt =
        QMI_M1_RFMT_PREFIX_WIDTH_VALUE_Q << QMI_M1_RFMT_PREFIX_WIDTH_LSB |
        QMI_M1_RFMT_ADDR_WIDTH_VALUE_Q << QMI_M1_RFMT_ADDR_WIDTH_LSB |
        QMI_M1_RFMT_SUFFIX_WIDTH_VALUE_Q << QMI_M1_RFMT_SUFFIX_WIDTH_LSB |
        QMI_M1_RFMT_DUMMY_WIDTH_VALUE_Q << QMI_M1_RFMT_DUMMY_WIDTH_LSB |
        QMI_M1_RFMT_DATA_WIDTH_VALUE_Q << QMI_M1_RFMT_DATA_WIDTH_LSB |
        QMI_M1_RFMT_PREFIX_LEN_VALUE_8 << QMI_M1_RFMT_PREFIX_LEN_LSB |
        QMI_M1_RFMT_DUMMY_LEN_VALUE_24 << QMI_M1_RFMT_DUMMY_LEN_LSB |
        QMI_M1_RFMT_SUFFIX_LEN_VALUE_NONE << QMI_M1_RFMT_SUFFIX_LEN_LSB;
    const uint32_t expected_wfmt =
        QMI_M1_WFMT_PREFIX_WIDTH_VALUE_Q << QMI_M1_WFMT_PREFIX_WIDTH_LSB |
        QMI_M1_WFMT_ADDR_WIDTH_VALUE_Q << QMI_M1_WFMT_ADDR_WIDTH_LSB |
        QMI_M1_WFMT_SUFFIX_WIDTH_VALUE_Q << QMI_M1_WFMT_SUFFIX_WIDTH_LSB |
        QMI_M1_WFMT_DUMMY_WIDTH_VALUE_Q << QMI_M1_WFMT_DUMMY_WIDTH_LSB |
        QMI_M1_WFMT_DATA_WIDTH_VALUE_Q << QMI_M1_WFMT_DATA_WIDTH_LSB |
        QMI_M1_WFMT_PREFIX_LEN_VALUE_8 << QMI_M1_WFMT_PREFIX_LEN_LSB |
        QMI_M1_WFMT_DUMMY_LEN_VALUE_NONE << QMI_M1_WFMT_DUMMY_LEN_LSB |
        QMI_M1_WFMT_SUFFIX_LEN_VALUE_NONE << QMI_M1_WFMT_SUFFIX_LEN_LSB;

    return (qmi_hw->m[1].timing & QMI_M1_TIMING_BITS) == expected_timing &&
        (qmi_hw->m[1].rfmt & QMI_M1_RFMT_BITS) == expected_rfmt &&
        (qmi_hw->m[1].rcmd & 0xFFu) == STPG_PSRAM_QUAD_READ_CMD &&
        (qmi_hw->m[1].wfmt & QMI_M1_WFMT_BITS) == expected_wfmt &&
        (qmi_hw->m[1].wcmd & 0xFFu) == STPG_PSRAM_QUAD_WRITE_CMD;
}

static bool __no_inline_not_in_flash_func(stpg_psram_reapply_qmi)(void) {
    stpg_psram_configure_qmi(g_psram_max_select, g_psram_min_deselect);
    return stpg_psram_qmi_configuration_matches(g_psram_max_select, g_psram_min_deselect);
}

static bool stpg_psram_self_test(void) {
    enum { TEST_SIZE = 32 };
    uint8_t original[TEST_SIZE];
    uint8_t pattern[TEST_SIZE];
    uint8_t readback[TEST_SIZE];
    volatile uint8_t *base = stpg_psram_ptr(STPG_PSRAM_STAGING_BASE);

    for (uint32_t i = 0; i < TEST_SIZE; ++i) {
        original[i] = base[i];
        pattern[i] = (uint8_t)(0xA5u ^ (uint8_t)(i * 17u));
    }

    for (uint32_t i = 0; i < TEST_SIZE; ++i) {
        base[i] = pattern[i];
    }
    __compiler_memory_barrier();
    stpg_psram_sync_range(STPG_PSRAM_STAGING_BASE, TEST_SIZE);
    __compiler_memory_barrier();

    for (uint32_t i = 0; i < TEST_SIZE; ++i) {
        readback[i] = base[i];
    }

    for (uint32_t i = 0; i < TEST_SIZE; ++i) {
        base[i] = original[i];
    }
    __compiler_memory_barrier();
    stpg_psram_sync_range(STPG_PSRAM_STAGING_BASE, TEST_SIZE);
    __compiler_memory_barrier();

    return memcmp(pattern, readback, TEST_SIZE) == 0;
}

void stpg_psram_init(void) {
    hw_set_bits(&xip_ctrl_hw->ctrl, XIP_CTRL_WRITABLE_M1_BITS);
    if (g_psram_initialized) {
        /*
         * A completed DUT RUN returns the GPIO window to its safe SIO state.
         * That window includes candidate CS1 pins, so the next PSRAM upload
         * must restore the accepted pin to XIP_CS1.  Do not call the SDK CS1
         * setters here: they also rewrite flash-device metadata.  Restore
         * only the GPIO mux, then independently validate the QMI M1 contract.
         */
        stpg_board_psram_restore_cs1_mux(g_psram_cs1_gpio);
        if (!stpg_psram_qmi_configuration_matches(g_psram_max_select, g_psram_min_deselect)) {
            (void)stpg_psram_reapply_qmi();
            /* QMI reconfiguration is a later pad/peripheral handoff than the
             * mux setup above. Reassert the board-owned CS1 pad invariant
             * after it, without including GPIO19 in the DUT RESET/STOP loop. */
            stpg_board_psram_restore_cs1_mux(g_psram_cs1_gpio);
        }
        return;
    }
    const uint32_t *candidates = 0;
    size_t count = 0u;
    candidates = stpg_board_psram_cs1_candidates(&count);
    if (candidates == 0 || count == 0u) {
        g_psram_initialized = false;
        return;
    }
    for (size_t i = 0; i < count; ++i) {
        const uint32_t gpio = candidates[i];
        stpg_board_psram_configure_cs1(gpio);
        if (flash_devinfo_get_cs_size(1u) != FLASH_DEVINFO_SIZE_8M) {
            stpg_board_psram_restore_cs1_mux(gpio);
            continue;
        }
        uint32_t max_select = 0u;
        uint32_t min_deselect = 0u;
        stpg_psram_qmi_select_timing(&max_select, &min_deselect);
        stpg_psram_configure_qmi(max_select, min_deselect);
        const bool qmi_matches =
            stpg_psram_qmi_configuration_matches(max_select, min_deselect);
        const bool self_test_passed = qmi_matches && stpg_psram_self_test();
        /* The first-time QMI setup and its PSRAM self-test happen after the
         * CS1 mux was selected. Restore XIP_CS1 and no-pull state after those
         * operations too; checking only at the mux handoff missed this final
         * initialization boundary in the R381 physical snapshot. */
        stpg_board_psram_restore_cs1_mux(gpio);
        if (self_test_passed) {
            g_psram_cs1_gpio = gpio;
            g_psram_max_select = max_select;
            g_psram_min_deselect = min_deselect;
            g_psram_initialized = true;
            return;
        }
    }
    g_psram_initialized = false;
}

bool stpg_psram_is_available(void) {
    return g_psram_initialized;
}

bool stpg_psram_get_selected_cs1_gpio(uint32_t *out_gpio) {
    if (!g_psram_initialized || out_gpio == 0) {
        return false;
    }
    *out_gpio = g_psram_cs1_gpio;
    return true;
}

uint32_t stpg_psram_get_selected_cs1_function(void) {
    if (!g_psram_initialized) {
        return UINT32_MAX;
    }
    return (uint32_t)gpio_get_function(g_psram_cs1_gpio);
}

bool stpg_psram_selected_cs1_mux_matches(void) {
    return g_psram_initialized
        && gpio_get_function(g_psram_cs1_gpio) == GPIO_FUNC_XIP_CS1;
}

stpg_err_t stpg_psram_prepare_access(void) {
    stpg_psram_init();
    if (!g_psram_initialized) {
        return ERR_PSRAM_UNAVAILABLE;
    }
    if (!stpg_psram_qmi_configuration_matches(g_psram_max_select, g_psram_min_deselect)) {
        const bool reapplied = stpg_psram_reapply_qmi();
        /* Keep the mux/pad contract after the optional QMI reconfiguration,
         * including the failure path, before any caller uses PSRAM again. */
        stpg_board_psram_restore_cs1_mux(g_psram_cs1_gpio);
        if (!reapplied) {
            return ERR_PSRAM_UNAVAILABLE;
        }
    }
    return ERR_NONE;
}

void stpg_psram_clear_write_diag(void) {
    memset(&g_psram_write_diag, 0, sizeof(g_psram_write_diag));
}

stpg_psram_write_diag_t stpg_psram_get_write_diag(void) {
    return g_psram_write_diag;
}

static stpg_err_t stpg_psram_check_bounds(uint32_t offset, uint32_t size) {
    if (offset > STPG_PSRAM_TOTAL_BYTES) {
        return ERR_PSRAM_BOUNDS;
    }
    if (size > STPG_PSRAM_TOTAL_BYTES - offset) {
        return ERR_PSRAM_BOUNDS;
    }
    return ERR_NONE;
}

void *stpg_psram_mapped_ptr(uint32_t offset, uint32_t size) {
    if (stpg_psram_check_bounds(offset, size) != ERR_NONE) {
        return 0;
    }
    return (void *)(uintptr_t)stpg_psram_ptr(offset);
}

stpg_err_t STPG_PSRAM_READ_FUNC(stpg_psram_read)(uint32_t offset, void *dst, uint32_t size) {
    stpg_err_t err = stpg_psram_check_bounds(offset, size);
    if (err != ERR_NONE) {
        return err;
    }
    if (size == 0u) {
        return ERR_NONE;
    }
    if (dst == 0) {
        return ERR_BAD_SIZE;
    }
    /*
     * Do not let libc turn a PSRAM XIP read into a load/burst sequence that
     * crosses the configured QMI M1 page break.  Rebase the volatile source
     * for every page.  Large aligned chunks use the XIP streaming FIFO and a
     * dedicated DMA channel, so QMI can chain reads without stalling the DMA
     * fabric on direct mapped-memory accesses.  Small aligned reads stay on
     * the CPU word path to avoid DMA setup cost; the byte fallback preserves
     * the complete general API for unaligned offsets, destinations, and tails.
     *
     * This is intentionally local to the on-device path; the host model
     * continues to use memcpy below.
     */
    uint8_t *out = (uint8_t *)dst;
    uint32_t cursor = 0u;
    while (cursor < size) {
        const uint32_t absolute = offset + cursor;
        const uint32_t page_offset = absolute % STPG_PSRAM_QMI_PAGEBREAK_BYTES;
        const uint32_t page_remaining = STPG_PSRAM_QMI_PAGEBREAK_BYTES - page_offset;
        const uint32_t chunk = (size - cursor) < page_remaining ? (size - cursor) : page_remaining;
        /* Rebase the mapped source for every QMI page.  Keeping one source
         * pointer and merely limiting the loop length prevents a C-level
         * overrun, but it does not force the RP2350 QMI window to start a new
         * transaction at the next page. */
        const volatile uint8_t *page_src = stpg_psram_ptr(absolute);
        uint8_t *const page_dst = out + cursor;
        const bool word_aligned =
            (((uintptr_t)page_src | (uintptr_t)page_dst | (uintptr_t)chunk) &
             (sizeof(uint32_t) - 1u)) == 0u;
        if (word_aligned && chunk >= STPG_PSRAM_XIP_STREAM_DMA_MIN_BYTES) {
            err = stpg_psram_read_page_dma(page_src, page_dst, chunk);
            if (err != ERR_NONE) {
                break;
            }
        } else if (word_aligned) {
            g_psram_read_cpu_word_chunk_count_raw += 1u;
            const volatile uint32_t *const page_words =
                (const volatile uint32_t *)(const volatile void *)page_src;
            const uint32_t word_count = chunk / sizeof(uint32_t);
            for (uint32_t index = 0u; index < word_count; ++index) {
                const uint32_t word = page_words[index];
                memcpy(page_dst + (index * sizeof(uint32_t)), &word, sizeof(word));
            }
        } else {
            g_psram_read_cpu_byte_chunk_count_raw += 1u;
            for (uint32_t index = 0u; index < chunk; ++index) {
                page_dst[index] = page_src[index];
            }
        }
        /* Complete every page-bounded QMI read before changing the source
         * address.  A compiler barrier alone does not wait for the bus. */
        __dsb();
        __compiler_memory_barrier();
        cursor += chunk;
    }
    stpg_psram_clear_cs1_pulls_after_access();
    return err;
}

stpg_err_t stpg_psram_write(uint32_t offset, const void *src, uint32_t size) {
    stpg_psram_clear_write_diag();
    stpg_err_t err = stpg_psram_check_bounds(offset, size);
    if (err != ERR_NONE) {
        return err;
    }
    if (size == 0u) {
        return ERR_NONE;
    }
    if (src == 0) {
        return ERR_BAD_SIZE;
    }
    const uint8_t *in = (const uint8_t *)src;
    volatile uint8_t *dst = stpg_psram_ptr(offset);

    stpg_err_t result = ERR_BAD_CRC;
    for (uint32_t attempt = 0u; attempt < STPG_PSRAM_VERIFY_RETRY_COUNT; ++attempt) {
        for (uint32_t i = 0; i < size; ++i) {
            dst[i] = in[i];
        }
        __compiler_memory_barrier();
        stpg_psram_sync_range(offset, size);
        __compiler_memory_barrier();

        bool verified = true;
        for (uint32_t i = 0; i < size; ++i) {
            if (dst[i] != in[i]) {
                verified = false;
                g_psram_write_diag.offset = offset;
                g_psram_write_diag.size = size;
                g_psram_write_diag.attempt = attempt + 1u;
                g_psram_write_diag.mismatch_index = i;
                g_psram_write_diag.expected = in[i];
                g_psram_write_diag.actual = dst[i];
                g_psram_write_diag.valid = true;
                break;
            }
        }
        if (verified) {
            result = ERR_NONE;
            break;
        }
    }
    stpg_psram_clear_cs1_pulls_after_access();
    return result;
}

stpg_err_t stpg_psram_fill(uint32_t offset, uint8_t value, uint32_t size) {
    stpg_err_t err = stpg_psram_check_bounds(offset, size);
    if (err != ERR_NONE) {
        return err;
    }
    volatile uint8_t *dst = stpg_psram_ptr(offset);
    for (uint32_t i = 0; i < size; ++i) {
        dst[i] = value;
    }
    __compiler_memory_barrier();
    stpg_psram_sync_range(offset, size);
    __compiler_memory_barrier();
    stpg_psram_clear_cs1_pulls_after_access();
    return ERR_NONE;
}

stpg_err_t stpg_psram_checksum(uint32_t offset, uint32_t size, uint32_t *out_crc32) {
    stpg_err_t err = stpg_psram_check_bounds(offset, size);
    if (err != ERR_NONE) {
        return err;
    }
    if (out_crc32 == 0) {
        return ERR_BAD_SIZE;
    }
    uint8_t scratch[128];
    uint32_t cursor = 0u;
    uint32_t crc = 0u;
    while (cursor < size) {
        const uint32_t chunk = (size - cursor) > sizeof(scratch) ? (uint32_t)sizeof(scratch) : (size - cursor);
        err = stpg_psram_read(offset + cursor, scratch, chunk);
        if (err != ERR_NONE) {
            return err;
        }
        crc = stpg_crc32_continue(crc, scratch, chunk);
        cursor += chunk;
    }
    *out_crc32 = crc;
    return ERR_NONE;
}

#else

#define STPG_PSRAM_READ_FUNC(name) name

static uint8_t g_psram_bytes[STPG_PSRAM_TOTAL_BYTES];
static bool g_psram_initialized;
static stpg_psram_write_diag_t g_psram_write_diag;
static uint32_t g_psram_cs1_gpio = 19u;
static stpg_psram_host_read_stats_t g_psram_host_read_stats;

void stpg_psram_host_reset_read_stats(void) {
    memset(&g_psram_host_read_stats, 0, sizeof(g_psram_host_read_stats));
}

stpg_psram_host_read_stats_t stpg_psram_host_get_read_stats(void) {
    return g_psram_host_read_stats;
}

void stpg_psram_init(void) {
    g_psram_initialized = true;
}

bool stpg_psram_is_available(void) {
    return g_psram_initialized;
}

bool stpg_psram_get_selected_cs1_gpio(uint32_t *out_gpio) {
    if (!g_psram_initialized || out_gpio == 0) {
        return false;
    }
    *out_gpio = g_psram_cs1_gpio;
    return true;
}

uint32_t stpg_psram_get_selected_cs1_function(void) {
    return g_psram_initialized ? 0u : UINT32_MAX;
}

bool stpg_psram_selected_cs1_mux_matches(void) {
    return g_psram_initialized;
}

stpg_err_t stpg_psram_prepare_access(void) {
    stpg_psram_init();
    return g_psram_initialized ? ERR_NONE : ERR_PSRAM_UNAVAILABLE;
}

void stpg_psram_clear_write_diag(void) {
    memset(&g_psram_write_diag, 0, sizeof(g_psram_write_diag));
}

stpg_psram_write_diag_t stpg_psram_get_write_diag(void) {
    return g_psram_write_diag;
}

static stpg_err_t stpg_psram_check_bounds(uint32_t offset, uint32_t size) {
    if (offset > STPG_PSRAM_TOTAL_BYTES) {
        return ERR_PSRAM_BOUNDS;
    }
    if (size > STPG_PSRAM_TOTAL_BYTES - offset) {
        return ERR_PSRAM_BOUNDS;
    }
    return ERR_NONE;
}

void *stpg_psram_mapped_ptr(uint32_t offset, uint32_t size) {
    if (stpg_psram_check_bounds(offset, size) != ERR_NONE) {
        return 0;
    }
    return (void *)(g_psram_bytes + offset);
}

stpg_err_t STPG_PSRAM_READ_FUNC(stpg_psram_read)(uint32_t offset, void *dst, uint32_t size) {
    g_psram_host_read_stats.read_calls += 1u;
    stpg_err_t err = stpg_psram_check_bounds(offset, size);
    if (err != ERR_NONE) {
        return err;
    }
    if (size == 0u) {
        return ERR_NONE;
    }
    if (dst == 0) {
        return ERR_BAD_SIZE;
    }
    memcpy(dst, g_psram_bytes + offset, size);
    return ERR_NONE;
}

stpg_err_t stpg_psram_write(uint32_t offset, const void *src, uint32_t size) {
    stpg_psram_clear_write_diag();
    stpg_err_t err = stpg_psram_check_bounds(offset, size);
    if (err != ERR_NONE) {
        return err;
    }
    if (size == 0u) {
        return ERR_NONE;
    }
    if (src == 0) {
        return ERR_BAD_SIZE;
    }
    memcpy(g_psram_bytes + offset, src, size);
    return ERR_NONE;
}

stpg_err_t stpg_psram_fill(uint32_t offset, uint8_t value, uint32_t size) {
    stpg_err_t err = stpg_psram_check_bounds(offset, size);
    if (err != ERR_NONE) {
        return err;
    }
    memset(g_psram_bytes + offset, value, size);
    return ERR_NONE;
}

stpg_err_t stpg_psram_checksum(uint32_t offset, uint32_t size, uint32_t *out_crc32) {
    g_psram_host_read_stats.checksum_calls += 1u;
    stpg_err_t err = stpg_psram_check_bounds(offset, size);
    if (err != ERR_NONE) {
        return err;
    }
    if (out_crc32 == 0) {
        return ERR_BAD_SIZE;
    }
    *out_crc32 = stpg_crc32(g_psram_bytes + offset, size);
    return ERR_NONE;
}

#endif
