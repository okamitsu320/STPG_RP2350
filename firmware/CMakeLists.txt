cmake_minimum_required(VERSION 3.13)

if(NOT DEFINED PICO_SDK_PATH AND EXISTS "${CMAKE_CURRENT_LIST_DIR}/../pico-sdk")
    set(PICO_SDK_PATH "${CMAKE_CURRENT_LIST_DIR}/../pico-sdk" CACHE PATH "Path to the Raspberry Pi Pico SDK")
endif()

if(NOT DEFINED PICO_BOARD)
    set(PICO_BOARD pico2 CACHE STRING "Pico board target")
endif()

if(DEFINED PICO_SDK_PATH AND NOT IS_ABSOLUTE "${PICO_SDK_PATH}")
    get_filename_component(PICO_SDK_PATH "${PICO_SDK_PATH}" ABSOLUTE BASE_DIR "${CMAKE_CURRENT_LIST_DIR}")
    set(PICO_SDK_PATH "${PICO_SDK_PATH}" CACHE PATH "Path to the Raspberry Pi Pico SDK" FORCE)
endif()

if(NOT DEFINED STPG_CLOCK_MODE)
    set(STPG_CLOCK_MODE 1 CACHE STRING "STPG clock mode: 0=150MHz, 1=200MHz")
endif()

if(NOT STPG_CLOCK_MODE STREQUAL "0" AND NOT STPG_CLOCK_MODE STREQUAL "1")
    message(FATAL_ERROR "STPG_CLOCK_MODE must be 0 (150MHz) or 1 (200MHz)")
endif()

if(NOT DEFINED STPG_PSRAM_QMI_CLKDIV)
    set(STPG_PSRAM_QMI_CLKDIV 2 CACHE STRING "PSRAM QMI M1 SCK divisor; 2 is the safe default")
endif()

if(NOT DEFINED STPG_PSRAM_QMI_RXDELAY)
    set(STPG_PSRAM_QMI_RXDELAY 2 CACHE STRING "PSRAM QMI M1 RX sampling delay in half-system-clock units")
endif()

if(NOT DEFINED STPG_PSRAM_QMI_PAGEBREAK)
    set(STPG_PSRAM_QMI_PAGEBREAK 1024 CACHE STRING "PSRAM QMI M1 page-break size in bytes")
endif()

if(NOT DEFINED STPG_PSRAM_XIP_STREAM_DMA_MIN_BYTES)
    set(STPG_PSRAM_XIP_STREAM_DMA_MIN_BYTES 256 CACHE STRING "Minimum aligned page-local PSRAM read size for XIP stream DMA")
endif()

if(STPG_PSRAM_XIP_STREAM_DMA_MIN_BYTES LESS 4 OR
   STPG_PSRAM_XIP_STREAM_DMA_MIN_BYTES GREATER STPG_PSRAM_QMI_PAGEBREAK)
    message(FATAL_ERROR
        "STPG_PSRAM_XIP_STREAM_DMA_MIN_BYTES must be between 4 and STPG_PSRAM_QMI_PAGEBREAK")
endif()
math(EXPR STPG_PSRAM_XIP_STREAM_DMA_MIN_BYTES_REMAINDER
    "${STPG_PSRAM_XIP_STREAM_DMA_MIN_BYTES} % 4")
if(NOT STPG_PSRAM_XIP_STREAM_DMA_MIN_BYTES_REMAINDER EQUAL 0)
    message(FATAL_ERROR "STPG_PSRAM_XIP_STREAM_DMA_MIN_BYTES must be 32-bit aligned")
endif()

if(NOT DEFINED STPG_PSRAM_ALLOW_UNSAFE_CLKDIV1)
    set(STPG_PSRAM_ALLOW_UNSAFE_CLKDIV1 OFF CACHE BOOL "Allow experimental QMI M1 CLKDIV=1; not validated for production")
endif()

if(NOT DEFINED STPG_STREAM_VECTOR_CACHE_NEXT_RECORDS)
    set(STPG_STREAM_VECTOR_CACHE_NEXT_RECORDS 2048 CACHE STRING "Next vector cache records for prefetch experiments")
endif()

if(NOT DEFINED STPG_STREAM_VECTOR_CACHE_READ_RECORDS)
    set(STPG_STREAM_VECTOR_CACHE_READ_RECORDS 256 CACHE STRING "Maximum vector records read per PSRAM cache refill")
endif()

if(STPG_STREAM_VECTOR_CACHE_READ_RECORDS LESS 1 OR
   STPG_STREAM_VECTOR_CACHE_READ_RECORDS GREATER 2048)
    message(FATAL_ERROR "STPG_STREAM_VECTOR_CACHE_READ_RECORDS must be between 1 and 2048")
endif()

if(NOT DEFINED STPG_STREAM_SLOT_COUNT)
    set(STPG_STREAM_SLOT_COUNT 3 CACHE STRING "Streaming ring slot count")
endif()

if(NOT DEFINED STPG_STREAM_BUFFER_EVENTS)
    set(STPG_STREAM_BUFFER_EVENTS 2048 CACHE STRING "Events per streaming ring slot")
endif()

if(NOT DEFINED STPG_FAIL_STOP)
    set(STPG_FAIL_STOP OFF CACHE BOOL "Enable best-effort same-RUN compare fail stop for materialized runs")
endif()

if(NOT DEFINED STPG_STREAM_FAIL_STOP)
    set(STPG_STREAM_FAIL_STOP OFF CACHE BOOL "Enable bounded SRAM-sample Rate Vector streaming compare fail stop")
endif()

if(NOT DEFINED STPG_MAX_RATES)
    set(STPG_MAX_RATES 64 CACHE STRING "Maximum admitted Rate Vector descriptors")
endif()

if(NOT DEFINED STPG_FIRMWARE_FLAVOR)
    set(STPG_FIRMWARE_FLAVOR production CACHE STRING "Firmware flavor: production or diagnostic")
endif()

if(STPG_FIRMWARE_FLAVOR STREQUAL "production")
    set(STPG_FW_DIAGNOSTIC 0)
elseif(STPG_FIRMWARE_FLAVOR STREQUAL "diagnostic")
    set(STPG_FW_DIAGNOSTIC 1)
else()
    message(FATAL_ERROR "STPG_FIRMWARE_FLAVOR must be production or diagnostic")
endif()

if(NOT DEFINED STPG_PSRAM_PATTERN_BASE)
    set(STPG_PSRAM_PATTERN_BASE 0x010000 CACHE STRING "PSRAM pattern region base offset")
endif()

if(NOT DEFINED STPG_PSRAM_PATTERN_BYTES)
    set(STPG_PSRAM_PATTERN_BYTES 0x4F0000 CACHE STRING "PSRAM pattern region capacity")
endif()

if(NOT DEFINED STPG_PSRAM_RESULT_BASE)
    set(STPG_PSRAM_RESULT_BASE 0x500000 CACHE STRING "PSRAM result region base offset")
endif()

if(NOT DEFINED STPG_PSRAM_RESULT_BYTES)
    set(STPG_PSRAM_RESULT_BYTES 0x200000 CACHE STRING "PSRAM result region capacity")
endif()

if(NOT DEFINED STPG_PSRAM_STAGING_BASE)
    set(STPG_PSRAM_STAGING_BASE 0x700000 CACHE STRING "PSRAM staging region base offset")
endif()

if(NOT DEFINED STPG_PSRAM_STAGING_BYTES)
    set(STPG_PSRAM_STAGING_BYTES 0x100000 CACHE STRING "PSRAM staging region capacity")
endif()

# Production images have one fixed capacity/identity contract.  The
# diagnostic flavor may select other bounded layouts, but a stale CMake cache
# must never silently turn the production image into the legacy 16-rate/
# 6M/1M-result configuration.
if(STPG_FIRMWARE_FLAVOR STREQUAL "production")
    if(NOT STPG_MAX_RATES EQUAL 64 OR
       NOT STPG_PSRAM_PATTERN_BASE STREQUAL "0x010000" OR
       NOT STPG_PSRAM_PATTERN_BYTES STREQUAL "0x4F0000" OR
       NOT STPG_PSRAM_RESULT_BASE STREQUAL "0x500000" OR
       NOT STPG_PSRAM_RESULT_BYTES STREQUAL "0x200000" OR
       NOT STPG_PSRAM_STAGING_BASE STREQUAL "0x700000" OR
       NOT STPG_PSRAM_STAGING_BYTES STREQUAL "0x100000")
        message(FATAL_ERROR
            "production firmware requires STPG_MAX_RATES=64 and the 5M/2M PSRAM layout")
    endif()
endif()

if(STPG_MAX_RATES LESS 1 OR STPG_MAX_RATES GREATER 255)
    message(FATAL_ERROR "STPG_MAX_RATES must be in the range 1..255")
