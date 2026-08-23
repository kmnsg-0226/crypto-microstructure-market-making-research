#pragma once

#include <array>
#include <cstdint>
#include <deque>
#include <filesystem>
#include <memory>
#include <string>
#include <vector>

#include "exchange/binance/instrument_info.hpp"
#include "historical/tardis_event_replayer.hpp"
#include "historical/tardis_trade_reader.hpp"
#include "research/passive_queue.hpp"

namespace crypto::research {

struct EventDatasetSummary {
    std::uint64_t book_events{};
    std::uint64_t trade_events{};
    std::uint64_t bbo_changes{};
    std::uint64_t decision_opportunities{};
    std::uint64_t candidate_quotes{};
    std::uint64_t full_fills{};
    std::uint64_t partial_fills{};
    std::uint64_t unfilled{};
    std::uint64_t snapshot_invalidations{};
    std::uint64_t invalid_book_events{};
    std::uint64_t cooldown_suppressed{};
    std::uint64_t rows{};

    bool operator==(const EventDatasetSummary&) const = default;
};

class EventDatasetExporter final : public historical::TardisEventObserver {
public:
    EventDatasetExporter(
        std::filesystem::path output_path,
        std::string date,
        historical::TardisEventTradeFile trades,
        std::uint64_t cooldown_us,
        std::uint64_t quote_lifetime_us,
        std::int64_t quote_qty_lots,
        std::string tick_size,
        std::string step_size,
        double weighted_obi_lambda);
    ~EventDatasetExporter() override;

    void before_depth_event(
        std::uint64_t exchange_timestamp_us,
        std::uint64_t local_timestamp_us,
        const binance::DepthEvent& event,
        const book::OrderBook& book) override;
    void after_depth_event(
        std::uint64_t exchange_timestamp_us,
        std::uint64_t local_timestamp_us,
        const binance::DepthEvent& event,
        const book::OrderBook& book) override;
    void before_snapshot(
        std::uint64_t exchange_timestamp_us,
        std::uint64_t local_timestamp_us,
        const book::OrderBook& book) override;
    void after_snapshot(
        std::uint64_t exchange_timestamp_us,
        std::uint64_t local_timestamp_us,
        const book::OrderBook& book) override;
    void finish(const book::OrderBook& book) override;

    [[nodiscard]] const EventDatasetSummary& summary() const noexcept { return summary_; }

public:
    static constexpr std::array<std::uint64_t, 5> windows_us_ = {
        10'000, 50'000, 100'000, 500'000, 1'000'000};

    struct Contribution {
        std::uint64_t local_time_us{};
        double delta_obi{};
        std::int64_t ofi_lots{};
        std::int64_t add_imbalance_lots{};
        std::int64_t cancel_imbalance_lots{};
        std::int64_t depletion_imbalance_lots{};
        std::int64_t replenishment_imbalance_lots{};
        std::uint64_t bbo_changes{};
        std::uint64_t depth_changes{};
        std::int64_t trade_qty_imbalance_lots{};
        std::int64_t trade_count_imbalance{};
        std::int64_t total_trade_qty_lots{};
        std::uint64_t trade_count{};
        std::uint64_t event_count{};
        double mid_abs_change_ticks{};
    };

    struct WindowState {
        std::deque<Contribution> events;
        Contribution sum;
    };

    struct PendingQuote {
        std::string prefix;
        PassiveQuoteSide side{PassiveQuoteSide::Bid};
        std::int64_t quote_price_ticks{};
        std::int64_t quote_qty_lots{};
        std::int64_t queue_ahead_lots{};
        std::uint64_t placement_local_time_us{};
        std::uint64_t expiry_local_time_us{};
    };

private:

    void add_contribution(const Contribution& contribution);
    void prune_windows(std::uint64_t local_timestamp_us);
    bool process_trades_before(std::uint64_t local_timestamp_us, const book::OrderBook& book);
    bool process_trades_equal(std::uint64_t local_timestamp_us, const book::OrderBook& book);
    void process_trade_group(const book::OrderBook& book);
    void maybe_decide(
        const book::OrderBook& book,
        std::uint64_t exchange_timestamp_us,
        std::uint64_t local_timestamp_us,
        const char* event_type);
    void enqueue_side(
        const book::OrderBook& book,
        const char* event_type,
        std::uint64_t exchange_timestamp_us,
        std::uint64_t local_timestamp_us,
        PassiveQuoteSide side);
    void resolve_until(std::uint64_t local_timestamp_us);
    void resolve_all(std::uint64_t cutoff_local_timestamp_us, bool snapshot_invalidated);
    void emit(PendingQuote&& pending, std::uint64_t end_local_timestamp_us, bool snapshot_invalidated);
    void reset_segment(std::uint64_t local_timestamp_us);

    std::filesystem::path output_path_;
    std::string date_;
    historical::TardisEventTradeFile trades_;
    PassiveTradeIndex trade_index_;
    std::unique_ptr<class CompressedTextWriter> writer_;
    binance::DecimalIncrement price_increment_;
    binance::DecimalIncrement qty_increment_;
    std::uint64_t cooldown_us_{};
    std::uint64_t quote_lifetime_us_{};
    std::int64_t quote_qty_lots_{};
    double weighted_obi_lambda_{};
    std::size_t next_trade_{};
    std::array<WindowState, 5> windows_;
    std::deque<PendingQuote> pending_;
    std::uint64_t segment_id_{};
    std::uint64_t opportunity_id_{};
    std::uint64_t last_decision_local_time_us_{};
    std::uint64_t last_trade_local_time_us_{};
    std::uint64_t last_book_local_time_us_{};
    std::uint64_t last_bbo_change_local_time_us_{};
    std::uint64_t last_mid_change_local_time_us_{};
    std::int64_t last_trade_streak_{};
    double previous_obi_l10_{};
    bool have_previous_obi_{};
    std::int64_t before_best_bid_{};
    std::int64_t before_best_ask_{};
    std::int64_t before_mid_x2_{};
    Contribution pending_book_contribution_;
    bool closed_{};
    EventDatasetSummary summary_;
};

}  // namespace crypto::research
