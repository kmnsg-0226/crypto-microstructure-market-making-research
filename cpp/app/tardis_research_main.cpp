#include <algorithm>
#include <chrono>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

#include "historical/tardis_l2_replayer.hpp"

namespace {

struct Arguments {
    std::filesystem::path l2;
    std::filesystem::path trades;
    std::filesystem::path output;
    std::filesystem::path report;
    std::string date;
    std::uint64_t repeat{2};
    std::uint64_t interval_ms{100};
    double weighted_obi_lambda{0.5};
};

void usage(const char* program) {
    std::cerr << "usage: " << program
              << " --date YYYY-MM-DD --l2 FILE.csv.gz --trades FILE.csv.gz"
                 " --output BASE.csv.zst [--report REPORT.json] [--repeat 2]"
                 " [--interval-ms 100] [--weighted-obi-lambda 0.5]\n";
}

Arguments parse_arguments(int argc, char** argv) {
    Arguments result;
    for (int i = 1; i < argc; ++i) {
        const std::string argument = argv[i];
        auto next = [&]() -> std::string {
            if (i + 1 >= argc) {
                throw std::invalid_argument("missing value for " + argument);
            }
            return argv[++i];
        };
        if (argument == "--date") {
            result.date = next();
        } else if (argument == "--l2") {
            result.l2 = next();
        } else if (argument == "--trades") {
            result.trades = next();
        } else if (argument == "--output") {
            result.output = next();
        } else if (argument == "--report") {
            result.report = next();
        } else if (argument == "--repeat") {
            result.repeat = std::stoull(next());
        } else if (argument == "--interval-ms") {
            result.interval_ms = std::stoull(next());
        } else if (argument == "--weighted-obi-lambda") {
            result.weighted_obi_lambda = std::stod(next());
        } else {
            throw std::invalid_argument("unknown argument: " + argument);
        }
    }
    if (result.date.empty() || result.l2.empty() || result.trades.empty() ||
        result.output.empty() || result.repeat == 0 || result.interval_ms == 0) {
        throw std::invalid_argument("required arguments are missing or invalid");
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
    const auto epoch = std::chrono::sys_days{day}.time_since_epoch();
    return static_cast<std::uint64_t>(
        std::chrono::duration_cast<std::chrono::microseconds>(epoch).count());
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

void write_json(
    std::ostream& output,
    const Arguments& arguments,
    const crypto::historical::TardisResearchRunSummary& summary,
    bool deterministic,
    bool byte_identical) {
    const auto& l2 = summary.l2;
    const auto& daily = summary.daily_export;
    output << "{\n"
           << "  \"schema\": \"tardis-research-export-report-v1\",\n"
           << "  \"date\": \"" << arguments.date << "\",\n"
           << "  \"l2_input\": \"" << arguments.l2.string() << "\",\n"
           << "  \"trades_input\": \"" << arguments.trades.string() << "\",\n"
           << "  \"output\": \"" << arguments.output.string() << "\",\n"
           << "  \"repeat_count\": " << arguments.repeat << ",\n"
           << "  \"deterministic_summary\": " << (deterministic ? "true" : "false") << ",\n"
           << "  \"byte_identical_export\": " << (byte_identical ? "true" : "false") << ",\n"
           << "  \"interval_ms\": " << arguments.interval_ms << ",\n"
           << "  \"weighted_obi_lambda\": " << arguments.weighted_obi_lambda << ",\n"
           << "  \"rows\": " << daily.rows << ",\n"
           << "  \"valid_rows\": " << daily.valid_rows << ",\n"
           << "  \"invalid_rows\": " << daily.invalid_rows << ",\n"
           << "  \"book_segments\": " << daily.book_segments << ",\n"
           << "  \"book_messages\": " << daily.book_messages << ",\n"
           << "  \"book_level_updates\": " << daily.book_level_updates << ",\n"
           << "  \"trade_rows\": " << summary.trade_rows << ",\n"
           << "  \"buy_trades\": " << summary.buy_trades << ",\n"
           << "  \"sell_trades\": " << summary.sell_trades << ",\n"
           << "  \"unknown_side_trades\": " << summary.unknown_side_trades << ",\n"
           << "  \"trade_timestamp_regressions\": " << summary.trade_timestamp_regressions << ",\n"
           << "  \"trade_local_timestamp_regressions\": "
           << summary.trade_local_timestamp_regressions << ",\n"
           << "  \"trades_outside_event_day\": " << summary.trades_outside_event_day << ",\n"
           << "  \"l2_snapshot_groups\": " << l2.snapshot_groups << ",\n"
           << "  \"l2_crossed_book_states\": " << l2.crossed_book_states << ",\n"
           << "  \"l2_empty_bid_states\": " << l2.empty_bid_states << ",\n"
           << "  \"l2_empty_ask_states\": " << l2.empty_ask_states << ",\n"
           << "  \"l2_parse_failures\": " << l2.parse_failures << ",\n"
           << "  \"l2_final_book_checksum\": \"0x" << std::hex << l2.final_book_checksum
           << std::dec << "\",\n"
           << "  \"l2_replay_checksum\": \"0x" << std::hex << l2.replay_checksum
           << std::dec << "\"\n"
           << "}\n";
}

}  // namespace

int main(int argc, char** argv) {
    try {
        const auto arguments = parse_arguments(argc, argv);
        const auto start_us = day_start_us(arguments.date);
        const auto end_us = start_us + 86'400'000'000ULL;
        const auto interval_us = arguments.interval_ms * 1000U;
        const crypto::historical::TardisL2Replayer replayer("0.10", "0.001");
        const auto first = replayer.replay_research_day(
            arguments.l2,
            arguments.trades,
            arguments.output,
            arguments.date,
            start_us,
            end_us,
            interval_us,
            arguments.weighted_obi_lambda);
        bool deterministic = true;
        bool byte_identical = true;
        const auto repeat_path = arguments.output.string() + ".repeat.part";
        for (std::uint64_t run = 1; run < arguments.repeat; ++run) {
            const auto repeated = replayer.replay_research_day(
                arguments.l2,
                arguments.trades,
                repeat_path,
                arguments.date,
                start_us,
                end_us,
                interval_us,
                arguments.weighted_obi_lambda);
            deterministic = deterministic && first == repeated;
            byte_identical = byte_identical && files_equal(arguments.output, repeat_path);
        }
        if (arguments.repeat > 1) {
            std::filesystem::remove(repeat_path);
        }

        std::ofstream report_file;
        std::ostream* output = &std::cout;
        if (!arguments.report.empty()) {
            if (arguments.report.has_parent_path()) {
                std::filesystem::create_directories(arguments.report.parent_path());
            }
            report_file.open(arguments.report, std::ios::binary | std::ios::trunc);
            if (!report_file) {
                throw std::runtime_error("cannot open report output");
            }
            output = &report_file;
        }
        write_json(*output, arguments, first, deterministic, byte_identical);
        const auto& l2 = first.l2;
        return deterministic && byte_identical && first.daily_export.rows == 864'000 &&
                       first.daily_export.valid_rows > 0 && first.unknown_side_trades == 0 &&
                       first.trade_timestamp_regressions == 0 &&
                       first.trade_local_timestamp_regressions == 0 && l2.parse_failures == 0 &&
                       l2.crossed_book_states == 0 && l2.empty_bid_states == 0 &&
                       l2.empty_ask_states == 0
                   ? 0
                   : 3;
    } catch (const std::exception& error) {
        usage(argv[0]);
        std::cerr << "tardis research export failed: " << error.what() << '\n';
        return 1;
    }
}
