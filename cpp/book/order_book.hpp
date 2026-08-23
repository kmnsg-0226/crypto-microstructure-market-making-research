#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <map>
#include <optional>
#include <vector>

#include "exchange/binance/message_types.hpp"

namespace crypto::book {

class OrderBook {
public:
    using BidMap = std::map<std::int64_t, std::int64_t, std::greater<>>;
    using AskMap = std::map<std::int64_t, std::int64_t>;

    void clear() noexcept;
    void invalidate() noexcept;
    bool load_snapshot(const binance::DepthSnapshot& snapshot);
    bool apply_depth_event(const binance::DepthEvent& event);

    [[nodiscard]] std::optional<std::int64_t> best_bid() const noexcept;
    [[nodiscard]] std::optional<std::int64_t> best_ask() const noexcept;
    [[nodiscard]] std::optional<std::int64_t> spread_ticks() const noexcept;
    [[nodiscard]] std::optional<std::int64_t> mid_ticks_x2() const noexcept;
    [[nodiscard]] std::vector<binance::PriceLevel> top_n_bids(std::size_t n) const;
    [[nodiscard]] std::vector<binance::PriceLevel> top_n_asks(std::size_t n) const;
    [[nodiscard]] std::uint64_t last_update_id() const noexcept { return last_update_id_; }
    [[nodiscard]] bool is_valid() const noexcept { return valid_; }
    [[nodiscard]] std::uint64_t checksum() const noexcept;
    [[nodiscard]] const BidMap& bids() const noexcept { return bids_; }
    [[nodiscard]] const AskMap& asks() const noexcept { return asks_; }

private:
    template <typename Map>
    static bool apply_levels(Map& side, const std::vector<binance::PriceLevel>& levels);
    [[nodiscard]] bool is_uncrossed() const noexcept;

    BidMap bids_;
    AskMap asks_;
    std::uint64_t last_update_id_{};
    bool valid_{};
};

}  // namespace crypto::book
