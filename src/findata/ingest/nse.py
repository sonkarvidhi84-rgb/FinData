"""NSE India clients: bhavcopy (daily prices), corporate actions and the equity master.

NSE publishes official end-of-day files on `nsearchives.nseindia.com`. We prefer these bulk
files over the JSON API (CLAUDE.md rule 7), and cache every downloaded zip under
`data/raw/nse/_cache/`, so a re-run never downloads the same file twice. The corporate-actions
data is only available through the website's JSON API, which needs a cookie set by first
visiting the HTML page.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import pandas as pd

from findata.ingest.base import BROWSER_UA, PoliteSession, utcnow
from findata.settings import Settings, get_settings

BHAVCOPY_URL = (
    "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip"
)
EQUITY_MASTER_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
CA_PAGE_URL = "https://www.nseindia.com/companies-listing/corporate-filings-actions"
CA_API_URL = "https://www.nseindia.com/api/corporates-corporateActions"
HOLIDAY_API_URL = "https://www.nseindia.com/api/holiday-master?type=trading"

NSE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

BHAV_COLUMNS = {
    "TradDt": "trade_date",
    "TckrSymb": "symbol",
    "SctySrs": "series",
    "ISIN": "isin",
    "FinInstrmNm": "name",
    "OpnPric": "open",
    "HghPric": "high",
    "LwPric": "low",
    "ClsPric": "close",
    "PrvsClsgPric": "prev_close",
    "TtlTradgVol": "volume",
    "TtlTrfVal": "turnover",
    "TtlNbOfTxsExctd": "trades",
}


def nse_session(min_interval_s: float = 1.0) -> PoliteSession:
    """Rate-limited session with browser-like headers (NSE blocks unknown agents)."""
    return PoliteSession(min_interval_s=min_interval_s, user_agent=BROWSER_UA, headers=NSE_HEADERS)


def _to_date(d: date | datetime | pd.Timestamp) -> date:
    return pd.Timestamp(d).date()


def daterange(start: date, end: date) -> Iterable[date]:
    """Every calendar day in [start, end], weekends included (NSE holds Saturday sessions)."""
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


# --------------------------------------------------------------------------- bhavcopy


def parse_bhavcopy(
    csv_bytes: bytes, source_ts: pd.Timestamp | None, ingest_ts: pd.Timestamp
) -> pd.DataFrame:
    """Parse one UDiFF bhavcopy CSV into the `nse_prices` shape (series EQ only).

    Args:
        csv_bytes: CSV content (already unzipped).
        source_ts: Publication time of the file (HTTP Last-Modified), UTC.
        ingest_ts: Download time, UTC.
    """
    raw = pd.read_csv(io.BytesIO(csv_bytes), dtype=str)
    raw.columns = [c.strip() for c in raw.columns]
    raw = raw[(raw["SctySrs"].str.strip() == "EQ") & (raw["FinInstrmTp"].str.strip() == "STK")]
    df = raw[list(BHAV_COLUMNS)].rename(columns=BHAV_COLUMNS)
    for col in ("symbol", "series", "isin", "name"):
        df[col] = df[col].str.strip()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    for col in ("open", "high", "low", "close", "prev_close", "volume", "turnover", "trades"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["source_ts"] = source_ts
    df["ingest_ts"] = ingest_ts
    return df.reset_index(drop=True)


def unzip_single_csv(zip_bytes: bytes) -> bytes:
    """Return the first CSV member of a zip archive."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
        return zf.read(name)


def parse_last_modified(value: str | None) -> pd.Timestamp | None:
    """Parse an HTTP `Last-Modified` header into a UTC timestamp."""
    if not value:
        return None
    try:
        return pd.Timestamp(parsedate_to_datetime(value)).tz_convert("UTC")
    except (TypeError, ValueError):
        return None


