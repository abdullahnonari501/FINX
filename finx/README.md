# FINX — Portfolio Intelligence System

> Sector-agnostic quantitative portfolio engine for the Pakistan Stock Exchange.
> Built for **CEP ES-447 Financial Engineering** (Spring 2026, GIK Institute).

FINX automates the entire workflow from Assignments 1-4: fundamental sector screening,
lifecycle classification, hybrid price models, CAPM, and Modern Portfolio Theory
optimization — all through a web interface that runs locally **or** on a public URL.

---

## What this implements (vs the CEP spec)

| CEP Requirement | Implementation |
|-----------------|----------------|
| Fetch fundamentals (5 yr) and prices (52 wk) | `data_fetcher.py` (psx-data-reader + yfinance with manual fallback) |
| Compute Net Income, BVPS, EPS, Cash Flow, ROE, ROA, EBIT, EBT | `compute_ratios()` in `app.py` |
| Lifecycle classification (Introduction → Growth → Maturity → Decline) | `lifecycle_stage()` — quantitative rules on CAGR & OCF |
| Hybrid Model 1: `P̂ = P + λ₁(EMA5−EMA20) + λ₂·ROC + λ₃(V/V̄₁₀)` | `run_model1()` — λ calibrated via least-squares grid search |
| Hybrid Model 2: `Signal = 0.6·Trend + 0.4·sign(M)` | `run_model2()` — SuperTrend period/multiplier optimized for Sharpe |
| CAPM (β, expected return, valuation) | `run_capm()` |
| MPT with no short-selling | `optimize_portfolio()` with SLSQP + bounds `[0,1]` |
| Ill-conditioned covariance handling | Ledoit-Wolf shrinkage in `ledoit_wolf_cov()` |
| Auto-include MDTL + MLCF benchmarks | `sectors.json → _meta.benchmark_tickers` |
| <5 sec for 20 stocks | LRU-cached CSV loading, vectorized NumPy throughout |
| GUI | Flask + modern fintech HTML/CSS/JS dashboard |

---

## Quick start (local)

```bash
# 1. Install
pip install -r requirements.txt

# 2. (optional) Fetch fresh price data for your sector
python data_fetcher.py --sector "Automobile Parts & Accessories" --years 5

# 3. Run
python app.py
# -> open http://localhost:5000
```

Real CSV data for the **Automobile Parts & Accessories** sector + MDTL/MLCF benchmarks
+ KSE-100 is bundled in `data/`. The app runs out-of-the-box without any internet
connection.

---

## Project structure

```
finx/
├── app.py                  # Flask backend (sector-agnostic engine)
├── data_fetcher.py         # Auto-pulls PSX prices + fundamentals
├── requirements.txt
├── Procfile                # for Render/Railway/Heroku
├── render.yaml             # one-click Render deploy
├── data/
│   ├── sectors.json        # ← EDIT THIS to add new sectors
│   ├── prices/
│   │   ├── ATBA_daily.csv
│   │   ├── BWHL_daily.csv
│   │   ├── KSE100_daily.csv
│   │   └── ... (one per ticker)
│   └── fundamentals/
│       ├── ATBA_fundamentals.csv
│       └── ... (one per ticker)
├── templates/index.html
└── static/
    ├── css/style.css
    └── js/app.js
```

---

## Adding a new sector (4 steps)

1. **Add the sector to `data/sectors.json`** under `"sectors"`:
   ```json
   "My New Sector": ["TICKER1", "TICKER2", "TICKER3"]
   ```

2. **Fetch prices automatically:**
   ```bash
   python data_fetcher.py --sector "My New Sector" --years 5
   ```
   This writes `data/prices/<TICKER>_daily.csv` for each ticker, using
   psx-data-reader → yfinance fallback.

