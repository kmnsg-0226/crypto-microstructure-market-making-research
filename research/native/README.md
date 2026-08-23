# Native Binance research index

The native Phase 1–6 evidence remains in its original directories to preserve imports, scripts,
and reproducibility.

| Phase | Question | Evidence |
|---|---|---|
| Corpus | Is native replay sequence-safe? | [`native_dev_v1`](../native_dev_v1/), [`docs`](../archive/docs/native_dev_v1_corpus.md) |
| 1–2 | Price formation, fills, adverse selection | [`native_predictive_v1`](../native_predictive_v1/), [`docs`](../archive/docs/native_predictive_v1.md) |
| 3 | Passive maker economics | [`native_economic_v1`](../native_economic_v1/), [`docs`](../archive/docs/native_economic_v1.md) |
| 4 | Queue tails and sweep/cancel risk | [`native_queue_tail_v1`](../native_queue_tail_v1/), [`native_cancel_falsification_v1`](../native_cancel_falsification_v1/) |
| 5 | Directional sweep monetisation | [`native_directional_sweep_v1`](../native_directional_sweep_v1/) |
| 6 | Signal decay and horizon extension | [`native_decay_v1`](../native_decay_v1/) |

The final standalone executable-PnL falsification is in
[`../native_executable_pnl/`](../native_executable_pnl/).
