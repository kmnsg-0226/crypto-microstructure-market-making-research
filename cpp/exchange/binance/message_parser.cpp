#include "exchange/binance/message_parser.hpp"

#include <stdexcept>
#include <string>

#include <simdjson.h>

namespace crypto::binance {
namespace {

simdjson::dom::element parse_payload(simdjson::dom::parser& parser, std::string_view json) {
    simdjson::dom::element root;
    if (const auto error = parser.parse(json).get(root); error) {
        throw std::invalid_argument(std::string("invalid Binance JSON: ") + simdjson::error_message(error));
    }
    simdjson::dom::element data;
    if (!root["data"].get(data)) {
        return data;
    }
    return root;
}

std::string get_string(simdjson::dom::element object, const char* field) {
    std::string_view value;
    if (const auto error = object[field].get(value); error) {
        throw std::invalid_argument(std::string("missing string field: ") + field);
    }
    return std::string(value);
}

std::int64_t get_i64(simdjson::dom::element object, const char* field, bool optional = false) {
    std::int64_t value{};
    if (const auto error = object[field].get(value); error) {
        if (optional && error == simdjson::NO_SUCH_FIELD) {
            return 0;
        }
        throw std::invalid_argument(std::string("missing integer field: ") + field);
    }
    return value;
}

std::uint64_t get_u64(simdjson::dom::element object, const char* field) {
    std::uint64_t value{};
    if (const auto error = object[field].get(value); error) {
        throw std::invalid_argument(std::string("missing unsigned integer field: ") + field);
    }
    return value;
}

std::vector<PriceLevel> parse_levels(
    simdjson::dom::element object,
    const char* field,
    const InstrumentInfo& instrument) {
    simdjson::dom::array rows;
    if (const auto error = object[field].get(rows); error) {
        throw std::invalid_argument(std::string("missing level array: ") + field);
    }

    std::vector<PriceLevel> result;
    for (auto row_element : rows) {
        simdjson::dom::array row;
        if (const auto error = row_element.get(row); error) {
            throw std::invalid_argument("book level is not an array");
        }
        auto it = row.begin();
        if (it == row.end()) {
            throw std::invalid_argument("book level is missing price");
        }
        std::string_view price;
        if (const auto error = (*it).get(price); error) {
            throw std::invalid_argument("book level price is not a string");
        }
        ++it;
        if (it == row.end()) {
            throw std::invalid_argument("book level is missing quantity");
        }
        std::string_view qty;
        if (const auto error = (*it).get(qty); error) {
            throw std::invalid_argument("book level quantity is not a string");
        }
        result.push_back({instrument.price_to_ticks(price), instrument.market_qty_to_lots(qty)});
    }
    return result;
}

}  // namespace

DepthSnapshot MessageParser::parse_snapshot(std::string_view json) const {
    simdjson::dom::parser parser;
    const auto payload = parse_payload(parser, json);
    DepthSnapshot snapshot;
    snapshot.last_update_id = get_u64(payload, "lastUpdateId");
    snapshot.event_time_ms = get_i64(payload, "E", true);
    snapshot.transaction_time_ms = get_i64(payload, "T", true);
    snapshot.bids = parse_levels(payload, "bids", instrument_);
    snapshot.asks = parse_levels(payload, "asks", instrument_);
    return snapshot;
}

DepthEvent MessageParser::parse_depth_event(std::string_view json) const {
    simdjson::dom::parser parser;
    const auto payload = parse_payload(parser, json);
    if (get_string(payload, "e") != "depthUpdate") {
        throw std::invalid_argument("unexpected event type for depth parser");
    }
    DepthEvent event;
    event.symbol = get_string(payload, "s");
    if (event.symbol != instrument_.symbol()) {
        throw std::invalid_argument("depth event symbol does not match instrument");
    }
    event.event_time_ms = get_i64(payload, "E");
    event.transaction_time_ms = get_i64(payload, "T");
    event.first_update_id = get_u64(payload, "U");
    event.final_update_id = get_u64(payload, "u");
    event.previous_final_update_id = get_u64(payload, "pu");
    event.bids = parse_levels(payload, "b", instrument_);
    event.asks = parse_levels(payload, "a", instrument_);
    return event;
}

AggTradeEvent MessageParser::parse_agg_trade(std::string_view json) const {
    simdjson::dom::parser parser;
    const auto payload = parse_payload(parser, json);
    if (get_string(payload, "e") != "aggTrade") {
        throw std::invalid_argument("unexpected event type for aggTrade parser");
    }
    AggTradeEvent event;
    event.symbol = get_string(payload, "s");
    if (event.symbol != instrument_.symbol()) {
        throw std::invalid_argument("aggTrade symbol does not match instrument");
    }
    event.event_time_ms = get_i64(payload, "E");
    event.transaction_time_ms = get_i64(payload, "T");
    event.aggregate_trade_id = get_u64(payload, "a");
    event.first_trade_id = get_u64(payload, "f");
    event.last_trade_id = get_u64(payload, "l");
    event.price_ticks = instrument_.price_to_ticks(get_string(payload, "p"));
    event.qty_lots = instrument_.market_qty_to_lots(get_string(payload, "q"));
    if (const auto error = payload["m"].get(event.buyer_is_maker); error) {
        throw std::invalid_argument("missing boolean field: m");
    }
    return event;
}

}  // namespace crypto::binance
