# Phase 5A — sweep risk as a directional signal, and what it would cost to trade

Development corpus `research/specs/native_dev_v1.json`, 71.4 hours of native Binance USD-M
capture, BTC $63,999 → $72,940, realised volatility 42.5 % annualised. Every number below is a
**blocked out-of-fold development estimate**. None of it is out of sample. The rotation-enabled
AWS file, every later AWS capture and the Tardis holdout were not opened.

Phase 4B falsified the passive best-touch market-making line. What survived it is a classifier:
the phase 4A sweep model predicts imminent level consumption very well. This phase asks the only
question that classifier can still answer.

> Does predicted level consumption carry economically meaningful information about the direction
> and magnitude of imminent price movement?

**Verdict: A — directional monetisation falsified.** The price information is real, strong,
monotone and stable across every block, day and segment. It is also **an order of magnitude too
small to pay for taker execution**: the best cell in the entire pre-registered grid moves
**0.87 bp**, and the cheapest hurdle in the fixed grid is 1 bp one way. Not one of the 96
threshold × horizon × hurdle cells — nor any of the 240 decile cells — clears even a single
one-way crossing, let alone a round trip. Section 14 gives the reading; sections 1–13 the evidence.

---

## 0. What was done

The phase 4A headline sweep model — `P(trade-through beyond the quote within 500 ms | X_t)`,
LightGBM, feature set `all` — is reused with nothing changed: same target, horizon, features,
folds, parameters and seed. The scores are the ones phase 4B produced at the 100 ms cadence, and
a test asserts every row of this phase's frame carries exactly the score phase 4B published.

**Direction convention.** A best ask under threat of upward consumption implies `+1`; a best bid
under threat of downward consumption implies `-1`. Every markout is
`sweep_direction × (mid(t+h) − mid(t))` in ticks. One consequence is worth stating up front,
because it is the discipline the whole phase rests on: the bid row and the ask row of the same
instant are exact mirrors, so **the pooled directional markout is identically zero**. Every
number below is a redistribution of that zero, not a level. A test asserts the sum is zero to
within 1e-6 over all 4,285,294 rows.

**Population.** 4,285,294 rows — every 100 ms instant in a validation block, both sides. Targets
come from the frozen phase 1 columns; only the 2 s markout and the size of the first mid move are
rebuilt from the phase 3 mid path, and the rebuild is reconciled against the frozen columns first:

| horizon | rows compared | censoring disagreements | rows differing | max difference |
|---|---|---|---|---|
| 100 ms | 4,285,284 | 0 | 32 | 148.5 ticks |
| 250 ms | 4,285,260 | 0 | 32 | 452.5 ticks |
| 500 ms | 4,285,236 | 0 | 32 | 148.5 ticks |
| 1000 ms | 4,285,176 | 0 | 32 | 148.5 ticks |
| 5000 ms | 4,284,696 | 0 | 32 | 148.5 ticks |

32 rows in 4.29 M — sixteen instants, both sides — resolve one event apart inside violent moves.
Censoring agrees exactly. The direction of the first mid move, computed from the mid path,
disagrees with the frozen phase 1 direction column on **2 rows out of 3,781,274**. Two
independently produced artifacts agree to that precision.

One scale note that governs every economic statement: at this corpus's mean mid, **1 tick =
0.0144 bp**, so **1 bp ≈ 69 ticks**.

---

## 1. Does sweep probability rank future directional movement?

Fixed deciles of the out-of-fold score, full opportunity denominator, 428,530 rows each.

| decile | mean score | realised sweep rate (500 ms) | P(next move follows) | median time to next mid move | first move (ticks) | 500 ms markout (ticks) | 1 s | 5 s |
|---|---|---|---|---|---|---|---|---|
| 0 | 0.0009 | 0.2 % | 23.5 % | 6390 ms | −10.4 | −1.65 | −3.21 | −11.39 |
| 1 | 0.0025 | 0.5 % | 36.7 % | 7385 ms | −4.99 | −1.38 | −2.49 | −6.95 |
| 3 | 0.0107 | 1.7 % | 45.9 % | 4735 ms | −2.61 | −2.50 | −3.94 | −7.55 |
| 5 | 0.0381 | 4.7 % | 48.6 % | 2481 ms | −2.11 | −3.93 | −5.54 | −6.95 |
| 7 | 0.1153 | 11.8 % | 56.5 % | 1534 ms | +3.24 | −1.19 | +0.00 | +5.19 |
| 8 | 0.2151 | 20.6 % | 61.8 % | 1033 ms | +7.26 | +2.25 | +5.90 | +15.10 |
| 9 | 0.5571 | 52.4 % | **72.3 %** | **259 ms** | +15.50 | **+16.89** | +21.36 | +29.85 |

Three things are **perfectly monotone**, Spearman = 1.00 across all ten deciles: the realised
sweep rate, `P(next mid move follows the sweep direction)`, and the fraction of 500 ms markouts
that are favourable. The mean directional markout is **not** monotone (Spearman 0.37 at 500 ms):
it is U-shaped, most negative in deciles 4–5.

