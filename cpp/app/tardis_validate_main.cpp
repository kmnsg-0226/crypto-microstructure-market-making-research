#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <stdexcept>
#include <string>

#include "exchange/binance/instrument_info.hpp"
#include "historical/tardis_l2_replayer.hpp"

namespace {

void usage(const char* program) {
    std::cerr << "usage: " << program
              << " FILE.csv.gz [--tick-size 0.10] [--step-size 0.001] [--repeat N]"
                 " [--output REPORT.json]\n";
}

std::string json_escape(const std::string& value) {
    std::string result;
    result.reserve(value.size());
    for (const char c : value) {
        if (c == '\\' || c == '"') {
            result.push_back('\\');
        }
        result.push_back(c);
    }
    return result;
}

bool equivalent(
    const crypto::historical::TardisReplaySummary& left,
    const crypto::historical::TardisReplaySummary& right) {
    return left == right;
}

void print_summary(
    std::ostream& output,
    const crypto::historical::TardisReplaySummary& summary,
    const crypto::binance::DecimalIncrement& price,
    const crypto::binance::DecimalIncrement& qty,
    std::uint64_t repeat_count,
    bool deterministic) {
    const auto optional_price = [&](std::int64_t value, bool present) {
        return present ? '"' + price.from_steps(value) + '"' : std::string("null");
    };
    const auto optional_qty = [&](std::int64_t value, bool present) {
        return present ? '"' + qty.from_steps(value) + '"' : std::string("null");
    };
    const bool has_bbo = summary.first_bbo_local_timestamp_us != 0;

    output << "{\n"
              << "  \"input\": \"" << json_escape(summary.input_path.string()) << "\",\n"
              << "  \"repeat_count\": " << repeat_count << ",\n"
              << "  \"deterministic\": " << (deterministic ? "true" : "false") << ",\n"
              << "  \"rows\": " << summary.rows << ",\n"
              << "  \"message_groups\": " << summary.message_groups << ",\n"
              << "  \"snapshot_rows\": " << summary.snapshot_rows << ",\n"
              << "  \"snapshot_groups\": " << summary.snapshot_groups << ",\n"
              << "  \"delta_rows\": " << summary.delta_rows << ",\n"
              << "  \"delta_messages\": " << summary.delta_messages << ",\n"
              << "  \"pre_snapshot_rows\": " << summary.pre_snapshot_rows << ",\n"
              << "  \"pre_snapshot_messages\": " << summary.pre_snapshot_messages << ",\n"
              << "  \"skipped_invalid_messages\": " << summary.skipped_invalid_messages << ",\n"
              << "  \"valid_states\": " << summary.valid_states << ",\n"
              << "  \"invalid_states\": " << summary.invalid_states << ",\n"
              << "  \"snapshot_load_failures\": " << summary.snapshot_load_failures << ",\n"
              << "  \"crossed_book_states\": " << summary.crossed_book_states << ",\n"
              << "  \"empty_bid_states\": " << summary.empty_bid_states << ",\n"
              << "  \"empty_ask_states\": " << summary.empty_ask_states << ",\n"
              << "  \"exchange_timestamp_regressions\": " << summary.exchange_timestamp_regressions << ",\n"
              << "  \"local_timestamp_regressions\": " << summary.local_timestamp_regressions << ",\n"
              << "  \"mixed_snapshot_flag_messages\": " << summary.mixed_snapshot_flag_messages << ",\n"
              << "  \"parse_failures\": " << summary.parse_failures << ",\n"
              << "  \"first_timestamp_us\": " << summary.first_timestamp_us << ",\n"
              << "  \"last_timestamp_us\": " << summary.last_timestamp_us << ",\n"
              << "  \"first_local_timestamp_us\": " << summary.first_local_timestamp_us << ",\n"
              << "  \"last_local_timestamp_us\": " << summary.last_local_timestamp_us << ",\n"
              << "  \"max_local_gap_us\": " << summary.max_local_gap_us << ",\n"
              << "  \"max_local_gap_ending_us\": " << summary.max_local_gap_ending_us << ",\n"
              << "  \"conservative_reconnect_gap_us\": " << summary.conservative_reconnect_gap_us << ",\n"
              << "  \"saw_snapshot\": " << (summary.saw_snapshot ? "true" : "false") << ",\n"
              << "  \"final_book_valid\": " << (summary.final_book_valid ? "true" : "false") << ",\n"
              << "  \"first_bbo\": {\"local_timestamp_us\": " << summary.first_bbo_local_timestamp_us
              << ", \"bid_price\": " << optional_price(summary.first_bid_ticks, has_bbo)
              << ", \"bid_qty\": " << optional_qty(summary.first_bid_qty_lots, has_bbo)
              << ", \"ask_price\": " << optional_price(summary.first_ask_ticks, has_bbo)
              << ", \"ask_qty\": " << optional_qty(summary.first_ask_qty_lots, has_bbo) << "},\n"
              << "  \"last_bbo\": {\"local_timestamp_us\": " << summary.last_bbo_local_timestamp_us
              << ", \"bid_price\": " << optional_price(summary.last_bid_ticks, has_bbo)
              << ", \"bid_qty\": " << optional_qty(summary.last_bid_qty_lots, has_bbo)
              << ", \"ask_price\": " << optional_price(summary.last_ask_ticks, has_bbo)
              << ", \"ask_qty\": " << optional_qty(summary.last_ask_qty_lots, has_bbo) << "},\n"
              << "  \"final_book_checksum\": \"0x" << std::hex << summary.final_book_checksum << std::dec << "\",\n"
              << "  \"replay_checksum\": \"0x" << std::hex << summary.replay_checksum << std::dec << "\",\n"
              << "  \"snapshots\": [\n";
    for (std::size_t i = 0; i < summary.snapshots.size(); ++i) {
        const auto& item = summary.snapshots[i];
        output << "    {\"start_timestamp_us\": " << item.start_timestamp_us
                  << ", \"end_timestamp_us\": " << item.end_timestamp_us
                  << ", \"start_local_timestamp_us\": " << item.start_local_timestamp_us
                  << ", \"end_local_timestamp_us\": " << item.end_local_timestamp_us
                  << ", \"previous_local_timestamp_us\": " << item.previous_local_timestamp_us
                  << ", \"rows\": " << item.rows
                  << ", \"valid\": " << (item.valid ? "true" : "false")
                  << ", \"best_bid\": " << optional_price(item.best_bid_ticks, item.valid)
                  << ", \"best_ask\": " << optional_price(item.best_ask_ticks, item.valid) << "}";
        if (i + 1 != summary.snapshots.size()) {
            output << ',';
        }
        output << '\n';
    }
    output << "  ]\n}\n";
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 2) {
        usage(argv[0]);
        return 2;
    }

