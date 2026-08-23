# Phase 6 — signal decay and horizon extension

Development corpus `research/specs/native_dev_v1.json`, 71.4 hours of native Binance USD-M
capture, BTC $63,999 → $72,940. Every number below is a **blocked out-of-fold development
estimate**. None of it is out of sample. The rotation-enabled AWS file, every later AWS capture,
every post-`native_dev_v1` native capture and the Tardis June/July/August holdout were not
opened.

Five phases established two walls. The passive line is blocked by an **information** limit
(phases 3, 4A, 4B: queue position is unobservable on aggregated L2 and economically decisive).
The active line is blocked by a **cost** limit (phase 5A: the best cell in the whole
pre-registered grid moved 0.87 bp against a hurdle grid starting at 1 bp). Neither is reopened
here. Phase 5A's own closing note named the one question the corpus had never been asked:

> Everything measured here lives inside one second, where the move is bounded by the tick grid
> and the fee is not. Whether the same book state predicts anything at 10 s to 10 min — where a
> 5 bp cost is a small fraction of the move — is a completely different question.

This phase asks exactly that, and the decomposition that decides it:

> Is a positive long-horizon markout merely inherited from the first one-to-five seconds of
> movement, or does signal strength at t predict **additional** movement after t + 5 s?

**Verdict: B — longer-horizon information survives but is marginal.** Information genuinely
persists past five seconds: the incremental edge after 5 s is resolved, positive and
block-stable at 10 s, 30 s and 60 s for every signal tested. It then dies — nothing resolves at
120 s, 300 s or 600 s. And it never becomes economically material: the largest break-even
all-in cost anywhere in the study is **0.693 bp**, and **0 of 486** cost cells clear even the
cheapest one-way hurdle in the fixed grid. Extending the horizon by a factor of thirty bought a
factor of 2.4 in gross edge and moved the answer no closer to the fee.

No model was trained in this phase. No horizon, threshold or decile was selected as best.
Pre-registration and input hashes: `research/native_decay_v1/methodology.json`.

One scale note that governs every economic statement: at this corpus's mean mid of 673,728
ticks, **1 tick = 0.0148 bp** and **1 bp ≈ 67 ticks**.

---

## 0. What was done

Long-horizon markouts were regenerated from the frozen phase 1 100 ms grid, gated against the
frozen targets, and six frozen signals were evaluated against them at nine fixed horizons under
a horizon-specific purge and a horizon-aware block bootstrap.

**Targets.** `markout_{h}s_ticks = mid(t + h) − mid(t)`, both mids read from the frozen phase 1
`mid_ticks` column. Because the phase 1 grid is contiguous and absolutely aligned at 100 ms —
asserted before any target is built, and a hole in the grid raises rather than interpolates —
the instant `t + h` is exactly the row `h / 100 ms` later inside the same segment. A horizon
that would cross a segment edge is **censored, never zero**.

**Signals.** All frozen, all reduced to one signed per-instant number by a fixed transform.
Nothing was fitted, refitted, tuned or combined.

| signal | source | transform | role |
|---|---|---|---|
| `obi_l1` | phase 1 grid, raw | identity | primary |
| `obi_l5`, `obi_l10` | phase 1 grid, raw | identity | secondary |
| `direction_p2_logistic` | phase 2 OOF `price_direction_linear` (AUC 0.7412) | `2p − 1` | primary |
| `direction_p2_lightgbm` | phase 2 OOF `price_direction_lightgbm` | `2p − 1` | secondary |
| `sweep_dir_p4a` | phase 4A sweep model, scored OOF at 100 ms by phase 4B | `p(ask) − p(bid)` | primary |

The sweep transform is the phase 5A direction convention — a threatened ask implies `+1`, a
threatened bid `−1` — written as one per-instant number so that every signal is scored on
identical rows. It is a fixed difference of two published columns, not a fitted combination.

**No frozen combined score exists.** Phase 5A fitted `book+flow+sweep` directional models but
published metrics only, never a stored out-of-fold prediction file. The brief permitted such a
score "only if already available"; it is not, and none was fitted here. This is recorded rather
than worked around.

---

## 1. The agreement gate

The brief required regenerated 1 s / 2 s / 5 s targets to be validated against the frozen
targets before anything else ran, and the phase to stop on disagreement. It did not stop.

| horizon | checked against | rows compared | censoring disagreements | rows differing | max difference |
|---|---|---|---|---|---|
| 1 s | frozen phase 1 `markout_1000ms_ticks` | 2,570,299 | **0** | **0** | **0.0 ticks** |
| 5 s | frozen phase 1 `markout_5000ms_ticks` | 2,569,979 | **0** | **0** | **0.0 ticks** |
| 2 s | phase 5A `markout_2000ms_ticks` | 2,142,528 | 0 | 16 | 148.5 ticks |

1 s and 5 s reproduce the frozen columns **exactly** — not to a tolerance, bit for bit, on 2.57 M
rows. That is a stronger check than phase 5A could run, because phase 5A rebuilt its horizons
from the phase 3 mid path while this phase reads the same grid the frozen columns were written
from.

2 s has no frozen phase 1 column, so it is checked against phase 5A's mid-path reconstruction.
The 16 disagreeing instants are **exactly** the 32 side-rows phase 5A already published as
resolving one event apart inside violent moves (phase 5A counted a bid and an ask row per
instant; this phase counts instants). That published discrepancy is reproduced, not re-litigated.

`research/native_decay_v1/target_agreement.json`.

---

## 2. Population, purge, dependence and effective sample

