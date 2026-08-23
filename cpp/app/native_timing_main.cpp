#include <cstdint>
#include <filesystem>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>

#include "replay/event_replayer.hpp"
#include "research/cross_stream_timing.hpp"

int main(int argc, char** argv) {
    try {
        if (argc < 4) {
            throw std::invalid_argument(
                "Usage: native_timing_audit RAW.chft.zst --out EVENTS.csv.zst\n"
                "       [--symbol BTCUSDT] [--file-index N] [--window-ms 5000]\n"
                "Measures the lag between an aggressive print and the depth stream's reaction.");
        }
        const std::filesystem::path input = argv[1];
        std::filesystem::path output;
        std::string symbol = "BTCUSDT";
        std::uint32_t file_index = 0;
        std::uint64_t window_ms = 5000;
        for (int i = 2; i < argc; ++i) {
            const std::string arg = argv[i];
            auto value = [&](const char* name) -> std::string {
                if (++i >= argc) {
                    throw std::invalid_argument(std::string("missing value for ") + name);
                }
                return argv[i];
            };
            if (arg == "--out") {
                output = value("--out");
            } else if (arg == "--symbol") {
                symbol = value("--symbol");
            } else if (arg == "--file-index") {
                file_index = static_cast<std::uint32_t>(std::stoul(value("--file-index")));
            } else if (arg == "--window-ms") {
                window_ms = std::stoull(value("--window-ms"));
            } else {
                throw std::invalid_argument("unknown argument: " + arg);
            }
        }
        if (output.empty()) {
            throw std::invalid_argument("--out is required");
        }

        const auto instrument =
            crypto::replay::EventReplayer::instrument_from_raw_file(input, symbol);
        crypto::research::CrossStreamTimingObserver observer(output, file_index, window_ms);
        crypto::replay::EventReplayer replayer(instrument);
        const auto replay = replayer.replay(
            input, std::nullopt, 100, 0, std::numeric_limits<std::uint64_t>::max(), false,
            &observer);
        const auto& summary = observer.summary();
        std::cout
            << "{\n"
            << "  \"schema\": \"crypto-hft-cross-stream-timing-v1\",\n"
            << "  \"raw_file\": \"" << input.filename().string() << "\",\n"
            << "  \"file_index\": " << file_index << ",\n"
            << "  \"observation_window_ms\": " << window_ms << ",\n"
            << "  \"raw_records\": " << replay.raw_records << ",\n"
            << "  \"parse_failures\": " << replay.parse_failures << ",\n"
            << "  \"trades_seen\": " << summary.trades_seen << ",\n"
            << "  \"trades_recorded\": " << summary.trades_recorded << ",\n"
            << "  \"at_quote\": " << summary.at_quote << ",\n"
            << "  \"through_quote\": " << summary.through_quote << ",\n"
            << "  \"outside_quote\": " << summary.outside_quote << ",\n"
            << "  \"discarded_on_desync\": " << summary.discarded_on_desync << ",\n"
            << "  \"unresolved_qty_reaction\": " << summary.unresolved_qty_reaction << ",\n"
            << "  \"unresolved_quote_removal\": " << summary.unresolved_quote_removal << ",\n"
            << "  \"unresolved_best_change\": " << summary.unresolved_best_change << ",\n"
            << "  \"unresolved_mid_adverse\": " << summary.unresolved_mid_adverse << "\n"
            << "}\n";
        return replay.parse_failures == 0 ? 0 : 2;
    } catch (const std::exception& error) {
        std::cerr << "native_timing_audit: " << error.what() << '\n';
        return 1;
    }
}
