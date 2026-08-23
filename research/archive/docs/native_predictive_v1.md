# Native predictive decomposition v1

Phase 2 on the frozen `native_dev_v1` corpus. Three questions, answered separately and
deliberately not combined:

- **A** — what does the book predict about the next price move?
- **B** — what does it predict about passive fill probability and fill mechanism?
- **C** — conditional on being filled, what predicts adverse selection?

No maker strategy search, no threshold optimisation, no fees, no holding periods, no PnL.

Everything here is **development data**. All model numbers are **blocked out-of-fold development
estimates**, never out-of-sample. The rotation-enabled AWS capture, every later AWS capture and
the Tardis June–August holdout were not read, loaded or summarised at any point.

Pre-registration: `research/native_predictive_v1/methodology.json`, written before any model was
fitted.

---

## 1. How large is the trade/depth receive-time lag around quote sweeps?

Large enough to dominate any sub-100 ms race. Event study over **5,174,906 aggressive prints**
(`cross_stream_timing.csv`, `cross_stream_timing_summary.csv`), measured on the local receive
clock, with no timestamp adjusted or reconciled anywhere.

For each print, the latency to the first subsequent depth event that reduces the displayed
quantity at the touched quote, removes it, moves the best price, or moves the mid adversely:

| Reaction | p10 | p25 | **p50** | p75 | p90 | p99 | <10 ms | <50 ms | <100 ms | >100 ms | never in 5 s |
|---|---|---|---|---|---|---|---|---|---|---|---|
| displayed quantity reduced | 12.4 | 30.6 | **60.4** | 92.3 | 268.1 | 2503 | 7.9 % | 40.9 % | 79.5 % | 19.5 % | 1.0 % |
| quote removed | 13.4 | 32.7 | **64.7** | 102.7 | 1076 | 4309 | 6.2 % | 32.5 % | 62.2 % | 21.9 % | 15.9 % |
| best price changed | 13.4 | 32.8 | **65.0** | 102.1 | 934 | 4110 | 6.9 % | 36.0 % | 69.2 % | 24.3 % | 6.5 % |
| mid moved adversely | 13.4 | 32.7 | **64.8** | 102.9 | 1104 | 4316 | 6.2 % | 32.2 % | 61.7 % | 21.7 % | 16.6 % |

All latencies in milliseconds. The exchange-clock version of the same lag (median 89 ms) is in
the artifact for completeness, but the two sockets stamp their own events and the clocks are not
assumed comparable.

**Only 6–8 % of prints are acknowledged by the depth stream within 10 ms.** The median is
~60 ms, which is what a 100 ms-batched diff stream produces: the acknowledgement lands in the
next batch, uniformly 0–100 ms later.

Split by where the print landed relative to the reconstructed touch:

| Passive side | Category | Prints | Share | Median quantity reaction | Quote removed within 5 s | Median trade size | Median quote size |
|---|---|---|---|---|---|---|---|
| bid | `through_quote` | 1,416,379 | 55.5 % | 47.1 ms | 98.7 % | 2 lots | 986 lots |
| bid | `at_quote` | 667,791 | 26.2 % | 92.5 ms | 52.5 % | 30 lots | 5,222 lots |
| bid | `outside_quote` | 468,386 | 18.3 % | 79.6 ms | 84.7 % | 2 lots | 901 lots |
| ask | `through_quote` | 1,476,082 | 56.3 % | 48.0 ms | 98.2 % | 2 lots | 1,092 lots |
| ask | `at_quote` | 652,410 | 24.9 % | 94.1 ms | 51.7 % | 30 lots | 5,227 lots |
| ask | `outside_quote` | 493,858 | 18.8 % | 78.2 ms | 85.2 % | 2 lots | 815 lots |

Three things stand out.

1. For `through_quote` prints — the majority — **every** reaction metric has essentially the
   same distribution: quantity reduction 47.5 ms, quote removal 48.4 ms, best-price change
   47.6 ms, adverse mid move 48.5 ms (medians). The book does not react in stages; the whole
   consequence of the sweep arrives at once in the next depth batch.
2. **18.6 % of all prints are `outside_quote`**: the trade printed on the far side of the
   reconstructed touch, so the depth book was already stale at the moment of the trade. This is
   not a reconstruction error — the sequence checks are clean, with zero gaps and zero crossed
   books — it is the feed being 100 ms behind.
3. `at_quote` prints are small nibbles (median 30 lots) at a deep quote (median ~5,200 lots)
   and the quote survives half the time. `through_quote` prints are the 2-lot tails of sweeps
   that annihilate a thinner (median ~1,000 lot) quote.

---

## 2. Is the existing `fill_before_adverse` statistic contaminated by feed ordering?

**Yes, almost entirely.** This is the most consequential result of the phase.

The phase 1 target was renamed `fill_before_observed_mid_adverse` to make the cross-feed nature
explicit, and the exporter now emits the raw latency to each competing event
(`{side}_time_to_fill_ms`, `_time_to_mid_adverse_ms`, `_time_to_quote_gone_ms`,
`_time_to_best_adverse_ms`) so any race variant can be rebuilt without re-running the replay.

The race was then re-run with a **handicap applied to the fill**: the fill only counts as winning
if it precedes the competing observation by at least Δ. No timestamp is altered; the handicap
asks how much of the result survives if the trade socket is assumed to lead the depth socket by
Δ. (`race_handicap.csv`)