**Population.** The headline population is the 2,142,647 instants that carry every signal — the
phase 2 validation blocks, which are also exactly the rows phase 4B's sweep scores cover — so
the six signals are compared on identical rows. `obi_*` needs no fold and is additionally
reported on the full 2.57 M-row corpus as a labelled secondary table (§9).

**Purge, horizon specific.** The brief forbade reusing the old flat 60 s purge, and it is not
reused at any horizon. An evaluated row must sit at least `h + 60 s` after the start of its own
validation block, so the whole span of length `h` *behind* the evaluated row is also
post-training and no evaluated target window overlaps — or sits within `h` of — anything the
scoring model could have seen.

**Dependence.** 100 ms rows are never treated as independent. Intervals come from a
horizon-aware moving block bootstrap with block length `max(30 min, 12 h)`, 500 draws, seed 0.
No iid standard error appears anywhere.

**Effective sample**, `effective_sample.csv`, for the headline population:

| h | purge | evaluated rows | non-overlapping anchors | rows per anchor | bootstrap block | bootstrap blocks |
|---|---|---|---|---|---|---|
| 1 s | 61 s | 2,136,488 | 213,651 | 10 | 30 min | 120 |
| 5 s | 65 s | 2,135,848 | 42,720 | 50 | 30 min | 120 |
| 10 s | 70 s | 2,135,048 | 21,352 | 100 | 30 min | 120 |
| 30 s | 90 s | 2,131,848 | 7,108 | 300 | 30 min | 120 |
| 60 s | 120 s | 2,127,048 | 3,547 | 600 | 30 min | 120 |
| 120 s | 180 s | 2,117,470 | 1,767 | 1,198 | 30 min | 120 |
| 300 s | 360 s | 2,090,470 | 700 | 2,986 | 1 h | 60 |
| 600 s | 660 s | 2,045,470 | **344** | 5,946 | 2 h | **30** |

The purge costs almost nothing — 4 % of rows at the longest horizon — because the blocks are
5.95 h wide. What costs everything is independence: **at ten minutes the corpus contains 344
independent windows and 30 bootstrap blocks.** Every long-horizon statement below has to be read
against that column, and §9 shows what happens when it is not.

Segment 2:3 is 117.8 s long and therefore supports no horizon of 120 s or more at all; it drops
out of those rows by the censoring rule rather than by any exclusion.

---

## 3. The drift problem, and why the controls are not optional

The corpus is a three-day 13–14 % BTC rally. At minute horizons that drift is not a nuisance
term, it is the largest thing in the data:

| h | unconditional mean markout, all rows | after 30-minute demeaning |
|---|---|---|
| 1 s | +0.41 ticks | 0.000 |
| 30 s | +12.48 ticks | 0.000 |
| 120 s | +48.30 ticks | 0.000 |
| 600 s | **+210.99 ticks** | 0.000 |

The two fixed controls are a decile **spread** (any drift common to a period cancels in the
top-minus-bottom difference) and **30-minute time-block demeaning** (which removes drift that
correlates with decile membership). Both are applied to already-realised returns only; no future
return is used as a feature anywhere, and a test pins that.

How much they matter, `obi_l1` cumulative decile means in ticks:

| h | raw top decile | raw bottom decile | demeaned top | demeaned bottom |
|---|---|---|---|---|
| 1 s | +20.2 | −19.1 | +19.7 | −19.5 |
| 30 s | +62.5 | −33.0 | +46.7 | −45.9 |
| 120 s | +90.7 | **+0.6** | +31.3 | −48.8 |
| 600 s | **+251.4** | **+180.6** | +19.3 | −32.4 |

**Untreated, at ten minutes both legs are strongly positive** — the bottom decile of order-book
imbalance "makes money" at +180 ticks, because everything did. The raw spread survives that only
because the difference cancels most of it; the raw *legs* are meaningless. Every headline number
in this report is the demeaned one, and the raw one is published beside it.

---

## 4. Decay profile: signed markout and hit rate

`decay_profile.csv`. Signed markout is `sign(signal) × markout`, over every evaluated row — a
per-trade directional quantity on 100 % of the denominator, not a selected tail.

Demeaned signed markout, ticks (bps in parentheses for `obi_l1`):

| h | obi_l1 | obi_l5 | obi_l10 | dir_p2_logistic | dir_p2_lgbm | sweep_dir_p4a |
|---|---|---|---|---|---|---|
| 1 s | 7.85 (0.114) | 7.93 | 7.98 | 8.04 | 8.13 | **8.55** |
| 2 s | 10.78 (0.157) | 10.88 | 10.97 | 11.03 | 11.05 | **11.68** |
| 5 s | 14.97 (0.219) | 15.01 | 15.16 | 15.54 | 15.16 | **16.02** |
| 10 s | 17.74 (0.260) | 17.71 | 17.88 | **18.32** | 17.71 | 18.46 |
| **30 s** | **21.07 (0.311)** | **21.29** | **21.62** | **22.02** | **21.15** | **21.05** |
| 60 s | 18.53 (0.275) | 19.29 | 19.79 | 19.99 | 18.97 | 19.00 |
| 120 s | 17.25 (0.255) | 17.94 | 18.08 | 18.65 | 17.95 | 17.51 |
| 300 s | 12.26 (0.179) | 12.04 | 11.76 | 12.15 | 13.28 | 13.79 |
| 600 s | 5.66 (0.081) | 4.96 | 4.51 | 3.98 | −0.11 | 5.58 |

Hit rate, `P(signed markout > 0 | the mid moved)`:

