# Frozen execution-economics research

## Result

The frozen microstructure signal remains directionally useful, but the V1 aggressive/taker
implementation is not tradable under the predefined baseline assumptions. Positive mid-price
markout survives the historical bid/ask spread and small-order top-10 depth walk, but it is far
smaller than a 5 bps fee on each side. This rejects the taker implementation; it does not reject
the signal as a fair-value or adverse-selection input.

No market making, passive fills, queue model, leverage optimization, or live orders were added.
The running Binance collector was not modified, stopped, or restarted.

## Reproducibility and split gate

- Frozen alpha spec: `research/specs/research_spec_frozen.json`
- Alpha SHA-256: `011ea143ddf646b4ba482b0030a6e86d416a7692e16349daa1017543ef0a6f84`
- Frozen execution spec: `research/specs/execution_spec_frozen.json`
- Execution SHA-256: `de04949d11e8788c926992b7abc217b01997d9f5b534d8cfc792cc9da94533d2`
- Code commit recorded before the experiment: `bf9605fd3c487ad2192d8d327ba82ff4bee928e8`
- L2 manifest SHA-256: `5893274ec07dde310d41d48b3b1e66bd01af4ab7507dc02aa11d409583a97a03`
- Trades manifest SHA-256: `ec3af0d61e9aa8258becaf4af980d09822385aeba15c992aa9ce35b87c79ae06`
- Dataset bundle SHA-256: `e8ae8653d67ee23ffd66eb9d717bb040f645f7657e176e515354d2b7affdfed6`

Development selection used only 2026-01-01 through 2026-05-01. The resulting execution spec was
written and hashed before June was evaluated. June validation and July/August OOS require the
frozen spec and exact split dates. The OOS audit log is written before OOS feature files are
loaded.

The selected rule is:

- frozen `combined` development OLS prediction;
- trade when absolute predicted 1-second markout is at least 20 BTCUSDT ticks;
- long for a positive prediction and short for a negative prediction;
- one position at a time, with intervening signals counted and skipped;
- fixed 1,000 USDT notional, floored to the 0.001 BTC quantity step;
- decision-time horizon of 1 second;
- baseline latency input 50 ms, which necessarily uses the next 100 ms historical quote;
- taker fill through contemporaneous BBO and, when needed, a top-10 VWAP walk;
- 5 bps fee per side as a configurable research assumption, not a claim about a current account;
- Layer 4 adds one adverse tick per fill.

The 20-tick rule was not profitable in development after costs. It was selected only because it
had the least-negative Layer 3 mean among candidates satisfying the predeclared activity floor.
All candidate thresholds had negative Layer 3 performance. No threshold was changed after June
or OOS was observed.

## Accounting

For a long, entry buys asks and exit sells bids. For a short, entry sells bids and exit buys asks.
The historical spread is therefore already embedded in cash-flow PnL and is not subtracted a
second time. Quantity above touch size walks visible levels in price order; insufficient top-10
depth rejects the trade. Entry, holding, and exit must remain in one valid feature segment.

The cost layers are:

1. decision mid to exit mid signed markout;
2. zero-latency bid/ask plus visible-depth execution, no fee;
3. Layer 2 plus fee;
4. actual delayed entry plus fee;
5. Layer 4 plus the conservative per-fill penalty.

Each trade records executable prices, quantity, entry and exit notional, entry and exit fee,
spread/depth/latency drag in ticks and USDT, turnover-normalized bps, participation, and the five
PnL layers.

## Headline split results

| Split | Days | Trades | L0 mid ticks/trade | L1 executable ticks/trade | L3 ticks/trade | L4 PnL USDT | L4 win rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| Development | 5 | 72,235 | 40.924 | 38.307 | -717.706 | -66,971.84 | 0.047% |
| June validation | 1 | 12,186 | 37.339 | 35.388 | -703.904 | -11,364.21 | 0.066% |
| July/August OOS | 2 | 14,519 | 32.897 | 30.920 | -586.762 | -13,697.58 | 0.034% |

OOS Layer 0 PnL was +772.32 USDT and zero-latency executable Layer 1 PnL was +725.97 USDT.
Baseline delayed Layer 3 PnL was -13,650.99 USDT and stressed Layer 4 PnL was -13,697.58 USDT.
OOS turnover was 28,030,198.12 USDT, exposure was 14,519 seconds, and Layer 4 maximum drawdown
was 13,697.58 USDT. The average Layer 4 result was -0.9434 USDT per completed trade.

## Why taker execution fails

| Component, ticks/trade | Development | Validation | OOS |
|---|---:|---:|---:|
| Mid markout | 40.924 | 37.339 | 32.897 |
| Spread drag | 1.857 | 1.470 | 1.354 |
| Visible-depth drag | 0.760 | 0.481 | 0.622 |
| Latency decay | 16.424 | 16.017 | 15.410 |
| Fee drag, 5 bps/side | 739.590 | 723.276 | 602.272 |
| Stress penalty | 2.000 | 2.000 | 2.000 |

The dominant failure is the fee, not the observed spread or top-10 depth walk. On OOS, a 50 ms
latency input maps to the next 100 ms quote and reduces executable edge from 30.94 to 15.51
ticks/trade. The 5 bps/side fee then removes about 602.27 ticks/trade.

The post-execution break-even fee was 0.149 bps/side in development, 0.134 in validation, and
0.130 in OOS. Break-even total friction measured from the decision mid was approximately 0.279,
0.258, and 0.276 bps/side respectively. These are far below the 5 bps/side baseline.

## Fee and latency sensitivity

The following rows exclude the extra Layer 4 penalty and show Layer 3 mean ticks/trade. The quote
delay column is the delay actually observable on the 100 ms dataset.

