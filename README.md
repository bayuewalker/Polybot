# 🤖 PolyBot — Polymarket Automated Trading Bot

> **Automated prediction market trading bot with real-time dashboard, Telegram alerts, and 5 AI-powered strategies.**

[![Version](https://img.shields.io/badge/version-v4.2-00d4aa?style=flat-square)](.)
[![Python](https://img.shields.io/badge/python-3.11+-blue?style=flat-square)](.)
[![Railway](https://img.shields.io/badge/hosted-Railway-blueviolet?style=flat-square)](https://railway.app)
[![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)](.)

---

## 📸 Preview

| Home Dashboard | Live Signals | Trades |
|---|---|---|
| Real-time portfolio & P&L | Live signals from bot | Open positions with SL/TP |

---

## ✨ Features

- **5 Trading Strategies** — Arbitrage, News Correlation, Mean Reversion, Volume Spike, Closing Soon
- **Real-time Dashboard** — Portfolio, P&L, Win Rate, Positions — all live, zero fake data
- **Telegram Alerts** — Signal found, trade opened/closed, daily summary with wallet address
- **User Settings from Frontend** — Private key, risk management, TP/SL — no Railway config needed
- **Smart Risk Management** — Kelly sizing, stop loss, daily loss limit, max exposure
- **Cross-device Settings** — Settings saved server-side, auto-load on any device
- **Bot runs 24/7 on Railway** — Closes browser? Bot keeps running
- **Paper Trading Mode** — Works without a wallet (no real trades)

---

## 🏗️ Architecture

```
Railway (Backend 24/7)          Cloudflare Pages (Frontend)
┌─────────────────────┐         ┌──────────────────────────┐
│  polybot.py         │◄────────│  index.html              │
│  ├── 5 Strategies   │  REST   │  ├── Portfolio dashboard  │
│  ├── Risk Manager   │  API    │  ├── Live signals         │
│  ├── Telegram Bot   │         │  ├── Open positions       │
│  ├── Web API        │         │  ├── Settings page        │
│  └── USDC Balance   │         │  └── Start/Stop controls  │
└─────────────────────┘         └──────────────────────────┘
         │
         ▼
    Polymarket CLOB API
    Polygon Blockchain
    Telegram Bot API
```

---

## 🚀 Quick Start

### 1. Clone the repo
```bash
git clone https://github.com/bayuewalker/Polybot.git
cd Polybot
```

### 2. Deploy backend to Railway

1. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
2. Select this repo
3. Add environment variables (see below)
4. Railway auto-deploys on every push ✅

### 3. Deploy frontend to Cloudflare Pages

1. Go to [pages.cloudflare.com](https://pages.cloudflare.com) → New project → Connect GitHub
2. Select this repo
3. Leave all build settings **empty**
4. Deploy ✅

### 4. Connect your wallet

1. Open your Cloudflare Pages URL
2. Tap **More → Settings**
3. Enter your Polygon wallet private key
4. Set your risk settings
5. Tap **Save & Apply**
6. Tap **▶ Run Bot**

---

## ⚙️ Environment Variables

Set these in Railway → Variables:

| Variable | Required | Description | Example |
|---|---|---|---|
| `TELEGRAM_TOKEN` | ✅ | Bot token from @BotFather | `123456:ABC...` |
| `TELEGRAM_CHAT_ID` | ✅ | Group chat ID(s) — comma separated | `-1001234567890` |
| `POLY_PRIVATE_KEY` | ⚠️ | Polygon wallet private key (or set via dashboard) | `0x...` |
| `POLYGON_RPC` | Optional | Custom RPC URL | `https://polygon-rpc.com` |
| `MAX_POSITION_USD` | Optional | Max $ per trade (default: 500) | `10` |
| `DAILY_LOSS_USD` | Optional | Daily loss limit (default: 100) | `50` |
| `MIN_LIQUIDITY` | Optional | Min market liquidity (default: 5000) | `1000` |
| `SIGNAL_COOLDOWN` | Optional | Seconds between same signal (default: 3600) | `3600` |
| `BOT_SETTINGS` | Optional | JSON settings override (for persistence) | `{"maxPosition":"10"}` |

> **Note:** Most settings can be configured directly from the frontend dashboard. Railway env vars are mainly for admin settings (Telegram, etc.)

---

## 📐 Trading Strategies

### ⚡ Arbitrage Scanner
Spots price gaps between Polymarket and external probability estimates. Trades when gap exceeds minimum edge threshold.

**Trigger:** External prob 62% vs Polymarket 50% → BUY YES at 50¢

### 📰 News Correlation
Analyzes news sentiment keywords and compares to market price. Fades divergence between news and price.

**Trigger:** "Fed rate cut" news → sentiment +70% vs market at 40% → BUY

### 📊 Mean Reversion
Detects when price moves 2+ standard deviations from its rolling average and fades the extreme move.

**Trigger:** Price spikes to 2.2σ above 7-day average → SELL

### 🔊 Volume Spike
Detects 3x+ unusual volume surges suggesting informed trading. Follows the direction.

**Trigger:** Normal vol $10k/day, today $47k → smart money detected → follow

### ⏰ Closing Soon
Targets markets resolving in 6–48 hours with strong directional momentum.

**Trigger:** Market resolves in 8 hours, price at 72% → BUY YES, hold to $1.00

---

## 🛡️ Risk Management

| Setting | Default | Description |
|---|---|---|
| Max Position Size | $500 | Maximum USDC per single trade |
| Daily Loss Limit | $100 | Bot stops if daily loss hits this |
| Stop Loss | 20% | Auto-close if price drops 20% from entry |
| Take Profit | None | Hold to market resolution (optional: set %) |
| Max Exposure | 50% | Max % of portfolio deployed at once |
| Max Positions | 10 | Maximum simultaneous open trades |
| Min Confidence | 55% | Minimum signal confidence to trade |
| Min Edge | 5% | Minimum price gap required |
| Kelly Fraction | 25% | Position sizing multiplier |

---

## 🔌 API Endpoints

The bot exposes a REST API on port 8080:

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Bot status, portfolio, P&L, win rate |
| `GET` | `/signals` | Latest trading signals |
| `GET` | `/positions` | Open positions with P&L |
| `GET` | `/balance` | Force sync real USDC balance |
| `GET` | `/settings` | Current bot settings (private key masked) |
| `POST` | `/settings` | Update settings (reloads bot live) |
| `POST` | `/start` | Resume trading |
| `POST` | `/stop` | Pause trading |

---

## 📲 Telegram Commands

| Command | Description |
|---|---|
| `/status` | Portfolio status, P&L, scan count |
| `/positions` | All open positions |
| `/signals` | Latest signals |
| `/leaderboard` | Top signals ranked by edge |
| `/pnl` | P&L report |
| `/stop` | Pause bot |
| `/resume` | Resume bot |
| `/help` | Command list |

---

## 📁 File Structure

```
Polybot/
├── polybot.py          # Backend bot (deploy to Railway)
├── index.html          # Frontend dashboard (deploy to Cloudflare Pages)
├── requirements.txt    # Python dependencies
├── Procfile            # Railway process config
└── README.md           # This file
```

---

## 📦 Requirements

```txt
aiohttp==3.9.5
python-dotenv==1.0.1
web3==6.20.0
python-telegram-bot==21.6
numpy==2.1.0
```

---

## 🔒 Security Notes

- **Never share your private key** — it's only stored locally in your browser
- The dashboard **never sends your private key to any server** except Railway (which you control)
- The per-wallet settings store on Railway **excludes** the private key
- The bot wallet address (public) is shown in Telegram — this is safe

---

## 📊 Version History

| Version | Changes |
|---|---|
| **v4.2** | Fixed crash bug, SL/TP for NO positions, async balance fetch, Telegram conflict fix |
| **v4.1** | Real-time stats, cross-device settings, position sync, no fake data |
| **v4.0** | Web API, frontend settings, start/stop control, per-wallet storage |
| **v3.0** | Signal cooldown, multi-group Telegram, 5 strategies, leaderboard |
| **v2.1** | Initial release |

---

## ⚠️ Disclaimer

> This bot is for **educational purposes**. Prediction market trading involves risk. Always start with small amounts. Past signal performance does not guarantee future results. The authors are not responsible for any financial losses.

---

## 📄 License

MIT License — feel free to fork and modify.

---

<div align="center">
  Made with ❤️ for Polymarket traders
  <br>
  <b>PolyBot v4.2</b> — Trade smarter, not harder
</div>
