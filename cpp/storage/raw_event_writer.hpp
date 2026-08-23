#pragma once

#include <atomic>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <functional>
#include <mutex>
#include <string>

#include "storage/raw_file_manifest.hpp"

namespace crypto::storage {

enum class RawEventType : std::uint8_t {
    ConnectionOpen = 1,
    ConnectionClose = 2,
    InstrumentInfo = 3,
    DepthSnapshot = 4,
    DepthUpdate = 5,
    AggTrade = 6,
    Status = 7,
};

struct RawRecord {
    std::uint64_t local_system_ns{};
    std::uint64_t local_steady_ns{};
    std::int64_t exchange_time_ms{};
    RawEventType type{RawEventType::Status};
    std::string session_id;
    std::string stream;
    std::string payload;

    bool operator==(const RawRecord&) const = default;
};

[[nodiscard]] std::uint64_t current_system_ns();
[[nodiscard]] std::uint64_t current_steady_ns();

constexpr std::uint64_t kUtcDayNs = 86'400ULL * 1'000'000'000ULL;

// Wall-clock bucketing of raw files. The bucket index is floor(local_system_ns / interval_ns),
// so a 24h interval lands exactly on UTC midnight; a short interval is only for tests.
struct RotationConfig {
    std::filesystem::path directory;
    std::string prefix;
    std::string extension{".chft.zst"};
    std::uint64_t interval_ns{};
};

class RawEventWriter {
public:
    // Fixed single output file; no rotation.
    explicit RawEventWriter(const std::filesystem::path& path, std::size_t block_target_bytes = 1U << 20U);
    // Rotating output. The first file opens on the first record, named for its bucket.
    explicit RawEventWriter(RotationConfig rotation, std::size_t block_target_bytes = 1U << 20U);
    ~RawEventWriter();

    RawEventWriter(const RawEventWriter&) = delete;
    RawEventWriter& operator=(const RawEventWriter&) = delete;

    // Invoked on the thread that triggered the close, while the writer mutex is held, once the
    // file is closed on disk. The owner fills in capture-side counter deltas and hands the
    // descriptor to a background finalizer; it must not block.
    using FileClosedHook = std::function<void(ClosedRawFile&)>;
    void set_on_file_closed(FileClosedHook hook);

    void write(const RawRecord& record);
    void flush();
    // Flushes and finalizes the current file. Idempotent; further writes open a new file.
    void close();

    [[nodiscard]] std::uint64_t records_written() const noexcept { return records_written_.load(); }
    [[nodiscard]] std::uint64_t rotations() const noexcept { return rotations_.load(); }
    [[nodiscard]] std::filesystem::path current_path() const;

private:
    void flush_locked();
    void open_segment_locked(std::uint64_t bucket, std::uint64_t system_ns);
    void close_segment_locked();
    [[nodiscard]] std::filesystem::path segment_path_locked(std::uint64_t bucket) const;

    std::ofstream out_;
    std::string block_;
    std::size_t block_target_bytes_;
    mutable std::mutex mutex_;
    std::atomic_uint64_t records_written_{};
    std::atomic_uint64_t rotations_{};

    RotationConfig rotation_;
    std::filesystem::path fixed_path_;
    FileClosedHook on_file_closed_;
    bool open_{};
    std::uint64_t current_bucket_{};
    std::filesystem::path current_path_;
    std::string previous_file_;
    std::uint64_t segments_opened_{};
    std::uint64_t segment_first_ns_{};
    std::uint64_t segment_last_ns_{};
    std::uint64_t segment_records_{};
    std::uint64_t segment_depth_events_{};
    std::uint64_t segment_trade_events_{};
    std::uint64_t segment_snapshots_{};
    bool closed_{};
};

class RawEventReader {
public:
    explicit RawEventReader(const std::filesystem::path& path);
    void for_each(const std::function<void(const RawRecord&)>& callback);

private:
    std::ifstream in_;
};

}  // namespace crypto::storage
