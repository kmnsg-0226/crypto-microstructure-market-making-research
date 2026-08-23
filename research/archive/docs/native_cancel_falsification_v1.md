# Phase 4B — counterfactual sweep-risk cancel / stay falsification

Development corpus `research/specs/native_dev_v1.json`, 71.4 hours of native Binance USD-M
capture. Every number below is a **blocked out-of-fold development estimate**. None of it is out
of sample. The rotation-enabled AWS file, every later AWS capture and the Tardis holdout were not
opened.

The question, and the only question:

> For an order that is already resting, does cancelling when observable out-of-fold sweep
> probability becomes high remove more adverse markout than favourable markout?

No entry rule, no requote, no re-entry, no inventory, no fees, no rebates, no spread capture, no
Sharpe, no PnL. A cancelled order simply disappears.

**Verdict: A — falsified, in the form tested.** Under the conservative and midpoint queue
assumptions the cancel signal is statistically *indistinguishable from deleting fills at random*,
and by several measures slightly worse. It is genuinely informative only at the extreme
optimistic queue bound, where most of the crossings still arrive after the fill. Section 14 gives
the full reading; sections 1–13 give the evidence.

---

## 0. What was done

The phase 4A headline sweep model — `P(trade-through beyond the quote within 500 ms | X_t)`,
LightGBM, feature set `all`, ROC AUC 0.879, PR lift 5.5x, expected sign in 120/120 blocks — is
reused unchanged as the cancel signal. Nothing about it was refitted to this phase's outcome.

Phase 4A fitted that model on a one-second decimation of the lifecycle grid. A resting order
cannot wait a second to react when the median best level lives 204 ms, so the identical model was
re-derived fold by fold from the identical training rows and asked for predictions on the **100 ms**
lifecycle rows inside each fold's validation window. `score_provenance.csv` compares the refit
against phase 4A's stored out-of-fold values on the one-second rows they share:

| folds | rows compared | max absolute difference |
|---|---|---|
| 10 | 428,522 | **5.0e-11** |

The stored phase 4A artifact is written to ten significant digits, so 5e-11 is that file's own
printing precision. The model is the same model.

The placements are the frozen phase 3 grid cohort, unchanged: one hypothetical 5-lot order per
side per second at whatever the touch happens to be, observed for 30 s. The counterfactual needs
no new replay. The hypothetical order does not influence the book, so cancelling it can only ever
*remove* the execution the never-cancel path already recorded. The never-cancel fill time is
known exactly; a cancellation that becomes effective before it removes it, one that becomes
effective at or after it does not.

**Cohort**: 428,534 placements — every phase 3 placement whose instant falls inside a validation
block. The 85,544 earlier placements sit in the initial training blocks and were excluded rather
than scored by a model that had seen them.

---

## 1. When a resting order can be advised at all

A decision instant is a 100 ms lifecycle row **strictly after** the placement instant, same side,
same segment, while the order's own quote price is still the best price on its side. Requiring
`t > T0` is deliberate: acting on the placement-instant score would be a quote / no-quote
decision, and entry filtering is not what this phase tests.

| | |
|---|---|
| Median decision instants per order | 35 |
| Mean | 83.7 |
| Orders with no decision instant at all | **5.5 %** |
| Median scored window | 4.5 s of a 30 s observation window |
| Mean scored fraction of the window | **31.6 %** |

Once the order's price stops being best, a level-sweep score no longer refers to the order's
level and no decision is taken. This is a real limit of aggregated L2 and it is left visible
rather than filled in with an assumption. It is not, on its own, damaging: for a resting bid, the
best bid moving *above* the quote means the order is now behind the touch and safer, and the case
where the level is swept is the case where the order has already filled.

---

## 2. The never-cancel baseline

| queue cell | (α, β) | opportunities | fills | fill rate | mean 1 s markout | median | p1 | p5 | favourable | cat 25 | cat 50 | cat 100 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| conservative | (1.0, 0.0) | 428,534 | 276,955 | 64.6 % | **−56.94** | −41.5 | −323.5 | −193.5 | 12.8 % | 66.3 % | 44.1 % | 19.9 % |
| midpoint | (0.5, 0.5) | 428,534 | 305,062 | 71.2 % | −42.11 | −26.5 | −299.5 | −174.5 | 32.9 % | 51.2 % | 34.1 % | 15.4 % |
| optimistic | (0.0, 1.0) | 428,534 | 414,222 | 96.7 % | −5.93 | +0.5 | −210.5 | −96.5 | 79.1 % | 15.7 % | 10.5 % | 4.7 % |

These are sensitivity bounds under aggregated-L2 uncertainty, not estimates of queue position,
and none of them is "best".

