# Observable queue dynamics and catastrophic fill risk, native_dev_v1

Phase 4A. Two questions:

- **A** — can observable best-price level dynamics replace the free queue-position parameter that
  dominated the phase 3 economics?
- **B** — can *catastrophic* passive fills be predicted, rather than merely average markout?

No strategy. No quote/no-quote rule, no cancel policy, no inventory, no fee-adjusted EV, no PnL.

All results are **development estimates** on the frozen `native_dev_v1` corpus. Nothing is out of
sample. The rotation-enabled AWS capture, every later AWS capture and the Tardis June–August
holdout were not read.

**A methodological rule applied throughout:** Binance publishes aggregated L2. There are no order
ids, no FIFO rank, and no clean separation of cancellation from execution. Level age is a
*lifecycle observable*, never an inferred queue rank; an unexplained displayed reduction is called
unexplained, never cancelled; and the level-birth cohort in §8 is *not* "front of queue".

Pre-registration and input hashes: `research/native_queue_tail_v1/methodology.json`.

---

## 1. How long do best-price levels survive?

**297,285 level episodes** across the 8 synchronized segments (148,623 bid, 148,662 ask). An
episode starts when a price becomes best on its side and ends when it stops being best; the same
price appearing again later is a new episode, and no episode crosses a segment boundary.

| p10 | p25 | **p50** | p75 | p90 | p99 | mean |
|---|---|---|---|---|---|---|
| 101 ms | 102 ms | **204 ms** | 1,020 ms | 3,469 ms | 25,093 ms | 1,729 ms |

The quantisation is the feed, not the market: the depth stream is batched at 100 ms, so a level
that appears and dies inside one batch is observed as ~101 ms. **At least a quarter of best-price
levels last a single depth batch**, and the median lasts two.

The distribution is extremely heavy-tailed — mean 1.7 s against a median of 0.2 s — which is the
first hint that "the level" is really two populations: transient prices that flicker at the touch,
and durable ones that anchor it.

## 2. How do levels disappear?

Almost exactly evenly, and in two economically opposite ways:

| Close reason | Share | Fully removed | Print-explained depletion | Any print at level | Any trade-through |
|---|---|---|---|---|---|
| `improved` — a better price appears inside | **50.1 %** | 1.3 % | 3.7 % | 66.0 % | 10.3 % |
| `stepped_away` — the book moves through it | **49.9 %** | 100 % | 21.7 % | 90.3 % | 84.7 % |
| `segment_end` — censored | 0.005 % | 0 % | 4.7 % | 75 % | 0 % |

**Half of all best-level "disappearances" are not consumption at all.** They are price
improvement: someone posts inside the spread and the old level stops being best while remaining
in the book, essentially intact (1.3 % fully removed). For a resting passive order that is a
benign event — the order is still there at its price, just no longer at the touch.

The other half is genuine: the level empties (100 % fully removed) and the book steps through it.
85 % of those saw a trade-through print.

This split matters for how §6 should be read: a model of "will this level stop being best" is
predicting a coin flip between two very different events.

## 3. How much depletion can be reconciled with aggressive trades?

**9.3 % of it, corpus-wide.** Of 3,260,124,622 lots removed from best levels, 303,953,955 lots
are accounted for by an aggressive print at that level under the frozen reconciliation rule
(prints account for later reductions, first come first served; future trades never explain an
earlier removal).

The split by fate is the informative part:

| Population | Removed lots | Print-explained | Share |
|---|---|---|---|
| all | 3,260,124,622 | 303,953,955 | **9.3 %** |
| `stepped_away` levels | 1,020,801,447 | 221,755,988 | **21.7 %** |
| `improved` levels | 2,239,144,446 | 82,189,491 | **3.7 %** |

The median episode's removal is **95.2 % unexplained**. This is the single most important number
for the queue question: on aggregated L2, roughly nine tenths of what leaves a best-price level
cannot be attributed to trading. It may be cancellation, execution the trade feed did not line up
with (§ phase 2 measured a ~60 ms median feed lag), or batching. **The data cannot tell them
apart, and this phase does not pretend otherwise** — which is exactly why the phase 3 β axis
existed and why it cannot be collapsed to a point estimate.

## 4. How common is replenishment, and does it help?

**38.3 % of episodes replenish at least once**, and when they do the median is 5 replenishment
events. Replenished quantity is 25.7 % of removed quantity overall.

