# Native Binance USD-M L2 research corpus: native_dev_v1

First research phase built on our own sequence-verified `.chft.zst` capture rather than on Tardis
normalized data. This document reports the corpus, the QC of the raw replay, the dataset and its
definitions, the passive-quote assumptions, and baseline diagnostics.

Everything here is **development data**. No file in this corpus is out of sample, and no
strategy search, threshold optimisation, fee model or PnL grid was run.

## 1. Corpus files and hashes

Frozen in `research/specs/native_dev_v1.json` at repository commit `8f80e4b8`.

| # | File | SHA-256 | Bytes | Raw records | Snapshots | Segments |
|---|------|---------|-------|-------------|-----------|----------|
| 0 | `BTCUSDT-LONDON-20260817T210035Z.chft.zst` | `99b6d4878ef32f4a237545e4019c9062d1bdaa56e9cf064b1d22b5b6a2109541` | 13,523,656 | 64,060 | 1 | 1 |
| 1 | `BTCUSDT-LONDON-20260817T221753Z.chft.zst` | `e093130b8f6f4bad640829871ff223474dfa5d3f9d93371d2fbfe05ac35cf35e` | 100,720,915 | 446,700 | 1 | 1 |
| 2 | `BTCUSDT-LONDON-20260818T062918Z.chft.zst` | `2867d44487ec066dfb66581ea2da4faa0b5b571c7f9787f0b8b5b110085d3f25` | 1,473,358,183 | 7,184,834 | 5 | 6 |

Source `native_binance_usdm`, collector location `aws_london`, symbol `BTCUSDT`, tick size 0.10,
quantity step 0.001. Final book checksums are `0x427ab1966b6b4308`, `0x1980a6324a23dce0`,
`0xf2e1fea4c7a85f84`.

The three files are **three separate collector processes**, each opening with its own
`exchangeInfo` record and its own REST snapshot. They are replayed independently and are never
concatenated. The rotation-enabled forward file and every later AWS file are excluded.

## 2. Usable market duration

| | Seconds | Hours |
|---|---|---|
| Captured span (sum of per-file first→last receive) | 257,054.77 | 71.40 |
| Usable research time (sum of segment durations) | 257,037.61 | 71.40 |
| Excluded (reconnect and resynchronisation gaps) | 17.16 | — |

The 77.8 s collection gap between file 0 and file 1, and the 1.0 s process restart between
file 1 and file 2, are outside every file's span and are therefore not part of either number.
They are simply not research time.

Totals across the corpus: 7,695,594 raw records, 2,519,710 depth events, 5,175,850 aggressive
trades, 7 REST snapshots.

## 3. Segment boundaries

A **segment** is a maximal interval during which the depth book was synchronized *and* the
aggressive-trade socket was connected. Both conditions matter: depth and trades arrive on
separate sockets and reconnect independently, and a trade outage leaves the book intact while
silently removing the flow that every trade feature and the whole fill model depend on.

Eight segments, listed in full in `research/native_dev_v1/segments.csv`:

| Key | Start (UTC) | End (UTC) | Duration s | Rows | Close reason |
|---|---|---|---|---|---|
| 0:1 | 2026-08-17T21:00:37.557Z | 2026-08-17T22:16:35.939Z | 4,558.4 | 45,584 | depth_desync |
| 1:1 | 2026-08-17T22:17:55.168Z | 2026-08-18T06:29:17.777Z | 29,482.6 | 294,826 | depth_desync |
| 2:1 | 2026-08-18T06:29:20.861Z | 2026-08-19T05:29:21.028Z | 82,800.2 | 828,002 | depth_desync |
| 2:2 | 2026-08-19T05:29:23.645Z | 2026-08-20T01:40:00.204Z | 72,636.6 | 726,366 | trade_stream_down |
| 2:3 | 2026-08-20T01:40:02.382Z | 2026-08-20T01:42:00.140Z | 117.8 | 1,178 | depth_desync |
| 2:4 | 2026-08-20T01:42:02.867Z | 2026-08-20T06:26:00.126Z | 17,037.3 | 170,373 | depth_desync |
| 2:5 | 2026-08-20T06:26:02.387Z | 2026-08-20T10:56:00.129Z | 16,197.7 | 161,978 | depth_desync |
| 2:6 | 2026-08-20T10:56:02.690Z | 2026-08-20T20:26:09.824Z | 34,207.1 | 342,072 | depth_desync |

