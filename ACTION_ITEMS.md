# LookThrough — Action Items (Personal Tool Edition)

You're building the best possible tool for yourself, not selling it. That single fact changes a surprising amount of the advice, so let's start there.

---

## What changes now that you're not commercializing

I gave you advice earlier that was right for a product and wrong for a personal tool. Correcting it:

| Earlier advice | Revised | Why |
|---|---|---|
| Need SOC 2 Type II | **Drop it entirely** | SOC 2 is what you buy to satisfy *someone else's* compliance team. You're your own user. |
| License a data feed before shipping | **Not needed** | Redistribution triggers licensing. Personal use of publicly published holdings files redistributes nothing. |
| Avoid headless browsers | **Actively recommended** | My caution was about building a business on brittle bot-evasion. For a personal tool it's just a browser fetching a public file. |
| Don't persist portfolio data | **Persist freely** | The risk was holding *client* data. This is your own portfolio on your own disk. |
| Capital Group conflict is a gating item | **Mostly moot** | Outside-business-activity policies target businesses. A personal analysis script isn't one. Still: don't run it from a work machine. |

**The biggest unlock:** you can now use approaches that are technically excellent but commercially awkward. That's most of Track 1.

---

## The highest-value change: run it locally

Before writing any code, reconsider the deployment. **Your Mac is a better host than Render's free tier**, and it's free.

**Why, in plain terms:** websites block traffic that looks like it's coming from a datacenter, because that's where scrapers live. Render, AWS, and Google Cloud IP ranges are published and widely blocked. Your home internet looks like a person browsing the web. BlackRock's firewall is far more likely to serve a CSV to your laptop than to a Render container.

**There's a real chance running locally fixes the iShares problem with zero code changes.** Test that before building anything clever:

```bash
cd ~/path/to/project
git pull
python issuer_catalog.py --refresh ishares
python diagnose.py IVV --raw
```

If that returns CSV, the WAF was blocking Render's IP, not your code.

**Cost comparison:**

| Approach | Cost/mo | Spin-down | Persistent disk | IP reputation |
|---|---|---|---|---|
| Local (`uvicorn` on your Mac) | **$0** | Never | Yes, free | **Residential — best** |
| Render free | $0 | 15 min | **No** | Datacenter — worst |
| Render Starter + disk | ~$7 | Never | Yes | Datacenter |

For a tool only you use, local wins on every axis. Run `uvicorn app:app --reload` when you want it; add a `launchd` job for the nightly refresh. Render becomes optional — useful only if you want to check holdings from your phone.

**Recommended shape:** local for everything. If you want remote access later, have the local machine commit the SQLite file and let Render free serve a read-only copy. Data collection happens where the IP is clean; serving happens wherever is convenient.

---

# Track 1 — Holdings look-through

The foundation. Every other feature reads from this table.

## 1.1 Test whether local execution fixes iShares

Covered above. Five minutes, and it may obviate the next item entirely.

## 1.2 If still blocked: use a real browser

**Why this works, plainly:** iShares' protection runs a JavaScript puzzle in the page and only serves data once it's solved. A plain HTTP request can't run JavaScript, so it fails and gets an HTML challenge page instead of your CSV. A headless browser *is* a real browser — it solves the puzzle automatically, because that's what browsers do.

**Why it's right for you specifically:** most reliable option, costs nothing. My earlier hesitation was about building a business on it.

```bash
pip install playwright
playwright install chromium
```

A fallback fetcher, roughly 30 lines, that `holdings.py` reaches for only when plain HTTP fails:

```python
def fetch_via_browser(url: str, timeout=30000) -> str:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        resp = page.goto(url, timeout=timeout)   # JS challenge solves here
        body = resp.text()
        browser.close()
        return body
```

Keep it as a **last resort in the cascade**, not the default: ~2 seconds and ~200MB of Chromium versus ~200ms for plain HTTP. Use it only for issuers that need it. (Playwright on Render needs a custom Docker image — another reason local wins.)

## 1.3 Verify the remaining issuers

Ten minutes each, highest value first:

1. **Schwab** — SCHD is in your tracked defaults and currently falls back to quarterly
2. **VanEck** — SMH, which you follow for the memory cycle
3. Vanguard, Global X, WisdomTree, First Trust, Capital Group, T. Rowe

```bash
# devtools → Network → XHR, click the holdings download, copy the request URL
# paste into issuer_endpoints.json → then:
python issuer_catalog.py --probe schwab
# probe passes → set "verified": true → done
```

**Why the config-file approach:** endpoints change. Keeping them in JSON makes a fix a one-line edit rather than a code change and redeploy. Unverified issuers are excluded from the cascade, so a broken URL costs nothing.

