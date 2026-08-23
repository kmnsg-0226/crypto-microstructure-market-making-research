#pragma once

#include <atomic>
#include <cstdint>
#include <functional>
#include <string>
#include <string_view>

namespace crypto::binance {

struct WebSocketCallbacks {
    std::function<void(std::string_view)> on_open;
    std::function<void(std::string_view, std::string_view)> on_message;
    std::function<void(std::string_view, std::string_view)> on_disconnect;
};

class WebSocketClient {
public:
    WebSocketClient(std::string url, std::string connection_name)
        : url_(std::move(url)), connection_name_(std::move(connection_name)) {}

    void run(std::atomic_bool& stop, const WebSocketCallbacks& callbacks) const;

private:
    [[nodiscard]] std::string make_session_id(std::uint64_t sequence) const;

    std::string url_;
    std::string connection_name_;
};

}  // namespace crypto::binance
