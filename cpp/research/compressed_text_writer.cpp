#include "research/compressed_text_writer.hpp"

#include <stdexcept>
#include <vector>

#include <zstd.h>

namespace crypto::research {

CompressedTextWriter::CompressedTextWriter(const std::filesystem::path& path) {
    if (path.has_parent_path()) {
        std::filesystem::create_directories(path.parent_path());
    }
    output_.open(path, std::ios::binary | std::ios::trunc);
    if (!output_) {
        throw std::runtime_error("cannot open compressed text output: " + path.string());
    }
}

CompressedTextWriter::~CompressedTextWriter() {
    try {
        close();
    } catch (...) {
    }
}

void CompressedTextWriter::append(std::string_view text) {
    buffer_.append(text);
    if (buffer_.size() >= (1U << 20U)) {
        flush();
    }
}

void CompressedTextWriter::close() {
    if (!closed_) {
        flush();
        output_.flush();
        if (!output_) {
            throw std::runtime_error("failed flushing compressed text output");
        }
        closed_ = true;
    }
}

void CompressedTextWriter::flush() {
    if (buffer_.empty()) {
        return;
    }
    std::vector<char> compressed(ZSTD_compressBound(buffer_.size()));
    const auto size = ZSTD_compress(
        compressed.data(), compressed.size(), buffer_.data(), buffer_.size(), 3);
    if (ZSTD_isError(size)) {
        throw std::runtime_error(
            std::string("zstd compression failed: ") + ZSTD_getErrorName(size));
    }
    output_.write(compressed.data(), static_cast<std::streamsize>(size));
    if (!output_) {
        throw std::runtime_error("failed writing compressed text output");
    }
    buffer_.clear();
}

}  // namespace crypto::research
