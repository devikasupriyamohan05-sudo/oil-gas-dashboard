"""
build_db.py — CLEAN & TRANSFORM LAYER (the SQL heart of the project)
====================================================================
Mirrors the whale project's approach exactly, just with oil & gas tables:

  RAW CSV  ->  staging tables (everything stored as TEXT, as imported)
           ->  typed "analysis" tables built with INSERT ... SELECT ... GROUP BY
               which in ONE pass:
                 (a) removes duplicate records (GROUP BY the natural key),
                 (b) casts text into proper types (REAL prices, INTEGER-ish dates),
                 (c) uses NULLIF(x,'') before casting so empty strings become real
                     NULLs instead of silently becoming 0,
           ->  a normalized region lookup table (joined by region_code),
           ->  reusable VIEWS for common queries,
           ->  INDEXES for speed,
           ->  VALIDATION queries (completeness, range, duplicate checks).

It also exports the clean tables back to CSV so the Streamlit app can load them
into an in-memory SQLite db (no server, no secrets — same trick as the whale app).

USAGE:  python build_db.py        (run after fetch_data.py)
Outputs: oilgas.db, data/clean_prices.csv, data/clean_production.csv
"""

import os
import sqlite3
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
DB_PATH = os.path.join(HERE, "oilgas.db")


def load_staging(conn):
    """Load raw CSVs into staging tables with ALL columns as TEXT (like a raw import)."""
    raw_prices = pd.read_csv(os.path.join(DATA_DIR, "raw_prices.csv"), dtype=str)
    raw_prod = pd.read_csv(os.path.join(DATA_DIR, "raw_production.csv"), dtype=str)
    raw_prices.to_sql("staging_prices", conn, if_exists="replace", index=False)
    raw_prod.to_sql("staging_production", conn, if_exists="replace", index=False)
    print(f"  staging_prices:     {len(raw_prices):,} rows")
    print(f"  staging_production: {len(raw_prod):,} rows")


def build_analysis_tables(conn):
    """Build typed, de-duplicated analysis tables from staging in single SQL passes."""
    cur = conn.cursor()

    # ---- PRICES: typed + deduped ----
    cur.executescript("""
    DROP TABLE IF EXISTS prices;
    CREATE TABLE prices (
        date       TEXT    NOT NULL,   -- ISO date 'YYYY-MM-DD'
        commodity  TEXT    NOT NULL,
        unit       TEXT,
        price      REAL                -- REAL, with empty strings -> NULL (not 0)
    );

    INSERT INTO prices (date, commodity, unit, price)
    SELECT
        date,
        commodity,
        unit,
        CAST(NULLIF(TRIM(price), '') AS REAL)   -- NULLIF guards against blank -> 0
    FROM staging_prices
    WHERE date IS NOT NULL AND date <> ''
    GROUP BY date, commodity;                   -- dedup on natural key (date+commodity)
    """)

    # ---- PRODUCTION: typed + deduped ----
    cur.executescript("""
    DROP TABLE IF EXISTS production;
    CREATE TABLE production (
        date         TEXT NOT NULL,   -- 'YYYY-MM' (monthly)
        region_code  TEXT NOT NULL,
        unit         TEXT,
        production   REAL
    );

    INSERT INTO production (date, region_code, unit, production)
    SELECT
        date,
        region_code,
        unit,
        CAST(NULLIF(TRIM(production), '') AS REAL)
    FROM staging_production
    WHERE date IS NOT NULL AND date <> ''
    GROUP BY date, region_code;
    """)

    # ---- NORMALIZED region lookup (joined by region_code) ----
    cur.executescript("""
    DROP TABLE IF EXISTS region;
    CREATE TABLE region (
        region_code TEXT PRIMARY KEY,
        region_name TEXT,
        lat         REAL,
        lon         REAL
    );

    INSERT OR IGNORE INTO region (region_code, region_name, lat, lon)
    SELECT
        region_code,
        region_name,
        CAST(NULLIF(TRIM(lat), '') AS REAL),
        CAST(NULLIF(TRIM(lon), '') AS REAL)
    FROM staging_production
    GROUP BY region_code;
    """)
    conn.commit()


