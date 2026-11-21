cmake_minimum_required(VERSION 3.13)

project(stpg_header_check C)
set(CMAKE_C_STANDARD 11)

if(NOT DEFINED STPG_MAX_RATES)
    set(STPG_MAX_RATES 64 CACHE STRING "Maximum admitted Rate Vector descriptors")
endif()

if(STPG_MAX_RATES LESS 1 OR STPG_MAX_RATES GREATER 255)
    message(FATAL_ERROR "STPG_MAX_RATES must be in the range 1..255")
endif()

add_compile_definitions(STPG_MAX_RATES=${STPG_MAX_RATES})
# Many existing host tests use assert(); Release must not silently erase them.
add_compile_options(-UNDEBUG)

add_executable(check_headers check_headers.c)
target_include_directories(check_headers PRIVATE ../include)
target_compile_options(check_headers PRIVATE -Wall -Wextra -Werror)

add_executable(test_crc32 test_crc32.c ../src/crc32.c)
target_include_directories(test_crc32 PRIVATE ../include)
target_compile_options(test_crc32 PRIVATE -Wall -Wextra -Werror)

add_executable(test_fail_stop test_fail_stop.c ../src/stpg_fail_stop.c)
target_include_directories(test_fail_stop PRIVATE ../include)
target_compile_options(test_fail_stop PRIVATE -Wall -Wextra -Werror)

add_executable(test_fpu_access test_fpu_access.c ../src/fpu_access.c)
target_include_directories(test_fpu_access PRIVATE ../include)
target_compile_options(test_fpu_access PRIVATE -Wall -Wextra -Werror)

add_executable(test_stream_handoff test_stream_handoff.c)
target_include_directories(test_stream_handoff PRIVATE ../include)
target_compile_options(test_stream_handoff PRIVATE -Wall -Wextra -Werror)

foreach(mode 0 1)
    add_executable(test_stream_watchdog_${mode} test_stream_watchdog.c)
    target_include_directories(test_stream_watchdog_${mode} PRIVATE ../include)
    target_compile_definitions(test_stream_watchdog_${mode} PRIVATE STPG_CLOCK_MODE=${mode})
    target_compile_options(test_stream_watchdog_${mode} PRIVATE -Wall -Wextra -Werror)
    add_test(NAME test_stream_watchdog_${mode} COMMAND test_stream_watchdog_${mode})
endforeach()

add_executable(test_pattern_validate
    test_pattern_validate.c
    board_host_stub.c
    ../src/crc32.c
    ../src/pattern_validate.c
)
target_include_directories(test_pattern_validate PRIVATE ../include)
target_compile_options(test_pattern_validate PRIVATE -Wall -Wextra -Werror)
# The embedded minimal binary fixture has a pinned 150 MHz header. These
# regression tests validate that historical fixture, not the production
# firmware's newly selected default clock.
target_compile_definitions(test_pattern_validate PRIVATE STPG_CLOCK_MODE=0)

add_executable(test_pattern_buffer
    test_pattern_buffer.c
    board_host_stub.c
    ../src/crc32.c
    ../src/flash.c
    ../src/pattern_buffer.c
    ../src/pattern_validate.c
    ../src/psram.c
)
target_include_directories(test_pattern_buffer PRIVATE ../include)
target_compile_options(test_pattern_buffer PRIVATE -Wall -Wextra -Werror)

add_executable(test_status
    test_status.c
    board_host_stub.c
    ../src/crc32.c
    ../src/flash.c
    ../src/pattern_buffer.c
    ../src/pattern_validate.c
    ../src/psram.c
    ../src/status.c
)
target_include_directories(test_status PRIVATE ../include)
target_compile_options(test_status PRIVATE -Wall -Wextra -Werror)

add_executable(test_psram
    test_psram.c
    board_host_stub.c
    ../src/crc32.c
    ../src/flash.c
    ../src/pattern_buffer.c
    ../src/pattern_validate.c
    ../src/psram.c
)
target_include_directories(test_psram PRIVATE ../include)
target_compile_options(test_psram PRIVATE -Wall -Wextra -Werror)

