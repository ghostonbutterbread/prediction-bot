const state = {
  data: null,
  selectedLanes: new Set(),
  lanePreset: 'all',
  rowPreset: 'latest',
  rowSearch: '',
};

const currency = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 });
const number = new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 });

document.getElementById('refreshButton').addEventListener('click', () => loadDashboard());
document.getElementById('rowPreset').addEventListener('change', (event) => {
  state.rowPreset = event.target.value;
  loadRows();
});
document.getElementById('rowSearch').addEventListener('input', debounce((event) => {
  state.rowSearch = event.target.value;
  loadRows();
}, 350));

loadDashboard();
setInterval(loadDashboard, 60000);

async function loadDashboard() {
  const response = await fetch('/api/dashboard');
  state.data = await response.json();
  renderDashboard();
  await loadRows();
}

async function loadRows() {
  const params = new URLSearchParams({
    preset: state.rowPreset,
    search: state.rowSearch,
    limit: '100',
  });
  for (const lane of activeLaneNames()) params.append('lane', lane);
  const table = document.getElementById('rowTable');
  table.innerHTML = '<tr><td colspan="10">Loading rows...</td></tr>';
  const response = await fetch(`/api/rows?${params.toString()}`);
  const payload = await response.json();
  renderRows(payload);
}

function renderDashboard() {
  const data = state.data;
  document.getElementById('lastUpdated').textContent = `Updated ${formatTime(data.generatedAt)}`;
  document.getElementById('collectorState').textContent = data.status.collector.runState;
  document.getElementById('collectorDetail').textContent = data.status.collector.tradingEnabled
    ? 'Trading enabled'
    : 'Observer / no order execution';
  document.getElementById('storageMetric').textContent = `${formatNumber(data.status.collector.storageUsageGb)} GB`;
  document.getElementById('storageDetail').textContent = `Cap ${formatNumber(data.status.collector.storageCapGb)} GB`;
  document.getElementById('pnlMetric').textContent = formatMoney(data.summary.totalPnl);
  document.getElementById('roiMetric').textContent = `${formatNumber(data.summary.roi)}% ROI across loaded summaries`;
  document.getElementById('blockerMetric').textContent = formatNumber(data.summary.missingFillBlockers);
  document.getElementById('blockerDetail').textContent =
    `${formatNumber(data.summary.resolverUnresolvedMarkets)} unresolved markets in resolver feed`;
  document.getElementById('summaryCounts').textContent =
    `${data.summary.positiveLaneCount} positive / ${data.summary.negativeLaneCount} negative`;
  renderLaneChips(data);
  renderLaneChart(filteredLanes());
}

function renderLaneChips(data) {
  const container = document.getElementById('laneChips');
  const presets = [
    ['all', 'All'],
    ['top-earners', 'Top earners'],
    ['lowest-earners', 'Lowest earners'],
    ['positive', 'Positive'],
    ['missing-fill', 'Missing fills'],
    ['current', 'Current'],
    ['replay', 'Replay'],
  ];
  const lanes = [...new Set(data.lanes.map((lane) => lane.name))].sort();
  container.innerHTML = [
    ...presets.map(([id, label]) => chip(label, state.lanePreset === id, () => {
      state.lanePreset = id;
      state.selectedLanes.clear();
      renderDashboard();
      loadRows();
    })),
    ...lanes.map((lane) => chip(lane, state.selectedLanes.has(lane), () => {
      state.lanePreset = 'custom';
      if (state.selectedLanes.has(lane)) state.selectedLanes.delete(lane);
      else state.selectedLanes.add(lane);
      renderDashboard();
      loadRows();
    })),
  ].join('');

  container.querySelectorAll('button').forEach((button) => {
    button.addEventListener('click', chipHandlers.get(button.dataset.key));
  });
}

const chipHandlers = new Map();
let chipKey = 0;
function chip(label, active, handler) {
  const key = `chip-${chipKey++}`;
  chipHandlers.set(key, handler);
  return `<button class="chip ${active ? 'active' : ''}" data-key="${key}" type="button">${escapeHtml(label)}</button>`;
}

