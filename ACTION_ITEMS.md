# LookThrough — Action Items

Sequenced work for three problems: **holdings freshness**, **storage/speed**, and the **portfolio look-through tool**.

The dependency worth stating up front: the portfolio tool is only as good as your holdings coverage. If IVV still falls through to N-PORT, every look-through inherits that staleness. **Track 1 gates the quality of Track 3**, even though Track 3 is the interesting build.

Run `python healthcheck.py` at any point to see current state. `--live` adds network probes.

---

## Track 1 — Holdings freshness (blocks everything else)

### 1.1 Verify the iShares URL — highest priority

A likely fix already shipped: the catalog now captures the fund's **real page path** (with slug) from `productPageUrl`, and `fetch_ishares` tries three URL patterns, validates the response is genuinely CSV (not an HTML challenge), and remembers which pattern worked.

```bash
python issuer_catalog.py --refresh ishares   # populate catalog w/ slug paths
python diagnose.py IVV --raw                 # see which pattern wins
```

If all three still fail, get the real URL from devtools → Network → XHR on the IVV holdings page and add it to `ISHARES_URL_PATTERNS` in `holdings.py`.

**Success looks like:** IVV `source=ishares`, as-of within a day or two.

### 1.2 Verify the other seven issuers

Each is ~10 minutes. Order by value: **Schwab first** (SCHD is in your tracked defaults and currently falls to quarterly), then VanEck (SMH), then the rest.

```bash
# devtools → Network → XHR → copy request URL into issuer_endpoints.json
python issuer_catalog.py --probe schwab
# probe passes → set "verified": true
```

Unverified issuers are excluded from the cascade, so a broken URL costs nothing.

### 1.3 Confirm the N-PORT recency fix live

The selector now ranks by **reporting period**, not list position, and verifies the period inside each document.

```bash
python diagnose.py IVV SMH
```

Expect as-of to move from 2025-09-30 to a 2026 quarter. **Note:** N-PORT is structurally 60–120 days behind. It's a floor, never "yesterday" — daily currency requires the issuer feed.

### 1.4 Handle SPY explicitly

SPY is a UIT and doesn't file NPORT-P like a `1940 Act` fund. It needs the SSGA daily file or it will never resolve. Currently returns 404 or falls through.

### 1.5 Email BlackRock

Set `HTTP_CONTACT` first so requests are identifiable, then ask about data access. The only fix that survives a compliance review once you have paying customers.

---

## Track 2 — Storage and speed

### 2.1 Move the refresh into the Action — do this first

Replace the `curl POST /refresh` step with:

```yaml
- run: pip install -r requirements.txt
- run: python refresh.py && python refresh_prices.py
```

**Why this is a real bug, not an optimization:** `/refresh` triggers a cold start then does minutes of rate-limited work (iShares at 8/60s, N-PORT probes). Render's proxy kills long HTTP requests, so the refresh gets terminated mid-run and `curl --fail` reports failure without telling you it was partial. Actions runners have a 6-hour limit and full network.

### 2.2 Pick a persistence story

| Option | Cost | Trade-off |
|---|---|---|
| Render Starter + disk | ~$7/mo | Cleanest. No spin-down, real disk. |
| Free Postgres (Supabase/Neon) | $0 | Survives deploys and spin-down. Requires porting `datastore.py` from SQLite. |
| Commit the DB to git | $0 | Matches your existing `fundamentals.db` pattern. Action builds it, Render reads it. Grows git history. |

**Render free tier has no persistent disks**, so `DATA_DIR` has nowhere durable to point and the store dies on every deploy and spin-up. `healthcheck.py` reports `degraded` when this happens.

### 2.3 Add `period` to the KPI primary key

Currently `(ticker, sector)` — it overwrites, so you can't chart capex intensity across eight quarters, which is the entire point of the metric. Schema change plus a few lines converts snapshots into a time series.

### 2.4 Pre-warm constituent data

