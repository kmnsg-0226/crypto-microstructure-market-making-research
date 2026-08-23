cmake_minimum_required(VERSION 3.20)
project(crypto_hft_l2 LANGUAGES CXX)

if(POLICY CMP0167)
    cmake_policy(SET CMP0167 NEW)
endif()

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(CURL REQUIRED)
find_package(Boost REQUIRED COMPONENTS system)
find_package(OpenSSL REQUIRED)
find_package(Threads REQUIRED)
find_package(ZLIB REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(SIMDJSON REQUIRED IMPORTED_TARGET simdjson)
pkg_check_modules(ZSTD REQUIRED IMPORTED_TARGET libzstd)

execute_process(
    COMMAND git rev-parse --short HEAD
    WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
    OUTPUT_VARIABLE CRYPTO_L2_GIT_COMMIT
    OUTPUT_STRIP_TRAILING_WHITESPACE
    ERROR_QUIET
)
if(NOT CRYPTO_L2_GIT_COMMIT)
    set(CRYPTO_L2_GIT_COMMIT "unknown")
endif()

add_library(crypto_l2
    exchange/binance/instrument_info.cpp
    exchange/binance/message_parser.cpp
    exchange/binance/rest_client.cpp
    exchange/binance/websocket_client.cpp
    exchange/binance/depth_sync.cpp
    book/order_book.cpp
    storage/raw_event_writer.cpp
    storage/raw_file_manifest.cpp
    replay/event_replayer.cpp
    research/compressed_text_writer.cpp
    research/microstructure_features.cpp
    research/cross_stream_timing.cpp
    research/native_dataset_exporter.cpp
    research/level_lifecycle.cpp
    research/queue_sensitivity.cpp
    research/passive_queue.cpp
    research/research_exporter.cpp
    research/tardis_daily_exporter.cpp
    historical/gzip_line_reader.cpp
    historical/tardis_l2_replayer.cpp
    historical/tardis_trade_reader.cpp
)

target_include_directories(crypto_l2 PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
# Resolved at configure time; re-run cmake to refresh the commit stamped into manifests.
target_compile_definitions(crypto_l2 PRIVATE CRYPTO_L2_BUILD_VERSION="${CRYPTO_L2_GIT_COMMIT}")
target_link_libraries(crypto_l2
    PUBLIC
        CURL::libcurl
        Boost::system
        OpenSSL::SSL
        OpenSSL::Crypto
        Threads::Threads
        ZLIB::ZLIB
        PkgConfig::SIMDJSON
        PkgConfig::ZSTD
)

if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
    target_compile_options(crypto_l2 PRIVATE -Wall -Wextra -Wpedantic -Wconversion -Wshadow)
endif()

add_executable(crypto_capture app/capture_main.cpp)
target_link_libraries(crypto_capture PRIVATE crypto_l2)

add_executable(crypto_replay app/replay_main.cpp)
target_link_libraries(crypto_replay PRIVATE crypto_l2)

add_executable(native_dataset_export app/native_dataset_main.cpp)
target_link_libraries(native_dataset_export PRIVATE crypto_l2)

add_executable(native_level_lifecycle app/native_level_main.cpp)
target_link_libraries(native_level_lifecycle PRIVATE crypto_l2)

add_executable(native_queue_sensitivity app/native_queue_main.cpp)
target_link_libraries(native_queue_sensitivity PRIVATE crypto_l2)

add_executable(native_timing_audit app/native_timing_main.cpp)
target_link_libraries(native_timing_audit PRIVATE crypto_l2)

add_executable(tardis_l2_validate app/tardis_validate_main.cpp)
target_link_libraries(tardis_l2_validate PRIVATE crypto_l2)

add_executable(tardis_research_export app/tardis_research_main.cpp)
target_link_libraries(tardis_research_export PRIVATE crypto_l2)

add_executable(tardis_passive_probe app/tardis_passive_main.cpp)
target_link_libraries(tardis_passive_probe PRIVATE crypto_l2)

add_library(crypto_event_l2
    historical/tardis_event_replayer.cpp
    research/event_dataset_exporter.cpp
)
target_include_directories(crypto_event_l2 PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(crypto_event_l2 PUBLIC crypto_l2)
if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
    target_compile_options(crypto_event_l2 PRIVATE -Wall -Wextra -Wpedantic -Wconversion -Wshadow)
endif()

add_executable(tardis_event_dataset app/tardis_event_dataset_main.cpp)
target_link_libraries(tardis_event_dataset PRIVATE crypto_event_l2)

enable_testing()
add_executable(crypto_l2_tests tests/test_main.cpp)
target_compile_definitions(crypto_l2_tests PRIVATE CRYPTO_L2_FIXTURE_DIR="${CMAKE_CURRENT_SOURCE_DIR}/tests/fixtures")
target_link_libraries(crypto_l2_tests PRIVATE crypto_l2)
add_test(NAME crypto_l2_tests COMMAND crypto_l2_tests)

# Argument validation runs before any network access, so these exit non-zero immediately.
set(invalid_capture_args_0 --symbol BTCUSDT)
set(invalid_capture_args_1 --output out.chft.zst --output-dir dir --rotate-utc-daily)
set(invalid_capture_args_2 --output out.chft.zst --rotate-utc-daily)
set(invalid_capture_args_3 --output-dir dir --rotate-utc-daily --rotate-every-sec 2)
set(invalid_capture_args_4 --output-dir dir)
foreach(case_index RANGE 0 4)
    add_test(NAME capture_invalid_cli_${case_index}
             COMMAND crypto_capture ${invalid_capture_args_${case_index}})
    set_tests_properties(capture_invalid_cli_${case_index} PROPERTIES WILL_FAIL TRUE)
endforeach()