**The fact that governs everything that follows**: the baseline is overwhelmingly loss-making in
quote-relative ticks. Conservative carries 17,381,933 ticks of negative markout against 1,613,046
of positive — a ratio of **10.78 : 1**. Midpoint is 7.59 : 1, optimistic 1.64 : 1.

---

## 3. The trap this phase had to avoid, and the benchmark that avoids it

Because the baseline is 10.78 : 1 adverse, **any** rule that removes fills removes far more
negative than positive markout. Cancelling every order would "preserve" 36.80 ticks per
opportunity at the conservative cell. A raw ratio of adverse-avoided to favourable-sacrificed is
therefore not evidence of anything: a coin flip scores 10.78.

So every economic cell is also compared against a **size-matched non-informative benchmark**: a
rule that removes the same number of baseline fills, chosen without information, and therefore
takes a proportional share of both sides of the baseline distribution.

- `avoidance_efficiency_lift` = (adverse ticks avoided / favourable ticks sacrificed) ÷ 10.78
- `net_markout_lift_over_random` = net markout preserved ÷ the same-size random rule's net

A value of 1.00 means "no better than deleting fills at random". This is not a new grid or a
selected threshold; it is the correct denominator for the question that was asked.

---

## 4. The headline cells

The 27 pre-registered cells, fixed before any output was looked at.

| queue cell | p_cancel | latency | cancel rate | net ticks / opportunity | **net lift vs random** | efficiency lift | cat-25 lift | cancel too late |
|---|---|---|---|---|---|---|---|---|
| conservative | 0.30 | 0 ms | 34.6 % | +9.65 | **0.95** | 0.78 | 0.97 | 45.5 % |
| conservative | 0.30 | 50 ms | 34.6 % | +8.67 | **0.94** | 0.77 | 0.96 | 50.7 % |
| conservative | 0.30 | 100 ms | 34.6 % | +7.92 | **0.94** | 0.75 | 0.95 | 54.6 % |
| conservative | 0.50 | 0 ms | 24.7 % | +3.02 | **0.93** | 0.55 | 0.94 | 76.6 % |
| conservative | 0.50 | 50 ms | 24.7 % | +2.52 | **0.92** | 0.53 | 0.92 | 80.2 % |
| conservative | 0.50 | 100 ms | 24.7 % | +2.16 | **0.90** | 0.50 | 0.91 | 82.7 % |
| conservative | 0.70 | 0 ms | 20.1 % | +0.46 | **0.91** | 0.38 | 0.97 | 95.6 % |
| conservative | 0.70 | 50 ms | 20.1 % | +0.35 | **0.92** | 0.37 | 0.96 | 96.6 % |
| conservative | 0.70 | 100 ms | 20.1 % | +0.26 | **0.89** | 0.33 | 0.92 | 97.4 % |
| midpoint | 0.30 | 0 ms | 34.6 % | +5.95 | **0.90** | 0.68 | 0.95 | 53.7 % |
| midpoint | 0.30 | 100 ms | 34.6 % | +4.29 | **0.85** | 0.62 | 0.92 | 64.9 % |
| midpoint | 0.50 | 0 ms | 24.7 % | +1.63 | **0.89** | 0.48 | 0.97 | 82.3 % |
| midpoint | 0.50 | 100 ms | 24.7 % | +0.91 | **0.78** | 0.41 | 0.89 | 88.7 % |
| midpoint | 0.70 | 0 ms | 20.1 % | +0.23 | **0.85** | 0.35 | 1.06 | 96.8 % |
| midpoint | 0.70 | 100 ms | 20.1 % | +0.10 | **0.68** | 0.28 | 0.89 | 98.3 % |
| optimistic | 0.30 | 0 ms | 34.6 % | +1.18 | **3.84** | 1.62 | 2.45 | 85.1 % |
| optimistic | 0.30 | 50 ms | 34.6 % | +0.83 | **3.63** | 1.59 | 2.33 | 88.9 % |
| optimistic | 0.30 | 100 ms | 34.6 % | +0.60 | **3.44** | 1.52 | 2.27 | 91.5 % |
| optimistic | 0.50 | 0 ms | 24.7 % | +0.33 | **4.46** | 1.37 | 2.90 | 95.0 % |
| optimistic | 0.50 | 50 ms | 24.7 % | +0.20 | **3.98** | 1.25 | 2.73 | 96.5 % |
| optimistic | 0.50 | 100 ms | 24.7 % | +0.13 | **3.47** | 1.09 | 2.60 | 97.5 % |
| optimistic | 0.70 | 0 ms | 20.1 % | +0.02 | **2.55** | 0.78 | 3.32 | 99.3 % |
| optimistic | 0.70 | 50 ms | 20.1 % | +0.01 | **1.72** | 0.71 | 3.00 | 99.5 % |
| optimistic | 0.70 | 100 ms | 20.1 % | +0.00 | **0.71** | 0.65 | 2.72 | 99.6 % |

