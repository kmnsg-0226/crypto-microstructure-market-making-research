# Passive maker economic feasibility, native_dev_v1

Phase 3. One question: **how far is the current best-touch passive maker setup from break-even,
and what is the gap made of?**

No strategy search. No threshold, holding period, quote distance or order size was tuned; no fee
schedule was applied; no α/β cell was chosen; no Sharpe or PnL exists in this phase.

All results are **development estimates** on the frozen `native_dev_v1` corpus. Nothing here is
out of sample. The rotation-enabled AWS capture, every later AWS capture and the Tardis
June–August holdout were not read.

Pre-registration and input hashes: `research/native_economic_v1/methodology.json`.

---

## 0. What was replayed, and how it was validated

A new causal replay (`native_queue_sensitivity`) places a hypothetical 5-lot (0.005 BTC) order at
the touch on both sides **once per second** — a deterministic decimation of the 100 ms decision
grid, so every placement instant is also a phase 2 row and the frozen out-of-fold predictions
join on an exact key. 257,039 placements × 2 sides × 25 queue assumptions = **12,851,950
placement-cell observations** across the same 8 segments.

The replay emits only *when* each order filled and by which mechanism, plus the mid path at
event resolution (148,872 points). Markouts are reconstructed downstream, so every horizon stays
re-derivable without another replay and the sign convention lives in one auditable place.

**Validation.** At α = 1, β = 0 the new replay reproduces the phase 1 fill model *exactly* —
queue ahead, quote price, fill presence, fill time, mechanism and post-fill markouts all match
bit-for-bit on both sides across every shared placement instant. That is asserted as a test.

### The two assumptions

| | Meaning | Conservative | Extreme bound |
|---|---|---|---|
| **α** | fraction of displayed quantity at the quote assumed to be *ahead* of the order | 1.00 (back of visible queue) | 0.00 (front of visible queue) |
| **β** | fraction of an *unexplained* displayed-quantity removal ahead of the order credited as queue advancement | 0.00 (never advances) | 1.00 (always advances) |

Neither is an estimate of true Binance queue position, and β is **not** a cancellation rate.
Aggregated L2 cannot identify whether a reduction is a cancellation, an execution not aligned
with the aggTrade feed, or a batching artefact, so nothing here labels it.

Removals are attributed in a fixed order — first against quantity added at the quote *after*
placement (which sits behind the order), then against aggressive prints not yet used to explain
a reduction, and only the remainder is "unexplained" and eligible for the β credit. The credit
also stops the moment the assumed queue ahead reaches zero, so β can never credit something that
was never in front of the order. Of the displayed reductions seen by resting orders,
**53.4 % were charged to quantity added behind, 7.4 % were explained by prints, and 39.2 % were
unexplained.**

---

## 1. How far from break-even at α = 1, β = 0?

Signed quote-relative markout, ticks, positive favours the passive trader. 514,078 eligible
opportunities, 319,014 filled within the 30 s window (62.1 %).

| Horizon | Mean | Median | p25 | p75 | Favourable | **Required benefit** |
|---|---|---|---|---|---|---|
| 100 ms | −33.07 | −26.5 | −46.5 | −9.5 | 10.8 % | **33.07 ticks** |
| 500 ms | −50.74 | −38.5 | −73.5 | −14.5 | 10.7 % | **50.74 ticks** |
| 1 s | −54.83 | −39.5 | −81.5 | −14.5 | 12.4 % | **54.83 ticks** |
| 5 s | −64.47 | −48.5 | −114.5 | −7.5 | 20.3 % | **64.47 ticks** |

The 1 s required benefit is **5.48 USD per BTC**, **0.81 bps**, and — see §9 — **106 half-spreads**.
Block bootstrap over 30-minute blocks: 54.83 ticks, 5–95 % interval [53.02, 56.57]. The gap is
not a sampling artefact.

---

## 2. What does queue position alone change?

Reading down the β = 0 column of the fixed 5 × 5 grid (`queue_sensitivity_surface.csv`):

