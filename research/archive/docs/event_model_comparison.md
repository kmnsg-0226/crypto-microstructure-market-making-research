# Event-time selective-maker comparison

## Outcome

No candidate survived the frozen January–May development gate. The Event Rule lost money before and after fees; the selected LightGBM and causal TCN policies rejected every quote because their predicted fill-weighted maker markout was below the frozen margin. Consequently June, July, and August were not opened, no native-forward tuning was started, and no model should be carried into native forward OOS.

This is a valid negative result. It is not evidence of a profitable HFT strategy.

## Reproducibility and experiment state

- Experiment-plan SHA-256: `98c055b7a27f68e98b15d057ca28265f5d2532e9f2d75d6fb002927877a7d767`
- Plan commit: `e4322dc`
- Deep training declaration SHA-256: `13462cf661b245da54c5651f903c99ec25d6f23c7a9f827de6ce957036649d48`
- Last committed declaration commit: `508bc31`
- Final machine report SHA-256: `4b729e446fe119dd47e735c4bdd2736913560a0bb8110dd11fdaf63bfc518db7`
- Implementation commit: not created. The commit approval call was rejected by the execution layer's usage limit; the implementation remains in the worktree.
- Large event datasets, predictions, and reports remain under ignored `data/research/tardis/` paths and were not staged.

The native Binance collector was not stopped, signalled, reconfigured, or rebuilt. Its active raw file grew from 207,501,078 to 207,554,648 bytes during a five-second read-only check on 2026-08-16.

## Existing architecture reused

The event adapter feeds the same C++ `OrderBook`, decimal tick/lot conversion, Tardis trade parser, `PassiveTradeIndex`, pessimistic visible-queue rules, and continuous inventory/PnL accounting used by the existing research pipeline. It does not implement a second queue simulator or PnL ledger.

To preserve the historical passive research source boundary, the new adapter is built as `crypto_event_l2`; the frozen `crypto_l2` sources are unchanged. The authoritative existing `TardisL2Replayer` validates each input first. The event adapter then emits grouped messages and refuses completion unless its final `OrderBook` checksum equals the authoritative replay checksum. A post-refactor May replay reproduced the earlier event export byte-for-byte (`e437bf12dc385a896c56036b10a0e4596e5e7036266cb64e45227b6970b545d5`) and ended at book checksum `0x7814251367c99ad8`.

## Dataset and ordering

Primary model input is event time, never independent price-level rows and never the 100ms research grid. All L2 rows sharing a completed vendor `local_timestamp` message are grouped before application. Cross-stream order is:

1. earlier `local_timestamp` events;
2. a completed book message;
3. trades at the exact same local timestamp in source-row order;
4. a hypothetical quote decision.

The canonical 100ms book grid is used only to look up post-fill future mids for labels. It is not a model input.

| Date | Quote-side rows | Filled | Valid 1s markouts | Book messages | Trades | Decision opportunities |
|---|---:|---:|---:|---:|---:|---:|
| 2026-01-01 | 349,424 | 27,230 | 27,230 | 1,642,731 | 1,056,983 | 174,712 |
| 2026-02-01 | 685,988 | 243,790 | 243,760 | 1,652,420 | 6,759,515 | 342,994 |
| 2026-03-01 | 663,304 | 197,902 | 197,899 | 1,661,194 | 5,961,363 | 331,652 |
| 2026-04-01 | 609,362 | 153,202 | 153,132 | 1,660,756 | 4,104,211 | 304,681 |
| 2026-05-01 | 577,858 | 98,238 | 98,105 | 1,661,061 | 3,166,686 | 288,929 |
| Total | 2,885,936 | 720,362 | 720,126 | 8,278,162 | 21,048,758 | 1,442,968 |

Every daily export was run twice and was byte-identical. Dataset-manifest SHA-256 values are recorded in the machine report.

## Exact decision and execution rule

A decision opportunity occurs after a valid BBO-changing book message or aggressive trade, subject to a fixed 100ms cooldown. Both bid (`quote_side=+1`) and ask (`quote_side=-1`) candidates see the same post-event state. Positive side-oriented values always mean favorable to that quote side; the bid/ask sign-pair audit found zero violations.

Quotes are fixed at the actual decision BBO for 1,000ms, size 0.005 BTC. Initial queue ahead is the displayed quantity at that fixed price. Opposite aggressor trades at the same price consume displayed queue before the hypothetical order; a strict trade-through fills the remaining order. Later additions stay behind the order. Cancellations or unexplained reductions never improve queue position. A replacement snapshot cancels an outstanding probe immediately; a true invalid book does not generate opportunities.

One live simulated order per side is allowed. Inventory starts at zero each disjoint day, uses 0.015 BTC soft and 0.025 BTC hard absolute limits, and lets risk-reducing quotes through even when the selector rejects. End inventory is flattened at the actual opposite BBO. Maker fee is 2bps and required liquidation taker fee is 5bps. Cashflow is actual buy/sell cashflow; spread is not charged a second time.

## Feature definitions