add_executable(test_samples
    test_samples.c
    board_host_stub.c
    ../src/crc32.c
    ../src/samples.c
)
target_include_directories(test_samples PRIVATE ../include)
target_compile_options(test_samples PRIVATE -Wall -Wextra -Werror)

foreach(staging 0 1 default)
    add_executable(test_sample_sram_staging_${staging}
        test_sample_sram_staging.c board_host_stub.c
        ../src/crc32.c ../src/samples.c ../src/psram.c)
    target_include_directories(test_sample_sram_staging_${staging} PRIVATE ../include)
    target_compile_options(test_sample_sram_staging_${staging} PRIVATE -Wall -Wextra -Werror)
    target_compile_definitions(test_sample_sram_staging_${staging} PRIVATE
        STPG_STREAM_PSRAM_COMPARE_SAMPLE=1)
    if(NOT staging STREQUAL "default")
        target_compile_definitions(test_sample_sram_staging_${staging} PRIVATE
            STPG_SAMPLE_PSRAM_SRAM_STAGING=${staging})
    endif()
    add_test(NAME test_sample_sram_staging_${staging} COMMAND test_sample_sram_staging_${staging})
endforeach()

add_executable(test_compare
    test_compare.c
    board_host_stub.c
    ../src/compare.c
    ../src/crc32.c
    ../src/samples.c
)
target_include_directories(test_compare PRIVATE ../include)
target_compile_options(test_compare PRIVATE -Wall -Wextra -Werror)

add_executable(test_rate_vector_run
    test_rate_vector_run.c
    board_host_stub.c
    ../src/crc32.c
    ../src/flash.c
    ../src/pattern_buffer.c
    ../src/pattern_validate.c
    ../src/psram.c
)
target_include_directories(test_rate_vector_run PRIVATE ../include)
target_compile_options(test_rate_vector_run PRIVATE -Wall -Wextra -Werror)

add_executable(test_rate_vector_wide
    test_rate_vector_wide.c
    board_host_stub.c
    ../src/crc32.c
    ../src/flash.c
    ../src/pattern_buffer.c
    ../src/pattern_validate.c
    ../src/psram.c
)
target_include_directories(test_rate_vector_wide PRIVATE ../include)
target_compile_options(test_rate_vector_wide PRIVATE -Wall -Wextra -Werror)

add_executable(test_rate_vector_streaming
    test_rate_vector_streaming.c
    board_host_stub.c
    ../src/crc32.c
    ../src/flash.c
    ../src/pattern_buffer.c
    ../src/pattern_validate.c
    ../src/psram.c
)
target_include_directories(test_rate_vector_streaming PRIVATE ../include)
target_compile_options(test_rate_vector_streaming PRIVATE -Wall -Wextra -Werror)
target_compile_definitions(test_rate_vector_streaming PRIVATE
    STPG_STREAM_COMPARE_FREE_BLOCK_FILL=1
    STPG_STREAM_COMPARE_SAMPLE_REPAIR=1
    STPG_STREAM_COMPARE_MIXED_RATE_REPAIR=1
    STPG_STREAM_COMPARE_EVENT_SCHEDULE=1
    STPG_STREAM_COMPARE_EVENT_SCHEDULE_HANDOFF=1
    STPG_STREAM_COMPARE_EVENT_SCHEDULE_CHAIN_HANDOFF=1
    STPG_STREAM_STATUS_LOGICAL_EVENTS=1
    STPG_STREAM_LARGE_RATE_VECTOR=1
)