After the holdings refresh, walk distinct constituents and populate prices and KPIs, so the first user request isn't a cold EDGAR walk.

### 2.5 Add a `kpi_coverage` table

Log attempted-vs-resolved per XBRL tag across your universe. Tells you which fallback tags to add from data rather than ticker-by-ticker discovery.

---

## Track 3 — Portfolio look-through tool

**Decide before building:** whether uploaded portfolios are persisted. Session-only is dramatically simpler and safer — the moment you store client holdings you're in scope for the SOC 2 work you already identified as non-negotiable for RIA sales, plus breach-notification obligations. Default to in-memory until security is deliberately designed.

### 3.1 Broker CSV normalizer

Fidelity, Schwab, and Vanguard exports have completely different headers. Build a column-detection layer (reuse `_find_col`) mapping to canonical `{ticker, quantity, market_value}`.

Handle the junk: cash rows, "Pending Activity", options symbols, footer totals, `$` and `,` in numbers, and the disclaimer block many brokers append below the data.

### 3.2 Position classifier

Is each row a fund needing look-through, or a direct holding? `classify_issuer` plus a fund-directory check covers most cases.

### 3.3 Aggregate in SQL, not Python

This is the decision that makes it fast:

```sql
SELECT h.holding, h.name, SUM(p.market_value * h.weight) AS exposure
FROM positions p
JOIN holdings h ON h.fund = p.ticker
GROUP BY h.holding
ORDER BY exposure DESC;
```

With holdings pre-warmed, a 30-position portfolio resolves in milliseconds instead of 30 network calls. The `idx_hold_holding` index already supports it.

### 3.4 Merge direct and indirect exposure

The headline feature: someone holds NVDA directly **and** through VTI, IVV, and SMH — total 8.3%, not four separate lines. Keep the decomposition so they can see where it comes from.

### 3.5 Handle weight integrity honestly

Fund weights rarely sum to 1.0 — cash, futures, FX forwards, negative derivative rows. Compute a `coverage` figure per fund and surface it. **Silently normalizing to 100% would overstate every exposure.**

### 3.6 Cap recursion for funds-of-funds

Some holdings are themselves funds. Depth limit of 2–3 with cycle detection. Flag anything unresolved rather than dropping it.

### 3.7 Report what you couldn't resolve

Unknown tickers, stale-source funds, and non-equity holdings should appear as an explicit "unresolved: X% of portfolio" line. An RIA needs to know the denominator.

---

## Known limitations (be honest with users)

| Limitation | Why | Mitigation |
|---|---|---|
| N-PORT is 60–120 days stale | SEC filing lag, structural | Flag `is_stale`; get the issuer daily feed |
| Semi-transparent ETFs publish a proxy portfolio | TCHP, TDVG, TEQI, TGRT, TSPA | Already flagged per-ticker; don't blend into look-through |
| Cloud segment revenue unavailable | `companyfacts` drops dimensional axes | Parse the XBRL instance / R-files |
| Bank NIM approximated | Avg earning assets not in `companyfacts` | Documented in warnings; reads slightly low |
| Scraped data isn't licensed for redistribution | Vendor ToS | License a feed before selling to RIAs |

## Compliance items

- **Capital Group employment** — review outside-business-activity and IP policies before commercializing, especially anything targeting CG product data.
- **SOC 2 Type II** — non-negotiable baseline for RIA sales; drives the persistence decision in Track 3.
- **Data licensing** — scraped issuer data is fine for a prototype, not for a product with customers.

---

## Suggested order

1. `python healthcheck.py` — baseline
2. Track 1.1 (iShares) + 1.3 (confirm N-PORT) — unblocks everything
3. Track 2.1 (Action runs refresh) — fixes a real bug on any tier
4. Track 2.2 (persistence) — makes caching real
5. Track 1.2 (remaining issuers) — incremental coverage
6. Track 3.1–3.4 (portfolio MVP)
7. Track 2.3–2.5, Track 3.5–3.7 (depth and honesty)
