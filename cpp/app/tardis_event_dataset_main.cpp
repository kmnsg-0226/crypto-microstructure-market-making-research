#include <algorithm>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

#include "exchange/binance/instrument_info.hpp"
#include "historical/tardis_event_replayer.hpp"
#include "research/event_dataset_exporter.hpp"

namespace {

struct Arguments {
    std::string date;
    std::filesystem::path l2;
    std::filesystem::path trades;
    std::filesystem::path output;
    std::filesystem::path report;
    std::uint64_t repeat{2};
};

Arguments parse(int argc, char** argv) {
    Arguments result;
    for (int i = 1; i < argc; ++i) {
        const std::string argument = argv[i];
        auto next = [&]() {
            if (i + 1 >= argc) {
                throw std::invalid_argument("missing value for " + argument);
            }
            return std::string(argv[++i]);
        };
        if (argument == "--date") result.date = next();
        else if (argument == "--l2") result.l2 = next();
        else if (argument == "--trades") result.trades = next();
        else if (argument == "--output") result.output = next();
        else if (argument == "--report") result.report = next();
        else if (argument == "--repeat") result.repeat = std::stoull(next());
        else throw std::invalid_argument("unknown argument: " + argument);
    }
    if (result.date.empty() || result.l2.empty() || result.trades.empty() ||
        result.output.empty() || result.repeat == 0) {
        throw std::invalid_argument("required arguments are missing");
    }
    return result;
}

bool files_equal(const std::filesystem::path& left, const std::filesystem::path& right) {
    if (std::filesystem::file_size(left) != std::filesystem::file_size(right)) return false;
    std::ifstream a(left, std::ios::binary);
    std::ifstream b(right, std::ios::binary);
    std::vector<char> aa(1U << 20U), bb(1U << 20U);
    while (a && b) {
        a.read(aa.data(), static_cast<std::streamsize>(aa.size()));
        b.read(bb.data(), static_cast<std::streamsize>(bb.size()));
        if (a.gcount() != b.gcount() ||
            !std::equal(aa.begin(), aa.begin() + a.gcount(), bb.begin())) return false;
    }
    return a.eof() && b.eof();
}

struct Run {
    crypto::historical::TardisReplaySummary replay;
    crypto::research::EventDatasetSummary events;
};

Run execute(const Arguments& arguments, const std::filesystem::path& output) {
    const crypto::binance::DecimalIncrement price("0.10");
    const crypto::binance::DecimalIncrement quantity("0.001");
    auto trades = crypto::historical::read_tardis_event_trade_file(
        arguments.trades, price, quantity);
    crypto::research::EventDatasetExporter exporter(
        output, arguments.date, std::move(trades), 100'000, 1'000'000, 5,
        "0.10", "0.001", 0.5);
    crypto::historical::TardisEventReplayer replayer("0.10", "0.001");
    Run result;
    result.replay = replayer.replay_with_observer(arguments.l2, exporter);
    result.events = exporter.summary();
    return result;
}

void report(const Arguments& arguments, const Run& run, bool deterministic, bool byte_identical) {
    std::ofstream file;
    std::ostream* out = &std::cout;
    if (!arguments.report.empty()) {
        if (arguments.report.has_parent_path()) {
            std::filesystem::create_directories(arguments.report.parent_path());
        }
        file.open(arguments.report, std::ios::binary | std::ios::trunc);
        if (!file) throw std::runtime_error("cannot write event dataset report");
        out = &file;
    }
    const auto& e = run.events;
    *out << "{\n"
         << "  \"schema\": \"event-selective-maker-export-report-v1\",\n"
         << "  \"date\": \"" << arguments.date << "\",\n"
         << "  \"l2_input\": \"" << arguments.l2.string() << "\",\n"
         << "  \"trades_input\": \"" << arguments.trades.string() << "\",\n"
         << "  \"output\": \"" << arguments.output.string() << "\",\n"
         << "  \"repeat_count\": " << arguments.repeat << ",\n"
         << "  \"deterministic_summary\": " << (deterministic ? "true" : "false") << ",\n"
         << "  \"byte_identical_output\": " << (byte_identical ? "true" : "false") << ",\n"
         << "  \"book_events\": " << e.book_events << ",\n"
         << "  \"trade_events\": " << e.trade_events << ",\n"
         << "  \"bbo_changes\": " << e.bbo_changes << ",\n"
         << "  \"decision_opportunities\": " << e.decision_opportunities << ",\n"
         << "  \"candidate_quotes\": " << e.candidate_quotes << ",\n"
         << "  \"full_fills\": " << e.full_fills << ",\n"
         << "  \"partial_fills\": " << e.partial_fills << ",\n"
         << "  \"unfilled\": " << e.unfilled << ",\n"
         << "  \"snapshot_invalidations\": " << e.snapshot_invalidations << ",\n"
         << "  \"invalid_book_events\": " << e.invalid_book_events << ",\n"
         << "  \"cooldown_suppressed\": " << e.cooldown_suppressed << ",\n"
         << "  \"rows\": " << e.rows << ",\n"
         << "  \"l2_snapshot_groups\": " << run.replay.snapshot_groups << ",\n"
         << "  \"l2_final_book_checksum\": \"0x" << std::hex
         << run.replay.final_book_checksum << std::dec << "\"\n}\n";
}

}  // namespace

int main(int argc, char** argv) {
    try {
        const auto arguments = parse(argc, argv);
        const auto first = execute(arguments, arguments.output);
        bool deterministic = true;
        bool byte_identical = true;
        const auto repeated_path = arguments.output.string() + ".repeat.part";
        for (std::uint64_t index = 1; index < arguments.repeat; ++index) {
            const auto repeated = execute(arguments, repeated_path);
            deterministic = deterministic && first.replay == repeated.replay &&
                first.events == repeated.events;
            byte_identical = byte_identical && files_equal(arguments.output, repeated_path);
        }
        if (arguments.repeat > 1) std::filesystem::remove(repeated_path);
        report(arguments, first, deterministic, byte_identical);
        return deterministic && byte_identical && first.events.rows > 0 &&
                       first.events.rows == first.events.candidate_quotes &&
                       first.replay.final_book_valid
                   ? 0 : 3;
    } catch (const std::exception& error) {
        std::cerr << "tardis event dataset failed: " << error.what() << '\n';
        return 1;
    }
}