add_executable(test_rate_vector_streaming_fail_stop
    test_rate_vector_streaming.c
    board_host_stub.c
    ../src/crc32.c
    ../src/flash.c
    ../src/pattern_buffer.c
    ../src/pattern_validate.c
    ../src/psram.c
)
target_include_directories(test_rate_vector_streaming_fail_stop PRIVATE ../include)
target_compile_options(test_rate_vector_streaming_fail_stop PRIVATE
    -Wall -Wextra -Werror -Wshadow
)
target_compile_definitions(test_rate_vector_streaming_fail_stop PRIVATE
    STPG_STREAM_COMPARE_FREE_BLOCK_FILL=1
    STPG_STREAM_COMPARE_SAMPLE_REPAIR=1
    STPG_STREAM_COMPARE_MIXED_RATE_REPAIR=1
    STPG_STREAM_COMPARE_EVENT_SCHEDULE=1
    STPG_STREAM_COMPARE_EVENT_SCHEDULE_HANDOFF=1
    STPG_STREAM_COMPARE_EVENT_SCHEDULE_CHAIN_HANDOFF=1
    STPG_STREAM_STATUS_LOGICAL_EVENTS=1
    STPG_STREAM_LARGE_RATE_VECTOR=1
    STPG_STREAM_FAIL_STOP=1
)

add_executable(test_rate_vector_loop
    test_rate_vector_loop.c
    board_host_stub.c
    ../src/crc32.c
    ../src/flash.c
    ../src/pattern_buffer.c
    ../src/pattern_validate.c
    ../src/psram.c
)

add_executable(test_flash
    test_flash.c
    board_host_stub.c
    ../src/crc32.c
    ../src/flash.c
    ../src/pattern_buffer.c
    ../src/pattern_validate.c
    ../src/psram.c
)
target_include_directories(test_flash PRIVATE ../include)
target_compile_options(test_flash PRIVATE -Wall -Wextra -Werror)
target_include_directories(test_rate_vector_loop PRIVATE ../include)
target_compile_options(test_rate_vector_loop PRIVATE -Wall -Wextra -Werror)
target_compile_definitions(test_rate_vector_loop PRIVATE
    STPG_STREAM_RATE_VECTOR_EVENT_LIMIT=4294967295u
)

enable_testing()
add_executable(test_reset_dispatch test_reset_dispatch.c)
target_include_directories(test_reset_dispatch PRIVATE ../include)
target_compile_options(test_reset_dispatch PRIVATE -Wall -Wextra -Werror -Wno-unused-function -ffunction-sections -fdata-sections)
target_link_options(test_reset_dispatch PRIVATE -Wl,--gc-sections)
add_test(NAME test_reset_dispatch COMMAND test_reset_dispatch)
add_executable(test_board_release test_board_release.c ../src/board_rp2350a_16pin.c)
target_include_directories(test_board_release PRIVATE gpio_stub ../include)
target_compile_options(test_board_release PRIVATE -Wall -Wextra -Werror)
add_test(NAME test_board_release COMMAND test_board_release)
foreach(cache_profile IN ITEMS practical practical_hot failstop practical_templates)
    set(cache_test test_precomputed_uop_cache_${cache_profile})
    add_executable(${cache_test}
        test_precomputed_uop_cache.c board_host_stub.c
        ../src/crc32.c ../src/flash.c ../src/pattern_validate.c
        ../src/psram.c ../src/compare.c ../src/samples.c)
    target_include_directories(${cache_test} PRIVATE ../include)
    target_compile_options(${cache_test} PRIVATE -Wall -Wextra -Werror)
    target_compile_definitions(${cache_test} PRIVATE
        STPG_STREAM_COMPARE_STREAMING=1 STPG_STREAM_LOOP_BATCH=1
        STPG_STREAM_FIFO_FILL_REQUESTS=1 STPG_STREAM_PRECOMPUTED_UOP_CAPACITY=32
        STPG_STREAM_RATE_VECTOR_EVENT_LIMIT=4294967295)
    if(cache_profile STREQUAL "practical_templates")
        target_compile_definitions(${cache_test} PRIVATE STPG_STREAM_RATE_TEMPLATE_FILL=1)
    endif()
    if(cache_profile STREQUAL "failstop")
        target_compile_definitions(${cache_test} PRIVATE
            STPG_STREAM_PRACTICAL_PROFILE=0 STPG_STREAM_PARTIAL_REPEAT_FAST_PATH=0
            STPG_STREAM_FAIL_STOP=1 STPG_STREAM_PSRAM_COMPARE_SAMPLE=0 STPG_STREAM_HOT_TELEMETRY=0)
    else()
        target_compile_definitions(${cache_test} PRIVATE
            STPG_STREAM_PRACTICAL_PROFILE=1 STPG_STREAM_PARTIAL_REPEAT_FAST_PATH=1
            STPG_STREAM_PSRAM_COMPARE_SAMPLE=1)
        if(cache_profile STREQUAL "practical_hot")
            target_compile_definitions(${cache_test} PRIVATE STPG_STREAM_HOT_TELEMETRY=1)
        else()
            target_compile_definitions(${cache_test} PRIVATE STPG_STREAM_HOT_TELEMETRY=0)
        endif()
    endif()
    add_test(NAME ${cache_test} COMMAND ${cache_test})
