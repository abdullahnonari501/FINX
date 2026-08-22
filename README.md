# FINX — Quantitative Portfolio Intelligence

A sector-agnostic quantitative portfolio engine for **Pakistan Stock Exchange (PSX)** equities. FINX runs the full workflow — fundamental screening, lifecycle classification, hybrid price forecasting, CAPM, and Modern Portfolio Theory optimization — behind a Flask web dashboard.

Built for **CEP ES-447 Financial Engineering** (Spring 2026, GIK Institute).

---

## Results

Validated on a **6-month out-of-sample holdout** against the KSE-100 benchmark:

| Metric | FINX portfolio | KSE-100 |
|---|---|---|
| Return | **+7.13%** | — |
| Sharpe ratio | **0.176** | −0.427 |

---

## Pipeline

```
Fundamentals (5 yr)  ──┐
                       ├──►  Screening + lifecycle stage  ──►  Top-N selection
Prices (52 wk)       ──┘                                            │
                                                                    ▼
                            Hybrid price models (M1 + M2)  ──►  CAPM (β, E[r])
                                                                    │
                                                                    ▼
                                       MPT optimization (SLSQP, long-only)
                                                                    │
                                                                    ▼
                                    Weights · efficient frontier · correlation
```

**Fundamental screening** — computes Net Income, BVPS, EPS, cash flow, ROE, ROA, EBIT, and EBT from 5 years of statements, then assigns a lifecycle stage (Introduction → Growth → Maturity → Decline) using quantitative rules on net-income CAGR and operating cash flow.

**Hybrid forecasting** — two models. Model 1 is a momentum blend, `P̂ = P + λ₁(EMA5−EMA20) + λ₂·ROC + λ₃(V/V̄₁₀)`, with λ calibrated by closed-form least squares and clipped to `[−3, 3]` so short training histories can't produce runaway forecasts. Model 2 fuses a SuperTrend signal with momentum sign, its period and multiplier grid-searched to maximize long-only Sharpe.

**Optimization** — SLSQP with bounds `[0, 1]` and `Σwᵢ = 1` (no short selling). Sample covariance goes ill-conditioned when the number of weeks approaches the number of assets, so FINX applies **Ledoit-Wolf shrinkage** toward a diagonal target.

---

## Quick start

```bash
cd finx
pip install -r requirements.txt
python app.py          # → http://localhost:5000
```

Real PSX data for the *Automobile Parts & Accessories* sector, plus MDTL/MLCF benchmarks and the KSE-100 index, ships in `finx/data/`. The app runs offline out of the box — no API keys, no rate limits.

To pull a different sector:

```bash
python data_fetcher.py --sector "Cement" --years 5
```

---

## API

| Endpoint | Method | Body | Returns |
|---|---|---|---|
| `/api/health` | GET | — | available tickers, sector count |
| `/api/sectors` | GET | — | sector → ticker map with availability flags |
| `/api/screen` | POST | `{sector}` | ranking, top-2, lifecycle stages, chart data |
| `/api/portfolio` | POST | `{tickers, mode}` | models, CAPM, weights, frontier, correlation |
| `/api/validate` | POST | `{sector, holdout_months}` | out-of-sample backtest |

`mode` is `"sharpe"` (default) or `"minvar"`.

---

## Stack

`Python 3.11` · `Flask` · `NumPy` · `Pandas` · `SciPy` · `Chart.js` · `gunicorn`

Deploys to Render, Railway, or Heroku — `finx/render.yaml` and `finx/Procfile` are both committed and current.

---

## Documentation

Full methodology, CSV schemas, sector-extension guide, calibration notes, and troubleshooting live in **[`finx/README.md`](finx/README.md)**.

---

## License

Coursework artifact. Not for redistribution.
