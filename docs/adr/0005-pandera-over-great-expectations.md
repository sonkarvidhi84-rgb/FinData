# ADR 0005: pandera for schema validation (rather than Great Expectations or soda-core)

**Status:** accepted

**Context.** Every dataset needs schema checks (types, nullability, ranges, ISIN format,
allowed values) generated from its YAML contract. The checks have to run inside the Python
pipeline and in unit tests, offline.

**Options considered.**
- **Great Expectations (GX).** A rich expectation library with data docs and checkpoints. It
  brings its own project and config store (a "data context"), a large dependency tree and a
  run model built around batches and data sources. That is valuable for a team with many
  pipelines, but for one in-process pandas pipeline it is mostly ceremony, and its API has
  changed substantially between major versions.
- **soda-core.** Checks are written in SodaCL YAML and executed as SQL against a warehouse.
  That is a good fit for *post-load* monitoring of tables, but it is weaker for validating
  DataFrames before they reach the database.
- **pandera.** A schema is a Python object that validates a DataFrame in-process, returns a
  structured failure table (`lazy=True`) and is easy to build programmatically from our YAML.
  It has a small dependency footprint and type annotations that work with mypy.

**Decision.** We use pandera for structural validity, generated from the YAML contract.
Financial logic (jumps, splits, cross-source differences) lives in our own pure-function rule
engine, because those checks need context such as corporate actions and a second source,
which a column-level schema library does not model well. pandera failures become `Anomaly`
records like every other check, so validity is measured, not just enforced.

**Trade-off.** We give up GX's generated data docs and checkpoint history. The project replaces
them with `dq_metric` and `dq_anomaly` tables, the markdown run reports and the dashboard. If
the warehouse grew to many teams and tables, soda-core checks on the warehouse would be the
natural addition alongside pandera, not a replacement for it.