endforeach()
add_test(NAME check_headers COMMAND check_headers)
add_test(NAME test_crc32 COMMAND test_crc32)
add_test(NAME test_fail_stop COMMAND test_fail_stop)
add_test(NAME test_fpu_access COMMAND test_fpu_access)
add_test(NAME test_stream_handoff COMMAND test_stream_handoff)
add_test(NAME test_pattern_validate COMMAND test_pattern_validate)
add_test(NAME test_pattern_buffer COMMAND test_pattern_buffer)
add_test(NAME test_status COMMAND test_status)
add_test(NAME test_psram COMMAND test_psram)
add_test(NAME test_samples COMMAND test_samples)
add_test(NAME test_compare COMMAND test_compare)
add_test(NAME test_rate_vector_run COMMAND test_rate_vector_run)
add_test(NAME test_rate_vector_wide COMMAND test_rate_vector_wide)

add_executable(test_rate_vector_r34_exact
    test_rate_vector_r34_exact.c
    board_host_stub.c
    ../src/crc32.c
    ../src/flash.c
    ../src/pattern_buffer.c
    ../src/pattern_validate.c
    ../src/psram.c
)
target_include_directories(test_rate_vector_r34_exact PRIVATE ../include)
target_compile_options(test_rate_vector_r34_exact PRIVATE -Wall -Wextra -Werror)
add_test(NAME test_rate_vector_r34_exact COMMAND test_rate_vector_r34_exact)

add_executable(test_rate_vector_r52_exact
    test_rate_vector_r52_exact.c
    board_host_stub.c
    ../src/crc32.c
    ../src/flash.c
    ../src/pattern_buffer.c
    ../src/pattern_validate.c
    ../src/psram.c
)
target_include_directories(test_rate_vector_r52_exact PRIVATE ../include)
target_compile_options(test_rate_vector_r52_exact PRIVATE -Wall -Wextra -Werror)
add_test(NAME test_rate_vector_r52_exact COMMAND test_rate_vector_r52_exact)

add_test(NAME test_rate_vector_streaming COMMAND test_rate_vector_streaming)
add_test(NAME test_rate_vector_streaming_fail_stop COMMAND test_rate_vector_streaming_fail_stop)

# Compile the same streaming implementation with the production/practical
# profile.  The test source contains compare-free, compare/sample, mixed-rate,
# cache-boundary, and repeat-shape cases; keeping this as a separate target
# prevents the historical diagnostic targets above from silently standing in
# for the default firmware contract.
add_executable(test_rate_vector_practical
    test_rate_vector_streaming.c
    board_host_stub.c
    ../src/crc32.c
    ../src/flash.c
    ../src/pattern_buffer.c
    ../src/pattern_validate.c
    ../src/psram.c
    ../src/compare.c
    ../src/samples.c
)
target_include_directories(test_rate_vector_practical PRIVATE ../include)
target_compile_options(test_rate_vector_practical PRIVATE -Wall -Wextra -Werror)
target_compile_definitions(test_rate_vector_practical PRIVATE
    STPG_STREAM_PRACTICAL_PROFILE=1
    STPG_STREAM_PARTIAL_REPEAT_FAST_PATH=1
    STPG_STREAM_HOT_TELEMETRY=1
    STPG_STREAM_COMPARE_FREE_BLOCK_FILL=1
    STPG_STREAM_PRODUCER_SRAM=1
    STPG_STREAM_REPEAT_BLOCK_FILL=1
    STPG_STREAM_REPEAT_BOUNDARY_FILL=1
    STPG_STREAM_REPEAT_BOUNDARY_CONTINUE=1
    STPG_STREAM_REPEAT_BOUNDARY_REARM=1
    STPG_STREAM_COMPARE_STREAMING=1
    STPG_STREAM_PSRAM_COMPARE_SAMPLE=1
    STPG_STREAM_FIFO_FILL_REQUESTS=1
    STPG_STREAM_PRECOMPUTED_UOP_CAPACITY=32
    STPG_STREAM_STATUS_LOGICAL_EVENTS=1
    STPG_STREAM_RATE_VECTOR_EVENT_LIMIT=4294967295
)
add_test(NAME test_rate_vector_practical COMMAND test_rate_vector_practical)

