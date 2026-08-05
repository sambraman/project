# ETF Holdings Backend

A small web service that fetches **full** ETF holdings from the issuers
(iShares, SPDR; Vanguard via N-PORT), caches them, and serves them as JSON to
your frontend. It refreshes once a day on a schedule, so users always get a fast
response and the issuer sites only get one polite request per fund per day.

```
frontend (Claude Design)  ->  THIS API  ->  cache (SQLite)
                                  ^
                          nightly refresh job -> issuer files
```

---

## What each file does

| File | Role |
|---|---|
| `holdings.py` | Fetches + parses holdings per issuer; `get_holdings(ticker)` is the core, plus a `python holdings.py <TICKER>` CLI |
| `nport_source.py` | The universal N-PORT fallback (parser + the one live-lookup spot to wire) |
| `cache.py` | Stores results in a local SQLite file |
| `refresh.py` | Pulls every tracked ticker into the cache; runs nightly or on demand |
| `app.py` | The web server (FastAPI) with the endpoints your frontend calls |
| `smoke_test.py` | Offline logic checks — runs the whole function path with **no network, no server** |
| `fixtures/` | Bundled sample issuer files (one per issuer type + real VTI/VEA/VXUS) so offline mode works out of the box |
| `Dockerfile` | Makes the deploy identical on any host |
| `.env.example` | All the settings you can configure |

> **Note (Aug 2026):** the folder originally shipped with only this README —
> the code above was (re)built here, ported from the look-through app's proven
> holdings logic and made **stdlib-only** so the function and its tests run with
> zero installs. FastAPI/pandas are needed only for the web server and live SPDR.

### Coverage — any ETF from an accepted issuer

`get_holdings()` no longer needs a ticker to be pre-registered. It **cascades**
through the daily feeds and returns the first that yields real holdings, then
falls back to N-PORT:

* **SPDR, Invesco, Vanguard** — the holdings URL is keyed by ticker, so *any* of
  their ETFs resolves live with no configuration (e.g. `XLE`, `RSP`, `VGT`).
* **iShares** — needs a numeric product id. Common funds are seeded; everything
  else is resolved from the live iShares product screener and cached to
  `.ishares_map.json`. (The screener URL is a `# VERIFY` spot.)
* **Everything else** (Schwab, other issuers) — routes to the N-PORT fallback,
  flagged `is_stale`. **The live N-PORT download is still a stub** (`# VERIFY` in
  `nport_source.py`): the parser is done and works on fixtures, but wiring the
  SEC EDGAR ticker→CIK→filing lookup is the one piece left for full non-daily
  coverage. Until then, non-daily issuers return a clear error live.

So live, with `OFFLINE=0`, the backend serves **any iShares / SPDR / Invesco /
Vanguard ETF**; other issuers need the N-PORT fetch wired.

---

## Test the holdings function *without* running the backend

The whole point: you can exercise the logic — routing, per-issuer parsing, the
as-of tag, the stale flag, caching — with **no server and no internet**, using
the bundled `fixtures/`.

```bash
python smoke_test.py                 # -> "ALL SMOKE CHECKS PASS"  (stdlib only)

# "ticker in, full holdings + as-of out", straight from the function:
python holdings.py IVV  --offline            # iShares daily (fresh)
python holdings.py QQQ  --offline            # Invesco daily (fresh)
python holdings.py SPY  --offline            # SPDR daily   (fresh)
python holdings.py VTI  --offline --limit 0  # Vanguard, ~1,100 real holdings
python holdings.py SCHD --offline            # N-PORT fallback (⚠ flagged stale)
python holdings.py IVV  --offline --json     # machine-readable
```

Each prints the fund's holdings sorted by weight with the issuer's **as-of
date** and, for quarterly N-PORT sources, a stale flag. In code:

```python
from holdings import get_holdings
res = get_holdings("IVV", offline=True)   # offline=False (default) fetches live
print(res.ticker, res.as_of, res.is_stale, res.count)
for h in res.holdings:
    print(h.ticker, h.name, h.weight)     # weight is a decimal fraction
```

Set `OFFLINE=1` to make `refresh.py` and the web server serve fixtures too —
handy for a no-network demo. Drop a real issuer file into `fixtures/` (named
`<TICKER>.<issuer>.csv/.json`, or `fixtures/nport/<TICKER>.xml`) to test more
tickers offline.

How each issuer is handled (most efficient method per issuer):

| Issuer | Method | Freshness |
|---|---|---|
| BlackRock (iShares) | Direct daily CSV (via product-id map) | Daily |
| State Street (SPDR) | Direct daily XLSX (stable ticker URL) | Daily |
| Invesco | Direct daily holdings CSV | Daily |
| Vanguard | N-PORT fallback (no clean daily feed) | Quarterly, flagged stale |
| Schwab + any other issuer | N-PORT universal fallback | Quarterly, flagged stale |

Unknown tickers no longer error — they route to the N-PORT fallback, so "etc."
issuers are still covered (just quarterly). Vanguard/Schwab/N-PORT paths need
your N-PORT parser wired via `nport_fetch`; until then they return a clear 501
telling you to wire it.