| Fill handicap | `fill_before_observed_mid_adverse` | `fill_before_quote_disappears` | `fill_before_depth_best_moves_adverse` |
|---|---|---|---|
| 0 ms | **85.4 % / 85.8 %** | 83.8 % / 84.4 % | 85.3 % / 85.7 % |
| 25 ms | 65.5 % / 65.5 % | 64.2 % / 64.4 % | 65.4 % / 65.4 % |
| 50 ms | 45.0 % / 44.4 % | 44.1 % / 43.6 % | 45.0 % / 44.3 % |
| **100 ms** | **10.7 % / 10.6 %** | 10.3 % / 10.3 % | 10.6 % / 10.6 % |
| 200 ms | 8.8 % / 8.9 % | 8.5 % / 8.6 % | 8.8 % / 8.9 % |
| 500 ms | 8.1 % / 8.2 % | 7.7 % / 7.9 % | 8.0 % / 8.1 % |

Rates are bid / ask, over 1.58–1.62 M resolved races per side. The measured median depth
reaction is ~48–65 ms, so at a handicap equal to the observed feed lag the statistic has already
lost half its value, and by 100 ms it has collapsed from 85 % to 11 %. The residual ~8 % is the
part where the fill genuinely precedes the book's adverse move by more than half a second.

The three alternative race definitions are **not more robust**. They track each other to within
a percentage point at every handicap, because they all race the same batched depth stream. The
problem is the feed asymmetry, not the choice of book event. Alternatives were still added and
are reported, but none of them earns promotion to a principal target.

**Consequence, applied throughout this phase:** no fill-versus-book race is used as an economic
target. The principal adverse-selection outcome is the **future quote-relative markout after
fill**, measured over 100 ms to 5 s — horizons an order of magnitude longer than the feed lag,
where a 60 ms observation offset is a second-order effect rather than the entire signal.

### Exact definitions and stream attribution

| Target | Fill event from | Competing event from | Definition |
|---|---|---|---|
| `fill_before_observed_mid_adverse` | aggTrade | @depth@100ms | full fill precedes a ≥1 tick adverse mid move |
| `fill_before_quote_disappears` | aggTrade | @depth@100ms | full fill precedes displayed quantity at the quote reaching zero |
| `fill_before_depth_best_moves_adverse` | aggTrade | @depth@100ms | full fill precedes the touch moving past the quote price |
| `postfill_markout_{h}_ticks` | aggTrade | @depth@100ms | signed mid displacement from the quote price, h after the fill |

Every one of these is cross-feed. The markout is the only one whose horizon dwarfs the lag.

Observation window 30 s for all races; unresolved outcomes are **censored, never coded 0**.
Denominators, censoring counts and bid/ask rates are in `race_handicap.csv` and
`fill_population.csv`.

---

## 3. Fill populations and mechanism

`fill_population.csv`, descriptive and in-sample:

| Side | Statistic | Eligible | Evaluable | Censored | Positives | Rate |
|---|---|---|---|---|---|---|
| bid | `fill_500ms` | 2,570,379 | 2,570,339 | 40 | 245,477 | 9.55 % |
| bid | `fill_1000ms` | 2,570,379 | 2,570,299 | 80 | 374,935 | 14.59 % |
| bid | `fill_5000ms` | 2,570,379 | 2,569,979 | 400 | 877,914 | 34.16 % |
| bid | full fill within 30 s | 2,570,379 | 2,570,379 | 0 | 1,608,397 | 62.57 % |
| ask | `fill_500ms` | 2,570,379 | 2,570,339 | 40 | 239,172 | 9.31 % |
| ask | `fill_1000ms` | 2,570,379 | 2,570,299 | 80 | 366,870 | 14.27 % |
| ask | `fill_5000ms` | 2,570,379 | 2,569,979 | 400 | 868,897 | 33.81 % |
| ask | full fill within 30 s | 2,570,379 | 2,570,379 | 0 | 1,582,363 | 61.56 % |

Mechanism split, and the post-fill markout conditional on it
(`mechanism_markout_summary.csv`):

| Side | Mechanism | Fills | Share | Median time to fill | Mean markout 1 s | Median markout 1 s | Favourable at 1 s | Mean markout 5 s |
|---|---|---|---|---|---|---|---|---|
| bid | all | 1,608,397 | 100 % | 4114 ms | −53.3 | −39.5 | 12.4 % | −61.5 |
| bid | trade-through | 1,266,631 | 78.8 % | 4346 ms | −56.8 | −41.5 | 6.2 % | −65.3 |
| bid | at quote | 341,766 | 21.2 % | 3400 ms | −40.3 | −20.5 | 35.7 % | −47.3 |
| ask | all | 1,582,363 | 100 % | 4066 ms | −56.4 | −40.5 | 12.4 % | −67.9 |
| ask | trade-through | 1,236,106 | 78.1 % | 4217 ms | −60.5 | −44.5 | 6.0 % | −73.0 |
| ask | at quote | 346,257 | 21.9 % | 3586 ms | −41.9 | −21.5 | 35.3 % | −49.8 |

Markouts in ticks, signed so positive favours the resting order. **The two mechanisms are not
economically the same event.** A trade-through fill is favourable 6 % of the time; an at-quote
fill 35 %. Both are strongly negative on average. Bid and ask agree to within a few ticks
throughout, which is the first piece of evidence for side symmetry.

---

## 4. Validation design

Expanding-window, chronologically blocked out-of-fold. `folds.csv`:

- 12 contiguous wall-clock blocks over the 71.4 h corpus, ~5.95 h each.
- Validation on blocks 2–11: **10 forward folds**, each ~214,000 decision rows.
- Training grows from 427,131 to 2,355,502 rows.
- **60 s purge** between the end of training and the start of validation, longer than the
  longest target chain (30 s observation window + 5 s post-fill markout = 35 s).
- No random split, no shuffled K-fold, no row-level cross validation anywhere.
- Segment 2:3 (117.8 s) is never a standalone validation fold; it sits inside a 5.95 h block.

| Fold | Train rows | Validation rows | Train ends (UTC) | Validation (UTC) |
|---|---|---|---|---|
| 0 | 427,131 | 214,277 | 2026-08-18T08:53:52.966Z | 08:54:52.966Z → 14:52:00.649Z |
| 1 | 641,408 | 214,277 | 2026-08-18T14:51:00.649Z | 14:52:00.649Z → 20:49:08.333Z |
| 2 | 855,685 | 214,277 | 2026-08-18T20:48:08.333Z | 20:49:08.333Z → 2026-08-19T02:46:16.016Z |
| 3 | 1,069,962 | 214,250 | 2026-08-19T02:45:16.016Z | 02:46:16.016Z → 08:43:23.700Z |
| 4 | 1,284,213 | 214,277 | 2026-08-19T08:42:23.700Z | 08:43:23.700Z → 14:40:31.383Z |
| 5 | 1,498,489 | 214,277 | 2026-08-19T14:39:31.383Z | 14:40:31.383Z → 20:37:39.066Z |
| 6 | 1,712,766 | 214,229 | 2026-08-19T20:36:39.066Z | 20:37:39.066Z → 2026-08-20T02:34:46.750Z |
| 7 | 1,926,995 | 214,255 | 2026-08-20T02:33:46.750Z | 02:34:46.750Z → 08:31:54.433Z |
| 8 | 2,141,250 | 214,252 | 2026-08-20T08:30:54.433Z | 08:31:54.433Z → 14:29:02.116Z |
| 9 | 2,355,502 | 214,276 | 2026-08-20T14:28:02.116Z | 14:29:02.116Z → 20:26:09.800Z |

Files 0 and 1 (Aug 17 21:00 → Aug 18 06:29) are entirely inside fold 0's training window.

Reported scores are aggregated at four levels — rows, 30-minute blocks, folds and segments — and
intervals come from a **moving block bootstrap over 30-minute blocks** (500 draws, seed 0).
No iid standard error is reported anywhere.

---

## 5. Model A — price formation

Two complementary problems, because a fixed-horizon 100 ms return is 94 % zero and would make
"predict nothing" the winning strategy.

### A1 — direction of the next mid move

Population: 1,890,637 scored out-of-fold rows of 2,142,647 (11.7 % censored — no mid move
observed inside the 30 s window or the segment ended first). Base rate 48.9 % up.

| Model | ROC AUC | Log loss | Brier | Block mean | Block median | Worst block | Blocks with AUC > 0.5 | Bootstrap 5–95 % |
|---|---|---|---|---|---|---|---|---|
| naive | 0.500 per fold | 0.6931 | 0.2500 | 0.5017 | 0.5000 | 0.4013 | 4/120 † | 0.4987–0.5055 |
| **logistic** | **0.7412** | 0.5998 | 0.2065 | 0.7399 | 0.7390 | 0.5948 | **120/120** | 0.7345–0.7453 |
| LightGBM | 0.7265 | 0.6166 | 0.2133 | 0.7261 | 0.7319 | 0.5820 | 120/120 | 0.7200–0.7322 |

† A constant predictor scores exactly 0.5 inside a fold. The four exceptions are 30-minute
blocks that straddle a fold boundary and therefore contain two different constants.

Per fold the logistic AUC runs 0.706–0.770 with no fold below 0.70. This is the strongest and
most stable result in the phase.

Drivers (LightGBM gain share / logistic coefficient sign, stable in 10/10 folds):
`obi_l1` 15.2 %, `weighted_obi_l5` 12.3 %, `microprice_minus_mid_ticks` 11.7 %,
`segment_age_ms` 11.4 %, `time_since_bbo_change_ms` 4.0 %. The logistic model puts
+1.02 on `log_bid_depth_l1` and −0.74 on `log_ask_depth_l1`: queue imbalance at the touch, and
essentially nothing else.

`conditional_behaviour.csv` shows how mechanical this is. Across `obi_l1` deciles, P(next move
up) runs **15.8 % → 84.3 %**, monotone in every decile.

### A2 — does a move happen soon?

| Horizon | Base rate | naive (per fold) | logistic | LightGBM | LightGBM block mean | Worst block |
|---|---|---|---|---|---|---|
| 250 ms | 12.05 % | 0.500 | 0.8723 | **0.8857** | 0.8229 | 0.7637 |
| 500 ms | 18.78 % | 0.500 | 0.8507 | **0.8572** | 0.7705 | 0.7001 |
| 1 s | 28.41 % | 0.500 | 0.8397 | 0.8396 | 0.7280 | 0.6475 |
| 5 s | 59.57 % | 0.500 | **0.8594** | 0.8426 | 0.6780 | 0.4692 |

Timing is far more predictable than direction. It is also almost entirely an activity forecast:
`trade_count_100ms` alone carries 26 % of the gain at the 250 ms horizon and the five
`trade_count` windows together carry 65 % (58 % at the 1 s horizon). Depth at the touch adds
9–11 %.

