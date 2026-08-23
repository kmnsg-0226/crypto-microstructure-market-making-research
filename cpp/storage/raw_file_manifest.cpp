#include "storage/raw_file_manifest.hpp"

#include <array>
#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <fstream>
#include <sstream>
#include <stdexcept>
#include <vector>

#include <fcntl.h>
#include <unistd.h>

#include <openssl/evp.h>

#ifndef CRYPTO_L2_BUILD_VERSION
#define CRYPTO_L2_BUILD_VERSION "unknown"
#endif

namespace crypto::storage {
namespace {

std::string json_string(std::string_view value) {
    std::string out;
    out.push_back('"');
    for (const char c : value) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (static_cast<unsigned char>(c) < 0x20U) {
                    std::array<char, 8> escape{};
                    std::snprintf(escape.data(), escape.size(), "\\u%04x",
                                  static_cast<unsigned>(static_cast<unsigned char>(c)));
                    out += escape.data();
                } else {
                    out.push_back(c);
                }
        }
    }
    out.push_back('"');
    return out;
}

std::string hostname() {
    std::array<char, 256> buffer{};
    if (::gethostname(buffer.data(), buffer.size() - 1) != 0) {
        return "unknown";
    }
    return buffer.data();
}

}  // namespace

std::string sha256_file(const std::filesystem::path& path) {
    std::ifstream in(path, std::ios::binary);
    if (!in) {
        throw std::runtime_error("cannot open file for hashing: " + path.string());
    }
    EVP_MD_CTX* context = EVP_MD_CTX_new();
    if (context == nullptr) {
        throw std::runtime_error("cannot allocate digest context");
    }
    try {
        if (EVP_DigestInit_ex(context, EVP_sha256(), nullptr) != 1) {
            throw std::runtime_error("cannot initialise sha256");
        }
        std::vector<char> buffer(1U << 20U);
        while (in) {
            in.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
            const auto read = static_cast<std::size_t>(in.gcount());
            if (read == 0) {
                break;
            }
            if (EVP_DigestUpdate(context, buffer.data(), read) != 1) {
                throw std::runtime_error("sha256 update failed");
            }
        }
        if (in.bad()) {
            throw std::runtime_error("read error while hashing: " + path.string());
        }
        std::array<unsigned char, EVP_MAX_MD_SIZE> digest{};
        unsigned int digest_size = 0;
        if (EVP_DigestFinal_ex(context, digest.data(), &digest_size) != 1) {
            throw std::runtime_error("sha256 finalisation failed");
        }
        EVP_MD_CTX_free(context);
        std::string hex;
        hex.reserve(static_cast<std::size_t>(digest_size) * 2U);
        for (unsigned int i = 0; i < digest_size; ++i) {
            std::array<char, 3> byte{};
            std::snprintf(byte.data(), byte.size(), "%02x", digest[i]);
            hex += byte.data();
        }
        return hex;
    } catch (...) {
        EVP_MD_CTX_free(context);
        throw;
    }
}

