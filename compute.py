"""
All the math lives here, and only here. populate.py calls this once per
instrument per batch run and stores the result; app.py never recomputes
anything -- it just reads what this module produced.

Keeping this isolated also makes it testable: you can unit-test
`ratio_stats()` against a synthetic price series without touching a network
or a database.
"""
import pandas as pd


def _to_series(rows):
    """rows: list[(date_str, value)] -> pd.Series indexed by date, sorted."""
    if not rows:
        return pd.Series(dtype=float)
    dates, values = zip(*rows)
    s = pd.Series([float(v) for v in values], index=pd.to_datetime(dates))
    return s.sort_index().dropna()


def ratio_stats(asset_rows, gold_rows, resample_freq=None):
    """
    Compute asset/gold ratio stats over the full overlapping history.

    resample_freq: None for daily-vs-daily series (equities, FX, futures).
        Pass "ME" (month-end) when the asset series is a lower-frequency
        macro series (e.g. a monthly/quarterly FRED series) so it can be
        meaningfully intersected with a daily gold series -- both sides get
        resampled to month-end before comparison.

    Returns dict with current_ratio, historical_mean, deviation_pct,
    n_observations, history_start -- or None if there isn't enough overlap
    to be meaningful.

    NOTE on comparability: different instruments have different amounts of
    history (an ETF launched in 2015 vs. a commodity future with 50 years of
    data). The "historical mean" here is only ever computed over each
    instrument's OWN available overlap with gold -- it is not normalized to
    a common start date. That means deviation_pct is internally consistent
    per instrument, but you should not assume two instruments' historical
    means are measuring the same time window. If you want a fair
    cross-instrument comparison, filter this table to instruments with
    similar `history_start` values, or truncate all series to a common
    start date before calling this function.
    """
    asset = _to_series(asset_rows)
    gold = _to_series(gold_rows)
    if asset.empty or gold.empty:
        return None

    if resample_freq:
        asset = asset.resample(resample_freq).last().dropna()
        gold = gold.resample(resample_freq).last().dropna()

    common = asset.index.intersection(gold.index)
    min_obs = 6 if resample_freq else 30  # macro series are inherently sparser
    if len(common) < min_obs:
        return None

    ratio = asset.loc[common] / gold.loc[common]
    ratio = ratio.replace([float("inf"), float("-inf")], pd.NA).dropna()
    if ratio.empty:
        return None

    current_ratio = float(ratio.iloc[-1])
    historical_mean = float(ratio.mean())
    if historical_mean == 0:
        return None

    deviation_pct = (current_ratio - historical_mean) / historical_mean * 100

    return {
        "current_ratio": current_ratio,
        "historical_mean": historical_mean,
        "deviation_pct": deviation_pct,
        "n_observations": int(len(ratio)),
        "history_start": ratio.index.min().strftime("%Y-%m-%d"),
        "last_price": float(asset.iloc[-1]),
    }


def sentiment(price_rows):
    """
    Cheap, dependency-free momentum read: distance from 52-week moving
    average blended with 14-period RSI. This is NOT a substitute for an
    analyst rating -- it's a rough technical-momentum label so the
    dashboard has something to show without hitting an external ratings API
    on every request. Returns (score, label) or (None, None).
    """
    s = _to_series(price_rows)
    if len(s) < 260:  # ~52 weeks of daily data
        return None, None

    ma = s.rolling(252).mean()
    if pd.isna(ma.iloc[-1]):
        return None, None

    pct_from_ma = (s.iloc[-1] - ma.iloc[-1]) / ma.iloc[-1] * 100

    delta = s.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    rsi_val = rsi.iloc[-1] if not pd.isna(rsi.iloc[-1]) else 50

    pct_scaled = max(-30, min(30, pct_from_ma)) / 30 * 70
    rsi_scaled = (rsi_val - 50) / 50 * 30
    score = max(-100, min(100, pct_scaled + rsi_scaled))

    if score > 40:
        label = "Strong Buy"
    elif score > 20:
        label = "Buy"
    elif score > -20:
        label = "Hold"
    elif score > -40:
        label = "Sell"
    else:
        label = "Strong Sell"

    return round(float(score), 1), label
