"""
FINX Portfolio Intelligence System
===================================
Sector-agnostic Flask backend implementing CEP ES-447 requirements:

  Phase 1  Fundamental screening (Assignments 1+2): BVPS, EPS, ROE, ROA, EBIT, EBT
  Phase 2  Lifecycle classification (Introduction/Growth/Maturity/Decline)
  Phase 3  Hybrid price models (Assignment 3):
              Model 1: P_hat = P + λ₁(EMA5−EMA20) + λ₂·ROC + λ₃(V/V̄₁₀)
              Model 2: Signal = 0.6·Trend + 0.4·sign(M)  (SuperTrend)
  Phase 4  CAPM (β, expected return, α, valuation status)
  Phase 5  MPT with Ledoit-Wolf shrinkage, no short-selling, max-Sharpe optimization

Data is loaded entirely from local CSVs under data/. Adding a new sector means
adding entries to data/sectors.json and dropping CSVs in data/prices/ and
data/fundamentals/.  Run data_fetcher.py to auto-populate prices.
"""

from __future__ import annotations
import json
import os
from pathlib import Path
from functools import lru_cache

import numpy as np
import pandas as pd
from flask import Flask, render_template, request, jsonify
from scipy.optimize import minimize

import warnings
warnings.filterwarnings("ignore")

ROOT = Path(__file__).parent
DATA = ROOT / "data"
PRICES_DIR = DATA / "prices"
FUND_DIR   = DATA / "fundamentals"
SECTORS_JSON = DATA / "sectors.json"

app = Flask(__name__)


# ===========================================================================
# CONFIG LOADING
# ===========================================================================
def load_config():
    with open(SECTORS_JSON) as f:
        return json.load(f)


def list_available_tickers() -> set[str]:
    """All tickers we have at least price data for."""
    return {p.stem.replace("_daily", "") for p in PRICES_DIR.glob("*_daily.csv")}


# ===========================================================================
# DATA LOADERS  (cached because CSVs don't change between requests)
# ===========================================================================
@lru_cache(maxsize=64)
def load_prices(ticker: str) -> pd.DataFrame | None:
    """Read a daily-price CSV. Returns a DataFrame indexed by date with OHLCV."""
    path = PRICES_DIR / f"{ticker}_daily.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path)
    df.columns = [str(c).upper().strip() for c in df.columns]

    # Tolerate CSVs that ship with an empty header row (header on row 2)
    if "DATE" not in df.columns:
        df = pd.read_csv(path, skiprows=1)
        df.columns = [str(c).upper().strip() for c in df.columns]
    if "DATE" not in df.columns:
        # last resort: assume PSX column order
        df.columns = (["DATE", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME", "CHANGE"]
                      + list(df.columns[7:]))[:len(df.columns)]

    # Parse DATE — PSX format is 'dd-Mon-YYYY'. Try strict first, fall back to inference.
    parsed = pd.to_datetime(df["DATE"], format="%d-%b-%Y", errors="coerce")
    if parsed.isna().sum() > len(df) * 0.5:
        parsed = pd.to_datetime(df["DATE"], errors="coerce", dayfirst=False)
    df["DATE"] = parsed
    df = df.dropna(subset=["DATE"]).sort_values("DATE").reset_index(drop=True)

    # Clean comma-thousands in any string columns and coerce numerics
    for col in ["OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"]:
        if col not in df.columns:
            continue
        # Always coerce to str first (handles object dtype, StringDtype, and mixed)
        df[col] = df[col].astype(str).str.replace(",", "", regex=False).str.strip()
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["CLOSE"]).set_index("DATE")
    return df


@lru_cache(maxsize=64)
def load_fundamentals(ticker: str) -> pd.DataFrame | None:
    """Read a fundamentals CSV. Returns DataFrame with years as index, metrics as columns."""
    path = FUND_DIR / f"{ticker}_fundamentals.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path)
    if df.empty or df.shape[1] < 2:
        return None
    metric_col = df.columns[0]
    year_cols = [c for c in df.columns[1:] if str(c).strip().isdigit()]
    if not year_cols:
        return None

    # Clean each value: strip commas, handle parens as negatives, blank → NaN
    def clean(v):
        if pd.isna(v):
            return np.nan
        s = str(v).strip().replace(",", "")
        if s in ("", "-", "—"):
            return np.nan
        neg = s.startswith("(") and s.endswith(")")
        if neg:
            s = s[1:-1]
        try:
            return -float(s) if neg else float(s)
        except ValueError:
            return np.nan

    long = df.melt(id_vars=[metric_col], value_vars=year_cols,
                   var_name="Year", value_name="Value")
    long["Value"] = long["Value"].apply(clean)
    wide = long.pivot(index="Year", columns=metric_col, values="Value")
    wide.index = wide.index.astype(int)
    wide = wide.sort_index()
    return wide


