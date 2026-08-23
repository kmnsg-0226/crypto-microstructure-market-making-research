#include <algorithm>
#include <array>
#include <charconv>
#include <chrono>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

#include "exchange/binance/instrument_info.hpp"
#include "historical/gzip_line_reader.hpp"
#include "historical/tardis_trade_reader.hpp"
#include "research/compressed_text_writer.hpp"
#include "research/passive_queue.hpp"

namespace {

constexpr std::string_view placement_header =
    "date,decision_time_us,placement_local_time_us,feature_segment_id,valid_book_state,"
    "best_bid_price,best_bid_qty,best_ask_price,best_ask_qty,spread_ticks,obi_l1,obi_l5,"
    "obi_l10,weighted_obi_l10,weighted_mid_minus_mid_ticks,normalized_ofi_1s,ti_1s,"
    "combined_prediction_1s_ticks,next_snapshot_local_time_us";

struct Arguments {
    std::string date;
    std::filesystem::path placements;
    std::filesystem::path trades;
    std::filesystem::path output;
    std::filesystem::path report;
    std::string quote_qty{"0.005"};
    std::uint64_t repeat{2};
};

struct Placement {
    std::string date;
    std::uint64_t decision_time_us{};
    std::uint64_t placement_local_time_us{};
    std::uint64_t feature_segment_id{};
    bool valid_book_state{};
    std::string best_bid_price;
    std::string best_bid_qty;
    std::string best_ask_price;
    std::string best_ask_qty;
    std::string spread_ticks;
    std::array<std::string, 8> signals;
    std::uint64_t next_snapshot_local_time_us{};
};

struct RunSummary {
    std::uint64_t placement_rows{};
    std::uint64_t candidate_quotes{};
    std::uint64_t valid_candidates{};
    std::uint64_t full_fills{};
    std::uint64_t partial_fills{};
    std::uint64_t unfilled{};
    std::uint64_t invalid_at_placement{};
    std::uint64_t invalid_snapshot{};
    std::uint64_t invalid_day_boundary{};
    std::uint64_t trade_through_fills{};
    std::uint64_t optimistic_full_fills{};
    std::uint64_t optimistic_partial_fills{};
    std::uint64_t optimistic_unfilled{};
    std::uint64_t optimistic_invalid_snapshot{};

    bool operator==(const RunSummary&) const = default;
};

void usage(const char* program) {
    std::cerr << "usage: " << program
              << " --date YYYY-MM-DD --placements FILE.csv.gz --trades FILE.csv.gz"
                 " --output PROBES.csv.zst [--report REPORT.json] [--quote-qty 0.005]"
                 " [--repeat 2]\n";
}

Arguments parse_arguments(int argc, char** argv) {
    Arguments result;
    for (int index = 1; index < argc; ++index) {
        const std::string argument = argv[index];
        auto next = [&]() -> std::string {
            if (index + 1 >= argc) {
                throw std::invalid_argument("missing value for " + argument);
            }
            return argv[++index];
        };
        if (argument == "--date") {
            result.date = next();
        } else if (argument == "--placements") {
            result.placements = next();
        } else if (argument == "--trades") {
            result.trades = next();
        } else if (argument == "--output") {
            result.output = next();
        } else if (argument == "--report") {
            result.report = next();
        } else if (argument == "--quote-qty") {
            result.quote_qty = next();
        } else if (argument == "--repeat") {
            result.repeat = std::stoull(next());
        } else {
            throw std::invalid_argument("unknown argument: " + argument);
        }
    }
    if (result.date.empty() || result.placements.empty() || result.trades.empty() ||
        result.output.empty() || result.repeat == 0) {
        throw std::invalid_argument("required passive probe arguments are missing");
    }
    return result;
}

std::uint64_t day_start_us(const std::string& text) {
    if (text.size() != 10 || text[4] != '-' || text[7] != '-') {
        throw std::invalid_argument("date must be YYYY-MM-DD");
    }
    const int year_value = std::stoi(text.substr(0, 4));
    const unsigned month_value = static_cast<unsigned>(std::stoul(text.substr(5, 2)));
    const unsigned day_value = static_cast<unsigned>(std::stoul(text.substr(8, 2)));
    const std::chrono::year_month_day day{
        std::chrono::year{year_value},
        std::chrono::month{month_value},
        std::chrono::day{day_value}};
    if (!day.ok()) {
        throw std::invalid_argument("invalid calendar date");
    }
    return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::microseconds>(
        std::chrono::sys_days{day}.time_since_epoch()).count());
}