Segment 2:3 exists only because the aggressive-trade socket dropped at 01:40:00.204Z and
reopened 2.18 s later, while the depth socket stayed alive for another two minutes. Splitting
there costs 2.18 s of data and buys one uniform boundary rule for features, targets and fills.

The depth event that *completes* synchronization after a snapshot is deliberately excluded from
the segment's flow: its effect is already carried in the segment's opening book state, so
counting it would import pre-segment information. Across the corpus 51 depth events fall outside
any segment for this reason or because the trade stream was down.

## 4. QC result

`research/native_dev_v1/qc.json`, produced per file by `native_dataset_export` and merged.

| Check | Result |
|---|---|
| Parse failures | 0 |
| Unrecoverable sequence gaps | 0 |
| Recoverable sequence gaps | 0 |
| Crossed book after synchronization | 0 |
| Empty bid / empty ask states | 0 / 0 |
| Missing initial synchronization | 0 |
| Stale depth events (pre-snapshot, discarded by design) | 14 |

**No QC failures.** The exporter exits non-zero on any of: unparseable or corrupted raw records,
a crossed book after synchronization, a missing initial synchronization state, or an
unrecoverable gap at end of file. Bad intervals are never silently dropped — they end a segment,
are recorded in `segments.csv` with a close reason, and the excluded time shows up as the
difference between captured span and usable research time.

## 5. Dataset

- `data/research/native_dev_v1/native_features_100ms_file{0,1,2}.csv.zst` — 2,570,379 rows,
  346 columns.
- Row key `(file_index, timestamp_ns)`; segment key `(file_index, segment_id)`.
- Missing values are empty fields, never zeros.
- Byte-identical on repeated runs over identical raw input (tested).

**Storage convention.** The repository already keeps heavy derived datasets under the ignored
`data/research/` tree and small reviewable artifacts under `research/`. That convention is used
here rather than the literal layout in the task: the dataset lives in `data/research/native_dev_v1/`
(711 MB compressed) and every artifact that belongs in review — `qc.json`, `dataset_schema.json`,
`segments.csv`, `feature_summary.csv`, `target_summary.csv`, `passive_summary.csv`,
`information_coefficients.csv`, `bucket_study.csv` — lives in `research/native_dev_v1/`. One
dataset file per raw capture, because the three captures are independent processes.

Row counts per file: 45,584 / 294,826 / 2,229,969.

## 6. Feature definitions

Prices are exchange ticks, quantities exchange steps, timestamps nanoseconds on the local
receive clock. The full column list is in `research/native_dev_v1/dataset_schema.json`.

**Grid.** 100 ms, aligned to absolute multiples of 100 ms since the Unix epoch. A sample at time
`t` reflects every raw record with a receive timestamp `<= t`, and every trailing window covers
`(t - w, t]` intersected with the segment.

`source`, `collector_location`, `symbol` and the instrument increments are constant across the
whole corpus, so they are recorded once in `dataset_schema.json` and `native_dev_v1.json` rather
than repeated on 2.57 M rows.

**Metadata** — `timestamp_ns`, `exchange_timestamp_ms`, `file_index`, `segment_id`,
`sequence_update_id`, `valid_book`, `segment_age_ms`, `window_warm` (1 once the segment is at
least 5 s old, so partially-covered windows are identifiable).

**Book state** — `spread_ticks`, `mid_ticks`, `microprice_ticks` and
`microprice_minus_mid_ticks`; `bid_px_1..10`, `bid_qty_1..10`, `ask_px_1..10`, `ask_qty_1..10`;
depth totals at L1/L5/L10 per side; `{side}_concentration_l5` and `_l10` (best-level quantity
over the level total); `{side}_dispersion_ticks_l10` (quantity-weighted tick distance of the
visible book from its own best price).