endif()

if(STPG_PSRAM_QMI_CLKDIV LESS 1 OR STPG_PSRAM_QMI_CLKDIV GREATER 255)
    message(FATAL_ERROR "STPG_PSRAM_QMI_CLKDIV must be in the range 1..255")
endif()

if(STPG_PSRAM_QMI_CLKDIV EQUAL 1 AND NOT STPG_PSRAM_ALLOW_UNSAFE_CLKDIV1)
    message(FATAL_ERROR "STPG_PSRAM_QMI_CLKDIV=1 requires STPG_PSRAM_ALLOW_UNSAFE_CLKDIV1=ON")
endif()

if(STPG_PSRAM_QMI_RXDELAY LESS 0 OR STPG_PSRAM_QMI_RXDELAY GREATER 7)
    message(FATAL_ERROR "STPG_PSRAM_QMI_RXDELAY must be in the range 0..7")
endif()

if(NOT STPG_PSRAM_QMI_PAGEBREAK STREQUAL "256" AND
   NOT STPG_PSRAM_QMI_PAGEBREAK STREQUAL "1024" AND
   NOT STPG_PSRAM_QMI_PAGEBREAK STREQUAL "4096")
    message(FATAL_ERROR "STPG_PSRAM_QMI_PAGEBREAK must be 256, 1024, or 4096")
endif()

if(STPG_STREAM_FAIL_STOP AND NOT STPG_FAIL_STOP)
    message(FATAL_ERROR "STPG_STREAM_FAIL_STOP requires STPG_FAIL_STOP=ON")
endif()

if(STPG_STREAM_SLOT_COUNT LESS 2)
    message(FATAL_ERROR "STPG_STREAM_SLOT_COUNT must be at least 2")
endif()

if(STPG_STREAM_BUFFER_EVENTS LESS 1 OR STPG_STREAM_BUFFER_EVENTS GREATER 8192)
    message(FATAL_ERROR "STPG_STREAM_BUFFER_EVENTS must be in the range 1..8192")
endif()

math(EXPR STPG_STREAM_TOTAL_EVENT_CAPACITY
    "${STPG_STREAM_SLOT_COUNT} * ${STPG_STREAM_BUFFER_EVENTS}")
if(STPG_STREAM_TOTAL_EVENT_CAPACITY GREATER 8192)
    message(FATAL_ERROR "streaming slots exceed the 128KiB pattern scratch capacity")
endif()
if(STPG_STREAM_FAIL_STOP AND STPG_STREAM_TOTAL_EVENT_CAPACITY GREATER 6551)
    message(FATAL_ERROR "fail-stop streaming slots exceed the 6551-event SRAM sample bound")
endif()

if(NOT DEFINED STPG_STREAM_PARTIAL_REPEAT_FAST_PATH)
    set(STPG_STREAM_PARTIAL_REPEAT_FAST_PATH OFF CACHE BOOL "Allow the rate-vector batch path to split a repeated vector at a chunk boundary")
endif()

if(NOT DEFINED STPG_STREAM_PRACTICAL_PROFILE)
    set(STPG_STREAM_PRACTICAL_PROFILE ON CACHE BOOL "Enable the supported practical Rate Vector streaming profile")
endif()

if(NOT DEFINED STPG_STREAM_LOOP_BATCH)
    set(STPG_STREAM_LOOP_BATCH OFF CACHE BOOL "Use bounded loop-aware batch generation for repeated practical Rate Vectors")
endif()

if(NOT DEFINED STPG_ENABLE_LINK_MAP)
    set(STPG_ENABLE_LINK_MAP OFF CACHE BOOL "Emit linker map for stpg_firmware")
endif()

if(NOT DEFINED STPG_ENABLE_STACK_USAGE)
    set(STPG_ENABLE_STACK_USAGE OFF CACHE BOOL "Emit GCC stack-usage records for non-contact budget checks")
endif()

if(NOT DEFINED STPG_STACK_WATERMARK_DIAGNOSTIC)
    set(STPG_STACK_WATERMARK_DIAGNOSTIC OFF CACHE BOOL "Paint unused core stacks in diagnostic firmware for read-only watermark snapshots")
endif()
if(STPG_STACK_WATERMARK_DIAGNOSTIC AND NOT STPG_FIRMWARE_FLAVOR STREQUAL "diagnostic")
    message(FATAL_ERROR "STPG_STACK_WATERMARK_DIAGNOSTIC=ON requires STPG_FIRMWARE_FLAVOR=diagnostic")
endif()
if(STPG_STACK_WATERMARK_DIAGNOSTIC)
    # Stack-watermark results are intended to characterize the practical
    # optimized firmware, with the bounded diagnostic instrumentation added.
    # An unoptimized Release configuration can retain otherwise-dead static
    # buffers in SRAM (notably the 40 KiB next-vector cache), both overflowing
    # RAM and measuring a different image.  Fail at configure time instead of
    # producing a misleading link failure or HWM candidate.
    if(NOT CMAKE_BUILD_TYPE STREQUAL "Release")
        message(FATAL_ERROR
            "STPG_STACK_WATERMARK_DIAGNOSTIC=ON requires CMAKE_BUILD_TYPE=Release")
    endif()
    separate_arguments(_stpg_stack_diag_release_flags NATIVE_COMMAND "${CMAKE_C_FLAGS_RELEASE}")
    set(_stpg_stack_diag_has_o3 OFF)
    foreach(_stpg_stack_diag_flag IN LISTS _stpg_stack_diag_release_flags)
        if(_stpg_stack_diag_flag MATCHES "^-O")
            if(NOT _stpg_stack_diag_flag STREQUAL "-O3")
                message(FATAL_ERROR
                    "STPG_STACK_WATERMARK_DIAGNOSTIC=ON requires exactly -O3 in CMAKE_C_FLAGS_RELEASE; "
                    "conflicting optimization flag: ${_stpg_stack_diag_flag}")
            endif()
            set(_stpg_stack_diag_has_o3 ON)
        endif()
    endforeach()
    if(NOT _stpg_stack_diag_has_o3)
        message(FATAL_ERROR
            "STPG_STACK_WATERMARK_DIAGNOSTIC=ON requires -O3 in CMAKE_C_FLAGS_RELEASE "
            "to match the optimized practical firmware profile")
    endif()
endif()

if(NOT DEFINED STPG_REFILL_TRACE_DIAGNOSTIC)
    set(STPG_REFILL_TRACE_DIAGNOSTIC OFF CACHE BOOL "Enable bounded per-refill timing trace for diagnostic firmware")
endif()

if(NOT DEFINED STPG_STREAM_HOT_TELEMETRY)
    if(STPG_STREAM_PRACTICAL_PROFILE)
        # The practical runtime must not pay per-event diagnostic accounting
        # by default. Diagnostic candidates can opt in explicitly with ON;
        # this setting must not control semantic fast-path availability.
        set(STPG_STREAM_HOT_TELEMETRY OFF CACHE BOOL "Keep per-event streaming timing and hot-loop telemetry")
    else()
        set(STPG_STREAM_HOT_TELEMETRY ON CACHE BOOL "Keep per-event streaming timing and hot-loop telemetry")
    endif()
endif()

if(NOT DEFINED STPG_STREAM_COMPARE_FREE_BLOCK_FILL)
    set(STPG_STREAM_COMPARE_FREE_BLOCK_FILL OFF CACHE BOOL "Use the no-compare rate-vector block-fill path when the pattern has no samples")
endif()

if(NOT DEFINED STPG_STREAM_PRODUCER_SRAM)
    set(STPG_STREAM_PRODUCER_SRAM OFF CACHE BOOL "Place the streaming producer hot path in SRAM for diagnostics")
endif()

if(NOT DEFINED STPG_STREAM_REPEAT_BLOCK_FILL)
    set(STPG_STREAM_REPEAT_BLOCK_FILL OFF CACHE BOOL "Use a compare-free repeated-vector word-template fill for diagnostics")
endif()

if(NOT DEFINED STPG_STREAM_REPEAT_BOUNDARY_FILL)
    set(STPG_STREAM_REPEAT_BOUNDARY_FILL OFF CACHE BOOL "Use generic boundary-prefix then repeat-block fill for diagnostics")
endif()

if(NOT DEFINED STPG_STREAM_REPEAT_BOUNDARY_CONTINUE)
    set(STPG_STREAM_REPEAT_BOUNDARY_CONTINUE OFF CACHE BOOL "Return to the span fast path after a boundary repeat-block fills a chunk")
endif()

if(NOT DEFINED STPG_STREAM_REPEAT_BOUNDARY_REARM)
    set(STPG_STREAM_REPEAT_BOUNDARY_REARM OFF CACHE BOOL "Re-arm the span fast path after generic boundary completion fills a chunk")
