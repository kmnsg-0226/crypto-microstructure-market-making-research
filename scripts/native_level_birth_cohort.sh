#!/usr/bin/env bash
# Level-birth cohort: one hypothetical order placed immediately after the depth event that makes
# a price the best on its side. Not "front of queue" — everything displayed at that instant is
# treated as ahead of the order.
set -euo pipefail
cd "$(dirname "$0")/.."

FILES=(
  BTCUSDT-LONDON-20260817T210035Z.chft.zst
  BTCUSDT-LONDON-20260817T221753Z.chft.zst
  BTCUSDT-LONDON-20260818T062918Z.chft.zst
)
mkdir -p data/research/native_queue_tail_v1 research/native_queue_tail_v1
for index in "${!FILES[@]}"; do
  ./build/cpp/native_queue_sensitivity "data/raw/aws_london/native_dev_v1/${FILES[$index]}" \
    --mode level_birth \
    --fills "data/research/native_queue_tail_v1/birth_fills_file${index}.csv.zst" \
    --mid "data/research/native_queue_tail_v1/birth_mid_file${index}.csv.zst" \
    --file-index "$index" > "research/native_queue_tail_v1/birth_qc_file${index}.json"
done