| h | obi_l1 | dir_p2_logistic | sweep_dir_p4a |
|---|---|---|---|
| 1 s | 0.742 | 0.746 | **0.751** |
| 5 s | 0.648 | 0.650 | 0.651 |
| 30 s | 0.569 | 0.570 | 0.569 |
| 120 s | 0.525 | 0.526 | 0.527 |
| 600 s | **0.506** | **0.508** | **0.510** |

Two facts, and they pull in opposite directions.

**The edge in ticks peaks at 30 seconds, not at one second.** Every one of the six signals
maximises there, and 30 s is 2.7× the 1 s value. Something real survives past five seconds.

**The hit rate decays monotonically to a coin flip.** From 0.74–0.75 at one second to 0.506–0.510
at ten minutes. The signal stops being a statement about direction long before ten minutes; what
little is left at 300–600 s is a small edge on an increasingly symmetric distribution.

---

## 5. Signal-strength deciles and monotonicity

`signal_deciles.csv`, `decile_monotonicity.csv`. Demeaned mean markout by `obi_l1` decile, ticks:

| h | d0 | d1 | d2 | d3 | d4 | d5 | d6 | d7 | d8 | d9 |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 s | −19.5 | −10.1 | −5.9 | −3.0 | −0.8 | +0.5 | +2.7 | +6.0 | +10.3 | +19.7 |
| 5 s | −32.6 | −21.2 | −12.5 | −6.2 | −2.3 | +0.7 | +5.2 | +13.2 | +21.4 | +34.4 |
| 30 s | −45.9 | −29.5 | −16.3 | −11.0 | −2.9 | +3.0 | +6.3 | +20.3 | +29.2 | +46.7 |
| 120 s | −48.8 | −27.8 | −8.4 | −2.5 | +1.7 | +2.3 | +9.3 | +20.9 | +22.0 | +31.3 |
| 600 s | −32.4 | −17.1 | +8.4 | +9.4 | +5.3 | −7.5 | −4.9 | +13.9 | +5.7 | +19.3 |

Strictly monotone increasing across all ten deciles?

| h | obi_l1 | obi_l5 | obi_l10 | dir_p2_logistic | dir_p2_lgbm | sweep_dir_p4a |
|---|---|---|---|---|---|---|
| 1 s → 60 s | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| 120 s | ✓ | ✓ | ✓ | ✓ | ✗ (ρ 0.988) | ✗ (ρ 0.988) |
| 300 s | ✗ (ρ 0.867) | ✗ (0.806) | ✗ (0.806) | ✗ (0.867) | ✗ (0.903) | ✗ (0.952) |
| 600 s | ✗ (ρ 0.636) | ✗ (0.673) | ✗ (0.636) | ✗ (0.406) | ✗ (**−0.115**) | ✗ (0.248) |

**Perfect monotonicity survives to 60 seconds for every signal and collapses beyond it.** By ten
minutes `direction_p2_lightgbm` has a *negative* rank correlation between decile and outcome.
This is the same boundary the incremental decomposition finds independently in §6, from a
different statistic.

---

## 6. Cumulative versus incremental — the main question

`cumulative_incremental.csv`, `reconciliation.csv`. The decision quantity is the drift-demeaned
top-minus-bottom decile spread; "resolved" means the 5–95 % bootstrap interval excludes zero
**and** the non-overlapping anchor estimate carries the same sign.

### Cumulative

`obi_l1`, demeaned spread in ticks, with the horizon-aware bootstrap interval:

| h | spread | 5–95 % | resolved | block sign share | anchor spread |
|---|---|---|---|---|---|
| 1 s | 39.16 | 35.99 – 43.46 | yes | 1.000 | 38.46 |
| 2 s | 50.99 | 47.47 – 55.66 | yes | 1.000 | 48.99 |
| 5 s | 66.99 | 63.44 – 72.49 | yes | 1.000 | 64.14 |
| 10 s | 76.28 | 70.76 – 83.88 | yes | 1.000 | 62.30 |
| **30 s** | **92.53** | 82.56 – 106.86 | yes | 0.992 | 77.97 |
| 60 s | 87.33 | 73.38 – 107.52 | yes | 0.908 | 100.63 |
| 120 s | 80.04 | 55.25 – 112.89 | yes | 0.775 | 27.72 |
| 300 s | 83.23 | 43.79 – 123.26 | yes | 0.650 | −41.48 |
| 600 s | 51.74 | **−3.37 – 113.14** | **no** | 0.633 | +701.51 |

### Incremental after 5 s

The same deciles — formed on the signal at t, never on any future return — applied to
`markout(t → t+h) − markout(t → t+5 s)`. Demeaned, ticks:

| h | obi_l1 | obi_l5 | obi_l10 | dir_p2_logistic | dir_p2_lgbm | sweep_dir_p4a |
|---|---|---|---|---|---|---|
| 10 s | **+9.3** | **+8.6** | **+8.8** | **+10.0** | **+10.8** | +5.3 |
| 30 s | **+25.5** | **+26.1** | **+25.6** | **+25.2** | **+26.9** | **+11.9** |
| 60 s | **+20.2** | **+21.6** | **+21.0** | **+19.3** | **+24.9** | +0.9 |
| 120 s | +12.8 | +12.6 | +12.8 | +12.1 | +15.1 | −4.1 |
| 300 s | +15.8 | +8.6 | −2.7 | +2.0 | +10.8 | +12.2 |
| 600 s | −15.6 | −22.4 | −32.2 | −39.8 | −36.5 | −48.5 |

