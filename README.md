# Value Desk — cheapness-vs-gold screener

## Architecture (and why it's shaped this way)

```
config/tickers.yaml   <- single source of truth: every instrument tracked
        |
        v
populate.py  ────►  fetchers/stooq_fetcher.py    (primary: equities/ETFs/FX, free, no key)
   (batch job,      fetchers/yfinance_fetcher.py  (fallback + fundamentals only)
   run on a         fetchers/fred_fetcher.py      (macro: housing/farmland/PPI)
   schedule)              |
        |                 v
        └──────────►  compute.py  (ratio / historical mean / deviation / momentum)
                            |
                            v
                       data/market.db  (SQLite)
                            ^
                            | read-only
                            |
                       app.py  (Flask API + serves screener.html)
```

**The core rule: `app.py` never calls Stooq, yfinance, or FRED.** All external
fetching happens in `populate.py`, on a schedule you control. This is the
fix for the original project's main problem — request handlers calling
`yf.Ticker().info` live meant every page load could fail or hang on Yahoo's
rate limits. Now a page load is a SQLite SELECT; it either has data or it
doesn't, and it's fast either way.

## Data sources, and why each one

| Category | Primary source | Fallback | Why |
|---|---|---|---|
| Sector/country ETFs | **Stooq** (`stooq.com/q/d/l/`) | yfinance | Plain CSV endpoint, no key, no login, far more stable than yfinance's scraped `.history()` |
| Currencies (FX) | **Stooq** FX pairs | yfinance `=X` tickers | Same reasoning; yfinance fallback for pairs Stooq doesn't carry |
| Commodity futures (gold, oil, copper...) | **yfinance** | — | Stooq doesn't reliably carry COMEX/NYMEX futures continuations; this is the one place yfinance stays primary |
| Fundamentals (P/E, analyst rating) | **yfinance**, fetched once per batch run | — | Only source for this; the fix is *when* you call it (batch, not per-request), not avoiding it entirely |
| Housing, farmland, commercial RE, PPI/freight | **FRED** | — | Already the right choice in the original build; kept as-is, just moved the API key out of source code |

