"""
PolyBot v5.0 — Polymarket Automated Trading Bot
================================================

LATEST UPDATE — v5.0 (major upgrade):
  ✅ NEW: Bayesian signal model — log-space numerically stable updates
  ✅ NEW: Z-score mispricing filter — S = (p_model - p_mkt) / σ
  ✅ NEW: Improved Kelly — fractional kelly with variance reduction
  ✅ NEW: Sharpe Ratio tracking — target SR > 1.5
  ✅ NEW: Max Drawdown guard — hard stop if MDD > 8%
  ✅ NEW: Profit Factor — gross_profit / gross_loss
  ✅ NEW: Value at Risk 95% — VAR = μ - 1.645·σ
  ✅ NEW: Performance targets — Win Rate >70%, Sharpe >1.5
  ✅ NEW: Market scoring — combined multi-factor signal score
  ✅ NEW: Signal deduplication improved — per-strategy cooldown
  ✅ NEW: /metrics endpoint — full performance dashboard
  ✅ KEPT: All v4.2 fixes — no crash, no conflicts, wallet balance

v4.2:
  ✅ Bot starts STOPPED, wallet shown in TG, TP/SL from frontend
  ✅ Telegram conflict fixed, settings persistence, balance fetch

v4.0–4.1:
  ✅ Web API, frontend settings, real-time dashboard, 5 strategies
"""

import os, asyncio, json, time, logging, hashlib, math
from datetime import datetime, timezone
from dataclasses import dataclass, field
from collections import deque
from typing import Optional
from dotenv import load_dotenv
from aiohttp import web as aioWeb
import aiohttp
import numpy as np
from web3 import Web3
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S", level=logging.INFO
)
log = logging.getLogger("polybot")

def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ════════════════════════════════════════════════════════════════════════════
# PERSISTENCE
# ════════════════════════════════════════════════════════════════════════════

SETTINGS_FILE  = "settings.json"
COOLDOWN_FILE  = "cooldown_state.json"
USER_STORE     = "user_store.json"

def load_file(path=SETTINGS_FILE) -> dict:
    try:
        if os.path.exists(path):
            with open(path) as f:
                d = json.load(f)
                if d: return d
    except Exception: pass
    if path == SETTINGS_FILE:
        env = os.getenv("BOT_SETTINGS","")
        if env:
            try: return json.loads(env)
            except: pass
    return {}

def save_file(data: dict, path=SETTINGS_FILE):
    try:
        with open(path,"w") as f: json.dump(data, f, indent=2)
        log.info(f"✅ Saved {path}: {[k for k in data if k!='privateKey']}")
    except Exception as e: log.error(f"Save {path} error: {e}")

def load_cooldown() -> dict:
    try:
        if os.path.exists(COOLDOWN_FILE):
            with open(COOLDOWN_FILE) as f: return json.load(f)
    except: pass
    return {}

def save_cooldown(d: dict):
    try:
        with open(COOLDOWN_FILE,"w") as f: json.dump(d, f)
    except: pass

def wallet_key(addr: str) -> str:
    a = addr.strip().lower()
    return a[:10]+a[-6:] if len(a)>16 else a


# ════════════════════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════════════════════

class Config:
    def __init__(self): self.reload()

    def reload(self):
        s = load_file()
        log.info(f"Config: {json.dumps({k:v for k,v in s.items() if k!='privateKey'})}")

        def g(k,e,d=""): v=s.get(k); return str(v).strip() if v and not str(v).startswith("0x•") else os.getenv(e,d)
        def gf(k,e,d):
            v=s.get(k)
            if v:
                try: r=float(v); log.info(f"  {k}={r} (settings)"); return r
                except: pass
            ev=os.getenv(e)
            if ev: log.info(f"  {k}={float(ev)} (env {e})"); return float(ev)
            log.info(f"  {k}={d} (default)"); return float(d)
        def gi(k,e,d):
            v=s.get(k)
            if v:
                try: return int(float(v))
                except: pass
            return int(float(os.getenv(e,str(d))))
        def gb(k,e,d):
            v=s.get(k)
            if v is not None: return str(v).lower() in("true","1","yes")
            return str(os.getenv(e,str(d))).lower() in("true","1","yes")

        self.PRIVATE_KEY  = g("privateKey","POLY_PRIVATE_KEY")
        self.TG_TOKEN     = g("tgToken","TELEGRAM_TOKEN")
        self.TG_CHAT_ID   = g("tgChatId","TELEGRAM_CHAT_ID")
        self.POLYGON_RPC  = g("polygonRpc","POLYGON_RPC","https://polygon-rpc.com")
        self.API_SECRET   = g("apiSecret","API_SECRET","polybot123")

        raw_exp  = gf("maxExposure","MAX_EXPOSURE",50)
        raw_stop = gf("stopLoss","STOP_LOSS_PCT",20)
        raw_edge = gf("minEdge","MIN_EDGE_PCT",5)
        raw_conf = gf("minConf","MIN_CONFIDENCE",55)

        self.MAX_POSITION = gf("maxPosition","MAX_POSITION_USD",500)
        self.MAX_EXPOSURE = raw_exp/100  if raw_exp>1  else raw_exp
        self.STOP_LOSS    = raw_stop/100 if raw_stop>1 else raw_stop
        self.MIN_EDGE     = raw_edge/100 if raw_edge>1 else raw_edge
        self.DAILY_LIMIT  = gf("dailyLimit","DAILY_LOSS_USD",100)
        self.MAX_POSITIONS= gi("maxPositions","MAX_POSITIONS",10)
        self.MIN_CONF     = raw_conf/100 if raw_conf>1 else raw_conf
        self.KELLY        = 0.25           # fractional Kelly (never full)
        self.COOLDOWN     = gi("cooldown","SIGNAL_COOLDOWN",3600)
        self.SCAN_INTERVAL= gi("scanInterval","SCAN_INTERVAL",60)
        self.MIN_LIQUIDITY= gf("minLiquidity","MIN_LIQUIDITY",5000)
        self.PRICE_MIN    = gf("priceMin","PRICE_MIN",0.05)
        self.PRICE_MAX    = gf("priceMax","PRICE_MAX",0.95)
        self.MARKET_LIMIT = gi("marketLimit","MARKET_LIMIT",300)
        self.API_PORT     = int(os.getenv("PORT","8080"))

        # v5.0 performance targets (from research docs)
        self.TARGET_WIN_RATE    = 0.70   # >70%
        self.TARGET_SHARPE      = 1.5    # SR > 1.5
        self.MAX_DRAWDOWN       = 0.08   # hard stop at 8% MDD
        self.MIN_PROFIT_FACTOR  = 1.5    # PF > 1.5
        self.MIN_Z_SCORE        = 1.5    # min mispricing z-score

        # TP/SL user override
        raw_tp = s.get("tpPct") or os.getenv("TP_PCT","")
        raw_sl = s.get("slPct") or os.getenv("SL_PCT","")
        try: self.TP_PCT = float(raw_tp)/100 if raw_tp else None
        except: self.TP_PCT = None
        try: self.SL_PCT = float(raw_sl)/100 if raw_sl else None
        except: self.SL_PCT = None

        strats = s.get("strategies",{})
        self.STRAT_ARB     = strats.get("arb",    gb("arb","STRAT_ARB",True))
        self.STRAT_NEWS    = strats.get("news",   gb("news","STRAT_NEWS",True))
        self.STRAT_MEANREV = strats.get("meanrev",gb("meanrev","STRAT_MEANREV",True))
        self.STRAT_VOL     = strats.get("vol",    gb("vol","STRAT_VOL",True))
        self.STRAT_CLOSE   = strats.get("close",  gb("close","STRAT_CLOSE",True))

        self.CLOB  = "https://clob.polymarket.com"
        self.GAMMA = "https://gamma-api.polymarket.com"

        log.info(f"MaxPos=${self.MAX_POSITION} SL={self.STOP_LOSS*100:.0f}% "
                 f"TP={'none' if not self.TP_PCT else f'{self.TP_PCT*100:.0f}%'} "
                 f"Kelly={self.KELLY} MinZ={self.MIN_Z_SCORE}")

    @property
    def chat_ids(self): return [c.strip() for c in self.TG_CHAT_ID.split(",") if c.strip()]


