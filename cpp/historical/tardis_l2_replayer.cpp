#include "historical/tardis_l2_replayer.hpp"

#include <array>
#include <charconv>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "book/order_book.hpp"
#include "exchange/binance/instrument_info.hpp"
#include "exchange/binance/message_types.hpp"
#include "historical/gzip_line_reader.hpp"

namespace crypto::historical {
namespace {

constexpr std::string_view expected_header =
    "exchange,symbol,timestamp,local_timestamp,is_snapshot,side,price,amount";

void hash_u64(std::uint64_t value, std::uint64_t& hash) noexcept {
    constexpr std::uint64_t prime = 1099511628211ULL;
    for (int i = 0; i < 8; ++i) {
        hash ^= value & 0xffU;
        hash *= prime;
        value >>= 8U;
    }
}

std::uint64_t parse_u64(std::string_view value, const char* field) {
    std::uint64_t result{};
    const auto [end, error] = std::from_chars(value.data(), value.data() + value.size(), result);
    if (error != std::errc{} || end != value.data() + value.size()) {
        throw std::invalid_argument(std::string("invalid ") + field);
    }
    return result;
}

std::array<std::string_view, 8> split_row(std::string_view line) {
    std::array<std::string_view, 8> fields;
    std::size_t start = 0;
    for (std::size_t i = 0; i < fields.size() - 1; ++i) {
        const auto comma = line.find(',', start);
        if (comma == std::string_view::npos) {
            throw std::invalid_argument("CSV row has fewer than eight columns");
        }
        fields[i] = line.substr(start, comma - start);
        start = comma + 1;
    }
    if (line.find(',', start) != std::string_view::npos) {
        throw std::invalid_argument("CSV row has more than eight columns");
    }
    fields.back() = line.substr(start);
    return fields;
}

struct ParsedRow {
    std::uint64_t timestamp_us{};
    std::uint64_t local_timestamp_us{};
    bool is_snapshot{};
    bool is_bid{};
    binance::PriceLevel level;
};

void classify_invalid_book(const book::OrderBook& book, TardisReplaySummary& summary) {
    const bool empty_bid = book.bids().empty();
    const bool empty_ask = book.asks().empty();
    if (empty_bid) {
        ++summary.empty_bid_states;
    }
    if (empty_ask) {
        ++summary.empty_ask_states;
    }
    if (!empty_bid && !empty_ask && book.bids().begin()->first >= book.asks().begin()->first) {
        ++summary.crossed_book_states;
    }
}

void capture_bbo(
    const book::OrderBook& book,
    std::uint64_t local_timestamp_us,
    TardisReplaySummary& summary,
    std::uint64_t& replay_hash) {
    if (!book.is_valid()) {
        ++summary.invalid_states;
        hash_u64(local_timestamp_us, replay_hash);
        hash_u64(0, replay_hash);
        return;
    }

    ++summary.valid_states;
    const auto& [bid_price, bid_qty] = *book.bids().begin();
    const auto& [ask_price, ask_qty] = *book.asks().begin();
    if (summary.first_bbo_local_timestamp_us == 0) {
        summary.first_bbo_local_timestamp_us = local_timestamp_us;
        summary.first_bid_ticks = bid_price;
        summary.first_bid_qty_lots = bid_qty;
        summary.first_ask_ticks = ask_price;
        summary.first_ask_qty_lots = ask_qty;
    }
    summary.last_bbo_local_timestamp_us = local_timestamp_us;
    summary.last_bid_ticks = bid_price;
    summary.last_bid_qty_lots = bid_qty;
    summary.last_ask_ticks = ask_price;
    summary.last_ask_qty_lots = ask_qty;

    hash_u64(local_timestamp_us, replay_hash);
    hash_u64(1, replay_hash);
    hash_u64(static_cast<std::uint64_t>(bid_price), replay_hash);
    hash_u64(static_cast<std::uint64_t>(bid_qty), replay_hash);
    hash_u64(static_cast<std::uint64_t>(ask_price), replay_hash);
    hash_u64(static_cast<std::uint64_t>(ask_qty), replay_hash);
}

}  // namespace

TardisL2Replayer::TardisL2Replayer(std::string tick_size, std::string step_size)
    : tick_size_(std::move(tick_size)), step_size_(std::move(step_size)) {}

TardisReplaySummary TardisL2Replayer::replay(const std::filesystem::path& input_path) const {
    return replay_impl(input_path, nullptr);
}

TardisResearchRunSummary TardisL2Replayer::replay_research_day(
    const std::filesystem::path& l2_path,
    const std::filesystem::path& trades_path,
    const std::filesystem::path& output_path,
    std::string date,
    std::uint64_t day_start_us,
    std::uint64_t day_end_us,
    std::uint64_t interval_us,
    double weighted_obi_lambda) const {
    const binance::DecimalIncrement price_increment(tick_size_);
    const binance::DecimalIncrement qty_increment(step_size_);
    const auto trades = read_tardis_trade_day(
        trades_path,
        day_start_us,
        day_end_us,
        interval_us,
        price_increment,
        qty_increment);
    if (trades.unknown_side_trades != 0 || trades.timestamp_regressions != 0 ||
        trades.local_timestamp_regressions != 0) {
        throw std::invalid_argument("Tardis trades failed aggressor or ordering validation");
    }
    research::TardisDailyResearchExporter exporter(
        output_path,
        std::move(date),
        day_start_us,
        day_end_us,
        interval_us,
        price_increment,
        qty_increment,
        trades,
        weighted_obi_lambda);
    TardisResearchRunSummary result;
    result.l2 = replay_impl(l2_path, &exporter);
    exporter.close();
    result.daily_export = exporter.summary();
    result.trade_rows = trades.rows;
    result.buy_trades = trades.buy_trades;
    result.sell_trades = trades.sell_trades;
    result.unknown_side_trades = trades.unknown_side_trades;
    result.trade_timestamp_regressions = trades.timestamp_regressions;
    result.trade_local_timestamp_regressions = trades.local_timestamp_regressions;
    result.trades_outside_event_day = trades.outside_event_day_rows;
    return result;
}

TardisReplaySummary TardisL2Replayer::replay_impl(
    const std::filesystem::path& input_path,
    research::TardisDailyResearchExporter* exporter) const {
    const binance::DecimalIncrement price_increment(tick_size_);
    const binance::DecimalIncrement qty_increment(step_size_);
    book::OrderBook book;
    TardisReplaySummary summary;
    summary.input_path = input_path;

    bool saw_header = false;
    bool have_previous_row = false;
    bool previous_row_snapshot = false;
    std::uint64_t previous_timestamp_us{};
    std::uint64_t previous_local_timestamp_us{};
    std::uint64_t previous_message_local_timestamp_us{};
    std::uint64_t synthetic_update_id{};
    std::uint64_t replay_hash = 1469598103934665603ULL;

    bool delta_open = false;
    std::uint64_t delta_timestamp_us{};
    std::uint64_t delta_local_timestamp_us{};
    binance::DepthEvent delta;

    bool snapshot_open = false;
    TardisSnapshotSummary snapshot_info;
    binance::DepthSnapshot snapshot;

    auto finish_delta = [&] {
        if (!delta_open) {
            return;
        }
        ++summary.delta_messages;
        delta.first_update_id = ++synthetic_update_id;
        delta.final_update_id = synthetic_update_id;
        delta.previous_final_update_id = synthetic_update_id - 1;
        if (exporter != nullptr) {
            exporter->before_book_event(delta_timestamp_us, false, book);
        }
        if (!summary.saw_snapshot) {
            ++summary.pre_snapshot_messages;
        } else if (!book.is_valid()) {
            ++summary.skipped_invalid_messages;
            ++summary.invalid_states;
            hash_u64(delta_local_timestamp_us, replay_hash);
            hash_u64(0, replay_hash);
        } else if (!book.apply_depth_event(delta)) {
            classify_invalid_book(book, summary);
            capture_bbo(book, delta_local_timestamp_us, summary, replay_hash);
        } else {
            capture_bbo(book, delta_local_timestamp_us, summary, replay_hash);
        }
        if (exporter != nullptr) {
            exporter->after_book_event(
                delta_timestamp_us,
                delta_local_timestamp_us,
                delta.bids.size() + delta.asks.size(),
                false,
                book);
        }
        previous_message_local_timestamp_us = delta_local_timestamp_us;
        delta = {};
        delta_open = false;
    };

    auto finish_snapshot = [&] {
        if (!snapshot_open) {
            return;
        }
        snapshot.last_update_id = ++synthetic_update_id;
        snapshot.event_time_ms = static_cast<std::int64_t>(snapshot_info.end_timestamp_us / 1000U);
        snapshot.transaction_time_ms = snapshot.event_time_ms;
        if (exporter != nullptr) {
            exporter->before_book_event(snapshot_info.end_timestamp_us, true, book);
        }
        snapshot_info.valid = book.load_snapshot(snapshot);
        if (!snapshot_info.valid) {
            ++summary.snapshot_load_failures;
            classify_invalid_book(book, summary);
        }
        summary.saw_snapshot = true;
        ++summary.snapshot_groups;
        if (snapshot_info.previous_local_timestamp_us != 0 &&
            snapshot_info.end_local_timestamp_us > snapshot_info.previous_local_timestamp_us) {
            summary.conservative_reconnect_gap_us +=
                snapshot_info.end_local_timestamp_us - snapshot_info.previous_local_timestamp_us;
        }
        if (snapshot_info.valid) {
            snapshot_info.best_bid_ticks = book.bids().begin()->first;
            snapshot_info.best_ask_ticks = book.asks().begin()->first;
        }
        capture_bbo(book, snapshot_info.end_local_timestamp_us, summary, replay_hash);
        if (exporter != nullptr) {
            exporter->after_book_event(
                snapshot_info.end_timestamp_us,
                snapshot_info.end_local_timestamp_us,
                snapshot_info.rows,
                true,
                book);
        }
        previous_message_local_timestamp_us = snapshot_info.end_local_timestamp_us;
        summary.snapshots.push_back(snapshot_info);
        snapshot = {};
        snapshot_info = {};
        snapshot_open = false;
    };

    for_each_gzip_line(input_path, [&](std::string_view line) {
        if (!saw_header) {
            if (line != expected_header) {
                throw std::invalid_argument("unexpected Tardis CSV header");
            }
            saw_header = true;
            return;
        }
        if (line.empty()) {
            return;
        }

        ParsedRow row;
        try {
            const auto fields = split_row(line);
            if (fields[0] != "binance-futures" || fields[1] != "BTCUSDT") {
                throw std::invalid_argument("unexpected exchange or symbol");
            }
            row.timestamp_us = parse_u64(fields[2], "timestamp");
            row.local_timestamp_us = parse_u64(fields[3], "local_timestamp");
            if (fields[4] == "true") {
                row.is_snapshot = true;
            } else if (fields[4] != "false") {
                throw std::invalid_argument("invalid is_snapshot");
            }
            if (fields[5] == "bid") {
                row.is_bid = true;
            } else if (fields[5] != "ask") {
                throw std::invalid_argument("invalid side");
            }
            row.level.price_ticks = price_increment.to_steps(fields[6]);
            row.level.qty_lots = qty_increment.to_steps(fields[7]);
        } catch (const std::exception& error) {
            ++summary.parse_failures;
            throw std::invalid_argument(
                "Tardis CSV parse failure at data row " + std::to_string(summary.rows + 1) +
                ": " + error.what());
        }

        ++summary.rows;
        if (summary.rows == 1) {
            summary.first_timestamp_us = row.timestamp_us;
            summary.first_local_timestamp_us = row.local_timestamp_us;
            summary.message_groups = 1;
        } else {
            if (row.timestamp_us < previous_timestamp_us) {
                ++summary.exchange_timestamp_regressions;
            }
            if (row.local_timestamp_us < previous_local_timestamp_us) {
                ++summary.local_timestamp_regressions;
            }
            if (row.local_timestamp_us != previous_local_timestamp_us) {
                ++summary.message_groups;
                if (row.local_timestamp_us > previous_local_timestamp_us) {
                    const auto gap = row.local_timestamp_us - previous_local_timestamp_us;
                    if (gap > summary.max_local_gap_us) {
                        summary.max_local_gap_us = gap;
                        summary.max_local_gap_ending_us = row.local_timestamp_us;
                    }
                }
            } else if (row.is_snapshot != previous_row_snapshot) {
                ++summary.mixed_snapshot_flag_messages;
            }
        }
        summary.last_timestamp_us = row.timestamp_us;
        summary.last_local_timestamp_us = row.local_timestamp_us;

        hash_u64(row.timestamp_us, replay_hash);
        hash_u64(row.local_timestamp_us, replay_hash);
        hash_u64(row.is_snapshot ? 1U : 0U, replay_hash);
        hash_u64(row.is_bid ? 1U : 0U, replay_hash);
        hash_u64(static_cast<std::uint64_t>(row.level.price_ticks), replay_hash);
        hash_u64(static_cast<std::uint64_t>(row.level.qty_lots), replay_hash);

        if (row.is_snapshot) {
            ++summary.snapshot_rows;
            if (!previous_row_snapshot) {
                finish_delta();
                snapshot_open = true;
                snapshot_info.start_timestamp_us = row.timestamp_us;
                snapshot_info.start_local_timestamp_us = row.local_timestamp_us;
                snapshot_info.previous_local_timestamp_us = previous_message_local_timestamp_us;
            }
            snapshot_info.end_timestamp_us = row.timestamp_us;
            snapshot_info.end_local_timestamp_us = row.local_timestamp_us;
            ++snapshot_info.rows;
            if (row.is_bid) {
                snapshot.bids.push_back(row.level);
            } else {
                snapshot.asks.push_back(row.level);
            }
        } else {
            ++summary.delta_rows;
            if (previous_row_snapshot) {
                finish_snapshot();
            }
            if (!delta_open || row.local_timestamp_us != delta_local_timestamp_us) {
                finish_delta();
                delta_open = true;
                delta_timestamp_us = row.timestamp_us;
                delta_local_timestamp_us = row.local_timestamp_us;
                delta.symbol = "BTCUSDT";
                delta.event_time_ms = static_cast<std::int64_t>(row.timestamp_us / 1000U);
                delta.transaction_time_ms = delta.event_time_ms;
            } else {
                delta_timestamp_us = std::max(delta_timestamp_us, row.timestamp_us);
            }
            if (!summary.saw_snapshot) {
                ++summary.pre_snapshot_rows;
            }
            if (row.is_bid) {
                delta.bids.push_back(row.level);
            } else {
                delta.asks.push_back(row.level);
            }
        }

        have_previous_row = true;
        previous_row_snapshot = row.is_snapshot;
        previous_timestamp_us = row.timestamp_us;
        previous_local_timestamp_us = row.local_timestamp_us;
    });

    if (!saw_header) {
        throw std::invalid_argument("empty Tardis CSV input");
    }
    if (snapshot_open) {
        finish_snapshot();
    }
    finish_delta();

    if (!have_previous_row) {
        throw std::invalid_argument("Tardis CSV contains no data rows");
    }
    summary.final_book_valid = book.is_valid();
    summary.final_book_checksum = book.checksum();
    summary.replay_checksum = replay_hash;
    if (exporter != nullptr) {
        exporter->finish(book);
    }
    return summary;
}

}  // namespace crypto::historical
