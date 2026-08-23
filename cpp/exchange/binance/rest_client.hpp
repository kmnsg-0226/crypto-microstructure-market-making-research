#pragma once

#include <string>
#include <string_view>

namespace crypto::binance {

class RestClient {
public:
    explicit RestClient(std::string base_url = "https://fapi.binance.com")
        : base_url_(std::move(base_url)) {}

    [[nodiscard]] std::string exchange_info() const;
    [[nodiscard]] std::string depth_snapshot(std::string_view symbol, int limit = 1000) const;
    [[nodiscard]] static std::string get(std::string_view url);

private:
    std::string base_url_;
};

}  // namespace crypto::binance