Replenishment is strongly associated with *survival*, and the association is monotone across the
whole range (`queue_bucket_studies.csv`, deciles of cumulative replenishment):

| Cumulative replenishment decile | P(level ends within 1 s) | P(trade-through within 1 s) |
|---|---|---|
| 0 (none) | 0.538 | 0.326 |
| 2 | 0.276 | 0.141 |
| 4 | 0.203 | 0.104 |
| 6 | 0.125 | 0.057 |
| 8 (most) | **0.059** | **0.025** |

A level that has been repeatedly refilled is roughly **nine times less likely to fail in the next
second** than one that has never been refilled. That is the clearest "healthy queue" signal in the
phase.

But note what it does *not* do: over the same deciles the catastrophic-fill rate moves only from
0.637 to 0.670 and the mean 1 s markout from −54.5 to −48.9. **Replenishment predicts level
survival; it barely touches fill quality.** §7 returns to this.

## 5. The hazard of level failure

A discrete-time hazard table rather than a parametric model (`hazard_curve.csv`): P(level ends in
the next 100 ms | it has survived to age t), bid side:

| Mean age | 57 ms | 318 ms | 1.3 s | 3.1 s | 7.2 s | 31 s | 76 s |
|---|---|---|---|---|---|---|---|
| hazard (next 100 ms) | **0.330** | 0.105 | 0.046 | 0.026 | 0.015 | 0.004 | **0.003** |
| sweep hazard (next 100 ms) | 0.185 | 0.067 | 0.030 | 0.016 | 0.009 | 0.003 | 0.002 |

The hazard falls by **two orders of magnitude** with age and is monotone in every bucket. Levels
are strongly "ageing-resistant": survival predicts survival.

The hazard framing is worth keeping because it says something the fixed-horizon classifications
obscure — the risk is overwhelmingly concentrated in the first few hundred milliseconds of a
level's life, which is precisely when a passive order that joined at level birth is exposed.

---

## 6. Model D — level failure and sweep risk

Blocked out-of-fold, identical fold geometry to phases 2 and 3: 12 wall-clock blocks, validation
on blocks 2–11, 60 s purge, ~428,500 scored placement-side rows per problem. PR AUC is reported
alongside ROC AUC because these targets are imbalanced.

| Target | Base rate | Model | ROC AUC | PR AUC | PR lift | Brier | Block mean | Worst block | Blocks > 0.5 |
|---|---|---|---|---|---|---|---|---|---|
| level disappears ≤ 500 ms | 18.9 % | naive | 0.500 | 0.298 | 1.58× | 0.158 | 0.500 | — | 4/120 |
| | | logistic | 0.852 | 0.645 | 3.42× | 0.105 | 0.749 | 0.660 | 120/120 |
| | | **LightGBM** | **0.860** | **0.670** | **3.55×** | 0.101 | 0.765 | 0.693 | 120/120 |
| level disappears ≤ 1 s | 28.4 % | logistic | 0.842 | 0.714 | 2.51× | 0.138 | 0.716 | 0.637 | 120/120 |
| | | **LightGBM** | **0.848** | **0.727** | **2.56×** | 0.134 | 0.731 | 0.642 | 120/120 |
| trade-through ≤ 500 ms | 10.4 % | logistic | 0.878 | 0.557 | 5.38× | 0.065 | 0.832 | 0.763 | 120/120 |
| | | **LightGBM** | **0.879** | **0.570** | **5.50×** | 0.064 | 0.831 | 0.767 | 120/120 |
| trade-through ≤ 1 s | 15.7 % | logistic | 0.856 | 0.584 | 3.73× | 0.095 | 0.800 | 0.725 | 120/120 |
| | | **LightGBM** | **0.858** | **0.595** | **3.80×** | 0.093 | 0.801 | 0.730 | 120/120 |

**Both are strongly and stably predictable.** Sweep risk is the stronger of the two: a PR AUC of
0.570 against a 10.4 % base rate is a **5.5× lift**, and every one of the 120 half-hour blocks is
above chance. LightGBM's edge over logistic regression is small throughout (≤ 0.8 AUC points),
consistent with phases 2 and 3.

### What the models say

The logistic coefficients are stable in sign across 10/10 folds and describe one coherent state
(`model_coefficients.csv`):

