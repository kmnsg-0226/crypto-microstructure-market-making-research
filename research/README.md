# Research evidence index

This directory contains frozen, compact evidence for the completed C++ crypto market
microstructure project. Raw captures and regenerated large datasets remain local and ignored.

- [`native/`](native/) indexes the native Binance Phase 1–6 studies: replay QC, OBI/flow,
  passive fills, queue tails, adverse selection, directional cost hurdles, and signal decay.
- [`native_executable_pnl/`](native_executable_pnl/) is the final six-capture executable-PnL
  validation: manifest, QC, OOF economics, feature importance, and frozen verdict.
- [`kraken_mbo_audit/`](kraken_mbo_audit/) records the order-level/FIFO feasibility audit.
- [`archive/`](archive/) retains superseded or auxiliary evidence, including the former
  aggregate-trade baseline and detailed historical documentation.

The portfolio-facing narrative is in [`../docs/`](../docs/) and the final project entry point is
[`project_final/`](project_final/).