class NseBhavcopyClient:
    """Daily equity OHLCV from the NSE UDiFF bhavcopy archive.

    A 404 for a date means no session was held, and we record that in `missing_days`. That
    availability is how we derive the trading calendar: NSE publishes no bhavcopy on a holiday,
    but it does publish one for a special Saturday session.
    """

    source = "nse"
    dataset = "nse_prices"
    partition_col = "trade_date"

    def __init__(self, session: PoliteSession | None = None, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.session = session or nse_session(self.settings.http_min_interval_s)
        self.available_days: list[date] = []
        self.missing_days: list[date] = []

    @property
    def cache_dir(self) -> Path:
        return self.settings.raw_dir / "nse" / "_cache" / "bhavcopy"

    def _download(self, d: date) -> tuple[bytes, pd.Timestamp | None] | None:
        """Fetch the zip for day `d` (from cache if present); None if NSE has no file."""
        ymd = d.strftime("%Y%m%d")
        zpath = self.cache_dir / f"{ymd}.zip"
        meta = self.cache_dir / f"{ymd}.lastmod"
        miss = self.cache_dir / f"{ymd}.404"
        if zpath.exists():
            lm = meta.read_text(encoding="utf-8") if meta.exists() else None
            return zpath.read_bytes(), parse_last_modified(lm)
        if miss.exists() and d < date.today() - timedelta(days=7):
            return None
        resp = self.session.get(BHAVCOPY_URL.format(ymd=ymd))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if resp.status_code == 404:
            miss.write_text("404", encoding="utf-8")
            return None
        resp.raise_for_status()
        zpath.write_bytes(resp.content)
        lm = resp.headers.get("Last-Modified")
        if lm:
            meta.write_text(lm, encoding="utf-8")
        return resp.content, parse_last_modified(lm)

    def fetch(
        self,
        start: date | datetime | pd.Timestamp,
        end: date | datetime | pd.Timestamp,
        symbols: list[str] | None = None,
    ) -> pd.DataFrame:
        """All EQ rows for every session in [start, end] (optionally filtered to `symbols`)."""
        frames: list[pd.DataFrame] = []
        self.available_days, self.missing_days = [], []
        for d in daterange(_to_date(start), _to_date(end)):
            got = self._download(d)
            if got is None:
                self.missing_days.append(d)
                continue
            self.available_days.append(d)
            frames.append(parse_bhavcopy(unzip_single_csv(got[0]), got[1], utcnow()))
        if not frames:
            return pd.DataFrame(columns=[*BHAV_COLUMNS.values(), "source_ts", "ingest_ts"])
        df = pd.concat(frames, ignore_index=True)
        if symbols:
            df = df[df["symbol"].isin(symbols)].reset_index(drop=True)
        return df


# --------------------------------------------------------------------------- corporate actions

_AMOUNT = r"R[se]\.?\s*([\d]+(?:\.\d+)?)"
_DIVIDEND_RE = re.compile(r"Dividend\s*-?\s*" + _AMOUNT, re.IGNORECASE)
_DISTRIBUTION_RE = re.compile(r"^Distribution\s*-?\s*" + _AMOUNT, re.IGNORECASE)
_SPLIT_RE = re.compile(
    r"Split.*?From\s*R[se]\.?\s*([\d.]+)\s*/?-?.*?To\s*R[se]\.?\s*([\d.]+)", re.IGNORECASE
)
_BONUS_RE = re.compile(r"Bonus\s*(\d+)\s*:\s*(\d+)", re.IGNORECASE)


def parse_subject(subject: str) -> dict[str, Any]:
    """Classify an NSE corporate-action subject and extract its economic terms.

    The financial meaning of each result:

    * split: "From Rs 10 To Re 1" means each share becomes 10 shares, so the fair post-split price
      is new_fv / old_fv (= 0.1) times the pre-split price. That is `ratio`.
    * bonus "a:b": `a` new shares for every `b` held, so the price scales by b / (a + b).
    * dividend: all cash components (interim + special, ...) are summed into `amount`.
    * rights / buyback / other: no mechanical price ratio is derived.

    Returns:
        dict with keys action_type, ratio, amount.
    """
    s = " ".join(subject.split())
    low = s.lower()
    if "split" in low:
        m = _SPLIT_RE.search(s)
        if m:
            old, new = float(m.group(1)), float(m.group(2))
            if old > 0 and new > 0:
                return {"action_type": "split", "ratio": new / old, "amount": None}
        return {"action_type": "split", "ratio": None, "amount": None}
    m = _BONUS_RE.search(s)
    if m and "bonus" in low:
        a, b = int(m.group(1)), int(m.group(2))
        if a + b > 0 and b > 0:
            return {"action_type": "bonus", "ratio": b / (a + b), "amount": None}
    if low.startswith("rights"):
        return {"action_type": "rights", "ratio": None, "amount": None}
    if "buy back" in low or "buyback" in low:
        return {"action_type": "buyback", "ratio": None, "amount": None}
    dist = _DISTRIBUTION_RE.search(s)
    if dist:
        return {"action_type": "dividend", "ratio": None, "amount": float(dist.group(1))}
    divs = _DIVIDEND_RE.findall(s)
    if divs:
        return {"action_type": "dividend", "ratio": None, "amount": sum(float(x) for x in divs)}
    return {"action_type": "other", "ratio": None, "amount": None}


def _nse_date(value: Any) -> pd.Timestamp | None:
    if value is None or str(value).strip() in ("", "-"):
        return None
    try:
        return pd.Timestamp(datetime.strptime(str(value).strip(), "%d-%b-%Y"))
    except ValueError:
        return None


def parse_corporate_actions(records: list[dict[str, Any]], ingest_ts: pd.Timestamp) -> pd.DataFrame:
    """Turn the NSE corporate-actions JSON into the `nse_corporate_actions` shape."""
    rows: list[dict[str, Any]] = []
    for r in records:
        ex = _nse_date(r.get("exDate"))
        subject = str(r.get("subject") or "").strip()
        if ex is None or not subject:
            continue
        fv = pd.to_numeric(str(r.get("faceVal") or ""), errors="coerce")
        rows.append(
            {
                "symbol": str(r.get("symbol") or "").strip(),
                "series": r.get("series"),
                "isin": r.get("isin"),
                "company": r.get("comp"),
                "subject": subject,
                "ex_date": ex,
                "record_date": _nse_date(r.get("recDate")),
                "face_value": None if pd.isna(fv) else float(fv),
                **parse_subject(subject),
                "ingest_ts": ingest_ts,
            }
        )
    cols = [
        "symbol",
        "series",
        "isin",
        "company",
        "subject",
        "ex_date",
        "record_date",
        "face_value",
        "action_type",
        "ratio",
        "amount",
        "ingest_ts",
    ]
    return pd.DataFrame(rows, columns=cols)


class NseCorporateActionsClient:
    """Corporate actions (dividends, splits, bonuses, ...) from the NSE website API."""

    source = "nse"
    dataset = "nse_corporate_actions"
    partition_col = "ex_date"

    def __init__(self, session: PoliteSession | None = None, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.session = session or nse_session(max(self.settings.http_min_interval_s, 2.0))
        self._warm = False

    def _warm_up(self) -> None:
        if not self._warm:
            self.session.get(CA_PAGE_URL)
            self._warm = True

    def fetch(
        self,
        start: date | datetime | pd.Timestamp,
        end: date | datetime | pd.Timestamp,
        symbols: list[str] | None = None,
    ) -> pd.DataFrame:
        """Actions with ex-date in [start, end], filtered to series EQ and `symbols`."""
        self._warm_up()
        params = {
            "index": "equities",
            "from_date": _to_date(start).strftime("%d-%m-%Y"),
            "to_date": _to_date(end).strftime("%d-%m-%Y"),
        }
        resp = self.session.get(CA_API_URL, params=params, headers={"Referer": CA_PAGE_URL})
        resp.raise_for_status()
        payload = resp.json()
        records = payload if isinstance(payload, list) else payload.get("data", [])
        df = parse_corporate_actions(records, utcnow())
        if symbols:
            df = df[df["symbol"].isin(symbols)]
        return df.reset_index(drop=True)


def fetch_holiday_names(session: PoliteSession | None = None) -> dict[date, str]:
    """Holiday names from NSE's holiday-master API (it only covers the current year)."""
    sess = session or nse_session()
    sess.get("https://www.nseindia.com/")
    resp = sess.get(HOLIDAY_API_URL)
    if resp.status_code != 200:
        return {}
    out: dict[date, str] = {}
    for item in resp.json().get("CM", []):
        d = _nse_date(item.get("tradingDate"))
        if d is not None:
            out[d.date()] = str(item.get("description", "")).strip()
    return out


# --------------------------------------------------------------------------- equity master


def parse_equity_master(csv_bytes: bytes, snapshot: date) -> pd.DataFrame:
    """Parse EQUITY_L.csv into the `nse_securities` shape."""
    raw = pd.read_csv(io.BytesIO(csv_bytes), dtype=str)
    raw.columns = [c.strip() for c in raw.columns]
    df = pd.DataFrame(
        {
            "symbol": raw["SYMBOL"].str.strip(),
            "company": raw["NAME OF COMPANY"].str.strip(),
            "series": raw["SERIES"].str.strip(),
            "listing_date": pd.to_datetime(
                raw["DATE OF LISTING"].str.strip(), format="%d-%b-%Y", errors="coerce"
            ),
            "isin": raw["ISIN NUMBER"].str.strip(),
            "face_value": pd.to_numeric(raw["FACE VALUE"], errors="coerce"),
        }
    )
    df["snapshot_date"] = pd.Timestamp(snapshot)
    return df


class NseSecuritiesClient:
    """Current NSE equity master: symbol, ISIN, listing date, face value."""

    source = "nse"
    dataset = "nse_securities"
    partition_col = "snapshot_date"

    def __init__(self, session: PoliteSession | None = None, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.session = session or nse_session(self.settings.http_min_interval_s)

    def fetch(
        self,
        start: date | datetime | pd.Timestamp | None = None,
        end: date | datetime | pd.Timestamp | None = None,
        symbols: list[str] | None = None,
    ) -> pd.DataFrame:
        """Today's snapshot of the master (start/end are ignored; it is not historical)."""
        resp = self.session.get(EQUITY_MASTER_URL)
        resp.raise_for_status()
        df = parse_equity_master(resp.content, date.today())
        if symbols:
            df = df[df["symbol"].isin(symbols)].reset_index(drop=True)
        return df