endif()

if(NOT DEFINED STPG_STREAM_COMPARE_STREAMING)
    set(STPG_STREAM_COMPARE_STREAMING ON CACHE BOOL "Allow Rate Vector streaming runs with sample/compare events")
endif()

if(NOT DEFINED STPG_STREAM_PSRAM_COMPARE_SAMPLE)
    set(STPG_STREAM_PSRAM_COMPARE_SAMPLE ON CACHE BOOL "Store large Rate Vector streaming samples in PSRAM and replay compare metadata per chunk")
endif()

if(NOT DEFINED STPG_STREAM_COMPARE_SAMPLE_REPAIR)
    set(STPG_STREAM_COMPARE_SAMPLE_REPAIR OFF CACHE BOOL "Use compare-point-only sample scheduling for diagnostic streaming")
endif()

if(NOT DEFINED STPG_STREAM_COMPARE_MIXED_RATE_REPAIR)
    set(STPG_STREAM_COMPARE_MIXED_RATE_REPAIR OFF CACHE BOOL "Support mixed drive-only and sampled rates in diagnostic reduced compare streaming")
endif()

if(NOT DEFINED STPG_STREAM_COMPARE_EVENT_SCHEDULE)
    set(STPG_STREAM_COMPARE_EVENT_SCHEDULE OFF CACHE BOOL "Use one delayed-sample schedule word per reduced output event")
endif()

if(NOT DEFINED STPG_STREAM_COMPARE_EVENT_SCHEDULE_HANDOFF)
    set(STPG_STREAM_COMPARE_EVENT_SCHEDULE_HANDOFF OFF CACHE BOOL "Wait for the reduced sample schedule DMA handoff before reconfiguration")
endif()

if(NOT DEFINED STPG_STREAM_COMPARE_EVENT_SCHEDULE_CHAIN_HANDOFF)
    set(STPG_STREAM_COMPARE_EVENT_SCHEDULE_CHAIN_HANDOFF OFF CACHE BOOL "Start the reduced sample schedule DMA for each chained output slot")
endif()

if(NOT DEFINED STPG_STREAM_STATUS_LOGICAL_EVENTS)
    set(STPG_STREAM_STATUS_LOGICAL_EVENTS ON CACHE BOOL "Report logical expanded Rate Vector event counts in streaming status")
endif()

if(NOT DEFINED STPG_STREAM_LARGE_RATE_VECTOR)
    set(STPG_STREAM_LARGE_RATE_VECTOR OFF CACHE BOOL "Allow bounded large compare-free Rate Vectors for streaming diagnostics")
endif()

if(NOT DEFINED STPG_STREAM_PSRAM_CAPACITY_RATE_VECTOR)
    set(STPG_STREAM_PSRAM_CAPACITY_RATE_VECTOR OFF CACHE BOOL "Allow compare-free compressed Rate Vector streaming up to the uint32 event-accounting limit")
endif()

# The practical profile is an explicit build contract. It enables only
# bounded optimizations whose eligibility checks preserve the generic path for
# all other vector shapes. HOT_TELEMETRY is a diagnostic-cost setting and is
# intentionally independent of semantic fast-path availability.
if(STPG_STREAM_PRACTICAL_PROFILE)
    set(STPG_STREAM_LOOP_BATCH ON CACHE BOOL "" FORCE)
    set(STPG_STREAM_PARTIAL_REPEAT_FAST_PATH ON CACHE BOOL "" FORCE)
    set(STPG_STREAM_COMPARE_FREE_BLOCK_FILL ON CACHE BOOL "" FORCE)
    set(STPG_STREAM_PRODUCER_SRAM ON CACHE BOOL "" FORCE)
    set(STPG_STREAM_REPEAT_BLOCK_FILL ON CACHE BOOL "" FORCE)
    set(STPG_STREAM_REPEAT_BOUNDARY_FILL ON CACHE BOOL "" FORCE)
    set(STPG_STREAM_REPEAT_BOUNDARY_CONTINUE ON CACHE BOOL "" FORCE)
    set(STPG_STREAM_REPEAT_BOUNDARY_REARM ON CACHE BOOL "" FORCE)
    set(STPG_STREAM_COMPARE_STREAMING ON CACHE BOOL "" FORCE)
    set(STPG_STREAM_PSRAM_COMPARE_SAMPLE ON CACHE BOOL "" FORCE)
    set(STPG_STREAM_FIFO_FILL_REQUESTS ON CACHE BOOL "" FORCE)
    set(STPG_STREAM_STATUS_LOGICAL_EVENTS ON CACHE BOOL "" FORCE)
    set(STPG_STREAM_RATE_VECTOR_EVENT_LIMIT 4294967295 CACHE STRING "" FORCE)
endif()

if(NOT DEFINED STPG_STREAM_RATE_VECTOR_EVENT_LIMIT)
    if(STPG_STREAM_PSRAM_COMPARE_SAMPLE)
        set(STPG_STREAM_RATE_VECTOR_EVENT_LIMIT 4294967295 CACHE STRING "Maximum emitted events admitted by compressed Rate Vector streaming")
    elseif(STPG_STREAM_PSRAM_CAPACITY_RATE_VECTOR)
        set(STPG_STREAM_RATE_VECTOR_EVENT_LIMIT 4294967295 CACHE STRING "Maximum emitted events admitted by compressed Rate Vector streaming")
    elseif(STPG_STREAM_LARGE_RATE_VECTOR)
        set(STPG_STREAM_RATE_VECTOR_EVENT_LIMIT 40960 CACHE STRING "Maximum emitted events admitted by compressed Rate Vector streaming")
    else()
        set(STPG_STREAM_RATE_VECTOR_EVENT_LIMIT 6551 CACHE STRING "Maximum emitted events admitted by compressed Rate Vector streaming")
    endif()
endif()

# Large fail-stop streams need the bounded loop template just as the practical
# stream does.  Its compare metadata is regenerated per replayed event below;
# avoid silently shipping the known refill-deadline-limited generic loop path.
if(STPG_STREAM_FAIL_STOP
    AND STPG_STREAM_RATE_VECTOR_EVENT_LIMIT GREATER 6551)
    set(STPG_STREAM_LOOP_BATCH ON CACHE BOOL "" FORCE)
endif()

if(STPG_STREAM_PSRAM_COMPARE_SAMPLE AND NOT STPG_STREAM_COMPARE_STREAMING)
    message(FATAL_ERROR "STPG_STREAM_PSRAM_COMPARE_SAMPLE requires STPG_STREAM_COMPARE_STREAMING=ON")
endif()

if(STPG_STREAM_PSRAM_COMPARE_SAMPLE AND STPG_STREAM_RATE_VECTOR_EVENT_LIMIT LESS_EQUAL 6551)
    message(FATAL_ERROR "STPG_STREAM_PSRAM_COMPARE_SAMPLE requires an oversized Rate Vector event limit")
endif()

if(STPG_STREAM_FAIL_STOP AND NOT STPG_STREAM_COMPARE_STREAMING)
    message(FATAL_ERROR "STPG_STREAM_FAIL_STOP requires STPG_STREAM_COMPARE_STREAMING=ON")
endif()

if(STPG_STREAM_FAIL_STOP AND STPG_STREAM_PSRAM_COMPARE_SAMPLE)
    message(FATAL_ERROR "STPG_STREAM_FAIL_STOP first supports SRAM samples only; disable STPG_STREAM_PSRAM_COMPARE_SAMPLE")
endif()

# Select the default after the practical profile and diagnostic options have
# resolved. Never enable PSRAM staging in the SRAM-only fail-stop profile.
set(_stpg_sample_staging_default OFF)
if(STPG_STREAM_PSRAM_COMPARE_SAMPLE AND NOT STPG_STREAM_FAIL_STOP AND NOT STPG_STREAM_COMPARE_SAMPLE_REPAIR)
    set(_stpg_sample_staging_default ON)
endif()
option(STPG_SAMPLE_PSRAM_SRAM_STAGING "Use SRAM RX ring with CPU flush to PSRAM (no direct sample DMA to XIP)" ${_stpg_sample_staging_default})

if(STPG_SAMPLE_PSRAM_SRAM_STAGING AND
   (NOT STPG_STREAM_PSRAM_COMPARE_SAMPLE OR STPG_STREAM_FAIL_STOP OR STPG_STREAM_COMPARE_SAMPLE_REPAIR))
    message(FATAL_ERROR "Sample SRAM staging requires standard PSRAM samples without fail-stop or reduced diagnostics")
endif()