# ════════════════════════════════════════════════════════════════════════════
# DATA MODELS
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Market:
    condition_id: str; question: str
    yes_price: float; no_price: float
    volume_24h: float; liquidity: float
    category: str; end_date: str
    token_id_yes: str = ""; token_id_no: str = ""

@dataclass
class Signal:
    market: Market; side: str; strategy: str
    model_prob: float; market_price: float
    edge: float; confidence: float
    z_score: float = 0.0          # v5.0 — mispricing z-score
    ev: float = 0.0               # v5.0 — expected value
    emoji: str = "📐"
    timestamp: str = field(default_factory=lambda: utcnow().isoformat())

    @property
    def key(self):
        return hashlib.md5(f"{self.market.condition_id}{self.side}{self.strategy}".encode()).hexdigest()[:12]

@dataclass
class Position:
    id: str; market: Market; side: str
    entry_price: float; current_price: float
    size_usd: float; shares: float
    pnl: float = 0.0; stop_loss: float = 0.0
    take_profit_price: float = 0.0
    opened_at: str = field(default_factory=lambda: utcnow().isoformat())

@dataclass
class Portfolio:
    total_value: float = 0.0; cash: float = 0.0
    positions: dict = field(default_factory=dict)
    daily_pnl: float = 0.0; all_time_pnl: float = 0.0
    trades_won: int = 0; trades_total: int = 0
    gross_profit: float = 0.0; gross_loss: float = 0.0
    signals_today: int = 0
    # v5.0 performance tracking
    returns: deque = field(default_factory=lambda: deque(maxlen=100))
    peak_value: float = 0.0


# ════════════════════════════════════════════════════════════════════════════
# v5.0 BAYESIAN MODEL
# ════════════════════════════════════════════════════════════════════════════

class BayesianModel:
    """
    Bayesian signal model with log-space numerical stability.
    From: Quantitative Prediction Market Research v2.3.1

    log P(H|D) = log P(H) + Σ log P(Dk|H) - log Z
    EV = p̂ - p  (positive EV = profitable trade)
    S  = (p_model - p_mkt) / σ  (z-score mispricing)
    """

    # Base rates by category (prior probabilities)
    PRIORS = {
        "politics":   0.48, "crypto":  0.42, "economics": 0.55,
        "sports":     0.50, "science": 0.52, "default":   0.50,
    }

    # News sentiment keywords → log-likelihood ratios
    KEYWORDS = {
        "rate cut": +0.6, "rate hike": -0.4, "inflation": -0.3,
        "bitcoin":  +0.4, "ethereum":  +0.3, "crypto":    +0.2,
        "election": +0.2, "win":       +0.4, "lose":      -0.4,
        "approval": +0.5, "veto":      -0.4, "recession": -0.5,
        "crash":    -0.6, "ban":       -0.5, "pump":      +0.4,
        "war":      -0.4, "peace":     +0.3, "ai":        +0.5,
        "lawsuit":  -0.3, "partnership": +0.3,
    }

    def __init__(self):
        self._price_history: dict[str, deque] = {}  # condition_id → deque of prices

    def _get_prior(self, category: str) -> float:
        return self.PRIORS.get(category.lower(), self.PRIORS["default"])

    def _sentiment_llr(self, question: str) -> float:
        """Compute log-likelihood ratio from news keywords."""
        q = question.lower()
        llr = sum(w for kw, w in self.KEYWORDS.items() if kw in q)
        return max(-1.5, min(1.5, llr))  # clip to [-1.5, 1.5]

    def update(self, market: Market) -> dict:
        """
        Bayesian update: compute calibrated probability, EV, z-score.
        Returns dict with model_prob, ev, z_score, sigma.
        """
        cid = market.condition_id

        # Track price history for rolling stats
        if cid not in self._price_history:
            self._price_history[cid] = deque(maxlen=168)  # 1 week at 1hr
        self._price_history[cid].append(market.yes_price)

        p_mkt = market.yes_price
        hist  = self._price_history[cid]

        # Prior (base rate for category)
        p_prior = self._get_prior(market.category)

        # Bayesian update in log-space (numerically stable)
        # log P(H|D) = log P(H) + Σ log P(Dk|H) - log Z
        log_prior = math.log(p_prior / (1 - p_prior))   # log-odds prior
        llr       = self._sentiment_llr(market.question) # evidence

        # Market price as additional evidence (Bayesian fusion)
        log_market = math.log(max(0.01, p_mkt) / max(0.01, 1 - p_mkt))

        # Weighted fusion (40% prior, 35% market, 25% sentiment)
        log_posterior = 0.40 * log_prior + 0.35 * log_market + 0.25 * llr

        # Convert back to probability
        p_model = 1 / (1 + math.exp(-log_posterior))
        p_model = max(0.01, min(0.99, p_model))

        # Expected Value (EV = p̂ - p)
        ev = p_model - p_mkt

        # Z-score mispricing: S = (p_model - p_mkt) / σ
        if len(hist) >= 5:
            sigma = float(np.std(list(hist))) + 1e-6
        else:
            sigma = 0.05  # default when insufficient history
        z_score = ev / sigma

        return {
            "model_prob": round(p_model, 4),
            "ev":         round(ev, 4),
            "z_score":    round(z_score, 2),
            "sigma":      round(sigma, 4),
            "edge":       round(abs(ev), 4),
        }

    def compute_confidence(self, ev: float, z_score: float, liquidity: float) -> float:
        """Multi-factor confidence score."""
        # EV component (normalized)
        ev_score = min(1.0, abs(ev) / 0.20)
        # Z-score component
        z_score_comp = min(1.0, abs(z_score) / 3.0)
        # Liquidity component (log-scaled)
        liq_score = min(1.0, math.log10(max(1, liquidity)) / 6.0)
        # Weighted combination
        conf = 0.45 * ev_score + 0.35 * z_score_comp + 0.20 * liq_score
        return round(max(0.0, min(1.0, conf)), 3)


# ════════════════════════════════════════════════════════════════════════════
# POLYMARKET CLIENT
# ════════════════════════════════════════════════════════════════════════════

class PolyClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg; self.session = None; self._acct = None
        self._init_wallet()

    def _init_wallet(self):
        if self.cfg.PRIVATE_KEY:
            try:
                w3 = Web3(Web3.HTTPProvider(self.cfg.POLYGON_RPC))
                self._acct = w3.eth.account.from_key(self.cfg.PRIVATE_KEY)
                log.info(f"Wallet: {self._acct.address[:10]}…")
            except Exception as e: log.error(f"Wallet error: {e}")

    async def start(self):
        self.session = aiohttp.ClientSession(headers={"User-Agent":"PolyBot/5.0"})

    async def stop(self):
        if self.session: await self.session.close()

    async def get_usdc_balance(self) -> float:
        if not self._acct: return 0.0
        addr = self._acct.address
        log.info(f"Fetching balance for {addr[:10]}…")

        # Method 1: Polymarket CLOB
        try:
            async with self.session.get(
                f"{self.cfg.CLOB}/balance-allowance",
                params={"asset_type":"COLLATERAL","account":addr}, timeout=10
            ) as r:
                log.info(f"  CLOB status: {r.status}")
                if r.status == 200:
                    d = await r.json()
                    log.info(f"  CLOB response: {d}")
                    raw = float(d.get("balance",0) or 0)
                    bal = raw/1_000_000 if raw > 1000 else raw
                    if bal > 0: log.info(f"  ✅ CLOB: ${bal}"); return round(bal,2)
        except Exception as e: log.warning(f"  CLOB error: {e}")

        # Method 2: eth_call to USDC contracts
        padded = addr[2:].lower().zfill(64)
        call_data = "0x70a08231" + padded
        for contract in ["0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
                         "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"]:
            for rpc in ["https://polygon.llamarpc.com","https://polygon.drpc.org"]:
                try:
                    async with self.session.post(rpc, json={
                        "jsonrpc":"2.0","method":"eth_call",
                        "params":[{"to":contract,"data":call_data},"latest"],"id":1
                    }, timeout=6) as r:
                        if r.status == 200:
                            resp = await r.json()
                            result = resp.get("result","0x0")
                            if result and result != "0x" and len(result) > 2:
                                bal = round(int(result,16)/1_000_000, 2)
                                if bal > 0: log.info(f"  ✅ eth_call: ${bal}"); return bal
                except Exception as e: log.debug(f"  RPC error: {e}")

        log.warning("All balance methods failed")
        return 0.0

    async def get_markets(self) -> list[Market]:
        try:
            async with self.session.get(
                f"{self.cfg.GAMMA}/markets",
                params={"active":"true","closed":"false","limit":self.cfg.MARKET_LIMIT},
                timeout=15
            ) as r:
                if r.status != 200: return []
                data = await r.json()
                out = []
                for m in data:
                    try:
                        tokens = m.get("tokens",[])
                        yt = next((t for t in tokens if t.get("outcome","").upper()=="YES"),{})
                        nt = next((t for t in tokens if t.get("outcome","").upper()=="NO"),{})
                        out.append(Market(
                            condition_id=m.get("conditionId",""),
                            question=m.get("question",""),
                            yes_price=float(yt.get("price",0.5)),
                            no_price=float(nt.get("price",0.5)),
                            volume_24h=float(m.get("volume24hr",0)),
                            liquidity=float(m.get("liquidity",0)),
                            category=m.get("category","unknown"),
                            end_date=m.get("endDate",""),
                            token_id_yes=yt.get("tokenId",""),
                            token_id_no=nt.get("tokenId",""),
                        ))
                    except: continue
                return out
        except Exception as e:
            log.error(f"get_markets: {e}"); return []

    async def place_order(self, token_id, side, size_usdc, price) -> dict:
        if not self._acct:
            return {"status":"paper","id":f"paper_{int(time.time())}"}
        order = {
            "orderType":"FOK","tokenID":token_id,
            "price":str(round(price,4)),"size":str(round(size_usdc/price,2)),
            "side":side.lower(),"feeRateBps":"0",
            "nonce":str(int(time.time()*1000)),
            "signer":self._acct.address,"maker":self._acct.address
        }
        try:
            from eth_account.messages import encode_defunct
            w3 = Web3(Web3.HTTPProvider(self.cfg.POLYGON_RPC))
            signed = self._acct.sign_message(
                encode_defunct(Web3.keccak(text=json.dumps(order,sort_keys=True)))
            )
            order["signature"] = signed.signature.hex()
            async with self.session.post(
                f"{self.cfg.CLOB}/order",
                json={"order":order,"owner":self._acct.address}, timeout=15
            ) as r:
                return await r.json()
        except Exception as e:
            log.error(f"place_order: {e}"); return {}


# ════════════════════════════════════════════════════════════════════════════
# v5.0 STRATEGIES — all use BayesianModel
# ════════════════════════════════════════════════════════════════════════════

class ArbStrategy:
    """Arbitrage: external probability vs Polymarket price."""
    EXT = {
        "will democrats":0.52,"federal reserve":0.70,"bitcoin":0.38,
        "elon":0.60,"trump":0.55,"recession":0.35,"rate cut":0.68,
        "election":0.50,"ethereum":0.40,"inflation":0.35,"ai":0.60,
        "climate":0.45,"war":0.30,"peace":0.65,
    }
    def analyze(self, markets, model: BayesianModel, cfg: Config) -> list[Signal]:
        out = []
        for m in markets:
            ext = next((v for k,v in self.EXT.items() if k in m.question.lower()), None)
            if ext is None: continue
            r = model.update(m)
            ev = ext - m.yes_price
            if abs(ev) < cfg.MIN_EDGE: continue
            side = "YES" if ev > 0 else "NO"
            conf = model.compute_confidence(r["ev"], r["z_score"], m.liquidity)
            if conf < cfg.MIN_CONF: continue
            if abs(r["z_score"]) < cfg.MIN_Z_SCORE: continue
            out.append(Signal(
                market=m, side=side, strategy="Arbitrage",
                model_prob=ext if side=="YES" else 1-ext,
                market_price=m.yes_price,
                edge=round(abs(ev),4), confidence=conf,
                z_score=r["z_score"], ev=r["ev"], emoji="⚡"
            ))
        return out

class NewsStrategy:
    """News correlation: sentiment-adjusted Bayesian model vs market."""
    def analyze(self, markets, model: BayesianModel, cfg: Config) -> list[Signal]:
        out = []
        for m in markets:
            r = model.update(m)
            if abs(r["ev"]) < cfg.MIN_EDGE: continue
            if abs(r["z_score"]) < cfg.MIN_Z_SCORE: continue
            side = "YES" if r["ev"] > 0 else "NO"
            conf = model.compute_confidence(r["ev"], r["z_score"], m.liquidity)
            if conf < cfg.MIN_CONF: continue
            out.append(Signal(
                market=m, side=side, strategy="NewsCorr",
                model_prob=r["model_prob"], market_price=m.yes_price,
                edge=r["edge"], confidence=conf,
                z_score=r["z_score"], ev=r["ev"], emoji="📰"
            ))
        return out

class MeanRevStrategy:
    """Mean reversion: fade price deviations > 2σ from rolling average."""
    def __init__(self): self._hist: dict[str, deque] = {}

    def analyze(self, markets, model: BayesianModel, cfg: Config) -> list[Signal]:
        out = []
        for m in markets:
            cid = m.condition_id
            if cid not in self._hist: self._hist[cid] = deque(maxlen=168)
            self._hist[cid].append(m.yes_price)
            if len(self._hist[cid]) < 10: continue
            arr = np.array(self._hist[cid])
            mu, sigma = arr.mean(), arr.std()
            if sigma < 0.005: continue
            z = (m.yes_price - mu) / sigma
            if abs(z) < 2.0: continue           # only 2σ+ moves
            side = "NO" if z > 0 else "YES"
            r = model.update(m)
            ev = abs(r["ev"])
            if ev < cfg.MIN_EDGE: continue
            conf = model.compute_confidence(ev, abs(z), m.liquidity)
            if conf < cfg.MIN_CONF: continue
            out.append(Signal(
                market=m, side=side, strategy="MeanRevert",
                model_prob=mu if side=="YES" else 1-mu,
                market_price=m.yes_price,
                edge=round(min(0.15, ev), 4), confidence=conf,
                z_score=round(z,2), ev=round(ev,4), emoji="📊"
            ))
        return out