3. **Fill fundamentals.** The fetcher will drop blank templates at
   `data/fundamentals/<TICKER>_fundamentals.csv`. Open each, fill the 9 metrics
   from the company's annual report (PSX.com.pk → company → financial reports).
   The schema is:
   ```
   Metric (in PKR Millions), 2020, 2021, 2022, 2023, 2024
   Total Assets, ...
   Total Liabilities, ...
   Total Equity, ...
   Total Shares Outstanding (Millions), ...
   EBIT (Operating Profit), ...
   EBT (Profit Before Tax), ...
   Net Income, ...
   Operating Cash Flow, ...
   EPS (PKR), ...
   ```

4. **Restart the app** (or it auto-discovers on next request). The new sector
   appears in the dropdown — no code changes needed.

---

## CSV schemas (canonical)

### `data/prices/<TICKER>_daily.csv`
```
DATE,OPEN,HIGH,LOW,CLOSE,VOLUME,CHANGE
02-May-2025,264,270.99,264,266.92,"42,723",5.56
```
Date format: `dd-Mon-YYYY`. Comma thousands separators are handled.

### `data/fundamentals/<TICKER>_fundamentals.csv`
Wide format with metric names in column 1 and one column per year. See
`ATBA_fundamentals.csv` for a working example.

---

## Deployment to Render (free tier)

1. Push this folder to a GitHub repo.
2. On [render.com](https://render.com) → **New** → **Web Service** → connect repo.
3. Render auto-detects `render.yaml`. Click **Create**.
4. Wait ~2 min. Your live URL appears (e.g. `finx-portfolio.onrender.com`).

Because all CSVs are committed to the repo, the deployed app has the data baked
in — no external API calls during runtime, no rate limits, fast cold start.

To deploy on **Railway** or **Heroku** instead: the `Procfile` works as-is.

---

## Calibration & methodology notes (for your report)

**λ calibration (Model 1).** Closed-form least-squares regression of
`P[t+1] − P[t]` on `(EMA5−EMA20, ROC, V/V̄₁₀)`. Coefficients clipped to `[−3, 3]`
to prevent runaway forecasts when training history is short.

**SuperTrend optimization (Model 2).** Grid search over `period ∈ {7, 10, 14, 21}`
and `multiplier ∈ {1.5, 2.0, 2.5, 3.0, 3.5}`. Selection maximizes long-only
in-sample Sharpe ratio.

**Lifecycle rules.** Apply in order:
1. `OCF_recent < 0` AND `NI_last < NI_prev` → **Decline**
2. `NI_CAGR > 15%` AND `OCF_recent > 0` → **Growth**
3. `OCF_recent > 0` AND `−5% < NI_CAGR < 15%` → **Maturity**
4. `NI_last > 0` AND `OCF_recent < 0` → **Introduction**
5. otherwise → **Decline**

**Covariance regularization (MPT).** Ledoit-Wolf shrinkage toward a diagonal
target. Handles the case where number of weeks ≈ number of assets, which
makes the sample covariance ill-conditioned.

**No-short constraint.** SLSQP with bounds `[0, 1]` and `Σwᵢ = 1`.

---

## API reference (for advanced use)

| Endpoint | Method | Body | Returns |
|----------|--------|------|---------|
| `/api/health` | GET | — | available tickers, sector count |
| `/api/sectors` | GET | — | sector → ticker map with data availability flags |
| `/api/screen` | POST | `{sector}` | ranking, top-2, lifecycle stages, chart data |
| `/api/portfolio` | POST | `{tickers, mode}` | technical models, CAPM, weights, frontier, correlation |

`mode` is `"sharpe"` (default) or `"minvar"`.

---

## Troubleshooting

**Sector dropdown is empty:** check `data/sectors.json` exists and is valid JSON.

**Screening fails with "No fundamentals":** at least one ticker in the sector
needs `data/fundamentals/<TICKER>_fundamentals.csv`.

**Portfolio fails with "Insufficient overlap":** price CSVs need ≥10 weeks of
shared trading dates. Run `data_fetcher.py --years 5` to extend history.

**Charts don't render:** check browser console — make sure Chart.js loaded from
the CDN.

---

## License
Coursework artifact. Not for redistribution.
