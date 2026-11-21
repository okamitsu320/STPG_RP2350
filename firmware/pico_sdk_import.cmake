# This project-level shim runs before project(). Prefer the CMake variable
# supplied by build_firmware.sh, then the environment variable, then the
# repository-local SDK. Once located, the SDK helper consumes its own fetch
# settings. Never form an include path from an empty environment variable
# (which would resolve to /external/pico_sdk_import.cmake).
if(DEFINED PICO_SDK_PATH AND NOT "${PICO_SDK_PATH}" STREQUAL "")
    set(STPG_PICO_SDK_IMPORT "${PICO_SDK_PATH}/external/pico_sdk_import.cmake")
elseif(DEFINED ENV{PICO_SDK_PATH} AND NOT "$ENV{PICO_SDK_PATH}" STREQUAL "")
    set(STPG_PICO_SDK_IMPORT "$ENV{PICO_SDK_PATH}/external/pico_sdk_import.cmake")
else()
    set(STPG_PICO_SDK_IMPORT "${CMAKE_CURRENT_LIST_DIR}/../pico-sdk/external/pico_sdk_import.cmake")
endif()

if(NOT EXISTS "${STPG_PICO_SDK_IMPORT}")
    message(FATAL_ERROR
        "Pico SDK import helper not found: ${STPG_PICO_SDK_IMPORT}. "
        "Set PICO_SDK_PATH to a Pico SDK checkout before configuring.")
endif()

include("${STPG_PICO_SDK_IMPORT}")
unset(STPG_PICO_SDK_IMPORT)
