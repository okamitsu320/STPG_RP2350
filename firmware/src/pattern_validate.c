#include "stpg_pattern_validate.h"

#include <stdbool.h>
#include <string.h>

#include "stpg_board.h"
#include "stpg_common.h"
#include "stpg_crc32.h"

static uint32_t load_u32_le(const void *ptr) {
    uint32_t value;
    memcpy(&value, ptr, sizeof(value));
    return value;
}

static uint32_t load_u24_le(const uint8_t bytes[3]) {
    return (uint32_t)bytes[0] | ((uint32_t)bytes[1] << 8u) | ((uint32_t)bytes[2] << 16u);
}

static stpg_err_t validate_header_fields(const PatternHeader *h, size_t total_size) {
    if (h->magic != STPG_PATTERN_MAGIC) return ERR_BAD_MAGIC;
    if (h->version != STPG_PATTERN_VERSION && h->version != STPG_PATTERN_VERSION_WIDE_WAIT) return ERR_BAD_VERSION;
    if (h->header_size != STPG_HEADER_SIZE) return ERR_PATTERN_INVALID;
    if (h->flags != 0u) return ERR_UNSUPPORTED;
    if (h->active_pins != STPG_ACTIVE_PINS_V01 && h->active_pins != STPG_ACTIVE_PINS_V11) return ERR_UNSUPPORTED;
    if (h->tick_hz != STPG_TICK_HZ) return ERR_UNSUPPORTED;
    const uint32_t expected_event_size = h->version == STPG_PATTERN_VERSION
        ? STPG_EXEC_EVENT_SIZE : STPG_EXEC_EVENT32_SIZE;
    if (h->event_size != expected_event_size) return ERR_PATTERN_INVALID;
    if (h->event_count == 0u || h->event_count > STPG_MAX_EVENT_COUNT) return ERR_PATTERN_INVALID;
    if (h->data_size != h->event_size * h->event_count) return ERR_PATTERN_INVALID;
    if ((size_t)h->data_size + STPG_HEADER_SIZE != total_size) return ERR_BAD_SIZE;
    return ERR_NONE;
}

static stpg_err_t validate_events(const uint8_t *event_data, uint32_t event_count, uint32_t active_pins,
                                  uint16_t version, uint32_t event_size) {
    const uint32_t pin_mask = active_pins == STPG_ACTIVE_PINS_V11 ? 0x00FFFFFFu : 0x0000FFFFu;
    for (uint32_t i = 0; i < event_count; ++i) {
        uint16_t flags;
        uint32_t out_value;
        uint32_t out_enable;
        uint32_t sample_mask;
        uint32_t expect_value;
        if (version == STPG_PATTERN_VERSION_WIDE_WAIT) {
            const ExecEvent32 *ev = (const ExecEvent32 *)(const void *)(event_data + (i * event_size));
            if (ev->reserved != 0u) return ERR_PATTERN_INVALID;
            flags = ev->flags;
            out_value = ev->out_value;
            out_enable = ev->out_enable;
            sample_mask = ev->sample_mask;
            expect_value = ev->expect_value;
        } else {
            const ExecEvent *ev = (const ExecEvent *)(const void *)(event_data + (i * event_size));
            flags = ev->flags;
            out_value = ev->out_value;
            out_enable = ev->out_enable;
            sample_mask = ev->sample_mask;
            expect_value = ev->expect_value;
        }
        if ((flags & ~(STPG_EVF_SAMPLE_POINT | STPG_EVF_COMPARE_POINT)) != 0u) return ERR_UNSUPPORTED;
        if ((out_value & ~pin_mask) != 0u) return ERR_UNSUPPORTED;
        if ((out_enable & ~pin_mask) != 0u) return ERR_UNSUPPORTED;
        if ((sample_mask & ~pin_mask) != 0u) return ERR_UNSUPPORTED;
        if ((expect_value & ~pin_mask) != 0u) return ERR_UNSUPPORTED;
    }
    return ERR_NONE;
}

