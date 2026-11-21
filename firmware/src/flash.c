#include "stpg_flash.h"

#include <stddef.h>
#include <string.h>

#include "stpg_common.h"
#include "stpg_crc32.h"
#include "stpg_protocol.h"
#include "stpg_psram.h"

#define STPG_FLASH_PAGE_SIZE   256u
#define STPG_FLASH_SECTOR_SIZE 4096u

/* Core 0 has a 2 KiB reserved stack. FLASH operations are serialized on
 * Core 0, so one 4 KiB workspace owns both wire-format metadata variants.
 * No caller may retain a pointer into this workspace across another FLASH
 * operation; public getters return small summaries or copy into caller-owned
 * persistent storage. */
static union {
    FlashSlotMeta slot;
    StpgBootConfig boot;
} g_flash_work;

static uint32_t stpg_flash_crc_with_zero_field(const void *record, size_t size,
                                               size_t field_offset) {
    static const uint32_t zero = 0u;
    const uint8_t *bytes = (const uint8_t *)record;
    uint32_t crc = stpg_crc32_continue(0u, bytes, field_offset);
    crc = stpg_crc32_continue(crc, &zero, sizeof(zero));
    return stpg_crc32_continue(crc, bytes + field_offset + sizeof(zero),
                               size - field_offset - sizeof(zero));
}

#ifdef PICO_ON_DEVICE
#include "hardware/flash.h"
#include "hardware/regs/addressmap.h"
#include "hardware/sync.h"
#include "pico/flash.h"

static void __not_in_flash_func(stpg_flash_erase_range)(uint32_t flash_offset, size_t count) {
    flash_range_erase(flash_offset, count);
}

static void __not_in_flash_func(stpg_flash_program_page)(uint32_t flash_offset, const uint8_t *page_buf) {
    flash_range_program(flash_offset, page_buf, STPG_FLASH_PAGE_SIZE);
}
#endif

#ifndef PICO_ON_DEVICE
static uint8_t g_flash_bytes[0x400000u];
static bool g_flash_initialized;
static const stpg_board_flash_profile_t g_host_flash_profile = {
    .flash_total_bytes = 0x400000u,
    .flash_fw_end = 0x100000u,
    .boot_config_offset = 0x100000u,
    .boot_config_size = 0x1000u,
    .slot_base = 0x101000u,
    .slot_size = 0x0FF000u,
    .slot_count = 3u,
};
#endif

static bool stpg_flash_profile_is_valid(const stpg_board_flash_profile_t *profile) {
    if (profile == 0) return false;
    if (profile->slot_count == 0u || profile->slot_count > 32u) return false;
    if (profile->slot_size < STPG_FLASH_SLOT_META_BYTES) return false;
    if ((profile->slot_size & (STPG_FLASH_SLOT_META_BYTES - 1u)) != 0u) return false;
    if ((profile->flash_fw_end & (STPG_FLASH_SLOT_META_BYTES - 1u)) != 0u) return false;
    if (profile->slot_base < profile->flash_fw_end) return false;
    if (profile->slot_base > profile->flash_total_bytes) return false;
    if ((uint64_t)profile->slot_size * (uint64_t)profile->slot_count > profile->flash_total_bytes) return false;
    if ((uint64_t)profile->slot_base + ((uint64_t)profile->slot_size * (uint64_t)profile->slot_count) > profile->flash_total_bytes) return false;
    return true;
}

static bool stpg_flash_boot_region_is_valid(const stpg_board_flash_profile_t *profile) {
    if (!stpg_flash_profile_is_valid(profile)) return false;
    if (profile->boot_config_size != STPG_BOOT_CONFIG_BYTES) return false;
    if ((profile->boot_config_offset & (STPG_BOOT_CONFIG_BYTES - 1u)) != 0u) return false;
    if (profile->boot_config_offset < profile->flash_fw_end) return false;
    if (profile->boot_config_offset > profile->flash_total_bytes) return false;
    if (profile->boot_config_size > profile->flash_total_bytes - profile->boot_config_offset) return false;
    if (profile->boot_config_offset + profile->boot_config_size > profile->slot_base) return false;
    return true;
}

