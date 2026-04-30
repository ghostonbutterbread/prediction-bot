# External Archive Import — Jon-Becker/prediction-market-analysis

Source repo: <https://github.com/Jon-Becker/prediction-market-analysis>

Imported source commit inspected: `fc43470d1a6e443fcd0d6d070dc43f1a0033ad1b`

Archive URL from upstream `scripts/download.sh`: <https://s3.jbecker.dev/data.tar.zst>

Local archive target:

```text
data/external/prediction_market_analysis/data/
```

`data/` is gitignored, so the large Parquet archive is intentionally kept out of git while remaining inside the prediction-bot repo workspace for training/import jobs.

## What the archive contains

Upstream docs describe a 36 GiB compressed archive with Parquet datasets:

```text
data/kalshi/markets/*.parquet
data/kalshi/trades/*.parquet
data/polymarket/markets/*.parquet
data/polymarket/trades/*.parquet
data/polymarket/blocks/*.parquet
data/polymarket/fpmm_trades/*.parquet
data/polymarket/fpmm_collateral_lookup.json
```

## Pricing fields we need

Kalshi markets include quote/last pricing:

- `yes_bid`, `yes_ask`, `no_bid`, `no_ask`, `last_price`
- `volume`, `volume_24h`, `open_interest`
- `result`, `created_time`, `open_time`, `close_time`, `_fetched_at`

Kalshi trades include execution pricing:

- `yes_price`, `no_price`, `count`, `taker_side`, `created_time`

Polymarket markets include current-ish market pricing:

- `outcome_prices` JSON string
- `volume`, `liquidity`, `active`, `closed`, `end_date`, `created_at`

Polymarket CTF/FPMM trade prices are not a single explicit price column; they are derivable from maker/taker asset amounts or collateral/outcome token amounts.

## Verification

Run this after the archive finishes extracting:

```bash
python3 scripts/inspect_prediction_market_archive.py
```

The verifier checks row counts, columns, price-field coverage, time ranges, and resolved-outcome availability.

## Initial comparison to Prediction Lab

### Upstream archive strengths

- Much deeper historical market/trade corpus than our live collector.
- Parquet storage is better for large analytical queries than JSONL.
- Includes both Kalshi and Polymarket.
- Includes trade-level data, not only market snapshots.
- Existing DuckDB analyses are immediately useful: calibration by price, maker/taker returns, category returns, longshot volume share, VWAP by hour, and market-type/category breakdowns.

### Prediction Lab strengths

- Stores our model/strategy decision context, not just market history.
- Records skipped/scored opportunities, confidence, edge, weather risk metadata, and hypothetical sizing fields.
- Has resolution logic tied to our replay/scoring model.
- Can run continuously against current markets with our exact strategy stack.

### Main gap/opportunity

The upstream archive is market/trade truth; Prediction Lab is strategy-observation truth. The useful merge is not replacing Prediction Lab — it is building an offline training/replay layer that joins historical truth from the archive with our strategy features and replay scoring.

## Good next ideas

1. Build a Parquet-backed `HistoricalMarketStore` for Kalshi archive reads.
2. Add an offline replay adapter that emits our internal `Market` objects from archive rows.
3. Use Kalshi trade data to estimate realistic slippage/spread/fill assumptions instead of flat assumptions.
4. Train calibration curves by category/price bucket: market price vs actual resolved win rate.
5. Build a “skipped winners” analysis: run our strategy over historical markets and compare skipped/accepted outcomes.
6. Compare maker-vs-taker edge by category to decide where live should avoid taker entries.
7. For Polymarket, derive execution prices from CTF/FPMM amount fields and normalize into a common trade schema later.

## Cautions

- Kalshi market rows are point-in-time fetch rows, not necessarily full tick-by-tick quote history unless repeated snapshots exist in the archive.
- Trade-level data gives realized prices and timing, but reconstructing the full order book at decision time may still require assumptions.
- Polymarket trade price normalization needs care because prices are derivable, not stored as a simple field.
