#pragma once

#include <array>
#include <cstdint>
#include <deque>
#include <filesystem>
#include <memory>
#include <string>
#include <vector>

#include "book/order_book.hpp"
#include "replay/event_replayer.hpp"

namespace crypto::research {

class CompressedTextWriter;

// Trailing feature windows, forward target horizons and the passive-quote assumptions of the
// native Binance USD-M decision dataset. Every horizon is expressed in milliseconds.
struct NativeDatasetConfig {
    static constexpr std::size_t kWindows = 5;
    static constexpr std::size_t kMarkouts = 5;
    static constexpr std::size_t kFillHorizons = 3;
    static constexpr std::size_t kPostFillHorizons = 4;
    static constexpr std::size_t kLevels = 10;

    std::uint64_t grid_ms{100};
    std::array<std::uint64_t, kWindows> window_ms{100, 250, 500, 1000, 5000};
    std::array<std::uint64_t, kMarkouts> markout_ms{100, 250, 500, 1000, 5000};
    std::array<std::uint64_t, kFillHorizons> fill_ms{500, 1000, 5000};
    std::array<std::uint64_t, kPostFillHorizons> postfill_ms{100, 500, 1000, 5000};
    // Hypothetical maker order size, in quantity steps (0.005 BTC at a 0.001 step size), matching
    // the size used by every earlier maker study in this repository.
    std::int64_t order_lots{5};
    // Mid moves in half ticks; an adverse move of one full tick is two half ticks.
    std::int64_t adverse_threshold_half_ticks{2};
    // Observation window for the fill / adverse race and for the next mid move. Outcomes still
    // undecided at the end of it are censored, never recorded as a zero. Native BTCUSDT holds a
    // one-tick spread with a near-static best price for seconds at a time, so a five-second
    // window would censor the great majority of these outcomes.
    std::uint64_t fill_horizon_ms{30000};
    double weight_lambda{0.5};
    std::uint32_t file_index{};
};

// One maximal interval during which the depth book was synchronized and the aggressive-trade
// stream was connected. Features, trailing windows and targets never cross one of these.
struct NativeSegmentQc {
    std::uint64_t segment_id{};
    std::uint64_t start_ns{};
    std::uint64_t end_ns{};
    std::int64_t first_exchange_time_ms{};
    std::int64_t last_exchange_time_ms{};
    std::uint64_t first_update_id{};
    std::uint64_t final_update_id{};
    std::uint64_t depth_events{};
    std::uint64_t trade_events{};
    std::uint64_t rows{};
    std::uint64_t bid_fills{};
    std::uint64_t ask_fills{};
    std::string close_reason;
};

struct NativeDatasetSummary {
    std::uint64_t rows{};
    std::uint64_t segments{};
    std::uint64_t snapshots{};
    std::uint64_t depth_stream_opens{};
    std::uint64_t trade_stream_opens{};
    std::uint64_t depth_events_in_segment{};
    std::uint64_t trade_events_in_segment{};
    std::uint64_t skipped_depth_events{};
    std::uint64_t crossed_book_after_sync{};
    std::uint64_t unrecoverable_gaps{};
    std::uint64_t missing_initial_sync{};
    std::uint64_t valid_research_ns{};
    std::vector<NativeSegmentQc> segment_qc;
};

// Builds the causal 100 ms decision dataset from native Binance raw capture. Attached to the
// native replay as a read-only observer, so the raw interpretation is shared with plain QC
// replay and the two can never disagree.
class NativeDatasetExporter final : public replay::ReplayObserver {
public:
    // Prices and quantities are emitted in exchange ticks and steps, so the instrument
    // definition is only needed by the caller writing the QC header.
    NativeDatasetExporter(
        const std::filesystem::path& output_path,
        NativeDatasetConfig config);
    ~NativeDatasetExporter() override;

    NativeDatasetExporter(const NativeDatasetExporter&) = delete;
    NativeDatasetExporter& operator=(const NativeDatasetExporter&) = delete;

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

    [[nodiscard]] const NativeDatasetSummary& summary() const noexcept { return summary_; }
    [[nodiscard]] static std::string csv_header(const NativeDatasetConfig& config);

private:
    // Flow counters carried by one raw event. Index layout, all in quantity steps except the
    // trailing counts: [0..9] bid depth added by pre-event rank, [10..19] bid depth removed,
    // [20..29] ask depth added, [30..39] ask depth removed, 40 buy qty, 41 sell qty,
    // 42 trade count, 43 buy trade count, 44 sell trade count, 45 applied depth events,
    // 46 absolute mid change in half ticks, 47 best-price changes.
    static constexpr std::size_t kCounters = 48;
    using Counters = std::array<std::int64_t, kCounters>;

    struct Contribution {
        std::uint64_t ns{};
        Counters values{};
    };

