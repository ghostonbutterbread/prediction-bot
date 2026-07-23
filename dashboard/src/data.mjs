import { execFileSync } from 'node:child_process';
import { createReadStream, existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { createInterface } from 'node:readline';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
export let REPO_ROOT = path.resolve(__dirname, '..', '..');

/** Test-only override for isolated fixtures; production leaves the repository root unchanged. */
export function setRepoRootForTests(repoRoot) {
  REPO_ROOT = path.resolve(repoRoot);
}

const RELATIVE_PATHS = {
  collectorState: 'data/beta_shadow/paper/prediction_lab/state.json',
  sharedRuntime: 'data/beta_shadow/shared_market_runtime/runtime_state.json',
  riskState: 'data/beta_shadow/paper/risk_state.json',
  sourceRouterPnl: 'data/derived_reports/current_shadow_pnl/source_router_state.json',
  sourceScoreboardPnl: 'data/derived_reports/current_shadow_pnl/source_scoreboard_state.json',
  gateCompare: 'data/derived_reports/source_router_gate_cross_compare_20260614/summary.json',
  sourceRouterDecisions: 'data/beta_shadow/paper/source_router_low_sample/paper_shadow_lane_decisions.jsonl',
  sourceScoreboardDecisions: 'data/beta_shadow/paper/source_scoreboard/paper_shadow_lane_decisions.jsonl',
  resolutionFeedState: 'data/beta_shadow/resolution_feed/source_router_low_sample/state.json',
  resolutionFeedReport: 'data/beta_shadow/resolution_feed/source_router_low_sample/latest_resolutions.report.json',
  resolvedPnlSummary: 'data/summaries/source_router_shadow_resolved_pnl_20260522T1609.json',
  resolvedRows: 'data/summaries/source_router_shadow_resolved_rows_20260522T1609.jsonl',
  resolutions: 'data/beta_shadow/resolutions/latest_resolutions.jsonl',
};

export function repoPath(relativePath) {
  return path.join(REPO_ROOT, relativePath);
}

export function readJson(relativePath, fallback = null) {
  const fullPath = repoPath(relativePath);
  try {
    return JSON.parse(readFileSync(fullPath, 'utf8'));
  } catch {
    return fallback;
  }
}

export function fileMeta(relativePath) {
  const fullPath = repoPath(relativePath);
  try {
    const stat = statSync(fullPath);
    return {
      path: relativePath,
      exists: true,
      sizeBytes: stat.size,
      modifiedAt: stat.mtime.toISOString(),
      ageSeconds: Math.max(0, Math.round((Date.now() - stat.mtimeMs) / 1000)),
    };
  } catch {
    return { path: relativePath, exists: false, sizeBytes: 0, modifiedAt: null, ageSeconds: null };
  }
}

export function extractTopLevelObjectFromJson(relativePath, key) {
  const fullPath = repoPath(relativePath);
  let raw;
  try {
    raw = readFileSync(fullPath, 'utf8');
  } catch {
    return null;
  }

  const keyIndex = raw.indexOf(`"${key}"`);
  if (keyIndex === -1) return null;
  const colonIndex = raw.indexOf(':', keyIndex);
  const objectStart = raw.indexOf('{', colonIndex);
  if (colonIndex === -1 || objectStart === -1) return null;

  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let index = objectStart; index < raw.length; index += 1) {
    const char = raw[index];
    if (escaped) {
      escaped = false;
      continue;
    }
    if (char === '\\') {
      escaped = true;
      continue;
    }
    if (char === '"') {
      inString = !inString;
      continue;
    }
    if (inString) continue;
    if (char === '{') depth += 1;
    if (char === '}') {
      depth -= 1;
      if (depth === 0) {
        return JSON.parse(raw.slice(objectStart, index + 1));
      }
    }
  }
  return null;
}

export function normalizeLane(name, lane, source, extra = {}) {
  const buys = Number(lane?.buy_rows ?? lane?.rows ?? 0);
  const wins = Number(lane?.winning_buy_rows ?? 0);
  const losses = Number(lane?.losing_buy_rows ?? 0);
  const pnl = numberOrNull(lane?.total_pnl_usd);
  const stake = numberOrNull(lane?.total_stake_usd);
  const roi = numberOrNull(lane?.roi_pct);
  const winRate = numberOrNull(lane?.win_rate_pct) ?? (wins + losses > 0 ? (wins / (wins + losses)) * 100 : null);

  return {
    id: `${source}:${name}`,
    name,
    source,
    pnl,
    roi,
    stake,
    buys,
    wins,
    losses,
    winRate,
    balance: numberOrNull(lane?.balance_usd),
    balanceReturn: numberOrNull(lane?.balance_return_pct),
    resolvedRows: Number(lane?.resolved_rows ?? lane?.rows ?? 0),
    calculableRows: Number(lane?.pnl_calculable_rows ?? 0),
    blockedRows: sumObject(lane?.blocker_counts),
    blockerCounts: lane?.blocker_counts ?? {},
    skipRows: Number(lane?.skip_rows ?? 0),
    appliedRows: Number(lane?.applied_rows ?? 0),
    sideMix: lane?.side_mix ?? {},
    status: pnl == null ? 'watching' : pnl >= 0 ? 'positive' : 'negative',
    ...extra,
  };
}