template <std::size_t Size>
std::array<std::string_view, Size> split(std::string_view line) {
    std::array<std::string_view, Size> fields;
    std::size_t start = 0;
    for (std::size_t index = 0; index < Size - 1; ++index) {
        const auto comma = line.find(',', start);
        if (comma == std::string_view::npos) {
            throw std::invalid_argument("CSV row has too few fields");
        }
        fields[index] = line.substr(start, comma - start);
        start = comma + 1;
    }
    if (line.find(',', start) != std::string_view::npos) {
        throw std::invalid_argument("CSV row has too many fields");
    }
    fields.back() = line.substr(start);
    return fields;
}

std::uint64_t parse_u64(std::string_view value, const char* field, bool allow_empty = false) {
    if (value.empty() && allow_empty) {
        return 0;
    }
    std::uint64_t result{};
    const auto [end, error] = std::from_chars(value.data(), value.data() + value.size(), result);
    if (error != std::errc{} || end != value.data() + value.size()) {
        throw std::invalid_argument(std::string("invalid placement ") + field);
    }
    return result;
}

Placement parse_placement(std::string_view line, const std::string& expected_date) {
    const auto fields = split<19>(line);
    if (fields[0] != expected_date) {
        throw std::invalid_argument("placement date mismatch");
    }
    Placement result;
    result.date = std::string(fields[0]);
    result.decision_time_us = parse_u64(fields[1], "decision_time_us");
    result.placement_local_time_us = parse_u64(fields[2], "placement_local_time_us");
    result.feature_segment_id = parse_u64(fields[3], "feature_segment_id", true);
    if (fields[4] == "1") {
        result.valid_book_state = true;
    } else if (fields[4] != "0") {
        throw std::invalid_argument("invalid placement book state");
    }
    result.best_bid_price = fields[5];
    result.best_bid_qty = fields[6];
    result.best_ask_price = fields[7];
    result.best_ask_qty = fields[8];
    result.spread_ticks = fields[9];
    for (std::size_t index = 0; index < result.signals.size(); ++index) {
        result.signals[index] = fields[10 + index];
    }
    result.next_snapshot_local_time_us = parse_u64(
        fields[18], "next_snapshot_local_time_us", true);
    return result;
}

bool files_equal(const std::filesystem::path& left, const std::filesystem::path& right) {
    if (std::filesystem::file_size(left) != std::filesystem::file_size(right)) {
        return false;
    }
    std::ifstream a(left, std::ios::binary);
    std::ifstream b(right, std::ios::binary);
    std::vector<char> a_buffer(1U << 20U);
    std::vector<char> b_buffer(1U << 20U);
    while (a && b) {
        a.read(a_buffer.data(), static_cast<std::streamsize>(a_buffer.size()));
        b.read(b_buffer.data(), static_cast<std::streamsize>(b_buffer.size()));
        if (a.gcount() != b.gcount() ||
            !std::equal(a_buffer.begin(), a_buffer.begin() + a.gcount(), b_buffer.begin())) {
            return false;
        }
    }
    return a.eof() && b.eof();
}

std::string header() {
    return "schema_version,date,decision_time_us,placement_local_time_us,feature_segment_id,side,"
           "quote_price,quote_qty,best_bid_at_entry,best_ask_at_entry,spread_ticks,"
           "queue_ahead_initial,displayed_bbo_qty,order_size_to_displayed_ratio,quote_lifetime_ms,"
           "expiry_local_time_us,next_snapshot_local_time_us,obi_l1,obi_l5,obi_l10,"
           "weighted_obi_l10,weighted_mid_minus_mid_ticks,normalized_ofi_1s,ti_1s,"
           "combined_prediction_1s_ticks,fill_status,first_fill_exchange_time_us,"
           "first_fill_local_time_us,full_fill_exchange_time_us,full_fill_local_time_us,"
           "fill_latency_us,filled_qty,queue_consumed,same_price_traded_qty,"
           "qualifying_trade_count,trade_through_price,fill_reason,cancel_reason,"
           "invalid_due_to_gap,invalid_due_to_snapshot,optimistic_fill_status,"
           "optimistic_first_fill_exchange_time_us,optimistic_first_fill_local_time_us,"
           "optimistic_full_fill_exchange_time_us,optimistic_full_fill_local_time_us,"
           "optimistic_fill_latency_us,optimistic_filled_qty,optimistic_queue_consumed,"
           "optimistic_fill_reason,optimistic_cancel_reason,optimistic_invalid_due_to_gap,"
           "optimistic_invalid_due_to_snapshot\n";
}

