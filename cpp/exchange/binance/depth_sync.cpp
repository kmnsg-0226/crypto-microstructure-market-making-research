#include "exchange/binance/depth_sync.hpp"

namespace crypto::binance {

DepthSynchronizer::DepthSynchronizer(book::OrderBook& book, std::size_t max_buffered_events)
    : book_(book), max_buffered_events_(max_buffered_events) {}

void DepthSynchronizer::on_connected() {
    book_.clear();
    buffer_.clear();
    first_event_applied_ = false;
    snapshot_update_id_ = 0;
    state_ = SyncState::Buffering;
}

void DepthSynchronizer::on_disconnected() {
    book_.invalidate();
    buffer_.clear();
    first_event_applied_ = false;
    snapshot_update_id_ = 0;
    state_ = SyncState::Disconnected;
}

void DepthSynchronizer::request_snapshot() {
    if (state_ != SyncState::Disconnected) {
        state_ = SyncState::SnapshotPending;
    }
}

void DepthSynchronizer::begin_resync() {
    book_.invalidate();
    first_event_applied_ = false;
    snapshot_update_id_ = 0;
    state_ = SyncState::Resyncing;
}

SyncResult DepthSynchronizer::buffer_event(const DepthEvent& event) {
    if (buffer_.size() >= max_buffered_events_) {
        book_.invalidate();
        buffer_.clear();
        ++gap_count_;
        state_ = SyncState::Invalid;
        return SyncResult::InvalidBook;
    }
    buffer_.push_back(event);
    return SyncResult::Buffered;
}

SyncResult DepthSynchronizer::detect_gap(const DepthEvent& event) {
    book_.invalidate();
    buffer_.clear();
    buffer_.push_back(event);
    first_event_applied_ = false;
    snapshot_update_id_ = 0;
    ++gap_count_;
    state_ = SyncState::GapDetected;
    return SyncResult::GapDetected;
}

SyncResult DepthSynchronizer::apply_first_event(const DepthEvent& event) {
    if (event.final_update_id < snapshot_update_id_) {
        ++stale_event_count_;
        return SyncResult::Stale;
    }
    if (event.first_update_id > snapshot_update_id_) {
        return detect_gap(event);
    }
    if (!(event.first_update_id <= snapshot_update_id_ && snapshot_update_id_ <= event.final_update_id)) {
        ++stale_event_count_;
        return SyncResult::Stale;
    }
    if (!book_.apply_depth_event(event)) {
        state_ = SyncState::Invalid;
        return SyncResult::InvalidBook;
    }
    first_event_applied_ = true;
    ++applied_event_count_;
    state_ = SyncState::Live;
    return SyncResult::Applied;
}

SyncResult DepthSynchronizer::apply_contiguous_event(const DepthEvent& event) {
    if (event.final_update_id <= book_.last_update_id()) {
        ++stale_event_count_;
        return SyncResult::Stale;
    }
    if (event.previous_final_update_id != book_.last_update_id()) {
        return detect_gap(event);
    }
    if (!book_.apply_depth_event(event)) {
        state_ = SyncState::Invalid;
        return SyncResult::InvalidBook;
    }
    ++applied_event_count_;
    state_ = SyncState::Live;
    return SyncResult::Applied;
}

SyncResult DepthSynchronizer::on_snapshot(const DepthSnapshot& snapshot) {
    if (state_ == SyncState::Disconnected) {
        return SyncResult::IgnoredDisconnected;
    }
    state_ = SyncState::Syncing;
    first_event_applied_ = false;
    snapshot_update_id_ = snapshot.last_update_id;
    if (!book_.load_snapshot(snapshot)) {
        state_ = SyncState::Invalid;
        return SyncResult::InvalidBook;
    }

    while (!buffer_.empty() && buffer_.front().final_update_id < snapshot_update_id_) {
        buffer_.pop_front();
        ++stale_event_count_;
    }
    if (buffer_.empty()) {
        return SyncResult::Buffered;
    }

    auto event = std::move(buffer_.front());
    buffer_.pop_front();
    auto result = apply_first_event(event);
    if (result != SyncResult::Applied) {
        if (result == SyncResult::GapDetected) {
            begin_resync();
            return SyncResult::NeedSnapshot;
        }
        return result;
    }

    while (!buffer_.empty()) {
        event = std::move(buffer_.front());
        buffer_.pop_front();
        result = apply_contiguous_event(event);
        if (result == SyncResult::GapDetected) {
            begin_resync();
            return SyncResult::NeedSnapshot;
        }
        if (result == SyncResult::InvalidBook) {
            return result;
        }
    }
    return SyncResult::Applied;
}

SyncResult DepthSynchronizer::on_depth(const DepthEvent& event) {
    switch (state_) {
        case SyncState::Disconnected:
            return SyncResult::IgnoredDisconnected;
        case SyncState::Buffering:
        case SyncState::SnapshotPending:
        case SyncState::Resyncing:
        case SyncState::GapDetected:
            return buffer_event(event);
        case SyncState::Invalid:
            return SyncResult::InvalidBook;
        case SyncState::Syncing: {
            const auto result = first_event_applied_
                ? apply_contiguous_event(event)
                : apply_first_event(event);
            if (result == SyncResult::GapDetected) {
                begin_resync();
                return SyncResult::NeedSnapshot;
            }
            return result;
        }
        case SyncState::Live:
            return apply_contiguous_event(event);
    }
    return SyncResult::InvalidBook;
}

const char* to_string(SyncState state) noexcept {
    switch (state) {
        case SyncState::Disconnected: return "DISCONNECTED";
        case SyncState::Buffering: return "BUFFERING";
        case SyncState::SnapshotPending: return "SNAPSHOT_PENDING";
        case SyncState::Syncing: return "SYNCING";
        case SyncState::Live: return "LIVE";
        case SyncState::GapDetected: return "GAP_DETECTED";
        case SyncState::Invalid: return "INVALID";
        case SyncState::Resyncing: return "RESYNCING";
    }
    return "UNKNOWN";
}

}  // namespace crypto::binance