That shape is not a defect, it is the mechanism. A low score on the ask side is a statement that
*this* side will not be swept — which makes it more likely the **other** side goes, so the
normalised markout is negative. Decile 0 is not the most negative because in decile 0 the mid
usually does not move at all inside 500 ms (median time to the next move: 6.4 seconds). The
bottom is "nothing happens", the middle is "the other side goes", the top is "this side goes".

At the four pre-registered thresholds, on the full denominator:

| threshold | share of opportunities | realised sweep rate | P(next move follows) | median time to next move | 100 ms | 250 ms | 500 ms | 1 s |
|---|---|---|---|---|---|---|---|---|
| — (all) | 100 % | 10.3 % | 50.0 % | 2342 ms | 0.000 bp | 0.000 bp | 0.000 bp | 0.000 bp |
| ≥ 0.30 | 10.1 % | 52.2 % | 72.2 % | 263 ms | 0.107 bp | 0.178 bp | 0.240 bp | 0.304 bp |
| ≥ 0.50 | 4.8 % | 71.0 % | 79.3 % | 89 ms | 0.231 bp | 0.334 bp | 0.404 bp | 0.462 bp |
| ≥ 0.70 | 2.4 % | 88.8 % | 88.0 % | 48 ms | 0.428 bp | 0.560 bp | 0.628 bp | 0.682 bp |
| ≥ 0.90 | **1.5 %** | **97.2 %** | **94.8 %** | **34 ms** | **0.585 bp** | **0.741 bp** | **0.811 bp** | **0.869 bp** |

The answer to the scientific question is unambiguously yes. The answer to the economic question is
in that last column and in section 9.

## 2. Probability or magnitude? (§5)

`E[R] = P(right)·E[size | right] − P(wrong)·E[size | wrong]`, 500 ms, by score band:

| score band | observations | P(right) | P(wrong) | P(no move) | size when right | size when wrong | E[R] ticks | size ratio |
|---|---|---|---|---|---|---|---|---|
| < 0.30 | 3,852,028 | 5.1 % | 8.6 % | 86.2 % | 54.4 | 54.4 | −1.88 | 1.00 |
| 0.30–0.50 | 227,764 | 31.8 % | 14.9 % | 53.3 % | 57.6 | 79.5 | +6.49 | 0.72 |
| 0.50–0.70 | 102,736 | 47.0 % | 18.1 % | 34.9 % | 63.6 | 95.1 | +12.60 | 0.67 |
| 0.70–0.90 | 39,992 | 63.7 % | 21.4 % | 14.9 % | 77.7 | 120.3 | +23.74 | 0.65 |
| ≥ 0.90 | 62,716 | **87.5 %** | 10.8 % | 1.6 % | 77.5 | **105.2** | +56.40 | 0.74 |

**It is entirely probability, and magnitude works against it.** The probability edge rises
monotonically from −0.035 to +0.767. But in every elevated band the move is **larger when the
model is wrong than when it is right** — 105 ticks versus 77 at the top band, a ratio of 0.74.
A high sweep score buys you frequency, not size, and it pays for that frequency with a fatter
adverse tail.

---

## 3. Where does the move actually happen? (§6)

Model-free event study, local receive time, τ = 0 at the event, side-normalised, whole corpus.

| event class | events | τ −500 | τ −250 | τ −100 | τ 0 | τ +25 | τ +50 | τ +100 | τ +500 | τ +5000 | share of move **before** the event (500 ms) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| aggressive trade-through | 2,892,461 | −15.1 | −13.2 | −9.5 | 0 | +17.9 | +36.6 | +65.3 | +84.4 | +104.9 | **15.2 %** |
| level disappearance | 148,309 | −62.4 | −58.0 | −42.6 | 0 | +0.09 | +0.20 | +1.74 | +18.4 | +36.2 | **77.2 %** |
| consumed without a through-print | 22,638 | −75.1 | −63.8 | −36.4 | 0 | +0.08 | +0.31 | +2.92 | +25.6 | +61.9 | 74.6 % |
| stepped away with a through-print | 125,671 | −60.1 | −56.9 | −43.7 | 0 | +0.09 | +0.18 | +1.52 | +17.1 | +31.6 | 77.8 % |
| price improvement | 148,960 | +62.2 | +57.8 | +42.5 | 0 | −0.09 | −0.20 | −1.76 | −18.3 | −36.0 | 77.2 % |

Two readings, and they point in opposite directions.

**The print is early.** Only 15 % of the movement around an aggressive trade-through has happened
by the time the print is on the wire; 85 % is still ahead, and 65 of the 84 ticks arrive within
100 ms. The trade is the cause, and it is observable before the effect.