std::string optional_u64(std::uint64_t value) {
    return value == 0 ? std::string{} : std::to_string(value);
}

std::string outcome_status(
    const crypto::research::PassiveFillOutcome& outcome,
    bool invalid_placement,
    bool invalid_snapshot,
    bool invalid_day) {
    if (invalid_placement) {
        return "invalid_at_placement";
    }
    if (outcome.state == crypto::research::PassiveFillState::Full) {
        return "full";
    }
    if (invalid_snapshot) {
        return outcome.state == crypto::research::PassiveFillState::Partial
            ? "partial_invalid_snapshot"
            : "invalid_snapshot";
    }
    if (invalid_day) {
        return outcome.state == crypto::research::PassiveFillState::Partial
            ? "partial_invalid_day_boundary"
            : "invalid_day_boundary";
    }
    return crypto::research::passive_fill_state_name(outcome.state);
}

std::string fill_reason(const crypto::research::PassiveFillOutcome& outcome) {
    if (outcome.filled_lots == 0) {
        return {};
    }
    return outcome.filled_by_trade_through ? "trade_through" : "same_price_aggressor_trade";
}

std::string cancel_reason(
    const crypto::research::PassiveFillOutcome& outcome,
    bool invalid_placement,
    bool invalid_snapshot,
    bool invalid_day) {
    if (invalid_placement) {
        return "invalid_book_at_placement";
    }
    if (outcome.state == crypto::research::PassiveFillState::Full) {
        return {};
    }
    if (invalid_snapshot) {
        return "replacement_snapshot";
    }
    if (invalid_day) {
        return "day_boundary";
    }
    return outcome.state == crypto::research::PassiveFillState::Partial
        ? "quote_expired_after_partial_fill"
        : "quote_expired_unfilled";
}

void count_outcome(
    RunSummary& summary,
    const crypto::research::PassiveFillOutcome& outcome,
    bool invalid_placement,
    bool invalid_snapshot,
    bool invalid_day,
    bool optimistic) {
    if (!optimistic) {
        if (invalid_placement) {
            ++summary.invalid_at_placement;
        } else if (outcome.state == crypto::research::PassiveFillState::Full) {
            ++summary.full_fills;
            summary.trade_through_fills += outcome.filled_by_trade_through;
        } else if (invalid_snapshot) {
            ++summary.invalid_snapshot;
        } else if (invalid_day) {
            ++summary.invalid_day_boundary;
        } else if (outcome.state == crypto::research::PassiveFillState::Partial) {
            ++summary.partial_fills;
        } else {
            ++summary.unfilled;
        }
        return;
    }
    if (invalid_placement) {
        return;
    }
    if (outcome.state == crypto::research::PassiveFillState::Full) {
        ++summary.optimistic_full_fills;
    } else if (invalid_snapshot) {
        ++summary.optimistic_invalid_snapshot;
    } else if (!invalid_day && outcome.state == crypto::research::PassiveFillState::Partial) {
        ++summary.optimistic_partial_fills;
    } else if (!invalid_day) {
        ++summary.optimistic_unfilled;
    }
}

