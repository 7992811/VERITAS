# Currency: whole-account LIVE authority

This document describes `veritas_currency_live_admission.py`. The module is an
admission dependency, with no broker order, cancellation, Telegram or deployment
operation. An audited evidence receipt is never a trade permission.

## Runtime wiring

The operational application constructs the authority with the same authenticated
adapter, explicit account and PostgreSQL connection factory as the coordinator:

```python
from veritas_currency_live_admission import create_live_admission

authority = create_live_admission(connect, adapter, account_id, clock=clock)
coordinator = CurrencyTradingCoordinator(
    # Existing repository, owner, facts and execution dependencies...
    live_admission=authority,
    preflight_live=(adapter.environment == "production"),
)
```

The coordinator calls `authority(terms=approved_terms, facts=fresh_facts, now=now)`
for production OPEN/ADD. It must still validate the approval, canonical event,
fresh broker facts, account allocation, idempotency and execution switches.
CLOSE/REDUCE and protective exits retain their existing reducing-only flow;
missing model evidence cannot prevent such exits through this new-risk gate.

`status()` returns cached diagnostics and performs no broker or database I/O.
Before a candidate has been checked, it returns `NOT_EVALUATED`, `eligible=false`,
`LIVE_MODEL_EVIDENCE_NOT_CHECKED`, `LIVE_ACCOUNT_HISTORY_EVIDENCE_NOT_CHECKED`, and
the list of required domains. It also reads the current LIVE enabled/armed
switches and reports their blockers immediately. A cached pass expires at the
earliest quote, account-snapshot or evidence deadline and is explicitly labelled
`admission_cached_only`. Never authorize an order from this display response.

The production factory enables `preflight_live`: it calls the authority with
the concrete prepared candidate and fresh `TradeFacts` before creating a
proposal for delivery. A known blocked candidate does not ask the owner to
approve. That call reads the broker and stores a real account observation; it
sends no order. The final execution path calls the authority again.

The console's `execution_guard` holds the same configuration-row lock used by
pause/disable across the final checks, durable claim, single submit and receipt.
The coordinator rechecks quote/event/admission expiry after guard acquisition,
after the claim, and through the adapter's final `pre_send_check` after broker
preflight and journal reservation. Only the adapter's explicit
`PreSubmissionBlocked` type proves that those reads did not reach an order
mutation. A definite unsent claim is terminally blocked by
`abort_unsubmitted_claim` with its original consumed UUID and audit reason;
it is not labelled a broker rejection. Any uncertainty after an attempted
submission preserves the durable barrier and requires reconciliation.

## What is actually checked

The authority reads the explicit account's accessible/open/full-access status,
GetPortfolio, GetPositions, all working orders and all active stop orders. It
cross-checks portfolio positions against account positions and rereads positions
and orders after metadata/quote work. A change during the check causes refusal.
Equity is `GetPortfolio.totalAmountPortfolio` denominated in RUB. Currency's
10,000-ruble allocation, free cash and guarantee margin are not substitutes for
whole-account equity.

The candidate's exact future UID, MOEX venue, RUB denomination, lot size, tick
size, tick value and permitted direction are checked against the broker's
instrument response. The actual bid/ask quote must be current. Only after these
checks does the authority construct the broker/direct-feed source gate. It
does not copy a paper source permission into production.

The initial supported account is RUB cash and RUB-settled MOEX futures. Other
currencies, securities, options and virtual exposures produce explicit blockers
with the affected UID/currency. They are never dropped from risk calculations.
Future support for these holdings requires their own valuation, protective-risk
and currency conversion evidence.

Any ordinary working order anywhere in the account blocks new risk until it is
reconciled. An active stop on another supported future is recognized as
protection only when it is the sole STOP_LOSS for the exact UID, points opposite
the entire held quantity and is not about to expire/already triggered. Its
reported RUB stop value is converted using that future's actual tick value and
tick size. Unmatched, partial, oversized and stop-limit orders block. No stop is
created, canceled or edited here.

