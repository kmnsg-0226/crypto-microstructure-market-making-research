# Frozen continuous-inventory market making

This experiment compares neutral and OBI-aware market making without changing the live
collector or placing orders. The complete policy was committed before its first outcome run.

## Execution and inventory semantics

- one-second decision cadence and one-second fixed BBO quote lifetime
- `0.005 BTC` per quote, at most one active order per side
- pessimistic visible-queue labels; both full and partial labeled quantities are booked
- full quantity is recognized at first fill time conservatively, while the order remains active
  until its labeled full-fill time
- soft absolute inventory limit `0.015 BTC`; hard limit `0.025 BTC`
- neutral quotes both sides subject only to inventory and active-order restrictions
- OBI-aware suppresses the toxic side at `|weighted_obi_l10| >= 0.8`, but never suppresses an
  inventory-reducing quote for OBI reasons
- maker fee `2 bps`; nonzero day-end inventory is crossed at the actual opposite BBO with a
  `5 bps` taker fee
- no depth slippage or impact is added to forced liquidation, so results remain optimistic there

## Frozen chronological results

| split | policy | fills | turnover USDT | gross PnL USDT | net PnL USDT | net bps | positive days |
|---|---|---:|---:|---:|---:|---:|---:|
| Jan-Mar development | neutral | 67,818 | 25,014,411 | -2,864.99 | -7,869.44 | -6.29 | 0 / 3 |
| Jan-Mar development | OBI-aware | 58,653 | 21,586,803 | -2,538.71 | -6,857.46 | -6.35 | 0 / 3 |
| Apr-May validation | neutral | 37,351 | 13,408,094 | -1,414.00 | -4,096.87 | -6.11 | 0 / 2 |
| Apr-May validation | OBI-aware | 31,487 | 11,307,315 | -1,227.52 | -3,489.98 | -6.17 | 0 / 2 |

Both policies were gross-negative before fees. OBI suppression reduced validation turnover by
about 15.7% and absolute net loss by about 14.8%, but net performance per one-way notional was
slightly worse. Neither policy had a positive day. Both kept the hard inventory-limit violation
count at zero and included forced liquidation and partial fills.

The frozen validation gate failed its per-day and pooled-profit conditions. June-August MM
replication therefore remains unopened, as required by the preregistered run policy. This MM
version is rejected; its lower absolute loss is risk reduction, not profitable edge.

## Commands

```bash
.venv/bin/python -m pyresearch.obi.continuous_mm audit
.venv/bin/python -m pyresearch.obi.continuous_mm development
# validation is intentionally single-use; use verify afterward
.venv/bin/python -m pyresearch.obi.continuous_mm verify
.venv/bin/python -m unittest tests.test_continuous_mm -v
```