| Feature | Level failure ≤ 1 s | Trade-through ≤ 1 s |
|---|---|---|
| `log_level_age_ms` | **−0.586** | **−0.718** |
| `log_own_depth_l1` / `log_current_qty` | −0.425 | −0.317 |
| `log_opp_depth_l1` | −0.441 | — |
| `log_prints_at_quote` | +0.261 | +0.355 |
| `log_prints_through` | — | +0.306 |
| `bbo_change_count_5000ms` | +0.288 | +0.257 |
| `signed_obi_l1` | — | −0.217 |

**A queue about to fail is: young, thin, already being printed against, in a book whose touch has
been moving.** That is a mechanically plausible description, and it is the one the hazard table in
§5 shows directly.

LightGBM's gain ranking puts trade intensity first for level failure (`trade_count_1000ms` 23.8 %,
the five `trade_count` windows 55 %) and own touch depth first for sweep risk
(`log_own_depth_l1` 28.1 %). Level age matters, but as one term in a state, not on its own.

## 7. Model E — catastrophic fill risk

Among **filled** opportunities only, with placement-time features exclusively: no fill-time book
state, no post-fill information, no later level evolution. Target `catastrophic_25` is
`markout_1s ≤ −25 ticks`, fixed before fitting.

| Queue cell | Target | Base rate | Model | ROC AUC | PR AUC | PR lift | Block mean | Worst block |
|---|---|---|---|---|---|---|---|---|
| conservative α=1, β=0 | catastrophic_25 | 66.3 % | LightGBM | 0.610 | 0.735 | **1.11×** | 0.604 | 0.462 |
| | catastrophic_50 | 44.1 % | LightGBM | 0.607 | 0.524 | 1.19× | 0.565 | 0.409 |
| midpoint α=.5, β=.5 | catastrophic_25 | 51.2 % | logistic | 0.606 | 0.591 | 1.15× | 0.579 | 0.237 |
| | catastrophic_50 | 34.1 % | LightGBM | 0.625 | 0.440 | 1.29× | 0.558 | 0.452 |
| **optimistic α=0, β=1** | catastrophic_25 | 15.8 % | LightGBM | **0.780** | 0.416 | **2.64×** | 0.691 | 0.614 |
| | catastrophic_50 | 10.5 % | LightGBM | **0.784** | 0.313 | **2.99×** | 0.656 | 0.507 |

**The answer depends entirely on the queue assumption, and the reason is mechanical.**

At the back of the queue the model is nearly useless: ROC AUC 0.61, PR lift **1.11×**, and the
worst half-hour block is *below* chance at 0.462. But look at the base rate — **66 % of fills are
already catastrophic**. Conditional on filling from the back of a deep queue you have almost
always been run over, so there is very little left to discriminate. Phase 3 said the same thing
economically; this says it informationally.

At the front of the queue only 15.8 % of fills are catastrophic, and **those are genuinely
predictable**: ROC AUC 0.780, PR lift 2.64×, every one of 120 blocks above chance, worst block
0.614. The gain ranking is concentrated — `trade_count_5000ms` 16.8 %, `log_own_depth_l10` 14.7 %
— rather than the diffuse ranking at the conservative cell where no feature exceeds 8.2 %.

**This is the most encouraging result in the phase**, and it is exactly complementary to phase 3:
queue priority is what makes the tail *separable*, not merely smaller.

## 8. Conditional tail severity

Among fills already at or below −25 ticks (183,651 observations, conservative cell), predicting
`−markout_1s`:

| Model | MAE | Spearman | Block mean ρ | Worst block | Blocks with ρ > 0 |
|---|---|---|---|---|---|
| naive (fold median) | 47.48 | — | 0.031 | −0.059 | 4/119 |
| ridge | 46.22 | 0.267 | 0.076 | −0.206 | 99/119 |
| LightGBM | **45.12** | **0.272** | 0.086 | −0.129 | 105/119 |

**Incidence is easier than severity, but only where incidence is predictable at all.** MAE
improves 2.4 ticks out of 47.5 (5 %), pooled rank correlation is 0.27, but the block-level mean
is only 0.086 and 14 of 119 blocks are negative. Once a fill is catastrophic, *how* catastrophic
is close to unpredictable from placement state.

## 9. Feature ablation — does queue lifecycle add anything?

Four pre-registered feature groups, identical folds, seeds and hyper-parameters throughout.
LightGBM shown; the logistic ordering is the same (`feature_ablation.csv`).

