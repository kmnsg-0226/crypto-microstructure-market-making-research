#include "exchange/binance/instrument_info.hpp"

#include <algorithm>
#include <cctype>
#include <limits>
#include <stdexcept>
#include <utility>

#include <simdjson.h>

namespace crypto::binance {
namespace {

std::int64_t pow10_checked(int exponent) {
    std::int64_t value = 1;
    for (int i = 0; i < exponent; ++i) {
        if (value > std::numeric_limits<std::int64_t>::max() / 10) {
            throw std::overflow_error("decimal scale exceeds int64 range");
        }
        value *= 10;
    }
    return value;
}

std::string required_string(simdjson::dom::element object, const char* field) {
    std::string_view value;
    const auto error = object[field].get(value);
    if (error) {
        throw std::invalid_argument(std::string("missing or invalid JSON string field: ") + field);
    }
    return std::string(value);
}

}  // namespace

DecimalIncrement::DecimalIncrement(std::string increment) : text_(std::move(increment)) {
    const auto dot = text_.find('.');
    decimals_ = dot == std::string::npos ? 0 : static_cast<int>(text_.size() - dot - 1);
    scale_ = pow10_checked(decimals_);
    increment_units_ = parse_scaled(text_);
    if (increment_units_ <= 0) {
        throw std::invalid_argument("increment must be positive");
    }
}

std::int64_t DecimalIncrement::parse_scaled(std::string_view value) const {
    if (value.empty()) {
        throw std::invalid_argument("empty decimal value");
    }

    bool negative = false;
    std::size_t pos = 0;
    if (value.front() == '+' || value.front() == '-') {
        negative = value.front() == '-';
        pos = 1;
    }
    if (pos == value.size()) {
        throw std::invalid_argument("invalid decimal sign without digits");
    }

    std::int64_t whole = 0;
    bool saw_digit = false;
    while (pos < value.size() && value[pos] != '.') {
        const char c = value[pos++];
        if (!std::isdigit(static_cast<unsigned char>(c))) {
            throw std::invalid_argument("invalid decimal character");
        }
        saw_digit = true;
        if (whole > (std::numeric_limits<std::int64_t>::max() - 9) / 10) {
            throw std::overflow_error("decimal value exceeds int64 range");
        }
        whole = whole * 10 + (c - '0');
    }

    std::int64_t fraction = 0;
    int used_decimals = 0;
    if (pos < value.size() && value[pos] == '.') {
        ++pos;
        while (pos < value.size()) {
            const char c = value[pos++];
            if (!std::isdigit(static_cast<unsigned char>(c))) {
                throw std::invalid_argument("invalid decimal character");
            }
            saw_digit = true;
            if (used_decimals < decimals_) {
                fraction = fraction * 10 + (c - '0');
                ++used_decimals;
            } else if (c != '0') {
                throw std::invalid_argument("decimal has precision beyond instrument increment");
            }
        }
    }
    if (!saw_digit) {
        throw std::invalid_argument("decimal has no digits");
    }
    while (used_decimals < decimals_) {
        fraction *= 10;
        ++used_decimals;
    }

    if (whole > (std::numeric_limits<std::int64_t>::max() - fraction) / scale_) {
        throw std::overflow_error("scaled decimal exceeds int64 range");
    }
    const std::int64_t scaled = whole * scale_ + fraction;
    return negative ? -scaled : scaled;
}

std::string DecimalIncrement::format_scaled(std::int64_t scaled) const {
    const bool negative = scaled < 0;
    const std::uint64_t magnitude = negative
        ? static_cast<std::uint64_t>(-(scaled + 1)) + 1
        : static_cast<std::uint64_t>(scaled);
    const auto scale = static_cast<std::uint64_t>(scale_);
    const auto whole = magnitude / scale;
    const auto fraction = magnitude % scale;

    std::string result = negative ? "-" : "";
    result += std::to_string(whole);
    if (decimals_ > 0) {
        result.push_back('.');
        std::string frac = std::to_string(fraction);
        result.append(static_cast<std::size_t>(decimals_) - frac.size(), '0');
        result += frac;
    }
    return result;
}

std::int64_t DecimalIncrement::to_steps(std::string_view value) const {
    const auto scaled = parse_scaled(value);
    if (scaled % increment_units_ != 0) {
        throw std::invalid_argument("value is not aligned to instrument increment");
    }
    return scaled / increment_units_;
}

std::string DecimalIncrement::from_steps(std::int64_t steps) const {
    const __int128 scaled = static_cast<__int128>(steps) * increment_units_;
    if (scaled > std::numeric_limits<std::int64_t>::max() ||
        scaled < std::numeric_limits<std::int64_t>::min()) {
        throw std::overflow_error("step conversion exceeds int64 range");
    }
    return format_scaled(static_cast<std::int64_t>(scaled));
}

std::string DecimalIncrement::from_half_steps(std::int64_t steps_x2) const {
    const __int128 scaled_x10 = static_cast<__int128>(steps_x2) * increment_units_ * 5;
    if (scaled_x10 > std::numeric_limits<std::int64_t>::max() ||
        scaled_x10 < std::numeric_limits<std::int64_t>::min()) {
        throw std::overflow_error("half-step conversion exceeds int64 range");
    }
    const auto value = static_cast<std::int64_t>(scaled_x10);
    const bool negative = value < 0;
    const std::uint64_t magnitude = negative
        ? static_cast<std::uint64_t>(-(value + 1)) + 1
        : static_cast<std::uint64_t>(value);
    const auto scale10 = static_cast<std::uint64_t>(scale_) * 10U;
    const auto whole = magnitude / scale10;
    const auto fraction = magnitude % scale10;
    std::string result = negative ? "-" : "";
    result += std::to_string(whole);
    const int output_decimals = decimals_ + 1;
    if (output_decimals > 0) {
        result.push_back('.');
        std::string frac = std::to_string(fraction);
        result.append(static_cast<std::size_t>(output_decimals) - frac.size(), '0');
        result += frac;
        while (result.size() > 1 && result.back() == '0' && result[result.size() - 2] != '.') {
            result.pop_back();
        }
    }
    return result;
}

InstrumentInfo::InstrumentInfo(
    std::string symbol,
    std::string contract_type,
    std::string status,
    std::string tick_size,
    std::string min_price,
    std::string max_price,
    std::string step_size,
    std::string min_qty,
    std::string max_qty)
    : symbol_(std::move(symbol)),
      contract_type_(std::move(contract_type)),
      status_(std::move(status)),
      price_increment_(std::move(tick_size)),
      qty_increment_(std::move(step_size)),
      min_price_ticks_(price_increment_.to_steps(min_price)),
      max_price_ticks_(price_increment_.to_steps(max_price)),
      min_qty_lots_(qty_increment_.to_steps(min_qty)),
      max_qty_lots_(qty_increment_.to_steps(max_qty)) {
    if (symbol_.empty() || contract_type_.empty() || status_.empty()) {
        throw std::invalid_argument("instrument identity fields cannot be empty");
    }
    if (min_price_ticks_ < 0 || max_price_ticks_ < min_price_ticks_ ||
        min_qty_lots_ < 0 || max_qty_lots_ < min_qty_lots_) {
        throw std::invalid_argument("invalid instrument filter bounds");
    }
}

InstrumentInfo InstrumentInfo::from_exchange_info_json(
    std::string_view json,
    std::string_view symbol) {
    simdjson::dom::parser parser;
    simdjson::dom::element root;
    if (const auto error = parser.parse(json).get(root); error) {
        throw std::invalid_argument(std::string("invalid exchangeInfo JSON: ") + simdjson::error_message(error));
    }

    simdjson::dom::array symbols;
    if (const auto error = root["symbols"].get(symbols); error) {
        throw std::invalid_argument("exchangeInfo is missing symbols array");
    }

    for (auto item : symbols) {
        const auto item_symbol = required_string(item, "symbol");
        if (item_symbol != symbol) {
            continue;
        }

        std::string tick_size;
        std::string min_price;
        std::string max_price;
        std::string step_size;
        std::string min_qty;
        std::string max_qty;

        simdjson::dom::array filters;
        if (const auto error = item["filters"].get(filters); error) {
            throw std::invalid_argument("instrument is missing filters array");
        }
        for (auto filter : filters) {
            const auto type = required_string(filter, "filterType");
            if (type == "PRICE_FILTER") {
                tick_size = required_string(filter, "tickSize");
                min_price = required_string(filter, "minPrice");
                max_price = required_string(filter, "maxPrice");
            } else if (type == "LOT_SIZE") {
                step_size = required_string(filter, "stepSize");
                min_qty = required_string(filter, "minQty");
                max_qty = required_string(filter, "maxQty");
            }
        }

        if (tick_size.empty() || step_size.empty()) {
            throw std::invalid_argument("instrument is missing PRICE_FILTER or LOT_SIZE");
        }
        return InstrumentInfo(
            item_symbol,
            required_string(item, "contractType"),
            required_string(item, "status"),
            tick_size,
            min_price,
            max_price,
            step_size,
            min_qty,
            max_qty);
    }

    throw std::invalid_argument("requested symbol not found in exchangeInfo");
}

std::int64_t InstrumentInfo::price_to_ticks(std::string_view price) const {
    const auto ticks = price_increment_.to_steps(price);
    if (ticks < min_price_ticks_ || ticks > max_price_ticks_) {
        throw std::out_of_range("price is outside exchange PRICE_FILTER bounds");
    }
    return ticks;
}

std::string InstrumentInfo::ticks_to_price(std::int64_t ticks) const {
    return price_increment_.from_steps(ticks);
}

std::string InstrumentInfo::ticks_x2_to_price(std::int64_t ticks_x2) const {
    return price_increment_.from_half_steps(ticks_x2);
}

std::int64_t InstrumentInfo::qty_to_lots(std::string_view qty) const {
    const auto lots = qty_increment_.to_steps(qty);
    if (lots != 0 && (lots < min_qty_lots_ || lots > max_qty_lots_)) {
        throw std::out_of_range("quantity is outside exchange LOT_SIZE bounds");
    }
    return lots;
}

std::int64_t InstrumentInfo::market_qty_to_lots(std::string_view qty) const {
    const auto lots = qty_increment_.to_steps(qty);
    if (lots < 0) {
        throw std::out_of_range("market-data quantity cannot be negative");
    }
    // A price level or aggregate trade may combine multiple valid orders and
    // therefore exceed the LOT_SIZE maximum for one submitted order.
    return lots;
}

std::string InstrumentInfo::lots_to_qty(std::int64_t lots) const {
    return qty_increment_.from_steps(lots);
}

}  // namespace crypto::binance