## 1.4 Confirm the N-PORT recency fix

```bash
python diagnose.py IVV SMH
```

Expect the as-of to move from 2025-09-30 to a 2026 quarter.

**Set expectations correctly:** N-PORT is a required quarterly filing that arrives 60–120 days late. That lag is deliberate — funds get it so competitors can't front-run their trades. It's a *floor*, never current. Daily accuracy requires the issuer's own file, which is why 1.1–1.3 matter.

## 1.5 Handle SPY

SPY is legally a Unit Investment Trust, not a `1940 Act` open-end fund, so it doesn't file NPORT-P at all. It needs SSGA's daily XLSX — your `parse_spdr_xlsx` already handles the format, it's the URL that needs fixing. Worth doing since SPY appears in nearly every portfolio.

## 1.6 Add holdings history

The table is already keyed `(fund, holding, as_of)`, so you're storing every snapshot — nothing reads it as a time series yet.

**Why it's worth having:** it shows what a fund *did*. When did IVV's Nvidia weight double? Did your small-cap fund quietly drift into mid-caps? No retail tool shows this, and you get it nearly free because the data's already there.

Add `GET /holdings/history?fund=IVV&holding=NVDA` — about one query against the existing index.

---

# Track 2 — Fundamentals and KPIs

You have 114 KPIs across 11 sectors. Here's how to make it substantially more powerful.

## 2.1 Use SEC's `frames` API for peer comparison ← biggest win

**The problem plainly:** knowing Microsoft's capex intensity is 18% tells you almost nothing alone. Knowing it's 18% *when the hyperscaler median is 12%* tells you a lot. You currently have no way to compute that median without pulling every company individually.

**The fix:** SEC publishes a `frames` endpoint returning *one concept for every filer in one period* in a single request:

```
https://data.sec.gov/api/xbrl/frames/us-gaap/PaymentsToAcquirePropertyPlantAndEquipment/USD/CY2026Q1.json
```

**Why this is dramatically more cost-efficient:** the obvious approach means one `companyfacts` call per company — 500 companies, 500 requests, and with SEC rate limits that's roughly an hour. The `frames` endpoint gets the same data in **one request**. That's the difference between a feature that's practical and one that isn't.

**Steps:**
1. New module `fundamentals/frames.py` with `get_frame(concept, unit, period)`
2. Cache each frame in the datastore under namespace `frames` — they never change once filed, so TTL can be months
3. Compute sector percentiles by intersecting the frame with your sector's ticker list
4. Add `percentile` to each `KPI` object

**Result:** every KPI gains context. "Capex intensity 18% — **87th percentile** among hyperscalers" instead of a bare number.

## 2.2 Add `period` to the KPI primary key

Currently keyed `(ticker, sector)`, so each refresh **overwrites the last**.

**Why that's a real loss:** the value of capex intensity is its *trajectory*. Rising for six straight quarters is a thesis; one reading is trivia. You're discarding history on every refresh.

Change the key to `(ticker, sector, period)`. One schema change, a few lines, and every KPI becomes a chartable time series.

## 2.3 Parse the XBRL instance for segment data

This unlocks Azure, AWS, and Google Cloud revenue — the item I couldn't deliver earlier.

**Why it's hard, plainly:** when a company reports "Azure revenue," it tags the number with a label saying "this belongs to the Intelligent Cloud segment." SEC's convenient `companyfacts` API strips those labels and returns bare numbers, so segment detail is genuinely absent — the data isn't there to extract.

**Where it does live:** the filing's XBRL instance document, and the pre-rendered "R-files" (the financial statement tables as HTML).

**Recommended approach — the cheap one:** parse the R-files rather than raw XBRL. They're HTML tables where segment rows are already laid out and labeled. Standard-library `html.parser` handles it; no new dependency, and it's an afternoon rather than a week.

Start with segment revenue for your five hyperscalers. Once that works, the pattern generalizes to bank segments and insurance lines of business.

## 2.4 Mine earnings releases (8-K EX-99.1)

**Why:** the best metrics often never appear in XBRL. Cloud growth rates, subscriber counts, same-store sales, backlog commentary — management puts these in the press release, not the financial statements.

The 8-K EX-99.1 exhibit is the earnings release, and EDGAR full-text search finds them. Parse for labeled figures near known phrases.

This is heuristic and will be imperfect: mark outputs `basis: "extracted"` with lower confidence than XBRL tags, and **always store the source sentence** so you can eyeball it.

## 2.5 Track KPI coverage as data

Add a `kpi_coverage` table logging attempted-vs-resolved per XBRL tag across your universe.