static stpg_err_t validate_rate_vector_header(const RateVectorHeader *h, size_t total_size) {
    if (h->magic != STPG_RATEVEC_MAGIC) return ERR_BAD_MAGIC;
    if (h->version != STPG_PATTERN_VERSION && h->version != STPG_PATTERN_VERSION_WIDE_WAIT) return ERR_BAD_VERSION;
    if (h->header_size != STPG_RATEVEC_HEADER_SIZE) return ERR_RATEVEC_BAD_HEADER;
    const uint32_t allowed_flags = STPG_RATEVEC_FLAG_HAS_LOOP;
    if ((h->flags & ~allowed_flags) != 0u) return ERR_UNSUPPORTED;
    if (h->active_pins != STPG_ACTIVE_PINS_V01 && h->active_pins != STPG_ACTIVE_PINS_V11) return ERR_UNSUPPORTED;
    if (h->tick_hz != STPG_TICK_HZ) return ERR_UNSUPPORTED;
    if (h->rate_count == 0u || h->rate_count > STPG_MAX_RATES) return ERR_RATEVEC_BAD_HEADER;
    if (h->slot_count == 0u || h->vector_count == 0u) return ERR_RATEVEC_BAD_HEADER;
    if ((h->rate_table_offset & 3u) != 0u || (h->slot_table_offset & 3u) != 0u || (h->vector_offset & 3u) != 0u) {
        return ERR_RATEVEC_BAD_OFFSET;
    }
    if (h->reserved1 != 0u || h->reserved2 != 0u) return ERR_UNSUPPORTED;
    const bool has_loop = (h->flags & STPG_RATEVEC_FLAG_HAS_LOOP) != 0u;
    if (has_loop != (h->reserved0 != 0u)) return ERR_RATEVEC_BAD_HEADER;
    if (h->rate_table_offset < STPG_RATEVEC_HEADER_SIZE || h->slot_table_offset < h->rate_table_offset) {
        return ERR_RATEVEC_BAD_OFFSET;
    }

    const uint32_t rate_desc_size = h->version == STPG_PATTERN_VERSION_WIDE_WAIT
        ? STPG_RATE_DESC32_SIZE : STPG_RATE_DESC_SIZE;
    const uint32_t rate_slot_size = h->version == STPG_PATTERN_VERSION_WIDE_WAIT
        ? STPG_RATE_SLOT32_SIZE : STPG_RATE_SLOT_SIZE;
    const uint64_t rate_end = (uint64_t)h->rate_table_offset + ((uint64_t)h->rate_count * rate_desc_size);
    const uint64_t slot_end = (uint64_t)h->slot_table_offset + ((uint64_t)h->slot_count * rate_slot_size);
    const uint64_t vector_end = (uint64_t)h->vector_offset + ((uint64_t)h->vector_count * STPG_VECTOR_RECORD_SIZE);
    const uint32_t loop_block_offset = h->reserved0 != 0u ? h->reserved0 : h->vector_offset;
    if ((loop_block_offset & 3u) != 0u) return ERR_RATEVEC_BAD_OFFSET;
    if (loop_block_offset < slot_end || loop_block_offset > h->vector_offset) return ERR_RATEVEC_BAD_OFFSET;
    if (rate_end > total_size || slot_end > total_size || vector_end > total_size) {
        return ERR_RATEVEC_BAD_OFFSET;
    }
    if (slot_end > loop_block_offset || rate_end > h->slot_table_offset) {
        return ERR_RATEVEC_BAD_OFFSET;
    }
    return ERR_NONE;
}

static stpg_err_t validate_macro_vector_header(const MacroVectorHeader *h, size_t total_size) {
    if (h->magic != STPG_MACROVEC_MAGIC) return ERR_BAD_MAGIC;
    if (h->version != STPG_PATTERN_VERSION) return ERR_BAD_VERSION;
    if (h->header_size != STPG_MACROVEC_HEADER_SIZE) return ERR_PATTERN_INVALID;
    if (h->flags != 0u) return ERR_UNSUPPORTED;
    if (h->active_pins != STPG_ACTIVE_PINS_V01 && h->active_pins != STPG_ACTIVE_PINS_V11) return ERR_UNSUPPORTED;
    if (h->tick_hz != STPG_TICK_HZ) return ERR_UNSUPPORTED;
    if (h->record_size != STPG_MACRO_RECORD_SIZE) return ERR_PATTERN_INVALID;
    if (h->record_count == 0u) return ERR_PATTERN_INVALID;
    if (h->data_size != h->record_count * STPG_MACRO_RECORD_SIZE) return ERR_PATTERN_INVALID;
    if ((size_t)h->data_size + STPG_MACROVEC_HEADER_SIZE != total_size) return ERR_BAD_SIZE;
    return ERR_NONE;
}

