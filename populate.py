"""
populate.py -- the ONLY part of this project that talks to the internet.

Run this on a schedule (cron, GitHub Actions, Render Cron Job -- see README)
and it will:
  1. Load config/tickers.yaml
  2. For each instrument, fetch price history (incrementally -- only pulls
     dates newer than what's already in the DB, and never anything older
     than `history_start_date` in tickers.yaml, to bound disk usage)
  3. Compute the gold ratio, historical mean, deviation, and momentum label
  4. Write everything to data/market.db

app.py never does any of this live. It only reads the `summary` table.

Usage:
    python populate.py                 # update everything
    python populate.py --full-refresh  # ignore incremental cache, re-fetch all history
    python populate.py --trim          # delete price rows older than history_start_date
                                        # (use once if you tightened the date cap after
                                        # data already existed, to reclaim disk space)
"""
import argparse
import datetime as dt
import sys
import time
from datetime import timezone  # for UTC-aware datetime

import yaml

import db
import compute
from fetchers import stooq_fetcher, yfinance_fetcher, fred_fetcher


def load_config(path="config/tickers.yaml"):
    with open(path) as f:
        return yaml.safe_load(f)


def fetch_price_history(ticker: str, source: str, stooq_kind: str = "equity",
                         full_refresh: bool = False, start_date: str = None):
    """
    Fetch and store price history for one ticker, incrementally. Returns
    True if any new data was written. start_date bounds how far back we ever
    ask for -- see `history_start_date` in tickers.yaml.
    """
    since = None if full_refresh else db.last_price_date(ticker)

    rows = None
    used_source = source

    if source == "stooq":
        symbol = stooq_fetcher.to_stooq_symbol(ticker, stooq_kind)
        rows = stooq_fetcher.fetch(symbol, start_date=start_date)
        if rows is None:
            print(f"    stooq had no data for {ticker} ({symbol}), falling back to yfinance")
            rows = yfinance_fetcher.fetch_history(ticker, start_date=start_date)
            used_source = "yfinance"
    elif source == "yfinance":
        rows = yfinance_fetcher.fetch_history(ticker, start_date=start_date)
    else:
        raise ValueError(f"unknown source: {source}")

    if rows is None:
        print(f"    FAILED to fetch {ticker} from any source")
        return False

    if since and not full_refresh:
        rows = [(d, c) for d, c in rows if d > since]

    n = db.upsert_prices(ticker, rows, used_source)
    print(f"    {ticker}: wrote {n} rows via {used_source}")
    return n > 0 or since is not None


def build_summary_row(key, category, label, ticker, gold_rows, commodity_link=None,
                       currency_link=None, asset_rows=None, resample_freq=None,
                       commodity_series=None, currency_series=None):
    """
    asset_rows: pass explicitly for FRED series (read from the fred_series
    table, not prices). Left as None for anything stored in the prices
    table, in which case it's looked up by ticker automatically.

    commodity_series and currency_series: optional price series for the linked
    commodity/currency. If provided, ratio_stats are computed for asset vs
    commodity and asset vs currency, and stored in new summary columns.
    """
    if asset_rows is None:
        asset_rows = db.get_price_series(ticker)
    stats = compute.ratio_stats(asset_rows, gold_rows, resample_freq=resample_freq)
    if stats is None:
        print(f"    skipping summary for {label}: insufficient overlapping history with gold")
        return
    score, sent_label = compute.sentiment(asset_rows)

    # Compute commodity ratio if commodity_series provided
    commodity_stats = None
    if commodity_series:
        commodity_stats = compute.ratio_stats(asset_rows, commodity_series, resample_freq=resample_freq)

    # Compute currency ratio if currency_series provided
    currency_stats = None
    if currency_series:
        currency_stats = compute.ratio_stats(asset_rows, currency_series, resample_freq=resample_freq)

    db.upsert_summary({
        "key": key,
        "category": category,
        "label": label,
        "ticker": ticker,
        "commodity_link": commodity_link,
        "currency_link": currency_link,
        "last_price": stats["last_price"],
        "current_ratio": stats["current_ratio"],
        "historical_mean": stats["historical_mean"],
        "deviation_pct": stats["deviation_pct"],
        "sentiment_label": sent_label,
        "sentiment_score": score,
        "n_observations": stats["n_observations"],
        "history_start": stats["history_start"],
        "last_updated": dt.datetime.now(timezone.utc).isoformat(),
        # New fields for commodity/currency relative values
        "commodity_ratio": commodity_stats["current_ratio"] if commodity_stats else None,
        "commodity_ratio_mean": commodity_stats["historical_mean"] if commodity_stats else None,
        "commodity_ratio_deviation_pct": commodity_stats["deviation_pct"] if commodity_stats else None,
        "currency_ratio": currency_stats["current_ratio"] if currency_stats else None,
        "currency_ratio_mean": currency_stats["historical_mean"] if currency_stats else None,
        "currency_ratio_deviation_pct": currency_stats["deviation_pct"] if currency_stats else None,
    })