| Target | Feature set | Features | ROC AUC | PR AUC | PR lift | Worst block |
|---|---|---|---|---|---|---|
| **level disappears ≤ 1 s** | static book | 19 | 0.7823 | 0.6093 | 2.14× | 0.585 |
| base rate 28.4 % | static book + flow | 68 | 0.8470 | 0.7248 | 2.55× | 0.644 |
| | **queue lifecycle only** | 28 | **0.8201** | **0.6626** | 2.33× | 0.604 |
| | all | 96 | 0.8476 | **0.7269** | 2.56× | 0.642 |
| **trade-through ≤ 1 s** | static book | 19 | 0.8014 | 0.4718 | 3.02× | 0.660 |
| base rate 15.7 % | static book + flow | 68 | 0.8571 | 0.5848 | 3.74× | 0.728 |
| | **queue lifecycle only** | 28 | **0.8464** | **0.5698** | 3.64× | 0.714 |
| | all | 96 | 0.8580 | **0.5950** | **3.80×** | 0.730 |
| **catastrophic_25** | static book | 19 | 0.5853 | 0.7173 | 1.08× | 0.468 |
| base rate 66.3 % | static book + flow | 68 | **0.6109** | **0.7370** | 1.11× | 0.499 |
| | queue lifecycle only | 28 | 0.5757 | 0.7125 | 1.07× | 0.409 |
| | all | 96 | 0.6097 | 0.7347 | 1.11× | 0.462 |

**The honest answer is: barely, and only for sweep risk.**

- **Queue lifecycle is a strong stand-alone predictor.** 28 lifecycle features alone reach ROC AUC
  0.820 on level failure and 0.846 on sweep risk, beating the 19 static book features (0.782 /
  0.801) and coming within 0.03 and 0.01 of the 68-feature book-plus-flow set. Something real is
  being measured.
- **But it is nearly redundant with recent depth and trade flow.** Adding all 28 lifecycle
  features on top of book + flow moves level-failure ROC AUC by **+0.0006** and PR AUC by +0.002.
  For sweep risk the gain is larger but still modest: **+0.0009 ROC AUC and +0.010 PR AUC**
  (0.585 → 0.595, a 1.7 % relative improvement), with the worst block improving 0.728 → 0.730.
- **For catastrophic fills it adds nothing and slightly hurts** — 0.6109 → 0.6097 ROC AUC, and
  lifecycle-only is the worst of the four sets at 0.576.

The mechanical reading is straightforward and worth stating rather than dressing up: cumulative
additions and removals at the best level over an episode are largely an *integral of the L1 depth
flow* that the phase 1 windows already carry. Reconstructing the episode makes the state
interpretable and gives a clean hazard, but it does not add much independent information.

## 10. The level-birth cohort

One hypothetical order placed immediately after the depth event that makes a price best on its
side: **297,285 placements**, exactly one per level episode. Everything displayed at that instant
is treated as ahead of the order; later additions queue behind it; a print stamped at the same
timestamp cannot fill it. **This is not "front of queue"** — it is the one placement rule under
which the whole post-placement lifecycle is observable.

| Cohort | Queue cell | Fill rate | Mean 1 s markout | Median | Favourable | cat_25 | cat_50 | cat_100 | p1 |
|---|---|---|---|---|---|---|---|---|---|
| grid | conservative | 62.1 % | −54.83 | −39.5 | 12.4 % | 65.5 % | 42.1 % | 18.3 % | −308.5 |
| **level birth** | conservative | **85.2 %** | **−48.21** | −39.5 | **23.3 %** | 59.7 % | **43.5 %** | **23.2 %** | **−471.5** |
| grid | optimistic | 96.5 % | −5.29 | +0.5 | 80.9 % | 14.2 % | 9.2 % | 4.0 % | −197.5 |
| level birth | optimistic | 94.9 % | −15.05 | +0.5 | 52.1 % | 36.7 % | 26.5 % | 14.0 % | −411.5 |

**Joining at level birth is not materially less adverse — it trades a better body for a worse
tail.** The mean improves by 6.6 ticks and the favourable fraction nearly doubles, but the 1st
percentile deteriorates from −308.5 to −471.5, `catastrophic_100` rises from 18.3 % to 23.2 %, and
at the optimistic bound the mean is three times *worse* (−15.05 versus −5.29).

The reason is visible in the ex-post decomposition (`level_birth_cohort.csv`, mechanism
description only, never a feature):