| α | Fill rate 1 s | Fill rate 30 s | Trade-through share | Median time to fill | Mean 1 s markout |
|---|---|---|---|---|---|
| 1.00 | 14.4 % | 62.1 % | 78.5 % | 4089 ms | **−54.83** |
| 0.75 | 14.8 % | 63.1 % | 66.7 % | 4008 ms | −52.46 |
| 0.50 | 15.7 % | 65.0 % | 54.4 % | 3839 ms | −48.26 |
| 0.25 | 18.1 % | 69.6 % | 35.9 % | 3472 ms | −39.92 |
| 0.00 | **69.5 %** | **96.5 %** | **2.8 %** | **419 ms** | **−5.29** |

Queue position is by far the dominant lever, and its effect is wildly non-linear. Moving from
α = 1 to α = 0.25 buys 15 ticks; the last step from α = 0.25 to α = 0 buys **35 ticks**. The
cliff is structural: at α = 0 the order is first in line and fills on the very next print at the
quote (median 419 ms), so the level almost never has to be swept and the trade-through share
collapses from 78.5 % to 2.8 %.

α = 0 is an extreme bound, not an achievable state. You cannot be ahead of a queue that already
exists at a price; only the creator of a new price level is at the front of it.

## 3. What does removal credit alone change?

Reading across the α = 1 row:

| β | Fill rate 30 s | Trade-through share | Mean 1 s markout | Required benefit |
|---|---|---|---|---|
| 0.00 | 62.1 % | 78.5 % | −54.83 | 54.83 |
| 0.25 | 62.3 % | 75.0 % | −54.24 | 54.24 |
| 0.50 | 62.7 % | 71.2 % | −53.34 | 53.34 |
| 0.75 | 63.3 % | 64.9 % | −51.96 | 51.96 |
| 1.00 | 64.9 % | **38.9 %** | −48.46 | 48.46 |

β is a weak lever on economics and a strong one on *mechanism*. Crediting every unexplained
removal halves the trade-through share (78.5 % → 38.9 %) but recovers only **6.4 ticks of 55**
— 12 % of the gap. Fill rate barely moves at all.

That dissociation matters: an assumption that dramatically changes *how* orders fill barely
changes *what they are worth*, because at the back of a deep queue an at-quote fill is nearly as
adverse as a sweep (§6).

By construction β is inert at α = 0 — there is nothing in front of the order for it to remove —
which the α = 0 row of every table confirms and a test asserts.

---

## 4. Is markout still materially negative at the extreme optimistic bound?

**Yes, on the mean. No, on the median.** At α = 0, β = 1, over 496,183 fills:

| Horizon | Mean | Median | p25 | p75 | Favourable |
|---|---|---|---|---|---|
| 100 ms | −2.98 | **+0.5** | +0.5 | +0.5 | 88.7 % |
| 500 ms | −4.82 | **+0.5** | +0.5 | +0.5 | 84.3 % |
| 1 s | −5.29 | **+0.5** | +0.5 | +0.5 | 80.9 % |
| 5 s | −6.51 | **+0.5** | −25.5 | +1.5 | 68.7 % |

The interquartile range at 1 s is **entirely at +0.5 ticks** — the exact half-spread. Four fills
in five capture it cleanly. The mean is dragged to −5.29 by a left tail: p10 is −44.5.

So even under an unachievable front-of-queue bound the *expected* fill still costs 5.29 ticks
(0.53 USD/BTC, 0.077 bps, **10.2 half-spreads**). The distribution is a classic maker profile —
win the half-spread often, lose 40–100 ticks occasionally — and on this instrument the tail
wins.

## 5. Does the 25-cell surface contain anything close to break-even?

No cell of the fixed grid has a non-negative mean markout at any horizon. Required 1 s benefit,
in ticks, over the whole surface:

