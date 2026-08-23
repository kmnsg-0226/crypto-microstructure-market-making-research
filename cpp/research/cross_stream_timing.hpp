#pragma once

#include <cstdint>
#include <deque>
#include <filesystem>
#include <memory>
#include <string>

#include "book/order_book.hpp"
#include "replay/event_replayer.hpp"

namespace crypto::research {

class CompressedTextWriter;

struct CrossStreamTimingSummary {
    std::uint64_t trades_seen{};
    std::uint64_t trades_recorded{};
    std::uint64_t at_quote{};
    std::uint64_t through_quote{};
    std::uint64_t outside_quote{};
    std::uint64_t discarded_on_desync{};
    std::uint64_t unresolved_qty_reaction{};
    std::uint64_t unresolved_quote_removal{};
    std::uint64_t unresolved_best_change{};
    std::uint64_t unresolved_mid_adverse{};
};

// Event study of the lag between an aggressive print and the depth stream's reaction to it.
//
// Binance publishes aggressive trades and order-book diffs on two independent sockets, and the
// diff stream is batched every 100 ms. Every "was the order filled before the book moved" style
// statistic is therefore a race between two feeds, not necessarily a statement about economic
// sequencing. This observer measures that lag directly, on local receive time, without
// adjusting or reconciling any timestamp.
class CrossStreamTimingObserver final : public replay::ReplayObserver {
public:
    CrossStreamTimingObserver(
        const std::filesystem::path& output_path,
        std::uint32_t file_index,
        std::uint64_t observation_window_ms = 5000);
    ~CrossStreamTimingObserver() override;

    CrossStreamTimingObserver(const CrossStreamTimingObserver&) = delete;
    CrossStreamTimingObserver& operator=(const CrossStreamTimingObserver&) = delete;

    void before_record(
        std::uint64_t local_ns,
        std::int64_t latest_exchange_ms,
        const book::OrderBook& book,
        bool live) override;
    void on_connection(std::uint64_t local_ns, std::string_view stream, bool open) override;
    void before_depth_event(
        const binance::DepthEvent& event,
        const book::OrderBook& book) override;
    void after_depth_event(
        std::uint64_t local_ns,
        std::int64_t exchange_ms,
        const book::OrderBook& book,
        bool applied,
        bool live) override;
    void after_snapshot(
        std::uint64_t local_ns,
        const book::OrderBook& book,
        bool live) override;
    void on_trade(
        std::uint64_t local_ns,
        const binance::AggTradeEvent& trade,
        const book::OrderBook& book,
        bool live) override;
    void after_record(
        std::uint64_t local_ns,
        std::int64_t latest_exchange_ms,
        const book::OrderBook& book,
        bool live) override;
    void finish(const book::OrderBook& book) override;

    [[nodiscard]] const CrossStreamTimingSummary& summary() const noexcept { return summary_; }
    [[nodiscard]] static std::string csv_header();

private:
    struct Pending {
        std::uint64_t receive_ns{};
        std::int64_t trade_exchange_ms{};
        bool aggressive_buy{};
        std::int64_t trade_price_ticks{};
        std::int64_t trade_qty_lots{};
        std::int64_t quote_px_ticks{};
        std::int64_t quote_qty_lots{};
        std::int64_t mid_x2{};
        std::int64_t spread_ticks{};
        const char* category{};
        std::uint64_t qty_reaction_ns{};
        std::int64_t qty_reaction_exchange_ms{};
        std::int64_t qty_after_reaction_lots{-1};
        std::uint64_t quote_removal_ns{};
        std::uint64_t best_change_ns{};
        std::uint64_t mid_adverse_ns{};
        std::uint64_t first_mid_move_ns{};
        std::int8_t first_mid_move_dir{};
        std::uint64_t depth_events_seen{};
    };

    void observe(std::uint64_t local_ns, std::int64_t exchange_ms, const book::OrderBook& book);
    void flush(std::uint64_t local_ns, bool force);
    void emit(const Pending& pending);
    void drop_pending();

    std::unique_ptr<CompressedTextWriter> writer_;
    std::uint32_t file_index_{};
    std::uint64_t window_ns_{};
    std::deque<Pending> pending_;
    CrossStreamTimingSummary summary_{};
    std::string row_;
    bool closed_{};
};

}  // namespace crypto::research