add_executable(test_rate_vector_practical_hot_off
    test_rate_vector_streaming.c
    board_host_stub.c
    ../src/crc32.c
    ../src/flash.c
    ../src/pattern_buffer.c
    ../src/pattern_validate.c
    ../src/psram.c
    ../src/compare.c
    ../src/samples.c
)
target_include_directories(test_rate_vector_practical_hot_off PRIVATE ../include)
target_compile_options(test_rate_vector_practical_hot_off PRIVATE -Wall -Wextra -Werror)
target_compile_definitions(test_rate_vector_practical_hot_off PRIVATE
    STPG_STREAM_PRACTICAL_PROFILE=1
    STPG_STREAM_PARTIAL_REPEAT_FAST_PATH=1
    STPG_STREAM_HOT_TELEMETRY=0
    STPG_STREAM_COMPARE_FREE_BLOCK_FILL=1
    STPG_STREAM_PRODUCER_SRAM=1
    STPG_STREAM_REPEAT_BLOCK_FILL=1
    STPG_STREAM_REPEAT_BOUNDARY_FILL=1
    STPG_STREAM_REPEAT_BOUNDARY_CONTINUE=1
    STPG_STREAM_REPEAT_BOUNDARY_REARM=1
    STPG_STREAM_COMPARE_STREAMING=1
    STPG_STREAM_PSRAM_COMPARE_SAMPLE=1
    STPG_STREAM_FIFO_FILL_REQUESTS=1
    STPG_STREAM_PRECOMPUTED_UOP_CAPACITY=32
    STPG_STREAM_STATUS_LOGICAL_EVENTS=1
    STPG_STREAM_RATE_VECTOR_EVENT_LIMIT=4294967295
)
add_test(NAME test_rate_vector_practical_hot_off COMMAND test_rate_vector_practical_hot_off)

add_executable(test_rate_vector_psram_capacity_streaming
    test_rate_vector_streaming.c
    board_host_stub.c
    ../src/crc32.c
    ../src/flash.c
    ../src/pattern_buffer.c
    ../src/pattern_validate.c
    ../src/psram.c
)
target_include_directories(test_rate_vector_psram_capacity_streaming PRIVATE ../include)
target_compile_options(test_rate_vector_psram_capacity_streaming PRIVATE -Wall -Wextra -Werror)
target_compile_definitions(test_rate_vector_psram_capacity_streaming PRIVATE
    STPG_STREAM_COMPARE_FREE_BLOCK_FILL=1
    STPG_STREAM_COMPARE_SAMPLE_REPAIR=1
    STPG_STREAM_COMPARE_MIXED_RATE_REPAIR=1
    STPG_STREAM_COMPARE_EVENT_SCHEDULE=1
    STPG_STREAM_COMPARE_EVENT_SCHEDULE_HANDOFF=1
    STPG_STREAM_COMPARE_EVENT_SCHEDULE_CHAIN_HANDOFF=1
    STPG_STREAM_STATUS_LOGICAL_EVENTS=1
    STPG_STREAM_PSRAM_CAPACITY_RATE_VECTOR=1
    STPG_STREAM_RATE_VECTOR_EVENT_LIMIT=4294967295
)
add_test(NAME test_rate_vector_psram_capacity_streaming COMMAND test_rate_vector_psram_capacity_streaming)