**The book update is late.** For a level that simply disappears, 77 % of the move is already gone
at τ = 0. That is the phase 2 feed asymmetry again: the depth stream reacts to the trade stream
with a median lag around 60 ms, so a level vanishing is a lagging confirmation of a price that has
already moved.

The mirror symmetry between price improvement and level disappearance (+62.19 versus −62.19 at
τ −500, +42.46 versus −42.46 at τ −100) is a by-product of the shared convention and is exactly
what a correct sign convention should produce: when the best bid steps up, the ask episode ends by
stepping away at the same instant, and the two normalise to opposite signs.

## 4. How much lead time is there? (§7)

Per level episode, the first 100 ms instant where the score crosses a fixed threshold.

**Crossing → first mid move in the implied direction** (never negative: a first passage is
strictly after the crossing):

| threshold | crossings | median | p10 | p90 | 0–25 ms | 25–50 ms | 50–100 ms | > 500 ms |
|---|---|---|---|---|---|---|---|---|
| ≥ 0.30 | 181,996 | 276 ms | 26 ms | 12,636 ms | 9.6 % | 9.7 % | 17.3 % | 42.0 % |
| ≥ 0.50 | 137,113 | 92 ms | 16 ms | 4792 ms | 15.8 % | 14.6 % | 22.6 % | 27.7 % |
| ≥ 0.70 | 91,941 | 50 ms | 9 ms | 766 ms | 27.3 % | 22.6 % | 27.9 % | 11.8 % |
| ≥ 0.90 | 61,349 | **35 ms** | 6 ms | 86 ms | **37.5 %** | 28.8 % | 27.0 % | 3.5 % |

**Crossing → actual level consumption:**

| threshold | median | fraction arriving **at or after** the consumption |
|---|---|---|
| ≥ 0.30 | +26 ms | 39.8 % |
| ≥ 0.50 | −5 ms | 55.0 % |
| ≥ 0.70 | −24 ms | 80.2 % |
| ≥ 0.90 | −32 ms | **97.8 %** |

At the threshold where the signal is precise it is not predicting the sweep, it is **detecting one
that has already printed** — 97.8 % of `p ≥ 0.90` crossings occur after the trade-through. This is
the same shape phase 4B found, and section 3 explains why it is not fatal here: the print leads
the price. The model reads the print in its flow features roughly 32 ms after it lands, and the
mid then moves a median 35 ms later.

The consequence for execution is severe and precise. At `p ≥ 0.90`, 40.8 of the eventual 56.4
ticks — **72 %** — arrive inside the first 100 ms, and the average first mid move alone is 42.3
ticks, **75 % of the whole 500 ms move**. An order that is not filled within roughly one 35 ms
window captures the remainder, not the move.

## 5. What happens when the signal is wrong? (§15)

| threshold | population | share | P(next move follows) | 100 ms | 500 ms | 500 ms in bp |
|---|---|---|---|---|---|---|
| ≥ 0.30 | consumed | 66.4 % | 90.8 % | +16.2 | +33.9 | +0.487 |
| ≥ 0.30 | **not consumed** | 33.6 % | 29.5 % | −5.1 | **−15.6** | −0.223 |
| ≥ 0.50 | not consumed | 23.9 % | 39.6 % | −0.9 | −9.5 | −0.136 |
| ≥ 0.70 | not consumed | 10.6 % | 59.4 % | +9.6 | +5.6 | +0.080 |
| ≥ 0.90 | **not consumed** | **1.0 %** | 85.9 % | +26.3 | **+51.2** | +0.739 |

False positives are **not violent reversals**. They are a mild opposite signal at low thresholds
(−0.22 bp at `p ≥ 0.30`), roughly flat at 0.70, and at `p ≥ 0.90` they are only 1 % of crossings
and still make money — the price moves even when the specific level survives. Whatever kills this
signal, it is not its false positives.

---

## 6. Does sweep risk add anything beyond OBI and flow? (§8, §9)

Same chronological folds as every earlier phase, minus one: the sweep probability is only out of
fold inside a validation block, so the first scored block has no scored past to train on and is
used for training only. Nine folds, 385,678 out-of-fold rows, models fitted on the one-second
decimation used since phase 4A.

**Primary target — next mid-move direction** (base rate 0.500, 344,648 labelled rows):

| feature set | features | logistic ROC AUC | LightGBM | log loss (logistic) | blocks with expected sign |
|---|---|---|---|---|---|
| OBI only | 7 | **0.7371** | 0.7332 | 0.6059 | 108 / 108 |
| sweep probability only | 1 | 0.6549 | 0.6574 | 0.6876 | 108 / 108 |
| book + flow | 68 | 0.7405 | 0.7358 | 0.6001 | 108 / 108 |
| book + flow + sweep | 69 | **0.7419** | 0.7372 | 0.5992 | 108 / 108 |

**Secondary — sign of the 500 ms directional markout** (ties excluded, 76,348 rows):

