"""
nport_source.py — the universal N-PORT fallback.

Every fund files form N-PORT with the SEC quarterly, listing its complete
portfolio. That makes it the catch-all for any issuer without a clean daily feed
(Schwab, Vanguard bond funds, and anything not otherwise routed). It's quarterly,
so callers flag these holdings `is_stale=True`.

Two pieces:
  * ``parse_nport_xml``  — pure parser for the N-PORT XML shape. Fully working;
    exercised offline by the bundled fixtures.
  * ``nport_fetch``      — locates + downloads the latest N-PORT for a ticker
    from SEC EDGAR, then parses it. Offline it reads a fixture. The *live* lookup
    is the one spot to confirm/replace with your own resolver — see # VERIFY.

To drop in your own N-PORT source, replace ``_live_nport_xml`` only; the parser
and the public ``nport_fetch`` contract stay the same.
"""

from __future__ import annotations

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
SEC_HEADERS = {"User-Agent": "etf-backend holdings bot (contact: you@example.com)"}


def _local(tag: str) -> str:
    """Strip the XML namespace so we can match tags regardless of prefix."""
    return tag.rsplit("}", 1)[-1]


def _find(elem, name):
    for child in elem.iter():
        if _local(child.tag) == name:
            return child
    return None


def _parse_date(text: str) -> str:
    text = (text or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return text


def parse_nport_xml(raw: bytes):
    """Parse an N-PORT filing into (as_of_iso, [(ticker, name, weight_decimal)]).

    Weights come from each security's <pctVal> (a percentage). Tickers aren't
    always present in N-PORT (many rows only carry CUSIP/ISIN/LEI); those keep a
    blank ticker and are still returned by name so weight totals stay honest.

    Returns raw tuples (not Holding objects) to keep this module free of an
    import cycle with holdings.py — the caller wraps them.
    """
    root = ET.fromstring(raw)

    # Reporting period end date lives under genInfo/repPdDate (a few variants).
    as_of = ""
    for name in ("repPdDate", "reportDate", "repPdEnd"):
        node = _find(root, name)
        if node is not None and node.text:
            as_of = _parse_date(node.text)
            break

    rows = []
    for sec in root.iter():
        if _local(sec.tag) != "invstOrSec":
            continue
        name = ticker = ""
        weight = 0.0
        for child in sec.iter():
            tag = _local(child.tag)
            if tag == "name" and not name:
                name = (child.text or "").strip()
            elif tag == "ticker":
                ticker = (child.get("value") or child.text or "").strip().upper()
            elif tag == "pctVal":
                try:
                    weight = float((child.text or "0").strip())
                except ValueError:
                    weight = 0.0
        if weight or name or ticker:
            rows.append((ticker, name, weight))
    return as_of, rows


def _live_nport_xml(ticker: str) -> bytes:
    """Fetch the most recent N-PORT XML for `ticker` from SEC EDGAR.

    # VERIFY — this is the one live spot to confirm on your machine. The flow is:
    ticker -> CIK (data.sec.gov ticker map) -> latest NPORT-P submission ->
    primary_doc.xml. The scaffolding is here; wire your resolver / confirm the
    accession lookup against a real filing, or swap this whole function for your
    existing N-PORT downloader.
    """
    raise NotImplementedError(
        "Live N-PORT lookup is not wired yet. Run with offline=True to use the "
        "bundled fixtures, or implement _live_nport_xml() (ticker -> CIK -> "
        "latest NPORT-P -> primary_doc.xml on SEC EDGAR)."
    )


def nport_fetch(ticker: str, offline: bool = False, fixtures_dir: Path | None = None):
    """Public entry: return (as_of_iso, [(ticker, name, weight_decimal)]).

    Weights are normalized to decimal fractions here (N-PORT pctVal is a percent).
    """
    if offline:
        fixtures_dir = Path(fixtures_dir or (Path(__file__).resolve().parent / "fixtures"))
        path = fixtures_dir / "nport" / f"{ticker.upper()}.xml"
        if not path.exists():
            from holdings import HoldingsError
            raise HoldingsError(
                f"offline N-PORT fixture for {ticker} not found at {path}. "
                f"Add fixtures/nport/{ticker.upper()}.xml or use a daily-feed ticker."
            )
        raw = path.read_bytes()
    else:
        raw = _live_nport_xml(ticker)

    as_of, rows = parse_nport_xml(raw)
    # Normalize percent -> decimal in one pass (mirrors holdings._normalize_weights).
    weights = [w for _, _, w in rows]
    scale = 100.0 if weights and max(weights) > 1.0 else 1.0
    from holdings import Holding
    holdings = [Holding(t, n, w / scale) for (t, n, w) in rows]
    return as_of, holdings