| The level then… | Placements | Fill rate | Mean 1 s markout | Favourable | cat_25 |
|---|---|---|---|---|---|
| was `improved` on (better price appeared inside) | 148,960 | 71.1 % | **−29.43** | 32.9 % | 52.9 % |
| `stepped_away` (was consumed and the book moved through) | 148,309 | **99.4 %** | **−61.70** | 16.4 % | 64.7 % |
| died within 250 ms | 154,915 | 88.6 % | −45.14 | 25.0 % | 58.8 % |
| was repeatedly replenished | 62,853 | 74.6 % | −48.70 | 25.7 % | 57.1 % |
| was consumed mainly by prints | 25,828 | 97.5 % | −57.56 | 18.1 % | 64.5 % |

**This is the observable explanation of the phase 3 result.** Split the cohort by what happened
to the level and the fills separate almost perfectly: on levels that were consumed you fill
**99.4 %** of the time and lose 61.7 ticks; on levels that were merely improved on you fill 71 %
of the time and lose 29.4. A passive order at the touch is filled overwhelmingly by the levels
that die badly. Queue priority dominates the economics because it determines how much of that
sorting you are exposed to.

## 11. Tail concentration

`tail_contribution.csv`, 1 s markout, share of the total *negative* markout:

| Cohort / cell | Fills | Worst 1 % | Worst 5 % | Worst 10 % | Best 90 % mean |
|---|---|---|---|---|---|
| grid, conservative | 318,998 | 7.5 % | 22.9 % | 36.2 % | **−36.80** |
| grid, midpoint | 355,052 | 9.2 % | 27.6 % | 43.1 % | −22.68 |
| **grid, optimistic** | 496,168 | **23.3 %** | **60.9 %** | **84.7 %** | **+6.72** |
| level birth, conservative | 253,373 | 11.8 % | 29.6 % | 43.7 % | −18.93 |
| level birth, optimistic | 281,971 | 17.1 % | 40.8 % | 58.2 % | +12.24 |

**Whether this is a "tail problem" depends entirely on queue position, and that reframes the whole
question.**

At the back of the queue there is no tail problem — there is a *level* problem. The best 90 % of
fills still average **−36.8 ticks**; removing the worst decile entirely leaves the economics
comfortably negative.

At the front of the queue there genuinely is a tail problem: the best 90 % of fills average
**+6.72 ticks** and the worst 10 % account for **84.7 %** of all adverse markout. That is the
first configuration anywhere in this project where the body of the distribution pays and a
tractable minority destroys it — and §7 shows that minority is predictable at ROC AUC 0.78.

## 12. Stability

Negative in every block, every UTC day and every segment (`tail_by_block.csv`).

| Phase 2 block | conservative mean markout | cat_25 | optimistic mean markout | cat_25 |
|---|---|---|---|---|
| 0 | −46.25 | 63.3 % | −2.47 | 8.0 % |
| 3 (quietest) | **−37.13** | 55.0 % | **−1.74** | **5.0 %** |
| 6 | −66.10 | 70.1 % | −11.50 | 25.8 % |
| 9 (busiest) | **−66.84** | 70.2 % | **−11.65** | 27.0 % |

The catastrophic rate at the optimistic bound moves 5.0 % → 28.8 % across blocks and 4.8 % → 23.8 %
across the four UTC days, tracking activity as BTC ran from 64.3k to 72.9k. Level dynamics vary
just as much: the level-failure rate ranges 6.9 % (file 0) to 50.1 % (segment 2:6) across segments.
**Directions are stable everywhere; magnitudes are strongly regime-dependent.**

Side symmetry is essentially exact for level dynamics — `level_disappears` rates agree to four
decimals between bid and ask, and trade-through rates to within 0.3 points. Markout keeps the
familiar ~3–5 tick ask penalty seen in phase 3, most plausibly a property of a three-day rally.

---

## 13. Answers

1. **How long do best levels survive?** Median 204 ms, p25 102 ms (one depth batch), p75 1.02 s,
   p90 3.47 s, mean 1.73 s. Heavy-tailed: a quarter are gone within one batch and one in a
   hundred lasts over 25 s.

2. **How do they disappear?** Almost exactly 50/50. **50.1 % by price improvement** — a better
   price appears inside and the old level stops being best while remaining in the book (only
   1.3 % fully removed). **49.9 % by being consumed** — the level empties (100 % fully removed)
   and the book steps through it; 85 % of those saw a trade-through.

3. **How much depletion reconciles with trades?** **9.3 % corpus-wide**; 21.7 % on levels that
   were consumed, 3.7 % on levels that were merely improved on. The median episode's depletion is
   95.2 % unexplained. This is the hard limit aggregated L2 imposes on the queue question.

