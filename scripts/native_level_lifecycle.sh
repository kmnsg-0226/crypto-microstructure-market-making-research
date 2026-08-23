#!/usr/bin/env bash
# Best-price level episodes and their observable lifecycle over the frozen native_dev_v1 corpus.
set -euo pipefail
cd "$(dirname "$0")/.."

FILES=(
  BTCUSDT-LONDON-20260817T210035Z.chft.zst
  BTCUSDT-LONDON-20260817T221753Z.chft.zst
  BTCUSDT-LONDON-20260818T062918Z.chft.zst
)
mkdir -p data/research/native_queue_tail_v1 research/native_queue_tail_v1
for index in "${!FILES[@]}"; do
  ./build/cpp/native_level_lifecycle "data/raw/aws_london/native_dev_v1/${FILES[$index]}" \
    --episodes "data/research/native_queue_tail_v1/level_episodes_file${index}.csv.zst" \
    --grid "data/research/native_queue_tail_v1/level_grid_file${index}.csv.zst" \
    --file-index "$index" > "research/native_queue_tail_v1/level_qc_file${index}.json"
done
