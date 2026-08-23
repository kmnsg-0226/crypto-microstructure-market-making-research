#include "replay/event_replayer.hpp"

#include <algorithm>
#include <memory>
#include <stdexcept>
#include <string>

#include <simdjson.h>

#include "exchange/binance/message_parser.hpp"
#include "research/research_exporter.hpp"
#include "storage/raw_event_writer.hpp"

namespace crypto::replay {
namespace {

bool is_depth_stream(std::string_view stream) {
    return stream.find("depth") != std::string_view::npos;
}

struct ChainManifest {
    bool present{};
    bool continuation{};
    std::string previous_file;
};

ChainManifest read_manifest(const std::filesystem::path& raw_path) {
    auto manifest_path = raw_path;
    manifest_path += ".manifest.json";
    if (!std::filesystem::exists(manifest_path)) {
        return {};
    }
    simdjson::dom::parser parser;
    simdjson::dom::element root;
    if (const auto error = parser.load(manifest_path.string()).get(root); error) {
        throw std::runtime_error(
            "invalid raw manifest " + manifest_path.string() + ": " + simdjson::error_message(error));
    }
    ChainManifest manifest;
    manifest.present = true;
    bool continuation = false;
    if (!root["continuation"].get(continuation)) {
        manifest.continuation = continuation;
    }
    std::string_view previous;
    if (!root["previous_file"].get(previous)) {
        manifest.previous_file = std::string(previous);
    }
    return manifest;
}

}  // namespace

void validate_raw_chain(const std::vector<std::filesystem::path>& paths) {
    if (paths.empty()) {
        throw std::invalid_argument("no raw input files supplied");
    }
    for (const auto& path : paths) {
        if (!std::filesystem::exists(path)) {
            throw std::runtime_error("raw input does not exist: " + path.string());
        }
    }
    for (std::size_t i = 0; i < paths.size(); ++i) {
        const auto manifest = read_manifest(paths[i]);
        if (!manifest.present) {
            continue;
        }
        if (i == 0) {
            // A continuation file depends on book state built in an earlier file; replaying one
            // on its own would silently produce an empty or partial research export.
            if (manifest.continuation) {
                throw std::runtime_error(
                    "first raw input " + paths[i].filename().string() +
                    " is a continuation file; start the chain at its independent predecessor " +
                    (manifest.previous_file.empty() ? std::string("(unknown)") : manifest.previous_file));
            }
            continue;
        }
        const auto expected = paths[i - 1].filename().string();
        if (manifest.previous_file != expected) {
            throw std::runtime_error(
                "raw chain break: " + paths[i].filename().string() + " follows " +
                (manifest.previous_file.empty() ? std::string("null") : manifest.previous_file) +
                ", not " + expected);
        }
    }
}

EventReplayer::EventReplayer(binance::InstrumentInfo instrument)
    : instrument_(std::move(instrument)) {}

binance::InstrumentInfo EventReplayer::instrument_from_raw_file(
    const std::filesystem::path& path,
    std::string_view symbol) {
    std::optional<binance::InstrumentInfo> result;
    storage::RawEventReader reader(path);
    reader.for_each([&](const storage::RawRecord& record) {
        if (!result && record.type == storage::RawEventType::InstrumentInfo) {
            result = binance::InstrumentInfo::from_exchange_info_json(record.payload, symbol);
        }
    });
    if (!result) {
        throw std::runtime_error("raw file does not contain instrument metadata");
    }
    return *result;
}

binance::InstrumentInfo EventReplayer::instrument_from_raw_files(
    const std::vector<std::filesystem::path>& paths,
    std::string_view symbol) {
    for (const auto& path : paths) {
        try {
            return instrument_from_raw_file(path, symbol);
        } catch (const std::runtime_error&) {
            // Continuation files hold no instrument metadata; keep scanning the chain.
        }
    }
    throw std::runtime_error("raw chain does not contain instrument metadata");
}

ReplaySummary EventReplayer::replay(
    const std::filesystem::path& path,
    const std::optional<std::filesystem::path>& research_output,
    std::uint64_t sample_interval_ms,
    std::uint64_t research_start_ns,
    std::uint64_t research_end_ns,
    bool include_invalid_samples,
    ReplayObserver* observer) {
    return replay(std::vector<std::filesystem::path>{path}, research_output, sample_interval_ms,
                  research_start_ns, research_end_ns, include_invalid_samples, observer);
}

ReplaySummary EventReplayer::replay(
    const std::vector<std::filesystem::path>& paths,
    const std::optional<std::filesystem::path>& research_output,
    std::uint64_t sample_interval_ms,
    std::uint64_t research_start_ns,
    std::uint64_t research_end_ns,
    bool include_invalid_samples,
    ReplayObserver* observer) {
    validate_raw_chain(paths);
    book::OrderBook book;
    binance::DepthSynchronizer sync(book);
    binance::MessageParser parser(instrument_);
    std::unique_ptr<research::ResearchSnapshotExporter> exporter;
    if (research_output) {
        exporter = std::make_unique<research::ResearchSnapshotExporter>(
            *research_output, instrument_, sample_interval_ms, 1000,
            research_start_ns, research_end_ns, include_invalid_samples);
    }

    ReplaySummary summary;
    std::int64_t latest_exchange_time_ms = 0;
    bool depth_connected = false;
    const auto on_record = [&](const storage::RawRecord& record) {
        ++summary.raw_records;
        if (summary.first_local_receive_ns == 0) {
            summary.first_local_receive_ns = record.local_system_ns;
        }
        summary.last_local_receive_ns = record.local_system_ns;
        if (record.exchange_time_ms != 0) {
            if (summary.first_exchange_time_ms == 0) {
                summary.first_exchange_time_ms = record.exchange_time_ms;
            } else {
                summary.first_exchange_time_ms = std::min(
                    summary.first_exchange_time_ms, record.exchange_time_ms);
            }
            summary.last_exchange_time_ms = std::max(
                summary.last_exchange_time_ms, record.exchange_time_ms);
        }
        const auto live_before = depth_connected && sync.is_live();
        if (exporter) {
            exporter->advance_before_event(
                record.local_system_ns, latest_exchange_time_ms, book, live_before);
        }
        if (observer) {
            observer->before_record(
                record.local_system_ns, latest_exchange_time_ms, book, live_before);
        }
        if (record.exchange_time_ms != 0) {
            latest_exchange_time_ms = std::max(latest_exchange_time_ms, record.exchange_time_ms);
        }

        try {
            switch (record.type) {
                case storage::RawEventType::ConnectionOpen:
                    if (observer) {
                        observer->on_connection(record.local_system_ns, record.stream, true);
                    }
                    if (is_depth_stream(record.stream)) {
                        depth_connected = true;
                        sync.on_connected();
                    }
                    break;
                case storage::RawEventType::ConnectionClose:
                    // Preserve the last valid state for the terminal checksum, but gate all
                    // research output while disconnected. A later open clears the book and
                    // starts the same buffering/snapshot state transition used live.
                    if (is_depth_stream(record.stream)) {
                        depth_connected = false;
                    }
                    if (observer) {
                        observer->on_connection(record.local_system_ns, record.stream, false);
                    }
                    break;
                case storage::RawEventType::DepthSnapshot:
                    sync.on_snapshot(parser.parse_snapshot(record.payload));
                    ++summary.snapshots;
                    if (observer) {
                        observer->after_snapshot(
                            record.local_system_ns, book, depth_connected && sync.is_live());
                    }
                    break;
                case storage::RawEventType::DepthUpdate: {
                    ++summary.depth_events;
                    if (!depth_connected) {
                        break;
                    }
                    const auto event = parser.parse_depth_event(record.payload);
                    if (observer) {
                        observer->before_depth_event(event, book);
                    }
                    const auto result = sync.on_depth(event);
                    if (observer) {
                        observer->after_depth_event(
                            record.local_system_ns, record.exchange_time_ms, book,
                            result == binance::SyncResult::Applied,
                            depth_connected && sync.is_live());
                    }
                    if (result == binance::SyncResult::GapDetected ||
                        result == binance::SyncResult::NeedSnapshot) {
                        ++summary.sequence_gaps;
                    }
                    if (result == binance::SyncResult::InvalidBook) {
                        if (book.bids().empty()) {
                            ++summary.empty_bid_states;
                        }
                        if (book.asks().empty()) {
                            ++summary.empty_ask_states;
                        }
                        if (!book.bids().empty() && !book.asks().empty() &&
                            book.bids().begin()->first >= book.asks().begin()->first) {
                            ++summary.crossed_book_states;
                        }
                    }
                    break;
                }
                case storage::RawEventType::AggTrade: {
                    ++summary.trade_events;
                    const auto trade = parser.parse_agg_trade(record.payload);
                    if (exporter) {
                        exporter->on_trade(trade, record.local_system_ns);
                    }
                    if (observer) {
                        observer->on_trade(
                            record.local_system_ns, trade, book,
                            depth_connected && sync.is_live());
                    }
                    break;
                }
                case storage::RawEventType::InstrumentInfo:
                case storage::RawEventType::Status:
                    break;
            }
        } catch (const std::exception&) {
            ++summary.parse_failures;
            if (record.type == storage::RawEventType::DepthUpdate) {
                sync.begin_resync();
            }
        }

        const auto live_after = depth_connected && sync.is_live();
        if (exporter) {
            exporter->sample_exact_event_time(
                record.local_system_ns, latest_exchange_time_ms, book, live_after);
        }
        if (observer) {
            observer->after_record(
                record.local_system_ns, latest_exchange_time_ms, book, live_after);
        }
    };

    // Book, sync and exporter state deliberately survive file boundaries: a rotation is a storage
    // boundary only, so a chain replays exactly as the unrotated stream would.
    for (const auto& path : paths) {
        storage::RawEventReader reader(path);
        reader.for_each(on_record);
    }

    if (exporter) {
        if (research_end_ns != std::numeric_limits<std::uint64_t>::max()) {
            exporter->finish_at(
                research_end_ns, latest_exchange_time_ms, book,
                depth_connected && sync.is_live());
        }
        exporter->close();
        summary.research_rows = exporter->rows_written();
        summary.research_valid_rows = exporter->valid_rows();
        summary.research_invalid_rows = exporter->invalid_rows();
        summary.invalid_book_intervals = exporter->invalid_intervals();
    }
    if (observer) {
        observer->finish(book);
    }
    summary.applied_depth_events = sync.applied_event_count();
    summary.stale_depth_events = sync.stale_event_count();
    summary.sequence_gaps = sync.gap_count();
    summary.final_update_id = book.last_update_id();
    summary.best_bid_ticks = book.best_bid();
    summary.best_ask_ticks = book.best_ask();
    summary.top_bids = book.top_n_bids(10);
    summary.top_asks = book.top_n_asks(10);
    summary.final_checksum = book.checksum();
    return summary;
}

}  // namespace crypto::replay
