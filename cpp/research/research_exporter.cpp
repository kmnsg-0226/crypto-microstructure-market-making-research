#include "research/research_exporter.hpp"

#include <algorithm>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <string>
#include "research/compressed_text_writer.hpp"
#include "research/microstructure_features.hpp"

namespace crypto::research {

namespace {

std::string csv_header() {
    std::ostringstream out;
    out << "timestamp_ns,exchange_timestamp_ms,sequence_update_id,valid_book_state"
        << ",best_bid_price,best_bid_qty,best_ask_price,best_ask_qty,spread,mid";
    for (int i = 1; i <= 10; ++i) {
        out << ",bid_px_" << i << ",bid_qty_" << i;
    }
    for (int i = 1; i <= 10; ++i) {
        out << ",ask_px_" << i << ",ask_qty_" << i;
    }
    out << ",last_trade_price,recent_buy_volume,recent_sell_volume\n";
    return out.str();
}

}  // namespace

ResearchSnapshotExporter::ResearchSnapshotExporter(
    const std::filesystem::path& path,
    const binance::InstrumentInfo& instrument,
    std::uint64_t sample_interval_ms,
    std::uint64_t recent_trade_window_ms,
    std::uint64_t start_ns,
    std::uint64_t end_ns,
    bool include_invalid)
    : instrument_(instrument),
      writer_(std::make_unique<CompressedTextWriter>(path)),
      interval_ns_(sample_interval_ms * 1'000'000ULL),
      trade_window_ns_(recent_trade_window_ms * 1'000'000ULL),
      start_ns_(start_ns),
      end_ns_(end_ns),
      include_invalid_(include_invalid) {
    if (sample_interval_ms == 0 || recent_trade_window_ms == 0) {
        throw std::invalid_argument("research sampling and trade windows must be positive");
    }
    if (end_ns_ <= start_ns_) {
        throw std::invalid_argument("research export end must be after start");
    }
    writer_->append(csv_header());
}

ResearchSnapshotExporter::~ResearchSnapshotExporter() = default;

void ResearchSnapshotExporter::initialize_grid(std::uint64_t local_system_ns) {
    if (next_sample_ns_) {
        return;
    }
    const auto first = std::max(local_system_ns, start_ns_);
    const auto quotient = first / interval_ns_;
    const auto remainder = first % interval_ns_;
    next_sample_ns_ = remainder == 0 ? first : (quotient + 1U) * interval_ns_;
}

void ResearchSnapshotExporter::prune_trades(std::uint64_t sample_ns) {
    const auto cutoff = sample_ns > trade_window_ns_ ? sample_ns - trade_window_ns_ : 0;
    while (!volumes_.empty() && volumes_.front().local_system_ns < cutoff) {
        recent_buy_lots_ -= volumes_.front().buy_lots;
        recent_sell_lots_ -= volumes_.front().sell_lots;
        volumes_.pop_front();
    }
}

void ResearchSnapshotExporter::write_sample(
    std::uint64_t sample_ns,
    std::int64_t exchange_time_ms,
    const book::OrderBook& book,
    bool synchronized) {
    const bool valid = synchronized && book.is_valid();
    if (!valid && !include_invalid_) {
        return;
    }
    prune_trades(sample_ns);
    // The native Binance replay and Tardis replay share this exact top-level feature engine.
    // The legacy export schema remains unchanged; richer fields are emitted by the historical
    // daily exporter without redefining their formulas.
    const auto instant = calculate_instant_book_features(book, 0.5);
    const auto bid_count = book.is_valid() ? std::min<std::size_t>(book.bids().size(), 10) : 0;
    const auto ask_count = book.is_valid() ? std::min<std::size_t>(book.asks().size(), 10) : 0;
    const auto bid = book.best_bid();
    const auto ask = book.best_ask();
    const auto mid_x2 = book.mid_ticks_x2();
    const auto spread = book.spread_ticks();
    std::ostringstream out;
    out << sample_ns << ',' << exchange_time_ms << ',' << book.last_update_id()
        << ',' << (valid ? 1 : 0)
        << ',' << (bid ? instrument_.ticks_to_price(*bid) : "")
        << ',' << (bid_count ? instrument_.lots_to_qty(instant.bids[0].qty_lots) : "")
        << ',' << (ask ? instrument_.ticks_to_price(*ask) : "")
        << ',' << (ask_count ? instrument_.lots_to_qty(instant.asks[0].qty_lots) : "")
        << ',' << (spread ? instrument_.ticks_to_price(*spread) : "")
        << ',' << (mid_x2 ? instrument_.ticks_x2_to_price(*mid_x2) : "");
    for (std::size_t i = 0; i < 10; ++i) {
        if (i < bid_count) {
            out << ',' << instrument_.ticks_to_price(instant.bids[i].price_ticks)
                << ',' << instrument_.lots_to_qty(instant.bids[i].qty_lots);
        } else {
            out << ",,";
        }
    }
    for (std::size_t i = 0; i < 10; ++i) {
        if (i < ask_count) {
            out << ',' << instrument_.ticks_to_price(instant.asks[i].price_ticks)
                << ',' << instrument_.lots_to_qty(instant.asks[i].qty_lots);
        } else {
            out << ",,";
        }
    }

    out << ',' << (last_trade_price_ticks_ ? instrument_.ticks_to_price(*last_trade_price_ticks_) : "")
        << ',' << instrument_.lots_to_qty(recent_buy_lots_)
        << ',' << instrument_.lots_to_qty(recent_sell_lots_)
        << '\n';
    writer_->append(out.str());
    ++rows_written_;
    if (valid) {
        ++valid_rows_;
        previous_sample_invalid_ = false;
    } else {
        ++invalid_rows_;
        if (!previous_sample_invalid_) {
            ++invalid_intervals_;
        }
        previous_sample_invalid_ = true;
    }
}

void ResearchSnapshotExporter::advance_before_event(
    std::uint64_t event_local_system_ns,
    std::int64_t latest_exchange_time_ms,
    const book::OrderBook& book,
    bool synchronized) {
    initialize_grid(event_local_system_ns);
    while (*next_sample_ns_ < event_local_system_ns && *next_sample_ns_ < end_ns_) {
        write_sample(*next_sample_ns_, latest_exchange_time_ms, book, synchronized);
        *next_sample_ns_ += interval_ns_;
    }
}

void ResearchSnapshotExporter::sample_exact_event_time(
    std::uint64_t event_local_system_ns,
    std::int64_t latest_exchange_time_ms,
    const book::OrderBook& book,
    bool synchronized) {
    initialize_grid(event_local_system_ns);
    if (*next_sample_ns_ == event_local_system_ns && *next_sample_ns_ < end_ns_) {
        write_sample(*next_sample_ns_, latest_exchange_time_ms, book, synchronized);
        *next_sample_ns_ += interval_ns_;
    }
}

void ResearchSnapshotExporter::on_trade(
    const binance::AggTradeEvent& event,
    std::uint64_t local_system_ns) {
    SignedVolume volume;
    volume.local_system_ns = local_system_ns;
    if (event.is_aggressive_buy()) {
        volume.buy_lots = event.qty_lots;
        recent_buy_lots_ += event.qty_lots;
    } else {
        volume.sell_lots = event.qty_lots;
        recent_sell_lots_ += event.qty_lots;
    }
    volumes_.push_back(volume);
    last_trade_price_ticks_ = event.price_ticks;
}

void ResearchSnapshotExporter::finish_at(
    std::uint64_t end_ns,
    std::int64_t latest_exchange_time_ms,
    const book::OrderBook& book,
    bool synchronized) {
    initialize_grid(end_ns);
    const auto limit = std::min(end_ns, end_ns_);
    while (*next_sample_ns_ < limit) {
        write_sample(*next_sample_ns_, latest_exchange_time_ms, book, synchronized);
        *next_sample_ns_ += interval_ns_;
    }
}

void ResearchSnapshotExporter::close() {
    writer_->close();
}

}  // namespace crypto::research