| feature set | logistic | LightGBM |
|---|---|---|
| OBI only | 0.8591 | 0.8571 |
| sweep only | 0.7717 | 0.7628 |
| book + flow | 0.8652 | 0.8773 |
| book + flow + sweep | 0.8679 | **0.8789** |

**Nested audit.** The sweep probability's incremental contribution, over the named controls
(OBI L1/L5/L10, microprice offset, signed trade flow, depth-flow pressure, event intensity,
spread) and over the full book-and-flow set:

| target | estimator | base | Δ ROC AUC | Δ log loss | blocks positive | bootstrap p05–p95 |
|---|---|---|---|---|---|---|
| next move direction | logistic | controls | **+0.0013** | −0.0014 | 88 % | 0.0018 – 0.0025 |
| next move direction | LightGBM | controls | +0.0024 | −0.0033 | 75 % | 0.0020 – 0.0030 |
| next move direction | LightGBM | book + flow | +0.0014 | −0.0020 | 68 % | 0.0009 – 0.0018 |
| 250 ms sign | LightGBM | controls | **+0.0066** | −0.0148 | 92 % | 0.0066 – 0.0087 |
| 500 ms sign | LightGBM | controls | +0.0046 | −0.0101 | 88 % | 0.0043 – 0.0059 |
| 1 s sign | LightGBM | book + flow | +0.0015 | −0.0023 | 78 % | 0.0014 – 0.0024 |

Every bootstrap interval excludes zero, so the increment is **real**. It is also **+0.001 to
+0.007 AUC** — between one tenth and seven tenths of a point. Residualising the sweep score
against the controls leaves an R² of 0.22–0.48, so it is not simply a linear restatement of them;
but whatever it holds beyond them barely moves a directional forecast.

**The blunt reading: seven imbalance features get 0.737 of the 0.742 that 69 features can reach.**
The directional information in this book is classic order-book imbalance. The sweep model is
mostly a nonlinear repackaging of it, with a small, genuine, and economically irrelevant residue.

LightGBM is not worth its complexity on the primary target — logistic beats it, 0.7419 to 0.7372.
It does earn its place on the 500 ms sign (0.8789 versus 0.8679). (The logistic fits emit the
same lbfgs convergence warning phase 2's did, at the frozen `max_iter`; the parameters were not
touched, per the no-tuning rule.)

## 7. Magnitude model (§10)

| target | feature set | estimator | MAE (ticks) | Spearman | sign accuracy | block worst Spearman |
|---|---|---|---|---|---|---|
| 500 ms | book + flow | ridge | 14.11 | 0.322 | 0.551 | 0.137 |
| 500 ms | + sweep | ridge | 14.21 | **0.347** | 0.607 | 0.141 |
| 500 ms | + sweep | LightGBM | **11.78** | 0.253 | **0.681** | 0.033 |
| 1 s | + sweep | ridge | 23.64 | 0.336 | 0.605 | 0.200 |
| 1 s | + sweep | LightGBM | 21.08 | 0.252 | 0.637 | 0.022 |

R² is 0.04–0.06 and is deliberately not the headline. The honest summary: **rank information about
magnitude exists and is stable (Spearman ≈ 0.33, positive in every block), but the level is barely
predictable.** MAE of 11.8–14.2 ticks sits at the same scale as the target's own mean absolute
value (11.3 ticks). LightGBM's median absolute error is essentially zero because it correctly
predicts "no move" most of the time; its higher sign accuracy and lower Spearman are two faces of
the same behaviour. The tails are heavy throughout and are reported as p5/p95 per prediction
bucket in `magnitude_buckets.csv`.

---

## 8. Conditional versus unconditional (§14)

At `p ≥ 0.90`, 500 ms:

| population | opportunities | share | mean markout | median | in bp |
|---|---|---|---|---|---|
| **unconditional (decision time)** | 62,716 | 100 % | **+56.40** | +44 | **+0.811 bp** |
| conditional on the sweep occurring | 60,965 | 97.2 % | +58.59 | +46 | +0.842 bp |
| conditional on no sweep | 1,751 | 2.8 % | −19.72 | 0 | −0.278 bp |

Every headline number in this report is the unconditional one. The gap at this threshold is small
only because the sweep almost always happens; at `p ≥ 0.30` the same split is +33.9 versus −15.6
ticks against an unconditional +16.8, and reporting the conditional figure there would have
inflated the edge by a factor of two.

## 9. The cost hurdle (§11, §12, §13)

Fixed one-way hurdles in basis points, chosen before any output was seen. The grid brackets the
sensitivity values this repository already carries in its own frozen specs — taker 5.0 bp per
side, maker 2.0 bp per leg — and is **not** taken from any live account. A round trip is twice a
one-way hurdle; that is arithmetic, not an extra grid point.

Maximum all-in execution cost the observed gross movement could absorb:

| threshold | 100 ms | 250 ms | 500 ms | 1 s | band |
|---|---|---|---|---|---|
| ≥ 0.30 | 0.107 bp | 0.178 bp | 0.240 bp | 0.304 bp | **< 1 bp** |
| ≥ 0.50 | 0.231 bp | 0.334 bp | 0.404 bp | 0.462 bp | **< 1 bp** |
| ≥ 0.70 | 0.428 bp | 0.560 bp | 0.628 bp | 0.682 bp | **< 1 bp** |
| ≥ 0.90 | 0.585 bp | 0.741 bp | 0.811 bp | **0.869 bp** | **< 1 bp** |

In ticks and dollars, the best cell is 60.5 ticks — **$6.05 per BTC** — at `p ≥ 0.90` over 1 s.

Against the fixed grid, at the headline 500 ms horizon:

| threshold | gross | net of 1 bp one way | net of 1 bp round trip | net of 5 bp round trip | clears anything? |
|---|---|---|---|---|---|
| ≥ 0.30 | 0.240 bp | −0.76 | −1.76 | −9.76 | no |
| ≥ 0.50 | 0.404 bp | −0.60 | −1.60 | −9.60 | no |
| ≥ 0.70 | 0.628 bp | −0.37 | −1.37 | −9.37 | no |
| ≥ 0.90 | 0.811 bp | **−0.19** | −1.19 | −9.19 | no |

**Every one of the 96 cells in the fixed threshold × horizon × hurdle grid is negative, and so are
all 240 decile cells.** The best cell in the whole study misses the cheapest single one-way crossing by 0.19 bp, and misses a round
trip at the repository's own historical taker assumption by 9.2 bp — a factor of about twelve.

## 10. Stability (§18)

120 chronological 30-minute blocks, block bootstrap, 500 draws, seed 0. No iid intervals anywhere.

| threshold | statistic | blocks | mean | median | worst | best | blocks positive | bootstrap p05–p95 |
|---|---|---|---|---|---|---|---|---|
| ≥ 0.30 | 500 ms markout | 120 | 0.240 bp | 0.279 | 0.123 | 0.430 | **120 / 120** | 0.225 – 0.258 |
| ≥ 0.50 | 500 ms markout | 120 | 0.404 bp | 0.461 | 0.250 | 0.741 | **120 / 120** | 0.382 – 0.430 |
| ≥ 0.70 | 500 ms markout | 120 | 0.628 bp | 0.618 | 0.293 | 0.946 | **120 / 120** | 0.605 – 0.655 |
| ≥ 0.90 | 500 ms markout | 120 | 0.811 bp | 0.728 | 0.363 | **1.111** | **120 / 120** | 0.768 – 0.858 |
| ≥ 0.90 | P(next move follows) | 120 | 0.948 | 0.993 | 0.792 | 1.000 | 120 / 120 | 0.931 – 0.965 |

This is as stable as anything in the project — and that stability is what makes the conclusion
firm rather than uncertain. **Four blocks out of 120 ever exceed 1 bp**, the best at 1.111 bp.
The signal is not sometimes-large-and-sometimes-small; it is reliably about 0.8 bp.

By UTC day at `p ≥ 0.90`: 0.630 / 0.894 / 0.770 bp. By segment: 0.622 / 0.896 / 0.625 / 0.774 /
0.772 / 0.769 bp. Nothing depends on one period.

## 11. Direction and regime (§16, §17)

**Side asymmetry.** The corpus contains a 14 % BTC rally, so this was checked rather than assumed:

| side | threshold | observations | P(next move follows) | 500 ms | 1 s |
|---|---|---|---|---|---|
| threatened ask (upward) | ≥ 0.90 | 31,569 | 94.6 % | **0.852 bp** | 0.928 bp |
| threatened bid (downward) | ≥ 0.90 | 31,147 | 95.0 % | **0.769 bp** | 0.810 bp |

Both directions work. Upward is about 11 % stronger at 500 ms, consistent with the rally, and the
downward leg is not a mirage. The unconditional per-side markouts are ±0.201 ticks — exact
mirrors, as they must be.

**Activity regimes**, `p ≥ 0.90`, 500 ms. Two of the four fixed quantile cuts degenerate at this
threshold because the variables are nearly constant there — the spread is 1 tick in essentially
every high-score row, and depth-event intensity puts 62,449 of 62,716 rows in one bucket. That is
reported rather than worked around. The two that do split:

| regime | bucket | observations | 500 ms markout |
|---|---|---|---|
| realised movement | low | 21,579 | 0.883 bp |
| realised movement | high | 41,137 | 0.773 bp |
| trade intensity | low | 11,061 | 0.675 bp |
| trade intensity | high | 51,521 | 0.841 bp |

The signal is **broad**, not confined to violent activity. If anything it is slightly better when
recent realised movement is low. It does not need a storm to work — it simply never gets big.

---

## 12. Verdict (§23 Q15): **A — directional monetisation falsified**

Not because the information is absent. Because of an arithmetic that no amount of modelling
changes:

- the strongest, most selective, most stable cell in the entire pre-registered grid moves
  **0.87 bp**;
