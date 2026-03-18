"""
PolyBot v3.0 — Polymarket Automated Trading Bot
================================================
Upgrades in v3.0:
  ✅ Signal cooldown — no duplicate alerts
  ✅ Multi-group Telegram broadcasting
  ✅ More strategies: Volume Spike, Closing Soon, Sentiment
  ✅ Smarter position sizing
  ✅ /leaderboard command — top signals ranked by edge
  ✅ /summary command — full daily report
  ✅ Market diversity filter — no repeat markets
  ✅ Confidence threshold filter
  ✅ Better Telegram message formatting
"""

import os, asyncio, json, time, logging, hashlib
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

import aiohttp
import numpy as np
from web3 import Web3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
)

load_dotenv()
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S", level=logging.INFO,
)
log = logging.getLogger("polybot")

def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ════════════════════════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════════════════════════

class Config:
    POLYMARKET_PRIVATE_KEY: str   = os.getenv("POLY_PRIVATE_KEY", "")
    POLYMARKET_API_KEY: str       = os.getenv("POLY_API_KEY", "")
    TELEGRAM_TOKEN: str           = os.getenv("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT_ID: str         = os.getenv("TELEGRAM_CHAT_ID", "")
    POLYGON_RPC: str              = os.getenv("POLYGON_RPC", "https://polygon-rpc.com")

    # Risk
    MAX_POSITION_USD: float       = float(os.getenv("MAX_POSITION_USD", "2000"))
    MAX_PORTFOLIO_EXPOSURE: float = float(os.getenv("MAX_EXPOSURE", "0.50"))
    STOP_LOSS_PCT: float          = float(os.getenv("STOP_LOSS_PCT", "0.20"))
    MIN_EDGE_REQUIRED: float      = float(os.getenv("MIN_EDGE_PCT", "0.05"))
    DAILY_LOSS_LIMIT: float       = float(os.getenv("DAILY_LOSS_USD", "500"))
    MAX_OPEN_POSITIONS: int       = int(os.getenv("MAX_POSITIONS", "10"))
    KELLY_FRACTION: float         = float(os.getenv("KELLY_FRACTION", "0.25"))
    MIN_CONFIDENCE: float         = float(os.getenv("MIN_CONFIDENCE", "0.55"))

    # Execution
    SCAN_INTERVAL_SEC: int        = int(os.getenv("SCAN_INTERVAL", "60"))
    MIN_MARKET_LIQUIDITY: float   = float(os.getenv("MIN_LIQUIDITY", "5000"))
    PRICE_MIN: float              = float(os.getenv("PRICE_MIN", "0.05"))
    PRICE_MAX: float              = float(os.getenv("PRICE_MAX", "0.95"))

    # Signal cooldown — don't re-alert same signal for N seconds
    SIGNAL_COOLDOWN_SEC: int      = int(os.getenv("SIGNAL_COOLDOWN", "3600"))  # 1 hour

    # Strategies
    ENABLE_ARBITRAGE: bool        = os.getenv("STRAT_ARB", "true").lower() == "true"
    ENABLE_NEWS_CORR: bool        = os.getenv("STRAT_NEWS", "true").lower() == "true"
    ENABLE_MEAN_REV: bool         = os.getenv("STRAT_MEANREV", "true").lower() == "true"
    ENABLE_VOLUME_SPIKE: bool     = os.getenv("STRAT_VOL", "true").lower() == "true"
    ENABLE_CLOSING_SOON: bool     = os.getenv("STRAT_CLOSE", "true").lower() == "true"

    CLOB_BASE: str                = "https://clob.polymarket.com"
    GAMMA_BASE: str               = "https://gamma-api.polymarket.com"

    @property
    def chat_ids(self) -> list[str]:
        """Support multiple chat IDs separated by comma."""
        return [c.strip() for c in self.TELEGRAM_CHAT_ID.split(",") if c.strip()]


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
    side: str
    strategy: str
    model_prob: float
    market_price: float
    edge: float
    confidence: float
    emoji: str = "📐"
    timestamp: str = field(default_factory=lambda: utcnow().isoformat())

    @property
    def signal_key(self) -> str:
        """Unique key for cooldown tracking."""
        return hashlib.md5(
            f"{self.market.condition_id}{self.side}{self.strategy}".encode()
        ).hexdigest()[:12]

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
    total_value: float  = 10000.0
    cash: float         = 10000.0
    positions: dict     = field(default_factory=dict)
    daily_pnl: float    = 0.0
    all_time_pnl: float = 0.0
    trades_won: int     = 0
    trades_total: int   = 0
    signals_today: int  = 0


# ════════════════════════════════════════════════════════════════════════════════
# POLYMARKET CLIENT
# ════════════════════════════════════════════════════════════════════════════════

class PolymarketClient:
    def __init__(self, cfg: Config):
        self.cfg      = cfg
        self.w3       = Web3(Web3.HTTPProvider(cfg.POLYGON_RPC))
        self.session: Optional[aiohttp.ClientSession] = None
        self._account = None
        if cfg.POLYMARKET_PRIVATE_KEY:
            self._account = self.w3.eth.account.from_key(cfg.POLYMARKET_PRIVATE_KEY)
            log.info(f"Wallet loaded: {self._account.address[:10]}…")

    async def start(self):
        self.session = aiohttp.ClientSession(
            headers={"User-Agent": "PolyBot/3.0", "Content-Type": "application/json"}
        )

    async def stop(self):
        if self.session:
            await self.session.close()

    async def get_markets(self, limit: int = 300) -> list[Market]:
        try:
            url    = f"{self.cfg.GAMMA_BASE}/markets"
            params = {"active": "true", "closed": "false", "limit": limit}
            async with self.session.get(url, params=params, timeout=15) as r:
                if r.status != 200:
                    return []
                data    = await r.json()
                markets = []
                for m in data:
                    try:
                        tokens    = m.get("tokens", [])
                        yes_tok   = next((t for t in tokens if t.get("outcome","").upper()=="YES"), {})
                        no_tok    = next((t for t in tokens if t.get("outcome","").upper()=="NO"), {})
                        markets.append(Market(
                            condition_id  = m.get("conditionId",""),
                            question      = m.get("question",""),
                            yes_price     = float(yes_tok.get("price", 0.5)),
                            no_price      = float(no_tok.get("price",  0.5)),
                            volume_24h    = float(m.get("volume24hr", 0)),
                            liquidity     = float(m.get("liquidity", 0)),
                            category      = m.get("category","unknown"),
                            end_date      = m.get("endDate",""),
                            token_id_yes  = yes_tok.get("tokenId",""),
                            token_id_no   = no_tok.get("tokenId",""),
                        ))
                    except Exception:
                        continue
                return markets
        except Exception as e:
            log.error(f"get_markets error: {e}")
            return []

    async def place_order(self, token_id: str, side: str,
                          size_usdc: float, price: float) -> dict:
        if not self._account:
            log.warning("Paper trading mode — no private key")
            return {"status": "paper", "id": f"paper_{int(time.time())}"}
        order = {
            "orderType": "FOK", "tokenID": token_id,
            "price": str(round(price, 4)),
            "size": str(round(size_usdc / price, 2)),
            "side": side.lower(), "feeRateBps": "0",
            "nonce": str(int(time.time() * 1000)),
            "signer": self._account.address,
            "maker": self._account.address,
        }
        try:
            from eth_account.messages import encode_defunct
            msg_hash = Web3.keccak(text=json.dumps(order, sort_keys=True))
            signed   = self._account.sign_message(encode_defunct(msg_hash))
            order["signature"] = signed.signature.hex()
            async with self.session.post(
                f"{self.cfg.CLOB_BASE}/order",
                json={"order": order, "owner": self._account.address}, timeout=15
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
# PROBABILITY MODEL
# ════════════════════════════════════════════════════════════════════════════════

class ProbabilityModel:
    BASE_RATES = {
        "politics":  {"yes_mean": 0.48, "vol": 0.18},
        "crypto":    {"yes_mean": 0.42, "vol": 0.22},
        "economics": {"yes_mean": 0.55, "vol": 0.15},
        "science":   {"yes_mean": 0.62, "vol": 0.12},
        "default":   {"yes_mean": 0.50, "vol": 0.20},
    }

    def calibrate(self, market_price: float, category: str,
                  sentiment: float = 0.0) -> float:
        cat  = category.lower() if category.lower() in self.BASE_RATES else "default"
        base = self.BASE_RATES[cat]
        p    = max(0.01, min(0.99, market_price))
        logit_p    = np.log(p / (1 - p))
        mean_logit = np.log(base["yes_mean"] / (1 - base["yes_mean"]))
        adjusted   = (1 - 0.3) * logit_p + 0.3 * mean_logit + sentiment * 0.4
        return round(float(1 / (1 + np.exp(-adjusted))), 4)

    def edge(self, model_prob: float, market_price: float) -> float:
        return round(model_prob - market_price, 4)


# ════════════════════════════════════════════════════════════════════════════════
# STRATEGIES
# ════════════════════════════════════════════════════════════════════════════════

class ArbitrageStrategy:
    """Cross-market price gap detection."""
    EXTERNAL = {
        "will democrats win":   0.52,
        "federal reserve":      0.70,
        "bitcoin":              0.38,
        "elon":                 0.60,
        "trump":                0.55,
        "recession":            0.35,
        "rate cut":             0.68,
        "election":             0.50,
    }

    def analyze(self, markets: list[Market], model: ProbabilityModel,
                min_edge: float) -> list[Signal]:
        signals = []
        for m in markets:
            ext = None
            for kw, price in self.EXTERNAL.items():
                if kw in m.question.lower():
                    ext = price
                    break
            if ext is None:
                continue
            edge_yes = ext - m.yes_price
            if abs(edge_yes) >= min_edge:
                side       = "YES" if edge_yes > 0 else "NO"
                mkt_price  = m.yes_price if side == "YES" else m.no_price
                model_prob = ext if side == "YES" else (1 - ext)
                confidence = min(0.95, abs(edge_yes) * 5)
                signals.append(Signal(
                    market=m, side=side, strategy="Arbitrage",
                    model_prob=model_prob, market_price=mkt_price,
                    edge=abs(edge_yes), confidence=round(confidence, 2),
                    emoji="⚡"
                ))
        return signals


class NewsCorrelationStrategy:
    """News sentiment vs market price divergence."""
    KEYWORDS = {
        "rate cut":   ("economics", +0.6),
        "inflation":  ("economics", -0.3),
        "bitcoin":    ("crypto",    +0.4),
        "election":   ("politics",  +0.2),
        "ai":         ("tech",      +0.5),
        "war":        ("politics",  -0.4),
        "crash":      ("crypto",    -0.5),
        "ban":        ("crypto",    -0.4),
        "approval":   ("politics",  +0.3),
        "recession":  ("economics", -0.4),
    }

    def __init__(self, model: ProbabilityModel):
        self.model = model

    def analyze(self, markets: list[Market], min_edge: float) -> list[Signal]:
        signals = []
        for m in markets:
            sentiment = 0.0
            for kw, (_, weight) in self.KEYWORDS.items():
                if kw in m.question.lower():
                    sentiment += weight
            sentiment = max(-1.0, min(1.0, sentiment))
            if abs(sentiment) < 0.2:
                continue
            model_prob = self.model.calibrate(m.yes_price, m.category, sentiment)
            edge       = self.model.edge(model_prob, m.yes_price)
            if abs(edge) >= min_edge:
                side       = "YES" if edge > 0 else "NO"
                confidence = min(0.90, abs(edge) * 6 * abs(sentiment))
                signals.append(Signal(
                    market=m, side=side, strategy="NewsCorr",
                    model_prob=model_prob, market_price=m.yes_price,
                    edge=abs(edge), confidence=round(confidence, 2),
                    emoji="📰"
                ))
        return signals


class MeanReversionStrategy:
    """Fade extreme price moves."""
    def __init__(self):
        self.price_history: dict[str, list[float]] = {}

    def update(self, market: Market):
        cid = market.condition_id
        if cid not in self.price_history:
            self.price_history[cid] = []
        self.price_history[cid].append(market.yes_price)
        if len(self.price_history[cid]) > 168:
            self.price_history[cid] = self.price_history[cid][-168:]

    def analyze(self, markets: list[Market], min_edge: float) -> list[Signal]:
        signals = []
        for m in markets:
            self.update(m)
            hist = self.price_history.get(m.condition_id, [])
            if len(hist) < 10:
                continue
            arr     = np.array(hist)
            mu      = arr.mean()
            sigma   = arr.std()
            if sigma < 0.001:
                continue
            z = (m.yes_price - mu) / sigma
            if abs(z) >= 1.8:
                side  = "NO" if z > 0 else "YES"
                edge  = min(0.15, abs(z - 1.8) * 0.02 + min_edge)
                mp    = m.yes_price if side == "YES" else m.no_price
                signals.append(Signal(
                    market=m, side=side, strategy="MeanRevert",
                    model_prob=mu if side=="YES" else (1-mu),
                    market_price=mp, edge=round(edge, 4),
                    confidence=round(min(0.80, abs(z) * 0.2), 2),
                    emoji="📊"
                ))
        return signals


class VolumeSpikeStrategy:
    """Detect unusual volume — big players entering."""
    def __init__(self):
        self.vol_history: dict[str, list[float]] = {}

    def analyze(self, markets: list[Market], min_edge: float) -> list[Signal]:
        signals = []
        for m in markets:
            cid = m.condition_id
            if cid not in self.vol_history:
                self.vol_history[cid] = []
            self.vol_history[cid].append(m.volume_24h)
            if len(self.vol_history[cid]) > 48:
                self.vol_history[cid] = self.vol_history[cid][-48:]
            hist = self.vol_history[cid]
            if len(hist) < 5 or m.volume_24h == 0:
                continue
            avg_vol = np.mean(hist[:-1])
            if avg_vol < 100:
                continue
            spike = m.volume_24h / avg_vol
            if spike >= 3.0:  # 3x normal volume
                # High volume = smart money — follow the price direction
                side       = "YES" if m.yes_price > 0.5 else "NO"
                mkt_price  = m.yes_price if side == "YES" else m.no_price
                confidence = min(0.85, 0.5 + (spike - 3) * 0.05)
                edge       = max(min_edge, min(0.12, (spike - 3) * 0.01))
                signals.append(Signal(
                    market=m, side=side, strategy="VolumeSpike",
                    model_prob=mkt_price + edge,
                    market_price=mkt_price, edge=round(edge, 4),
                    confidence=round(confidence, 2),
                    emoji="🔊"
                ))
        return signals


class ClosingSoonStrategy:
    """Markets closing within 48h with a price far from 0 or 1 — high urgency."""
    def analyze(self, markets: list[Market], min_edge: float) -> list[Signal]:
        signals = []
        now = utcnow()
        for m in markets:
            if not m.end_date:
                continue
            try:
                end = datetime.fromisoformat(m.end_date.replace("Z",""))
                hours_left = (end - now).total_seconds() / 3600
            except Exception:
                continue
            # Closing in 6–48 hours with price between 0.15 and 0.85
            if 6 <= hours_left <= 48 and 0.15 <= m.yes_price <= 0.85:
                # Price should be resolving toward 0 or 1 — fade the middle
                distance_from_50 = abs(m.yes_price - 0.5)
                if distance_from_50 < 0.15:
                    continue  # too close to 50/50, skip
                side       = "YES" if m.yes_price > 0.5 else "NO"
                mkt_price  = m.yes_price if side == "YES" else m.no_price
                edge       = max(min_edge, distance_from_50 * 0.1)
                confidence = min(0.78, 0.5 + distance_from_50)
                signals.append(Signal(
                    market=m, side=side, strategy="ClosingSoon",
                    model_prob=mkt_price + edge,
                    market_price=mkt_price, edge=round(edge, 4),
                    confidence=round(confidence, 2),
                    emoji="⏰"
                ))
        return signals


# ════════════════════════════════════════════════════════════════════════════════
# RISK MANAGER
# ════════════════════════════════════════════════════════════════════════════════

class RiskManager:
    def __init__(self, cfg: Config, portfolio: Portfolio):
        self.cfg              = cfg
        self.portfolio        = portfolio
        self.daily_start      = portfolio.total_value

    def can_trade(self) -> tuple[bool, str]:
        daily_loss = self.daily_start - self.portfolio.total_value
        if daily_loss >= self.cfg.DAILY_LOSS_LIMIT:
            return False, f"Daily loss limit ${self.cfg.DAILY_LOSS_LIMIT:.0f} reached"
        deployed = self.portfolio.total_value - self.portfolio.cash
        if deployed / max(1, self.portfolio.total_value) >= self.cfg.MAX_PORTFOLIO_EXPOSURE:
            return False, f"Max exposure {self.cfg.MAX_PORTFOLIO_EXPOSURE*100:.0f}% reached"
        if len(self.portfolio.positions) >= self.cfg.MAX_OPEN_POSITIONS:
            return False, f"Max {self.cfg.MAX_OPEN_POSITIONS} positions reached"
        return True, "OK"

    def size_position(self, signal: Signal, price: float) -> float:
        odds   = max(0.01, (1.0 / price) - 1.0)
        kelly  = max(0.0, (signal.model_prob * odds - (1 - signal.model_prob)) / odds)
        kelly  *= self.cfg.KELLY_FRACTION
        size   = kelly * self.portfolio.cash
        return round(min(size, self.cfg.MAX_POSITION_USD, self.portfolio.cash * 0.20), 2)

    def stop_loss(self, entry: float, side: str) -> float:
        if side == "YES":
            return round(entry * (1 - self.cfg.STOP_LOSS_PCT), 4)
        return round(entry * (1 + self.cfg.STOP_LOSS_PCT), 4)

    def check_stops(self) -> list[str]:
        triggered = []
        for pid, pos in self.portfolio.positions.items():
            if pos.side == "YES" and pos.current_price <= pos.stop_loss:
                triggered.append(pid)
            elif pos.side == "NO" and pos.current_price >= pos.stop_loss:
                triggered.append(pid)
        return triggered

    def update_pnl(self, pos: Position, price: float) -> Position:
        pos.current_price = price
        pos.pnl = (price - pos.entry_price) * pos.shares if pos.side == "YES" \
                  else (pos.entry_price - price) * pos.shares
        return pos


# ════════════════════════════════════════════════════════════════════════════════
# TELEGRAM BOT
# ════════════════════════════════════════════════════════════════════════════════

class TelegramBot:
    def __init__(self, token: str, chat_ids: list[str], trading_bot):
        self.token      = token
        self.chat_ids   = chat_ids
        self.bot        = trading_bot
        self.app: Optional[Application] = None

    def build(self):
        if not self.token:
            log.warning("No Telegram token — alerts disabled")
            return
        self.app = Application.builder().token(self.token).build()
        for cmd, fn in [
            ("start",     self._cmd_start),
            ("status",    self._cmd_status),
            ("positions", self._cmd_positions),
            ("pnl",       self._cmd_pnl),
            ("signals",   self._cmd_signals),
            ("stop",      self._cmd_stop),
            ("resume",    self._cmd_resume),
            ("risk",      self._cmd_risk),
            ("close",     self._cmd_close),
            ("leaderboard", self._cmd_leaderboard),
            ("summary",   self._cmd_summary),
            ("help",      self._cmd_start),
        ]:
            self.app.add_handler(CommandHandler(cmd, fn))
        log.info("Telegram bot configured")

    async def broadcast(self, text: str):
        """Send to ALL configured chat IDs."""
        if not self.app or not self.chat_ids:
            return
        for cid in self.chat_ids:
            try:
                await self.app.bot.send_message(
                    chat_id=cid, text=text, parse_mode="HTML"
                )
            except Exception as e:
                log.error(f"Telegram send error to {cid}: {e}")

    async def send(self, text: str):
        await self.broadcast(text)

    # ── Formatted alerts ─────────────────────────────────────────────────────

    async def alert_signal(self, sig: Signal):
        stars = "⭐" * min(5, int(sig.confidence * 5))
        text = (
            f"{sig.emoji} <b>New Signal — {sig.strategy}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>{sig.market.question[:70]}</b>\n\n"
            f"Side:       <b>{sig.side}</b>\n"
            f"Edge:       <b>+{sig.edge*100:.1f}pp</b>\n"
            f"Confidence: <b>{sig.confidence*100:.0f}%</b> {stars}\n"
            f"Model:      <code>{sig.model_prob:.3f}</code> vs "
            f"Market: <code>{sig.market_price:.3f}</code>\n"
            f"Strategy:   <code>{sig.strategy}</code>\n"
            f"Liquidity:  <code>${sig.market.liquidity:,.0f}</code>\n"
            f"Volume 24h: <code>${sig.market.volume_24h:,.0f}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
        )
        await self.broadcast(text)

    async def alert_trade(self, pos: Position, action: str):
        if action == "open":
            text = (
                f"✅ <b>Trade Opened</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📊 <b>{pos.market.question[:60]}…</b>\n\n"
                f"Side:  <b>{pos.side}</b>\n"
                f"Price: <code>{pos.entry_price:.4f}</code>\n"
                f"Size:  <code>${pos.size_usd:.2f} USDC</code>\n"
                f"Stop:  <code>{pos.stop_loss:.4f}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🕐 {utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
            )
        else:
            emoji = "💰" if pos.pnl >= 0 else "❌"
            pnl_s = f"+${pos.pnl:.2f}" if pos.pnl >= 0 else f"-${abs(pos.pnl):.2f}"
            text = (
                f"{emoji} <b>Trade Closed</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📊 <b>{pos.market.question[:60]}…</b>\n\n"
                f"Side:   <b>{pos.side}</b>\n"
                f"Entry:  <code>{pos.entry_price:.4f}</code>\n"
                f"Exit:   <code>{pos.current_price:.4f}</code>\n"
                f"P&L:    <b>{pnl_s}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🕐 {utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
            )
        await self.broadcast(text)

    async def alert_daily(self, portfolio: Portfolio):
        wr   = (portfolio.trades_won / max(1, portfolio.trades_total)) * 100
        pnl  = portfolio.daily_pnl
        sign = "+" if pnl >= 0 else ""
        text = (
            f"📊 <b>Daily Summary — {utcnow().strftime('%Y-%m-%d')}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💼 Portfolio:   <b>${portfolio.total_value:,.2f}</b>\n"
            f"📈 Daily P&L:   <b>{sign}${pnl:.2f}</b>\n"
            f"🏆 All-time:    <b>${portfolio.all_time_pnl:+.2f}</b>\n"
            f"🎯 Win Rate:    <b>{wr:.1f}%</b>\n"
            f"📋 Trades:      <b>{portfolio.trades_total}</b> total\n"
            f"🔍 Signals:     <b>{portfolio.signals_today}</b> today\n"
            f"📂 Open:        <b>{len(portfolio.positions)}</b> positions\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        await self.broadcast(text)

    # ── Commands ─────────────────────────────────────────────────────────────

    async def _cmd_start(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        await u.message.reply_html(
            "🤖 <b>PolyBot v3.0</b> — Polymarket Trading Bot\n\n"
            "<b>Commands:</b>\n"
            "/status — Portfolio &amp; bot status\n"
            "/positions — Open positions\n"
            "/signals — Latest signals\n"
            "/leaderboard — Top signals by edge\n"
            "/pnl — P&amp;L report\n"
            "/summary — Full daily summary\n"
            "/stop — Pause trading\n"
            "/resume — Resume trading\n"
            "/risk [%] — Set max exposure\n"
            "/close [id] — Close a position"
        )

    async def _cmd_status(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        bot  = self.bot
        port = bot.portfolio
        status = "🟢 RUNNING" if bot.running else "🔴 STOPPED"
        deployed = port.total_value - port.cash
        exposure = (deployed / max(1, port.total_value)) * 100
        await u.message.reply_html(
            f"<b>Bot Status: {status}</b>\n\n"
            f"💼 Portfolio: <b>${port.total_value:,.2f}</b>\n"
            f"💵 Cash: <code>${port.cash:,.2f}</code>\n"
            f"📂 Positions: <code>{len(port.positions)}</code>\n"
            f"📊 Exposure: <code>{exposure:.1f}%</code>\n"
            f"📈 Daily P&L: <b>${port.daily_pnl:+.2f}</b>\n"
            f"🔍 Scans: <code>#{bot._scan_count}</code>"
        )

    async def _cmd_positions(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        port = self.bot.portfolio
        if not port.positions:
            await u.message.reply_text("📭 No open positions.")
            return
        lines = []
        for pid, pos in port.positions.items():
            pnl_s = f"+${pos.pnl:.2f}" if pos.pnl >= 0 else f"-${abs(pos.pnl):.2f}"
            lines.append(
                f"• <b>{pos.market.question[:45]}…</b>\n"
                f"  {pos.side} @{pos.entry_price:.4f} | P&L: {pnl_s} | ${pos.size_usd:.0f}"
            )
        await u.message.reply_html(
            f"<b>📂 Open Positions ({len(port.positions)})</b>\n\n" + "\n\n".join(lines)
        )

    async def _cmd_pnl(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        port = self.bot.portfolio
        wr   = (port.trades_won / max(1, port.trades_total)) * 100
        await u.message.reply_html(
            f"<b>📈 P&L Report</b>\n\n"
            f"Today:    <b>${port.daily_pnl:+.2f}</b>\n"
            f"All-time: <b>${port.all_time_pnl:+.2f}</b>\n"
            f"Win rate: <b>{wr:.1f}%</b> ({port.trades_won}/{port.trades_total})"
        )

    async def _cmd_signals(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        sigs = self.bot.latest_signals[:5]
        if not sigs:
            await u.message.reply_text("📭 No active signals.")
            return
        lines = []
        for s in sigs:
            lines.append(
                f"{s.emoji} <b>{s.side}</b> {s.market.question[:40]}…\n"
                f"   Edge: +{s.edge*100:.1f}pp | Conf: {s.confidence*100:.0f}% | {s.strategy}"
            )
        await u.message.reply_html("<b>🔍 Latest Signals</b>\n\n" + "\n\n".join(lines))

    async def _cmd_leaderboard(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        sigs = sorted(self.bot.latest_signals, key=lambda s: s.edge, reverse=True)[:5]
        if not sigs:
            await u.message.reply_text("📭 No signals yet.")
            return
        lines = []
        medals = ["🥇","🥈","🥉","4️⃣","5️⃣"]
        for i, s in enumerate(sigs):
            lines.append(
                f"{medals[i]} <b>{s.side}</b> {s.market.question[:38]}…\n"
                f"   +{s.edge*100:.1f}pp edge | {s.confidence*100:.0f}% conf | {s.strategy}"
            )
        await u.message.reply_html("<b>🏆 Top Signals by Edge</b>\n\n" + "\n\n".join(lines))

    async def _cmd_summary(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        await self.bot.tg.alert_daily(self.bot.portfolio)

    async def _cmd_stop(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        self.bot.running = False
        await u.message.reply_text("🔴 Bot paused. Use /resume to restart.")

    async def _cmd_resume(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        self.bot.running = True
        await u.message.reply_text("🟢 Bot resumed!")

    async def _cmd_risk(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        try:
            pct = float(c.args[0].strip('%')) / 100
            self.bot.cfg.MAX_PORTFOLIO_EXPOSURE = pct
            await u.message.reply_text(f"✅ Max exposure set to {pct*100:.0f}%")
        except Exception:
            await u.message.reply_text("Usage: /risk 40")

    async def _cmd_close(self, u: Update, c: ContextTypes.DEFAULT_TYPE):
        if not c.args:
            await u.message.reply_text("Usage: /close [position_id]")
            return
        pid = c.args[0]
        if pid in self.bot.portfolio.positions:
            await self.bot.close_position(pid, reason="manual")
            await u.message.reply_text(f"✅ Position {pid} closed.")
        else:
            await u.message.reply_text(f"❌ Position {pid} not found.")


# ════════════════════════════════════════════════════════════════════════════════
# MAIN TRADING BOT
# ════════════════════════════════════════════════════════════════════════════════

class TradingBot:
    def __init__(self):
        self.cfg             = Config()
        self.portfolio       = Portfolio()
        self.client          = PolymarketClient(self.cfg)
        self.model           = ProbabilityModel()
        self.risk            = RiskManager(self.cfg, self.portfolio)
        self.running         = True
        self.latest_signals: list[Signal] = []
        self._scan_count     = 0
        self._last_daily     = utcnow().date()

        # ── Signal cooldown tracker: key → timestamp ──
        self._signal_sent: dict[str, float] = {}

        # ── Strategies ──
        self.strat_arb      = ArbitrageStrategy()
        self.strat_news     = NewsCorrelationStrategy(self.model)
        self.strat_meanrev  = MeanReversionStrategy()
        self.strat_volume   = VolumeSpikeStrategy()
        self.strat_closing  = ClosingSoonStrategy()

        # ── Telegram ──
        self.tg = TelegramBot(self.cfg.TELEGRAM_TOKEN, self.cfg.chat_ids, self)
        self.tg.build()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        await self.client.start()
        log.info("PolyBot v3.0 started")
        await self.tg.send(
            "🤖 <b>PolyBot v3.0 Started</b>\n\n"
            "🔍 Scanning Polymarket…\n"
            "⚡ Arbitrage | 📰 News | 📊 MeanRev | 🔊 Volume | ⏰ ClosingSoon\n\n"
            "Type /help for commands"
        )

        if self.tg.app:
            await self.tg.app.initialize()
            await self.tg.app.updater.start_polling(drop_pending_updates=True)
            await self.tg.app.start()

        while True:
            try:
                if self.running:
                    await self.scan_cycle()
                await self._daily_check()
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

    # ── Scan ──────────────────────────────────────────────────────────────────

    async def scan_cycle(self):
        self._scan_count += 1
        log.info(f"Scan #{self._scan_count} started")

        markets = await self.client.get_markets(limit=300)
        log.info(f"Fetched {len(markets)} markets")

        markets = self._filter_markets(markets)
        log.info(f"After filtering: {len(markets)} markets")

        await self._update_positions(markets)

        signals = self._generate_signals(markets)
        self.latest_signals = signals
        log.info(f"Signals generated: {len(signals)}")

        # Process top 3 signals only
        sent = 0
        for sig in signals:
            if sent >= 3:
                break
            await self._process_signal(sig)
            sent += 1

    def _filter_markets(self, markets: list[Market]) -> list[Market]:
        cfg    = self.cfg
        seen   = set()
        result = []
        for m in markets:
            if m.liquidity < cfg.MIN_MARKET_LIQUIDITY:
                continue
            if not (cfg.PRICE_MIN <= m.yes_price <= cfg.PRICE_MAX):
                continue
            if m.condition_id in self.portfolio.positions:
                continue
            if m.condition_id in seen:
                continue
            seen.add(m.condition_id)
            result.append(m)
        return result

    def _generate_signals(self, markets: list[Market]) -> list[Signal]:
        signals = []
        if self.cfg.ENABLE_ARBITRAGE:
            signals += self.strat_arb.analyze(markets, self.model, self.cfg.MIN_EDGE_REQUIRED)
        if self.cfg.ENABLE_NEWS_CORR:
            signals += self.strat_news.analyze(markets, self.cfg.MIN_EDGE_REQUIRED)
        if self.cfg.ENABLE_MEAN_REV:
            signals += self.strat_meanrev.analyze(markets, self.cfg.MIN_EDGE_REQUIRED)
        if self.cfg.ENABLE_VOLUME_SPIKE:
            signals += self.strat_volume.analyze(markets, self.cfg.MIN_EDGE_REQUIRED)
        if self.cfg.ENABLE_CLOSING_SOON:
            signals += self.strat_closing.analyze(markets, self.cfg.MIN_EDGE_REQUIRED)

        # Filter by confidence
        signals = [s for s in signals if s.confidence >= self.cfg.MIN_CONFIDENCE]

        # Sort by edge descending
        signals.sort(key=lambda s: s.edge, reverse=True)

        # Remove duplicate markets — keep best signal per market
        seen_markets = set()
        unique = []
        for s in signals:
            if s.market.condition_id not in seen_markets:
                seen_markets.add(s.market.condition_id)
                unique.append(s)
        return unique

    def _is_on_cooldown(self, sig: Signal) -> bool:
        key       = sig.signal_key
        last_sent = self._signal_sent.get(key, 0)
        return (time.time() - last_sent) < self.cfg.SIGNAL_COOLDOWN_SEC

    def _mark_sent(self, sig: Signal):
        self._signal_sent[sig.signal_key] = time.time()
        # Cleanup old entries
        cutoff = time.time() - self.cfg.SIGNAL_COOLDOWN_SEC * 2
        self._signal_sent = {k: v for k, v in self._signal_sent.items() if v > cutoff}

    async def _process_signal(self, sig: Signal):
        # ── Cooldown check ──
        if self._is_on_cooldown(sig):
            log.info(f"Signal on cooldown: {sig.market.question[:40]}")
            return

        self._mark_sent(sig)
        self.portfolio.signals_today += 1

        # ── Send Telegram alert ──
        await self.tg.alert_signal(sig)

        # ── Risk check before trading ──
        can, reason = self.risk.can_trade()
        if not can:
            log.warning(f"Risk block: {reason}")
            return

        price    = sig.market.yes_price if sig.side == "YES" else sig.market.no_price
        size_usd = self.risk.size_position(sig, price)
        if size_usd < 10:
            return

        token_id = sig.market.token_id_yes if sig.side == "YES" else sig.market.token_id_no
        resp     = await self.client.place_order(token_id, sig.side, size_usd, price)
        order_id = resp.get("id") or resp.get("orderID") or f"pos_{int(time.time())}"

        pos = Position(
            id=order_id, market=sig.market, side=sig.side,
            entry_price=price, current_price=price,
            size_usd=size_usd, shares=size_usd / price,
            stop_loss=self.risk.stop_loss(price, sig.side),
        )
        self.portfolio.positions[order_id] = pos
        self.portfolio.cash -= size_usd
        self.portfolio.trades_total += 1

        log.info(f"Position: {sig.side} {sig.market.question[:40]} @{price:.4f} ${size_usd:.2f}")
        await self.tg.alert_trade(pos, "open")

    async def _update_positions(self, markets: list[Market]):
        mkt_map = {m.condition_id: m for m in markets}
        for pid, pos in list(self.portfolio.positions.items()):
            mkt = mkt_map.get(pos.market.condition_id)
            if not mkt:
                continue
            curr = mkt.yes_price if pos.side == "YES" else mkt.no_price
            self.portfolio.positions[pid] = self.risk.update_pnl(pos, curr)

        for pid in self.risk.check_stops():
            await self.close_position(pid, reason="stop_loss")

    async def close_position(self, pid: str, reason: str = "manual"):
        pos = self.portfolio.positions.pop(pid, None)
        if not pos:
            return
        self.portfolio.cash          += pos.size_usd + pos.pnl
        self.portfolio.total_value    = self.portfolio.cash + sum(
            p.size_usd for p in self.portfolio.positions.values()
        )
        self.portfolio.daily_pnl     += pos.pnl
        self.portfolio.all_time_pnl  += pos.pnl
        if pos.pnl > 0:
            self.portfolio.trades_won += 1
        log.info(f"Position closed ({reason}): {pid} | P&L: ${pos.pnl:+.2f}")
        await self.tg.alert_trade(pos, "close")

    async def _daily_check(self):
        today = utcnow().date()
        if today != self._last_daily:
            self._last_daily = today
            await self.tg.alert_daily(self.portfolio)
            self.portfolio.daily_pnl    = 0.0
            self.portfolio.signals_today = 0
            self.risk.daily_start       = self.portfolio.total_value


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
