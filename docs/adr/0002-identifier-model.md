# ADR 0002 — `dim_security` is the exchange listing; identifiers are SCD2 history

**Status:** accepted

**Context.** It's tempting to key securities on the ISIN, but ISINs are not permanent. When
Nestlé India split its shares 1:10 (ex-date 2024-01-05), the face value went from Rs 10 to Rs 1,
and NSE started printing a new ISIN (`INE239A01016` → `INE239A01024`) on the same ticker
`NESTLEIND`. Tickers change too (renames, mergers), and FIGI and LEI arrive from other sources
on their own schedules. Keying the dimension on any one of them would either split one economic
security into two rows or merge two into one.

**Decision.** `dim_security` has a surrogate key, and its natural key is the *listing*
`(exchange_id, symbol)`. Every external identifier lives in `map_security_identifier`
(`id_type`, `id_value`, `source`, `valid_from`, `valid_to`), a Kimball type-2 slowly changing
history. ISIN windows are derived from what the bhavcopy actually printed: the min/max trade date
of each (symbol, ISIN) pair, with `valid_to = NULL` for the current one. FIGI and composite FIGI
(OpenFIGI) and LEI (GLEIF) attach to the ISIN they were looked up with. Facts keep the
`isin_observed` on each price row, so lineage back to the source is never lost.

**Trade-off.** A point-in-time question ("which ISIN did this ticker have on 2024-01-03?") needs a
date-range join instead of a simple equality join. In return, identifier changes are modelled
explicitly instead of overwriting history, and the `identifier_conflict` rule can tell a
legitimate succession (non-overlapping windows) from a real conflict (two ISINs on the same
day). The listing key does assume that a ticker rename creates a new listing row. We have no
renames in the demo period; this is noted in known issues.
