#include "research/native_dataset_exporter.hpp"

#include <algorithm>
#include <charconv>
#include <cmath>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <system_error>

#include "research/compressed_text_writer.hpp"
#include "research/microstructure_features.hpp"

namespace crypto::research {
namespace {

constexpr std::size_t kBidAdd = 0;
constexpr std::size_t kBidRemove = 10;
constexpr std::size_t kAskAdd = 20;
constexpr std::size_t kAskRemove = 30;
constexpr std::size_t kBuyQty = 40;
constexpr std::size_t kSellQty = 41;
constexpr std::size_t kTradeCount = 42;
constexpr std::size_t kBuyCount = 43;
constexpr std::size_t kSellCount = 44;
constexpr std::size_t kDepthEvents = 45;
constexpr std::size_t kAbsMidChangeHalfTicks = 46;
constexpr std::size_t kBboChanges = 47;

constexpr std::size_t kBid = 0;
constexpr std::size_t kAsk = 1;

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

// Nine significant digits: enough to keep every ratio and markout distinguishable, short enough
// that the compressed dataset stays manageable, and identical on every run of the same input.
void append_double(std::string& out, double value) {
    if (!std::isfinite(value)) {
        return;
    }
    std::array<char, 32> buffer{};
    const auto result = std::to_chars(
        buffer.data(), buffer.data() + buffer.size(), value, std::chars_format::general, 9);
    if (result.ec != std::errc{}) {
        throw std::runtime_error("cannot format research value");
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

std::string window_suffix(std::uint64_t window_ms) {
    return "_" + std::to_string(window_ms) + "ms";
}

}  // namespace

std::string NativeDatasetExporter::csv_header(const NativeDatasetConfig& config) {
    std::ostringstream out;
    out << "timestamp_ns,exchange_timestamp_ms,file_index,segment_id,sequence_update_id"
           ",valid_book,segment_age_ms,window_warm";
    out << ",spread_ticks,mid_ticks,microprice_ticks,microprice_minus_mid_ticks";
    for (std::size_t i = 1; i <= NativeDatasetConfig::kLevels; ++i) {
        out << ",bid_px_" << i;
    }
    for (std::size_t i = 1; i <= NativeDatasetConfig::kLevels; ++i) {
        out << ",bid_qty_" << i;
    }
    for (std::size_t i = 1; i <= NativeDatasetConfig::kLevels; ++i) {
        out << ",ask_px_" << i;
    }
    for (std::size_t i = 1; i <= NativeDatasetConfig::kLevels; ++i) {
        out << ",ask_qty_" << i;
    }
    out << ",bid_depth_l1,ask_depth_l1,bid_depth_l5,ask_depth_l5,bid_depth_l10,ask_depth_l10"
           ",bid_concentration_l5,ask_concentration_l5,bid_concentration_l10,ask_concentration_l10"
           ",bid_dispersion_ticks_l10,ask_dispersion_ticks_l10";
    out << ",obi_l1,obi_l5,obi_l10,weighted_obi_l5,weighted_obi_l10";
    out << ",time_since_trade_ms,time_since_depth_ms,time_since_bbo_change_ms"
           ",time_since_mid_change_ms";
    for (const auto window : config.window_ms) {
        const auto suffix = window_suffix(window);
        for (std::size_t i = 1; i <= NativeDatasetConfig::kLevels; ++i) {
            out << ",bid_depth_add_l" << i << suffix;
        }
        for (std::size_t i = 1; i <= NativeDatasetConfig::kLevels; ++i) {
            out << ",bid_depth_remove_l" << i << suffix;
        }
        for (std::size_t i = 1; i <= NativeDatasetConfig::kLevels; ++i) {
            out << ",ask_depth_add_l" << i << suffix;
        }
        for (std::size_t i = 1; i <= NativeDatasetConfig::kLevels; ++i) {
            out << ",ask_depth_remove_l" << i << suffix;
        }
        out << ",depth_flow_pressure_l5" << suffix
            << ",depth_flow_pressure_l10" << suffix
            << ",buy_qty" << suffix
            << ",sell_qty" << suffix
            << ",signed_volume" << suffix
            << ",trade_imbalance" << suffix
            << ",trade_count" << suffix
            << ",depth_event_count" << suffix
            << ",bbo_change_count" << suffix
            << ",backward_mid_abs_change_ticks" << suffix;
    }
    out << ",next_mid_move_dir,time_to_next_mid_move_ms";
    for (const auto horizon : config.markout_ms) {
        out << ",markout" << window_suffix(horizon) << "_ticks";
    }
    for (const char* side : {"bid", "ask"}) {
        out << ',' << side << "_eligible"
            << ',' << side << "_quote_px_ticks"
            << ',' << side << "_queue_ahead_lots";
        for (const auto horizon : config.fill_ms) {
            out << ',' << side << "_fill" << window_suffix(horizon);
        }
        out << ',' << side << "_filled_lots" << window_suffix(config.fill_horizon_ms)
            << ',' << side << "_fill_before_observed_mid_adverse"
            << ',' << side << "_time_to_fill_ms"
            << ',' << side << "_time_to_mid_adverse_ms"
            << ',' << side << "_time_to_quote_gone_ms"
            << ',' << side << "_time_to_best_adverse_ms"
            << ',' << side << "_fill_via_trade_through"
            << ',' << side << "_fill_mid_ticks";
        for (const auto horizon : config.postfill_ms) {
            out << ',' << side << "_postfill_markout" << window_suffix(horizon) << "_ticks";
        }
    }
    out << '\n';
    return out.str();
}

NativeDatasetExporter::NativeDatasetExporter(
    const std::filesystem::path& output_path,
    NativeDatasetConfig config)
    : config_(config),
      writer_(std::make_unique<CompressedTextWriter>(output_path)) {
    if (config_.grid_ms == 0 || config_.order_lots <= 0 || config_.fill_horizon_ms == 0) {
        throw std::invalid_argument("invalid native dataset configuration");
    }
    if (config_.adverse_threshold_half_ticks <= 0) {
        throw std::invalid_argument("adverse threshold must be positive");
    }
    grid_ns_ = config_.grid_ms * 1'000'000ULL;
    fill_horizon_ns_ = config_.fill_horizon_ms * 1'000'000ULL;
    for (std::size_t i = 0; i < NativeDatasetConfig::kWindows; ++i) {
        window_ns_[i] = config_.window_ms[i] * 1'000'000ULL;
    }
    for (std::size_t i = 0; i < NativeDatasetConfig::kMarkouts; ++i) {
        markout_ns_[i] = config_.markout_ms[i] * 1'000'000ULL;
    }
    for (std::size_t i = 0; i < NativeDatasetConfig::kFillHorizons; ++i) {
        fill_ns_[i] = config_.fill_ms[i] * 1'000'000ULL;
        if (fill_ns_[i] > fill_horizon_ns_) {
            throw std::invalid_argument("fill horizon shorter than a requested fill target");
        }
    }
    for (std::size_t i = 0; i < NativeDatasetConfig::kPostFillHorizons; ++i) {
        postfill_ns_[i] = config_.postfill_ms[i] * 1'000'000ULL;
    }
    if (!std::is_sorted(markout_ns_.begin(), markout_ns_.end())) {
        throw std::invalid_argument("markout horizons must be ascending");
    }
    writer_->append(csv_header(config_));
    row_buffer_.reserve(8192);
}

NativeDatasetExporter::~NativeDatasetExporter() {
    try {
        if (!closed_) {
            writer_->close();
        }
    } catch (...) {
    }
}

void NativeDatasetExporter::push_contribution(const Contribution& contribution) {
    if (!in_segment_) {
        return;
    }
    flow_.push_back(contribution);
    for (auto& sum : window_sums_) {
        for (std::size_t i = 0; i < kCounters; ++i) {
            sum[i] += contribution.values[i];
        }
    }
}

void NativeDatasetExporter::prune_windows(std::uint64_t sample_ns) {
    for (std::size_t w = 0; w < NativeDatasetConfig::kWindows; ++w) {
        auto& head = window_head_[w];
        while (head < flow_.size() && flow_[head].ns + window_ns_[w] <= sample_ns) {
            const auto& values = flow_[head].values;
            for (std::size_t i = 0; i < kCounters; ++i) {
                window_sums_[w][i] -= values[i];
            }
            ++head;
        }
    }
    // Windows are nested, so the widest one bounds what is still referenced.
    const auto widest = *std::min_element(window_head_.begin(), window_head_.end());
    if (widest > 0) {
        flow_.erase(flow_.begin(), flow_.begin() + static_cast<std::ptrdiff_t>(widest));
        for (auto& head : window_head_) {
            head -= widest;
        }
    }
}

void NativeDatasetExporter::open_segment(std::uint64_t local_ns, const book::OrderBook& book) {
    in_segment_ = true;
    segment_start_ns_ = local_ns;
    flow_.clear();
    window_sums_ = {};
    window_head_ = {};
    pending_.clear();
    watching_rows_ = 0;
    last_trade_ns_ = 0;
    last_depth_ns_ = 0;
    last_bbo_change_ns_ = 0;
    last_mid_change_ns_ = 0;
    min_deadline_ns_ = std::numeric_limits<std::uint64_t>::max();
    have_last_resolved_mid_ = false;
    tail_unsynced_depth_events_ = 0;
    const auto remainder = local_ns % grid_ns_;
    next_sample_ns_ = remainder == 0 ? local_ns : local_ns + (grid_ns_ - remainder);
    current_segment_ = {};
    current_segment_.segment_id = ++summary_.segments;
    current_segment_.start_ns = local_ns;
    current_segment_.first_exchange_time_ms = latest_exchange_ms_;
    current_segment_.first_update_id = book.last_update_id();
}

void NativeDatasetExporter::close_segment(std::uint64_t local_ns, std::string reason) {
    if (!in_segment_) {
        return;
    }
    for (const auto& row : pending_) {
        write_row(row);
    }
    pending_.clear();
    watching_rows_ = 0;
    flow_.clear();
    window_sums_ = {};
    window_head_ = {};
    in_segment_ = false;
    current_segment_.end_ns = local_ns;
    current_segment_.last_exchange_time_ms = latest_exchange_ms_;
    current_segment_.close_reason = std::move(reason);
    summary_.valid_research_ns += local_ns - segment_start_ns_;
    summary_.segment_qc.push_back(current_segment_);
}

void NativeDatasetExporter::advance_grid(
    std::uint64_t local_ns,
    bool inclusive,
    const book::OrderBook& book) {
    if (!in_segment_) {
        return;
    }
    while (next_sample_ns_ < local_ns || (inclusive && next_sample_ns_ == local_ns)) {
        emit_sample(next_sample_ns_, book);
        next_sample_ns_ += grid_ns_;
    }
}

void NativeDatasetExporter::emit_sample(std::uint64_t sample_ns, const book::OrderBook& book) {
    if (!book.is_valid()) {
        return;
    }
    const auto features = calculate_instant_book_features(book, config_.weight_lambda);
    if (!features.has_l1) {
        return;
    }
    prune_windows(sample_ns);

    const auto bid_levels = std::min<std::size_t>(book.bids().size(), NativeDatasetConfig::kLevels);
    const auto ask_levels = std::min<std::size_t>(book.asks().size(), NativeDatasetConfig::kLevels);
    const auto mid_x2 = features.bids[0].price_ticks + features.asks[0].price_ticks;

    auto& out = row_buffer_;
    out.clear();
    append_u64(out, sample_ns);
    out.push_back(',');
    append_i64(out, latest_exchange_ms_);
    out.push_back(',');
    append_u64(out, config_.file_index);
    out.push_back(',');
    append_u64(out, current_segment_.segment_id);
    out.push_back(',');
    append_u64(out, book.last_update_id());
    out.append(",1,");
    append_double(out, static_cast<double>(sample_ns - segment_start_ns_) / 1e6);
    out.push_back(',');
    out.push_back(sample_ns >= segment_start_ns_ + window_ns_[NativeDatasetConfig::kWindows - 1]
                      ? '1'
                      : '0');

    out.push_back(',');
    append_i64(out, features.asks[0].price_ticks - features.bids[0].price_ticks);
    out.push_back(',');
    append_double(out, static_cast<double>(mid_x2) / 2.0);
    out.push_back(',');
    append_double(out, features.weighted_mid_ticks.value_or(
        std::numeric_limits<double>::quiet_NaN()));
    out.push_back(',');
    append_double(out, features.weighted_mid_minus_mid_ticks.value_or(
        std::numeric_limits<double>::quiet_NaN()));

    const auto emit_levels = [&](const std::array<binance::PriceLevel, 10>& levels,
                                 std::size_t count,
                                 bool price) {
        for (std::size_t i = 0; i < NativeDatasetConfig::kLevels; ++i) {
            out.push_back(',');
            if (i < count) {
                append_i64(out, price ? levels[i].price_ticks : levels[i].qty_lots);
            }
        }
    };
    emit_levels(features.bids, bid_levels, true);
    emit_levels(features.bids, bid_levels, false);
    emit_levels(features.asks, ask_levels, true);
    emit_levels(features.asks, ask_levels, false);

    const auto depth_sum = [](const std::array<binance::PriceLevel, 10>& levels,
                              std::size_t count,
                              std::size_t levels_wanted) -> std::int64_t {
        std::int64_t total = 0;
        for (std::size_t i = 0; i < std::min(count, levels_wanted); ++i) {
            total += levels[i].qty_lots;
        }
        return total;
    };
    const auto bid_l1 = features.bids[0].qty_lots;
    const auto ask_l1 = features.asks[0].qty_lots;
    const auto bid_l5 = depth_sum(features.bids, bid_levels, 5);
    const auto ask_l5 = depth_sum(features.asks, ask_levels, 5);
    const auto bid_l10 = depth_sum(features.bids, bid_levels, 10);
    const auto ask_l10 = depth_sum(features.asks, ask_levels, 10);
    for (const auto value : {bid_l1, ask_l1, bid_l5, ask_l5, bid_l10, ask_l10}) {
        out.push_back(',');
        append_i64(out, value);
    }
    const auto ratio = [](std::int64_t numerator, std::int64_t denominator) {
        return denominator == 0
            ? std::numeric_limits<double>::quiet_NaN()
            : static_cast<double>(numerator) / static_cast<double>(denominator);
    };
    for (const auto value : {ratio(bid_l1, bid_l5), ratio(ask_l1, ask_l5),
                             ratio(bid_l1, bid_l10), ratio(ask_l1, ask_l10)}) {
        out.push_back(',');
        append_double(out, value);
    }
    // Quantity-weighted distance of the visible book from its own best price, in ticks: a large
    // value means depth sits far behind the touch.
    const auto dispersion = [&](const std::array<binance::PriceLevel, 10>& levels,
                                std::size_t count,
                                bool bid_side) {
        std::int64_t weight = 0;
        double distance = 0.0;
        for (std::size_t i = 0; i < count; ++i) {
            const auto offset = bid_side
                ? levels[0].price_ticks - levels[i].price_ticks
                : levels[i].price_ticks - levels[0].price_ticks;
            distance += static_cast<double>(offset) * static_cast<double>(levels[i].qty_lots);
            weight += levels[i].qty_lots;
        }
        return weight == 0 ? std::numeric_limits<double>::quiet_NaN()
                           : distance / static_cast<double>(weight);
    };
    out.push_back(',');
    append_double(out, dispersion(features.bids, bid_levels, true));
    out.push_back(',');
    append_double(out, dispersion(features.asks, ask_levels, false));

    for (const auto& value : {features.obi_l1, features.obi_l5, features.obi_l10,
                              features.weighted_obi_l5, features.weighted_obi_l10}) {
        out.push_back(',');
        append_double(out, value.value_or(std::numeric_limits<double>::quiet_NaN()));
    }

    // Elapsed time since the last in-segment occurrence of each event class. Empty until the
    // event has been seen at least once inside the current segment.
    for (const auto stamp : {last_trade_ns_, last_depth_ns_, last_bbo_change_ns_,
                             last_mid_change_ns_}) {
        out.push_back(',');
        if (stamp != 0 && sample_ns >= stamp) {
            append_double(out, static_cast<double>(sample_ns - stamp) / 1e6);
        }
    }

    for (std::size_t w = 0; w < NativeDatasetConfig::kWindows; ++w) {
        const auto& sum = window_sums_[w];
        for (const auto base : {kBidAdd, kBidRemove, kAskAdd, kAskRemove}) {
            for (std::size_t i = 0; i < NativeDatasetConfig::kLevels; ++i) {
                out.push_back(',');
                append_i64(out, sum[base + i]);
            }
        }
        // Exponentially distance-weighted net depth pressure, normalised by the weighted gross
        // flow so it stays inside [-1, 1] regardless of activity.
        const auto pressure = [&](std::size_t levels) {
            double net = 0.0;
            double gross = 0.0;
            for (std::size_t i = 0; i < levels; ++i) {
                const double weight = std::exp(-config_.weight_lambda * static_cast<double>(i));
                const auto bid_add = static_cast<double>(sum[kBidAdd + i]);
                const auto bid_remove = static_cast<double>(sum[kBidRemove + i]);
                const auto ask_add = static_cast<double>(sum[kAskAdd + i]);
                const auto ask_remove = static_cast<double>(sum[kAskRemove + i]);
                net += weight * ((bid_add - bid_remove) - (ask_add - ask_remove));
                gross += weight * (bid_add + bid_remove + ask_add + ask_remove);
            }
            return gross == 0.0 ? std::numeric_limits<double>::quiet_NaN() : net / gross;
        };
        out.push_back(',');
        append_double(out, pressure(5));
        out.push_back(',');
        append_double(out, pressure(NativeDatasetConfig::kLevels));
        out.push_back(',');
        append_i64(out, sum[kBuyQty]);
        out.push_back(',');
        append_i64(out, sum[kSellQty]);
        out.push_back(',');
        append_i64(out, sum[kBuyQty] - sum[kSellQty]);
        out.push_back(',');
        append_double(out, ratio(sum[kBuyQty] - sum[kSellQty], sum[kBuyQty] + sum[kSellQty]));
        out.push_back(',');
        append_i64(out, sum[kTradeCount]);
        out.push_back(',');
        append_i64(out, sum[kDepthEvents]);
        out.push_back(',');
        append_i64(out, sum[kBboChanges]);
        out.push_back(',');
        append_double(out, static_cast<double>(sum[kAbsMidChangeHalfTicks]) / 2.0);
    }

    PendingRow row;
    row.ns = sample_ns;
    row.prefix = out;
    row.mid_x2 = mid_x2;
    row.side[kBid].quote_px_ticks = features.bids[0].price_ticks;
    row.side[kBid].queue_ahead_lots = bid_l1;
    row.side[kAsk].quote_px_ticks = features.asks[0].price_ticks;
    row.side[kAsk].queue_ahead_lots = ask_l1;
    refresh_next_deadline(row);
    min_deadline_ns_ = std::min(min_deadline_ns_, row.next_deadline_ns);
    ++watching_rows_;
    pending_.push_back(std::move(row));
    ++current_segment_.rows;
    ++summary_.rows;
}

// Book-state races for every resting hypothetical order, evaluated on the depth stream only.
// Kept separate from the trade-driven fill simulation precisely so the two feeds stay
// distinguishable downstream: a fill is observed on aggTrade, these events on @depth@100ms.
void NativeDatasetExporter::watch_book_races(
    std::uint64_t local_ns,
    const book::OrderBook& book) {
    if (watching_rows_ == 0 || book.bids().empty() || book.asks().empty()) {
        return;
    }
    const auto best_bid = book.bids().begin()->first;
    const auto best_ask = book.asks().begin()->first;
    for (auto& row : pending_) {
        if (!row.watching) {
            continue;
        }
        bool outstanding = false;
        for (std::size_t s = 0; s < row.side.size(); ++s) {
            auto& side = row.side[s];
            const bool censor = row.ns + fill_horizon_ns_ <= local_ns;
            if (side.best_adverse_ns == 0) {
                // The touch has walked away from the quote price on the resting order's side.
                const bool adverse = s == kBid ? best_bid < side.quote_px_ticks
                                               : best_ask > side.quote_px_ticks;
                if (adverse) {
                    side.best_adverse_ns = local_ns;
                    // A price beyond the quote can only be best once the quote itself is gone.
                    if (side.quote_gone_ns == 0) {
                        side.quote_gone_ns = local_ns;
                    }
                } else if (!censor) {
                    outstanding = true;
                }
            }
            if (side.quote_gone_ns == 0) {
                const bool at_touch = s == kBid ? best_bid == side.quote_px_ticks
                                                : best_ask == side.quote_px_ticks;
                if (!at_touch) {
                    const auto remaining = s == kBid
                        ? quantity_at(book.bids(), side.quote_px_ticks)
                        : quantity_at(book.asks(), side.quote_px_ticks);
                    if (remaining == 0) {
                        side.quote_gone_ns = local_ns;
                    }
                }
                if (side.quote_gone_ns == 0 && !censor) {
                    outstanding = true;
                }
            }
        }
        if (!outstanding) {
            row.watching = false;
            --watching_rows_;
        }
    }
}

void NativeDatasetExporter::refresh_next_deadline(PendingRow& row) const {
    auto deadline = std::numeric_limits<std::uint64_t>::max();
    for (std::size_t k = 0; k < NativeDatasetConfig::kMarkouts; ++k) {
        if (!row.markout_done[k]) {
            deadline = std::min(deadline, row.ns + markout_ns_[k]);
        }
    }
    for (auto& side : row.side) {
        for (std::size_t k = 0; k < NativeDatasetConfig::kFillHorizons; ++k) {
            if (side.fill_flag[k] < 0) {
                deadline = std::min(deadline, row.ns + fill_ns_[k]);
            }
        }
        if (side.filled_lots_at_horizon < 0 ||
            side.fill_before_observed_mid_adverse < 0 || row.watching) {
            deadline = std::min(deadline, row.ns + fill_horizon_ns_);
        }
        for (std::size_t k = 0; k < NativeDatasetConfig::kPostFillHorizons; ++k) {
            if (side.postfill_done[k]) {
                continue;
            }
            deadline = std::min(
                deadline,
                side.full_filled ? side.full_fill_ns + postfill_ns_[k]
                                 : row.ns + fill_horizon_ns_);
        }
    }
    row.next_deadline_ns = deadline;
}

// Mid-driven updates that can only ever fire once per row, cheap enough to run on every
// observed mid change without recomputing the row's deadline set.
void NativeDatasetExporter::apply_mid_move(
    PendingRow& row,
    std::int64_t mid_x2,
    std::uint64_t now_ns,
    std::int64_t adverse_threshold_half_ticks,
    std::uint64_t move_cap_ns) {
    if (mid_x2 == row.mid_x2 || now_ns > move_cap_ns) {
        return;
    }
    if (row.next_move_dir == 0) {
        row.next_move_dir = mid_x2 > row.mid_x2 ? std::int8_t{1} : std::int8_t{-1};
        row.next_move_ms = static_cast<double>(now_ns - row.ns) / 1e6;
    }
    for (std::size_t s = 0; s < row.side.size(); ++s) {
        auto& side = row.side[s];
        if (side.adverse) {
            continue;
        }
        // A resting bid is adversely selected when the mid falls away from it, and vice versa.
        const auto signed_drift = s == kBid ? mid_x2 - row.mid_x2 : row.mid_x2 - mid_x2;
        if (signed_drift <= -adverse_threshold_half_ticks) {
            side.adverse = true;
            side.mid_adverse_ns = now_ns;
            if (side.fill_before_observed_mid_adverse < 0) {
                side.fill_before_observed_mid_adverse = 0;
            }
        }
    }
}

void NativeDatasetExporter::resolve_pending(
    std::uint64_t now_ns,
    std::uint64_t due_ns,
    const book::OrderBook& book) {
    if (pending_.empty() || !book.is_valid() || book.bids().empty() || book.asks().empty()) {
        return;
    }
    const auto mid_x2 = book.bids().begin()->first + book.asks().begin()->first;
    const bool mid_changed = !have_last_resolved_mid_ || mid_x2 != last_resolved_mid_x2_;
    if (due_ns < min_deadline_ns_ && !mid_changed) {
        return;
    }
    last_resolved_mid_x2_ = mid_x2;
    have_last_resolved_mid_ = true;

    auto next_min = std::numeric_limits<std::uint64_t>::max();
    for (auto& row : pending_) {
        if (row.ns >= now_ns) {
            next_min = std::min(next_min, row.next_deadline_ns);
            continue;
        }
        if (mid_changed) {
            apply_mid_move(
                row, mid_x2, now_ns, config_.adverse_threshold_half_ticks,
                row.ns + fill_horizon_ns_);
        }
        if (row.next_deadline_ns > due_ns) {
            next_min = std::min(next_min, row.next_deadline_ns);
            continue;
        }
        const double drift = static_cast<double>(mid_x2 - row.mid_x2) / 2.0;
        for (std::size_t k = 0; k < NativeDatasetConfig::kMarkouts; ++k) {
            if (!row.markout_done[k] && row.ns + markout_ns_[k] <= due_ns) {
                row.markout[k] = drift;
                row.markout_done[k] = true;
            }
        }
        for (std::size_t s = 0; s < row.side.size(); ++s) {
            auto& side = row.side[s];
            for (std::size_t k = 0; k < NativeDatasetConfig::kFillHorizons; ++k) {
                if (side.fill_flag[k] < 0 && row.ns + fill_ns_[k] <= due_ns) {
                    side.fill_flag[k] = static_cast<std::int8_t>(
                        side.full_filled && side.full_fill_ns <= row.ns + fill_ns_[k] ? 1 : 0);
                }
            }
            if (row.ns + fill_horizon_ns_ <= due_ns) {
                if (side.filled_lots_at_horizon < 0) {
                    side.filled_lots_at_horizon = std::clamp(
                        side.consumed_lots - side.queue_ahead_lots,
                        std::int64_t{0},
                        config_.order_lots);
                }
                if (side.fill_before_observed_mid_adverse < 0) {
                    // Neither a fill nor an adverse move inside the horizon: censored, not a zero.
                    side.fill_before_observed_mid_adverse = 2;
                }
                if (!side.full_filled) {
                    side.postfill_done.fill(true);
                }
            }
            if (side.full_filled) {
                for (std::size_t k = 0; k < NativeDatasetConfig::kPostFillHorizons; ++k) {
                    if (!side.postfill_done[k] && side.full_fill_ns + postfill_ns_[k] <= due_ns) {
                        side.postfill[k] = s == kBid
                            ? static_cast<double>(mid_x2) / 2.0 -
                                  static_cast<double>(side.quote_px_ticks)
                            : static_cast<double>(side.quote_px_ticks) -
                                  static_cast<double>(mid_x2) / 2.0;
                        side.postfill_done[k] = true;
                    }
                }
            }
        }
        refresh_next_deadline(row);
        next_min = std::min(next_min, row.next_deadline_ns);
    }
    min_deadline_ns_ = next_min;
    flush_front_resolved();
}

void NativeDatasetExporter::flush_front_resolved() {
    while (!pending_.empty() &&
           pending_.front().next_deadline_ns == std::numeric_limits<std::uint64_t>::max()) {
        write_row(pending_.front());
        pending_.pop_front();
    }
}

void NativeDatasetExporter::write_row(const PendingRow& row) {
    auto& out = row_buffer_;
    out.clear();
    out.append(row.prefix);
    out.push_back(',');
    if (row.next_move_dir != 0) {
        append_i64(out, row.next_move_dir);
    }
    out.push_back(',');
    if (row.next_move_dir != 0) {
        append_double(out, row.next_move_ms);
    }
    for (std::size_t k = 0; k < NativeDatasetConfig::kMarkouts; ++k) {
        out.push_back(',');
        if (row.markout_done[k]) {
            append_double(out, row.markout[k]);
        }
    }
    for (std::size_t s = 0; s < row.side.size(); ++s) {
        const auto& side = row.side[s];
        out.append(",1,");
        append_i64(out, side.quote_px_ticks);
        out.push_back(',');
        append_i64(out, side.queue_ahead_lots);
        for (std::size_t k = 0; k < NativeDatasetConfig::kFillHorizons; ++k) {
            out.push_back(',');
            if (side.fill_flag[k] >= 0) {
                append_i64(out, side.fill_flag[k]);
            }
        }
        out.push_back(',');
        if (side.filled_lots_at_horizon >= 0) {
            append_i64(out, side.filled_lots_at_horizon);
        }
        out.push_back(',');
        if (side.fill_before_observed_mid_adverse == 0 ||
            side.fill_before_observed_mid_adverse == 1) {
            append_i64(out, side.fill_before_observed_mid_adverse);
        }
        out.push_back(',');
        if (side.full_filled) {
            append_double(out, static_cast<double>(side.full_fill_ns - row.ns) / 1e6);
        }
        for (const auto stamp : {side.mid_adverse_ns, side.quote_gone_ns,
                                 side.best_adverse_ns}) {
            out.push_back(',');
            if (stamp != 0) {
                append_double(out, static_cast<double>(stamp - row.ns) / 1e6);
            }
        }
        out.push_back(',');
        if (side.full_filled) {
            out.push_back(side.filled_by_trade_through ? '1' : '0');
        }
        out.push_back(',');
        if (side.full_filled) {
            append_double(out, static_cast<double>(side.fill_mid_x2) / 2.0);
        }
        for (std::size_t k = 0; k < NativeDatasetConfig::kPostFillHorizons; ++k) {
            out.push_back(',');
            if (side.full_filled && side.postfill_done[k]) {
                append_double(out, side.postfill[k]);
            }
        }
    }
    out.push_back('\n');
    writer_->append(out);
}

void NativeDatasetExporter::before_record(
    std::uint64_t local_ns,
    std::int64_t latest_exchange_ms,
    const book::OrderBook& book,
    bool live) {
    book_live_ = live;
    latest_exchange_ms_ = latest_exchange_ms;
    if (!in_segment_) {
        return;
    }
    if (!usable()) {
        return;
    }
    const auto due_ns = local_ns == 0 ? 0 : local_ns - 1;
    resolve_pending(local_ns, due_ns, book);
    advance_grid(local_ns, false, book);
    // Several grid points can fall inside one gap between raw events. The rows just emitted for
    // the earlier ones already have horizons in the observed past, and the book has not moved
    // since, so they must be resolved against this state rather than against a later one.
    resolve_pending(local_ns, due_ns, book);
}

void NativeDatasetExporter::on_connection(
    std::uint64_t local_ns,
    std::string_view stream,
    bool open) {
    (void)local_ns;
    if (stream.find("aggTrade") == std::string_view::npos) {
        if (stream.find("depth") != std::string_view::npos && open) {
            ++summary_.depth_stream_opens;
        }
        return;
    }
    // Depth and aggressive trades run on separate sockets and reconnect independently. A trade
    // outage leaves the book intact but silently removes the flow the fill model depends on, so
    // it ends the usable segment exactly like a depth desync does.
    if (open) {
        ++summary_.trade_stream_opens;
    }
    trade_stream_up_ = open;
}

void NativeDatasetExporter::before_depth_event(
    const binance::DepthEvent& event,
    const book::OrderBook& book) {
    pending_depth_flow_ = {};
    pending_depth_flow_valid_ = book.is_valid();
    if (!pending_depth_flow_valid_) {
        return;
    }
    pre_event_best_bid_ = book.bids().empty() ? 0 : book.bids().begin()->first;
    pre_event_best_ask_ = book.asks().empty() ? 0 : book.asks().begin()->first;
    pre_event_mid_x2_ = pre_event_best_bid_ != 0 && pre_event_best_ask_ != 0
        ? pre_event_best_bid_ + pre_event_best_ask_
        : 0;
    std::array<std::int64_t, NativeDatasetConfig::kLevels> bid_px{};
    std::array<std::int64_t, NativeDatasetConfig::kLevels> ask_px{};
    std::size_t bid_n = 0;
    std::size_t ask_n = 0;
    for (auto it = book.bids().begin(); it != book.bids().end() && bid_n < bid_px.size(); ++it) {
        bid_px[bid_n++] = it->first;
    }
    for (auto it = book.asks().begin(); it != book.asks().end() && ask_n < ask_px.size(); ++it) {
        ask_px[ask_n++] = it->first;
    }
    // Flow is attributed to the rank the price held in the pre-event book, which is the only
    // ranking an observer could have known before the update arrived.
    for (const auto& level : event.bids) {
        std::size_t rank = 0;
        while (rank < bid_n && bid_px[rank] > level.price_ticks) {
            ++rank;
        }
        if (rank >= NativeDatasetConfig::kLevels) {
            continue;
        }
        const auto difference = level.qty_lots - quantity_at(book.bids(), level.price_ticks);
        if (difference > 0) {
            pending_depth_flow_.values[kBidAdd + rank] += difference;
        } else if (difference < 0) {
            pending_depth_flow_.values[kBidRemove + rank] += -difference;
        }
    }
    for (const auto& level : event.asks) {
        std::size_t rank = 0;
        while (rank < ask_n && ask_px[rank] < level.price_ticks) {
            ++rank;
        }
        if (rank >= NativeDatasetConfig::kLevels) {
            continue;
        }
        const auto difference = level.qty_lots - quantity_at(book.asks(), level.price_ticks);
        if (difference > 0) {
            pending_depth_flow_.values[kAskAdd + rank] += difference;
        } else if (difference < 0) {
            pending_depth_flow_.values[kAskRemove + rank] += -difference;
        }
    }
}

void NativeDatasetExporter::after_depth_event(
    std::uint64_t local_ns,
    std::int64_t exchange_ms,
    const book::OrderBook& book,
    bool applied,
    bool live) {
    (void)book;
    (void)exchange_ms;
    book_live_ = live;
    if (!applied) {
        pending_depth_flow_valid_ = false;
        if (in_segment_) {
            close_segment(local_ns, "depth_desync");
        }
        ++tail_unsynced_depth_events_;
        ++summary_.skipped_depth_events;
        return;
    }
    if (!in_segment_) {
        ++tail_unsynced_depth_events_;
        ++summary_.skipped_depth_events;
        return;
    }
    ++current_segment_.depth_events;
    ++summary_.depth_events_in_segment;
    current_segment_.final_update_id = book.last_update_id();
    last_depth_ns_ = local_ns;
    if (!pending_depth_flow_valid_) {
        return;
    }
    if (!book.bids().empty() && !book.asks().empty()) {
        const auto best_bid = book.bids().begin()->first;
        const auto best_ask = book.asks().begin()->first;
        if (pre_event_best_bid_ != 0 &&
            (best_bid != pre_event_best_bid_ || best_ask != pre_event_best_ask_)) {
            pending_depth_flow_.values[kBboChanges] = 1;
            last_bbo_change_ns_ = local_ns;
        }
        const auto mid_x2 = best_bid + best_ask;
        if (pre_event_mid_x2_ != 0 && mid_x2 != pre_event_mid_x2_) {
            pending_depth_flow_.values[kAbsMidChangeHalfTicks] =
                std::abs(mid_x2 - pre_event_mid_x2_);
            last_mid_change_ns_ = local_ns;
        }
    }
    pending_depth_flow_.ns = local_ns;
    pending_depth_flow_.values[kDepthEvents] = 1;
    push_contribution(pending_depth_flow_);
    pending_depth_flow_valid_ = false;
    watch_book_races(local_ns, book);
}

void NativeDatasetExporter::after_snapshot(
    std::uint64_t local_ns,
    const book::OrderBook& book,
    bool live) {
    (void)book;
    ++summary_.snapshots;
    book_live_ = live;
    if (in_segment_) {
        // A replacement snapshot means the previous book state was abandoned.
        close_segment(local_ns, "resnapshot");
    }
}

void NativeDatasetExporter::on_trade(
    std::uint64_t local_ns,
    const binance::AggTradeEvent& trade,
    const book::OrderBook& book,
    bool live) {
    book_live_ = live;
    if (!in_segment_) {
        return;
    }
    ++current_segment_.trade_events;
    ++summary_.trade_events_in_segment;
    last_trade_ns_ = local_ns;
    Contribution contribution;
    contribution.ns = local_ns;
    contribution.values[kTradeCount] = 1;
    if (trade.is_aggressive_buy()) {
        contribution.values[kBuyQty] = trade.qty_lots;
        contribution.values[kBuyCount] = 1;
    } else {
        contribution.values[kSellQty] = trade.qty_lots;
        contribution.values[kSellCount] = 1;
    }
    push_contribution(contribution);

    const auto mid_x2 = book.is_valid() && !book.bids().empty() && !book.asks().empty()
        ? book.bids().begin()->first + book.asks().begin()->first
        : 0;
    // Only the opposite side of an aggressor can consume a resting maker order.
    const std::size_t s = trade.is_aggressive_buy() ? kAsk : kBid;
    for (auto& row : pending_) {
        // Strictly after placement: a print stamped at the decision instant is already part of
        // the row's trailing features and must not also fill the order it informed. Past the
        // fill horizon the passive outcome is already frozen.
        if (row.ns >= local_ns || row.ns + fill_horizon_ns_ <= local_ns) {
            continue;
        }
        auto& side = row.side[s];
        if (side.full_filled) {
            continue;
        }
        if (s == kBid) {
            if (trade.price_ticks > side.quote_px_ticks) {
                continue;
            }
        } else if (trade.price_ticks < side.quote_px_ticks) {
            continue;
        }
        if (trade.price_ticks == side.quote_px_ticks) {
            side.consumed_lots += trade.qty_lots;
        } else {
            // A print through the quote price means everything resting there traded away first,
            // so a hypothetical order queued behind that depth is fully filled.
            side.consumed_lots = side.queue_ahead_lots + config_.order_lots;
            side.filled_by_trade_through = true;
        }
        if (side.consumed_lots >= side.queue_ahead_lots + config_.order_lots) {
            side.full_filled = true;
            side.full_fill_ns = local_ns;
            side.fill_mid_x2 = mid_x2;
            if (side.fill_before_observed_mid_adverse < 0) {
                side.fill_before_observed_mid_adverse = 1;
            }
            ++(s == kBid ? current_segment_.bid_fills : current_segment_.ask_fills);
            refresh_next_deadline(row);
            min_deadline_ns_ = std::min(min_deadline_ns_, row.next_deadline_ns);
        }
    }
}

void NativeDatasetExporter::after_record(
    std::uint64_t local_ns,
    std::int64_t latest_exchange_ms,
    const book::OrderBook& book,
    bool live) {
    book_live_ = live;
    latest_exchange_ms_ = latest_exchange_ms;
    last_record_ns_ = local_ns;
    if (in_segment_) {
        if (usable()) {
            resolve_pending(local_ns, local_ns, book);
            advance_grid(local_ns, true, book);
            resolve_pending(local_ns, local_ns, book);
            current_segment_.last_exchange_time_ms = latest_exchange_ms_;
        } else {
            close_segment(local_ns, book_live_ ? "trade_stream_down" : "depth_desync");
        }
        return;
    }
    if (usable() && book.is_valid()) {
        open_segment(local_ns, book);
    }
}

void NativeDatasetExporter::finish(const book::OrderBook& book) {
    (void)book;
    if (in_segment_) {
        close_segment(std::max(last_record_ns_, segment_start_ns_), "end_of_input");
    }
    if (summary_.segments == 0) {
        summary_.missing_initial_sync = 1;
    }
    if (tail_unsynced_depth_events_ > 0) {
        summary_.unrecoverable_gaps = 1;
    }
    writer_->close();
    closed_ = true;
}

}  // namespace crypto::research
