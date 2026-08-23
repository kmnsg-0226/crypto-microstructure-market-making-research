# OBI profitability search loop

## Success standard

This loop is not allowed to call an in-sample or already-seen historical result profitable.
Each search family is declared before its outcomes are calculated. Historical work may only
produce a frozen candidate. Completion requires at least seven consecutive days and 500
completed trades on post-freeze native Binance data, positive performance after actual account
fees, day-by-day reporting, and a block-bootstrap 95% lower bound above zero. The live collector
is not modified, stopped, or restarted by this work.

The fixed audit specification is
`research/specs/obi_profitability_search_spec.json` with SHA-256
`e028ee58abdc0aa5e47aab075eb5177a0a04616597ecea943f55ed2e878f8c51`.
All eight historical dates had already been seen before this loop began, so June through August
are retrospective only.

## Loop 1: fixed-horizon taker upper bound

The first family crossed the actual opposite BBO 100 ms after each decision and exited at the
actual opposite BBO after 0.5, 1, 2, 5, 10, 30, 60, or 300 seconds. It charged 5 bps per side
but deliberately assumed zero depth slippage and zero market impact. This is an optimistic
upper bound: exact top-10 execution cannot improve a failed candidate.

The grid covered 14 signals, eight absolute-signal tail thresholds, trend and contrarian
directions, 14 trade/book/volatility/time regimes, and eight horizons. Development-only signal
and regime thresholds were written before price outcomes were opened.

Results:

- declared policy cells: `25,088`
- activity-eligible cells: `20,830`
- cells with positive pooled net upper-bound bps: `0`
- cells positive on all five development days: `0`
- candidates advanced to exact top-10 execution: `0`
- best observed optimistic gross BBO mean: `+1.66 bps`
- corresponding net after 5 bps per side: `-8.34 bps`
- ranking-first worst-day-robust policy net: `-8.93 bps`

The strongest gross cell used the frozen combined prediction, trend direction, one-tick spread,
99th-percentile absolute signal, and a five-minute horizon. It still covered only about one
sixth of the round-trip fee. The ranking-first policy used OBI-L1 trend, UTC 08-16, 90th
percentile, and five minutes; none of its development days was positive.

This entire fixed-horizon taker family is rejected without reading June/July/August outcomes and
without running the exact top-10 engine. The next loop must change the execution structure or
the causal signal construction rather than mine a finer threshold from the same failed family.

## Loop 2: persistent pressure and long horizons

Loop 2 was separately declared after seeing Loop 1 fail. It replaced instantaneous signals with
full-window trailing OBI/OFI/TI pressure over 1-60 seconds and extended fixed exits to 1 minute,
5 minutes, 15 minutes, 30 minutes, 1 hour, 2 hours, and 4 hours. Every rolling window is causal,
includes only observations available at the decision, requires a complete finite window, and
resets at every feature-segment boundary. The actual BBO, 100 ms latency, 5 bps fee per side,
and optimistic zero-depth-slippage upper bound were retained.

Results:

- declared policy cells: `12,096`
- activity-eligible cells: `10,008`
- positive pooled development upper bounds: `13`
- candidates positive on four or five development days: `0`
- candidates advanced to exact execution: `0`

The highest pooled cell was 30-second trailing TI, contrarian direction, UTC 16-24, 95th
absolute percentile, and a four-hour hold. Its optimistic gross mean was `+14.53 bps` and net
mean was `+4.54 bps`, but the five daily net means were:

| date | observations | net upper-bound bps |
|---|---:|---:|
| 2026-01-01 | 3,986 | -5.94 |
| 2026-02-01 | 1,317 | +20.34 |
| 2026-03-01 | 518 | +22.83 |
| 2026-04-01 | 1,474 | -15.01 |
| 2026-05-01 | 6,050 | +11.21 |

The apparent pooled edge is therefore dominated by day/regime drift and heavily overlapping
four-hour observations. The worst-day-first candidate was also positive on only three dates and
had pooled net `-4.60 bps`. Loop 2 is rejected without opening the retrospective dates or
promoting a candidate to exact execution. A pooled mean is not accepted when the preregistered
day-stability gate fails.

## Loop 3: signal-reactive passive entry, taker exit

Loop 3 changed the fee and execution structure. Entry used only full fills from the existing
pessimistic visible-queue maker model; partial fills were not promoted to completed entries.
After a fill, the position exited at the actual opposite BBO with 100 ms latency. The primary
cost was 2 bps maker plus 5 bps taker. Depth slippage and impact at exit were deliberately zero,
so this remained an optimistic upper bound.

