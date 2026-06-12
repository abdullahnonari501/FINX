// ============================================================
// FINX Frontend Controller
// ============================================================

const PALETTE = ['#00633e', '#b89653', '#2c5282', '#b54f2a', '#5d4a8a', '#1a7a8a', '#8a5d1a', '#5d8a1a'];

let state = {
  sector: null,
  top2: [],
  capm: null,
  weights: null,
  charts: {},   // Chart.js instances keyed by canvas id
};

// ---------- Init: ticker count badge ---------------------------------------
fetch('/api/health')
  .then(r => r.json())
  .then(d => {
    document.getElementById('tickerCount').innerHTML =
      `<span class="dot live"></span> ${d.available_tickers.length} tickers · ${d.sectors_loaded} sectors`;
  });

// ---------- Helpers --------------------------------------------------------
function fmt(v, dp = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return Number(v).toLocaleString('en-US', { minimumFractionDigits: dp, maximumFractionDigits: dp });
}
function killChart(id) {
  if (state.charts[id]) { state.charts[id].destroy(); delete state.charts[id]; }
}
function setStep(n) {
  document.querySelectorAll('.step').forEach(el => {
    const k = +el.dataset.step;
    el.classList.toggle('active', k === n);
    el.classList.toggle('done', k < n);
  });
}
function show(id) { document.getElementById(id).classList.remove('hidden'); }
function hide(id) { document.getElementById(id).classList.add('hidden'); }

// ============================================================
// PHASE 1 — SECTOR SCREENING
// ============================================================
document.getElementById('btnScreen').addEventListener('click', async () => {
  const sector = document.getElementById('sectorSelect').value;
  if (!sector) { alert('Please select a sector first.'); return; }

  state.sector = sector;
  show('screenLoading'); hide('screenResults');

  try {
    const res = await fetch('/api/screen', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sector })
    });
    const data = await res.json();
    hide('screenLoading');

    if (data.error) {
      alert(data.error + (data.skipped ? `\n\nMissing data for: ${data.skipped.map(s=>s.ticker).join(', ')}` : ''));
      return;
    }
    renderScreening(data);
    setStep(2);
    show('panel-portfolio');
    document.getElementById('panel-portfolio').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) {
    hide('screenLoading');
    alert('Screening failed: ' + e.message);
  }
});

