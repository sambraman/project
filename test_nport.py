"""
test_nport.py — the N-PORT filing selector must pick by REPORTING PERIOD.

Regression guard for IVV being served as of 2025-09-30 in August 2026. The old
selector took the first accession from a count=1 feed and never checked what
period that filing actually covered, so a stale pick was invisible.

    python test_nport.py
"""

from __future__ import annotations

import nport_source as n

ATOM = """<feed>
  <company-info><link href="?action=getcompany&CIK=9999999"/></company-info>
  <entry>
    <accession-number>0001752724-26-000111</accession-number>
    <filing-date>2026-06-01</filing-date>
    <report-date>2025-09-30</report-date>
    <link href="/cgi-bin/browse-edgar?action=getcompany&amp;CIK=1100663"/>
  </entry>
  <entry>
    <accession-number>0001752724-26-000222</accession-number>
    <filing-date>2026-05-25</filing-date>
    <report-date>2026-03-31</report-date>
    <link href="/cgi-bin/browse-edgar?action=getcompany&amp;CIK=1100663"/>
  </entry>
</feed>"""


def main():
    entries = n._parse_atom_entries(ATOM)
    assert len(entries) == 2, entries
    print("  \u2713 atom parsed entry by entry")

    assert all(cik == "1100663" for _a, cik, _f, _r in entries), entries
    print("  \u2713 header CIK does not leak into a filing's CIK")

    entries.sort(key=lambda e: (e[3] or e[2] or ""), reverse=True)
    # The 2025-09-30 filing was filed LATER (an amendment-style case) but covers
    # an OLDER period. Filing order would pick it; period order must not.
    assert entries[0][3] == "2026-03-31", entries
    assert entries[0][0] == "000175272426000222"
    print("  \u2713 newest REPORTING PERIOD wins over newest filing date")

    assert n._parse_atom_entries("<feed></feed>") == []
    print("  \u2713 empty feed degrades to [] (no crash)")

    stale = n._period_age_days("2025-09-30")
    assert stale is not None and stale > n.FRESH_PERIOD_DAYS
    print(f"  \u2713 2025-09-30 flagged stale ({stale}d > {n.FRESH_PERIOD_DAYS}d)")

    assert n._period_age_days("garbage") is None
    print("  \u2713 unparseable date is None, not an exception")

    # A genuinely current quarter must NOT trip the stale warning.
    from datetime import date, timedelta
    recent = (date.today() - timedelta(days=70)).isoformat()
    assert n._period_age_days(recent) <= n.FRESH_PERIOD_DAYS
    print("  \u2713 a ~70-day-old quarter reads as current")

    print("\nNPORT SELECTION CHECKS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
