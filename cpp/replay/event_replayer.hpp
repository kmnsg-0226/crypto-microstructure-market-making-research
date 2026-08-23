#pragma once

#include <cstdint>
#include <filesystem>
#include <limits>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "book/order_book.hpp"
#include "exchange/binance/depth_sync.hpp"
#include "exchange/binance/instrument_info.hpp"
#include "exchange/binance/message_types.hpp"

namespace crypto::replay {

struct ReplaySummary {
    std::uint64_t raw_records{};
    std::uint64_t first_local_receive_ns{};
    std::uint64_t last_local_receive_ns{};
    std::int64_t first_exchange_time_ms{};
    std::int64_t last_exchange_time_ms{};
    std::uint64_t depth_events{};
    std::uint64_t applied_depth_events{};
    std::uint64_t stale_depth_events{};
    std::uint64_t trade_events{};
    std::uint64_t snapshots{};
    std::uint64_t sequence_gaps{};
    std::uint64_t parse_failures{};
    std::uint64_t final_update_id{};
    std::optional<std::int64_t> best_bid_ticks;
    std::optional<std::int64_t> best_ask_ticks;
    std::vector<binance::PriceLevel> top_bids;
    std::vector<binance::PriceLevel> top_asks;
    std::uint64_t final_checksum{};
    std::uint64_t research_rows{};
    std::uint64_t research_valid_rows{};
    std::uint64_t research_invalid_rows{};
    std::uint64_t invalid_book_intervals{};
    std::uint64_t crossed_book_states{};
    std::uint64_t empty_bid_states{};
    std::uint64_t empty_ask_states{};

    bool operator==(const ReplaySummary&) const = default;
};

// Read-only view of replay progress, used by native research exporters so that the raw
// interpretation (connection state, depth synchronization, book application order) lives in
// exactly one place. An observer must never mutate the book or the synchronizer; replay
// summaries are therefore identical with and without one attached.
class ReplayObserver {
public:
    virtual ~ReplayObserver() = default;

    // The book reflects every record strictly before local_ns.
    virtual void before_record(
        std::uint64_t local_ns,
        std::int64_t latest_exchange_ms,
        const book::OrderBook& book,
        bool live) = 0;
    // Every ConnectionOpen / ConnectionClose record, for any stream.
    virtual void on_connection(std::uint64_t local_ns, std::string_view stream, bool open) = 0;
    // Called with the pre-image of the book, before the event levels are applied.
    virtual void before_depth_event(
        const binance::DepthEvent& event,
        const book::OrderBook& book) = 0;
    virtual void after_depth_event(
        std::uint64_t local_ns,
        std::int64_t exchange_ms,
        const book::OrderBook& book,
        bool applied,
        bool live) = 0;
    virtual void after_snapshot(
        std::uint64_t local_ns,
        const book::OrderBook& book,
        bool live) = 0;
    virtual void on_trade(
        std::uint64_t local_ns,
        const binance::AggTradeEvent& trade,
        const book::OrderBook& book,
        bool live) = 0;
    // The book reflects the record at local_ns and everything before it.
    virtual void after_record(
        std::uint64_t local_ns,
        std::int64_t latest_exchange_ms,
        const book::OrderBook& book,
        bool live) = 0;
    virtual void finish(const book::OrderBook& book) = 0;
};

// Validates that a chronological chain of rotated raw files can be replayed as one stream.
// Sidecar manifests, when present, must agree on the previous_file linkage and the first file
// must not be a continuation of something outside the chain. Throws on an invalid chain.
void validate_raw_chain(const std::vector<std::filesystem::path>& paths);

class EventReplayer {
public:
    explicit EventReplayer(binance::InstrumentInfo instrument);

    [[nodiscard]] static binance::InstrumentInfo instrument_from_raw_file(
        const std::filesystem::path& path,
        std::string_view symbol);

    // Continuation files carry no instrument metadata; the chain is scanned until it is found.
    [[nodiscard]] static binance::InstrumentInfo instrument_from_raw_files(
        const std::vector<std::filesystem::path>& paths,
        std::string_view symbol);

    // Replays a chronological chain as a single stream: book, depth sync and exporter state are
    // carried across file boundaries, exactly as if the files had never been rotated.
    [[nodiscard]] ReplaySummary replay(
        const std::vector<std::filesystem::path>& paths,
        const std::optional<std::filesystem::path>& research_output = std::nullopt,
        std::uint64_t sample_interval_ms = 100,
        std::uint64_t research_start_ns = 0,
        std::uint64_t research_end_ns = std::numeric_limits<std::uint64_t>::max(),
        bool include_invalid_samples = false,
        ReplayObserver* observer = nullptr);

    [[nodiscard]] ReplaySummary replay(
        const std::filesystem::path& path,
        const std::optional<std::filesystem::path>& research_output = std::nullopt,
        std::uint64_t sample_interval_ms = 100,
        std::uint64_t research_start_ns = 0,
        std::uint64_t research_end_ns = std::numeric_limits<std::uint64_t>::max(),
        bool include_invalid_samples = false,
        ReplayObserver* observer = nullptr);

private:
    binance::InstrumentInfo instrument_;
};

}  // namespace crypto::replay
