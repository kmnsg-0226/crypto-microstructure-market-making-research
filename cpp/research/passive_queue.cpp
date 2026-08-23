#include "research/passive_queue.hpp"

#include <algorithm>
#include <limits>
#include <stdexcept>

namespace crypto::research {
namespace {

bool earlier(
    std::uint64_t left_local,
    std::uint64_t left_ordinal,
    std::uint64_t right_local,
    std::uint64_t right_ordinal) noexcept {
    return left_local < right_local ||
           (left_local == right_local && left_ordinal < right_ordinal);
}

std::size_t lower_local(
    const std::vector<PassiveTradeIndex::IndexedTrade>& trades,
    std::uint64_t value) {
    return static_cast<std::size_t>(std::lower_bound(
        trades.begin(),
        trades.end(),
        value,
        [](const auto& trade, std::uint64_t timestamp) {
            return trade.local_time_us < timestamp;
        }) - trades.begin());
}

std::size_t upper_local(
    const std::vector<PassiveTradeIndex::IndexedTrade>& trades,
    std::uint64_t value) {
    return static_cast<std::size_t>(std::upper_bound(
        trades.begin(),
        trades.end(),
        value,
        [](std::uint64_t timestamp, const auto& trade) {
            return timestamp < trade.local_time_us;
        }) - trades.begin());
}

}  // namespace

const char* passive_fill_state_name(PassiveFillState state) noexcept {
    switch (state) {
        case PassiveFillState::Unfilled:
            return "unfilled";
        case PassiveFillState::Partial:
            return "partial";
        case PassiveFillState::Full:
            return "full";
    }
    return "unknown";
}

void PassiveTradeIndex::ExtremeTree::build(
    const std::vector<IndexedTrade>& trades,
    bool find_less) {
    find_less_ = find_less;
    trade_count_ = trades.size();
    leaf_count_ = 1;
    while (leaf_count_ < std::max<std::size_t>(1, trade_count_)) {
        leaf_count_ *= 2;
    }
    const auto identity = find_less_ ? std::numeric_limits<std::int64_t>::max()
                                     : std::numeric_limits<std::int64_t>::min();
    tree_.assign(leaf_count_ * 2, identity);
    for (std::size_t index = 0; index < trades.size(); ++index) {
        tree_[leaf_count_ + index] = trades[index].price_ticks;
    }
    for (std::size_t index = leaf_count_; index-- > 1;) {
        tree_[index] = find_less_ ? std::min(tree_[index * 2], tree_[index * 2 + 1])
                                  : std::max(tree_[index * 2], tree_[index * 2 + 1]);
    }
}

bool PassiveTradeIndex::ExtremeTree::satisfies(
    std::int64_t value,
    std::int64_t quote) const noexcept {
    return find_less_ ? value < quote : value > quote;
}

std::size_t PassiveTradeIndex::ExtremeTree::first_satisfying_impl(
    std::size_t node,
    std::size_t node_begin,
    std::size_t node_end,
    std::size_t query_begin,
    std::size_t query_end,
    std::int64_t quote_price_ticks) const {
    if (node_end <= query_begin || query_end <= node_begin ||
        !satisfies(tree_[node], quote_price_ticks)) {
        return trade_count_;
    }
    if (node_end - node_begin == 1) {
        return node_begin < trade_count_ ? node_begin : trade_count_;
    }
    const auto middle = node_begin + (node_end - node_begin) / 2;
    const auto left = first_satisfying_impl(
        node * 2,
        node_begin,
        middle,
        query_begin,
        query_end,
        quote_price_ticks);
    if (left != trade_count_) {
        return left;
    }
    return first_satisfying_impl(
        node * 2 + 1,
        middle,
        node_end,
        query_begin,
        query_end,
        quote_price_ticks);
}

std::size_t PassiveTradeIndex::ExtremeTree::first_satisfying(
    std::size_t begin,
    std::size_t end,
    std::int64_t quote_price_ticks) const {
    if (begin >= end || end > trade_count_ || tree_.empty()) {
        return trade_count_;
    }
    return first_satisfying_impl(1, 0, leaf_count_, begin, end, quote_price_ticks);
}

void PassiveTradeIndex::add_trade(const historical::TardisTrade& trade) {
    if (finalized_) {
        throw std::logic_error("cannot add a trade to a finalized passive index");
    }
    if (trade.side == historical::AggressorSide::Unknown) {
        throw std::invalid_argument("passive fill index rejects unknown aggressor side");
    }
    if (trade.price_ticks <= 0 || trade.qty_lots <= 0) {
        throw std::invalid_argument("passive fill index requires positive trade price and quantity");
    }
    local_timestamp_regressions_ +=
        rows_ != 0 && trade.local_timestamp_us < previous_local_timestamp_us_;
    previous_local_timestamp_us_ = trade.local_timestamp_us;
    const IndexedTrade indexed{
        trade.timestamp_us,
        trade.local_timestamp_us,
        rows_,
        trade.price_ticks,
        trade.qty_lots,
    };
    auto& trades = trade.side == historical::AggressorSide::Buy ? buys_ : sells_;
    auto& exact = trade.side == historical::AggressorSide::Buy ? buy_exact_ : sell_exact_;
    trades.push_back(indexed);
    auto& series = exact[trade.price_ticks];
    series.exchange_times.push_back(trade.timestamp_us);
    series.local_times.push_back(trade.local_timestamp_us);
    series.source_ordinals.push_back(rows_);
    series.quantities.push_back(trade.qty_lots);
    ++rows_;
}

void PassiveTradeIndex::finalize() {
    if (finalized_) {
        return;
    }
    if (local_timestamp_regressions_ != 0) {
        throw std::invalid_argument("trade local timestamps regress in passive index");
    }
    for (auto* exact : {&buy_exact_, &sell_exact_}) {
        for (auto& [price, series] : *exact) {
            (void)price;
            series.prefix_quantities.reserve(series.quantities.size() + 1);
            series.prefix_quantities.push_back(0);
            for (const auto quantity : series.quantities) {
                const auto previous = series.prefix_quantities.back();
                if (quantity > std::numeric_limits<std::int64_t>::max() - previous) {
                    throw std::overflow_error("passive exact-price quantity overflow");
                }
                series.prefix_quantities.push_back(previous + quantity);
            }
        }
    }
    buy_through_tree_.build(buys_, false);
    sell_through_tree_.build(sells_, true);
    finalized_ = true;
}

PassiveFillOutcome PassiveTradeIndex::evaluate_side(
    const std::vector<IndexedTrade>& trades,
    const std::unordered_map<std::int64_t, ExactPriceSeries>& exact,
    const ExtremeTree& through_tree,
    std::int64_t quote_price_ticks,
    std::int64_t order_qty_lots,
    std::int64_t queue_ahead_lots,
    std::uint64_t placement_local_time_us,
    std::uint64_t exclusive_end_local_time_us) {
    if (quote_price_ticks <= 0 || order_qty_lots <= 0 || queue_ahead_lots < 0 ||
        exclusive_end_local_time_us <= placement_local_time_us) {
        throw std::invalid_argument("invalid passive fill evaluation interval or quantity");
    }
    PassiveFillOutcome result;
    result.queue_ahead_initial_lots = queue_ahead_lots;
    const auto all_begin = upper_local(trades, placement_local_time_us);
    const auto all_end = lower_local(trades, exclusive_end_local_time_us);
    const auto through_index =
        through_tree.first_satisfying(all_begin, all_end, quote_price_ticks);

    const auto series_it = exact.find(quote_price_ticks);
    std::uint64_t first_exact_local{};
    std::uint64_t first_exact_exchange{};
    std::uint64_t first_exact_ordinal{std::numeric_limits<std::uint64_t>::max()};
    std::uint64_t full_exact_local{};
    std::uint64_t full_exact_exchange{};
    std::uint64_t full_exact_ordinal{std::numeric_limits<std::uint64_t>::max()};
    std::size_t exact_start{};
    std::size_t exact_end{};
    const ExactPriceSeries* exact_series{};
    if (series_it != exact.end()) {
        const auto& series = series_it->second;
        exact_series = &series;
        exact_start = static_cast<std::size_t>(std::upper_bound(
            series.local_times.begin(), series.local_times.end(), placement_local_time_us) -
            series.local_times.begin());
        exact_end = static_cast<std::size_t>(std::lower_bound(
            series.local_times.begin(), series.local_times.end(), exclusive_end_local_time_us) -
            series.local_times.begin());
        const auto start_prefix = series.prefix_quantities[exact_start];

        const auto find_crossing = [&](std::int64_t needed) -> std::size_t {
            if (needed <= 0) {
                return exact_end;
            }
            const auto target = start_prefix + needed;
            const auto iterator = std::lower_bound(
                series.prefix_quantities.begin() + static_cast<std::ptrdiff_t>(exact_start + 1),
                series.prefix_quantities.begin() + static_cast<std::ptrdiff_t>(exact_end + 1),
                target);
            return iterator == series.prefix_quantities.begin() +
                                   static_cast<std::ptrdiff_t>(exact_end + 1)
                ? exact_end
                : static_cast<std::size_t>(iterator - series.prefix_quantities.begin() - 1);
        };
        const auto first = find_crossing(queue_ahead_lots + 1);
        if (first < exact_end) {
            first_exact_local = series.local_times[first];
            first_exact_exchange = series.exchange_times[first];
            first_exact_ordinal = series.source_ordinals[first];
        }
        const auto full = find_crossing(queue_ahead_lots + order_qty_lots);
        if (full < exact_end) {
            full_exact_local = series.local_times[full];
            full_exact_exchange = series.exchange_times[full];
            full_exact_ordinal = series.source_ordinals[full];
        }
    }

    const bool have_through = through_index < trades.size();
    const auto through_local = have_through ? trades[through_index].local_time_us : 0;
    const auto through_exchange = have_through ? trades[through_index].exchange_time_us : 0;
    const auto through_ordinal = have_through
        ? trades[through_index].source_ordinal
        : std::numeric_limits<std::uint64_t>::max();
    const bool through_is_first = have_through &&
        (first_exact_local == 0 || earlier(
            through_local,
            through_ordinal,
            first_exact_local,
            first_exact_ordinal));
    if (through_is_first) {
        result.first_fill_local_time_us = through_local;
        result.first_fill_exchange_time_us = through_exchange;
        result.first_fill_source_ordinal = through_ordinal;
    } else if (first_exact_local != 0) {
        result.first_fill_local_time_us = first_exact_local;
        result.first_fill_exchange_time_us = first_exact_exchange;
        result.first_fill_source_ordinal = first_exact_ordinal;
    }

    const bool through_is_full = have_through &&
        (full_exact_local == 0 || earlier(
            through_local,
            through_ordinal,
            full_exact_local,
            full_exact_ordinal));

    // Diagnostic queue/trade quantities stop at the event that resolves the
    // order.  Trades later in the probe lifetime cannot affect an order that
    // was already fully filled by an exact-price print or trade-through.
    if (exact_series != nullptr) {
        std::size_t effective_end = exact_end;
        if (through_is_full) {
            std::size_t lower = exact_start;
            std::size_t upper = exact_end;
            while (lower < upper) {
                const auto middle = lower + (upper - lower) / 2;
                if (earlier(
                        exact_series->local_times[middle],
                        exact_series->source_ordinals[middle],
                        through_local,
                        through_ordinal)) {
                    lower = middle + 1;
                } else {
                    upper = middle;
                }
            }
            effective_end = lower;
        } else if (full_exact_local != 0) {
            const auto full_iterator = std::lower_bound(
                exact_series->source_ordinals.begin() +
                    static_cast<std::ptrdiff_t>(exact_start),
                exact_series->source_ordinals.begin() +
                    static_cast<std::ptrdiff_t>(exact_end),
                full_exact_ordinal);
            effective_end = static_cast<std::size_t>(
                full_iterator - exact_series->source_ordinals.begin()) + 1;
        }
        const auto total = exact_series->prefix_quantities[effective_end] -
                           exact_series->prefix_quantities[exact_start];
        result.same_price_traded_lots = total;
        result.queue_consumed_lots = std::min(queue_ahead_lots, total);
        result.filled_lots = std::clamp(
            total - queue_ahead_lots, std::int64_t{0}, order_qty_lots);
        result.qualifying_trade_count = effective_end - exact_start;
    }
    if (through_is_full) {
        result.full_fill_local_time_us = through_local;
        result.full_fill_exchange_time_us = through_exchange;
        result.full_fill_source_ordinal = through_ordinal;
        result.trade_through_price_ticks = trades[through_index].price_ticks;
        result.filled_by_trade_through = true;
        result.queue_consumed_lots = queue_ahead_lots;
        ++result.qualifying_trade_count;
        result.filled_lots = order_qty_lots;
    } else if (full_exact_local != 0) {
        result.full_fill_local_time_us = full_exact_local;
        result.full_fill_exchange_time_us = full_exact_exchange;
        result.full_fill_source_ordinal = full_exact_ordinal;
        result.filled_lots = order_qty_lots;
    }
    if (result.filled_lots == order_qty_lots) {
        result.state = PassiveFillState::Full;
    } else if (result.filled_lots > 0) {
        result.state = PassiveFillState::Partial;
    }
    return result;
}

PassiveFillOutcome PassiveTradeIndex::evaluate(
    PassiveQuoteSide quote_side,
    std::int64_t quote_price_ticks,
    std::int64_t order_qty_lots,
    std::int64_t queue_ahead_lots,
    std::uint64_t placement_local_time_us,
    std::uint64_t exclusive_end_local_time_us) const {
    if (!finalized_) {
        throw std::logic_error("passive trade index must be finalized before evaluation");
    }
    if (quote_side == PassiveQuoteSide::Bid) {
        return evaluate_side(
            sells_,
            sell_exact_,
            sell_through_tree_,
            quote_price_ticks,
            order_qty_lots,
            queue_ahead_lots,
            placement_local_time_us,
            exclusive_end_local_time_us);
    }
    return evaluate_side(
        buys_,
        buy_exact_,
        buy_through_tree_,
        quote_price_ticks,
        order_qty_lots,
        queue_ahead_lots,
        placement_local_time_us,
        exclusive_end_local_time_us);
}

}  // namespace crypto::research