Bold = resolved. Which cells resolve:

| h | obi_l1 | obi_l5 | obi_l10 | dir_p2_logistic | dir_p2_lgbm | sweep_dir_p4a |
|---|---|---|---|---|---|---|
| 10 s | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ (anchor sign disagrees) |
| 30 s | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| 60 s | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ |
| 120 s | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| 300 s | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| 600 s | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |

Block sign share on 30-minute blocks (the stability floor is 0.60):

| h | obi_l1 | dir_p2_logistic | dir_p2_lgbm | sweep_dir_p4a |
|---|---|---|---|---|
| 10 s | 0.775 | 0.742 | 0.758 | 0.767 |
| 30 s | 0.717 | 0.717 | 0.767 | 0.667 |
| 60 s | 0.608 | 0.600 | 0.675 | 0.575 |
| 120 s | 0.508 | 0.517 | 0.583 | 0.533 |
| 300 s | 0.517 | 0.467 | 0.517 | 0.450 |

### The answer

The decomposition reconciles **exactly** — `cumulative(h) = pivot(h population) + incremental` to
a worst residual of 3.2e-14 ticks over 72 rows, because both terms use the same deciles on the
same population. Demeaned, `obi_l1`:

| h | cumulative | of which inherited from the first 5 s | additional after 5 s | inherited share |
|---|---|---|---|---|
| 10 s | 76.28 | 66.99 | +9.29 | **87.8 %** |
| **30 s** | **92.53** | 67.01 | **+25.52** | **72.4 %** |
| 60 s | 87.33 | 67.13 | +20.21 | 76.9 % |
| 120 s | 80.04 | 67.27 | +12.77 | 84.0 % |
| 300 s | 83.23 | 67.42 | +15.82 | 81.0 % |
| 600 s | 51.74 | 67.34 | −15.60 | 130.2 % |

**Both things are true, and the proportions are the finding.** Long-horizon markout is *mostly*
inherited — 72–88 % of it at every horizon where anything resolves — but it is **not only**
inherited. Signal strength at t predicts genuinely additional movement between 5 s and 60 s, and
that increment is resolved, block-stable and reproduced by the independent anchor sample.

Past 120 s nothing resolves. The point estimates go negative at 600 s for all six signals — the
inherited share exceeds 100 %, meaning the price gives some of the first five seconds back — but
not one of those cells is distinguishable from zero, so **this phase does not claim a reversal.**

For `sweep_dir_p4a` the inherited share is 93.7 % at 10 s, 86.9 % at 30 s and 98.8 % at 60 s. The
phase 4A sweep classifier is the most nearly ultra-short of the six: it adds the least after five
seconds, which is what a model trained on a 500 ms consumption target should look like.

---

## 7. Up and down legs

`up_down_legs.csv`. Reported separately because a common drift shifts both legs the same way
while a real directional signal moves them apart. `obi_l1`, mean markout in ticks:

| h | raw top decile | raw bottom decile | demeaned top | demeaned bottom |
|---|---|---|---|---|
| 1 s | +20.21 | −19.06 | +19.70 | −19.47 |
| 5 s | +36.94 | −30.55 | +34.37 | −32.62 |
| 30 s | +62.51 | −33.03 | +46.66 | −45.87 |
| 60 s | +69.12 | −24.03 | +38.39 | −48.94 |
| 120 s | +90.71 | +0.58 | +31.26 | −48.79 |
| 300 s | +168.75 | +67.67 | +32.80 | −50.43 |
| 600 s | +251.42 | +180.58 | +19.30 | −32.43 |

Raw, the two legs converge and both go positive — the rally. Demeaned, they stay apart, and the
**down leg keeps working longer than the up leg**: at 300 s the demeaned bottom decile is −50.4
while the top is only +32.8. In a corpus that rallied 13 % that is the leg the drift was fighting,
which is mild evidence that the demeaning is not over-correcting. It is one instrument over three
days in one direction and should not be read as a structural asymmetry.

---

## 8. Stability by UTC day and segment

`stability.csv`. The 30-minute block sign share in §6 passes the pre-registered floor, but the
coarser groupings show the increment is **concentrated, not uniform**. `obi_l1`, demeaned
incremental-after-5 s spread in ticks:

| h | 2026-08-18 | 2026-08-19 | 2026-08-20 |
|---|---|---|---|
| 10 s | +12.1 | +15.2 | +3.4 |
| 30 s | +26.4 | **+52.8** | **+5.1** |
| 60 s | +21.8 | **+59.6** | **−8.7** |
| 120 s | +11.7 | +77.2 | −32.7 |
| 300 s | −20.2 | +71.5 | −4.1 |

By segment, the same quantity at 30 s: 2:1 +26.4, 2:2 +47.7, **2:4 +3.7**, 2:5 +30.8,
**2:6 −0.3**. (Segment 2:3 is 117.8 s long; its handful of rows produce −107 at 30 s and it
carries no horizon of 120 s or more at all. It is reported, not smoothed.)

**On the final UTC day the incremental edge is approximately zero at 30 s and negative at 60 s.**
The corpus-level increment is carried disproportionately by 2026-08-19 — the day containing the
squeeze that phase 2 already flagged as driving block-level dispersion — and by segment 2:2. This
does not overturn the pre-registered classification, which is decided on 30-minute blocks, but it
is the single largest reason to treat the "survives past 5 s" finding as marginal rather than
established, and it is why the verdict is B and not C.

---

## 9. The independence check, and what overlap hides

