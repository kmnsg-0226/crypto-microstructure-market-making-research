#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

#include "replay/event_replayer.hpp"
#include "research/native_dataset_exporter.hpp"

namespace {

struct Config {
    std::filesystem::path input;
    std::vector<std::filesystem::path> continuation_inputs;
    std::filesystem::path dataset;
    std::optional<std::filesystem::path> qc;
    std::string symbol{"BTCUSDT"};
    crypto::research::NativeDatasetConfig dataset_config;
};

Config parse_args(int argc, char** argv) {
    if (argc < 2) {
        throw std::invalid_argument(
            "Usage: native_dataset_export RAW.chft.zst --dataset OUT.csv.zst [--qc OUT.json]\n"
            "       [--chain NEXT.chft.zst] [--symbol BTCUSDT] [--file-index N] [--grid-ms 100] [--order-lots 5]\n"
            "       [--adverse-half-ticks 2] [--fill-horizon-ms 30000] [--lambda 0.5]\n"
            "One raw file per run: the corpus files are separate collector processes and must\n"
            "never be replayed as one continuous book.");
    }
    Config config;
    config.input = argv[1];
    for (int i = 2; i < argc; ++i) {
        const std::string arg = argv[i];
        auto value = [&](const char* name) -> std::string {
            if (++i >= argc) {
                throw std::invalid_argument(std::string("missing value for ") + name);
            }
            return argv[i];
        };
        if (arg == "--dataset") {
            config.dataset = value("--dataset");
        } else if (arg == "--chain") {
            config.continuation_inputs.emplace_back(value("--chain"));
        } else if (arg == "--qc") {
            config.qc = value("--qc");
        } else if (arg == "--symbol") {
            config.symbol = value("--symbol");
        } else if (arg == "--file-index") {
            config.dataset_config.file_index =
                static_cast<std::uint32_t>(std::stoul(value("--file-index")));
        } else if (arg == "--grid-ms") {
            config.dataset_config.grid_ms = std::stoull(value("--grid-ms"));
        } else if (arg == "--order-lots") {
            config.dataset_config.order_lots = std::stoll(value("--order-lots"));
        } else if (arg == "--adverse-half-ticks") {
            config.dataset_config.adverse_threshold_half_ticks =
                std::stoll(value("--adverse-half-ticks"));
        } else if (arg == "--fill-horizon-ms") {
            config.dataset_config.fill_horizon_ms = std::stoull(value("--fill-horizon-ms"));
        } else if (arg == "--lambda") {
            config.dataset_config.weight_lambda = std::stod(value("--lambda"));
        } else {
            throw std::invalid_argument("unknown argument: " + arg);
        }
    }
    if (config.dataset.empty()) {
        throw std::invalid_argument("--dataset is required");
    }
    return config;
}

void write_qc(
    std::ostream& out,
    const Config& config,
    const crypto::binance::InstrumentInfo& instrument,
    const crypto::replay::ReplaySummary& replay,
    const crypto::research::NativeDatasetSummary& dataset,
    const std::vector<std::string>& failures) {
    const auto& c = config.dataset_config;
    out << std::setprecision(12)
        << "{\n"
        << "  \"schema\": \"crypto-hft-native-qc-v1\",\n"
        << "  \"source\": \"native_binance_usdm\",\n"
        << "  \"raw_file\": \"" << config.input.filename().string() << "\",\n"
        << "  \"file_index\": " << c.file_index << ",\n"
        << "  \"symbol\": \"" << instrument.symbol() << "\",\n"
        << "  \"tick_size\": \"" << instrument.tick_size() << "\",\n"
        << "  \"step_size\": \"" << instrument.step_size() << "\",\n"
        << "  \"file\": {\n"
        << "    \"raw_records\": " << replay.raw_records << ",\n"
        << "    \"depth_events\": " << replay.depth_events << ",\n"
        << "    \"applied_depth_events\": " << replay.applied_depth_events << ",\n"
        << "    \"stale_depth_events\": " << replay.stale_depth_events << ",\n"
        << "    \"trade_events\": " << replay.trade_events << ",\n"
        << "    \"snapshots\": " << replay.snapshots << ",\n"
        << "    \"depth_stream_opens\": " << dataset.depth_stream_opens << ",\n"
        << "    \"trade_stream_opens\": " << dataset.trade_stream_opens << ",\n"
        << "    \"sequence_gaps\": " << replay.sequence_gaps << ",\n"
        << "    \"parse_failures\": " << replay.parse_failures << ",\n"
        << "    \"crossed_book_states\": " << replay.crossed_book_states << ",\n"
        << "    \"empty_bid_states\": " << replay.empty_bid_states << ",\n"
        << "    \"empty_ask_states\": " << replay.empty_ask_states << ",\n"
        << "    \"first_local_receive_ns\": " << replay.first_local_receive_ns << ",\n"
        << "    \"last_local_receive_ns\": " << replay.last_local_receive_ns << ",\n"
        << "    \"first_exchange_time_ms\": " << replay.first_exchange_time_ms << ",\n"
        << "    \"last_exchange_time_ms\": " << replay.last_exchange_time_ms << ",\n"
        << "    \"final_update_id\": " << replay.final_update_id << ",\n"
        << "    \"final_checksum\": \"0x" << std::hex << replay.final_checksum << std::dec
        << "\",\n"
        << "    \"duration_s\": "
        << static_cast<double>(replay.last_local_receive_ns - replay.first_local_receive_ns) / 1e9
        << "\n  },\n"
        << "  \"dataset\": {\n"
        << "    \"rows\": " << dataset.rows << ",\n"
        << "    \"segments\": " << dataset.segments << ",\n"
        << "    \"depth_events_in_segment\": " << dataset.depth_events_in_segment << ",\n"
        << "    \"trade_events_in_segment\": " << dataset.trade_events_in_segment << ",\n"
        << "    \"depth_events_outside_segment\": " << dataset.skipped_depth_events << ",\n"
        << "    \"valid_research_s\": "
        << static_cast<double>(dataset.valid_research_ns) / 1e9 << ",\n"
        << "    \"grid_ms\": " << c.grid_ms << ",\n"
        << "    \"order_lots\": " << c.order_lots << ",\n"
        << "    \"adverse_threshold_half_ticks\": " << c.adverse_threshold_half_ticks << ",\n"
        << "    \"fill_horizon_ms\": " << c.fill_horizon_ms << ",\n"
        << "    \"weight_lambda\": " << c.weight_lambda << "\n"
        << "  },\n"
        << "  \"segments\": [\n";
    for (std::size_t i = 0; i < dataset.segment_qc.size(); ++i) {
        const auto& segment = dataset.segment_qc[i];
        out << "    {\"segment_id\": " << segment.segment_id
            << ", \"start_ns\": " << segment.start_ns
            << ", \"end_ns\": " << segment.end_ns
            << ", \"duration_s\": "
            << static_cast<double>(segment.end_ns - segment.start_ns) / 1e9
            << ", \"first_exchange_time_ms\": " << segment.first_exchange_time_ms
            << ", \"last_exchange_time_ms\": " << segment.last_exchange_time_ms
            << ", \"first_update_id\": " << segment.first_update_id
            << ", \"final_update_id\": " << segment.final_update_id
            << ", \"depth_events\": " << segment.depth_events
            << ", \"trade_events\": " << segment.trade_events
            << ", \"rows\": " << segment.rows
            << ", \"bid_full_fills\": " << segment.bid_fills
            << ", \"ask_full_fills\": " << segment.ask_fills
            << ", \"close_reason\": \"" << segment.close_reason << "\"}"
            << (i + 1 == dataset.segment_qc.size() ? "\n" : ",\n");
    }
    out << "  ],\n  \"failures\": [";
    for (std::size_t i = 0; i < failures.size(); ++i) {
        out << (i == 0 ? "" : ", ") << '"' << failures[i] << '"';
    }
    out << "]\n}\n";
}

}  // namespace