For ADD, the candidate passed to the existing LIVE risk gate is the full
resulting CNY exposure; the held CNY position is not counted a second time.
The saved held stop, source and timeframe must match. Every other holding
contributes notional, stop risk and modeled costs. More than one risk asset
requires an audited, complete correlation matrix; missing pairs are not assumed
uncorrelated. Gross exposure uses contract notional, not margin.

The unchanged `veritas_live.authorize_candidate()` still controls promotion,
probability, economics, risk, reconciliation, durable storage and arming. The
same LIVE caps also include modeled costs in the new authority's net stop-risk
check. The canonical entry cost multiple remains 1.1. The module does not
change strategy rules, PAPER permissions, promotion thresholds or LIVE caps.

## Existing research cannot currently be silently promoted

The inspected repository has useful research evidence, but no existing producer
of a complete current-version LIVE promotion record:

| Existing source | Why automatic LIVE conversion would lose evidence |
| --- | --- |
| `run_bootstrap_backtest()` / `knowledge_backtest_oos_stats` | Per-rule records are overwritten by `(rule_id, asset, horizon, sample)`. They do not bind exact deployment, complete current policy, source identity or model. The producer labels its assumed-cost/public-feed output research. |
| `v75_parameter_lab()` | Selects admission filters using historical IS/OOS, then checks a vault. Its result explicitly forbids automatic champion promotion. It is not a calibration, CI or data-parity certificate for the current model. |
| `calibration_quality()` | Aggregates episode outcomes across versions and derives a diagnostic probability from confidence. It explicitly reports research diagnostics. |
| `veritas_strategy_quality.py` and `veritas_launch_readiness.py` | Preserve useful exact-entry PAPER evidence. They do not prove independently held-out OOS, a sealed vault, or whole real-account unit NAV. |
| `docs/research/breakout_retest_manifest_20261007.json` | An isolated BTC/ETH Coinbase spot experiment, with no strategy promotion and no CNY broker/account validation. |
| `docs/audits/2026-10-07-system-evidence.json` | Explicitly records `out_of_sample_profit_proven=false`. |

Existing reports can be supplied to an independent reviewer as supporting
artifacts. Missing immutable provenance must be established from original
records, not stamped onto old aggregates using the current deployment. The
intake below supports an existing `validation_snapshots` row when a real
validation producer exports the exact audited schema. There is no required
unset callback and no unconditional `eligible=true` escape hatch.

## Concrete evidence intake and trust boundary

The internal API is:

```python
publish_audited_artifact(connect, document, artifact_bytes, review, now=None)
publish_validation_snapshot(connect, snapshot_id, artifact_bytes, review, now=None)
```

The second function reads exactly one existing `validation_snapshots.snapshot_id`
using the application's canonical search path (`veritas_v90` in the live app).
Its payload must contain
`live_authority_artifact` in the schema below. Ordinary research/paper JSON is
rejected with `LIVE_VALIDATION_ARTIFACT_EXPORT_REQUIRED`.

`artifact_bytes` maps each SHA-256 in the manifest to the actual nonempty bytes
reviewed. The total document, review and referenced blobs is bounded at 2 MiB.
Ingestion verifies those hashes and preserves both the original bytes and the
typed document. The loader checks them again. SHA-256 proves artifact identity;
the authorized independent reviewer remains responsible for experimental
independence, metric correctness and broker-ledger completeness. An arbitrary
HTTP client has no review/publish endpoint. Database-authorized operator access
is the trust boundary.

The review object requires nonempty `reviewer` and `audit_id`, a timezone-aware
`reviewed_at`, and `decision="ACCEPT_EVIDENCE"`. Review must follow observation
and cannot be future-dated beyond the same two-second clock allowance used by
admission. Publication is append-only within the API. Identical submissions are
idempotent. Newer adverse evidence takes precedence; the loader never searches
back for an older passing report. Different records with an identical latest
review timestamp are ambiguous and block. A newer reviewed document with
`revoked=true` revokes that binding. Direct database-admin tampering is outside
the API boundary; content/blob hashes detect inconsistent edits on load.

