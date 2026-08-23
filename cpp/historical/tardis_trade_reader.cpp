#include "historical/tardis_trade_reader.hpp"

#include <array>
#include <charconv>
#include <limits>
#include <stdexcept>

#include "historical/gzip_line_reader.hpp"

namespace crypto::historical {
namespace {

constexpr std::string_view expected_header =
    "exchange,symbol,timestamp,local_timestamp,id,side,price,amount";

std::array<std::string_view, 8> split_row(std::string_view line) {
    std::array<std::string_view, 8> fields;
    std::size_t start = 0;
    for (std::size_t i = 0; i < fields.size() - 1; ++i) {
        const auto comma = line.find(',', start);
        if (comma == std::string_view::npos) {
            throw std::invalid_argument("trade CSV row has fewer than eight columns");
        }
        fields[i] = line.substr(start, comma - start);
        start = comma + 1;
    }
    if (line.find(',', start) != std::string_view::npos) {
        throw std::invalid_argument("trade CSV row has more than eight columns");
    }
    fields.back() = line.substr(start);
    return fields;
}

std::uint64_t parse_u64(std::string_view value, const char* field) {
    std::uint64_t result{};
    const auto [end, error] = std::from_chars(value.data(), value.data() + value.size(), result);
    if (error != std::errc{} || end != value.data() + value.size()) {
        throw std::invalid_argument(std::string("invalid trade ") + field);
    }
    return result;
}

void checked_add(std::int64_t& destination, std::int64_t value) {
    if ((value > 0 && destination > std::numeric_limits<std::int64_t>::max() - value) ||
        (value < 0 && destination < std::numeric_limits<std::int64_t>::min() - value)) {
        throw std::overflow_error("trade bucket integer overflow");
    }
    destination += value;
}

std::int64_t checked_notional(std::int64_t price_ticks, std::int64_t qty_lots) {
    const __int128 value = static_cast<__int128>(price_ticks) * qty_lots;
    if (value > std::numeric_limits<std::int64_t>::max() || value < 0) {
        throw std::overflow_error("trade notional integer overflow");
    }
    return static_cast<std::int64_t>(value);
}

}  // namespace

TardisTrade parse_tardis_trade_row(
    std::string_view line,
    const binance::DecimalIncrement& price_increment,
    const binance::DecimalIncrement& qty_increment) {
    const auto fields = split_row(line);
    if (fields[0] != "binance-futures" || fields[1] != "BTCUSDT") {
        throw std::invalid_argument("unexpected exchange or symbol in trade row");
    }
    TardisTrade result;
    result.timestamp_us = parse_u64(fields[2], "timestamp");
    result.local_timestamp_us = parse_u64(fields[3], "local_timestamp");
    result.id = std::string(fields[4]);
    if (fields[5] == "buy") {
        result.side = AggressorSide::Buy;
    } else if (fields[5] == "sell") {
        result.side = AggressorSide::Sell;
    } else if (fields[5] == "unknown") {
        result.side = AggressorSide::Unknown;
    } else {
        throw std::invalid_argument("invalid Tardis aggressor side");
    }
    result.price_ticks = price_increment.to_steps(fields[6]);
    result.qty_lots = qty_increment.to_steps(fields[7]);
    if (result.price_ticks <= 0 || result.qty_lots <= 0) {
        throw std::invalid_argument("trade price and quantity must be positive");
    }
    return result;
}

TardisTradeDay read_tardis_trade_day(
    const std::filesystem::path& path,
    std::uint64_t day_start_us,
    std::uint64_t day_end_us,
    std::uint64_t grid_interval_us,
    const binance::DecimalIncrement& price_increment,
    const binance::DecimalIncrement& qty_increment) {
    if (day_end_us <= day_start_us || grid_interval_us == 0 ||
        (day_end_us - day_start_us) % grid_interval_us != 0) {
        throw std::invalid_argument("invalid trade aggregation time grid");
    }
    TardisTradeDay result;
    result.buckets.resize((day_end_us - day_start_us) / grid_interval_us);
    bool saw_header = false;
    std::uint64_t previous_timestamp{};
    std::uint64_t previous_local_timestamp{};
    for_each_gzip_line(path, [&](std::string_view line) {
        if (!saw_header) {
            if (line != expected_header) {
                throw std::invalid_argument("unexpected Tardis trade CSV header");
            }
            saw_header = true;
            return;
        }
        if (line.empty()) {
            return;
        }
        const auto trade = parse_tardis_trade_row(line, price_increment, qty_increment);
        if (result.rows == 0) {
            result.first_timestamp_us = trade.timestamp_us;
            result.first_local_timestamp_us = trade.local_timestamp_us;
        } else {
            result.timestamp_regressions += trade.timestamp_us < previous_timestamp;
            result.local_timestamp_regressions += trade.local_timestamp_us < previous_local_timestamp;
        }
        ++result.rows;
        result.last_timestamp_us = trade.timestamp_us;
        result.last_local_timestamp_us = trade.local_timestamp_us;
        previous_timestamp = trade.timestamp_us;
        previous_local_timestamp = trade.local_timestamp_us;

        if (trade.side == AggressorSide::Buy) {
            ++result.buy_trades;
        } else if (trade.side == AggressorSide::Sell) {
            ++result.sell_trades;
        } else {
            ++result.unknown_side_trades;
        }
        if (trade.timestamp_us < day_start_us || trade.timestamp_us >= day_end_us) {
            ++result.outside_event_day_rows;
            return;
        }
        const auto offset = trade.timestamp_us - day_start_us;
        const auto index = (offset + grid_interval_us - 1U) / grid_interval_us;
        if (index >= result.buckets.size()) {
            ++result.outside_event_day_rows;
            return;
        }
        auto& bucket = result.buckets[index];
        const auto notional = checked_notional(trade.price_ticks, trade.qty_lots);
        if (trade.side == AggressorSide::Buy) {
            checked_add(bucket.aggressive_buy_qty_lots, trade.qty_lots);
            checked_add(bucket.aggressive_buy_notional_units, notional);
            ++bucket.buy_trade_count;
        } else if (trade.side == AggressorSide::Sell) {
            checked_add(bucket.aggressive_sell_qty_lots, trade.qty_lots);
            checked_add(bucket.aggressive_sell_notional_units, notional);
            ++bucket.sell_trade_count;
        }
        ++bucket.trade_count;
        bucket.latest_timestamp_us = trade.timestamp_us;
        bucket.latest_local_timestamp_us = trade.local_timestamp_us;
    });
    if (!saw_header || result.rows == 0) {
        throw std::invalid_argument("empty Tardis trade CSV");
    }
    return result;
}

}  // namespace crypto::historical