function renderScreening(data) {
  document.getElementById('sectorLabel').textContent = data.sector;
  document.getElementById('rankedCount').textContent = `${data.ranking.length} STOCKS RANKED`;

  // Warning banner (e.g. ticker excluded from top-2 due to missing prices)
  const existing = document.getElementById('screenWarning');
  if (existing) existing.remove();
  if (data.warning) {
    const w = document.createElement('div');
    w.id = 'screenWarning';
    w.className = 'warning-banner';
    w.innerHTML = `<strong>⚠ Notice:</strong> ${data.warning}`;
    document.getElementById('screenResults').prepend(w);
  }

  // Ranking table
  const tbody = document.querySelector('#rankingTable tbody');
  tbody.innerHTML = '';
  data.ranking.forEach((r, i) => {
    const isTop = data.top2.includes(r.ticker);
    const tr = document.createElement('tr');
    if (isTop) tr.classList.add('top');
    if (!r.has_prices) tr.classList.add('noprice');
    const priceFlag = r.has_prices ? '' : ' <span title="No price CSV" style="color:#c14242;font-size:10px">⊘</span>';
    tr.innerHTML = `
      <td>${i + 1}</td>
      <td class="ticker">${r.ticker}${priceFlag}</td>
      <td>${fmt(r.score)}</td>
      <td><span class="stage-badge stage-${r.stage}">${r.stage}</span></td>
      <td class="${r.ROE > 0 ? 'cell-pos' : 'cell-neg'}">${fmt(r.ROE)}</td>
      <td class="${r.ROA > 0 ? 'cell-pos' : 'cell-neg'}">${fmt(r.ROA)}</td>
      <td>${fmt(r.BVPS)}</td>
      <td>${fmt(r.EPS)}</td>
      <td>${fmt(r.EBIT, 0)}</td>
      <td>${fmt(r.EBT, 0)}</td>
      <td class="${r.NetIncome > 0 ? 'cell-pos' : 'cell-neg'}">${fmt(r.NetIncome, 0)}</td>`;
    tbody.appendChild(tr);
  });

  // Top 2 spotlight cards — use server-provided top2 list (only price-eligible tickers)
  const top2grid = document.getElementById('top2Grid');
  top2grid.innerHTML = '';
  const top2Rows = data.top2.map(t => data.ranking.find(r => r.ticker === t)).filter(Boolean);
  top2Rows.forEach((r, i) => {
    top2grid.innerHTML += `
      <div class="top2-card">
        <div class="top2-rank">${i === 0 ? 'Primary pick' : 'Secondary pick'}</div>
        <div class="top2-ticker">${r.ticker}</div>
        <div class="top2-stage">
          <span class="stage-badge stage-${r.stage}">${r.stage}</span>
          &nbsp;${r.reason}
        </div>
        <div class="top2-metrics">
          <div class="top2-metric"><div class="lbl">ROE</div><div class="val">${fmt(r.ROE,1)}%</div></div>
          <div class="top2-metric"><div class="lbl">BVPS</div><div class="val">${fmt(r.BVPS,1)}</div></div>
          <div class="top2-metric"><div class="lbl">EPS</div><div class="val">${fmt(r.EPS,1)}</div></div>
        </div>
      </div>`;
  });

  state.top2 = data.top2;
  renderTickerChips();

  // Fundamental trend charts
  const grid = document.getElementById('fundCharts');
  grid.innerHTML = '';
  Object.keys(data.charts).slice(0, 6).forEach(t => {
    const cdata = data.charts[t];
    const card = document.createElement('div');
    card.className = 'chart-card';
    card.innerHTML = `<div class="chart-card-title">${t} · 5-YEAR TREND</div>
                      <div class="chart-wrap"><canvas id="fund_${t}"></canvas></div>`;
    grid.appendChild(card);

    requestAnimationFrame(() => {
      killChart(`fund_${t}`);
      const ctx = document.getElementById(`fund_${t}`);
      state.charts[`fund_${t}`] = new Chart(ctx, {
        type: 'line',
        data: {
          labels: cdata.years,
          datasets: [
            { label: 'ROE %',  data: cdata.ROE,  borderColor: PALETTE[0], backgroundColor: PALETTE[0]+'20', yAxisID: 'y', tension: 0.3, borderWidth: 2 },
            { label: 'ROA %',  data: cdata.ROA,  borderColor: PALETTE[1], backgroundColor: PALETTE[1]+'20', yAxisID: 'y', tension: 0.3, borderWidth: 2 },
            { label: 'BVPS',   data: cdata.BVPS, borderColor: PALETTE[2], backgroundColor: PALETTE[2]+'20', yAxisID: 'y1', tension: 0.3, borderWidth: 2, borderDash: [4,3] },
          ]
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          interaction: { intersect: false, mode: 'index' },
          plugins: {
            legend: { labels: { font: { size: 11, family: 'JetBrains Mono' }, boxWidth: 10 } },
          },
          scales: {
            y:  { type: 'linear', position: 'left',  title: { display: true, text: '%' }, grid: { color: '#eee' } },
            y1: { type: 'linear', position: 'right', title: { display: true, text: 'BVPS (PKR)' }, grid: { display: false } },
            x:  { grid: { color: '#eee' }, ticks: { font: { family: 'JetBrains Mono', size: 10 } } }
          }
        }
      });
    });
  });
}

// ============================================================
// PHASE 2 — TICKER CHIPS (Top 2 + benchmarks)
// ============================================================
function renderTickerChips() {
  const chips = document.getElementById('tickerChips');
  chips.innerHTML = '';
  state.top2.forEach(t => {
    chips.innerHTML += `<span class="chip gold">★ ${t}</span>`;
  });
  // Benchmarks (read from server config)
  ['MDTL', 'MLCF'].forEach(b => {
    if (!state.top2.includes(b)) chips.innerHTML += `<span class="chip bench">⌬ ${b}</span>`;
  });
}