The lazy schema consists of these new, explicitly public tables:

| Table | Contents |
| --- | --- |
| `veritas_currency_live_evidence_v1` | Immutable document/review digest, kind, exact binding digest, review timestamp and original JSON |
| `veritas_currency_live_artifact_blobs_v1` | Content-addressed reviewed artifact bytes |
| `veritas_currency_live_account_observations_v1` | Actual account snapshots observed by this authority, observed time and snapshot digest |

The two evidence kinds share this envelope:

```json
{
  "schema": "VERITAS_AUDITED_LIVE_EVIDENCE_V1",
  "kind": "MODEL or ACCOUNT_HISTORY",
  "binding": {},
  "observed_at": "timezone-aware observation timestamp",
  "valid_until": "timezone-aware expiry timestamp",
  "revoked": false,
  "artifacts": {
    "domain": {"origin": "required origin below", "sha256": "64 lowercase hex characters"}
  },
  "payload": {}
}
```

### MODEL

Use `model_binding(approved_terms)` to obtain the exact binding. It includes
account, production environment, CNY UID, asset, horizon, direction, event
family, model version, signal source-identity hash, strategy epoch, deployment
SHA, entry-policy hash, LIVE-risk-policy hash and canonical policy version.
The signed native entry context must match its readable representation.

Required artifact origins are:

| Domain | Origin |
| --- | --- |
| `oos` | `INDEPENDENT_OOS` |
| `vault` | `SEALED_VAULT` |
| `high_cost` | `COST_STRESS` |
| `calibration` | `HELD_OUT_CALIBRATION` |
| `shadow` | `VERSIONED_SHADOW` |
| `ci` | `CODE_CI` |
| `data_parity` | `DATA_PARITY` |
| `correlations` when other futures are held | `VERSIONED_RETURN_CORRELATION` |

`payload.promotion` must contain exactly the fields of
`veritas_promotion.PromotionEvidence`, with native JSON numeric/boolean types.
Its model version must match the binding. Ingestion accepts a valid but failing
report; the existing promotion gate will continue to block its use.

`payload.calibration` contains `method="EMPIRICAL_HELD_OUT_COHORT"`,
`population_binding_sha256` equal to the envelope binding's SHA-256,
`n` equal to `promotion.calibration_n`, and integer `successes` between zero and
`n`. The authority computes `successes/n`. A confidence score, prior probability,
paper readiness label or unrelated asset's calibration is never substituted.
This initial predictor is the audited conditional cohort for that exact bound
model/source/horizon/direction/event family. Other calibration model formats
need an independently validated predictor implementation before being admitted.

Correlation evidence, when necessary, is in `payload.correlations`: native
integer `observations >= 2` and `matrix`, keyed by `CNYRUBF` and
`TBANK:<instrument UID>` for other futures. Every required pair must have a
finite numeric value within [-1,1]. Two supplied directions must agree. The
matrix inherits the exact model artifact's observation and expiry.

Model evidence can be valid for at most seven days from observation, and may
expire earlier. It must be reissued for a new exact deployment/policy binding.
Publishing a passing model report does not arm execution.

### ACCOUNT_HISTORY

The binding is exactly `{"account_id": "explicit broker account", "environment": "production"}`.
Required artifact origins are `nav=BROKER_UNIT_NAV_LEDGER` and
`operations=TBANK_BROKER_OPERATIONS`. Evidence expires within 30 seconds of its
observation and must match the actual account snapshot digest and RUB equity.

Required payload fields are:

