#pragma once

#include <string_view>

#include "exchange/binance/instrument_info.hpp"
#include "exchange/binance/message_types.hpp"

namespace crypto::binance {

class MessageParser {
public:
    explicit MessageParser(const InstrumentInfo& instrument) : instrument_(instrument) {}

    [[nodiscard]] DepthSnapshot parse_snapshot(std::string_view json) const;
    [[nodiscard]] DepthEvent parse_depth_event(std::string_view json) const;
    [[nodiscard]] AggTradeEvent parse_agg_trade(std::string_view json) const;

private:
    const InstrumentInfo& instrument_;
};

}  // namespace crypto::binance