if(_stpg_sample_staging_default AND NOT STPG_SAMPLE_PSRAM_SRAM_STAGING)
    message(WARNING
        "STPG_SAMPLE_PSRAM_SRAM_STAGING=OFF uses direct PSRAM sample DMA with observed waveform stretching. "
        "OFF is for diagnostic comparison only; set -DSTPG_SAMPLE_PSRAM_SRAM_STAGING=ON for normal use, "
        "including when reusing an old CMake cache.")
endif()
message(STATUS "STPG_SAMPLE_PSRAM_SRAM_STAGING=${STPG_SAMPLE_PSRAM_SRAM_STAGING}")

if(STPG_STREAM_RATE_VECTOR_EVENT_LIMIT LESS 1 OR STPG_STREAM_RATE_VECTOR_EVENT_LIMIT GREATER 4294967295)
    message(FATAL_ERROR "STPG_STREAM_RATE_VECTOR_EVENT_LIMIT must be in the range 1..4294967295")
endif()

if(NOT DEFINED STPG_STREAM_FIFO_FILL_REQUESTS)
    set(STPG_STREAM_FIFO_FILL_REQUESTS OFF CACHE BOOL "Preserve streaming Core1 fill-request enqueue order")
endif()

if(NOT DEFINED STPG_STREAM_PRECOMPUTED_UOP_CAPACITY)
    set(STPG_STREAM_PRECOMPUTED_UOP_CAPACITY 32 CACHE STRING "Bounded number of Rate Vector group UOPs prepared before streaming")
endif()

option(STPG_STREAM_RATE_TEMPLATE_FILL "Experimental 16/24-pin cached-rate template refill (no loops or inline comparison)" OFF)

if(STPG_STREAM_PRECOMPUTED_UOP_CAPACITY LESS 1 OR STPG_STREAM_PRECOMPUTED_UOP_CAPACITY GREATER 254)
    message(FATAL_ERROR "STPG_STREAM_PRECOMPUTED_UOP_CAPACITY must be in the range 1..254")
endif()

if(STPG_STREAM_FAIL_STOP AND NOT STPG_STREAM_FIFO_FILL_REQUESTS)
    message(FATAL_ERROR
        "STPG_STREAM_FAIL_STOP requires STPG_STREAM_FIFO_FILL_REQUESTS=ON "
        "to preserve slot metadata publication order")
endif()

if(STPG_STREAM_FAIL_STOP AND NOT STPG_STREAM_PRODUCER_SRAM)
    message(FATAL_ERROR
        "STPG_STREAM_FAIL_STOP requires STPG_STREAM_PRODUCER_SRAM=ON "
        "for the Core 1 producer and fill path")
endif()

if(NOT DEFINED STPG_STREAM_DENSE_VECTOR_FILL)
    set(STPG_STREAM_DENSE_VECTOR_FILL OFF CACHE BOOL "Use a compare-free repeat=1 direct-vector fill for a narrowly validated dense shape")
endif()

if(NOT DEFINED STPG_PREFETCH_PHASE1_MAP_HOOK)
    set(STPG_PREFETCH_PHASE1_MAP_HOOK OFF CACHE BOOL "Enable one live prefetch hook for static map comparison only")
endif()

if(NOT DEFINED STPG_PREFETCH_PHASE1_TRUE_INERT_PAD_HOOK)
    set(STPG_PREFETCH_PHASE1_TRUE_INERT_PAD_HOOK OFF CACHE BOOL "Enable true inert local text-growth hook with no prefetch semantics")
endif()

if(NOT DEFINED STPG_PREFETCH_PHASE1_TRUE_INERT_PAD_NOPS)
    set(STPG_PREFETCH_PHASE1_TRUE_INERT_PAD_NOPS 76 CACHE STRING "NOP count for true inert local text-growth hook")
endif()

if(NOT DEFINED STPG_USB_TUD_TASK_EXT_TRUE_INERT_PAD_HOOK)
    set(STPG_USB_TUD_TASK_EXT_TRUE_INERT_PAD_HOOK OFF CACHE BOOL "Enable true inert pad immediately before tud_task_ext()")
endif()

if(NOT DEFINED STPG_USB_TUD_TASK_EXT_START_PHASE_SHIFT_HOOK)
    set(STPG_USB_TUD_TASK_EXT_START_PHASE_SHIFT_HOOK OFF CACHE BOOL "Enable inert pad emitted immediately before tud_task_ext() symbol without changing tud_task_ext() body")
endif()

if(NOT DEFINED STPG_USB_TUD_TASK_EXT_START_PHASE_SHIFT_NOPS)
    set(STPG_USB_TUD_TASK_EXT_START_PHASE_SHIFT_NOPS 12 CACHE STRING "NOP count for tud_task_ext() start-phase-only shift probe")
endif()

if(NOT DEFINED STPG_USB_TUD_TASK_EXT_SPLIT_A_HOOK)
    set(STPG_USB_TUD_TASK_EXT_SPLIT_A_HOOK OFF CACHE BOOL "Enable inert pad at tud_task_ext() split A")
endif()

if(NOT DEFINED STPG_USB_TUD_TASK_EXT_SPLIT_A1_HOOK)
    set(STPG_USB_TUD_TASK_EXT_SPLIT_A1_HOOK OFF CACHE BOOL "Enable inert pad at tud_task_ext() split A1")
endif()

if(NOT DEFINED STPG_USB_TUD_TASK_EXT_SPLIT_A1_PRE_HOOK)
    set(STPG_USB_TUD_TASK_EXT_SPLIT_A1_PRE_HOOK OFF CACHE BOOL "Enable inert pad at tud_task_ext() split A1_pre")
endif()

if(NOT DEFINED STPG_USB_TUD_TASK_EXT_SPLIT_A1_POST_HOOK)
    set(STPG_USB_TUD_TASK_EXT_SPLIT_A1_POST_HOOK OFF CACHE BOOL "Enable inert pad at tud_task_ext() split A1_post")
endif()

if(NOT DEFINED STPG_USB_TUD_TASK_EXT_SPLIT_A1_GUARD_HOOK)
    set(STPG_USB_TUD_TASK_EXT_SPLIT_A1_GUARD_HOOK OFF CACHE BOOL "Enable inert pad at tud_task_ext() split A1_guard")
endif()

if(NOT DEFINED STPG_USB_TUD_TASK_EXT_SPLIT_A1_BODY0_HOOK)
    set(STPG_USB_TUD_TASK_EXT_SPLIT_A1_BODY0_HOOK OFF CACHE BOOL "Enable inert pad at tud_task_ext() split A1_body0")
endif()

if(NOT DEFINED STPG_USB_TUD_TASK_EXT_SPLIT_A1_TAIL0_HOOK)
    set(STPG_USB_TUD_TASK_EXT_SPLIT_A1_TAIL0_HOOK OFF CACHE BOOL "Enable inert pad at tud_task_ext() split A1_tail0")
endif()

if(NOT DEFINED STPG_USB_TUD_TASK_EXT_SPLIT_A2_HOOK)
    set(STPG_USB_TUD_TASK_EXT_SPLIT_A2_HOOK OFF CACHE BOOL "Enable inert pad at tud_task_ext() split A2")
endif()

if(NOT DEFINED STPG_USB_TUD_TASK_EXT_SPLIT_A3_HOOK)
    set(STPG_USB_TUD_TASK_EXT_SPLIT_A3_HOOK OFF CACHE BOOL "Enable inert pad at tud_task_ext() split A3")
endif()

if(NOT DEFINED STPG_USB_TUD_TASK_EXT_SPLIT_B_HOOK)
    set(STPG_USB_TUD_TASK_EXT_SPLIT_B_HOOK OFF CACHE BOOL "Enable inert pad at tud_task_ext() split B")
endif()

if(NOT DEFINED STPG_USB_TUD_TASK_EXT_SPLIT_C_HOOK)
    set(STPG_USB_TUD_TASK_EXT_SPLIT_C_HOOK OFF CACHE BOOL "Enable inert pad at tud_task_ext() split C")
endif()

if(NOT DEFINED STPG_USB_TUD_TASK_EXT_SPLIT_D_HOOK)
    set(STPG_USB_TUD_TASK_EXT_SPLIT_D_HOOK OFF CACHE BOOL "Enable inert pad at tud_task_ext() split D")
endif()

if(NOT DEFINED STPG_USB_USBD_OPEN_EDPT_PAIR_HOOK)
    set(STPG_USB_USBD_OPEN_EDPT_PAIR_HOOK OFF CACHE BOOL "Enable inert pad at usbd_open_edpt_pair() entry")
endif()

if(NOT DEFINED STPG_USB_TUD_CDC_N_CONNECTED_HOOK)
    set(STPG_USB_TUD_CDC_N_CONNECTED_HOOK OFF CACHE BOOL "Enable inert pad at tud_cdc_n_connected() entry")
endif()