std::string utc_iso8601(std::uint64_t system_ns) {
    if (system_ns == 0) {
        return {};
    }
    const auto seconds = static_cast<std::time_t>(system_ns / 1'000'000'000ULL);
    const auto nanos = static_cast<unsigned long>(system_ns % 1'000'000'000ULL);
    std::tm parts{};
    if (::gmtime_r(&seconds, &parts) == nullptr) {
        return {};
    }
    std::array<char, 32> stamp{};
    if (std::strftime(stamp.data(), stamp.size(), "%Y-%m-%dT%H:%M:%S", &parts) == 0) {
        return {};
    }
    std::array<char, 16> fraction{};
    std::snprintf(fraction.data(), fraction.size(), ".%09luZ", nanos);
    return std::string(stamp.data()) + fraction.data();
}

std::string write_manifest(const ClosedRawFile& file) {
    const auto size_bytes = std::filesystem::file_size(file.path);
    const auto digest = sha256_file(file.path);

    auto optional_string = [](const std::string& value) {
        return value.empty() ? std::string("null") : json_string(value);
    };

    std::ostringstream json;
    json << "{\n"
         << "  \"schema\": \"chft-raw-manifest\",\n"
         << "  \"schema_version\": 1,\n"
         << "  \"symbol\": " << json_string(file.symbol) << ",\n"
         << "  \"venue\": " << json_string(file.venue) << ",\n"
         << "  \"filename\": " << json_string(file.path.filename().string()) << ",\n"
         << "  \"file_start_receive_ns\": " << file.first_system_ns << ",\n"
         << "  \"file_start_receive_utc\": " << optional_string(utc_iso8601(file.first_system_ns)) << ",\n"
         << "  \"file_end_receive_ns\": " << file.last_system_ns << ",\n"
         << "  \"file_end_receive_utc\": " << optional_string(utc_iso8601(file.last_system_ns)) << ",\n"
         << "  \"file_size_bytes\": " << size_bytes << ",\n"
         << "  \"sha256\": " << json_string(digest) << ",\n"
         << "  \"previous_file\": " << optional_string(file.previous_file) << ",\n"
         << "  \"continuation\": " << (file.continuation ? "true" : "false") << ",\n"
         << "  \"requires_previous_file\": " << (file.continuation ? "true" : "false") << ",\n"
         << "  \"start_update_id\": " << file.start_update_id << ",\n"
         << "  \"end_update_id\": " << file.end_update_id << ",\n"
         << "  \"hostname\": " << json_string(hostname()) << ",\n"
         << "  \"build_version\": " << json_string(CRYPTO_L2_BUILD_VERSION) << ",\n"
         << "  \"counters\": {";
    for (std::size_t i = 0; i < file.counters.size(); ++i) {
        json << (i == 0 ? "\n" : ",\n") << "    " << json_string(file.counters[i].first) << ": "
             << file.counters[i].second;
    }
    json << (file.counters.empty() ? "}\n" : "\n  }\n") << "}\n";
    const auto text = json.str();

    auto manifest_path = file.path;
    manifest_path += ".manifest.json";
    auto temporary_path = manifest_path;
    temporary_path += ".tmp";

    const int fd = ::open(temporary_path.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) {
        throw std::runtime_error("cannot open manifest temporary: " + temporary_path.string());
    }
    std::size_t written = 0;
    while (written < text.size()) {
        const auto chunk = ::write(fd, text.data() + written, text.size() - written);
        if (chunk <= 0) {
            ::close(fd);
            throw std::runtime_error("cannot write manifest temporary: " + temporary_path.string());
        }
        written += static_cast<std::size_t>(chunk);
    }
    if (::fsync(fd) != 0) {
        ::close(fd);
        throw std::runtime_error("cannot fsync manifest temporary: " + temporary_path.string());
    }
    if (::close(fd) != 0) {
        throw std::runtime_error("cannot close manifest temporary: " + temporary_path.string());
    }
    std::filesystem::rename(temporary_path, manifest_path);
    return digest;
}

ManifestFinalizer::~ManifestFinalizer() {
    try {
        wait_all();
    } catch (...) {
    }
}

void ManifestFinalizer::submit(ClosedRawFile file) {
    std::lock_guard lock(mutex_);
    std::erase_if(pending_, [](const std::future<void>& task) {
        return task.wait_for(std::chrono::seconds(0)) == std::future_status::ready;
    });
    auto task = [this, file = std::move(file)] {
        try {
            const auto digest = write_manifest(file);
            std::fprintf(stderr, "finalized file=%s sha256=%s\n",
                         file.path.filename().string().c_str(), digest.c_str());
        } catch (const std::exception& error) {
            // The raw file is never touched by finalization; a failure only costs the sidecar.
            ++failures_;
            std::fprintf(stderr, "manifest_fail file=%s error=%s\n",
                         file.path.string().c_str(), error.what());
        }
    };
    try {
        pending_.push_back(std::async(std::launch::async, std::move(task)));
    } catch (const std::exception& error) {
        // A finalizer that cannot start must not take the collector down with it; the raw file
        // is already closed and intact, only its sidecar is missing.
        ++failures_;
        std::fprintf(stderr, "manifest_fail reason=cannot_start error=%s\n", error.what());
    }
}

void ManifestFinalizer::wait_all() {
    std::vector<std::future<void>> pending;
    {
        std::lock_guard lock(mutex_);
        pending.swap(pending_);
    }
    for (auto& task : pending) {
        if (task.valid()) {
            task.get();
        }
    }
}

}  // namespace crypto::storage
