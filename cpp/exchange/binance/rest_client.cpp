#include "exchange/binance/rest_client.hpp"

#include <mutex>
#include <stdexcept>

#include <curl/curl.h>

namespace crypto::binance {
namespace {

void ensure_curl_initialized() {
    static std::once_flag once;
    std::call_once(once, [] {
        if (curl_global_init(CURL_GLOBAL_DEFAULT) != CURLE_OK) {
            throw std::runtime_error("curl_global_init failed");
        }
    });
}

std::size_t write_callback(char* data, std::size_t size, std::size_t count, void* user) {
    const auto bytes = size * count;
    static_cast<std::string*>(user)->append(data, bytes);
    return bytes;
}

}  // namespace

std::string RestClient::get(std::string_view url) {
    ensure_curl_initialized();
    CURL* handle = curl_easy_init();
    if (handle == nullptr) {
        throw std::runtime_error("curl_easy_init failed");
    }

    std::string response;
    const std::string owned_url(url);
    curl_easy_setopt(handle, CURLOPT_URL, owned_url.c_str());
    curl_easy_setopt(handle, CURLOPT_WRITEFUNCTION, write_callback);
    curl_easy_setopt(handle, CURLOPT_WRITEDATA, &response);
    curl_easy_setopt(handle, CURLOPT_USERAGENT, "crypto-hft-l2/1.0");
    curl_easy_setopt(handle, CURLOPT_TIMEOUT, 15L);
    curl_easy_setopt(handle, CURLOPT_CONNECTTIMEOUT, 10L);

    const CURLcode code = curl_easy_perform(handle);
    long status = 0;
    curl_easy_getinfo(handle, CURLINFO_RESPONSE_CODE, &status);
    curl_easy_cleanup(handle);
    if (code != CURLE_OK) {
        throw std::runtime_error(std::string("REST request failed: ") + curl_easy_strerror(code));
    }
    if (status < 200 || status >= 300) {
        throw std::runtime_error("REST request returned HTTP " + std::to_string(status) + ": " + response);
    }
    return response;
}

std::string RestClient::exchange_info() const {
    return get(base_url_ + "/fapi/v1/exchangeInfo");
}

std::string RestClient::depth_snapshot(std::string_view symbol, int limit) const {
    if (limit != 5 && limit != 10 && limit != 20 && limit != 50 && limit != 100 &&
        limit != 500 && limit != 1000) {
        throw std::invalid_argument("unsupported Binance depth snapshot limit");
    }
    return get(base_url_ + "/fapi/v1/depth?symbol=" + std::string(symbol) +
               "&limit=" + std::to_string(limit));
}

}  // namespace crypto::binance
