#include "exchange/binance/websocket_client.hpp"

#include <chrono>
#include <stdexcept>
#include <thread>

#include <boost/asio/connect.hpp>
#include <boost/asio/io_context.hpp>
#include <boost/asio/ip/tcp.hpp>
#include <boost/asio/ssl/context.hpp>
#include <boost/asio/ssl/host_name_verification.hpp>
#include <boost/beast/core.hpp>
#include <boost/beast/ssl.hpp>
#include <boost/beast/websocket.hpp>
#include <openssl/err.h>
#include <openssl/ssl.h>

namespace crypto::binance {
namespace {

namespace asio = boost::asio;
namespace beast = boost::beast;
namespace ssl = asio::ssl;
namespace websocket = beast::websocket;
using tcp = asio::ip::tcp;

struct WebSocketEndpoint {
    std::string host;
    std::string port;
    std::string target;
};

WebSocketEndpoint parse_wss_url(std::string_view url) {
    constexpr std::string_view scheme = "wss://";
    if (!url.starts_with(scheme)) {
        throw std::invalid_argument("only wss:// WebSocket URLs are supported");
    }

    url.remove_prefix(scheme.size());
    const auto path_at = url.find('/');
    const std::string_view authority = url.substr(0, path_at);
    const std::string_view target = path_at == std::string_view::npos ? std::string_view{"/"}
                                                                      : url.substr(path_at);
    if (authority.empty()) {
        throw std::invalid_argument("WebSocket URL has no host");
    }

    const auto port_at = authority.rfind(':');
    if (port_at == std::string_view::npos) {
        return {std::string(authority), "443", std::string(target)};
    }
    if (port_at == 0 || port_at + 1 == authority.size()) {
        throw std::invalid_argument("invalid WebSocket host or port");
    }
    return {std::string(authority.substr(0, port_at)),
            std::string(authority.substr(port_at + 1)),
            std::string(target)};
}

std::uint64_t system_time_ns() {
    return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count());
}

}  // namespace

std::string WebSocketClient::make_session_id(std::uint64_t sequence) const {
    return connection_name_ + "-" + std::to_string(system_time_ns()) + "-" + std::to_string(sequence);
}

void WebSocketClient::run(std::atomic_bool& stop, const WebSocketCallbacks& callbacks) const {
    const WebSocketEndpoint endpoint = parse_wss_url(url_);
    std::uint64_t session_sequence = 0;

    while (!stop.load(std::memory_order_relaxed)) {
        ++session_sequence;
        const auto session_id = make_session_id(session_sequence);
        std::string disconnect_reason;

        try {
            asio::io_context io_context;
            ssl::context tls_context(ssl::context::tls_client);
            tls_context.set_default_verify_paths();

            tcp::resolver resolver(io_context);
            websocket::stream<beast::ssl_stream<beast::tcp_stream>> socket(io_context, tls_context);

            if (!SSL_set_tlsext_host_name(socket.next_layer().native_handle(), endpoint.host.c_str())) {
                const auto error = static_cast<int>(::ERR_get_error());
                throw beast::system_error(
                    beast::error_code(error, asio::error::get_ssl_category()),
                    "failed to set TLS SNI hostname");
            }

            socket.next_layer().set_verify_mode(ssl::verify_peer);
            socket.next_layer().set_verify_callback(ssl::host_name_verification(endpoint.host));

            const auto addresses = resolver.resolve(endpoint.host, endpoint.port);
            beast::get_lowest_layer(socket).expires_after(std::chrono::seconds(15));
            beast::get_lowest_layer(socket).connect(addresses);
            socket.next_layer().handshake(ssl::stream_base::client);

            websocket::stream_base::timeout timeout = websocket::stream_base::timeout::suggested(
                beast::role_type::client);
            timeout.handshake_timeout = std::chrono::seconds(15);
            timeout.idle_timeout = std::chrono::seconds(15);
            timeout.keep_alive_pings = true;
            socket.set_option(timeout);
            socket.set_option(websocket::stream_base::decorator([](websocket::request_type& request) {
                request.set(beast::http::field::user_agent, "crypto-hft-l2/1.0");
            }));
            socket.handshake(endpoint.host, endpoint.target);

            if (callbacks.on_open) {
                callbacks.on_open(session_id);
            }

            const auto connected_at = std::chrono::steady_clock::now();
            beast::flat_buffer buffer;
            while (!stop.load(std::memory_order_relaxed)) {
                socket.read(buffer);
                if (!socket.got_text()) {
                    buffer.consume(buffer.size());
                    continue;
                }

                const std::string message = beast::buffers_to_string(buffer.data());
                buffer.consume(buffer.size());
                if (callbacks.on_message) {
                    callbacks.on_message(session_id, message);
                }

                // Binance disconnects market streams after 24 hours. Reconnect first so
                // each handover is explicit in the raw event log.
                if (std::chrono::steady_clock::now() - connected_at >= std::chrono::hours(23)) {
                    disconnect_reason = "proactive reconnect before 24-hour server limit";
                    break;
                }
            }

            const bool stopped_by_caller = stop.load(std::memory_order_relaxed);
            if (stopped_by_caller) {
                disconnect_reason = "collector stop";
            }
            beast::error_code close_error;
            socket.close(websocket::close_code::normal, close_error);
            if (!stopped_by_caller && disconnect_reason.empty() && close_error) {
                disconnect_reason = close_error.message();
            }
        } catch (const std::exception& error) {
            disconnect_reason = error.what();
        }

        if (callbacks.on_disconnect) {
            callbacks.on_disconnect(session_id, disconnect_reason);
        }
        if (!stop.load(std::memory_order_relaxed)) {
            std::this_thread::sleep_for(std::chrono::seconds(1));
        }
    }
}

}  // namespace crypto::binance