void stpg_flash_init(void) {
#ifndef PICO_ON_DEVICE
    if (!g_flash_initialized) {
        memset(g_flash_bytes, 0xFF, sizeof(g_flash_bytes));
        g_flash_initialized = true;
    }
#endif
}

const stpg_board_flash_profile_t *stpg_flash_profile(void) {
#ifdef PICO_ON_DEVICE
    return stpg_board_flash_profile();
#else
    stpg_flash_init();
    return &g_host_flash_profile;
#endif
}

uint32_t stpg_flash_capability_flags(void) {
    const stpg_board_flash_profile_t *profile = stpg_flash_profile();
    if (!stpg_flash_profile_is_valid(profile)) return 0u;
    stpg_psram_init();
    uint32_t flags = CAP_FLASH_SLOTS;
    if (stpg_psram_is_available()) flags |= CAP_UPLOAD_FLASH;
    if (stpg_flash_boot_region_is_valid(profile)) flags |= CAP_HEADLESS_BOOT;
    return flags;
}

static stpg_err_t stpg_flash_read_bytes(uint32_t offset, void *dst, uint32_t size) {
    const stpg_board_flash_profile_t *profile = stpg_flash_profile();
    if (!stpg_flash_profile_is_valid(profile)) return ERR_UNSUPPORTED;
    if (dst == 0) return ERR_BAD_SIZE;
    if (offset > profile->flash_total_bytes) return ERR_BAD_SIZE;
    if (size > profile->flash_total_bytes - offset) return ERR_BAD_SIZE;
#ifdef PICO_ON_DEVICE
    memcpy(dst, (const void *)(uintptr_t)(XIP_BASE + offset), size);
#else
    memcpy(dst, g_flash_bytes + offset, size);
#endif
    return ERR_NONE;
}

static uint32_t stpg_flash_slot_offset(const stpg_board_flash_profile_t *profile, uint32_t slot_id) {
    return profile->slot_base + (slot_id * profile->slot_size);
}

static stpg_err_t stpg_flash_slot_bounds(const stpg_board_flash_profile_t *profile,
                                         uint32_t slot_id,
                                         uint32_t offset_within_slot,
                                         uint32_t size) {
    if (!stpg_flash_profile_is_valid(profile)) return ERR_UNSUPPORTED;
    if (slot_id >= profile->slot_count) return ERR_BAD_SIZE;
    if (offset_within_slot > profile->slot_size) return ERR_FLASH_PROTECTED;
    if (size > profile->slot_size - offset_within_slot) return ERR_FLASH_PROTECTED;
    return ERR_NONE;
}

static bool stpg_flash_bytes_all_ff(const uint8_t *data, uint32_t size) {
    for (uint32_t i = 0; i < size; ++i) {
        if (data[i] != 0xFFu) return false;
    }
    return true;
}

static stpg_err_t stpg_flash_read_boot_config_raw(StpgBootConfig *out_cfg) {
    const stpg_board_flash_profile_t *profile = stpg_flash_profile();
    if (!stpg_flash_boot_region_is_valid(profile)) return ERR_UNSUPPORTED;
    if (out_cfg == 0) return ERR_BAD_SIZE;
    return stpg_flash_read_bytes(profile->boot_config_offset, out_cfg, sizeof(*out_cfg));
}

static bool stpg_flash_boot_config_valid_bits(uint32_t flags) {
    return (flags & ~STPG_BOOT_FLAG_ENABLE_AUTO_RUN) == 0u;
}

static bool stpg_flash_boot_config_is_all_ff(const StpgBootConfig *cfg) {
    return stpg_flash_bytes_all_ff((const uint8_t *)cfg, sizeof(*cfg));
}

static stpg_err_t stpg_flash_validate_boot_config(const StpgBootConfig *cfg, const stpg_board_flash_profile_t *profile) {
    if (cfg->magic != STPG_BOOT_CONFIG_MAGIC) return ERR_FLASH_BAD_META;
    if (stpg_flash_crc_with_zero_field(cfg, sizeof(*cfg), offsetof(StpgBootConfig, config_crc32))
        != cfg->config_crc32) return ERR_FLASH_BAD_META;
    if (cfg->version != STPG_BOOT_CONFIG_VERSION) return ERR_FLASH_BAD_META;
    if (!stpg_flash_boot_config_valid_bits(cfg->boot_flags)) return ERR_FLASH_BAD_META;
    if (cfg->boot_slot_id != STPG_BOOT_SLOT_DISABLED && cfg->boot_slot_id >= profile->slot_count) {
        return ERR_FLASH_BAD_META;
    }
    return ERR_NONE;
}