- **72 %** of that arrives inside the first 100 ms, and 75 % of it is the single next mid tick, a
  median **35 ms** after the signal;
- the cheapest hurdle in the fixed grid is **1 bp one way**, and a taker round trip at this
  repository's own historical assumption is **10 bp**;
- so the gap is roughly **an order of magnitude**, and it is stable enough that 120 blocks out of
  120 agree it is a gap.

A signal cannot be executed into by crossing the spread when the entire move is smaller than the
fee for crossing it. And it cannot be captured passively either: capturing it passively means
resting a quote at the touch and being filled by exactly the aggressive print the model is
detecting — which is the adverse-selection mechanism phase 4B falsified, in the same corpus,
using the same model.

That is the whole result. The two ways to monetise a directional signal are to pay for immediacy
or to sell it, and this corpus closes both doors on this signal: the first on cost, the second on
queue position.

---

## 13. Direct answers (§23)

**1. Does sweep probability monotonically rank future directional price movement?**
The realised sweep rate, `P(next move follows the sweep direction)` and the favourable fraction
are monotone across all ten deciles, Spearman 1.00. The *mean* directional markout is not
(Spearman 0.37): it is U-shaped, because a low score is itself a directional statement about the
other side, and the bottom decile is dominated by "nothing moves at all". At the four fixed
thresholds the ranking is clean and strong: 0.107 → 0.585 bp at 100 ms, 0.240 → 0.811 bp at 500 ms.

**2. How much of the eventual move has already occurred when the signal becomes observable?**
Measured from the aggressive print, only **15 %** — the print leads. Measured from a level simply
disappearing, **77 %** — the book lags. Measured from the model's own threshold crossing at
`p ≥ 0.90`, the crossing arrives a median 32 ms *after* the print, and 72 % of the remaining
500 ms move then arrives within 100 ms.

**3. How much lead time exists?**
To the first mid move in the implied direction: median 276 / 92 / 50 / 35 ms at `p ≥` 0.30 / 0.50 /
0.70 / 0.90. At 0.90, 37.5 % of crossings are followed within 25 ms and only 3.5 % after 500 ms.
To the actual consumption: negative at every threshold above 0.30 — 97.8 % of `p ≥ 0.90` crossings
happen after the level has already been traded through.

**4. What is the unconditional directional markout?**
Pooled over everything it is exactly zero, by construction. At `p ≥ 0.90`: 0.585 / 0.741 / 0.811 /
0.869 bp at 100 ms / 250 ms / 500 ms / 1 s, on the full decision-time denominator, from 1.5 % of
opportunities. At `p ≥ 0.30`: 0.107 / 0.178 / 0.240 / 0.304 bp from 10.1 % of opportunities.

**5. Conditional on an actual sweep, what is the price path?**
+17.9 ticks by 25 ms, +36.6 by 50 ms, +65.3 by 100 ms, +84.4 by 500 ms, +104.9 by 5 s, against
only −15.1 ticks in the preceding 500 ms. Conditional on a sweep at `p ≥ 0.90` the 500 ms markout
is +58.6 ticks against the unconditional +56.4 — a 4 % difference, because at that threshold the
sweep happens 97 % of the time.

**6. How damaging are false positives?**
Mildly, and only at low thresholds. At `p ≥ 0.30` the 33.6 % of crossings that never see a
consumption return −0.223 bp. At `p ≥ 0.70` they are +0.080 bp. At `p ≥ 0.90` they are 1.0 % of
crossings and return **+0.739 bp** — the price moves even when that particular level survives.
False positives are not what breaks this.

**7. Does sweep probability add information beyond OBI and recent flow?**
Yes, genuinely, and by almost nothing: **+0.0013 to +0.0066 ROC AUC** over the controls, with every
block-bootstrap interval excluding zero. Seven OBI features reach 0.7371 on the primary target;
adding the whole 68-feature book-and-flow set plus the sweep score reaches 0.7419. Residualising
the sweep score against the controls leaves R² 0.22–0.48, so it is not a linear restatement — but
what it holds beyond them does not move a directional forecast.

**8. Is logistic enough, or does LightGBM add material value?**
Logistic is enough, and on the primary target it is better: 0.7419 versus 0.7372. LightGBM earns
its place only on the 500 ms markout sign, 0.8789 versus 0.8679, and on the magnitude model it
trades Spearman (0.253 versus 0.347) for sign accuracy (0.681 versus 0.607). Nothing here needs a
gradient-boosted model.

**9. What is the gross edge in ticks and bps?**
Best cell in the entire study: **60.5 ticks = $6.05 per BTC = 0.869 bp**, at `p ≥ 0.90` over 1 s,
on 1.5 % of opportunities. At the headline 500 ms horizon: 56.4 ticks = $5.64 = 0.811 bp.

