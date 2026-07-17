"""
Stooq (stooq.com) -- free, unauthenticated, no API key, no login.

This is the PRIMARY source for bulk historical daily closes on US equities/
ETFs and FX pairs. It's a plain CSV endpoint, not a scraped webpage, which
makes it far more stable in production than yfinance's `.info`/`.history()`
scraping. It does not provide fundamentals (P/E, ratings) or most commodity
futures -- yfinance covers those (see yfinance_fetcher.py).

Symbol conventions (the main gotcha with Stooq):
  US stocks/ETFs : "xle.us"   (lowercase, .us suffix)
  FX pairs       : "eurusd"   (no suffix, no slash)
  Indices        : "^spx"
Full symbol reference: https://stooq.com/db/h/
"""
import io
import time
import requests
import pandas as pd

STOOQ_URL = "https://stooq.com/q/d/l/"


def fetch(symbol: str, start_date: str = None, retries: int = 2, pause: float = 1.0):
    """
    Return list[(date_str, close_float)] sorted ascending, or None on failure.
    start_date: "YYYY-MM-DD" -- if given, only history from this date forward
    is requested (bounds disk usage; see `history_start_date` in tickers.yaml).
    """
    params = {"s": symbol, "i": "d"}
    if start_date:
        params["d1"] = start_date.replace("-", "")
        params["d2"] = ""  # empty = up to today
    for attempt in range(retries + 1):
        try:
            resp = requests.get(STOOQ_URL, params=params, timeout=15)
            resp.raise_for_status()
            text = resp.text
            if "Exceeded" in text or len(text) < 40:
                return None
            df = pd.read_csv(io.StringIO(text))
            if df.empty or "Close" not in df.columns or "Date" not in df.columns:
                return None
            df = df[["Date", "Close"]].dropna()
            return list(df.itertuples(index=False, name=None))
        except Exception:
            if attempt < retries:
                time.sleep(pause)
                continue
            return None
    return None


def to_stooq_symbol(ticker: str, kind: str) -> str:
    """Translate our internal ticker convention into a Stooq symbol.
    kind: 'equity' | 'fx' | 'index'
    """
    if kind == "fx":
        return ticker.replace("=X", "").lower()
    if kind == "index":
        return "^" + ticker.lower().lstrip("^")
    return ticker.lower() + ".us"