    struct PendingSide {
        std::int64_t quote_px_ticks{};
        std::int64_t queue_ahead_lots{};
        std::int64_t consumed_lots{};
        std::int64_t filled_lots_at_horizon{-1};
        bool full_filled{};
        bool filled_by_trade_through{};
        std::uint64_t full_fill_ns{};
        std::int64_t fill_mid_x2{};
        bool adverse{};
        std::array<std::int8_t, NativeDatasetConfig::kFillHorizons> fill_flag{-1, -1, -1};
        std::array<double, NativeDatasetConfig::kPostFillHorizons> postfill{};
        std::array<bool, NativeDatasetConfig::kPostFillHorizons> postfill_done{};
        std::int8_t fill_before_observed_mid_adverse{-1};
        // Latency from placement to the first observation of each competing event, so every
        // race variant, including lag-handicapped ones, can be rebuilt downstream without
        // re-running the replay. Zero means not observed inside the window.
        std::uint64_t mid_adverse_ns{};
        std::uint64_t quote_gone_ns{};
        std::uint64_t best_adverse_ns{};
    };

    struct PendingRow {
        std::uint64_t ns{};
        std::string prefix;
        std::int64_t mid_x2{};
        std::array<double, NativeDatasetConfig::kMarkouts> markout{};
        std::array<bool, NativeDatasetConfig::kMarkouts> markout_done{};
        std::int8_t next_move_dir{};
        double next_move_ms{-1.0};
        std::array<PendingSide, 2> side;
        std::uint64_t next_deadline_ns{};
        // Cleared once every book-state race on both sides has been observed or censored, so
        // settled rows cost one predictable branch per depth event instead of six lookups.
        bool watching{true};
    };

    void push_contribution(const Contribution& contribution);
    void prune_windows(std::uint64_t sample_ns);
    void open_segment(std::uint64_t local_ns, const book::OrderBook& book);
    void close_segment(std::uint64_t local_ns, std::string reason);
    void advance_grid(std::uint64_t local_ns, bool inclusive, const book::OrderBook& book);
    void emit_sample(std::uint64_t sample_ns, const book::OrderBook& book);
    // due_ns bounds which horizons may resolve: the caller passes now_ns once the
    // record at now_ns has been applied, and now_ns - 1 before it has.
    void resolve_pending(std::uint64_t now_ns, std::uint64_t due_ns,
                         const book::OrderBook& book);
    static void apply_mid_move(
        PendingRow& row,
        std::int64_t mid_x2,
        std::uint64_t now_ns,
        std::int64_t adverse_threshold_half_ticks,
        std::uint64_t move_cap_ns);
    void watch_book_races(std::uint64_t local_ns, const book::OrderBook& book);
    void refresh_next_deadline(PendingRow& row) const;
    void flush_front_resolved();
    void write_row(const PendingRow& row);
    [[nodiscard]] bool usable() const noexcept { return book_live_ && trade_stream_up_; }

    NativeDatasetConfig config_;
    std::unique_ptr<CompressedTextWriter> writer_;
    std::array<std::uint64_t, NativeDatasetConfig::kWindows> window_ns_{};
    std::array<std::uint64_t, NativeDatasetConfig::kMarkouts> markout_ns_{};
    std::array<std::uint64_t, NativeDatasetConfig::kFillHorizons> fill_ns_{};
    std::array<std::uint64_t, NativeDatasetConfig::kPostFillHorizons> postfill_ns_{};
    std::uint64_t grid_ns_{};
    std::uint64_t fill_horizon_ns_{};

    std::deque<Contribution> flow_;
    std::array<Counters, NativeDatasetConfig::kWindows> window_sums_{};
    std::array<std::size_t, NativeDatasetConfig::kWindows> window_head_{};

    std::deque<PendingRow> pending_;
    std::uint64_t min_deadline_ns_{};
    std::int64_t last_resolved_mid_x2_{};
    bool have_last_resolved_mid_{};

    Contribution pending_depth_flow_;
    bool pending_depth_flow_valid_{};
    std::int64_t pre_event_mid_x2_{};
    std::int64_t pre_event_best_bid_{};
    std::int64_t pre_event_best_ask_{};
    std::size_t watching_rows_{};

    // Latest in-segment occurrence of each event class; zero until first seen in the segment.
    std::uint64_t last_trade_ns_{};
    std::uint64_t last_depth_ns_{};
    std::uint64_t last_bbo_change_ns_{};
    std::uint64_t last_mid_change_ns_{};

    bool book_live_{};
    bool trade_stream_up_{};
    bool in_segment_{};
    std::uint64_t segment_start_ns_{};
    std::uint64_t next_sample_ns_{};
    std::int64_t latest_exchange_ms_{};
    std::uint64_t last_record_ns_{};
    std::uint64_t tail_unsynced_depth_events_{};
    bool closed_{};
    NativeSegmentQc current_segment_{};
    NativeDatasetSummary summary_{};
    std::string row_buffer_;
};

}  // namespace crypto::research