# ===========================================================================
# PHASE 1 — FUNDAMENTAL METRICS  (Assignment 1 + 2)
# ===========================================================================
def compute_ratios(fund: pd.DataFrame) -> pd.DataFrame:
    """Adds BVPS, EPS, ROE, ROA to the fundamentals frame."""
    df = fund.copy()

    def col(*keys):
        """Find the first column whose name contains any of the keys (case-insensitive)."""
        for k in keys:
            for c in df.columns:
                if k.lower() in str(c).lower():
                    return c
        return None

    ni    = col("Net Income")
    te    = col("Total Equity")
    ta    = col("Total Assets")
    shares= col("Shares Outstanding")
    eps_c = col("EPS")
    ebit_c= col("EBIT")
    ebt_c = col("EBT")
    ocf_c = col("Operating Cash Flow")

    if te and shares:
        df["BVPS"] = df[te] / df[shares]
    if ni and te:
        df["ROE"] = (df[ni] / df[te]) * 100
    if ni and ta:
        df["ROA"] = (df[ni] / df[ta]) * 100
    if eps_c:  df["EPS"]  = df[eps_c]
    if ebit_c: df["EBIT"] = df[ebit_c]
    if ebt_c:  df["EBT"]  = df[ebt_c]
    if ni:     df["NetIncome"] = df[ni]
    if ocf_c:  df["OCF"]  = df[ocf_c]
    return df


def lifecycle_stage(metrics: pd.DataFrame) -> tuple[str, str]:
    """
    Classify lifecycle stage based on trends in revenue/income, OCF, ICF (proxied),
    per CEP rubric guidance.

    Rules:
      Decline:      OCF negative across most recent 2 years OR Net Income trending down sharply
      Introduction: positive but very small/erratic Net Income, OCF still negative
      Growth:       Net Income CAGR > 15% AND OCF positive AND growing
      Maturity:     positive OCF, stable Net Income (low growth, low decline)
    """
    if "NetIncome" not in metrics.columns or len(metrics) < 3:
        return "Unknown", "Insufficient data"

    ni  = metrics["NetIncome"].dropna()
    ocf = metrics.get("OCF", pd.Series(dtype=float)).dropna()

    if len(ni) < 3:
        return "Unknown", "Insufficient income data"

    # Compute geometric growth (CAGR) on Net Income
    first, last = ni.iloc[0], ni.iloc[-1]
    n = len(ni) - 1
    cagr = ((last / first) ** (1 / n) - 1) if first > 0 and last > 0 else None

    # OCF signals
    ocf_recent = ocf.tail(2).mean() if len(ocf) >= 2 else (ocf.iloc[-1] if len(ocf) else 0)
    ocf_positive = ocf_recent > 0

    if not ocf_positive and ni.iloc[-1] < ni.iloc[-2]:
        return "Decline", f"OCF negative ({ocf_recent:,.0f}) and Net Income falling"
    if cagr is not None and cagr > 0.15 and ocf_positive:
        return "Growth", f"NI CAGR {cagr*100:.1f}% with positive OCF ({ocf_recent:,.0f})"
    if ocf_positive and cagr is not None and -0.05 < cagr < 0.15:
        return "Maturity", f"Stable NI growth ({cagr*100:.1f}%), positive OCF"
    if ni.iloc[-1] > 0 and not ocf_positive:
        return "Introduction", "Income positive but OCF still negative"
    return "Decline", f"NI CAGR {cagr*100:.1f}% — weak fundamentals" if cagr is not None else "Weak fundamentals"


def fundamental_score(metrics: pd.DataFrame) -> float:
    """Composite score for ranking stocks within a sector (higher = stronger)."""
    if metrics is None or metrics.empty:
        return -1e9
    last = metrics.iloc[-1]
    score = 0.0
    score += last.get("ROE", 0)  * 1.0
    score += last.get("ROA", 0)  * 1.0
    score += np.log1p(max(last.get("BVPS", 0), 0)) * 5
    score += np.log1p(max(last.get("EBIT", 0), 0)) * 2
    # Penalize negative net income
    if last.get("NetIncome", 0) < 0:
        score -= 50
    return float(score)


