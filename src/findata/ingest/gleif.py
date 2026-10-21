"""GLEIF: Legal Entity Identifiers (ISO 17442) for the issuers in the universe.

GLEIF's ISIN-to-LEI relationship comes from ANNA. It covers few Indian ISINs: in Oct 2026 the
`filter[isin]` lookup came back empty for every name we tried. So we try the ISIN first and
then fall back to *entity resolution* by legal name: query GLEIF for Indian entities matching
the NSE company name, and accept a candidate only if its normalised legal name is identical.
That conservative match prefers a missing LEI to a wrong one. The resolution method is recorded
in `docs/data-sources.md`.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

import pandas as pd

from findata.ingest.base import PoliteSession, utcnow
from findata.settings import Settings, get_settings

GLEIF_URL = "https://api.gleif.org/api/v1/lei-records"
PARENT_URL = "https://api.gleif.org/api/v1/lei-records/{lei}/direct-parent"

LEI_COLUMNS = [
    "isin",
    "lei",
    "legal_name",
    "country",
    "entity_status",
    "registration_status",
    "parent_lei",
    "ingest_ts",
]


def normalise_name(name: str) -> str:
    """Canonical form for exact legal-name matching: upper-case, LTD to LIMITED, & to AND."""
    s = name.upper().replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = re.sub(r"\bLTD\b", "LIMITED", s)
    s = re.sub(r"\bCO\b", "COMPANY", s)
    return " ".join(s.split())


def parse_record(rec: dict[str, Any]) -> dict[str, Any]:
    """Flatten one GLEIF `lei-records` JSON:API record."""
    attrs = rec.get("attributes", {})
    entity = attrs.get("entity", {})
    return {
        "lei": attrs.get("lei") or rec.get("id"),
        "legal_name": (entity.get("legalName") or {}).get("name"),
        "country": (entity.get("legalAddress") or {}).get("country"),
        "entity_status": entity.get("status"),
        "registration_status": (attrs.get("registration") or {}).get("status"),
    }


def best_name_match(records: list[dict[str, Any]], company: str) -> dict[str, Any] | None:
    """The record whose normalised legal name equals the normalised company name, if any."""
    target = normalise_name(company)
    for rec in records:
        flat = parse_record(rec)
        if flat["legal_name"] and normalise_name(flat["legal_name"]) == target:
            return flat
    return None


class GleifClient:
    """Look up LEIs for (isin, company name) pairs, plus each entity's direct parent."""

    source = "gleif"
    dataset = "gleif_lei"
    partition_col = "snapshot"

    def __init__(self, session: PoliteSession | None = None, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.session = session or PoliteSession(
            min_interval_s=max(self.settings.http_min_interval_s, 1.0),
            user_agent=self.settings.user_agent,
            headers={"Accept": "application/vnd.api+json"},
        )

    def _query(self, params: dict[str, str]) -> list[dict[str, Any]]:
        resp = self.session.get(GLEIF_URL, params=params)
        if resp.status_code != 200:
            return []
        data = resp.json().get("data", [])
        return list(data) if isinstance(data, list) else []

    def lookup(self, isin: str, company: str | None) -> dict[str, Any] | None:
        """ISIN lookup first; then exact normalised legal-name match among Indian entities."""
        recs = self._query({"filter[isin]": isin})
        if recs:
            return parse_record(recs[0])
        if not company:
            return None
        recs = self._query(
            {
                "filter[entity.legalName]": company,
                "filter[entity.legalAddress.country]": "IN",
                "page[size]": "10",
            }
        )
        return best_name_match(recs, company)

    def parent_lei(self, lei: str) -> str | None:
        """Direct accounting-consolidation parent; GLEIF answers 404 when none is reported."""
        resp = self.session.get(PARENT_URL.format(lei=lei))
        if resp.status_code != 200:
            return None
        try:
            data = resp.json().get("data") or {}
        except ValueError:
            return None
        parent = data.get("attributes", {}).get("lei") or data.get("id")
        return str(parent) if parent else None

    def fetch(
        self,
        start: date | datetime | pd.Timestamp | None = None,
        end: date | datetime | pd.Timestamp | None = None,
        symbols: list[str] | None = None,
        names: dict[str, str] | None = None,
    ) -> pd.DataFrame:
        """`symbols` are ISINs; `names` maps ISIN -> company name for the fallback lookup."""
        names = names or {}
        rows: list[dict[str, Any]] = []
        cache: dict[str, dict[str, Any] | None] = {}
        for isin in sorted(set(symbols or [])):
            company = names.get(isin)
            key = normalise_name(company) if company else isin
            if key not in cache:
                hit = self.lookup(isin, company)
                if hit is not None:
                    hit["parent_lei"] = self.parent_lei(str(hit["lei"]))
                cache[key] = hit
            found = cache[key]
            if found is not None:
                rows.append({"isin": isin, **found, "ingest_ts": utcnow()})
        return pd.DataFrame(rows, columns=LEI_COLUMNS)