| α ↓ / β → | 0.00 | 0.25 | 0.50 | 0.75 | 1.00 |
|---|---|---|---|---|---|
| **0.00** | 5.29 | 5.29 | 5.29 | 5.29 | 5.29 |
| **0.25** | 39.92 | 32.65 | 26.21 | 23.97 | 22.69 |
| **0.50** | 48.26 | 45.93 | 40.11 | 33.36 | 30.29 |
| **0.75** | 52.46 | 51.28 | 49.50 | 44.91 | 38.19 |
| **1.00** | 54.83 | 54.24 | 53.34 | 51.96 | 48.46 |

The best cell in the grid is 5.29 ticks from break-even; the worst is 54.83. The whole surface
spans a factor of ten, and every point of it is a cost.

---

## 6. Where do the poor economics come from?

`fill_mechanism_decomposition.csv` splits E[M | fill] into P(TT)·E[M | TT] + P(AQ)·E[M | AQ].
The reconstruction is exact to floating point in every cell (asserted as a test). At 1 s, pooled
over both sides:

| Assumption | P(TT) | E[M \| TT] | P(AQ) | E[M \| AQ] | Total |
|---|---|---|---|---|---|
| α = 1, β = 0 | 78.5 % | −58.6 | 21.5 % | −41.0 | −54.83 |
| α = .5, β = .5 | 14.0 % | −52.6 | 86.0 % | −38.1 | −40.11 |
| α = 0, β = 1 | 2.8 % | −47.8 | 97.2 % | **−4.05** | −5.29 |

**The answer to "A, B, or C" is: all three, but not equally.**

- **Trade-through fills are always toxic** and barely improve with queue assumption: −58.6 →
  −47.8 across the whole grid. A sweep is a sweep.
- **At-quote fills are themselves deeply adverse at the back of the queue** (−41.0 ticks) and
  almost benign at the front (−4.05). This is the single most important finding of the phase.
  An "at-quote" fill behind 1,000 lots means the entire queue in front was consumed by
  aggressive flow before you — which is itself a strong adverse signal. At the front of the
  queue, the same label means only "the next print arrived".
- **Mechanism mix does most of the visible work** — 78.5 % → 2.8 % trade-through — but the
  quality change *within* at-quote fills (−41.0 → −4.05) is what actually moves the total.

So: the economics are not primarily a mechanism-mix problem. They are a queue-position problem
that expresses itself through the mechanism mix.

## 7. When does the adverse selection happen?

`markout_paths.csv`. Neither Case 1 (immediate then plateau), Case 2 (gradual) nor Case 3
(recovery) fits alone — the answer is **front-loaded and then continuing, with no recovery
anywhere**:

| Population | 100 ms | 500 ms | 1 s | 5 s |
|---|---|---|---|---|
| α = 1, β = 0, trade-through | −35.2 | −54.3 | −58.6 | −69.1 |
| α = 1, β = 0, at-quote | −25.2 | −37.8 | −41.0 | −47.7 |
| α = 0, β = 1, at-quote | −2.09 | −3.64 | −4.05 | −5.24 |
| α = 0, β = 1, trade-through | −33.5 | −45.4 | −47.8 | −50.2 |

Roughly 55–60 % of the eventual 5 s damage is already present at 100 ms, and the remainder
accrues steadily. **No population recovers at any horizon.** Medians tell a different story from
means: at α = 0 the median stays pinned at +0.5 ticks all the way out to 5 s while the mean
deteriorates from −2.98 to −6.51, which is the signature of a widening tail rather than a
drifting centre.

## 8. Required benefit in absolute units

`break_even_benefit.csv` carries all four horizons in ticks, USD per BTC and basis points, the
latter formed per observation against that observation's own quote before averaging.

| Assumption | 1 s ticks | USD/BTC | bps |
|---|---|---|---|
| α = 1, β = 0 | 54.83 | 5.483 | 0.806 |
| α = .5, β = .5 | 40.11 | 4.011 | 0.590 |
| α = 0, β = 1 | 5.29 | 0.529 | 0.077 |

## 9. How many spread-widths is that?

The spread is 1 tick 99.8 % of the time (mean 1.034 ticks), so the observable half-spread scale
is ~0.5 ticks. Required 1 s benefit expressed in half-spreads:

