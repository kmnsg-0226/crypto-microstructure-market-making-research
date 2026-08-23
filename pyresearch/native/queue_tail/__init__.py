"""Phase 4A: observable queue dynamics and catastrophic passive-fill risk.

Two questions, both descriptive or predictive and neither of them a strategy:

    A. can observable best-price level dynamics replace the free queue-position parameter,
    B. can the catastrophic tail of passive fills be predicted from placement-time state.

Nothing here claims true queue position. Binance publishes aggregated L2: no order ids, no FIFO
rank, no clean separation of cancellation from execution. Level age is a lifecycle observable,
never an inferred queue rank.
"""
