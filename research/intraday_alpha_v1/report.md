# Historical directional-alpha V1 feasibility study

Universe: BTCUSDT, ETHUSDT, SOLUSDT perpetuals. Data window: 2024-01-01 through 2025-12-31 UTC, 210,528 aligned 5m rows per symbol. No existing native microstructure, Kraken, or forward-capture data was used.

## Timestamp and execution semantics

Features use a fully completed 5m bar ending at decision timestamp `t`. The hypothetical trade enters at the next observable bar open (`t+5m`) and exits after the requested horizon. A 60m target therefore uses `open[t+1]` to `close[t+13]`. OOS tapes are non-overlapping at each horizon (`every h bars`).

Costs were 4bp taker fee plus 1.5bp slippage per side: 11bp round trip before funding. Funding was joined backward as-of its event timestamp and charged only when the simulated holding interval crossed the event. Future funding announcements were not forward-filled.

Validation used chronological folds with a 365-day training window, 90-day validation window, 90-day test window, and a horizon purge at train/validation/test boundaries. 2025-07-01 onward was held out as a final test period. The score threshold was fixed at zero before evaluation; no final-period optimization was performed. Logistic/Ridge fitting was subsampled hourly for runtime, while predictions and economic tapes remained 5m-aligned.

AggTrades were not included in V1: bulk historical files are materially larger and the OHLCV/mark/index/premium/OI/funding subset already tests feasibility.

## Economic result

Every symbol/horizon’s best pre-final-OOS configuration had negative net edge after costs:

| Symbol | 5m net bp | 15m net bp | 30m net bp | 60m net bp | Classification |
|---|---:|---:|---:|---:|---|
| BTCUSDT | -10.78 | -10.43 | -9.29 | -8.73 | C |
| ETHUSDT | -10.87 | -10.23 | -9.69 | -5.82 | C |
| SOLUSDT | -10.76 | -10.20 | -8.58 | -11.21 | C |

These are the best rows across the simple models and feature ablations before the final holdout; gross edge was below the approximately 11bp round-trip cost in every case. Accuracy/AUC improvements were small and economically irrelevant. The strongest pre-final candidate was ETHUSDT 60m, with 5.18bp gross edge and -5.82bp net edge; its AUC was 0.497.

The final holdout contained isolated positive rows, including ETHUSDT 60m momentum at +4.09bp net edge and SOLUSDT 60m momentum at +1.84bp, but these were not positive in pre-final OOS and were not selected. They do not establish robustness. The positive holdout rows were also concentrated: their top-5%-trade net-P&L shares were approximately 1.86 and 4.78 respectively.

Full per-symbol/per-horizon/model/ablation metrics include observations, accuracy, AUC, IC, deciles, gross/net Sharpe, annualized return, drawdown, turnover, trade count, gross edge, all-in cost, net edge, long/short edge, and year stability in `metrics.json`; the scalar columns are also in `metrics.csv`.

## Feature ablations

The ablations were price/volume only, plus premium/basis, plus open interest, and full including funding. Adding basis, open interest, or funding did not produce a positive pre-final net edge. No ablation justified adding aggTrade flow at this stage.

## Final verdict

All 12 symbol/horizon combinations are economically classified C: economically falsified for this V1 after conservative taker costs. No primary horizon/configuration is selected for further work. AUC or gross-only results are not sufficient to reopen the strategy.