**Why:** today you discover a missing tag when a company shows a blank. With coverage logging you can ask "which tags fail most often?" and fix the top five, improving dozens of companies at once. Turns tag maintenance from whack-a-mole into a prioritized list.

## 2.6 Add a data-quality score

Each KPI already carries `basis` and `tag`. Roll them into a per-company score — what fraction resolved from XBRL tags vs derived vs missing — and surface it. You'll instantly know whether a company's numbers are trustworthy or half-guessed.

## 2.7 Sectors worth adding next

The gaps that would matter to you:

- **Aerospace/defense** — backlog, book-to-bill (ties to your GE Vernova work)
- **Homebuilders** — orders, cancellation rate, backlog
- **Asset managers** — AUM, net flows, fee rate (your actual industry)
- **Payments/fintech** — TPV, take rate
- **Shipping/logistics** — rates, utilization

Asset managers is the interesting one: nobody builds good tooling for it, and you'd know immediately whether the numbers look right.

---

# Track 3 — Storage and speed

## 3.1 Move the refresh out of the web request

**Why this is a bug, not an optimization:** `POST /refresh` does minutes of rate-limited work. Web servers kill long requests — Render's proxy at around 100 seconds. The refresh gets terminated mid-run and reports failure without telling you it was partial, so you'd see incomplete data with no obvious cause.

**Locally**, run it directly on a schedule:

```bash
# crontab -e  →  weekdays at 6pm
0 18 * * 1-5 cd ~/project && /usr/bin/python3 refresh.py
```

**On Actions**, call the script rather than the endpoint:

```yaml
- run: python refresh.py && python refresh_prices.py
```

Either way the work happens somewhere with a 6-hour budget instead of a 100-second one.

## 3.2 Persistence

Running locally makes this trivial — SQLite on your disk is persistent, fast, and free. Point `DATA_DIR` at a real folder and you're done.

**If you also want remote access**, the cheapest reliable pattern is the one your repo already uses for `fundamentals.db`: build the database locally or in Actions, commit it, let Render serve a read-only copy. Zero runtime fetching, immune to spin-down, $0.

**Skip Postgres** unless you need concurrent writers. For one user, SQLite is faster and simpler — I recommended Postgres back when this looked like a product, and that no longer applies.

## 3.3 Add indexes as queries demand them

You have `idx_hold_holding` for reverse look-through. Add others when a query gets slow — you'll want `(ticker, period)` on `kpis` once 2.2 lands, and `(fund, as_of)` for holdings history.

**Why indexes matter, plainly:** without one, SQLite reads every row to answer "which funds hold NVDA." With one, it jumps straight there. On 50,000 rows that's the difference between noticeable and instant.

## 3.4 Set up a backup

Your SQLite file will eventually hold months of accumulated filings you can't easily re-fetch. `cp lookthrough.db lookthrough-$(date +%F).db` weekly, or point Time Machine at the folder. Cheap insurance against a bad migration.

---

# Track 4 — "My Portfolio" tool

The payoff feature. A working MVP first, then what makes it genuinely better than anything available.

## Phase 1 — MVP (exposures on screen)

### 4.1 Broker CSV normalizer

Every broker exports a different shape. Fidelity uses `Symbol`/`Quantity`/`Current Value`; Schwab uses `Symbol`/`Quantity`/`Market Value` with a disclaimer block below the data; Vanguard splits across two tables.

Build a detection layer (reuse `_find_col` from `holdings.py`) mapping to canonical `{ticker, quantity, market_value}`.

**The unglamorous parts that will actually break it:**
- Cash rows (`SPAXX**`, `CASH`, `Pending Activity`)
- Currency formatting — `$1,234.56` needs stripping before `float()`
- Footer totals that look like positions
- Options symbols (`-AAPL250117C150`) — detect and exclude
- Trailing disclaimer paragraphs
- Fidelity's `**` footnote markers appended to tickers

Write it against **your actual export**. Generic CSV parsers fail on real broker files.

### 4.2 Classify each position

Fund needing look-through, or direct holding? `classify_issuer` plus a fund-directory check covers it.

### 4.3 Aggregate in SQL

The decision that determines whether the tool feels instant:

```sql
SELECT h.holding, h.name, SUM(p.market_value * h.weight) AS exposure
FROM positions p
JOIN holdings h ON h.fund = p.ticker AND h.as_of = (
    SELECT as_of FROM holdings_meta WHERE fund = p.ticker
)
GROUP BY h.holding
ORDER BY exposure DESC;
```

**Why SQL rather than Python loops:** with holdings pre-warmed, the database does the whole join in memory in milliseconds. Looping in Python over 30 positions × 500 holdings each means 15,000 iterations plus dictionary merging — and worse, it tempts you into fetching per position. Let SQLite do what it's good at.