4. **How common is replenishment?** 38.3 % of episodes replenish at least once, median 5 events
   when they do; replenished quantity is 25.7 % of removed quantity.

5. **Do repeatedly replenished levels survive?** Strongly yes. Across replenishment deciles,
   P(level ends within 1 s) falls 0.538 → 0.059 and P(trade-through within 1 s) falls 0.326 →
   0.025 — roughly a **ninefold** difference.

6. **What predicts near-term level failure?** Age above all (`log_level_age_ms` coefficient −0.586,
   stable in 10/10 folds; hazard falls from 0.330 to 0.003 per 100 ms across age deciles), then
   thin own depth, recent prints at the level, and a touch that has been moving.
   Trade intensity carries the largest LightGBM gain share (55 % across the five windows).

7. **What predicts sweep risk?** The same state more sharply: age (−0.718), own touch depth
   (28.1 % of gain), prints already landing at and through the level, and adverse signed OBI.
   ROC AUC 0.879 at 500 ms with a **5.5× PR lift**.

8. **Does queue lifecycle add beyond static book and flow?** Barely. Lifecycle features alone are a strong stand-alone
   predictor — ROC AUC 0.820 on level failure and 0.846 on sweep risk from 28 features, beating
   19 static book features — but on top of book *plus recent flow* they add **+0.0006 ROC AUC**
   for level failure, **+0.0009 ROC AUC and +0.010 PR AUC** for sweep risk, and **nothing** for
   catastrophic fills. Episode-cumulative depth flow is largely an integral of the L1 flow
   windows phase 1 already computed.

9. **How concentrated is the loss in the tail?** It depends on queue position, which is the
   finding. At the back of the queue it is not concentrated at all — the best 90 % of fills still
   average −36.8 ticks. At the front of the queue it is extremely concentrated: the best 90 %
   average **+6.72 ticks** and the worst 10 % account for **84.7 %** of all adverse markout.

10. **Share of total negative 1 s markout from the worst fills?** Conservative: 7.5 % / 22.9 % /
    36.2 % from the worst 1 % / 5 % / 10 %. Optimistic: **23.3 % / 60.9 % / 84.7 %**.

11. **Can `catastrophic_25` be predicted?** At the back of the queue, essentially no — ROC AUC
    0.610, PR lift **1.11×**, worst block below chance. But the base rate is 66 %: conditional on
    filling from the back of a deep queue you have almost always been run over. At the front of
    the queue, **yes** — ROC AUC 0.780, PR lift 2.64×, 120/120 blocks above chance.

12. **Can `catastrophic_50` be predicted?** Same pattern, slightly stronger at the optimistic
    bound: ROC AUC 0.784, PR lift 2.99×, base rate 10.5 %.

13. **Incidence or severity?** **Incidence**, clearly — where incidence is predictable at all.
    Conditional severity reaches only ρ = 0.272 pooled, block mean 0.086, with 14 of 119 blocks
    negative and MAE improving 5 % over a constant. A defensible reading is: *incidence
    predictable, severity essentially not*.

14. **What state describes a queue about to fail?** Young, thin, already being printed against,
    never replenished, in a book whose touch has been moving, with adverse signed imbalance. Every
    one of those terms is sign-stable across all ten folds, and the hazard table shows the effect
    directly: risk is concentrated in the first few hundred milliseconds of a level's life.

15. **Does the phase 3 "queue priority dominates" result have an observable explanation?**
    **Yes, and it is clean.** Split the level-birth cohort by what the level then did: on levels
    that were consumed you fill **99.4 %** of the time and lose 61.7 ticks; on levels that were
    merely improved on you fill 71 % and lose 29.4. A passive order at the touch is filled
    overwhelmingly by the levels that die badly, and queue priority determines how much of that
    sorting you absorb.

16. **What happens in the level-birth cohort?** 297,285 placements, one per episode. Fill rate
    rises to 85.2 % (from 62.1 %), mean 1 s markout improves to −48.21 (from −54.83) and the
    favourable fraction nearly doubles to 23.3 %.

17. **Is it materially less adverse?** **No.** The median is identical (−39.5), the 1st percentile
    is far worse (−471.5 versus −308.5), `catastrophic_100` rises from 18.3 % to 23.2 %, and at
    the optimistic bound the mean is three times worse (−15.05 versus −5.29). It buys a better
    body at the price of a worse tail.