static stpg_err_t validate_macro_p2_vector_header(const MacroVectorHeader *h, size_t total_size) {
    if (h->magic != STPG_MACROP2VEC_MAGIC) return ERR_BAD_MAGIC;
    if (h->version != STPG_PATTERN_VERSION) return ERR_BAD_VERSION;
    if (h->header_size != STPG_MACROVEC_HEADER_SIZE) return ERR_PATTERN_INVALID;
    if (h->flags != 0u) return ERR_UNSUPPORTED;
    if (h->active_pins != STPG_ACTIVE_PINS_V01 && h->active_pins != STPG_ACTIVE_PINS_V11) return ERR_UNSUPPORTED;
    if (h->tick_hz != STPG_TICK_HZ) return ERR_UNSUPPORTED;
    if (h->record_size != STPG_MACRO_P2_RECORD_SIZE) return ERR_PATTERN_INVALID;
    if (h->record_count == 0u) return ERR_PATTERN_INVALID;
    if (h->data_size != h->record_count * STPG_MACRO_P2_RECORD_SIZE) return ERR_PATTERN_INVALID;
    if ((size_t)h->data_size + STPG_MACROVEC_HEADER_SIZE != total_size) return ERR_BAD_SIZE;
    return ERR_NONE;
}

static stpg_err_t validate_vector_record_fields(const VectorRecord *vector, uint32_t rate_count, bool allow_loop_ref, uint32_t active_pins) {
    if ((vector->flags & ~STPG_VECTOR_VALID_FLAG_MASK) != 0u) return ERR_RATEVEC_BAD_HEADER;
    if (allow_loop_ref && vector->flags == STPG_VECTOR_FLAG_LOOP_REF) {
        const LoopRefRecord *loop_ref = (const LoopRefRecord *)(const void *)vector;
        if (loop_ref->reserved0 != 0u || loop_ref->reserved1 != 0u
            || loop_ref->reserved2 != 0u || loop_ref->reserved3 != 0u || loop_ref->reserved4 != 0u) {
            return ERR_RATEVEC_BAD_HEADER;
        }
        return ERR_NONE;
    }
    if (vector->rate_id >= rate_count) return ERR_VECTOR_BAD_RATE_ID;
    if (vector->flags != 0u && vector->flags != STPG_VECTOR_FLAG_EXPLICIT_WAIT_P4_END) return ERR_RATEVEC_BAD_HEADER;
    if (vector->repeat == 0u) return ERR_RATEVEC_BAD_HEADER;
    const uint32_t pin_mask = active_pins == STPG_ACTIVE_PINS_V11 ? 0x00FFFFFFu : 0x0000FFFFu;
    if ((vector->drive_value & ~pin_mask) != 0u
        || (vector->drive_enable & ~pin_mask) != 0u
        || (vector->expect_value & ~pin_mask) != 0u
        || (vector->expect_mask & ~pin_mask) != 0u) {
        return ERR_RATEVEC_BAD_HEADER;
    }
    return ERR_NONE;
}

static stpg_err_t validate_loop_blocks(const uint8_t *buf, const RateVectorHeader *h, uint32_t *out_loop_block_count) {
    uint32_t loop_block_count = 0u;
    uint32_t loop_offset = h->reserved0;
    if (loop_offset == 0u) {
        if (out_loop_block_count != 0) *out_loop_block_count = 0u;
        return ERR_NONE;
    }
    while (loop_offset < h->vector_offset) {
        if (loop_offset + STPG_LOOP_BLOCK_HEADER_SIZE > h->vector_offset) return ERR_RATEVEC_BAD_OFFSET;
        const LoopBlockHeader *loop_header = (const LoopBlockHeader *)(const void *)(buf + loop_offset);
        if (loop_header->loop_count == 0u || loop_header->body_record_count == 0u) return ERR_LOOP_UNSUPPORTED;
        const uint64_t body_end = (uint64_t)loop_offset + STPG_LOOP_BLOCK_HEADER_SIZE
            + ((uint64_t)loop_header->body_record_count * STPG_VECTOR_RECORD_SIZE);
        if (body_end > h->vector_offset) return ERR_RATEVEC_BAD_OFFSET;
        const uint32_t body_offset = loop_offset + STPG_LOOP_BLOCK_HEADER_SIZE;
        for (uint32_t index = 0; index < loop_header->body_record_count; ++index) {
            const VectorRecord *vector = (const VectorRecord *)(const void *)(buf + body_offset + (index * STPG_VECTOR_RECORD_SIZE));
            stpg_err_t err = validate_vector_record_fields(vector, h->rate_count, false, h->active_pins);
            if (err != ERR_NONE) return err;
        }
        loop_offset = (uint32_t)body_end;
        loop_block_count += 1u;
        if (loop_block_count > STPG_MAX_LOOP_BLOCKS) return ERR_RATE_TOO_COMPLEX;
    }
    if (loop_offset != h->vector_offset) return ERR_RATEVEC_BAD_OFFSET;
    if (out_loop_block_count != 0) *out_loop_block_count = loop_block_count;
    return ERR_NONE;
}

