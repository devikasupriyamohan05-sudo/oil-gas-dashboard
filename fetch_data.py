"""
fetch_data.py — LIVE INGESTION LAYER
====================================
Pulls live oil & gas data from the U.S. EIA Open Data API (https://www.eia.gov/opendata/)
and writes raw CSV files that the rest of the pipeline cleans and serves.

This is the only part of the project that changed conceptually from the whale project:
the whale project downloaded ONE static CSV; here we FETCH FRESH data on every run, so
you can re-run this (or schedule it) to keep the dashboard current.

Two data domains are pulled and combined downstream:
  1. PRICES      — WTI crude, Brent crude, Henry Hub natural gas (daily spot prices)
  2. PRODUCTION  — crude oil field production by U.S. PADD region (monthly)

USAGE
-----
  # 1. Get a free EIA API key (instant): https://www.eia.gov/opendata/register.php
  # 2. Put it in an environment variable (recommended) or a .env file:
  export EIA_API_KEY=your_key_here
  python fetch_data.py

  # No key yet? Generate realistic demo data so you can build/run everything first:
  python fetch_data.py --demo

Outputs (written to ./data/):
  raw_prices.csv       one row per (date, commodity, price)
  raw_production.csv   one row per (date, region, production)
  fetch_meta.json      provenance: when fetched, source, mode (live/demo)
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime, timezone

import requests
import pandas as pd

# --------------------------------------------------------------------------------------
# CONFIG — everything that could change about the EIA API lives here, so if a route or
# series id ever moves you only edit this block. Each entry is one API request.
# --------------------------------------------------------------------------------------

EIA_BASE = "https://api.eia.gov/v2"

# PRICE SERIES: (route, facet_filters, label, unit)
# - petroleum/pri/spt holds crude spot prices (RWTC = WTI Cushing, RBRTE = Brent)
# - natural-gas/pri/fut holds Henry Hub spot (RNGWHHD)
PRICE_REQUESTS = [
    {
        "commodity": "WTI Crude",
        "route": "petroleum/pri/spt",
        "facets": {"series": ["RWTC"]},
        "frequency": "daily",
        "unit": "$/barrel",
    },
    {
        "commodity": "Brent Crude",
        "route": "petroleum/pri/spt",
        "facets": {"series": ["RBRTE"]},
        "frequency": "daily",
        "unit": "$/barrel",
    },
    {
        "commodity": "Henry Hub Natural Gas",
        "route": "natural-gas/pri/fut",
        "facets": {"series": ["RNGWHHD"]},
        "frequency": "daily",
        "unit": "$/MMBtu",
    },
]

# PRODUCTION: crude oil field production by PADD region (monthly).
# duoarea codes: R10..R50 = PADD 1..5 ; NUS = U.S. total.
# Each PADD also gets a representative lat/lon so the dashboard can draw a map.
PADD_REGIONS = {
    "R10": {"name": "PADD 1 — East Coast",      "lat": 39.0,  "lon": -77.0},
    "R20": {"name": "PADD 2 — Midwest",         "lat": 41.5,  "lon": -93.0},
    "R30": {"name": "PADD 3 — Gulf Coast",      "lat": 29.8,  "lon": -95.4},
    "R40": {"name": "PADD 4 — Rocky Mountain",  "lat": 43.0,  "lon": -107.0},
    "R50": {"name": "PADD 5 — West Coast",      "lat": 37.0,  "lon": -120.0},
}

PRODUCTION_REQUEST = {
    "route": "petroleum/crd/crpdn",
    "facets": {"duoarea": list(PADD_REGIONS.keys()), "process": ["FPF"]},  # FPF = field production
    "frequency": "monthly",
    "unit": "thousand barrels",
}

# How far back to pull (keeps payloads small and the demo fast).
START_DATE = "2015-01-01"

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


# --------------------------------------------------------------------------------------
# LIVE FETCH
# --------------------------------------------------------------------------------------

def _eia_get(route, facets, frequency, api_key, start=START_DATE, max_rows=5000):
    """Call one EIA v2 data endpoint and return the list of records."""
    url = f"{EIA_BASE}/{route}/data/"
    params = {
        "api_key": api_key,
        "frequency": frequency,
        "data[0]": "value",
        "start": start,
        "sort[0][column]": "period",
        "sort[0][direction]": "asc",
        "offset": 0,
        "length": max_rows,
    }
    # EIA expects facets in the form facets[<name>][]=<value>
    for fname, fvals in facets.items():
        params[f"facets[{fname}][]"] = fvals

    resp = requests.get(url, params=params, timeout=60)
    resp.raise_for_status()
    payload = resp.json()
    if "response" not in payload:
        raise RuntimeError(f"Unexpected EIA payload for {route}: {json.dumps(payload)[:300]}")
    return payload["response"].get("data", [])


def fetch_live(api_key):
    """Pull prices + production live from EIA. Returns (prices_df, production_df)."""
    print("Fetching LIVE data from EIA API...")

    # ---- Prices ----
    price_rows = []
    for req in PRICE_REQUESTS:
        print(f"  • {req['commodity']} ({req['route']})")
        records = _eia_get(req["route"], req["facets"], req["frequency"], api_key)
        for r in records:
            price_rows.append({
                "date": r.get("period"),
                "commodity": req["commodity"],
                "unit": req["unit"],
                "price": r.get("value"),
            })
        time.sleep(0.3)  # be polite to the API
    prices_df = pd.DataFrame(price_rows)

    # ---- Production by region ----
    print(f"  • Crude production by PADD ({PRODUCTION_REQUEST['route']})")
    prod_records = _eia_get(
        PRODUCTION_REQUEST["route"],
        PRODUCTION_REQUEST["facets"],
        PRODUCTION_REQUEST["frequency"],
        api_key,
    )
    prod_rows = []
    for r in prod_records:
        duoarea = r.get("duoarea")
        region = PADD_REGIONS.get(duoarea, {})
        prod_rows.append({
            "date": r.get("period"),
            "region_code": duoarea,
            "region_name": region.get("name", duoarea),
            "lat": region.get("lat"),
            "lon": region.get("lon"),
            "unit": PRODUCTION_REQUEST["unit"],
            "production": r.get("value"),
        })
    production_df = pd.DataFrame(prod_rows)

    return prices_df, production_df


# --------------------------------------------------------------------------------------
# DEMO FETCH — realistic synthetic data so the whole pipeline runs without an API key.
# Clearly labeled as demo in fetch_meta.json so it is never mistaken for real data.
# --------------------------------------------------------------------------------------

def fetch_demo():
    """Generate plausible-looking prices + production so you can build offline."""
    import numpy as np
    print("Generating DEMO data (no API key required)...")
    rng = np.random.default_rng(42)

    # Daily prices, last ~5 years
    days = pd.date_range(start=START_DATE, end=datetime.now(), freq="D")
    price_rows = []
    bases = {"WTI Crude": (70, 12, "$/barrel"),
             "Brent Crude": (75, 12, "$/barrel"),
             "Henry Hub Natural Gas": (3.2, 1.1, "$/MMBtu")}
    for commodity, (base, vol, unit) in bases.items():
        # random walk with mean reversion
        series = [base]
        for _ in range(1, len(days)):
            nxt = series[-1] + rng.normal(0, vol * 0.03) + (base - series[-1]) * 0.01
            series.append(max(0.5, nxt))
        for d, p in zip(days, series):
            price_rows.append({"date": d.strftime("%Y-%m-%d"), "commodity": commodity,
                               "unit": unit, "price": round(p, 2)})
    prices_df = pd.DataFrame(price_rows)

    # Monthly production by PADD; PADD 3 (Gulf Coast) dominates
    months = pd.date_range(start=START_DATE, end=datetime.now(), freq="MS")
    region_base = {"R10": 1500, "R20": 18000, "R30": 130000, "R40": 22000, "R50": 14000}
    prod_rows = []
    for code, base in region_base.items():
        region = PADD_REGIONS[code]
        level = base
        for m in months:
            level = level * (1 + rng.normal(0.004, 0.02))  # gentle growth + noise
            prod_rows.append({
                "date": m.strftime("%Y-%m"),
                "region_code": code,
                "region_name": region["name"],
                "lat": region["lat"],
                "lon": region["lon"],
                "unit": "thousand barrels",
                "production": round(level, 1),
            })
    production_df = pd.DataFrame(prod_rows)
    return prices_df, production_df


# --------------------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Fetch live oil & gas data from EIA.")
    parser.add_argument("--demo", action="store_true",
                        help="Generate synthetic demo data instead of calling the API.")
    args = parser.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    api_key = os.environ.get("EIA_API_KEY", "").strip()

    mode = "demo"
    if args.demo:
        prices_df, production_df = fetch_demo()
    elif api_key:
        try:
            prices_df, production_df = fetch_live(api_key)
            mode = "live"
        except Exception as e:
            print(f"\n!! Live fetch failed: {e}\n   Falling back to demo data.\n")
            prices_df, production_df = fetch_demo()
    else:
        print("\nNo EIA_API_KEY found in environment.")
        print("Get a free key at https://www.eia.gov/opendata/register.php, then:")
        print("  export EIA_API_KEY=your_key\n")
        print("Generating demo data for now so you can build the rest of the pipeline.\n")
        prices_df, production_df = fetch_demo()

    # Drop rows with no value (mirrors the whale project's NULL handling philosophy)
    prices_df = prices_df.dropna(subset=["price"])
    production_df = production_df.dropna(subset=["production"])

    prices_path = os.path.join(DATA_DIR, "raw_prices.csv")
    prod_path = os.path.join(DATA_DIR, "raw_production.csv")
    prices_df.to_csv(prices_path, index=False)
    production_df.to_csv(prod_path, index=False)

    meta = {
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "source": "U.S. EIA Open Data API v2" if mode == "live" else "synthetic demo data",
        "price_rows": len(prices_df),
        "production_rows": len(production_df),
        "commodities": sorted(prices_df["commodity"].unique().tolist()),
        "regions": sorted(production_df["region_name"].unique().tolist()),
    }
    with open(os.path.join(DATA_DIR, "fetch_meta.json"), "w") as f:
        json.dump(meta, f, indent=2)

    print(f"\nDone ({mode} mode).")
    print(f"  {prices_path}: {len(prices_df):,} rows")
    print(f"  {prod_path}: {len(production_df):,} rows")
    print(f"  Next: python build_db.py")


if __name__ == "__main__":
    main()