RunSummary run_placements(
    const Arguments& arguments,
    const crypto::research::PassiveTradeIndex& index,
    const crypto::binance::DecimalIncrement& price_increment,
    const crypto::binance::DecimalIncrement& qty_increment,
    std::int64_t quote_qty_lots,
    std::uint64_t day_end,
    const std::filesystem::path& output_path) {
    crypto::research::CompressedTextWriter writer(output_path);
    writer.append(header());
    RunSummary summary;
    bool saw_header = false;
    std::uint64_t previous_decision{};
    const std::array<std::uint64_t, 4> lifetimes_ms{100, 500, 1000, 5000};
    crypto::historical::for_each_gzip_line(arguments.placements, [&](std::string_view line) {
        if (!saw_header) {
            if (line != placement_header) {
                throw std::invalid_argument("unexpected passive placement CSV header");
            }
            saw_header = true;
            return;
        }
        if (line.empty()) {
            return;
        }
        const auto placement = parse_placement(line, arguments.date);
        if (summary.placement_rows != 0 && placement.decision_time_us <= previous_decision) {
            throw std::invalid_argument("passive placement decision grid is not strictly increasing");
        }
        previous_decision = placement.decision_time_us;
        ++summary.placement_rows;

        for (const auto side : {crypto::research::PassiveQuoteSide::Bid,
                                crypto::research::PassiveQuoteSide::Ask}) {
            const bool is_bid = side == crypto::research::PassiveQuoteSide::Bid;
            const auto& quote_text = is_bid ? placement.best_bid_price : placement.best_ask_price;
            const auto& displayed_text = is_bid ? placement.best_bid_qty : placement.best_ask_qty;
            const bool valid_placement = placement.valid_book_state && !quote_text.empty() &&
                                         !displayed_text.empty() &&
                                         placement.placement_local_time_us < day_end;
            const auto quote_ticks = valid_placement ? price_increment.to_steps(quote_text) : 0;
            const auto displayed_lots = valid_placement ? qty_increment.to_steps(displayed_text) : 0;

            for (const auto lifetime_ms : lifetimes_ms) {
                ++summary.candidate_quotes;
                const auto expiry = placement.placement_local_time_us + lifetime_ms * 1000U;
                const bool snapshot_boundary = valid_placement &&
                    placement.next_snapshot_local_time_us > placement.placement_local_time_us &&
                    placement.next_snapshot_local_time_us < expiry;
                const bool day_boundary = valid_placement && day_end < expiry;
                auto end = expiry;
                if (snapshot_boundary) {
                    end = std::min(end, placement.next_snapshot_local_time_us);
                }
                if (day_boundary) {
                    end = std::min(end, day_end);
                }
                crypto::research::PassiveFillOutcome baseline;
                crypto::research::PassiveFillOutcome optimistic;
                if (valid_placement && end > placement.placement_local_time_us) {
                    baseline = index.evaluate(
                        side,
                        quote_ticks,
                        quote_qty_lots,
                        displayed_lots,
                        placement.placement_local_time_us,
                        end);
                    optimistic = index.evaluate(
                        side,
                        quote_ticks,
                        quote_qty_lots,
                        0,
                        placement.placement_local_time_us,
                        end);
                    ++summary.valid_candidates;
                }
                const bool baseline_snapshot_invalid = snapshot_boundary &&
                    baseline.state != crypto::research::PassiveFillState::Full;
                const bool baseline_day_invalid = day_boundary &&
                    baseline.state != crypto::research::PassiveFillState::Full;
                const bool optimistic_snapshot_invalid = snapshot_boundary &&
                    optimistic.state != crypto::research::PassiveFillState::Full;
                const bool optimistic_day_invalid = day_boundary &&
                    optimistic.state != crypto::research::PassiveFillState::Full;
                count_outcome(
                    summary,
                    baseline,
                    !valid_placement,
                    baseline_snapshot_invalid,
                    baseline_day_invalid,
                    false);
                count_outcome(
                    summary,
                    optimistic,
                    !valid_placement,
                    optimistic_snapshot_invalid,
                    optimistic_day_invalid,
                    true);

                std::ostringstream row;
                row << "tardis-passive-probes-v1," << placement.date << ','
                    << placement.decision_time_us << ',' << placement.placement_local_time_us << ','
                    << (placement.feature_segment_id == 0
                            ? std::string{}
                            : std::to_string(placement.feature_segment_id))
                    << ',' << (is_bid ? "bid" : "ask") << ',' << quote_text << ','
                    << qty_increment.from_steps(quote_qty_lots) << ',' << placement.best_bid_price
                    << ',' << placement.best_ask_price << ',' << placement.spread_ticks << ','
                    << (valid_placement ? qty_increment.from_steps(displayed_lots) : std::string{})
                    << ',' << displayed_text << ',';
                if (valid_placement && displayed_lots > 0) {
                    row << static_cast<double>(quote_qty_lots) /
                               static_cast<double>(displayed_lots);
                }
                row << ',' << lifetime_ms << ',' << expiry << ','
                    << optional_u64(placement.next_snapshot_local_time_us);
                for (const auto& signal : placement.signals) {
                    row << ',' << signal;
                }
                row << ',' << outcome_status(
                               baseline,
                               !valid_placement,
                               baseline_snapshot_invalid,
                               baseline_day_invalid)
                    << ',' << optional_u64(baseline.first_fill_exchange_time_us)
                    << ',' << optional_u64(baseline.first_fill_local_time_us)
                    << ',' << optional_u64(baseline.full_fill_exchange_time_us)
                    << ',' << optional_u64(baseline.full_fill_local_time_us)
                    << ',';
                if (baseline.first_fill_local_time_us != 0) {
                    row << baseline.first_fill_local_time_us - placement.placement_local_time_us;
                }
                row << ',' << qty_increment.from_steps(baseline.filled_lots)
                    << ',' << qty_increment.from_steps(baseline.queue_consumed_lots)
                    << ',' << qty_increment.from_steps(baseline.same_price_traded_lots)
                    << ',' << baseline.qualifying_trade_count
                    << ',' << (baseline.trade_through_price_ticks == 0
                            ? std::string{}
                            : price_increment.from_steps(baseline.trade_through_price_ticks))
                    << ',' << fill_reason(baseline)
                    << ',' << cancel_reason(
                               baseline,
                               !valid_placement,
                               baseline_snapshot_invalid,
                               baseline_day_invalid)
                    << ',' << ((!valid_placement || baseline_snapshot_invalid || baseline_day_invalid)
                            ? 1
                            : 0)
                    << ',' << (baseline_snapshot_invalid ? 1 : 0)
                    << ',' << outcome_status(
                               optimistic,
                               !valid_placement,
                               optimistic_snapshot_invalid,
                               optimistic_day_invalid)
                    << ',' << optional_u64(optimistic.first_fill_exchange_time_us)
                    << ',' << optional_u64(optimistic.first_fill_local_time_us)
                    << ',' << optional_u64(optimistic.full_fill_exchange_time_us)
                    << ',' << optional_u64(optimistic.full_fill_local_time_us)
                    << ',';
                if (optimistic.first_fill_local_time_us != 0) {
                    row << optimistic.first_fill_local_time_us - placement.placement_local_time_us;
                }
                row << ',' << qty_increment.from_steps(optimistic.filled_lots)
                    << ',' << qty_increment.from_steps(optimistic.queue_consumed_lots)
                    << ',' << fill_reason(optimistic)
                    << ',' << cancel_reason(
                               optimistic,
                               !valid_placement,
                               optimistic_snapshot_invalid,
                               optimistic_day_invalid)
                    << ',' << ((!valid_placement || optimistic_snapshot_invalid ||
                                optimistic_day_invalid)
                            ? 1
                            : 0)
                    << ',' << (optimistic_snapshot_invalid ? 1 : 0) << '\n';
                writer.append(row.str());
            }
        }
    });
    if (!saw_header || summary.placement_rows == 0) {
        throw std::invalid_argument("empty passive placement input");
    }
    writer.close();
    return summary;
}