if(NOT DEFINED STPG_USB_BOARD_INIT_HOOK)
    set(STPG_USB_BOARD_INIT_HOOK OFF CACHE BOOL "Enable inert pad at board_init() entry")
endif()

if(NOT DEFINED STPG_USB_L1_AFTER_TUDEXT_LINKER_HOOK)
    set(STPG_USB_L1_AFTER_TUDEXT_LINKER_HOOK OFF CACHE BOOL "Enable linker-driven pad immediately after .text.tud_task_ext")
endif()

if(NOT DEFINED STPG_USB_L1_AFTER_TUDEXT_PAD_NOPS)
    set(STPG_USB_L1_AFTER_TUDEXT_PAD_NOPS 2 CACHE STRING "NOP count for linker-driven pad after .text.tud_task_ext")
endif()

if(NOT DEFINED STPG_USB_L1V2_USBD_REPACK_HOOK)
    set(STPG_USB_L1V2_USBD_REPACK_HOOK OFF CACHE BOOL "Enable object-local usbd.c.o repack after .text.tud_task_ext")
endif()

if(NOT DEFINED STPG_USB_L1V2_PAD_NOPS)
    set(STPG_USB_L1V2_PAD_NOPS 2 CACHE STRING "NOP count for object-local usbd.c.o repack pad")
endif()

if(NOT DEFINED STPG_USB_L2V1_USBD_REPACK_HOOK)
    set(STPG_USB_L2V1_USBD_REPACK_HOOK OFF CACHE BOOL "Enable object-local usbd.c.o repack before .text.tud_task_ext")
endif()

if(NOT DEFINED STPG_USB_L2V1_PAD_NOPS)
    set(STPG_USB_L2V1_PAD_NOPS 2 CACHE STRING "NOP count for object-local usbd.c.o repack pad before .text.tud_task_ext")
endif()

if(NOT DEFINED STPG_USB_TUD_RHPORT_INIT_TRUE_INERT_PAD_HOOK)
    set(STPG_USB_TUD_RHPORT_INIT_TRUE_INERT_PAD_HOOK OFF CACHE BOOL "Enable true inert pad immediately before tud_rhport_init()")
endif()

if(NOT DEFINED STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS)
    set(STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS 76 CACHE STRING "NOP count for TinyUSB startup/task isolated inert pad")
endif()

if(NOT DEFINED STPG_USB_L3_CLUSTER_GROUPING_HOOK)
    set(STPG_USB_L3_CLUSTER_GROUPING_HOOK OFF CACHE BOOL "Enable six-object L3 USB cluster grouping experiment")
endif()

if(NOT DEFINED STPG_USB_L3_CLUSTER_PAD_NOPS)
    set(STPG_USB_L3_CLUSTER_PAD_NOPS 0 CACHE STRING "NOP count for L3 USB cluster grouping pad")
endif()

if(NOT DEFINED STPG_USB_L3V3_CLUSTER_REPACK_HOOK)
    set(STPG_USB_L3V3_CLUSTER_REPACK_HOOK OFF CACHE BOOL "Enable five-object L3 v3 USB cluster repack experiment")
endif()

if(NOT DEFINED STPG_USB_L3V3_CLUSTER_PAD_NOPS)
    set(STPG_USB_L3V3_CLUSTER_PAD_NOPS 0 CACHE STRING "NOP count for L3 v3 USB cluster repack pad")
endif()

include(pico_sdk_import.cmake)

project(stpg_firmware C CXX ASM)
set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)

find_package(Python3 REQUIRED COMPONENTS Interpreter)

pico_sdk_init()

add_executable(stpg_firmware
    src/board_rp2350a_16pin.c
    src/compare.c
    src/command_dispatch.c
    src/crc32.c
    src/flash.c
    src/flash_xip.c
    src/fpu_access.c
    src/main.c
    src/pattern_buffer.c
    src/pattern_validate.c
    src/psram.c
    src/samples.c
    src/stpg_fail_stop.c
    src/stpg_runner.c
    src/status.c
    src/usb_descriptors.c
    src/usb_protocol.c
)

if(STPG_STACK_WATERMARK_DIAGNOSTIC)
    target_sources(stpg_firmware PRIVATE src/stpg_stack_watermark.c)
    target_compile_definitions(stpg_firmware PRIVATE STPG_STACK_WATERMARK_DIAGNOSTIC=1)
endif()

# CPACR may be clear when the RUN path is entered.  This source must not use
# VFP registers before it enables and verifies CP10/CP11 access.
set_source_files_properties(src/fpu_access.c PROPERTIES
    COMPILE_OPTIONS "-mgeneral-regs-only")

if(STPG_USB_L1_AFTER_TUDEXT_LINKER_HOOK)
    target_sources(stpg_firmware PRIVATE
        src/usb_placement_pad.c)
    target_compile_definitions(stpg_firmware PRIVATE
        STPG_USB_L1_AFTER_TUDEXT_PAD_NOPS=${STPG_USB_L1_AFTER_TUDEXT_PAD_NOPS})
    pico_set_linker_script(stpg_firmware
        ${CMAKE_CURRENT_LIST_DIR}/ld/stpg_memmap_usb_l1.ld)
endif()

math(EXPR STPG_USB_PLACEMENT_REPACK_HOOK_COUNT "0")
foreach(_stpg_usb_placement_hook
    STPG_USB_L1_AFTER_TUDEXT_LINKER_HOOK
    STPG_USB_L1V2_USBD_REPACK_HOOK
    STPG_USB_L2V1_USBD_REPACK_HOOK
    STPG_USB_L3_CLUSTER_GROUPING_HOOK
    STPG_USB_L3V3_CLUSTER_REPACK_HOOK)
    if(${_stpg_usb_placement_hook})
        math(EXPR STPG_USB_PLACEMENT_REPACK_HOOK_COUNT "${STPG_USB_PLACEMENT_REPACK_HOOK_COUNT} + 1")
    endif()
endforeach()

if(STPG_USB_PLACEMENT_REPACK_HOOK_COUNT GREATER 1)
    message(FATAL_ERROR "Only one of STPG_USB_L1_AFTER_TUDEXT_LINKER_HOOK, STPG_USB_L1V2_USBD_REPACK_HOOK, STPG_USB_L2V1_USBD_REPACK_HOOK, STPG_USB_L3_CLUSTER_GROUPING_HOOK, or STPG_USB_L3V3_CLUSTER_REPACK_HOOK may be ON")
endif()

if(STPG_USB_L1V2_USBD_REPACK_HOOK OR STPG_USB_L2V1_USBD_REPACK_HOOK)
    get_filename_component(STPG_USB_L1V2_USBD_SRC
        "${PICO_TINYUSB_PATH}/src/device/usbd.c"
        ABSOLUTE)
    if(STPG_USB_L1V2_USBD_REPACK_HOOK)
        add_library(stpg_usb_l1v2_pad OBJECT EXCLUDE_FROM_ALL
            src/usb_placement_pad_l1v2.S)
        target_compile_definitions(stpg_usb_l1v2_pad PRIVATE
            STPG_USB_L1V2_PAD_NOPS=${STPG_USB_L1V2_PAD_NOPS})
        add_dependencies(stpg_firmware stpg_usb_l1v2_pad)
        set(STPG_USB_REPACK_PAD_OBJECT "$<TARGET_OBJECTS:stpg_usb_l1v2_pad>")
        set(STPG_USB_REPACK_PAD_SECTION ".text.stpg_usb_pad_after_tud_task_ext")
        set(STPG_USB_REPACK_POSITION "after")
    else()
        add_library(stpg_usb_l2v1_pad OBJECT EXCLUDE_FROM_ALL
            src/usb_placement_pad_l2v1.S)
        target_compile_definitions(stpg_usb_l2v1_pad PRIVATE
            STPG_USB_L2V1_PAD_NOPS=${STPG_USB_L2V1_PAD_NOPS})
        add_dependencies(stpg_firmware stpg_usb_l2v1_pad)
        set(STPG_USB_REPACK_PAD_OBJECT "$<TARGET_OBJECTS:stpg_usb_l2v1_pad>")
        set(STPG_USB_REPACK_PAD_SECTION ".text.stpg_usb_pad_before_tud_task_ext")
        set(STPG_USB_REPACK_POSITION "before")
    endif()
    set(STPG_USB_L1V2_USBD_OBJ
        "${CMAKE_CURRENT_BINARY_DIR}/CMakeFiles/stpg_firmware.dir${STPG_USB_L1V2_USBD_SRC}.o")
    set(STPG_USB_L1V2_USBD_BACKUP
        "${CMAKE_CURRENT_BINARY_DIR}/usbd_l1v2_original.o")
    set(STPG_USB_L1V2_REPACKED_OBJ
        "${CMAKE_CURRENT_BINARY_DIR}/usbd_l1v2_repacked.o")
    add_custom_command(TARGET stpg_firmware PRE_LINK
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${STPG_USB_L1V2_USBD_OBJ}"
            "${STPG_USB_L1V2_USBD_BACKUP}"
        COMMAND ${Python3_EXECUTABLE}
            "${CMAKE_CURRENT_LIST_DIR}/../tools/scripts/repack_usbd_after_tud_task_ext.py"
            --input-usbd "${STPG_USB_L1V2_USBD_BACKUP}"
            --input-pad "${STPG_USB_REPACK_PAD_OBJECT}"
            --output "${STPG_USB_L1V2_REPACKED_OBJ}"
            --anchor-section ".text.tud_task_ext"
            --pad-section "${STPG_USB_REPACK_PAD_SECTION}"
            --position "${STPG_USB_REPACK_POSITION}"
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${STPG_USB_L1V2_REPACKED_OBJ}"
            "${STPG_USB_L1V2_USBD_OBJ}"
        VERBATIM)
    add_custom_command(TARGET stpg_firmware POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${STPG_USB_L1V2_USBD_BACKUP}"
            "${STPG_USB_L1V2_USBD_OBJ}"
        VERBATIM)