static stpg_err_t stpg_flash_validate_slot_payload_crc(uint32_t slot_id, const FlashSlotMeta *meta) {
    const stpg_board_flash_profile_t *profile = stpg_flash_profile();
    if (!stpg_flash_profile_is_valid(profile)) return ERR_UNSUPPORTED;
    uint32_t crc = 0u;
    uint8_t scratch[128];
    const uint32_t base = stpg_flash_slot_offset(profile, slot_id) + meta->pattern_offset;
    for (uint32_t cursor = 0u; cursor < meta->pattern_size; ) {
        const uint32_t chunk = (meta->pattern_size - cursor) > sizeof(scratch) ? (uint32_t)sizeof(scratch) : (meta->pattern_size - cursor);
        stpg_err_t err = stpg_flash_read_bytes(base + cursor, scratch, chunk);
        if (err != ERR_NONE) return err;
        crc = stpg_crc32_continue(crc, scratch, chunk);
        cursor += chunk;
    }
    return crc == meta->pattern_crc32 ? ERR_NONE : ERR_FLASH_BAD_META;
}

#ifdef PICO_ON_DEVICE
typedef struct {
    uint32_t flash_offset;
    size_t count;
} stpg_flash_erase_params_t;

typedef struct {
    uint32_t flash_offset;
    const uint8_t *page_buf;
} stpg_flash_program_params_t;

static void __not_in_flash_func(stpg_flash_safe_erase_cb)(void *param) {
    stpg_flash_erase_params_t *p = (stpg_flash_erase_params_t *)param;
    stpg_flash_erase_range(p->flash_offset, p->count);
}

static void __not_in_flash_func(stpg_flash_safe_program_cb)(void *param) {
    stpg_flash_program_params_t *p = (stpg_flash_program_params_t *)param;
    stpg_flash_program_page(p->flash_offset, p->page_buf);
}

static stpg_err_t stpg_flash_safe_execute_erase(uint32_t flash_offset, size_t count) {
    stpg_flash_erase_params_t params = {
        .flash_offset = flash_offset,
        .count = count,
    };
    const int rc = flash_safe_execute(stpg_flash_safe_erase_cb, &params, 30000u);
    return rc == PICO_OK ? ERR_NONE : ERR_FLASH_ERASE_FAILED;
}

static stpg_err_t stpg_flash_safe_execute_program(uint32_t flash_offset, const uint8_t *page_buf) {
    stpg_flash_program_params_t params = {
        .flash_offset = flash_offset,
        .page_buf = page_buf,
    };
    const int rc = flash_safe_execute(stpg_flash_safe_program_cb, &params, 30000u);
    return rc == PICO_OK ? ERR_NONE : ERR_FLASH_WRITE_FAILED;
}
#endif

static stpg_err_t stpg_flash_validate_meta(const FlashSlotMeta *meta, uint32_t slot_size) {
    if (meta->magic != STPG_FLASH_SLOT_MAGIC) return ERR_FLASH_SLOT_EMPTY;
    if (stpg_flash_crc_with_zero_field(meta, sizeof(*meta), offsetof(FlashSlotMeta, meta_crc32))
        != meta->meta_crc32) return ERR_FLASH_BAD_META;
    if (meta->meta_version != STPG_FLASH_SLOT_META_VERSION) return ERR_FLASH_BAD_META;
    if (meta->pattern_offset < STPG_FLASH_SLOT_META_BYTES) return ERR_FLASH_BAD_META;
    if (meta->pattern_size == 0u) return ERR_FLASH_BAD_META;
    if (meta->pattern_offset > slot_size) return ERR_FLASH_BAD_META;
    if (meta->pattern_size > slot_size - meta->pattern_offset) return ERR_FLASH_BAD_META;
    if (meta->format != STPG_PATTERN_FORMAT_RAW_EVENT
        && meta->format != STPG_PATTERN_FORMAT_RATE_VECTOR
        && meta->format != STPG_PATTERN_FORMAT_MACRO_STREAM
        && meta->format != STPG_PATTERN_FORMAT_MACRO_P2) {
        return ERR_FLASH_BAD_META;
    }
    if (meta->active_pins != STPG_ACTIVE_PINS_V01 && meta->active_pins != STPG_ACTIVE_PINS_V11) return ERR_FLASH_BAD_META;
    if (meta->tick_hz != STPG_TICK_HZ) return ERR_FLASH_BAD_META;
    return ERR_NONE;
}

