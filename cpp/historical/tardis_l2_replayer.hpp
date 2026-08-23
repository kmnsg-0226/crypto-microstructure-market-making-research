#pragma once

#include <cstdint>
#include <filesystem>
#include <string>
#include <vector>

#include "research/tardis_daily_exporter.hpp"

namespace crypto::historical {

struct TardisSnapshotSummary {
    std::uint64_t start_timestamp_us{};
    std::uint64_t end_timestamp_us{};
    std::uint64_t start_local_timestamp_us{};
    std::uint64_t end_local_timestamp_us{};
    std::uint64_t previous_local_timestamp_us{};
    std::uint64_t rows{};
    bool valid{};
    std::int64_t best_bid_ticks{};
    std::int64_t best_ask_ticks{};

    bool operator==(const TardisSnapshotSummary&) const = default;
};

struct TardisReplaySummary {
    std::filesystem::path input_path;
    std::uint64_t rows{};
    std::uint64_t message_groups{};
    std::uint64_t snapshot_rows{};
    std::uint64_t snapshot_groups{};
    std::uint64_t delta_rows{};
    std::uint64_t delta_messages{};
    std::uint64_t pre_snapshot_rows{};
    std::uint64_t pre_snapshot_messages{};
    std::uint64_t skipped_invalid_messages{};
    std::uint64_t valid_states{};
    std::uint64_t invalid_states{};
    std::uint64_t snapshot_load_failures{};
    std::uint64_t crossed_book_states{};
    std::uint64_t empty_bid_states{};
    std::uint64_t empty_ask_states{};
    std::uint64_t exchange_timestamp_regressions{};
    std::uint64_t local_timestamp_regressions{};
    std::uint64_t mixed_snapshot_flag_messages{};
    std::uint64_t parse_failures{};
    std::uint64_t first_timestamp_us{};
    std::uint64_t last_timestamp_us{};
    std::uint64_t first_local_timestamp_us{};
    std::uint64_t last_local_timestamp_us{};
    std::uint64_t max_local_gap_us{};
    std::uint64_t max_local_gap_ending_us{};
    std::uint64_t conservative_reconnect_gap_us{};
    std::uint64_t first_bbo_local_timestamp_us{};
    std::int64_t first_bid_ticks{};
    std::int64_t first_bid_qty_lots{};
    std::int64_t first_ask_ticks{};
    std::int64_t first_ask_qty_lots{};
    std::uint64_t last_bbo_local_timestamp_us{};
    std::int64_t last_bid_ticks{};
    std::int64_t last_bid_qty_lots{};
    std::int64_t last_ask_ticks{};
    std::int64_t last_ask_qty_lots{};
    std::uint64_t final_book_checksum{};
    std::uint64_t replay_checksum{};
    bool saw_snapshot{};
    bool final_book_valid{};
    std::vector<TardisSnapshotSummary> snapshots;

    bool operator==(const TardisReplaySummary&) const = default;
};

struct TardisResearchRunSummary {
    TardisReplaySummary l2;
    research::TardisDailyExportSummary daily_export;
    std::uint64_t trade_rows{};
    std::uint64_t buy_trades{};
    std::uint64_t sell_trades{};
    std::uint64_t unknown_side_trades{};
    std::uint64_t trade_timestamp_regressions{};
    std::uint64_t trade_local_timestamp_regressions{};
    std::uint64_t trades_outside_event_day{};

    bool operator==(const TardisResearchRunSummary&) const = default;
};

class TardisL2Replayer {
public:
    TardisL2Replayer(std::string tick_size, std::string step_size);

    [[nodiscard]] TardisReplaySummary replay(const std::filesystem::path& input_path) const;
    [[nodiscard]] TardisResearchRunSummary replay_research_day(
        const std::filesystem::path& l2_path,
        const std::filesystem::path& trades_path,
        const std::filesystem::path& output_path,
        std::string date,
        std::uint64_t day_start_us,
        std::uint64_t day_end_us,
        std::uint64_t interval_us,
        double weighted_obi_lambda) const;

private:
    [[nodiscard]] TardisReplaySummary replay_impl(
        const std::filesystem::path& input_path,
        research::TardisDailyResearchExporter* exporter) const;

    std::string tick_size_;
    std::string step_size_;
};

}  // namespace crypto::historical