void write_report(
    std::ostream& output,
    const Arguments& arguments,
    const crypto::research::PassiveTradeIndex& index,
    const RunSummary& summary,
    bool deterministic,
    bool byte_identical) {
    output << "{\n"
           << "  \"schema\": \"tardis-passive-probe-report-v1\",\n"
           << "  \"date\": \"" << arguments.date << "\",\n"
           << "  \"placements\": \"" << arguments.placements.string() << "\",\n"
           << "  \"trades\": \"" << arguments.trades.string() << "\",\n"
           << "  \"output\": \"" << arguments.output.string() << "\",\n"
           << "  \"quote_qty_btc\": \"" << arguments.quote_qty << "\",\n"
           << "  \"repeat_count\": " << arguments.repeat << ",\n"
           << "  \"deterministic_summary\": " << (deterministic ? "true" : "false") << ",\n"
           << "  \"byte_identical_output\": " << (byte_identical ? "true" : "false") << ",\n"
           << "  \"trade_rows\": " << index.rows() << ",\n"
           << "  \"buy_trades\": " << index.buy_trades() << ",\n"
           << "  \"sell_trades\": " << index.sell_trades() << ",\n"
           << "  \"trade_local_timestamp_regressions\": "
           << index.local_timestamp_regressions() << ",\n"
           << "  \"placement_rows\": " << summary.placement_rows << ",\n"
           << "  \"candidate_quotes\": " << summary.candidate_quotes << ",\n"
           << "  \"valid_candidates\": " << summary.valid_candidates << ",\n"
           << "  \"full_fills\": " << summary.full_fills << ",\n"
           << "  \"partial_fills\": " << summary.partial_fills << ",\n"
           << "  \"unfilled\": " << summary.unfilled << ",\n"
           << "  \"invalid_at_placement\": " << summary.invalid_at_placement << ",\n"
           << "  \"invalid_snapshot\": " << summary.invalid_snapshot << ",\n"
           << "  \"invalid_day_boundary\": " << summary.invalid_day_boundary << ",\n"
           << "  \"trade_through_fills\": " << summary.trade_through_fills << ",\n"
           << "  \"optimistic_full_fills\": " << summary.optimistic_full_fills << ",\n"
           << "  \"optimistic_partial_fills\": " << summary.optimistic_partial_fills << ",\n"
           << "  \"optimistic_unfilled\": " << summary.optimistic_unfilled << ",\n"
           << "  \"optimistic_invalid_snapshot\": "
           << summary.optimistic_invalid_snapshot << "\n"
           << "}\n";
}

}  // namespace