endif()

if(STPG_USB_L3_CLUSTER_GROUPING_HOOK)
    target_sources(stpg_firmware PRIVATE
        src/usb_placement_pad_l3.S)
    target_compile_definitions(stpg_firmware PRIVATE
        STPG_USB_L3_CLUSTER_PAD_NOPS=${STPG_USB_L3_CLUSTER_PAD_NOPS})
    pico_set_linker_script(stpg_firmware
        ${CMAKE_CURRENT_LIST_DIR}/ld/stpg_memmap_usb_l3v2.ld)
endif()

if(STPG_USB_L3V3_CLUSTER_REPACK_HOOK)
    get_filename_component(STPG_USB_L3V3_USBD_SRC
        "${PICO_TINYUSB_PATH}/src/device/usbd.c"
        ABSOLUTE)
    get_filename_component(STPG_USB_L3V3_USBD_CONTROL_SRC
        "${PICO_TINYUSB_PATH}/src/device/usbd_control.c"
        ABSOLUTE)
    get_filename_component(STPG_USB_L3V3_DCD_SRC
        "${PICO_TINYUSB_PATH}/src/portable/raspberrypi/rp2040/dcd_rp2040.c"
        ABSOLUTE)
    get_filename_component(STPG_USB_L3V3_TUSB_SRC
        "${PICO_TINYUSB_PATH}/src/tusb.c"
        ABSOLUTE)
    get_filename_component(STPG_USB_L3V3_FIFO_SRC
        "${PICO_TINYUSB_PATH}/src/common/tusb_fifo.c"
        ABSOLUTE)

    add_library(stpg_usb_l3v3_pad OBJECT EXCLUDE_FROM_ALL
        src/usb_placement_pad_l3.S)
    target_compile_definitions(stpg_usb_l3v3_pad PRIVATE
        STPG_USB_L3_CLUSTER_PAD_NOPS=${STPG_USB_L3V3_CLUSTER_PAD_NOPS})
    add_library(stpg_usb_empty OBJECT EXCLUDE_FROM_ALL
        src/usb_placement_empty.S)
    add_dependencies(stpg_firmware stpg_usb_l3v3_pad stpg_usb_empty)
    target_link_options(stpg_firmware PRIVATE
        "-Wl,-u,stpg_usb_l3_cluster_pad")

    set(STPG_USB_L3V3_USBD_OBJ
        "${CMAKE_CURRENT_BINARY_DIR}/CMakeFiles/stpg_firmware.dir${STPG_USB_L3V3_USBD_SRC}.o")
    set(STPG_USB_L3V3_USBD_CONTROL_OBJ
        "${CMAKE_CURRENT_BINARY_DIR}/CMakeFiles/stpg_firmware.dir${STPG_USB_L3V3_USBD_CONTROL_SRC}.o")
    set(STPG_USB_L3V3_DCD_OBJ
        "${CMAKE_CURRENT_BINARY_DIR}/CMakeFiles/stpg_firmware.dir${STPG_USB_L3V3_DCD_SRC}.o")
    set(STPG_USB_L3V3_TUSB_OBJ
        "${CMAKE_CURRENT_BINARY_DIR}/CMakeFiles/stpg_firmware.dir${STPG_USB_L3V3_TUSB_SRC}.o")
    set(STPG_USB_L3V3_FIFO_OBJ
        "${CMAKE_CURRENT_BINARY_DIR}/CMakeFiles/stpg_firmware.dir${STPG_USB_L3V3_FIFO_SRC}.o")

    set(STPG_USB_L3V3_USBD_BACKUP
        "${CMAKE_CURRENT_BINARY_DIR}/usbd_l3v3_original.o")
    set(STPG_USB_L3V3_USBD_CONTROL_BACKUP
        "${CMAKE_CURRENT_BINARY_DIR}/usbd_control_l3v3_original.o")
    set(STPG_USB_L3V3_DCD_BACKUP
        "${CMAKE_CURRENT_BINARY_DIR}/dcd_rp2040_l3v3_original.o")
    set(STPG_USB_L3V3_TUSB_BACKUP
        "${CMAKE_CURRENT_BINARY_DIR}/tusb_l3v3_original.o")
    set(STPG_USB_L3V3_FIFO_BACKUP
        "${CMAKE_CURRENT_BINARY_DIR}/tusb_fifo_l3v3_original.o")

    set(STPG_USB_L3V3_REPACKED_OBJ
        "${CMAKE_CURRENT_BINARY_DIR}/usb_cluster_l3v3_repacked.o")

    add_custom_command(TARGET stpg_firmware PRE_LINK
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${STPG_USB_L3V3_USBD_OBJ}"
            "${STPG_USB_L3V3_USBD_BACKUP}"
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${STPG_USB_L3V3_USBD_CONTROL_OBJ}"
            "${STPG_USB_L3V3_USBD_CONTROL_BACKUP}"
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${STPG_USB_L3V3_DCD_OBJ}"
            "${STPG_USB_L3V3_DCD_BACKUP}"
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${STPG_USB_L3V3_TUSB_OBJ}"
            "${STPG_USB_L3V3_TUSB_BACKUP}"
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${STPG_USB_L3V3_FIFO_OBJ}"
            "${STPG_USB_L3V3_FIFO_BACKUP}"
        COMMAND ${Python3_EXECUTABLE}
            "${CMAKE_CURRENT_LIST_DIR}/../tools/scripts/repack_usb_cluster.py"
            --output "${STPG_USB_L3V3_REPACKED_OBJ}"
            --input-pad "$<TARGET_OBJECTS:stpg_usb_l3v3_pad>"
            --anchor-object "${STPG_USB_L3V3_USBD_BACKUP}"
            --anchor-section ".time_critical.tinyusb"
            --pad-section ".stpg_usb_l3_cluster_pad"
            --position "after"
            --inputs
                "${STPG_USB_L3V3_DCD_BACKUP}"
                "${STPG_USB_L3V3_USBD_BACKUP}"
                "${STPG_USB_L3V3_USBD_CONTROL_BACKUP}"
                "${STPG_USB_L3V3_TUSB_BACKUP}"
                "${STPG_USB_L3V3_FIFO_BACKUP}"
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${STPG_USB_L3V3_REPACKED_OBJ}"
            "${STPG_USB_L3V3_USBD_OBJ}"
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "$<TARGET_OBJECTS:stpg_usb_empty>"
            "${STPG_USB_L3V3_USBD_CONTROL_OBJ}"
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "$<TARGET_OBJECTS:stpg_usb_empty>"
            "${STPG_USB_L3V3_DCD_OBJ}"
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "$<TARGET_OBJECTS:stpg_usb_empty>"
            "${STPG_USB_L3V3_TUSB_OBJ}"
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "$<TARGET_OBJECTS:stpg_usb_empty>"
            "${STPG_USB_L3V3_FIFO_OBJ}"
        VERBATIM)

    add_custom_command(TARGET stpg_firmware POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${STPG_USB_L3V3_USBD_BACKUP}"
            "${STPG_USB_L3V3_USBD_OBJ}"
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${STPG_USB_L3V3_USBD_CONTROL_BACKUP}"
            "${STPG_USB_L3V3_USBD_CONTROL_OBJ}"
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${STPG_USB_L3V3_DCD_BACKUP}"
            "${STPG_USB_L3V3_DCD_OBJ}"
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${STPG_USB_L3V3_TUSB_BACKUP}"
            "${STPG_USB_L3V3_TUSB_OBJ}"
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${STPG_USB_L3V3_FIFO_BACKUP}"
            "${STPG_USB_L3V3_FIFO_OBJ}"
        VERBATIM)