export function buildLaneRows() {
  const rows = [];

  const sourceRouterLanes = extractTopLevelObjectFromJson(RELATIVE_PATHS.sourceRouterPnl, 'lanes') ?? {};
  for (const [name, lane] of Object.entries(sourceRouterLanes)) {
    rows.push(normalizeLane(name, lane, 'current source-router pnl', { family: 'current' }));
  }

  const sourceScoreboardLanes = extractTopLevelObjectFromJson(RELATIVE_PATHS.sourceScoreboardPnl, 'lanes') ?? {};
  for (const [name, lane] of Object.entries(sourceScoreboardLanes)) {
    rows.push(normalizeLane(name, lane, 'current source-scoreboard pnl', { family: 'current' }));
  }

  for (const sweep of loadAggregateSweepRows('data/derived_reports/source_router_low_edge_sweep_20260617', 'low-edge replay')) {
    rows.push(sweep);
  }
  for (const sweep of loadAggregateSweepRows('data/derived_reports/source_router_edge_sweep_20260616', 'high-edge replay')) {
    rows.push(sweep);
  }

  const gateCompare = readJson(RELATIVE_PATHS.gateCompare, null);
  for (const config of gateCompare?.configs ?? []) {
    if (config.latest_per_market) {
      rows.push(normalizeLane(config.name, config.latest_per_market, 'market-capped gates', {
        family: 'replay',
        configPath: config.config_path,
        gateMode: 'latest per market',
      }));
    }
  }

  const historicalSummary = readJson(RELATIVE_PATHS.resolvedPnlSummary, null);
  for (const [name, lane] of Object.entries(historicalSummary?.by_lane ?? {})) {
    rows.push(normalizeLane(name, lane, 'resolved row table summary', {
      family: 'historical',
      artifactPath: RELATIVE_PATHS.resolvedPnlSummary,
    }));
  }

  return dedupeRows(rows).sort((a, b) => (b.pnl ?? -Infinity) - (a.pnl ?? -Infinity));
}

