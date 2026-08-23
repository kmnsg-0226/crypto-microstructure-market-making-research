#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>

#include "book/order_book.hpp"
#include "exchange/binance/message_types.hpp"

namespace crypto::research {

struct BestLevelState {
    std::int64_t bid_price_ticks{};
    std::int64_t bid_qty_lots{};
    std::int64_t ask_price_ticks{};
    std::int64_t ask_qty_lots{};

    bool operator==(const BestLevelState&) const = default;
};

struct InstantBookFeatures {
    std::array<binance::PriceLevel, 10> bids{};
    std::array<binance::PriceLevel, 10> asks{};
    bool has_l1{};
    bool has_l5{};
    bool has_l10{};
    std::int64_t total_bid_depth_l5{};
    std::int64_t total_ask_depth_l5{};
    std::int64_t total_bid_depth_l10{};
    std::int64_t total_ask_depth_l10{};
    std::optional<double> obi_l1;
    std::optional<double> obi_l5;
    std::optional<double> obi_l10;
    std::optional<double> weighted_obi_l1;
    std::optional<double> weighted_obi_l5;
    std::optional<double> weighted_obi_l10;
    std::optional<double> weighted_mid_ticks;
    std::optional<double> weighted_mid_minus_mid_ticks;
};

[[nodiscard]] std::optional<BestLevelState> best_level_state(const book::OrderBook& book);

[[nodiscard]] InstantBookFeatures calculate_instant_book_features(
    const book::OrderBook& book,
    double weighted_obi_lambda);

[[nodiscard]] std::int64_t l1_ofi_contribution(
    const BestLevelState& previous,
    const BestLevelState& current) noexcept;

}  // namespace crypto::research
