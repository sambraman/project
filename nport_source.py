"""
nport_source.py — the universal N-PORT fallback, sourced live from SEC EDGAR.

Every registered fund files form N-PORT with the SEC, listing its complete
portfolio. That makes it the catch-all for any issuer without a clean daily feed
(Schwab, Vanguard bond funds, and anything not otherwise routed). It's quarterly
with a filing lag, so callers flag these holdings `is_stale=True`.

The live flow (all confirmed working against SEC EDGAR):
  1. ticker -> (CIK, seriesId) via SEC's official fund directory
     (https://www.sec.gov/files/company_tickers_mf.json), cached to
     .sec_fund_map.json.
  2. seriesId -> the fund's latest NPORT-P accession via EDGAR browse.
  3. accession -> primary_doc.xml, parsed into holdings.

N-PORT identifies each holding by name + CUSIP + ISIN (+ LEI), *not* ticker — so
``nport_fetch`` returns dict rows carrying those identifiers, and the caller
(holdings.fetch_nport) enriches them to tickers via OpenFIGI.

Returned rows: [{"name", "cusip", "isin", "lei", "ticker", "weight"}] where
`weight` is a percentage (pctVal); the caller normalizes to a decimal.
"""

from __future__ import annotations

import json
import re
import ssl
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CONTEXT = ssl.create_default_context()

# SEC requires a descriptive User-Agent with contact info on automated requests.
SEC_HEADERS = {"User-Agent": "etf-backend holdings bot (contact: sambraman12@gmail.com)"}
SEC_FUND_MAP_URL = "https://www.sec.gov/files/company_tickers_mf.json"
_FUND_MAP_CACHE = Path(__file__).resolve().parent / ".sec_fund_map.json"


def _get(url: str, timeout: int = 45) -> bytes:
    req = urllib.request.Request(url, headers=SEC_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as r:
        return r.read()


# --------------------------------------------------------------------------- #
# Parsing (pure; exercised offline by the bundled fixtures)
# --------------------------------------------------------------------------- #
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _first_text(root, name):
    for e in root.iter():
        if _local(e.tag) == name and e.text:
            return e.text.strip()
    return ""


def _parse_date(text: str) -> str:
    text = (text or "").strip()
    if "T" in text and text[:4].isdigit():
        text = text.split("T", 1)[0]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return text


def parse_nport_xml(raw: bytes):
    """Parse an N-PORT filing into (as_of_iso, rows).

    rows: [{"name", "cusip", "isin", "lei", "ticker", "weight"}]. `ticker` is
    usually blank (N-PORT rarely carries it); the caller enriches from CUSIP/ISIN.
    `weight` is the pctVal percentage. Rows with no weight and no id are skipped.
    """
    root = ET.fromstring(raw)
    as_of = ""
    for name in ("repPdDate", "reportDate", "repPdEnd"):
        as_of = _first_text(root, name)
        if as_of:
            as_of = _parse_date(as_of)
            break

    rows = []
    for sec in root.iter():
        if _local(sec.tag) != "invstOrSec":
            continue
        row = {"name": "", "cusip": "", "isin": "", "lei": "", "ticker": "", "weight": 0.0}
        for child in sec.iter():
            tag = _local(child.tag)
            if tag == "name" and not row["name"]:
                row["name"] = (child.text or "").strip()
            elif tag == "title" and not row["name"]:
                row["name"] = (child.text or "").strip()
            elif tag == "cusip" and not row["cusip"]:
                row["cusip"] = (child.text or "").strip()
            elif tag == "lei" and not row["lei"]:
                row["lei"] = (child.text or "").strip()
            elif tag == "isin":
                row["isin"] = (child.get("value") or child.text or "").strip()
            elif tag == "ticker":
                row["ticker"] = (child.get("value") or child.text or "").strip().upper()
            elif tag == "pctVal":
                try:
                    row["weight"] = float((child.text or "0").strip())
                except ValueError:
                    row["weight"] = 0.0
        if row["weight"] or row["name"] or row["cusip"] or row["isin"]:
            rows.append(row)
    return as_of, rows


# --------------------------------------------------------------------------- #
# Live SEC EDGAR resolution
# --------------------------------------------------------------------------- #
def _load_fund_directory() -> dict:
    """SEC fund directory: TICKER -> {"cik", "series"}. Cached to disk."""
    if _FUND_MAP_CACHE.exists():
        try:
            return json.loads(_FUND_MAP_CACHE.read_text())
        except Exception:
            pass
    data = json.loads(_get(SEC_FUND_MAP_URL))
    out = {}
    for row in data.get("data", []):
        # fields: [cik, seriesId, classId, symbol]
        cik, series, _cls, symbol = row[0], row[1], row[2], row[3]
        if symbol:
            out[str(symbol).upper()] = {"cik": cik, "series": series}
    try:
        _FUND_MAP_CACHE.write_text(json.dumps(out))
    except Exception:
        pass
    return out


def _latest_nport_accession(series: str):
    """Return (trust_cik, accession_no_dashes) for the fund series' most recent
    NPORT-P filing, or (None, None). EDGAR accepts a Series ID in the CIK slot."""
    atom = _get(
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
        f"&CIK={series}&type=NPORT-P&dateb=&owner=include&count=1&output=atom"
    ).decode("utf-8", "replace")
    accs = re.findall(r"accession-number>([\d-]+)<", atom)
    ciks = re.findall(r"CIK=(\d+)", atom)
    if not accs:
        return None, None
    cik = ciks[0] if ciks else None
    return cik, accs[0].replace("-", "")


def _live_nport_rows(ticker: str):
    directory = _load_fund_directory()
    rec = directory.get(ticker.upper())
    if not rec:
        raise LookupError(
            f"{ticker} not found in the SEC fund directory (not an SEC-registered "
            f"fund, or a UIT like SPY that files separately)."
        )
    trust_cik, acc = _latest_nport_accession(rec["series"])
    if not acc:
        raise LookupError(f"no NPORT-P filing found for {ticker} (series {rec['series']}).")
    cik = trust_cik or rec["cik"]
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"
    # primary_doc.xml is the N-PORT payload in every current filing.
    raw = _get(base + "primary_doc.xml")
    return parse_nport_xml(raw)


def nport_fetch(ticker: str, offline: bool = False, fixtures_dir: Path | None = None):
    """Public entry: return (as_of_iso, rows). See module docstring for row shape."""
    if offline:
        fixtures_dir = Path(fixtures_dir or (Path(__file__).resolve().parent / "fixtures"))
        path = fixtures_dir / "nport" / f"{ticker.upper()}.xml"
        if not path.exists():
            from holdings import HoldingsError
            raise HoldingsError(
                f"offline N-PORT fixture for {ticker} not found at {path}. "
                f"Add fixtures/nport/{ticker.upper()}.xml or use a daily-feed ticker."
            )
        return parse_nport_xml(path.read_bytes())

    try:
        return _live_nport_rows(ticker)
    except LookupError as e:
        from holdings import HoldingsError
        raise HoldingsError(str(e)) from e
