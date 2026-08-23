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

// Observable lifecycle of a best-price level.
//
// A level episode begins when a price becomes the best price on its side and ends when it stops
// being best, when the synchronized segment ends, or when the book resynchronizes. The same
// numerical price appearing again later is a new episode.
//
// Nothing here claims queue position. Binance publishes aggregated L2: there are no order ids,
// no FIFO rank, and no way to separate a cancellation from an execution the trade feed did not
// line up with. Every quantity below is a property of the displayed level, not of any order in
// it, and a reduction that the aggressive-trade stream cannot account for is called unexplained,
// never cancelled.
struct LevelLifecycleConfig {
    static constexpr std::size_t kHorizons = 5;
    static constexpr std::array<std::uint64_t, kHorizons> kHorizonMs{100, 250, 500, 1000, 5000};

    std::uint64_t grid_ms{100};
    std::uint32_t file_index{};
};

struct LevelLifecycleSummary {
    std::uint64_t segments{};
    std::uint64_t episodes{};
    std::uint64_t grid_rows{};
    std::uint64_t depth_events{};
    std::uint64_t trade_events{};
    std::uint64_t prints_at_quote{};
    std::uint64_t prints_through{};
    std::uint64_t prints_outside{};
    std::uint64_t explained_removal_lots{};
    std::uint64_t unexplained_removal_lots{};
    std::uint64_t replenished_lots{};
};

class LevelLifecycleObserver final : public replay::ReplayObserver {
public:
    LevelLifecycleObserver(
        const std::filesystem::path& episodes_path,
        const std::filesystem::path& grid_path,
        LevelLifecycleConfig config);
    ~LevelLifecycleObserver() override;

    LevelLifecycleObserver(const LevelLifecycleObserver&) = delete;
    LevelLifecycleObserver& operator=(const LevelLifecycleObserver&) = delete;

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

    [[nodiscard]] const LevelLifecycleSummary& summary() const noexcept { return summary_; }
    [[nodiscard]] static std::string episodes_header();
    [[nodiscard]] static std::string grid_header();

private:
    struct Episode {
        bool active{};
        std::uint64_t id{};
        std::uint64_t segment_id{};
        std::uint8_t side{};
        std::int64_t price_ticks{};
        std::uint64_t start_ns{};
        std::int64_t initial_qty{};
        std::int64_t current_qty{};
        std::int64_t max_qty{};
        std::int64_t min_qty{};
        std::int64_t cum_add{};
        std::int64_t cum_remove{};
        std::int64_t cum_explained_remove{};
        std::int64_t cum_unexplained_remove{};
        std::int64_t cum_trade_at_quote{};
        std::int64_t cum_trade_through{};
        std::int64_t cum_replenish{};
        // Aggressive volume printed at the level and not yet used to account for a displayed
        // reduction. Frozen reconciliation rule, shared with the phase 3 queue replay.
        std::int64_t trade_pool{};
        std::uint64_t add_events{};
        std::uint64_t remove_events{};
        std::uint64_t replenish_events{};
        std::uint64_t prints_at_quote{};
        std::uint64_t prints_through{};
        std::uint64_t last_qty_change_ns{};
        std::uint64_t last_add_ns{};
        std::uint64_t last_remove_ns{};
        std::uint64_t last_print_ns{};
        std::uint64_t last_replenish_ns{};
        std::int64_t pre_event_qty{-1};
    };

    struct Pending {
        std::uint64_t ns{};
        std::uint8_t side{};
        std::int64_t price_ticks{};
        std::uint64_t segment_id{};
        std::array<std::int64_t, LevelLifecycleConfig::kHorizons> at_quote_volume{};
        std::array<std::int64_t, LevelLifecycleConfig::kHorizons> through_volume{};
        std::array<std::uint8_t, LevelLifecycleConfig::kHorizons> through_flag{};
        std::string prefix;
    };

    void open_segment(std::uint64_t local_ns, const book::OrderBook& book);
    void close_segment(std::uint64_t local_ns, const char* reason);
    void start_episode(
        std::uint8_t side, std::int64_t price_ticks, std::int64_t qty, std::uint64_t local_ns);
    void end_episode(std::uint8_t side, std::uint64_t local_ns, const char* reason);
    void apply_level_delta(Episode& episode, std::int64_t delta, std::uint64_t local_ns);
    void advance_grid(std::uint64_t local_ns, bool inclusive, const book::OrderBook& book);
    void emit_grid_row(std::uint64_t sample_ns, const book::OrderBook& book);
    void retire(std::uint64_t local_ns, bool force);
    void emit_pending(const Pending& pending, std::uint64_t segment_end_ns);
    [[nodiscard]] bool usable() const noexcept { return book_live_ && trade_stream_up_; }

    LevelLifecycleConfig config_;
    std::unique_ptr<CompressedTextWriter> episodes_;
    std::unique_ptr<CompressedTextWriter> grid_;
    std::uint64_t grid_ns_{};
    std::array<std::uint64_t, LevelLifecycleConfig::kHorizons> horizon_ns_{};
    std::uint64_t widest_ns_{};

    std::array<Episode, 2> levels_{};
    std::deque<Pending> pending_;
    std::uint64_t next_episode_id_{};
    std::uint64_t next_sample_ns_{};

    bool book_live_{};
    bool trade_stream_up_{};
    bool in_segment_{};
    std::uint64_t segment_id_{};
    std::uint64_t segment_start_ns_{};
    std::uint64_t last_record_ns_{};
    bool closed_{};
    std::string row_;
    LevelLifecycleSummary summary_{};
};

}  // namespace crypto::research