def run(full_refresh: bool = False):
    db.init_db()
    cfg = load_config()
    start_date = cfg.get("history_start_date")  # e.g. "2010-01-01", or None for unbounded

    print("=== Fetching gold (the reference asset for every ratio) ===")
    gold_ticker = cfg["gold_ticker"]
    fetch_price_history(gold_ticker, cfg["gold_source"], full_refresh=full_refresh, start_date=start_date)
    gold_rows = db.get_price_series(gold_ticker)
    if not gold_rows:
        print("FATAL: could not fetch gold price history, aborting.")
        sys.exit(1)

    # --- Build lookup maps for commodity and currency price series by label ---
    # We'll use these later when processing sectors and country_etfs
    commodity_series_by_label = {}
    for c in cfg.get("commodities", []):
        series = db.get_price_series(c["ticker"])
        if series:
            commodity_series_by_label[c["label"]] = series
    currency_series_by_label = {}
    # Currency labels are the country names; we also have the code, but we key by label
    # because the sector config uses the label string (e.g., "WTI Crude Oil")
    for c in cfg.get("currencies", []):
        series = db.get_price_series(c["ticker"])
        if series:
            currency_series_by_label[c["label"]] = series

    print("\n=== Commodities ===")
    for c in cfg.get("commodities", []):
        print(f"  {c['label']} ({c['ticker']})")
        fetch_price_history(c["ticker"], c["source"], full_refresh=full_refresh, start_date=start_date)
        build_summary_row(f"commodity:{c['label']}", "commodity", c["label"], c["ticker"], gold_rows)

    print("\n=== Commodity ETFs ===")
    for c in cfg.get("commodity_etfs", []):
        print(f"  {c['label']} ({c['ticker']})")
        fetch_price_history(c["ticker"], c["source"], c.get("stooq_kind", "equity"),
                             full_refresh, start_date)
        build_summary_row(f"commodity_etf:{c['ticker']}", "commodity_etf", c["label"], c["ticker"], gold_rows)

    print("\n=== Alt assets ===")
    for a in cfg.get("alt_assets", []):
        print(f"  {a['label']} ({a['ticker']})")
        fetch_price_history(a["ticker"], a["source"], full_refresh=full_refresh, start_date=start_date)
        build_summary_row(f"alt_asset:{a['ticker']}", "alt_asset", a["label"], a["ticker"], gold_rows)

    print("\n=== Sectors ===")
    for s in cfg.get("sectors", []):
        print(f"  {s['label']} ({s['ticker']})")
        fetch_price_history(s["ticker"], s["source"], s.get("stooq_kind", "equity"),
                             full_refresh, start_date)
        # batch-fetch fundamentals here (once per run, not per web request)
        fund = yfinance_fetcher.fetch_fundamentals(s["ticker"])
        if fund:
            db.upsert_fundamentals(s["ticker"], fund["name"], fund["pe"], fund["pb"],
                                    fund["rating"], dt.datetime.now(timezone.utc).isoformat())
        # Get commodity and currency series for this sector if they exist
        commodity_series = commodity_series_by_label.get(s.get("commodity")) if s.get("commodity") else None
        currency_series = currency_series_by_label.get(s.get("currency")) if s.get("currency") else None
        build_summary_row(f"sector:{s['ticker']}", "sector", s["label"], s["ticker"],
                           gold_rows, s.get("commodity"), s.get("currency"),
                           commodity_series=commodity_series, currency_series=currency_series)
        time.sleep(0.5)  # be polite to the fundamentals source

    print("\n=== Country ETFs ===")
    for s in cfg.get("country_etfs", []):
        print(f"  {s['label']} ({s['ticker']})")
        fetch_price_history(s["ticker"], s["source"], s.get("stooq_kind", "equity"),
                             full_refresh, start_date)
        commodity_series = commodity_series_by_label.get(s.get("commodity")) if s.get("commodity") else None
        currency_series = currency_series_by_label.get(s.get("currency")) if s.get("currency") else None
        build_summary_row(f"country_etf:{s['ticker']}", "country_etf", s["label"], s["ticker"],
                           gold_rows, s.get("commodity"), s.get("currency"),
                           commodity_series=commodity_series, currency_series=currency_series)

    print("\n=== Currencies ===")
    for c in cfg.get("currencies", []):
        print(f"  {c['label']} ({c['ticker']})")
        fetch_price_history(c["ticker"], c["source"], full_refresh=full_refresh, start_date=start_date)
        build_summary_row(f"currency:{c['code']}", "currency", c["label"], c["ticker"], gold_rows)

    print("\n=== FRED macro series ===")
    for f in cfg.get("fred_series", []):
        print(f"  {f['label']} ({f['series_id']})")
        rows = fred_fetcher.fetch(f["series_id"], observation_start=start_date)
        if rows:
            db.upsert_fred(f["series_id"], rows)
            fred_rows = db.get_fred_series(f["series_id"])
            build_summary_row(f"fred:{f['series_id']}", "fred", f["label"], f["series_id"],
                               gold_rows, asset_rows=fred_rows, resample_freq="ME")
        else:
            print(f"    FAILED to fetch FRED series {f['series_id']}")

    db.set_meta("last_full_run", dt.datetime.now(timezone.utc).isoformat())
    print("\nDone.")


def trim(cutoff_date: str):
    n = db.delete_prices_before(cutoff_date)
    print(f"Deleted {n} price rows older than {cutoff_date}.")
    print("Run `VACUUM` via a sqlite3 shell against data/market.db afterwards "
          "to actually shrink the file on disk (SQLite doesn't reclaim space "
          "automatically after deletes).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--full-refresh", action="store_true",
                         help="ignore incremental cache and re-fetch full history for every ticker")
    parser.add_argument("--trim", action="store_true",
                         help="delete price rows older than history_start_date and exit "
                              "(use once to reclaim disk if you tightened the date cap)")
    args = parser.parse_args()

    if args.trim:
        db.init_db()
        cfg = load_config()
        cutoff = cfg.get("history_start_date")
        if not cutoff:
            print("No history_start_date set in tickers.yaml -- nothing to trim.")
            sys.exit(0)
        trim(cutoff)
    else:
        run(full_refresh=args.full_refresh)
