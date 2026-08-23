# Methodology

## Features

The native 100 ms exporter samples only synchronized L2 state. It exposes L1/L5/L10 and weighted
OBI, microprice displacement, spread, visible depth, trailing depth-flow/OFI proxies, aggressive
trade imbalance/volume, activity, and backward price movement. Displayed L2 reductions are not
called cancellations because aggregated L2 cannot identify their cause.

## Queue and fills

Binance aggregated L2 exposes no order IDs or FIFO rank. Passive studies therefore make explicit
conservative assumptions: a quote joins behind displayed quantity, later additions remain behind,
matching aggressive prints may advance queue, trade-through may fill, and snapshots/gaps cancel
the probe. These are bounds, not observed queue position.

## Validation and costs

Validation uses chronological contiguous blocks, no random splits, purges exceeding forward label
horizons, and segment-safe targets. The final executable-PnL study enters long at the next
observable ask and exits at the bid; shorts reverse this. Spread is embedded once in that bid/ask
return, then 1/2/3/5 bp per-side costs are subtracted. Economic results use a non-overlapping tape.

The expanded manifest freezes exact hashes, synchronized intervals, and exclusion of the unopened
2026-08-23 holdout. Raw and large derived data remain local; compact QC and reports are retained.
