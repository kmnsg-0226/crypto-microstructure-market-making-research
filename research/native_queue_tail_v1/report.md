# Observable queue dynamics and catastrophic fill risk, native_dev_v1

The full report lives with the other phase reports at
[`docs/native_queue_tail_v1.md`](../archive/docs/native_queue_tail_v1.md), following the repository
convention that narrative reports sit under `docs/` and machine-readable artifacts under
`research/`.

| File | Contents |
|---|---|
| `methodology.json` | pre-registration, feature groups, fixed thresholds, input hashes, git commit |
| `queue_feature_schema.json` | feature sets, exact target definitions, causality and terminology rules |
| `level_episodes.csv` | episode counts and statistics by side, close reason and duration bucket |
| `level_survival_summary.csv` | level lifetime distribution by side and close reason |
| `depletion_replenishment_summary.csv` | how displayed depletion reconciles against aggressive prints |
| `hazard_curve.csv` | discrete-time hazard of level failure and sweep by level-age bucket |
| `sweep_risk_summary.csv` | level-failure and trade-through base rates by side and segment |
| `queue_feature_summary.csv` | distribution of every lifecycle feature |
| `tail_distribution.csv` | markout distribution and catastrophic rates per cohort and queue cell |
| `tail_contribution.csv` | share of total adverse markout from the worst 1 %, 5 % and 10 % of fills |
| `queue_bucket_studies.csv` | fixed decile studies of lifecycle signals against every outcome |
| `queue_interaction_studies.csv` | fixed 5×5 interaction studies |
| `level_birth_cohort.csv` | the level-birth cohort, including ex-post lifecycle decomposition |
| `folds.csv` | chronological fold boundaries, identical geometry to phases 2 and 3 |
| `level_failure_metrics.csv` | blocked OOF metrics for P(level disappears within h) |
| `sweep_model_metrics.csv` | blocked OOF metrics for P(trade-through within h) |
| `catastrophic_model_metrics.csv` | blocked OOF metrics for catastrophic_25 and catastrophic_50 |
| `tail_severity_metrics.csv` | blocked OOF metrics for conditional tail severity |
| `feature_ablation.csv` | the pre-registered four-way feature-group comparison |
| `model_coefficients.csv`, `feature_importance.csv` | interpretation |
| `calibration.csv`, `fold_metrics.csv` | calibration and per-fold detail |
| `level_qc_file{0,1,2}.json`, `birth_qc_file{0,1,2}.json` | replay QC per raw file |

Row-level datasets live under `data/research/native_queue_tail_v1/`.

Nothing in this phase selects a threshold, a state, a queue assumption or a quoting rule. All
results are development estimates; none is out of sample. Level age is a lifecycle observable and
is never treated as a queue rank; unexplained displayed removal is never called cancellation.