**State imbalance** — `obi_l1`, `obi_l5`, `obi_l10`, plus exponentially distance-weighted
`weighted_obi_l5` and `weighted_obi_l10` (λ = 0.5).

**Per-level book flow**, for each trailing window in {100, 250, 500, 1000, 5000} ms —
`bid_depth_add_l1..10`, `bid_depth_remove_l1..10`, `ask_depth_add_l1..10`,
`ask_depth_remove_l1..10`. A change is attributed to the rank the price held in the **pre-event**
book, which is the only ranking an observer could have known before the update arrived.

The names are deliberately neutral. A negative displayed-quantity change on an aggregated L2
feed can be an execution, a cancellation, or both, and the `@depth@100ms` stream nets everything
that happened inside a 100 ms bucket. Nothing in this dataset calls a reduction a cancellation.
Where the trade stream does provide causal evidence, it is exposed separately as trade flow and
as `{side}_fill_via_trade_through`.

**Weighted multi-level pressure**, per window — `depth_flow_pressure_l5` and
`depth_flow_pressure_l10`: exponentially distance-weighted net depth flow (bid adds minus bid
removes minus ask adds plus ask removes) divided by the weighted gross flow, so it stays in
[-1, 1] regardless of activity. Empty when nothing moved in the window.

**Trade flow**, per window — `buy_qty` (buyer-initiated), `sell_qty` (seller-initiated),
`signed_volume`, `trade_imbalance` (empty when the window holds no trades), `trade_count`,
`depth_event_count`.

## 7. Target definitions

All targets are strictly within segment. A horizon that would cross a segment end is left
**empty**, never zero.

**Pure price**
- `next_mid_move_dir` (+1/-1) and `time_to_next_mid_move_ms` — direction and latency of the first
  mid change, observed for up to 30 s.
- `markout_{100,250,500,1000,5000}ms_ticks` — `mid(t + h) - mid(t)` in ticks, where `mid(t + h)`
  is the mid implied by every record with a receive timestamp `<= t + h`.

**Passive fill** (per side, for a hypothetical order at the best price on that side)
- `{side}_eligible` — the denominator; 1 on every emitted row.
- `{side}_quote_px_ticks`, `{side}_queue_ahead_lots`.
- `{side}_fill_{500,1000,5000}ms` — 1 if the order is *fully* filled inside the horizon.
- `{side}_filled_lots_30000ms` — partial fill quantity at the end of the observation window.
- `{side}_fill_before_observed_mid_adverse` — 1 if the full fill happens before the mid moves one full tick
  against the quote, 0 if the adverse move comes first, **empty if neither happens within 30 s**.
- `{side}_time_to_fill_ms`, `{side}_fill_via_trade_through`, `{side}_fill_mid_ticks`.

**Post-fill adverse selection**
- `{side}_postfill_markout_{100,500,1000,5000}ms_ticks` — signed edge against the quote price:
  `mid(T_fill + h) - quote` for a bid, `quote - mid(T_fill + h)` for an ask.

Because every row carries `{side}_eligible` alongside the outcome columns, the denominator of
eligible orders always survives into the analysis. `research/native_dev_v1/passive_summary.csv`
names the exact population behind every rate and every conditional mean.

## 8. Passive queue assumptions

Deliberately conservative, and matching the assumptions already frozen for earlier maker work:

- The order sits at the best price on its side at the decision instant, 5 lots (0.005 BTC).
- **Initial queue ahead is the entire displayed quantity at that price.** No claim is made about
  true queue position; aggregated Binance L2 cannot support one.
- Later additions at the quote price are assumed to queue *behind* the hypothetical order.
- **Displayed-quantity decreases never advance the queue.** Only aggressive prints at exactly the
  quote price consume it.
- A print beyond the quote price implies everything resting there traded away first, so the order
  is treated as fully filled (`fill_via_trade_through = 1`).
- A print stamped at the decision instant itself is part of that row's trailing features and is
  **not** allowed to fill the order it informed. Fills require a strictly later print.
- The race is observed for 30 s. Undecided outcomes are censored, never recorded as zeros.

