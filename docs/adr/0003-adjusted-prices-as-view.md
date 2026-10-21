# ADR 0003 — Store unadjusted prices; compute adjusted prices through a factor-segment view

**Status:** accepted

**Context.** Return calculations need prices adjusted for splits, bonuses and dividends.
Otherwise Nestlé's 1:10 split looks like a 90% crash. But adjusted prices are a *derived* series:
every new corporate action changes all the adjusted history before it. If only adjusted prices
are stored, a late or wrong corporate action silently corrupts history and can't be audited,
and the official exchange prints that reconciliation needs are gone.

**Decision.** `fact_price_daily` stores the prices exactly as the exchange published them.
Corporate actions are turned into per-event factors in `fact_adjustment_factor`, using backward,
CRSP-style adjustment: split/bonus factor = post/pre price ratio; cash-dividend factor =
1 − dividend / close before the ex-date. The cumulative factor for a date is the product of the
factors of all later events, which is piecewise constant between ex-dates. We therefore store
one row per interval in `adj_factor_segment` (`valid_from`, `valid_to`, `cumulative_factor`),
and the view `v_price_adjusted` does a range join:
`adjusted_close = close × cumulative_factor`. Segments are rebuilt from all events on every
load.

**Trade-off.** We chose segments over computing `EXP(SUM(LN(factor)))` inside the view. Log and
exp functions aren't guaranteed in SQLite, and segments keep the view a plain portable join that
runs the same on SQLite and Postgres. The cost is one derived table that must be rebuilt when
actions change, which the loader does in the same transaction. A materialised adjusted series
would make reads faster, but at this data size that isn't needed.