class VolSpikeStrategy:
    """Volume spike: 3x+ unusual volume = informed trading, follow direction."""
    def __init__(self): self._vol_hist: dict[str, deque] = {}

    def analyze(self, markets, model: BayesianModel, cfg: Config) -> list[Signal]:
        out = []
        for m in markets:
            cid = m.condition_id
            if cid not in self._vol_hist: self._vol_hist[cid] = deque(maxlen=48)
            self._vol_hist[cid].append(m.volume_24h)
            if len(self._vol_hist[cid]) < 5 or m.volume_24h == 0: continue
            avg = np.mean(list(self._vol_hist[cid])[:-1])
            if avg < 100: continue
            spike = m.volume_24h / avg
            if spike < 3.0: continue            # 3x threshold
            r = model.update(m)
            side = "YES" if m.yes_price > 0.5 else "NO"
            mp = m.yes_price if side == "YES" else m.no_price
            edge = round(max(cfg.MIN_EDGE, min(0.12, (spike-3)*0.01)), 4)
            conf = model.compute_confidence(edge, r["z_score"], m.liquidity)
            if conf < cfg.MIN_CONF: continue
            out.append(Signal(
                market=m, side=side, strategy="VolumeSpike",
                model_prob=mp+edge, market_price=mp,
                edge=edge, confidence=conf,
                z_score=r["z_score"], ev=round(spike,1), emoji="🔊"
            ))
        return out

class ClosingStrategy:
    """Closing soon: markets resolving 6–48hrs with strong directional momentum."""
    def analyze(self, markets, model: BayesianModel, cfg: Config) -> list[Signal]:
        out = []
        now = utcnow()
        for m in markets:
            if not m.end_date: continue
            try:
                hrs = (datetime.fromisoformat(m.end_date.replace("Z","")) - now).total_seconds()/3600
            except: continue
            if not (6 <= hrs <= 48): continue
            if not (0.15 <= m.yes_price <= 0.85): continue
            d = abs(m.yes_price - 0.5)
            if d < 0.15: continue               # need strong direction
            r = model.update(m)
            side = "YES" if m.yes_price > 0.5 else "NO"
            mp = m.yes_price if side=="YES" else m.no_price
            edge = round(max(cfg.MIN_EDGE, d*0.10), 4)
            conf = model.compute_confidence(edge, r["z_score"], m.liquidity)
            if conf < cfg.MIN_CONF: continue
            out.append(Signal(
                market=m, side=side, strategy="ClosingSoon",
                model_prob=mp+edge, market_price=mp,
                edge=edge, confidence=conf,
                z_score=r["z_score"], ev=round(d,3), emoji="⏰"
            ))
        return out


# ════════════════════════════════════════════════════════════════════════════
# v5.0 RISK MANAGER — with Sharpe, MDD, VaR, Profit Factor
# ════════════════════════════════════════════════════════════════════════════

class Risk:
    def __init__(self, cfg: Config, port: Portfolio):
        self.cfg = cfg
        self.port = port
        self.day_start = 0.0

    def can_trade(self) -> tuple[bool, str]:
        # Daily loss limit
        if self.day_start > 0 and (self.day_start - self.port.total_value) >= self.cfg.DAILY_LIMIT:
            return False, "Daily loss limit reached"
        # Max exposure
        deployed = sum(p.size_usd for p in self.port.positions.values())
        if deployed / max(1, self.port.total_value) >= self.cfg.MAX_EXPOSURE:
            return False, "Max exposure reached"
        # Max positions
        if len(self.port.positions) >= self.cfg.MAX_POSITIONS:
            return False, "Max positions reached"
        # v5.0: Max drawdown guard (hard stop)
        mdd = self.max_drawdown()
        if mdd > self.cfg.MAX_DRAWDOWN:
            return False, f"Max drawdown {mdd*100:.1f}% exceeded ({self.cfg.MAX_DRAWDOWN*100:.0f}% limit)"
        return True, "OK"

    def kelly_size(self, sig: Signal, price: float) -> float:
        """
        Fractional Kelly position sizing.
        f = (p·b - q) / b  where b = decimal odds - 1
        Use 25% Kelly (fractional) to reduce variance.
        """
        p = sig.model_prob
        b = max(0.01, (1/price) - 1)   # decimal odds - 1
        q = 1 - p
        f_kelly = max(0, (p*b - q) / b)
        f_frac  = f_kelly * self.cfg.KELLY   # fractional Kelly
        # Clamp to max position and 20% of cash
        size = round(min(f_frac * self.port.cash, self.cfg.MAX_POSITION, self.port.cash*0.20), 2)
        log.info(f"  Kelly: p={p:.3f} b={b:.3f} f={f_kelly:.3f} frac={f_frac:.3f} size=${size}")
        return size

    def stop_price(self, entry: float, side: str) -> float:
        sl = self.cfg.SL_PCT if self.cfg.SL_PCT else self.cfg.STOP_LOSS
        return round(entry * (1 - sl), 4)

    def tp_price(self, entry: float, side: str) -> float:
        if not self.cfg.TP_PCT: return 0.0
        return round(entry * (1 + self.cfg.TP_PCT), 4)

    def check_exits(self) -> list[tuple[str,str]]:
        to_close = []
        for pid, p in self.port.positions.items():
            if p.current_price <= p.stop_loss:
                to_close.append((pid, "stop_loss"))
            elif p.take_profit_price and p.current_price >= p.take_profit_price:
                to_close.append((pid, "take_profit"))
        return to_close

    def update_pnl(self, pos: Position, price: float) -> Position:
        pos.current_price = price
        pos.pnl = (price - pos.entry_price) * pos.shares
        return pos

    # ── v5.0 Performance Metrics ──────────────────────────────────────────

    def sharpe_ratio(self) -> float:
        """SR = (avg_return - 0) / std_return  (risk-free ≈ 0 for crypto)"""
        r = list(self.port.returns)
        if len(r) < 5: return 0.0
        arr = np.array(r)
        return round(float(arr.mean() / (arr.std() + 1e-8)), 3)

    def max_drawdown(self) -> float:
        """MDD = (Peak - Trough) / Peak"""
        if self.port.peak_value <= 0: return 0.0
        current = self.port.total_value
        return round((self.port.peak_value - current) / self.port.peak_value, 4)

    def value_at_risk_95(self) -> float:
        """VAR 95% = μ - 1.645·σ  (daily loss threshold)"""
        r = list(self.port.returns)
        if len(r) < 10: return 0.0
        arr = np.array(r)
        return round(float(arr.mean() - 1.645 * arr.std()), 4)

    def profit_factor(self) -> float:
        """PF = gross_profit / gross_loss  (healthy bot > 1.5)"""
        if self.port.gross_loss == 0: return 0.0
        return round(self.port.gross_profit / self.port.gross_loss, 3)

    def win_rate(self) -> float:
        if self.port.trades_total == 0: return 0.0
        return round(self.port.trades_won / self.port.trades_total, 3)

    def record_return(self, pnl: float):
        """Record trade return for Sharpe/VaR calculation."""
        if self.port.total_value > 0:
            ret = pnl / self.port.total_value
            self.port.returns.append(ret)
        # Track peak for MDD
        if self.port.total_value > self.port.peak_value:
            self.port.peak_value = self.port.total_value


# ════════════════════════════════════════════════════════════════════════════
# TELEGRAM BOT
# ════════════════════════════════════════════════════════════════════════════