## 9. Baseline diagnostics

### Book state

| Statistic | Value |
|---|---|
| Spread = 1 tick | 99.816 % of rows |
| Spread > 10 ticks | 0.061 % of rows |
| Median L1 depth | 7,834 lots bid / 7,926 lots ask (≈ 7.9 BTC) |
| Median L10 depth | 9,012 / 9,130 lots |
| Median `bid_concentration_l5` | 0.981 |
| `obi_l1` | mean −0.008, p1 −0.987, p99 +0.985 |
| Mid range | 639,988.5 → 729,400.5 ticks ($63,999 → $72,940) |

### Targets

| Target | Observed | Mean | Median | p1 | p99 |
|---|---|---|---|---|---|
| `markout_100ms_ticks` | 2,570,358 | +0.033 | 0 | −42 | +42 |
| `markout_500ms_ticks` | 2,570,339 | +0.165 | 0 | −112 | +115 |
| `markout_1000ms_ticks` | 2,570,299 | +0.331 | 0 | −160 | +167 |
| `markout_5000ms_ticks` | 2,569,979 | +1.652 | 0 | −362 | +374 |

94.3 % of 100 ms markouts are **exactly zero**. Trimmed 1 s-sampled realized volatility is 12 %
(files 0 and 1) and 40 % (file 2) annualized — entirely ordinary; the raw standard deviations
above are inflated by a small number of violent bursts, not by a broken book.

### Passive outcomes (`passive_summary.csv`)

| Statistic | Bid | Ask | Denominator |
|---|---|---|---|
| Full fill within 30 s | 62.57 % | 61.56 % | 2,570,379 eligible |
| `fill_5000ms` | 34.16 % | 33.81 % | opportunities with the full horizon in-segment |
| `fill_1000ms` | 14.59 % | 14.27 % | as above |
| `fill_500ms` | 9.55 % | 9.31 % | as above |
| `fill_before_observed_mid_adverse` | 85.59 % | 86.00 % | 1,608,906 / 1,582,753 resolved races |
| `fill_via_trade_through` | 78.75 % | 78.12 % | 1,608,397 / 1,582,363 full fills |
| Post-fill markout +100 ms | −32.46 | −33.68 | filled orders |
| Post-fill markout +1 s | −53.32 | −56.44 | filled orders |
| Post-fill markout +5 s | −61.47 | −67.95 | filled orders |

Roughly 37 % of eligible opportunities never fill inside 30 s and are excluded from the markout
means as *unfilled*, not as missing. The counts above make that explicit.

### Univariate information coefficients

Against `markout_1000ms_ticks` over 2,570,299 observations
(`information_coefficients.csv`, `bucket_study.csv`):

| Signal | Pearson | Spearman |
|---|---|---|
| `obi_l1` | 0.158 | 0.317 |
| `weighted_obi_l5` | 0.159 | 0.316 |
| `obi_l10` | 0.159 | 0.311 |
| `microprice_minus_mid_ticks` | 0.035 | 0.317 |
| `depth_imbalance_l10_lots` | 0.060 | 0.255 |
| `net_depth_flow_l1_500ms` | 0.094 | 0.188 |
| `depth_flow_pressure_l10_1000ms` | 0.077 | 0.145 |
| `signed_volume_1000ms` | 0.035 | 0.143 |

Decile bucket means are cleanly monotone. `obi_l1` deciles run −16.85, −8.62, −4.93, −2.39,
−0.39, +0.77, +2.75, +5.64, +9.47, +17.86 ticks; `net_depth_flow_l1_500ms` deciles run −12.97 …
+14.46 ticks.

**These are diagnostics, not alpha.** Spearman is computed against a target that is 94 % ties, so
it largely measures which side breaks; nothing here has been costed, and no threshold has been
fitted. A 1-tick spread on a $68,000 instrument makes the relationship between queue imbalance
and the direction of the next book flip nearly mechanical.

## 10. What is different from the earlier Tardis work

