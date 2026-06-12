"""
FINX Data Fetcher
=================
Pulls 5 years of daily price data and (where possible) fundamentals for PSX tickers
from psxdata and yfinance, with graceful fallback. Writes to data/prices/ and
data/fundamentals/ in the canonical schema.

Usage
-----
    python data_fetcher.py --tickers ATBA BWHL GTYR --years 5
    python data_fetcher.py --sector "Cement"
    python data_fetcher.py --all          # fetch every ticker in sectors.json
    python data_fetcher.py --fundamentals BWHL GTYR  # try fundamentals scrape only

The script NEVER fabricates data. If both sources fail for a ticker, it writes
an empty template CSV with the correct schema so you can fill it manually
from the company's annual report.
"""

from __future__ import annotations
import argparse
import json
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd
import numpy as np

# ---- Optional data sources ------------------------------------------------
try:
    from psx import stocks as psx_stocks            # psx-data-reader pkg
    PSX_AVAILABLE = True
except ImportError:
    try:
        import psxdata as psx_alt
        PSX_AVAILABLE = "psxdata"
    except ImportError:
        PSX_AVAILABLE = False

try:
    import yfinance as yf
    YF_AVAILABLE = True
except ImportError:
    YF_AVAILABLE = False

ROOT = Path(__file__).parent
PRICES_DIR = ROOT / "data" / "prices"
FUND_DIR   = ROOT / "data" / "fundamentals"
SECTORS_JSON = ROOT / "data" / "sectors.json"

PRICES_DIR.mkdir(parents=True, exist_ok=True)
FUND_DIR.mkdir(parents=True, exist_ok=True)

