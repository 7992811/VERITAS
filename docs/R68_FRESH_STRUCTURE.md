# R68: fresh structure and direct NQ quotes

The MOEX minute loader previously fetched ascending pages from two days ago. Its seven-second budget could expire before reaching the current session, while cache retrieval time looked fresh. R68 requests the newest hour first, then bounded earlier slices, and merges minutes by timestamp. Only complete closed five-minute buckets are emitted. A failed older request preserves the latest observations; freshness uses candle close time.

NQ no longer requests QQQ or uses proxy/quote-anchor candles in its structure. Public NASD100_FUT quotes retain their actual observation time. If direct current quotes are unavailable, delayed futures remain research data; the existing execution quote-age limit still blocks entries. Proxy quotes are rejected at source admission, entry/add, cached quote publication, mark selection, and protective execution.

A shared context check now rejects stale/incomplete structure even when no breakout event exists. Preliminary and final entry checks use the same event clock. Serialized event age cannot remain artificially zero; future retest timestamps cannot revive old events. Health exposes entry-context freshness per asset separately from service uptime.

Validation: 233 targeted Python regressions plus paper UI regressions passed. Live MOEX cold load returned 173 complete bars with an approximately four-minute-old close within its seven-second budget, retaining them after an older backfill timeout. Live NQ returned 500 native bars with zero synthetic anchors and no QQQ source. In that observation the public current quote was unavailable, so the adapter correctly retained delayed research data.

Paper-only changes. Risk limits, commissions and economics thresholds are unchanged. No historical trades were rewritten. These corrections do not establish profitable expectancy; that requires subsequent out-of-sample evidence after costs.