State features are side-oriented OBI L1/L5/L10, weighted OBI L10, weighted-mid minus mid, spread, L1/L5/L10 depth, queue ahead, and queue/L1 ratio.

For trailing 10/50/100/500/1000ms event windows, the exporter records delta OBI, OFI, aggregate add/cancel imbalance, best-level depletion/replenishment imbalance, BBO/depth-change counts, aggressive trade quantity/count imbalance, trade intensity, average size, and signed last-trade streak. Timing features record time since trade, book update, BBO change, and mid change; event arrival intensity and backward absolute mid movement cover short activity and volatility.

Adds and cancels are level-update proxies only. A positive size difference is an aggregate addition and a negative difference is an aggregate removal. Depletion/replenishment is restricted to the previous best level. The report makes no order-ID or exact FIFO claim. Fold normalization uses only train-fold finite medians and IQRs, clipped to ±10; no full-day or future normalization is used.

## Labels

- Fill: any positive pessimistic-queue fill before the 1,000ms expiry.
- Primary markout: side-oriented maker markout from the fixed quote price to the future mid 1s after the first fill.
- Diagnostics: 100ms, 500ms, and 5s after fill.
- Lookup: first 100ms grid point at or after exchange fill time, then the horizon.
- Validity: fill and future mid must be inside the same valid book segment.
- TCN markout loss: Huber loss on filled, valid examples only.

Markout decomposition tests confirm `maker markout = fill-price advantage + post-fill mid move`; spread is not double-counted.

## Chronological development split

The fixed folds were Jan–Feb→Mar, Jan–Mar→Apr, and Jan–Apr→May. Corresponding training rows/fills were 1,035,412/271,020; 1,698,716/468,922; and 2,308,078/622,124. Validation fill balances were 29.84%, 25.14%, and 17.00%.

The development gate required positive pooled gross and net PnL, at least two of three positive folds, worst-fold net PnL no worse than -$1,000, at least 500 fills, and zero inventory violations. It was not weakened after results.

## Models

### Event Rule

Six votes are side-oriented OBI L10, 100ms delta OBI, 100ms OFI, 100ms trade quantity imbalance, weighted-mid displacement, and 100ms best-level depletion imbalance. Only 3/6, 4/6, and 5/6 were tested. The robust fold objective selected 5/6.

### LightGBM

B1 is a fill classifier and B2 is a 1s conditional maker-markout Huber regressor trained only on filled examples. The decision score is `p(fill) × predicted markout ticks`. The frozen grid contained two small tree configurations, four ablations (static, static+flow, static+queue, full), and margins -1/0/+1 tick. Seeds were fixed at 20260816.

The selected economic configuration was static-only, 15 leaves, depth 5, learning rate 0.05, and -1 tick margin. Fill early stopping selected 64/51/139 trees across the three folds; the markout models used the 300-tree maximum. LightGBM has no single useful scalar parameter count because it is an ensemble; leaves, trees, and feature count are reported instead.

### Deep causal model

The TCN uses 24 compact event features, channels `[32,32]`, kernel 3, dilations `[1,2]`, dropout 0.1, and separate fill and markout heads. It has 5,506 trainable parameters. Training uses deterministic CPU PyTorch, batch 256, AdamW, at most 12 epochs, patience 2, and 250,000 evenly spaced training/validation event rows per fold.

The exact receptive field of the frozen two-layer architecture is seven events. Therefore the declared 128- and 256-history candidates are mathematically identical at the final output; their predictions and metrics were identical. This redundancy is an explicit limitation, not hidden architecture search.

## Predictive results

The best full LightGBM fill AUC rose from 0.811 (Mar) to 0.830 (Apr) and 0.855 (May); full-flow information clearly improved fill classification over static AUCs of 0.733, 0.765, and 0.779. Queue features alone added little. LightGBM markout rank correlation remained weak at roughly 0.08–0.14.

The TCN fill AUC was 0.782, 0.803, and 0.832; markout rank correlation was 0.074, 0.094, and 0.121. Its markout MAE improved relative to LightGBM on the reported folds, but its fill-weighted score still selected no quotes at the chosen margin. Predictive improvement did not translate into economic improvement.

Calibration-decile results are in `data/research/tardis/reports/event_models/lightgbm/calibration.csv`. Predictive metrics are secondary because the simulator, not AUC, determines the selection result.

## Development economics

| Policy | Gross PnL | Net PnL | Net bps | Fees | Positive days | Worst day | Fills | Max inventory | Break-even maker fee |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Neutral | -$3,579.53 | -$10,190.73 | -6.167 | $6,611.20 | 0/3 | -$4,450.14 | 95,280 | 0.019 BTC | -1.084bps |
| Existing OBI MM | -$2,977.53 | -$8,408.61 | -6.194 | $5,431.08 | 0/3 | -$3,807.56 | 78,396 | 0.019 BTC | -1.097bps |
| Event Rule 5/6 | -$1,182.56 | -$3,347.49 | -6.185 | $2,164.93 | 0/3 | -$1,711.34 | 31,474 | 0.019 BTC | -1.093bps |
| LightGBM selected | $0 | $0 | n/a | $0 | 0/3 | $0 | 0 | 0 | n/a |
| TCN selected | $0 | $0 | n/a | $0 | 0/3 | $0 | 0 | 0 | n/a |

