#include <algorithm>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

#include <simdjson.h>
#include <map>

#include <zlib.h>
#include <zstd.h>

#include "book/order_book.hpp"
#include "exchange/binance/depth_sync.hpp"
#include "exchange/binance/instrument_info.hpp"
#include "exchange/binance/message_parser.hpp"
#include "historical/tardis_l2_replayer.hpp"
#include "historical/tardis_trade_reader.hpp"
#include "replay/event_replayer.hpp"
#include "research/microstructure_features.hpp"
#include "research/native_dataset_exporter.hpp"
#include "research/level_lifecycle.hpp"
#include "research/queue_sensitivity.hpp"
#include "research/passive_queue.hpp"
#include "storage/raw_event_writer.hpp"
#include "storage/raw_file_manifest.hpp"

namespace {

int failures = 0;

#define REQUIRE(condition) do { \
    if (!(condition)) { \
        std::cerr << __FILE__ << ':' << __LINE__ << " requirement failed: " #condition "\n"; \
        ++failures; \
    } \
} while (false)

template <typename Function>
void require_throws(Function&& function, const char* expression, int line) {
    try {
        function();
        std::cerr << __FILE__ << ':' << line << " expected exception: " << expression << '\n';
        ++failures;
    } catch (const std::exception&) {
    }
}

#define REQUIRE_THROWS(expression) require_throws([&] { (void)(expression); }, #expression, __LINE__)

std::string fixture(const std::string& name) {
    const auto path = std::filesystem::path(CRYPTO_L2_FIXTURE_DIR) / name;
    std::ifstream in(path, std::ios::binary);
    if (!in) {
        throw std::runtime_error("cannot open fixture: " + path.string());
    }
    return std::string(std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>());
}

crypto::binance::InstrumentInfo test_instrument() {
    return crypto::binance::InstrumentInfo::from_exchange_info_json(
        fixture("exchange_info.json"), "BTCUSDT");
}

void test_precision() {
    const auto instrument = test_instrument();
    REQUIRE(instrument.contract_type() == "PERPETUAL");
    REQUIRE(instrument.status() == "TRADING");
    REQUIRE(instrument.price_to_ticks("60000.10") == 600001);
    REQUIRE(instrument.price_to_ticks("0.10") == 1);
    REQUIRE(instrument.price_to_ticks("1000000.00") == 10000000);
    REQUIRE(instrument.price_to_ticks(instrument.ticks_to_price(1)) == 1);
    REQUIRE(instrument.price_to_ticks(instrument.ticks_to_price(10000000)) == 10000000);
    REQUIRE(instrument.ticks_to_price(600001) == "60000.10");
    REQUIRE(instrument.ticks_x2_to_price(1200001) == "60000.05");
    REQUIRE(instrument.qty_to_lots("0.001") == 1);
    REQUIRE(instrument.qty_to_lots("1000.000") == 1000000);
    REQUIRE(instrument.market_qty_to_lots("1000.001") == 1000001);
    REQUIRE(instrument.lots_to_qty(5) == "0.005");
    REQUIRE(instrument.qty_to_lots(instrument.lots_to_qty(1)) == 1);
    REQUIRE(instrument.qty_to_lots(instrument.lots_to_qty(1000000)) == 1000000);
    REQUIRE_THROWS(instrument.price_to_ticks("60000.15"));
    REQUIRE_THROWS(instrument.price_to_ticks("0.00"));
    REQUIRE_THROWS(instrument.price_to_ticks("1000000.10"));
    REQUIRE_THROWS(instrument.qty_to_lots("0.0005"));
    REQUIRE_THROWS(instrument.qty_to_lots("1000.001"));
    REQUIRE_THROWS(instrument.market_qty_to_lots("-0.001"));
}

void test_parser_and_snapshot() {
    const auto instrument = test_instrument();
    const crypto::binance::MessageParser parser(instrument);
    const auto snapshot = parser.parse_snapshot(fixture("snapshot.json"));
    REQUIRE(snapshot.last_update_id == 100);
    REQUIRE(snapshot.event_time_ms == 1000000);
    REQUIRE(snapshot.transaction_time_ms == 999999);
    REQUIRE(snapshot.bids.size() == 2);
    REQUIRE(snapshot.asks.size() == 2);

    crypto::book::OrderBook book;
    REQUIRE(book.load_snapshot(snapshot));
    REQUIRE(book.is_valid());
    REQUIRE(book.best_bid() == 600000);
    REQUIRE(book.best_ask() == 600001);
    REQUIRE(book.spread_ticks() == 1);
    REQUIRE(book.mid_ticks_x2() == 1200001);
    REQUIRE((book.top_n_bids(2)[1] == crypto::binance::PriceLevel{599999, 2000}));
    REQUIRE((book.top_n_asks(2)[1] == crypto::binance::PriceLevel{600002, 2000}));

    const auto update = parser.parse_depth_event(fixture("depth_update_1.json"));
    REQUIRE(update.first_update_id == 100);
    REQUIRE(update.final_update_id == 101);
    REQUIRE(update.previous_final_update_id == 99);
    REQUIRE(update.symbol == "BTCUSDT");
    REQUIRE(update.event_time_ms == 1000100);
    REQUIRE(update.transaction_time_ms == 1000099);
    REQUIRE(update.bids.size() == 2);
    REQUIRE(update.asks.size() == 2);
    REQUIRE(book.apply_depth_event(update));
    REQUIRE(book.bids().at(600000) == 1200);
    REQUIRE(book.bids().at(599998) == 500);
    REQUIRE(book.asks().count(600002) == 0);
    REQUIRE(book.asks().at(600003) == 3000);

    const auto trade = parser.parse_agg_trade(fixture("agg_trade.json"));
    REQUIRE(trade.aggregate_trade_id == 12345);
    REQUIRE(trade.symbol == "BTCUSDT");
    REQUIRE(trade.event_time_ms == 1000150);
    REQUIRE(trade.transaction_time_ms == 1000149);
    REQUIRE(trade.first_trade_id == 200);
    REQUIRE(trade.last_trade_id == 202);
    REQUIRE(trade.price_ticks == 600001);
    REQUIRE(trade.qty_lots == 5);
    REQUIRE(trade.is_aggressive_buy());

    auto wrong_symbol = fixture("depth_update_1.json");
    wrong_symbol.replace(wrong_symbol.find("BTCUSDT"), 7, "ETHUSDT");
    REQUIRE_THROWS(parser.parse_depth_event(wrong_symbol));
}

void test_incremental_and_crossed_invariant() {
    const auto instrument = test_instrument();
    const crypto::binance::MessageParser parser(instrument);
    crypto::book::OrderBook book;
    REQUIRE(book.load_snapshot(parser.parse_snapshot(fixture("snapshot.json"))));
    REQUIRE(book.apply_depth_event(parser.parse_depth_event(fixture("depth_update_1.json"))));
    REQUIRE(book.apply_depth_event(parser.parse_depth_event(fixture("depth_update_2.json"))));
    REQUIRE(book.bids().count(599999) == 0);
    REQUIRE(book.asks().at(600001) == 1700);

    crypto::binance::DepthEvent unknown_deletion;
    unknown_deletion.final_update_id = 103;
    unknown_deletion.bids.push_back({500000, 0});
    REQUIRE(book.apply_depth_event(unknown_deletion));
    REQUIRE(book.is_valid());

    crypto::binance::DepthEvent crossed;
    crossed.final_update_id = 104;
    crossed.bids.push_back({600002, 1});
    REQUIRE(!book.apply_depth_event(crossed));
    REQUIRE(!book.is_valid());
    REQUIRE(!book.best_bid().has_value());

    crypto::book::OrderBook negative_book;
    REQUIRE(negative_book.load_snapshot(parser.parse_snapshot(fixture("snapshot.json"))));
    crypto::binance::DepthEvent negative;
    negative.final_update_id = 101;
    negative.bids.push_back({600000, -1});
    REQUIRE(!negative_book.apply_depth_event(negative));
    REQUIRE(!negative_book.is_valid());
}

void test_sequence_state_machine() {
    const auto instrument = test_instrument();
    const crypto::binance::MessageParser parser(instrument);
    const auto snapshot = parser.parse_snapshot(fixture("snapshot.json"));
    const auto first = parser.parse_depth_event(fixture("depth_update_1.json"));
    const auto second = parser.parse_depth_event(fixture("depth_update_2.json"));

    crypto::book::OrderBook book;
    crypto::binance::DepthSynchronizer sync(book, 10);
    REQUIRE(sync.state() == crypto::binance::SyncState::Disconnected);
    sync.on_connected();
    REQUIRE(sync.state() == crypto::binance::SyncState::Buffering);
    REQUIRE(sync.on_depth(first) == crypto::binance::SyncResult::Buffered);
    sync.request_snapshot();
    REQUIRE(sync.on_snapshot(snapshot) == crypto::binance::SyncResult::Applied);
    REQUIRE(sync.is_live());
    REQUIRE(book.last_update_id() == 101);
    REQUIRE(sync.on_depth(second) == crypto::binance::SyncResult::Applied);
    REQUIRE(book.last_update_id() == 102);
    REQUIRE(sync.on_depth(second) == crypto::binance::SyncResult::Stale);

    auto gap = second;
    gap.first_update_id = 104;
    gap.final_update_id = 104;
    gap.previous_final_update_id = 103;
    REQUIRE(sync.on_depth(gap) == crypto::binance::SyncResult::GapDetected);
    REQUIRE(sync.state() == crypto::binance::SyncState::GapDetected);
    REQUIRE(!book.is_valid());
    REQUIRE(sync.gap_count() == 1);

    sync.begin_resync();
    REQUIRE(sync.state() == crypto::binance::SyncState::Resyncing);

    crypto::book::OrderBook snapshot_only_book;
    crypto::binance::DepthSynchronizer snapshot_only_sync(snapshot_only_book, 10);
    snapshot_only_sync.on_connected();
    snapshot_only_sync.request_snapshot();
    REQUIRE(snapshot_only_sync.on_snapshot(snapshot) == crypto::binance::SyncResult::Buffered);
    REQUIRE(snapshot_only_book.is_valid());
    REQUIRE(!snapshot_only_sync.is_live());
    REQUIRE(snapshot_only_sync.on_depth(first) == crypto::binance::SyncResult::Applied);
    REQUIRE(snapshot_only_sync.is_live());

    snapshot_only_sync.on_disconnected();
    REQUIRE(snapshot_only_sync.state() == crypto::binance::SyncState::Disconnected);
    REQUIRE(!snapshot_only_book.is_valid());
    snapshot_only_sync.on_connected();
    REQUIRE(snapshot_only_sync.on_depth(first) == crypto::binance::SyncResult::Buffered);
    snapshot_only_sync.request_snapshot();
    REQUIRE(snapshot_only_sync.on_snapshot(snapshot) == crypto::binance::SyncResult::Applied);
    REQUIRE(snapshot_only_sync.is_live());

    crypto::book::OrderBook overflow_book;
    crypto::binance::DepthSynchronizer overflow_sync(overflow_book, 1);
    overflow_sync.on_connected();
    REQUIRE(overflow_sync.on_depth(first) == crypto::binance::SyncResult::Buffered);
    REQUIRE(overflow_sync.on_depth(second) == crypto::binance::SyncResult::InvalidBook);
    REQUIRE(overflow_sync.state() == crypto::binance::SyncState::Invalid);
    REQUIRE(!overflow_book.is_valid());
}