export function buildDashboardData() {
  const collectorState = readJson(RELATIVE_PATHS.collectorState, {});
  const sharedRuntime = readJson(RELATIVE_PATHS.sharedRuntime, {});
  const riskState = readJson(RELATIVE_PATHS.riskState, {});
  const laneRows = buildLaneRows();
  const selectedRows = laneRows.filter((row) => row.pnl !== null);
  const currentRows = laneRows.filter((row) => row.family === 'current');
  const totalPnl = round2(selectedRows.reduce((total, row) => total + (row.pnl ?? 0), 0));
  const totalStake = round2(selectedRows.reduce((total, row) => total + (row.stake ?? 0), 0));
  const positiveRows = laneRows.filter((row) => (row.pnl ?? -1) > 0);
  const negativeRows = laneRows.filter((row) => (row.pnl ?? 1) < 0);
  const resolutionFeedState = readJson(RELATIVE_PATHS.resolutionFeedState, {});
  const resolutionFeedReport = readJson(RELATIVE_PATHS.resolutionFeedReport, {});

  return {
    generatedAt: new Date().toISOString(),
    repoRoot: REPO_ROOT,
    status: {
      collector: {
        runState: collectorState.run_state ?? 'unknown',
        lastCollectAt: collectorState.last_collect_at ?? null,
        lastError: collectorState.last_error ?? null,
        observerMode: Boolean(collectorState.observer_mode),
        tradingEnabled: Boolean(collectorState.trading_enabled),
        orderExecutionEnabled: Boolean(collectorState.order_execution_enabled),
        paused: Boolean(collectorState.paused),
        storageUsageGb: numberOrNull(collectorState.storage_usage_gb),
        storageCapGb: 100,
        activeGroup: collectorState.active_group ?? null,
        strategyVersion: collectorState.strategy_version ?? null,
      },
      sharedRuntime: {
        idle: Boolean(sharedRuntime.idle),
        publisher: sharedRuntime.publisher ?? null,
        latestSnapshot: sharedRuntime.latest_snapshot ?? null,
        consumerCount: Object.keys(sharedRuntime.consumers ?? {}).length,
        updatedAt: sharedRuntime.updated_at ?? null,
      },
      risk: {
        startingBalance: numberOrNull(riskState.starting_balance),
        currentBalance: numberOrNull(riskState.current_balance),
        availableCash: numberOrNull(riskState.available_cash),
        maxDrawdownHalt: Boolean(riskState.max_drawdown_halt),
        openPositions: Number(riskState.open_positions ?? 0),
        consecutiveLosses: Number(riskState.consecutive_losses ?? 0),
        tradingEnabled: Boolean(riskState.trading_enabled),
      },
    },
    summary: {
      totalPnl,
      totalStake,
      roi: totalStake > 0 ? round2((totalPnl / totalStake) * 100) : null,
      laneCount: laneRows.length,
      positiveLaneCount: positiveRows.length,
      negativeLaneCount: negativeRows.length,
      bestLane: laneRows.find((row) => row.pnl !== null) ?? null,
      worstLane: [...laneRows].reverse().find((row) => row.pnl !== null) ?? null,
      missingFillBlockers: sumBlocker(currentRows, 'missing_fill_price'),
      missingResolutionBlockers: sumBlocker(currentRows, 'missing_resolution'),
      loadedMissingFillBlockers: sumBlocker(laneRows, 'missing_fill_price'),
      loadedMissingResolutionBlockers: sumBlocker(laneRows, 'missing_resolution'),
      resolverResolvedMarkets: Number(resolutionFeedState.resolved_market_count ?? 0),
      resolverUnresolvedMarkets: Number(resolutionFeedState.unresolved_market_count ?? resolutionFeedReport.unresolved_market_count ?? 0),
      resolverLastRefreshAt: resolutionFeedState.last_refresh_at ?? null,
      resolverDecisionLedgers: resolutionFeedState.decision_ledger_paths ?? [resolutionFeedState.decision_ledger_path].filter(Boolean),
    },
    files: Object.fromEntries(Object.entries(RELATIVE_PATHS).map(([key, value]) => [key, fileMeta(value)])),
    lanes: laneRows,
    presets: [
      { id: 'all', label: 'All lanes' },
      { id: 'top-earners', label: 'Top earners' },
      { id: 'lowest-earners', label: 'Lowest earners' },
      { id: 'positive', label: 'Positive only' },
      { id: 'missing-fill', label: 'Missing fills' },
      { id: 'current', label: 'Current states' },
      { id: 'replay', label: 'Replay lanes' },
    ],
  };
}

export function runMonitorSnapshot() {
  const configPath = 'data/runtime_configs/paper_source_router_shared_shadow_20260608.yaml';
  try {
    const output = execFileSync('python3', ['scripts/prediction_lab_monitor.py', '--config', configPath, '--json'], {
      cwd: REPO_ROOT,
      encoding: 'utf8',
      timeout: 8000,
      maxBuffer: 1024 * 1024,
    });
    return JSON.parse(output);
  } catch (error) {
    return {
      healthy: false,
      summary: error.message,
      issues: [{ code: 'monitor_unavailable', message: error.message, severity: 'warning' }],
    };
  }
}

export async function buildTradeRows({ lanes = [], preset = 'latest', search = '', limit = 100 } = {}) {
  const fullPath = repoPath(RELATIVE_PATHS.resolvedRows);
  if (!existsSync(fullPath)) {
    return { rows: [], totalScanned: 0, totalMatched: 0, sourcePath: RELATIVE_PATHS.resolvedRows };
  }

  const selectedLanes = new Set(lanes.filter(Boolean));
  const normalizedSearch = search.trim().toLowerCase();
  const maxRows = Math.max(1, Math.min(Number(limit) || 100, 1000));
  const rows = [];
  const compareRows = sortForPreset(preset);
  let totalScanned = 0;
  let totalMatched = 0;

  const lineReader = createInterface({
    input: createReadStream(fullPath, { encoding: 'utf8' }),
    crlfDelay: Infinity,
  });

  for await (const line of lineReader) {
    if (!line.trim()) continue;
    totalScanned += 1;
    let raw;
    try {
      raw = JSON.parse(line);
    } catch {
      continue;
    }
    const row = normalizeTradeRow(raw);
    if (selectedLanes.size > 0 && !selectedLanes.has(row.lane)) continue;
    if (!matchesPreset(row, preset)) continue;
    if (normalizedSearch && !row.searchText.includes(normalizedSearch)) continue;
    totalMatched += 1;
    addBoundedRow(rows, row, compareRows, maxRows);
  }

  rows.sort(compareRows);
  return {
    rows: rows.map(({ searchText: _searchText, ...row }) => row),
    totalScanned,
    totalMatched,
    sourcePath: RELATIVE_PATHS.resolvedRows,
  };
}