endif()

pico_generate_pio_header(stpg_firmware
    ${CMAKE_CURRENT_LIST_DIR}/src/stpg_runner.pio
)
pico_generate_pio_header(stpg_firmware
    ${CMAKE_CURRENT_LIST_DIR}/src/stpg_runner27.pio
)
pico_generate_pio_header(stpg_firmware
    ${CMAKE_CURRENT_LIST_DIR}/src/stpg_sample_in.pio
)
pico_generate_pio_header(stpg_firmware
    ${CMAKE_CURRENT_LIST_DIR}/src/stpg_sample_in27.pio
)
pico_generate_pio_header(stpg_firmware
    ${CMAKE_CURRENT_LIST_DIR}/src/stpg_runner_reduced.pio
)
pico_generate_pio_header(stpg_firmware
    ${CMAKE_CURRENT_LIST_DIR}/src/stpg_runner27_reduced.pio
)
pico_generate_pio_header(stpg_firmware
    ${CMAKE_CURRENT_LIST_DIR}/src/stpg_sample_delayed_in.pio
)
pico_generate_pio_header(stpg_firmware
    ${CMAKE_CURRENT_LIST_DIR}/src/stpg_sample_delayed_in27.pio
)
pico_generate_pio_header(stpg_firmware
    ${CMAKE_CURRENT_LIST_DIR}/src/stpg_sample_delayed_schedule_in.pio
)
pico_generate_pio_header(stpg_firmware
    ${CMAKE_CURRENT_LIST_DIR}/src/stpg_sample_delayed_schedule_in27.pio
)

target_include_directories(stpg_firmware PRIVATE include)

target_compile_definitions(stpg_firmware PRIVATE
    STPG_CLOCK_MODE=${STPG_CLOCK_MODE}
    STPG_FW_DIAGNOSTIC=${STPG_FW_DIAGNOSTIC}
    STPG_PSRAM_QMI_CLKDIV=${STPG_PSRAM_QMI_CLKDIV}
    STPG_PSRAM_QMI_RXDELAY=${STPG_PSRAM_QMI_RXDELAY}
    STPG_PSRAM_QMI_PAGEBREAK=${STPG_PSRAM_QMI_PAGEBREAK}
    STPG_PSRAM_XIP_STREAM_DMA_MIN_BYTES=${STPG_PSRAM_XIP_STREAM_DMA_MIN_BYTES}
    STPG_MAX_RATES=${STPG_MAX_RATES}
    STPG_PSRAM_PATTERN_BASE=${STPG_PSRAM_PATTERN_BASE}
    STPG_PSRAM_PATTERN_BYTES=${STPG_PSRAM_PATTERN_BYTES}
    STPG_PSRAM_RESULT_BASE=${STPG_PSRAM_RESULT_BASE}
    STPG_PSRAM_RESULT_BYTES=${STPG_PSRAM_RESULT_BYTES}
    STPG_PSRAM_STAGING_BASE=${STPG_PSRAM_STAGING_BASE}
    STPG_PSRAM_STAGING_BYTES=${STPG_PSRAM_STAGING_BYTES}
    STPG_STREAM_PRACTICAL_PROFILE=$<BOOL:${STPG_STREAM_PRACTICAL_PROFILE}>
    STPG_STREAM_LOOP_BATCH=$<BOOL:${STPG_STREAM_LOOP_BATCH}>
    STPG_STREAM_VECTOR_CACHE_NEXT_RECORDS=${STPG_STREAM_VECTOR_CACHE_NEXT_RECORDS}
    STPG_STREAM_VECTOR_CACHE_READ_RECORDS=${STPG_STREAM_VECTOR_CACHE_READ_RECORDS}
    STPG_STREAM_SLOT_COUNT=${STPG_STREAM_SLOT_COUNT}
    STPG_STREAM_BUFFER_EVENTS=${STPG_STREAM_BUFFER_EVENTS}
    STPG_FAIL_STOP=$<BOOL:${STPG_FAIL_STOP}>
    STPG_STREAM_FAIL_STOP=$<BOOL:${STPG_STREAM_FAIL_STOP}>
    STPG_STREAM_PARTIAL_REPEAT_FAST_PATH=$<BOOL:${STPG_STREAM_PARTIAL_REPEAT_FAST_PATH}>
    STPG_STREAM_HOT_TELEMETRY=$<BOOL:${STPG_STREAM_HOT_TELEMETRY}>
    STPG_STREAM_RATE_TEMPLATE_FILL=$<BOOL:${STPG_STREAM_RATE_TEMPLATE_FILL}>
    STPG_STREAM_COMPARE_FREE_BLOCK_FILL=$<BOOL:${STPG_STREAM_COMPARE_FREE_BLOCK_FILL}>
    STPG_STREAM_PRODUCER_SRAM=$<BOOL:${STPG_STREAM_PRODUCER_SRAM}>
    STPG_STREAM_REPEAT_BLOCK_FILL=$<BOOL:${STPG_STREAM_REPEAT_BLOCK_FILL}>
    STPG_STREAM_REPEAT_BOUNDARY_FILL=$<BOOL:${STPG_STREAM_REPEAT_BOUNDARY_FILL}>
    STPG_STREAM_REPEAT_BOUNDARY_CONTINUE=$<BOOL:${STPG_STREAM_REPEAT_BOUNDARY_CONTINUE}>
    STPG_STREAM_REPEAT_BOUNDARY_REARM=$<BOOL:${STPG_STREAM_REPEAT_BOUNDARY_REARM}>
    STPG_STREAM_COMPARE_STREAMING=$<BOOL:${STPG_STREAM_COMPARE_STREAMING}>
    STPG_STREAM_PSRAM_COMPARE_SAMPLE=$<BOOL:${STPG_STREAM_PSRAM_COMPARE_SAMPLE}>
    STPG_SAMPLE_PSRAM_SRAM_STAGING=$<BOOL:${STPG_SAMPLE_PSRAM_SRAM_STAGING}>
    STPG_STREAM_COMPARE_SAMPLE_REPAIR=$<BOOL:${STPG_STREAM_COMPARE_SAMPLE_REPAIR}>
    STPG_STREAM_COMPARE_MIXED_RATE_REPAIR=$<BOOL:${STPG_STREAM_COMPARE_MIXED_RATE_REPAIR}>
    STPG_STREAM_COMPARE_EVENT_SCHEDULE=$<BOOL:${STPG_STREAM_COMPARE_EVENT_SCHEDULE}>
    STPG_STREAM_COMPARE_EVENT_SCHEDULE_HANDOFF=$<BOOL:${STPG_STREAM_COMPARE_EVENT_SCHEDULE_HANDOFF}>
    STPG_STREAM_COMPARE_EVENT_SCHEDULE_CHAIN_HANDOFF=$<BOOL:${STPG_STREAM_COMPARE_EVENT_SCHEDULE_CHAIN_HANDOFF}>
    STPG_STREAM_STATUS_LOGICAL_EVENTS=$<BOOL:${STPG_STREAM_STATUS_LOGICAL_EVENTS}>
    STPG_STREAM_LARGE_RATE_VECTOR=$<BOOL:${STPG_STREAM_LARGE_RATE_VECTOR}>
    STPG_STREAM_RATE_VECTOR_EVENT_LIMIT=${STPG_STREAM_RATE_VECTOR_EVENT_LIMIT}
    STPG_STREAM_PSRAM_CAPACITY_RATE_VECTOR=$<BOOL:${STPG_STREAM_PSRAM_CAPACITY_RATE_VECTOR}>
    STPG_STREAM_FIFO_FILL_REQUESTS=$<BOOL:${STPG_STREAM_FIFO_FILL_REQUESTS}>
    STPG_STREAM_PRECOMPUTED_UOP_CAPACITY=${STPG_STREAM_PRECOMPUTED_UOP_CAPACITY}
    STPG_STREAM_DENSE_VECTOR_FILL=$<BOOL:${STPG_STREAM_DENSE_VECTOR_FILL}>
    PICO_DEFAULT_UART_TX_PIN=0
    PICO_DEFAULT_UART_RX_PIN=1
)

if(STPG_REFILL_TRACE_DIAGNOSTIC)
    target_compile_definitions(stpg_firmware PRIVATE
        STPG_REFILL_TRACE_DIAGNOSTIC=1)
endif()

if(STPG_ENABLE_LINK_MAP)
    target_link_options(stpg_firmware PRIVATE
        "-Wl,-Map=${CMAKE_CURRENT_BINARY_DIR}/stpg_firmware.map")
endif()

if(STPG_ENABLE_STACK_USAGE)
    target_compile_options(stpg_firmware PRIVATE -fstack-usage)
