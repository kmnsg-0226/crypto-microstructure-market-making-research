#pragma once

#include <cstdint>
#include <deque>
#include <filesystem>
#include <memory>
#include <optional>
#include <limits>

#include "book/order_book.hpp"
#include "exchange/binance/instrument_info.hpp"
#include "exchange/binance/message_types.hpp"

namespace crypto::research {

class CompressedTextWriter;

class ResearchSnapshotExporter {
public:
    ResearchSnapshotExporter(
        const std::filesystem::path& path,
        const binance::InstrumentInfo& instrument,
        std::uint64_t sample_interval_ms = 100,
        std::uint64_t recent_trade_window_ms = 1000,
        std::uint64_t start_ns = 0,
        std::uint64_t end_ns = std::numeric_limits<std::uint64_t>::max(),
        bool include_invalid = false);
    ~ResearchSnapshotExporter();

    ResearchSnapshotExporter(const ResearchSnapshotExporter&) = delete;
    ResearchSnapshotExporter& operator=(const ResearchSnapshotExporter&) = delete;

    void advance_before_event(
        std::uint64_t event_local_system_ns,
        std::int64_t latest_exchange_time_ms,
        const book::OrderBook& book,
        bool synchronized);
    void sample_exact_event_time(
        std::uint64_t event_local_system_ns,
        std::int64_t latest_exchange_time_ms,
        const book::OrderBook& book,
        bool synchronized);
    void on_trade(const binance::AggTradeEvent& event, std::uint64_t local_system_ns);
    void finish_at(
        std::uint64_t end_ns,
        std::int64_t latest_exchange_time_ms,
        const book::OrderBook& book,
        bool synchronized);
    void close();

    [[nodiscard]] std::uint64_t rows_written() const noexcept { return rows_written_; }
    [[nodiscard]] std::uint64_t valid_rows() const noexcept { return valid_rows_; }
    [[nodiscard]] std::uint64_t invalid_rows() const noexcept { return invalid_rows_; }
    [[nodiscard]] std::uint64_t invalid_intervals() const noexcept { return invalid_intervals_; }

private:
    struct SignedVolume {
        std::uint64_t local_system_ns{};
        std::int64_t buy_lots{};
        std::int64_t sell_lots{};
    };

    void initialize_grid(std::uint64_t local_system_ns);
    void prune_trades(std::uint64_t sample_ns);
    void write_sample(
        std::uint64_t sample_ns,
        std::int64_t exchange_time_ms,
        const book::OrderBook& book,
        bool synchronized);

    const binance::InstrumentInfo& instrument_;
    std::unique_ptr<CompressedTextWriter> writer_;
    std::uint64_t interval_ns_;
    std::uint64_t trade_window_ns_;
    std::uint64_t start_ns_;
    std::uint64_t end_ns_;
    bool include_invalid_{};
    std::optional<std::uint64_t> next_sample_ns_;
    std::optional<std::int64_t> last_trade_price_ticks_;
    std::deque<SignedVolume> volumes_;
    std::int64_t recent_buy_lots_{};
    std::int64_t recent_sell_lots_{};
    std::uint64_t rows_written_{};
    std::uint64_t valid_rows_{};
    std::uint64_t invalid_rows_{};
    std::uint64_t invalid_intervals_{};
    bool previous_sample_invalid_{};
};

}  // namespace crypto::research
