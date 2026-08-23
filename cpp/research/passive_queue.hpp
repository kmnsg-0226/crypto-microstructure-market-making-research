#pragma once

#include <cstddef>
#include <cstdint>
#include <limits>
#include <unordered_map>
#include <vector>

#include "historical/tardis_trade_reader.hpp"

namespace crypto::research {

enum class PassiveQuoteSide {
    Bid,
    Ask,
};

enum class PassiveFillState {
    Unfilled,
    Partial,
    Full,
};

struct PassiveFillOutcome {
    PassiveFillState state{PassiveFillState::Unfilled};
    std::int64_t queue_ahead_initial_lots{};
    std::int64_t queue_consumed_lots{};
    std::int64_t same_price_traded_lots{};
    std::int64_t filled_lots{};
    std::uint64_t qualifying_trade_count{};
    std::uint64_t first_fill_exchange_time_us{};
    std::uint64_t first_fill_local_time_us{};
    std::uint64_t full_fill_exchange_time_us{};
    std::uint64_t full_fill_local_time_us{};
    std::uint64_t first_fill_source_ordinal{std::numeric_limits<std::uint64_t>::max()};
    std::uint64_t full_fill_source_ordinal{std::numeric_limits<std::uint64_t>::max()};
    std::int64_t trade_through_price_ticks{};
    bool filled_by_trade_through{};

    bool operator==(const PassiveFillOutcome&) const = default;
};

class PassiveTradeIndex {
public:
    void add_trade(const historical::TardisTrade& trade);
    void finalize();

    [[nodiscard]] PassiveFillOutcome evaluate(
        PassiveQuoteSide quote_side,
        std::int64_t quote_price_ticks,
        std::int64_t order_qty_lots,
        std::int64_t queue_ahead_lots,
        std::uint64_t placement_local_time_us,
        std::uint64_t exclusive_end_local_time_us) const;

    [[nodiscard]] std::uint64_t rows() const noexcept { return rows_; }
    [[nodiscard]] std::uint64_t buy_trades() const noexcept { return buys_.size(); }
    [[nodiscard]] std::uint64_t sell_trades() const noexcept { return sells_.size(); }
    [[nodiscard]] std::uint64_t local_timestamp_regressions() const noexcept {
        return local_timestamp_regressions_;
    }

public:
    struct IndexedTrade {
        std::uint64_t exchange_time_us{};
        std::uint64_t local_time_us{};
        std::uint64_t source_ordinal{};
        std::int64_t price_ticks{};
        std::int64_t qty_lots{};
    };

    struct ExactPriceSeries {
        std::vector<std::uint64_t> exchange_times;
        std::vector<std::uint64_t> local_times;
        std::vector<std::uint64_t> source_ordinals;
        std::vector<std::int64_t> quantities;
        std::vector<std::int64_t> prefix_quantities;
    };

    class ExtremeTree {
    public:
        void build(const std::vector<IndexedTrade>& trades, bool find_less);
        [[nodiscard]] std::size_t first_satisfying(
            std::size_t begin,
            std::size_t end,
            std::int64_t quote_price_ticks) const;

    private:
        [[nodiscard]] std::size_t first_satisfying_impl(
            std::size_t node,
            std::size_t node_begin,
            std::size_t node_end,
            std::size_t query_begin,
            std::size_t query_end,
            std::int64_t quote_price_ticks) const;
        [[nodiscard]] bool satisfies(std::int64_t value, std::int64_t quote) const noexcept;

        std::vector<std::int64_t> tree_;
        std::size_t leaf_count_{};
        std::size_t trade_count_{};
        bool find_less_{};
    };

private:
    [[nodiscard]] static PassiveFillOutcome evaluate_side(
        const std::vector<IndexedTrade>& trades,
        const std::unordered_map<std::int64_t, ExactPriceSeries>& exact,
        const ExtremeTree& through_tree,
        std::int64_t quote_price_ticks,
        std::int64_t order_qty_lots,
        std::int64_t queue_ahead_lots,
        std::uint64_t placement_local_time_us,
        std::uint64_t exclusive_end_local_time_us);

    std::vector<IndexedTrade> buys_;
    std::vector<IndexedTrade> sells_;
    std::unordered_map<std::int64_t, ExactPriceSeries> buy_exact_;
    std::unordered_map<std::int64_t, ExactPriceSeries> sell_exact_;
    ExtremeTree buy_through_tree_;
    ExtremeTree sell_through_tree_;
    std::uint64_t rows_{};
    std::uint64_t previous_local_timestamp_us_{};
    std::uint64_t local_timestamp_regressions_{};
    bool finalized_{};
};

[[nodiscard]] const char* passive_fill_state_name(PassiveFillState state) noexcept;

}  // namespace crypto::research