The least restrictive economically active LightGBM candidate (flow, config 1, -1 tick) still lost gross -$13.53 and net -$35.54 on 291 fills. The active TCN -1 tick candidate lost gross -$10.64 and net -$21.41 on 154 fills. Neither was profitable before fees.

The Event Rule reduced loss and turnover relative to Neutral and static OBI, but did not create positive edge. Its pooled selected-fill mean 1s maker markout was -72.44 ticks (median -56.5). May manual audit counted 4,958 adverse versus 695 favorable selected fills with a valid 1s label.

All policies had zero inventory-limit violations. Daily Sharpe is retained only as an exploratory statistic and is economically meaningless for the zero-activity policies.

## Static-vs-event and queue ablations

Dynamic flow improved LightGBM fill prediction materially, but the conditional maker markout remained negative enough that margin 0/+1 rejected all quotes and margin -1 lost money. Queue-ahead features added little beyond static book state. Thus event information improved fill classification, but did not show incremental net maker value beyond static OBI under this queue and fee model.

## Manual audit

The deterministic May audit contains three examples for each available category: quoted, rejected, filled bid, filled ask, adverse fill, and favorable fill. Each trace includes recent vote inputs, decision score, queue ahead, fill timestamps/quantity, future mid, and 1s maker markout. LightGBM and TCN selected policies have no quoted or filled traces because they rejected every development-fold opportunity; fabricating such traces would be misleading.

## June–August and native forward

June, July, and August frozen historical evaluation was not run. All three candidates failed the predeclared development gate, so opening those months would violate the experiment plan. No historical JJA result is reported as zero, missing, or implied.

No candidate qualifies for 7/14/30-day native forward OOS. The native collector should continue unchanged for future data accumulation, but there is no frozen model worth deploying to that stream from this experiment.

## Tests

- C++ build and `crypto_l2_tests`: pass.
- New event Python tests: 10/10 pass, covering plan hash, side signs, target exclusion, gap-safe labels, causal padding, receptive-field causality, parameter count, filled-only markout loss, risk-reducing override, actual cashflow, and inventory cap.
- Full Python suite: 89 pass, 1 error.

The sole full-suite error is the historical raw `tardis_passive_probe` binary SHA receipt. Re-linking that executable in this session changed the macOS Mach-O UUID and linker adhoc signature (81 bytes) even after the frozen source files were restored. The frozen maker spec and its expected binary SHA were deliberately not rewritten. Its frozen source-bundle audit remains unchanged. This receipt issue is unresolved and should not be confused with an event-model test failure.

## Answers to the research questions

A. Event-level information improved fill AUC and reduced the Event Rule's loss relative to static OBI, but did not improve net economics to positive territory.

B. LightGBM identified fill probability better than the rule, but did not identify economically better passive fills: the selected policy placed zero quotes, and the active -1 tick alternative lost gross and net.

C. The deep model did not materially outperform LightGBM. Its fill AUC was lower; markout MAE was somewhat better, but the economic decision remained no-quote or loss-making.

D. No selected ML method produced filled quotes. Among active evaluated policies, no method had positive post-fill markout; the Event Rule's selected-fill mean was -72.44 ticks.

E. The best admissible net maker economics were zero from abstaining, which fails minimum activity. Among active strategies the TCN -1 tick diagnostic lost the least dollars, but it is not an admissible profitable strategy.

F. No method had both positive gross and positive net economics.

G. No active method was stable by day; the selected Event Rule was negative on all three validation days.

H. No model reached June–August, because none survived development.

I. Added model complexity was not economically justified.

J. No model is worth carrying unchanged into native forward OOS.

K. The strongest evidence is adverse selection plus fee burden, with weak conditional-markout prediction and monthly instability. Fill prediction itself was learnable, so “fill prediction failure” is not the primary diagnosis. This experiment alone cannot prove that single-venue Binance BTC L2 contains no useful information; it shows that this compact event set, fixed BBO quoting rule, pessimistic queue, and tested model budget did not extract a positive maker edge.

## Unresolved limitations

- Only each month's first day is represented; it is broad in regime but sparse in calendar coverage.
- Tardis normalized L2 has no order IDs; additions/cancellations are aggregate proxies.
- The queue model is deliberately pessimistic and does not model hidden queue priority.
- Forced liquidation uses BBO without depth impact, which is optimistic.
- The TCN's declared 128/256 histories exceed its seven-event receptive field.
- Model artifacts were not retained because all candidates failed development; rejected specifications and all metrics were retained.
- The historical passive binary receipt cannot be restored after nondeterministic relinking without changing the frozen evidence, which was not done.
- The requested local implementation commit remains blocked by the execution approval usage limit.
