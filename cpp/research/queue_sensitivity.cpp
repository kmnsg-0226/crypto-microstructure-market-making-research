#include "research/queue_sensitivity.hpp"

#include <algorithm>
#include <charconv>
#include <sstream>
#include <stdexcept>

#include "research/compressed_text_writer.hpp"

namespace crypto::research {
namespace {

constexpr std::uint8_t kBid = 0;
constexpr std::uint8_t kAsk = 1;

void append_u64(std::string& out, std::uint64_t value) {
    std::array<char, 24> buffer{};
    const auto result = std::to_chars(buffer.data(), buffer.data() + buffer.size(), value);
    out.append(buffer.data(), result.ptr);
}

void append_i64(std::string& out, std::int64_t value) {
    std::array<char, 24> buffer{};
    const auto result = std::to_chars(buffer.data(), buffer.data() + buffer.size(), value);
    out.append(buffer.data(), result.ptr);
}

std::int64_t quantity_at(const book::OrderBook::BidMap& side, std::int64_t price_ticks) {
    const auto found = side.find(price_ticks);
    return found == side.end() ? 0 : found->second;
}

std::int64_t quantity_at(const book::OrderBook::AskMap& side, std::int64_t price_ticks) {
    const auto found = side.find(price_ticks);
    return found == side.end() ? 0 : found->second;
}

// Integer scaling keeps every cell of the grid bit-reproducible; truncation is the conservative
// direction for both the initial queue and the removal credit.
std::int64_t scale_percent(std::int64_t value, std::int64_t percent) {
    return (value * percent) / 100;
}

}  // namespace

std::string QueueSensitivityObserver::fills_header() {
    return "placement_ns,file_index,segment_id,side,alpha_pct,beta_pct,quote_px_ticks"
           ",displayed_qty_lots,queue_ahead_lots,mid_x2_at_placement,spread_ticks"
           ",observed_end_ns,fill_ns,mechanism,filled_lots\n";
}

std::string QueueSensitivityObserver::mid_header() {
    return "ns,file_index,segment_id,mid_x2\n";
}

QueueSensitivityObserver::QueueSensitivityObserver(
    const std::filesystem::path& fills_path,
    const std::filesystem::path& mid_path,
    QueueSensitivityConfig config)
    : config_(config),
      fills_(std::make_unique<CompressedTextWriter>(fills_path)),
      mid_(std::make_unique<CompressedTextWriter>(mid_path)) {
    if (config_.placement_interval_ms == 0 || config_.observation_window_ms == 0 ||
        config_.order_lots <= 0) {
        throw std::invalid_argument("invalid queue sensitivity configuration");
    }
    placement_ns_ = config_.placement_interval_ms * 1'000'000ULL;
    window_ns_ = config_.observation_window_ms * 1'000'000ULL;
    fills_->append(fills_header());
    mid_->append(mid_header());
    row_.reserve(256);
}

QueueSensitivityObserver::~QueueSensitivityObserver() {
    try {
        if (!closed_) {
            fills_->close();
            mid_->close();
        }
    } catch (...) {
    }
}

void QueueSensitivityObserver::open_segment(std::uint64_t local_ns) {
    in_segment_ = true;
    ++segment_id_;
    ++summary_.segments;
    segment_start_ns_ = local_ns;
    pending_.clear();
    have_last_mid_ = false;
    have_last_best_ = false;
    const auto remainder = local_ns % placement_ns_;
    next_placement_ns_ = remainder == 0 ? local_ns : local_ns + (placement_ns_ - remainder);
}

void QueueSensitivityObserver::close_segment(std::uint64_t local_ns) {
    if (!in_segment_) {
        return;
    }
    // Every resting order is abandoned at the segment edge and emitted with the observation
    // window it actually got, so nothing is ever resolved across a boundary.
    for (const auto& order : pending_) {
        emit(order, std::min(order.placement_ns + window_ns_, local_ns));
    }
    pending_.clear();
    in_segment_ = false;
}

void QueueSensitivityObserver::record_mid(std::uint64_t local_ns, std::int64_t mid_x2) {
    if (have_last_mid_ && mid_x2 == last_mid_x2_) {
        return;
    }
    last_mid_x2_ = mid_x2;
    have_last_mid_ = true;
    auto& out = row_;
    out.clear();
    append_u64(out, local_ns);
    out.push_back(',');
    append_u64(out, config_.file_index);
    out.push_back(',');
    append_u64(out, segment_id_);
    out.push_back(',');
    append_i64(out, mid_x2);
    out.push_back('\n');
    mid_->append(out);
    ++summary_.mid_path_points;
}

void QueueSensitivityObserver::place_due(
    std::uint64_t local_ns,
    bool inclusive,
    const book::OrderBook& book) {
    while (next_placement_ns_ < local_ns || (inclusive && next_placement_ns_ == local_ns)) {
        place(next_placement_ns_, book);
        next_placement_ns_ += placement_ns_;
    }
}

void QueueSensitivityObserver::place_side(
    std::uint8_t side,
    std::int64_t price_ticks,
    std::int64_t displayed_qty,
    std::int64_t mid_x2,
    std::int64_t spread_ticks,
    std::uint64_t local_ns) {
    Order order;
    order.placement_ns = local_ns;
    order.segment_id = segment_id_;
    order.side = side;
    order.quote_px_ticks = price_ticks;
    order.displayed_qty_lots = displayed_qty;
    order.mid_x2_at_placement = mid_x2;
    order.spread_ticks = spread_ticks;
    for (std::size_t i = 0; i < QueueSensitivityConfig::kSteps; ++i) {
        for (std::size_t j = 0; j < QueueSensitivityConfig::kSteps; ++j) {
            const auto cell = i * QueueSensitivityConfig::kSteps + j;
            // alpha is the only thing that touches the initial queue ahead.
            order.remaining_ahead[cell] =
                scale_percent(displayed_qty, QueueSensitivityConfig::kPercents[i]);
        }
    }
    pending_.push_back(order);
    ++summary_.placements;
}

void QueueSensitivityObserver::place(std::uint64_t local_ns, const book::OrderBook& book) {
    if (!book.is_valid() || book.bids().empty() || book.asks().empty()) {
        return;
    }
    const auto best_bid = book.bids().begin()->first;
    const auto best_ask = book.asks().begin()->first;
    const auto mid_x2 = best_bid + best_ask;
    const auto spread = best_ask - best_bid;
    place_side(kBid, best_bid, book.bids().begin()->second, mid_x2, spread, local_ns);
    place_side(kAsk, best_ask, book.asks().begin()->second, mid_x2, spread, local_ns);
}

void QueueSensitivityObserver::place_on_level_birth(
    std::uint64_t local_ns,
    const book::OrderBook& book) {
    if (!book.is_valid() || book.bids().empty() || book.asks().empty()) {
        return;
    }
    const auto best_bid = book.bids().begin()->first;
    const auto best_ask = book.asks().begin()->first;
    const auto mid_x2 = best_bid + best_ask;
    const auto spread = best_ask - best_bid;
    const std::array<std::int64_t, 2> best{best_bid, best_ask};
    for (std::uint8_t side = kBid; side <= kAsk; ++side) {
        if (have_last_best_ && last_best_[side] == best[side]) {
            continue;
        }
        // The order arrives immediately after the event that made this price best, so every lot
        // already displayed there was resting before it and counts as ahead.
        const auto qty = side == kBid ? book.bids().begin()->second
                                      : book.asks().begin()->second;
        place_side(side, best[side], qty, mid_x2, spread, local_ns);
    }
    last_best_ = best;
    have_last_best_ = true;
}

void QueueSensitivityObserver::retire(std::uint64_t local_ns, bool force) {
    while (!pending_.empty()) {
        const auto& front = pending_.front();
        if (!force && local_ns < front.placement_ns + window_ns_) {
            break;
        }
        emit(front, front.placement_ns + window_ns_);
        pending_.pop_front();
    }
}

void QueueSensitivityObserver::emit(const Order& order, std::uint64_t observed_end_ns) {
    for (std::size_t i = 0; i < QueueSensitivityConfig::kSteps; ++i) {
        for (std::size_t j = 0; j < QueueSensitivityConfig::kSteps; ++j) {
            const auto cell = i * QueueSensitivityConfig::kSteps + j;
            auto& out = row_;
            out.clear();
            append_u64(out, order.placement_ns);
            out.push_back(',');
            append_u64(out, config_.file_index);
            out.push_back(',');
            append_u64(out, order.segment_id);
            out.push_back(',');
            append_u64(out, order.side);
            out.push_back(',');
            append_i64(out, QueueSensitivityConfig::kPercents[i]);
            out.push_back(',');
            append_i64(out, QueueSensitivityConfig::kPercents[j]);
            out.push_back(',');
            append_i64(out, order.quote_px_ticks);
            out.push_back(',');
            append_i64(out, order.displayed_qty_lots);
            out.push_back(',');
            append_i64(
                out,
                scale_percent(order.displayed_qty_lots, QueueSensitivityConfig::kPercents[i]));
            out.push_back(',');
            append_i64(out, order.mid_x2_at_placement);
            out.push_back(',');
            append_i64(out, order.spread_ticks);
            out.push_back(',');
            append_u64(out, observed_end_ns);
            out.push_back(',');
            append_u64(out, order.fill_ns[cell]);
            out.push_back(',');
            append_u64(out, static_cast<std::uint64_t>(order.mechanism[cell]));
            out.push_back(',');
            append_i64(out, order.filled[cell]);
            out.push_back('\n');
            fills_->append(out);
            ++summary_.emitted_rows;
        }
    }
}

void QueueSensitivityObserver::before_record(
    std::uint64_t local_ns,
    std::int64_t,
    const book::OrderBook& book,
    bool live) {
    book_live_ = live;
    if (!in_segment_ || !usable()) {
        return;
    }
    retire(local_ns, false);
    if (config_.mode == PlacementMode::Grid) {
        place_due(local_ns, false, book);
    }
}

void QueueSensitivityObserver::on_connection(std::uint64_t, std::string_view stream, bool open) {
    if (stream.find("aggTrade") != std::string_view::npos) {
        trade_stream_up_ = open;
    }
}

void QueueSensitivityObserver::before_depth_event(
    const binance::DepthEvent&,
    const book::OrderBook& book) {
    if (!in_segment_ || !book.is_valid()) {
        return;
    }
    for (auto& order : pending_) {
        order.pre_event_qty = order.side == kBid
            ? quantity_at(book.bids(), order.quote_px_ticks)
            : quantity_at(book.asks(), order.quote_px_ticks);
    }
}

void QueueSensitivityObserver::after_depth_event(
    std::uint64_t local_ns,
    std::int64_t,
    const book::OrderBook& book,
    bool applied,
    bool live) {
    book_live_ = live;
    if (!applied || !in_segment_ || !book.is_valid()) {
        for (auto& order : pending_) {
            order.pre_event_qty = -1;
        }
        return;
    }
    ++summary_.depth_events;
    for (auto& order : pending_) {
        if (order.pre_event_qty < 0) {
            continue;
        }
        const auto after = order.side == kBid
            ? quantity_at(book.bids(), order.quote_px_ticks)
            : quantity_at(book.asks(), order.quote_px_ticks);
        const auto delta = after - order.pre_event_qty;
        order.pre_event_qty = -1;
        if (delta > 0) {
            // Later additions sit behind the hypothetical order and can never advance it.
            order.added_behind += delta;
            continue;
        }
        if (delta == 0) {
            continue;
        }
        auto removal = -delta;
        // Charge the removal against quantity known to have joined behind us first, so beta can
        // never credit something that was never ahead of the order.
        const auto from_behind = std::min(order.added_behind, removal);
        order.added_behind -= from_behind;
        removal -= from_behind;
        summary_.behind_attributed_removal_lots += static_cast<std::uint64_t>(from_behind);
        if (removal == 0) {
            continue;
        }
        // Aggressive prints at this price already advanced the queue when they arrived; the
        // displayed reduction they cause must not be counted a second time.
        const auto explained = std::min(order.trade_pool, removal);
        order.trade_pool -= explained;
        removal -= explained;
        summary_.trade_explained_removal_lots += static_cast<std::uint64_t>(explained);
        if (removal == 0) {
            continue;
        }
        summary_.unexplained_removal_lots += static_cast<std::uint64_t>(removal);
        for (std::size_t i = 0; i < QueueSensitivityConfig::kSteps; ++i) {
            for (std::size_t j = 0; j < QueueSensitivityConfig::kSteps; ++j) {
                const auto cell = i * QueueSensitivityConfig::kSteps + j;
                if (order.remaining_ahead[cell] == 0) {
                    // The queue in front of the order is already gone; anything removed now was
                    // behind it, so beta must not credit it.
                    continue;
                }
                const auto credit =
                    scale_percent(removal, QueueSensitivityConfig::kPercents[j]);
                order.remaining_ahead[cell] =
                    std::max<std::int64_t>(0, order.remaining_ahead[cell] - credit);
            }
        }
    }
    if (!book.bids().empty() && !book.asks().empty()) {
        record_mid(local_ns, book.bids().begin()->first + book.asks().begin()->first);
    }
    if (config_.mode == PlacementMode::LevelBirth && usable()) {
        place_on_level_birth(local_ns, book);
    }
}

void QueueSensitivityObserver::after_snapshot(
    std::uint64_t local_ns,
    const book::OrderBook&,
    bool live) {
    book_live_ = live;
    if (in_segment_) {
        close_segment(local_ns);
    }
}

void QueueSensitivityObserver::on_trade(
    std::uint64_t local_ns,
    const binance::AggTradeEvent& trade,
    const book::OrderBook&,
    bool live) {
    book_live_ = live;
    if (!in_segment_) {
        return;
    }
    ++summary_.trade_events;
    const std::uint8_t hit = trade.is_aggressive_buy() ? kAsk : kBid;
    for (auto& order : pending_) {
        // A print stamped at the placement instant informed the decision and must never fill it.
        if (order.side != hit || order.placement_ns >= local_ns) {
            continue;
        }
        const bool through = order.side == kBid
            ? trade.price_ticks < order.quote_px_ticks
            : trade.price_ticks > order.quote_px_ticks;
        if (through) {
            for (std::size_t cell = 0; cell < QueueSensitivityConfig::kCells; ++cell) {
                if (order.mechanism[cell] != Mechanism::None) {
                    continue;
                }
                // Everything resting at the quote traded away before this print could occur.
                order.remaining_ahead[cell] = 0;
                order.filled[cell] = config_.order_lots;
                order.fill_ns[cell] = local_ns;
                order.mechanism[cell] = Mechanism::TradeThrough;
            }
            continue;
        }
        if (trade.price_ticks != order.quote_px_ticks) {
            continue;
        }
        order.trade_pool += trade.qty_lots;
        for (std::size_t cell = 0; cell < QueueSensitivityConfig::kCells; ++cell) {
            if (order.mechanism[cell] != Mechanism::None) {
                continue;
            }
            const auto consumed = std::min(order.remaining_ahead[cell], trade.qty_lots);
            order.remaining_ahead[cell] -= consumed;
            const auto overflow = trade.qty_lots - consumed;
            if (overflow <= 0) {
                continue;
            }
            order.filled[cell] =
                std::min(config_.order_lots, order.filled[cell] + overflow);
            if (order.filled[cell] >= config_.order_lots) {
                order.fill_ns[cell] = local_ns;
                order.mechanism[cell] = Mechanism::AtQuote;
            }
        }
    }
}

void QueueSensitivityObserver::after_record(
    std::uint64_t local_ns,
    std::int64_t,
    const book::OrderBook& book,
    bool live) {
    book_live_ = live;
    last_record_ns_ = local_ns;
    if (in_segment_) {
        if (usable()) {
            retire(local_ns, false);
            if (config_.mode == PlacementMode::Grid) {
                place_due(local_ns, true, book);
            }
        } else {
            close_segment(local_ns);
        }
        return;
    }
    if (usable() && book.is_valid()) {
        open_segment(local_ns);
        if (!book.bids().empty() && !book.asks().empty()) {
            record_mid(local_ns, book.bids().begin()->first + book.asks().begin()->first);
        }
        if (config_.mode == PlacementMode::Grid) {
            place_due(local_ns, true, book);
        } else {
            place_on_level_birth(local_ns, book);
        }
    }
}

void QueueSensitivityObserver::finish(const book::OrderBook&) {
    close_segment(std::max(last_record_ns_, segment_start_ns_));
    fills_->close();
    mid_->close();
    closed_ = true;
}

}  // namespace crypto::research