`time_since_mid_change_ms` deciles show the same thing from the other side: P(move within 1 s)
falls **67.9 % → 3.1 %** from the freshest to the stalest decile, with P(next move up) flat at
≈0.49 throughout. Waiting time predicts *when*, not *which way*.

A note on the pooled naive AUC. Pooling ten per-fold constants produces an apparent AUC of
0.68–0.70 purely because the base rate drifts between blocks. Per fold and per block the naive
AUC is 0.500, as it must be. This is exactly why the dependence-aware columns, not the pooled
ones, carry the interpretation.

---

## 6. Model B — passive fill

Side-normalised pooled frame: 4,285,294 out-of-fold opportunities (2,142,647 decision rows × 2
sides). `side` is metadata, never a feature.

| Horizon | Base rate | naive | logistic | LightGBM | LightGBM block mean | Worst block | Bootstrap 5–95 % |
|---|---|---|---|---|---|---|---|
| 500 ms | 10.73 % | 0.500 | 0.8773 | **0.8828** | 0.8402 | 0.7718 | 0.8350–0.8463 |
| 1 s | 16.30 % | 0.500 | 0.8587 | **0.8605** | 0.8091 | 0.7412 | 0.8031–0.8158 |
| 5 s | 37.20 % | 0.500 | **0.8273** | 0.8213 | 0.7483 | 0.6770 | 0.7418–0.7551 |

Fill is the most predictable of the three components after move timing, and it is stable:
120/120 blocks above 0.5 at every horizon, worst single block 0.677.

One feature dominates: **`log_own_depth_l1` carries 32–35 % of the gain** at every horizon,
followed by the `trade_count` windows (19–32 % in total, shifting from the 100–500 ms windows at
the 500 ms horizon to the 5 s window at the 5 s horizon) and `log_queue_ahead_lots`.
The logistic coefficients agree — own touch depth and queue ahead negative, `bbo_change_count`
and `trade_count` positive. In plain terms: a thin own quote in a busy book fills; a fat quiet
quote does not. That is close to a mechanical statement, which is why the AUC is high.

Calibration is good and monotone across all ten deciles (predicted 0.003 → 0.664 against
realised 0.005 → 0.672 at the 1 s horizon).

### Fill mechanism

`P(trade-through | fill)`, trained on filled opportunities only, predicted everywhere:

| Model | ROC AUC | Base rate | Block mean | Worst block |
|---|---|---|---|---|
| naive | 0.500 per fold | 78.9 % | 0.5003 | 0.4630 |
| logistic | 0.7852 | 78.9 % | 0.7852 | 0.3843 |
| **LightGBM** | **0.7903** | 78.9 % | 0.7925 | 0.5339 |

**Yes — the two mechanisms are separately predictable.** Calibration runs 0.37 → 0.99 predicted
against 0.41 → 0.99 realised. `log_own_depth_l1` (25 %) and `signed_obi_l1` (11 %) drive it: a
thin own quote is annihilated, a thick one gets worked. Across `obi_l1` deciles the bid
trade-through rate runs **50.9 % → 98.4 %**.

### Side symmetry

| Problem | Pooled | Bid only | Ask only |
|---|---|---|---|
| `fill_1000ms` AUC (LightGBM) | 0.8605 | 0.8617 | 0.8562 |
| `markout_1000ms` Spearman (LightGBM) | 0.1682 | 0.1332 | 0.1771 |
| `markout_1000ms` Spearman (logistic/ridge) | 0.1805 | 0.1673 | 0.1756 |

Pooling costs nothing and on the markout problem beats the bid-only model, which is what a
symmetric process plus twice the training data should look like. The descriptive statistics
agree: bid and ask fill rates differ by 1.0 point and mechanism-conditional markouts by ~2
ticks. **Symmetry is supported; the pooled model is retained.**

---

## 7. Model C — conditional adverse selection

Among filled opportunities, predicting the signed quote-relative mid markout one second after
the fill. 2,769,976 scored out-of-fold fills. Mean target −56.98 ticks, median −41.5.

| Model | MAE | Median abs error | RMSE | Spearman | R² | Block mean | Worst block | Blocks with ρ > 0 |
|---|---|---|---|---|---|---|---|---|
| naive (fold median) | 52.85 | 32.00 | 98.72 | — | −0.047 | −0.010 | −0.119 | 2/120 |
| ridge | 52.01 | 33.05 | 96.34 | **0.1805** | 0.003 | 0.1545 | −0.1430 | 118/120 |
| LightGBM (L1) | **51.88** | **30.49** | 97.43 | 0.1682 | −0.020 | 0.1538 | −0.0134 | 119/120 |

And the binary form, `good_fill_1s` (markout > 0):

| Model | ROC AUC | Base rate | Block mean | Worst block | Bootstrap 5–95 % |
|---|---|---|---|---|---|
| naive | 0.500 per fold | 12.85 % | 0.5014 | 0.4162 | 0.4990–0.5040 |
| **logistic** | **0.6838** | 12.85 % | 0.6617 | 0.3163 | 0.6521–0.6721 |
| LightGBM | 0.6795 | 12.85 % | 0.6591 | 0.4148 | 0.6498–0.6682 |

