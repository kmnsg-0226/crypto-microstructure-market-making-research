#include <cstdint>
#include <filesystem>
#include <iomanip>
#include <iostream>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>
#include <limits>

#include "replay/event_replayer.hpp"

namespace {

struct ReplayConfig {
    std::vector<std::filesystem::path> inputs;
    std::string symbol{"BTCUSDT"};
    std::optional<std::filesystem::path> research_output;
    std::uint64_t sample_ms{100};
    std::uint64_t start_ns{};
    std::uint64_t end_ns{std::numeric_limits<std::uint64_t>::max()};
    bool include_invalid{};
};

ReplayConfig parse_args(int argc, char** argv) {
    if (argc < 2) {
        throw std::invalid_argument(
            "Usage: crypto_replay FILE [FILE ...] [--input FILE] [--symbol BTCUSDT] "
            "[--export FILE.csv.zst] [--sample-ms 100] [--start-ns N] [--end-ns N] "
            "[--include-invalid]\n"
            "Rotated files must be supplied in chronological order; book state is carried "
            "across the boundaries.");
    }
    ReplayConfig config;
    int i = 1;
    // Leading positional arguments are the raw chain, in chronological order.
    for (; i < argc && std::string_view(argv[i]).substr(0, 2) != "--"; ++i) {
        config.inputs.emplace_back(argv[i]);
    }
    for (; i < argc; ++i) {
        const std::string arg = argv[i];
        auto require_value = [&](const char* name) -> std::string {
            if (++i >= argc) {
                throw std::invalid_argument(std::string("missing value for ") + name);
            }
            return argv[i];
        };
        if (arg == "--input") {
            config.inputs.emplace_back(require_value("--input"));
        } else if (arg == "--symbol") {
            config.symbol = require_value("--symbol");
        } else if (arg == "--export") {
            config.research_output = require_value("--export");
        } else if (arg == "--sample-ms") {
            config.sample_ms = std::stoull(require_value("--sample-ms"));
        } else if (arg == "--start-ns") {
            config.start_ns = std::stoull(require_value("--start-ns"));
        } else if (arg == "--end-ns") {
            config.end_ns = std::stoull(require_value("--end-ns"));
        } else if (arg == "--include-invalid") {
            config.include_invalid = true;
        } else {
            throw std::invalid_argument("unknown argument: " + arg);
        }
    }
    if (config.inputs.empty()) {
        throw std::invalid_argument("at least one raw input file is required");
    }
    return config;
}

std::string optional_number(const std::optional<std::int64_t>& value) {
    return value ? std::to_string(*value) : "null";
}

void print_levels(std::string_view name, const std::vector<crypto::binance::PriceLevel>& levels) {
    std::cout << "  \"" << name << "\": [";
    for (std::size_t i = 0; i < levels.size(); ++i) {
        if (i != 0) {
            std::cout << ',';
        }
        std::cout << '[' << levels[i].price_ticks << ',' << levels[i].qty_lots << ']';
    }
    std::cout << "],\n";
}

}  // namespace

int main(int argc, char** argv) {
    try {
        const auto config = parse_args(argc, argv);
        crypto::replay::validate_raw_chain(config.inputs);
        const auto instrument = crypto::replay::EventReplayer::instrument_from_raw_files(
            config.inputs, config.symbol);
        crypto::replay::EventReplayer replayer(instrument);
        const auto summary = replayer.replay(
            config.inputs, config.research_output, config.sample_ms,
            config.start_ns, config.end_ns, config.include_invalid);

        std::cout
            << "{\n"
            << "  \"symbol\": \"" << instrument.symbol() << "\",\n"
            << "  \"input_files\": " << config.inputs.size() << ",\n"
            << "  \"tick_size\": \"" << instrument.tick_size() << "\",\n"
            << "  \"step_size\": \"" << instrument.step_size() << "\",\n"
            << "  \"raw_records\": " << summary.raw_records << ",\n"
            << "  \"first_local_receive_ns\": " << summary.first_local_receive_ns << ",\n"
            << "  \"last_local_receive_ns\": " << summary.last_local_receive_ns << ",\n"
            << "  \"first_exchange_time_ms\": " << summary.first_exchange_time_ms << ",\n"
            << "  \"last_exchange_time_ms\": " << summary.last_exchange_time_ms << ",\n"
            << "  \"depth_events\": " << summary.depth_events << ",\n"
            << "  \"applied_depth_events\": " << summary.applied_depth_events << ",\n"
            << "  \"stale_depth_events\": " << summary.stale_depth_events << ",\n"
            << "  \"trade_events\": " << summary.trade_events << ",\n"
            << "  \"sequence_gaps\": " << summary.sequence_gaps << ",\n"
            << "  \"parse_failures\": " << summary.parse_failures << ",\n"
            << "  \"final_update_id\": " << summary.final_update_id << ",\n"
            << "  \"best_bid_ticks\": " << optional_number(summary.best_bid_ticks) << ",\n"
            << "  \"best_ask_ticks\": " << optional_number(summary.best_ask_ticks) << ",\n";
        print_levels("top_bids_ticks_lots", summary.top_bids);
        print_levels("top_asks_ticks_lots", summary.top_asks);
        std::cout
            << "  \"final_checksum\": \"0x" << std::hex << summary.final_checksum << std::dec << "\",\n"
            << "  \"research_rows\": " << summary.research_rows << ",\n"
            << "  \"research_valid_rows\": " << summary.research_valid_rows << ",\n"
            << "  \"research_invalid_rows\": " << summary.research_invalid_rows << ",\n"
            << "  \"invalid_book_intervals\": " << summary.invalid_book_intervals << ",\n"
            << "  \"crossed_book_states\": " << summary.crossed_book_states << ",\n"
            << "  \"empty_bid_states\": " << summary.empty_bid_states << ",\n"
            << "  \"empty_ask_states\": " << summary.empty_ask_states << "\n"
            << "}\n";
        return summary.parse_failures == 0 ? 0 : 2;
    } catch (const std::exception& error) {
        std::cerr << "crypto_replay: " << error.what() << '\n';
        return 1;
    }
}
