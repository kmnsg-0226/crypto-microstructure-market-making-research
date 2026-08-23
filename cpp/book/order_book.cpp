#include "book/order_book.hpp"

#include <limits>

namespace crypto::book {
namespace {

void hash_u64(std::uint64_t value, std::uint64_t& hash) noexcept {
    constexpr std::uint64_t prime = 1099511628211ULL;
    for (int i = 0; i < 8; ++i) {
        hash ^= value & 0xffU;
        hash *= prime;
        value >>= 8U;
    }
}

}  // namespace

void OrderBook::clear() noexcept {
    bids_.clear();
    asks_.clear();
    last_update_id_ = 0;
    valid_ = false;
}

void OrderBook::invalidate() noexcept {
    valid_ = false;
}

template <typename Map>
bool OrderBook::apply_levels(Map& side, const std::vector<binance::PriceLevel>& levels) {
    for (const auto& level : levels) {
        if (level.price_ticks < 0 || level.qty_lots < 0) {
            return false;
        }
        if (level.qty_lots == 0) {
            side.erase(level.price_ticks);
        } else {
            side[level.price_ticks] = level.qty_lots;
        }
    }
    return true;
}

bool OrderBook::is_uncrossed() const noexcept {
    return !bids_.empty() && !asks_.empty() && bids_.begin()->first < asks_.begin()->first;
}

bool OrderBook::load_snapshot(const binance::DepthSnapshot& snapshot) {
    clear();
    if (!apply_levels(bids_, snapshot.bids) || !apply_levels(asks_, snapshot.asks)) {
        clear();
        return false;
    }
    last_update_id_ = snapshot.last_update_id;
    valid_ = is_uncrossed();
    return valid_;
}

bool OrderBook::apply_depth_event(const binance::DepthEvent& event) {
    if (!valid_) {
        return false;
    }
    if (!apply_levels(bids_, event.bids) || !apply_levels(asks_, event.asks)) {
        invalidate();
        return false;
    }
    last_update_id_ = event.final_update_id;
    if (!is_uncrossed()) {
        invalidate();
        return false;
    }
    return true;
}

std::optional<std::int64_t> OrderBook::best_bid() const noexcept {
    if (!valid_ || bids_.empty()) {
        return std::nullopt;
    }
    return bids_.begin()->first;
}

std::optional<std::int64_t> OrderBook::best_ask() const noexcept {
    if (!valid_ || asks_.empty()) {
        return std::nullopt;
    }
    return asks_.begin()->first;
}

std::optional<std::int64_t> OrderBook::spread_ticks() const noexcept {
    const auto bid = best_bid();
    const auto ask = best_ask();
    if (!bid || !ask) {
        return std::nullopt;
    }
    return *ask - *bid;
}

std::optional<std::int64_t> OrderBook::mid_ticks_x2() const noexcept {
    const auto bid = best_bid();
    const auto ask = best_ask();
    if (!bid || !ask) {
        return std::nullopt;
    }
    if (*bid > std::numeric_limits<std::int64_t>::max() - *ask) {
        return std::nullopt;
    }
    return *bid + *ask;
}

std::vector<binance::PriceLevel> OrderBook::top_n_bids(std::size_t n) const {
    std::vector<binance::PriceLevel> result;
    if (!valid_) {
        return result;
    }
    result.reserve(std::min(n, bids_.size()));
    for (const auto& [price, qty] : bids_) {
        if (result.size() == n) {
            break;
        }
        result.push_back({price, qty});
    }
    return result;
}

std::vector<binance::PriceLevel> OrderBook::top_n_asks(std::size_t n) const {
    std::vector<binance::PriceLevel> result;
    if (!valid_) {
        return result;
    }
    result.reserve(std::min(n, asks_.size()));
    for (const auto& [price, qty] : asks_) {
        if (result.size() == n) {
            break;
        }
        result.push_back({price, qty});
    }
    return result;
}

std::uint64_t OrderBook::checksum() const noexcept {
    std::uint64_t hash = 1469598103934665603ULL;
    hash_u64(last_update_id_, hash);
    hash_u64(valid_ ? 1U : 0U, hash);
    hash_u64(bids_.size(), hash);
    for (const auto& [price, qty] : bids_) {
        hash_u64(static_cast<std::uint64_t>(price), hash);
        hash_u64(static_cast<std::uint64_t>(qty), hash);
    }
    hash_u64(asks_.size(), hash);
    for (const auto& [price, qty] : asks_) {
        hash_u64(static_cast<std::uint64_t>(price), hash);
        hash_u64(static_cast<std::uint64_t>(qty), hash);
    }
    return hash;
}

}  // namespace crypto::book
