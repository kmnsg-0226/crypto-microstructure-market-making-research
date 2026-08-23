#include "historical/tardis_event_replayer.hpp"

#include <algorithm>
#include <array>
#include <charconv>
#include <stdexcept>
#include <string_view>

#include "historical/gzip_line_reader.hpp"

namespace crypto::historical {
namespace {

constexpr std::string_view l2_header =
    "exchange,symbol,timestamp,local_timestamp,is_snapshot,side,price,amount";
constexpr std::string_view trade_header =
    "exchange,symbol,timestamp,local_timestamp,id,side,price,amount";

std::array<std::string_view, 8> split(std::string_view line) {
    std::array<std::string_view, 8> fields;
    std::size_t start = 0;
    for (std::size_t index = 0; index < fields.size() - 1; ++index) {
        const auto comma = line.find(',', start);
        if (comma == std::string_view::npos) {
            throw std::invalid_argument("event adapter CSV row has fewer than eight columns");
        }
        fields[index] = line.substr(start, comma - start);
        start = comma + 1;
    }
    if (line.find(',', start) != std::string_view::npos) {
        throw std::invalid_argument("event adapter CSV row has more than eight columns");
    }
    fields.back() = line.substr(start);
    return fields;
}

std::uint64_t parse_u64(std::string_view value) {
    std::uint64_t result{};
    const auto [end, error] = std::from_chars(value.data(), value.data() + value.size(), result);
    if (error != std::errc{} || end != value.data() + value.size()) {
        throw std::invalid_argument("invalid event adapter timestamp");
    }
    return result;
}

struct L2Row {
    std::uint64_t exchange_time_us{};
    std::uint64_t local_time_us{};
    bool snapshot{};
    bool bid{};
    binance::PriceLevel level;
};

}  // namespace

TardisEventTradeFile read_tardis_event_trade_file(
    const std::filesystem::path& path,
    const binance::DecimalIncrement& price_increment,
    const binance::DecimalIncrement& qty_increment) {
    TardisEventTradeFile result;
    bool header = false;
    std::uint64_t previous_exchange{};
    std::uint64_t previous_local{};
    for_each_gzip_line(path, [&](std::string_view line) {
        if (!header) {
            if (line != trade_header) throw std::invalid_argument("unexpected Tardis trade header");
            header = true;
            return;
        }
        if (line.empty()) return;
        auto trade = parse_tardis_trade_row(line, price_increment, qty_increment);
        if (!result.trades.empty()) {
            result.timestamp_regressions += trade.timestamp_us < previous_exchange;
            result.local_timestamp_regressions += trade.local_timestamp_us < previous_local;
        }
        previous_exchange = trade.timestamp_us;
        previous_local = trade.local_timestamp_us;
        if (trade.side == AggressorSide::Buy) ++result.buy_trades;
        else if (trade.side == AggressorSide::Sell) ++result.sell_trades;
        else ++result.unknown_side_trades;
        result.trades.push_back(std::move(trade));
    });
    if (!header || result.trades.empty()) throw std::invalid_argument("empty Tardis trade CSV");
    return result;
}

TardisEventReplayer::TardisEventReplayer(std::string tick_size, std::string step_size)
    : tick_size_(std::move(tick_size)), step_size_(std::move(step_size)) {}

TardisReplaySummary TardisEventReplayer::replay_with_observer(
    const std::filesystem::path& input_path,
    TardisEventObserver& observer) const {
    // The frozen validator remains untouched and is the authoritative summary.
    const TardisL2Replayer validator(tick_size_, step_size_);
    const auto summary = validator.replay(input_path);
    const binance::DecimalIncrement price(tick_size_);
    const binance::DecimalIncrement quantity(step_size_);
    book::OrderBook book;
    bool saw_header = false;
    bool group_open = false;
    bool group_snapshot = false;
    std::uint64_t group_exchange{};
    std::uint64_t group_local{};
    std::uint64_t synthetic_update_id{};
    binance::DepthEvent delta;
    binance::DepthSnapshot snapshot;

    auto finish_group = [&] {
        if (!group_open) return;
        if (group_snapshot) {
            snapshot.last_update_id = ++synthetic_update_id;
            snapshot.event_time_ms = static_cast<std::int64_t>(group_exchange / 1000U);
            snapshot.transaction_time_ms = snapshot.event_time_ms;
            observer.before_snapshot(group_exchange, group_local, book);
            if (!book.load_snapshot(snapshot)) {
                throw std::invalid_argument("event adapter snapshot failed frozen OrderBook semantics");
            }
            observer.after_snapshot(group_exchange, group_local, book);
            snapshot = {};
        } else {
            delta.first_update_id = ++synthetic_update_id;
            delta.final_update_id = synthetic_update_id;
            delta.previous_final_update_id = synthetic_update_id - 1;
            observer.before_depth_event(group_exchange, group_local, delta, book);
            if (!book.is_valid() || !book.apply_depth_event(delta)) {
                throw std::invalid_argument("event adapter delta failed frozen OrderBook semantics");
            }
            observer.after_depth_event(group_exchange, group_local, delta, book);
            delta = {};
        }
        group_open = false;
    };

    for_each_gzip_line(input_path, [&](std::string_view line) {
        if (!saw_header) {
            if (line != l2_header) throw std::invalid_argument("unexpected Tardis L2 header");
            saw_header = true;
            return;
        }
        if (line.empty()) return;
        const auto fields = split(line);
        if (fields[0] != "binance-futures" || fields[1] != "BTCUSDT") {
            throw std::invalid_argument("unexpected event adapter exchange or symbol");
        }
        L2Row row;
        row.exchange_time_us = parse_u64(fields[2]);
        row.local_time_us = parse_u64(fields[3]);
        if (fields[4] == "true") row.snapshot = true;
        else if (fields[4] != "false") throw std::invalid_argument("invalid snapshot flag");
        if (fields[5] == "bid") row.bid = true;
        else if (fields[5] != "ask") throw std::invalid_argument("invalid L2 side");
        row.level.price_ticks = price.to_steps(fields[6]);
        row.level.qty_lots = quantity.to_steps(fields[7]);

        if (group_open &&
            (row.local_time_us != group_local || row.snapshot != group_snapshot)) {
            finish_group();
        }
        if (!group_open) {
            group_open = true;
            group_snapshot = row.snapshot;
            group_exchange = row.exchange_time_us;
            group_local = row.local_time_us;
            if (!row.snapshot) {
                delta.symbol = "BTCUSDT";
                delta.event_time_ms = static_cast<std::int64_t>(row.exchange_time_us / 1000U);
                delta.transaction_time_ms = delta.event_time_ms;
            }
        } else {
            group_exchange = std::max(group_exchange, row.exchange_time_us);
        }
        auto& levels = row.bid
            ? (row.snapshot ? snapshot.bids : delta.bids)
            : (row.snapshot ? snapshot.asks : delta.asks);
        levels.push_back(row.level);
    });
    finish_group();
    if (!saw_header || book.checksum() != summary.final_book_checksum) {
        throw std::invalid_argument("event adapter diverged from frozen Tardis replay checksum");
    }
    observer.finish(book);
    return summary;
}

}  // namespace crypto::historical