| Split | Latency input | Quote delay | 0 bps | 2 bps/side | 5 bps/side |
|---|---:|---:|---:|---:|---:|
| Development | 0 ms | 0 ms | 38.31 | -257.53 | -701.28 |
| Development | 50 ms | 100 ms | 21.88 | -273.95 | -717.71 |
| Development | 250 ms | 300 ms | 12.02 | -283.82 | -727.57 |
| Validation | 0 ms | 0 ms | 35.43 | -253.88 | -687.85 |
| Validation | 50 ms | 100 ms | 19.37 | -269.94 | -703.90 |
| Validation | 250 ms | 300 ms | 10.30 | -279.01 | -712.97 |
| OOS | 0 ms | 0 ms | 30.94 | -209.97 | -571.33 |
| OOS | 50 ms | 100 ms | 15.51 | -225.40 | -586.76 |
| OOS | 250 ms | 300 ms | 8.15 | -232.76 | -594.12 |

No sensitivity cell was used to revise the headline rule.

## Horizon and signal diagnostics

At the common frozen 20-tick prediction threshold, fully stressed mean ticks/trade were:

| Split | 100 ms | 500 ms | 1 s | 5 s |
|---|---:|---:|---:|---:|
| Development | -765.58 | -725.93 | -719.71 | -727.22 |
| Validation | -756.45 | -715.52 | -705.90 | -697.18 |
| OOS | -613.27 | -586.07 | -588.76 | -594.67 |

The 100 ms and 500 ms model predictions rarely exceed 20 ticks, so their trade counts are much
smaller and this is a diagnostic rather than a new horizon selection. The canonical 1-second
horizon remains unchanged.

The same frozen 20-tick threshold produced 72,235/12,186/14,519 combined-model trades by split.
The frozen OBI-only and TI-only model predictions did not reach 20 ticks, so their baseline
comparison rows correctly contain zero trades. The development candidate table still preserves
their predeclared lower-threshold results. Selecting separate post-OOS thresholds would violate
the frozen protocol, so it was not done. This limits the direct OBI-vs-TI execution comparison and
is reported rather than repaired after seeing OOS.

## Counts, gaps, capacity, and determinism

Canonical completed trades were 72,235 development, 12,186 validation, and 14,519 OOS. Signals
skipped because another position was active were 189,343, 31,666, and 35,562. Gap exclusions were
17, 12, and 17. Top-10 depth exclusions were 6, 7, and 94. No quote was fabricated across an
invalid segment.

At 1,000 USDT, the visible-liquidity participation flag was raised 5,575 times in development,
176 in validation, and 358 in OOS. This is a warning, not an exclusion. The predefined 10,000
USDT scenario both reduced completed trades and substantially increased flagged participation;
it provides no evidence of scalable capacity. The 100 USDT scenario had no participation flags
but remained deeply negative after fees.

Every canonical day was replayed twice. Trade-frame checksums and counters matched on all eight
days. Split checksums are recorded in each `run_summary.json`.

## Sharpe caveat

Layer 4 sample daily Sharpe was -2.086 over five development days and -1.355 over two OOS days.
June has one day, so its daily volatility and Sharpe are undefined. Annualized values are emitted
only as `EXPLORATORY / LOW-SAMPLE`; eight isolated dates cannot support a robust Sharpe claim. The
development five-day bootstrap interval for unannualized daily Sharpe was approximately
[-6.53, -1.46]. No Sharpe value was used for model or threshold selection.

## Outputs and commands

The implementation is in `execution_research/engine.py`, `execution_research/pipeline.py`, and
`execution_research/reporting.py`. Machine-readable artifacts are written below
`data/research/tardis/reports/execution/` and include:

- per-split canonical trades, counters, daily PnL, layer metrics, determinism, and run summary;
- cross-split `stage_layer_summary.csv` and `daily_pnl_all_splits.csv`;
- `horizon_comparison.csv`, `signal_comparison.csv`, `cost_sensitivity.csv`, and
  `capacity_comparison.csv`;
- `final_summary.json` and the pre-OOS `oos/oos_audit_log.json`.

Reproduction order:

```bash
.venv/bin/python -m pyresearch.execution.pipeline select
.venv/bin/python -m pyresearch.execution.pipeline development
.venv/bin/python -m pyresearch.execution.pipeline validation
.venv/bin/python -m pyresearch.execution.pipeline oos
.venv/bin/python -m pyresearch.execution.reporting
.venv/bin/python -m unittest discover -s tests
./build/cpp/crypto_l2_tests
```

Once a frozen spec exists, `select` verifies and returns it without rerunning development
selection. A new selection requires a deliberately new experiment/spec; it must not overwrite
this experiment after validation/OOS has been opened.

## Unresolved limitations and next boundary

- The data contains eight isolated first-of-month dates, not eight continuous months. Daily risk
  and Sharpe estimates are very weak.
- A 100 ms grid cannot distinguish 25 from 50 or 100 ms execution; 250 ms maps to 300 ms.
- Top-10 displayed depth has no queue, hidden liquidity, temporary impact, or exchange transport
  model. The participation flag shows even the 1,000 USDT scenario is sometimes aggressive.
- The fee baseline is configurable and deliberately conservative as a research input. Account- or
  tier-specific fees can be substituted without changing alpha or execution logic.
- The common selected threshold gives no OBI-only/TI-only trades. A future experiment may
  predeclare model-specific development thresholds, but it must create a new execution experiment
  and keep new forward data untouched.
- The continued live collector should accumulate a substantially longer forward sample. That
  sample should be schema-validated and evaluated prospectively; it should not be repeatedly used
  to tune this frozen OOS result.

Per the milestone boundary, no maker or market-making implementation follows from this report.
