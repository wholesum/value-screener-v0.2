"""
FRED (Federal Reserve Economic Data) -- free, requires an API key (instant,
no cost: https://fred.stlouisfed.org/docs/api/api_key.html).

This is the best free source for national housing indices (Case-Shiller),
farmland values, commercial real estate price indices, and PPI series. It
worked well in the original build; the only change here is pulling the key
out of source code and into an environment variable, and supporting
incremental fetches (observation_start) so re-runs don't re-download 50
years of history every time.
"""
import os
import pandas as pd
import requests

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"


def fetch(series_id: str, observation_start: str = None):
    """Return list[(date_str, value_float)] or None on failure."""
    if not FRED_API_KEY:
        raise RuntimeError(
            "FRED_API_KEY environment variable is not set. "
            "Get a free key at https://fred.stlouisfed.org/docs/api/api_key.html"
        )
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "asc",
        "units": "lin",
    }
    if observation_start:
        params["observation_start"] = observation_start
    try:
        resp = requests.get(FRED_BASE_URL, params=params, timeout=30)
        if resp.status_code != 200:
            return None
        observations = resp.json().get("observations", [])
        out = []
        for obs in observations:
            val = obs.get("value")
            if val in (None, "."):
                continue
            try:
                out.append((obs["date"], float(val)))
            except (TypeError, ValueError):
                continue
        return out or None
    except Exception:
        return None