foreach(replay_target test_rate_vector_compare_replay test_rate_vector_compare_replay_direct)
    add_executable(${replay_target}
        test_rate_vector_compare_replay.c
        board_host_stub.c
        ../src/compare.c
        ../src/crc32.c
        ../src/flash.c
        ../src/pattern_buffer.c
        ../src/pattern_validate.c
        ../src/psram.c
        ../src/samples.c
    )
    target_include_directories(${replay_target} PRIVATE ../include)
    target_compile_options(${replay_target} PRIVATE -Wall -Wextra -Werror)
    target_compile_definitions(${replay_target} PRIVATE
        STPG_STREAM_PRACTICAL_PROFILE=1
        STPG_STREAM_PARTIAL_REPEAT_FAST_PATH=1
        STPG_STREAM_COMPARE_FREE_BLOCK_FILL=1
        STPG_STREAM_PRODUCER_SRAM=1
        STPG_STREAM_REPEAT_BLOCK_FILL=1
        STPG_STREAM_REPEAT_BOUNDARY_FILL=1
        STPG_STREAM_REPEAT_BOUNDARY_CONTINUE=1
        STPG_STREAM_REPEAT_BOUNDARY_REARM=1
        STPG_STREAM_COMPARE_STREAMING=1
        STPG_STREAM_PSRAM_COMPARE_SAMPLE=1
        STPG_STREAM_FIFO_FILL_REQUESTS=1
        STPG_STREAM_PRECOMPUTED_UOP_CAPACITY=32
        STPG_STREAM_STATUS_LOGICAL_EVENTS=1
        STPG_STREAM_RATE_VECTOR_EVENT_LIMIT=4294967295
    )
    if(replay_target STREQUAL "test_rate_vector_compare_replay_direct")
        target_compile_definitions(${replay_target} PRIVATE STPG_SAMPLE_PSRAM_SRAM_STAGING=0)
    endif()
    add_test(NAME ${replay_target} COMMAND ${replay_target})
endforeach()

add_executable(test_rate_vector_practical_loops
    test_rate_vector_loop.c
    board_host_stub.c
    ../src/crc32.c
    ../src/flash.c
    ../src/pattern_buffer.c
    ../src/pattern_validate.c
    ../src/psram.c
    ../src/compare.c
    ../src/samples.c
)
target_include_directories(test_rate_vector_practical_loops PRIVATE ../include)
target_compile_options(test_rate_vector_practical_loops PRIVATE -Wall -Wextra -Werror)
target_compile_definitions(test_rate_vector_practical_loops PRIVATE
    # The pinned F311 host fixtures encode a 150 MHz pattern clock.
    STPG_CLOCK_MODE=0
    STPG_STREAM_PRACTICAL_PROFILE=1
    STPG_STREAM_LOOP_BATCH=1
    STPG_STREAM_PARTIAL_REPEAT_FAST_PATH=1
    STPG_STREAM_HOT_TELEMETRY=0
    STPG_STREAM_COMPARE_FREE_BLOCK_FILL=1
    STPG_STREAM_PRODUCER_SRAM=1
    STPG_STREAM_REPEAT_BLOCK_FILL=1
    STPG_STREAM_REPEAT_BOUNDARY_FILL=1
    STPG_STREAM_REPEAT_BOUNDARY_CONTINUE=1
    STPG_STREAM_REPEAT_BOUNDARY_REARM=1
    STPG_STREAM_COMPARE_STREAMING=1
    STPG_STREAM_PSRAM_COMPARE_SAMPLE=1
    STPG_STREAM_FIFO_FILL_REQUESTS=1
    STPG_STREAM_PRECOMPUTED_UOP_CAPACITY=32
    STPG_STREAM_STATUS_LOGICAL_EVENTS=1
    STPG_STREAM_RATE_VECTOR_EVENT_LIMIT=4294967295
)
add_test(NAME test_rate_vector_practical_loops COMMAND test_rate_vector_practical_loops)