**This is real but weak.** The MAE improvement over a constant is 1.0 tick out of 52.9 (1.8 %),
R² is essentially zero, and the rank correlation is ~0.17 with a per-fold range of 0.10–0.29
(LightGBM: 0.137, 0.193, 0.178, 0.200, 0.285, 0.144, 0.138, 0.145, 0.144, 0.101). It peaks in
fold 4 — the block containing the 2026-08-19 squeeze — and sits near 0.14 with a 0.101 tail
afterwards. `sign_accuracy` of 0.871 is *not* a result: 87 % of fills are negative, so a constant
negative prediction scores the same.

No single book feature dominates. The largest gain share is `segment_age_ms` at 16.4 %, followed
by `time_since_bbo_change_ms` 6.4 % and `log_opp_depth_l1` 6.0 %. That `segment_age_ms` leads is
a warning, addressed in §9.


---

## 8. The critical joint diagnostic

**Are the states that are easiest to fill also the states with the worst post-fill markout?**

Out-of-fold predictions only. This is a structural diagnostic, not a trading rule: no threshold
is chosen, no expected value is computed.

Marginal view, deciles of the out-of-fold predicted `P(fill within 5 s)`
(`predicted_fill_vs_markout.csv`, 4,285,294 opportunities):

| Decile | Predicted P(fill 5 s) | Realised fill 5 s | Realised fill 30 s | Trade-through rate | Mean realised 1 s markout |
|---|---|---|---|---|---|
| 0 | 0.023 | 0.045 | 0.262 | 0.947 | −49.7 |
| 1 | 0.053 | 0.081 | 0.359 | 0.925 | −53.8 |
| 2 | 0.092 | 0.133 | 0.456 | 0.903 | −56.3 |
| 3 | 0.146 | 0.202 | 0.553 | 0.883 | −58.0 |
| 4 | 0.220 | 0.284 | 0.648 | 0.864 | −59.4 |
| 5 | 0.313 | 0.374 | 0.725 | 0.846 | **−61.0** |
| 6 | 0.418 | 0.459 | 0.780 | 0.811 | −60.8 |
| 7 | 0.540 | 0.564 | 0.836 | 0.766 | −59.7 |
| 8 | 0.688 | 0.695 | 0.889 | 0.692 | −56.6 |
| 9 | 0.880 | 0.883 | 0.956 | 0.584 | −50.0 |

**The answer is no — not monotonically.** The relationship is hump-shaped. The worst markout sits
in the *middle* of the fill-probability distribution, and the easiest-to-fill decile is among the
*least* adverse, because its fills are disproportionately at-quote (58 % trade-through versus
95 % in the hardest-to-fill decile) and at-quote fills are 16 ticks less adverse.

The fill model is well calibrated at its own horizon: predicted 0.023 → 0.880 against realised
0.045 → 0.883, slightly under-confident in the lowest deciles and near-exact above the median.
The 30 s column is shown alongside because the markout column is conditioned on any fill within
the observation window, not only fills inside 5 s.

Two-dimensional view, out-of-fold fill-probability quintile × out-of-fold predicted-markout
quintile (`joint_fill_adverse_bucket.csv`). Mean realised 1 s markout, ticks:

| fill ↓ / adverse → | q0 | q1 | q2 | q3 | q4 |
|---|---|---|---|---|---|
| **q0** | −54.1 | −51.1 | −52.9 | −51.8 | −50.1 |
| **q1** | −62.0 | −63.2 | −62.2 | −53.2 | −42.8 |
| **q2** | −75.6 | −66.7 | −63.2 | −55.7 | −40.7 |
| **q3** | −74.3 | −67.4 | −62.0 | −55.4 | −41.7 |
| **q4** | −69.3 | −59.7 | −56.3 | −51.3 | **−38.9** |

Realised fill rate over the same grid runs 0.243 (q0,q0) to 0.942 (q4,q4), and the trade-through
rate falls from 0.923 to 0.520 along the same diagonal. Cell populations are 103k–251k.

Three readings:

1. **Model C carries information Model B does not.** Within the highest fill quintile the
   predicted-markout quintile still separates realised markout by 30 ticks (−69.3 → −38.9).
2. **The best joint cell is the top-right**, not the top-left: highest predicted fill *and*
   least predicted adverse selection, at −38.9 ticks. Fill probability and adverse selection are
   not the straightforward trade-off the phase 1 headline implied.
3. **Every cell is deeply negative.** The most favourable corner of a 25-cell surface still loses
   38.9 ticks (~$3.90) per fill on a 1 s markout, against a captured half-spread of 0.5 ticks.
   Nothing in this surface is close to break-even before fees are even considered.

The same tension is visible without any model. Across `obi_l1` deciles: P(bid fill within 1 s)
falls 43.2 % → 6.1 %, the bid trade-through rate rises 50.9 % → 98.4 %, and the mean bid markout
is *least* bad at the thin-bid extreme (−38.6) and worst in the middle (−63.9 at decile 7).

---

## 9. Robustness: is any of this just a clock?

`segment_age_ms` — how far into the current synchronized segment the decision sits — is causal
and was pre-registered, but it is a position-in-session index, not a book state, and it carried
the largest single gain share in the markout model (16.4 %) and the fourth largest in the
direction model (11.4 %). That is exactly the shape a spurious result takes.

The two headline problems were therefore refitted with that one feature removed, under the same
folds, seeds and hyper-parameters. This is a robustness check, not a feature search: no result
here was used to choose a feature set. (`robustness_metrics.csv`)