**Endpoints:**
- `GET /health` — is it alive
- `GET /holdings?ticker=IVV` — full holdings (from cache; fetches live on a miss)
- `GET /tickers` — what's cached and how fresh
- `POST /refresh` — trigger a refresh (needs a secret token)

---

## Part 1 — Run it on your own computer first

Do this before deploying. It confirms everything works and lets you see it.

### 1. Install Python 3.12+
Check with `python3 --version`. If missing, install from python.org.

### 2. Get the code into a folder
Put all these files in one folder, open a terminal, and `cd` into it.

### 3. Make a virtual environment (an isolated space for this project's packages)
```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
```
You'll see `(.venv)` appear in your prompt. That means it worked.

### 4. Install the dependencies
```bash
pip install -r requirements.txt
```

### 5. Create your settings file
```bash
cp .env.example .env             # Windows: copy .env.example .env
```
Open `.env` and set `REFRESH_TOKEN` to any long random string. Leave the rest
as-is for now.

### 6. Load the settings and start the server
```bash
# load .env into your shell (mac/Linux):
export $(grep -v '^#' .env | xargs)

uvicorn app:app --reload
```
`--reload` means it restarts automatically when you edit code — handy while
developing.

### 7. Try it
Open your browser to:
- http://127.0.0.1:8000/health  → should show `{"status":"ok",...}`
- http://127.0.0.1:8000/holdings?ticker=IVV  → full holdings for IVV
- http://127.0.0.1:8000/docs  → an auto-generated page where you can click to
  test every endpoint (FastAPI gives you this for free)

> First call to a ticker fetches it live (a few seconds). After that it's cached
> and instant. If a fetch fails with a 403, see Troubleshooting.

Stop the server with `Ctrl+C`.

---

## Part 2 — Put it on the internet (Render)

Render has the gentlest path for a first backend. ~10 minutes.

### 1. Push the code to GitHub
- Make a free GitHub account if you don't have one.
- Create a new **empty** repository.
- In your project folder:
```bash
git init
git add .
git commit -m "ETF holdings backend"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```
(The `.gitignore` already keeps your `.env` and database file out of GitHub —
secrets never get committed.)

### 2. Create the service on Render
- Sign up at render.com (free).
- Click **New +  →  Web Service**.
- Connect your GitHub and pick the repo.
- Render auto-detects the Dockerfile. Confirm these:
  - **Instance type:** *Starter ($7/mo)* for reliable nightly refresh, or *Free*
    to try it (see the scheduling note below).
  - Everything else can stay default.

### 3. Add your environment variables
In the service's **Environment** tab, add the same keys from your `.env`:
- `TRACKED_TICKERS` = `IVV,IJH,IJR,SPY,XLK,XLF`
- `REFRESH_TOKEN` = your long random string
- `ALLOWED_ORIGINS` = your frontend's URL (set to `*` until you have it)
- `REFRESH_HOUR` = `22`, `REFRESH_TZ` = `America/New_York`

### 4. Deploy
Click **Create Web Service**. Render builds and gives you a public URL like
`https://your-app.onrender.com`. Test `.../health` in your browser.

That URL is your `API_BASE` — paste it into the Claude Design frontend config.

---

## The one scheduling caveat (important)

The nightly refresh runs *inside* the web service. That's reliable **only if the
service is always on** (Render Starter, Railway Hobby, or any paid always-on
tier).

On Render's **free** tier, the service sleeps after 15 minutes idle, so the
in-process nightly job may not fire. Two fixes:
1. **Easiest:** use the $7/mo Starter tier — always on, scheduler just works.
2. **Free:** keep the free tier and trigger refresh from *outside* once a day by
   calling `POST /refresh` with your token. A free scheduler like a GitHub
   Actions cron or cron-job.org can hit:
   ```
   POST https://your-app.onrender.com/refresh
   Header:  x-refresh-token: <your REFRESH_TOKEN>
   ```
   On-demand fetching still works regardless — any ticker a user requests that
   isn't cached gets fetched live and cached on the spot.

---

## Wiring Vanguard later

Vanguard funds return a 501 until you connect your existing N-PORT parser. In
`app.py` and `refresh.py`, change `get_holdings(ticker)` to
`get_holdings(ticker, nport_fetch=your_parser)`, where `your_parser(ticker)`
returns `(as_of_date, list_of_Holding)`. Then add Vanguard tickers to
`TRACKED_TICKERS`. They'll be flagged `is_stale=true` automatically.

---

## Troubleshooting

- **A fetch returns 403 / empty:** the issuer blocked the request. The browser
  User-Agent header in `holdings.py` handles this; if it still happens, that
  issuer changed something — check the `# VERIFY` spots in `holdings.py`.
- **iShares ticker "not found in product map":** the ticker→product map needs
  building/refreshing; delete `.ishares_map.json` and retry.
- **Frontend can't call the API (CORS error in browser console):** set
  `ALLOWED_ORIGINS` to your frontend's exact URL and redeploy.
- **Everything is slow on the first request after a while:** free-tier cold
  start. Expected; upgrade to always-on or ignore for a hobby build.
