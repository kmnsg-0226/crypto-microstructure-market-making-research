#pragma once

#include <filesystem>
#include <functional>
#include <string_view>

namespace crypto::historical {

using LineCallback = std::function<void(std::string_view)>;

void for_each_gzip_line(const std::filesystem::path& path, const LineCallback& callback);

}  // namespace crypto::historical