stpg_err_t stpg_flash_get_slot_info(uint32_t slot_id, stpg_flash_slot_info_t *out_info) {
    const stpg_board_flash_profile_t *profile = stpg_flash_profile();
    if (!stpg_flash_profile_is_valid(profile)) return ERR_UNSUPPORTED;
    if (out_info == 0) return ERR_BAD_SIZE;
    if (slot_id >= profile->slot_count) return ERR_BAD_SIZE;
    memset(out_info, 0, sizeof(*out_info));

    FlashSlotMeta *const meta = &g_flash_work.slot;
    stpg_err_t err = stpg_flash_read_bytes(stpg_flash_slot_offset(profile, slot_id), meta, sizeof(*meta));
    if (err != ERR_NONE) return err;
    if (stpg_flash_bytes_all_ff((const uint8_t *)meta, sizeof(*meta))) {
        return ERR_NONE;
    }

    if (meta->magic != STPG_FLASH_SLOT_MAGIC) {
        return ERR_NONE;
    }

    err = stpg_flash_validate_meta(meta, profile->slot_size);
    if (err != ERR_NONE) return err;
    out_info->valid = true;
    out_info->pattern_size = meta->pattern_size;
    out_info->pattern_offset = meta->pattern_offset;
    out_info->format = meta->format;
    out_info->active_pins = meta->active_pins;
    out_info->tick_hz = meta->tick_hz;
    return ERR_NONE;
}

stpg_err_t stpg_flash_get_slot_meta(uint32_t slot_id, FlashSlotMeta *out_meta) {
    if (out_meta == 0 || out_meta == &g_flash_work.slot) return ERR_BAD_SIZE;
    stpg_flash_slot_info_t info;
    stpg_err_t err = stpg_flash_get_slot_info(slot_id, &info);
    if (err != ERR_NONE) return err;
    if (!info.valid) return ERR_FLASH_SLOT_EMPTY;
    memcpy(out_meta, &g_flash_work.slot, sizeof(*out_meta));
    return ERR_NONE;
}

stpg_err_t stpg_flash_get_boot_config(stpg_flash_boot_config_info_t *out_info) {
    const stpg_board_flash_profile_t *profile = stpg_flash_profile();
    if (out_info == 0) return ERR_BAD_SIZE;
    memset(out_info, 0, sizeof(*out_info));
    if (!stpg_flash_boot_region_is_valid(profile)) return ERR_UNSUPPORTED;

    StpgBootConfig *const raw = &g_flash_work.boot;
    stpg_err_t err = stpg_flash_read_boot_config_raw(raw);
    if (err != ERR_NONE) return err;
    if (stpg_flash_boot_config_is_all_ff(raw)) {
        return ERR_NONE;
    }
    if (stpg_flash_validate_boot_config(raw, profile) != ERR_NONE) {
        return ERR_NONE;
    }
    out_info->valid = true;
    out_info->version = raw->version;
    out_info->boot_slot_id = raw->boot_slot_id;
    out_info->boot_flags = raw->boot_flags;
    return ERR_NONE;
}

static stpg_err_t stpg_flash_validate_runnable_slot(uint32_t slot_id) {
    const stpg_board_flash_profile_t *profile = stpg_flash_profile();
    if (!stpg_flash_profile_is_valid(profile)) return ERR_UNSUPPORTED;
    if (slot_id >= profile->slot_count) return ERR_UNSUPPORTED;
    stpg_flash_slot_info_t info;
    stpg_err_t err = stpg_flash_get_slot_info(slot_id, &info);
    if (err != ERR_NONE) return err;
    if (!info.valid) return ERR_FLASH_SLOT_EMPTY;
    return stpg_flash_validate_slot_payload_crc(slot_id, &g_flash_work.slot);
}

