#include "research/microstructure_features.hpp"

#include <algorithm>
#include <cmath>
#include <vector>

namespace crypto::research {
namespace {

std::optional<double> imbalance(
    const std::array<binance::PriceLevel, 10>& bids,
    const std::array<binance::PriceLevel, 10>& asks,
    std::size_t levels,
    double lambda) {
    double bid = 0.0;
    double ask = 0.0;
    for (std::size_t i = 0; i < levels; ++i) {
        const double weight = std::exp(-lambda * static_cast<double>(i));
        bid += weight * static_cast<double>(bids[i].qty_lots);
        ask += weight * static_cast<double>(asks[i].qty_lots);
    }
    const double total = bid + ask;
    if (total == 0.0) {
        return std::nullopt;
    }
    return (bid - ask) / total;
}

std::optional<double> unweighted_imbalance(
    const std::array<binance::PriceLevel, 10>& bids,
    const std::array<binance::PriceLevel, 10>& asks,
    std::size_t levels) {
    std::int64_t bid{};
    std::int64_t ask{};
    for (std::size_t i = 0; i < levels; ++i) {
        bid += bids[i].qty_lots;
        ask += asks[i].qty_lots;
    }
    const auto total = bid + ask;
    if (total == 0) {
        return std::nullopt;
    }
    return static_cast<double>(bid - ask) / static_cast<double>(total);
}

}  // namespace

std::optional<BestLevelState> best_level_state(const book::OrderBook& book) {
    if (!book.is_valid() || book.bids().empty() || book.asks().empty()) {
        return std::nullopt;
    }
    const auto& [bid_price, bid_qty] = *book.bids().begin();
    const auto& [ask_price, ask_qty] = *book.asks().begin();
    return BestLevelState{bid_price, bid_qty, ask_price, ask_qty};
}

InstantBookFeatures calculate_instant_book_features(
    const book::OrderBook& book,
    double weighted_obi_lambda) {
    InstantBookFeatures result;
    if (!book.is_valid() || weighted_obi_lambda < 0.0) {
        return result;
    }
    const auto bids = book.top_n_bids(10);
    const auto asks = book.top_n_asks(10);
    const auto copy_count = [](const std::vector<binance::PriceLevel>& source, auto& destination) {
        std::copy_n(source.begin(), std::min(source.size(), destination.size()), destination.begin());
    };
    copy_count(bids, result.bids);
    copy_count(asks, result.asks);
    result.has_l1 = !bids.empty() && !asks.empty();
    result.has_l5 = bids.size() >= 5 && asks.size() >= 5;
    result.has_l10 = bids.size() >= 10 && asks.size() >= 10;

    if (result.has_l5) {
        for (std::size_t i = 0; i < 5; ++i) {
            result.total_bid_depth_l5 += result.bids[i].qty_lots;
            result.total_ask_depth_l5 += result.asks[i].qty_lots;
        }
    }
    if (result.has_l10) {
        for (std::size_t i = 0; i < 10; ++i) {
            result.total_bid_depth_l10 += result.bids[i].qty_lots;
            result.total_ask_depth_l10 += result.asks[i].qty_lots;
        }
    }
    if (result.has_l1) {
        result.obi_l1 = unweighted_imbalance(result.bids, result.asks, 1);
        result.weighted_obi_l1 = imbalance(result.bids, result.asks, 1, weighted_obi_lambda);
        const auto bid_qty = static_cast<double>(result.bids[0].qty_lots);
        const auto ask_qty = static_cast<double>(result.asks[0].qty_lots);
        const auto total = bid_qty + ask_qty;
        if (total > 0.0) {
            const double weighted_mid =
                (ask_qty * static_cast<double>(result.bids[0].price_ticks) +
                 bid_qty * static_cast<double>(result.asks[0].price_ticks)) /
                total;
            result.weighted_mid_ticks = weighted_mid;
            result.weighted_mid_minus_mid_ticks =
                weighted_mid -
                (static_cast<double>(result.bids[0].price_ticks) +
                 static_cast<double>(result.asks[0].price_ticks)) /
                    2.0;
        }
    }
    if (result.has_l5) {
        result.obi_l5 = unweighted_imbalance(result.bids, result.asks, 5);
        result.weighted_obi_l5 = imbalance(result.bids, result.asks, 5, weighted_obi_lambda);
    }
    if (result.has_l10) {
        result.obi_l10 = unweighted_imbalance(result.bids, result.asks, 10);
        result.weighted_obi_l10 = imbalance(result.bids, result.asks, 10, weighted_obi_lambda);
    }
    return result;
}

std::int64_t l1_ofi_contribution(
    const BestLevelState& previous,
    const BestLevelState& current) noexcept {
    std::int64_t value = 0;
    if (current.bid_price_ticks >= previous.bid_price_ticks) {
        value += current.bid_qty_lots;
    }
    if (current.bid_price_ticks <= previous.bid_price_ticks) {
        value -= previous.bid_qty_lots;
    }
    if (current.ask_price_ticks <= previous.ask_price_ticks) {
        value -= current.ask_qty_lots;
    }
    if (current.ask_price_ticks >= previous.ask_price_ticks) {
        value += previous.ask_qty_lots;
    }
    return value;
}

}  // namespace crypto::research
