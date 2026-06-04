"""
app.py — STREAMLIT REGIONAL DASHBOARD
=====================================
Same architecture as the whale prediction app:
  - loads the clean CSVs into an IN-MEMORY SQLite database,
  - all numbers come from pure SQL over real fetched records (no AI/API at query time),
  - shows a map, ranked tables, and trend charts,
  - has an expander that prints the exact SQL used, so the logic is transparent.

The user picks a commodity and a region; the dashboard shows live prices, regional
crude production, where production is concentrated (map + ranking), and how prices and
production have moved together over time.

Run locally:   streamlit run app.py
Deploy free:   push to GitHub -> share.streamlit.io  (see DEPLOY.md)
"""

import os
import json
import sqlite3
import pandas as pd
import streamlit as st

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")

st.set_page_config(page_title="Oil & Gas Regional Dashboard", page_icon="🛢️", layout="wide")


# --------------------------------------------------------------------------------------
# DATA LOADING — read clean CSVs into an in-memory SQLite db (cached across reruns)
# --------------------------------------------------------------------------------------
@st.cache_data(ttl=3600)
def load_into_sqlite():
    prices = pd.read_csv(os.path.join(DATA_DIR, "clean_prices.csv"))
    prod = pd.read_csv(os.path.join(DATA_DIR, "clean_production.csv"))
    meta = {}
    meta_path = os.path.join(DATA_DIR, "fetch_meta.json")
    if os.path.exists(meta_path):
        with open(meta_path) as f:
            meta = json.load(f)
    return prices, prod, meta


def query(prices, prod, sql, params=()):
    """Run SQL against a fresh in-memory db built from the two dataframes."""
    conn = sqlite3.connect(":memory:")
    prices.to_sql("prices", conn, index=False)
    prod.to_sql("production", conn, index=False)
    df = pd.read_sql(sql, conn, params=params)
    conn.close()
    return df


# --------------------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------------------
st.title("🛢️ U.S. Oil & Gas Regional Dashboard")

if not os.path.exists(os.path.join(DATA_DIR, "clean_prices.csv")):
    st.error("No data found. Run `python fetch_data.py` then `python build_db.py` first.")
    st.stop()

prices, prod, meta = load_into_sqlite()

# Provenance banner
mode = meta.get("mode", "unknown")
fetched = meta.get("fetched_at_utc", "?")
if mode == "live":
    st.success(f"Live data from {meta.get('source')} · last fetched {fetched}")
else:
    st.warning(f"⚠️ DEMO data (synthetic) · {meta.get('source','')}. "
               f"Add a free EIA API key and re-run `fetch_data.py` to go live.")

# ---- Controls ----
c1, c2 = st.columns(2)
commodities = sorted(prices["commodity"].unique().tolist())
regions = prod[["region_code", "region_name"]].drop_duplicates().sort_values("region_name")
with c1:
    commodity = st.selectbox("Commodity", commodities,
                             index=commodities.index("WTI Crude") if "WTI Crude" in commodities else 0)
with c2:
    region_opts = ["All regions"] + regions["region_name"].tolist()
    region_name = st.selectbox("Production region", region_opts)

# --------------------------------------------------------------------------------------
# KPI ROW — latest price + production from SQL
# --------------------------------------------------------------------------------------
latest_price_sql = """
SELECT commodity, unit, price, date
FROM prices
WHERE commodity = ?
ORDER BY date DESC
LIMIT 1
"""
lp = query(prices, prod, latest_price_sql, (commodity,))

prev_price_sql = "SELECT price FROM prices WHERE commodity = ? ORDER BY date DESC LIMIT 30"
pp = query(prices, prod, prev_price_sql, (commodity,))

k1, k2, k3 = st.columns(3)
if not lp.empty:
    cur_price = lp["price"].iloc[0]
    delta = None
    if len(pp) >= 30:
        delta = f"{cur_price - pp['price'].iloc[-1]:+.2f} vs 30d ago"
    k1.metric(f"Latest {commodity}", f"{cur_price:,.2f} {lp['unit'].iloc[0]}", delta)
    k2.metric("As of", lp["date"].iloc[0])