stpg_err_t stpg_flash_list_slots(uint32_t *out_valid_bitmap, uint32_t *out_slot_count, uint32_t *out_slot_size) {
    const stpg_board_flash_profile_t *profile = stpg_flash_profile();
    if (!stpg_flash_profile_is_valid(profile)) return ERR_UNSUPPORTED;
    if (out_valid_bitmap == 0 || out_slot_count == 0 || out_slot_size == 0) return ERR_BAD_SIZE;

    uint32_t bitmap = 0u;
    for (uint32_t slot_id = 0; slot_id < profile->slot_count; ++slot_id) {
        stpg_flash_slot_info_t info;
        stpg_err_t err = stpg_flash_get_slot_info(slot_id, &info);
        if (err == ERR_FLASH_BAD_META || err == ERR_FLASH_SLOT_EMPTY) {
            continue;
        }
        if (err != ERR_NONE) return err;
        if (info.valid) {
            bitmap |= (1u << slot_id);
        }
    }
    *out_valid_bitmap = bitmap;
    *out_slot_count = profile->slot_count;
    *out_slot_size = profile->slot_size;
    return ERR_NONE;
}

static stpg_err_t stpg_flash_write_boot_config_raw(uint32_t slot_id, uint32_t boot_flags) {
    const stpg_board_flash_profile_t *profile = stpg_flash_profile();
    if (!stpg_flash_boot_region_is_valid(profile)) return ERR_FLASH_PROTECTED;
    const uint32_t flash_offset = profile->boot_config_offset;
    StpgBootConfig *const copy = &g_flash_work.boot;
    memset(copy, 0xFF, sizeof(*copy));
    copy->magic = STPG_BOOT_CONFIG_MAGIC;
    copy->version = STPG_BOOT_CONFIG_VERSION;
    copy->boot_slot_id = slot_id;
    copy->boot_flags = boot_flags;
    copy->config_crc32 = 0u;
    copy->config_crc32 = stpg_crc32(copy, sizeof(*copy));
    stpg_err_t err = ERR_NONE;
#ifdef PICO_ON_DEVICE
    err = stpg_flash_safe_execute_erase(flash_offset, profile->boot_config_size);
    if (err != ERR_NONE) return err;
    uint8_t page_buf[STPG_FLASH_PAGE_SIZE];
    const uint8_t *input = (const uint8_t *)copy;
    for (uint32_t cursor = 0u; cursor < sizeof(*copy); cursor += STPG_FLASH_PAGE_SIZE) {
        memcpy(page_buf, input + cursor, STPG_FLASH_PAGE_SIZE);
        err = stpg_flash_safe_execute_program(flash_offset + cursor, page_buf);
        if (err != ERR_NONE) return err;
    }
#else
    memset(g_flash_bytes + flash_offset, 0xFF, profile->boot_config_size);
    memcpy(g_flash_bytes + flash_offset, copy, sizeof(*copy));
#endif
    stpg_flash_boot_config_info_t info;
    err = stpg_flash_get_boot_config(&info);
    if (err != ERR_NONE) return ERR_FLASH_WRITE_FAILED;
    if (!info.valid) return ERR_FLASH_WRITE_FAILED;
    if (info.version != STPG_BOOT_CONFIG_VERSION
        || info.boot_slot_id != slot_id || info.boot_flags != boot_flags) return ERR_FLASH_WRITE_FAILED;
    return ERR_NONE;
}

stpg_err_t stpg_flash_set_boot_slot(uint32_t slot_id, uint32_t boot_flags) {
    const stpg_board_flash_profile_t *profile = stpg_flash_profile();
    if (!stpg_flash_boot_region_is_valid(profile)) return ERR_UNSUPPORTED;
    if (!stpg_flash_boot_config_valid_bits(boot_flags)) return ERR_UNSUPPORTED;
    stpg_err_t err = stpg_flash_validate_runnable_slot(slot_id);
    if (err != ERR_NONE) return err;

    return stpg_flash_write_boot_config_raw(slot_id, boot_flags);
}

