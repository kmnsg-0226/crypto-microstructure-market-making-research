#!/usr/bin/env bash
# Cross-stream timing event study over the frozen native_dev_v1 corpus.
# One raw file per invocation: the three captures are separate collector processes.
set -euo pipefail
cd "$(dirname "$0")/.."

FILES=(
  BTCUSDT-LONDON-20260817T210035Z.chft.zst
  BTCUSDT-LONDON-20260817T221753Z.chft.zst
  BTCUSDT-LONDON-20260818T062918Z.chft.zst
)
mkdir -p data/research/native_predictive_v1 research/native_predictive_v1
for index in "${!FILES[@]}"; do
  ./build/cpp/native_timing_audit "data/raw/aws_london/native_dev_v1/${FILES[$index]}" \
    --out "data/research/native_predictive_v1/cross_stream_timing_file${index}.csv.zst" \
    --file-index "$index" > "research/native_predictive_v1/timing_qc_file${index}.json"
done