static stpg_err_t validate_rate_slots(const uint8_t *buf, const RateVectorHeader *h) {
    for (uint32_t rate_index = 0; rate_index < h->rate_count; ++rate_index) {
        uint32_t period_ticks;
        uint32_t slot_count;
        uint32_t slot_offset;
        uint8_t rate_flags;
        if (h->version == STPG_PATTERN_VERSION_WIDE_WAIT) {
            const RateDesc32 *rate = (const RateDesc32 *)(const void *)(buf + h->rate_table_offset
                + (rate_index * STPG_RATE_DESC32_SIZE));
            if (rate->reserved != 0u) return ERR_RATEVEC_BAD_HEADER;
            period_ticks = rate->period_ticks;
            slot_count = rate->slot_count;
            slot_offset = rate->slot_offset;
            rate_flags = rate->flags;
        } else {
            const RateDesc *rate = (const RateDesc *)(const void *)(buf + h->rate_table_offset
                + (rate_index * STPG_RATE_DESC_SIZE));
            period_ticks = rate->period_ticks;
            slot_count = rate->slot_count;
            slot_offset = rate->slot_offset;
            rate_flags = rate->flags;
        }
        if (period_ticks == 0u) return ERR_RATE_PERIOD_MISMATCH;
        if (slot_count == 0u || slot_count > STPG_MAX_SLOTS_PER_RATE) return ERR_RATE_TOO_COMPLEX;
        if (rate_flags != 0u) return ERR_RATEVEC_BAD_HEADER;
        if ((uint64_t)slot_offset + slot_count > h->slot_count) return ERR_RATEVEC_BAD_OFFSET;

        uint32_t prev_time_ticks = 0u;
        for (uint32_t slot_index = 0; slot_index < slot_count; ++slot_index) {
            uint32_t time_ticks;
            uint16_t flags;
            uint32_t drive_mask;
            uint32_t dir_mask;
            uint32_t sample_mask;
            if (h->version == STPG_PATTERN_VERSION_WIDE_WAIT) {
                const RateSlot32 *slot = (const RateSlot32 *)(const void *)(buf + h->slot_table_offset
                    + ((slot_offset + slot_index) * STPG_RATE_SLOT32_SIZE));
                if (slot->reserved != 0u) return ERR_RATEVEC_BAD_HEADER;
                time_ticks = slot->time_ticks;
                flags = slot->flags;
                drive_mask = slot->drive_mask;
                dir_mask = slot->dir_mask;
                sample_mask = slot->sample_mask;
            } else {
                const RateSlot *slot = (const RateSlot *)(const void *)(buf + h->slot_table_offset
                    + ((slot_offset + slot_index) * STPG_RATE_SLOT_SIZE));
                time_ticks = slot->time_ticks;
                flags = slot->flags;
                drive_mask = slot->drive_mask;
                dir_mask = slot->dir_mask;
                sample_mask = slot->sample_mask;
            }
            const uint16_t drive_op = flags & STPG_RATE_SLOT_DRIVE_OP_MASK;
            if (drive_op > STPG_DRIVE_COMPLEMENT) return ERR_RATEVEC_BAD_HEADER;
            if ((flags & ~STPG_RATE_SLOT_VALID_FLAG_MASK) != 0u) return ERR_RATEVEC_BAD_HEADER;
            const uint32_t pin_mask = h->active_pins == STPG_ACTIVE_PINS_V11 ? 0x00FFFFFFu : 0x0000FFFFu;
            if ((drive_mask & ~pin_mask) != 0u
                || (dir_mask & ~pin_mask) != 0u
                || (sample_mask & ~pin_mask) != 0u) {
                return ERR_RATEVEC_BAD_HEADER;
            }
            if (time_ticks >= period_ticks) return ERR_RATE_PERIOD_MISMATCH;
            if (slot_index > 0u && time_ticks < prev_time_ticks) return ERR_RATE_PERIOD_MISMATCH;
            prev_time_ticks = time_ticks;
        }
    }
    return ERR_NONE;
}