stpg_err_t stpg_flash_disable_boot_slot(void) {
    const stpg_board_flash_profile_t *profile = stpg_flash_profile();
    if (!stpg_flash_boot_region_is_valid(profile)) return ERR_UNSUPPORTED;
    return stpg_flash_write_boot_config_raw(STPG_BOOT_SLOT_DISABLED, 0u);
}

stpg_err_t stpg_flash_erase_slot(uint32_t slot_id) {
    const stpg_board_flash_profile_t *profile = stpg_flash_profile();
    stpg_err_t err = stpg_flash_slot_bounds(profile, slot_id, 0u, profile != 0 ? profile->slot_size : 0u);
    if (err != ERR_NONE) return err;
    const uint32_t flash_offset = stpg_flash_slot_offset(profile, slot_id);
#ifdef PICO_ON_DEVICE
    err = stpg_flash_safe_execute_erase(flash_offset, profile->slot_size);
    if (err != ERR_NONE) return err;
#else
    memset(g_flash_bytes + flash_offset, 0xFF, profile->slot_size);
#endif
    return ERR_NONE;
}

stpg_err_t stpg_flash_read_slot_payload(uint32_t slot_id, uint32_t payload_offset, void *dst, uint32_t size) {
    const stpg_board_flash_profile_t *profile = stpg_flash_profile();
    if (dst == 0) return ERR_BAD_SIZE;
    if (!stpg_flash_profile_is_valid(profile)) return ERR_UNSUPPORTED;
    stpg_flash_slot_info_t info;
    stpg_err_t err = stpg_flash_get_slot_info(slot_id, &info);
    if (err != ERR_NONE) return err;
    if (!info.valid) return ERR_FLASH_SLOT_EMPTY;
    if (payload_offset > info.pattern_size) return ERR_BAD_SIZE;
    if (size > info.pattern_size - payload_offset) return ERR_BAD_SIZE;
    return stpg_flash_read_bytes(stpg_flash_slot_offset(profile, slot_id)
                                 + info.pattern_offset + payload_offset,
                                 dst, size);
}

stpg_err_t stpg_flash_write_slot_payload_from_psram(uint32_t slot_id, uint32_t pattern_offset,
                                                    uint32_t pattern_size, uint32_t psram_offset,
                                                    uint32_t expected_crc32) {
    const stpg_board_flash_profile_t *profile = stpg_flash_profile();
    stpg_err_t err = stpg_flash_slot_bounds(profile, slot_id, pattern_offset, pattern_size);
    if (err != ERR_NONE) return err;
    const uint32_t slot_offset = stpg_flash_slot_offset(profile, slot_id);
    const uint32_t flash_offset = slot_offset + pattern_offset;
    if ((flash_offset & (STPG_FLASH_PAGE_SIZE - 1u)) != 0u) return ERR_FLASH_PROTECTED;
    uint32_t psram_crc = 0u;
    err = stpg_psram_checksum(psram_offset, pattern_size, &psram_crc);
    if (err != ERR_NONE) return ERR_FLASH_WRITE_FAILED;
    if (psram_crc != expected_crc32) return ERR_FLASH_WRITE_FAILED;
#ifdef PICO_ON_DEVICE
    const uint32_t program_size = (pattern_size + (STPG_FLASH_PAGE_SIZE - 1u)) & ~(STPG_FLASH_PAGE_SIZE - 1u);
    uint8_t page_buf[STPG_FLASH_PAGE_SIZE];
    for (uint32_t cursor = 0u; cursor < program_size; cursor += STPG_FLASH_PAGE_SIZE) {
        memset(page_buf, 0xFF, sizeof(page_buf));
        if (cursor < pattern_size) {
            const uint32_t remaining = pattern_size - cursor;
            const uint32_t chunk = remaining > STPG_FLASH_PAGE_SIZE ? STPG_FLASH_PAGE_SIZE : remaining;
            err = stpg_psram_read(psram_offset + cursor, page_buf, chunk);
            if (err != ERR_NONE) return ERR_FLASH_WRITE_FAILED;
        }
        err = stpg_flash_safe_execute_program(flash_offset + cursor, page_buf);
        if (err != ERR_NONE) return err;
    }
#else
    const uint32_t program_size = (pattern_size + (STPG_FLASH_PAGE_SIZE - 1u)) & ~(STPG_FLASH_PAGE_SIZE - 1u);
    memset(g_flash_bytes + flash_offset, 0xFF, program_size);
    err = stpg_psram_read(psram_offset, g_flash_bytes + flash_offset, pattern_size);
    if (err != ERR_NONE) return ERR_FLASH_WRITE_FAILED;
#endif
    uint32_t verify_crc = 0u;
    uint8_t scratch[128];
    for (uint32_t cursor = 0u; cursor < pattern_size; ) {
        const uint32_t chunk = (pattern_size - cursor) > sizeof(scratch) ? (uint32_t)sizeof(scratch) : (pattern_size - cursor);
        err = stpg_flash_read_bytes(flash_offset + cursor, scratch, chunk);
        if (err != ERR_NONE) return ERR_FLASH_WRITE_FAILED;
        verify_crc = stpg_crc32_continue(verify_crc, scratch, chunk);
        cursor += chunk;
    }
    return verify_crc == expected_crc32 ? ERR_NONE : ERR_FLASH_WRITE_FAILED;
}

