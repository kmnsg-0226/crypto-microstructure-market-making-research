#pragma once

#include <atomic>
#include <cstdint>
#include <filesystem>
#include <future>
#include <mutex>
#include <string>
#include <utility>
#include <vector>

namespace crypto::storage {

// Description of a raw capture file that has been fully closed. Produced by the writer at a
// rotation boundary and consumed by the background finalizer; the closed file itself is never
// modified again.
struct ClosedRawFile {
    std::filesystem::path path;
    std::string symbol;
    std::string venue{"Binance USD-M"};
    std::string previous_file;  // empty for an independent initial file
    bool continuation{};
    std::uint64_t first_system_ns{};
    std::uint64_t last_system_ns{};
    std::uint64_t start_update_id{};
    std::uint64_t end_update_id{};
    // Ordered so the serialized manifest is deterministic. Values are per-file deltas.
    std::vector<std::pair<std::string, std::uint64_t>> counters;
};

[[nodiscard]] std::string sha256_file(const std::filesystem::path& path);

// ISO-8601 UTC with nanosecond precision, or "null" for a zero timestamp.
[[nodiscard]] std::string utc_iso8601(std::uint64_t system_ns);

// Writes <path>.manifest.json via a temporary file, fsync, rename. Returns the SHA-256.
std::string write_manifest(const ClosedRawFile& file);

// Runs manifest finalization off the ingest path. One task per closed file; rotations are rare
// (daily in production) so no worker pool is warranted.
// ponytail: thread per closed file, switch to a single worker if rotation ever becomes frequent.
class ManifestFinalizer {
public:
    ManifestFinalizer() = default;
    ~ManifestFinalizer();

    ManifestFinalizer(const ManifestFinalizer&) = delete;
    ManifestFinalizer& operator=(const ManifestFinalizer&) = delete;

    void submit(ClosedRawFile file);
    void wait_all();
    [[nodiscard]] std::uint64_t failures() const noexcept { return failures_.load(); }

private:
    std::mutex mutex_;
    std::vector<std::future<void>> pending_;
    std::atomic_uint64_t failures_{};
};

}  // namespace crypto::storage
