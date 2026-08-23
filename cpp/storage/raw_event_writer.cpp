#include "storage/raw_event_writer.hpp"

#include <array>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <limits>
#include <stdexcept>
#include <string_view>
#include <type_traits>
#include <vector>

#include <zstd.h>

namespace crypto::storage {
namespace {

constexpr std::array<char, 8> kMagic{'C', 'H', 'F', 'T', 'L', '2', 'R', '1'};
constexpr std::uint32_t kVersion = 1;

template <typename T>
void append_le(std::string& out, T value) {
    using U = std::make_unsigned_t<T>;
    U bits = static_cast<U>(value);
    for (std::size_t i = 0; i < sizeof(T); ++i) {
        out.push_back(static_cast<char>((bits >> (i * 8U)) & 0xffU));
    }
}

void append_string(std::string& out, std::string_view value) {
    if (value.size() > std::numeric_limits<std::uint32_t>::max()) {
        throw std::length_error("raw record string is too large");
    }
    append_le(out, static_cast<std::uint32_t>(value.size()));
    out.append(value);
}

template <typename T>
T read_le(std::string_view data, std::size_t& offset) {
    if (offset + sizeof(T) > data.size()) {
        throw std::runtime_error("truncated raw event block");
    }
    using U = std::make_unsigned_t<T>;
    U value = 0;
    for (std::size_t i = 0; i < sizeof(T); ++i) {
        value |= static_cast<U>(static_cast<unsigned char>(data[offset + i])) << (i * 8U);
    }
    offset += sizeof(T);
    return static_cast<T>(value);
}

std::string read_string(std::string_view data, std::size_t& offset) {
    const auto size = read_le<std::uint32_t>(data, offset);
    if (offset + size > data.size()) {
        throw std::runtime_error("truncated raw event string");
    }
    std::string value(data.substr(offset, size));
    offset += size;
    return value;
}

template <typename T>
void write_file_le(std::ofstream& out, T value) {
    std::string encoded;
    append_le(encoded, value);
    out.write(encoded.data(), static_cast<std::streamsize>(encoded.size()));
}

template <typename T>
T read_file_le(std::ifstream& in) {
    std::array<char, sizeof(T)> bytes{};
    in.read(bytes.data(), static_cast<std::streamsize>(bytes.size()));
    if (!in) {
        throw std::runtime_error("truncated raw event file");
    }
    std::size_t offset = 0;
    return read_le<T>(std::string_view(bytes.data(), bytes.size()), offset);
}

}  // namespace

std::uint64_t current_system_ns() {
    return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count());
}

std::uint64_t current_steady_ns() {
    return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count());
}

RawEventWriter::RawEventWriter(const std::filesystem::path& path, std::size_t block_target_bytes)
    : block_target_bytes_(block_target_bytes), fixed_path_(path) {
    std::lock_guard lock(mutex_);
    open_segment_locked(0, current_system_ns());
}

RawEventWriter::RawEventWriter(RotationConfig rotation, std::size_t block_target_bytes)
    : block_target_bytes_(block_target_bytes), rotation_(std::move(rotation)) {
    if (rotation_.interval_ns == 0) {
        throw std::invalid_argument("rotation interval must be non-zero");
    }
    // The first file is named for the first record's bucket, so nothing is opened here.
    std::filesystem::create_directories(rotation_.directory);
}

RawEventWriter::~RawEventWriter() {
    try {
        close();
    } catch (...) {
    }
}

void RawEventWriter::set_on_file_closed(FileClosedHook hook) {
    std::lock_guard lock(mutex_);
    on_file_closed_ = std::move(hook);
}

std::filesystem::path RawEventWriter::current_path() const {
    std::lock_guard lock(mutex_);
    return current_path_;
}