The anchor sample is the honest independent one, and at long horizons it disagrees violently with
the overlapping estimate. Cumulative demeaned spread at 600 s:

| signal | full-sample spread | bootstrap 5–95 % | anchor spread | anchor rows |
|---|---|---|---|---|
| obi_l1 | +51.7 | −3.4 – 113.1 | **+701.5** | 62 |
| obi_l5 | +45.4 | −1.5 – 95.3 | +451.2 | 63 |
| obi_l10 | +35.8 | +0.3 – 76.3 | +508.6 | 65 |
| direction_p2_logistic | +29.4 | −16.2 – 86.5 | **+1170.3** | 64 |
| direction_p2_lightgbm | +24.1 | −15.0 – 71.7 | +549.9 | 72 |
| sweep_dir_p4a | +31.0 | −17.8 – 85.3 | +957.5 | 65 |

Those anchor numbers are not a finding, they are **noise with 62 to 72 observations in it** — 344
anchors split into ten deciles. Reporting them is the point: at ten minutes this corpus cannot
distinguish a 50-tick effect from a 700-tick one, and any long-horizon result computed on 2.05 M
overlapping rows without a dependence-aware interval would have looked enormously significant. It
is also why the classification requires the anchor sign to agree before calling anything resolved
— a requirement that fired, and removed `sweep_dir_p4a` at 10 s and 60 s from the resolved set.

**Headline versus full corpus.** Restricting to out-of-fold-scored rows changes nothing material
(`obi_l1`, demeaned signed markout, ticks): 7.85 vs 6.93 at 1 s, 21.07 vs 20.81 at 30 s, 5.66 vs
2.70 at 600 s, with hit rates within 0.02 everywhere. The results are not an artifact of the
population choice.

---

## 10. Cost diagnostic

`cost_hurdle.csv`. Diagnostic only: the break-even is the gross edge itself — the largest all-in
cost the observed movement could have absorbed. No PnL, Sharpe, entry rule, threshold selection
or holding-period optimisation exists in this phase, and the hurdles are the fixed phase 5A grid,
not a live account fee tier.

A decile spread is a **long-short** quantity spanning two trades, so the per-trade edge — half the
spread — is what one round trip would have to pay for. Demeaned, best cell per signal:

| signal | best horizon | gross spread | **gross edge per trade** | break-even all-in cost |
|---|---|---|---|---|
| direction_p2_logistic | 30 s | 1.386 bp | **0.693 bp** | 0.693 bp |
| obi_l5 | 30 s | 1.378 bp | 0.689 bp | 0.689 bp |
| obi_l10 | 30 s | 1.374 bp | 0.687 bp | 0.687 bp |
| obi_l1 | 30 s | 1.362 bp | 0.681 bp | 0.681 bp |
| sweep_dir_p4a | 30 s | 1.326 bp | 0.663 bp | 0.663 bp |
| direction_p2_lightgbm | 30 s | 1.298 bp | 0.649 bp | 0.649 bp |

Against the fixed grid at the best cell in the entire study (0.693 bp):

| hurdle, one way | 1.0 | 2.0 | 3.0 | 5.0 | 7.5 | 10.0 |
|---|---|---|---|---|---|---|
| net of one way | −0.31 | −1.31 | −2.31 | −4.31 | −6.81 | −9.31 |
| net of round trip | −1.31 | −3.31 | −5.31 | −9.31 | −14.31 | −19.31 |

**0 of 486 cells clear even the cheapest one-way hurdle. 0 of 486 clear a round trip.**

The comparison against phase 5A is the sharpest way to say what this phase found. Phase 5A's best
cell was **0.87 bp at 1 s**, on a highly selective 1.5 % of opportunities. This phase's best cell
is **0.693 bp at 30 s**, on a 10 % decile. The two populations are not the same and the numbers
are not directly comparable, but the order of magnitude is: extending the horizon thirtyfold did
not move the edge into a different regime. The gross edge grew 2.4× from 1 s to 30 s while the
fee stayed fixed — and it started roughly 3.5× below the cheapest one-way hurdle, so a 2.4×
improvement leaves it still short, and short by more at every horizon beyond 30 s.

---

## 11. Classification of each signal

By the rules fixed in `spec.py` before any result was seen. `classification.csv`.

| signal | classification | resolved positive incremental | resolved negative | stable at h ≥ 120 s |
|---|---|---|---|---|
| `obi_l1` | **short continuation** | 10 s, 30 s, 60 s | none | none |
| `obi_l5` | **short continuation** | 10 s, 30 s, 60 s | none | none |
| `obi_l10` | **short continuation** | 10 s, 30 s, 60 s | none | none |
| `direction_p2_logistic` | **short continuation** | 10 s, 30 s, 60 s | none | none |
| `direction_p2_lightgbm` | **short continuation** | 10 s, 30 s, 60 s | none | none |
| `sweep_dir_p4a` | **short continuation** | 30 s | none | none |

No signal is *ultra-short only*: every one adds resolved movement after the five-second pivot.
No signal reaches *medium-horizon persistence*: nothing resolves at 120 s or beyond. No signal is
a *reversal*: the negative 600 s point estimates are uniformly unresolved. Nothing is *ambiguous*:
no signal carries resolved increments of both signs, no resolved cell fails the stability floor,
and no resolved cell flips sign between the raw and demeaned views.

`sweep_dir_p4a` is the weakest of the six and sits closest to the ultra-short boundary — it
resolves at 30 s only, and 87–99 % of its long-horizon markout is inherited from the first five
seconds. That is consistent with it being a 500 ms level-consumption classifier.

