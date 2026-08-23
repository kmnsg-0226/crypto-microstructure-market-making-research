# Binance intraday-alpha data feasibility audit

Targets: BTCUSDT, ETHUSDT, SOLUSDT USDⓈ-M perpetuals. The audit used 20 REST rows per symbol/dataset and Vision S3 metadata only; no historical ZIP archive was downloaded.

## Coverage

| Dataset | Free source | Earliest → latest observed | Frequency / limitations | Bulk download |
|---|---|---|---|---|
| 1m OHLCV | REST `/fapi/v1/klines`; Vision `data/futures/um/daily/klines/{symbol}/1m/` | BTC/ETH 2019-12-31 → 2026-05-15; SOL 2020-09-14 → 2026-05-15 | 1m; REST bounded by request limit (max 1,500) and pagination | Yes, Vision daily ZIPs |
| 5m OHLCV | REST `/fapi/v1/klines`; Vision `.../5m/` | Same instrument dates as 1m | 5m; same REST pagination limit | Yes, Vision daily ZIPs |
| aggTrades | REST `/fapi/v1/aggTrades`; Vision `data/futures/um/daily/aggTrades/{symbol}/` | BTC/ETH 2019-12-31 → 2026-05-15; SOL 2020-09-14 → 2026-05-15 | Aggregate trades, not raw individual matches; REST historical search window is restricted to recent 2 days | Yes, Vision daily ZIPs; files are materially larger |
| Funding | REST `/fapi/v1/fundingRate` | BTC/ETH earliest returned: 2020-07-18; SOL: 2020-09-13; latest sample: 2026-08-22 16:00 UTC | Event-time funding records, normally 8h in the sample; REST response limit is 1,000 and must be queried in time windows | No funding-rate Vision archive found under the tested prefix |
| Mark price | REST `/fapi/v1/markPriceKlines`; Vision `.../markPriceKlines/{symbol}/5m/` | BTC/ETH 2019-12-23 → 2026-05-15; SOL 2020-09-13 → 2026-05-15 | 5m; REST pagination/request limits | Yes, Vision daily ZIPs |
| Index price | REST `/fapi/v1/indexPriceKlines`; Vision `.../indexPriceKlines/{symbol}/5m/` | BTC/ETH 2019-12-23 → 2026-05-15; SOL 2021-01-01 → 2026-05-15 | 5m; pair-scoped REST endpoint | Yes, Vision daily ZIPs |
| Premium index | REST `/fapi/v1/premiumIndexKlines`; Vision `.../premiumIndexKlines/{symbol}/5m/` | BTC/ETH 2019-12-24 → 2026-05-15; SOL 2020-09-14 → 2026-05-15 | 5m premium-index OHLC-style series; REST request limits | Yes, Vision daily ZIPs |
| Open interest history | REST `/futures/data/openInterestHist`; Vision `data/futures/um/daily/metrics/{symbol}/` | BTC 2020-09-01; ETH/SOL 2021-12-01 → 2026-05-15 | 5m sample; REST boundary parameters were rejected, so REST is recent-history only in practice; Vision metrics also contain other market metrics | Yes, Vision daily metrics ZIPs |

## Sample validation

- All three symbols returned 20 rows for 1m, 5m, mark, index, premium, and open-interest samples; funding and aggTrades also returned 20 rows.
- 1m, 5m, mark, index, premium, and open-interest samples had the expected 60,000ms or 300,000ms spacing with no obvious gaps.
- Funding samples were approximately 28,800,000ms apart with only millisecond timestamp noise.
- AggTrade timestamps were irregular by design; no obvious API failure was observed in the small samples.
- Funding and open-interest rows include a `symbol` field. Kline and aggTrade arrays do not; their symbol consistency is guaranteed only by the symbol-scoped request/path, so the audit records that scope explicitly.
- All samples use exchange timestamps in milliseconds. They can be joined causally without lookahead by flooring to the target bar, using only observations with timestamp at or before the decision time, and lagging close-derived features by one complete bar.

## Recommended first backtest

Use a common 5m clock and this small feature set:

1. 5m OHLCV as the base; retain 1m OHLCV only for constructing intrabar/15m/30m/60m aggregates.
2. 5m mark, index, and premium-index bars for basis/premium features.
3. 5m open interest from Vision metrics for the historical backtest window.
4. Funding events as-of their funding timestamp; never forward-fill them into pre-event bars.
5. Aggregate aggTrades into 5m signed volume/count/imbalance features if trade-flow information is needed.

This supports 5m, 15m, 30m, and 60m directional labels without needing another data source. The practical backtest cutoff for bulk-aligned data is the Vision latest date observed here, 2026-05-15; newer data needs REST stitching or a later archive refresh.

## Verdict: A

There is enough free, multi-year history for a robust first single-venue intraday-alpha V1. Funding lacks a tested Vision bulk archive and open-interest REST history is limited, but both have usable free alternatives for the historical window; these are implementation constraints, not blockers for the initial OHLCV/trade-flow/basis/OI backtest.
