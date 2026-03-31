# Prediction Market Trading Bot 🤖

Multi-exchange prediction market trading bot with news sentiment analysis, Kelly Criterion position sizing, and modular strategy engine.

## Supported Exchanges

| Exchange | Status | API |
|----------|--------|-----|
| **Kalshi** | ✅ Ready | RSA-PSS signing |
| Polymarket | 🔄 Planned | EIP-712 signing |

## Features

- **Multi-signal strategy engine** — price mispricing + news sentiment + volume analysis + time decay
- **News-reactive trading** — Google News RSS integration with sentiment scoring
- **Kelly Criterion** — mathematically optimal position sizing
- **Multi-exchange** — trade the same market across platforms
- **Risk management** — edge thresholds, confidence gates, position limits
- **SQLite logging** — every scan, signal, and trade recorded

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy and configure
cp .env.example .env
# Edit .env with your Kalshi API credentials

# 3. Place your Kalshi private key
# Download from Kalshi → Settings → API
# Save as `kalshi_private_key` (no extension) in project root

# 4. Run
python main.py demo      # Demo mode (safe, test data)
python main.py paper     # Paper trading (live data, no real orders)
python main.py live      # Live trading (real money!)
```

## Commands

| Command | Description |
|---------|-------------|
| `python main.py demo` | Demo mode with test account |
| `python main.py paper` | Paper trade on live markets |
| `python main.py live [interval]` | Live trading (default: 120s intervals) |
| `python main.py markets` | Browse active markets |
| `python main.py news <query>` | Test news feed for a topic |

## Architecture

```
┌──────────────────────────────────────────┐
│              Prediction Bot               │
├──────────────────────────────────────────┤
│                                          │
│  ┌──────────┐  ┌──────────────────────┐  │
│  │  Kalshi  │  │   Strategy Engine    │  │
│  │ Exchange │──│  • Price mispricing  │  │
│  └──────────┘  │  • News sentiment   │  │
│                │  • Volume analysis   │  │
│  ┌──────────┐  │  • Time decay       │  │
│  │Polymarket│  └──────────────────────┘  │
│  │(planned) │         │                  │
│  └──────────┘         ▼                  │
│              ┌──────────────────────┐    │
│              │   Kelly Sizer        │    │
│              │   (position sizing)  │    │
│              └──────────────────────┘    │
│                       │                  │
│              ┌──────────────────────┐    │
│              │   News Feed          │    │
│              │   (RSS + sentiment)  │    │
│              └──────────────────────┘    │
└──────────────────────────────────────────┘
```

## Strategy Details

### Signal Ensemble (weighted)
- **Price signal (40%)** — Order book imbalance + longshot/volume bias
- **Live data (50%)** — Real weather forecasts, crypto prices, forex rates (when applicable)
- **News signal (15%)** — Yahoo Finance / Bing News RSS sentiment scoring with fallback chain
- **Volume signal (15%)** — Market liquidity/efficiency
- **Time signal (10%)** — Days to resolution factor

When the news feed is unavailable, its weight is redistributed proportionally to the remaining active signals (not zeroed out).

### Position Sizing
Uses **half-Kelly Criterion** for mathematically optimal bet sizing:
- Kalshi fee (7% on winnings) is deducted from expected value before computing Kelly fraction
- Scales position by edge strength after fees
- Capped at 10% of balance per trade (paper) / 5% (live)

### Risk Controls
- Minimum 5% edge required (configurable)
- Minimum 50% confidence threshold
- Maximum 10% portfolio per position
- **Session kill-switch**: permanently halts if balance drops >20% from high-water mark
- Consecutive-loss cooldown, daily loss limit, max drawdown pause
- All trades saved to JSON session files

## Environment Variables Reference

### Kalshi credentials
| Variable | Default | Description |
|----------|---------|-------------|
| `KALSHI_API_KEY_ID` | — | Your Kalshi API key ID |
| `KALSHI_PRIVATE_KEY_PATH` | `kalshi_private_key` | Path to RSA private key file |
| `KALSHI_USE_DEMO` | `true` | `true` = demo account, `false` = real money |
| `KALSHI_FEE_RATE` | `0.07` | Kalshi fee on winnings (7%). Deducted from Kelly EV before sizing. |

### Strategy
| Variable | Default | Description |
|----------|---------|-------------|
| `MIN_EDGE` | `0.015` | Minimum edge to enter a trade (e.g. `0.05` = 5%) |
| `MIN_CONFIDENCE` | `0.50` | Minimum composite signal confidence |
| `NEWS_WEIGHT` | `0.15` | Weight of news signal in ensemble |
| `ENABLE_NEWS_FALLBACK` | `true` | Enable Yahoo Finance / Bing News RSS fallback when primary news fails. If all sources fail, the bot degrades to price+volume signals only and redistributes the news weight. |

### Risk management
| Variable | Default | Description |
|----------|---------|-------------|
| `KELLY_FRACTION` | `0.5` | Kelly multiplier (0.5 = half-Kelly) |
| `MAX_POSITION_PCT` | `0.10` | Max % of balance per trade |
| `DAILY_LOSS_LIMIT_PCT` | `20` | Stop new trades if down this % today |
| `MAX_DRAWDOWN_PCT` | `0.20` | **Session kill-switch**: halt permanently if balance drops this fraction below `max(session_start, session_peak)`. Requires manual reset. |
| `FORCE_RESUME` | `false` | Set to `true` to clear the max-drawdown halt without deleting `data/risk_state.json` |
| `MAX_OPEN_POSITIONS` | `10` | Max concurrent open positions |

### Paper trading
| Variable | Default | Description |
|----------|---------|-------------|
| `PAPER_SCAN_INTERVAL` | `120` | Seconds between scan cycles |
| `STARTING_BALANCE` | `100.0` | Virtual starting balance for paper trading |
| `PAPER_LOG_FILE` | — | Override log file path (supports per-instance logging) |

## Getting Kalshi API Keys

1. Create account at [kalshi.com](https://kalshi.com) (or [demo.kalshi.co](https://demo.kalshi.co) for testing)
2. Go to **Settings → API**
3. Generate API key (you get a Key ID + download private key)
4. Save Key ID in `.env` as `KALSHI_API_KEY_ID`
5. Save private key file as `kalshi_private_key` in project root

## Roadmap

- [ ] Polymarket exchange adapter
- [ ] Cross-market arbitrage (Kalshi vs Polymarket)
- [ ] WebSocket real-time data feeds
- [ ] ML probability model (logistic regression → XGBoost)
- [ ] Historical backtesting framework
- [ ] Streamlit dashboard

## Risk Warning

⚠️ This is experimental software. Prediction market trading involves risk of loss. Start with demo mode, then paper trade, then tiny real positions. Never trade more than you can afford to lose.

## Project Structure

```
prediction-bot/
├── main.py                 # CLI entry point
├── .env                    # API keys and config
├── bot/
│   ├── exchanges/
│   │   ├── base.py         # Exchange interface
│   │   └── kalshi.py       # Kalshi adapter
│   ├── strategies/
│   │   └── enhanced.py     # Multi-signal strategy + Kelly
│   ├── feeds/
│   │   └── news.py         # News feed + sentiment
│   └── runner.py           # Main bot orchestration
├── data/                   # Trade logs (auto-created)
└── README.md
```