| Problem | Metric | With clock | Without clock | Worst block, without |
|---|---|---|---|---|
| `price_direction` (logistic) | AUC | 0.7412 | 0.7416 | 0.6003 |
| `price_direction` (LightGBM) | AUC | 0.7265 | **0.7355** | 0.6051 |
| `markout_1000ms` (ridge) | Spearman | 0.1805 | 0.1770 | −0.1401 |
| `markout_1000ms` (LightGBM) | Spearman | 0.1682 | **0.1970** | **+0.0274** |
| `markout_1000ms` (LightGBM) | MAE | 51.88 | **51.40** | — |
| `good_fill_1s` (logistic) | AUC | 0.6838 | 0.6827 | 0.3154 |
| `good_fill_1s` (LightGBM) | AUC | 0.6795 | **0.6912** | 0.4643 |

**Nothing depends on the clock; removing it makes every LightGBM result better.** The markout
model improves from ρ = 0.168 to ρ = 0.197, its MAE improves by half a tick, and its worst
30-minute block moves from −0.013 to +0.027 — meaning **120/120 blocks now have the expected
sign**. LightGBM was spending capacity on a session index and getting slightly worse for it.

The practical consequences: the adverse-selection signal is genuine book state, and with the
clock removed LightGBM finally earns its keep on Model C, which it did not in the headline run.

---

## 10. Answers to the questions this phase had to settle

1. **How large is the trade/depth receive-time lag around quote sweeps?** Median ~60 ms;
   only 6–8 % of prints are acknowledged by the depth stream within 10 ms; 19.5 % take more than
   100 ms. For `through_quote` prints every book reaction — quantity, removal, best price,
   adverse mid — lands together at a median 48 ms, which is the signature of a 100 ms batched
   feed rather than of staged market behaviour.

2. **Is `fill_before_adverse` contaminated by feed ordering?** Yes, almost entirely. 85 % at
   zero handicap, 44 % at 50 ms, **11 % at 100 ms**, asymptoting to ~8 %. The alternatives
   (`quote_disappears`, `depth_best_moves_adverse`) behave identically because they race the
   same feed. The statistic is retained as a diagnostic under an explicit name and is **not**
   used as an economic target anywhere.

3. **What predicts next mid-move direction?** Queue imbalance at the touch, and little else.
   `obi_l1`, `weighted_obi_l5` and the microprice offset carry 39 % of the gain; the logistic
   model is essentially `log_bid_depth_l1 − log_ask_depth_l1`. OOF AUC 0.741, 120/120 blocks
   above 0.5, worst block 0.595. Across `obi_l1` deciles, P(up) runs 15.8 % → 84.3 %.

4. **What predicts whether a move happens soon?** Trade arrival intensity. `trade_count` windows
   carry 55 % of the gain at 250 ms. OOF AUC 0.886 at 250 ms falling to 0.840 at 1 s.
   `time_since_mid_change_ms` deciles move P(move within 1 s) from 67.9 % to 3.1 % while leaving
   P(up) flat at 0.49 — timing and direction are genuinely separate problems.

5. **Does LightGBM materially improve on the linear baselines?** Mostly no. It wins clearly only
   on short-horizon move intensity (+1.3 AUC points at 250 ms) and, once the clock feature is
   removed, on adverse selection (ρ 0.197 vs 0.177; AUC 0.691 vs 0.683). It **loses** to logistic
   regression on price direction (0.727 vs 0.741) and on the 5 s horizons of both move intensity
   and fill. On a 71-hour single-instrument sample the interpretable baseline is the right
   default; the non-linear model is not buying much.

6. **What predicts passive fill probability?** Own-side depth at the touch, overwhelmingly:
   `log_own_depth_l1` is 32–35 % of the gain at every horizon, with trade intensity and queue
   ahead behind it. OOF AUC 0.883 / 0.861 / 0.821 at 500 ms / 1 s / 5 s, well calibrated across
   all deciles. This is close to a mechanical relationship, which is why it scores so well and
   why it should not be mistaken for alpha.

7. **Are trade-through fills predictable separately from at-quote fills?** Yes. `P(trade-through
   | fill)` reaches OOF AUC 0.790 with calibration from 0.41 to 0.99 realised. Thin own quotes
   are annihilated, thick ones get worked: the bid trade-through rate runs 50.9 % → 98.4 % across
   `obi_l1` deciles.

8. **What predicts post-fill adverse selection?** Something, but weakly, and no single feature
   dominates. Spearman 0.197 (LightGBM without the clock), MAE 51.40 against a naive 52.85 — a
   1.5 tick improvement on a −57 tick mean. Opposite-side depth, own and opposite concentration,
   book dispersion and time since the last BBO change all contribute a few percent each. R² is
   indistinguishable from zero.

9. **How different are trade-through and at-quote post-fill markouts?** Very. At 1 s:
   trade-through −56.8 / −60.5 ticks (bid / ask) and favourable only 6 % of the time; at-quote
   −40.3 / −41.9 and favourable 35 % of the time. They should never be pooled in an economic
   model.

10. **Are high-fill-probability states systematically more adverse?** **No — the relationship is
    hump-shaped.** Mean markout worsens from −49.7 to −61.0 across the first six predicted-fill
    deciles and then improves back to −50.0 at the top decile, because the easiest fills are
    disproportionately at-quote. The naive "easy fills are the bad fills" story is wrong here.
    What is true is that *every* cell of the 5 × 5 joint surface is between −39 and −76 ticks.