int main(int argc, char** argv) {
    try {
        const auto config = parse_args(argc, argv);
        std::vector<std::filesystem::path> inputs{config.input};
        inputs.insert(inputs.end(), config.continuation_inputs.begin(), config.continuation_inputs.end());
        const auto instrument = crypto::replay::EventReplayer::instrument_from_raw_files(
            inputs, config.symbol);
        crypto::research::NativeDatasetExporter exporter(
            config.dataset, config.dataset_config);
        crypto::replay::EventReplayer replayer(instrument);
        const auto replay = replayer.replay(
            inputs, std::nullopt, 100, 0,
            std::numeric_limits<std::uint64_t>::max(), false, &exporter);
        const auto& dataset = exporter.summary();

        // Bad intervals are never silently dropped: every one of these ends the run non-zero.
        std::vector<std::string> failures;
        if (replay.parse_failures != 0) {
            failures.emplace_back("corrupted_or_unparseable_records");
        }
        if (replay.crossed_book_states != 0) {
            failures.emplace_back("crossed_book_after_synchronization");
        }
        if (dataset.missing_initial_sync != 0) {
            failures.emplace_back("missing_initial_synchronization_state");
        }
        if (dataset.unrecoverable_gaps != 0) {
            failures.emplace_back("unrecoverable_sequence_gap_at_end_of_file");
        }

        if (config.qc) {
            std::ofstream qc(*config.qc);
            if (!qc) {
                throw std::runtime_error("cannot open qc output: " + config.qc->string());
            }
            write_qc(qc, config, instrument, replay, dataset, failures);
            qc.flush();
            if (!qc) {
                throw std::runtime_error("failed writing qc output");
            }
        }
        write_qc(std::cout, config, instrument, replay, dataset, failures);
        for (const auto& failure : failures) {
            std::cerr << "native_dataset_export: QC failure: " << failure << '\n';
        }
        return failures.empty() ? 0 : 2;
    } catch (const std::exception& error) {
        std::cerr << "native_dataset_export: " << error.what() << '\n';
        return 1;
    }
}
