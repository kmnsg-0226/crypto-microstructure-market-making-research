#include "historical/gzip_line_reader.hpp"

#include <cstddef>
#include <stdexcept>
#include <string>
#include <vector>

#include <zlib.h>

namespace crypto::historical {

void for_each_gzip_line(const std::filesystem::path& path, const LineCallback& callback) {
    gzFile file = gzopen(path.c_str(), "rb");
    if (file == nullptr) {
        throw std::runtime_error("cannot open gzip input: " + path.string());
    }
    if (gzbuffer(file, 1U << 20U) != 0) {
        gzclose(file);
        throw std::runtime_error("cannot configure gzip input buffer");
    }

    try {
        std::vector<char> buffer(8U << 20U);
        std::string carry;
        while (true) {
            const int read = gzread(file, buffer.data(), static_cast<unsigned int>(buffer.size()));
            if (read < 0) {
                int error_number = Z_OK;
                const char* message = gzerror(file, &error_number);
                throw std::runtime_error(std::string("gzip read failed: ") +
                                         (message == nullptr ? "unknown error" : message));
            }
            if (read == 0) {
                break;
            }

            const auto size = static_cast<std::size_t>(read);
            std::size_t start = 0;
            if (!carry.empty()) {
                const auto newline = std::string_view(buffer.data(), size).find('\n');
                if (newline == std::string_view::npos) {
                    carry.append(buffer.data(), size);
                    continue;
                }
                carry.append(buffer.data(), newline);
                if (!carry.empty() && carry.back() == '\r') {
                    carry.pop_back();
                }
                callback(std::string_view(carry));
                carry.clear();
                start = newline + 1;
            }

            while (start < size) {
                const auto remaining = std::string_view(buffer.data() + start, size - start);
                const auto newline = remaining.find('\n');
                if (newline == std::string_view::npos) {
                    carry.assign(remaining);
                    break;
                }
                auto line = remaining.substr(0, newline);
                if (!line.empty() && line.back() == '\r') {
                    line.remove_suffix(1);
                }
                callback(line);
                start += newline + 1;
            }
        }
        if (!carry.empty()) {
            if (carry.back() == '\r') {
                carry.pop_back();
            }
            callback(std::string_view(carry));
        }
    } catch (...) {
        gzclose(file);
        throw;
    }

    if (gzclose(file) != Z_OK) {
        throw std::runtime_error("gzip close failed: " + path.string());
    }
}

}  // namespace crypto::historical
