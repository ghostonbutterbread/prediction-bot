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
- **Price signal (40%)** — Order book imbalance analysis
- **News signal (30%)** — Google News RSS sentiment scoring
- **Volume signal (15%)** — Market liquidity/efficiency
- **Time signal (15%)** — Days to resolution factor

### Position Sizing
Uses **half-Kelly Criterion** for mathematically optimal bet sizing:
- Calculates edge from model probability vs market price
- Scales position by edge strength
- Capped at 10% of balance per trade

### Risk Controls
- Minimum 5% edge required
- Minimum 50% confidence threshold
- Maximum 10% portfolio per position
- All trades logged to SQLite

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