1. **The best price is nearly static, then jumps.** Only 1.1–6.4 % of consecutive 100 ms samples
   show any mid change, yet the mid moves in steps of 8–25 ticks when it does move. A 100 ms
   grid over BTCUSDT is mostly sampling a stationary quote. Any model trained on 100 ms markouts
   is really being trained on a rare-event classifier.

2. **The fill instant is stale, and by a lot.** At the moment a hypothetical bid fills, the
   reconstructed mid still sits **+0.5 ticks above the quote** (median). One hundred milliseconds
   later it is **26.5 ticks below** it. The aggressive-trade socket reports the sweep before the
   100 ms-batched depth socket reflects it, so `fill_mid_ticks` is a stale price by up to one
   depth batch. A markout measured against the mid at fill time would look almost free. This
   effect is invisible in Tardis normalized data, which carries its own timestamping, and it is
   the single most important caution for the modelling phase. Both `fill_mid_ticks` and the
   quote-relative markouts are exported so the discrepancy stays visible.

3. **Fills are mostly annihilations, not queue progress.** 78 % of full fills arrive as a
   trade-through — the level was swept, not worked through. Even the 22 % that are consumed at
   the quote price carry a median −8.5 tick markout at +100 ms. Sitting at the back of an
   ~8 BTC queue, the conservative model only ever fills when the price is leaving.

4. **The two sockets fail independently.** The trade stream dropped once while the book stayed
   perfectly synchronized. A pipeline keyed only on depth synchronization would have carried
   2.18 s of silently trade-free data straight into the fill model. Tardis delivers one merged,
   pre-cleaned stream and hides this failure mode entirely.

5. **Multiple grid points land in a single event gap.** With ~30 records per second and a 100 ms
   grid, several samples are frequently emitted from one event callback. Handled naively, the
   earlier samples get marked out against a *later* book state. This was a real bug found by the
   tests here and is now covered by a regression test; it is a trap specific to event-driven
   native replay that grid-native vendor data does not present.

6. **Native captures carry information Tardis discards** and this pipeline now uses it: the REST
   snapshot boundaries that define segments, per-socket connection and disconnect reasons,
   `pu`-chained sequence validation, both the local receive clock and the exchange clock, and the
   final book checksum per file.

## 11. Files changed

**New**
- `cpp/research/native_dataset_exporter.{hpp,cpp}` — the causal 100 ms decision-dataset exporter.
- `cpp/app/native_dataset_main.cpp` — `native_dataset_export` CLI, one raw file per run.
- `native_research/{__init__,corpus,diagnostics,pipeline}.py` — corpus freeze, orchestration,
  baseline diagnostics.
- `tests/test_native_research.py` — 15 boundary, causality and determinism tests.
- `research/specs/native_dev_v1.json` — frozen corpus definition.
- `research/native_dev_v1/` — `qc.json`, `qc_file{0,1,2}.json`, `dataset_schema.json`,
  `segments.csv`, `feature_summary.csv`, `target_summary.csv`, `passive_summary.csv`,
  `information_coefficients.csv`, `bucket_study.csv`.
- `docs/native_dev_v1_corpus.md` — this report.

**Modified**
- `cpp/replay/event_replayer.{hpp,cpp}` — added an optional read-only `ReplayObserver` and a
  `snapshots` counter. The observer never mutates the book or the synchronizer, so replay
  summaries are identical with and without one attached; this is asserted by test.
- `cpp/CMakeLists.txt` — new source file and the `native_dataset_export` target.
- `cpp/tests/test_main.cpp` — three native dataset test groups.

**Reuse audit.** `microstructure_features` is used unchanged for OBI, weighted OBI, microprice
and depth totals — its assumptions hold on native raw. `research_exporter` (the legacy 100 ms
schema) is left untouched and now serves as an independent cross-check: its mid series matches
the native dataset on all 45,584 shared rows of file 0. `event_dataset_exporter` and its
`TardisEventObserver` base are Tardis-specific (they consume a whole-file trade table and a
normalized event replayer) and were **not** reused. `passive_queue.PassiveTradeIndex` requires
every trade up front and is typed on `historical::TardisTrade`, so its *rules* were reimplemented
as a streaming simulator; a test asserts the two agree on fill state, fill time and
trade-through flag for the same prints.