| α ↓ / β → | 0.00 | 0.25 | 0.50 | 0.75 | 1.00 |
|---|---|---|---|---|---|
| **0.00** | 10.2 | 10.2 | 10.2 | 10.2 | 10.2 |
| **0.25** | 77.2 | 63.2 | 50.7 | 46.4 | 43.9 |
| **0.50** | 93.4 | 88.9 | 77.6 | 64.6 | 58.6 |
| **0.75** | 101.5 | 99.2 | 95.8 | 86.9 | 73.9 |
| **1.00** | **106.1** | 105.0 | 103.2 | 100.5 | 93.8 |

Under the current assumption the adverse markout is **106× the entire observable half-spread**.
Under the extreme optimistic bound it is still **10×**. Capturing the whole spread on every fill
— which no passive trader does — would close roughly 1 % of the conservative gap and 10 % of the
optimistic one.

---

## 10. Does the phase 2 toxicity model still rank markout?

`oof_toxicity_deciles.csv`, using only frozen blocked out-of-fold predictions joined on the exact
row key. 1,285,602 of 1,542,234 headline placement-cell rows fall inside a phase 2 validation
block and carry a prediction; the rest have none and drop out.

Mean realised 1 s markout by predicted-markout decile:

| Decile | α = 1, β = 0 | α = .5, β = .5 | α = 0, β = 1 |
|---|---|---|---|
| 0 (most toxic predicted) | −72.6 | −54.7 | −4.90 |
| 3 | −62.3 | −47.3 | −5.07 |
| 6 | −55.3 | −40.2 | −5.20 |
| 9 (least toxic predicted) | **−37.7** | **−28.0** | **−9.78** |
| spread across deciles | 34.9 ticks | 26.7 ticks | −4.9 ticks |

**The ranking survives at α = 1 and α = 0.5 and inverts at α = 0.** The phase 2 model was trained
on outcomes generated under α = 1, β = 0, where the dominant driver of markout is whether the
queue gets swept. Once the order is at the front of the queue that driver is gone, and the
model's ordering not only weakens but reverses — its "least toxic" decile becomes the worst.

This is a direct caution: **a toxicity model is conditional on the queue model it was trained
under**, and cannot be carried into a different queue assumption without refitting.

## 11. Are high predicted-fill states better or worse after queue sensitivity?

`oof_fill_deciles.csv`. The relationship *flips sign* between the two bounds:

| Predicted-fill decile | α = 1, β = 0 | α = 0, β = 1 |
|---|---|---|
| 0 (lowest) | −49.7 | **+1.33** |
| 2 | −56.4 | −0.04 |
| 5 | −61.3 | −2.42 |
| 9 (highest) | −49.7 | −30.0 |

At α = 1 the relation is hump-shaped and everywhere deeply negative — the phase 2 finding. At
α = 0 it becomes cleanly monotone decreasing, and the **two lowest deciles have a positive mean
markout**: +1.33 and +0.52 ticks, 96 % and 95 % favourable, over 42,854 opportunities each.

This is the only favourable region anywhere in the phase and it is reported here descriptively
and not pursued. Three reasons it is not an opportunity:

1. It requires α = 0, which is not a state that can be occupied at an existing price level.
2. The magnitude is **0.13 USD per BTC, 0.021 bps** at 1 s. Any realistic per-trade cost on this
   instrument is measured in whole basis points — one to two orders of magnitude larger. No fee
   schedule is applied here; this is a scale statement, not a net calculation.
3. The mechanism is mechanical, not informational: a low predicted fill probability at the front
   of the queue selects quiet states where nothing happens, and the +0.5 tick half-spread is
   collected because no adverse flow arrived.

The 5 × 5 out-of-fold surface (`oof_joint_surface.csv`) shows the same shape: at α = 0 the entire
lowest fill quintile is positive (+0.69 to +1.28 ticks) and the highest is −19 to −24, while at
α = 1 every one of the 25 cells lies between −38.9 and −76.8.