int main(int argc, char** argv) {
    try {
        const auto arguments = parse_arguments(argc, argv);
        const crypto::binance::DecimalIncrement price_increment("0.10");
        const crypto::binance::DecimalIncrement qty_increment("0.001");
        const auto quote_qty_lots = qty_increment.to_steps(arguments.quote_qty);
        if (quote_qty_lots <= 0) {
            throw std::invalid_argument("quote quantity must be positive");
        }
        crypto::research::PassiveTradeIndex index;
        bool saw_trade_header = false;
        crypto::historical::for_each_gzip_line(arguments.trades, [&](std::string_view line) {
            if (!saw_trade_header) {
                if (line != "exchange,symbol,timestamp,local_timestamp,id,side,price,amount") {
                    throw std::invalid_argument("unexpected Tardis trade CSV header");
                }
                saw_trade_header = true;
                return;
            }
            if (!line.empty()) {
                index.add_trade(crypto::historical::parse_tardis_trade_row(
                    line, price_increment, qty_increment));
            }
        });
        if (!saw_trade_header || index.rows() == 0) {
            throw std::invalid_argument("empty passive trade input");
        }
        index.finalize();
        const auto end_us = day_start_us(arguments.date) + 86'400'000'000ULL;
        if (arguments.output.has_parent_path()) {
            std::filesystem::create_directories(arguments.output.parent_path());
        }
        const auto first = run_placements(
            arguments,
            index,
            price_increment,
            qty_increment,
            quote_qty_lots,
            end_us,
            arguments.output);
        bool deterministic = true;
        bool byte_identical = true;
        const auto repeat_path = std::filesystem::path(arguments.output.string() + ".repeat.part");
        for (std::uint64_t run = 1; run < arguments.repeat; ++run) {
            const auto repeated = run_placements(
                arguments,
                index,
                price_increment,
                qty_increment,
                quote_qty_lots,
                end_us,
                repeat_path);
            deterministic = deterministic && first == repeated;
            byte_identical = byte_identical && files_equal(arguments.output, repeat_path);
        }
        if (arguments.repeat > 1) {
            std::filesystem::remove(repeat_path);
        }
        std::ofstream report_file;
        std::ostream* report = &std::cout;
        if (!arguments.report.empty()) {
            if (arguments.report.has_parent_path()) {
                std::filesystem::create_directories(arguments.report.parent_path());
            }
            report_file.open(arguments.report, std::ios::binary | std::ios::trunc);
            if (!report_file) {
                throw std::runtime_error("cannot open passive report output");
            }
            report = &report_file;
        }
        write_report(*report, arguments, index, first, deterministic, byte_identical);
        return deterministic && byte_identical && index.local_timestamp_regressions() == 0 &&
                       first.placement_rows == 864'000 &&
                       first.candidate_quotes == 6'912'000
                   ? 0
                   : 3;
    } catch (const std::exception& error) {
        usage(argv[0]);
        std::cerr << "tardis passive probe failed: " << error.what() << '\n';
        return 1;
    }
}
