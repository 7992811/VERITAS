# Owner-directed Currency trials

Version: `OWNER_MANUAL_TRIAL_V1`, broker terms `currency-owner-manual-v1`.

The private owner may request a one-contract CNYRUBf trial without a model
signal. This is an explicit owner decision, not model promotion or a synthetic
canonical breakout. No model probability, native candles or event proof is
invented. Automatic model proposals retain their existing admission path.

## Telegram

Send `/currency_manual` to the configured private bot for the parameter guide.

```
/currency_manual SELL 1 LIMIT STOP TARGET MINUTES
/currency_manual BUY 1 LIMIT STOP TARGET MINUTES
/currency_manual_close LIMIT
```

Replace every placeholder with the owner's explicit values. Prices must be
positive exact decimals on the current contract tick (decimal comma is accepted
by the Telegram parser). MINUTES is the expected holding period used to model
costs, from 1 to 60; it is not an automatic time exit. SELL requires target below
limit below stop; BUY requires stop below limit below target. The limit must
cover the current executable bid/ask for the existing FAK order policy.

The opening trial is exactly one contract from a flat Currency position and a
flat whole broker account. Other account holdings or working orders block this
initial scope; they are not dropped from the account view. Adds, reversals,
market orders and arbitrary sizing are unsupported. The close command reduces
only the recorded manual one-contract position and requires its own approval.

Preparation creates no broker order. The existing private delivery worker sends
the immutable terms and signed owner buttons. The owner must approve that
specific message before the normal execution worker can claim and submit it.
The bot/chat/owner, environment, account and exact UID remain bound. Existing
pause, enabled/armed switches, submission journal, UNKNOWN reconciliation and
once-only claim semantics apply. No production trade is part of development or
offline validation of this feature.

Telegram message identity deterministically identifies the manual request.
Retrying the same request returns the original signed proposal without changing
its prices or expiry; changing parameters under that ID is rejected. A new
command is a new intent and needs a new confirmation. A lost response must be
resolved by the original proposal/status, not by blindly sending a new command.

## Risk authority and current production prerequisite

Manual entries use `ManualAccountAdmission`, separate from model admission.
It rereads the whole account through the existing exact-contract broker reader,
reconciles positions/orders/stops, saves a durable real observation and requires
independently signed `ACCOUNT_CONTROLS` evidence for these exact terms. Its
cashflow-adjusted daily, weekly and high-water unit NAV and kill-switch values
are verified by the existing account-history verifier. Unknown history is a
blocker, not a zero loss or a new baseline.

The same live account risk helper supplies stop-risk (including costs), total
and correlated stop-risk, single-asset, gross exposure, drawdown, daily and
weekly loss limits for both model and manual authorities. Currency's separate
margin budget, broker lot allowance, whole-contract exposure and economics
checks run at preparation and after approval. Model promotion, forecast
calibration and native breakout admission are not asserted for owner decisions.

The existing signed evidence envelope retains legacy `model_version` and
`model_binding` field names. For manual terms they explicitly contain
`OWNER_MANUAL_TRIAL_V1`, `decision_authority=OWNER_MANUAL_TRIAL_V1` and
`event_family=OWNER_SPECIFIED_LIMIT_ORDER`. Only ACCOUNT_CONTROLS is requested
and read; this is not a MODEL_ADMISSION document. The repository's legacy
`canonical_event_id` column holds `MANUAL-<request UUID>`.

This feature does not supply a new independent issuer or reconstruct missing
account NAV history. The production repository previously had no automatic
producer of complete independently attested account history for new exact order
terms. An absent, expired or mismatched attestation blocks real preparation with
`LIVE_ACCOUNT_HISTORY_EVIDENCE_REQUIRED` (or the corresponding precise refusal).
Offline success is not evidence that a particular real account is ready.

## Protected API and reviewed retry

`POST /internal/currency-trading/prepare-manual` requires the existing service
authentication plus exact private owner fields, selected account/environment
scope and a `request` object. For OPEN its exact keys are `request_id` (canonical
UUID), `action`, `side`, integer `lots=1`, string `limit_price`, `stop_price`,
`target_price` and integer `hold_minutes`. CLOSE accepts only `request_id`,
`action=CLOSE` and `limit_price`. Unbound, paused or unsettled scopes refuse.

The private cached `status.manual_account_admission.evidence_request` contains
the concrete failed-preflight terms/scope and observed account digest with
`trade_permission=false`. An independent issuer can use the existing signed
artifact intake to attest ACCOUNT_CONTROLS for that exact scope.

`POST /internal/currency-trading/prepare-manual-reviewed` takes the original
`request` and unchanged `terms` after import. It requires the same authenticated
private owner and reruns current facts, exact request/owner/environment binding,
account risk, price, margin and expiry. The intent's original deadline is never
renewed. Successful preparation still requires Telegram approval; the intake
and reviewed endpoint never submit an order.

Stop and target are owner-specified levels for risk and exit proposals. They do
not install exchange stop orders. Stop/target exits retain separate approval.
The original manual decision authority survives fills and ledger reconstruction,
so the manual close command cannot close an unrelated model position.