---

## 12. Final project verdict: **B — longer-horizon information survives but is marginal**

The pre-registered rule for B is "a resolved, stable positive incremental edge after 5 s exists,
but the gross edge stays below a 2 bp round trip". Both halves hold, and neither is close.

**It survives.** Six independent signals, three of them raw book state and three of them frozen
out-of-fold model scores, all show the same profile: perfect decile monotonicity to 60 s, an edge
peaking at 30 s, and a resolved positive increment after the five-second pivot at 10 s, 30 s and
60 s. The increments are 13–28 % of the cumulative edge, block-stable at 0.60–0.78, and confirmed
by the independent anchor sample.

**It is marginal.** The best break-even all-in cost anywhere is 0.693 bp against a grid that
starts at 1 bp one way, so nothing clears anything. The hit rate is 0.57 at the peak horizon and
0.51 at ten minutes. The increment is concentrated in one UTC day and two segments, and is
approximately zero on the last day of the corpus. Nothing at all resolves beyond 120 s, where the
corpus holds 344 independent windows.

Where this sits against the programme:

| phase | question | outcome |
|---|---|---|
| 3 | passive maker break-even | spans break-even to catastrophic across α; α unobservable |
| 4A | can observable state replace α? | no — 9.3 % of depletion print-explained |
| 4B | can sweep risk protect a resting order? | no — indistinguishable from random except at α = 0 |
| 5A | can sweep risk be traded directionally inside 1 s? | information real, 0.87 bp against a ≥ 1 bp hurdle |
| **6** | **does the information survive past 5 s and become material?** | **survives to ~60 s, dies by 120 s, peaks at 0.69 bp** |

Phase 5A closed the sub-second door on cost and left the horizon question open. **This phase
closes it too, and on the same wall.** The information does not vanish when you look further out
— it grows for thirty seconds — but it grows far too slowly to outrun a fixed fee, and it stops
growing long before the horizon at which a 5 bp cost would be a small fraction of the move. The
hoped-for regime, where the move scales with time while the cost stays constant, does not arrive:
by 120 s the edge is already shrinking and unresolvable, and the price begins giving back the
first five seconds.

### Since the verdict is B: what new data would be needed

Not a new model, and not another phase on this corpus. Three inputs, in the order I would change
them.

- **A cheaper venue, for the active line.** The measurement is now bracketed from both ends: the
  best sub-second cell is 0.87 bp (phase 5A) and the best 30-second cell is 0.69 bp (here). An
  all-in round-trip cost under roughly 0.7 bp — about 0.35 bp per side — is the threshold at which
  any of this becomes arithmetic rather than a deficit, and no retail-accessible venue offers it.
  This is a fee-tier and venue-access question, not a research question, and it should be settled
  before any further modelling.
- **An instrument with a larger move-to-cost ratio.** BTCUSDT perpetual has a 1-tick spread 99.8 %
  of the time and 94 % of its 100 ms markouts are exactly zero. The same book state on an
  instrument whose short-horizon moves are several times larger relative to its fees would be
  testing the same hypothesis on data that can actually resolve it. The pipeline built across
  phases 1–6 is instrument-agnostic and would transfer.
- **A longer and more varied corpus, for the horizon question specifically.** 71.4 hours is 344
  independent ten-minute windows and three UTC days of a single 13 % rally. The medium-horizon
  cells here did not resolve, and §9 shows that is a sample-size limit as much as an effect-size
  one. Settling whether anything genuinely lives at 2–10 minutes needs weeks of capture spanning
  a falling and a quiet regime — which the untouched forward AWS captures will eventually provide,
  and which is precisely why they must stay untouched until there is a single hypothesis worth
  spending them on.

An L3/MBO feed remains the right answer for the *passive* line (phases 3, 4A, 4B) and nothing here
changes that. But it would not help the active line at all: this phase's wall is cost and
effect size, not queue observability.

What I would **not** do is add fees, sizing, inventory or a backtest to any of this. Those are
second-order terms on a first-order gap that is still, after a thirtyfold horizon extension,
roughly a factor of one and a half short of the cheapest hurdle in the grid and a factor of
fourteen short of a realistic taker round trip.

---

## 13. Direct answers

**1. Do the regenerated targets agree with the frozen ones at ≤ 5 s?**
Exactly. 1 s and 5 s reproduce the frozen phase 1 columns bit for bit on 2.57 M rows: zero rows
differing, zero censoring disagreements, maximum absolute difference 0.0 ticks. 2 s reproduces
phase 5A's mid-path reconstruction with 16 disagreeing instants, which are precisely the 32
side-rows phase 5A published.

**2. Mean and median signed markout, in ticks and bps?**
Peaks at 30 s for all six signals: 21.1–22.0 ticks, 0.31–0.32 bp. At 1 s it is 7.9–8.6 ticks
(0.11–0.12 bp); at 600 s it is −0.1 to +5.7 ticks (0.00–0.08 bp). Medians and the raw/demeaned
pair for every cell are in `decay_profile.csv`.

**3. Hit rate?**
0.742–0.751 at 1 s, 0.648–0.651 at 5 s, 0.569–0.570 at 30 s, 0.525–0.527 at 120 s, and
0.506–0.510 at 600 s. Monotone decay to a coin flip in every signal.

**4. Do signal-strength deciles order the outcome?**
Perfectly — strictly monotone across all ten deciles — from 1 s to 60 s for all six signals. At
120 s four of six remain strictly monotone. At 300 s none does (ρ 0.81–0.95) and at 600 s the
ordering is gone (ρ −0.12 to +0.67).