11. **Which results are stable across chronological blocks?** Direction (120/120 blocks above
    0.5, worst 0.595), move intensity (120/120 at every horizon), fill (120/120 at every
    horizon, worst 0.677), and mechanism (120/120). All four are stable in a strong sense.

12. **Which relationships weaken under blocked OOF?** Adverse selection. Its per-fold Spearman
    sits near 0.14 across the last four folds and falls to 0.101 in the final one, against a
    peak of 0.285 in the squeeze block and its worst
    30-minute block is only marginally positive even after the clock is removed. `good_fill_1s`
    shows the same drift (per-fold AUC 0.712 early, 0.631–0.648 in the last two folds). The
    contrast with phase 1 is worth stating carefully, because the two numbers measure different
    things: phase 1 reported an in-sample rank IC of 0.317 for a *single raw signal* (`obi_l1`)
    against the *unconditional* 1 s mid markout, whereas this phase reports ρ ≈ 0.20 for a
    *full model* against the *post-fill* markout out of fold. The direction result that
    underlies the phase 1 number reproduces cleanly (AUC 0.741, every block positive); it is
    specifically the conditional-on-fill markout that is weak.

13. **Is there enough evidence to justify a maker EV model?** The components are real and
    separable: fill probability and fill mechanism are strongly and stably predictable,
    direction is strongly predictable, and adverse selection is weakly but consistently
    predictable. That is enough structure to build the EV decomposition on.

    But the level is discouraging and should be stated before anything is built. The *best* cell
    of the joint surface — highest predicted fill, lowest predicted adverse selection, 250,656
    opportunities — still realises **−38.9 ticks** of 1 s markout per fill against a half-spread
    of 0.5 ticks. On this instrument, in this corpus, under this deliberately pessimistic queue
    model, there is no region of the state space where passive quoting at the touch is close to
    break-even on a one-second horizon. Phase 3 should be scoped as *finding out how far from
    break-even the best states are and why*, not as a search for a profitable configuration.

14. **What limitations remain because Binance gives aggregated L2 rather than L3?** Listed in
    full below; the binding one is that queue position is unobservable, so the fill model is
    conditioned on an assumption (whole displayed quantity ahead, reductions never advance) that
    cannot be validated from this data and is deliberately pessimistic. The observed −39 tick
    floor is therefore a lower bound on quality, not an upper bound.

15. **What should remain untouched?** The rotation-enabled AWS capture, every later AWS capture,
    and the Tardis June–August holdout. None was read in this phase.

---

## 11. Artifacts, code and tests

### Committed, reviewable (`research/native_predictive_v1/`)

`methodology.json` (the pre-registration), `folds.csv`, `cross_stream_timing.csv`,
`cross_stream_timing_summary.csv`, `race_handicap.csv`, `timing_qc_file{0,1,2}.json`,
`fill_population.csv`, `mechanism_markout_summary.csv`, `price_direction_metrics.csv`,
`move_intensity_metrics.csv`, `fill_metrics.csv`, `fill_mechanism_metrics.csv`,
`adverse_selection_metrics.csv`, `robustness_metrics.csv`, `pooled_metrics.csv`,
`fold_metrics.csv`, `calibration.csv`, `model_coefficients.csv`, `feature_importance.csv`,
`joint_fill_adverse_bucket.csv`, `predicted_fill_vs_markout.csv`, `conditional_behaviour.csv`.

### Heavy, ignored (`data/research/native_predictive_v1/`)

`cross_stream_timing_file{0,1,2}.csv.zst` (5.17 M print-level timing events, 130 MB),
`model_frame_file{0,1,2}.parquet` (the reduced float32 feature/target frames, 543 MB),
`oof_price_predictions.csv.zst` (2.14 M rows, 162 MB),
`oof_side_predictions.csv.zst` (4.29 M rows, 643 MB).

Model B and Model C predictions share one file rather than two: they are estimated on the same
side-normalised rows with the same keys, so splitting them would duplicate 4.29 M key columns to
no benefit. Every problem's predictions are separate columns named `{problem}_{model}`, with the
problem's label in `y_{problem}` (NaN where that problem's population excludes the row).

This follows the existing repository convention — heavy derived data under the ignored `data/`
tree, small reviewable artifacts under `research/`. All outputs are deterministic from identical
inputs; seeds, hyper-parameters and the stride used for preprocessing quantiles are recorded in
`methodology.json`.

### Code

**New**
- `cpp/research/cross_stream_timing.{hpp,cpp}` — per-print depth-reaction event study observer.
- `cpp/app/native_timing_main.cpp` — `native_timing_audit` CLI.
- `scripts/native_timing_audit.sh` — runs the audit over the frozen corpus.
- `native_predictive/{__init__,spec,data,modeling,timing,pipeline}.py`.
- `tests/test_native_predictive.py` — 19 leakage-audit tests.
- `docs/native_predictive_v1.md` — this report.

**Modified**
- `cpp/research/native_dataset_exporter.{hpp,cpp}` — renamed `{side}_fill_before_adverse` to
  `{side}_fill_before_observed_mid_adverse`; added `{side}_time_to_{mid_adverse,quote_gone,
  best_adverse}_ms`, `time_since_{trade,depth,bbo_change,mid_change}_ms`,
  `bbo_change_count_{w}` and `backward_mid_abs_change_ticks_{w}`. The dataset is now 366
  columns, re-exported from the same frozen raw files.