**10. What all-in execution-cost hurdle would eliminate that edge?**
**0.87 bp all-in, at the very best cell** — 0.43 bp per side on a round trip. Every other cell is
smaller. The whole fixed grid starts at 1 bp one way.

**11. Is the gross edge remotely large enough to justify a taker study?**
No. It misses the cheapest one-way hurdle in the grid by 0.19 bp and a realistic taker round trip
by roughly a factor of twelve. Only 4 of 120 chronological blocks ever exceed 1 bp.

**12. Does the answer remain stable across chronological blocks?**
Completely: 120 / 120 blocks positive at every threshold, bootstrap p05–p95 of 0.768–0.858 bp at
`p ≥ 0.90`, worst block 0.363 bp, best 1.111 bp. The stability is what makes the negative answer
firm rather than uncertain.

**13. Does it work in both directions?**
Yes. Upward 0.852 bp, downward 0.769 bp at `p ≥ 0.90` / 500 ms. The rally makes upward stronger by
about 11 %, not by a factor.

**14. Is the signal broad across activity regimes?**
Yes, and slightly stronger in *quiet* conditions: 0.883 bp in the low realised-movement bucket
against 0.773 bp in the high one. Two of the four fixed regime cuts degenerate at `p ≥ 0.90`
because spread and depth-event intensity are nearly constant there; that is reported rather than
patched.

**15. Classification: A — directional monetisation falsified.** See section 12.

**16. Since A: what the project should become, and what new data would be required.**

Five phases now converge on a single diagnosis, and it is not a modelling failure:

| phase | question | outcome |
|---|---|---|
| 3 | passive maker break-even | spans break-even to catastrophic across α; α unobservable |
| 4A | can observable state replace α? | no — 9.3 % of depletion print-explained; lifecycle adds ~nothing over flow |
| 4B | can sweep risk protect a resting order? | no — indistinguishable from deleting fills at random except at α = 0 |
| 5A | can sweep risk be traded directionally? | information real and stable, but 0.87 bp against a ≥ 1 bp hurdle |

The passive side is blocked by an **information** limit: aggregated L2 hides queue position. The
active side is blocked by a **cost** limit: the movement is smaller than the fee. Those are
different walls, and no further work on this corpus with this feed goes through either.

I would keep this as microstructure research and change one input at a time:

- **The feed, for the passive line.** Every wall in phases 3, 4A and 4B is the same wall:
  aggregated L2 with no order identity. An L3 / MBO feed — Binance's own per-order data where
  available, or a venue that publishes it — turns α from a 5 × 5 sensitivity grid into an
  observable. That single change makes every phase 3 and 4A result decidable rather than bounded.
  This is a data-acquisition problem, not a modelling one.
- **The cost, for the active line.** 0.87 bp gross is not a hopeless number; it is a number that
  loses to a 4–5 bp taker fee. It would need a venue or fee tier roughly an order of magnitude
  cheaper, or an instrument whose short-horizon moves are much larger relative to costs. Before
  any of that is worth studying, the honest first step is to establish what the cheapest
  realistically attainable all-in cost actually is — and to note that at the *observed* 0.87 bp it
  would have to be under about 0.4 bp per side, which no retail-accessible venue offers.
- **The horizon.** Everything measured here lives inside one second, where the move is bounded by
  the tick grid and the fee is not. Whether the same book state predicts anything at 10 s to
  10 min — where a 5 bp cost is a small fraction of the move — is a completely different question
  that this corpus can be asked, has not been asked, and would need its own pre-registration.

What I would **not** do is add fees, inventory, sizing or a backtest to the current line. Those
are second-order terms on a first-order gap of roughly ten to one.

**17. Smallest next execution experiment.** Not applicable — the verdict is A. For the record, the
experiment that *would* be justified if the verdict were B or C is the one this phase deliberately
did not run: a fixed-rule, latency-aware fill simulation that asks how much of the 0.87 bp a taker
order could actually capture given the 35 ms median lead, before any fee. On these numbers that
study would be measuring how much of a losing trade is recoverable.

---

## 14. Limitations

- Development data throughout. Nothing here is out of sample, and nothing here has seen forward
  AWS or Tardis data.
- One instrument, one venue, 71.4 hours, three UTC days, six segments, one 14 % rally at 42.5 %
  annualised realised volatility. A quieter or a falling regime is untested.
- The directional markout is a mid-to-mid move. It is an upper bound on what any execution could
  capture: it charges nothing for the spread, for queue position, for latency, for market impact,
  or for the fact that the counterparty on the other side of a sweep is precisely the informed
  flow that caused it.
- Cost hurdles are fixed sensitivities in basis points. They are not a live account fee tier, and
  the repository's own historical values (taker 5.0 bp per side, maker 2.0 bp per leg) are cited
  as reference points, not as this user's fees.
- Models are fitted on the one-second decimation used since phase 4A and scored on it; the
  descriptive tables use every 100 ms row. Neighbouring 100 ms rows inside one level episode are
  near-duplicates, which is the same reasoning phase 4A recorded.