---

## 12. Stability across time

`queue_sensitivity_by_block.csv`. Required 1 s benefit by phase 2 validation block:

| Block | α = 1, β = 0 | α = 0, β = 1 |
|---|---|---|
| 0 | 46.3 | 2.47 |
| 1 | 44.5 | 2.44 |
| 2 | 46.2 | 2.40 |
| 3 | **37.1** | **1.74** |
| 4 | 49.2 | 3.19 |
| 5 | 64.4 | 7.72 |
| 6 | 66.1 | 11.50 |
| 7 | 57.7 | 5.56 |
| 8 | 65.2 | 10.57 |
| 9 | **66.8** | **11.65** |

**Negative in every block, every UTC day and every segment**, at every queue assumption. The
result is not one volatile period: the best block is still 37 ticks (74 half-spreads) from
break-even at the conservative assumption. It does worsen monotonically across the corpus —
42.9 → 62.7 ticks from 17 to 20 August at α = 1 — tracking rising activity as BTC moved from
64.3k to 72.9k.

Block bootstrap 5–95 % intervals on the required 1 s benefit: [53.02, 56.57] at α = 1, β = 0;
[38.32, 41.84] at the midpoint; [4.64, 5.93] at α = 0, β = 1. No iid standard error is used
anywhere.

## 13. Are quiet states also negative?

`activity_regimes.csv`. Yes — everywhere, at every assumption, in every bucket:

| Trade-intensity quintile | mean trades/s | α = 1, β = 0 | α = 0, β = 1 |
|---|---|---|---|
| 0 (quietest) | 1.3 | −45.2 | −1.97 |
| 2 | 4.4 | −56.3 | −3.56 |
| 4 (busiest) | 83.8 | −57.8 | **−13.27** |

Realized-activity buckets tell the same story: −49.7 → −61.6 at α = 1, and −2.54 → −12.20 at
α = 0. Adverse selection scales with activity — by a factor of 6.7 at the optimistic bound — but
**it does not vanish in quiet states.** The quietest quintile at α = 1 is still 90 half-spreads
from break-even.

The `spread_state` bucketing collapsed to a single bucket: with a 1-tick spread on 99.8 % of
placements there is no cross-sectional spread variation to condition on.

## 14. Side asymmetry

`side_asymmetry.csv`. Bid and ask are close but not identical, and the ask side is consistently
worse:

| Assumption | Bid required benefit | Ask required benefit | Gap |
|---|---|---|---|
| α = 1, β = 0 | 53.32 | 56.36 | 3.04 |
| α = .5, β = .5 | 38.52 | 41.72 | 3.20 |
| α = 0, β = 1 | 4.90 | 5.68 | 0.77 |

Fill rates differ by ~1 point and trade-through shares by under 1 point. The 3-tick ask penalty
is stable across the whole grid and is most plausibly a property of *this sample*: the corpus is
a 13 % three-day rally, and a resting ask in a rising market is picked off more often than a
resting bid. It should not be read as a structural asymmetry of the instrument.

---

## 15. Structural classification

The evidence supports **CASE A, with the shape of CASE C**, and the two are not mutually
exclusive here:

- **CASE A holds on the pooled mean.** The extreme optimistic bound α = 0, β = 1 — which is not
  an occupiable state — still leaves a mean 1 s markout of −5.29 ticks, ten times the entire
  observable half-spread, in every block and every activity regime.
- **CASE C describes the shape.** All the improvement in the surface is concentrated at the
  front-of-queue extreme. The step from α = 0.25 to α = 0 is worth 35 of the 50 ticks available
  across the whole grid; β contributes 6 ticks at most.
- **CASE B is not supported.** The midpoint assumption α = 0.5, β = 0.5 sits at −40.11 ticks,
  78 half-spreads from break-even — closer to the conservative corner than to break-even.