- `cpp/CMakeLists.txt`, `cpp/tests/test_main.cpp` — new target and new assertions.
- `native_research/diagnostics.py`, `docs/native_dev_v1_corpus.md` — follow the rename.

### Test results

**C++** — `ctest` 6/6 pass, including `crypto_l2_tests` (16 groups). The native dataset group
gained assertions that pin the phase 2 semantics: a fill recorded from the trade stream while
the depth stream shows no quote removal, no touch move and no adverse mid move before the
segment ends (the feed-asynchrony case, made explicit as a fixture); an ask-side book race that
fires on `time_to_best_adverse_ms` and `time_to_quote_gone_ms` at 50 ms while the one-tick
`time_to_mid_adverse_ms` race does not fire at all; and in-segment-only activity features.

**Python** — `tests/test_native_predictive.py`, 19 tests, all pass:

- purge exceeds every target horizon, and the methodology declares no random splitting;
- Model C features contain no `fill`, `markout`, `postfill`, `time_to_`, `adverse`,
  `quote_gone` or `through` token — placement-time state only;
- the side-normalised frame does not contain `side` itself;
- side normalisation flips signed features and swaps own for opposite, leaving symmetric
  quantities untouched;
- move labels are censored exactly when the horizon did not fit inside the segment;
- no price target, fill outcome or post-fill markout crosses a segment boundary;
- a trade at the decision instant cannot fill the order it informed (`time_to_fill_ms > 0`);
- `fill_at_quote` and `fill_via_trade_through` are mutually consistent and sum to one;
- folds are chronological, expanding, non-overlapping and purged, with the latest training
  row's longest target ending before the validation block opens;
- segment 2:3 is never a standalone validation fold;
- every OOF prediction lies inside a validation block and is unique per key;
- repeated runs with the same spec produce identical predictions and identical metrics;
- no committed metric file contains the string `out_of_sample`.

`tests/test_native_research.py` (phase 1, 15 tests) still passes against the extended 366-column
dataset.

**The suite must be run as two processes on this machine.** `tests/test_event_models.py` imports
torch and `tests/test_native_predictive.py` fits LightGBM; on macOS each library ships its own
OpenMP runtime, and loading both into one interpreter deadlocks at the first LightGBM parallel
region (setting `OMP_NUM_THREADS=1` turns the deadlock into a silent crash rather than fixing
it). This is a pre-existing environment constraint that phase 2 exposed by adding the first
test module that actually fits a LightGBM model; it is not a defect in either module. Run:

```
python -m unittest $(cd tests && ls test_*.py | sed 's/\.py$//' | grep -v test_native_predictive \
    | sed 's/^/tests./' | tr '\n' ' ')      # 105 tests, 104 pass + the frozen-hash gate below
python -m unittest tests.test_native_predictive          # 19 tests, all pass
```

The known pre-existing `test_passive_pipeline.test_frozen_source_and_binary_gate` failure is
untouched. It pins the SHA-256 of a binary built at commit `628618b` and cannot pass at HEAD
independently of this work; its expected hash was **not** updated, so the audit trail survives.

---

## Limitations that no amount of modelling fixes

1. **Binance publishes aggregated L2, not L3.** There is no queue position, no order identity,
   no ability to distinguish a cancellation from an execution inside a 100 ms netted diff. Every
   fill number here rests on the conservative assumption that the entire displayed quantity is
   ahead of the hypothetical order and that reductions never advance it. That assumption is
   deliberately pessimistic and cannot be validated from this data.
2. **The depth feed is batched at 100 ms and lags the trade feed by ~60 ms at the median.** Any
   quantity defined by the order of a trade event and a book event within that window is a
   statement about feeds, not about markets. Section 2 quantifies this; it does not remove it.
3. **The mid is a poor instantaneous price.** It is unchanged across 94 % of 100 ms steps, then
   jumps 8–25 ticks. Markouts are zero-inflated with extreme tails, which is why the regression
   objective and the headline metrics here are L1/rank based rather than R².
4. **71 hours is three days of one instrument.** Two of the ten folds contain a violent squeeze
   (2026-08-19T19:28Z) that moves the mid ~1,250 ticks in a second. Block-level dispersion in
   the results below is driven substantially by that single episode.
5. **`fill_at_quote` is not the same as "worked the queue".** It only means no print occurred
   beyond the quote price before the cumulative same-price volume exceeded the assumed queue.
6. **Adverse selection is the weakest of the three components and the least stable.** Its
   out-of-fold rank correlation decays across the last four folds, so a phase 3 that leans on
   Model C should re-estimate that decay on forward data before trusting it.

## What stays untouched for forward validation

- The rotation-enabled AWS capture file currently being written.
- Every AWS capture after `BTCUSDT-LONDON-20260818T062918Z.chft.zst`.
- The Tardis June/July/August holdout.

None were read, loaded, summarised or used to derive any feature, threshold or hyper-parameter
in this phase.

## Reproducing

```
cmake -S cpp -B build/cpp && cmake --build build/cpp -j8
python -m pyresearch.native.core.pipeline all          # phase 1 corpus, QC and dataset
bash scripts/native_timing_audit.sh             # cross-stream event study (see report §1)
python -m pyresearch.native.predictive.pipeline all        # frames, timing, descriptive, models, joint
# torch and LightGBM cannot share an interpreter here, so run these separately
python -m unittest tests.test_native_research
python -m unittest tests.test_native_predictive
```
