#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace crypto::binance {

struct PriceLevel {
    std::int64_t price_ticks{};
    std::int64_t qty_lots{};

    bool operator==(const PriceLevel&) const = default;
};

struct DepthSnapshot {
    std::uint64_t last_update_id{};
    std::int64_t event_time_ms{};
    std::int64_t transaction_time_ms{};
    std::vector<PriceLevel> bids;
    std::vector<PriceLevel> asks;
};

struct DepthEvent {
    std::string symbol;
    std::int64_t event_time_ms{};
    std::int64_t transaction_time_ms{};
    std::uint64_t first_update_id{};
    std::uint64_t final_update_id{};
    std::uint64_t previous_final_update_id{};
    std::vector<PriceLevel> bids;
    std::vector<PriceLevel> asks;
};

struct AggTradeEvent {
    std::string symbol;
    std::int64_t event_time_ms{};
    std::int64_t transaction_time_ms{};
    std::uint64_t aggregate_trade_id{};
    std::uint64_t first_trade_id{};
    std::uint64_t last_trade_id{};
    std::int64_t price_ticks{};
    std::int64_t qty_lots{};
    bool buyer_is_maker{};

    [[nodiscard]] bool is_aggressive_buy() const noexcept { return !buyer_is_maker; }
};

}  // namespace crypto::binance