Every net-ticks column is positive. Every conservative and midpoint lift is **below 1.00**. Those
two statements are the whole result: the positive numbers come from filling less, not from
filling better.

---

## 5. What actually survives the cancel

The cleanest single refutation is what happens to the fills that are still there.

**Conservative (α = 1, β = 0), zero latency:**

| policy | cancel rate | surviving fill rate | surviving mean | surviving p1 | surviving favourable |
|---|---|---|---|---|---|
| never cancel | — | 64.6 % | **−56.94** | −323.5 | 12.82 % |
| p ≥ 0.10 | 59.1 % | 22.1 % | **−58.96** | −356.5 | 11.35 % |
| p ≥ 0.30 | 34.6 % | 46.8 % | −58.04 | −325.2 | 11.90 % |
| p ≥ 0.50 | 24.7 % | 58.9 % | −57.31 | −321.5 | 12.44 % |
| p ≥ 0.90 | 16.7 % | 64.6 % | −56.94 | −323.5 | 12.82 % |

Cancelling on high sweep risk makes the surviving book of fills **worse**, not better: the mean
falls from −56.94 to −58.96 and the 1st percentile from −323.5 to −356.5, while the favourable
share drops from 12.8 % to 11.3 %. The signal is removing fills that were better than the cohort
average.

Catastrophic exposure per opportunity does fall (0.4286 → 0.1499 at p ≥ 0.10) — but the fill rate
falls from 64.6 % to 22.1 % at the same time. Per surviving fill, the cat-25 rate goes 66.3 % →
67.8 %: slightly up.

**Optimistic (α = 0, β = 1), zero latency:**

| policy | cancel rate | surviving fill rate | surviving mean | surviving p1 | surviving favourable |
|---|---|---|---|---|---|
| never cancel | — | 96.7 % | **−5.93** | −210.5 | 79.09 % |
| p ≥ 0.10 | 59.1 % | 76.3 % | **−3.73** | −204.5 | 83.18 % |
| p ≥ 0.30 | 34.6 % | 91.5 % | −4.98 | −205.5 | 80.84 % |
| p ≥ 0.50 | 24.7 % | 95.4 % | −5.67 | −208.5 | 79.62 % |

Here the intervention does what a protective rule is supposed to do: the surviving mean improves,
the favourable share rises, the left tail moves in. The effect is small in absolute ticks because
the optimistic baseline is nearly break-even to begin with.

---

## 6. Does sweep risk rank passive-order toxicity? (§18)

Fixed decile study of the out-of-fold score at the placement instant. Descriptive only — this is
explicitly **not** an entry rule.

**Conservative:**

| decile | mean score | fill prob. | trade-through prob. | mean 1 s markout | cat-25 **per fill** | cat-25 **per opportunity** |
|---|---|---|---|---|---|---|
| 0 | 0.0009 | 0.262 | 0.002 | −49.63 | **0.692** | 0.181 |
| 2 | 0.0053 | 0.440 | 0.010 | −54.07 | 0.689 | 0.303 |
| 4 | 0.0209 | 0.653 | 0.030 | **−60.45** | 0.694 | 0.453 |
| 6 | 0.0657 | 0.790 | 0.075 | −61.20 | 0.676 | 0.535 |
| 8 | 0.2133 | 0.892 | 0.208 | −56.84 | 0.632 | 0.564 |
| 9 | 0.5537 | 0.951 | 0.526 | **−51.12** | **0.604** | 0.574 |

The score ranks **fill probability** (0.26 → 0.95) and **trade-through probability** (0.002 →
0.526) beautifully and monotonically. It ranks catastrophic exposure *per opportunity*
monotonically too — but that is mostly the fill probability again.

Conditional on filling it does **not** rank toxicity. Mean markout is hump-shaped, worst in the
middle deciles, and the catastrophic rate among fills *falls* from 0.692 to 0.604 across the
score. This is exactly the asymmetry that kills the cancel policy: the states the model flags are
the states where you were going to fill anyway, and those fills are not the worse ones.

**Optimistic** behaves the way the intervention needs:

| decile | fill prob. | mean 1 s markout | cat-25 per fill | favourable |
|---|---|---|---|---|
| 0 | 0.911 | **+1.47** | 0.027 | 96.1 % |
| 4 | 0.969 | −1.22 | 0.104 | 86.0 % |
| 9 | 0.989 | **−28.91** | **0.457** | 41.2 % |

At α = 0 the fill probability is already saturated (0.91 → 0.99), so the score has nowhere to
express itself except through the markout — and it does, monotonically, across every decile.

**The mechanism of the whole phase, in one sentence**: at the back of the queue the sweep score is
a fill-probability model; at the front of the queue it is a toxicity model.