18. **Does the extreme tail survive in the birth cohort?** Yes, and it gets heavier. The worst
    1 % of birth-cohort fills average −839 ticks against −452 for the grid cohort.

19. **Are results stable?** Directions yes, magnitudes no. Every block, day and segment is
    negative at every queue cell, but the catastrophic rate at the optimistic bound moves 5.0 % →
    28.8 % across blocks and the level-failure rate ranges 6.9 % → 50.1 % across segments, all
    tracking activity. Bid and ask level dynamics agree to four decimal places.

20. **Is a queue-aware quote / stay / cancel policy justified as the next phase?** **Yes,
    conditionally, and it is better justified than a generic maker-EV search.** Three findings
    support it and one qualifies it:

    - Sweep risk is predictable at ROC AUC 0.879 with a 5.5× PR lift, stable in 120/120 blocks —
      that is a genuinely usable *cancel* signal, and it is about the level, not about a fill.
    - Level failure is predictable at ROC AUC 0.860 with the same stability.
    - At the front of the queue, catastrophic fills are separable (ROC AUC 0.780) and the body of
      the distribution pays (+6.72 ticks over the best 90 %).
    - The qualification: **none of that changes the phase 3 arithmetic**. At the realistic
      back-of-queue assumption the median fill is still −39.5 ticks and 66 % are catastrophic. A
      cancel policy can only help if the order is far enough up the queue for the body of the
      distribution to be worth protecting, and this data cannot tell us whether it is.

21. **The smallest defensible next experiment.** Not a strategy. A **counterfactual cancel
    study**: for each hypothetical order already simulated, ask what the realised markout
    distribution would have been if the order had been withdrawn the moment the out-of-fold sweep
    model crossed a *pre-registered grid* of probabilities — the same discipline as the phase 3
    α/β grid, no threshold chosen. Measure only (a) how much of the left tail is avoidable, (b)
    what fraction of favourable fills is given up, (c) how both vary with queue assumption. It
    reuses every artifact built here, adds one causal decision rule, and answers whether the
    predictability found in this phase is economically reachable — before anything resembling
    inventory, fees or PnL is introduced.

22. **Should monetisation stop here?** Not yet, but the bar for phase 4B should be explicit and
    it should be a falsification test, not a search: *does cancelling on the sweep signal remove
    more adverse markout than favourable markout, across the whole pre-registered threshold grid
    and at every queue assumption?* If it does not, the passive-maker line stops on this
    instrument, and the queue-lifecycle models found here remain useful as inputs to a different
    problem — quoting away from the touch, or taking-side timing — rather than as a market maker.

## 14. What this means for a future market maker

Stated as implications, not as an implementation:

- **The cancel signal is the strong result, not the quote signal.** Sweep risk within 500 ms is
  the most predictable thing in this phase (5.5× PR lift, stable everywhere). It is a statement
  about the level, available continuously while an order rests, which is exactly the shape a
  stay/cancel decision needs.
- **Level age is the workhorse, and it is not queue position.** An old, replenished, thick level
  is nine times less likely to fail in the next second. That is a *lifecycle* fact. It correlates
  with queue priority for an order that joined at birth, but this data never observes rank and
  nothing here should be converted into one.
- **Half of level endings are harmless.** Price improvement is not adverse selection. Any
  cancel rule trained on "level stops being best" is training on a coin flip between a benign and
  a hostile event; the two must be modelled separately.
- **Joining early is not the answer.** The level-birth cohort fills more, is favourable more
  often, and has a heavier tail. Early entry buys exposure to exactly the levels that get
  consumed.
- **The tail only becomes the problem once queue priority is good.** At the back of the queue
  there is nothing to protect. That ordering matters: queue-priority acquisition has to come
  first, and this corpus cannot measure it.

---

## Limitations

1. **Aggregated L2 remains aggregated.** 90.7 % of best-level depletion is unexplained. No
   lifecycle feature converts that into a queue position, and none of them is treated as one.
2. **The 100 ms depth batching quantises everything.** A quarter of level "lifetimes" are one
   batch long, and sub-100 ms dynamics are invisible by construction.
3. **One instrument, three days, a 13 % rally.** The bid/ask symmetry observed here may be a
   property of this sample.
4. **The level-birth cohort is a diagnostic, not a strategy.** Joining every new best level is not
   something a maker would do; it is the one placement rule that makes the whole post-placement
   lifecycle observable.