    std::filesystem::path input = argv[1];
    std::string tick_size = "0.10";
    std::string step_size = "0.001";
    std::filesystem::path output_path;
    std::uint64_t repeat_count = 2;
    try {
        for (int i = 2; i < argc; ++i) {
            const std::string argument = argv[i];
            if (argument == "--tick-size" && i + 1 < argc) {
                tick_size = argv[++i];
            } else if (argument == "--step-size" && i + 1 < argc) {
                step_size = argv[++i];
            } else if (argument == "--repeat" && i + 1 < argc) {
                repeat_count = std::stoull(argv[++i]);
                if (repeat_count == 0) {
                    throw std::invalid_argument("repeat count must be positive");
                }
            } else if (argument == "--output" && i + 1 < argc) {
                output_path = argv[++i];
            } else {
                throw std::invalid_argument("unknown or incomplete argument: " + argument);
            }
        }

        const crypto::historical::TardisL2Replayer replayer(tick_size, step_size);
        const auto first = replayer.replay(input);
        bool deterministic = true;
        for (std::uint64_t run = 1; run < repeat_count; ++run) {
            deterministic = deterministic && equivalent(first, replayer.replay(input));
        }
        std::ofstream output_file;
        std::ostream* output = &std::cout;
        if (!output_path.empty()) {
            output_file.open(output_path, std::ios::binary | std::ios::trunc);
            if (!output_file) {
                throw std::runtime_error("cannot open output: " + output_path.string());
            }
            output = &output_file;
        }
        print_summary(
            *output,
            first,
            crypto::binance::DecimalIncrement(tick_size),
            crypto::binance::DecimalIncrement(step_size),
            repeat_count,
            deterministic);
        return deterministic && first.parse_failures == 0 && first.local_timestamp_regressions == 0 &&
                       first.snapshot_load_failures == 0 && first.crossed_book_states == 0 &&
                       first.empty_bid_states == 0 && first.empty_ask_states == 0 && first.final_book_valid
                   ? 0
                   : 3;
    } catch (const std::exception& error) {
        std::cerr << "tardis validation failed: " << error.what() << '\n';
        return 1;
    }
}
