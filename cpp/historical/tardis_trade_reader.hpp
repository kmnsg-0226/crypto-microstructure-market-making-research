#pragma once

#include <cstdint>
#include <filesystem>
#include <string>
#include <string_view>
#include <vector>

#include "exchange/binance/instrument_info.hpp"

namespace crypto::historical {

enum class AggressorSide {
    Buy,
    Sell,
    Unknown,
};

struct TardisTrade {
    std::uint64_t timestamp_us{};
    std::uint64_t local_timestamp_us{};
    std::string id;
    AggressorSide side{AggressorSide::Unknown};
    std::int64_t price_ticks{};
    std::int64_t qty_lots{};
};

struct TardisTradeBucket {
    std::int64_t aggressive_buy_qty_lots{};
    std::int64_t aggressive_sell_qty_lots{};
    std::int64_t aggressive_buy_notional_units{};
    std::int64_t aggressive_sell_notional_units{};
    std::uint64_t trade_count{};
    std::uint64_t buy_trade_count{};
    std::uint64_t sell_trade_count{};
    std::uint64_t latest_timestamp_us{};
    std::uint64_t latest_local_timestamp_us{};
};

struct TardisTradeDay {
    std::vector<TardisTradeBucket> buckets;
    std::uint64_t rows{};
    std::uint64_t buy_trades{};
    std::uint64_t sell_trades{};
    std::uint64_t unknown_side_trades{};
    std::uint64_t timestamp_regressions{};
    std::uint64_t local_timestamp_regressions{};
    std::uint64_t outside_event_day_rows{};
    std::uint64_t first_timestamp_us{};
    std::uint64_t last_timestamp_us{};
    std::uint64_t first_local_timestamp_us{};
    std::uint64_t last_local_timestamp_us{};
};

[[nodiscard]] TardisTrade parse_tardis_trade_row(
    std::string_view line,
    const binance::DecimalIncrement& price_increment,
    const binance::DecimalIncrement& qty_increment);

[[nodiscard]] TardisTradeDay read_tardis_trade_day(
    const std::filesystem::path& path,
    std::uint64_t day_start_us,
    std::uint64_t day_end_us,
    std::uint64_t grid_interval_us,
    const binance::DecimalIncrement& price_increment,
    const binance::DecimalIncrement& qty_increment);

}  // namespace crypto::historical
