# Offline order validation — 7 October 2026

## Scope

The `Offline order lifecycle` workflow exercises the existing generic coordinator
against an in-memory venue. It runs on pull requests and main pushes. Socket
connections are forbidden during scenario loading and execution. No account,
token, broker endpoint, deployment setting or production position is used or modified.

Eighteen tests cover acknowledgement; full and partial fills; weighted price;
cancellation of the unexecuted remainder; retry of a known order; recovery after
a lost acknowledgement when lookup finds the accepted order; recreation of the
coordinator; rejection; position mismatch; heartbeat loss; disabled authority;
invalid numeric data and tolerance; duplicate/missing instruments; invalid order
quantity; a reused client ID with different parameters; corrupted existing fills;
and explicit UNKNOWN state without resubmission.

The generic coordinator now rejects NaN/infinite/boolean quantities, ambiguous
net-position snapshots, non-positive order quantities, conflicting client IDs and
inconsistent existing fills. Authorization must be the boolean True, not a truthy
string. No additional trading permission is introduced.

## Integration and protection

The first candidate used d5393c783e141c01532b07f9628ec888409d8db3 (91.7.20).
Before publication it was integrated with current main
265a3c109dcb326351db9a8e9cdda2d0fae35aeb (91.7.21), preserving the separate private
Currency console and its own coordinator, exact Decimal accounting, guarded
submission, signed settlement and owner/session tests. The generic coordinator
checks here are additional coverage, not a replacement for those 440 broker tests.
The combined exact head must pass all existing Safety CI and Currency recovery
checks as well as the new offline suite before merge.

Canonical protective I/O already projects current fields, batches at most
16 position deltas and isolates optional failures with savepoints. The earlier
experimental PathBuffer and guard rewrite are deliberately not included here:
there must be one protective writer, not competing implementations.
All STOP/TP/funding, source, clock, rollback and required execution-audit checks
remain intact.

## What passing these tests does not establish

This is not a TBank sandbox session and does not certify any real-money adapter.
The recreated-coordinator test retains the in-memory venue journal; it is not a
proof of durable recovery across a broker outage or process crash. In particular,
a lost acknowledgement followed by an unavailable/empty broker lookup still needs
provider-specific reconciliation and durable unknown-order handling before live
use. Cross-process submission races, broker-side protection and actual lot/currency
conversion also require separate integration evidence in the concrete adapter.

No orders-enabled/armed settings are changed. Runtime latency must be measured on
the deployed service under full load. A successful offline test cannot close the
production latency issue or authorize a live order.
