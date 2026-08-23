#include "research/tardis_daily_exporter.hpp"

#include <algorithm>
#include <cmath>
#include <iomanip>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>

#include "research/compressed_text_writer.hpp"
#include "research/microstructure_features.hpp"

namespace crypto::research {
namespace {

std::string header() {
    std::ostringstream out;
    out << "schema_version,date,sample_time_us,latest_book_event_time_us,latest_book_local_time_us"
        << ",latest_trade_time_us,latest_trade_local_time_us,valid_book_state,book_segment_id"
        << ",seconds_since_last_book_event,seconds_since_last_trade"
        << ",best_bid_price,best_bid_qty,best_ask_price,best_ask_qty,spread_ticks,mid,mid_ticks_x2";
    for (int i = 1; i <= 10; ++i) {
        out << ",bid_px_" << i << ",bid_qty_" << i;
    }
    for (int i = 1; i <= 10; ++i) {
        out << ",ask_px_" << i << ",ask_qty_" << i;
    }
    out << ",total_bid_depth_l5,total_ask_depth_l5,total_bid_depth_l10,total_ask_depth_l10"
        << ",bid_ask_depth_ratio_l5,bid_ask_depth_ratio_l10"
        << ",obi_l1,obi_l5,obi_l10,weighted_obi_l1,weighted_obi_l5,weighted_obi_l10"
        << ",weighted_mid,weighted_mid_minus_mid,weighted_mid_minus_mid_ticks"
        << ",book_message_count_100ms,book_level_update_count_100ms,ofi_100ms_bucket"
        << ",aggressive_buy_qty_100ms,aggressive_sell_qty_100ms"
        << ",aggressive_buy_notional_100ms,aggressive_sell_notional_100ms"
        << ",trade_count_100ms,buy_trade_count_100ms,sell_trade_count_100ms,trade_volume_100ms\n";
    return out.str();
}

std::string fixed(double value, int precision = 12) {
    std::ostringstream out;
    out << std::fixed << std::setprecision(precision) << value;
    auto result = out.str();
    while (result.size() > 1 && result.back() == '0') {
        result.pop_back();
    }
    if (!result.empty() && result.back() == '.') {
        result.push_back('0');
    }
    return result;
}

std::string optional_double(const std::optional<double>& value) {
    return value ? fixed(*value) : std::string{};
}

}  // namespace

TardisDailyResearchExporter::TardisDailyResearchExporter(
    const std::filesystem::path& path,
    std::string date,
    std::uint64_t day_start_us,
    std::uint64_t day_end_us,
    std::uint64_t interval_us,
    const binance::DecimalIncrement& price_increment,
    const binance::DecimalIncrement& qty_increment,
    const historical::TardisTradeDay& trades,
    double weighted_obi_lambda)
    : date_(std::move(date)),
      day_start_us_(day_start_us),
      day_end_us_(day_end_us),
      interval_us_(interval_us),
      price_increment_(price_increment),
      qty_increment_(qty_increment),
      trades_(trades),
      weighted_obi_lambda_(weighted_obi_lambda),
      tick_size_value_(std::stod(price_increment.text())),
      notional_unit_value_(std::stod(price_increment.text()) * std::stod(qty_increment.text())),
      writer_(std::make_unique<CompressedTextWriter>(path)) {
    if (day_end_us_ <= day_start_us_ || interval_us_ == 0 ||
        (day_end_us_ - day_start_us_) % interval_us_ != 0 ||
        trades_.buckets.size() != (day_end_us_ - day_start_us_) / interval_us_ ||
        weighted_obi_lambda_ < 0.0) {
        throw std::invalid_argument("invalid Tardis daily research export configuration");
    }
    writer_->append(header());
}

TardisDailyResearchExporter::~TardisDailyResearchExporter() = default;

void TardisDailyResearchExporter::before_book_event(
    std::uint64_t event_timestamp_us,
    bool replacement_snapshot,
    const book::OrderBook& book) {
    if (previous_book_event_timestamp_us_ != 0 &&
        event_timestamp_us < previous_book_event_timestamp_us_) {
        throw std::invalid_argument("book exchange timestamp regressed during research export");
    }
    if (replacement_snapshot) {
        if (previous_book_event_timestamp_us_ != 0) {
            emit_through(previous_book_event_timestamp_us_, book, observable_);
        }
        emit_before(event_timestamp_us, book, false);
        observable_ = false;
        previous_best_.reset();
    } else {
        emit_before(event_timestamp_us, book, observable_);
    }
}

void TardisDailyResearchExporter::after_book_event(
    std::uint64_t event_timestamp_us,
    std::uint64_t event_local_timestamp_us,
    std::size_t level_updates,
    bool snapshot,
    const book::OrderBook& book) {
    latest_book_timestamp_us_ = event_timestamp_us;
    latest_book_local_timestamp_us_ = event_local_timestamp_us;
    previous_book_event_timestamp_us_ = event_timestamp_us;
    ++bucket_book_messages_;
    bucket_level_updates_ += level_updates;
    ++summary_.book_messages;
    summary_.book_level_updates += level_updates;

    const auto current = best_level_state(book);
    if (snapshot) {
        ++segment_id_;
        ++summary_.book_segments;
        observable_ = current.has_value();
        previous_best_ = current;
        bucket_ofi_lots_ = 0;
    } else if (observable_ && previous_best_ && current) {
        bucket_ofi_lots_ += l1_ofi_contribution(*previous_best_, *current);
        previous_best_ = current;
    } else {
        observable_ = false;
        previous_best_.reset();
    }
}

void TardisDailyResearchExporter::emit_before(
    std::uint64_t exclusive_timestamp_us,
    const book::OrderBook& book,
    bool observable) {
    while (next_index_ < trades_.buckets.size() &&
           day_start_us_ + next_index_ * interval_us_ < exclusive_timestamp_us) {
        write_sample(book, observable);
    }
}

void TardisDailyResearchExporter::emit_through(
    std::uint64_t inclusive_timestamp_us,
    const book::OrderBook& book,
    bool observable) {
    while (next_index_ < trades_.buckets.size() &&
           day_start_us_ + next_index_ * interval_us_ <= inclusive_timestamp_us) {
        write_sample(book, observable);
    }
}

void TardisDailyResearchExporter::write_sample(const book::OrderBook& book, bool observable) {
    const auto sample_time_us = day_start_us_ + next_index_ * interval_us_;
    const auto& trade = trades_.buckets[next_index_];
    if (trade.latest_timestamp_us != 0) {
        latest_trade_timestamp_us_ = trade.latest_timestamp_us;
        latest_trade_local_timestamp_us_ = trade.latest_local_timestamp_us;
    }
    const auto features = calculate_instant_book_features(book, weighted_obi_lambda_);
    const bool valid = observable && book.is_valid() && features.has_l10;
    std::ostringstream out;
    out << "tardis-100ms-v1," << date_ << ',' << sample_time_us << ','
        << (latest_book_timestamp_us_ ? std::to_string(latest_book_timestamp_us_) : "") << ','
        << (latest_book_local_timestamp_us_ ? std::to_string(latest_book_local_timestamp_us_) : "") << ','
        << (latest_trade_timestamp_us_ ? std::to_string(latest_trade_timestamp_us_) : "") << ','
        << (latest_trade_local_timestamp_us_ ? std::to_string(latest_trade_local_timestamp_us_) : "") << ','
        << (valid ? 1 : 0) << ',' << (valid ? std::to_string(segment_id_) : "") << ','
        << (latest_book_timestamp_us_ && sample_time_us >= latest_book_timestamp_us_
                ? fixed(static_cast<double>(sample_time_us - latest_book_timestamp_us_) / 1'000'000.0, 6)
                : "")
        << ','
        << (latest_trade_timestamp_us_ && sample_time_us >= latest_trade_timestamp_us_
                ? fixed(static_cast<double>(sample_time_us - latest_trade_timestamp_us_) / 1'000'000.0, 6)
                : "");

    if (valid) {
        const auto bid = features.bids[0];
        const auto ask = features.asks[0];
        out << ',' << price_increment_.from_steps(bid.price_ticks)
            << ',' << qty_increment_.from_steps(bid.qty_lots)
            << ',' << price_increment_.from_steps(ask.price_ticks)
            << ',' << qty_increment_.from_steps(ask.qty_lots)
            << ',' << (ask.price_ticks - bid.price_ticks)
            << ',' << price_increment_.from_half_steps(bid.price_ticks + ask.price_ticks)
            << ',' << (bid.price_ticks + ask.price_ticks);
        for (const auto& level : features.bids) {
            out << ',' << price_increment_.from_steps(level.price_ticks)
                << ',' << qty_increment_.from_steps(level.qty_lots);
        }
        for (const auto& level : features.asks) {
            out << ',' << price_increment_.from_steps(level.price_ticks)
                << ',' << qty_increment_.from_steps(level.qty_lots);
        }
        const auto ratio_l5 = features.total_ask_depth_l5 > 0
            ? std::optional<double>(static_cast<double>(features.total_bid_depth_l5) /
                                    static_cast<double>(features.total_ask_depth_l5))
            : std::nullopt;
        const auto ratio_l10 = features.total_ask_depth_l10 > 0
            ? std::optional<double>(static_cast<double>(features.total_bid_depth_l10) /
                                    static_cast<double>(features.total_ask_depth_l10))
            : std::nullopt;
        const auto weighted_mid_price = features.weighted_mid_ticks
            ? std::optional<double>(*features.weighted_mid_ticks * tick_size_value_)
            : std::nullopt;
        const auto weighted_mid_minus_price = features.weighted_mid_minus_mid_ticks
            ? std::optional<double>(*features.weighted_mid_minus_mid_ticks * tick_size_value_)
            : std::nullopt;
        out << ',' << qty_increment_.from_steps(features.total_bid_depth_l5)
            << ',' << qty_increment_.from_steps(features.total_ask_depth_l5)
            << ',' << qty_increment_.from_steps(features.total_bid_depth_l10)
            << ',' << qty_increment_.from_steps(features.total_ask_depth_l10)
            << ',' << optional_double(ratio_l5)
            << ',' << optional_double(ratio_l10)
            << ',' << optional_double(features.obi_l1)
            << ',' << optional_double(features.obi_l5)
            << ',' << optional_double(features.obi_l10)
            << ',' << optional_double(features.weighted_obi_l1)
            << ',' << optional_double(features.weighted_obi_l5)
            << ',' << optional_double(features.weighted_obi_l10)
            << ',' << optional_double(weighted_mid_price)
            << ',' << optional_double(weighted_mid_minus_price)
            << ',' << optional_double(features.weighted_mid_minus_mid_ticks);
    } else {
        for (int i = 0; i < 62; ++i) {
            out << ',';
        }
    }

    const auto trade_volume = trade.aggressive_buy_qty_lots + trade.aggressive_sell_qty_lots;
    out << ',' << bucket_book_messages_
        << ',' << bucket_level_updates_
        << ',' << qty_increment_.from_steps(bucket_ofi_lots_)
        << ',' << qty_increment_.from_steps(trade.aggressive_buy_qty_lots)
        << ',' << qty_increment_.from_steps(trade.aggressive_sell_qty_lots)
        << ',' << fixed(
            static_cast<double>(trade.aggressive_buy_notional_units) * notional_unit_value_, 8)
        << ',' << fixed(
            static_cast<double>(trade.aggressive_sell_notional_units) * notional_unit_value_, 8)
        << ',' << trade.trade_count
        << ',' << trade.buy_trade_count
        << ',' << trade.sell_trade_count
        << ',' << qty_increment_.from_steps(trade_volume)
        << '\n';
    writer_->append(out.str());

    if (summary_.rows == 0) {
        summary_.first_sample_time_us = sample_time_us;
    }
    summary_.last_sample_time_us = sample_time_us;
    ++summary_.rows;
    if (valid) {
        ++summary_.valid_rows;
    } else {
        ++summary_.invalid_rows;
    }
    bucket_book_messages_ = 0;
    bucket_level_updates_ = 0;
    bucket_ofi_lots_ = 0;
    ++next_index_;
}

void TardisDailyResearchExporter::finish(const book::OrderBook& book) {
    if (previous_book_event_timestamp_us_ != 0) {
        emit_through(previous_book_event_timestamp_us_, book, observable_);
    }
    while (next_index_ < trades_.buckets.size()) {
        // Reaching the normal daily file boundary is not itself a disconnect. Keep the last
        // valid completed state observable; explicit replacement-snapshot gaps were already
        // invalidated in before_book_event().
        write_sample(book, observable_);
    }
    summary_.trades = trades_.rows;
    summary_.final_book_observable = observable_;
}

void TardisDailyResearchExporter::close() {
    if (!closed_) {
        writer_->close();
        closed_ = true;
    }
}

}  // namespace crypto::research