class TGBot:
    def __init__(self, cfg: Config, bot):
        self.cfg = cfg; self.trading_bot = bot; self.app = None

    def _wallet(self):
        a = self.trading_bot.client._acct
        if a: return f"👛 <code>{a.address[:6]}...{a.address[-4:]}</code>"
        return "👛 No wallet"

    def build(self):
        if not self.cfg.TG_TOKEN: log.warning("No TG token"); return
        self.app = Application.builder().token(self.cfg.TG_TOKEN).build()
        for cmd, fn in [
            ("start",self._start),("help",self._start),("status",self._status),
            ("positions",self._positions),("pnl",self._pnl),
            ("signals",self._signals),("metrics",self._metrics),
            ("stop",self._stop),("resume",self._resume),
            ("leaderboard",self._leaderboard),
        ]:
            self.app.add_handler(CommandHandler(cmd, fn))
        log.info("Telegram configured")

    async def send(self, text):
        if not self.app: return
        for cid in self.cfg.chat_ids:
            try: await self.app.bot.send_message(chat_id=cid,text=text,parse_mode="HTML")
            except Exception as e: log.error(f"TG {cid}: {e}")

    async def alert_signal(self, sig: Signal):
        stars = "⭐"*min(5, int(sig.confidence*5))
        await self.send(
            f"{sig.emoji} <b>Signal — {sig.strategy}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>{sig.market.question[:70]}</b>\n\n"
            f"Side: <b>{sig.side}</b> | Edge: <b>+{sig.edge*100:.1f}pp</b>\n"
            f"EV: <code>{sig.ev:+.4f}</code> | Z: <code>{sig.z_score:+.2f}σ</code>\n"
            f"Confidence: <b>{sig.confidence*100:.0f}%</b> {stars}\n"
            f"Model: <code>{sig.model_prob:.3f}</code> vs Market: <code>{sig.market_price:.3f}</code>\n"
            f"Liquidity: <code>${sig.market.liquidity:,.0f}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{self._wallet()} | 🕐 {utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
        )

    async def alert_trade(self, pos: Position, action: str, reason: str = ""):
        w = self._wallet()
        ts = utcnow().strftime('%Y-%m-%d %H:%M UTC')

        if action == "open":
            # Calculate potential profit/loss amounts
            sl_loss_usd  = round((pos.entry_price - pos.stop_loss) * pos.shares, 2)
            sl_loss_pct  = round((pos.entry_price - pos.stop_loss) / pos.entry_price * 100, 1)
            if pos.take_profit_price:
                tp_gain_usd = round((pos.take_profit_price - pos.entry_price) * pos.shares, 2)
                tp_gain_pct = round((pos.take_profit_price - pos.entry_price) / pos.entry_price * 100, 1)
                tp_line = f"🎯 TP: <code>{pos.take_profit_price:.4f}</code> → <b>+${tp_gain_usd:.2f} (+{tp_gain_pct:.1f}%)</b>"
            else:
                tp_line = f"🎯 TP: <code>Hold to resolve at $1.00</code>"
                max_gain_usd = round((1.0 - pos.entry_price) * pos.shares, 2)
                max_gain_pct = round((1.0 - pos.entry_price) / pos.entry_price * 100, 1)
                tp_line += f"\n   Max gain: <b>+${max_gain_usd:.2f} (+{max_gain_pct:.1f}%)</b>"

            await self.send(
                f"✅ <b>Trade Opened</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>{pos.market.question[:65]}</b>\n\n"
                f"Side: <b>{pos.side}</b> | Entry: <code>{pos.entry_price:.4f}</code>\n"
                f"💰 Size: <b>${pos.size_usd:.2f} USDC</b> ({pos.shares:.2f} shares)\n\n"
                f"🛑 SL: <code>{pos.stop_loss:.4f}</code> → <b>-${sl_loss_usd:.2f} (-{sl_loss_pct:.1f}%)</b>\n"
                f"{tp_line}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{w} | 🕐 {ts}"
            )

        else:
            # P&L in both $ and %
            pnl_usd  = pos.pnl
            pnl_pct  = (pnl_usd / pos.size_usd * 100) if pos.size_usd > 0 else 0
            price_chg = pos.current_price - pos.entry_price
            price_chg_pct = (price_chg / pos.entry_price * 100) if pos.entry_price > 0 else 0

            is_win = pnl_usd >= 0
            emoji  = "💰" if is_win else "❌"
            pnl_str  = f"+${pnl_usd:.2f} (+{pnl_pct:.1f}%)" if is_win else f"-${abs(pnl_usd):.2f} (-{abs(pnl_pct):.1f}%)"

            # Reason label
            reason_labels = {
                "stop_loss":    "🛑 Stop Loss",
                "take_profit":  "🎯 Take Profit",
                "manual":       "👤 Manual",
                "market_resolved": "✅ Resolved",
            }
            reason_label = reason_labels.get(reason, f"📋 {reason.replace('_',' ').title()}")

            # Duration
            try:
                opened  = datetime.fromisoformat(pos.opened_at)
                elapsed = utcnow() - opened
                hours   = int(elapsed.total_seconds() // 3600)
                mins    = int((elapsed.total_seconds() % 3600) // 60)
                duration = f"{hours}h {mins}m" if hours > 0 else f"{mins}m"
            except:
                duration = "—"

            await self.send(
                f"{emoji} <b>Trade Closed — {reason_label}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 <b>{pos.market.question[:65]}</b>\n\n"
                f"Side: <b>{pos.side}</b>\n"
                f"Entry: <code>{pos.entry_price:.4f}</code> → Exit: <code>{pos.current_price:.4f}</code>\n"
                f"Price change: <code>{price_chg:+.4f} ({price_chg_pct:+.1f}%)</code>\n\n"
                f"💵 Size: <code>${pos.size_usd:.2f} USDC</code>\n"
                f"{'📈' if is_win else '📉'} P&L: <b>{pnl_str}</b>\n"
                f"⏱ Duration: <code>{duration}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{w} | 🕐 {ts}"
            )

    async def alert_daily(self, port: Portfolio, risk: Risk):
        pf = risk.profit_factor(); sr = risk.sharpe_ratio(); mdd = risk.max_drawdown()
        pf_ok = "✅" if pf >= 1.5 else "⚠️"
        sr_ok = "✅" if sr >= 1.5 else "⚠️"
        await self.send(
            f"📊 <b>Daily Summary — {utcnow().strftime('%Y-%m-%d')}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{self._wallet()}\n\n"
            f"💼 Portfolio: <b>${port.total_value:,.2f}</b>\n"
            f"📈 Daily P&L: <b>${port.daily_pnl:+.2f}</b>\n"
            f"🏆 All-time: <b>${port.all_time_pnl:+.2f}</b>\n"
            f"🎯 Win Rate: <b>{risk.win_rate()*100:.1f}%</b> ({port.trades_total} trades)\n\n"
            f"📐 <b>v5.0 Metrics</b>\n"
            f"{pf_ok} Profit Factor: <code>{pf:.2f}</code> (target >1.5)\n"
            f"{sr_ok} Sharpe Ratio: <code>{sr:.2f}</code> (target >1.5)\n"
            f"📉 Max Drawdown: <code>{mdd*100:.1f}%</code> (limit 8%)\n"
            f"🔍 Signals: <b>{port.signals_today}</b> today\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )

    async def _start(self,u,c):
        await u.message.reply_html(
            "🤖 <b>PolyBot v5.0</b>\n\n"
            "/status — Portfolio & bot status\n"
            "/positions — Open trades\n"
            "/signals — Latest signals\n"
            "/metrics — Sharpe, MDD, PF, VaR\n"
            "/leaderboard — Top signals by edge\n"
            "/pnl — P&L report\n"
            "/stop — Pause bot\n"
            "/resume — Resume bot"
        )

    async def _status(self,u,c):
        b = self.trading_bot; p = b.portfolio; r = b.risk
        deployed = sum(pos.size_usd for pos in p.positions.values())
        await u.message.reply_html(
            f"<b>{'🟢 RUNNING' if b.running else '🔴 STOPPED'}</b>\n\n"
            f"{self._wallet()}\n\n"
            f"💼 Portfolio: <b>${p.total_value:,.2f}</b>\n"
            f"💵 Cash: <code>${p.cash:,.2f}</code>\n"
            f"📂 Positions: <code>{len(p.positions)}</code> (${deployed:.0f} deployed)\n"
            f"📈 Daily P&L: <b>${p.daily_pnl:+.2f}</b>\n"
            f"📊 Scan: <code>#{b._scan_count}</code>\n"
            f"📉 MDD: <code>{r.max_drawdown()*100:.1f}%</code>"
        )

    async def _metrics(self,u,c):
        r = self.trading_bot.risk; p = self.trading_bot.portfolio
        pf = r.profit_factor(); sr = r.sharpe_ratio()
        mdd = r.max_drawdown(); var = r.value_at_risk_95()
        wr = r.win_rate()
        await u.message.reply_html(
            f"📐 <b>v5.0 Performance Metrics</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🎯 Win Rate: <b>{wr*100:.1f}%</b> {'✅' if wr>=0.70 else '⚠️'} (target 70%)\n"
            f"📊 Sharpe Ratio: <b>{sr:.3f}</b> {'✅' if sr>=1.5 else '⚠️'} (target 1.5)\n"
            f"💰 Profit Factor: <b>{pf:.3f}</b> {'✅' if pf>=1.5 else '⚠️'} (target 1.5)\n"
            f"📉 Max Drawdown: <b>{mdd*100:.1f}%</b> {'✅' if mdd<0.08 else '🛑'} (limit 8%)\n"
            f"⚠️ VaR 95%: <b>{var*100:.2f}%</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Trades: {p.trades_total} | Won: {p.trades_won}\n"
            f"Gross P: ${p.gross_profit:.2f} | Gross L: ${p.gross_loss:.2f}"
        )

    async def _positions(self,u,c):
        p = self.trading_bot.portfolio
        if not p.positions: await u.message.reply_text("📭 No open positions."); return
        lines=[f"• <b>{pos.market.question[:40]}…</b>\n  {pos.side} | P&L: {'+' if pos.pnl>=0 else ''}${pos.pnl:.2f}" for pos in p.positions.values()]
        await u.message.reply_html(f"<b>📂 Positions ({len(p.positions)})</b>\n\n"+"\n\n".join(lines))

    async def _pnl(self,u,c):
        p = self.trading_bot.portfolio; r = self.trading_bot.risk
        await u.message.reply_html(
            f"<b>📈 P&L Report</b>\n"
            f"Today: <b>${p.daily_pnl:+.2f}</b>\n"
            f"All-time: <b>${p.all_time_pnl:+.2f}</b>\n"
            f"Win rate: <b>{r.win_rate()*100:.1f}%</b>"
        )

    async def _signals(self,u,c):
        sigs = self.trading_bot.latest_signals[:5]
        if not sigs: await u.message.reply_text("📭 No signals."); return
        lines = [
            f"{s.emoji} <b>{s.side}</b> {s.market.question[:40]}…\n"
            f"Edge: +{s.edge*100:.1f}pp | Z: {s.z_score:+.1f}σ | {s.confidence*100:.0f}% | {s.strategy}"
            for s in sigs
        ]
        await u.message.reply_html("<b>🔍 Signals</b>\n\n"+"\n\n".join(lines))

    async def _leaderboard(self,u,c):
        sigs = sorted(self.trading_bot.latest_signals, key=lambda s: s.z_score, reverse=True)[:5]
        if not sigs: await u.message.reply_text("📭 No signals."); return
        medals = ["🥇","🥈","🥉","4️⃣","5️⃣"]
        lines = [
            f"{medals[i]} <b>{s.side}</b> {s.market.question[:38]}…\n"
            f"Z: {s.z_score:+.1f}σ | EV: {s.ev:+.3f} | {s.strategy}"
            for i,s in enumerate(sigs)
        ]
        await u.message.reply_html("<b>🏆 Top Signals (by Z-score)</b>\n\n"+"\n\n".join(lines))

    async def _stop(self,u,c):
        self.trading_bot.running = False
        await u.message.reply_html(f"🔴 <b>Bot Paused</b>\n{self._wallet()}\n\n/resume to restart.")

    async def _resume(self,u,c):
        self.trading_bot.running = True
        await u.message.reply_html(f"🟢 <b>Bot Resumed</b>\n{self._wallet()}\n\nNow scanning…")


# ════════════════════════════════════════════════════════════════════════════
# WEB API
# ════════════════════════════════════════════════════════════════════════════

class WebAPI:
    def __init__(self, bot): self.bot = bot

    def cors(self, r):
        r.headers.update({
            "Access-Control-Allow-Origin":"*",
            "Access-Control-Allow-Methods":"GET,POST,OPTIONS",
            "Access-Control-Allow-Headers":"Content-Type,X-API-Secret",
        })
        return r

    async def handle_options(self, req):
        return self.cors(aioWeb.Response(status=200))

    async def handle_health(self, req):
        b=self.bot; p=b.portfolio; r=b.risk
        deployed = sum(pos.size_usd for pos in p.positions.values())
        data = {
            "status":       "running" if b.running else "stopped",
            "version":      "5.0",
            "scan":         b._scan_count,
            "portfolio":    round(p.total_value,2),
            "cash":         round(p.cash,2),
            "deployed":     round(deployed,2),
            "daily_pnl":    round(p.daily_pnl,2),
            "all_time_pnl": round(p.all_time_pnl,2),
            "positions":    len(p.positions),
            "signals_today":p.signals_today,
            "trades_total": p.trades_total,
            "trades_won":   p.trades_won,
            "win_rate":     r.win_rate(),
            "sharpe":       r.sharpe_ratio(),
            "max_drawdown": r.max_drawdown(),
            "profit_factor":r.profit_factor(),
            "var_95":       r.value_at_risk_95(),
            "exposure_pct": round(deployed/max(1,p.total_value)*100,1),
            "wallet":       b.client._acct.address if b.client._acct else None,
        }
        return self.cors(aioWeb.Response(text=json.dumps(data),content_type="application/json"))

    async def handle_status(self, req): return await self.handle_health(req)

    async def handle_metrics(self, req):
        r = self.bot.risk; p = self.bot.portfolio
        data = {
            "win_rate":      r.win_rate(),
            "sharpe_ratio":  r.sharpe_ratio(),
            "profit_factor": r.profit_factor(),
            "max_drawdown":  r.max_drawdown(),
            "var_95":        r.value_at_risk_95(),
            "gross_profit":  round(p.gross_profit,2),
            "gross_loss":    round(p.gross_loss,2),
            "trades_total":  p.trades_total,
            "trades_won":    p.trades_won,
            "targets": {
                "win_rate":     {"value":r.win_rate(),"target":0.70,"ok":r.win_rate()>=0.70},
                "sharpe":       {"value":r.sharpe_ratio(),"target":1.5,"ok":r.sharpe_ratio()>=1.5},
                "profit_factor":{"value":r.profit_factor(),"target":1.5,"ok":r.profit_factor()>=1.5},
                "max_drawdown": {"value":r.max_drawdown(),"target":0.08,"ok":r.max_drawdown()<0.08},
            }
        }
        return self.cors(aioWeb.Response(text=json.dumps(data),content_type="application/json"))

    async def handle_signals(self, req):
        sigs = self.bot.latest_signals[:20]
        return self.cors(aioWeb.Response(text=json.dumps({
            "count": len(sigs),
            "signals": [{
                "side":       s.side, "strategy":  s.strategy,
                "confidence": round(s.confidence,3), "edge": round(s.edge,4),
                "ev":         round(s.ev,4), "z_score": round(s.z_score,2),
                "emoji":      s.emoji, "timestamp": s.timestamp,
                "question":   s.market.question,
                "market": {
                    "question":  s.market.question,
                    "yes_price": s.market.yes_price,
                    "liquidity": s.market.liquidity,
                    "category":  s.market.category,
                }
            } for s in sigs]
        }), content_type="application/json"))

    async def handle_positions(self, req):
        positions = list(self.bot.portfolio.positions.values())
        return self.cors(aioWeb.Response(text=json.dumps({
            "count":     len(positions),
            "total_pnl": round(sum(p.pnl for p in positions),2),
            "deployed":  round(sum(p.size_usd for p in positions),2),
            "positions": [{
                "id":            pos.id,
                "question":      pos.market.question,
                "side":          pos.side,
                "entry_price":   round(pos.entry_price,4),
                "current_price": round(pos.current_price,4),
                "size_usd":      round(pos.size_usd,2),
                "pnl":           round(pos.pnl,2),
                "stop_loss":     round(pos.stop_loss,4),
                "take_profit":   round(pos.take_profit_price,4) if pos.take_profit_price else None,
                "opened_at":     pos.opened_at,
            } for pos in positions]
        }), content_type="application/json"))

    async def handle_balance(self, req):
        balance = await self.bot.sync_balance()
        wallet = self.bot.client._acct.address if self.bot.client._acct else None
        return self.cors(aioWeb.Response(text=json.dumps({
            "balance":      balance,
            "portfolio":    self.bot.portfolio.total_value,
            "cash":         self.bot.portfolio.cash,
            "wallet":       wallet,
            "wallet_short": f"{wallet[:6]}...{wallet[-4:]}" if wallet else None,
        }), content_type="application/json"))

    async def handle_get_settings(self, req):
        s = load_file()
        safe = {k:v for k,v in s.items() if k!="privateKey"}
        if s.get("privateKey"): safe["privateKey"] = "0x"+"•"*20
        safe["_has_wallet"] = bool(s.get("privateKey"))
        return self.cors(aioWeb.Response(text=json.dumps(safe),content_type="application/json"))

    async def handle_post_settings(self, req):
        try: data = await req.json()
        except Exception as e:
            return self.cors(aioWeb.Response(status=400,text=f'{{"error":"{e}"}}'))
        log.info(f"POST /settings keys: {[k for k in data if k!='privateKey']}")
        existing = load_file()
        if data.get("privateKey","").startswith("0x•"):
            data["privateKey"] = existing.get("privateKey","")
        data["_saved_at"] = utcnow().isoformat()
        save_file(data)
        verify = load_file()
        log.info(f"Verify: maxPosition={verify.get('maxPosition')} slPct={verify.get('slPct')}")
        self.bot.cfg.reload()
        if data.get("privateKey") and data["privateKey"] != existing.get("privateKey"):
            self.bot.client._init_wallet()
        if data.get("tgToken") and data["tgToken"] != existing.get("tgToken"):
            self.bot.tg = TGBot(self.bot.cfg, self.bot)
            self.bot.tg.build()
        return self.cors(aioWeb.Response(
            text='{"status":"ok","message":"Settings saved and applied!"}',
            content_type="application/json"
        ))

    async def handle_start(self, req):
        self.bot.running = True
        log.info("Bot started via API")
        return self.cors(aioWeb.Response(
            text='{"status":"ok","message":"Bot started"}',content_type="application/json"))

    async def handle_stop(self, req):
        self.bot.running = False
        log.info("Bot stopped via API")
        return self.cors(aioWeb.Response(
            text='{"status":"ok","message":"Bot paused"}',content_type="application/json"))

    async def start(self, port):
        app = aioWeb.Application()
        app.router.add_route("OPTIONS","/{path_info:.*}",self.handle_options)
        app.router.add_get("/health",   self.handle_health)
        app.router.add_get("/status",   self.handle_status)
        app.router.add_get("/metrics",  self.handle_metrics)
        app.router.add_get("/signals",  self.handle_signals)
        app.router.add_get("/positions",self.handle_positions)
        app.router.add_get("/balance",  self.handle_balance)
        app.router.add_get("/settings", self.handle_get_settings)
        app.router.add_post("/settings",self.handle_post_settings)
        app.router.add_post("/start",   self.handle_start)
        app.router.add_post("/stop",    self.handle_stop)
        runner = aioWeb.AppRunner(app)
        await runner.setup()
        await aioWeb.TCPSite(runner,"0.0.0.0",port).start()
        log.info(f"Web API on port {port}")


# ════════════════════════════════════════════════════════════════════════════
# MAIN TRADING BOT
# ════════════════════════════════════════════════════════════════════════════

class TradingBot:
    def __init__(self):
        self.cfg            = Config()
        self.portfolio      = Portfolio()
        self.client         = PolyClient(self.cfg)
        self.model          = BayesianModel()        # v5.0
        self.risk           = Risk(self.cfg, self.portfolio)
        self.running        = False  # STOPPED by default
        self.latest_signals: list[Signal] = []
        self._scan_count    = 0
        self._last_daily    = utcnow().date()
        self._sent          = load_cooldown()

        self.strat_arb      = ArbStrategy()
        self.strat_news     = NewsStrategy()
        self.strat_meanrev  = MeanRevStrategy()
        self.strat_vol      = VolSpikeStrategy()
        self.strat_close    = ClosingStrategy()

        self.tg             = TGBot(self.cfg, self)
        self.tg.build()
        self.api            = WebAPI(self)

    async def sync_balance(self) -> float:
        balance = await self.client.get_usdc_balance()
        if balance > 0:
            deployed = sum(p.size_usd for p in self.portfolio.positions.values())
            self.portfolio.cash        = round(balance - deployed, 2)
            self.portfolio.total_value = round(balance, 2)
            if self.portfolio.peak_value == 0:
                self.portfolio.peak_value = balance
            log.info(f"Balance synced: ${balance:.2f} (cash: ${self.portfolio.cash:.2f})")
        return balance

    async def start(self):
        await self.client.start()
        await self.sync_balance()
        await self.api.start(self.cfg.API_PORT)

        wallet_short = f"{self.client._acct.address[:6]}...{self.client._acct.address[-4:]}" \
                       if self.client._acct else "⚠️ Not connected"
        log.info(f"PolyBot v5.0 started — wallet: {wallet_short} — STOPPED")
        await self.tg.send(
            f"🤖 <b>PolyBot v5.0 Started</b>\n\n"
            f"👛 Wallet: <code>{wallet_short}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🧠 Bayesian Model | ⚡ 5 Strategies\n"
            f"📐 Z-score Filter | 📊 Kelly Sizing\n"
            f"🌐 Web API: active\n"
            f"🔴 Status: <b>STOPPED</b> — tap ▶ Run Bot to start\n\n"
            f"Type /help for commands"
        )

        if self.tg.app:
            try:
                await self.tg.app.initialize()
                await self.tg.app.bot.delete_webhook(drop_pending_updates=True)
                await self.tg.app.updater.start_polling(
                    drop_pending_updates=True,
                    allowed_updates=["message","callback_query"]
                )
                await self.tg.app.start()
                log.info("Telegram polling started")
            except Exception as e:
                log.error(f"Telegram start error (non-fatal): {e}")

        while True:
            try:
                if self.running:
                    await self.scan()
                    if self._scan_count % 2 == 0:
                        await self.sync_balance()
                await self._daily_check()
                await asyncio.sleep(self.cfg.SCAN_INTERVAL)
            except asyncio.CancelledError: break
            except Exception as e:
                log.error(f"Main loop: {e}")
                await asyncio.sleep(10)

    async def stop(self):
        if self.tg.app:
            try:
                await self.tg.app.updater.stop()
                await self.tg.app.stop()
                await self.tg.app.shutdown()
            except: pass
        await self.client.stop()

    async def scan(self):
        self._scan_count += 1
        sl_pct = self.cfg.SL_PCT*100 if self.cfg.SL_PCT else self.cfg.STOP_LOSS*100
        tp_pct = f"{self.cfg.TP_PCT*100:.0f}%" if self.cfg.TP_PCT else "none"
        log.info(f"Scan #{self._scan_count} | MaxPos=${self.cfg.MAX_POSITION} | "
                 f"SL={sl_pct:.0f}% | TP={tp_pct} | Running={'YES' if self.running else 'NO'}")

        markets = await self.client.get_markets()
        log.info(f"Fetched {len(markets)}")

        # Filter markets
        seen, filtered = set(), []
        for m in markets:
            if m.liquidity < self.cfg.MIN_LIQUIDITY: continue
            if not (self.cfg.PRICE_MIN <= m.yes_price <= self.cfg.PRICE_MAX): continue
            if m.condition_id in self.portfolio.positions: continue
            if m.condition_id in seen: continue
            seen.add(m.condition_id); filtered.append(m)
        log.info(f"Filtered: {len(filtered)}")

        await self._update_positions(markets)

        # Run all strategies
        signals: list[Signal] = []
        if self.cfg.STRAT_ARB:     signals += self.strat_arb.analyze(filtered, self.model, self.cfg)
        if self.cfg.STRAT_NEWS:    signals += self.strat_news.analyze(filtered, self.model, self.cfg)
        if self.cfg.STRAT_MEANREV: signals += self.strat_meanrev.analyze(filtered, self.model, self.cfg)
        if self.cfg.STRAT_VOL:     signals += self.strat_vol.analyze(filtered, self.model, self.cfg)
        if self.cfg.STRAT_CLOSE:   signals += self.strat_close.analyze(filtered, self.model, self.cfg)

        # Sort by Z-score (v5.0 — best signal first)
        signals.sort(key=lambda s: abs(s.z_score), reverse=True)

        # Deduplicate by market
        seen_mkt, unique = set(), []
        for s in signals:
            if s.market.condition_id not in seen_mkt:
                seen_mkt.add(s.market.condition_id); unique.append(s)
        self.latest_signals = unique
        on_cd = sum(1 for s in unique if self._on_cooldown(s))
        log.info(f"Signals: {len(unique)} total | {on_cd} on cooldown | {len(unique)-on_cd} ready")

        for sig in unique[:3]:
            await self._process(sig)

    def _market_cooldown_key(self, sig: Signal) -> str:
        """Use market+side as key so SAME market can't alert from different strategies."""
        return hashlib.md5(f"{sig.market.condition_id}:{sig.side}".encode()).hexdigest()[:12]

    def _on_cooldown(self, sig: Signal) -> bool:
        key = self._market_cooldown_key(sig)
        last = self._sent.get(key, 0)
        elapsed = time.time() - last
        on_cd = elapsed < self.cfg.COOLDOWN
        if on_cd:
            remaining = int(self.cfg.COOLDOWN - elapsed)
            log.info(f"🔕 Cooldown: {sig.market.question[:45]} — {remaining}s left")
        return on_cd

    def _mark_sent(self, sig: Signal):
        key = self._market_cooldown_key(sig)
        self._sent[key] = time.time()
        # Also mark the old strategy-level key for backwards compatibility
        self._sent[sig.key] = time.time()
        # Clean expired entries
        cutoff = time.time() - self.cfg.COOLDOWN * 2
        self._sent = {k:v for k,v in self._sent.items() if v > cutoff}
        save_cooldown(self._sent)
        log.info(f"✅ Signal marked — cooldown {self.cfg.COOLDOWN}s for: {sig.market.question[:45]}")

    async def _process(self, sig: Signal):
        if self._on_cooldown(sig):
            remaining = int(self.cfg.COOLDOWN - (time.time() - self._sent.get(sig.key,0)))
            log.info(f"Cooldown: {sig.market.question[:40]} — {remaining}s left")
            return
        self._mark_sent(sig)
        self.portfolio.signals_today += 1
        await self.tg.alert_signal(sig)

        can, reason = self.risk.can_trade()
        if not can:
            log.warning(f"Risk block: {reason}"); return

        price = sig.market.yes_price if sig.side=="YES" else sig.market.no_price
        size  = self.risk.kelly_size(sig, price)
        if size < 1:
            log.info(f"Size too small: ${size:.2f}"); return

        tid = sig.market.token_id_yes if sig.side=="YES" else sig.market.token_id_no
        resp = await self.client.place_order(tid, sig.side, size, price)
        oid  = resp.get("id") or resp.get("orderID") or f"pos_{int(time.time())}"

        pos = Position(
            id=oid, market=sig.market, side=sig.side,
            entry_price=price, current_price=price, size_usd=size,
            shares=size/price,
            stop_loss=self.risk.stop_price(price, sig.side),
            take_profit_price=self.risk.tp_price(price, sig.side),
        )
        self.portfolio.positions[oid] = pos
        self.portfolio.cash -= size
        self.portfolio.trades_total += 1
        log.info(f"Opened: {sig.side} {sig.market.question[:40]} ${size:.2f} "
                 f"SL={pos.stop_loss:.4f} TP={pos.take_profit_price or 'none'}")
        await self.tg.alert_trade(pos, "open")

    async def _update_positions(self, markets: list[Market]):
        mmap = {m.condition_id:m for m in markets}
        for pid, pos in list(self.portfolio.positions.items()):
            m = mmap.get(pos.market.condition_id)
            if not m: continue
            curr = m.yes_price if pos.side=="YES" else m.no_price
            self.portfolio.positions[pid] = self.risk.update_pnl(pos, curr)
        for pid, reason in self.risk.check_exits():
            await self.close(pid, reason)

    async def close(self, pid: str, reason: str="manual"):
        pos = self.portfolio.positions.pop(pid, None)
        if not pos: return

        # Update portfolio
        self.portfolio.cash         += pos.size_usd + pos.pnl
        self.portfolio.total_value   = self.portfolio.cash + sum(
            p.size_usd for p in self.portfolio.positions.values()
        )
        self.portfolio.daily_pnl    += pos.pnl
        self.portfolio.all_time_pnl += pos.pnl

        # v5.0 — track gross P/L for profit factor
        if pos.pnl >= 0:
            self.portfolio.trades_won   += 1
            self.portfolio.gross_profit += pos.pnl
        else:
            self.portfolio.gross_loss   += abs(pos.pnl)

        # Record return for Sharpe/VaR
        self.risk.record_return(pos.pnl)

        log.info(f"Closed ({reason}): {pid} P&L ${pos.pnl:+.2f} "
                 f"| SR={self.risk.sharpe_ratio():.2f} PF={self.risk.profit_factor():.2f}")
        await self.tg.alert_trade(pos, "close", reason)

    async def _daily_check(self):
        today = utcnow().date()
        if today != self._last_daily:
            self._last_daily = today
            await self.tg.alert_daily(self.portfolio, self.risk)
            self.portfolio.daily_pnl     = 0.0
            self.portfolio.signals_today = 0
            self.risk.day_start          = self.portfolio.total_value


# ════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════

async def main():
    bot = TradingBot()
    try:
        await bot.start()
    except KeyboardInterrupt:
        await bot.stop()

if __name__ == "__main__":
    asyncio.run(main())
