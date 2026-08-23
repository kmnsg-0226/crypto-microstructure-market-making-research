#include "research/event_dataset_exporter.hpp"

#include <algorithm>
#include <cmath>
#include <iomanip>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <string_view>

#include "research/compressed_text_writer.hpp"
#include "research/microstructure_features.hpp"

namespace crypto::research {
namespace {

void accumulate(EventDatasetExporter::Contribution& into,
                const EventDatasetExporter::Contribution& value,
                int sign) {
    into.delta_obi += static_cast<double>(sign) * value.delta_obi;
    into.ofi_lots += sign * value.ofi_lots;
    into.add_imbalance_lots += sign * value.add_imbalance_lots;
    into.cancel_imbalance_lots += sign * value.cancel_imbalance_lots;
    into.depletion_imbalance_lots += sign * value.depletion_imbalance_lots;
    into.replenishment_imbalance_lots += sign * value.replenishment_imbalance_lots;
    if (sign > 0) {
        into.bbo_changes += value.bbo_changes;
        into.depth_changes += value.depth_changes;
        into.trade_count += value.trade_count;
        into.event_count += value.event_count;
    } else {
        into.bbo_changes -= value.bbo_changes;
        into.depth_changes -= value.depth_changes;
        into.trade_count -= value.trade_count;
        into.event_count -= value.event_count;
    }
    into.trade_qty_imbalance_lots += sign * value.trade_qty_imbalance_lots;
    into.trade_count_imbalance += sign * value.trade_count_imbalance;
    into.total_trade_qty_lots += sign * value.total_trade_qty_lots;
    into.mid_abs_change_ticks += static_cast<double>(sign) * value.mid_abs_change_ticks;
}

std::int64_t quantity_at(
    const book::OrderBook::BidMap& side,
    std::int64_t price_ticks) {
    const auto found = side.find(price_ticks);
    return found == side.end() ? 0 : found->second;
}

std::int64_t quantity_at(
    const book::OrderBook::AskMap& side,
    std::int64_t price_ticks) {
    const auto found = side.find(price_ticks);
    return found == side.end() ? 0 : found->second;
}

double elapsed_ms(std::uint64_t now, std::uint64_t then) {
    return then != 0 && now >= then
        ? static_cast<double>(now - then) / 1000.0
        : -1.0;
}

std::string optional_u64(std::uint64_t value) {
    return value == 0 ? std::string{} : std::to_string(value);
}

const char* side_name(PassiveQuoteSide side) {
    return side == PassiveQuoteSide::Bid ? "bid" : "ask";
}

std::string header() {
    std::ostringstream out;
    out << "schema_version,date,opportunity_id,decision_event_type,decision_exchange_time_us,"
           "decision_local_time_us,placement_local_time_us,feature_segment_id,valid_book_state,"
           "side,quote_side,quote_price,quote_qty,expiry_local_time_us,best_bid_price,"
           "best_bid_qty,best_ask_price,best_ask_qty,side_obi_l1,side_obi_l5,side_obi_l10,"
           "side_weighted_obi_l10,side_weighted_mid_minus_mid_ticks,spread_ticks,"
           "bid_depth_l1_lots,ask_depth_l1_lots,bid_depth_l5_lots,ask_depth_l5_lots,"
           "bid_depth_l10_lots,ask_depth_l10_lots,queue_ahead_lots,queue_to_l1_ratio";
    for (const auto window : std::array<int, 5>{10, 50, 100, 500, 1000}) {
        out << ",side_delta_obi_l10_" << window << "ms"
            << ",side_ofi_lots_" << window << "ms"
            << ",side_add_lots_" << window << "ms"
            << ",side_cancel_lots_" << window << "ms"
            << ",side_depletion_lots_" << window << "ms"
            << ",side_replenishment_lots_" << window << "ms"
            << ",bbo_change_count_" << window << "ms"
            << ",depth_change_count_" << window << "ms"
            << ",side_trade_qty_imbalance_" << window << "ms"
            << ",side_trade_count_imbalance_" << window << "ms"
            << ",trade_intensity_" << window << "ms"
            << ",average_trade_size_lots_" << window << "ms"
            << ",side_last_trade_streak_" << window << "ms";
    }
    out << ",time_since_trade_ms,time_since_book_ms,time_since_bbo_change_ms,"
           "time_since_mid_change_ms,event_arrival_intensity_100ms,"
           "backward_mid_return_abs_sum_100ms,backward_mid_return_abs_sum_1000ms,"
           "fill_status,first_fill_exchange_time_us,first_fill_local_time_us,"
           "full_fill_exchange_time_us,full_fill_local_time_us,filled_qty,"
           "queue_consumed_lots,invalid_due_to_snapshot\n";
    return out.str();
}

}  // namespace

EventDatasetExporter::EventDatasetExporter(
    std::filesystem::path output_path,
    std::string date,
    historical::TardisEventTradeFile trades,
    std::uint64_t cooldown_us,
    std::uint64_t quote_lifetime_us,
    std::int64_t quote_qty_lots,
    std::string tick_size,
    std::string step_size,
    double weighted_obi_lambda)
    : output_path_(std::move(output_path)),
      date_(std::move(date)),
      trades_(std::move(trades)),
      writer_(std::make_unique<CompressedTextWriter>(output_path_)),
      price_increment_(std::move(tick_size)),
      qty_increment_(std::move(step_size)),
      cooldown_us_(cooldown_us),
      quote_lifetime_us_(quote_lifetime_us),
      quote_qty_lots_(quote_qty_lots),
      weighted_obi_lambda_(weighted_obi_lambda) {
    if (cooldown_us_ == 0 || quote_lifetime_us_ == 0 || quote_qty_lots_ <= 0) {
        throw std::invalid_argument("invalid event dataset timing or quote quantity");
    }
    if (trades_.unknown_side_trades != 0 || trades_.local_timestamp_regressions != 0) {
        throw std::invalid_argument("event dataset requires ordered known-side trades");
    }
    for (const auto& trade : trades_.trades) {
        trade_index_.add_trade(trade);
    }
    trade_index_.finalize();
    writer_->append(header());
}

EventDatasetExporter::~EventDatasetExporter() {
    try {
        if (!closed_) {
            writer_->close();
        }
    } catch (...) {
    }
}

void EventDatasetExporter::add_contribution(const Contribution& contribution) {
    for (auto& window : windows_) {
        window.events.push_back(contribution);
        accumulate(window.sum, contribution, 1);
    }
}

void EventDatasetExporter::prune_windows(std::uint64_t local_timestamp_us) {
    for (std::size_t index = 0; index < windows_.size(); ++index) {
        auto& window = windows_[index];
        const auto width = windows_us_[index];
        while (!window.events.empty() &&
               window.events.front().local_time_us + width < local_timestamp_us) {
            accumulate(window.sum, window.events.front(), -1);
            window.events.pop_front();
        }
    }
}

bool EventDatasetExporter::process_trades_before(
    std::uint64_t local_timestamp_us,
    const book::OrderBook& book) {
    bool processed = false;
    while (next_trade_ < trades_.trades.size() &&
           trades_.trades[next_trade_].local_timestamp_us < local_timestamp_us) {
        process_trade_group(book);
        processed = true;
    }
    return processed;
}

bool EventDatasetExporter::process_trades_equal(
    std::uint64_t local_timestamp_us,
    const book::OrderBook& book) {
    if (next_trade_ >= trades_.trades.size() ||
        trades_.trades[next_trade_].local_timestamp_us != local_timestamp_us) {
        return false;
    }
    process_trade_group(book);
    return true;
}

void EventDatasetExporter::process_trade_group(const book::OrderBook& book) {
    const auto local_time = trades_.trades[next_trade_].local_timestamp_us;
    resolve_until(local_time);
    Contribution contribution;
    contribution.local_time_us = local_time;
    std::uint64_t latest_exchange{};
    while (next_trade_ < trades_.trades.size() &&
           trades_.trades[next_trade_].local_timestamp_us == local_time) {
        const auto& trade = trades_.trades[next_trade_++];
        latest_exchange = std::max(latest_exchange, trade.timestamp_us);
        if (trade.side == historical::AggressorSide::Buy) {
            contribution.trade_qty_imbalance_lots += trade.qty_lots;
            ++contribution.trade_count_imbalance;
            last_trade_streak_ = last_trade_streak_ >= 0 ? last_trade_streak_ + 1 : 1;
        } else {
            contribution.trade_qty_imbalance_lots -= trade.qty_lots;
            --contribution.trade_count_imbalance;
            last_trade_streak_ = last_trade_streak_ <= 0 ? last_trade_streak_ - 1 : -1;
        }
        contribution.total_trade_qty_lots += trade.qty_lots;
        ++contribution.trade_count;
        ++contribution.event_count;
        ++summary_.trade_events;
    }
    last_trade_local_time_us_ = local_time;
    add_contribution(contribution);
    prune_windows(local_time);
    maybe_decide(book, latest_exchange, local_time, "trade");
}

void EventDatasetExporter::before_depth_event(
    std::uint64_t,
    std::uint64_t local_timestamp_us,
    const binance::DepthEvent& event,
    const book::OrderBook& book) {
    process_trades_before(local_timestamp_us, book);
    resolve_until(local_timestamp_us);
    pending_book_contribution_ = {};
    pending_book_contribution_.local_time_us = local_timestamp_us;
    pending_book_contribution_.event_count = 1;
    const auto state = best_level_state(book);
    before_best_bid_ = state ? state->bid_price_ticks : 0;
    before_best_ask_ = state ? state->ask_price_ticks : 0;
    before_mid_x2_ = state ? state->bid_price_ticks + state->ask_price_ticks : 0;

    for (const auto& level : event.bids) {
        const auto old_qty = quantity_at(book.bids(), level.price_ticks);
        const auto difference = level.qty_lots - old_qty;
        pending_book_contribution_.ofi_lots += difference;
        ++pending_book_contribution_.depth_changes;
        if (difference > 0) {
            pending_book_contribution_.add_imbalance_lots += difference;
            if (level.price_ticks == before_best_bid_) {
                pending_book_contribution_.replenishment_imbalance_lots += difference;
            }
        } else if (difference < 0) {
            const auto removed = -difference;
            pending_book_contribution_.cancel_imbalance_lots -= removed;
            if (level.price_ticks == before_best_bid_) {
                pending_book_contribution_.depletion_imbalance_lots -= removed;
            }
        }
    }
    for (const auto& level : event.asks) {
        const auto old_qty = quantity_at(book.asks(), level.price_ticks);
        const auto difference = level.qty_lots - old_qty;
        pending_book_contribution_.ofi_lots -= difference;
        ++pending_book_contribution_.depth_changes;
        if (difference > 0) {
            pending_book_contribution_.add_imbalance_lots -= difference;
            if (level.price_ticks == before_best_ask_) {
                pending_book_contribution_.replenishment_imbalance_lots -= difference;
            }
        } else if (difference < 0) {
            const auto removed = -difference;
            pending_book_contribution_.cancel_imbalance_lots += removed;
            if (level.price_ticks == before_best_ask_) {
                pending_book_contribution_.depletion_imbalance_lots += removed;
            }
        }
    }
}

void EventDatasetExporter::after_depth_event(
    std::uint64_t exchange_timestamp_us,
    std::uint64_t local_timestamp_us,
    const binance::DepthEvent&,
    const book::OrderBook& book) {
    ++summary_.book_events;
    last_book_local_time_us_ = local_timestamp_us;
    if (!book.is_valid()) {
        ++summary_.invalid_book_events;
        return;
    }
    const auto features = calculate_instant_book_features(book, weighted_obi_lambda_);
    if (features.obi_l10) {
        if (have_previous_obi_) {
            pending_book_contribution_.delta_obi = *features.obi_l10 - previous_obi_l10_;
        }
        previous_obi_l10_ = *features.obi_l10;
        have_previous_obi_ = true;
    }
    const auto state = best_level_state(book);
    const bool bbo_changed = state &&
        (state->bid_price_ticks != before_best_bid_ || state->ask_price_ticks != before_best_ask_);
    if (bbo_changed) {
        pending_book_contribution_.bbo_changes = 1;
        ++summary_.bbo_changes;
        last_bbo_change_local_time_us_ = local_timestamp_us;
    }
    if (state) {
        const auto mid_x2 = state->bid_price_ticks + state->ask_price_ticks;
        if (before_mid_x2_ != 0 && mid_x2 != before_mid_x2_) {
            pending_book_contribution_.mid_abs_change_ticks =
                std::abs(static_cast<double>(mid_x2 - before_mid_x2_)) / 2.0;
            last_mid_change_local_time_us_ = local_timestamp_us;
        }
    }
    add_contribution(pending_book_contribution_);
    prune_windows(local_timestamp_us);
    const bool same_time_trade = process_trades_equal(local_timestamp_us, book);
    if (bbo_changed && !same_time_trade) {
        maybe_decide(book, exchange_timestamp_us, local_timestamp_us, "book_bbo");
    } else if (bbo_changed && same_time_trade &&
               last_decision_local_time_us_ != local_timestamp_us) {
        maybe_decide(book, exchange_timestamp_us, local_timestamp_us, "book_bbo_trade");
    }
}

void EventDatasetExporter::before_snapshot(
    std::uint64_t,
    std::uint64_t local_timestamp_us,
    const book::OrderBook& book) {
    process_trades_before(local_timestamp_us, book);
    resolve_until(local_timestamp_us);
    resolve_all(local_timestamp_us, true);
}

void EventDatasetExporter::after_snapshot(
    std::uint64_t,
    std::uint64_t local_timestamp_us,
    const book::OrderBook& book) {
    reset_segment(local_timestamp_us);
    process_trades_equal(local_timestamp_us, book);
}

void EventDatasetExporter::reset_segment(std::uint64_t local_timestamp_us) {
    ++segment_id_;
    for (auto& window : windows_) {
        window = {};
    }
    have_previous_obi_ = false;
    last_trade_streak_ = 0;
    last_book_local_time_us_ = local_timestamp_us;
    last_bbo_change_local_time_us_ = local_timestamp_us;
    last_mid_change_local_time_us_ = local_timestamp_us;
}

void EventDatasetExporter::maybe_decide(
    const book::OrderBook& book,
    std::uint64_t exchange_timestamp_us,
    std::uint64_t local_timestamp_us,
    const char* event_type) {
    if (!book.is_valid()) {
        ++summary_.invalid_book_events;
        return;
    }
    if (last_decision_local_time_us_ != 0 &&
        local_timestamp_us < last_decision_local_time_us_ + cooldown_us_) {
        ++summary_.cooldown_suppressed;
        return;
    }
    prune_windows(local_timestamp_us);
    const auto features = calculate_instant_book_features(book, weighted_obi_lambda_);
    if (!features.has_l10 || !features.obi_l1 || !features.obi_l5 || !features.obi_l10 ||
        !features.weighted_obi_l10 || !features.weighted_mid_minus_mid_ticks) {
        ++summary_.invalid_book_events;
        return;
    }
    last_decision_local_time_us_ = local_timestamp_us;
    ++opportunity_id_;
    ++summary_.decision_opportunities;
    enqueue_side(book, event_type, exchange_timestamp_us, local_timestamp_us, PassiveQuoteSide::Bid);
    enqueue_side(book, event_type, exchange_timestamp_us, local_timestamp_us, PassiveQuoteSide::Ask);
}

void EventDatasetExporter::enqueue_side(
    const book::OrderBook& book,
    const char* event_type,
    std::uint64_t exchange_timestamp_us,
    std::uint64_t local_timestamp_us,
    PassiveQuoteSide side) {
    const auto features = calculate_instant_book_features(book, weighted_obi_lambda_);
    const double direction = side == PassiveQuoteSide::Bid ? 1.0 : -1.0;
    const auto& level = side == PassiveQuoteSide::Bid ? features.bids[0] : features.asks[0];
    const auto queue_ahead = level.qty_lots;
    std::ostringstream out;
    out << std::setprecision(12)
        << "event-selective-maker-v1," << date_ << ',' << opportunity_id_ << ',' << event_type
        << ',' << exchange_timestamp_us << ',' << local_timestamp_us << ',' << local_timestamp_us
        << ',' << segment_id_ << ",1," << side_name(side) << ','
        << (side == PassiveQuoteSide::Bid ? 1 : -1) << ','
        << price_increment_.from_steps(level.price_ticks) << ','
        << qty_increment_.from_steps(quote_qty_lots_) << ','
        << local_timestamp_us + quote_lifetime_us_ << ','
        << price_increment_.from_steps(features.bids[0].price_ticks) << ','
        << qty_increment_.from_steps(features.bids[0].qty_lots) << ','
        << price_increment_.from_steps(features.asks[0].price_ticks) << ','
        << qty_increment_.from_steps(features.asks[0].qty_lots) << ','
        << direction * *features.obi_l1 << ','
        << direction * *features.obi_l5 << ','
        << direction * *features.obi_l10 << ','
        << direction * *features.weighted_obi_l10 << ','
        << direction * *features.weighted_mid_minus_mid_ticks << ','
        << features.asks[0].price_ticks - features.bids[0].price_ticks << ','
        << features.bids[0].qty_lots << ',' << features.asks[0].qty_lots << ','
        << features.total_bid_depth_l5 << ',' << features.total_ask_depth_l5 << ','
        << features.total_bid_depth_l10 << ',' << features.total_ask_depth_l10 << ','
        << queue_ahead << ",1";

    for (std::size_t index = 0; index < windows_.size(); ++index) {
        const auto& sum = windows_[index].sum;
        const auto window_seconds = static_cast<double>(windows_us_[index]) / 1'000'000.0;
        const auto average_size = sum.trade_count == 0
            ? 0.0
            : static_cast<double>(sum.total_trade_qty_lots) / static_cast<double>(sum.trade_count);
        const auto streak = last_trade_local_time_us_ != 0 &&
                last_trade_local_time_us_ + windows_us_[index] >= local_timestamp_us
            ? direction * static_cast<double>(last_trade_streak_)
            : 0.0;
        out << ',' << direction * sum.delta_obi
            << ',' << direction * static_cast<double>(sum.ofi_lots)
            << ',' << direction * static_cast<double>(sum.add_imbalance_lots)
            << ',' << direction * static_cast<double>(sum.cancel_imbalance_lots)
            << ',' << direction * static_cast<double>(sum.depletion_imbalance_lots)
            << ',' << direction * static_cast<double>(sum.replenishment_imbalance_lots)
            << ',' << sum.bbo_changes << ',' << sum.depth_changes
            << ',' << direction * static_cast<double>(sum.trade_qty_imbalance_lots)
            << ',' << direction * static_cast<double>(sum.trade_count_imbalance)
            << ',' << static_cast<double>(sum.trade_count) / window_seconds
            << ',' << average_size << ',' << streak;
    }
    const auto& hundred = windows_[2].sum;
    out << ',' << elapsed_ms(local_timestamp_us, last_trade_local_time_us_)
        << ',' << elapsed_ms(local_timestamp_us, last_book_local_time_us_)
        << ',' << elapsed_ms(local_timestamp_us, last_bbo_change_local_time_us_)
        << ',' << elapsed_ms(local_timestamp_us, last_mid_change_local_time_us_)
        << ',' << static_cast<double>(hundred.event_count) / 0.1
        << ',' << hundred.mid_abs_change_ticks
        << ',' << windows_[4].sum.mid_abs_change_ticks;

    pending_.push_back(PendingQuote{
        out.str(), side, level.price_ticks, quote_qty_lots_, queue_ahead,
        local_timestamp_us, local_timestamp_us + quote_lifetime_us_});
    ++summary_.candidate_quotes;
}

void EventDatasetExporter::resolve_until(std::uint64_t local_timestamp_us) {
    while (!pending_.empty() && pending_.front().expiry_local_time_us <= local_timestamp_us) {
        auto pending = std::move(pending_.front());
        pending_.pop_front();
        const auto end = pending.expiry_local_time_us;
        emit(std::move(pending), end, false);
    }
}

void EventDatasetExporter::resolve_all(
    std::uint64_t cutoff_local_timestamp_us,
    bool snapshot_invalidated) {
    while (!pending_.empty()) {
        auto pending = std::move(pending_.front());
        pending_.pop_front();
        const auto end = std::min(pending.expiry_local_time_us, cutoff_local_timestamp_us);
        const bool invalidated =
            snapshot_invalidated && cutoff_local_timestamp_us < pending.expiry_local_time_us;
        emit(
            std::move(pending),
            end,
            invalidated);
    }
}

void EventDatasetExporter::emit(
    PendingQuote&& pending,
    std::uint64_t end_local_timestamp_us,
    bool snapshot_invalidated) {
    const auto outcome = trade_index_.evaluate(
        pending.side,
        pending.quote_price_ticks,
        pending.quote_qty_lots,
        pending.queue_ahead_lots,
        pending.placement_local_time_us,
        end_local_timestamp_us);
    if (outcome.state == PassiveFillState::Full) {
        ++summary_.full_fills;
    } else if (outcome.state == PassiveFillState::Partial) {
        ++summary_.partial_fills;
    } else {
        ++summary_.unfilled;
    }
    summary_.snapshot_invalidations += snapshot_invalidated;
    std::ostringstream out;
    out << pending.prefix << ',' << passive_fill_state_name(outcome.state) << ','
        << optional_u64(outcome.first_fill_exchange_time_us) << ','
        << optional_u64(outcome.first_fill_local_time_us) << ','
        << optional_u64(outcome.full_fill_exchange_time_us) << ','
        << optional_u64(outcome.full_fill_local_time_us) << ','
        << qty_increment_.from_steps(outcome.filled_lots) << ','
        << outcome.queue_consumed_lots << ',' << (snapshot_invalidated ? 1 : 0) << '\n';
    writer_->append(out.str());
    ++summary_.rows;
}

void EventDatasetExporter::finish(const book::OrderBook& book) {
    while (next_trade_ < trades_.trades.size()) {
        process_trade_group(book);
    }
    resolve_all(std::numeric_limits<std::uint64_t>::max(), false);
    writer_->close();
    closed_ = true;
}

}  // namespace crypto::research
