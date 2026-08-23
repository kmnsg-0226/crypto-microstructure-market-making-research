#pragma once

#include <cstdint>
#include <string>
#include <string_view>

namespace crypto::binance {

class DecimalIncrement {
public:
    explicit DecimalIncrement(std::string increment);

    [[nodiscard]] std::int64_t to_steps(std::string_view value) const;
    [[nodiscard]] std::string from_steps(std::int64_t steps) const;
    [[nodiscard]] std::string from_half_steps(std::int64_t steps_x2) const;
    [[nodiscard]] const std::string& text() const noexcept { return text_; }
    [[nodiscard]] int decimals() const noexcept { return decimals_; }

private:
    [[nodiscard]] std::int64_t parse_scaled(std::string_view value) const;
    [[nodiscard]] std::string format_scaled(std::int64_t scaled) const;

    std::string text_;
    int decimals_{};
    std::int64_t scale_{1};
    std::int64_t increment_units_{1};
};

class InstrumentInfo {
public:
    InstrumentInfo(
        std::string symbol,
        std::string contract_type,
        std::string status,
        std::string tick_size,
        std::string min_price,
        std::string max_price,
        std::string step_size,
        std::string min_qty,
        std::string max_qty);

    [[nodiscard]] static InstrumentInfo from_exchange_info_json(
        std::string_view json,
        std::string_view symbol);

    [[nodiscard]] std::int64_t price_to_ticks(std::string_view price) const;
    [[nodiscard]] std::string ticks_to_price(std::int64_t ticks) const;
    [[nodiscard]] std::string ticks_x2_to_price(std::int64_t ticks_x2) const;
    [[nodiscard]] std::int64_t qty_to_lots(std::string_view qty) const;
    [[nodiscard]] std::int64_t market_qty_to_lots(std::string_view qty) const;
    [[nodiscard]] std::string lots_to_qty(std::int64_t lots) const;

    [[nodiscard]] const std::string& symbol() const noexcept { return symbol_; }
    [[nodiscard]] const std::string& contract_type() const noexcept { return contract_type_; }
    [[nodiscard]] const std::string& status() const noexcept { return status_; }
    [[nodiscard]] const std::string& tick_size() const noexcept { return price_increment_.text(); }
    [[nodiscard]] const std::string& step_size() const noexcept { return qty_increment_.text(); }
    [[nodiscard]] std::int64_t min_price_ticks() const noexcept { return min_price_ticks_; }
    [[nodiscard]] std::int64_t max_price_ticks() const noexcept { return max_price_ticks_; }
    [[nodiscard]] std::int64_t min_qty_lots() const noexcept { return min_qty_lots_; }
    [[nodiscard]] std::int64_t max_qty_lots() const noexcept { return max_qty_lots_; }

private:
    std::string symbol_;
    std::string contract_type_;
    std::string status_;
    DecimalIncrement price_increment_;
    DecimalIncrement qty_increment_;
    std::int64_t min_price_ticks_{};
    std::int64_t max_price_ticks_{};
    std::int64_t min_qty_lots_{};
    std::int64_t max_qty_lots_{};
};

}  // namespace crypto::binance