---

## 7. Does the warning arrive in time? (§19)

Lead time = never-cancel fill instant − first threshold crossing. Negative means the crossing
happened *after* the fill.

**Conservative, share of crossings that arrive at or after the fill:**

| population | p ≥ 0.30 | p ≥ 0.50 | p ≥ 0.70 |
|---|---|---|---|
| all fills | 45.5 % | **76.6 %** | **95.6 %** |
| adverse fills | 46.7 % | 78.0 % | 95.9 % |
| catastrophic 25 | 47.7 % | **78.8 %** | 96.0 % |
| catastrophic 50 | 46.7 % | 77.6 % | 95.7 % |
| catastrophic 100 | 44.5 % | 74.5 % | 94.5 % |

Median lead time at p ≥ 0.50 is **−22 ms**: the median crossing happens after the fill. At p ≥ 0.30
the median is +42 ms and 38.5 % of crossings lead by more than 250 ms — but only 50.7 % of fills
get a crossing at all, so roughly one adverse fill in five is genuinely forewarned by a quarter of
a second.

Optimistic is worse still: 85.1 % / 95.0 % / 99.3 % of crossings arrive at or after the fill at
p ≥ 0.30 / 0.50 / 0.70.

## 8. Advance warning or concurrent signature? (§20)

For each fill, whether the score was above the threshold at the **last** decision instant before
it, and for how long that run had been going.

**Conservative:**

| threshold | above at last instant | of those, a single 100 ms observation | mean consecutive observations | median run duration |
|---|---|---|---|---|
| 0.30 | 19.1 % | 52.8 % | 2.45 | 0 ms |
| 0.50 | 8.5 % | **73.7 %** | 1.49 | 0 ms |
| 0.70 | 3.9 % | **92.2 %** | 1.11 | 0 ms |

At the thresholds where the signal is precise it is a **concurrent signature of a sweep already
under way**, not a warning: a single 100 ms observation, lighting up as the print lands. At
optimistic the catastrophic subpopulation looks better (29.7 % above threshold at the last instant
at p ≥ 0.30, mean run 2.3 observations, median warning 112 ms) but the same collapse happens as the
threshold rises.

No persistence filter was invented as a policy. This is descriptive.

---

## 9. Latency (§9)

Protection decays smoothly and never reverses sign, but it decays.

| queue cell | p ≥ 0.30, net lift vs random | 0 ms | 25 ms | 50 ms | 100 ms |
|---|---|---|---|---|---|
| conservative | | 0.95 | — | 0.94 | 0.94 |
| midpoint | | 0.90 | — | 0.87 | 0.85 |
| optimistic | | 3.84 | — | 3.63 | 3.44 |

Latency is not what kills this. The conservative cell was already at 0.95 with a perfect,
zero-latency, no-slippage cancel. The falsification criterion "protection disappears at 50–100 ms
latency" is **not** the one that triggers.

---

## 10. Queue-assumption transport (§22)

The phase 2 toxicity model failed to transport across α. The sweep model transports its
*classification* — it is the same model, scoring the same observable state — but its **economic
value does not**.

| p_cancel | efficiency lift, conservative | midpoint | optimistic |
|---|---|---|---|
| 0.10 | 1.15 | 0.99 | 1.48 |
| 0.20 | 0.95 | 0.82 | 1.69 |
| 0.30 | 0.78 | 0.68 | 1.62 |
| 0.50 | 0.55 | 0.48 | 1.37 |
| 0.70 | 0.38 | 0.35 | 0.78 |
| 0.90 | 0.16 | 0.19 | 0.51 |

`net_markout_lift_over_random` is below 1.00 in **all 36 conservative cells and all 36 midpoint
cells** of the full grid, and above 1.00 in 30 of 36 optimistic cells. The pre-registered
falsifier "only α = 0 works" is triggered exactly as written.

---

## 11. Stability (§23, §24)

30-minute chronological blocks, block bootstrap, 500 draws, seed 0. No iid intervals anywhere.

| queue cell | p_cancel | latency | blocks | block mean lift | median | worst | share of blocks with positive net | bootstrap p05–p95 |
|---|---|---|---|---|---|---|---|---|
| conservative | 0.30 | 0 ms | 120 | 0.842 | 0.869 | 0.398 | 100 % | **0.818 – 0.867** |
| conservative | 0.50 | 0 ms | 116 | 0.805 | 0.801 | −0.044 | 99.1 % | 0.755 – 0.852 |
| conservative | 0.70 | 100 ms | 84 | 0.803 | 0.720 | −2.27 | 91.7 % | 0.679 – 0.942 |
| midpoint | 0.30 | 0 ms | 120 | 0.767 | 0.783 | 0.063 | 100 % | 0.731 – 0.804 |
| midpoint | 0.50 | 100 ms | 116 | 0.672 | 0.626 | −0.366 | 94.8 % | 0.596 – 0.755 |
| optimistic | 0.30 | 0 ms | 120 | **9.63** | 4.22 | −13.4 | 98.3 % | **5.92 – 14.69** |
| optimistic | 0.50 | 100 ms | 112 | 5.34 | 2.94 | −47.8 | 83.0 % | 3.25 – 7.76 |

