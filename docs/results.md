# Results and limitations

## Native Binance phases

Native replay established statistically useful OBI, depth-flow, microprice, trade-flow, and
sweep-risk relationships. The subsequent phases tested the frictions those relationships omit:
queue uncertainty, adverse selection, passive maker EV, counterfactual cancellation, directional
cost hurdles, and signal decay. Passive maker feasibility was poor under conservative assumptions;
directional sweep monetisation was falsified. Longer-horizon information remained marginal and did
not overturn the economic result. The retained Phase 1–6 evidence is indexed at
[`research/native/README.md`](../research/native/README.md).

## Kraken MBO/FIFO

The Kraken Futures PI_XBTUSD MBO audit found useful order-event and execution evidence but
remaining FIFO attribution, retention, and snapshot warm-up ambiguity. Its B/B2 feasibility
verdict supports further validation, not queue-certainty claims. See
[`research/kraken_mbo_audit/report.md`](../research/kraken_mbo_audit/report.md).

## Final executable-PnL result

Six closed native captures supplied 122.961354 synchronized usable hours with clean QC and no
temporal overlap. Blocked OOF long/short executable bid/ask returns across 250 ms–60 s compared
OBI-only ridge, linear ridge, and a fixed LightGBM model at 10/5/2/1% tails. The economic tape
forbade overlapping positions.

The strongest LightGBM non-overlapping OOF result was +0.1245 bp/trade gross and -9.8755
bp/trade net at the primary 5 bp-per-side cost. No LightGBM tape was positive across the 1–5
bp-per-side sensitivity grid. Final verdict: **C — standalone Binance microstructure alpha is
economically closed under this protocol.** The 2026-08-23 holdout remains deliberately unopened.

See [`research/native_executable_pnl/report.md`](../research/native_executable_pnl/report.md) and
[`oof_economics.csv`](../research/native_executable_pnl/oof_economics.csv).

## Limitations

- Aggregated L2 cannot prove FIFO queue position or distinguish every cancellation from trade.
- The data is limited in duration, venue, and collector scope.
- Cost hurdles are feasibility assumptions, not account-specific fee claims.
- This is research and falsification evidence, not investment advice or a live strategy.