Six OBI/microstructure signals, three tail thresholds, maker trend/contrarian orientation, four
quote lifetimes, seven post-fill holding horizons, eight queue/time regimes, and four cancel
modes produced `32,256` development cells. Dynamic cancel modes stopped a quote after its signal
left the same sign, left the entry tail, or reached the opposite tail. Only post-placement
observations were used, cancel latency was 100 ms, and a fill at or before the effective cancel
time still counted.

Results:

- activity-eligible unique cells: `18,631`
- positive pooled cells at maker 2 + taker 5 bps: `0`
- positive pooled cells even at maker 0 + taker 5 bps: `0`
- candidates advanced to exact non-overlap/top-10 exit: `0`
- best optimistic gross mean: `+3.59 bps`
- best maker-0/taker-5 net: `-1.41 bps`
- best primary net: `-3.41 bps`

The best pooled cell was TI-1s maker-contrarian, tail-exit cancel, five-second quote lifetime,
one-hour hold, and low-queue UTC 16-24. It was positive on only one of five dates; primary daily
net ranged from `-10.19` to `+3.40` bps. Dynamic cancellation improved the fixed-lifetime
variant, but not enough to cover even a zero-maker-fee/taker-exit structure. Loop 3 is rejected
without reading later dates.

## Loop 4: pessimistic maker-to-maker round trip

Loop 4 removed the taker exit. Both entry and exit had to be `full` under the existing
pessimistic visible-queue labels; partial fills were treated as no completed round trip. The
opposite-side exit probe was placed only after the entry full fill was known on the local clock,
plus at least 100 ms. Entry and exit fills also had to remain in the same valid feature segment.
The primary cost was 2 bps maker on each leg. No midpoint or fabricated execution was used.

Six OBI/microstructure signals, two frozen tails, two orientations, two entry lifetimes, two
causal entry-cancel modes, six hold delays, two exit lifetimes, and eight queue/time regimes
produced `9,216` development cells. This was still an optimistic capacity screen because
eligible probe pairs could overlap; any survivor had to pass a one-position-at-a-time replay.

Results:

- activity-eligible unique cells: `4,570`
- positive pooled cells after 2 + 2 bps maker fees: `23`
- cells positive on three development dates: `11`
- cells positive on four or five development dates: `0`
- candidates advanced to one-position exact execution: `0`
- best pooled gross mean: `+13.47 bps`
- best pooled primary net: `+9.49 bps`
- best pooled candidate positive dates: `2 / 5`
- best pooled candidate worst date: `-6.77 bps`

The best pooled result was TI-1s maker-contrarian with tail-exit cancellation, five-second
entry and exit lifetimes, a four-hour delay, and UTC 16-24. Its daily primary net was `-5.47`,
`+6.57`, `+32.43`, `-6.77`, and `-4.79` bps. The highest-ranked day-stability candidate was
positive on three dates but had pooled net `-0.69 bps` and worst-day net `-6.90 bps`. This is
regime drift combined with a long, overlapping screen, not a promotable strategy. Loop 4 is
rejected without reading June-August outcomes. A second run reproduced every result artifact
byte-for-byte.

## Loop 5: OBI state-transition maker exit

Loop 5 replaced the fixed holding delay with the first causal transition of the entry signal.
The exit trigger was the first post-fill observation that left the entry tail, crossed zero, or
reached the opposite tail. A one-hour or four-hour cap was used if the transition did not occur.
Both legs still required pessimistic visible-queue full fills, stayed within one valid feature
segment, and paid 2 bps maker fees per leg.

Results:

- declared cells: `9,216`
- activity-eligible unique cells: `4,724`
- positive pooled cells after primary fees: `0`
- positive cells on any development date set: `0`
- best pooled gross mean: `+0.58 bps`
- best pooled primary net: `-3.42 bps`
- best pooled worst date: `-5.37 bps`

Waiting for an OBI state reversal shortened and standardized the realized price movement, but
the best gross edge was far below the approximately 4 bps maker round-trip cost. Tail exit and
sign exit were gross-negative; even the best opposite-tail rule was insufficient. Loop 5 is
rejected without opening June-August outcomes.

## Loop 6: causal momentum-gated OBI

