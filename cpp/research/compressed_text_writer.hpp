#pragma once

#include <filesystem>
#include <fstream>
#include <string>
#include <string_view>

namespace crypto::research {

class CompressedTextWriter {
public:
    explicit CompressedTextWriter(const std::filesystem::path& path);
    ~CompressedTextWriter();

    CompressedTextWriter(const CompressedTextWriter&) = delete;
    CompressedTextWriter& operator=(const CompressedTextWriter&) = delete;

    void append(std::string_view text);
    void close();

private:
    void flush();

    std::ofstream output_;
    std::string buffer_;
    bool closed_{};
};

}  // namespace crypto::research
