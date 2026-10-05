# R87 — three exact futures and all matrix timeframes

The T-Invest read-only adapter now defaults to these independent broker feeds:

| Display label | API asset key | Exact broker ticker | Venue / contract |
| --- | --- | --- | --- |
| CNYRUBf | CNYRUBF | CNYRUBF | MOEX perpetual currency future |
| NQf | NQF | NAZ6 | MOEX NASD-12.26, December 2026 contract |
| MOEXf | MOEXF | IMOEXF | MOEX perpetual index future |

**NQF here is the specific MOEX NASD contract, not the CME NQ future or a
continuous synthetic series.** It is not silently substituted for VERITAS's
existing NQ source. MOEXF is separate from the cash index. Existing paper
positions, source bindings, signal calculations and cost rules are unchanged.
No order, cancellation or transfer methods are enabled.

Each ticker must resolve to exactly one UID. GetInstrumentBy and FutureBy must
agree on ticker and UID. The future's real exchange must be MOEX, tick size and
lot must exist, and a dated future must have a future expiration date. A missing,
ambiguous, expired or mismatched instrument is reported; no alternative ticker
or expiry is selected automatically. Contract metadata is visible on the status
page. Prices remain in the broker's native points, with lot and tick values
reported separately.

## Timeframes and history

| Interval | History requested | Refresh |
| --- | --- | --- |
| 1m | 1 day | 20 seconds |
| 5m | 7 days | 60 seconds |
| 1h | 90 days | 5 minutes |
| 4h | 90 days | 15 minutes |
| 1d | 5 years, limited by this contract's actual history | 1 hour |
| 3d | Derived from the same native daily history | With 1d |
| 7d | Native broker weekly bars, up to 5 years | 1 hour |

All native requests use exchange candles and the documented per-interval request
limits. Only complete, valid, non-future OHLCV bars are cached, sorted and
deduplicated by timestamp. Empty history is NO_DATA, not a ready source. A failed
interval is ERROR without preventing other instruments/intervals from loading.
Old successful caches become STALE if no timely download confirms them.

3d uses fixed three-calendar-day buckets anchored to Unix epoch at midnight UTC.
It sums actual daily volume and uses first open / highest high / lowest low /
last close. The current incomplete bucket and the initial partially covered
bucket are omitted. Non-trading dates are not filled and a bucket reports its
actual source-bar count. This is explicitly derived history, not native broker
3d candles. 7d means a broker calendar week, not a rolling seven-day window.

History is never extended by stitching other expiries or another provider. The
requested lookback is an upper bound; actual history starts when the particular
contract's candles become available. Availability is separate from whether the
number of bars is sufficient for a strategy.

Cold intervals are prioritized before refresh work. A pass makes at most twelve
history requests with a thirty-second scheduling budget (plus any already-running
eight-second RPC). Accounts refresh at most once a minute. The worker refreshes
quotes/books approximately every twenty seconds; one last-price stream covers
the resolved UIDs and reconnects when the resolved set changes.

## Configuration and inspection

The existing `TBANK_API_TOKEN` is reused in place, never read into source code or
returned by an endpoint. Optional `TBANK_TICKERS` replaces the default map; to
select the three feeds explicitly, use:

```json
{"CNYRUBF":"CNYRUBF","NQF":"NAZ6","MOEXF":"IMOEXF"}
```

- `/integrations/tbank` shows each exact contract and seven interval counts.
- `/api/v1/integrations/tbank` includes per-contract/per-interval state and dates.
- `/api/v1/integrations/tbank/candles?asset=NQF&interval=1m` returns cached bars.
- Private account and portfolio routes still require the separate application
  authentication token and fail closed without it.

The runtime uses the same read-only protobuf provenance and scoped TLS roots as
R83. Future and FutureResponse are added from the same published protocol commit.

## Validation

56 local tests pass across the T-Invest, R82 cost, profit protection and R75 floor
suites. They cover an actual local gRPC FutureBy exchange, exact identity/venue
and expiry validation, request limits, scheduling, isolated history failures,
closed-candle filtering, 3d OHLCV/boundaries, stale caches and private HTTP access.
Live response availability must additionally be confirmed after deployment.
