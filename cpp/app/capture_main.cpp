#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <filesystem>
#include <iostream>
#include <mutex>
#include <stdexcept>
#include <memory>
#include <string>
#include <string_view>
#include <thread>
#include <utility>

#include "book/order_book.hpp"
#include "exchange/binance/depth_sync.hpp"
#include "exchange/binance/instrument_info.hpp"
#include "exchange/binance/message_parser.hpp"
#include "exchange/binance/rest_client.hpp"
#include "exchange/binance/websocket_client.hpp"
#include "storage/raw_event_writer.hpp"
#include "storage/raw_file_manifest.hpp"

namespace {

struct CaptureConfig {
    std::string symbol{"BTCUSDT"};
    std::filesystem::path output;
    std::filesystem::path output_dir;
    std::string output_prefix;
    bool rotate_utc_daily{};
    std::uint64_t rotate_every_sec{};
    std::uint64_t duration_sec{};
    std::string rest_base{"https://fapi.binance.com"};
    std::string ws_base{"wss://fstream.binance.com"};
};

// Capture-side counters that the writer cannot derive from the record stream itself.
struct CounterSnapshot {
    std::uint64_t reconnects{};
    std::uint64_t sequence_gaps{};
    std::uint64_t parse_failures{};
    std::uint64_t invalid_records{};
    std::uint64_t dropped_records{};
    std::uint64_t snapshot_failures{};
    std::uint64_t fatal_errors{};
};

struct CaptureStats {
    std::atomic_uint64_t depth_events{};
    std::atomic_uint64_t trade_events{};
    std::atomic_uint64_t reconnects{};
    std::atomic_uint64_t sequence_gaps{};
    std::atomic_uint64_t parse_failures{};
    std::atomic_uint64_t invalid_records{};
    std::atomic_uint64_t dropped_records{};
    std::atomic_uint64_t snapshots{};
    std::atomic_uint64_t snapshot_failures{};
    std::atomic_uint64_t depth_connections{};
    std::atomic_uint64_t trade_connections{};
    std::atomic_uint64_t fatal_errors{};
};

std::atomic_bool* global_stop = nullptr;

void handle_signal(int) {
    if (global_stop != nullptr) {
        global_stop->store(true, std::memory_order_relaxed);
    }
}

std::string lowercase(std::string value) {
    for (char& c : value) {
        if (c >= 'A' && c <= 'Z') {
            c = static_cast<char>(c - 'A' + 'a');
        }
    }
    return value;
}

CaptureConfig parse_args(int argc, char** argv) {
    CaptureConfig config;
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        auto require_value = [&](const char* name) -> std::string {
            if (++i >= argc) {
                throw std::invalid_argument(std::string("missing value for ") + name);
            }
            return argv[i];
        };
        if (arg == "--symbol") {
            config.symbol = require_value("--symbol");
        } else if (arg == "--output") {
            config.output = require_value("--output");
        } else if (arg == "--output-dir") {
            config.output_dir = require_value("--output-dir");
        } else if (arg == "--output-prefix") {
            config.output_prefix = require_value("--output-prefix");
        } else if (arg == "--rotate-utc-daily") {
            config.rotate_utc_daily = true;
        } else if (arg == "--rotate-every-sec") {
            config.rotate_every_sec = std::stoull(require_value("--rotate-every-sec"));
        } else if (arg == "--duration-sec") {
            config.duration_sec = std::stoull(require_value("--duration-sec"));
        } else if (arg == "--rest-base") {
            config.rest_base = require_value("--rest-base");
        } else if (arg == "--ws-base") {
            config.ws_base = require_value("--ws-base");
        } else if (arg == "--help") {
            std::cout
                << "Usage: crypto_capture --output FILE [--symbol BTCUSDT] [--duration-sec N]\n"
                << "       [--rest-base URL] [--ws-base wss://fstream.binance.com]\n"
                << "   or: crypto_capture --output-dir DIR --rotate-utc-daily\n"
                << "       [--output-prefix BTCUSDT-LONDON] [...]\n"
                << "\n"
                << "  --rotate-every-sec N  test-only rotation interval; not for production use\n"
                << "                        and mutually exclusive with --rotate-utc-daily\n";
            std::exit(0);
        } else {
            throw std::invalid_argument("unknown argument: " + arg);
        }
    }
    const bool rotating = config.rotate_utc_daily || config.rotate_every_sec != 0;
    if (config.output.empty() == config.output_dir.empty()) {
        throw std::invalid_argument("exactly one of --output or --output-dir is required");
    }
    if (!config.output.empty()) {
        if (rotating) {
            throw std::invalid_argument("rotation requires --output-dir, not --output");
        }
        if (!config.output_prefix.empty()) {
            throw std::invalid_argument("--output-prefix requires --output-dir");
        }
    } else {
        if (config.rotate_utc_daily && config.rotate_every_sec != 0) {
            throw std::invalid_argument(
                "--rotate-utc-daily and --rotate-every-sec are mutually exclusive");
        }
        if (!rotating) {
            throw std::invalid_argument(
                "--output-dir requires --rotate-utc-daily or --rotate-every-sec");
        }
        if (config.output_prefix.empty()) {
            config.output_prefix = config.symbol;
        }
    }
    return config;
}