// ============================================================
// PHASE 3 — PORTFOLIO OPTIMIZATION
// ============================================================
document.getElementById('btnOptimize').addEventListener('click', async () => {
  if (state.top2.length === 0) { alert('Run sector screening first.'); return; }
  const mode = document.querySelector('input[name=mode]:checked').value;

  show('optLoading'); hide('optResults');

  try {
    const res = await fetch('/api/portfolio', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tickers: state.top2.slice(), mode })
    });
    const data = await res.json();
    hide('optLoading');

    if (data.error) { alert(data.error); return; }
    renderOptimization(data);
    setStep(3);
  } catch (e) {
    hide('optLoading');
    alert('Optimization failed: ' + e.message);
  }
});

function renderOptimization(data) {
  show('optResults');

  // Warning banner for tickers that were dropped
  const existing = document.getElementById('optWarning');
  if (existing) existing.remove();
  if (data.missing && data.missing.length > 0) {
    const w = document.createElement('div');
    w.id = 'optWarning';
    w.className = 'warning-banner';
    w.innerHTML = `<strong>⚠ Dropped from portfolio (no price CSV):</strong> ${data.missing.join(', ')}.
      To include them, run: <code>python data_fetcher.py --tickers ${data.missing.join(' ')} --years 5</code>`;
    document.getElementById('optResults').prepend(w);
  }

  // Hero metrics
  document.getElementById('mExpRet').textContent = data.portfolio.expected_return + '%';
  document.getElementById('mRisk').textContent   = data.portfolio.risk + '%';
  document.getElementById('mSharpe').textContent = data.portfolio.sharpe.toFixed(2);
  document.getElementById('mRf').textContent     = data.portfolio.risk_free + '%';

  // Donut + weight list
  renderDonut(data.weights);
  renderWeightList(data.weights);

  // Efficient frontier
  renderFrontier(data.frontier, data.portfolio);

  // Technical signals
  renderTechGrid(data.technical);

  // CAPM table
  renderCAPM(data.capm);

  // Correlation heatmap
  renderCorrelation(data.correlation);

  // Final recommendation
  document.getElementById('recText').textContent = data.final_recommendation;

  // Save for export
  state.lastReport = data;
  document.getElementById('optResults').scrollIntoView({ behavior: 'smooth', block: 'start' });

  // Unlock step 4 (validation)
  document.getElementById('panel-validate').classList.remove('hidden');
}

function renderDonut(weights) {
  killChart('donutChart');
  const entries = Object.entries(weights).filter(([,v]) => v > 0.5);
  state.charts.donutChart = new Chart(document.getElementById('donutChart'), {
    type: 'doughnut',
    data: {
      labels: entries.map(e => e[0]),
      datasets: [{
        data: entries.map(e => e[1]),
        backgroundColor: entries.map((_, i) => PALETTE[i % PALETTE.length]),
        borderWidth: 2, borderColor: '#fff'
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false, cutout: '65%',
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: c => ` ${c.label}: ${c.raw.toFixed(1)}%` } }
      }
    }
  });
}

function renderWeightList(weights) {
  const list = document.getElementById('weightList');
  list.innerHTML = '';
  const entries = Object.entries(weights).sort((a, b) => b[1] - a[1]);
  entries.forEach((e, i) => {
    const [t, v] = e;
    list.innerHTML += `
      <div class="weight-row">
        <div class="weight-dot" style="background:${PALETTE[i % PALETTE.length]}"></div>
        <div class="weight-tkr">${t}</div>
        <div class="weight-bar"><div class="weight-bar-fill" style="width:${v}%; background:${PALETTE[i % PALETTE.length]}"></div></div>
        <div class="weight-pct">${v.toFixed(1)}%</div>
      </div>`;
  });
}