# Optional non-contact diagnostic utility; never needs external assets for ctest.
add_executable(audit_rate_vector_file
    audit_rate_vector_file.c board_host_stub.c ../src/compare.c ../src/crc32.c
    ../src/flash.c ../src/pattern_validate.c
    ../src/psram.c ../src/samples.c)
target_include_directories(audit_rate_vector_file PRIVATE ../include)
target_compile_options(audit_rate_vector_file PRIVATE -Wall -Wextra -Werror)
get_target_property(practical_emitter_definitions test_rate_vector_practical_loops COMPILE_DEFINITIONS)
list(FILTER practical_emitter_definitions EXCLUDE REGEX "^STPG_CLOCK_MODE=")
target_compile_definitions(audit_rate_vector_file PRIVATE ${practical_emitter_definitions}
    STPG_CLOCK_MODE=1 STPG_STREAM_VECTOR_CACHE_READ_RECORDS=256)

foreach(template_test IN ITEMS test_rate_template_fill audit_rate_vector_file_templates
        test_rate_vector_practical_templates test_rate_vector_template_loops test_rate_vector_template_failstop)
    if(template_test STREQUAL "test_rate_template_fill")
        set(template_source test_rate_template_fill.c)
        set(template_buffer_source "") # Includes pattern_buffer.c for private reference comparison.
    elseif(template_test STREQUAL "audit_rate_vector_file_templates")
        set(template_source audit_rate_vector_file.c)
        set(template_buffer_source "") # Audit inspects the real prepared producer route.
    elseif(template_test STREQUAL "test_rate_vector_practical_templates")
        set(template_source test_rate_vector_streaming.c)
        set(template_buffer_source ../src/pattern_buffer.c)
    else()
        set(template_source test_rate_vector_loop.c)
        set(template_buffer_source ../src/pattern_buffer.c)
    endif()
    add_executable(${template_test} ${template_source} ${template_buffer_source}
        board_host_stub.c ../src/compare.c ../src/crc32.c ../src/flash.c
        ../src/pattern_validate.c ../src/psram.c ../src/samples.c)
    target_include_directories(${template_test} PRIVATE ../include)
    target_compile_options(${template_test} PRIVATE -Wall -Wextra -Werror)
    target_compile_definitions(${template_test} PRIVATE STPG_STREAM_RATE_TEMPLATE_FILL=1)
    if(template_test STREQUAL "test_rate_template_fill" OR template_test MATCHES "^audit_")
        # Match the physical production read-cache size for A/B evidence and
        # mid-chunk prefetch failure tests, not the host header default 2048.
        target_compile_definitions(${template_test} PRIVATE STPG_STREAM_VECTOR_CACHE_READ_RECORDS=256)
    endif()
    if(template_test STREQUAL "test_rate_vector_template_failstop")
        target_compile_definitions(${template_test} PRIVATE STPG_STREAM_FAIL_STOP=1
            STPG_STREAM_LOOP_BATCH=1 STPG_STREAM_COMPARE_FREE_BLOCK_FILL=1
            STPG_STREAM_COMPARE_STREAMING=1 STPG_STREAM_FIFO_FILL_REQUESTS=1
            STPG_STREAM_RATE_VECTOR_EVENT_LIMIT=4294967295)
    else()
        target_compile_definitions(${template_test} PRIVATE ${practical_emitter_definitions})
        if(NOT template_test MATCHES "^audit_")
            target_compile_definitions(${template_test} PRIVATE STPG_CLOCK_MODE=0)
        endif()
    endif()
    if(NOT template_test MATCHES "^audit_")
        add_test(NAME ${template_test} COMMAND ${template_test})
    else()
        target_compile_definitions(${template_test} PRIVATE STPG_CLOCK_MODE=1)
    endif()
endforeach()