The conservative and midpoint bootstrap intervals lie **entirely below 1.00**. The result is not
noise: the cancel signal is reliably, measurably *worse* than random there. The optimistic
intervals lie entirely above 1.00.

Every UTC day and every one of the six segments repeats the same split. Net lift over random at
`p ≥ 0.30`, zero latency:

| | by UTC day (3) | by segment (6) |
|---|---|---|
| conservative | 0.89 – 0.97 | 0.81 – 0.94 |
| midpoint | 0.82 – 0.91 | 0.74 – 0.87 |
| optimistic | 2.65 – 9.00 | 2.31 – 8.20 |

No period carries the effect on its own, and no period rescues the conservative cell.

---

## 12. Structural ordering across the grid (§17)

Perfectly monotone, in every queue cell, Spearman = ±1.00 over the nine fixed thresholds:

- cancel rate falls as `p_cancel` rises,
- surviving catastrophic exposure per opportunity rises as `p_cancel` rises,
- favourable fills sacrificed falls,
- adverse markout avoided falls.

The mechanics behave exactly as designed. But `net_markout_lift_over_random` is **not** monotone
(Spearman −0.98 / −0.30 / −0.32), and at conservative it is highest at the loosest threshold, which
is where the rule most resembles "cancel everything". A clean structural ordering with no
informational content is precisely the outcome §26 was written to refuse to accept.

## 13. Which fills does the cancel protect against? (§21)

At p ≥ 0.50, zero latency:

| queue cell | mechanism | baseline fills | prevented | too late | efficiency lift vs random |
|---|---|---|---|---|---|
| conservative | at quote | 58,358 | 5,897 | 77.0 % | **0.88** |
| conservative | trade-through | 218,597 | 18,493 | 76.5 % | **0.96** |
| optimistic | at quote | 400,545 | 5,218 | 95.0 % | **6.42** |
| optimistic | trade-through | 13,677 | 127 | 89.9 % | **−1.51** |

Two things worth stating plainly. First, at the conservative cell the cancel is no more timely
against trade-through annihilation than against at-quote fills — 76.5 % versus 77.0 % too late —
and is below random on both. Second, where the signal *does* pay, at α = 0, it pays on **toxic
at-quote fills** (lift 6.42), not on trade-through (lift −1.51, i.e. worse than random). A model
trained to predict trade-through earns its keep, when it earns it at all, by identifying states
where the mid is about to run and the at-quote fill will be adverse — not by dodging the sweep it
was named after.

---

## 14. Verdict against the pre-registered criteria (§26)

| supporting criterion | met? |
|---|---|
| increasing sweep risk ⇒ increasingly adverse fill outcomes | **partly** — per opportunity yes, conditional on filling no at α = 1 |
| cancellation removes materially more negative than positive markout over a broad threshold range | **no** — not once measured against a size-matched random rule |
| the behaviour survives non-zero latency | yes |
| stable across chronological blocks | yes, stably *below random* at α = 1 and α = 0.5 |
| does not depend on α = 0 alone | **no** |
| lead time meaningfully positive before a large share of catastrophic fills | **no** — 78.8 % of crossings arrive at or after the fill at p ≥ 0.50 |
| improvement not obtained by cancelling almost every order | **no** — the best conservative cell cancels 59 % of orders and is still only 1.15× random |

| falsifying criterion | triggered? |
|---|---|
| protection disappears at 50–100 ms latency | no |
| favourable markout sacrificed comparable to adverse markout avoided | **yes**, relative to the random benchmark |
| only α = 0 works | **yes** |
| only one narrow threshold works | partly — only the loosest threshold clears 1.0 at α = 1 |
| majority of crossings occur at or after the fill | **yes** at every threshold ≥ 0.50 |
| block stability poor | no — it is stably bad |
| tail only improves when nearly all fills are eliminated | **yes** at α = 1 |

Four of seven falsifiers fire, and three of seven supporting criteria fail outright. The criteria
were not relaxed after the results were seen.

### Answer to §31 Q16: **A — FALSIFIED.**

The first genuinely market-maker-like control action — withdrawing a resting quote ahead of a
predicted sweep — does **not** survive causal, latency-aware, blocked out-of-fold falsification
on this corpus, under the queue assumptions that are not extreme bounds.