function renderFrontier(frontier, portfolio) {
  killChart('frontierChart');
  state.charts.frontierChart = new Chart(document.getElementById('frontierChart'), {
    type: 'scatter',
    data: {
      datasets: [
        {
          label: 'Frontier',
          data: frontier.risks.map((r, i) => ({ x: r, y: frontier.returns[i] })),
          showLine: true, borderColor: PALETTE[0], borderWidth: 2,
          backgroundColor: PALETTE[0]+'30', pointRadius: 2,
        },
        {
          label: 'Optimal',
          data: [{ x: portfolio.risk, y: portfolio.expected_return }],
          backgroundColor: PALETTE[1], borderColor: PALETTE[1],
          pointRadius: 9, pointStyle: 'star',
        }
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { font: { size: 11, family: 'JetBrains Mono' } } } },
      scales: {
        x: { title: { display: true, text: 'Risk (σ, %)' }, grid: { color: '#eee' } },
        y: { title: { display: true, text: 'Expected Return (%)' }, grid: { color: '#eee' } }
      }
    }
  });
}

function renderTechGrid(technical) {
  const grid = document.getElementById('techGrid');
  grid.innerHTML = '';
  Object.entries(technical).forEach(([t, d]) => {
    const card = document.createElement('div');
    card.className = 'tech-card';
    const diff = d.forecast_pct;
    card.innerHTML = `
      <div class="tech-header">
        <div class="tech-ticker">${t}</div>
        <div class="tech-rec ${d.recommendation}">${d.recommendation}</div>
      </div>
      <div class="tech-prices">
        <div><div class="lbl">Close</div><div class="val">${fmt(d.close)}</div></div>
        <div><div class="lbl">Forecast P̂</div><div class="val ${diff >= 0 ? 'cell-pos' : 'cell-neg'}">${fmt(d.forecast)} (${diff >= 0 ? '+' : ''}${fmt(diff,1)}%)</div></div>
        <div><div class="lbl">Signal</div><div class="val">${fmt(d.signal, 2)}</div></div>
      </div>
      <div class="tech-mini"><canvas id="mini_${t}"></canvas></div>
      <div class="tech-meta">
        λ₁=${d.lambdas.l1} · λ₂=${d.lambdas.l2} · λ₃=${d.lambdas.l3}
        &nbsp;|&nbsp; ST(${d.supertrend.period},${d.supertrend.mult}) Sharpe=${d.supertrend.sharpe}
      </div>`;
    grid.appendChild(card);

    requestAnimationFrame(() => {
      killChart(`mini_${t}`);
      state.charts[`mini_${t}`] = new Chart(document.getElementById(`mini_${t}`), {
        type: 'line',
        data: {
          labels: d.history.dates,
          datasets: [
            { label: 'Close',    data: d.history.close,    borderColor: PALETTE[0], borderWidth: 1.8, pointRadius: 0, tension: 0.25 },
            { label: 'EMA5',     data: d.history.ema5,     borderColor: PALETTE[1], borderWidth: 1.2, pointRadius: 0, tension: 0.25, borderDash: [3,2] },
            { label: 'EMA20',    data: d.history.ema20,    borderColor: PALETTE[2], borderWidth: 1.2, pointRadius: 0, tension: 0.25, borderDash: [3,2] },
            { label: 'Forecast', data: d.history.forecast, borderColor: PALETTE[3], borderWidth: 1.2, pointRadius: 0, tension: 0.25 },
          ]
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          interaction: { intersect: false, mode: 'index' },
          plugins: { legend: { display: false }, tooltip: { enabled: true } },
          scales: { x: { display: false }, y: { display: true, ticks: { font: { size: 9 } }, grid: { color: '#f5f5f5' } } }
        }
      });
    });
  });
}

function renderCAPM(capm) {
  const tbody = document.querySelector('#capmTable tbody');
  tbody.innerHTML = '';
  capm.forEach(r => {
    const aCls = r.alpha > 0 ? 'cell-pos' : 'cell-neg';
    tbody.innerHTML += `
      <tr>
        <td class="ticker">${r.stock}</td>
        <td>${fmt(r.beta, 3)}</td>
        <td>${r.nature}</td>
        <td>${fmt(r.expected_return)}</td>
        <td>${fmt(r.actual_return)}</td>
        <td class="${aCls}">${r.alpha >= 0 ? '+' : ''}${fmt(r.alpha)}</td>
        <td><span class="stage-badge stage-${r.status === 'Undervalued' ? 'Growth' : r.status === 'Overvalued' ? 'Decline' : 'Maturity'}">${r.status}</span></td>
      </tr>`;
  });
}

