#pragma once

#include <array>
#include <cstdint>
#include <deque>
#include <filesystem>
#include <memory>
#include <string>

#include "book/order_book.hpp"
#include "replay/event_replayer.hpp"

namespace crypto::research {

class CompressedTextWriter;

// Queue-model sensitivity replay.
//
// Aggregated Binance L2 shows displayed quantity at a price, never queue position and never the
// identity of a removal. Two assumptions therefore have to be made and cannot be estimated:
//
//   alpha  the fraction of the displayed quantity at the quote assumed to sit ahead of the
//          hypothetical order at placement. alpha = 1 is the conservative back-of-queue rule
//          used in phases 1 and 2; alpha = 0 is an extreme front-of-queue bound.
//   beta   the fraction of an unexplained displayed-quantity removal ahead of the order that is
//          credited as queue advancement. beta = 0 is the conservative rule; beta = 1 is an
//          extreme bound in which every unexplained removal is treated as if it had left the
//          queue in front of us.
//
// Neither is an estimate of true queue position. The grid exists to bound the economics.
// Where the hypothetical order is placed. Grid is the phase 3 population: one placement per
// interval at whatever the touch happens to be. LevelBirth places one order immediately after
// the depth event that makes a price the best on its side, so the whole level lifecycle after
// placement is observed. LevelBirth is not "front of queue": everything displayed at that
// instant was resting there first and is treated as ahead.
enum class PlacementMode { Grid, LevelBirth };

struct QueueSensitivityConfig {
    static constexpr std::size_t kSteps = 5;
    static constexpr std::array<std::int64_t, kSteps> kPercents{0, 25, 50, 75, 100};
    static constexpr std::size_t kCells = kSteps * kSteps;

    // One placement per second, a deterministic decimation of the 100 ms decision grid used in
    // phases 1 and 2, so every placement instant is also a phase 2 row and the out-of-fold
    // predictions join exactly.
    PlacementMode mode{PlacementMode::Grid};
    std::uint64_t placement_interval_ms{1000};
    std::uint64_t observation_window_ms{30000};
    std::int64_t order_lots{5};
    std::uint32_t file_index{};
};

struct QueueSensitivitySummary {
    std::uint64_t segments{};
    std::uint64_t placements{};
    std::uint64_t emitted_rows{};
    std::uint64_t mid_path_points{};
    std::uint64_t depth_events{};
    std::uint64_t trade_events{};
    std::uint64_t unexplained_removal_lots{};
    std::uint64_t trade_explained_removal_lots{};
    std::uint64_t behind_attributed_removal_lots{};
};

// Emits two compact artifacts instead of one wide dataset:
//   * one row per (placement, side, alpha, beta) carrying the fill time and mechanism,
//   * the mid path, one point per observed mid change.
// Markouts are then computed downstream by looking the fill time up in the mid path, which keeps
// every horizon and every statistic re-derivable without another replay.
class QueueSensitivityObserver final : public replay::ReplayObserver {
public:
    QueueSensitivityObserver(
        const std::filesystem::path& fills_path,
        const std::filesystem::path& mid_path,
        QueueSensitivityConfig config);
    ~QueueSensitivityObserver() override;

    QueueSensitivityObserver(const QueueSensitivityObserver&) = delete;
    QueueSensitivityObserver& operator=(const QueueSensitivityObserver&) = delete;

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

    [[nodiscard]] const QueueSensitivitySummary& summary() const noexcept { return summary_; }
    [[nodiscard]] static std::string fills_header();
    [[nodiscard]] static std::string mid_header();

private:
    enum class Mechanism : std::uint8_t { None = 0, AtQuote = 1, TradeThrough = 2 };

    struct Order {
        std::uint64_t placement_ns{};
        std::uint64_t segment_id{};
        std::uint8_t side{};  // 0 bid, 1 ask
        std::int64_t quote_px_ticks{};
        std::int64_t displayed_qty_lots{};
        std::int64_t mid_x2_at_placement{};
        std::int64_t spread_ticks{};
        // Quantity added at the quote price after placement. Later additions queue behind the
        // hypothetical order, so a subsequent removal is charged against them first and only the
        // excess can ever be credited as queue advancement.
        std::int64_t added_behind{};
        // Aggressive volume at the quote price not yet used to explain a displayed reduction.
        std::int64_t trade_pool{};
        std::int64_t pre_event_qty{-1};
        std::array<std::int64_t, QueueSensitivityConfig::kCells> remaining_ahead{};
        std::array<std::int64_t, QueueSensitivityConfig::kCells> filled{};
        std::array<std::uint64_t, QueueSensitivityConfig::kCells> fill_ns{};
        std::array<Mechanism, QueueSensitivityConfig::kCells> mechanism{};
    };

    void open_segment(std::uint64_t local_ns);
    void close_segment(std::uint64_t local_ns);
    void place_due(std::uint64_t local_ns, bool inclusive, const book::OrderBook& book);
    void place(std::uint64_t local_ns, const book::OrderBook& book);
    void place_side(
        std::uint8_t side,
        std::int64_t price_ticks,
        std::int64_t displayed_qty,
        std::int64_t mid_x2,
        std::int64_t spread_ticks,
        std::uint64_t local_ns);
    void place_on_level_birth(std::uint64_t local_ns, const book::OrderBook& book);
    void retire(std::uint64_t local_ns, bool force);
    void emit(const Order& order, std::uint64_t observed_end_ns);
    void record_mid(std::uint64_t local_ns, std::int64_t mid_x2);
    [[nodiscard]] bool usable() const noexcept { return book_live_ && trade_stream_up_; }

    QueueSensitivityConfig config_;
    std::unique_ptr<CompressedTextWriter> fills_;
    std::unique_ptr<CompressedTextWriter> mid_;
    std::uint64_t placement_ns_{};
    std::uint64_t window_ns_{};
    std::uint64_t next_placement_ns_{};
    std::deque<Order> pending_;

    bool book_live_{};
    bool trade_stream_up_{};
    bool in_segment_{};
    std::uint64_t segment_id_{};
    std::uint64_t segment_start_ns_{};
    std::uint64_t last_record_ns_{};
    std::int64_t last_mid_x2_{};
    bool have_last_mid_{};
    std::array<std::int64_t, 2> last_best_{};
    bool have_last_best_{};
    bool closed_{};
    std::string row_;
    QueueSensitivitySummary summary_{};
};

}  // namespace crypto::research