# ===========================================================================
# PHASE 2 — HYBRID PRICE MODELS (Assignment 3)
# ===========================================================================
def calibrate_lambdas(df: pd.DataFrame) -> tuple[float, float, float, float]:
    """
    Grid-search λ₁, λ₂, λ₃ to minimize 1-step MSE on
        P_hat[t+1] = P[t] + λ₁(EMA5−EMA20) + λ₂·ROC + λ₃(V/V̄₁₀)
    Returns (λ₁, λ₂, λ₃, RMSE).
    """
    df = df.copy()
    df["EMA5"]  = df["CLOSE"].ewm(span=5,  adjust=False).mean()
    df["EMA20"] = df["CLOSE"].ewm(span=20, adjust=False).mean()
    df["ROC"]   = df["CLOSE"].diff(4)
    df["VR"]    = df["VOLUME"] / df["VOLUME"].rolling(10).mean()
    df = df.dropna()
    if len(df) < 30:
        return 0.5, 0.1, 0.1, np.nan

    X1 = (df["EMA5"] - df["EMA20"]).values[:-1]
    X2 = df["ROC"].values[:-1]
    X3 = df["VR"].values[:-1]
    P  = df["CLOSE"].values[:-1]
    Y  = df["CLOSE"].values[1:]      # next-day close

    # Closed-form OLS on (Y - P) = λ₁X1 + λ₂X2 + λ₃X3
    X = np.column_stack([X1, X2, X3])
    target = Y - P
    try:
        lam, *_ = np.linalg.lstsq(X, target, rcond=None)
        pred = P + X @ lam
        rmse = float(np.sqrt(np.mean((pred - Y) ** 2)))
        # Clip to sane magnitudes so the forecast doesn't explode
        lam = np.clip(lam, -3, 3)
        return float(lam[0]), float(lam[1]), float(lam[2]), rmse
    except Exception:
        return 0.5, 0.1, 0.1, np.nan


def run_model1(df: pd.DataFrame, lambdas=None) -> pd.DataFrame:
    """Adds EMA5, EMA20, ROC, VR, P_hat to df. Calibrates λ if not given."""
    df = df.copy()
    df["EMA5"]  = df["CLOSE"].ewm(span=5,  adjust=False).mean()
    df["EMA20"] = df["CLOSE"].ewm(span=20, adjust=False).mean()
    df["ROC"]   = df["CLOSE"].diff(4)
    df["VR"]    = (df["VOLUME"] / df["VOLUME"].rolling(10).mean()).fillna(1)
    if lambdas is None:
        l1, l2, l3, _ = calibrate_lambdas(df)
    else:
        l1, l2, l3 = lambdas
    df["P_hat"] = df["CLOSE"] + l1 * (df["EMA5"] - df["EMA20"]) \
                              + l2 * df["ROC"].fillna(0) \
                              + l3 * df["VR"]
    df.attrs["lambdas"] = (l1, l2, l3)
    return df


def supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> pd.Series:
    """Classic SuperTrend trend signal (+1 uptrend / -1 downtrend)."""
    hl2 = (df["HIGH"] + df["LOW"]) / 2
    tr1 = df["HIGH"] - df["LOW"]
    tr2 = (df["HIGH"] - df["CLOSE"].shift()).abs()
    tr3 = (df["LOW"]  - df["CLOSE"].shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()

    upper = hl2 + mult * atr
    lower = hl2 - mult * atr
    trend = pd.Series(index=df.index, dtype=float)
    trend.iloc[0] = 1
    for i in range(1, len(df)):
        prev = trend.iloc[i - 1]
        if df["CLOSE"].iloc[i] > upper.iloc[i - 1]:
            trend.iloc[i] = 1
        elif df["CLOSE"].iloc[i] < lower.iloc[i - 1]:
            trend.iloc[i] = -1
        else:
            trend.iloc[i] = prev
    return trend.fillna(0)


def optimize_supertrend(df: pd.DataFrame) -> tuple[int, float, float]:
    """Grid-search (period, multiplier) maximizing in-sample Sharpe of the long-only signal."""
    best = (10, 3.0, -np.inf)
    rets = df["CLOSE"].pct_change()
    for period in (7, 10, 14, 21):
        for mult in (1.5, 2.0, 2.5, 3.0, 3.5):
            tr = supertrend(df, period, mult)
            strat = rets * tr.shift(1).fillna(0).clip(lower=0)  # long-only
            if strat.std() > 0:
                sharpe = strat.mean() / strat.std() * np.sqrt(252)
                if sharpe > best[2]:
                    best = (period, mult, sharpe)
    return best


def run_model2(df: pd.DataFrame) -> pd.DataFrame:
    """Model 2: Signal = 0.6·Trend + 0.4·sign(Momentum)"""
    df = df.copy()
    period, mult, sharpe = optimize_supertrend(df)
    df["Trend"]    = supertrend(df, period, mult)
    df["Momentum"] = df["CLOSE"] - df["CLOSE"].shift(10)
    df["Signal"]   = 0.6 * df["Trend"] + 0.4 * np.sign(df["Momentum"].fillna(0))
    df.attrs["supertrend_params"] = (period, mult, sharpe)
    return df


# ===========================================================================
# PHASE 3 — CAPM
# ===========================================================================
def weekly_returns(df: pd.DataFrame) -> pd.Series:
    """Daily -> weekly close returns."""
    w = df["CLOSE"].resample("W-FRI").last().pct_change().dropna()
    return w


def run_capm(stock_returns: dict[str, pd.Series], market: pd.Series,
             risk_free: float = 0.18) -> list[dict]:
    """β, expected return (annualized), α, valuation status."""
    out = []
    rm_annual = market.mean() * 52
    for s, r in stock_returns.items():
        common = r.index.intersection(market.index)
        if len(common) < 10:
            continue
        rr = r.loc[common]
        mm = market.loc[common]
        var_m = mm.var()
        if var_m == 0:
            continue
        beta = rr.cov(mm) / var_m
        actual = rr.mean() * 52
        expected = risk_free + beta * (rm_annual - risk_free)
        alpha = actual - expected
        if alpha > 0.02:    status = "Undervalued"
        elif alpha < -0.02: status = "Overvalued"
        else:               status = "Fairly Valued"
        nature = "Aggressive" if beta > 1.1 else ("Defensive" if beta < 0.9 else "Neutral")
        out.append({
            "stock": s,
            "beta":  round(float(beta), 3),
            "expected_return": round(float(expected) * 100, 2),
            "actual_return":   round(float(actual)   * 100, 2),
            "alpha":  round(float(alpha) * 100, 2),
            "status": status,
            "nature": nature,
        })
    return out


# ===========================================================================
# PHASE 4 — MPT WITH LEDOIT-WOLF SHRINKAGE
# ===========================================================================
def ledoit_wolf_cov(returns_df: pd.DataFrame) -> np.ndarray:
    """Shrinkage covariance — fixes ill-conditioning when n_assets ~ n_obs."""
    R = returns_df.values
    T, N = R.shape
    sample = np.cov(R, rowvar=False)
    target = np.diag(np.diag(sample))   # diagonal target

    # Estimate shrinkage intensity (simplified Ledoit-Wolf)
    Rc = R - R.mean(axis=0)
    pi_mat = (Rc**2).T @ (Rc**2) / T - sample**2
    pi = pi_mat.sum()
    gamma = ((sample - target) ** 2).sum()
    shrinkage = max(0.0, min(1.0, pi / (gamma * T) if gamma > 0 else 0.2))
    return shrinkage * target + (1 - shrinkage) * sample


def optimize_portfolio(returns_df: pd.DataFrame, risk_free: float = 0.18, mode: str = "sharpe"):
    """Max-Sharpe or min-variance with long-only constraint."""
    cov_annual = ledoit_wolf_cov(returns_df) * 52
    mean_annual = returns_df.mean().values * 52
    n = len(returns_df.columns)

    def port_perf(w):
        r = float(np.dot(w, mean_annual))
        v = float(np.sqrt(np.dot(w.T, np.dot(cov_annual, w))))
        return r, v

    if mode == "minvar":
        obj = lambda w: float(np.dot(w.T, np.dot(cov_annual, w)))
    else:
        def obj(w):
            r, v = port_perf(w)
            return -(r - risk_free) / v if v > 0 else 1e6

    constraints = {"type": "eq", "fun": lambda w: np.sum(w) - 1}
    bounds = [(0.0, 1.0)] * n
    init = np.ones(n) / n
    result = minimize(obj, init, method="SLSQP", bounds=bounds, constraints=constraints)
    w = result.x
    r, v = port_perf(w)
    sharpe = (r - risk_free) / v if v > 0 else 0
    return w, r, v, sharpe, cov_annual, mean_annual


def efficient_frontier(returns_df: pd.DataFrame, cov_annual: np.ndarray,
                       mean_annual: np.ndarray, n_points: int = 40) -> dict:
    """Sweep target returns to trace the frontier."""
    target_rs = np.linspace(mean_annual.min(), mean_annual.max(), n_points)
    risks, rets = [], []
    n = len(mean_annual)
    bounds = [(0, 1)] * n
    for tgt in target_rs:
        cons = [
            {"type": "eq", "fun": lambda w: np.sum(w) - 1},
            {"type": "eq", "fun": lambda w, t=tgt: np.dot(w, mean_annual) - t},
        ]
        res = minimize(lambda w: float(np.dot(w.T, np.dot(cov_annual, w))),
                       np.ones(n)/n, method="SLSQP", bounds=bounds, constraints=cons)
        if res.success:
            risks.append(float(np.sqrt(res.fun) * 100))
            rets.append(float(tgt * 100))
    return {"risks": risks, "returns": rets}


# ===========================================================================
# PHASE 6 — VALIDATION / OUT-OF-SAMPLE BACKTEST
# ===========================================================================
def max_drawdown(equity_curve: pd.Series) -> float:
    """Maximum peak-to-trough drawdown (returned as a positive fraction)."""
    if len(equity_curve) < 2:
        return 0.0
    running_max = equity_curve.cummax()
    drawdowns = (equity_curve - running_max) / running_max
    return float(-drawdowns.min())


def sharpe_ratio(returns: pd.Series, risk_free: float = 0.18, freq: int = 52) -> float:
    """Annualized Sharpe from a return series."""
    if returns.std() == 0 or len(returns) < 2:
        return 0.0
    excess = returns.mean() * freq - risk_free
    vol = returns.std() * np.sqrt(freq)
    return float(excess / vol) if vol > 0 else 0.0


def run_validation(sector: str, holdout_months: int = 6,
                   risk_free: float = 0.18) -> dict:
    """
    Out-of-sample backtest replicating the CEP's validation requirement.

    Procedure
    ---------
    1. Determine the holdout window (last N months of available data).
    2. Run the FINX pipeline on data BEFORE the holdout cutoff:
         - Score fundamentals → pick top-2 tickers
         - Run MPT on top-2 + MDTL + MLCF using only in-sample returns
    3. Apply the resulting weights to OUT-OF-SAMPLE returns. Compute the
       portfolio's weekly returns through the holdout window.
    4. Build a benchmark: equal-weighted basket of the same 4 tickers
       (proxy for the "KSE-100 equal-weight" baseline per CEP rubric).
    5. Compare cumulative growth, Sharpe, max drawdown, and total alpha.

    Returns a dict suitable for direct JSON response.
    """
    cfg = load_config()
    if sector not in cfg["sectors"]:
        return {"error": f"Unknown sector: {sector}"}

    benchmarks = cfg["_meta"]["benchmark_tickers"]
    available = list_available_tickers()

    # --- 1. Pick top-2 from fundamentals (only those with prices) ----------
    candidates = []
    for t in cfg["sectors"][sector]:
        if t not in available:
            continue
        fund = load_fundamentals(t)
        if fund is None or fund.empty:
            continue
        metrics = compute_ratios(fund)
        candidates.append((t, fundamental_score(metrics)))
    candidates.sort(key=lambda x: -x[1])
    top2 = [t for t, _ in candidates[:2]]
    portfolio_tickers = list(dict.fromkeys(top2 + benchmarks))
    if len(portfolio_tickers) < 2:
        return {"error": "Not enough tickers with both fundamentals and prices to validate"}

    # --- 2. Load prices, derive weekly returns, find holdout boundary ------
    weekly_all = {}
    for t in portfolio_tickers:
        df = load_prices(t)
        if df is None or len(df) < 30:
            continue
        weekly_all[t] = weekly_returns(df)
    if len(weekly_all) < 2:
        return {"error": "Insufficient price history for backtest"}

    # Find common date range
    common_idx = None
    for s in weekly_all.values():
        common_idx = s.index if common_idx is None else common_idx.intersection(s.index)
    if common_idx is None or len(common_idx) < 12:
        return {"error": "Insufficient overlapping weekly returns",
                "weeks_available": len(common_idx) if common_idx is not None else 0}

    aligned = pd.DataFrame({t: weekly_all[t].reindex(common_idx) for t in weekly_all}).dropna()
    last_date = aligned.index.max()
    cutoff = last_date - pd.DateOffset(months=holdout_months)
    in_sample  = aligned.loc[aligned.index <= cutoff]
    out_sample = aligned.loc[aligned.index >  cutoff]

    if len(in_sample) < 8 or len(out_sample) < 4:
        return {
            "error": (f"Insufficient data: in-sample weeks={len(in_sample)}, "
                      f"out-sample weeks={len(out_sample)}. Need ≥8 in-sample and "
                      f"≥4 holdout. Run data_fetcher.py to extend price history."),
            "in_sample_weeks": len(in_sample),
            "out_sample_weeks": len(out_sample),
        }

    # --- 3. Optimize portfolio on IN-SAMPLE data only ----------------------
    w_opt, r_in, v_in, sharpe_in, _, _ = optimize_portfolio(
        in_sample, risk_free=risk_free, mode="sharpe")
    weights = {t: float(w_opt[i]) for i, t in enumerate(in_sample.columns)}

    # --- 4. Apply weights to OUT-OF-SAMPLE returns -------------------------
    finx_oos_returns = (out_sample * pd.Series(weights)).sum(axis=1)
    finx_equity = (1 + finx_oos_returns).cumprod()

    # Equal-weight benchmark on same 4 tickers
    n = len(out_sample.columns)
    bench_weights = {t: 1.0 / n for t in out_sample.columns}
    bench_oos_returns = (out_sample * pd.Series(bench_weights)).sum(axis=1)
    bench_equity = (1 + bench_oos_returns).cumprod()

    # KSE-100 as second benchmark if available
    kse_curve = None
    kse = load_prices("KSE100")
    if kse is not None:
        kse_w = weekly_returns(kse)
        kse_oos = kse_w.reindex(out_sample.index).dropna()
        if len(kse_oos) >= 4:
            kse_curve = (1 + kse_oos).cumprod()

    # --- 5. Compute metrics -----------------------------------------------
    def metrics_block(rets, equity, label):
        total_return = float(equity.iloc[-1] - 1) if len(equity) else 0
        return {
            "label":          label,
            "total_return":   round(total_return * 100, 2),
            "annualized":     round((float(rets.mean()) * 52) * 100, 2),
            "volatility":     round((float(rets.std()) * np.sqrt(52)) * 100, 2),
            "sharpe":         round(sharpe_ratio(rets, risk_free), 3),
            "max_drawdown":   round(max_drawdown(equity) * 100, 2),
            "weeks":          int(len(rets)),
        }

    finx_metrics  = metrics_block(finx_oos_returns,  finx_equity,  "FINX Portfolio")
    bench_metrics = metrics_block(bench_oos_returns, bench_equity, "Equal-weight Basket")
    kse_metrics = None
    if kse_curve is not None and len(kse_curve):
        kse_metrics = metrics_block(
            kse_curve.pct_change().dropna(), kse_curve, "KSE-100 Index")

    # Information ratio (alpha vs benchmark over holdout)
    alpha_total = finx_metrics["total_return"] - bench_metrics["total_return"]

    # --- 6. Build equity curves for the chart ------------------------------
    dates_str = out_sample.index.strftime("%Y-%m-%d").tolist()
    equity_curves = {
        "dates": dates_str,
        "finx":      [round(float(v), 4) for v in finx_equity],
        "benchmark": [round(float(v), 4) for v in bench_equity],
    }
    if kse_curve is not None:
        equity_curves["kse100"] = [round(float(v), 4)
            for v in kse_curve.reindex(out_sample.index).ffill().fillna(1)]

    return {
        "sector": sector,
        "holdout_months": holdout_months,
        "cutoff_date":    cutoff.strftime("%Y-%m-%d"),
        "last_date":      last_date.strftime("%Y-%m-%d"),
        "portfolio_tickers": list(in_sample.columns),
        "weights":      {t: round(weights[t] * 100, 2) for t in weights},
        "in_sample":  {
            "weeks":         len(in_sample),
            "expected_return": round(r_in * 100, 2),
            "expected_risk":   round(v_in * 100, 2),
            "expected_sharpe": round(sharpe_in, 3),
        },
        "out_sample": {
            "finx":      finx_metrics,
            "benchmark": bench_metrics,
            "kse100":    kse_metrics,
            "alpha_total": round(alpha_total, 2),
        },
        "equity_curves": equity_curves,
    }


# ===========================================================================
# FLASK ROUTES
# ===========================================================================
@app.route("/")
def home():
    cfg = load_config()
    sectors = list(cfg["sectors"].keys())
    return render_template("index.html",
                           sectors=sectors,
                           benchmarks=cfg["_meta"]["benchmark_tickers"])


@app.route("/api/sectors")
def api_sectors():
    cfg = load_config()
    available = list_available_tickers()
    payload = {}
    for sec, tickers in cfg["sectors"].items():
        payload[sec] = [{"ticker": t, "has_data": t in available} for t in tickers]
    payload["_meta"] = cfg["_meta"]
    return jsonify(payload)


@app.route("/api/screen", methods=["POST"])
def api_screen():
    """Phase 1+2 — fundamental screening for one sector."""
    sector = request.json.get("sector")
    cfg = load_config()
    if sector not in cfg["sectors"]:
        return jsonify({"error": f"Unknown sector: {sector}"}), 400

    tickers = cfg["sectors"][sector]
    available = list_available_tickers()
    rows = []
    charts = {}
    skipped = []

    for t in tickers:
        fund = load_fundamentals(t)
        if fund is None or fund.empty:
            skipped.append({"ticker": t, "reason": "no fundamentals CSV"})
            continue
        has_prices = t in available
        metrics = compute_ratios(fund)
        score = fundamental_score(metrics)
        stage, reason = lifecycle_stage(metrics)
        last = metrics.iloc[-1]
        rows.append({
            "ticker": t,
            "score":  round(score, 2),
            "stage":  stage,
            "reason": reason,
            "has_prices": has_prices,
            "BVPS":   None if pd.isna(last.get("BVPS")) else round(float(last["BVPS"]), 2),
            "ROE":    None if pd.isna(last.get("ROE"))  else round(float(last["ROE"]), 2),
            "ROA":    None if pd.isna(last.get("ROA"))  else round(float(last["ROA"]), 2),
            "EBIT":   None if pd.isna(last.get("EBIT")) else round(float(last["EBIT"]), 2),
            "EBT":    None if pd.isna(last.get("EBT"))  else round(float(last["EBT"]), 2),
            "EPS":    None if pd.isna(last.get("EPS"))  else round(float(last["EPS"]), 2),
            "NetIncome": None if pd.isna(last.get("NetIncome")) else round(float(last["NetIncome"]), 2),
        })
        years = metrics.index.astype(str).tolist()
        def safe_list(col):
            return [None if pd.isna(v) else round(float(v), 2)
                    for v in metrics.get(col, pd.Series([np.nan]*len(years)))]
        charts[t] = {
            "years": years,
            "BVPS": safe_list("BVPS"),
            "ROE":  safe_list("ROE"),
            "ROA":  safe_list("ROA"),
            "EBIT": safe_list("EBIT"),
            "NetIncome": safe_list("NetIncome"),
            "OCF": safe_list("OCF"),
        }

    if not rows:
        return jsonify({"error": "No fundamentals data found for any ticker in this sector",
                        "skipped": skipped}), 404

    rows.sort(key=lambda r: r["score"], reverse=True)
    # Top-2 must be tickers we have prices for — otherwise CAPM/MPT can't include them
    eligible = [r for r in rows if r["has_prices"]]
    top2 = [r["ticker"] for r in eligible[:2]]
    warning = None
    if len(eligible) < 2:
        warning = (f"Only {len(eligible)} ticker(s) in this sector have price data. "
                   f"Run: python data_fetcher.py --sector \"{sector}\" --years 5")
    elif len(eligible) < len(rows):
        excluded = [r["ticker"] for r in rows if not r["has_prices"]]
        warning = f"Skipped from top-2 (missing price CSV): {', '.join(excluded)}"

    return jsonify({
        "sector":   sector,
        "ranking":  rows,
        "top2":     top2,
        "charts":   charts,
        "skipped":  skipped,
        "warning":  warning,
    })


@app.route("/api/portfolio", methods=["POST"])
def api_portfolio():
    """Phase 3+4+5 — price models, CAPM, MPT for chosen tickers."""
    data = request.json
    tickers: list[str] = data.get("tickers", [])
    mode = data.get("mode", "sharpe")
    cfg = load_config()
    rf = cfg["_meta"].get("risk_free_rate", 0.18)

    # Always include benchmarks if not already present
    for b in cfg["_meta"]["benchmark_tickers"]:
        if b not in tickers:
            tickers.append(b)
    tickers = list(dict.fromkeys(tickers))

    price_data = {}
    technical  = {}
    missing    = []

    for t in tickers:
        df = load_prices(t)
        if df is None or len(df) < 30:
            missing.append(t)
            continue
        m1 = run_model1(df)
        m2 = run_model2(m1)
        price_data[t] = m2
        last = m2.iloc[-1]
        l1, l2, l3 = m1.attrs["lambdas"]
        st_period, st_mult, st_sharpe = m2.attrs["supertrend_params"]
        forecast_pct = (last["P_hat"] - last["CLOSE"]) / last["CLOSE"] * 100
        if last["Signal"] > 0.4 and forecast_pct > 0:
            recommendation = "BUY"
        elif last["Signal"] < -0.4 and forecast_pct < 0:
            recommendation = "SELL"
        else:
            recommendation = "HOLD"
        technical[t] = {
            "close":        round(float(last["CLOSE"]), 2),
            "forecast":     round(float(last["P_hat"]), 2),
            "forecast_pct": round(float(forecast_pct), 2),
            "signal":       round(float(last["Signal"]), 3),
            "trend":        int(last["Trend"]),
            "momentum":     round(float(last["Momentum"]), 2) if not pd.isna(last["Momentum"]) else 0,
            "recommendation": recommendation,
            "lambdas":      {"l1": round(l1, 3), "l2": round(l2, 3), "l3": round(l3, 3)},
            "supertrend":   {"period": st_period, "mult": st_mult,
                             "sharpe": round(float(st_sharpe), 3)},
            "history": {
                "dates":   m2.index.strftime("%Y-%m-%d").tolist()[-180:],
                "close":   [round(float(v), 2) for v in m2["CLOSE"].tail(180)],
                "forecast":[round(float(v), 2) for v in m2["P_hat"].tail(180)],
                "ema5":    [round(float(v), 2) for v in m2["EMA5"].tail(180)],
                "ema20":   [round(float(v), 2) for v in m2["EMA20"].tail(180)],
            }
        }

    if len(price_data) < 2:
        return jsonify({"error": "Need price data for at least 2 stocks",
                        "missing": missing}), 400

    # Build aligned weekly return matrix
    weekly = {t: weekly_returns(df) for t, df in price_data.items()}
    common_idx = None
    for s in weekly.values():
        common_idx = s.index if common_idx is None else common_idx.intersection(s.index)
    if common_idx is None or len(common_idx) < 10:
        return jsonify({"error": "Insufficient overlapping price history for CAPM/MPT"}), 400

    returns_df = pd.DataFrame({t: weekly[t].loc[common_idx] for t in weekly}).dropna()
    market = load_prices("KSE100")
    if market is not None:
        m_weekly = weekly_returns(market)
        market_aligned = m_weekly.reindex(returns_df.index).dropna()
        capm = run_capm({t: returns_df[t] for t in returns_df.columns},
                        market_aligned, risk_free=rf)
    else:
        capm = []

    # MPT
    w, ret, vol, sharpe, cov_annual, mean_annual = optimize_portfolio(
        returns_df, risk_free=rf, mode=mode)
    frontier = efficient_frontier(returns_df, cov_annual, mean_annual)

    weights = {t: round(float(w[i]) * 100, 2) for i, t in enumerate(returns_df.columns)}

    # Correlation matrix
    corr = returns_df.corr().round(3).to_dict()

    # Determine final recommendation
    top_weighted = sorted(weights.items(), key=lambda x: -x[1])[:2]
    final_text = (
        f"Optimal portfolio: " + ", ".join(f"{t} {w}%" for t, w in weights.items() if w > 1) +
        f". Expected annual return {ret*100:.1f}%, risk {vol*100:.1f}%, Sharpe {sharpe:.2f}. " +
        f"Primary holdings: {top_weighted[0][0]} and {top_weighted[1][0]}."
    )

    return jsonify({
        "tickers":   list(returns_df.columns),
        "technical": technical,
        "capm":      capm,
        "weights":   weights,
        "portfolio": {
            "expected_return": round(ret * 100, 2),
            "risk":            round(vol * 100, 2),
            "sharpe":          round(sharpe, 3),
            "risk_free":       round(rf * 100, 2),
            "mode":            mode,
        },
        "frontier":   frontier,
        "correlation": corr,
        "final_recommendation": final_text,
        "missing": missing,
    })


@app.route("/api/validate", methods=["POST"])
def api_validate():
    """Phase 6 — out-of-sample backtest comparing FINX portfolio vs equal-weight benchmark."""
    data = request.json or {}
    sector = data.get("sector")
    holdout_months = int(data.get("holdout_months", 6))
    cfg = load_config()
    rf = cfg["_meta"].get("risk_free_rate", 0.18)

    result = run_validation(sector, holdout_months=holdout_months, risk_free=rf)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)


@app.route("/api/health")
def health():
    return jsonify({
        "ok": True,
        "available_tickers": sorted(list_available_tickers()),
        "sectors_loaded": len(load_config()["sectors"]),
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
