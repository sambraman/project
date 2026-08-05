"""
classify.py — sector + geography for a company, from SEC submissions data.

SEC gives a SIC code (industry) and the filer's location. We map the SIC to a
broad, GICS-style sector label and resolve the location to a country. GICS is
proprietary, so the sector here is a SIC-derived approximation — good enough to
group/filter a portfolio by sector, not an official GICS assignment.
"""

from __future__ import annotations

# SIC ranges -> broad sector. Checked in order; first containing range wins.
# (Finer than SIC's own divisions so tech/health/energy separate out sensibly.)
_SIC_SECTORS = [
    ((100, 999), "Materials"),                 # agriculture/forestry/fishing
    ((1000, 1499), "Materials"),               # metal & mineral mining
    ((1500, 1799), "Industrials"),             # construction
    ((2000, 2199), "Consumer Staples"),        # food
    ((2200, 2399), "Consumer Discretionary"),  # textiles/apparel
    ((2400, 2599), "Industrials"),             # lumber/furniture
    ((2600, 2699), "Materials"),               # paper
    ((2700, 2799), "Communication Services"),  # publishing
    ((2800, 2829), "Materials"),               # industrial chemicals
    ((2830, 2836), "Health Care"),             # biological/pharma prep
    ((2840, 2844), "Consumer Staples"),        # soap/detergents/cosmetics (e.g. PG, CL)
    ((2845, 2899), "Materials"),               # other chemicals
    ((2900, 2999), "Energy"),                  # petroleum refining
    ((3000, 3299), "Materials"),               # rubber/plastics/stone/glass
    ((3300, 3399), "Materials"),               # primary metals
    ((3400, 3569), "Industrials"),             # fabricated metal / machinery
    ((3570, 3579), "Information Technology"),  # computers & office equipment
    ((3580, 3659), "Industrials"),
    ((3660, 3669), "Information Technology"),  # communications equipment
    ((3670, 3679), "Information Technology"),  # semiconductors & electronics
    ((3680, 3699), "Information Technology"),
    ((3700, 3716), "Consumer Discretionary"),  # motor vehicles
    ((3720, 3799), "Industrials"),             # aerospace/transport equipment
    ((3800, 3826), "Information Technology"),   # instruments
    ((3827, 3859), "Health Care"),             # medical/optical instruments
    ((3860, 3999), "Consumer Discretionary"),
    ((4000, 4499), "Industrials"),             # transportation
    ((4500, 4599), "Industrials"),             # air transport
    ((4600, 4699), "Energy"),                  # pipelines
    ((4700, 4799), "Industrials"),
    ((4800, 4899), "Communication Services"),  # communications/telecom
    ((4900, 4949), "Utilities"),               # electric/gas/water
    ((4950, 4999), "Utilities"),
    ((5000, 5199), "Consumer Discretionary"),  # wholesale
    ((5200, 5399), "Consumer Discretionary"),  # retail general
    ((5400, 5499), "Consumer Staples"),        # food stores
    ((5500, 5999), "Consumer Discretionary"),  # retail
    ((6000, 6199), "Financials"),              # banks
    ((6200, 6299), "Financials"),              # brokers/finance
    ((6300, 6499), "Financials"),              # insurance
    ((6500, 6599), "Real Estate"),
    ((6798, 6798), "Real Estate"),             # REITs (GICS puts these in Real Estate)
    ((6700, 6799), "Financials"),              # holding/investment
    ((7000, 7299), "Consumer Discretionary"),  # hotels/personal services
    ((7300, 7372), "Information Technology"),   # computer/data services & software
    ((7373, 7379), "Information Technology"),
    ((7380, 7899), "Communication Services"),   # advertising/media/entertainment
    ((7900, 7999), "Communication Services"),
    ((8000, 8099), "Health Care"),             # health services
    ((8100, 8999), "Industrials"),             # professional services
]

# SEC EDGAR "stateOrCountry" codes that are US states/territories -> United States.
_US_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO",
    "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
    "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "PR", "VI", "GU", "AS", "MP",
}

# A handful of EDGAR foreign country codes (extend as needed).
_COUNTRY_CODES = {
    "A0": "Canada", "A1": "Canada", "B0": "United Kingdom", "F4": "China",
    "F5": "Taiwan", "L2": "Switzerland", "L3": "Germany", "M2": "Japan",
    "N4": "Netherlands", "1K": "Israel", "K3": "South Korea", "D8": "France",
}


def sic_to_sector(sic) -> str:
    try:
        code = int(sic)
    except (TypeError, ValueError):
        return ""
    for (lo, hi), sector in _SIC_SECTORS:
        if lo <= code <= hi:
            return sector
    return ""


def resolve_country(state_or_country: str, state_of_incorporation: str) -> str:
    code = (state_or_country or "").strip().upper()
    if code in _US_CODES:
        return "United States"
    if code in _COUNTRY_CODES:
        return _COUNTRY_CODES[code]
    if (state_of_incorporation or "").strip().upper() in _US_CODES:
        return "United States"
    return _COUNTRY_CODES.get((state_of_incorporation or "").strip().upper(), code)