std::unique_ptr<crypto::storage::RawEventWriter> make_writer(const CaptureConfig& config) {
    if (config.output_dir.empty()) {
        return std::make_unique<crypto::storage::RawEventWriter>(config.output);
    }
    crypto::storage::RotationConfig rotation;
    rotation.directory = config.output_dir;
    rotation.prefix = config.output_prefix;
    rotation.interval_ns = config.rotate_utc_daily
        ? crypto::storage::kUtcDayNs
        : config.rotate_every_sec * 1'000'000'000ULL;
    return std::make_unique<crypto::storage::RawEventWriter>(std::move(rotation));
}

crypto::storage::RawRecord make_record(
    crypto::storage::RawEventType type,
    std::string session,
    std::string stream,
    std::string payload,
    std::int64_t exchange_time_ms = 0,
    std::uint64_t system_ns = 0,
    std::uint64_t steady_ns = 0) {
    return {
        system_ns == 0 ? crypto::storage::current_system_ns() : system_ns,
        steady_ns == 0 ? crypto::storage::current_steady_ns() : steady_ns,
        exchange_time_ms,
        type,
        std::move(session),
        std::move(stream),
        std::move(payload),
    };
}

void print_stats(
    const CaptureStats& stats,
    const crypto::storage::RawEventWriter& writer,
    const crypto::binance::DepthSynchronizer& sync,
    std::uint64_t elapsed_sec,
    const std::string& current_output,
    bool final) {
    std::cerr
        << (final ? "final" : "stats")
        << " elapsed_sec=" << elapsed_sec
        << " depth_events=" << stats.depth_events.load()
        << " trade_events=" << stats.trade_events.load()
        << " reconnects=" << stats.reconnects.load()
        << " sequence_gaps=" << stats.sequence_gaps.load()
        << " parse_failures=" << stats.parse_failures.load()
        << " invalid_records=" << stats.invalid_records.load()
        << " dropped_records=" << stats.dropped_records.load()
        << " snapshots=" << stats.snapshots.load()
        << " snapshot_failures=" << stats.snapshot_failures.load()
        << " fatal_errors=" << stats.fatal_errors.load()
        << " raw_records=" << writer.records_written()
        << " rotations=" << writer.rotations()
        << " current_output=" << current_output
        << " sync_state=" << crypto::binance::to_string(sync.state())
        << " buffered_depth=" << sync.buffered_events()
        << '\n';
}

}  // namespace