std::filesystem::path RawEventWriter::segment_path_locked(std::uint64_t bucket) const {
    const auto seconds = static_cast<std::time_t>(
        (bucket * rotation_.interval_ns) / 1'000'000'000ULL);
    std::tm parts{};
    if (::gmtime_r(&seconds, &parts) == nullptr) {
        throw std::runtime_error("cannot format rotation timestamp");
    }
    std::array<char, 32> stamp{};
    if (std::strftime(stamp.data(), stamp.size(), "%Y%m%dT%H%M%SZ", &parts) == 0) {
        throw std::runtime_error("cannot format rotation timestamp");
    }
    const auto base = rotation_.directory / (rotation_.prefix + "-" + stamp.data());
    auto candidate = base;
    candidate += rotation_.extension;
    // A restart inside the same bucket, or a backwards clock step, must never truncate an
    // existing capture file.
    for (unsigned attempt = 1; std::filesystem::exists(candidate); ++attempt) {
        if (attempt > 1000U) {
            throw std::runtime_error("cannot find a free raw output name for " + base.string());
        }
        candidate = base;
        candidate += "-" + std::to_string(attempt) + rotation_.extension;
    }
    return candidate;
}

void RawEventWriter::open_segment_locked(std::uint64_t bucket, std::uint64_t system_ns) {
    const auto path = rotation_.interval_ns != 0 ? segment_path_locked(bucket) : fixed_path_;
    if (path.has_parent_path()) {
        std::filesystem::create_directories(path.parent_path());
    }
    out_.clear();
    out_.open(path, std::ios::binary | std::ios::trunc);
    if (!out_) {
        throw std::runtime_error("cannot open raw event output: " + path.string());
    }
    out_.write(kMagic.data(), static_cast<std::streamsize>(kMagic.size()));
    write_file_le(out_, kVersion);
    if (!out_) {
        throw std::runtime_error("cannot write raw event header: " + path.string());
    }
    open_ = true;
    current_bucket_ = bucket;
    current_path_ = path;
    segment_first_ns_ = system_ns;
    segment_last_ns_ = system_ns;
    segment_records_ = 0;
    segment_depth_events_ = 0;
    segment_trade_events_ = 0;
    segment_snapshots_ = 0;
    ++segments_opened_;
    if (rotation_.interval_ns != 0) {
        if (segments_opened_ > 1) {
            ++rotations_;
        }
        std::fprintf(stderr, "rotation new_file=%s previous_file=%s\n",
                     path.filename().string().c_str(),
                     previous_file_.empty() ? "null" : previous_file_.c_str());
    }
}

void RawEventWriter::close_segment_locked() {
    if (!open_) {
        return;
    }
    flush_locked();
    out_.flush();
    out_.close();
    open_ = false;
    if (out_.fail()) {
        throw std::runtime_error("failed closing raw event output: " + current_path_.string());
    }
    std::uint64_t bytes = 0;
    std::error_code error;
    const auto size = std::filesystem::file_size(current_path_, error);
    if (!error) {
        bytes = size;
    }
    if (rotation_.interval_ns != 0) {
        std::fprintf(stderr, "rotation old_file=%s records=%llu bytes=%llu\n",
                     current_path_.filename().string().c_str(),
                     static_cast<unsigned long long>(segment_records_),
                     static_cast<unsigned long long>(bytes));
    }
    if (on_file_closed_) {
        ClosedRawFile closed;
        closed.path = current_path_;
        closed.previous_file = previous_file_;
        closed.continuation = segments_opened_ > 1;
        closed.first_system_ns = segment_first_ns_;
        closed.last_system_ns = segment_last_ns_;
        closed.counters = {
            {"raw_records", segment_records_},
            {"depth_events", segment_depth_events_},
            {"trade_events", segment_trade_events_},
            {"snapshots", segment_snapshots_},
        };
        on_file_closed_(closed);
    }
    previous_file_ = current_path_.filename().string();
}

void RawEventWriter::close() {
    std::lock_guard lock(mutex_);
    close_segment_locked();
    closed_ = true;
}

void RawEventWriter::write(const RawRecord& record) {
    std::lock_guard lock(mutex_);
    const auto stamp = record.local_system_ns != 0 ? record.local_system_ns : current_system_ns();
    if (rotation_.interval_ns != 0) {
        const auto bucket = stamp / rotation_.interval_ns;
        if (!open_) {
            open_segment_locked(bucket, stamp);
        } else if (bucket > current_bucket_) {
            // Rotation happens strictly between complete records: the previous record is already
            // serialized into the closed file, this one starts the new file.
            close_segment_locked();
            open_segment_locked(bucket, stamp);
        }
    } else if (!open_) {
        if (closed_) {
            throw std::runtime_error("raw event writer is closed");
        }
        open_segment_locked(0, stamp);
    }
    std::string encoded;
    append_le(encoded, record.local_system_ns);
    append_le(encoded, record.local_steady_ns);
    append_le(encoded, record.exchange_time_ms);
    append_le(encoded, static_cast<std::uint8_t>(record.type));
    append_string(encoded, record.session_id);
    append_string(encoded, record.stream);
    append_string(encoded, record.payload);
    if (encoded.size() > std::numeric_limits<std::uint32_t>::max()) {
        throw std::length_error("raw event record is too large");
    }
    append_le(block_, static_cast<std::uint32_t>(encoded.size()));
    block_.append(encoded);
    ++records_written_;
    ++segment_records_;
    segment_last_ns_ = stamp;
    switch (record.type) {
        case RawEventType::DepthUpdate: ++segment_depth_events_; break;
        case RawEventType::AggTrade: ++segment_trade_events_; break;
        case RawEventType::DepthSnapshot: ++segment_snapshots_; break;
        default: break;
    }
    if (block_.size() >= block_target_bytes_) {
        flush_locked();
    }
}

void RawEventWriter::flush_locked() {
    if (block_.empty()) {
        return;
    }
    const auto bound = ZSTD_compressBound(block_.size());
    std::vector<char> compressed(bound);
    const auto compressed_size = ZSTD_compress(
        compressed.data(), compressed.size(), block_.data(), block_.size(), 3);
    if (ZSTD_isError(compressed_size)) {
        throw std::runtime_error(std::string("zstd compression failed: ") + ZSTD_getErrorName(compressed_size));
    }
    if (block_.size() > std::numeric_limits<std::uint32_t>::max() ||
        compressed_size > std::numeric_limits<std::uint32_t>::max()) {
        throw std::length_error("raw event block exceeds file format limit");
    }
    write_file_le(out_, static_cast<std::uint32_t>(block_.size()));
    write_file_le(out_, static_cast<std::uint32_t>(compressed_size));
    out_.write(compressed.data(), static_cast<std::streamsize>(compressed_size));
    if (!out_) {
        throw std::runtime_error("failed writing raw event block");
    }
    block_.clear();
}

void RawEventWriter::flush() {
    std::lock_guard lock(mutex_);
    if (!open_) {
        return;
    }
    flush_locked();
    out_.flush();
}

RawEventReader::RawEventReader(const std::filesystem::path& path) {
    in_.open(path, std::ios::binary);
    if (!in_) {
        throw std::runtime_error("cannot open raw event input: " + path.string());
    }
    std::array<char, kMagic.size()> magic{};
    in_.read(magic.data(), static_cast<std::streamsize>(magic.size()));
    if (!in_ || magic != kMagic) {
        throw std::runtime_error("invalid raw event file magic");
    }
    if (read_file_le<std::uint32_t>(in_) != kVersion) {
        throw std::runtime_error("unsupported raw event file version");
    }
}

void RawEventReader::for_each(const std::function<void(const RawRecord&)>& callback) {
    while (in_.peek() != std::char_traits<char>::eof()) {
        const auto raw_size = read_file_le<std::uint32_t>(in_);
        const auto compressed_size = read_file_le<std::uint32_t>(in_);
        std::vector<char> compressed(compressed_size);
        in_.read(compressed.data(), static_cast<std::streamsize>(compressed.size()));
        if (!in_) {
            throw std::runtime_error("truncated compressed raw event block");
        }
        std::string block(raw_size, '\0');
        const auto decoded = ZSTD_decompress(
            block.data(), block.size(), compressed.data(), compressed.size());
        if (ZSTD_isError(decoded) || decoded != raw_size) {
            throw std::runtime_error("invalid compressed raw event block");
        }

        std::size_t offset = 0;
        const std::string_view view(block);
        while (offset < view.size()) {
            const auto record_size = read_le<std::uint32_t>(view, offset);
            if (offset + record_size > view.size()) {
                throw std::runtime_error("truncated raw event record");
            }
            const auto record_view = view.substr(offset, record_size);
            std::size_t record_offset = 0;
            RawRecord record;
            record.local_system_ns = read_le<std::uint64_t>(record_view, record_offset);
            record.local_steady_ns = read_le<std::uint64_t>(record_view, record_offset);
            record.exchange_time_ms = read_le<std::int64_t>(record_view, record_offset);
            record.type = static_cast<RawEventType>(read_le<std::uint8_t>(record_view, record_offset));
            record.session_id = read_string(record_view, record_offset);
            record.stream = read_string(record_view, record_offset);
            record.payload = read_string(record_view, record_offset);
            if (record_offset != record_view.size()) {
                throw std::runtime_error("raw event record has trailing bytes");
            }
            callback(record);
            offset += record_size;
        }
    }
}

}  // namespace crypto::storage
