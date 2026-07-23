import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test, { afterEach, beforeEach } from 'node:test';
import {
  buildDashboardData,
  buildLaneRows,
  buildTradeRows,
  extractTopLevelObjectFromJson,
  setRepoRootForTests,
} from '../src/data.mjs';

let fixtureRoot;

function writeJson(relativePath, value) {
  const target = path.join(fixtureRoot, relativePath);
  mkdirSync(path.dirname(target), { recursive: true });
  writeFileSync(target, JSON.stringify(value), 'utf8');
}

function writeJsonl(relativePath, rows) {
  const target = path.join(fixtureRoot, relativePath);
  mkdirSync(path.dirname(target), { recursive: true });
  writeFileSync(target, rows.map((row) => JSON.stringify(row)).join('\n') + '\n', 'utf8');
}

beforeEach(() => {
  fixtureRoot = mkdtempSync(path.join(tmpdir(), 'prediction-bot-dashboard-'));
  setRepoRootForTests(fixtureRoot);
  writeJson('data/derived_reports/current_shadow_pnl/source_router_state.json', {
    lanes: {
      shadow_source_router: {
        buy_rows: 3,
        winning_buy_rows: 2,
        losing_buy_rows: 1,
        total_pnl_usd: 4.5,
        total_stake_usd: 30,
        roi_pct: 15,
      },
    },
  });
  writeJson('data/derived_reports/current_shadow_pnl/source_scoreboard_state.json', {
    lanes: {
      shadow_source_scoreboard: {
        buy_rows: 2,
        winning_buy_rows: 1,
        losing_buy_rows: 1,
        total_pnl_usd: -1,
        total_stake_usd: 20,
        roi_pct: -5,
      },
    },
  });
  writeJson('data/beta_shadow/paper/prediction_lab/state.json', { run_state: 'observer', observer_mode: true });
  writeJsonl('data/summaries/source_router_shadow_resolved_rows_20260522T1609.jsonl', [{
    lane_id: 'shadow_source_router',
    market_id: 'KXTEST-1',
    observed_at: '2026-07-01T00:00:00Z',
    action: 'BUY_NO',
    entry_price: 0.6,
    resolution: { outcome: 'NO' },
    pnl: { pnl_usd: 4, stake_usd: 10, won: true },
  }]);
});

afterEach(() => {
  rmSync(fixtureRoot, { recursive: true, force: true });
});

test('extracts lane objects without requiring unrelated runtime artifacts', () => {
  const lanes = extractTopLevelObjectFromJson('data/derived_reports/current_shadow_pnl/source_router_state.json', 'lanes');
  assert.ok(lanes);
  assert.ok(Object.hasOwn(lanes, 'shadow_source_router'));
});

test('builds normalized lane rows from isolated current-state fixtures', () => {
  const rows = buildLaneRows();
  assert.equal(rows.length, 2);
  assert.ok(rows.some((row) => row.name === 'shadow_source_router'));
  assert.ok(rows.every((row) => typeof row.id === 'string' && row.id.length > 0));
});

test('dashboard payload includes status, summary, presets, and lanes', () => {
  const payload = buildDashboardData();
  assert.ok(payload.generatedAt);
  assert.equal(payload.status.collector.runState, 'observer');
  assert.equal(payload.summary.laneCount, 2);
  assert.ok(payload.presets.some((preset) => preset.id === 'top-earners'));
  assert.equal(payload.lanes.length, 2);
});

test('builds paginated bottom-table rows from an isolated resolved-row fixture', async () => {
  const payload = await buildTradeRows({ preset: 'top-earners', limit: 5 });
  assert.equal(payload.totalScanned, 1);
  assert.equal(payload.rows.length, 1);
  assert.equal(payload.rows[0].lane, 'shadow_source_router');
  assert.equal(payload.rows[0].market, 'KXTEST-1');
});