function renderCorrelation(corr) {
  const tickers = Object.keys(corr);
  const wrap = document.getElementById('corrHeatmap');
  let html = '<table class="corr-table"><thead><tr><th></th>';
  tickers.forEach(t => html += `<th>${t}</th>`);
  html += '</tr></thead><tbody>';
  tickers.forEach(t1 => {
    html += `<tr><th>${t1}</th>`;
    tickers.forEach(t2 => {
      const v = corr[t1][t2];
      // Colorize: red for negative, green for positive
      let color;
      if (v >= 0.7) color = 'rgba(0,99,62,0.55)';
      else if (v >= 0.3) color = 'rgba(0,168,107,0.35)';
      else if (v >= -0.3) color = 'rgba(200,200,200,0.4)';
      else if (v >= -0.7) color = 'rgba(193,66,66,0.35)';
      else color = 'rgba(193,66,66,0.55)';
      const textColor = Math.abs(v) > 0.5 ? '#fff' : '#222';
      html += `<td class="cell" style="background:${color}; color:${textColor}">${v.toFixed(2)}</td>`;
    });
    html += '</tr>';
  });
  html += '</tbody></table>';
  wrap.innerHTML = html;
}

// ============================================================
// EXPORT
// ============================================================
document.getElementById('btnDownload').addEventListener('click', () => {
  if (!state.lastReport) return;
  const payload = {
    timestamp: new Date().toISOString(),
    sector: state.sector,
    ...state.lastReport
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `finx_report_${state.sector.replace(/\W+/g, '_')}_${Date.now()}.json`;
  a.click();
});

// ============================================================
// PHASE 4 — VALIDATION / OUT-OF-SAMPLE BACKTEST
// ============================================================
document.getElementById('btnValidate').addEventListener('click', async () => {
  if (!state.sector) { alert('Run screening first.'); return; }
  const holdout_months = parseInt(document.getElementById('holdoutSelect').value, 10);

  show('valLoading'); hide('valResults');

  try {
    const res = await fetch('/api/validate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sector: state.sector, holdout_months })
    });
    const data = await res.json();
    hide('valLoading');

    if (data.error) {
      alert(data.error);
      return;
    }
    renderValidation(data);
    setStep(4);
  } catch (e) {
    hide('valLoading');
    alert('Validation failed: ' + e.message);
  }
});