static stpg_err_t validate_vectors(const uint8_t *buf, const RateVectorHeader *h, uint32_t loop_block_count) {
    uint32_t p4_end_marker_count = 0u;
    for (uint32_t vector_index = 0; vector_index < h->vector_count; ++vector_index) {
        const VectorRecord *vector = (const VectorRecord *)(const void *)(buf + h->vector_offset
            + (vector_index * STPG_VECTOR_RECORD_SIZE));
        stpg_err_t err = validate_vector_record_fields(vector, h->rate_count, true, h->active_pins);
        if (err != ERR_NONE) return err;
        if (vector->flags == STPG_VECTOR_FLAG_LOOP_REF) {
            const LoopRefRecord *loop_ref = (const LoopRefRecord *)(const void *)vector;
            if (loop_ref->loop_id >= loop_block_count) return ERR_RATEVEC_BAD_OFFSET;
        }
        if (vector->flags == STPG_VECTOR_FLAG_EXPLICIT_WAIT_P4_END) {
            if ((h->flags & STPG_RATEVEC_FLAG_EXPLICIT_WAIT_P4) == 0u) return ERR_RATEVEC_BAD_HEADER;
            p4_end_marker_count += 1u;
        }
    }
    if ((h->flags & STPG_RATEVEC_FLAG_EXPLICIT_WAIT_P4) != 0u && p4_end_marker_count != 1u) return ERR_RATEVEC_BAD_HEADER;
    return ERR_NONE;
}

static stpg_err_t validate_macro_records(const uint8_t *buf, const MacroVectorHeader *h) {
    const uint32_t pin_mask = h->active_pins == STPG_ACTIVE_PINS_V11 ? 0x00FFFFFFu : 0x0000FFFFu;
    const uint8_t *record_data = buf + STPG_MACROVEC_HEADER_SIZE;
    for (uint32_t index = 0u; index < h->record_count; ++index) {
        const MacroVectorRecord *record =
            (const MacroVectorRecord *)(const void *)(record_data + (index * STPG_MACRO_RECORD_SIZE));
        const uint32_t drive_value = load_u24_le(record->drive_value);
        const uint32_t drive_enable = load_u24_le(record->drive_enable);
        const uint32_t sample_mask = load_u24_le(record->sample_mask);
        const uint32_t expect_value = load_u24_le(record->expect_value);
        if (record->repeat == 0u) return ERR_PATTERN_INVALID;
        if ((record->flags & ~(STPG_EVF_SAMPLE_POINT | STPG_EVF_COMPARE_POINT)) != 0u) return ERR_UNSUPPORTED;
        if ((record->flags & STPG_EVF_COMPARE_POINT) != 0u && (record->flags & STPG_EVF_SAMPLE_POINT) == 0u) {
            return ERR_PATTERN_INVALID;
        }
        if (sample_mask != 0u && (record->flags & STPG_EVF_SAMPLE_POINT) == 0u) return ERR_PATTERN_INVALID;
        if (record->period_ticks < STPG_T_MIN_TICKS) return ERR_RATE_PERIOD_MISMATCH;
        if ((drive_value & ~pin_mask) != 0u || (drive_enable & ~pin_mask) != 0u
            || (sample_mask & ~pin_mask) != 0u || (expect_value & ~pin_mask) != 0u) {
            return ERR_UNSUPPORTED;
        }
        switch (record->family) {
            case 1u:
            case 2u:
                if (record->phase_ticks == 0u || record->phase_ticks >= record->period_ticks) return ERR_RATE_PERIOD_MISMATCH;
                break;
            case 3u:
                if (record->phase_ticks != 0u) return ERR_RATE_PERIOD_MISMATCH;
                if (sample_mask != 0u) return ERR_PATTERN_INVALID;
                break;
            case 4u:
                if (record->phase_ticks != 0u) return ERR_RATE_PERIOD_MISMATCH;
                if (sample_mask == 0u) return ERR_PATTERN_INVALID;
                break;
            case 5u:
                if (record->phase_ticks != 0u) return ERR_RATE_PERIOD_MISMATCH;
                if (sample_mask == 0u) return ERR_PATTERN_INVALID;
                break;
            default:
                return ERR_UNSUPPORTED;
        }
    }
    return ERR_NONE;
}