# Reduced output requires the full schedule pipeline, never PSRAM sample/replay.
# Test every individual missing prerequisite and preserve the complete diagnostic
# route. These tests inspect preparation, not an artificially forced filler.
foreach(route_profile IN ITEMS incomplete_repair incomplete_schedule incomplete_handoff
        incomplete_chain complete_diagnostic psram_complete practical_off practical_on)
    set(route_target test_stream_route_${route_profile})
    add_executable(${route_target} test_stream_runtime_route.c board_host_stub.c
        ../src/compare.c ../src/crc32.c ../src/flash.c ../src/pattern_validate.c
        ../src/psram.c ../src/samples.c)
    target_include_directories(${route_target} PRIVATE ../include)
    target_compile_options(${route_target} PRIVATE -Wall -Wextra -Werror)
    set(route_repair 1)
    set(route_schedule 1)
    set(route_handoff 1)
    set(route_chain 1)
    set(route_psram 0)
    set(route_templates 0)
    set(route_expected 0)
    if(route_profile STREQUAL "incomplete_repair")
        set(route_repair 0)
    elseif(route_profile STREQUAL "incomplete_schedule")
        set(route_schedule 0)
    elseif(route_profile STREQUAL "incomplete_handoff")
        set(route_handoff 0)
    elseif(route_profile STREQUAL "incomplete_chain")
        set(route_chain 0)
    elseif(route_profile STREQUAL "complete_diagnostic")
        set(route_expected 1)
    elseif(route_profile STREQUAL "psram_complete")
        set(route_psram 1)
    else()
        set(route_psram 1)
        set(route_repair 0)
        set(route_schedule 0)
        set(route_handoff 0)
        set(route_chain 0)
        if(route_profile STREQUAL "practical_on")
            set(route_templates 1)
        endif()
    endif()
    target_compile_definitions(${route_target} PRIVATE STPG_CLOCK_MODE=1
        STPG_STREAM_COMPARE_STREAMING=1 STPG_STREAM_COMPARE_FREE_BLOCK_FILL=1
        STPG_STREAM_PARTIAL_REPEAT_FAST_PATH=1 STPG_STREAM_LOOP_BATCH=1
        STPG_STREAM_RATE_VECTOR_EVENT_LIMIT=4294967295 STPG_STREAM_HOT_TELEMETRY=0
        STPG_STREAM_COMPARE_SAMPLE_REPAIR=${route_repair}
        STPG_STREAM_COMPARE_EVENT_SCHEDULE=${route_schedule}
        STPG_STREAM_COMPARE_EVENT_SCHEDULE_HANDOFF=${route_handoff}
        STPG_STREAM_COMPARE_EVENT_SCHEDULE_CHAIN_HANDOFF=${route_chain}
        STPG_STREAM_PSRAM_COMPARE_SAMPLE=${route_psram}
        STPG_STREAM_RATE_TEMPLATE_FILL=${route_templates} EXPECT_REDUCED=${route_expected})
    add_test(NAME ${route_target} COMMAND ${route_target})
endforeach()

add_executable(test_rate_vector_fail_stop_loops
    test_rate_vector_loop.c
    board_host_stub.c
    ../src/compare.c
    ../src/crc32.c
    ../src/flash.c
    ../src/pattern_buffer.c
    ../src/pattern_validate.c
    ../src/psram.c
    ../src/samples.c
)
target_include_directories(test_rate_vector_fail_stop_loops PRIVATE ../include)
target_compile_options(test_rate_vector_fail_stop_loops PRIVATE -Wall -Wextra -Werror -Wshadow)
target_compile_definitions(test_rate_vector_fail_stop_loops PRIVATE
    STPG_STREAM_FAIL_STOP=1
    STPG_STREAM_LOOP_BATCH=1
    STPG_STREAM_COMPARE_FREE_BLOCK_FILL=1
    STPG_STREAM_COMPARE_STREAMING=1
    STPG_STREAM_FIFO_FILL_REQUESTS=1
    STPG_STREAM_RATE_VECTOR_EVENT_LIMIT=4294967295
)
add_test(NAME test_rate_vector_fail_stop_loops COMMAND test_rate_vector_fail_stop_loops)

add_test(NAME test_rate_vector_loop COMMAND test_rate_vector_loop)
add_test(NAME test_flash COMMAND test_flash)