There is one honest qualification and it should not be inflated into a rescue. At α = 0, β = 1 the
signal *is* informative: 3.4–4.5× the random benchmark, stable in 98 % of blocks, surviving 100 ms
latency, ranking markout monotonically across all ten deciles, and it protects against toxic
at-quote fills. But α = 0 is the extreme optimistic bound of phase 3 — the assumption that nothing
displayed at the quote is ahead of us and that every unexplained removal advanced our queue. It is
not an estimate of queue position and there is no evidence in this corpus that it is attainable.
Phase 4A already established that aggregated L2 accounts for only 9.3 % of best-level depletion
with prints; that is the same ceiling in a different form.

---

## 15. Direct answers (§31)

**1. Does OOF sweep risk monotonically rank future passive-fill toxicity?**
It monotonically ranks *fill probability* (0.26 → 0.95) and *trade-through probability* (0.002 →
0.53) at every queue cell. It monotonically ranks catastrophic exposure per opportunity. It does
**not** rank toxicity conditional on filling at α = 1: mean markout is hump-shaped and the cat-25
rate among fills falls from 0.692 to 0.604 across the deciles. At α = 0 it does rank it, cleanly
and monotonically.

**2. How much negative 1 s markout can cancellation avoid?**
Up to 11.07 M of 17.38 M ticks (63.7 %) at conservative p ≥ 0.10, falling to 0.014 M (0.08 %) at
p ≥ 0.90. At optimistic, 2.11 M of 6.31 M (33.5 %) at p ≥ 0.10.

**3. How much positive 1 s markout does it sacrifice?**
0.89 M of 1.61 M ticks (55.3 %) at conservative p ≥ 0.10 — a *larger* proportional share than the
negative side. At optimistic p ≥ 0.10, 0.87 M of 3.85 M (22.6 %) against 33.5 % of the negative
side: proportionally smaller, which is what informativeness looks like.

**4. Ratio of adverse ticks avoided to favourable ticks sacrificed?**
Conservative 12.42 (p ≥ 0.10) down to 1.71 (p ≥ 0.90). Midpoint 7.55 → 1.46. Optimistic 2.43 → 0.83.
**Against the size-matched random benchmark those become 1.15 → 0.16, 0.99 → 0.19 and 1.48 → 0.51.**
The raw ratios look impressive only because the baseline is 10.78 : 1 adverse to begin with.

**5. How much cat-25 / cat-50 / cat-100 can be avoided?**
At conservative p ≥ 0.30, exposure per opportunity falls 0.4286 → 0.3142 (cat-25), 0.2851 → 0.2084
(cat-50), 0.1284 → 0.0930 (cat-100). The avoidance lifts over random are 0.97, 0.98 and 1.00 — the
reduction is entirely proportional to the fills removed. At optimistic p ≥ 0.30 the lifts are 2.45,
2.40 and 2.37: a genuine reduction.

**6. How often does cancellation arrive too late?**
Conservative: 12.9 % (p ≥ 0.10), 45.5 % (0.30), 76.6 % (0.50), 95.6 % (0.70), 99.8 % (0.90) at zero
latency; add 8–9 points at 100 ms. Optimistic is 65.4 % → 99.96 % over the same range.

**7. How much warning time exists before catastrophic fills?**
At p ≥ 0.30, conservative cat-25 fills: median +20 ms, p75 +776 ms, 36.1 % lead by more than 250 ms,
47.7 % arrive too late. At p ≥ 0.50 the median is −22 ms. There is a real advance-warning tail, but
it is a minority of a minority.

**8. Does protection survive 25 / 50 / 100 ms latency?**
Yes, in the sense that nothing collapses: net lift moves 0.95 → 0.94 (conservative p ≥ 0.30) and
3.84 → 3.44 (optimistic p ≥ 0.30). Latency is not the binding constraint.

**9. Does protection work at each queue cell?**
No at (1.0, 0.0). No at (0.5, 0.5). Yes at (0.0, 1.0).

**10. Does the sweep signal transport across queue assumptions better than the old toxicity model?**
Its *ranking* transports — it is one model scoring one observable state, and its classification
metrics are stable. Its *economic value* does not: below random at two of three cells, 3.4–4.5×
random at the third. That is a different failure from phase 2's, and no better.

**11. Is protection stable across blocks, days and segments?**
Yes, and that is the problem. Conservative bootstrap intervals sit entirely below 1.00 in every
headline cell, on all three UTC days and in all six segments. The effect is stable and stably
uninformative.

**12. Broad across thresholds or isolated?**
The only conservative cells clearing 1.00 are `p ≥ 0.10` (1.15) and marginally `p ≥ 0.20` (0.95).
Everything from 0.30 upward is below random. That is the narrowest possible support, and it sits
at the threshold that behaves most like cancelling everything.