- The directional models run on nine folds, not ten: the sweep probability is only out of fold
  inside a validation block, so the first scored block is training-only.
- Two of the four fixed regime cuts degenerate at high sweep scores because spread and depth-event
  intensity are nearly constant there.
- 32 rows in 4.29 M resolve one event apart between the phase 1 exporter and the phase 3 mid path,
  always inside violent moves. The frozen phase 1 columns are used wherever they exist.

## 15. What stays untouched for forward validation

The rotation-enabled AWS capture file, every later AWS capture, every post-`native_dev_v1` native
capture and the Tardis June/July/August holdout were not opened, loaded, summarised or used to
derive any feature, threshold or hurdle in this phase.

The known pre-existing frozen `passive_binary_sha256` gate in `tests/test_passive_pipeline.py`
still fails and its expected hash was **not** updated. It belongs to an experiment frozen at
commit `628618b` and its failure is independent of this work.

---

## 16. Artifacts and tests

Committed, `research/native_directional_sweep_v1/`: `methodology.json`, `folds.csv`,
`frame_qc.json`, `sweep_deciles.csv`, `decile_monotonicity.csv`, `event_study.csv`,
`signal_lead_time.csv`, `false_positive_analysis.csv`, `direction_model_comparison.csv`,
`incremental_information.csv`, `residual_diagnostic.csv`, `magnitude_model.csv`,
`magnitude_buckets.csv`, `calibration.csv`, `fold_metrics.csv`, `gross_edge.csv`,
`probability_magnitude.csv`, `conditional_vs_unconditional.csv`, `cost_hurdle.csv`,
`break_even_cost.csv`, `side_asymmetry.csv`, `activity_regimes.csv`, `block_stability.csv`,
`block_stability_summary.csv`, `day_stability.csv`, `segment_stability.csv`, `report.md`.

Heavy, ignored, `data/research/native_directional_sweep_v1/`: `directional_frame.parquet`
(4,285,294 rows × 116 columns), `event_paths.parquet` (3.3 M aligned event paths),
`episode_crossings.parquet`.

**New code**: `native_directional/{__init__,spec,data,events,signal,analysis,models,pipeline}.py`,
`tests/test_native_directional.py`. **Modified**: nothing in any earlier phase.

### Tests

`tests/test_native_directional.py`, 35 tests. The ones that matter:

- a threatened ask normalises `+1` and a threatened bid `-1`; a bid row and an ask row of the same
  instant are exact mirrors; and over the real 4.29 M-row frame the pooled directional markout
  sums to zero, which no sign error could survive;
- the signed size of the first mid move carries the same sign as the frozen phase 1 direction
  column, on synthetic data and, at 2 disagreements in 3.78 M, on the real corpus;
- a horizon that would leave its segment is censored rather than zeroed, checked both on synthetic
  data and by asserting over the whole frame that every observed markout satisfies
  `t + h ≤ segment_end`;
- the first-passage search is strictly after its query instant, finds the first strictly
  greater/smaller mid, and censors when the direction never resolves inside the segment;
- event-study taus are measured from the event instant in receive time and censored outside the
  event's own segment; a bid-side event normalises the other way;
- conditional and unconditional populations are separate labelled rows, the two conditional
  populations partition the unconditional one exactly, and a high-score population keeps its share
  of the full denominator;
- false positives remain in the published table and its two populations sum to one;
- no cost hurdle appears in any model's feature list, every fixed hurdle is reported for every row,
  and break-even equals the gross movement itself with the correct round-trip halving;
- every scored row lies inside its own fold's validation window, the purge exceeds the sweep
  horizon, the scores are byte-comparable to the ones phase 4B published, and the directional folds
  start exactly one block after the scored span begins;
- the published threshold and hurdle grids equal the pre-registered ones;
- repeated target construction and decile assignment are identical.

Runs actually performed:

| suite | result |
|---|---|
| C++ `ctest` in `build/cpp` | **6/6 pass** |
| every Python module except the two needing their own process | **217 tests, 216 pass** |
| `tests.test_native_predictive` (separate process) | **19/19 pass** |
| `tests.test_event_models` (separate process) | **10/10 pass** |

The single failure is the known pre-existing frozen `passive_binary_sha256` gate, untouched.
`tests.test_native_predictive` and `tests.test_event_models` need their own processes because
LightGBM and torch load duplicate OpenMP runtimes on macOS and deadlock together — a pre-existing
property of the repository.

## 17. Reproducing

```
python -m pyresearch.native.directional.pipeline frame        # the 4.29 M row directional frame
python -m pyresearch.native.directional.pipeline descriptive  # deciles, decomposition, hurdles, stability
python -m pyresearch.native.directional.pipeline events       # raw event-time study
python -m pyresearch.native.directional.pipeline signal       # lead time and false positives
python -m pyresearch.native.directional.pipeline models       # comparison, incremental audit, magnitude
```