total_prod_sql = """
SELECT ROUND(SUM(production)/1000.0, 1) AS mmbbl
FROM production
WHERE date = (SELECT MAX(date) FROM production)
"""
tp = query(prices, prod, total_prod_sql)
if not tp.empty and tp["mmbbl"].iloc[0] is not None:
    k3.metric("Latest U.S. crude output (all PADDs)", f"{tp['mmbbl'].iloc[0]:,.1f} MMbbl/mo")

st.divider()

# --------------------------------------------------------------------------------------
# MAP + REGIONAL RANKING — where production is concentrated (latest month)
# --------------------------------------------------------------------------------------
st.subheader("Where production is concentrated")

map_sql = """
SELECT region_name, lat, lon, production
FROM production
WHERE date = (SELECT MAX(date) FROM production)
  AND production IS NOT NULL
ORDER BY production DESC
"""
map_df = query(prices, prod, map_sql)

mc1, mc2 = st.columns([3, 2])
with mc1:
    if not map_df.empty:
        # scale dot size by production for a quick density read
        plot_df = map_df.rename(columns={"lat": "latitude", "lon": "longitude"}).copy()
        st.map(plot_df, size="production", latitude="latitude", longitude="longitude")
with mc2:
    st.markdown("**Latest-month crude production by region**")
    st.dataframe(
        map_df[["region_name", "production"]].rename(
            columns={"region_name": "Region", "production": "Thousand bbl"}),
        hide_index=True, use_container_width=True)

with st.expander("SQL used for the map ranking"):
    st.code(map_sql, language="sql")

st.divider()

# --------------------------------------------------------------------------------------
# TRENDS — price over time + production over time (filtered by region if chosen)
# --------------------------------------------------------------------------------------
st.subheader("Trends over time")

t1, t2 = st.columns(2)
with t1:
    st.markdown(f"**{commodity} price**")
    price_trend = query(prices, prod,
                        "SELECT date, price FROM prices WHERE commodity = ? ORDER BY date",
                        (commodity,))
    price_trend["date"] = pd.to_datetime(price_trend["date"])
    st.line_chart(price_trend.set_index("date")["price"])

with t2:
    if region_name == "All regions":
        st.markdown("**Crude production — U.S. total (all PADDs)**")
        prod_trend = query(prices, prod, """
            SELECT date, SUM(production) AS production
            FROM production GROUP BY date ORDER BY date""")
    else:
        st.markdown(f"**Crude production — {region_name}**")
        prod_trend = query(prices, prod, """
            SELECT date, production FROM production
            WHERE region_name = ? ORDER BY date""", (region_name,))
    prod_trend["date"] = pd.to_datetime(prod_trend["date"])
    st.line_chart(prod_trend.set_index("date")["production"])

# --------------------------------------------------------------------------------------
# PRICE vs PRODUCTION — the "combine" insight: monthly avg price next to production
# --------------------------------------------------------------------------------------
st.divider()
st.subheader("Price vs. production (monthly)")

combo_sql = """
WITH pm AS (
    SELECT substr(date,1,7) AS month, AVG(price) AS avg_price
    FROM prices WHERE commodity = ? GROUP BY substr(date,1,7)
),
pr AS (
    SELECT substr(date,1,7) AS month, SUM(production) AS production
    FROM production GROUP BY substr(date,1,7)
)
SELECT pm.month, pm.avg_price, pr.production
FROM pm JOIN pr ON pr.month = pm.month
ORDER BY pm.month
"""
combo = query(prices, prod, combo_sql, (commodity,))
if not combo.empty:
    combo["month"] = pd.to_datetime(combo["month"])
    combo = combo.set_index("month")
    cc1, cc2 = st.columns(2)
    cc1.line_chart(combo["avg_price"], use_container_width=True)
    cc1.caption(f"Monthly avg {commodity} price")
    cc2.line_chart(combo["production"], use_container_width=True)
    cc2.caption("Monthly U.S. crude production (all PADDs)")

    corr = combo["avg_price"].corr(combo["production"])
    st.info(f"Correlation between monthly {commodity} price and U.S. crude production "
            f"over the available history: **{corr:+.2f}** "
            f"(near 0 = little linear relationship; ±1 = strong).")

with st.expander("SQL used for price vs. production"):
    st.code(combo_sql, language="sql")

st.caption("All figures computed live with SQL over fetched EIA records. "
           "Re-run fetch_data.py + build_db.py (or schedule it) to refresh.")