**5. Cumulative return by horizon?**
Demeaned decile spread, `obi_l1`: 39.2 → 51.0 → 67.0 → 76.3 → **92.5 (30 s)** → 87.3 → 80.0 →
83.2 → 51.7 ticks. Resolved at every horizon through 300 s; unresolved at 600 s.

**6. Incremental return after 5 s — the main question?**
Real but minority. Resolved, positive and stable at 10 s, 30 s and 60 s for five of six signals
(30 s only for `sweep_dir_p4a`), worth +9 to +27 ticks. **72–88 % of the cumulative long-horizon
markout is inherited from the first five seconds**; 13–28 % is genuinely additional. Nothing
resolves at 120 s or beyond.

**7. Is the long-horizon markout just drift?**
No, but it would look like it untreated. The unconditional mean markout at 600 s is +211 ticks
and the raw bottom decile is +180.6; after 30-minute demeaning the legs separate cleanly to
+19.3 / −32.4. The decile-spread construction and the demeaning each remove drift independently
and the demeaned result is the reported one everywhere.

**8. Up and down separately?**
Both work demeaned, and the down leg persists longer: at 300 s `obi_l1` is +32.8 (top) against
−50.4 (bottom). Raw, both legs are positive from 120 s onward, which is the rally, not the signal.

**9. Stability across UTC days and segments?**
Directions stable at 30-minute block resolution (sign share 0.60–0.78 for every resolved cell),
but concentrated at day and segment resolution. The 30 s incremental edge runs +26.4 / +52.8 /
**+5.1** across the three UTC days and is −0.3 in segment 2:6 and +3.7 in 2:4. On the last day it
is negative at 60 s.

**10. Effective sample size?**
2.13 M evaluated rows but only 213,651 independent anchors at 1 s, 7,108 at 30 s, 1,767 at 120 s
and **344 at 600 s** — 5,946 overlapping rows per independent window. Bootstrap blocks fall from
120 to 30 over the same range.

**11. Does the non-overlapping sample agree?**
Through 60 s, yes, closely. Beyond 120 s, no: at 600 s the anchor spreads run +451 to +1,170 ticks
against full-sample estimates of +24 to +52, on 62–72 rows per estimate. That disagreement is the
reason no long-horizon cell is called resolved.

**12. Break-even cost and gross edge?**
Best cell in the entire study: gross decile spread 1.386 bp, gross edge per trade **0.693 bp**,
break-even all-in cost 0.693 bp — `direction_p2_logistic` at 30 s. Every signal's best cell is at
30 s and lies between 0.649 and 0.693 bp.

**13. Does anything clear the hurdle grid?**
No. **0 of 486 cells** clear the cheapest one-way hurdle of 1 bp; 0 of 486 clear any round trip.
The best cell misses one way by 0.31 bp and a 5 bp taker round trip by 9.31 bp.

**14. Classification of each signal?**
All six are **short continuation**: resolved positive incremental movement after 5 s at horizons
up to 60 s, nothing resolved at 120 s or beyond. None is ultra-short only, medium-horizon
persistent, a reversal, or ambiguous.

**15. Final verdict?**
**B.** Longer-horizon information survives — genuinely, reproducibly, in six independent signals —
but it is marginal: it peaks at 30 s, dies by 120 s, is concentrated in one day of three, and at
its best is 0.693 bp against a grid starting at 1 bp.

**16. Since B, what new data is needed?**
An all-in round-trip cost below roughly 0.7 bp, or an instrument with a materially larger
move-to-cost ratio, or a corpus long enough and varied enough to resolve the 2–10 minute range
that 344 independent windows cannot. §12 gives the ordering. The forward AWS captures are the
natural source for the third, and are exactly why they remain untouched.

---

## 14. Limitations

1. **Development data throughout.** Nothing here is out of sample. All model-derived signals are
   blocked out-of-fold development estimates on the corpus that chose them.
2. **One instrument, one venue, 71.4 hours, three UTC days, six usable segments, one 13–14 %
   rally.** A falling or quiet regime is untested, and §8 shows the increment is already
   day-dependent inside this sample.
3. **344 independent windows at ten minutes.** The medium-horizon cells are unresolved, and that
   is at least partly a sample-size statement rather than an effect-size one. This phase does not
   claim there is nothing at 2–10 minutes; it claims this corpus cannot see it.
4. **Mid-to-mid markouts are an upper bound on any execution.** They charge nothing for the
   spread, queue position, latency, market impact, or for the fact that the counterparty is the
   flow that caused the move. Phase 5A's caution applies unchanged.
5. **The drift control is a 30-minute block mean.** It removes level drift local to half an hour;
   it does not remove any drift component that is itself correlated with the signal inside a
   block. The decile-spread construction is the second, independent defence, and the raw numbers
   are published alongside so the size of the correction is always visible.
6. **The decile spread is a long-short construct.** It is reported as a gross edge and halved for
   the per-trade cost comparison; it is not a strategy and no position, sizing or holding rule
   exists anywhere in this phase.
7. **`sweep_dir_p4a` is a fixed difference of two frozen per-side probabilities.** It is the
   phase 5A direction convention written per instant, and is not the same object as phase 5A's
   per-side thresholded population; the two are not numerically comparable and are not compared.
8. **Segment 2:3 is 117.8 s long** and supports no horizon of 120 s or more. Its short-horizon
   cells are noisy and are reported rather than dropped.

## 15. What stays untouched for forward validation

