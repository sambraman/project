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
from datetime import date, datetime
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


def _parse_atom_entries(atom: str):
    """[(accession, cik, filing_date, report_date)] parsed ENTRY BY ENTRY.

    The old code ran re.findall over the whole document, so accession[0] and
    CIK[0] came from unrelated places — the atom header carries CIK= links
    before any filing entry, so the CIK could belong to the feed, not the
    filing. Splitting on <entry> keeps each filing's fields together.
    """
    out = []
    for chunk in re.split(r"<entry[\s>]", atom)[1:]:
        acc = re.search(r"accession-n(?:umber|unber)>([\d-]+)<", chunk)
        if not acc:
            continue
        cik = re.search(r"CIK=(\d+)", chunk)
        filed = re.search(r"filing-date>([\d-]+)<", chunk)
        report = re.search(r"(?:report|period)-date>([\d-]+)<", chunk)
        out.append((acc.group(1).replace("-", ""),
                    cik.group(1) if cik else None,
                    filed.group(1) if filed else "",
                    report.group(1) if report else ""))
    return out


def _nport_candidates(series: str, count: int = 40):
    """Candidate NPORT-P filings for a series, NEWEST REPORTING PERIOD FIRST.

    Why not just take the first filing EDGAR returns: filing order is not
    period order. An amendment (NPORT-P/A) restating an old quarter can be
    filed after a newer original, and a stale pick is invisible downstream —
    which is exactly how IVV ended up served as of 2025-09-30. Sorting on the
    report date (falling back to filing date) makes the choice correct
    regardless of how EDGAR happens to order the feed.
    """
    atom = _get(
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
        f"&CIK={series}&type=NPORT-P&dateb=&owner=include&count={count}"
        "&output=atom"
    ).decode("utf-8", "replace")
    entries = _parse_atom_entries(atom)
    # Sort key: report date if the feed gave one, else filing date.
    entries.sort(key=lambda e: (e[3] or e[2] or ""), reverse=True)
    return entries


def _latest_nport_accession(series: str):
    """(trust_cik, accession) for the most recent NPORT-P. Kept for
    compatibility; prefer _nport_candidates for the full ranked list."""
    cands = _nport_candidates(series, count=40)
    if not cands:
        return None, None
    acc, cik, _filed, _report = cands[0]
    return cik, acc


MAX_NPORT_PROBES = 4
FRESH_PERIOD_DAYS = 135      # a current quarterly filing is ~60-120 days back


def _period_age_days(as_of: str):
    try:
        y, m, d = map(int, str(as_of).split("-"))
        return (date.today() - date(y, m, d)).days
    except Exception:
        return None


def _live_nport_rows(ticker: str):
    directory = _load_fund_directory()
    rec = directory.get(ticker.upper())
    if not rec:
        raise LookupError(
            f"{ticker} not found in the SEC fund directory (not an SEC-registered "
            f"fund, or a UIT like SPY that files separately)."
        )
    cands = _nport_candidates(rec["series"])
    if not cands:
        raise LookupError(
            f"no NPORT-P filing found for {ticker} (series {rec['series']}).")

    # Walk candidates newest-first and VERIFY the period inside each document.
    # The feed's ordering is a hint, not proof; the filing itself is the truth.
    # Stop as soon as one is genuinely current, so the normal case costs one
    # request and only a stale-looking result triggers extra probes.
    best = None
    for acc, trust_cik, filed, _report in cands[:MAX_NPORT_PROBES]:
        cik = trust_cik or rec["cik"]
        try:
            raw = _get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"
                       "primary_doc.xml")
            as_of, rows = parse_nport_xml(raw)
        except Exception:
            continue
        if not rows:
            continue
        if best is None or (as_of or "") > (best[0] or ""):
            best = (as_of, rows, filed)
        age = _period_age_days(as_of)
        if age is not None and age <= FRESH_PERIOD_DAYS:
            break            # current enough; no need to look further

    if best is None:
        raise LookupError(f"no parsable NPORT-P document for {ticker}.")

    as_of, rows, filed = best
    age = _period_age_days(as_of)
    if age is not None and age > FRESH_PERIOD_DAYS:
        print(f"  \u26a0 {ticker}: newest N-PORT period is {as_of} ({age} days "
              f"old, filed {filed}). The fund may have stopped filing under "
              f"this series, or the daily issuer feed is the only current "
              f"source.")
    return as_of, rows


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