function addBoundedRow(rows, row, compareRows, maxRows) {
  if (rows.length < maxRows) {
    rows.push(row);
    return;
  }

  let worstIndex = 0;
  for (let index = 1; index < rows.length; index += 1) {
    if (compareRows(rows[index], rows[worstIndex]) > 0) {
      worstIndex = index;
    }
  }

  if (compareRows(row, rows[worstIndex]) < 0) {
    rows[worstIndex] = row;
  }
}

function loadAggregateSweepRows(relativeDir, source) {
  const fullDir = repoPath(relativeDir);
  if (!existsSync(fullDir)) return [];
  const rows = [];
  for (const entry of readdirSync(fullDir, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const relativePath = path.join(relativeDir, entry.name, 'aggregate_summary.json');
    const data = readJson(relativePath, null);
    if (!data) continue;
    rows.push(normalizeLane(data.composition ?? entry.name, data, source, {
      family: 'replay',
      artifactPath: relativePath,
    }));
  }
  return rows;
}

function normalizeTradeRow(row) {
  const pnl = numberOrNull(row?.pnl?.pnl_usd);
  const stake = numberOrNull(row?.pnl?.stake_usd);
  const roi = stake && pnl !== null ? (pnl / stake) * 100 : null;
  const market = row.market_id ?? row?.resolution?.market_id ?? '';
  const lane = row.lane_id ?? 'unknown';
  const action = row.action ?? 'UNKNOWN';
  const side = row.side ?? sideFromAction(action);
  const status = row.blocker ? 'blocked' : row?.pnl?.won === true ? 'won' : row?.pnl?.won === false ? 'lost' : 'open';

  const normalized = {
    id: row.lane_decision_id ?? `${lane}:${market}:${row.observed_at ?? ''}`,
    lane,
    market,
    observedAt: row.observed_at ?? null,
    action,
    side,
    reason: row.reason_code ?? row.reason ?? '',
    confidence: numberOrNull(row.confidence),
    entryPrice: numberOrNull(row.entry_price),
    fillPrice: numberOrNull(row.fill_price),
    stake,
    pnl,
    roi,
    won: row?.pnl?.won ?? null,
    status,
    blocker: row.blocker ?? null,
    outcome: row?.resolution?.outcome ?? null,
    resolvedAt: row?.resolution?.resolved_at ?? null,
    sharedCandidateId: row?.resolution?.shared_candidate_id ?? row.shared_candidate_id ?? null,
  };

  normalized.searchText = [
    normalized.lane,
    normalized.market,
    normalized.action,
    normalized.side,
    normalized.reason,
    normalized.outcome,
    normalized.status,
    normalized.blocker,
    normalized.sharedCandidateId,
  ]
    .filter(Boolean)
    .join(' ')
    .toLowerCase();

  return normalized;
}

function sideFromAction(action) {
  if (String(action).includes('YES')) return 'YES';
  if (String(action).includes('NO')) return 'NO';
  return '';
}

function matchesPreset(row, preset) {
  switch (preset) {
    case 'top-earners':
    case 'lowest-earners':
    case 'best-roi':
    case 'worst-roi':
      return row.blocker === null && row.pnl !== null;
    case 'winners':
      return row.won === true;
    case 'losers':
      return row.won === false;
    case 'blockers':
      return row.blocker !== null;
    case 'buys':
      return String(row.action).startsWith('BUY');
    case 'skips':
      return row.action === 'SKIP';
    default:
      return true;
  }
}

function sortForPreset(preset) {
  if (preset === 'lowest-earners') return (a, b) => (a.pnl ?? Infinity) - (b.pnl ?? Infinity);
  if (preset === 'best-roi') return (a, b) => (b.roi ?? -Infinity) - (a.roi ?? -Infinity);
  if (preset === 'worst-roi') return (a, b) => (a.roi ?? Infinity) - (b.roi ?? Infinity);
  if (preset === 'top-earners') return (a, b) => (b.pnl ?? -Infinity) - (a.pnl ?? -Infinity);
  return (a, b) => String(b.observedAt ?? '').localeCompare(String(a.observedAt ?? ''));
}

function dedupeRows(rows) {
  const seen = new Set();
  return rows.filter((row) => {
    if (seen.has(row.id)) return false;
    seen.add(row.id);
    return true;
  });
}

function numberOrNull(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function sumObject(value) {
  if (!value || typeof value !== 'object') return 0;
  return Object.values(value).reduce((total, item) => total + Number(item || 0), 0);
}

function sumBlocker(rows, key) {
  return rows.reduce((total, row) => total + Number(row.blockerCounts?.[key] ?? 0), 0);
}

function round2(value) {
  return Math.round(Number(value || 0) * 100) / 100;
}
