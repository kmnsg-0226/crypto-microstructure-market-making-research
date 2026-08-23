# Sweep risk as a directional signal, native_dev_v1

The full report lives with the other phase reports at
[`docs/native_directional_sweep_v1.md`](../archive/docs/native_directional_sweep_v1.md), following the
repository convention that narrative reports sit under `docs/` and machine-readable artifacts
under `research/`.

**Verdict: A — directional monetisation falsified.** The price information is real, monotone and
stable in 120 of 120 chronological blocks, but the best cell in the whole pre-registered grid
moves 0.87 bp against a fixed hurdle grid that starts at 1 bp one way. Every one of the 96
threshold × horizon × hurdle cells is negative.

| File | Contents |
|---|---|
| `methodology.json` | pre-registration, signal provenance, direction convention, input hashes, git commit |
| `folds.csv` | chronological folds; one fewer than earlier phases, because the first scored block is training-only |
| `frame_qc.json` | reconciliation of the mid-path reconstruction against the frozen phase 1 markouts |
| `sweep_deciles.csv` | fixed decile study: realised sweeps, next-move direction, markouts at six horizons |
| `decile_monotonicity.csv` | which decile orderings are monotone and which are not |
| `gross_edge.csv` | the four fixed thresholds against the full opportunity denominator |
| `probability_magnitude.csv` | E[R] split into probability and magnitude terms, by score band and side |
| `conditional_vs_unconditional.csv` | decision-time versus sweep-conditional outcomes, separately labelled |
| `event_study.csv` | model-free event-time paths around consumption, disappearance and improvement |
| `signal_lead_time.csv` | crossing to first same-direction mid move, and crossing to actual consumption |
| `false_positive_analysis.csv` | what a high score returns when the sweep does not happen |
| `direction_model_comparison.csv` | OBI, sweep alone, book+flow, book+flow+sweep, blocked OOF |
| `incremental_information.csv` | nested comparisons with block-bootstrapped delta AUC |
| `residual_diagnostic.csv` | sweep score residualised against the controls; diagnostic only |
| `magnitude_model.csv`, `magnitude_buckets.csv` | magnitude models and realised-by-predicted buckets |
| `calibration.csv`, `fold_metrics.csv` | calibration and per-fold detail |
| `cost_hurdle.csv` | gross movement against the fixed one-way and round-trip hurdle grid |
| `break_even_cost.csv` | maximum all-in cost the movement could absorb, in ticks, USD/BTC and bp |
| `side_asymmetry.csv` | threatened ask versus threatened bid, reported separately |
| `activity_regimes.csv` | fixed causal quantile buckets of movement, trade and depth intensity, spread |
| `block_stability.csv`, `block_stability_summary.csv` | 120 chronological blocks, block-bootstrapped |
| `day_stability.csv`, `segment_stability.csv` | the same by UTC day and by segment |

Heavy intermediates are ignored and live under `data/research/native_directional_sweep_v1/`.

Every number is a blocked out-of-fold **development** estimate. Nothing here is out of sample.
The cost hurdles are labelled sensitivities, not a live account fee tier. The rotation-enabled AWS
capture, every later AWS file and the Tardis holdout remain untouched.