endif()

if(STPG_PREFETCH_PHASE1_MAP_HOOK)
    target_compile_definitions(stpg_firmware PRIVATE
        STPG_PREFETCH_PHASE1_MAP_HOOK=1)
endif()

if(STPG_PREFETCH_PHASE1_TRUE_INERT_PAD_HOOK)
    target_compile_definitions(stpg_firmware PRIVATE
        STPG_PREFETCH_PHASE1_TRUE_INERT_PAD_HOOK=1
        STPG_PREFETCH_PHASE1_TRUE_INERT_PAD_NOPS=${STPG_PREFETCH_PHASE1_TRUE_INERT_PAD_NOPS})
endif()

if(STPG_USB_TUD_TASK_EXT_TRUE_INERT_PAD_HOOK)
    add_compile_definitions(
        STPG_USB_TUD_TASK_EXT_TRUE_INERT_PAD_HOOK=1
        STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS=${STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS})
endif()

if(STPG_USB_TUD_TASK_EXT_START_PHASE_SHIFT_HOOK)
    add_compile_definitions(
        STPG_USB_TUD_TASK_EXT_START_PHASE_SHIFT_HOOK=1
        STPG_USB_TUD_TASK_EXT_START_PHASE_SHIFT_NOPS=${STPG_USB_TUD_TASK_EXT_START_PHASE_SHIFT_NOPS})
endif()

set(STPG_USB_TUD_TASK_EXT_SPLIT_HOOK_COUNT 0)
foreach(_split_hook
    STPG_USB_TUD_TASK_EXT_START_PHASE_SHIFT_HOOK
    STPG_USB_TUD_TASK_EXT_SPLIT_A_HOOK
    STPG_USB_TUD_TASK_EXT_SPLIT_A1_HOOK
    STPG_USB_TUD_TASK_EXT_SPLIT_A1_PRE_HOOK
    STPG_USB_TUD_TASK_EXT_SPLIT_A1_POST_HOOK
    STPG_USB_TUD_TASK_EXT_SPLIT_A1_GUARD_HOOK
    STPG_USB_TUD_TASK_EXT_SPLIT_A1_BODY0_HOOK
    STPG_USB_TUD_TASK_EXT_SPLIT_A1_TAIL0_HOOK
    STPG_USB_TUD_TASK_EXT_SPLIT_A2_HOOK
    STPG_USB_TUD_TASK_EXT_SPLIT_A3_HOOK
    STPG_USB_TUD_TASK_EXT_SPLIT_B_HOOK
    STPG_USB_TUD_TASK_EXT_SPLIT_C_HOOK
    STPG_USB_TUD_TASK_EXT_SPLIT_D_HOOK)
    if(${_split_hook})
        math(EXPR STPG_USB_TUD_TASK_EXT_SPLIT_HOOK_COUNT "${STPG_USB_TUD_TASK_EXT_SPLIT_HOOK_COUNT} + 1")
    endif()
endforeach()

if(STPG_USB_TUD_TASK_EXT_SPLIT_HOOK_COUNT GREATER 1)
    message(FATAL_ERROR "Only one of STPG_USB_TUD_TASK_EXT_START_PHASE_SHIFT_HOOK or STPG_USB_TUD_TASK_EXT_SPLIT_{A,A1,A1_PRE,A1_POST,A1_GUARD,A1_BODY0,A1_TAIL0,A2,A3,B,C,D}_HOOK may be ON at a time")
endif()

if(STPG_USB_TUD_TASK_EXT_SPLIT_A_HOOK)
    add_compile_definitions(
        STPG_USB_TUD_TASK_EXT_SPLIT_A_HOOK=1
        STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS=${STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS})
endif()

if(STPG_USB_TUD_TASK_EXT_SPLIT_A1_HOOK)
    add_compile_definitions(
        STPG_USB_TUD_TASK_EXT_SPLIT_A1_HOOK=1
        STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS=${STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS})
endif()

if(STPG_USB_TUD_TASK_EXT_SPLIT_A1_PRE_HOOK)
    add_compile_definitions(
        STPG_USB_TUD_TASK_EXT_SPLIT_A1_PRE_HOOK=1
        STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS=${STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS})
endif()

if(STPG_USB_TUD_TASK_EXT_SPLIT_A1_POST_HOOK)
    add_compile_definitions(
        STPG_USB_TUD_TASK_EXT_SPLIT_A1_POST_HOOK=1
        STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS=${STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS})
endif()

if(STPG_USB_TUD_TASK_EXT_SPLIT_A1_GUARD_HOOK)
    add_compile_definitions(
        STPG_USB_TUD_TASK_EXT_SPLIT_A1_GUARD_HOOK=1
        STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS=${STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS})
endif()

if(STPG_USB_TUD_TASK_EXT_SPLIT_A1_BODY0_HOOK)
    add_compile_definitions(
        STPG_USB_TUD_TASK_EXT_SPLIT_A1_BODY0_HOOK=1
        STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS=${STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS})
endif()

if(STPG_USB_TUD_TASK_EXT_SPLIT_A1_TAIL0_HOOK)
    add_compile_definitions(
        STPG_USB_TUD_TASK_EXT_SPLIT_A1_TAIL0_HOOK=1
        STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS=${STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS})
endif()

if(STPG_USB_TUD_TASK_EXT_SPLIT_A2_HOOK)
    add_compile_definitions(
        STPG_USB_TUD_TASK_EXT_SPLIT_A2_HOOK=1
        STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS=${STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS})
endif()

if(STPG_USB_TUD_TASK_EXT_SPLIT_A3_HOOK)
    add_compile_definitions(
        STPG_USB_TUD_TASK_EXT_SPLIT_A3_HOOK=1
        STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS=${STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS})
endif()

if(STPG_USB_TUD_TASK_EXT_SPLIT_B_HOOK)
    add_compile_definitions(
        STPG_USB_TUD_TASK_EXT_SPLIT_B_HOOK=1
        STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS=${STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS})
endif()

if(STPG_USB_TUD_TASK_EXT_SPLIT_C_HOOK)
    add_compile_definitions(
        STPG_USB_TUD_TASK_EXT_SPLIT_C_HOOK=1
        STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS=${STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS})
endif()

if(STPG_USB_TUD_TASK_EXT_SPLIT_D_HOOK)
    add_compile_definitions(
        STPG_USB_TUD_TASK_EXT_SPLIT_D_HOOK=1
        STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS=${STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS})
endif()

if(STPG_USB_USBD_OPEN_EDPT_PAIR_HOOK)
    add_compile_definitions(
        STPG_USB_USBD_OPEN_EDPT_PAIR_HOOK=1
        STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS=${STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS})
endif()

if(STPG_USB_TUD_CDC_N_CONNECTED_HOOK)
    add_compile_definitions(
        STPG_USB_TUD_CDC_N_CONNECTED_HOOK=1
        STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS=${STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS})
endif()

if(STPG_USB_BOARD_INIT_HOOK)
    add_compile_definitions(
        STPG_USB_BOARD_INIT_HOOK=1
        STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS=${STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS})
endif()

if(STPG_USB_TUD_RHPORT_INIT_TRUE_INERT_PAD_HOOK)
    add_compile_definitions(
        STPG_USB_TUD_RHPORT_INIT_TRUE_INERT_PAD_HOOK=1
        STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS=${STPG_USB_STARTUP_TRUE_INERT_PAD_NOPS})
endif()

target_link_libraries(stpg_firmware PRIVATE
    hardware_dma
    pico_stdlib
    pico_flash
    pico_multicore
    hardware_pio
    pico_unique_id
    hardware_clocks
    hardware_gpio
    hardware_watchdog
    tinyusb_device
    tinyusb_board
)

pico_enable_stdio_usb(stpg_firmware 0)
pico_enable_stdio_uart(stpg_firmware 1)

pico_add_extra_outputs(stpg_firmware)

add_executable(gpio19_toggle_smoke
    src/gpio19_toggle_smoke.c
)

target_link_libraries(gpio19_toggle_smoke PRIVATE
    pico_stdlib
    hardware_gpio
)

pico_enable_stdio_usb(gpio19_toggle_smoke 0)
pico_enable_stdio_uart(gpio19_toggle_smoke 0)

pico_add_extra_outputs(gpio19_toggle_smoke)

add_executable(swd_probe_smoke
    src/swd_probe_smoke.c
)

target_link_libraries(swd_probe_smoke PRIVATE
    pico_stdlib
    hardware_gpio
)

pico_enable_stdio_usb(swd_probe_smoke 0)
pico_enable_stdio_uart(swd_probe_smoke 0)

pico_add_extra_outputs(swd_probe_smoke)
