# Lineage

How each warehouse table is derived, from public source to `dq_*` tables. Every arrow is code in
`src/findata`: `ingest/` (source → raw), `validate/` (raw → staging + anomalies),
`model/loaders.py` (staging → warehouse) and `quality/` (warehouse → dq tables).

```mermaid
flowchart LR
    subgraph Sources
        NSEB[NSE bhavcopy<br/>UDiFF CSV]
        NSECA[NSE corporate<br/>actions API]
        NSEM[NSE EQUITY_L<br/>master]
        YF[Yahoo Finance<br/>yfinance]
        FBIL[FBIL reference<br/>rates]
        FRED[FRED<br/>fredgraph CSV]
        WB[World Bank<br/>WDI API]
        FIGI[OpenFIGI<br/>mapping API]
        GLEIF[GLEIF<br/>LEI API]
        UNI[config/universe.yaml]
        CAL[config/calendars/nse.yaml]
    end

    subgraph Raw["data/raw (parquet, dt= partitions)"]
        r_px[nse_prices]
        r_ca[nse_corporate_actions]
        r_sec[nse_securities]
        r_yf[yahoo_prices]
        r_fx[fx_rates]
        r_fred[fred_macro]
        r_wb[worldbank_macro]
        r_figi[openfigi_mapping]
        r_lei[gleif_lei]
    end

    subgraph Staging["data/staging (validated)"]
        s_all[staged datasets]
        s_anom[anomalies.parquet]
    end

    subgraph Warehouse
        dim_security
        dim_issuer
        dim_exchange
        dim_currency
        dim_calendar
        dim_macro_series
        map_security_identifier
        fact_price_daily
        fact_corporate_action
        fact_adjustment_factor
        adj_factor_segment
        v_price_adjusted[[v_price_adjusted]]
        fact_macro_series
        fact_fx_rate
        dq_anomaly
        dq_metric
    end

    NSEB --> r_px
    NSECA --> r_ca
    NSEM --> r_sec
    YF --> r_yf
    FBIL --> r_fx
    FRED --> r_fred
    WB --> r_wb
    FIGI --> r_figi
    GLEIF --> r_lei

    r_px & r_ca & r_sec & r_yf & r_fx & r_fred & r_wb & r_figi & r_lei --> s_all
    r_px & r_ca & r_yf & r_fx & r_fred & r_wb --> s_anom
    CAL --> s_anom

    UNI --> dim_security
    s_all -->|nse_securities, openfigi| dim_security
    s_all -->|gleif| dim_issuer
    dim_issuer --> dim_security
    s_all -->|nse_prices ISIN windows, openfigi, gleif| map_security_identifier
    CAL --> dim_calendar
    s_all -->|nse_prices, yahoo_prices| fact_price_daily
    s_all -->|nse_corporate_actions| fact_corporate_action
    fact_corporate_action --> fact_adjustment_factor
    fact_price_daily -->|close before ex-date| fact_adjustment_factor
    fact_adjustment_factor --> adj_factor_segment
    fact_price_daily & adj_factor_segment & dim_security --> v_price_adjusted
    s_all -->|fred, worldbank: all vintages| fact_macro_series
    dim_macro_series --> fact_macro_series
    s_all -->|fx_rates| fact_fx_rate
    dim_currency --> fact_fx_rate
    s_anom --> dq_anomaly
    fact_price_daily & fact_fx_rate --> dq_metric
```

## Notes

- **Raw is append-by-partition.** Re-ingesting a date replaces its `dt=` partition, so raw is
  idempotent. Macro datasets are partitioned by *vintage* date, which keeps every revision.
- **Warehouse scope = universe.** `dim_security` and the price/action facts hold the 20 symbols
  in `config/universe.yaml`. Raw and staging keep the full NSE file.
- **Adjusted prices are derived, never stored as the only copy.** Raw closes are kept in
  `fact_price_daily`, and `v_price_adjusted` multiplies them by the factor segment (ADR 0003).
- **Analytics** (`src/findata/analytics/queries.py`) read `v_price_adjusted`,
  `fact_corporate_action`, `fact_fx_rate` and `fact_macro_series` (latest vintage).