function renderValidation(data) {
  show('valResults');

  // Hero metrics
  const f = data.out_sample.finx;
  const b = data.out_sample.benchmark;
  document.getElementById('vFinxRet').textContent  = (f.total_return >= 0 ? '+' : '') + f.total_return.toFixed(2) + '%';
  document.getElementById('vBenchRet').textContent = (b.total_return >= 0 ? '+' : '') + b.total_return.toFixed(2) + '%';
  const alpha = data.out_sample.alpha_total;
  document.getElementById('vAlpha').textContent    = (alpha >= 0 ? '+' : '') + alpha.toFixed(2) + '%';
  document.getElementById('vDD').textContent       = f.max_drawdown.toFixed(2) + '%';
  document.getElementById('vCutoff').textContent   = `CUTOFF: ${data.cutoff_date} → ${data.last_date}`;

  // Equity curve
  renderEquityChart(data.equity_curves);

  // Comparison table
  const tbody = document.querySelector('#valTable tbody');
  tbody.innerHTML = '';
  const rows = [data.out_sample.finx, data.out_sample.benchmark];
  if (data.out_sample.kse100) rows.push(data.out_sample.kse100);
  rows.forEach((r, i) => {
    const isFinx = i === 0;
    const tr = document.createElement('tr');
    if (isFinx) tr.classList.add('top');
    const retCls = r.total_return >= 0 ? 'cell-pos' : 'cell-neg';
    const shCls  = r.sharpe >= 0 ? 'cell-pos' : 'cell-neg';
    tr.innerHTML = `
      <td class="ticker">${r.label}</td>
      <td class="${retCls}">${r.total_return >= 0 ? '+' : ''}${fmt(r.total_return)}</td>
      <td>${fmt(r.annualized)}</td>
      <td>${fmt(r.volatility)}</td>
      <td class="${shCls}">${fmt(r.sharpe, 3)}</td>
      <td>${fmt(r.max_drawdown)}</td>
      <td>${r.weeks}</td>`;
    tbody.appendChild(tr);
  });

  // In-sample vs realized
  const insample = document.getElementById('insampleGrid');
  const realizedAnnualized = data.out_sample.finx.annualized;
  const realizedSharpe     = data.out_sample.finx.sharpe;
  const forecastAnnualized = data.in_sample.expected_return;
  const forecastSharpe     = data.in_sample.expected_sharpe;
  insample.innerHTML = `
    <div class="insample-card">
      <div class="insample-lbl">Annualized Return</div>
      <div class="insample-row">
        <div class="insample-side"><div class="tag">FORECAST</div><div class="val">${fmt(forecastAnnualized)}%</div></div>
        <div class="insample-arrow">→</div>
        <div class="insample-side"><div class="tag">REALIZED</div><div class="val ${realizedAnnualized >= 0 ? 'cell-pos' : 'cell-neg'}">${fmt(realizedAnnualized)}%</div></div>
      </div>
    </div>
    <div class="insample-card">
      <div class="insample-lbl">Sharpe Ratio</div>
      <div class="insample-row">
        <div class="insample-side"><div class="tag">FORECAST</div><div class="val">${fmt(forecastSharpe, 3)}</div></div>
        <div class="insample-arrow">→</div>
        <div class="insample-side"><div class="tag">REALIZED</div><div class="val ${realizedSharpe >= 0 ? 'cell-pos' : 'cell-neg'}">${fmt(realizedSharpe, 3)}</div></div>
      </div>
    </div>
    <div class="insample-card">
      <div class="insample-lbl">Training / Holdout Weeks</div>
      <div class="insample-row">
        <div class="insample-side"><div class="tag">IN-SAMPLE</div><div class="val">${data.in_sample.weeks}</div></div>
        <div class="insample-arrow">→</div>
        <div class="insample-side"><div class="tag">OUT-SAMPLE</div><div class="val">${data.out_sample.finx.weeks}</div></div>
      </div>
    </div>`;

  document.getElementById('valResults').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function renderEquityChart(curves) {
  killChart('equityChart');
  const datasets = [
    { label: 'FINX Portfolio', data: curves.finx,      borderColor: PALETTE[0], backgroundColor: PALETTE[0]+'20', borderWidth: 2.5, pointRadius: 0, tension: 0.25, fill: true },
    { label: 'Equal-weight',   data: curves.benchmark, borderColor: PALETTE[1], borderWidth: 1.8, pointRadius: 0, tension: 0.25, borderDash: [4,3] },
  ];
  if (curves.kse100) {
    datasets.push({ label: 'KSE-100', data: curves.kse100, borderColor: PALETTE[2], borderWidth: 1.5, pointRadius: 0, tension: 0.25, borderDash: [2,2] });
  }
  state.charts.equityChart = new Chart(document.getElementById('equityChart'), {
    type: 'line',
    data: { labels: curves.dates, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { intersect: false, mode: 'index' },
      plugins: {
        legend: { labels: { font: { family: 'JetBrains Mono', size: 11 }, boxWidth: 14 } },
        annotation: {
          annotations: {
            par: {
              type: 'line', yMin: 1, yMax: 1,
              borderColor: 'rgba(0,0,0,0.25)', borderWidth: 1, borderDash: [3,3],
              label: { content: 'Start = 1.0', display: true, position: 'end',
                       font: { size: 10, family: 'JetBrains Mono' } }
            }
          }
        }
      },
      scales: {
        x: { ticks: { font: { size: 10, family: 'JetBrains Mono' }, maxTicksLimit: 8 }, grid: { color: '#eee' } },
        y: { title: { display: true, text: 'Growth of $1' }, grid: { color: '#eee' } }
      }
    }
  });
}