5. **Half of all level endings are price improvement**, which is benign for a resting order. Any
   future use of a level-failure model must separate the two close reasons, which this phase
   reports but does not model separately.

## What stays untouched for forward validation

The rotation-enabled AWS capture, every later AWS capture, and the Tardis June–August holdout.
None was read in this phase.

## Artifacts and tests

Committed, `research/native_queue_tail_v1/`: `methodology.json`, `queue_feature_schema.json`,
`level_episodes.csv`, `level_survival_summary.csv`, `depletion_replenishment_summary.csv`,
`hazard_curve.csv`, `sweep_risk_summary.csv`, `queue_feature_summary.csv`,
`tail_distribution.csv`, `tail_contribution.csv`, `tail_by_block.csv`,
`queue_bucket_studies.csv`, `queue_interaction_studies.csv`, `level_birth_cohort.csv`,
`folds.csv`, `level_failure_metrics.csv`, `sweep_model_metrics.csv`,
`catastrophic_model_metrics.csv`, `tail_severity_metrics.csv`, `feature_ablation.csv`,
`model_coefficients.csv`, `feature_importance.csv`, `calibration.csv`, `fold_metrics.csv`,
`level_qc_file{0,1,2}.json`, `birth_qc_file{0,1,2}.json`, `report.md`.

Heavy, ignored, `data/research/native_queue_tail_v1/` (398 MB): `level_episodes_file{0,1,2}.csv.zst`
(297,285 episodes), `level_grid_file{0,1,2}.csv.zst` (5.14 M lifecycle rows at 100 ms),
`birth_fills_file{0,1,2}.csv.zst`, `birth_mid_file{0,1,2}.csv.zst`,
`queue_tail_model_frame.parquet`, and the three out-of-fold prediction files.

**New code**: `cpp/research/level_lifecycle.{hpp,cpp}`, `cpp/app/native_level_main.cpp`,
`native_queue_tail/{__init__,spec,data,analysis,pipeline}.py`,
`tests/test_native_queue_tail.py`, `scripts/native_level_lifecycle.sh`,
`scripts/native_level_birth_cohort.sh`. **Modified**: `cpp/research/queue_sensitivity.{hpp,cpp}`
and `cpp/app/native_queue_main.cpp` gained a `--mode level_birth` placement mode (grid-mode output
is byte-identical to phase 3, asserted by test); `native_predictive/modeling.py` gained PR AUC.

### Tests

**C++** — `ctest` 6/6, `crypto_l2_tests` 20 groups. Two new groups pin the level semantics on a
hand-built capture: an episode that grows before any reduction (an addition, not a
replenishment), a reduction with no print to explain it, a print followed by the reduction it
accounts for, a replenishment, a full removal, and the *same price recreated* — asserted to be a
new episode with a new identifier and no inherited history. The level-birth group asserts that a
placement follows the event that made its price best, that the whole displayed quantity at that
instant is ahead, and that no order fills from anything stamped at or before its own placement.

**Python** — `tests/test_native_queue_tail.py`, 30 tests, all pass: fixed catastrophic thresholds;
the methodology's refusal to claim queue position or call unexplained removal a cancellation; no
placement feature containing fill-time state; episodes well formed, non-overlapping and never
crossing a segment; the same price recurring producing distinct identifiers; replenishment
requiring a prior reduction; removal reconciliation being conservative and complete
(`explained + unexplained == removed`, `explained <= printed`); quantity bookkeeping closing
exactly; lifecycle state monotone forward inside its own episode; survival and sweep targets never
crossing a segment edge; a segment end being censoring rather than level failure; nested sweep and
catastrophic targets; catastrophic targets existing only for observed fills; severity defined only
on the severe tail; the birth cohort matching the episode population one-for-one; folds matching
phase 2 geometry and purge; a falling hazard; and byte-identical repeated replay.

`tests/test_native_research.py`, `tests/test_native_economic.py` and
`tests/test_native_predictive.py` all still pass. Full run: **151 tests, 150 pass**, plus 19 in the
LightGBM-isolated module. The one failure is the known pre-existing frozen
`passive_binary_sha256` gate from an experiment frozen at commit `628618b`; its expected hash was
**not** updated.

## Reproducing

```
cmake -S cpp -B build/cpp && cmake --build build/cpp -j8
bash scripts/native_level_lifecycle.sh
bash scripts/native_level_birth_cohort.sh
python -m pyresearch.native.queue_tail.pipeline all
python -m unittest tests.test_native_queue_tail
```