**13. Is the improvement achieved only by cancelling most orders?**
Yes. The best conservative efficiency lift, 1.15, comes from cancelling 59.1 % of opportunities and
removing 65.8 % of all baseline fills. The ceiling of "cancel everything" is +36.80 ticks per
opportunity; p ≥ 0.10 reaches +23.76 — 65 % of the way there, by removing 66 % of the fills.

**14. Trade-through or toxic at-quote?**
Neither, at α = 1: both mechanisms are below random and both are ~77 % too late. At α = 0 it is
**at-quote** protection (lift 6.42) and it is actively harmful on trade-through (lift −1.51).

**15. Advance warning or concurrent signature?**
Concurrent signature. At conservative p ≥ 0.50, only 8.5 % of fills had the score above threshold at
the last decision instant before the fill, and 73.7 % of those were a single 100 ms observation.

**16. Verdict: A — FALSIFIED.** See section 14.

**17. If A, what should the project become instead?**

The evidence across phases 3, 4A and 4B converges on one structural fact: **on aggregated Binance
L2 the queue position is both unobservable and decisive.** Phase 3 showed the economics span
break-even to catastrophic across α. Phase 4A showed observable lifecycle state cannot recover
queue rank (9.3 % of depletion print-explained) and adds almost nothing over existing flow
features. Phase 4B now shows that the strongest observable signal in the whole programme cannot
protect a resting order once it is committed, because at the back of the queue it predicts *that*
you fill rather than *how badly*.

Three directions follow, in the order I would take them:

- **Change the data, not the model.** Every wall hit in phases 3, 4A and 4B is the same wall:
  aggregated L2 with no order identity. A venue or feed that exposes L3 / MBO — or Binance's own
  per-order data where available — would make α an observable rather than a 5×5 grid. That is the
  single change that would make every earlier result decidable rather than bounded. This is a
  data-acquisition problem, not a modelling one, and it is the honest next move for the
  passive-maker thesis.
- **Stop asking the passive question and ask the directional one.** The sweep model is a good
  classifier: ROC AUC 0.879, PR lift 5.5×, 120/120 blocks, and it ranks fill probability and
  trade-through probability monotonically. What it demonstrably knows is *when a level is about to
  be consumed*. That is a statement about imminent price movement, and it has never been evaluated
  as one. A short-horizon directional or liquidity-taking study using the same frozen folds and
  the same discipline would test the information that actually exists in this corpus, rather than
  the information the market-making frame needed it to have.
- **If the passive frame is kept, move the decision earlier.** Everything in this phase says the
  damage is determined before the order rests. The only remaining passive lever this corpus can
  speak to is *whether to quote at all* at a given instant — which is exactly what §27 and §32
  forbade here, deliberately, and which would have to be pre-registered as its own study with its
  own falsification criteria rather than reached for as a consolation.

I would not add fees, rebates, inventory or requoting to the current line. Those are second-order
terms on a first-order effect that is not there: at the conservative queue assumption the cancel
decision carries no information, and a maker rebate does not create information.

**18. If B, what evidence is missing?** Not applicable — the verdict is A. For completeness, the
evidence that would move it to B is a queue-position observable: if α were measured rather than
bounded, and the measured value sat near the optimistic end, the α = 0 result would become a real
result instead of a bound.

**19. If C, the smallest next experiment.** Not applicable.

---

## 16. Limitations

- Development data throughout. Nothing here is out of sample and nothing here has seen forward
  AWS data.
- One instrument, one venue, 71.4 hours, three UTC days, six segments.
- The counterfactual is exact for a hypothetical order that does not influence the book. A real
  resting order changes displayed depth and can change what other participants do; that effect is
  unmodelled and unmeasurable from this data.
- Partial fills follow the phase 3 convention: an order that never completes is not a fill and
  carries no markout. Cancelling such an order therefore changes nothing in the economics, which
  is conservative for the cancel policy, not against it.
- Decision instants exist only while the order's quote price is still the best on its side —
  31.6 % of the observation window on average. A different signal, defined for a level that is no
  longer at the touch, could in principle cover the rest. This one is not.
- The cancel latencies are fixed sensitivities, not measurements of Binance cancel latency.
- α and β are sensitivity bounds under aggregated-L2 uncertainty, never estimates of queue
  position, and level age is never a queue rank.
- No fee schedule, rebate, spread capture, inventory value or opportunity cost is included by
  design. Section 14 explains why adding them would not change the verdict.

## 17. What stays untouched for forward validation

The rotation-enabled AWS capture file, every later AWS capture, every future native capture and
the Tardis June/July/August holdout were not opened, loaded, summarised or used to derive any
feature or threshold in this phase.

The known pre-existing frozen `passive_binary_sha256` gate in
`tests/test_passive_pipeline.py` still fails and its expected hash was **not** updated. It
belongs to an experiment frozen at commit `628618b` and its failure is independent of this work.

