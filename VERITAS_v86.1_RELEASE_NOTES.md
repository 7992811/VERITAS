# VERITAS v86.1 — Team Learning + UI/Data Integrity Freeze

Status: **FROZEN**

This branch is an isolated snapshot of the work completed on 24 September 2026. It must not be used as the continuing development branch.

## Baseline separation

- Engine baseline before this block: `93e133b`
- Gateway baseline before this block: `e7d1c3f`
- Final engine component: `676434df85676836b7dd72e57ae4e50792deb321`
- Final gateway component: `b8a3068afbc3b65eed24f4b9cc961e65eacba97d`

## Included in v86.1

- Dedicated `TEAM_EXPERIENCE` learning layer.
- Team experience stored separately from books/research and `CLOSED_FINAL` statistical learning.
- Ten structured team-experience cards.
- Validation and application journals for team experience.
- Team rules cannot reverse signal direction or bypass hard safety/source/risk gates.
- Full v86 analysis fields propagated to the first page instead of being replaced by zero/None placeholders.
- Parallel asset-analysis loading and short-lived caches.
- Reduced dependence on legacy `veritas-intelligence-v1`.
- HTML and CLOSED_FINAL renderer fallback.
- Display-only USD/RUB backup context.
- Risk mandate aligned to gross leverage 2.0x and single-asset exposure 100%; RelativeValue remains stricter at 1.0x / 50%.

## Verified state at freeze

- 35/35 signal cells.
- 8/8 portfolios.
- 42 closed records; 41 eligible for learning and 1 recovered historical record.
- 0 pending finalizations.
- 10 team-experience cards active.
- Team experience matched the current directional rows and application events were persisted.
- Final gateway self-test: OK.
- Final page HTML: `APP_HTML_READY`.
- No `GATEWAY_500` after the final verified start.
- Standard portfolio mandates verified at `max_gross=2.0`, `asset_cap=1.0`.

## Freeze rule

Do not commit subsequent VERITAS development into this branch. Start the next iteration from a new development branch/version and keep this branch as the rollback/audit snapshot.
