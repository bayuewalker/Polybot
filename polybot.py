"""
PolyBot — Polymarket Automated Trading Bot
==========================================
Full backend: market scanning, signal detection, trade execution,
risk management, and Telegram alerts.

Requirements:
    pip install py-clob-client python-telegram-bot web3 aiohttp
    pip install pandas numpy scikit-learn python-dotenv loguru schedule

Usage:
    1. Copy .env.example to .env and fill in your keys
    2. python polybot.py

Telegram Commands:
    /status   /positions   /pnl   /stop   /start   /signals   /close [id]   /risk [%]
"""

import os
import asyncio
import json
import time
import logging
from datetime import datetime, timedelta, timezone
def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)
from dataclasses import dataclass, field, asdict
from typing import Optional
from dotenv import load_dotenv

# ─── External deps ─────────────────────────────────────────────────────────────
import aiohttp
import numpy as np
from web3 import Web3
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

load_dotenv()
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger("polybot")


# ════════════════════════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════════════════════════

class Config:
    # — API Credentials —
    POLYMARKET_PRIVATE_KEY: str = os.getenv("POLY_PRIVATE_KEY", "")
    POLYMARKET_API_KEY: str     = os.getenv("POLY_API_KEY", "")
    POLYMARKET_SECRET: str      = os.getenv("POLY_SECRET", "")
    POLYMARKET_PASSPHRASE: str  = os.getenv("POLY_PASSPHRASE", "")
    TELEGRAM_TOKEN: str         = os.getenv("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT_ID: str       = os.getenv("TELEGRAM_CHAT_ID", "")
    POLYGON_RPC: str            = os.getenv("POLYGON_RPC", "https://polygon-rpc.com")

    # — Risk Parameters —
    MAX_POSITION_USD: float     = float(os.getenv("MAX_POSITION_USD", "2000"))
    MAX_PORTFOLIO_EXPOSURE: float = float(os.getenv("MAX_EXPOSURE", "0.50"))   # 50%
    STOP_LOSS_PCT: float        = float(os.getenv("STOP_LOSS_PCT", "0.20"))
    MIN_EDGE_REQUIRED: float    = float(os.getenv("MIN_EDGE_PCT", "0.05"))     # 5pp
    DAILY_LOSS_LIMIT: float     = float(os.getenv("DAILY_LOSS_USD", "500"))
    MAX_OPEN_POSITIONS: int     = int(os.getenv("MAX_POSITIONS", "10"))
    KELLY_FRACTION: float       = float(os.getenv("KELLY_FRACTION", "0.25"))  # quarter-Kelly

    # — Execution —
    SCAN_INTERVAL_SEC: int      = int(os.getenv("SCAN_INTERVAL", "60"))
    SLIPPAGE_TOLERANCE: float   = float(os.getenv("SLIPPAGE", "0.01"))
    MIN_MARKET_LIQUIDITY: float = float(os.getenv("MIN_LIQUIDITY", "10000"))
    PRICE_MIN: float            = float(os.getenv("PRICE_MIN", "0.05"))
    PRICE_MAX: float            = float(os.getenv("PRICE_MAX", "0.95"))

    # — Strategies —
    ENABLE_ARBITRAGE: bool      = os.getenv("STRAT_ARB", "true").lower() == "true"
    ENABLE_NEWS_CORR: bool      = os.getenv("STRAT_NEWS", "true").lower() == "true"
    ENABLE_KELLY: bool          = os.getenv("STRAT_KELLY", "false").lower() == "true"
    ENABLE_MEAN_REV: bool       = os.getenv("STRAT_MEANREV", "false").lower() == "true"

    # — Polymarket API Base —
    CLOB_BASE: str              = "https://clob.polymarket.com"
    GAMMA_BASE: str             = "https://gamma-api.polymarket.com"


# ════════════════════════════════════════════════════════════════════════════════
# DATA MODELS
# ════════════════════════════════════════════════════════════════════════════════

@dataclass
class Market:
    condition_id: str
    question: str
    yes_price: float
    no_price: float
    volume_24h: float
    liquidity: float
    category: str
    end_date: str
    token_id_yes: str = ""
    token_id_no: str  = ""


@dataclass
class Signal:
    market: Market
    side: str           # "YES" | "NO"
    strategy: str
    model_prob: float   # our model's probability
    market_price: float # current market price
    edge: float         # model_prob - market_price  (signed)
    confidence: float   # 0–1
    timestamp: str = field(default_factory=lambda: utcnow().isoformat())


@dataclass
class Position:
    id: str
    market: Market
    side: str
    entry_price: float
    current_price: float
    size_usd: float
    shares: float
    pnl: float = 0.0
    stop_loss: float = 0.0
    opened_at: str = field(default_factory=lambda: utcnow().isoformat())
    status: str = "open"


@dataclass
class Portfolio:
    total_value: float = 10000.0
    cash: float        = 10000.0
    positions: dict    = field(default_factory=dict)   # id → Position
    daily_pnl: float   = 0.0
    all_time_pnl: float= 0.0
    trades_won: int    = 0
    trades_total: int  = 0


# ════════════════════════════════════════════════════════════════════════════════
# POLYMARKET API CLIENT
# ════════════════════════════════════════════════════════════════════════════════

class PolymarketClient:
    """
    Async wrapper around Polymarket's Gamma (markets) and CLOB (order book) APIs.
    Handles authentication, market fetching, order placement via EIP-712 signing.
    """

    def __init__(self, cfg: Config):
        self.cfg   = cfg
        self.w3    = Web3(Web3.HTTPProvider(cfg.POLYGON_RPC))
        self.session: Optional[aiohttp.ClientSession] = None
        self._account = None
        if cfg.POLYMARKET_PRIVATE_KEY:
            self._account = self.w3.eth.account.from_key(cfg.POLYMARKET_PRIVATE_KEY)
            log.info(f"Wallet loaded: {self._account.address[:10]}…")

    async def start(self):
        self.session = aiohttp.ClientSession(
            headers={"User-Agent": "PolyBot/2.1", "Content-Type": "application/json"}
        )

    async def stop(self):
        if self.session:
            await self.session.close()

    # ── Market Data ─────────────────────────────────────────────────────────────

    async def get_markets(self, limit: int = 200) -> list[Market]:
        """Fetch active markets from Gamma API."""
        try:
            url = f"{self.cfg.GAMMA_BASE}/markets"
            params = {"active": "true", "closed": "false", "limit": limit}
            async with self.session.get(url, params=params, timeout=15) as r:
                if r.status != 200:
                    log.warning(f"Gamma API returned {r.status}")
                    return []
                data = await r.json()
                markets = []
                for m in data:
                    try:
                        tokens = m.get("tokens", [])
                        yes_tok = next((t for t in tokens if t.get("outcome","").upper()=="YES"), {})
                        no_tok  = next((t for t in tokens if t.get("outcome","").upper()=="NO"), {})
                        yes_price = float(yes_tok.get("price", 0.5))
                        no_price  = float(no_tok.get("price",  0.5))
                        markets.append(Market(
                            condition_id = m.get("conditionId",""),
                            question     = m.get("question",""),
                            yes_price    = yes_price,
                            no_price     = no_price,
                            volume_24h   = float(m.get("volume24hr", 0)),
                            liquidity    = float(m.get("liquidity", 0)),
                            category     = m.get("category","unknown"),
                            end_date     = m.get("endDate",""),
                            token_id_yes = yes_tok.get("tokenId",""),
                            token_id_no  = no_tok.get("tokenId",""),
                        ))
                    except Exception:
                        continue
                return markets
        except Exception as e:
            log.error(f"get_markets error: {e}")
            return []

    async def get_orderbook(self, token_id: str) -> dict:
        """Fetch CLOB order book for a token."""
        try:
            url = f"{self.cfg.CLOB_BASE}/book"
            params = {"token_id": token_id}
            async with self.session.get(url, params=params, timeout=10) as r:
                return await r.json() if r.status == 200 else {}
        except Exception as e:
            log.error(f"get_orderbook error: {e}")
            return {}

    # ── Order Execution ──────────────────────────────────────────────────────────

    async def place_market_order(self, token_id: str, side: str,
                                  size_usdc: float, price: float) -> dict:
        """
        Place a market order via Polymarket CLOB API.
        In production: sign EIP-712 message and POST to /order.
        """
        if not self._account:
            log.warning("No private key configured — paper trading mode")
            return {"status": "paper", "id": f"paper_{int(time.time())}"}

        order_payload = {
            "orderType": "FOK",
            "tokenID": token_id,
            "price": str(round(price, 4)),
            "size": str(round(size_usdc / price, 2)),
            "side": side.lower(),
            "feeRateBps": "0",
            "nonce": str(int(time.time() * 1000)),
            "signer": self._account.address,
            "maker": self._account.address,
        }
        # Sign using EIP-712 (simplified — use py-clob-client for full implementation)
        msg_hash = Web3.keccak(text=json.dumps(order_payload, sort_keys=True))
        signature = self._account.sign_message(
            {"messageHash": msg_hash, "message": msg_hash}
        )
        order_payload["signature"] = signature.signature.hex()

        try:
            async with self.session.post(
                f"{self.cfg.CLOB_BASE}/order",
                json={"order": order_payload, "owner": self._account.address},
                timeout=15
            ) as r:
                resp = await r.json()
                if resp.get("success"):
                    log.info(f"Order placed: {resp.get('orderID')}")
                else:
                    log.error(f"Order failed: {resp}")
                return resp
        except Exception as e:
            log.error(f"place_order error: {e}")
            return {}


# ════════════════════════════════════════════════════════════════════════════════
# MODEL / PROBABILITY ENGINE
# ════════════════════════════════════════════════════════════════════════════════

class ProbabilityModel:
    """
    Generates model probabilities using:
    1. Historical base rates per category
    2. Simple logistic calibration
    3. Sentiment adjustment from news signals
    """

    BASE_RATES = {
        "politics": {"yes_mean": 0.48, "vol": 0.18},
        "crypto":   {"yes_mean": 0.42, "vol": 0.22},
        "economics":{"yes_mean": 0.55, "vol": 0.15},
        "science":  {"yes_mean": 0.62, "vol": 0.12},
        "default":  {"yes_mean": 0.50, "vol": 0.20},
    }

    def __init__(self):
        self.sentiment_cache: dict[str, float] = {}

    def calibrate(self, market_price: float, category: str,
                  sentiment: float = 0.0) -> float:
        """
        Produce model probability given market price, category, and
        sentiment signal (−1 to +1).
        """
        cat = category.lower() if category.lower() in self.BASE_RATES else "default"
        base = self.BASE_RATES[cat]
        # Logit transform market price
        p = max(0.01, min(0.99, market_price))
        logit_p = np.log(p / (1 - p))
        # Apply category-level mean-reversion pull
        mean_logit = np.log(base["yes_mean"] / (1 - base["yes_mean"]))
        alpha = 0.3  # shrinkage
        adjusted_logit = (1 - alpha) * logit_p + alpha * mean_logit
        # Apply sentiment shift
        adjusted_logit += sentiment * 0.4
        prob = 1 / (1 + np.exp(-adjusted_logit))
        return round(float(prob), 4)

    def edge(self, model_prob: float, market_price: float) -> float:
        return round(model_prob - market_price, 4)

    def kelly_fraction(self, prob: float, odds: float) -> float:
        """Kelly criterion: f = (p*b - q) / b  where b = 1/price - 1"""
        q = 1 - prob
        if odds <= 0:
            return 0.0
        k = (prob * odds - q) / odds
        return max(0.0, min(k * self.cfg_kelly, 0.25)) if hasattr(self,'cfg_kelly') else max(0.0, min(k * 0.25, 0.25))


# ════════════════════════════════════════════════════════════════════════════════
# STRATEGIES
# ════════════════════════════════════════════════════════════════════════════════

class ArbitrageStrategy:
    """Detect price gaps between Polymarket and external sources."""

    EXTERNAL_PRICES: dict = {
        # Simulated external market prices for demo
        # In production: fetch from Manifold Markets, Metaculus, Kalshi APIs
        "will democrats win": 0.52,
        "federal reserve":    0.70,
        "bitcoin":            0.38,
    }

    def find_opportunities(self, markets: list[Market],
                           model: ProbabilityModel,
                           min_edge: float) -> list[Signal]:
        signals = []
        for m in markets:
            # Check against external price
            ext_price = None
            for kw, price in self.EXTERNAL_PRICES.items():
                if kw in m.question.lower():
                    ext_price = price
                    break

            if ext_price is not None:
                edge_yes = ext_price - m.yes_price
                if abs(edge_yes) >= min_edge:
                    side       = "YES" if edge_yes > 0 else "NO"
                    mkt_price  = m.yes_price if side == "YES" else m.no_price
                    model_prob = ext_price if side == "YES" else (1 - ext_price)
                    confidence = min(0.95, abs(edge_yes) * 5)
                    signals.append(Signal(
                        market      = m,
                        side        = side,
                        strategy    = "Arbitrage",
                        model_prob  = model_prob,
                        market_price= mkt_price,
                        edge        = abs(edge_yes),
                        confidence  = round(confidence, 2),
                    ))
        return signals


class NewsCorrelationStrategy:
    """
    Detect when market price diverges from news sentiment.
    In production: integrate NewsAPI, Twitter API, RSS feeds.
    """

    KEYWORDS = {
        "rate cut": ("economics", +0.6),
        "inflation":("economics", -0.3),
        "bitcoin":  ("crypto",    +0.4),
        "election": ("politics",  +0.2),
        "ai":       ("technology",+0.5),
    }

    def __init__(self, model: ProbabilityModel):
        self.model = model
        self.sentiment_scores: dict[str, float] = {}

    def update_sentiment(self, headline: str) -> float:
        """Compute sentiment score from headline. Demo version — no NLP."""
        score = 0.0
        hl = headline.lower()
        positive = ["win","pass","approve","rise","gain","cut","growth"]
        negative = ["lose","fail","reject","drop","fall","crash","decline"]
        for w in positive:
            if w in hl: score += 0.2
        for w in negative:
            if w in hl: score -= 0.2
        return max(-1.0, min(1.0, score))

    def analyze(self, markets: list[Market], min_edge: float) -> list[Signal]:
        signals = []
        for m in markets:
            sentiment = 0.0
            for kw, (cat, weight) in self.KEYWORDS.items():
                if kw in m.question.lower():
                    sentiment += weight
                    break
            sentiment = max(-1.0, min(1.0, sentiment))
            if abs(sentiment) < 0.2:
                continue

            model_prob  = self.model.calibrate(m.yes_price, m.category, sentiment)
            mkt_price   = m.yes_price
            edge        = self.model.edge(model_prob, mkt_price)

            if abs(edge) >= min_edge:
                side = "YES" if edge > 0 else "NO"
                confidence = min(0.92, abs(edge) * 6 * abs(sentiment))
                signals.append(Signal(
                    market      = m,
                    side        = side,
                    strategy    = "NewsCorr",
                    model_prob  = model_prob,
                    market_price= mkt_price,
                    edge        = abs(edge),
                    confidence  = round(confidence, 2),
                ))
        return signals


class MeanReversionStrategy:
    """Fade markets that have deviated > 2σ from their rolling average."""

    def __init__(self):
        self.price_history: dict[str, list[float]] = {}

    def update(self, market: Market):
        cid = market.condition_id
        if cid not in self.price_history:
            self.price_history[cid] = []
        self.price_history[cid].append(market.yes_price)
        if len(self.price_history[cid]) > 168:   # 7 days × 24 hours
            self.price_history[cid] = self.price_history[cid][-168:]

    def analyze(self, markets: list[Market], min_edge: float) -> list[Signal]:
        signals = []
        for m in markets:
            self.update(m)
            hist = self.price_history.get(m.condition_id, [])
            if len(hist) < 20:
                continue
            arr    = np.array(hist)
            mu     = arr.mean()
            sigma  = arr.std()
            if sigma < 0.001:
                continue
            z_score = (m.yes_price - mu) / sigma
            if abs(z_score) >= 2.0:
                side  = "NO" if z_score > 0 else "YES"
                edge  = min(0.15, abs(z_score - 2.0) * 0.02 + min_edge)
                mp    = m.yes_price if side == "YES" else m.no_price
                signals.append(Signal(
                    market      = m,
                    side        = side,
                    strategy    = "MeanReversion",
                    model_prob  = mu if side == "YES" else (1 - mu),
                    market_price= mp,
                    edge        = round(edge, 4),
                    confidence  = round(min(0.80, abs(z_score) * 0.2), 2),
                ))
        return signals


# ════════════════════════════════════════════════════════════════════════════════
# RISK MANAGER
# ════════════════════════════════════════════════════════════════════════════════

class RiskManager:

    def __init__(self, cfg: Config, portfolio: Portfolio):
        self.cfg       = cfg
        self.portfolio = portfolio
        self.daily_start_value: float = portfolio.total_value

    def check_daily_loss(self) -> bool:
        daily_loss = self.daily_start_value - self.portfolio.total_value
        return daily_loss >= self.cfg.DAILY_LOSS_LIMIT

    def check_exposure(self) -> bool:
        deployed = self.portfolio.total_value - self.portfolio.cash
        ratio    = deployed / max(1.0, self.portfolio.total_value)
        return ratio >= self.cfg.MAX_PORTFOLIO_EXPOSURE

    def check_position_count(self) -> bool:
        return len(self.portfolio.positions) >= self.cfg.MAX_OPEN_POSITIONS

    def can_trade(self) -> tuple[bool, str]:
        if self.check_daily_loss():
            return False, f"Daily loss limit reached (${self.cfg.DAILY_LOSS_LIMIT:.0f})"
        if self.check_exposure():
            return False, f"Max portfolio exposure reached ({self.cfg.MAX_PORTFOLIO_EXPOSURE*100:.0f}%)"
        if self.check_position_count():
            return False, f"Max open positions reached ({self.cfg.MAX_OPEN_POSITIONS})"
        return True, "OK"

    def size_position(self, signal: Signal, price: float) -> float:
        """Kelly-adjusted position sizing, capped at MAX_POSITION_USD."""
        odds     = (1.0 / price) - 1.0
        kelly_f  = max(0.0, (signal.model_prob * odds - (1 - signal.model_prob)) / odds)
        kelly_f  *= self.cfg.KELLY_FRACTION    # fractional Kelly
        size_usd  = kelly_f * self.portfolio.cash
        return round(min(size_usd, self.cfg.MAX_POSITION_USD, self.portfolio.cash * 0.20), 2)

    def compute_stop_loss(self, entry_price: float, side: str) -> float:
        if side == "YES":
            return round(entry_price * (1 - self.cfg.STOP_LOSS_PCT), 4)
        else:
            return round(entry_price * (1 + self.cfg.STOP_LOSS_PCT), 4)

    def check_stop_losses(self) -> list[str]:
        """Return list of position IDs that hit stop loss."""
        triggered = []
        for pid, pos in self.portfolio.positions.items():
            if pos.side == "YES" and pos.current_price <= pos.stop_loss:
                triggered.append(pid)
                log.warning(f"Stop loss triggered: {pid} @ {pos.current_price:.4f}")
            elif pos.side == "NO" and pos.current_price >= pos.stop_loss:
                triggered.append(pid)
                log.warning(f"Stop loss triggered: {pid} @ {pos.current_price:.4f}")
        return triggered

    def update_pnl(self, pos: Position, current_price: float) -> Position:
        pos.current_price = current_price
        if pos.side == "YES":
            pos.pnl = (current_price - pos.entry_price) * pos.shares
        else:
            pos.pnl = (pos.entry_price - current_price) * pos.shares
        return pos


# ════════════════════════════════════════════════════════════════════════════════
# TELEGRAM BOT
# ════════════════════════════════════════════════════════════════════════════════

class TelegramBot:

    def __init__(self, token: str, chat_id: str, trading_bot: "TradingBot"):
        self.token       = token
        self.chat_id     = chat_id
        self.trading_bot = trading_bot
        self.app: Optional[Application] = None

    def build(self):
        if not self.token:
            log.warning("No Telegram token — alerts disabled")
            return
        self.app = Application.builder().token(self.token).build()
        self.app.add_handler(CommandHandler("start",     self._cmd_start))
        self.app.add_handler(CommandHandler("status",    self._cmd_status))
        self.app.add_handler(CommandHandler("positions", self._cmd_positions))
        self.app.add_handler(CommandHandler("pnl",       self._cmd_pnl))
        self.app.add_handler(CommandHandler("signals",   self._cmd_signals))
        self.app.add_handler(CommandHandler("stop",      self._cmd_stop))
        self.app.add_handler(CommandHandler("resume",    self._cmd_resume))
        self.app.add_handler(CommandHandler("risk",      self._cmd_risk))
        self.app.add_handler(CommandHandler("close",     self._cmd_close))
        self.app.add_handler(CallbackQueryHandler(self._callback))
        log.info("Telegram bot configured")

    async def send(self, text: str, parse_mode: str = "HTML"):
        if not self.app or not self.chat_id:
            return
        try:
            await self.app.bot.send_message(
                chat_id    = self.chat_id,
                text       = text,
                parse_mode = parse_mode,
            )
        except Exception as e:
            log.error(f"Telegram send error: {e}")

    async def send_trade_alert(self, pos: Position, action: str):
        emoji  = "✅" if action == "open" else ("💰" if pos.pnl >= 0 else "❌")
        pnl_s  = f"+${pos.pnl:.2f}" if pos.pnl >= 0 else f"-${abs(pos.pnl):.2f}"
        text   = (
            f"{emoji} <b>Trade {'Opened' if action == 'open' else 'Closed'}</b>\n\n"
            f"📊 <b>{pos.market.question[:60]}…</b>\n"
            f"Side: <b>{pos.side}</b> | Price: <code>{pos.entry_price:.4f}</code>\n"
            f"Size: <code>${pos.size_usd:.2f} USDC</code>\n"
        )
        if action == "close":
            text += f"P&L: <b>{pnl_s}</b>\n"
        text += f"\n🕐 {utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
        await self.send(text)

    async def send_signal_alert(self, sig: Signal):
        emoji = "📈" if sig.side == "YES" else "📉"
        await self.send(
            f"{emoji} <b>New Signal — {sig.strategy}</b>\n\n"
            f"<b>{sig.market.question[:60]}…</b>\n"
            f"Side: <b>{sig.side}</b> | Edge: <code>+{sig.edge*100:.1f}pp</code>\n"
            f"Confidence: <code>{sig.confidence*100:.0f}%</code>\n"
            f"Model: <code>{sig.model_prob:.3f}</code> vs Market: <code>{sig.market_price:.3f}</code>"
        )

    async def send_daily_summary(self, portfolio: Portfolio):
        wr  = (portfolio.trades_won / max(1, portfolio.trades_total)) * 100
        pnl = portfolio.daily_pnl
        text = (
            f"📊 <b>Daily Summary — {utcnow().strftime('%Y-%m-%d')}</b>\n\n"
            f"Portfolio: <b>${portfolio.total_value:,.2f}</b>\n"
            f"Daily P&L: <b>{'+'if pnl>=0 else ''}{pnl:.2f}</b>\n"
            f"All-time P&L: <b>${portfolio.all_time_pnl:+.2f}</b>\n"
            f"Win Rate: <code>{wr:.1f}%</code> ({portfolio.trades_won}/{portfolio.trades_total})\n"
            f"Open Positions: <code>{len(portfolio.positions)}</code>"
        )
        await self.send(text)

    # ── Commands ───────────────────────────────────────────────────────────────

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_html(
            "<b>🤖 PolyBot v2.1.0</b>\n\n"
            "Polymarket Automated Trading Bot\n\n"
            "<b>Commands:</b>\n"
            "/status — Bot status &amp; portfolio\n"
            "/positions — Open positions\n"
            "/pnl — Today's P&amp;L\n"
            "/signals — Active signals\n"
            "/stop — Stop trading\n"
            "/resume — Resume trading\n"
            "/risk [%] — Set max exposure\n"
            "/close [id] — Close a position"
        )

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        bot  = self.trading_bot
        port = bot.portfolio
        status = "🟢 RUNNING" if bot.running else "🔴 STOPPED"
        await update.message.reply_html(
            f"<b>Bot Status: {status}</b>\n\n"
            f"Portfolio: <b>${port.total_value:,.2f}</b>\n"
            f"Cash: <code>${port.cash:,.2f}</code>\n"
            f"Open Positions: <code>{len(port.positions)}</code>\n"
            f"Daily P&L: <b>${port.daily_pnl:+.2f}</b>"
        )

    async def _cmd_positions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        port = self.trading_bot.portfolio
        if not port.positions:
            await update.message.reply_text("No open positions.")
            return
        lines = []
        for pid, pos in port.positions.items():
            pnl_s = f"+${pos.pnl:.2f}" if pos.pnl >= 0 else f"-${abs(pos.pnl):.2f}"
            lines.append(f"• {pos.market.question[:40]}…\n  {pos.side} @{pos.entry_price:.4f} | {pnl_s}")
        await update.message.reply_html(
            f"<b>Open Positions ({len(port.positions)})</b>\n\n" + "\n\n".join(lines)
        )

    async def _cmd_pnl(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        port = self.trading_bot.portfolio
        await update.message.reply_html(
            f"<b>P&amp;L Report</b>\n\n"
            f"Today: <b>${port.daily_pnl:+.2f}</b>\n"
            f"All-time: <b>${port.all_time_pnl:+.2f}</b>"
        )

    async def _cmd_signals(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        sigs = self.trading_bot.latest_signals[:5]
        if not sigs:
            await update.message.reply_text("No active signals.")
            return
        lines = [f"• {s.side} {s.market.question[:40]}…\n  Edge: +{s.edge*100:.1f}pp | {s.strategy}" for s in sigs]
        await update.message.reply_html("<b>Active Signals</b>\n\n" + "\n\n".join(lines))

    async def _cmd_stop(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        self.trading_bot.running = False
        await update.message.reply_text("🔴 Bot stopped. Use /resume to restart.")

    async def _cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        self.trading_bot.running = True
        await update.message.reply_text("🟢 Bot resumed.")

    async def _cmd_risk(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            pct = float(ctx.args[0].strip('%')) / 100
            self.trading_bot.cfg.MAX_PORTFOLIO_EXPOSURE = pct
            await update.message.reply_text(f"Max exposure updated to {pct*100:.0f}%")
        except Exception:
            await update.message.reply_text("Usage: /risk 40  (sets 40% max exposure)")

    async def _cmd_close(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            await update.message.reply_text("Usage: /close [position_id]")
            return
        pid = ctx.args[0]
        if pid in self.trading_bot.portfolio.positions:
            await self.trading_bot.close_position(pid, reason="manual")
            await update.message.reply_text(f"Position {pid} closed.")
        else:
            await update.message.reply_text(f"Position {pid} not found.")

    async def _callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()


# ════════════════════════════════════════════════════════════════════════════════
# MAIN TRADING BOT
# ════════════════════════════════════════════════════════════════════════════════

class TradingBot:

    def __init__(self):
        self.cfg               = Config()
        self.portfolio         = Portfolio()
        self.client            = PolymarketClient(self.cfg)
        self.model             = ProbabilityModel()
        self.risk              = RiskManager(self.cfg, self.portfolio)
        self.running           = True
        self.latest_signals: list[Signal] = []

        # Strategies
        self.arb_strategy   = ArbitrageStrategy()
        self.news_strategy  = NewsCorrelationStrategy(self.model)
        self.meanrev_strat  = MeanReversionStrategy()

        # Telegram
        self.tg = TelegramBot(self.cfg.TELEGRAM_TOKEN, self.cfg.TELEGRAM_CHAT_ID, self)
        self.tg.build()

        # State
        self._scan_count   = 0
        self._last_daily   = utcnow().date()

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start(self):
        await self.client.start()
        log.info("PolyBot started")
        await self.tg.send("🤖 <b>PolyBot Started</b>\n\nScanning Polymarket…")

        if self.tg.app:
            await self.tg.app.initialize()
            await self.tg.app.updater.start_polling(drop_pending_updates=True)
            await self.tg.app.start()

        while True:
            try:
                if self.running:
                    await self.scan_cycle()
                await self._daily_summary_check()
                await asyncio.sleep(self.cfg.SCAN_INTERVAL_SEC)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Main loop error: {e}")
                await asyncio.sleep(10)

    async def stop(self):
        if self.tg.app:
            await self.tg.app.updater.stop()
            await self.tg.app.stop()
            await self.tg.app.shutdown()
        await self.client.stop()
        log.info("PolyBot stopped")

    # ── Scan Cycle ─────────────────────────────────────────────────────────────

    async def scan_cycle(self):
        self._scan_count += 1
        log.info(f"Scan #{self._scan_count} started")

        # 1. Fetch markets
        markets = await self.client.get_markets(limit=200)
        log.info(f"Fetched {len(markets)} markets")

        # 2. Filter
        markets = self._filter_markets(markets)
        log.info(f"After filtering: {len(markets)} markets")

        # 3. Update stop losses
        await self._update_positions(markets)

        # 4. Generate signals
        signals = self._generate_signals(markets)
        self.latest_signals = signals
        log.info(f"Signals generated: {len(signals)}")

        # 5. Execute trades
        for sig in signals[:3]:  # max 3 trades per cycle
            await self._process_signal(sig)

    def _filter_markets(self, markets: list[Market]) -> list[Market]:
        cfg = self.cfg
        result = []
        for m in markets:
            if m.liquidity < cfg.MIN_MARKET_LIQUIDITY:
                continue
            if not (cfg.PRICE_MIN <= m.yes_price <= cfg.PRICE_MAX):
                continue
            if m.condition_id in self.portfolio.positions:
                continue
            result.append(m)
        return result

    def _generate_signals(self, markets: list[Market]) -> list[Signal]:
        signals = []
        if self.cfg.ENABLE_ARBITRAGE:
            signals += self.arb_strategy.find_opportunities(markets, self.model, self.cfg.MIN_EDGE_REQUIRED)
        if self.cfg.ENABLE_NEWS_CORR:
            signals += self.news_strategy.analyze(markets, self.cfg.MIN_EDGE_REQUIRED)
        if self.cfg.ENABLE_MEAN_REV:
            signals += self.meanrev_strat.analyze(markets, self.cfg.MIN_EDGE_REQUIRED)
        # Sort by confidence descending
        signals.sort(key=lambda s: s.confidence, reverse=True)
        return signals

    async def _process_signal(self, sig: Signal):
        can, reason = self.risk.can_trade()
        if not can:
            log.warning(f"Risk block: {reason}")
            return

        price     = sig.market.yes_price if sig.side == "YES" else sig.market.no_price
        size_usd  = self.risk.size_position(sig, price)
        if size_usd < 10:
            log.info(f"Signal skipped — position size too small (${size_usd:.2f})")
            return

        token_id  = sig.market.token_id_yes if sig.side == "YES" else sig.market.token_id_no
        resp      = await self.client.place_market_order(token_id, sig.side, size_usd, price)
        order_id  = resp.get("id") or resp.get("orderID") or f"pos_{int(time.time())}"

        pos = Position(
            id           = order_id,
            market       = sig.market,
            side         = sig.side,
            entry_price  = price,
            current_price= price,
            size_usd     = size_usd,
            shares       = size_usd / price,
            stop_loss    = self.risk.compute_stop_loss(price, sig.side),
        )
        self.portfolio.positions[order_id] = pos
        self.portfolio.cash       -= size_usd
        self.portfolio.trades_total += 1

        log.info(f"Position opened: {sig.side} {sig.market.question[:40]} @ {price:.4f} | ${size_usd:.2f}")
        await self.tg.send_trade_alert(pos, "open")
        await self.tg.send_signal_alert(sig)

    async def _update_positions(self, markets: list[Market]):
        mkt_map = {m.condition_id: m for m in markets}
        triggered = []
        for pid, pos in list(self.portfolio.positions.items()):
            mkt = mkt_map.get(pos.market.condition_id)
            if not mkt:
                continue
            curr = mkt.yes_price if pos.side == "YES" else mkt.no_price
            pos  = self.risk.update_pnl(pos, curr)
            self.portfolio.positions[pid] = pos
        triggered = self.risk.check_stop_losses()
        for pid in triggered:
            await self.close_position(pid, reason="stop_loss")

    async def close_position(self, pid: str, reason: str = "manual"):
        pos = self.portfolio.positions.pop(pid, None)
        if not pos:
            return
        self.portfolio.cash          += pos.size_usd + pos.pnl
        self.portfolio.total_value    = self.portfolio.cash + sum(p.size_usd for p in self.portfolio.positions.values())
        self.portfolio.daily_pnl     += pos.pnl
        self.portfolio.all_time_pnl  += pos.pnl
        if pos.pnl > 0:
            self.portfolio.trades_won += 1
        log.info(f"Position closed ({reason}): {pid} | PnL: ${pos.pnl:+.2f}")
        await self.tg.send_trade_alert(pos, "close")

    async def _daily_summary_check(self):
        today = utcnow().date()
        if today != self._last_daily:
            self._last_daily = today
            await self.tg.send_daily_summary(self.portfolio)
            self.portfolio.daily_pnl = 0.0
            self.risk.daily_start_value = self.portfolio.total_value


# ════════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════════

async def main():
    bot = TradingBot()
    try:
        await bot.start()
    except KeyboardInterrupt:
        log.info("Shutting down…")
        await bot.stop()

if __name__ == "__main__":
    asyncio.run(main())