static stpg_err_t validate_macro_p2_records(const uint8_t *buf, const MacroVectorHeader *h) {
    const uint32_t pin_mask = h->active_pins == STPG_ACTIVE_PINS_V11 ? 0x00FFFFFFu : 0x0000FFFFu;
    const uint8_t *record_data = buf + STPG_MACROVEC_HEADER_SIZE;
    for (uint32_t index = 0u; index < h->record_count; ++index) {
        const MacroVectorRecordP2 *record =
            (const MacroVectorRecordP2 *)(const void *)(record_data + (index * STPG_MACRO_P2_RECORD_SIZE));
        const uint32_t drive0_value = load_u24_le(record->drive0_value);
        const uint32_t drive0_enable = load_u24_le(record->drive0_enable);
        const uint32_t drive1_value = load_u24_le(record->drive1_value);
        const uint32_t drive1_enable = load_u24_le(record->drive1_enable);
        const uint32_t sample0_mask = load_u24_le(record->sample0_mask);
        const uint32_t sample1_mask = load_u24_le(record->sample1_mask);
        const uint32_t expect0_value = load_u24_le(record->expect0_value);
        const uint32_t expect1_value = load_u24_le(record->expect1_value);
        if (record->repeat == 0u) return ERR_PATTERN_INVALID;
        if (record->source_family < 1u || record->source_family > 5u) return ERR_UNSUPPORTED;
        if (record->exec_family != 1u && record->exec_family != 2u && record->exec_family != 3u
            && record->exec_family != 4u && record->exec_family != 5u && record->exec_family != 0x81u) {
            return ERR_UNSUPPORTED;
        }
        if (record->period_ticks < STPG_T_MIN_TICKS) return ERR_RATE_PERIOD_MISMATCH;
        if ((uint32_t)record->wait_first_ticks + (uint32_t)record->wait_second_ticks > (uint32_t)record->period_ticks) {
            return ERR_RATE_PERIOD_MISMATCH;
        }
        if ((drive0_value & ~pin_mask) != 0u || (drive0_enable & ~pin_mask) != 0u
            || (drive1_value & ~pin_mask) != 0u || (drive1_enable & ~pin_mask) != 0u
            || (sample0_mask & ~pin_mask) != 0u || (sample1_mask & ~pin_mask) != 0u
            || (expect0_value & ~pin_mask) != 0u || (expect1_value & ~pin_mask) != 0u) {
            return ERR_UNSUPPORTED;
        }
        if ((expect0_value & ~sample0_mask) != 0u || (expect1_value & ~sample1_mask) != 0u) {
            return ERR_PATTERN_INVALID;
        }
        if (record->exec_family == 1u || record->exec_family == 2u || record->exec_family == 0x81u) {
            if (record->wait_first_ticks == 0u) return ERR_RATE_PERIOD_MISMATCH;
        } else {
            if (record->wait_first_ticks != 0u) return ERR_RATE_PERIOD_MISMATCH;
        }
    }
    return ERR_NONE;
}

stpg_err_t stpg_validate_pattern(const uint8_t *buf, size_t size, uint32_t expected_total_crc) {
    if (buf == 0 || size < 4u || size > STPG_PSRAM_PATTERN_BYTES) {
        return ERR_BAD_SIZE;
    }

    const uint32_t magic = load_u32_le(buf);
    if (magic == STPG_RATEVEC_MAGIC) {
        return stpg_validate_rate_vector(buf, size, expected_total_crc, 0);
    }
    if (magic == STPG_MACROVEC_MAGIC) {
        return stpg_validate_macro_vector(buf, size, expected_total_crc, 0);
    }
    if (magic == STPG_MACROP2VEC_MAGIC) {
        return stpg_validate_macro_p2_vector(buf, size, expected_total_crc, 0);
    }
    if (magic != STPG_PATTERN_MAGIC) return ERR_BAD_MAGIC;
    if (size < STPG_HEADER_SIZE || size > STPG_MAX_PATTERN_BYTES) {
        return ERR_BAD_SIZE;
    }

    if (stpg_crc32(buf, size) != expected_total_crc) return ERR_BAD_CRC;

    const PatternHeader *h = (const PatternHeader *)(const void *)buf;
    stpg_err_t err = validate_header_fields(h, size);
    if (err != ERR_NONE) return err;

    if (stpg_crc32(buf, STPG_HEADER_SIZE - sizeof(h->header_crc32)) != h->header_crc32) {
        return ERR_BAD_CRC;
    }

    const uint8_t *event_data = buf + STPG_HEADER_SIZE;
    if (stpg_crc32(event_data, h->data_size) != h->data_crc32) {
        return ERR_BAD_CRC;
    }

    return validate_events(event_data, h->event_count, h->active_pins, h->version, h->event_size);
}

