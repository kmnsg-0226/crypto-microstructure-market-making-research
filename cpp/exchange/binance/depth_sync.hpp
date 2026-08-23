#pragma once

#include <cstddef>
#include <deque>

#include "book/order_book.hpp"
#include "exchange/binance/message_types.hpp"

namespace crypto::binance {

enum class SyncState {
    Disconnected,
    Buffering,
    SnapshotPending,
    Syncing,
    Live,
    GapDetected,
    Invalid,
    Resyncing,
};

enum class SyncResult {
    Buffered,
    Applied,
    Stale,
    GapDetected,
    NeedSnapshot,
    InvalidBook,
    IgnoredDisconnected,
};

class DepthSynchronizer {
public:
    explicit DepthSynchronizer(book::OrderBook& book, std::size_t max_buffered_events = 100000);

    void on_connected();
    void on_disconnected();
    void request_snapshot();
    void begin_resync();
    SyncResult on_snapshot(const DepthSnapshot& snapshot);
    SyncResult on_depth(const DepthEvent& event);

    [[nodiscard]] SyncState state() const noexcept { return state_; }
    [[nodiscard]] bool is_live() const noexcept { return state_ == SyncState::Live && book_.is_valid(); }
    [[nodiscard]] std::size_t buffered_events() const noexcept { return buffer_.size(); }
    [[nodiscard]] std::uint64_t gap_count() const noexcept { return gap_count_; }
    [[nodiscard]] std::uint64_t applied_event_count() const noexcept { return applied_event_count_; }
    [[nodiscard]] std::uint64_t stale_event_count() const noexcept { return stale_event_count_; }

private:
    SyncResult buffer_event(const DepthEvent& event);
    SyncResult apply_first_event(const DepthEvent& event);
    SyncResult apply_contiguous_event(const DepthEvent& event);
    SyncResult detect_gap(const DepthEvent& event);

    book::OrderBook& book_;
    std::deque<DepthEvent> buffer_;
    std::size_t max_buffered_events_;
    SyncState state_{SyncState::Disconnected};
    bool first_event_applied_{};
    std::uint64_t snapshot_update_id_{};
    std::uint64_t gap_count_{};
    std::uint64_t applied_event_count_{};
    std::uint64_t stale_event_count_{};
};

[[nodiscard]] const char* to_string(SyncState state) noexcept;

}  // namespace crypto::binance