int main(int argc, char** argv) {
    try {
        const auto config = parse_args(argc, argv);
        std::atomic_bool stop{false};
        global_stop = &stop;
        std::signal(SIGINT, handle_signal);
        std::signal(SIGTERM, handle_signal);

        crypto::binance::RestClient rest(config.rest_base);
        const auto exchange_info_json = rest.exchange_info();
        const auto instrument = crypto::binance::InstrumentInfo::from_exchange_info_json(
            exchange_info_json, config.symbol);
        if (instrument.contract_type() != "PERPETUAL" || instrument.status() != "TRADING") {
            throw std::runtime_error("requested instrument is not a trading perpetual contract");
        }

        // Declared before the writer so outstanding finalization outlives writer destruction.
        crypto::storage::ManifestFinalizer finalizer;
        auto writer_owner = make_writer(config);
        auto& writer = *writer_owner;

        // Only touched from the writer's file-closed hook, which runs under the writer mutex.
        CounterSnapshot previous_counters;
        std::atomic_uint64_t last_update_id{};
        std::atomic_uint64_t segment_start_update_id{};
        CaptureStats stats;
        writer.set_on_file_closed([&](crypto::storage::ClosedRawFile& file) {
            file.symbol = config.symbol;
            const CounterSnapshot now{
                stats.reconnects.load(),
                stats.sequence_gaps.load(),
                stats.parse_failures.load(),
                stats.invalid_records.load(),
                stats.dropped_records.load(),
                stats.snapshot_failures.load(),
                stats.fatal_errors.load(),
            };
            file.counters.emplace_back("reconnects", now.reconnects - previous_counters.reconnects);
            file.counters.emplace_back("sequence_gaps", now.sequence_gaps - previous_counters.sequence_gaps);
            file.counters.emplace_back("parse_failures", now.parse_failures - previous_counters.parse_failures);
            file.counters.emplace_back("invalid_records", now.invalid_records - previous_counters.invalid_records);
            file.counters.emplace_back("dropped_records", now.dropped_records - previous_counters.dropped_records);
            file.counters.emplace_back("snapshot_failures", now.snapshot_failures - previous_counters.snapshot_failures);
            file.counters.emplace_back("fatal_errors", now.fatal_errors - previous_counters.fatal_errors);
            previous_counters = now;
            const auto end_update_id = last_update_id.load();
            file.start_update_id = segment_start_update_id.exchange(end_update_id);
            file.end_update_id = end_update_id;
            // Hashing a multi-gigabyte file must never run on the ingest path.
            finalizer.submit(file);
        });

        writer.write(make_record(
            crypto::storage::RawEventType::InstrumentInfo,
            "rest-metadata",
            "exchangeInfo",
            exchange_info_json));

        crypto::book::OrderBook book;
        crypto::binance::DepthSynchronizer sync(book);
        crypto::binance::MessageParser parser(instrument);
        std::mutex sync_mutex;
        std::atomic_bool snapshot_requested{false};
        std::string current_depth_session;

        const auto lower_symbol = lowercase(config.symbol);
        const std::string depth_stream = lower_symbol + "@depth@100ms";
        const std::string trade_stream = lower_symbol + "@aggTrade";
        const std::string depth_url = config.ws_base + "/public/ws/" + depth_stream;
        const std::string trade_url = config.ws_base + "/market/ws/" + trade_stream;

        crypto::binance::WebSocketClient depth_client(depth_url, "depth");
        crypto::binance::WebSocketClient trade_client(trade_url, "aggtrade");

        auto depth_thread = std::thread([&] {
            try {
                depth_client.run(stop, {
                    .on_open = [&](std::string_view session) {
                        const auto connections = ++stats.depth_connections;
                        if (connections > 1) {
                            ++stats.reconnects;
                        }
                        writer.write(make_record(
                            crypto::storage::RawEventType::ConnectionOpen,
                            std::string(session), depth_stream, "{}"));
                        {
                            std::lock_guard lock(sync_mutex);
                            current_depth_session = session;
                            sync.on_connected();
                        }
                        snapshot_requested.store(true, std::memory_order_release);
                    },
                    .on_message = [&](std::string_view session, std::string_view payload) {
                        const auto system_ns = crypto::storage::current_system_ns();
                        const auto steady_ns = crypto::storage::current_steady_ns();
                        crypto::binance::DepthEvent event;
                        try {
                            event = parser.parse_depth_event(payload);
                        } catch (const std::exception&) {
                            writer.write(make_record(
                                crypto::storage::RawEventType::DepthUpdate,
                                std::string(session), depth_stream, std::string(payload),
                                0, system_ns, steady_ns));
                            ++stats.parse_failures;
                            ++stats.invalid_records;
                            std::lock_guard lock(sync_mutex);
                            sync.begin_resync();
                            snapshot_requested.store(true, std::memory_order_release);
                            return;
                        }
                        writer.write(make_record(
                            crypto::storage::RawEventType::DepthUpdate,
                            std::string(session), depth_stream, std::string(payload),
                            event.transaction_time_ms, system_ns, steady_ns));
                        ++stats.depth_events;
                        std::lock_guard lock(sync_mutex);
                        const auto result = sync.on_depth(event);
                        last_update_id.store(book.last_update_id(), std::memory_order_relaxed);
                        if (result == crypto::binance::SyncResult::GapDetected ||
                            result == crypto::binance::SyncResult::NeedSnapshot) {
                            ++stats.sequence_gaps;
                            sync.begin_resync();
                            snapshot_requested.store(true, std::memory_order_release);
                        } else if (result == crypto::binance::SyncResult::InvalidBook) {
                            ++stats.invalid_records;
                            sync.begin_resync();
                            snapshot_requested.store(true, std::memory_order_release);
                        }
                    },
                    .on_disconnect = [&](std::string_view session, std::string_view reason) {
                        if (!stop.load(std::memory_order_relaxed)) {
                            std::cerr << "depth disconnected session=" << session
                                      << " reason=" << reason << '\n';
                        }
                        writer.write(make_record(
                            crypto::storage::RawEventType::ConnectionClose,
                            std::string(session), depth_stream,
                            std::string("{\"reason\":\"") + std::string(reason) + "\"}"));
                        std::lock_guard lock(sync_mutex);
                        if (current_depth_session == session) {
                            current_depth_session.clear();
                            sync.on_disconnected();
                        }
                    },
                });
            } catch (const std::exception& error) {
                ++stats.fatal_errors;
                std::cerr << "depth transport fatal error: " << error.what() << '\n';
                stop.store(true, std::memory_order_relaxed);
            }
        });

        auto trade_thread = std::thread([&] {
            try {
                trade_client.run(stop, {
                    .on_open = [&](std::string_view session) {
                        const auto connections = ++stats.trade_connections;
                        if (connections > 1) {
                            ++stats.reconnects;
                        }
                        writer.write(make_record(
                            crypto::storage::RawEventType::ConnectionOpen,
                            std::string(session), trade_stream, "{}"));
                    },
                    .on_message = [&](std::string_view session, std::string_view payload) {
                        const auto system_ns = crypto::storage::current_system_ns();
                        const auto steady_ns = crypto::storage::current_steady_ns();
                        crypto::binance::AggTradeEvent event;
                        try {
                            event = parser.parse_agg_trade(payload);
                        } catch (const std::exception&) {
                            writer.write(make_record(
                                crypto::storage::RawEventType::AggTrade,
                                std::string(session), trade_stream, std::string(payload),
                                0, system_ns, steady_ns));
                            ++stats.parse_failures;
                            return;
                        }
                        writer.write(make_record(
                            crypto::storage::RawEventType::AggTrade,
                            std::string(session), trade_stream, std::string(payload),
                            event.transaction_time_ms, system_ns, steady_ns));
                        ++stats.trade_events;
                    },
                    .on_disconnect = [&](std::string_view session, std::string_view reason) {
                        if (!stop.load(std::memory_order_relaxed)) {
                            std::cerr << "aggTrade disconnected session=" << session
                                      << " reason=" << reason << '\n';
                        }
                        writer.write(make_record(
                            crypto::storage::RawEventType::ConnectionClose,
                            std::string(session), trade_stream,
                            std::string("{\"reason\":\"") + std::string(reason) + "\"}"));
                    },
                });
            } catch (const std::exception& error) {
                ++stats.fatal_errors;
                std::cerr << "aggTrade transport fatal error: " << error.what() << '\n';
                stop.store(true, std::memory_order_relaxed);
            }
        });

        const auto start = std::chrono::steady_clock::now();
        auto next_log = start + std::chrono::seconds(10);
        while (!stop.load(std::memory_order_relaxed)) {
            const auto now = std::chrono::steady_clock::now();
            const auto elapsed = static_cast<std::uint64_t>(
                std::chrono::duration_cast<std::chrono::seconds>(now - start).count());
            if (config.duration_sec > 0 && elapsed >= config.duration_sec) {
                stop.store(true, std::memory_order_relaxed);
                break;
            }

            if (snapshot_requested.exchange(false, std::memory_order_acq_rel)) {
                std::string session_at_request;
                {
                    std::lock_guard lock(sync_mutex);
                    if (!current_depth_session.empty()) {
                        session_at_request = current_depth_session;
                        sync.request_snapshot();
                    }
                }
                if (!session_at_request.empty()) {
                    try {
                        const auto snapshot_json = rest.depth_snapshot(config.symbol, 1000);
                        const auto snapshot = parser.parse_snapshot(snapshot_json);
                        writer.write(make_record(
                            crypto::storage::RawEventType::DepthSnapshot,
                            session_at_request,
                            "fapi/v1/depth",
                            snapshot_json,
                            snapshot.transaction_time_ms != 0
                                ? snapshot.transaction_time_ms
                                : snapshot.event_time_ms));
                        ++stats.snapshots;
                        std::lock_guard lock(sync_mutex);
                        if (current_depth_session != session_at_request) {
                            snapshot_requested.store(true, std::memory_order_release);
                        } else {
                            const auto result = sync.on_snapshot(snapshot);
                            if (result == crypto::binance::SyncResult::NeedSnapshot ||
                                result == crypto::binance::SyncResult::InvalidBook) {
                                ++stats.snapshot_failures;
                                sync.begin_resync();
                                snapshot_requested.store(true, std::memory_order_release);
                            }
                        }
                    } catch (const std::exception& error) {
                        ++stats.snapshot_failures;
                        std::cerr << "snapshot error: " << error.what() << '\n';
                        snapshot_requested.store(true, std::memory_order_release);
                        std::this_thread::sleep_for(std::chrono::seconds(1));
                    }
                }
            }

            if (now >= next_log) {
                try {
                    writer.flush();
                } catch (const std::exception& error) {
                    ++stats.fatal_errors;
                    std::cerr << "raw storage fatal error: " << error.what() << '\n';
                    stop.store(true, std::memory_order_relaxed);
                    break;
                }
                const auto current_output = writer.current_path().filename().string();
                std::lock_guard lock(sync_mutex);
                print_stats(stats, writer, sync, elapsed, current_output, false);
                next_log = now + std::chrono::seconds(10);
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
        }

        if (depth_thread.joinable()) {
            depth_thread.join();
        }
        if (trade_thread.joinable()) {
            trade_thread.join();
        }
        writer.flush();
        const auto elapsed = static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::seconds>(
            std::chrono::steady_clock::now() - start).count());
        const auto final_path = writer.current_path();
        {
            const auto current_output = final_path.filename().string();
            std::lock_guard lock(sync_mutex);
            print_stats(stats, writer, sync, elapsed, current_output, true);
        }
        // Finish the current file and its manifest before exiting.
        writer.close();
        finalizer.wait_all();
        std::error_code size_error;
        std::cerr << "output=" << final_path << " bytes="
                  << std::filesystem::file_size(final_path, size_error)
                  << " rotations=" << writer.rotations() << '\n';
        if (finalizer.failures() != 0) {
            std::cerr << "rotation_fail manifests=" << finalizer.failures() << '\n';
            return 2;
        }
        return stats.fatal_errors.load() == 0 ? 0 : 2;
    } catch (const std::exception& error) {
        std::cerr << "crypto_capture: " << error.what() << '\n';
        return 1;
    }
}