stpg_err_t stpg_flash_write_slot_meta(uint32_t slot_id, const FlashSlotMeta *meta) {
    const stpg_board_flash_profile_t *profile = stpg_flash_profile();
    if (meta == 0) return ERR_BAD_SIZE;
    stpg_err_t err = stpg_flash_slot_bounds(profile, slot_id, 0u, sizeof(*meta));
    if (err != ERR_NONE) return err;
    FlashSlotMeta *const copy = &g_flash_work.slot;
    memcpy(copy, meta, sizeof(*copy));
    copy->meta_crc32 = 0u;
    copy->meta_crc32 = stpg_crc32(copy, sizeof(*copy));
    const uint32_t flash_offset = stpg_flash_slot_offset(profile, slot_id);
#ifdef PICO_ON_DEVICE
    uint8_t page_buf[STPG_FLASH_PAGE_SIZE];
    const uint8_t *input = (const uint8_t *)copy;
    for (uint32_t cursor = 0u; cursor < sizeof(*copy); cursor += STPG_FLASH_PAGE_SIZE) {
        memcpy(page_buf, input + cursor, STPG_FLASH_PAGE_SIZE);
        err = stpg_flash_safe_execute_program(flash_offset + cursor, page_buf);
        if (err != ERR_NONE) return err;
    }
#else
    memcpy(g_flash_bytes + flash_offset, copy, sizeof(*copy));
#endif
    stpg_flash_slot_info_t info;
    err = stpg_flash_get_slot_info(slot_id, &info);
    if (err != ERR_NONE) return ERR_FLASH_BAD_META;
    if (!info.valid) return ERR_FLASH_BAD_META;
    const size_t crc_offset = offsetof(FlashSlotMeta, meta_crc32);
    const uint8_t *written = (const uint8_t *)&g_flash_work.slot;
    const uint8_t *original = (const uint8_t *)meta;
    if (memcmp(written, original, crc_offset) != 0
        || memcmp(written + crc_offset + sizeof(uint32_t),
                  original + crc_offset + sizeof(uint32_t),
                  sizeof(*meta) - crc_offset - sizeof(uint32_t)) != 0
        || g_flash_work.slot.meta_crc32 != stpg_flash_crc_with_zero_field(
            meta, sizeof(*meta), crc_offset)) return ERR_FLASH_BAD_META;
    return ERR_NONE;
}

#ifndef PICO_ON_DEVICE
void stpg_flash_test_reset(void) {
    memset(g_flash_bytes, 0xFF, sizeof(g_flash_bytes));
    g_flash_initialized = true;
}

stpg_err_t stpg_flash_test_write(uint32_t offset, const void *src, uint32_t size) {
    const stpg_board_flash_profile_t *profile = stpg_flash_profile();
    if (src == 0) return ERR_BAD_SIZE;
    if (offset > profile->flash_total_bytes) return ERR_BAD_SIZE;
    if (size > profile->flash_total_bytes - offset) return ERR_BAD_SIZE;
    memcpy(g_flash_bytes + offset, src, size);
    return ERR_NONE;
}
#endif
