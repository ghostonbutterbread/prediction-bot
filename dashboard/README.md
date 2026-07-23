# Prediction Bot Dashboard

Read-only local dashboard for lane status, PnL summaries, blockers, and resolved-row filtering.

## Run

```bash
cd /mnt/data-collection/prediction-bot/dashboard
npm start
```

Open `http://127.0.0.1:4173`.

## Notes

- This dashboard does not mutate runtime state, trading config, wallets, or orders.
- Aggregate lane cards come from current PnL states and replay summaries under `data/derived_reports/`.
- The blocker card uses current-lane missing-fill blockers and the active resolver feed's unresolved market count.
- The bottom table is server-filtered from `data/summaries/source_router_shadow_resolved_rows_20260522T1609.jsonl`.
- Raw multi-GB lane ledgers are shown as file metadata only; they are not loaded in the browser.
