# U.S. Oil & Gas Regional Dashboard — Live Data Pipeline

A live-data sibling of the whale-sighting project. Instead of downloading one static CSV,
this pulls **fresh** oil & gas data from the U.S. Energy Information Administration (EIA)
Open Data API every time you run it, cleans and types it in SQL, and serves a regional
dashboard built on pure SQL over real records.

```
fetch_data.py   →  build_db.py   →  app.py
(LIVE ingest)      (clean + SQL)     (Streamlit dashboard)
```

## What it shows

- Live prices: WTI crude, Brent crude, Henry Hub natural gas (daily spot).
- Crude oil production by U.S. PADD region (monthly).
- A map of where production is concentrated, a ranked table, price/production trends,
  and a price-vs-production view with a correlation readout.

## What changed vs. the whale project

The whale project loaded a fixed CSV once. Here, **ingestion is live**: `fetch_data.py`
calls the EIA API on demand (and can be scheduled). Everything downstream — SQL cleaning,
typed analysis tables, views, indexes, the Streamlit app — is the same pattern you already
know. The ingestion layer is the only architectural change.

## Quick start

```bash
pip install -r requirements.txt

# Option A — run immediately with realistic DEMO data (no key needed):
python fetch_data.py --demo
python build_db.py
streamlit run app.py

# Option B — go LIVE with real EIA data:
#   1. Get a free key (instant): https://www.eia.gov/opendata/register.php
#   2. export EIA_API_KEY=your_key_here     (Windows: set EIA_API_KEY=your_key_here)
python fetch_data.py        # pulls live data
python build_db.py
streamlit run app.py
```

If no key is set and you don't pass `--demo`, the script tells you how to get a key and
falls back to demo data so you're never blocked. The dashboard always shows a banner
saying whether it's displaying **live** or **demo** data.

## How the pieces work

**`fetch_data.py` — live ingestion.** Calls EIA v2 endpoints (config block at the top:
`petroleum/pri/spt` for crude prices, `natural-gas/pri/fut` for Henry Hub, and
`petroleum/crd/crpdn` for crude production by PADD region). Writes `data/raw_prices.csv`,
`data/raw_production.csv`, and `data/fetch_meta.json` (provenance: live vs demo, timestamp).

**`build_db.py` — clean + transform in SQL.** Loads the raw CSVs into TEXT-only *staging*
tables, then builds typed *analysis* tables with a single `INSERT … SELECT … GROUP BY` that
(1) dedupes on the natural key, (2) casts to `REAL`, and (3) uses `NULLIF(x,'')` so blanks
become real NULLs instead of 0 — exactly the whale-project cleaning recipe. Adds a
normalized `region` lookup table, reusable views, indexes, and prints validation checks.
Exports `data/clean_prices.csv` and `data/clean_production.csv` for the app.

**`app.py` — Streamlit dashboard.** Loads the clean CSVs into an **in-memory SQLite** db and
answers every question with SQL (the queries are visible in expanders). No secrets at run
time, so it deploys to Streamlit Cloud with nothing to configure.

## Keeping it fresh

Re-run `python fetch_data.py && python build_db.py` whenever you want new data. To automate,
schedule that pair (cron, a GitHub Action, or your task scheduler). See `DEPLOY.md`.

## Tech stack

EIA Open Data API (live source) · Python (requests, pandas) · SQLite · Streamlit ·
GitHub + Streamlit Cloud. Cost: $0.

## A note on the EIA series IDs

If the EIA ever moves a route or series id, you only edit the CONFIG block at the top of
`fetch_data.py` — the rest of the pipeline is unaffected. Current series used:
`RWTC` (WTI), `RBRTE` (Brent), `RNGWHHD` (Henry Hub), and PADD production via `duoarea`
codes `R10`–`R50`.