## 12. Test results

**C++** — `ctest` 6/6 pass, including `crypto_l2_tests` (16 groups, all pass). New groups:

- `native_dataset_segments_and_boundaries` — a synthetic capture with a trade-stream outage, a
  depth reconnect with replacement snapshot, and a print stamped exactly on a decision instant.
  Asserts 3 segments with the right close reasons; every row inside its own segment on the
  absolute grid; the first row of a segment sees zero flow from before it; no target populated on
  the last row of any segment; per-level add and remove reconstructed causally and kept apart; a
  same-instant print visible as a feature but unable to fill the order it informed; and grid
  points emitted in one batch marked out against the state that actually held at each horizon.
- `native_dataset_determinism_and_replay_invariance` — two runs byte-identical, and the bare
  replay summary equal to the summary produced with the exporter attached.
- `native_queue_matches_frozen_passive_index` — the streaming fill simulator agrees with
  `PassiveTradeIndex` across three placement times.

**Python** — `python -m unittest discover -s tests`: 105 tests, 104 pass, 1 pre-existing error.

`tests/test_native_research.py` (15 tests) passes in full against the real corpus: corpus hashes,
QC without failures, every row inside its segment, a regular grid, no rolling feature crossing a
boundary, nested trailing windows, no target crossing a boundary, markouts equal to the
independently sampled grid mid across all 2.57 M rows, passive outcomes censored rather than
assumed, queue simulation using only placement-time state, reconnect regions excluded, schema
matching the exported header, replay invariance, byte-identical re-export, and diagnostics
always reporting their population.

### Pre-existing failure

`test_passive_pipeline.test_frozen_source_and_binary_gate` fails with
`post-freeze maker input changed: passive_binary_sha256`. This gate pins the SHA-256 of the built
`build/cpp/tardis_passive_probe` binary as it stood when the maker methodology was frozen at
commit `628618b`. **It cannot pass at HEAD independently of this work**: three commits between
that freeze and HEAD (`5e7bc22`, `dca354f`, `8f80e4b`) already modified `passive_queue.cpp`,
`tardis_passive_main.cpp`, `event_replayer.cpp` and `raw_event_writer.cpp`, all of which link
into that binary. Verified by building `tardis_passive_probe` from an unmodified HEAD worktree:

```
frozen spec  : c93fb9b2f46786370bfd9bd6b1960f9be31ef89cd5285c5ca8aa0ca73b01dee5
HEAD rebuild : f3ab5c8da83cc3ca5ecde62e79a0cc69ac5811ea17a970afdd676fa2468a7ca1
```

The binary sitting in `build/cpp` before this session was a stale artifact from around the freeze
commit; rebuilding for this work is what surfaced the drift. The companion
`maker_source_bundle_sha256` gate still passes, so the maker research *sources* are unchanged.
Re-freezing the hash would destroy the audit trail and is left as a decision for whoever owns
that experiment.

## Amended by phase 2

The phase 2 predictive decomposition extended the exporter in place: `{side}_fill_before_adverse`
was renamed `{side}_fill_before_observed_mid_adverse` to make its cross-feed nature explicit,
`{side}_time_to_{mid_adverse,quote_gone,best_adverse}_ms` were added so any race variant can be
rebuilt without re-running the replay, and `time_since_*`, `bbo_change_count_*` and
`backward_mid_abs_change_ticks_*` were added as activity features. The dataset is now 366
columns. Nothing above changed in value; see `docs/native_predictive_v1.md`.

## Reproducing

```
cmake -S cpp -B build/cpp && cmake --build build/cpp -j8
python -m pyresearch.native.core.pipeline all     # export, qc, schema, freeze, diagnose
python -m unittest tests.test_native_research
```

## Not done, by design

No LightGBM, no deep learning, no profitability search, no OBI threshold or holding-period grid,
no fee model, no forward AWS files. The next phase estimates `P(next move | X)`, `P(fill | X)` and
`E(post-fill markout | fill, X)` separately, and only then combines them into expected value.
