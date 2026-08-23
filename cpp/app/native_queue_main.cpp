#include <cstdint>
#include <filesystem>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>

#include "replay/event_replayer.hpp"
#include "research/queue_sensitivity.hpp"

int main(int argc, char** argv) {
    try {
        if (argc < 4) {
            throw std::invalid_argument(
                "Usage: native_queue_sensitivity RAW.chft.zst --fills FILLS.csv.zst\n"
                "       --mid MID.csv.zst [--symbol BTCUSDT] [--file-index N]\n"
                "       [--mode grid|level_birth] [--placement-ms 1000] [--window-ms 30000]\n"
                "       [--order-lots 5]\n"
                "Replays the fixed 5x5 alpha/beta queue-assumption grid causally.");
        }
        const std::filesystem::path input = argv[1];
        std::filesystem::path fills;
        std::filesystem::path mid;
        std::string symbol = "BTCUSDT";
        crypto::research::QueueSensitivityConfig config;
        for (int i = 2; i < argc; ++i) {
            const std::string arg = argv[i];
            auto value = [&](const char* name) -> std::string {
                if (++i >= argc) {
                    throw std::invalid_argument(std::string("missing value for ") + name);
                }
                return argv[i];
            };
            if (arg == "--fills") {
                fills = value("--fills");
            } else if (arg == "--mid") {
                mid = value("--mid");
            } else if (arg == "--symbol") {
                symbol = value("--symbol");
            } else if (arg == "--file-index") {
                config.file_index = static_cast<std::uint32_t>(std::stoul(value("--file-index")));
            } else if (arg == "--placement-ms") {
                config.placement_interval_ms = std::stoull(value("--placement-ms"));
            } else if (arg == "--window-ms") {
                config.observation_window_ms = std::stoull(value("--window-ms"));
            } else if (arg == "--mode") {
                const auto mode = value("--mode");
                if (mode == "grid") {
                    config.mode = crypto::research::PlacementMode::Grid;
                } else if (mode == "level_birth") {
                    config.mode = crypto::research::PlacementMode::LevelBirth;
                } else {
                    throw std::invalid_argument("--mode must be grid or level_birth");
                }
            } else if (arg == "--order-lots") {
                config.order_lots = std::stoll(value("--order-lots"));
            } else {
                throw std::invalid_argument("unknown argument: " + arg);
            }
        }
        if (fills.empty() || mid.empty()) {
            throw std::invalid_argument("--fills and --mid are required");
        }

        const auto instrument =
            crypto::replay::EventReplayer::instrument_from_raw_file(input, symbol);
        crypto::research::QueueSensitivityObserver observer(fills, mid, config);
        crypto::replay::EventReplayer replayer(instrument);
        const auto replay = replayer.replay(
            input, std::nullopt, 100, 0, std::numeric_limits<std::uint64_t>::max(), false,
            &observer);
        const auto& summary = observer.summary();
        std::cout
            << "{\n"
            << "  \"schema\": \"crypto-hft-queue-sensitivity-v1\",\n"
            << "  \"raw_file\": \"" << input.filename().string() << "\",\n"
            << "  \"file_index\": " << config.file_index << ",\n"
            << "  \"mode\": \""
            << (config.mode == crypto::research::PlacementMode::Grid ? "grid"
                                                                     : "level_birth")
            << "\",\n"
            << "  \"placement_interval_ms\": " << config.placement_interval_ms << ",\n"
            << "  \"observation_window_ms\": " << config.observation_window_ms << ",\n"
            << "  \"order_lots\": " << config.order_lots << ",\n"
            << "  \"raw_records\": " << replay.raw_records << ",\n"
            << "  \"parse_failures\": " << replay.parse_failures << ",\n"
            << "  \"crossed_book_states\": " << replay.crossed_book_states << ",\n"
            << "  \"segments\": " << summary.segments << ",\n"
            << "  \"placements\": " << summary.placements << ",\n"
            << "  \"emitted_rows\": " << summary.emitted_rows << ",\n"
            << "  \"mid_path_points\": " << summary.mid_path_points << ",\n"
            << "  \"depth_events\": " << summary.depth_events << ",\n"
            << "  \"trade_events\": " << summary.trade_events << ",\n"
            << "  \"unexplained_removal_lots\": " << summary.unexplained_removal_lots << ",\n"
            << "  \"trade_explained_removal_lots\": " << summary.trade_explained_removal_lots
            << ",\n"
            << "  \"behind_attributed_removal_lots\": "
            << summary.behind_attributed_removal_lots << "\n"
            << "}\n";
        return replay.parse_failures == 0 ? 0 : 2;
    } catch (const std::exception& error) {
        std::cerr << "native_queue_sensitivity: " << error.what() << '\n';
        return 1;
    }
}
