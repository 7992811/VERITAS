# R79 GOLD / NQ public quote audit — 2026-10-05

## Confirmed parsing defect

ProFinance's public widget uses a leading `+` / `-` in LP as display formatting.
Its `q_show.js` calls `setValueToTdById` with `hide_sign=true` for LP, and removes
one leading sign. VERITAS previously parsed the sign as a negative price and
discarded downward updates, causing intermittent fallback to older sources.

Provider implementation inspected:
https://jq.profinance.ru/html/htmlquotes/js/q_show.js

Observed protocol records at 16:52 Moscow (13:52 UTC):

```
S=NASD100_FUT;LP=-31178.00;T=16:52:30
S=Gold;LP=-4142.88;T=16:52:31
```

The parser now accepts exactly one optional leading sign, retains the raw value
for audit, and still rejects zero, malformed/non-finite prices and stale clocks.
The public feed still has an inferred date and unverified exact contract. This
does not promote it to a broker execution feed.

## Actual instrument mapping

| Asset | Public current quote | Historical / fallback anchor |
| --- | --- | --- |
| NQ | ProFinance `NASD100_FUT` (not `NASD100` index) | Yahoo `NQ=F`, Nasdaq 100 Dec 26 in retrieved metadata |
| GOLD | ProFinance `Gold`, no verified expiration/contract | Yahoo `GC=F`, Gold Dec 26 in retrieved metadata |

Yahoo chart metadata retrieved during this audit carried latest trade timestamps
around 13:41 UTC, over ten minutes behind the ProFinance sample above. Prices
from these different timestamps do not establish a same-time basis difference.

The GOLD adapter also retains its existing GLD proxy fallback. An unqualified
ProFinance Gold quote, GLD-derived estimate and a dated GC futures price must not
be assumed to be the same instrument merely because their prices are close.
The audit does not establish whether ProFinance Gold is spot or a specific future.

## Protective exit correction

The independent guard previously fetched Yahoo GC=F for all GOLD positions,
including positions entered from ProFinance. It now requests ProFinance Gold
when all GOLD positions carry that entry source. If unavailable, only a cached
quote with the same source identity can be used, subject to existing freshness
checks. It does not substitute GC=F into those stops.

A per-position source check precedes protective path, profit-lock, stop/target
and fill-quote use. Different explicit contract IDs remain rejected. Known
Yahoo GC entries cannot use ProFinance Gold either. Legacy entries without
source metadata retain their previous behavior. Mixed-source books still use
the existing one-quote-per-asset architecture: unmatched positions are skipped,
not repriced across sources. Exact contract reconciliation remains outstanding.

No positions, historical P&L, stop distances or model limits were rewritten.

## Validation

38 targeted tests pass: protocol parsing, source-consistent protection, R79
continuation, NQ futures-versus-index selection and existing protective exits.
The three failing assertions/errors in `test_veritas_fresh_structure` also
reproduce on the pre-change main snapshot; they are unrelated to this patch.
