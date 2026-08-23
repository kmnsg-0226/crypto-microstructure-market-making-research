# Passive maker economic feasibility, native_dev_v1

The full report lives with the other phase reports at [`docs/native_economic_v1.md`](../archive/docs/native_economic_v1.md),
following the repository convention that narrative reports sit under `docs/` and machine-readable
artifacts under `research/`.

Artifacts in this directory:

| File | Contents |
|---|---|
| `methodology.json` | pre-registration, α/β grid, input hashes, git commit |
| `queue_sensitivity_surface.csv` | the fixed 5×5 α/β grid: fills, mechanism, markouts, break-even benefit |
| `queue_sensitivity_by_block.csv` | the same by phase 2 validation block, UTC day and segment |
| `break_even_benefit.csv` | required execution benefit in ticks, USD per BTC and basis points |
| `break_even_block_bootstrap.csv` | block-bootstrap intervals on the headline break-even number |
| `fill_mechanism_decomposition.csv` | E[M \| fill] split into trade-through and at-quote parts |
| `markout_paths.csv` | markout at 100 ms / 500 ms / 1 s / 5 s, by mechanism and side |
| `oof_toxicity_deciles.csv` | frozen phase 2 toxicity deciles under three queue assumptions |
| `oof_fill_deciles.csv` | frozen phase 2 fill-probability deciles under the same |
| `oof_joint_surface.csv` | 5×5 fill × toxicity surface under the same |
| `activity_regimes.csv` | trade intensity, realized activity and spread state buckets |
| `side_asymmetry.csv` | every headline statistic split by bid and ask |
| `queue_qc_file{0,1,2}.json` | replay QC and removal-attribution counters per raw file |

Nothing in this phase selects a threshold, a horizon, a queue assumption or a state to trade.
All results are development estimates; none is out of sample.