stpg_err_t stpg_validate_rate_vector(const uint8_t *buf, size_t size, uint32_t expected_total_crc,
                                      RateVectorHeader *out_header) {
    if (buf == 0 || size < STPG_RATEVEC_HEADER_SIZE || size > STPG_PSRAM_PATTERN_BYTES) {
        return ERR_BAD_SIZE;
    }
    if (load_u32_le(buf) != STPG_RATEVEC_MAGIC) return ERR_BAD_MAGIC;
    if (stpg_crc32(buf, size) != expected_total_crc) return ERR_RATEVEC_BAD_CRC;

    const RateVectorHeader *h = (const RateVectorHeader *)(const void *)buf;
    stpg_err_t err = validate_rate_vector_header(h, size);
    if (err != ERR_NONE) return err;
    if (stpg_crc32(buf, STPG_RATEVEC_HEADER_SIZE - sizeof(h->header_crc32)) != h->header_crc32) {
        return ERR_RATEVEC_BAD_CRC;
    }

    const uint32_t vector_end = h->vector_offset + (h->vector_count * STPG_VECTOR_RECORD_SIZE);
    const uint32_t data_crc = stpg_crc32(buf + h->rate_table_offset, vector_end - h->rate_table_offset);
    if (data_crc != h->data_crc32) return ERR_RATEVEC_BAD_CRC;

    err = validate_rate_slots(buf, h);
    if (err != ERR_NONE) return err;
    uint32_t loop_block_count = 0u;
    err = validate_loop_blocks(buf, h, &loop_block_count);
    if (err != ERR_NONE) return err;
    err = validate_vectors(buf, h, loop_block_count);
    if (err != ERR_NONE) return err;

    if (out_header != 0) {
        *out_header = *h;
    }
    return ERR_NONE;
}

stpg_err_t stpg_validate_macro_vector(const uint8_t *buf, size_t size, uint32_t expected_total_crc,
                                      MacroVectorHeader *out_header) {
    if (buf == 0 || size < STPG_MACROVEC_HEADER_SIZE || size > STPG_MAX_PATTERN_BYTES) {
        return ERR_BAD_SIZE;
    }
    if (load_u32_le(buf) != STPG_MACROVEC_MAGIC) return ERR_BAD_MAGIC;
    if (stpg_crc32(buf, size) != expected_total_crc) return ERR_BAD_CRC;

    const MacroVectorHeader *h = (const MacroVectorHeader *)(const void *)buf;
    stpg_err_t err = validate_macro_vector_header(h, size);
    if (err != ERR_NONE) return err;
    if (stpg_crc32(buf, STPG_MACROVEC_HEADER_SIZE - sizeof(h->header_crc32)) != h->header_crc32) {
        return ERR_BAD_CRC;
    }
    const uint8_t *record_data = buf + STPG_MACROVEC_HEADER_SIZE;
    if (stpg_crc32(record_data, h->data_size) != h->data_crc32) {
        return ERR_BAD_CRC;
    }
    err = validate_macro_records(buf, h);
    if (err != ERR_NONE) return err;
    if (out_header != 0) {
        *out_header = *h;
    }
    return ERR_NONE;
}

stpg_err_t stpg_validate_macro_p2_vector(const uint8_t *buf, size_t size, uint32_t expected_total_crc,
                                         MacroVectorHeader *out_header) {
    if (buf == 0 || size < STPG_MACROVEC_HEADER_SIZE || size > STPG_MAX_PATTERN_BYTES) {
        return ERR_BAD_SIZE;
    }
    if (load_u32_le(buf) != STPG_MACROP2VEC_MAGIC) return ERR_BAD_MAGIC;
    if (stpg_crc32(buf, size) != expected_total_crc) return ERR_BAD_CRC;

    const MacroVectorHeader *h = (const MacroVectorHeader *)(const void *)buf;
    stpg_err_t err = validate_macro_p2_vector_header(h, size);
    if (err != ERR_NONE) return err;
    if (stpg_crc32(buf, STPG_MACROVEC_HEADER_SIZE - sizeof(h->header_crc32)) != h->header_crc32) {
        return ERR_BAD_CRC;
    }
    const uint8_t *record_data = buf + STPG_MACROVEC_HEADER_SIZE;
    if (stpg_crc32(record_data, h->data_size) != h->data_crc32) {
        return ERR_BAD_CRC;
    }
    err = validate_macro_p2_records(buf, h);
    if (err != ERR_NONE) return err;
    if (out_header != 0) {
        *out_header = *h;
    }
    return ERR_NONE;
}