void test_raw_round_trip_and_replay_determinism() {
    const auto temp_dir = std::filesystem::temp_directory_path() / "crypto_hft_l2_tests";
    std::filesystem::create_directories(temp_dir);
    const auto raw_path = temp_dir / "fixture.chft.zst";
    const auto export_path = temp_dir / "fixture.csv.zst";

    auto write = [&](crypto::storage::RawEventWriter& writer,
                     std::uint64_t time_ns,
                     crypto::storage::RawEventType type,
                     std::string session,
                     std::string stream,
                     std::string payload,
                     std::int64_t exchange_ms) {
        writer.write({time_ns, time_ns + 7, exchange_ms, type,
                      std::move(session), std::move(stream), std::move(payload)});
    };
    {
        crypto::storage::RawEventWriter writer(raw_path, 128);
        write(writer, 1'000'000'000ULL, crypto::storage::RawEventType::InstrumentInfo,
              "rest", "exchangeInfo", fixture("exchange_info.json"), 0);
        write(writer, 1'100'000'000ULL, crypto::storage::RawEventType::ConnectionOpen,
              "depth-1", "btcusdt@depth@100ms", "{}", 0);
        write(writer, 1'200'000'000ULL, crypto::storage::RawEventType::DepthUpdate,
              "depth-1", "btcusdt@depth@100ms", fixture("depth_update_1.json"), 1000099);
        write(writer, 1'250'000'000ULL, crypto::storage::RawEventType::AggTrade,
              "trade-1", "btcusdt@aggTrade", fixture("agg_trade.json"), 1000149);
        write(writer, 1'300'000'000ULL, crypto::storage::RawEventType::DepthSnapshot,
              "depth-1", "fapi/v1/depth", fixture("snapshot.json"), 999999);
        write(writer, 1'400'000'000ULL, crypto::storage::RawEventType::DepthUpdate,
              "depth-1", "btcusdt@depth@100ms", fixture("depth_update_2.json"), 1000199);
        write(writer, 1'450'000'000ULL, crypto::storage::RawEventType::ConnectionClose,
              "depth-1", "btcusdt@depth@100ms", "{\"reason\":\"collector stop\"}", 0);
        write(writer, 1'500'000'000ULL, crypto::storage::RawEventType::ConnectionClose,
              "trade-1", "btcusdt@aggTrade", "{\"reason\":\"collector stop\"}", 0);
        writer.flush();
        REQUIRE(writer.records_written() == 8);
    }

    std::vector<crypto::storage::RawRecord> records;
    crypto::storage::RawEventReader reader(raw_path);
    reader.for_each([&](const crypto::storage::RawRecord& record) { records.push_back(record); });
    REQUIRE(records.size() == 8);
    REQUIRE(records[2].payload == fixture("depth_update_1.json"));
    REQUIRE(records[2].session_id == "depth-1");

    const auto instrument = crypto::replay::EventReplayer::instrument_from_raw_file(raw_path, "BTCUSDT");
    crypto::replay::EventReplayer replayer(instrument);
    const auto first = replayer.replay(raw_path, export_path, 100);
    const auto second = replayer.replay(raw_path);
    REQUIRE(first.raw_records == 8);
    REQUIRE(first.first_local_receive_ns == 1'000'000'000ULL);
    REQUIRE(first.last_local_receive_ns == 1'500'000'000ULL);
    REQUIRE(first.first_exchange_time_ms == 999999);
    REQUIRE(first.last_exchange_time_ms == 1000199);
    REQUIRE(first.depth_events == 2);
    REQUIRE(first.applied_depth_events == 2);
    REQUIRE(first.trade_events == 1);
    REQUIRE(first.sequence_gaps == 0);
    REQUIRE(first.parse_failures == 0);
    REQUIRE(first.final_update_id == 102);
    REQUIRE(first.best_bid_ticks == 600000);
    REQUIRE(first.best_ask_ticks == 600001);
    REQUIRE(first.raw_records == second.raw_records);
    REQUIRE(first.depth_events == second.depth_events);
    REQUIRE(first.applied_depth_events == second.applied_depth_events);
    REQUIRE(first.trade_events == second.trade_events);
    REQUIRE(first.final_update_id == second.final_update_id);
    REQUIRE(first.best_bid_ticks == second.best_bid_ticks);
    REQUIRE(first.best_ask_ticks == second.best_ask_ticks);
    REQUIRE(first.final_checksum == second.final_checksum);
    REQUIRE(first.top_bids == second.top_bids);
    REQUIRE(first.top_asks == second.top_asks);
    REQUIRE(first.research_rows > 0);
    REQUIRE(std::filesystem::file_size(export_path) > 0);
}

void test_tardis_normalized_replay_and_resnapshot() {
    const auto temp_dir = std::filesystem::temp_directory_path() / "crypto_hft_l2_tests";
    std::filesystem::create_directories(temp_dir);
    const auto path = temp_dir / "tardis.csv.gz";
    const std::string csv =
        "exchange,symbol,timestamp,local_timestamp,is_snapshot,side,price,amount\n"
        "binance-futures,BTCUSDT,100,100,false,ask,10.1,1.000\n"
        "binance-futures,BTCUSDT,200,200,true,ask,10.1,2.000\n"
        "binance-futures,BTCUSDT,200,200,true,bid,10.0,3.000\n"
        "binance-futures,BTCUSDT,300,300,false,bid,10.0,4.000\n"
        "binance-futures,BTCUSDT,300,300,false,ask,10.1,5.000\n"
        "binance-futures,BTCUSDT,400,400,false,bid,10.2,1.000\n"
        "binance-futures,BTCUSDT,500,500,false,bid,9.9,1.000\n"
        "binance-futures,BTCUSDT,600,600,true,ask,10.4,1.000\n"
        "binance-futures,BTCUSDT,600,600,true,bid,10.3,2.000\n"
        "binance-futures,BTCUSDT,700,700,false,bid,10.3,3.000\n";
    gzFile output = gzopen(path.c_str(), "wb");
    REQUIRE(output != nullptr);
    if (output != nullptr) {
        REQUIRE(gzwrite(output, csv.data(), static_cast<unsigned int>(csv.size())) ==
                static_cast<int>(csv.size()));
        REQUIRE(gzclose(output) == Z_OK);
    }

    const crypto::historical::TardisL2Replayer replayer("0.10", "0.001");
    const auto first = replayer.replay(path);
    const auto second = replayer.replay(path);
    REQUIRE(first.rows == 10);
    REQUIRE(first.message_groups == 7);
    REQUIRE(first.snapshot_rows == 4);
    REQUIRE(first.snapshot_groups == 2);
    REQUIRE(first.delta_rows == 6);
    REQUIRE(first.pre_snapshot_rows == 1);
    REQUIRE(first.pre_snapshot_messages == 1);
    REQUIRE(first.crossed_book_states == 1);
    REQUIRE(first.skipped_invalid_messages == 1);
    REQUIRE(first.snapshot_load_failures == 0);
    REQUIRE(first.final_book_valid);
    REQUIRE(first.first_bid_ticks == 100);
    REQUIRE(first.first_ask_ticks == 101);
    REQUIRE(first.last_bid_ticks == 103);
    REQUIRE(first.last_ask_ticks == 104);
    REQUIRE(first.final_book_checksum == second.final_book_checksum);
    REQUIRE(first.replay_checksum == second.replay_checksum);
}

void test_microstructure_feature_formulas() {
    crypto::binance::DepthSnapshot snapshot;
    for (std::int64_t level = 0; level < 10; ++level) {
        snapshot.bids.push_back({100 - level, level == 0 ? 30 : level + 1});
        snapshot.asks.push_back({101 + level, level == 0 ? 10 : level + 2});
    }
    crypto::book::OrderBook book;
    REQUIRE(book.load_snapshot(snapshot));
    const auto features = crypto::research::calculate_instant_book_features(book, 0.5);
    REQUIRE(features.has_l1);
    REQUIRE(features.has_l5);
    REQUIRE(features.has_l10);
    REQUIRE(features.obi_l1.has_value());
    REQUIRE(std::abs(*features.obi_l1 - 0.5) < 1e-12);
    REQUIRE(features.weighted_obi_l10.has_value());
    REQUIRE(*features.weighted_obi_l10 >= -1.0);
    REQUIRE(*features.weighted_obi_l10 <= 1.0);
    REQUIRE(features.weighted_mid_ticks.has_value());
    REQUIRE(std::abs(*features.weighted_mid_ticks - 100.75) < 1e-12);
    REQUIRE(features.weighted_mid_minus_mid_ticks.has_value());
    REQUIRE(std::abs(*features.weighted_mid_minus_mid_ticks - 0.25) < 1e-12);

    const crypto::research::BestLevelState previous{100, 10, 101, 20};
    const crypto::research::BestLevelState quantity_change{100, 12, 101, 15};
    REQUIRE(crypto::research::l1_ofi_contribution(previous, quantity_change) == 7);
    const crypto::research::BestLevelState bid_up_ask_up{101, 5, 102, 7};
    REQUIRE(crypto::research::l1_ofi_contribution(previous, bid_up_ask_up) == 25);
    const crypto::research::BestLevelState bid_down_ask_down{99, 8, 100, 6};
    REQUIRE(crypto::research::l1_ofi_contribution(previous, bid_down_ask_down) == -16);
}

void test_tardis_trade_side_and_causal_bucketing() {
    const crypto::binance::DecimalIncrement price_increment("0.10");
    const crypto::binance::DecimalIncrement qty_increment("0.001");
    const auto buy = crypto::historical::parse_tardis_trade_row(
        "binance-futures,BTCUSDT,1000000,1000007,1,buy,60000.10,0.002",
        price_increment,
        qty_increment);
    REQUIRE(buy.side == crypto::historical::AggressorSide::Buy);
    REQUIRE(buy.price_ticks == 600001);
    REQUIRE(buy.qty_lots == 2);
    const auto sell = crypto::historical::parse_tardis_trade_row(
        "binance-futures,BTCUSDT,1000001,1000008,2,sell,60000.20,0.003",
        price_increment,
        qty_increment);
    REQUIRE(sell.side == crypto::historical::AggressorSide::Sell);
    REQUIRE_THROWS(crypto::historical::parse_tardis_trade_row(
        "binance-futures,BTCUSDT,1000001,1000008,3,BUY,60000.20,0.003",
        price_increment,
        qty_increment));

    const auto temp_dir = std::filesystem::temp_directory_path() / "crypto_hft_l2_tests";
    std::filesystem::create_directories(temp_dir);
    const auto path = temp_dir / "tardis-trades.csv.gz";
    const std::string csv =
        "exchange,symbol,timestamp,local_timestamp,id,side,price,amount\n"
        "binance-futures,BTCUSDT,1000000,1000007,1,buy,60000.10,0.002\n"
        "binance-futures,BTCUSDT,1000001,1000008,2,sell,60000.20,0.003\n"
        "binance-futures,BTCUSDT,1100000,1100009,3,buy,60000.30,0.004\n"
        "binance-futures,BTCUSDT,1150000,1150010,4,sell,60000.40,0.005\n"
        "binance-futures,BTCUSDT,1299999,1300010,5,buy,60000.50,0.006\n";
    gzFile output = gzopen(path.c_str(), "wb");
    REQUIRE(output != nullptr);
    if (output != nullptr) {
        REQUIRE(gzwrite(output, csv.data(), static_cast<unsigned int>(csv.size())) ==
                static_cast<int>(csv.size()));
        REQUIRE(gzclose(output) == Z_OK);
    }
    const auto day = crypto::historical::read_tardis_trade_day(
        path, 1'000'000, 1'300'000, 100'000, price_increment, qty_increment);
    REQUIRE(day.rows == 5);
    REQUIRE(day.buy_trades == 3);
    REQUIRE(day.sell_trades == 2);
    REQUIRE(day.unknown_side_trades == 0);
    REQUIRE(day.outside_event_day_rows == 1);
    REQUIRE(day.buckets.size() == 3);
    REQUIRE(day.buckets[0].trade_count == 1);
    REQUIRE(day.buckets[0].aggressive_buy_qty_lots == 2);
    REQUIRE(day.buckets[1].trade_count == 2);
    REQUIRE(day.buckets[1].aggressive_sell_qty_lots == 3);
    REQUIRE(day.buckets[1].aggressive_buy_qty_lots == 4);
    REQUIRE(day.buckets[2].trade_count == 1);
    REQUIRE(day.buckets[2].aggressive_sell_qty_lots == 5);
}

void test_tardis_daily_export_reset_and_determinism() {
    const auto temp_dir = std::filesystem::temp_directory_path() / "crypto_hft_l2_tests";
    std::filesystem::create_directories(temp_dir);
    const auto l2_path = temp_dir / "tardis-daily-l2.csv.gz";
    const auto trades_path = temp_dir / "tardis-daily-trades.csv.gz";
    const auto first_path = temp_dir / "tardis-daily-first.csv.zst";
    const auto second_path = temp_dir / "tardis-daily-second.csv.zst";

    std::ostringstream l2;
    l2 << "exchange,symbol,timestamp,local_timestamp,is_snapshot,side,price,amount\n";
    for (int level = 0; level < 10; ++level) {
        l2 << "binance-futures,BTCUSDT,200,200,true,ask," << 10.1 + level * 0.1
           << ",1.000\n";
        l2 << "binance-futures,BTCUSDT,200,200,true,bid," << 10.0 - level * 0.1
           << ",1.000\n";
    }
    l2 << "binance-futures,BTCUSDT,300,300,false,bid,10.0,2.000\n"
       << "binance-futures,BTCUSDT,300,300,false,ask,10.1,2.000\n";
    for (int level = 0; level < 10; ++level) {
        l2 << "binance-futures,BTCUSDT,600,600,true,ask," << 10.3 + level * 0.1
           << ",1.000\n";
        l2 << "binance-futures,BTCUSDT,600,600,true,bid," << 10.2 - level * 0.1
           << ",1.000\n";
    }
    l2 << "binance-futures,BTCUSDT,700,700,false,bid,10.2,3.000\n";
    const auto l2_text = l2.str();
    gzFile l2_output = gzopen(l2_path.c_str(), "wb");
    REQUIRE(l2_output != nullptr);
    if (l2_output != nullptr) {
        REQUIRE(gzwrite(l2_output, l2_text.data(), static_cast<unsigned int>(l2_text.size())) ==
                static_cast<int>(l2_text.size()));
        REQUIRE(gzclose(l2_output) == Z_OK);
    }

    const std::string trades =
        "exchange,symbol,timestamp,local_timestamp,id,side,price,amount\n"
        "binance-futures,BTCUSDT,250,251,1,buy,10.1,0.002\n"
        "binance-futures,BTCUSDT,600,601,2,sell,10.2,0.003\n";
    gzFile trade_output = gzopen(trades_path.c_str(), "wb");
    REQUIRE(trade_output != nullptr);
    if (trade_output != nullptr) {
        REQUIRE(gzwrite(trade_output, trades.data(), static_cast<unsigned int>(trades.size())) ==
                static_cast<int>(trades.size()));
        REQUIRE(gzclose(trade_output) == Z_OK);
    }

    const crypto::historical::TardisL2Replayer replayer("0.10", "0.001");
    const auto first = replayer.replay_research_day(
        l2_path, trades_path, first_path, "1970-01-01", 0, 1'000, 100, 0.5);
    const auto second = replayer.replay_research_day(
        l2_path, trades_path, second_path, "1970-01-01", 0, 1'000, 100, 0.5);
    REQUIRE(first == second);
    REQUIRE(first.daily_export.rows == 10);
    REQUIRE(first.daily_export.valid_rows == 6);
    REQUIRE(first.daily_export.invalid_rows == 4);
    REQUIRE(first.daily_export.book_segments == 2);
    REQUIRE(first.daily_export.trades == 2);
    REQUIRE(first.daily_export.final_book_observable);
    std::ifstream first_stream(first_path, std::ios::binary);
    std::ifstream second_stream(second_path, std::ios::binary);
    const std::string first_bytes{
        std::istreambuf_iterator<char>(first_stream), std::istreambuf_iterator<char>()};
    const std::string second_bytes{
        std::istreambuf_iterator<char>(second_stream), std::istreambuf_iterator<char>()};
    REQUIRE(first_bytes == second_bytes);
}

crypto::historical::TardisTrade passive_trade(
    std::uint64_t exchange_time,
    std::uint64_t local_time,
    crypto::historical::AggressorSide side,
    std::int64_t price,
    std::int64_t quantity) {
    return crypto::historical::TardisTrade{
        exchange_time,
        local_time,
        std::to_string(local_time),
        side,
        price,
        quantity,
    };
}

void test_passive_queue_conservative_fills() {
    crypto::research::PassiveTradeIndex index;
    index.add_trade(passive_trade(90, 100, crypto::historical::AggressorSide::Sell, 100, 8));
    index.add_trade(passive_trade(190, 200, crypto::historical::AggressorSide::Sell, 100, 4));
    index.add_trade(passive_trade(290, 300, crypto::historical::AggressorSide::Sell, 100, 3));
    index.add_trade(passive_trade(310, 320, crypto::historical::AggressorSide::Sell, 100, 50));
    index.add_trade(passive_trade(390, 400, crypto::historical::AggressorSide::Buy, 101, 12));
    index.add_trade(passive_trade(490, 500, crypto::historical::AggressorSide::Buy, 101, 4));
    index.finalize();

    const auto bid_partial = index.evaluate(
        crypto::research::PassiveQuoteSide::Bid, 100, 5, 10, 0, 250);
    REQUIRE(bid_partial.state == crypto::research::PassiveFillState::Partial);
    REQUIRE(bid_partial.queue_ahead_initial_lots == 10);
    REQUIRE(bid_partial.queue_consumed_lots == 10);
    REQUIRE(bid_partial.filled_lots == 2);
    REQUIRE(bid_partial.first_fill_local_time_us == 200);

    const auto bid_full = index.evaluate(
        crypto::research::PassiveQuoteSide::Bid, 100, 5, 10, 0, 350);
    REQUIRE(bid_full.state == crypto::research::PassiveFillState::Full);
    REQUIRE(bid_full.filled_lots == 5);
    REQUIRE(bid_full.full_fill_local_time_us == 300);
    REQUIRE(!bid_full.filled_by_trade_through);
    REQUIRE(bid_full.same_price_traded_lots == 15);
    REQUIRE(bid_full.qualifying_trade_count == 3);

    const auto ask_partial = index.evaluate(
        crypto::research::PassiveQuoteSide::Ask, 101, 5, 10, 0, 450);
    REQUIRE(ask_partial.state == crypto::research::PassiveFillState::Partial);
    REQUIRE(ask_partial.filled_lots == 2);
    REQUIRE(ask_partial.first_fill_local_time_us == 400);

    const auto ask_full = index.evaluate(
        crypto::research::PassiveQuoteSide::Ask, 101, 5, 10, 0, 550);
    REQUIRE(ask_full.state == crypto::research::PassiveFillState::Full);
    REQUIRE(ask_full.full_fill_local_time_us == 500);

    // No book-size/cancellation input is supplied to the pessimistic model. Queue only moves
    // through qualifying trades; later additions are therefore mechanically behind our order.
    const auto no_trade = index.evaluate(
        crypto::research::PassiveQuoteSide::Bid, 99, 5, 10, 0, 550);
    REQUIRE(no_trade.state == crypto::research::PassiveFillState::Unfilled);
    REQUIRE(no_trade.queue_consumed_lots == 0);

    // The exact expiry event is excluded, as is an event tied with placement local time.
    const auto expires_first = index.evaluate(
        crypto::research::PassiveQuoteSide::Bid, 100, 5, 0, 100, 200);
    REQUIRE(expires_first.state == crypto::research::PassiveFillState::Unfilled);
    const auto same_time_is_prior = index.evaluate(
        crypto::research::PassiveQuoteSide::Bid, 100, 5, 0, 200, 300);
    REQUIRE(same_time_is_prior.state == crypto::research::PassiveFillState::Unfilled);

    const auto repeat = index.evaluate(
        crypto::research::PassiveQuoteSide::Bid, 100, 5, 10, 0, 350);
    REQUIRE(repeat == bid_full);
}

void test_passive_queue_trade_through_and_fixed_quote() {
    crypto::research::PassiveTradeIndex index;
    index.add_trade(passive_trade(100, 101, crypto::historical::AggressorSide::Sell, 101, 100));
    index.add_trade(passive_trade(200, 201, crypto::historical::AggressorSide::Sell, 99, 1));
    index.add_trade(passive_trade(220, 221, crypto::historical::AggressorSide::Sell, 100, 500));
    index.add_trade(passive_trade(300, 301, crypto::historical::AggressorSide::Buy, 100, 100));
    index.add_trade(passive_trade(400, 401, crypto::historical::AggressorSide::Buy, 102, 1));
    index.finalize();

    // Trades at a newer BBO do not move our fixed bid at 100. A sell below 100 trades through it.
    const auto bid = index.evaluate(
        crypto::research::PassiveQuoteSide::Bid, 100, 5, 1'000, 0, 250);
    REQUIRE(bid.state == crypto::research::PassiveFillState::Full);
    REQUIRE(bid.filled_by_trade_through);
    REQUIRE(bid.trade_through_price_ticks == 99);
    REQUIRE(bid.full_fill_local_time_us == 201);
    REQUIRE(bid.queue_consumed_lots == 1'000);
    REQUIRE(bid.same_price_traded_lots == 0);
    REQUIRE(bid.qualifying_trade_count == 1);

    const auto ask = index.evaluate(
        crypto::research::PassiveQuoteSide::Ask, 101, 5, 1'000, 250, 450);
    REQUIRE(ask.state == crypto::research::PassiveFillState::Full);
    REQUIRE(ask.filled_by_trade_through);
    REQUIRE(ask.trade_through_price_ticks == 102);
    REQUIRE(ask.full_fill_local_time_us == 401);

    const auto optimistic = index.evaluate(
        crypto::research::PassiveQuoteSide::Bid, 101, 5, 0, 0, 150);
    REQUIRE(optimistic.state == crypto::research::PassiveFillState::Full);
    REQUIRE(optimistic.full_fill_local_time_us == 101);
}

std::string rotation_depth_json(
    std::int64_t exchange_ms,
    std::int64_t first_id,
    std::int64_t final_id,
    std::int64_t previous_id,
    const std::string& bid_qty) {
    return std::string(R"({"e":"depthUpdate","E":)") + std::to_string(exchange_ms) +
           R"(,"T":)" + std::to_string(exchange_ms - 1) +
           R"(,"s":"BTCUSDT","U":)" + std::to_string(first_id) +
           R"(,"u":)" + std::to_string(final_id) +
           R"(,"pu":)" + std::to_string(previous_id) +
           R"(,"b":[["60000.00",")" + bid_qty + R"("]],"a":[["60000.10","1.500"]]})";
}

std::string rotation_trade_json(std::int64_t exchange_ms, std::int64_t trade_id) {
    return std::string(R"({"e":"aggTrade","E":)") + std::to_string(exchange_ms) +
           R"(,"s":"BTCUSDT","a":)" + std::to_string(trade_id) +
           R"(,"p":"60000.10","q":"0.005","f":200,"l":202,"T":)" +
           std::to_string(exchange_ms - 1) + R"(,"m":false})";
}

std::string rotation_snapshot_json(std::int64_t last_update_id, std::int64_t exchange_ms) {
    return std::string(R"({"lastUpdateId":)") + std::to_string(last_update_id) +
           R"(,"E":)" + std::to_string(exchange_ms) +
           R"(,"T":)" + std::to_string(exchange_ms - 1) +
           R"(,"bids":[["60000.00","1.000"],["59999.90","2.000"]])" +
           R"(,"asks":[["60000.10","1.500"],["60000.20","2.000"]]})";
}

// Deterministic capture stream laid out against a two second rotation interval so that the
// boundaries land on the cases production must survive: between two depth records, between a
// depth and a trade record, in the middle of a burst, and across a reconnect.
std::vector<crypto::storage::RawRecord> rotation_dataset() {
    constexpr std::uint64_t base = 1'700'000'000ULL * 1'000'000'000ULL;
    std::vector<crypto::storage::RawRecord> records;
    std::int64_t last_final_id = 99;
    std::int64_t exchange_ms = 1'000'100;
    std::int64_t trade_id = 500;

    auto push = [&](std::uint64_t offset_ns,
                    crypto::storage::RawEventType type,
                    std::string session,
                    std::string stream,
                    std::string payload,
                    std::int64_t exchange_time_ms) {
        records.push_back({base + offset_ns, base + offset_ns + 7, exchange_time_ms, type,
                           std::move(session), std::move(stream), std::move(payload)});
    };
    auto depth = [&](std::uint64_t offset_ns, const std::string& bid_qty) {
        const auto first_id = last_final_id + 1;
        const auto final_id = last_final_id + 2;
        const auto previous_id = last_final_id;
        last_final_id = final_id;
        exchange_ms += 100;
        push(offset_ns, crypto::storage::RawEventType::DepthUpdate, "depth-1",
             "btcusdt@depth@100ms",
             rotation_depth_json(exchange_ms, first_id, final_id, previous_id, bid_qty),
             exchange_ms - 1);
    };
    auto trade = [&](std::uint64_t offset_ns) {
        exchange_ms += 50;
        ++trade_id;
        push(offset_ns, crypto::storage::RawEventType::AggTrade, "trade-1", "btcusdt@aggTrade",
             rotation_trade_json(exchange_ms, trade_id), exchange_ms - 1);
    };

    push(0, crypto::storage::RawEventType::InstrumentInfo, "rest", "exchangeInfo",
         fixture("exchange_info.json"), 0);
    push(100'000'000ULL, crypto::storage::RawEventType::ConnectionOpen, "depth-1",
         "btcusdt@depth@100ms", "{}", 0);
    push(150'000'000ULL, crypto::storage::RawEventType::ConnectionOpen, "trade-1",
         "btcusdt@aggTrade", "{}", 0);
    push(300'000'000ULL, crypto::storage::RawEventType::DepthSnapshot, "depth-1", "fapi/v1/depth",
         fixture("snapshot.json"), 999'999);
    depth(400'000'000ULL, "1.100");
    trade(500'000'000ULL);
    // Bucket 0 -> 1 boundary falls between two depth records.
    depth(1'999'000'000ULL, "1.200");
    depth(2'000'000'000ULL, "1.300");
    depth(2'500'000'000ULL, "1.400");
    // Bucket 1 -> 2 boundary falls between a depth record and a trade record.
    depth(3'999'000'000ULL, "1.500");
    trade(4'000'000'000ULL);
    depth(4'100'000'000ULL, "1.600");
    // Burst straddling the bucket 2 -> 3 boundary.
    for (int i = 0; i < 40; ++i) {
        depth(5'999'900'000ULL + static_cast<std::uint64_t>(i) * 2'500ULL, "2.000");
    }
    for (int i = 0; i < 40; ++i) {
        depth(6'000'000'000ULL + static_cast<std::uint64_t>(i) * 2'500ULL, "2.100");
    }
    // Reconnect straddling the bucket 3 -> 4 boundary.
    push(7'900'000'000ULL, crypto::storage::RawEventType::ConnectionClose, "depth-1",
         "btcusdt@depth@100ms", R"({"reason":"peer reset"})", 0);
    push(8'000'000'000ULL, crypto::storage::RawEventType::ConnectionOpen, "depth-2",
         "btcusdt@depth@100ms", "{}", 0);
    const auto resnapshot_id = last_final_id + 5;
    exchange_ms += 100;
    push(8'100'000'000ULL, crypto::storage::RawEventType::DepthSnapshot, "depth-2", "fapi/v1/depth",
         rotation_snapshot_json(resnapshot_id, exchange_ms), exchange_ms - 1);
    last_final_id = resnapshot_id - 1;
    depth(8'200'000'000ULL, "1.700");
    trade(8'300'000'000ULL);
    push(9'000'000'000ULL, crypto::storage::RawEventType::ConnectionClose, "depth-2",
         "btcusdt@depth@100ms", R"({"reason":"collector stop"})", 0);
    push(9'100'000'000ULL, crypto::storage::RawEventType::ConnectionClose, "trade-1",
         "btcusdt@aggTrade", R"({"reason":"collector stop"})", 0);
    return records;
}

void feed(crypto::storage::RawEventWriter& writer,
          const std::vector<crypto::storage::RawRecord>& records) {
    for (const auto& record : records) {
        writer.write(record);
    }
}

std::vector<std::filesystem::path> rotated_files(const std::filesystem::path& directory) {
    std::vector<std::filesystem::path> files;
    for (const auto& entry : std::filesystem::directory_iterator(directory)) {
        if (entry.path().extension() == ".zst") {
            files.push_back(entry.path());
        }
    }
    // Bucket-start filenames sort chronologically.
    std::sort(files.begin(), files.end());
    return files;
}

std::vector<crypto::storage::RawRecord> read_all(const std::vector<std::filesystem::path>& paths) {
    std::vector<crypto::storage::RawRecord> records;
    for (const auto& path : paths) {
        crypto::storage::RawEventReader reader(path);
        reader.for_each([&](const crypto::storage::RawRecord& record) { records.push_back(record); });
    }
    return records;
}

simdjson::dom::element load_manifest(
    simdjson::dom::parser& parser, const std::filesystem::path& raw_path) {
    auto manifest_path = raw_path;
    manifest_path += ".manifest.json";
    return parser.load(manifest_path.string());
}

std::string manifest_string(simdjson::dom::element manifest, const char* field) {
    std::string_view value;
    if (manifest[field].get(value)) {
        return {};
    }
    return std::string(value);
}

std::uint64_t manifest_counter(simdjson::dom::element manifest, const char* field) {
    std::uint64_t value = 0;
    if (manifest["counters"][field].get(value)) {
        return std::numeric_limits<std::uint64_t>::max();
    }
    return value;
}

void test_raw_rotation_matches_unrotated_capture() {
    const auto temp_dir = std::filesystem::temp_directory_path() / "crypto_hft_l2_tests" / "rotation";
    std::filesystem::remove_all(temp_dir);
    std::filesystem::create_directories(temp_dir);
    const auto baseline_path = temp_dir / "baseline.chft.zst";
    const auto rotate_dir = temp_dir / "rotated";
    const auto export_a = temp_dir / "research_A.csv.zst";
    const auto export_b = temp_dir / "research_B.csv.zst";

    const auto records = rotation_dataset();
    crypto::storage::ManifestFinalizer finalizer;
    auto attach_manifests = [&](crypto::storage::RawEventWriter& writer) {
        writer.set_on_file_closed([&](crypto::storage::ClosedRawFile& file) {
            file.symbol = "BTCUSDT";
            file.counters.emplace_back("sequence_gaps", 0);
            finalizer.submit(file);
        });
    };

    {
        crypto::storage::RawEventWriter writer(baseline_path, 128);
        attach_manifests(writer);
        feed(writer, records);
        writer.close();
        REQUIRE(writer.records_written() == records.size());
        REQUIRE(writer.rotations() == 0);
    }

    crypto::storage::RotationConfig rotation;
    rotation.directory = rotate_dir;
    rotation.prefix = "BTCUSDT-TEST";
    rotation.interval_ns = 2'000'000'000ULL;
    std::uint64_t rotations = 0;
    {
        crypto::storage::RawEventWriter writer(rotation, 128);
        attach_manifests(writer);
        feed(writer, records);
        writer.close();
        rotations = writer.rotations();
        REQUIRE(writer.records_written() == records.size());
    }
    finalizer.wait_all();
    REQUIRE(finalizer.failures() == 0);

    const auto files = rotated_files(rotate_dir);
    REQUIRE(files.size() == 5);
    REQUIRE(rotations == 4);
    REQUIRE(files.front().filename().string() == "BTCUSDT-TEST-20231114T221320Z.chft.zst");

    // Every rotated file decompresses on its own, and the chain reproduces the record stream
    // exactly: same records, same order, nothing duplicated at a boundary and nothing dropped.
    const auto chain_records = read_all(files);
    REQUIRE(chain_records == records);
    REQUIRE(read_all({baseline_path}) == records);

    std::uint64_t manifest_records = 0;
    std::uint64_t manifest_depth = 0;
    std::uint64_t manifest_trades = 0;
    std::uint64_t manifest_snapshots = 0;
    simdjson::dom::parser parser;
    for (std::size_t i = 0; i < files.size(); ++i) {
        const auto manifest = load_manifest(parser, files[i]);
        REQUIRE(manifest_string(manifest, "filename") == files[i].filename().string());
        REQUIRE(manifest_string(manifest, "symbol") == "BTCUSDT");
        REQUIRE(manifest_string(manifest, "venue") == "Binance USD-M");
        REQUIRE(manifest_string(manifest, "sha256") == crypto::storage::sha256_file(files[i]));
        REQUIRE(!manifest_string(manifest, "hostname").empty());
        bool continuation = false;
        REQUIRE(!manifest["continuation"].get(continuation));
        bool requires_previous = false;
        REQUIRE(!manifest["requires_previous_file"].get(requires_previous));
        REQUIRE(continuation == (i != 0));
        REQUIRE(requires_previous == continuation);
        REQUIRE(manifest_string(manifest, "previous_file") ==
                (i == 0 ? std::string() : files[i - 1].filename().string()));
        std::uint64_t size_bytes = 0;
        REQUIRE(!manifest["file_size_bytes"].get(size_bytes));
        REQUIRE(size_bytes == std::filesystem::file_size(files[i]));
        manifest_records += manifest_counter(manifest, "raw_records");
        manifest_depth += manifest_counter(manifest, "depth_events");
        manifest_trades += manifest_counter(manifest, "trade_events");
        manifest_snapshots += manifest_counter(manifest, "snapshots");
    }
    REQUIRE(manifest_records == records.size());

    const auto baseline_manifest = load_manifest(parser, baseline_path);
    bool baseline_continuation = true;
    REQUIRE(!baseline_manifest["continuation"].get(baseline_continuation));
    REQUIRE(!baseline_continuation);
    REQUIRE(manifest_string(baseline_manifest, "previous_file").empty());

    const auto instrument =
        crypto::replay::EventReplayer::instrument_from_raw_files(files, "BTCUSDT");
    crypto::replay::EventReplayer replayer(instrument);
    const auto unrotated = replayer.replay(baseline_path, export_a, 100);
    const auto rotated = replayer.replay(files, export_b, 100);

    // The research export is the actual product; rotation may not perturb a single byte of it.
    REQUIRE(unrotated == rotated);
    REQUIRE(unrotated.raw_records == records.size());
    REQUIRE(unrotated.depth_events == manifest_depth);
    REQUIRE(unrotated.trade_events == manifest_trades);
    REQUIRE(manifest_snapshots == 2);
    REQUIRE(unrotated.research_rows > 0);
    REQUIRE(unrotated.applied_depth_events > 0);
    REQUIRE(unrotated.best_bid_ticks.has_value());
    std::ifstream stream_a(export_a, std::ios::binary);
    std::ifstream stream_b(export_b, std::ios::binary);
    const std::string bytes_a{
        std::istreambuf_iterator<char>(stream_a), std::istreambuf_iterator<char>()};
    const std::string bytes_b{
        std::istreambuf_iterator<char>(stream_b), std::istreambuf_iterator<char>()};
    REQUIRE(!bytes_a.empty());
    REQUIRE(bytes_a == bytes_b);

    // A continuation file is not independently replayable, and a broken chain must fail loudly
    // rather than quietly emit a truncated research export.
    REQUIRE_THROWS(crypto::replay::validate_raw_chain({files[1], files[2]}));
    REQUIRE_THROWS(crypto::replay::validate_raw_chain({files[0], files[2]}));
    REQUIRE_THROWS(crypto::replay::validate_raw_chain({}));
    REQUIRE_THROWS(crypto::replay::validate_raw_chain({temp_dir / "missing.chft.zst"}));
    crypto::replay::validate_raw_chain(files);
}

void test_raw_rotation_shutdown_and_collision() {
    const auto temp_dir = std::filesystem::temp_directory_path() / "crypto_hft_l2_tests" / "rotation_edges";
    std::filesystem::remove_all(temp_dir);
    std::filesystem::create_directories(temp_dir);
    const auto records = rotation_dataset();

    crypto::storage::RotationConfig rotation;
    rotation.prefix = "EDGE";
    rotation.interval_ns = 2'000'000'000ULL;

    // Shutdown immediately before a rotation: the last complete record is finalized into the
    // current file and no empty successor is created.
    const auto before_dir = temp_dir / "before";
    rotation.directory = before_dir;
    crypto::storage::ManifestFinalizer before_finalizer;
    std::size_t before_boundary = 0;
    while (records[before_boundary].local_system_ns <
           records.front().local_system_ns + 2'000'000'000ULL) {
        ++before_boundary;
    }
    {
        crypto::storage::RawEventWriter writer(rotation, 128);
        writer.set_on_file_closed([&](crypto::storage::ClosedRawFile& file) {
            before_finalizer.submit(file);
        });
        feed(writer, {records.begin(), records.begin() + static_cast<std::ptrdiff_t>(before_boundary)});
        writer.close();
    }
    before_finalizer.wait_all();
    REQUIRE(before_finalizer.failures() == 0);
    const auto before_files = rotated_files(before_dir);
    REQUIRE(before_files.size() == 1);
    REQUIRE(read_all(before_files).size() == before_boundary);

    // Shutdown immediately after a rotation: both files exist, both are finalized, and the new
    // file holds exactly the first record of the new bucket.
    const auto after_dir = temp_dir / "after";
    rotation.directory = after_dir;
    crypto::storage::ManifestFinalizer after_finalizer;
    {
        crypto::storage::RawEventWriter writer(rotation, 128);
        writer.set_on_file_closed([&](crypto::storage::ClosedRawFile& file) {
            after_finalizer.submit(file);
        });
        feed(writer, {records.begin(), records.begin() + static_cast<std::ptrdiff_t>(before_boundary) + 1});
        writer.close();
    }
    after_finalizer.wait_all();
    REQUIRE(after_finalizer.failures() == 0);
    const auto after_files = rotated_files(after_dir);
    REQUIRE(after_files.size() == 2);
    REQUIRE(read_all({after_files[0]}).size() == before_boundary);
    REQUIRE(read_all({after_files[1]}).size() == 1);
    REQUIRE(read_all({after_files[1]}).front() == records[before_boundary]);
    simdjson::dom::parser parser;
    REQUIRE(std::filesystem::exists(std::filesystem::path(after_files[0]) += ".manifest.json"));
    REQUIRE(manifest_string(load_manifest(parser, after_files[1]), "previous_file") ==
            after_files[0].filename().string());

    // A restart inside the same bucket must not truncate the file already on disk.
    const auto collision_dir = temp_dir / "collision";
    rotation.directory = collision_dir;
    for (int run = 0; run < 2; ++run) {
        crypto::storage::RawEventWriter writer(rotation, 128);
        feed(writer, {records.begin(), records.begin() + 3});
        writer.close();
    }
    const auto collision_files = rotated_files(collision_dir);
    REQUIRE(collision_files.size() == 2);
    // '-' sorts before '.', so the collision-suffixed name is first by filename.
    REQUIRE(collision_files[0].filename().string() == "EDGE-20231114T221320Z-1.chft.zst");
    REQUIRE(collision_files[1].filename().string() == "EDGE-20231114T221320Z.chft.zst");
    REQUIRE(read_all({collision_files[0]}).size() == 3);
    REQUIRE(read_all({collision_files[1]}).size() == 3);
}

// ---- native dataset tests (appended into cpp/tests/test_main.cpp) ----

std::string read_zstd_text(const std::filesystem::path& path) {
    std::ifstream input(path, std::ios::binary);
    const std::string compressed{
        std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
    std::string text;
    ZSTD_DCtx* context = ZSTD_createDCtx();
    ZSTD_inBuffer in{compressed.data(), compressed.size(), 0};
    std::vector<char> chunk(1 << 16);
    while (in.pos < in.size) {
        ZSTD_outBuffer out{chunk.data(), chunk.size(), 0};
        const auto result = ZSTD_decompressStream(context, &out, &in);
        if (ZSTD_isError(result)) {
            ZSTD_freeDCtx(context);
            throw std::runtime_error("cannot decompress test export");
        }
        text.append(chunk.data(), out.pos);
    }
    ZSTD_freeDCtx(context);
    return text;
}

struct CsvTable {
    std::vector<std::string> header;
    std::vector<std::vector<std::string>> rows;

    [[nodiscard]] std::size_t index_of(const std::string& name) const {
        const auto found = std::find(header.begin(), header.end(), name);
        if (found == header.end()) {
            throw std::runtime_error("unknown column " + name);
        }
        return static_cast<std::size_t>(found - header.begin());
    }
    [[nodiscard]] const std::string& at(std::size_t row, const std::string& name) const {
        return rows.at(row).at(index_of(name));
    }
    [[nodiscard]] double number(std::size_t row, const std::string& name) const {
        return std::stod(at(row, name));
    }
};

CsvTable parse_csv(const std::string& text) {
    CsvTable table;
    std::istringstream stream(text);
    std::string line;
    bool first = true;
    while (std::getline(stream, line)) {
        std::vector<std::string> fields;
        std::string field;
        std::istringstream row(line);
        while (std::getline(row, field, ',')) {
            fields.push_back(field);
        }
        // getline drops a trailing empty field; every row has the same width as the header.
        if (!line.empty() && line.back() == ',') {
            fields.emplace_back();
        }
        if (first) {
            table.header = std::move(fields);
            first = false;
        } else if (!line.empty()) {
            fields.resize(table.header.size());
            table.rows.push_back(std::move(fields));
        }
    }
    return table;
}

// Raw capture exercising every boundary the native dataset must respect: an initial
// synchronization, a trade-stream outage, a depth reconnect with a replacement snapshot, and
// trades that land exactly on a decision instant.
std::filesystem::path write_native_fixture(const std::filesystem::path& path) {
    constexpr std::uint64_t ms = 1'000'000ULL;
    auto snapshot = [](std::uint64_t update_id) {
        std::ostringstream out;
        out << R"({"lastUpdateId":)" << update_id << R"(,"E":1000000,"T":999999,"bids":[)"
            << R"(["60000.00","1.000"],["59999.90","2.000"],["59999.80","3.000"]],"asks":[)"
            << R"(["60000.10","1.000"],["60000.20","2.000"],["60000.30","3.000"]]})";
        return out.str();
    };
    auto depth = [](std::uint64_t first_id, std::uint64_t final_id, std::uint64_t previous,
                    const std::string& bids, const std::string& asks) {
        std::ostringstream out;
        out << R"({"e":"depthUpdate","E":1000100,"T":1000099,"s":"BTCUSDT","U":)" << first_id
            << R"(,"u":)" << final_id << R"(,"pu":)" << previous << R"(,"b":[)" << bids
            << R"(],"a":[)" << asks << "]}";
        return out.str();
    };
    auto trade = [](const char* price, const char* qty, bool buyer_is_maker) {
        std::ostringstream out;
        out << R"({"e":"aggTrade","E":1000150,"s":"BTCUSDT","a":1,"p":")" << price
            << R"(","q":")" << qty << R"(","f":1,"l":1,"T":1000149,"m":)"
            << (buyer_is_maker ? "true" : "false") << "}";
        return out.str();
    };

    crypto::storage::RawEventWriter writer(path, 128);
    auto write = [&](std::uint64_t time_ns, crypto::storage::RawEventType type,
                     std::string stream, std::string payload) {
        writer.write({time_ns, time_ns, 1000099, type, "session", std::move(stream),
                      std::move(payload)});
    };
    write(1000 * ms, crypto::storage::RawEventType::InstrumentInfo, "exchangeInfo",
          fixture("exchange_info.json"));
    write(1001 * ms, crypto::storage::RawEventType::ConnectionOpen, "btcusdt@aggTrade", "{}");
    write(1002 * ms, crypto::storage::RawEventType::ConnectionOpen, "btcusdt@depth@100ms", "{}");
    write(1003 * ms, crypto::storage::RawEventType::DepthSnapshot, "fapi/v1/depth",
          snapshot(100));
    // Synchronization completes on the first event bridging the snapshot, so segment 1 starts
    // at 1150ms rather than at the snapshot itself. The best bid grows then shrinks, so the
    // per-level add and remove counters must both be non-zero and must not net each other out.
    write(1150 * ms, crypto::storage::RawEventType::DepthUpdate, "btcusdt@depth@100ms",
          depth(100, 101, 99, R"(["60000.00","4.000"])", ""));
    write(1250 * ms, crypto::storage::RawEventType::DepthUpdate, "btcusdt@depth@100ms",
          depth(102, 102, 101, R"(["60000.00","1.500"])", ""));
    // A print at exactly the 1300ms decision instant: it belongs to that row's trailing window
    // but must never fill the order the same row places.
    write(1300 * ms, crypto::storage::RawEventType::AggTrade, "btcusdt@aggTrade",
          trade("60000.00", "9.000", true));
    // A sell through the bid fills the 1300ms bid order completely.
    write(1450 * ms, crypto::storage::RawEventType::AggTrade, "btcusdt@aggTrade",
          trade("59999.90", "0.500", true));
    // The trade socket drops: the book is still synchronized but the segment must end here.
    write(1600 * ms, crypto::storage::RawEventType::ConnectionClose, "btcusdt@aggTrade",
          R"({"reason":"stream truncated"})");
    write(1700 * ms, crypto::storage::RawEventType::DepthUpdate, "btcusdt@depth@100ms",
          depth(103, 103, 102, R"(["60000.00","7.000"])", ""));
    write(1750 * ms, crypto::storage::RawEventType::ConnectionOpen, "btcusdt@aggTrade", "{}");
    // Segment 2 runs 1750ms .. 2100ms, then the depth socket drops and resynchronizes.
    write(1900 * ms, crypto::storage::RawEventType::DepthUpdate, "btcusdt@depth@100ms",
          depth(104, 104, 103, R"(["59999.90","5.000"])", ""));
    write(2100 * ms, crypto::storage::RawEventType::ConnectionClose, "btcusdt@depth@100ms",
          R"({"reason":"stream truncated"})");
    write(2200 * ms, crypto::storage::RawEventType::ConnectionOpen, "btcusdt@depth@100ms", "{}");
    write(2300 * ms, crypto::storage::RawEventType::DepthSnapshot, "fapi/v1/depth",
          snapshot(200));
    // Segment 3 runs 2350ms .. 2900ms.
    write(2350 * ms, crypto::storage::RawEventType::DepthUpdate, "btcusdt@depth@100ms",
          depth(200, 201, 199, R"(["59999.90","6.000"])", ""));
    write(2550 * ms, crypto::storage::RawEventType::DepthUpdate, "btcusdt@depth@100ms",
          depth(202, 202, 201, R"(["60000.00","2.000"])", ""));
    // A long gap with no events at all, then a move of the best ask. The rows emitted for the
    // grid points inside the gap must be marked out against the book as it stood then, not
    // against the book this event leaves behind.
    write(2750 * ms, crypto::storage::RawEventType::DepthUpdate, "btcusdt@depth@100ms",
          depth(203, 203, 202, "", R"(["60000.10","0.000"])"));
    write(2900 * ms, crypto::storage::RawEventType::ConnectionClose, "btcusdt@depth@100ms",
          R"({"reason":"collector stop"})");
    write(2901 * ms, crypto::storage::RawEventType::ConnectionClose, "btcusdt@aggTrade",
          R"({"reason":"collector stop"})");
    writer.close();
    return path;
}

crypto::research::NativeDatasetConfig native_test_config() {
    crypto::research::NativeDatasetConfig config;
    config.grid_ms = 100;
    config.order_lots = 5;
    config.fill_horizon_ms = 300;
    config.fill_ms = {100, 200, 300};
    config.postfill_ms = {100, 100, 100, 100};
    config.markout_ms = {100, 100, 200, 200, 300};
    return config;
}

void test_native_dataset_segments_and_boundaries() {
    const auto temp_dir = std::filesystem::temp_directory_path() / "crypto_hft_l2_tests";
    std::filesystem::create_directories(temp_dir);
    const auto raw_path = write_native_fixture(temp_dir / "native-fixture.chft.zst");
    const auto dataset_path = temp_dir / "native-fixture.csv.zst";

    const auto instrument =
        crypto::replay::EventReplayer::instrument_from_raw_file(raw_path, "BTCUSDT");
    crypto::research::NativeDatasetSummary summary;
    {
        crypto::research::NativeDatasetExporter exporter(dataset_path, native_test_config());
        crypto::replay::EventReplayer replayer(instrument);
        const auto replay = replayer.replay(
            raw_path, std::nullopt, 100, 0, std::numeric_limits<std::uint64_t>::max(), false,
            &exporter);
        REQUIRE(replay.parse_failures == 0);
        REQUIRE(replay.crossed_book_states == 0);
        REQUIRE(replay.snapshots == 2);
        summary = exporter.summary();
    }

    // A trade-stream outage ends a segment even though the book stayed synchronized, and a
    // replacement snapshot starts a third.
    REQUIRE(summary.segments == 3);
    REQUIRE(summary.missing_initial_sync == 0);
    REQUIRE(summary.unrecoverable_gaps == 0);
    REQUIRE(summary.segment_qc.size() == 3);
    REQUIRE(summary.segment_qc[0].close_reason == "trade_stream_down");
    REQUIRE(summary.segment_qc[1].close_reason == "depth_desync");
    REQUIRE(summary.segment_qc[2].close_reason == "depth_desync");
    // Three depth events land outside every segment: the two that complete synchronization
    // after a snapshot, whose effect is already in the segment's opening book state, and the
    // 1700ms update that arrived while the trade stream was down.
    REQUIRE(summary.skipped_depth_events == 3);

    const auto table = parse_csv(read_zstd_text(dataset_path));
    REQUIRE(!table.rows.empty());
    for (const auto& row : table.rows) {
        REQUIRE(row.size() == table.header.size());
    }

    constexpr std::uint64_t ms = 1'000'000ULL;
    std::map<std::string, std::vector<std::size_t>> by_segment;
    for (std::size_t i = 0; i < table.rows.size(); ++i) {
        by_segment[table.at(i, "segment_id")].push_back(i);
    }
    REQUIRE(by_segment.size() == 3);

    // Every row sits inside its own segment, and the grid is aligned to absolute epoch time.
    for (const auto& segment : summary.segment_qc) {
        const auto& indices = by_segment.at(std::to_string(segment.segment_id));
        REQUIRE(!indices.empty());
        for (const auto index : indices) {
            const auto stamp = std::stoull(table.at(index, "timestamp_ns"));
            REQUIRE(stamp >= segment.start_ns);
            REQUIRE(stamp <= segment.end_ns);
            REQUIRE(stamp % (100 * ms) == 0);
        }
    }

    // No trailing window crosses a segment start. The first row of segment 2 reports no flow at
    // all, even though a depth update at 1700ms and two trades happened shortly before it, and
    // the widest window still sees nothing one interval later beyond the in-segment update.
    const auto second_first = by_segment.at("2").front();
    REQUIRE(table.at(second_first, "timestamp_ns") == "1800000000");
    for (const char* column : {"buy_qty_5000ms", "sell_qty_5000ms", "trade_count_5000ms",
                               "depth_event_count_5000ms", "bid_depth_add_l1_5000ms",
                               "bid_depth_remove_l1_5000ms"}) {
        REQUIRE(table.at(second_first, column) == "0");
    }
    const auto second_next = by_segment.at("2")[1];
    REQUIRE(table.at(second_next, "depth_event_count_5000ms") == "1");
    REQUIRE(table.at(second_next, "trade_count_5000ms") == "0");

    // No target crosses a segment end: the last row of every segment has no forward markout,
    // no fill flag and no passive outcome at all, rather than a fabricated zero.
    for (const auto& [segment_id, indices] : by_segment) {
        (void)segment_id;
        const auto last = indices.back();
        for (const char* column : {"markout_100ms_ticks", "markout_300ms_ticks",
                                   "bid_fill_100ms", "ask_fill_100ms",
                                   "bid_fill_before_observed_mid_adverse", "bid_time_to_fill_ms",
                                   "bid_postfill_markout_100ms_ticks"}) {
            REQUIRE(table.at(last, column).empty());
        }
    }

    const auto find_row = [&](std::uint64_t stamp_ns) {
        for (std::size_t i = 0; i < table.rows.size(); ++i) {
            if (std::stoull(table.at(i, "timestamp_ns")) == stamp_ns) {
                return i;
            }
        }
        throw std::runtime_error("missing expected sample");
    };

    // Per-level depth flow is causal and keeps additions and removals apart. The 1250ms update
    // takes 2.500 BTC off the best bid; the 100 ms window at 1200ms cannot see it yet, the
    // 100 ms window at 1300ms does, and the 250 ms window still carries it at 1400ms.
    const auto at_1200 = find_row(1200 * ms);
    const auto at_1300 = find_row(1300 * ms);
    const auto at_1400 = find_row(1400 * ms);
    REQUIRE(table.at(at_1200, "bid_depth_remove_l1_100ms") == "0");
    REQUIRE(table.at(at_1200, "bid_depth_remove_l1_250ms") == "0");
    REQUIRE(table.at(at_1300, "bid_depth_remove_l1_100ms") == "2500");
    REQUIRE(table.at(at_1300, "bid_depth_add_l1_100ms") == "0");
    REQUIRE(table.at(at_1400, "bid_depth_remove_l1_100ms") == "0");
    REQUIRE(table.at(at_1400, "bid_depth_remove_l1_250ms") == "2500");
    // A pure addition one level down is booked as an add at the rank it takes, never as a
    // removal, and never leaks into the earlier row.
    REQUIRE(table.at(find_row(2600 * ms), "bid_depth_add_l1_100ms") == "1000");
    REQUIRE(table.at(find_row(2500 * ms), "bid_depth_add_l1_100ms") == "0");

    // Grid points emitted in one batch after a quiet gap are marked out against the state that
    // actually held at each horizon. The best ask moves at 2750ms, so the 2600ms row still sees
    // an unchanged mid one interval later while the 2700ms row sees the move.
    REQUIRE(table.number(find_row(2600 * ms), "markout_100ms_ticks") == 0.0);
    REQUIRE(table.number(find_row(2700 * ms), "markout_100ms_ticks") == 0.5);
    REQUIRE(table.number(find_row(2600 * ms), "markout_200ms_ticks") == 0.5);

    // The print stamped at the 1300ms decision instant is inside that row's trailing window and
    // is therefore visible as a feature.
    REQUIRE(table.at(at_1300, "sell_qty_100ms") == "9000");
    // It must not fill the order the same row places: only the later trade-through resolves it.
    REQUIRE(table.at(at_1300, "bid_queue_ahead_lots") == "1500");
    REQUIRE(table.at(at_1300, "bid_fill_100ms") == "0");
    REQUIRE(table.number(at_1300, "bid_time_to_fill_ms") == 150.0);
    REQUIRE(table.at(at_1300, "bid_fill_via_trade_through") == "1");

    // The same print is strictly after the 1200ms placement, so it does advance that order's
    // queue: the whole 4.000 BTC displayed at the quote is assumed to be ahead of it.
    REQUIRE(table.at(at_1200, "bid_queue_ahead_lots") == "4000");
    REQUIRE(table.at(at_1200, "bid_fill_100ms") == "1");
    REQUIRE(table.number(at_1200, "bid_time_to_fill_ms") == 100.0);
    REQUIRE(table.at(at_1200, "bid_fill_via_trade_through") == "0");

    // Feed asynchrony made explicit. Both 1200ms and 1300ms bid orders are recorded as filled
    // from the aggressive-trade stream, yet the depth stream never shows the quote leaving or
    // the touch moving before the segment ends. A race between a trade-stream event and a
    // depth-stream event is therefore not a statement about economic sequencing on its own.
    for (const auto row : {at_1200, at_1300}) {
        REQUIRE(!table.at(row, "bid_time_to_fill_ms").empty());
        REQUIRE(table.at(row, "bid_time_to_quote_gone_ms").empty());
        REQUIRE(table.at(row, "bid_time_to_best_adverse_ms").empty());
        REQUIRE(table.at(row, "bid_time_to_mid_adverse_ms").empty());
    }

    // In segment 3 the best ask is removed at 2750ms. That is adverse for a resting ask and is
    // observed on the depth stream alone, and it moves the mid by only half a tick, so the
    // book-race latencies fire while the one-tick mid-adverse race does not. The three
    // definitions are genuinely different events, not restatements of each other.
    const auto at_2700 = find_row(2700 * ms);
    REQUIRE(table.number(at_2700, "ask_time_to_best_adverse_ms") == 50.0);
    REQUIRE(table.number(at_2700, "ask_time_to_quote_gone_ms") == 50.0);
    REQUIRE(table.at(at_2700, "ask_time_to_mid_adverse_ms").empty());
    REQUIRE(table.number(find_row(2600 * ms), "ask_time_to_best_adverse_ms") == 150.0);
    // The bid side is untouched by that event.
    REQUIRE(table.at(at_2700, "bid_time_to_best_adverse_ms").empty());
    REQUIRE(table.at(at_2700, "bid_time_to_quote_gone_ms").empty());

    // Activity features are in-segment only: the first row of segment 3 has no prior trade and
    // no prior mid change to measure against, and the window counters agree.
    const auto third_first = by_segment.at("3").front();
    REQUIRE(table.at(third_first, "time_since_trade_ms").empty());
    REQUIRE(table.at(third_first, "bbo_change_count_5000ms") == "0");
    REQUIRE(table.at(third_first, "backward_mid_abs_change_ticks_5000ms") == "0");
    // The 2750ms event is the segment's first mid change; one interval later it is visible.
    REQUIRE(table.number(find_row(2800 * ms), "backward_mid_abs_change_ticks_100ms") == 0.5);
    REQUIRE(table.number(find_row(2800 * ms), "time_since_mid_change_ms") == 50.0);
}

void test_native_dataset_determinism_and_replay_invariance() {
    const auto temp_dir = std::filesystem::temp_directory_path() / "crypto_hft_l2_tests";
    std::filesystem::create_directories(temp_dir);
    const auto raw_path = write_native_fixture(temp_dir / "native-determinism.chft.zst");
    const auto instrument =
        crypto::replay::EventReplayer::instrument_from_raw_file(raw_path, "BTCUSDT");

    const auto run = [&](const std::filesystem::path& output) {
        crypto::research::NativeDatasetExporter exporter(output, native_test_config());
        crypto::replay::EventReplayer replayer(instrument);
        return replayer.replay(
            raw_path, std::nullopt, 100, 0, std::numeric_limits<std::uint64_t>::max(), false,
            &exporter);
    };
    const auto first_path = temp_dir / "native-determinism-1.csv.zst";
    const auto second_path = temp_dir / "native-determinism-2.csv.zst";
    const auto first = run(first_path);
    const auto second = run(second_path);
    REQUIRE(first == second);

    std::ifstream first_stream(first_path, std::ios::binary);
    std::ifstream second_stream(second_path, std::ios::binary);
    const std::string first_bytes{
        std::istreambuf_iterator<char>(first_stream), std::istreambuf_iterator<char>()};
    const std::string second_bytes{
        std::istreambuf_iterator<char>(second_stream), std::istreambuf_iterator<char>()};
    REQUIRE(!first_bytes.empty());
    REQUIRE(first_bytes == second_bytes);

    // Raw replay results are invariant to research export: the observer only reads state.
    crypto::replay::EventReplayer bare_replayer(instrument);
    const auto bare = bare_replayer.replay(raw_path);
    REQUIRE(bare == first);
}

void test_native_queue_matches_frozen_passive_index() {
    // The streaming fill simulator must reproduce the conservative visible-queue rules already
    // frozen in PassiveTradeIndex: the whole displayed quantity is ahead of the order, only
    // prints at the quote price advance the queue, and a print beyond it fills completely.
    const auto quote_price = std::int64_t{600000};
    const auto order_lots = std::int64_t{5};
    const auto queue_ahead = std::int64_t{1500};

    struct Print {
        std::uint64_t local_us;
        std::int64_t price;
        std::int64_t qty;
        bool aggressive_buy;
    };
    const std::vector<Print> prints{
        {1300, 600000, 9000, false},
        {1350, 599999, 500, false},
        {1400, 600000, 100, false},
    };

    crypto::research::PassiveTradeIndex index;
    for (const auto& print : prints) {
        index.add_trade(passive_trade(
            print.local_us,
            print.local_us,
            print.aggressive_buy ? crypto::historical::AggressorSide::Buy
                                 : crypto::historical::AggressorSide::Sell,
            print.price,
            print.qty));
    }
    index.finalize();

    for (const std::uint64_t placement : {std::uint64_t{1200}, std::uint64_t{1300},
                                          std::uint64_t{1320}}) {
        const auto expected = index.evaluate(
            crypto::research::PassiveQuoteSide::Bid, quote_price, order_lots, queue_ahead,
            placement, 1500);
        // The same rules, evaluated by walking the prints forward exactly as the exporter does.
        std::int64_t consumed = 0;
        bool filled = false;
        bool through = false;
        std::uint64_t fill_time = 0;
        for (const auto& print : prints) {
            if (print.local_us <= placement || filled || print.aggressive_buy) {
                continue;
            }
            if (print.price > quote_price) {
                continue;
            }
            if (print.price == quote_price) {
                consumed += print.qty;
            } else {
                consumed = queue_ahead + order_lots;
                through = true;
            }
            if (consumed >= queue_ahead + order_lots) {
                filled = true;
                fill_time = print.local_us;
            }
        }
        REQUIRE(filled == (expected.state == crypto::research::PassiveFillState::Full));
        if (filled) {
            REQUIRE(fill_time == expected.full_fill_local_time_us);
            REQUIRE(through == expected.filled_by_trade_through);
        }
    }
}


// Raw capture designed so that the alpha and beta axes of the queue grid produce different,
// hand-checkable fill outcomes on the same event stream.
std::filesystem::path write_queue_fixture(const std::filesystem::path& path) {
    constexpr std::uint64_t ms = 1'000'000ULL;
    auto snapshot = []() {
        return std::string(
            R"({"lastUpdateId":100,"E":1000000,"T":999999,"bids":[)"
            R"(["60000.00","4.000"],["59999.90","2.000"],["59999.80","3.000"]],"asks":[)"
            R"(["60000.10","1.000"],["60000.20","2.000"],["60000.30","3.000"]]})");
    };
    auto depth = [](std::uint64_t first_id, std::uint64_t final_id, std::uint64_t previous,
                    const std::string& bids) {
        std::ostringstream out;
        out << R"({"e":"depthUpdate","E":1000100,"T":1000099,"s":"BTCUSDT","U":)" << first_id
            << R"(,"u":)" << final_id << R"(,"pu":)" << previous << R"(,"b":[)" << bids
            << R"(],"a":[]})";
        return out.str();
    };
    auto trade = [](const char* price, const char* qty) {
        std::ostringstream out;
        out << R"({"e":"aggTrade","E":1000150,"s":"BTCUSDT","a":1,"p":")" << price
            << R"(","q":")" << qty << R"(","f":1,"l":1,"T":1000149,"m":true})";
        return out.str();
    };

    crypto::storage::RawEventWriter writer(path, 128);
    auto write = [&](std::uint64_t time_ns, crypto::storage::RawEventType type,
                     std::string stream, std::string payload) {
        writer.write({time_ns, time_ns, 1000099, type, "session", std::move(stream),
                      std::move(payload)});
    };
    write(1000 * ms, crypto::storage::RawEventType::InstrumentInfo, "exchangeInfo",
          fixture("exchange_info.json"));
    write(1001 * ms, crypto::storage::RawEventType::ConnectionOpen, "btcusdt@aggTrade", "{}");
    write(1002 * ms, crypto::storage::RawEventType::ConnectionOpen, "btcusdt@depth@100ms", "{}");
    write(1003 * ms, crypto::storage::RawEventType::DepthSnapshot, "fapi/v1/depth", snapshot());
    // Bridges the snapshot without changing anything, so the segment opens at 1150ms.
    write(1150 * ms, crypto::storage::RawEventType::DepthUpdate, "btcusdt@depth@100ms",
          depth(100, 101, 99, R"(["60000.00","4.000"])"));
    // 2.000 BTC leaves the best bid with no aggressive print to explain it: exactly the
    // unexplained removal the beta axis is defined over.
    write(1250 * ms, crypto::storage::RawEventType::DepthUpdate, "btcusdt@depth@100ms",
          depth(102, 102, 101, R"(["60000.00","2.000"])"));
    write(1300 * ms, crypto::storage::RawEventType::AggTrade, "btcusdt@aggTrade",
          trade("60000.00", "0.010"));
    // Added after placement, so it queues behind the hypothetical order, and removed again.
    // Beta must charge that removal against the addition and credit nothing.
    write(1400 * ms, crypto::storage::RawEventType::DepthUpdate, "btcusdt@depth@100ms",
          depth(103, 103, 102, R"(["60000.00","5.000"])"));
    write(1450 * ms, crypto::storage::RawEventType::DepthUpdate, "btcusdt@depth@100ms",
          depth(104, 104, 103, R"(["60000.00","2.000"])"));
    write(1700 * ms, crypto::storage::RawEventType::ConnectionClose, "btcusdt@depth@100ms",
          R"({"reason":"collector stop"})");
    write(1701 * ms, crypto::storage::RawEventType::ConnectionClose, "btcusdt@aggTrade",
          R"({"reason":"collector stop"})");
    writer.close();
    return path;
}

void test_queue_sensitivity_alpha_and_beta_axes() {
    const auto temp_dir = std::filesystem::temp_directory_path() / "crypto_hft_l2_tests";
    std::filesystem::create_directories(temp_dir);
    const auto raw_path = write_queue_fixture(temp_dir / "queue-fixture.chft.zst");
    const auto fills_path = temp_dir / "queue-fills.csv.zst";
    const auto mid_path = temp_dir / "queue-mid.csv.zst";

    crypto::research::QueueSensitivityConfig config;
    config.placement_interval_ms = 100;
    config.observation_window_ms = 400;
    config.order_lots = 5;
    const auto instrument =
        crypto::replay::EventReplayer::instrument_from_raw_file(raw_path, "BTCUSDT");
    crypto::research::QueueSensitivitySummary summary;
    {
        crypto::research::QueueSensitivityObserver observer(fills_path, mid_path, config);
        crypto::replay::EventReplayer replayer(instrument);
        const auto replay = replayer.replay(
            raw_path, std::nullopt, 100, 0, std::numeric_limits<std::uint64_t>::max(), false,
            &observer);
        REQUIRE(replay.parse_failures == 0);
        summary = observer.summary();
    }
    REQUIRE(summary.segments == 1);
    REQUIRE(summary.unexplained_removal_lots > 0);
    // The 3.000 BTC that joined the queue after placement and left again is charged against the
    // addition, never credited as queue advancement.
    REQUIRE(summary.behind_attributed_removal_lots >= 3000);

    const auto table = parse_csv(read_zstd_text(fills_path));
    REQUIRE(!table.rows.empty());
    constexpr std::uint64_t ms = 1'000'000ULL;

    const auto find = [&](std::uint64_t placement_ns, const std::string& side,
                          int alpha, int beta) {
        for (std::size_t i = 0; i < table.rows.size(); ++i) {
            if (std::stoull(table.at(i, "placement_ns")) == placement_ns &&
                table.at(i, "side") == side &&
                std::stoi(table.at(i, "alpha_pct")) == alpha &&
                std::stoi(table.at(i, "beta_pct")) == beta) {
                return i;
            }
        }
        throw std::runtime_error("missing queue cell");
    };

    // The best bid displays 4.000 BTC at the 1200ms placement, so alpha scales the queue ahead
    // and nothing else.
    for (const int alpha : {0, 25, 50, 75, 100}) {
        for (const int beta : {0, 25, 50, 75, 100}) {
            const auto row = find(1200 * ms, "0", alpha, beta);
            REQUIRE(std::stoll(table.at(row, "displayed_qty_lots")) == 4000);
            REQUIRE(std::stoll(table.at(row, "queue_ahead_lots")) == alpha * 40);
        }
    }

    // 2.000 BTC of unexplained removal arrives before the only aggressive print, so the order
    // fills exactly when alpha * 4000 - beta * 2000 falls to zero. Nine of the twenty-five
    // assumptions clear that bar and sixteen do not.
    std::size_t filled_cells = 0;
    for (const int alpha : {0, 25, 50, 75, 100}) {
        for (const int beta : {0, 25, 50, 75, 100}) {
            const auto row = find(1200 * ms, "0", alpha, beta);
            const auto queue_after_credit = alpha * 40 - beta * 20;
            const bool expected = queue_after_credit <= 0;
            const bool observed = std::stoull(table.at(row, "fill_ns")) != 0;
            REQUIRE(observed == expected);
            if (observed) {
                ++filled_cells;
                REQUIRE(std::stoull(table.at(row, "fill_ns")) == 1300 * ms);
                REQUIRE(table.at(row, "mechanism") == "1");  // at quote, not a trade-through
                REQUIRE(std::stoll(table.at(row, "filled_lots")) == config.order_lots);
            }
        }
    }
    REQUIRE(filled_cells == 9);

    // The ask side sees no aggressive print at all in this capture. An empty assumed queue is
    // not a fill: neither alpha nor beta can manufacture an execution without a trade, because
    // removals only ever advance the queue and never execute anything.
    for (const int alpha : {0, 25, 50, 75, 100}) {
        for (const int beta : {0, 25, 50, 75, 100}) {
            const auto row = find(1200 * ms, "1", alpha, beta);
            REQUIRE(table.at(row, "fill_ns") == "0");
            REQUIRE(table.at(row, "mechanism") == "0");
            REQUIRE(std::stoll(table.at(row, "filled_lots")) == 0);
        }
    }

    // The print stamped at the 1300ms placement instant informed that decision and must never
    // fill it, even with an empty assumed queue.
    for (const int beta : {0, 25, 50, 75, 100}) {
        REQUIRE(table.at(find(1300 * ms, "0", 0, beta), "fill_ns") == "0");
    }

    // Nothing is resolved past the segment edge: the observation window is clipped at the close.
    for (const auto& row : table.rows) {
        const auto placement = std::stoull(row[table.index_of("placement_ns")]);
        const auto observed_end = std::stoull(row[table.index_of("observed_end_ns")]);
        const auto fill = std::stoull(row[table.index_of("fill_ns")]);
        REQUIRE(observed_end <= placement + config.observation_window_ms * ms);
        if (fill != 0) {
            REQUIRE(fill > placement);
            REQUIRE(fill <= observed_end);
        }
    }
}

void test_queue_sensitivity_determinism() {
    const auto temp_dir = std::filesystem::temp_directory_path() / "crypto_hft_l2_tests";
    std::filesystem::create_directories(temp_dir);
    const auto raw_path = write_queue_fixture(temp_dir / "queue-determinism.chft.zst");
    const auto instrument =
        crypto::replay::EventReplayer::instrument_from_raw_file(raw_path, "BTCUSDT");
    crypto::research::QueueSensitivityConfig config;
    config.placement_interval_ms = 100;
    config.observation_window_ms = 400;

    const auto run = [&](const char* tag) {
        const auto fills = temp_dir / (std::string("queue-det-fills-") + tag + ".csv.zst");
        const auto mid = temp_dir / (std::string("queue-det-mid-") + tag + ".csv.zst");
        crypto::research::QueueSensitivityObserver observer(fills, mid, config);
        crypto::replay::EventReplayer replayer(instrument);
        const auto replay = replayer.replay(
            raw_path, std::nullopt, 100, 0, std::numeric_limits<std::uint64_t>::max(), false,
            &observer);
        std::ifstream stream(fills, std::ios::binary);
        return std::make_pair(
            replay,
            std::string(std::istreambuf_iterator<char>(stream),
                        std::istreambuf_iterator<char>()));
    };
    const auto first = run("a");
    const auto second = run("b");
    REQUIRE(first.first == second.first);
    REQUIRE(!first.second.empty());
    REQUIRE(first.second == second.second);
}



// Raw capture whose best bid is added to, reduced with and without an explaining print,
// replenished, removed entirely and then recreated at the same price.
std::filesystem::path write_lifecycle_fixture(const std::filesystem::path& path) {
    constexpr std::uint64_t ms = 1'000'000ULL;
    const std::string snapshot =
        R"({"lastUpdateId":100,"E":1000000,"T":999999,"bids":[)"
        R"(["60000.00","4.000"],["59999.90","2.000"],["59999.80","3.000"]],"asks":[)"
        R"(["60000.10","1.000"],["60000.20","2.000"],["60000.30","3.000"]]})";
    auto depth = [](std::uint64_t first_id, std::uint64_t final_id, std::uint64_t previous,
                    const std::string& bids) {
        std::ostringstream out;
        out << R"({"e":"depthUpdate","E":1000100,"T":1000099,"s":"BTCUSDT","U":)" << first_id
            << R"(,"u":)" << final_id << R"(,"pu":)" << previous << R"(,"b":[)" << bids
            << R"(],"a":[]})";
        return out.str();
    };
    auto trade = [](const char* price, const char* qty) {
        std::ostringstream out;
        out << R"({"e":"aggTrade","E":1000150,"s":"BTCUSDT","a":1,"p":")" << price
            << R"(","q":")" << qty << R"(","f":1,"l":1,"T":1000149,"m":true})";
        return out.str();
    };

    crypto::storage::RawEventWriter writer(path, 128);
    auto write = [&](std::uint64_t time_ns, crypto::storage::RawEventType type,
                     std::string stream, std::string payload) {
        writer.write({time_ns, time_ns, 1000099, type, "session", std::move(stream),
                      std::move(payload)});
    };
    write(1000 * ms, crypto::storage::RawEventType::InstrumentInfo, "exchangeInfo",
          fixture("exchange_info.json"));
    write(1001 * ms, crypto::storage::RawEventType::ConnectionOpen, "btcusdt@aggTrade", "{}");
    write(1002 * ms, crypto::storage::RawEventType::ConnectionOpen, "btcusdt@depth@100ms", "{}");
    write(1003 * ms, crypto::storage::RawEventType::DepthSnapshot, "fapi/v1/depth", snapshot);
    write(1150 * ms, crypto::storage::RawEventType::DepthUpdate, "btcusdt@depth@100ms",
          depth(100, 101, 99, R"(["60000.00","4.000"])"));
    // Growth before anything has been removed: an addition, never a replenishment.
    write(1250 * ms, crypto::storage::RawEventType::DepthUpdate, "btcusdt@depth@100ms",
          depth(102, 102, 101, R"(["60000.00","5.000"])"));
    // A reduction with no print to account for it.
    write(1300 * ms, crypto::storage::RawEventType::DepthUpdate, "btcusdt@depth@100ms",
          depth(103, 103, 102, R"(["60000.00","3.000"])"));
    write(1350 * ms, crypto::storage::RawEventType::AggTrade, "btcusdt@aggTrade",
          trade("60000.00", "0.500"));
    // A reduction the preceding print fully accounts for.
    write(1400 * ms, crypto::storage::RawEventType::DepthUpdate, "btcusdt@depth@100ms",
          depth(104, 104, 103, R"(["60000.00","2.500"])"));
    // Growth after a reduction: this one is a replenishment.
    write(1450 * ms, crypto::storage::RawEventType::DepthUpdate, "btcusdt@depth@100ms",
          depth(105, 105, 104, R"(["60000.00","4.000"])"));
    // The level is emptied and the book steps down.
    write(1500 * ms, crypto::storage::RawEventType::DepthUpdate, "btcusdt@depth@100ms",
          depth(106, 106, 105, R"(["60000.00","0.000"])"));
    // The same price is recreated: a new episode, not a continuation of the old one.
    write(1550 * ms, crypto::storage::RawEventType::DepthUpdate, "btcusdt@depth@100ms",
          depth(107, 107, 106, R"(["60000.00","1.000"])"));
    write(1700 * ms, crypto::storage::RawEventType::ConnectionClose, "btcusdt@depth@100ms",
          R"({"reason":"collector stop"})");
    write(1701 * ms, crypto::storage::RawEventType::ConnectionClose, "btcusdt@aggTrade",
          R"({"reason":"collector stop"})");
    writer.close();
    return path;
}

void test_level_lifecycle_episodes_and_reconciliation() {
    const auto temp_dir = std::filesystem::temp_directory_path() / "crypto_hft_l2_tests";
    std::filesystem::create_directories(temp_dir);
    const auto raw_path = write_lifecycle_fixture(temp_dir / "lifecycle-fixture.chft.zst");
    const auto episodes_path = temp_dir / "lifecycle-episodes.csv.zst";
    const auto grid_path = temp_dir / "lifecycle-grid.csv.zst";

    const auto instrument =
        crypto::replay::EventReplayer::instrument_from_raw_file(raw_path, "BTCUSDT");
    crypto::research::LevelLifecycleSummary summary;
    {
        crypto::research::LevelLifecycleObserver observer(
            episodes_path, grid_path, crypto::research::LevelLifecycleConfig{});
        crypto::replay::EventReplayer replayer(instrument);
        const auto replay = replayer.replay(
            raw_path, std::nullopt, 100, 0, std::numeric_limits<std::uint64_t>::max(), false,
            &observer);
        REQUIRE(replay.parse_failures == 0);
        summary = observer.summary();
    }
    REQUIRE(summary.segments == 1);

    const auto table = parse_csv(read_zstd_text(episodes_path));
    REQUIRE(!table.rows.empty());
    constexpr std::uint64_t ms = 1'000'000ULL;

    std::vector<std::size_t> bid_at_60000;
    for (std::size_t i = 0; i < table.rows.size(); ++i) {
        if (table.at(i, "side") == "0" && table.at(i, "price_ticks") == "600000") {
            bid_at_60000.push_back(i);
        }
    }
    // The same numerical price becoming best twice is two episodes with different identifiers.
    REQUIRE(bid_at_60000.size() == 2);
    REQUIRE(table.at(bid_at_60000[0], "level_episode_id") !=
            table.at(bid_at_60000[1], "level_episode_id"));

    const auto first = bid_at_60000[0];
    // The episode starts when the price becomes best, which is the event that completes
    // synchronization, not the snapshot.
    REQUIRE(std::stoull(table.at(first, "start_ns")) == 1150 * ms);
    REQUIRE(std::stoull(table.at(first, "end_ns")) == 1500 * ms);
    REQUIRE(table.at(first, "initial_qty") == "4000");
    REQUIRE(table.at(first, "max_qty") == "5000");
    REQUIRE(table.at(first, "min_qty") == "0");
    REQUIRE(table.at(first, "final_qty") == "0");
    // 1.000 added before any reduction plus 1.500 added after one.
    REQUIRE(table.at(first, "cum_add") == "2500");
    REQUIRE(table.at(first, "add_events") == "2");
    // Only the addition that followed a reduction counts as replenishment.
    REQUIRE(table.at(first, "cum_replenish") == "1500");
    REQUIRE(table.at(first, "replenish_events") == "1");
    REQUIRE(table.at(first, "cum_remove") == "6500");
    REQUIRE(table.at(first, "remove_events") == "3");
    // The single 0.500 print accounts for exactly 500 lots of the reduction that followed it;
    // the other 6000 lots are unexplained, which is not a claim that they were cancelled.
    REQUIRE(table.at(first, "cum_explained_remove") == "500");
    REQUIRE(table.at(first, "cum_unexplained_remove") == "6000");
    REQUIRE(table.at(first, "cum_trade_at_quote") == "500");
    REQUIRE(table.at(first, "prints_at_quote") == "1");
    REQUIRE(table.at(first, "close_reason") == "stepped_away");

    const auto second = bid_at_60000[1];
    REQUIRE(std::stoull(table.at(second, "start_ns")) == 1550 * ms);
    REQUIRE(table.at(second, "initial_qty") == "1000");
    // A fresh episode carries none of the previous one's history.
    REQUIRE(table.at(second, "cum_remove") == "0");
    REQUIRE(table.at(second, "cum_replenish") == "0");
    REQUIRE(table.at(second, "prints_at_quote") == "0");

    // The level the book stepped down to lived only between the two, and ended because a better
    // bid appeared inside rather than because it was consumed.
    std::size_t stepped_to = table.rows.size();
    for (std::size_t i = 0; i < table.rows.size(); ++i) {
        if (table.at(i, "side") == "0" && table.at(i, "price_ticks") == "599999") {
            stepped_to = i;
        }
    }
    REQUIRE(stepped_to != table.rows.size());
    REQUIRE(std::stoull(table.at(stepped_to, "start_ns")) == 1500 * ms);
    REQUIRE(std::stoull(table.at(stepped_to, "end_ns")) == 1550 * ms);
    REQUIRE(table.at(stepped_to, "close_reason") == "improved");

    // No episode may straddle the segment, and every one is closed inside it.
    for (const auto& row : table.rows) {
        REQUIRE(row[table.index_of("segment_id")] == "1");
        const auto start = std::stoull(row[table.index_of("start_ns")]);
        const auto end = std::stoull(row[table.index_of("end_ns")]);
        REQUIRE(end >= start);
        REQUIRE(start >= 1150 * ms);
        REQUIRE(end <= 1700 * ms);
    }

    // Cumulative state on the grid is causal: at 1200ms only the events up to then are in it.
    const auto grid = parse_csv(read_zstd_text(grid_path));
    std::size_t at_1200 = grid.rows.size();
    std::size_t at_1400 = grid.rows.size();
    for (std::size_t i = 0; i < grid.rows.size(); ++i) {
        if (grid.at(i, "side") != "0") {
            continue;
        }
        const auto stamp = std::stoull(grid.at(i, "timestamp_ns"));
        if (stamp == 1200 * ms) {
            at_1200 = i;
        } else if (stamp == 1400 * ms) {
            at_1400 = i;
        }
    }
    REQUIRE(at_1200 != grid.rows.size());
    REQUIRE(at_1400 != grid.rows.size());
    REQUIRE(grid.at(at_1200, "current_qty") == "4000");
    REQUIRE(grid.at(at_1200, "cum_add") == "0");
    REQUIRE(grid.at(at_1200, "cum_remove") == "0");
    REQUIRE(grid.at(at_1200, "prints_at_quote") == "0");
    // By 1400ms the addition, the unexplained reduction, the print and the reduction it
    // explains have all arrived, and nothing later has.
    REQUIRE(grid.at(at_1400, "current_qty") == "2500");
    REQUIRE(grid.at(at_1400, "cum_add") == "1000");
    REQUIRE(grid.at(at_1400, "cum_remove") == "2500");
    REQUIRE(grid.at(at_1400, "cum_explained_remove") == "500");
    REQUIRE(grid.at(at_1400, "cum_unexplained_remove") == "2000");
    REQUIRE(grid.at(at_1400, "cum_replenish") == "0");
    REQUIRE(grid.at(at_1400, "prints_at_quote") == "1");
}

void test_level_birth_cohort_placement() {
    const auto temp_dir = std::filesystem::temp_directory_path() / "crypto_hft_l2_tests";
    std::filesystem::create_directories(temp_dir);
    const auto raw_path = write_lifecycle_fixture(temp_dir / "birth-fixture.chft.zst");
    const auto fills_path = temp_dir / "birth-fills.csv.zst";
    const auto mid_path = temp_dir / "birth-mid.csv.zst";

    crypto::research::QueueSensitivityConfig config;
    config.mode = crypto::research::PlacementMode::LevelBirth;
    config.observation_window_ms = 400;
    const auto instrument =
        crypto::replay::EventReplayer::instrument_from_raw_file(raw_path, "BTCUSDT");
    {
        crypto::research::QueueSensitivityObserver observer(fills_path, mid_path, config);
        crypto::replay::EventReplayer replayer(instrument);
        const auto replay = replayer.replay(
            raw_path, std::nullopt, 100, 0, std::numeric_limits<std::uint64_t>::max(), false,
            &observer);
        REQUIRE(replay.parse_failures == 0);
    }
    const auto table = parse_csv(read_zstd_text(fills_path));
    constexpr std::uint64_t ms = 1'000'000ULL;

    std::map<std::pair<std::uint64_t, std::string>, std::size_t> placements;
    for (std::size_t i = 0; i < table.rows.size(); ++i) {
        if (table.at(i, "alpha_pct") == "100" && table.at(i, "beta_pct") == "0") {
            placements[{std::stoull(table.at(i, "placement_ns")), table.at(i, "side")}] = i;
        }
    }
    // One bid placement when the book steps down to 59999.90 and another when 60000.00 is
    // recreated; each arrives after the event that made its price best.
    REQUIRE(placements.count({1500 * ms, "0"}) == 1);
    REQUIRE(placements.count({1550 * ms, "0"}) == 1);
    const auto stepped = placements.at({1500 * ms, "0"});
    REQUIRE(table.at(stepped, "quote_px_ticks") == "599999");
    // Everything already displayed at that instant is ahead of the hypothetical order.
    REQUIRE(table.at(stepped, "displayed_qty_lots") == "2000");
    REQUIRE(table.at(stepped, "queue_ahead_lots") == "2000");
    const auto recreated = placements.at({1550 * ms, "0"});
    REQUIRE(table.at(recreated, "quote_px_ticks") == "600000");
    REQUIRE(table.at(recreated, "displayed_qty_lots") == "1000");

    // A price that never becomes best again produces no second placement, and no order can be
    // filled by anything stamped at or before its own placement instant.
    for (const auto& row : table.rows) {
        const auto placement = std::stoull(row[table.index_of("placement_ns")]);
        const auto fill = std::stoull(row[table.index_of("fill_ns")]);
        if (fill != 0) {
            REQUIRE(fill > placement);
        }
    }
}


}  // namespace

int main() {
    const std::vector<std::pair<const char*, std::function<void()>>> tests{
        {"precision", test_precision},
        {"parser_and_snapshot", test_parser_and_snapshot},
        {"incremental_and_crossed_invariant", test_incremental_and_crossed_invariant},
        {"sequence_state_machine", test_sequence_state_machine},
        {"raw_round_trip_and_replay_determinism", test_raw_round_trip_and_replay_determinism},
        {"tardis_normalized_replay_and_resnapshot", test_tardis_normalized_replay_and_resnapshot},
        {"microstructure_feature_formulas", test_microstructure_feature_formulas},
        {"tardis_trade_side_and_causal_bucketing", test_tardis_trade_side_and_causal_bucketing},
        {"tardis_daily_export_reset_and_determinism", test_tardis_daily_export_reset_and_determinism},
        {"passive_queue_conservative_fills", test_passive_queue_conservative_fills},
        {"passive_queue_trade_through_and_fixed_quote", test_passive_queue_trade_through_and_fixed_quote},
        {"raw_rotation_matches_unrotated_capture", test_raw_rotation_matches_unrotated_capture},
        {"raw_rotation_shutdown_and_collision", test_raw_rotation_shutdown_and_collision},
        {"native_dataset_segments_and_boundaries", test_native_dataset_segments_and_boundaries},
        {"native_dataset_determinism_and_replay_invariance",
         test_native_dataset_determinism_and_replay_invariance},
        {"native_queue_matches_frozen_passive_index", test_native_queue_matches_frozen_passive_index},
        {"queue_sensitivity_alpha_and_beta_axes", test_queue_sensitivity_alpha_and_beta_axes},
        {"queue_sensitivity_determinism", test_queue_sensitivity_determinism},
        {"level_lifecycle_episodes_and_reconciliation",
         test_level_lifecycle_episodes_and_reconciliation},
        {"level_birth_cohort_placement", test_level_birth_cohort_placement},
    };

    for (const auto& [name, test] : tests) {
        try {
            test();
            std::cout << "[test] " << name << " complete\n";
        } catch (const std::exception& error) {
            std::cerr << "[test] " << name << " threw: " << error.what() << '\n';
            ++failures;
        }
    }
    if (failures != 0) {
        std::cerr << failures << " test requirement(s) failed\n";
        return 1;
    }
    std::cout << "all tests passed\n";
    return 0;
}
