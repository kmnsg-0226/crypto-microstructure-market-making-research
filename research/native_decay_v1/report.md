# Signal decay and horizon extension, native_dev_v1

The full report lives with the other phase reports at [`docs/native_decay_v1.md`](../archive/docs/native_decay_v1.md),
following the repository convention that narrative reports sit under `docs/` and machine-readable
artifacts under `research/`.

**Verdict: B — longer-horizon information survives but is marginal.** The incremental edge after
the five-second pivot is resolved, positive and block-stable at 10 s, 30 s and 60 s for every
signal tested, and 72–88 % of the cumulative long-horizon markout is nonetheless inherited from
the first five seconds. Nothing resolves at 120 s or beyond. The largest break-even all-in cost
anywhere in the study is 0.693 bp, and 0 of 486 cost cells clear even the cheapest one-way hurdle
of 1 bp.

| File | Contents |
|---|---|
| `methodology.json` | pre-registration, signals, purge and bootstrap geometry, input hashes |
| `target_agreement.json` | the gate: regenerated 1 s / 5 s against the frozen phase 1 columns, 2 s against the phase 5A reconstruction |
| `decay_profile.csv` | signed markout, hit rate and effective sample per signal × horizon, raw and demeaned |
| `signal_deciles.csv` | fixed decile study of every signal against every horizon |
| `decile_monotonicity.csv` | where the decile ordering survives and where it breaks |
| `cumulative_incremental.csv` | the decision quantity: cumulative and incremental-after-5 s decile spreads, with horizon-aware block bootstrap, block sign share and the non-overlapping anchor check |
| `reconciliation.csv` | cumulative = pivot + incremental, on each horizon's own population |
| `up_down_legs.csv` | every headline statistic split into up and down legs, raw and demeaned |
| `stability.csv` | the same by UTC day and by segment |
| `effective_sample.csv` | evaluated rows, non-overlapping anchors, bootstrap blocks and purge per horizon |
| `cost_hurdle.csv` | gross edge and break-even all-in cost against the fixed 1/2/3/5/7.5/10 bp grid |
| `classification.csv` | each signal against the five pre-registered decay classes |
| `verdict.json` | the project verdict against the pre-registered A–E rules |

The heavy frame lives under `data/research/native_decay_v1/decay_frame.parquet`.

No model is trained in this phase, no horizon, threshold or decile is selected as best, and no
PnL, Sharpe, entry rule or holding period exists anywhere in it. Every number is a blocked
out-of-fold **development** estimate; nothing here is out of sample. The rotation-enabled AWS
capture, every later AWS capture and the Tardis holdout remain untouched, and the known
`passive_binary_sha256` audit failure was left unmodified.
