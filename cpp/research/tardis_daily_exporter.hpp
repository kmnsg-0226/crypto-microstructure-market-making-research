#pragma once

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <memory>
#include <optional>
#include <string>

#include "book/order_book.hpp"
#include "exchange/binance/instrument_info.hpp"
#include "historical/tardis_trade_reader.hpp"
#include "research/microstructure_features.hpp"

namespace crypto::research {

class CompressedTextWriter;

struct TardisDailyExportSummary {
    std::uint64_t rows{};
    std::uint64_t valid_rows{};
    std::uint64_t invalid_rows{};
    std::uint64_t book_segments{};
    std::uint64_t book_messages{};
    std::uint64_t book_level_updates{};
    std::uint64_t trades{};
    std::uint64_t first_sample_time_us{};
    std::uint64_t last_sample_time_us{};
    bool final_book_observable{};

    bool operator==(const TardisDailyExportSummary&) const = default;
};

class TardisDailyResearchExporter {
public:
    TardisDailyResearchExporter(
        const std::filesystem::path& path,
        std::string date,
        std::uint64_t day_start_us,
        std::uint64_t day_end_us,
        std::uint64_t interval_us,
        const binance::DecimalIncrement& price_increment,
        const binance::DecimalIncrement& qty_increment,
        const historical::TardisTradeDay& trades,
        double weighted_obi_lambda);
    ~TardisDailyResearchExporter();

    TardisDailyResearchExporter(const TardisDailyResearchExporter&) = delete;
    TardisDailyResearchExporter& operator=(const TardisDailyResearchExporter&) = delete;

    void before_book_event(
        std::uint64_t event_timestamp_us,
        bool replacement_snapshot,
        const book::OrderBook& book);
    void after_book_event(
        std::uint64_t event_timestamp_us,
        std::uint64_t event_local_timestamp_us,
        std::size_t level_updates,
        bool snapshot,
        const book::OrderBook& book);
    void finish(const book::OrderBook& book);
    void close();

    [[nodiscard]] const TardisDailyExportSummary& summary() const noexcept { return summary_; }

private:
    void emit_before(
        std::uint64_t exclusive_timestamp_us,
        const book::OrderBook& book,
        bool observable);
    void emit_through(
        std::uint64_t inclusive_timestamp_us,
        const book::OrderBook& book,
        bool observable);
    void write_sample(const book::OrderBook& book, bool observable);

    std::string date_;
    std::uint64_t day_start_us_{};
    std::uint64_t day_end_us_{};
    std::uint64_t interval_us_{};
    const binance::DecimalIncrement& price_increment_;
    const binance::DecimalIncrement& qty_increment_;
    const historical::TardisTradeDay& trades_;
    double weighted_obi_lambda_{};
    double tick_size_value_{};
    double notional_unit_value_{};
    std::unique_ptr<CompressedTextWriter> writer_;
    std::uint64_t next_index_{};
    std::uint64_t latest_book_timestamp_us_{};
    std::uint64_t latest_book_local_timestamp_us_{};
    std::uint64_t latest_trade_timestamp_us_{};
    std::uint64_t latest_trade_local_timestamp_us_{};
    std::uint64_t previous_book_event_timestamp_us_{};
    std::uint64_t segment_id_{};
    std::uint64_t bucket_book_messages_{};
    std::uint64_t bucket_level_updates_{};
    std::int64_t bucket_ofi_lots_{};
    bool observable_{};
    bool closed_{};
    std::optional<BestLevelState> previous_best_;
    TardisDailyExportSummary summary_;
};

}  // namespace crypto::research
