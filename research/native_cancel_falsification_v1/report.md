# Counterfactual sweep-risk cancel / stay falsification, native_dev_v1

The full report lives with the other phase reports at
[`docs/native_cancel_falsification_v1.md`](../archive/docs/native_cancel_falsification_v1.md),
following the repository convention that narrative reports sit under `docs/` and
machine-readable artifacts under `research/`.

**Verdict: A — falsified.** Under the conservative and midpoint queue assumptions, cancelling a
resting order on high out-of-fold sweep probability is indistinguishable from deleting fills at
random, and by several measures slightly worse. It is informative only at the extreme optimistic
queue bound, where most crossings still arrive after the fill.

| File | Contents |
|---|---|
| `methodology.json` | pre-registration, signal provenance, causal rules, input hashes, git commit |
| `grid_spec.json` | the fixed threshold, latency and queue grids and the 27 headline cells |
| `folds.csv` | chronological fold boundaries, identical geometry to phases 2, 3 and 4A |
| `score_provenance.csv` | per-fold agreement between the refit and phase 4A's stored OOF predictions |
| `signal_coverage.json` | how much of a resting order's window carries a usable score at all |
| `sweep_score_deciles.csv` | fixed decile study of the OOF score against passive economics, per queue cell |
| `threshold_latency_surface.csv` | the whole queue × threshold × latency grid, plus the never-cancel baseline |
| `headline_cells.csv` | the 27 pre-registered headline cells |
| `avoided_vs_sacrificed.csv` | adverse avoided, favourable sacrificed, net preserved and the random-benchmark lifts |
| `tail_protection.csv` | baseline versus surviving markout distributions and catastrophic exposure |
| `signal_lead_time.csv` | distance from first threshold crossing to the never-cancel fill |
| `signal_persistence.csv` | whether the crossing is advance warning or a concurrent signature |
| `mechanism_decomposition.csv` | at-quote versus trade-through, per headline cell |
| `queue_transport.csv` | the same statistics across the three queue assumptions side by side |
| `threshold_monotonicity.csv` | structural ordering across the nine fixed thresholds |
| `block_stability.csv`, `block_stability_summary.csv` | 120 chronological blocks, block-bootstrapped |
| `day_stability.csv`, `segment_stability.csv` | the same by UTC day and by segment |

Heavy intermediates are ignored and live under `data/research/native_cancel_falsification_v1/`.

Every number is a blocked out-of-fold **development** estimate. Nothing here is out of sample.
The rotation-enabled AWS capture, every later AWS file and the Tardis holdout remain untouched.
