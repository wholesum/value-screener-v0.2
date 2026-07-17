"""
yfinance -- unofficial Yahoo Finance client.

Kept for two things Stooq can't do:
  1. Commodity futures history (GC=F, CL=F, HG=F, ...) and some FX crosses
  2. Point-in-time fundamentals (trailing P/E, price/book, analyst rating)

IMPORTANT: this is the fragile, rate-limited half of the stack -- `.info` and
`.funds_data` are scraped, not a real API, and Yahoo throttles datacenter IPs
aggressively. That's almost certainly what was breaking the original app.

The fix is architectural, not a library swap: this module is ONLY ever
imported by populate.py (a batch job you run on a schedule, e.g. daily via
cron or GitHub Actions). It is never imported by app.py. A live Flask
request should never wait on Yahoo.
"""
import time
import pandas as pd
import yfinance as yf

try:
    from curl_cffi import requests as curl_requests
    _SESSION = curl_requests.Session(impersonate="chrome")
except Exception:
    _SESSION = None


def fetch_history(ticker: str, period: str = "max", retries: int = 2, pause: float = 2.0):
    for attempt in range(retries + 1):
        try:
            kwargs = dict(period=period, interval="1d", progress=False, timeout=30)
            if _SESSION:
                kwargs["session"] = _SESSION
            data = yf.download(ticker, **kwargs)
            if data is None or data.empty:
                raise ValueError("empty response")
            close = data["Close"]
            if isinstance(close, pd.DataFrame):
                close = close.iloc[:, 0]
            close = close.dropna()
            if close.empty:
                raise ValueError("no closes")
            return list(zip(close.index.strftime("%Y-%m-%d"), close.values.tolist()))
        except Exception:
            if attempt < retries:
                time.sleep(pause)
                continue
            return None
    return None


def fetch_fundamentals(ticker: str):
    try:
        kwargs = {}
        if _SESSION:
            kwargs["session"] = _SESSION
        t = yf.Ticker(ticker, **kwargs)
        info = t.info
        if not info:
            return None
        return {
            "name": info.get("shortName", ticker),
            "pe": info.get("trailingPE") or info.get("forwardPE"),
            "pb": info.get("priceToBook"),
            "rating": info.get("recommendationKey", "hold"),
        }
    except Exception:
        return None