The one qualification worth stating plainly: at α = 0 the *median* fill is favourable (+0.5
ticks, 81 % of fills) and the negative mean is entirely a tail phenomenon. That is a different
problem from "the trade does not work" — it is "the trade works four times in five and the fifth
time costs more than the four gains".

## 16. Is a Phase 4 maker EV simulator justified?

**Not as previously scoped.** An EV simulator layering fees, inventory and cancel policy on top
of these numbers would be adding second-order terms to a first-order gap of 10–106 half-spreads.
Its answer is already determined by this phase, and building it would risk becoming a search for
assumptions that close the gap.

Two components *are* solid enough to build on: fill probability and fill mechanism are strongly
and stably predictable (phase 2), and the mechanism decomposition here is exact and stable across
time. What is missing is not modelling machinery — it is knowledge of where a real order actually
sits in the queue, which is precisely the parameter the whole result hinges on.

## 17. The next most justified question

Two, in order.

**A. Measure queue dynamics instead of assuming them.** The whole result turns on α, and α is
currently a free parameter spanning a 10× economic range. This phase already built the machinery
to reconcile the two feeds: of the displayed reductions seen by a resting order, 53.4 % are
attributable to quantity that joined behind it, 7.4 % are explained by aggressive prints, and
39.2 % are unexplained. That decomposition — how a price level actually empties, how long
displayed quantity survives, how often a level is created rather than joined — is measurable from
the existing corpus without any new assumption, and it would replace the α axis with evidence.

**B. Attack the tail, not the mean.** At the front-of-queue bound the median fill earns the
half-spread and the mean loses 5.29 ticks. The entire economic question is the 10–20 % of fills
in the left tail. Whether those are *predictable at placement time* — as opposed to the
queue-sweep proxy the phase 2 toxicity model actually learned, which §10 shows does not survive a
change of queue assumption — is a well-posed microstructure question that this corpus can answer.

A quote-placement / cancel-requote policy study only becomes meaningful after A, because the
value of cancelling depends entirely on where in the queue the order sits.

---

## Limitations

1. **α is unidentified, not estimated.** Binance publishes aggregated L2. Queue position is not
   observable and the 10× economic range across the α axis is the honest width of that ignorance.
2. **β is a sensitivity axis, not a cancellation rate.** 39.2 % of ahead-of-order removals are
   unexplained; how many are cancellations versus feed-misaligned executions versus batching is
   not identifiable from this data.
3. **One instrument, three days, one direction.** The corpus is a 13 % rally; the 3-tick ask
   penalty is probably a sample property.
4. **A fixed 1 s placement grid with no cancel policy.** Every order rests for the full 30 s
   observation window. A real maker cancels; that is exactly why this phase does not compute EV.
5. **The 5 s markout horizon is short.** Nothing here says what happens to inventory afterwards,
   and no inventory value is assumed.
6. **Phase 2 predictions were reused, not refitted** — correct for avoiding leakage, but §10
   shows they are conditional on the α = 1, β = 0 queue model they were trained under.

## Artifacts

Committed, `research/native_economic_v1/`: `methodology.json`, `queue_sensitivity_surface.csv`,
`queue_sensitivity_by_block.csv`, `break_even_benefit.csv`, `break_even_block_bootstrap.csv`,
`fill_mechanism_decomposition.csv`, `markout_paths.csv`, `oof_toxicity_deciles.csv`,
`oof_fill_deciles.csv`, `oof_joint_surface.csv`, `activity_regimes.csv`, `side_asymmetry.csv`,
`queue_qc_file{0,1,2}.json`, plus `report.md` indexing them.

Heavy, ignored, `data/research/native_economic_v1/`: `queue_fills_file{0,1,2}.csv.zst`
(12.85 M placement-cell rows, 70 MB), `mid_path_file{0,1,2}.csv.zst` (148,872 mid changes, 1.5 MB).

## Reproducing

```
cmake -S cpp -B build/cpp && cmake --build build/cpp -j8
bash scripts/native_queue_sensitivity.sh
python -m pyresearch.native.economic.pipeline all
python -m unittest tests.test_native_economic
```