Stooq symbol quirks (the main gotcha if you add tickers): US equities/ETFs
need a `.us` suffix (`xle.us`), FX pairs have no suffix (`eurusd`), indices
get a `^` prefix. `fetchers/stooq_fetcher.to_stooq_symbol()` handles this —
just set `stooq_kind` correctly in the YAML.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env        # then edit .env with a free FRED API key
export $(cat .env | xargs)  # or use python-dotenv / your host's env vars
python populate.py          # first run: fetches full history for everything in tickers.yaml
python app.py                # serves on :5000
```

## Adding tickers

Everything lives in `config/tickers.yaml`. Add a row under the right
category, set `source` (`stooq` or `yfinance`), and — for sectors/country
ETFs/currencies — optionally link a `commodity` and `currency` for the
convergence screen. Then:

```bash
python populate.py   # incremental: only fetches new data + new tickers
```

No code changes needed for new tickers. Code changes are only needed if you
add a genuinely new *category* (not sector/currency/commodity/country_etf/fred).

## Keeping data fresh in production

`populate.py` is meant to run on a schedule, separately from the web
process:
- **Cron** on any VPS: `0 6 * * * cd /path/to/project && python populate.py`
- **GitHub Actions** (free): a scheduled workflow that runs `populate.py`
  and commits `data/market.db`, or pushes it somewhere the web app can read it
- **Render Cron Job** (free tier available): same idea, separate from the web service

Because fetching is incremental (`db.last_price_date()` per ticker), daily
runs are cheap — you're only pulling the last day or two of data per
instrument, not re-downloading full history.

## Hosting it (and running it from your phone)

**The split that makes this easy:** `populate.py` needs unrestricted internet
access; `app.py` needs none at all (it only reads SQLite). So they run in
two different free places.

### 1. Push this project to a GitHub repo

Can be private. Add your FRED key as a repo secret:
`Settings → Secrets and variables → Actions → New repository secret` →
name it `FRED_API_KEY`.

### 2. Let GitHub Actions keep the data fresh

`.github/workflows/populate.yml` is already set up to run `populate.py`
daily and commit the refreshed `data/market.db` back to the repo. Nothing
to configure beyond step 1 — it starts working the moment the repo exists.
Full outbound internet access, free, no whitelist issues.

**To manually refresh from your phone:** open the GitHub mobile app → your
repo → Actions tab → "Refresh market data" → Run workflow. That's the
refresh button — no custom endpoint needed.

### 3. Host the web app on PythonAnywhere (free tier)

Why PythonAnywhere and not Render/Railway: their free tiers wipe the
filesystem on every sleep/redeploy, which breaks a SQLite-backed app.
PythonAnywhere's free tier has real persistent disk and stays always-on
(no cold-start delay when you open it on your phone) — and since `app.py`
never calls the outside world, PythonAnywhere's free-tier internet
whitelist (which would block Stooq/yfinance/FRED) never comes into play.

1. Create a free account at pythonanywhere.com.
2. Open a **Bash console** and clone your repo:
   ```bash
   git clone https://github.com/yourname/your-repo.git value-desk
   cd value-desk
   mkvirtualenv --python=python3.12 value-desk-env
   pip install -r requirements.txt
   ```
3. Go to the **Web** tab → Add a new web app → Manual configuration →
   Flask → point it at `app.py`, and set the virtualenv to the one you just
   made.
4. **Sync the data.** PythonAnywhere's free tier does not include Scheduled
   Tasks at all (that's a paid-only feature) — so there's no free way to
   auto-run this on a timer. Two options:
   - **Manual (works today, zero setup):** whenever you want fresh data,
     open a Bash console (including from the GitHub/PythonAnywhere mobile
     experience) and run:
     ```bash
     cd ~/mysite && git fetch origin && git reset --hard origin/main
     ```
     **Use `git fetch` + `reset --hard`, not `git pull`.** The Actions
     workflow amends and force-pushes the data commit each day (see "Disk
     budget" below) to avoid growing `.git` forever — a plain `git pull`
     will refuse to apply that as a non-fast-forward change. `reset --hard`
     always makes your local copy match the remote exactly, regardless of
     history rewrites, and is safe to run repeatedly. No app reload needed
     afterwards — `db.py` opens a new SQLite connection per request, so it
     picks up the new file automatically.
   - **Automatic, if you upgrade PythonAnywhere later:** the same command
     as a paid Scheduled Task, run daily a bit after your GitHub Actions
     run completes.
5. Hit the green **Reload** button on the Web tab once, to start the app.

### Disk budget (relevant on a 512MB-quota host)

Two separate levers control `data/market.db` size, and one separate lever
controls how big your git clone gets over time:

- **`history_start_date` in `config/tickers.yaml`** bounds how far back every
  fetch goes. This is the main lever — at ~280 tracked instruments and a
  16-year cap, expect `market.db` to land around 60-90MB.
- **Number of tickers** matters less than you'd think, linearly. Doubling
  the ticker count roughly doubles the DB size for a fixed date range.
- **`.github/workflows/populate.yml` amends and force-pushes** the daily data
  commit instead of creating a new one. Without this, `.git` history would
  grow by roughly one full DB size *per day* forever, since SQLite files
  don't diff well in git — that would blow your quota within weeks
  regardless of how well-curated your ticker list is.

If you ever tighten `history_start_date` after data already exists, run
`python populate.py --trim` once to delete the now-out-of-range rows (note:
SQLite doesn't shrink the file automatically after a delete — you'd want to
run `VACUUM` via a `sqlite3 data/market.db` shell afterwards to actually
reclaim the disk space).

Your dashboard is now live at `https://yourname.pythonanywhere.com`.

### 4. On your phone

- Open that URL in Safari/Chrome, then **Add to Home Screen** — it'll
  behave like a lightweight app icon, no App Store involved.
- Since PythonAnywhere free apps don't sleep, it opens instantly every time.
- For an on-demand refresh instead of waiting for the daily schedule, use
  the GitHub mobile app as described in step 2.

## Known limitations, stated plainly

- **Historical means aren't normalized to a common start date.** An ETF with
  5 years of history and a commodity future with 50 years of history each
  get their "historical mean" computed over their *own* available window.
  Deviation % is internally consistent per instrument but two instruments'
  means may reflect very different eras. If you want strict comparability,
  truncate all series to a common start date before computing ratios
  (see the docstring in `compute.ratio_stats`).
- **"Cheap vs. its own history" is a description, not a causal explanation.**
  A ratio can sit far from its historical mean because of a temporary
  dislocation (good) or a permanent structural shift (not mean-reverting —
  e.g. a commodity that's been permanently displaced by a substitute).
  This tool flags candidates; it doesn't diagnose *why*.
- **The convergence/asymmetric-upside screen's sector→commodity→currency
  links are a manually curated hypothesis** (`commodity`/`currency` fields
  in the YAML), not a statistically fitted relationship. Treat it as a
  starting point for research, not a signal.
- Not investment advice.
