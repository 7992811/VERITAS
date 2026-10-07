# Ordinary book handoff after repeated structural retries

## Confirmed production defect

On 2026-10-07, instance `srv-daoavtuk1f9s73bbq6l0-m5vm2`, release
`a91234326e9c55687a82ad94174dc2f2267afa5c`, recorded an Impulse book start at
21:47:53.679 UTC and a commit at 21:48:30.483 UTC. The transaction measured
`python_lock_wait_seconds=324.107`, `lock_hold_seconds=35.990`,
`accounting_seconds=32.407`, and `db_lock_wait_seconds=0.002`.

This was not one uninterrupted five-minute book owner. During the wait,
17 recorded protective passes held the book for 5.509–13.105 seconds each.
Structural passes also repeatedly acquired and released it: the successful
passes logged at 21:42:45.189, 21:43:56.276 and 21:44:58.986 took 8.804,
10.520 and 9.705 seconds respectively. Ordinary waiters remained visible.
Later BUSY passes reported one structural entry reservation and one ordinary
waiter. Structural retry passes rose from 116 to 311 over 209 total passes.

The only production `reserve_entry_turn()` caller is structural runtime.
The previous lock could repeatedly renew a reservation, or grant a new one
after a successful structural pass, without considering the age of an
already-blocked ordinary book. A bounded individual lease therefore did not
bound how often an ordinary book could lose the next nonprotective turn.

## Change

The local reentrant book lock records blocking ordinary waiters in arrival
order using the monotonic clock. Once the oldest waiter has waited 20 seconds,
it precedes structural reservations and newly arriving ordinary/nonblocking
callers for the next nonprotective turn. The 20-second window matches the
existing default structural handoff lease; it changes scheduling fairness,
not a trading parameter or an execution quote's lifetime.

Waiting protection continues to take precedence over every ordinary waiter.
The current owner completes its transaction, and its reentrant acquisitions
remain immediate. Before the aging boundary, existing structural handoff
behavior remains available. Reservation and pending-entry APIs are unchanged;
a structural entry that does not obtain the lock remains subject to the same
event, source and fresh-quote validation on retry.

The condition wait wakes at the aging boundary even if the reservation lasts
longer. After that boundary, an occupied book or waiting protection causes a
normal condition wait, not a zero-time busy loop. Queue entries are removed
on acquisition, timeout and exception. Nonblocking polling attempts are not
retained as blocking waiters.

Bounded diagnostics expose the oldest blocking ordinary wait and the last
handoff reason. They retain no frames, account values, signals or payloads.

## What the bound means

Twenty seconds is an aging threshold for selection of the next nonprotective
owner. It is not a maximum transaction wait or a protection latency guarantee.
An already-held transaction and queued protective passes still run first;
PostgreSQL's existing cross-process advisory lock also remains authoritative.
This repair eliminates repeated structural bypass of an aged ordinary waiter.
It does not preempt a transaction or promise progress during uninterrupted
protective demand.

## Validation and release

Focused tests exercise repeated reservation renewals, repeated successful
structural acquisitions, protection precedence, nonblocking barge prevention,
ordered aged waiters, reentry, timeout/exception cleanup and the boundary wake.
Existing Currency reservation/freshness and structural retry tests stay in
place. The focused suite is explicitly included in both Safety and Structural
Latency workflows. Existing architecture ceilings remain unchanged.

All four existing release gates are required for the final integrated head:
Safety (including native PostgreSQL financial state and rollback checks),
Structural Latency, Currency Recovery and Offline Order Lifecycle. Exact run
identities and inspected results are recorded in the pull request. No broker
action, history rewrite, schema migration, resource upgrade or source-policy
relaxation is part of this change.

Production acceptance must use the deployed commit identity and separately
observe local wait, accounting work and book hold. The earlier 91.8.1 storage
repair and the parallel Moscow daytime learning schedule both change workload
and duration, so an overall before/after duration is not a controlled estimate
of this fairness change alone. The operational check is that an aged ordinary
waiter receives the next available nonprotective turn while protection retains
priority.

The final integration also preserves main `107564e7447d9219a1bf14c62624f38df9c3c195`: Currency reader validation, observation of every queued barrier before reserving, learning exclusions, and projected NAV/mark reads. That parallel release used 91.8.2; the queue fairness release is therefore 91.8.3.