function filteredLanes() {
  const lanes = state.data.lanes;
  if (state.selectedLanes.size) return lanes.filter((lane) => state.selectedLanes.has(lane.name));
  switch (state.lanePreset) {
    case 'top-earners':
      return [...lanes].filter((lane) => lane.pnl !== null).sort((a, b) => b.pnl - a.pnl).slice(0, 8);
    case 'lowest-earners':
      return [...lanes].filter((lane) => lane.pnl !== null).sort((a, b) => a.pnl - b.pnl).slice(0, 8);
    case 'positive':
      return lanes.filter((lane) => (lane.pnl ?? -1) > 0);
    case 'missing-fill':
      return lanes.filter((lane) => (lane.blockerCounts?.missing_fill_price ?? 0) > 0);
    case 'current':
      return lanes.filter((lane) => lane.family === 'current');
    case 'replay':
      return lanes.filter((lane) => lane.family === 'replay');
    default:
      return lanes.slice(0, 14);
  }
}

function activeLaneNames() {
  if (state.selectedLanes.size) return [...state.selectedLanes];
  return [];
}

function renderLaneChart(lanes) {
  const container = document.getElementById('laneChart');
  const maxAbs = Math.max(1, ...lanes.map((lane) => Math.abs(lane.pnl ?? 0)));
  container.innerHTML = lanes.map((lane) => {
    const width = Math.max(3, Math.round((Math.abs(lane.pnl ?? 0) / maxAbs) * 100));
    const positive = (lane.pnl ?? 0) >= 0;
    return `
      <div class="bar-row">
        <div class="bar-label" title="${escapeHtml(lane.name)}">${escapeHtml(lane.name)}</div>
        <div class="bar-track">
          <div class="bar ${positive ? 'positive' : 'negative'}" style="width:${width}%"></div>
        </div>
        <div class="bar-value ${positive ? 'good' : 'bad'}">${formatMoney(lane.pnl)}</div>
      </div>`;
  }).join('') || '<p class="empty">No lanes match this filter.</p>';
}

function renderRows(payload) {
  document.getElementById('tableSource').textContent =
    `${formatNumber(payload.totalMatched)} matched / ${formatNumber(payload.totalScanned)} scanned from ${payload.sourcePath}`;
  document.getElementById('rowTable').innerHTML = payload.rows.map((row) => `
    <tr>
      <td>${escapeHtml(row.lane)}</td>
      <td title="${escapeHtml(row.market)}">${escapeHtml(row.market)}</td>
      <td>${escapeHtml(row.action)}</td>
      <td>${escapeHtml(row.side ?? '')}</td>
      <td>${formatPrice(row.fillPrice)}</td>
      <td>${formatMoney(row.stake)}</td>
      <td>${escapeHtml(row.outcome ?? '')}</td>
      <td class="${(row.pnl ?? 0) >= 0 ? 'good' : 'bad'}">${formatMoney(row.pnl)}</td>
      <td>${formatNumber(row.roi)}%</td>
      <td><span class="status ${row.status}">${escapeHtml(row.blocker ?? row.status)}</span></td>
    </tr>
  `).join('') || '<tr><td colspan="10">No rows match this filter.</td></tr>';
}

function formatMoney(value) {
  return value === null || value === undefined ? 'n/a' : currency.format(value);
}

function formatNumber(value) {
  return value === null || value === undefined || Number.isNaN(Number(value)) ? 'n/a' : number.format(value);
}

function formatPrice(value) {
  return value === null || value === undefined ? 'n/a' : Number(value).toFixed(2);
}

function formatTime(value) {
  return new Date(value).toLocaleString();
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#039;',
  }[char]));
}

function debounce(fn, wait) {
  let timeout;
  return (...args) => {
    clearTimeout(timeout);
    timeout = setTimeout(() => fn(...args), wait);
  };
}