Loop 6 returned to fixed maker-to-maker exits but gated OBI entries with only past price state.
Five-minute and one-hour mid-price moves were computed strictly backward within the same feature
segment, then oriented to the proposed inventory side. This tested continuation, opposition,
aligned multi-horizon momentum, and one-hour-trend pullback regimes. Both legs still required
pessimistic full fills and paid 2 bps maker fees each.

The overlapping development screen produced the first historical day-stable upper-bound result:

- declared cells: `4,992`
- activity-eligible unique cells: `2,477`
- positive pooled cells after primary fees: `383`
- positive on all five development dates: `8`
- best day-stability candidate pooled net: `+9.65 bps`
- that candidate's worst development date: `+4.52 bps`
- completed overlapping probe pairs: `6,911`

All eight survivors used a one-hour hold. Under the preregistered one-pending-or-open-position
exact rule, even an impossible best case with zero fill latency and no unfilled-order blocking
can complete only `23` round trips per day and `115` across five days. That is below the fixed
minimum of 50 per day and 500 total. The candidates therefore fail the exact capacity precheck
before partial-fill or PnL replay; the thousands of screen observations were overlapping exposure,
not executable trade capacity. The development artifacts were reproduced byte-for-byte.

### Frozen ablation and one-time June-August replication

The development rank-1 rule was frozen without retuning. Removing only its OBI/combined-signal
tail left a momentum-only ablation with pooled net `+7.00 bps`, four positive development dates,
and worst-date net `-2.92 bps`. Restoring the OBI filter improved pooled net to `+9.65 bps`, all
five dates positive, and worst-date net `+4.52 bps`.

The frozen rule then opened June, July, and August exactly once. OBI+momentum returned `+7.00`,
`-3.98`, and `-1.44` bps by month, with pooled net `+0.40 bps`. Momentum-only pooled net was
`+7.40 bps` but failed badly in August at `-19.76 bps`. The frozen success gate failed because
OBI+momentum was positive in only one of three months and did not outperform momentum-only on a
pooled basis. No intraday branch was created, the exact capacity failure remains, and directional
Loop search stops here.

## Loop 7: capacity-feasible short momentum holds

Loop 7 kept the causal five-minute/one-hour momentum gates from Loop 6 but reduced maker-to-maker
holding periods to one and five minutes. These horizons can satisfy the one-position activity
ceiling and are closer to the intended HFT-like design.

Results:

- declared cells: `4,992`
- activity-eligible unique cells: `2,782`
- positive pooled cells after primary fees: `2`
- positive on all five development dates: `0`
- best pooled primary net: `+0.17 bps`
- best candidate positive dates: `1 / 5`
- best candidate worst date: `-5.10 bps`
- best one-minute primary net: `-3.28 bps`

The five-minute best case barely crossed the pooled fee boundary but failed on four dates. The
one-minute family did not cover costs. No candidate advances to exact execution, and later dates
remain unopened. Two executions produced byte-identical artifacts.

## Reproduction

```bash
.venv/bin/python -m pyresearch.obi.search thresholds
.venv/bin/python -m pyresearch.obi.search development
.venv/bin/python -m pyresearch.obi.persistent_search thresholds
.venv/bin/python -m pyresearch.obi.persistent_search development
.venv/bin/python -m pyresearch.obi.passive_entry_search thresholds
.venv/bin/python -m pyresearch.obi.passive_entry_search development
.venv/bin/python -m pyresearch.obi.maker_roundtrip_search thresholds
.venv/bin/python -m pyresearch.obi.maker_roundtrip_search development
.venv/bin/python -m pyresearch.obi.state_exit_search thresholds
.venv/bin/python -m pyresearch.obi.state_exit_search development
.venv/bin/python -m pyresearch.obi.momentum_gate_search thresholds
.venv/bin/python -m pyresearch.obi.momentum_gate_search development
.venv/bin/python -m pyresearch.obi.momentum_gate_search capacity
.venv/bin/python -m pyresearch.obi.short_momentum_search thresholds
.venv/bin/python -m pyresearch.obi.short_momentum_search development
.venv/bin/python -m pyresearch.obi.loop6_frozen_ablation development
# validation is intentionally single-use; subsequent checks use `verify`
.venv/bin/python -m pyresearch.obi.loop6_frozen_ablation verify
.venv/bin/python -m unittest tests.test_obi_profitability_search -v
```

Ignored machine-readable outputs are under
`data/research/tardis/reports/obi_profitability/`. The deterministic development threshold,
aggregate, ranking, and shortlist artifacts retain their SHA-256 hashes.
