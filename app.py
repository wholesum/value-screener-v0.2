"""
app.py -- the web layer. Deliberately dumb: every endpoint is a SELECT
against data/market.db, populated ahead of time by populate.py. Nothing in
here calls Stooq, yfinance, or FRED. That's what makes this fast and
resistant to the rate-limit/timeout problems the original had -- a page
load can't fail because Yahoo Finance is throttling you.

Run populate.py on a schedule (cron, GitHub Actions, Render Cron Job -- see
README) to keep data/market.db fresh; this process just serves it.
"""
import yaml
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

import db

app = Flask(__name__, static_folder=".")
CORS(app)


@app.route("/")
def root():
    return send_from_directory(".", "screener.html")


def load_config(path="config/tickers.yaml"):
    with open(path) as f:
        return yaml.safe_load(f)


@app.route("/api/dashboard")
def dashboard():
    """Everything the main screen needs, grouped by category. Pure DB read."""
    try:
        sectors = db.get_summary("sector") + db.get_summary("country_etf")
        currencies = db.get_summary("currency")
        commodities = db.get_summary("commodity") + db.get_summary("commodity_etf")
        alt_assets = db.get_summary("alt_asset")
        macro = db.get_summary("fred")
        last_run = db.get_meta("last_full_run")
        return jsonify({
            "sectors": sectors,
            "currencies": currencies,
            "commodities": commodities,
            "alt_assets": alt_assets,
            "macro": macro,
            "last_updated": last_run,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/convergence")
def convergence():
    """
    The 'asymmetric upside' screen: for each sector/country ETF, pull in the
    deviation_pct of the commodity and currency it's linked to (via
    config/tickers.yaml) and average them. All inputs are precomputed
    summary rows -- this is just arithmetic, no external calls, so it's
    safe to compute per-request.

    Additionally, we now include the precomputed sector-vs-commodity and
    sector-vs-currency deviations (stored in the sector's summary row) for
    richer analysis.
    """
    try:
        cfg = load_config()
        commodity_rows = {r["label"]: r for r in db.get_summary("commodity")}
        # currency_rows keyed by label; also index by code for convenience
        currency_by_code = {}
        for c in cfg.get("currencies", []):
            row = next((r for r in db.get_summary("currency") if r["key"] == f"currency:{c['code']}"), None)
            if row:
                currency_by_code[c["code"]] = row

        results = []
        for group_key in ("sector", "country_etf"):
            group_cfg = cfg.get("sectors" if group_key == "sector" else "country_etfs", [])
            for entry in group_cfg:
                sec_row = db.get_summary_by_key(f"{group_key}:{entry['ticker']}")
                if not sec_row or sec_row["deviation_pct"] is None:
                    continue
                vals = [sec_row["deviation_pct"]]
                commodity_dev = None
                currency_dev = None
                if entry.get("commodity"):
                    crow = commodity_rows.get(entry["commodity"])
                    if crow and crow["deviation_pct"] is not None:
                        commodity_dev = crow["deviation_pct"]
                        vals.append(commodity_dev)
                if entry.get("currency"):
                    crow = currency_by_code.get(entry["currency"])
                    if crow and crow["deviation_pct"] is not None:
                        currency_dev = crow["deviation_pct"]
                        vals.append(currency_dev)
                results.append({
                    "label": entry["label"],
                    "ticker": entry["ticker"],
                    "category": group_key,
                    "sector_deviation_pct": sec_row["deviation_pct"],
                    "commodity_link": entry.get("commodity"),
                    "commodity_deviation_pct": commodity_dev,
                    "currency_link": entry.get("currency"),
                    "currency_deviation_pct": currency_dev,
                    "avg_deviation_pct": sum(vals) / len(vals),
                    "n_factors": len(vals),
                    "sentiment_label": sec_row["sentiment_label"],
                    # New fields: sector vs commodity and sector vs currency
                    "sector_commodity_ratio_deviation": sec_row.get("commodity_ratio_deviation_pct"),
                    "sector_currency_ratio_deviation": sec_row.get("currency_ratio_deviation_pct"),
                })
        results.sort(key=lambda r: r["avg_deviation_pct"])
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/history/<path:key>")
def history(key):
    """
    Full price history for one instrument, for charting. `key` is the
    summary table key, e.g. 'sector:XLE' or 'commodity:Gold'.
    """
    row = db.get_summary_by_key(key)
    if not row:
        return jsonify({"error": "not found"}), 404
    if row["category"] == "fred":
        series = db.get_fred_series(row["ticker"])
    else:
        series = db.get_price_series(row["ticker"])
    return jsonify({
        "key": key,
        "label": row["label"],
        "dates": [d for d, _ in series],
        "values": [v for _, v in series],
    })


@app.route("/api/search")
def search():
    q = request.args.get("q", "").strip().upper()
    if not q:
        return jsonify({"error": "query required"}), 400
    all_rows = db.get_summary()
    matches = [r for r in all_rows if q in r["label"].upper() or (r["ticker"] and q in r["ticker"].upper())]
    return jsonify(matches)


if __name__ == "__main__":
    db.init_db()
    app.run(debug=True, port=5000)