| Field | Required meaning |
| --- | --- |
| `method` | `BROKER_CASHFLOW_ADJUSTED_UNIT_NAV` |
| `snapshot_sha256` | Digest of the exact normalized current broker snapshot recorded by the authority |
| `equity_rub` | Actual account total equity in that snapshot |
| `units_outstanding` | Positive units from the independently reconciled cash-flow-adjusted account ledger |
| `cashflows_reconciled` | Explicit boolean true, backed by the operations artifact |
| `cashflows_reconciled_through` | Coverage at least through observation; no unobserved future interval |
| `day_start_at`, `week_start_at` | Moscow midnight and Monday midnight respectively for the current account period |
| `day_start_unit_nav`, `week_start_unit_nav` | Positive independently established unit NAV at those boundaries |
| `risk_epoch_start_at`, `history_covered_from` | Persistent risk-history origin and complete coverage from at least that origin; origin must predate the current week |
| `high_water_unit_nav` | Positive historical high water over that covered risk history, at least as high as both period baselines |
| `kill_switch` | Explicit current boolean account kill state |

Current unit NAV is actual equity divided by reconciled units. Daily and weekly
returns are its ratios to the respective baselines minus one. Drawdown compares
it with the historical high water (or the new high if higher). A deposit cannot
be silently counted as trading profit: unit issuance and all external flows
must be independently reconciled first. Missing baselines are never zero.

There is currently no whole-account unit-NAV/cash-flow history producer in the
repository. The recorded actual snapshots are a concrete input to such a
reconciler, together with broker operation exports and independently verified
opening/period baselines. Until that history exists and is current, the
`LIVE_ACCOUNT_HISTORY_EVIDENCE_REQUIRED` or more specific coverage/staleness
blocker is real. A new first observation cannot manufacture a prior week or
erase historical drawdown. An audited historical import is the supported
bootstrap; independently accumulated verified history is the ongoing source.

## Operator ingestion example

The following is intended for an authorized operator in a controlled process
with an already configured database connection. It neither reads a broker token
nor communicates with the broker. The operator first prepares reviewed
`document.json`, `review.json` and a directory of exact blobs named by digest.
Use genuine reviewed documents; the test suite's synthetic fixtures are not
production evidence.

```python
import json
from pathlib import Path
from veritas_currency_live_admission import publish_audited_artifact

root = Path("/controlled/audited-evidence-bundle")
document = json.loads((root / "document.json").read_text())
review = json.loads((root / "review.json").read_text())
blobs = {item["sha256"]: (root / "blobs" / item["sha256"]).read_bytes()
         for item in document["artifacts"].values()}
# connect is the application's established PostgreSQL connection factory.
receipt = publish_audited_artifact(connect, document, blobs, review)
assert receipt["trade_permission"] is False
```

For a compatible existing validation snapshot, replace the final call with
`publish_validation_snapshot(connect, snapshot_id, blobs, review)`. The API
rechecks timestamp windows, metric types, independent domain manifests, exact
bindings and all bytes before inserting. No command here should be interpreted
as approval to enable, arm or submit a real order.

## Verification and protocol references

Run `python -m unittest -v test_veritas_currency_live_admission`. Tests use actual
LIVE economics/promotion/risk gates and a transactional SQLite adapter. They
cover absent and tampered evidence, old versions, adverse/ambiguous reviews,
paper-prior non-bypass, whole-equity sizing, ADD concentration, loss/drawdown/kill
limits, other-account exposures/orders, protective-stop quantities, complete
correlation evidence, I/O staleness and cached-status expiry. They prove boundary
behavior, not live profitability or a successful real broker fill.

Primary broker documentation inspected for the mapping:

- [Operations service messages: GetPortfolio, GetPositions, PortfolioPosition](https://developer.tbank.ru/invest/services/operations/methods)
- [Portfolio valuation behavior](https://developer.tbank.ru/invest/services/operations/head-operations)
- [Stop orders: quantity, MoneyValue prices, direction, type and status](https://developer.tbank.ru/invest/services/stop-orders/stoporders)

The account-period and unit-NAV conventions above are this authority's explicit
audited history contract. They are not an assertion that GetPortfolio itself
provides weekly return or historical high-water NAV.