### 4.4 Merge direct and indirect

The headline output: you own NVDA directly *and* through VTI, IVV, and SMH. Show **total 8.3%** with the breakdown underneath. That one view is why the tool exists — nobody sees their real concentration otherwise.

### 4.5 Be honest about coverage

Fund weights rarely sum to 1.0 — cash, futures, FX forwards, negative derivative rows.

**Why this matters more than it sounds:** if a fund's file accounts for only 94% of assets and you silently scale to 100%, every exposure you report is overstated by 6%. Showing "94% resolved" is both more honest and more useful.

## Phase 2 — What makes it genuinely good

### 4.6 Look-through portfolio fundamentals ← the killer feature

Combine Track 2 with Track 4: compute the **weighted-average fundamentals of your entire portfolio**, including everything held inside ETFs.

What's my blended P/E? Weighted ROE? Aggregate capex intensity? What share of my equity exposure sits in companies with negative earnings?

**Why this is worth building:** essentially nothing available to retail does this properly — Morningstar X-Ray gives a crude version. You'd have real portfolio-level fundamentals computed from filings, and you already have both halves of the machinery.

Implementation is a weighted average over your exposure table joined to the KPI table. Skip holdings with missing KPIs, and **report what fraction you covered** so the number is interpretable.

### 4.7 Overlap analysis

How much do two funds actually duplicate? People hold VTI and VOO thinking they're diversifying; the overlap is enormous.

```sql
SELECT SUM(MIN(a.weight, b.weight)) AS overlap
FROM holdings a JOIN holdings b ON a.holding = b.holding
WHERE a.fund = 'VTI' AND b.fund = 'VOO';
```

Output as a matrix across your funds. Immediately actionable and genuinely surprising.

### 4.8 Concentration and hidden risk

- Top 10 holdings as a share of total equity exposure
- Sector and geography breakdown *after* look-through — your real tech weight, not your fund labels
- Single-name risk: is one company 12% of your net worth via four different funds?

### 4.9 Fee analysis

Weighted average expense ratio and annual dollar cost. Expense ratios aren't in XBRL, but they're in each fund's N-CEN and prospectus, and small enough to hand-curate as JSON for the funds you actually own.

**Why bother:** "$1,240/year in fund fees" concentrates the mind far better than "0.31% blended."

### 4.10 Trade simulator

You built something like this before. "If I sell $10k of VTI and buy $10k of SCHD, how does my Nvidia exposure change?" Runs entirely against cached data — no network, instant.

### 4.11 Snapshots over time

Store each upload with a timestamp. Then you can see how your true exposures drifted *even when you didn't trade*, because the funds traded underneath you. Genuinely novel view.

---

## Suggested order

| # | Item | Why here | Effort |
|---|---|---|---|
| 1 | Run locally, test iShares (1.1) | May fix everything; 5 minutes | Trivial |
| 2 | Playwright fallback if needed (1.2) | Definitive WAF fix | Half day |
| 3 | Refresh off the request path (3.1) | Real bug on any tier | 1 hour |
| 4 | Verify Schwab + VanEck (1.3) | Biggest coverage gain | 30 min |
| 5 | Portfolio MVP (4.1–4.5) | The payoff feature | 2–3 days |
| 6 | KPI period key (2.2) | Cheap, unlocks trends | 1 hour |
| 7 | Look-through fundamentals (4.6) | The differentiator | 1 day |
| 8 | Frames API percentiles (2.1) | Makes every KPI meaningful | 1 day |
| 9 | Overlap + concentration (4.7–4.8) | High insight per line of code | 1 day |
| 10 | Segment revenue via R-files (2.3) | Hardest; do it when you want it | 2–3 days |

## Total cost

**$0**, running locally on free data sources. EODHD you already pay for. Optional Render Starter at ~$7/mo only if you want phone access — and even that has a free workaround via a committed database.

The genuinely scarce resource is your time, which is why the ordering front-loads the highest insight-per-hour items.

## Known limitations (worth remembering even for a personal tool)

| Limitation | Why | Mitigation |
|---|---|---|
| N-PORT is 60–120 days stale | Deliberate SEC lag | Flag `is_stale`; get the issuer daily feed |
| Semi-transparent ETFs publish a proxy portfolio | TCHP, TDVG, TEQI, TGRT, TSPA | Already flagged; don't blend into look-through |
| Cloud segment revenue absent | `companyfacts` drops dimensional axes | Track 2.3 |
| Bank NIM approximated | Avg earning assets not in `companyfacts` | Documented in warnings; reads slightly low |
| Fund weights don't sum to 1.0 | Cash, derivatives, FX | Report coverage, never silently normalize |