stpg_err_t stpg_pattern_summary_from_buffer(const uint8_t *buf, size_t size, stpg_pattern_summary_t *out_summary) {
    if (out_summary == 0) return ERR_BAD_SIZE;
    memset(out_summary, 0, sizeof(*out_summary));
    if (buf == 0 || size < 4u) return ERR_BAD_SIZE;

    const uint32_t magic = load_u32_le(buf);
    if (magic == STPG_PATTERN_MAGIC) {
        if (size < STPG_HEADER_SIZE) return ERR_BAD_SIZE;
        const PatternHeader *h = (const PatternHeader *)(const void *)buf;
        out_summary->format = STPG_PATTERN_FORMAT_RAW_EVENT;
        out_summary->pattern_size = (uint32_t)size;
        out_summary->entry_count = h->event_count;
        out_summary->aux_count = 0u;
        return ERR_NONE;
    }
    if (magic == STPG_MACROVEC_MAGIC) {
        if (size < STPG_MACROVEC_HEADER_SIZE) return ERR_BAD_SIZE;
        const MacroVectorHeader *h = (const MacroVectorHeader *)(const void *)buf;
        out_summary->format = STPG_PATTERN_FORMAT_MACRO_STREAM;
        out_summary->pattern_size = (uint32_t)size;
        out_summary->entry_count = h->record_count;
        out_summary->aux_count = 0u;
        return ERR_NONE;
    }
    if (magic == STPG_MACROP2VEC_MAGIC) {
        if (size < STPG_MACROVEC_HEADER_SIZE) return ERR_BAD_SIZE;
        const MacroVectorHeader *h = (const MacroVectorHeader *)(const void *)buf;
        out_summary->format = STPG_PATTERN_FORMAT_MACRO_P2;
        out_summary->pattern_size = (uint32_t)size;
        out_summary->entry_count = h->record_count;
        out_summary->aux_count = 0u;
        return ERR_NONE;
    }
    if (magic == STPG_RATEVEC_MAGIC) {
        if (size < STPG_RATEVEC_HEADER_SIZE) return ERR_BAD_SIZE;
        const RateVectorHeader *h = (const RateVectorHeader *)(const void *)buf;
        out_summary->format = STPG_PATTERN_FORMAT_RATE_VECTOR;
        out_summary->pattern_size = (uint32_t)size;
        out_summary->entry_count = h->vector_count;
        out_summary->aux_count = h->rate_count;
        return ERR_NONE;
    }
    return ERR_BAD_MAGIC;
}

stpg_err_t stpg_expand_raw_events_to_pio_words(const uint8_t *buf, size_t size,
                                                uint32_t *pio_words,
                                                uint32_t max_word_count,
                                                uint32_t *out_word_count,
                                                uint32_t *out_sample_event_count) {
    if (buf == 0 || pio_words == 0 || out_word_count == 0 || size < STPG_HEADER_SIZE) {
        return ERR_PATTERN_INVALID;
    }

    const PatternHeader *h = (const PatternHeader *)(const void *)buf;
    if (h->version != STPG_PATTERN_VERSION && h->version != STPG_PATTERN_VERSION_WIDE_WAIT) return ERR_BAD_VERSION;
    const uint32_t event_size = h->version == STPG_PATTERN_VERSION
        ? STPG_EXEC_EVENT_SIZE : STPG_EXEC_EVENT32_SIZE;
    if (h->event_size != event_size) return ERR_PATTERN_INVALID;
    const uint32_t required_words = h->event_count * 4u;
    if (required_words > max_word_count) return ERR_PATTERN_INVALID;
    if ((size_t)h->data_size + STPG_HEADER_SIZE != size) return ERR_BAD_SIZE;

    uint32_t sample_event_count = 0u;
    const uint8_t *event_data = buf + STPG_HEADER_SIZE;
    const uint32_t logical_mask = h->active_pins == STPG_ACTIVE_PINS_V11 ? 0x00FFFFFFu : 0x0000FFFFu;
    for (uint32_t i = 0; i < h->event_count; ++i) {
        uint32_t wait_ticks;
        uint16_t flags;
        uint32_t out_value;
        uint32_t out_enable;
        if (h->version == STPG_PATTERN_VERSION_WIDE_WAIT) {
            const ExecEvent32 *ev = (const ExecEvent32 *)(const void *)(event_data + (i * event_size));
            if (ev->reserved != 0u) return ERR_PATTERN_INVALID;
            wait_ticks = ev->wait_ticks;
            flags = ev->flags;
            out_value = ev->out_value;
            out_enable = ev->out_enable;
        } else {
            const ExecEvent *ev = (const ExecEvent *)(const void *)(event_data + (i * event_size));
            wait_ticks = ev->wait_ticks;
            flags = ev->flags;
            out_value = ev->out_value;
            out_enable = ev->out_enable;
        }
        const uint32_t sample_trigger = (flags & STPG_EVF_SAMPLE_POINT) ? 1u : 0u;
        pio_words[i * 4u + 0u] = wait_ticks;
        pio_words[i * 4u + 1u] = sample_trigger;
        pio_words[i * 4u + 2u] = stpg_board_pack_logical_to_window(out_value & logical_mask, h->active_pins);
        pio_words[i * 4u + 3u] = stpg_board_pack_logical_to_window(out_enable & logical_mask, h->active_pins);
        sample_event_count += sample_trigger;
    }

    *out_word_count = required_words;
    if (out_sample_event_count != 0) {
        *out_sample_event_count = sample_event_count;
    }
    return ERR_NONE;
}
