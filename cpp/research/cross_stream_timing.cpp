#include "research/cross_stream_timing.hpp"

#include <array>
#include <charconv>
#include <stdexcept>

#include "research/compressed_text_writer.hpp"

namespace crypto::research {
namespace {

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

void append_ms(std::string& out, std::uint64_t from_ns, std::uint64_t to_ns) {
    if (to_ns == 0 || to_ns < from_ns) {
        return;
    }
    std::array<char, 32> buffer{};
    const auto result = std::to_chars(
        buffer.data(), buffer.data() + buffer.size(),
        static_cast<double>(to_ns - from_ns) / 1e6, std::chars_format::general, 9);
    if (result.ec != std::errc{}) {
        throw std::runtime_error("cannot format latency");
    }
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

}  // namespace

std::string CrossStreamTimingObserver::csv_header() {
    return "receive_ns,file_index,trade_exchange_time_ms,aggressor,passive_side,category"
           ",trade_price_ticks,trade_qty_lots,quote_px_ticks,quote_qty_lots,mid_ticks"
           ",spread_ticks,depth_qty_reaction_latency_ms,quote_removal_latency_ms"
           ",best_price_change_latency_ms,mid_adverse_move_latency_ms"
           ",first_mid_move_latency_ms,first_mid_move_is_adverse"
           ",qty_reaction_exchange_lag_ms,quote_qty_after_reaction_lots"
           ",depth_events_until_resolution\n";
}

CrossStreamTimingObserver::CrossStreamTimingObserver(
    const std::filesystem::path& output_path,
    std::uint32_t file_index,
    std::uint64_t observation_window_ms)
    : writer_(std::make_unique<CompressedTextWriter>(output_path)),
      file_index_(file_index),
      window_ns_(observation_window_ms * 1'000'000ULL) {
    if (observation_window_ms == 0) {
        throw std::invalid_argument("timing observation window must be positive");
    }
    writer_->append(csv_header());
    row_.reserve(512);
}

CrossStreamTimingObserver::~CrossStreamTimingObserver() {
    try {
        if (!closed_) {
            writer_->close();
        }
    } catch (...) {
    }
}

void CrossStreamTimingObserver::before_record(
    std::uint64_t,
    std::int64_t,
    const book::OrderBook&,
    bool) {}

void CrossStreamTimingObserver::on_connection(std::uint64_t, std::string_view stream, bool open) {
    // A depth reconnect abandons the book the pending events were measured against.
    if (!open && stream.find("depth") != std::string_view::npos) {
        drop_pending();
    }
}

void CrossStreamTimingObserver::before_depth_event(
    const binance::DepthEvent&,
    const book::OrderBook&) {}

void CrossStreamTimingObserver::after_depth_event(
    std::uint64_t local_ns,
    std::int64_t exchange_ms,
    const book::OrderBook& book,
    bool applied,
    bool live) {
    if (!applied || !live) {
        if (!applied) {
            drop_pending();
        }
        return;
    }
    observe(local_ns, exchange_ms, book);
    flush(local_ns, false);
}

void CrossStreamTimingObserver::after_snapshot(
    std::uint64_t,
    const book::OrderBook&,
    bool) {
    drop_pending();
}

void CrossStreamTimingObserver::on_trade(
    std::uint64_t local_ns,
    const binance::AggTradeEvent& trade,
    const book::OrderBook& book,
    bool live) {
    ++summary_.trades_seen;
    if (!live || book.bids().empty() || book.asks().empty()) {
        return;
    }
    Pending pending;
    pending.receive_ns = local_ns;
    pending.trade_exchange_ms = trade.transaction_time_ms != 0 ? trade.transaction_time_ms
                                                               : trade.event_time_ms;
    pending.aggressive_buy = trade.is_aggressive_buy();
    pending.trade_price_ticks = trade.price_ticks;
    pending.trade_qty_lots = trade.qty_lots;
    const auto best_bid = book.bids().begin()->first;
    const auto best_ask = book.asks().begin()->first;
    // The passive side is the one the aggressor consumes.
    pending.quote_px_ticks = pending.aggressive_buy ? best_ask : best_bid;
    pending.quote_qty_lots = pending.aggressive_buy
        ? book.asks().begin()->second
        : book.bids().begin()->second;
    pending.mid_x2 = best_bid + best_ask;
    pending.spread_ticks = best_ask - best_bid;
    const auto beyond = pending.aggressive_buy
        ? pending.trade_price_ticks > pending.quote_px_ticks
        : pending.trade_price_ticks < pending.quote_px_ticks;
    const auto behind = pending.aggressive_buy
        ? pending.trade_price_ticks < pending.quote_px_ticks
        : pending.trade_price_ticks > pending.quote_px_ticks;
    if (beyond) {
        pending.category = "through_quote";
        ++summary_.through_quote;
    } else if (behind) {
        // The print is on the far side of the reconstructed touch: the depth stream has not
        // caught up with a book the aggressor already traded against.
        pending.category = "outside_quote";
        ++summary_.outside_quote;
    } else {
        pending.category = "at_quote";
        ++summary_.at_quote;
    }
    ++summary_.trades_recorded;
    pending_.push_back(pending);
}

void CrossStreamTimingObserver::after_record(
    std::uint64_t local_ns,
    std::int64_t,
    const book::OrderBook&,
    bool) {
    flush(local_ns, false);
}

void CrossStreamTimingObserver::observe(
    std::uint64_t local_ns,
    std::int64_t exchange_ms,
    const book::OrderBook& book) {
    if (pending_.empty() || book.bids().empty() || book.asks().empty()) {
        return;
    }
    const auto best_bid = book.bids().begin()->first;
    const auto best_ask = book.asks().begin()->first;
    const auto mid_x2 = best_bid + best_ask;
    for (auto& pending : pending_) {
        if (pending.receive_ns > local_ns) {
            continue;
        }
        ++pending.depth_events_seen;
        const auto best = pending.aggressive_buy ? best_ask : best_bid;
        if (pending.qty_reaction_ns == 0 || pending.quote_removal_ns == 0) {
            const auto remaining = pending.aggressive_buy
                ? quantity_at(book.asks(), pending.quote_px_ticks)
                : quantity_at(book.bids(), pending.quote_px_ticks);
            if (pending.qty_reaction_ns == 0 && remaining < pending.quote_qty_lots) {
                pending.qty_reaction_ns = local_ns;
                pending.qty_reaction_exchange_ms = exchange_ms;
                pending.qty_after_reaction_lots = remaining;
            }
            if (pending.quote_removal_ns == 0 && remaining == 0) {
                pending.quote_removal_ns = local_ns;
            }
        }
        if (pending.best_change_ns == 0 && best != pending.quote_px_ticks) {
            pending.best_change_ns = local_ns;
        }
        if (mid_x2 != pending.mid_x2) {
            if (pending.first_mid_move_ns == 0) {
                pending.first_mid_move_ns = local_ns;
                // Adverse is measured for the passive side the aggressor hit: a resting bid is
                // hurt by a falling mid, a resting ask by a rising one.
                const bool adverse = pending.aggressive_buy ? mid_x2 > pending.mid_x2
                                                            : mid_x2 < pending.mid_x2;
                pending.first_mid_move_dir = adverse ? std::int8_t{1} : std::int8_t{-1};
            }
            if (pending.mid_adverse_ns == 0) {
                const bool adverse = pending.aggressive_buy ? mid_x2 > pending.mid_x2
                                                            : mid_x2 < pending.mid_x2;
                if (adverse) {
                    pending.mid_adverse_ns = local_ns;
                }
            }
        }
    }
}

void CrossStreamTimingObserver::flush(std::uint64_t local_ns, bool force) {
    while (!pending_.empty()) {
        const auto& front = pending_.front();
        const bool resolved = front.qty_reaction_ns != 0 && front.quote_removal_ns != 0 &&
                              front.best_change_ns != 0 && front.mid_adverse_ns != 0 &&
                              front.first_mid_move_ns != 0;
        const bool expired = force || (local_ns >= front.receive_ns + window_ns_);
        if (!resolved && !expired) {
            break;
        }
        emit(front);
        pending_.pop_front();
    }
}

void CrossStreamTimingObserver::drop_pending() {
    // A desync destroys the comparison baseline; these events are reported as discarded rather
    // than emitted with latencies measured against a book that no longer exists.
    summary_.discarded_on_desync += pending_.size();
    pending_.clear();
}

void CrossStreamTimingObserver::emit(const Pending& pending) {
    summary_.unresolved_qty_reaction += pending.qty_reaction_ns == 0;
    summary_.unresolved_quote_removal += pending.quote_removal_ns == 0;
    summary_.unresolved_best_change += pending.best_change_ns == 0;
    summary_.unresolved_mid_adverse += pending.mid_adverse_ns == 0;

    auto& out = row_;
    out.clear();
    append_u64(out, pending.receive_ns);
    out.push_back(',');
    append_u64(out, file_index_);
    out.push_back(',');
    append_i64(out, pending.trade_exchange_ms);
    out.push_back(',');
    out.append(pending.aggressive_buy ? "buy" : "sell");
    out.push_back(',');
    out.append(pending.aggressive_buy ? "ask" : "bid");
    out.push_back(',');
    out.append(pending.category);
    out.push_back(',');
    append_i64(out, pending.trade_price_ticks);
    out.push_back(',');
    append_i64(out, pending.trade_qty_lots);
    out.push_back(',');
    append_i64(out, pending.quote_px_ticks);
    out.push_back(',');
    append_i64(out, pending.quote_qty_lots);
    out.push_back(',');
    append_i64(out, pending.mid_x2);
    out.push_back(',');
    append_i64(out, pending.spread_ticks);
    for (const auto stamp : {pending.qty_reaction_ns, pending.quote_removal_ns,
                             pending.best_change_ns, pending.mid_adverse_ns,
                             pending.first_mid_move_ns}) {
        out.push_back(',');
        append_ms(out, pending.receive_ns, stamp);
    }
    out.push_back(',');
    if (pending.first_mid_move_dir != 0) {
        append_i64(out, pending.first_mid_move_dir > 0 ? 1 : 0);
    }
    out.push_back(',');
    if (pending.qty_reaction_ns != 0 && pending.trade_exchange_ms != 0 &&
        pending.qty_reaction_exchange_ms != 0) {
        // Exchange-clock version of the same lag. Reported alongside, never substituted: the
        // two streams stamp their own events and the clocks are not assumed comparable.
        append_i64(out, pending.qty_reaction_exchange_ms - pending.trade_exchange_ms);
    }
    out.push_back(',');
    if (pending.qty_after_reaction_lots >= 0) {
        append_i64(out, pending.qty_after_reaction_lots);
    }
    out.push_back(',');
    append_u64(out, pending.depth_events_seen);
    out.push_back('\n');
    writer_->append(out);
}

void CrossStreamTimingObserver::finish(const book::OrderBook&) {
    flush(0, true);
    writer_->close();
    closed_ = true;
}

}  // namespace crypto::research