The rotation-enabled AWS capture file, every later AWS capture, every post-`native_dev_v1` native
capture and the Tardis June/July/August holdout were not opened, loaded, summarised or used to
derive any feature, threshold or hurdle in this phase.

The known pre-existing frozen `passive_binary_sha256` gate in `tests/test_passive_pipeline.py`
still fails and its expected hash was **not** updated. It belongs to an experiment frozen at
commit `628618b` and its failure is independent of this work; a test in this phase asserts the
expected hash is still the original value.

---

## 16. Artifacts and tests

Committed, `research/native_decay_v1/`: `methodology.json` (the pre-registration, with input
hashes and git-tracked source columns), `target_agreement.json`, `decay_profile.csv`,
`signal_deciles.csv`, `decile_monotonicity.csv`, `cumulative_incremental.csv`,
`reconciliation.csv`, `up_down_legs.csv`, `stability.csv`, `effective_sample.csv`,
`cost_hurdle.csv`, `classification.csv`, `verdict.json`, `report.md`.

Heavy, ignored, `data/research/native_decay_v1/`: `decay_frame.parquet` (2,570,379 rows × 50
columns — the grid mid, nine horizons of markout and forward mid, nine anchor flags, six signals,
fold and block identifiers).

**New code**: `native_decay/{__init__,spec,data,analysis,pipeline}.py`,
`tests/test_native_decay.py`, `docs/native_decay_v1.md`. **Modified**: nothing in any earlier
phase. The only reuse from an earlier module is `native_predictive.modeling.block_bootstrap`,
called with this phase's own seed, draws and horizon-aware blocks.

### Tests

`tests/test_native_decay.py`, 34 tests, all pass. The ones the brief asked for:

- **target agreement at ≤ 5 s** — the committed gate shows zero rows differing and zero censoring
  disagreements against the frozen phase 1 columns at 1 s and 5 s, and 2 s within phase 5A's own
  published tolerance; separately, a synthetic grid rising one tick per 100 ms must produce
  exactly +10 ticks at 1 s and the forward mid must be the row ten later;
- **no segment crossing** — on synthetic data the last ten rows of a segment carry NaN and not
  zero at a 1 s horizon, a segment never borrows the next segment's price, and over the real
  2.57 M-row frame every observed markout at every one of the nine horizons satisfies
  `t + h ≤ segment_end`;
- **sign normalisation** — `2p − 1` maps 0.5 to 0; the sweep transform is `p(ask) − p(bid)` and
  mirroring the two sides flips it exactly; a signed markout follows the signal's sign, and
  flipping the signal flips the reported edge; up and down legs are published separately;
- **horizon-aware purge** — `purge_seconds(h) == h + 60` at every horizon, the old flat 60 s
  constant is declared not reused, every evaluated row at every horizon sits at least its own
  purge into its validation block, and the evaluated row count is monotone decreasing in horizon;
- **non-overlap anchors** — anchors are exactly `h`-spaced inside a segment, restart at each
  segment, and are identical whether computed on the full grid or on a filtered subset, so no
  later purge can move them; the anchor count falls monotonically with horizon and is under 1,000
  at ten minutes;
- **cumulative / incremental reconciliation** — `cumulative(h) − (pivot + incremental)` is below
  1e-9 ticks across all 72 published rows, both adjustments and every horizon beyond the pivot are
  covered, and the pivot is asserted to be measured on the horizon's own population (which is
  strictly smaller than the 5 s population, so the distinction is real);
- **no horizon selected as best** — `spec` exposes no primary/best horizon attribute, the
  methodology declares why, and no committed JSON artifact contains `best_horizon`,
  `selected_horizon`, `chosen_horizon` or `optimal`;
- **deterministic rerun** — deciles are identical across repeated calls and exactly balanced,
  repeated preparation and analysis of the same rows produce identical spread records and
  identical decile tables, and the bootstrap is seeded at 0 with 500 draws.

Plus: the phase declares zero models trained and prohibits PnL, Sharpe, entry rules and
holding-period optimisation; a recursive walk asserts no artifact claims a development result is
out of sample and no committed CSV header contains `out_of_sample`; no signal name contains a
future-return token and the methodology declares future returns are not used as features; the
bootstrap block length is `max(30 min, 12 h)` and the block count falls with horizon; the
incremental term is exactly zero when every row moves identically after the pivot and exactly
equals the cumulative term when nothing moves before it; a hole in the 100 ms grid raises rather
than being silently shifted across; and the frozen `passive_binary_sha256` is asserted to still be
`c93fb9b2…`, unmodified.

Runs actually performed:

| suite | result |
|---|---|
| C++ `ctest` in `build/cpp` | **6/6 pass** (no C++ was changed in this phase) |
| every Python module except the two needing their own process | **251 tests, 250 pass** |
| `tests.test_native_predictive` (separate process) | **19/19 pass** |
| `tests.test_event_models` (separate process) | **10/10 pass** |

The single failure in the 251 is the known pre-existing frozen `passive_binary_sha256` gate,
untouched. `tests.test_native_predictive` and `tests.test_event_models` need their own processes
because LightGBM and torch load duplicate OpenMP runtimes on macOS and deadlock together — a
pre-existing property of the repository.

## 17. Reproducing

```
python -m pyresearch.native.decay.pipeline frame      # build the decay frame and run the agreement gate
python -m pyresearch.native.decay.pipeline analyse    # every descriptive table
python -m pyresearch.native.decay.pipeline verdict    # classification, verdict and methodology
python -m unittest tests.test_native_decay
```