# Canonical schemas
PRICE_COLS = ["DATE", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME", "CHANGE"]
FUND_METRICS = [
    "Total Assets",
    "Total Liabilities",
    "Total Equity",
    "Total Shares Outstanding (Millions)",
    "EBIT (Operating Profit)",
    "EBT (Profit Before Tax)",
    "Net Income",
    "Operating Cash Flow",
    "EPS (PKR)",
]


# ---------------------------------------------------------------------------
# Price fetching
# ---------------------------------------------------------------------------
def fetch_prices_psx(ticker: str, years: int) -> pd.DataFrame | None:
    """Try the psx-data-reader package (most reliable PSX scraper)."""
    if PSX_AVAILABLE is False:
        return None
    try:
        end = datetime.today()
        start = end - timedelta(days=365 * years + 30)
        if PSX_AVAILABLE == "psxdata":
            df = psx_alt.stocks(ticker, start=start.strftime("%Y-%m-%d"),
                                 end=end.strftime("%Y-%m-%d"))
        else:
            df = psx_stocks(ticker, start=start, end=end)
        if df is None or df.empty:
            return None
        df = df.reset_index()
        df.columns = [c.upper().strip() for c in df.columns]
        # Normalize column names
        rename = {"TIME": "DATE", "DATETIME": "DATE"}
        df = df.rename(columns=rename)
        for c in ["OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"]:
            if c not in df.columns:
                return None
        df["DATE"] = pd.to_datetime(df["DATE"]).dt.strftime("%d-%b-%Y")
        df["CHANGE"] = df["CLOSE"].diff().round(2)
        return df[PRICE_COLS].dropna(subset=["CLOSE"])
    except Exception as e:
        print(f"  [psx] {ticker}: {e}")
        return None


def fetch_prices_yf(ticker: str, years: int) -> pd.DataFrame | None:
    """Yahoo Finance fallback — works for major PSX tickers with .KA suffix."""
    if not YF_AVAILABLE:
        return None
    try:
        df = yf.download(f"{ticker}.KA", period=f"{years}y",
                         interval="1d", progress=False, auto_adjust=False)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        df = df.reset_index()
        df.columns = [str(c).upper().strip() for c in df.columns]
        df = df.rename(columns={"ADJ CLOSE": "ADJCLOSE"})
        df["DATE"] = pd.to_datetime(df["DATE"]).dt.strftime("%d-%b-%Y")
        df["CHANGE"] = df["CLOSE"].diff().round(2)
        return df[PRICE_COLS].dropna(subset=["CLOSE"])
    except Exception as e:
        print(f"  [yf]  {ticker}: {e}")
        return None


def fetch_prices(ticker: str, years: int = 5) -> bool:
    """Try every source for prices. Returns True if a CSV was written."""
    out_path = PRICES_DIR / f"{ticker}_daily.csv"
    print(f"[prices] {ticker} ({years}y)...")

    df = fetch_prices_psx(ticker, years)
    src = "psx"
    if df is None or df.empty:
        df = fetch_prices_yf(ticker, years)
        src = "yfinance"

    if df is not None and not df.empty:
        # Format VOLUME with commas like your existing CSVs
        df = df.copy()
        df["VOLUME"] = df["VOLUME"].apply(
            lambda v: f"{int(v):,}" if pd.notna(v) and v == v else ""
        )
        df.to_csv(out_path, index=False)
        print(f"  -> wrote {len(df)} rows from {src}  ->  {out_path.name}")
        return True

    print(f"  -> FAILED: no source returned data for {ticker}")
    return False


# ---------------------------------------------------------------------------
# Fundamentals fetching (best-effort — most PSX small caps will need manual fill)
# ---------------------------------------------------------------------------
def empty_fundamentals_template(ticker: str) -> pd.DataFrame:
    """A blank template in your canonical schema — fill from the annual report."""
    current_year = datetime.now().year
    years = list(range(current_year - 5, current_year))
    df = pd.DataFrame({"Metric (in PKR Millions)": FUND_METRICS})
    for y in years:
        df[str(y)] = ""
    return df


def fetch_fundamentals_yf(ticker: str) -> pd.DataFrame | None:
    """Pull what yfinance has. Coverage is sparse for PSX smallcaps."""
    if not YF_AVAILABLE:
        return None
    try:
        tk = yf.Ticker(f"{ticker}.KA")
        bs = tk.balance_sheet         # balance sheet
        is_ = tk.financials           # income statement
        cf = tk.cashflow              # cash flow
        if bs is None or bs.empty:
            return None

        years = sorted([c.year for c in bs.columns])[-5:]
        out = pd.DataFrame({"Metric (in PKR Millions)": FUND_METRICS})

        def grab(frame, *candidates):
            for cand in candidates:
                for idx in frame.index:
                    if cand.lower() in str(idx).lower():
                        return frame.loc[idx]
            return None

        ta = grab(bs, "Total Assets")
        tl = grab(bs, "Total Liab")
        te = grab(bs, "Total Stockholder Equity", "Stockholders Equity", "Total Equity")
        ni = grab(is_, "Net Income")
        ebit = grab(is_, "EBIT", "Operating Income")
        ebt  = grab(is_, "Income Before Tax", "Pretax")
        ocf  = grab(cf, "Total Cash From Operating", "Operating Cash Flow")
        sh   = tk.info.get("sharesOutstanding")

        for y in years:
            col = str(y)
            ts = pd.Timestamp(year=y, month=12, day=31)
            def near(series):
                if series is None: return ""
                # nearest date in series to ts
                try:
                    nearest = series.index[abs(series.index - ts).argmin()]
                    val = series.loc[nearest]
                    return round(float(val) / 1e6, 2) if pd.notna(val) else ""
                except Exception:
                    return ""

            out.loc[out["Metric (in PKR Millions)"] == "Total Assets", col]      = near(ta)
            out.loc[out["Metric (in PKR Millions)"] == "Total Liabilities", col] = near(tl)
            out.loc[out["Metric (in PKR Millions)"] == "Total Equity", col]      = near(te)
            out.loc[out["Metric (in PKR Millions)"] == "Net Income", col]        = near(ni)
            out.loc[out["Metric (in PKR Millions)"] == "EBIT (Operating Profit)", col] = near(ebit)
            out.loc[out["Metric (in PKR Millions)"] == "EBT (Profit Before Tax)", col] = near(ebt)
            out.loc[out["Metric (in PKR Millions)"] == "Operating Cash Flow", col]     = near(ocf)
            if sh:
                out.loc[out["Metric (in PKR Millions)"] == "Total Shares Outstanding (Millions)", col] = round(sh / 1e6, 2)
        return out
    except Exception as e:
        print(f"  [yf-fund] {ticker}: {e}")
        return None


def fetch_fundamentals(ticker: str, force_template: bool = False) -> bool:
    """Try sources, otherwise drop a blank template for manual fill."""
    out_path = FUND_DIR / f"{ticker}_fundamentals.csv"
    if out_path.exists() and not force_template:
        print(f"[fund] {ticker}: already exists, skipping (delete file to refetch)")
        return True

    print(f"[fund] {ticker}...")
    df = None if force_template else fetch_fundamentals_yf(ticker)

    if df is not None and not df.empty:
        df.to_csv(out_path, index=False)
        print(f"  -> wrote scraped fundamentals -> {out_path.name}")
        return True

    df = empty_fundamentals_template(ticker)
    df.to_csv(out_path, index=False)
    print(f"  -> wrote BLANK TEMPLATE -> {out_path.name}  (fill from annual report)")
    return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def load_sectors() -> dict:
    with open(SECTORS_JSON) as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser(description="FINX data fetcher (PSX prices + fundamentals)")
    ap.add_argument("--tickers", nargs="*", help="Explicit ticker symbols (e.g. ATBA BWHL)")
    ap.add_argument("--sector", help="Fetch all tickers in this sector (from sectors.json)")
    ap.add_argument("--all", action="store_true", help="Fetch every ticker in sectors.json")
    ap.add_argument("--years", type=int, default=5, help="Years of price history (default 5)")
    ap.add_argument("--prices-only", action="store_true")
    ap.add_argument("--fundamentals-only", action="store_true")
    ap.add_argument("--force-template", action="store_true",
                    help="Always write blank fundamentals template (skip scrape)")
    args = ap.parse_args()

    tickers: list[str] = []
    if args.tickers:
        tickers.extend([t.upper() for t in args.tickers])
    if args.sector:
        s = load_sectors()
        if args.sector not in s["sectors"]:
            print(f"Sector '{args.sector}' not found. Available: {list(s['sectors'].keys())}")
            sys.exit(1)
        tickers.extend(s["sectors"][args.sector])
    if args.all:
        s = load_sectors()
        for syms in s["sectors"].values():
            tickers.extend(syms)
        tickers.extend(s["_meta"]["benchmark_tickers"])
        tickers.append(s["_meta"]["market_index"])

    if not tickers:
        ap.print_help()
        sys.exit(1)

    tickers = list(dict.fromkeys(tickers))  # de-dup, preserve order
    print(f"\nFetching for {len(tickers)} tickers: {', '.join(tickers)}")
    print(f"Sources available: psx={PSX_AVAILABLE} yfinance={YF_AVAILABLE}\n")

    price_ok = price_fail = fund_ok = fund_fail = 0
    for t in tickers:
        if not args.fundamentals_only:
            (fetch_prices(t, args.years) and (price_ok := price_ok + 1)) \
                or (price_fail := price_fail + 1)
        if not args.prices_only:
            (fetch_fundamentals(t, args.force_template) and (fund_ok := fund_ok + 1)) \
                or (fund_fail := fund_fail + 1)
        time.sleep(0.5)  # be polite to PSX servers

    print(f"\n=== Summary ===")
    print(f"Prices:        {price_ok} ok, {price_fail} failed")
    print(f"Fundamentals:  {fund_ok} scraped, {fund_fail} blank templates")
    print(f"\nTip: blank templates need to be filled from each company's annual report (PSX.com.pk).")


if __name__ == "__main__":
    main()