def build_views_and_indexes(conn):
    """Reusable views for common queries + indexes for speed (whale-project style)."""
    conn.executescript("""
    -- Production joined to region names/coords (the table the app reads most)
    DROP VIEW IF EXISTS v_production_geo;
    CREATE VIEW v_production_geo AS
    SELECT p.date, p.region_code, r.region_name, r.lat, r.lon,
           p.production, p.unit
    FROM production p
    JOIN region r ON r.region_code = p.region_code;

    -- Latest price per commodity (handy for KPI cards)
    DROP VIEW IF EXISTS v_latest_price;
    CREATE VIEW v_latest_price AS
    SELECT commodity, unit, price, date
    FROM prices
    WHERE (commodity, date) IN (
        SELECT commodity, MAX(date) FROM prices GROUP BY commodity
    );

    -- Monthly average price (so prices line up with monthly production)
    DROP VIEW IF EXISTS v_price_monthly;
    CREATE VIEW v_price_monthly AS
    SELECT substr(date,1,7) AS month, commodity, AVG(price) AS avg_price
    FROM prices
    GROUP BY substr(date,1,7), commodity;

    CREATE INDEX IF NOT EXISTS idx_prices_comm_date ON prices(commodity, date);
    CREATE INDEX IF NOT EXISTS idx_prod_region_date ON production(region_code, date);
    """)
    conn.commit()


def validate(conn):
    """Validation queries: completeness, range sanity, duplicates."""
    cur = conn.cursor()
    print("\nVALIDATION")
    print("-" * 50)

    # Completeness: NULL prices
    null_prices = cur.execute("SELECT COUNT(*) FROM prices WHERE price IS NULL").fetchone()[0]
    total_prices = cur.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    print(f"  prices: {total_prices:,} rows, {null_prices} NULL prices")

    # Range sanity: prices should be positive and not absurd
    bad_prices = cur.execute(
        "SELECT COUNT(*) FROM prices WHERE price IS NOT NULL AND (price < 0 OR price > 1000)"
    ).fetchone()[0]
    print(f"  prices out of plausible range (<0 or >1000): {bad_prices}")

    # Duplicate check (should be 0 after GROUP BY dedup)
    dup_prices = cur.execute("""
        SELECT COUNT(*) FROM (
            SELECT date, commodity, COUNT(*) c FROM prices
            GROUP BY date, commodity HAVING c > 1
        )""").fetchone()[0]
    dup_prod = cur.execute("""
        SELECT COUNT(*) FROM (
            SELECT date, region_code, COUNT(*) c FROM production
            GROUP BY date, region_code HAVING c > 1
        )""").fetchone()[0]
    print(f"  duplicate (date,commodity) groups: {dup_prices}")
    print(f"  duplicate (date,region) groups:    {dup_prod}")

    # Region join completeness
    orphan = cur.execute("""
        SELECT COUNT(*) FROM production p
        LEFT JOIN region r ON r.region_code = p.region_code
        WHERE r.region_code IS NULL""").fetchone()[0]
    print(f"  production rows with no matching region: {orphan}")
    print("-" * 50)


def export_clean_csvs(conn):
    """Export clean tables to CSV for the Streamlit app's in-memory SQLite."""
    prices = pd.read_sql("SELECT * FROM prices ORDER BY date, commodity", conn)
    prod = pd.read_sql("SELECT * FROM v_production_geo ORDER BY date, region_code", conn)
    prices.to_csv(os.path.join(DATA_DIR, "clean_prices.csv"), index=False)
    prod.to_csv(os.path.join(DATA_DIR, "clean_production.csv"), index=False)
    print(f"\nExported clean_prices.csv ({len(prices):,}) and "
          f"clean_production.csv ({len(prod):,})")


def main():
    if not os.path.exists(os.path.join(DATA_DIR, "raw_prices.csv")):
        raise SystemExit("No raw data found. Run `python fetch_data.py` (or `--demo`) first.")

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = sqlite3.connect(DB_PATH)

    print(f"Building {os.path.basename(DB_PATH)} ...")
    load_staging(conn)
    build_analysis_tables(conn)
    build_views_and_indexes(conn)
    validate(conn)
    export_clean_csvs(conn)

    conn.close()
    print("\nDone. Next: streamlit run app.py")


if __name__ == "__main__":
    main()
