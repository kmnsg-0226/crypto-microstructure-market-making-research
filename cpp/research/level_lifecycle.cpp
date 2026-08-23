#include "research/level_lifecycle.hpp"

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

void append_ms(std::string& out, std::uint64_t from_ns, std::uint64_t to_ns) {
    if (from_ns == 0 || to_ns < from_ns) {
        return;
    }
    std::array<char, 32> buffer{};
    const auto result = std::to_chars(
        buffer.data(), buffer.data() + buffer.size(),
        static_cast<double>(to_ns - from_ns) / 1e6, std::chars_format::general, 9);
    if (result.ec != std::errc{}) {
        throw std::runtime_error("cannot format elapsed time");
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

std::string LevelLifecycleObserver::episodes_header() {
    return "level_episode_id,file_index,segment_id,side,price_ticks,start_ns,end_ns"
           ",duration_ms,initial_qty,final_qty,max_qty,min_qty,cum_add,cum_remove"
           ",cum_explained_remove,cum_unexplained_remove,cum_trade_at_quote,cum_trade_through"
           ",cum_replenish,add_events,remove_events,replenish_events,prints_at_quote"
           ",prints_through,close_reason\n";
}

std::string LevelLifecycleObserver::grid_header() {
    std::ostringstream out;
    out << "timestamp_ns,file_index,segment_id,side,level_episode_id,quote_price_ticks"
           ",current_qty,initial_qty,max_qty,min_qty,level_age_ms,time_since_qty_change_ms"
           ",time_since_add_ms,time_since_remove_ms,time_since_print_ms"
           ",time_since_replenish_ms,cum_add,cum_remove,cum_explained_remove"
           ",cum_unexplained_remove,cum_trade_at_quote,cum_trade_through,cum_replenish"
           ",add_events,remove_events,replenish_events,prints_at_quote,prints_through";
    for (const auto horizon : LevelLifecycleConfig::kHorizonMs) {
        out << ",trade_through_within_" << horizon << "ms"
            << ",trade_at_quote_volume_" << horizon << "ms"
            << ",trade_through_volume_" << horizon << "ms";
    }
    out << '\n';
    return out.str();
}

LevelLifecycleObserver::LevelLifecycleObserver(
    const std::filesystem::path& episodes_path,
    const std::filesystem::path& grid_path,
    LevelLifecycleConfig config)
    : config_(config),
      episodes_(std::make_unique<CompressedTextWriter>(episodes_path)),
      grid_(std::make_unique<CompressedTextWriter>(grid_path)) {
    if (config_.grid_ms == 0) {
        throw std::invalid_argument("level lifecycle grid must be positive");
    }
    grid_ns_ = config_.grid_ms * 1'000'000ULL;
    for (std::size_t i = 0; i < LevelLifecycleConfig::kHorizons; ++i) {
        horizon_ns_[i] = LevelLifecycleConfig::kHorizonMs[i] * 1'000'000ULL;
    }
    widest_ns_ = horizon_ns_.back();
    episodes_->append(episodes_header());
    grid_->append(grid_header());
    row_.reserve(1024);
}

LevelLifecycleObserver::~LevelLifecycleObserver() {
    try {
        if (!closed_) {
            episodes_->close();
            grid_->close();
        }
    } catch (...) {
    }
}

void LevelLifecycleObserver::start_episode(
    std::uint8_t side,
    std::int64_t price_ticks,
    std::int64_t qty,
    std::uint64_t local_ns) {
    auto& episode = levels_[side];
    episode = Episode{};
    episode.active = true;
    episode.id = ++next_episode_id_;
    episode.segment_id = segment_id_;
    episode.side = side;
    episode.price_ticks = price_ticks;
    episode.start_ns = local_ns;
    episode.initial_qty = qty;
    episode.current_qty = qty;
    episode.max_qty = qty;
    episode.min_qty = qty;
    ++summary_.episodes;
}

void LevelLifecycleObserver::end_episode(
    std::uint8_t side,
    std::uint64_t local_ns,
    const char* reason) {
    auto& episode = levels_[side];
    if (!episode.active) {
        return;
    }
    auto& out = row_;
    out.clear();
    append_u64(out, episode.id);
    out.push_back(',');
    append_u64(out, config_.file_index);
    out.push_back(',');
    append_u64(out, episode.segment_id);
    out.push_back(',');
    append_u64(out, episode.side);
    out.push_back(',');
    append_i64(out, episode.price_ticks);
    out.push_back(',');
    append_u64(out, episode.start_ns);
    out.push_back(',');
    append_u64(out, local_ns);
    out.push_back(',');
    append_ms(out, episode.start_ns, local_ns);
    for (const auto value : {episode.initial_qty, episode.current_qty, episode.max_qty,
                             episode.min_qty, episode.cum_add, episode.cum_remove,
                             episode.cum_explained_remove, episode.cum_unexplained_remove,
                             episode.cum_trade_at_quote, episode.cum_trade_through,
                             episode.cum_replenish}) {
        out.push_back(',');
        append_i64(out, value);
    }
    for (const auto value : {episode.add_events, episode.remove_events,
                             episode.replenish_events, episode.prints_at_quote,
                             episode.prints_through}) {
        out.push_back(',');
        append_u64(out, value);
    }
    out.push_back(',');
    out.append(reason);
    out.push_back('\n');
    episodes_->append(out);
    episode.active = false;
}

void LevelLifecycleObserver::apply_level_delta(
    Episode& episode,
    std::int64_t delta,
    std::uint64_t local_ns) {
    if (delta == 0) {
        return;
    }
    episode.current_qty += delta;
    episode.max_qty = std::max(episode.max_qty, episode.current_qty);
    episode.min_qty = std::min(episode.min_qty, episode.current_qty);
    episode.last_qty_change_ns = local_ns;
    if (delta > 0) {
        episode.cum_add += delta;
        ++episode.add_events;
        episode.last_add_ns = local_ns;
        // Replenishment is an increase that follows a reduction inside the same episode; an
        // episode that has only ever grown has not replenished anything.
        if (episode.remove_events != 0) {
            episode.cum_replenish += delta;
            ++episode.replenish_events;
            episode.last_replenish_ns = local_ns;
            summary_.replenished_lots += static_cast<std::uint64_t>(delta);
        }
        return;
    }
    const auto removal = -delta;
    ++episode.remove_events;
    episode.cum_remove += removal;
    episode.last_remove_ns = local_ns;
    // Aggressive prints already observed at this level account for the reduction first. Only
    // what is left over is unexplained, and unexplained is not a synonym for cancelled.
    const auto explained = std::min(episode.trade_pool, removal);
    episode.trade_pool -= explained;
    episode.cum_explained_remove += explained;
    episode.cum_unexplained_remove += removal - explained;
    summary_.explained_removal_lots += static_cast<std::uint64_t>(explained);
    summary_.unexplained_removal_lots += static_cast<std::uint64_t>(removal - explained);
}

void LevelLifecycleObserver::open_segment(std::uint64_t local_ns, const book::OrderBook& book) {
    in_segment_ = true;
    ++segment_id_;
    ++summary_.segments;
    segment_start_ns_ = local_ns;
    pending_.clear();
    const auto remainder = local_ns % grid_ns_;
    next_sample_ns_ = remainder == 0 ? local_ns : local_ns + (grid_ns_ - remainder);
    if (!book.bids().empty()) {
        start_episode(kBid, book.bids().begin()->first, book.bids().begin()->second, local_ns);
    }
    if (!book.asks().empty()) {
        start_episode(kAsk, book.asks().begin()->first, book.asks().begin()->second, local_ns);
    }
}

void LevelLifecycleObserver::close_segment(std::uint64_t local_ns, const char* reason) {
    if (!in_segment_) {
        return;
    }
    retire(local_ns, true);
    end_episode(kBid, local_ns, reason);
    end_episode(kAsk, local_ns, reason);
    in_segment_ = false;
}

void LevelLifecycleObserver::advance_grid(
    std::uint64_t local_ns,
    bool inclusive,
    const book::OrderBook& book) {
    while (next_sample_ns_ < local_ns || (inclusive && next_sample_ns_ == local_ns)) {
        emit_grid_row(next_sample_ns_, book);
        next_sample_ns_ += grid_ns_;
    }
}

void LevelLifecycleObserver::emit_grid_row(
    std::uint64_t sample_ns,
    const book::OrderBook& book) {
    if (!book.is_valid()) {
        return;
    }
    for (std::uint8_t side = kBid; side <= kAsk; ++side) {
        const auto& episode = levels_[side];
        if (!episode.active) {
            continue;
        }
        auto& out = row_;
        out.clear();
        append_u64(out, sample_ns);
        out.push_back(',');
        append_u64(out, config_.file_index);
        out.push_back(',');
        append_u64(out, episode.segment_id);
        out.push_back(',');
        append_u64(out, side);
        out.push_back(',');
        append_u64(out, episode.id);
        out.push_back(',');
        append_i64(out, episode.price_ticks);
        for (const auto value : {episode.current_qty, episode.initial_qty, episode.max_qty,
                                 episode.min_qty}) {
            out.push_back(',');
            append_i64(out, value);
        }
        out.push_back(',');
        append_ms(out, episode.start_ns, sample_ns);
        for (const auto stamp : {episode.last_qty_change_ns, episode.last_add_ns,
                                 episode.last_remove_ns, episode.last_print_ns,
                                 episode.last_replenish_ns}) {
            out.push_back(',');
            append_ms(out, stamp, sample_ns);
        }
        for (const auto value : {episode.cum_add, episode.cum_remove,
                                 episode.cum_explained_remove, episode.cum_unexplained_remove,
                                 episode.cum_trade_at_quote, episode.cum_trade_through,
                                 episode.cum_replenish}) {
            out.push_back(',');
            append_i64(out, value);
        }
        for (const auto value : {episode.add_events, episode.remove_events,
                                 episode.replenish_events, episode.prints_at_quote,
                                 episode.prints_through}) {
            out.push_back(',');
            append_u64(out, value);
        }

        Pending pending;
        pending.ns = sample_ns;
        pending.side = side;
        pending.price_ticks = episode.price_ticks;
        pending.segment_id = episode.segment_id;
        pending.prefix = out;
        pending_.push_back(std::move(pending));
        ++summary_.grid_rows;
    }
}

void LevelLifecycleObserver::retire(std::uint64_t local_ns, bool force) {
    while (!pending_.empty()) {
        const auto& front = pending_.front();
        if (!force && local_ns < front.ns + widest_ns_) {
            break;
        }
        emit_pending(front, force ? local_ns : 0);
        pending_.pop_front();
    }
}

void LevelLifecycleObserver::emit_pending(
    const Pending& pending,
    std::uint64_t segment_end_ns) {
    auto& out = row_;
    out.clear();
    out.append(pending.prefix);
    for (std::size_t i = 0; i < LevelLifecycleConfig::kHorizons; ++i) {
        // A horizon that would reach past the segment edge is censored, never recorded as a
        // zero: the events that would have decided it were simply never observed.
        const bool censored =
            segment_end_ns != 0 && pending.ns + horizon_ns_[i] > segment_end_ns;
        out.push_back(',');
        if (!censored) {
            append_u64(out, pending.through_flag[i]);
        }
        out.push_back(',');
        if (!censored) {
            append_i64(out, pending.at_quote_volume[i]);
        }
        out.push_back(',');
        if (!censored) {
            append_i64(out, pending.through_volume[i]);
        }
    }
    out.push_back('\n');
    grid_->append(out);
}

void LevelLifecycleObserver::before_record(
    std::uint64_t local_ns,
    std::int64_t,
    const book::OrderBook& book,
    bool live) {
    book_live_ = live;
    if (!in_segment_ || !usable()) {
        return;
    }
    retire(local_ns, false);
    advance_grid(local_ns, false, book);
}

void LevelLifecycleObserver::on_connection(std::uint64_t, std::string_view stream, bool open) {
    if (stream.find("aggTrade") != std::string_view::npos) {
        trade_stream_up_ = open;
    }
}

void LevelLifecycleObserver::before_depth_event(
    const binance::DepthEvent&,
    const book::OrderBook& book) {
    if (!in_segment_ || !book.is_valid()) {
        return;
    }
    for (std::uint8_t side = kBid; side <= kAsk; ++side) {
        auto& episode = levels_[side];
        episode.pre_event_qty = episode.active
            ? (side == kBid ? quantity_at(book.bids(), episode.price_ticks)
                            : quantity_at(book.asks(), episode.price_ticks))
            : -1;
    }
}

void LevelLifecycleObserver::after_depth_event(
    std::uint64_t local_ns,
    std::int64_t,
    const book::OrderBook& book,
    bool applied,
    bool live) {
    book_live_ = live;
    if (!applied || !in_segment_) {
        for (auto& episode : levels_) {
            episode.pre_event_qty = -1;
        }
        return;
    }
    ++summary_.depth_events;
    if (!book.is_valid()) {
        return;
    }
    // The quantity change at the still-current level is attributed to the episode that was
    // active when the event arrived, before any decision about whether the level is still best.
    for (std::uint8_t side = kBid; side <= kAsk; ++side) {
        auto& episode = levels_[side];
        if (!episode.active || episode.pre_event_qty < 0) {
            episode.pre_event_qty = -1;
            continue;
        }
        const auto after = side == kBid ? quantity_at(book.bids(), episode.price_ticks)
                                        : quantity_at(book.asks(), episode.price_ticks);
        apply_level_delta(episode, after - episode.pre_event_qty, local_ns);
        episode.pre_event_qty = -1;
    }
    for (std::uint8_t side = kBid; side <= kAsk; ++side) {
        const auto& side_map_empty = side == kBid ? book.bids().empty() : book.asks().empty();
        if (side_map_empty) {
            end_episode(side, local_ns, "book_side_empty");
            continue;
        }
        const auto best = side == kBid ? book.bids().begin()->first
                                       : book.asks().begin()->first;
        const auto qty = side == kBid ? book.bids().begin()->second
                                      : book.asks().begin()->second;
        auto& episode = levels_[side];
        if (episode.active && episode.price_ticks == best) {
            continue;
        }
        if (episode.active) {
            // A bid episode whose price is no longer best either stepped away, meaning the book
            // moved down through it, or was improved on, meaning a better bid appeared inside.
            const bool stepped_away = side == kBid ? best < episode.price_ticks
                                                   : best > episode.price_ticks;
            end_episode(side, local_ns, stepped_away ? "stepped_away" : "improved");
        }
        start_episode(side, best, qty, local_ns);
    }
}

void LevelLifecycleObserver::after_snapshot(
    std::uint64_t local_ns,
    const book::OrderBook&,
    bool live) {
    book_live_ = live;
    if (in_segment_) {
        close_segment(local_ns, "resynchronized");
    }
}

void LevelLifecycleObserver::on_trade(
    std::uint64_t local_ns,
    const binance::AggTradeEvent& trade,
    const book::OrderBook&,
    bool live) {
    book_live_ = live;
    if (!in_segment_) {
        return;
    }
    ++summary_.trade_events;
    const std::uint8_t side = trade.is_aggressive_buy() ? kAsk : kBid;
    auto& episode = levels_[side];
    if (episode.active) {
        const bool through = side == kBid ? trade.price_ticks < episode.price_ticks
                                          : trade.price_ticks > episode.price_ticks;
        if (trade.price_ticks == episode.price_ticks) {
            episode.cum_trade_at_quote += trade.qty_lots;
            episode.trade_pool += trade.qty_lots;
            ++episode.prints_at_quote;
            episode.last_print_ns = local_ns;
            ++summary_.prints_at_quote;
        } else if (through) {
            episode.cum_trade_through += trade.qty_lots;
            ++episode.prints_through;
            episode.last_print_ns = local_ns;
            ++summary_.prints_through;
        } else {
            // The print landed on the far side of the reconstructed touch: the depth feed had
            // not caught up. It consumed no part of this level.
            ++summary_.prints_outside;
        }
    }
    for (auto& pending : pending_) {
        if (pending.side != side || pending.ns >= local_ns) {
            continue;
        }
        const bool through = side == kBid ? trade.price_ticks < pending.price_ticks
                                          : trade.price_ticks > pending.price_ticks;
        const bool at_quote = trade.price_ticks == pending.price_ticks;
        if (!through && !at_quote) {
            continue;
        }
        for (std::size_t i = 0; i < LevelLifecycleConfig::kHorizons; ++i) {
            if (local_ns > pending.ns + horizon_ns_[i]) {
                continue;
            }
            if (through) {
                pending.through_flag[i] = 1;
                pending.through_volume[i] += trade.qty_lots;
            } else {
                pending.at_quote_volume[i] += trade.qty_lots;
            }
        }
    }
}

void LevelLifecycleObserver::after_record(
    std::uint64_t local_ns,
    std::int64_t,
    const book::OrderBook& book,
    bool live) {
    book_live_ = live;
    last_record_ns_ = local_ns;
    if (in_segment_) {
        if (usable()) {
            retire(local_ns, false);
            advance_grid(local_ns, true, book);
        } else {
            close_segment(local_ns, "segment_end");
        }
        return;
    }
    if (usable() && book.is_valid()) {
        open_segment(local_ns, book);
        advance_grid(local_ns, true, book);
    }
}

void LevelLifecycleObserver::finish(const book::OrderBook&) {
    close_segment(std::max(last_record_ns_, segment_start_ns_), "end_of_input");
    episodes_->close();
    grid_->close();
    closed_ = true;
}

}  // namespace crypto::research
