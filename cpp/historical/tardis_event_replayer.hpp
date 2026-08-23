#pragma once

#include <cstdint>
#include <filesystem>
#include <string>
#include <vector>

#include "book/order_book.hpp"
#include "exchange/binance/instrument_info.hpp"
#include "exchange/binance/message_types.hpp"
#include "historical/tardis_l2_replayer.hpp"
#include "historical/tardis_trade_reader.hpp"

namespace crypto::historical {

struct TardisEventTradeFile {
    std::vector<TardisTrade> trades;
    std::uint64_t buy_trades{};
    std::uint64_t sell_trades{};
    std::uint64_t unknown_side_trades{};
    std::uint64_t timestamp_regressions{};
    std::uint64_t local_timestamp_regressions{};
};

[[nodiscard]] TardisEventTradeFile read_tardis_event_trade_file(
    const std::filesystem::path& path,
    const binance::DecimalIncrement& price_increment,
    const binance::DecimalIncrement& qty_increment);

class TardisEventObserver {
public:
    virtual ~TardisEventObserver() = default;
    virtual void before_depth_event(
        std::uint64_t exchange_timestamp_us,
        std::uint64_t local_timestamp_us,
        const binance::DepthEvent& event,
        const book::OrderBook& book) = 0;
    virtual void after_depth_event(
        std::uint64_t exchange_timestamp_us,
        std::uint64_t local_timestamp_us,
        const binance::DepthEvent& event,
        const book::OrderBook& book) = 0;
    virtual void before_snapshot(
        std::uint64_t exchange_timestamp_us,
        std::uint64_t local_timestamp_us,
        const book::OrderBook& book) = 0;
    virtual void after_snapshot(
        std::uint64_t exchange_timestamp_us,
        std::uint64_t local_timestamp_us,
        const book::OrderBook& book) = 0;
    virtual void finish(const book::OrderBook& book) = 0;
};

class TardisEventReplayer {
public:
    TardisEventReplayer(std::string tick_size, std::string step_size);
    [[nodiscard]] TardisReplaySummary replay_with_observer(
        const std::filesystem::path& input_path,
        TardisEventObserver& observer) const;

private:
    std::string tick_size_;
    std::string step_size_;
};

}  // namespace crypto::historical