---

## 18. Artifacts and tests

Committed, `research/native_cancel_falsification_v1/`: `methodology.json`, `grid_spec.json`,
`folds.csv`, `score_provenance.csv`, `signal_coverage.json`, `sweep_score_deciles.csv`,
`threshold_latency_surface.csv`, `headline_cells.csv`, `avoided_vs_sacrificed.csv`,
`tail_protection.csv`, `signal_lead_time.csv`, `signal_persistence.csv`,
`mechanism_decomposition.csv`, `queue_transport.csv`, `threshold_monotonicity.csv`,
`block_stability.csv`, `block_stability_summary.csv`, `day_stability.csv`,
`segment_stability.csv`, `report.md`.

Heavy, ignored, `data/research/native_cancel_falsification_v1/`: `decision_frame_file{0,1,2}.parquet`
(5,140,758 rows at 100 ms), `sweep_scores_file2.parquet` (4,285,294 out-of-fold scores),
`decision_timeline.parquet`, `cohort.parquet` — 1.0 GB in total, all of it regenerable from the
stages listed below. Files 0 and 1 lie entirely inside the initial training blocks and therefore
have no out-of-fold scores at all.

**New code**: `native_cancel/{__init__,spec,scoring,counterfactual,analysis,pipeline}.py`,
`tests/test_native_cancel.py`. **Modified**: `native_queue_tail/data.py` — `build_model_frame`
gained optional `step_ms` and `files` arguments, both defaulting to the phase 4A behaviour, so the
same join can be produced at the 100 ms cadence for scoring without changing what any model trains
on.

### Tests

`tests/test_native_cancel.py`, 41 tests. The ones that matter:

- a decision instant is strictly after the placement instant, so the study cannot degenerate into
  an entry filter;
- a fill stamped at the effective cancellation instant **survives** — information observed at `t`
  cannot retract an execution stamped at `t` — and a fill one nanosecond later is prevented;
- a cancellation cannot prevent a fill that already happened, and its lead time is recorded as
  negative;
- effective cancel time is exactly decision time plus the fixed latency, checked across the whole
  latency grid;
- increasing latency can only ever remove protection, never create it;
- a score from another price, another side, another segment or another capture file is never
  joined to an order;
- thresholds produce nested crossing times;
- every order lands in exactly one disposition and every baseline fill is either prevented or
  surviving;
- fill counts reconcile exactly, prevented fills partition into avoided / sacrificed / censored,
  and a cancelled order is never counted as a zero-markout fill;
- an undefined ratio is `NaN`, never infinity;
- the markout sign convention is inherited from phase 3 and a bid and an ask mirror exactly;
- the refit reproduces phase 4A's stored out-of-fold predictions to that artifact's printing
  precision, every scored row lies inside its own fold's validation window, and the purge exceeds
  the sweep horizon;
- the cohort lies entirely inside the validation span, no order or decision crosses a segment
  boundary, α and β are exactly the frozen cells and the cancellation logic never touches them;
- the published grid is exactly the pre-registered one and the surface retains every denominator;
- repeated runs are identical and the timeline is independent of input row order.

`tests/test_native_research.py`, `tests/test_native_economic.py`,
`tests/test_native_queue_tail.py` and `tests/test_native_predictive.py` all still pass unchanged;
the `build_model_frame` signature change defaults to exactly the phase 4A behaviour.

Runs actually performed:

| suite | result |
|---|---|
| C++ `ctest` in `build/cpp` | **6/6 pass** |
| every Python module except the two that need their own process | **182 tests, 181 pass** |
| `tests.test_native_predictive` (separate process) | **19/19 pass** |
| `tests.test_event_models` (separate process) | **10/10 pass** |

`tests.test_native_predictive` and `tests.test_event_models` must each run in their own process:
LightGBM and torch load duplicate OpenMP runtimes on macOS and deadlock when imported together,
which is why `python -m unittest discover` over the whole `tests/` directory hangs. That is a
pre-existing property of the repository, not of this phase.

The single failure in the 182 is the known pre-existing frozen `passive_binary_sha256` gate in
`tests/test_passive_pipeline.py`, from an experiment frozen at commit `628618b`. Its expected hash
was **not** updated.

## 19. Reproducing

```
python -m pyresearch.native.cancel.pipeline frames     # 100 ms decision frames, ~5.1 M rows
python -m pyresearch.native.cancel.pipeline scores     # refit the phase 4A sweep model, score at 100 ms
python -m pyresearch.native.cancel.pipeline timeline   # first-crossing times for the nine fixed thresholds
python -m pyresearch.native.cancel.pipeline surface    # the threshold x latency x queue grid and stability
python -m pyresearch.native.cancel.pipeline signal     # score deciles and signal persistence
```
