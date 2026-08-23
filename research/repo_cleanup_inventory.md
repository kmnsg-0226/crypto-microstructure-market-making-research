# Repository cleanup inventory

Created before any move or deletion. This inventory is intentionally conservative: research
results, including negative results, are retained.

## A. Core source code

- `cpp/` — C++20 collector, Binance protocol/parser/synchronizer, integer order book, raw
  storage, deterministic replay, feature exporters, research observers, and CLI applications.
- `native_research/`, `native_predictive/`, `native_queue_tail/`, `native_economic/`,
  `native_cancel/`, `native_directional/`, `native_decay/`, and `native_executable_pnl/` —
  native-capture research orchestration and analysis.
- `historical/` and `event_research/` — historical L2 adapters and event-time research.
- `clients/`, `market/`, `execution/`, `execution_research/`, `portfolio/`, `risk/`,
  `storage/`, `strategy/`, and `monitoring/` — retained Python prototype and reusable support
  code. It is historical context, not the portfolio project's advertised trading system.
- `passive_research/` and `obi_research/` — older research implementation retained for the
  negative feasibility evidence.
- `scripts/` — reproducible native research entry points.

## B. Tests

- `cpp/tests/` — deterministic C++ fixtures and replay/synchronization tests.
- `tests/` — Python tests for collector semantics, research boundaries, execution economics,
  and native research phases.

## C. Final/current research

- `research/native_executable_pnl/` — frozen six-capture development manifest, QC, OOF
  executable-PnL economics, cost gate, and final negative verdict.
- `research/kraken_mbo_audit/` — Kraken MBO/FIFO feasibility audit and follow-up.
- `research/specs/native_dev_v1.json` and the frozen research specifications — compact
  reproducibility contracts.
- `docs/native_dev_v1_corpus.md`, `docs/native_predictive_v1.md`,
  `docs/native_queue_tail_v1.md`, `docs/native_economic_v1.md`,
  `docs/native_cancel_falsification_v1.md`, `docs/native_directional_sweep_v1.md`, and
  `docs/native_decay_v1.md` — primary native Phase 1–6 evidence.

## D. Older but important research evidence

- `research/native_dev_v1/`, `research/native_predictive_v1/`,
  `research/native_queue_tail_v1/`, `research/native_economic_v1/`,
  `research/native_cancel_falsification_v1/`, `research/native_directional_sweep_v1/`, and
  `research/native_decay_v1/` — phase outputs retained as evidence, not deleted as failures.
- `research/intraday_alpha_v1/`, `research/intraday_alpha_audit/`,
  `research/funding_basis_audit/`, and `research/results/` — older contextual results.
- `docs/obi_profitability_loop.md`, `docs/continuous_inventory_mm.md`,
  `docs/passive_approach_exploration.md`, `docs/passive_fill_adverse_selection_research.md`,
  `docs/execution_economics_research.md`, and `docs/event_model_comparison.md` — earlier
  feasibility/falsification evidence.
- `backtest/` and its `results/` directory — legacy aggregate-trade backtests; retained as
  historical evidence, not presented as current native-L2 conclusions.

## E. Raw/local data

- `data/raw/`, `data/historical/`, `data/cryptohft_test/`, and `data/research/` — local raw,
  cached, and large derived data. They are intentionally ignored.
- Root `BTCUSDT-aggTrades-*` CSV/ZIP files — local legacy aggregate-trade inputs; they should
  be ignored explicitly and never moved or deleted by this cleanup.
- `*.chft.zst` files — immutable native raw captures; never modified or deleted.

## F. Generated outputs

- `build/` — CMake-generated build products.
- `data/research/` — regenerated feature exports, frames, OOF predictions, and trade tapes.
- `research/*` CSV/JSON/Markdown results — compact review artifacts; current and negative
  results are retained for version control where useful.
- `graphify-out/` — generated graph/index report and cache, not research evidence.

## G. Temporary/debug/obsolete artifacts

- `__pycache__/` and `*.pyc` outside `.venv/` — disposable Python bytecode.
- `.venv/` — local virtual environment, ignored.
- `bot.db` — local SQLite runtime database, ignored.
- `graphify-out/cache/` — disposable generated tool cache.
- `.vscode/` — local editor configuration; retained untouched because ownership is unclear.

## H. Unclear / intentionally retained

- `main.py`, `config.py`, and the paper-trading package tree — retained as historical prototype
  context; the README now separates it from the C++ microstructure research identity.
- `backtest/results/` — no destructive cleanup: these older outputs may be useful provenance.
- Existing uncommitted files and modifications visible in `git status` — preserved exactly;
  they predate this cleanup and include the native research implementation and evidence.

## Cleanup decision

No source or research output requires a risky bulk move. The cleanup will add stable indexes and
top-level documentation, strengthen ignore rules, and remove only disposable bytecode/cache
artifacts after verification. `graphify-out/` remains local/generated and is ignored rather than
deleted, preserving the user's current workspace.

## Post-inventory disposition

The legacy Python paper-trading package tree, its entry points, and the aggregate-trade backtest
code were subsequently retired from the public root. Historical aggregate-trade results moved to
[`archive/legacy_aggregate_trade_baseline/`](archive/legacy_aggregate_trade_baseline/); ignored
local `backtest/data/` was not moved or modified.

The Python research packages listed above were subsequently consolidated under
[`../pyresearch/`](../pyresearch/), and detailed phase documents moved to
[`archive/docs/`](archive/docs/). The original listings remain as the pre-move inventory record.
