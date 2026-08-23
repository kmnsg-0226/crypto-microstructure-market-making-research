#include <cstdint>
#include <filesystem>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>

#include "replay/event_replayer.hpp"
#include "research/level_lifecycle.hpp"

int main(int argc, char** argv) {
    try {
        if (argc < 4) {
            throw std::invalid_argument(
                "Usage: native_level_lifecycle RAW.chft.zst --episodes EP.csv.zst\n"
                "       --grid GRID.csv.zst [--symbol BTCUSDT] [--file-index N] [--grid-ms 100]\n"
                "Reconstructs best-price level episodes and their observable lifecycle state.");
        }
        const std::filesystem::path input = argv[1];
        std::filesystem::path episodes;
        std::filesystem::path grid;
        std::string symbol = "BTCUSDT";
        crypto::research::LevelLifecycleConfig config;
        for (int i = 2; i < argc; ++i) {
            const std::string arg = argv[i];
            auto value = [&](const char* name) -> std::string {
                if (++i >= argc) {
                    throw std::invalid_argument(std::string("missing value for ") + name);
                }
                return argv[i];
            };
            if (arg == "--episodes") {
                episodes = value("--episodes");
            } else if (arg == "--grid") {
                grid = value("--grid");
            } else if (arg == "--symbol") {
                symbol = value("--symbol");
            } else if (arg == "--file-index") {
                config.file_index = static_cast<std::uint32_t>(std::stoul(value("--file-index")));
            } else if (arg == "--grid-ms") {
                config.grid_ms = std::stoull(value("--grid-ms"));
            } else {
                throw std::invalid_argument("unknown argument: " + arg);
            }
        }
        if (episodes.empty() || grid.empty()) {
            throw std::invalid_argument("--episodes and --grid are required");
        }

        const auto instrument =
            crypto::replay::EventReplayer::instrument_from_raw_file(input, symbol);
        crypto::research::LevelLifecycleObserver observer(episodes, grid, config);
        crypto::replay::EventReplayer replayer(instrument);
        const auto replay = replayer.replay(
            input, std::nullopt, 100, 0, std::numeric_limits<std::uint64_t>::max(), false,
            &observer);
        const auto& summary = observer.summary();
        std::cout
            << "{\n"
            << "  \"schema\": \"crypto-hft-level-lifecycle-v1\",\n"
            << "  \"raw_file\": \"" << input.filename().string() << "\",\n"
            << "  \"file_index\": " << config.file_index << ",\n"
            << "  \"grid_ms\": " << config.grid_ms << ",\n"
            << "  \"raw_records\": " << replay.raw_records << ",\n"
            << "  \"parse_failures\": " << replay.parse_failures << ",\n"
            << "  \"segments\": " << summary.segments << ",\n"
            << "  \"episodes\": " << summary.episodes << ",\n"
            << "  \"grid_rows\": " << summary.grid_rows << ",\n"
            << "  \"depth_events\": " << summary.depth_events << ",\n"
            << "  \"trade_events\": " << summary.trade_events << ",\n"
            << "  \"prints_at_quote\": " << summary.prints_at_quote << ",\n"
            << "  \"prints_through\": " << summary.prints_through << ",\n"
            << "  \"prints_outside\": " << summary.prints_outside << ",\n"
            << "  \"explained_removal_lots\": " << summary.explained_removal_lots << ",\n"
            << "  \"unexplained_removal_lots\": " << summary.unexplained_removal_lots << ",\n"
            << "  \"replenished_lots\": " << summary.replenished_lots << "\n"
            << "}\n";
        return replay.parse_failures == 0 ? 0 : 2;
    } catch (const std::exception& error) {
        std::cerr << "native_level_lifecycle: " << error.what() << '\n';
        return 1;
    }
}
