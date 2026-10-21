"""Offline fakes for ingest tests: a scripted HTTP session and a tmp-dir Settings."""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from findata.settings import Settings


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        content: bytes = b"",
        json_data: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.content = content if json_data is None else json.dumps(json_data).encode()
        self._json = json_data
        self.headers = headers or {}

    def json(self) -> Any:
        if self._json is None:
            return json.loads(self.content or b"null")
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


Handler = Callable[[str, str, dict[str, Any]], FakeResponse]


class FakeSession:
    """Stands in for PoliteSession: routes (method, url, kwargs) to a handler and logs calls."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("GET", url, kwargs))
        return self.handler("GET", url, kwargs)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("POST", url, kwargs))
        return self.handler("POST", url, kwargs)


def make_zip(name: str, content: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, content)
    return buf.getvalue()


BHAV_HEADER = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
    "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,"
    "PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,"
    "TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd01,Rsvd02,Rsvd03,Rsvd04,"
)


def bhav_csv(day: str = "2024-01-05") -> bytes:
    rows = [
        f"{day},{day},CM,NSE,STK,2885,INE002A01018,RELIANCE,EQ,,,,,RELIANCE INDUSTRIES LTD,"
        "2600.00,2620.00,2590.00,2610.50,2611,2595.00,,2610.50,,,1000,2610500.00,50,F1,1,,,,,",
        f"{day},{day},CM,NSE,STK,17963,INE239A01024,NESTLEIND,EQ,,,,,NESTLE INDIA LTD,"
        "2550.00,2600.00,2500.00,2580.00,2580,25500.00,,2580.00,,,500,1290000.00,20,F1,1,,,,,",
        f"{day},{day},CM,NSE,STK,99999,INE999Z01011,SOMEBOND,N1,,,,,SOME BOND,"
        "100,100,100,100,100,100,,100,,,1,100,1,F1,1,,,,,",
    ]
    return ("\n".join([BHAV_HEADER, *rows]) + "\n").encode()


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", http_min_interval_s=0.0)
