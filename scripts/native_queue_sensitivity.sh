#!/usr/bin/env bash
# Fixed 5x5 alpha/beta queue-assumption replay over the frozen native_dev_v1 corpus.
# One raw file per invocation: the three captures are separate collector processes.
set -euo pipefail
cd "$(dirname "$0")/.."

FILES=(
  BTCUSDT-LONDON-20260817T210035Z.chft.zst
  BTCUSDT-LONDON-20260817T221753Z.chft.zst
  BTCUSDT-LONDON-20260818T062918Z.chft.zst
)
mkdir -p data/research/native_economic_v1 research/native_economic_v1
for index in "${!FILES[@]}"; do
  ./build/cpp/native_queue_sensitivity "data/raw/aws_london/native_dev_v1/${FILES[$index]}" \
    --fills "data/research/native_economic_v1/queue_fills_file${index}.csv.zst" \
    --mid "data/research/native_economic_v1/mid_path_file${index}.csv.zst" \
    --file-index "$index" > "research/native_economic_v1/queue_qc_file${index}.json"
done
