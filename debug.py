"""
Quidax Market Monitor — API-based (replaces Selenium scraper)
─────────────────────────────────────────────────────────────
Endpoints used:
  Depth  : GET /exchange-open-api/api/v1/markets/{symbol}/depth?limit=200
  K-Line : GET /exchange-open-api/api/v1/markets/{symbol}/k?period=1&limit=60
           (1-minute candles, last 60 minutes — a rolling hourly window,
            NOT 60-minute candles. See KLINE_CANDLE_MINUTES below.)

Run modes:
  python quidax_monitor.py          # continuous loop (1-min cycle)
  python quidax_monitor.py --once   # single pass then exit
"""

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
import pandas as pd

try:
    from dotenv import load_dotenv
    load_dotenv()  # loads a local .env file if python-dotenv is installed
except ImportError:
    pass  # fine — env vars can also be set directly in the shell/systemd unit

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION  (replace placeholders before running)
# ══════════════════════════════════════════════════════════════════════════════

API_HEADERS = {"accept": "application/json"}

# Secrets come from the environment ONLY — never hardcode them here.
# Set QUIDAX_TG_BOT_TOKEN and QUIDAX_TG_CHAT_IDS (comma-separated) in a
# .env file (gitignored) or in your systemd unit's Environment= lines.
TELEGRAM_BOT_TOKEN = os.environ.get("QUIDAX_TG_BOT_TOKEN", "")
TELEGRAM_CHAT_IDS  = [c.strip() for c in os.environ.get("QUIDAX_TG_CHAT_IDS", "").split(",") if c.strip()]

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
    print("⚠️  QUIDAX_TG_BOT_TOKEN / QUIDAX_TG_CHAT_IDS not set — Telegram alerts are disabled.")

BASE_API_URL        = "https://openapi.quidax.io/exchange-open-api/api/v1"
DEPTH_LIMIT         = 200   # max order book levels per side
KLINE_CANDLE_MINUTES = 1    # candle size requested from the API
KLINE_LOOKBACK_MINUTES = 60 # how far back we pull candles (rolling window)

# Pairs config: list of [symbol, target_spread_pct | null]
# null target  → monitor-only (no spread anomaly check)
PAIRS: list[tuple[str, Optional[float]]] = [
    ('aaveusdt'    , 0.3   ),  # AAVE_USDT
    ('adausdt'     , 2.0  ),  # ADA_USDT
    ('algousdt'    , 2.0   ),  # ALGO_USDT
    ('bchusdt'     , 1.20  ),  # BCH_USDT
    ('bnbusdt'     , 0.3   ),  # BNB_USDT
    ('bonkusdt'    , 2.0   ),  # BONK_USDT
    ('btcusdt'     , 0.2   ),  # BTC_USDT
    ('cakeusdt'    , 0.3   ),  # CAKE_USDT
    ('cfxusdt'     , 2.0   ),  # CFX_USDT
    ('dashusdt'    , 2.0   ),  # DASH_USDT
    ('dotusdt'     , 0.26  ),  # DOT_USDT
    ('dogeusdt'    , 0.26  ),  # DOGE_USDT
    ('ethusdt'     , 0.25  ),  # ETH_USDT
    ('fartcoinusdt', 2.0   ),  # FARTCOIN_USDT
    ('flokiusdt'   , 0.5   ),  # FLOKI_USDT
    ('hypeusdt'    , 2.0   ),  # HYPE_USDT
    ('linkusdt'    , 0.26  ),  # LINK_USDT
    ('lskusdt'     , 1.5   ),  # LSK_USDT
    ('ltcusdt'     , 0.3   ),  # LTC_USDT
    ('pepeusdt'    , 0.5   ),  # PEPE_USDT
    ('polusdt'     , 0.5   ),  # POL_USDT
    ('rndrusdt'  , 2.0   ),  # RENDER_USDT
    ('shibusdt'    , 0.4   ),  # SHIB_USDT
    ('slpusdt'     , 2.0   ),  # SLP_USDT
    ('solusdt'     , 0.25  ),  # SOL_USDT
    ('suiusdt'     , 2.0   ),  # SUI_USDT
    ('tonusdt'     , 0.3   ),  # TON_USDT
    ('trxusdt'     , 0.3   ),  # TRX_USDT
    ('usdcusdt'    , 0.02  ),  # USDC_USDT
    ('wifusdt'     , 2.0   ),  # WIF_USDT
    ('xlmusdt'     , 0.3   ),  # XLM_USDT
    ('xrpusdt'     , 0.3   ),  # XRP_USDT
    ('xyousdt'     , 1.0   ),  # XYO_USDT
    ('usdtcngn'    , None  ),  # USDT_CNGN
    ('btcngn'      , 0.7   ),  # BTC_NGN
    ('usdtngn'     , 0.95  ),  # USDT_NGN
    ('ethngn'      , 0.75  ),  # ETH_NGN
    ('trxngn'      , 0.75  ),  # TRX_NGN
    ('xrpngn'      , 0.5   ),  # XRP_NGN
    ('dashngn'     , 0.5   ),  # DASH_NGN
    ('ltcngn'      , 0.5   ),  # LTC_NGN
    ('solngn'      , 0.8   ),  # SOL_NGN
    ('usdcngn'     , 1.2   ),  # USDC_NGN
    ('cngnngn'     , None  ),  # CNGN_NGN
    ('usdtghs'     , 1.3   ),  # USDT_GHS
]

# ── Alert timing ──────────────────────────────────────────────────────────────
ANOMALY_ALERT_AFTER_MINUTES = 10     # fire alert only after anomaly persists this long
ALERT_COOLDOWN_MINUTES      = 30     # minimum gap between repeat alerts for same pair+issue
CYCLE_SLEEP_SECONDS         = 60     # sleep between full scan cycles

# ── Orderbook health thresholds ───────────────────────────────────────────────
MIN_ORDERBOOK_LAYERS        = 10
THIN_DEPTH_THRESHOLD        = 5_000  # USD-equiv total depth within spread band
DEPTH_IMBALANCE_RATIO       = 5.0    # kept for capture; not alerting
STALE_OB_CYCLES             = 3      # consecutive identical top-of-book → stale
MID_PRICE_ALERT_THRESHOLD   = 25     # % change in mid-price between cycles

# ── Spread anomaly gate ───────────────────────────────────────────────────────
DWS_POOR_THRESHOLD          = 0.5    # A2 only counts when DWS also exceeds this
MIN_ABS_SPREAD_DIFF_PCT     = 0.05   # percentage-point floor; ignore relative
                                      # blow-ups smaller than this in absolute
                                      # terms (protects tiny-target pairs like
                                      # usdcusdt where 0.02% target makes any
                                      # tiny move look like a huge % diff)

# ── Concurrency ───────────────────────────────────────────────────────────────
MAX_CONCURRENT_PAIRS        = 10     # asyncio semaphore limit

# ── Persistence ───────────────────────────────────────────────────────────────
DATA_DIR   = "data"
# DATA_DIR = "/app/data"
STATE_FILE = os.path.join(DATA_DIR, "health_state.json")

# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS / HELPERS
# ══════════════════════════════════════════════════════════════════════════════

NIGERIAN_TZ = timezone(timedelta(hours=1))

CURRENCY_SYMBOLS = {"USDT": "$", "NGN": "₦", "GHS": "₵"}
HIGH_VOL_TOKENS  = {"BTC", "ETH", "SOL", "USDC"}


def ngt_now() -> datetime:
    return datetime.now(NIGERIAN_TZ)


# Quote currencies actually present in PAIRS — checked longest-first so
# "usdt" (4 chars) isn't mistaken for a 3-char suffix.
KNOWN_QUOTE_CURRENCIES = ("usdt", "ngn", "ghs")


def split_symbol(sym: str) -> tuple[str, str]:
    """
    Split a concatenated symbol like 'btcusdt' into (base, quote).
    Quote length varies (ngn/ghs = 3 chars, usdt = 4 chars) — a fixed
    sym[:-3] slice silently mis-splits every usdt pair (e.g. 'btcusdt'
    -> 'btcu' instead of 'btc'), which made base-token checks like
    HIGH_VOL_TOKENS membership fail for BTC/ETH/SOL/USDC against USDT.
    """
    lower = sym.lower()
    for quote in sorted(KNOWN_QUOTE_CURRENCIES, key=len, reverse=True):
        if lower.endswith(quote):
            return lower[:-len(quote)], quote
    # Unknown quote currency — fall back to the old 3-char assumption
    # rather than crashing, but this symbol should be added above.
    return lower[:-3], lower[-3:]


# Pairs explicitly marked monitor-only (no spread target) also skip
# volume-spike alerting — they're flagged that way for a reason.
MONITOR_ONLY_SYMBOLS = {sym for sym, target in PAIRS if target is None}


def get_threshold(sym: str) -> Optional[float]:
    """Volume spike threshold in quote-currency units."""
    if sym.lower() in MONITOR_ONLY_SYMBOLS:
        return None
    base, quote = split_symbol(sym)
    base, quote = base.upper(), quote.upper()
    if sym.lower() == "usdtngn":
        return 50_000_000
    if quote == "NGN":
        return 50_000_000 if base in HIGH_VOL_TOKENS else 5_000_000
    if quote == "GHS":
        return 60_000
    return 100_000 if base in HIGH_VOL_TOKENS else 5_000


def get_currency_symbol(sym: str) -> str:
    _, quote = split_symbol(sym)
    return CURRENCY_SYMBOLS.get(quote.upper(), "$")


def format_depth(val: float) -> str:
    if not val:              return "$0"
    if val >= 1_000_000:    return f"${val/1_000_000:.2f}M"
    if val >= 1_000:        return f"${val/1_000:.1f}K"
    return f"${val:.0f}"


# ══════════════════════════════════════════════════════════════════════════════
# API LAYER
# ══════════════════════════════════════════════════════════════════════════════

FETCH_MAX_RETRIES   = 2     # additional attempts after the first failure
FETCH_RETRY_BACKOFF = 1.5   # seconds, doubles each retry


async def _request_json(session: aiohttp.ClientSession, url: str) -> dict:
    """GET a URL and return parsed JSON, retrying transient failures."""
    last_exc = None
    for attempt in range(FETCH_MAX_RETRIES + 1):
        try:
            async with session.get(url, headers={"accept": "application/json"},
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                resp.raise_for_status()
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last_exc = e
            if attempt < FETCH_MAX_RETRIES:
                await asyncio.sleep(FETCH_RETRY_BACKOFF * (2 ** attempt))
    raise last_exc


async def fetch_depth(session: aiohttp.ClientSession, symbol: str) -> dict:
    """Returns raw depth payload: {asks: [[price,qty],...], bids: [[price,qty],...]}"""
    url = f"{BASE_API_URL}/markets/{symbol}/depth?limit={DEPTH_LIMIT}"
    payload = await _request_json(session, url)
    # Response envelope: {"status": "success", "data": {"asks": [...], "bids": [...], ...}}
    return payload["data"]


async def fetch_kline(session: aiohttp.ClientSession, symbol: str) -> list:
    """
    Returns 1-minute candles for the last 60 minutes (a rolling window,
    not calendar-day-scoped — see get_recent_spikes).
    Anchors via ?timestamp=<lookback_ms> so we never miss the current
    incomplete hour — one call, exactly KLINE_LOOKBACK_MINUTES candles,
    no looping needed.

    Each candle: [timestamp_ms, open, high, low, close, volume]  ← strings
    """
    lookback_ms = int((ngt_now().timestamp() - KLINE_LOOKBACK_MINUTES * 60) * 1000)
    url = (f"{BASE_API_URL}/markets/{symbol}/k"
           f"?period={KLINE_CANDLE_MINUTES}&limit={KLINE_LOOKBACK_MINUTES}&timestamp={lookback_ms}")
    payload = await _request_json(session, url)
    # Response envelope: {"status": "success", "data": [[ts_ms, o, h, l, c, vol], ...]}
    return payload["data"]


# ══════════════════════════════════════════════════════════════════════════════
# ORDERBOOK ANALYTICS
# ══════════════════════════════════════════════════════════════════════════════

def build_orderbook_dfs(raw: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Convert raw depth payload to ask/bid DataFrames with columns
    [price, amount].  Asks sorted ascending, bids descending.
    """
    def to_df(rows):
        if not rows:
            return pd.DataFrame(columns=["price", "amount"])
        df = pd.DataFrame(rows, columns=["price", "amount"])
        df = df.astype(float)
        return df

    asks = to_df(raw.get("asks", []))
    bids = to_df(raw.get("bids", []))
    if not asks.empty:
        asks = asks.sort_values("price").reset_index(drop=True)
    if not bids.empty:
        bids = bids.sort_values("price", ascending=False).reset_index(drop=True)
    return asks, bids


def compute_mid_and_spread(asks_df: pd.DataFrame, bids_df: pd.DataFrame) -> tuple[float, float, float]:
    """Returns (mid_price, spread_abs, spread_pct)."""
    best_ask = asks_df["price"].iloc[0]
    best_bid = bids_df["price"].iloc[0]
    mid      = (best_ask + best_bid) / 2
    spread   = best_ask - best_bid
    spread_pct = (spread / mid) * 100 if mid else 0.0
    return mid, spread, spread_pct


def calculate_liquidity_depth(asks_df: pd.DataFrame, bids_df: pd.DataFrame,
                               mid: float, spread_pct_range: float) -> float:
    if asks_df.empty or bids_df.empty:
        return 0.0
    upper = mid * (1 + spread_pct_range / 100)
    lower = mid * (1 - spread_pct_range / 100)
    bid_d = (bids_df.loc[bids_df["price"] >= lower, "price"] *
             bids_df.loc[bids_df["price"] >= lower, "amount"]).sum()
    ask_d = (asks_df.loc[asks_df["price"] <= upper, "price"] *
             asks_df.loc[asks_df["price"] <= upper, "amount"]).sum()
    return bid_d + ask_d


def calculate_dws(asks_df: pd.DataFrame, bids_df: pd.DataFrame,
                   mid: float, num_levels: int = 10) -> float:
    if asks_df.empty or bids_df.empty:
        return 0.0
    a_sub = asks_df.head(num_levels)
    b_sub = bids_df.head(num_levels)
    num   = ((a_sub["amount"] * (a_sub["price"] - mid)).abs().sum() +
             (b_sub["amount"] * (mid - b_sub["price"])).abs().sum())
    den   = a_sub["amount"].sum() + b_sub["amount"].sum()
    return (num / den) / mid * 100 if den > 0 else 0.0


def calculate_depth_imbalance(asks_df: pd.DataFrame, bids_df: pd.DataFrame,
                               mid: float, spread_pct_range: float) -> tuple:
    if asks_df.empty or bids_df.empty:
        return None, None
    upper = mid * (1 + spread_pct_range / 100)
    lower = mid * (1 - spread_pct_range / 100)
    bid_d = (bids_df.loc[bids_df["price"] >= lower, "price"] *
             bids_df.loc[bids_df["price"] >= lower, "amount"]).sum()
    ask_d = (asks_df.loc[asks_df["price"] <= upper, "price"] *
             asks_df.loc[asks_df["price"] <= upper, "amount"]).sum()
    if ask_d == 0 and bid_d == 0:
        return 1.0, "balanced"
    lighter = min(bid_d, ask_d)
    heavier = max(bid_d, ask_d)
    if lighter == 0:
        return float("inf"), "bids" if bid_d > ask_d else "asks"
    return heavier / lighter, ("bids" if bid_d > ask_d else "asks")


def get_top_of_book_snapshot(asks_df: pd.DataFrame, bids_df: pd.DataFrame) -> Optional[dict]:
    if asks_df.empty or bids_df.empty:
        return None
    return {
        "ba_p": round(float(asks_df["price"].iloc[0]),  8),
        "ba_a": round(float(asks_df["amount"].iloc[0]), 8),
        "bb_p": round(float(bids_df["price"].iloc[0]),  8),
        "bb_a": round(float(bids_df["amount"].iloc[0]), 8),
    }


# ══════════════════════════════════════════════════════════════════════════════
# K-LINE SPIKE DETECTION
# ══════════════════════════════════════════════════════════════════════════════

def get_recent_spikes(candles: list, sym: str) -> list:
    """
    Aggregates the last KLINE_LOOKBACK_MINUTES of 1-minute candles into a
    single rolling quote-volume figure. Flags if the aggregate exceeds
    the threshold.

    This is a pure rolling window — it intentionally does NOT filter by
    calendar date. An earlier version filtered out candles that fell on
    a different NGT calendar date than "now," which silently dropped up
    to ~59 minutes of real data in the window spanning each midnight.

    Volume per candle is in base currency; quote value = vol × close_price.
    All candle values arrive as strings from the API.
    """
    threshold = get_threshold(sym)
    if threshold is None or not candles:
        return []

    currency = get_currency_symbol(sym)

    total_quote_volume = 0.0
    candle_count        = 0
    window_start         = None
    window_end           = None

    for candle in candles:
        try:
            ts, o, h, l, c, volume = candle[:6]
            candle_dt = datetime.fromtimestamp(int(ts) / 1000, tz=NIGERIAN_TZ)

            quote_value = float(volume) * float(c)
            total_quote_volume += quote_value
            candle_count       += 1

            if window_start is None or candle_dt < window_start:
                window_start = candle_dt
            if window_end is None or candle_dt > window_end:
                window_end = candle_dt

        except (ValueError, TypeError, IndexError):
            continue

    if candle_count == 0:
        return []

    if total_quote_volume >= threshold:
        window_label = (
            f"{window_start.strftime('%H:%M')}–{window_end.strftime('%H:%M')}"
            if window_start and window_end else f"last {KLINE_LOOKBACK_MINUTES} min"
        )
        return [{
            "window":       window_label,
            "candle_count": candle_count,
            "quote_volume": total_quote_volume,
            "currency":     currency,
        }]

    return []


# ══════════════════════════════════════════════════════════════════════════════
# PERSISTENCE
# ══════════════════════════════════════════════════════════════════════════════

_state_lock = asyncio.Lock()


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def update_daily_log(all_results: list):
    """
    Long-format daily log: one ROW per (market, check) rather than 3 new
    COLUMNS per check. At a 60s cycle interval the old wide format could
    grow past 1,000+ columns in a single day, with every cycle paying the
    cost of reading and rewriting the whole (ever-growing) file. This
    version only appends — cost per cycle stays flat regardless of how
    many checks have already run today.

    Columns: Timestamp, Market, Status, Issues, Depth
    """
    now   = ngt_now()
    today = now.strftime("%Y-%m-%d")
    path  = os.path.join(DATA_DIR, f"daily_log_{today}.csv")
    ts    = now.strftime("%H:%M:%S")

    pair_syms   = [sym for sym, _ in PAIRS]
    results_map = {r["symbol"]: r for r in all_results}

    rows = []
    for m in pair_syms:
        if m in results_map:
            r = results_map[m]
            rows.append({
                "Timestamp": ts,
                "Market":    m,
                "Status":    r["status"].upper(),
                "Issues":    r.get("issues", ""),
                "Depth":     f"{r['depth_1.25x']} / {r['depth_1.5x']}",
            })
        else:
            rows.append({
                "Timestamp": ts, "Market": m, "Status": "SKIPPED",
                "Issues": "", "Depth": "",
            })

    new_df      = pd.DataFrame(rows)
    file_exists = os.path.exists(path)
    new_df.to_csv(path, mode="a", header=not file_exists, index=False)
    print(f"✅ Daily log appended: {path} (+{len(rows)} rows)")


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

_telegram_lock = asyncio.Lock()

TELEGRAM_MAX_CHARS = 4000  # stay under Telegram's 4096 hard limit


def _chunk_telegram_message(msg: str, max_chars: int = TELEGRAM_MAX_CHARS) -> list:
    """Split a message into chunks at line boundaries, each under max_chars."""
    if len(msg) <= max_chars:
        return [msg]
    lines = msg.split("\n")
    chunks, current = [], []
    current_len = 0
    for line in lines:
        extra = len(line) + (1 if current else 0)  # +1 for the joining "\n"
        if current and current_len + extra > max_chars:
            chunks.append("\n".join(current))
            current, current_len = [line], len(line)
        else:
            current.append(line)
            current_len += extra
    if current:
        chunks.append("\n".join(current))
    return chunks


async def send_telegram(msg: str, session: aiohttp.ClientSession):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        return
    async with _telegram_lock:
        for chat_id in TELEGRAM_CHAT_IDS:
            chat_id = str(chat_id).strip()
            if not chat_id:
                continue
            for chunk in _chunk_telegram_message(msg):
                try:
                    await session.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                        json={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"},
                        timeout=aiohttp.ClientTimeout(total=10),
                    )
                except Exception as e:
                    print(f"⚠️  Telegram send failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# PER-PAIR WORKER
# ══════════════════════════════════════════════════════════════════════════════

async def process_pair(
    symbol: str,
    target: Optional[float],
    shared_state: dict,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
) -> Optional[dict]:
    """
    Fetches depth + kline for one pair, runs all health checks,
    updates shared_state (time-based anomaly tracking), and returns a result dict.
    Alert firing decisions are left to the caller (main loop).
    """
    async with semaphore:
        monitor_only = target is None
        try:
            # ── Fetch ──────────────────────────────────────────────────────
            depth_raw, kline_raw = await asyncio.gather(
                fetch_depth(session, symbol),
                fetch_kline(session, symbol),
            )

            asks_df, bids_df = build_orderbook_dfs(depth_raw)

            if asks_df.empty or bids_df.empty:
                print(f"[{symbol}] ✗ Empty orderbook side — skipping")
                return None

            ask_layers = len(asks_df)
            bid_layers = len(bids_df)

            # ── Derived metrics ────────────────────────────────────────────
            mid_price, spread_abs, curr_spread = compute_mid_and_spread(asks_df, bids_df)
            dws       = calculate_dws(asks_df, bids_df, mid_price)
            depth_25  = calculate_liquidity_depth(asks_df, bids_df, mid_price, curr_spread * 1.25)
            depth_50  = calculate_liquidity_depth(asks_df, bids_df, mid_price, curr_spread * 1.50)
            imbalance_ratio, heavier_side = calculate_depth_imbalance(asks_df, bids_df, mid_price, curr_spread * 1.25)
            ob_snapshot = get_top_of_book_snapshot(asks_df, bids_df)

            # ── Spread anomaly (A2) ────────────────────────────────────────
            if not monitor_only and target is not None:
                diff = ((curr_spread - target) / target) * 100
                abs_diff_pp = abs(curr_spread - target)  # percentage-point move
                spread_anomaly = (diff > 100 or diff < -75) and abs_diff_pp >= MIN_ABS_SPREAD_DIFF_PCT
            else:
                diff           = None
                spread_anomaly = False

            dws_poor    = dws > DWS_POOR_THRESHOLD
            a2_confirmed = spread_anomaly and dws_poor

            # ── Issue detection ────────────────────────────────────────────
            issues = []

            # A1 — Crossed orderbook
            best_ask = asks_df["price"].iloc[0]
            best_bid = bids_df["price"].iloc[0]
            if best_bid >= best_ask:
                issues.append(("A1", "CRITICAL",
                    f"Crossed orderbook — best bid {best_bid:,.6g} ≥ best ask {best_ask:,.6g}"))

            # A3 — One-sided market (shouldn't happen after empty check, but guard anyway)
            if asks_df.empty:
                issues.append(("A3", "CRITICAL", "One-sided market — no ask orders"))
            elif bids_df.empty:
                issues.append(("A3", "CRITICAL", "One-sided market — no bid orders"))

            # A2 — Spread widening
            if spread_anomaly:
                dws_note = (f" | DWS: {dws:.4f} "
                            f"({'poor — strike counted' if dws_poor else 'ok — skipped'})")
                issues.append(("A2", "HIGH",
                    f"Spread {curr_spread:.4f}% vs target {target}% "
                    f"(diff {diff:+.1f}%){dws_note}"))

            # Shallow orderbook
            if ask_layers < MIN_ORDERBOOK_LAYERS or bid_layers < MIN_ORDERBOOK_LAYERS:
                issues.append(("A2", "HIGH",
                    f"Shallow orderbook — asks:{ask_layers} bids:{bid_layers} "
                    f"(min {MIN_ORDERBOOK_LAYERS})"))

            # A4 — Thin mid-market (informational)
            if 0 < depth_25 < THIN_DEPTH_THRESHOLD:
                issues.append(("A4", "MEDIUM",
                    f"Thin mid-market — depth within spread: {format_depth(depth_25)} "
                    f"(min {format_depth(THIN_DEPTH_THRESHOLD)})"))

            # ── Actionable issues (drive time-based alert timer) ───────────
            critical_issues = [i for i in issues if i[1] == "CRITICAL"]
            shallow_issues  = [i for i in issues if i[0] == "A2" and "Shallow" in i[2]]
            spread_a2       = [i for i in issues if i[0] == "A2" and "Spread" in i[2]]

            actionable = critical_issues + shallow_issues
            if a2_confirmed:
                actionable += spread_a2

            is_poor = bool(actionable)

            # ── Time-based anomaly state  ──────────────────────────────────
            now_iso = ngt_now().isoformat()
            async with _state_lock:
                p = shared_state.get(symbol, {
                    "anomaly_since":    None,   # ISO when anomaly first seen
                    "last_alert":       None,   # ISO of last Telegram alert
                    "last_mid_price":   None,
                    "last_ob_snapshot": None,
                    "stale_ob_count":   0,
                    "price_move_alert": None,   # ISO of last price-move alert
                })

                # ── Mid-price movement ─────────────────────────────────────
                price_move_label = None
                last_mid = p.get("last_mid_price")
                if mid_price and last_mid:
                    pct = ((mid_price - last_mid) / last_mid) * 100
                    if abs(pct) >= MID_PRICE_ALERT_THRESHOLD:
                        direction = "📈" if pct > 0 else "📉"
                        price_move_label = (f"{direction} Price moved {pct:+.2f}% "
                                            f"({last_mid:,.6g} → {mid_price:,.6g})")
                p["last_mid_price"] = mid_price

                # ── Stale orderbook ────────────────────────────────────────
                last_snap = p.get("last_ob_snapshot")
                if ob_snapshot and ob_snapshot == last_snap:
                    p["stale_ob_count"] = p.get("stale_ob_count", 0) + 1
                else:
                    p["stale_ob_count"] = 0
                p["last_ob_snapshot"] = ob_snapshot

                stale_triggered = p["stale_ob_count"] >= STALE_OB_CYCLES
                if stale_triggered:
                    p["stale_ob_count"] = 0
                    if ob_snapshot:
                        stale_issue = (
                            "STALE", "HIGH",
                            f"Stale orderbook — top-of-book unchanged for "
                            f"{STALE_OB_CYCLES} consecutive checks "
                            f"(ask {ob_snapshot['ba_p']:,.6g}×{ob_snapshot['ba_a']}, "
                            f"bid {ob_snapshot['bb_p']:,.6g}×{ob_snapshot['bb_a']})"
                        )
                        issues.append(stale_issue)
                        actionable.append(stale_issue)
                        is_poor = True

                # ── Anomaly timer ──────────────────────────────────────────
                if is_poor:
                    if not p["anomaly_since"]:
                        p["anomaly_since"] = now_iso
                else:
                    p["anomaly_since"] = None
                    p["last_alert"]    = None   # reset cooldown when market recovers

                # Determine whether to fire an alert this cycle
                should_alert = False
                if is_poor and p["anomaly_since"]:
                    anomaly_since_dt = datetime.fromisoformat(p["anomaly_since"])
                    age_minutes      = (ngt_now() - anomaly_since_dt).total_seconds() / 60

                    last_alert_dt = (datetime.fromisoformat(p["last_alert"])
                                     if p["last_alert"] else None)
                    cooldown_ok   = (last_alert_dt is None or
                                     (ngt_now() - last_alert_dt).total_seconds() / 60
                                     >= ALERT_COOLDOWN_MINUTES)

                    if age_minutes >= ANOMALY_ALERT_AFTER_MINUTES and cooldown_ok:
                        should_alert    = True
                        p["last_alert"] = now_iso

                shared_state[symbol] = p

            # ── Spike detection from K-line ────────────────────────────────
            spikes = get_recent_spikes(kline_raw, symbol)
            if spikes:
                print(f"[{symbol}] 🚨 {len(spikes)} volume spike(s) detected")
            else:
                print(f"[{symbol}] ✅ No trade spikes")

            print(f"[{symbol}] ✓ spread={curr_spread:.4f}% mid={mid_price:,.4f} "
                  f"dws={dws:.4f} layers={ask_layers}/{bid_layers} "
                  f"poor={is_poor} alert={should_alert}")

            return {
                "timestamp":       ngt_now().strftime("%Y-%m-%d %H:%M:%S"),
                "symbol":          symbol,
                "monitor_only":    monitor_only,
                "status":          "Warning" if is_poor else "Checked",
                "issues":          "|".join(f"{i[0]}:{i[1]}" for i in issues) if issues else "",
                "should_alert":    should_alert,
                "current_spread":  round(curr_spread, 6),
                "spread_abs":      round(spread_abs, 8),
                "target_spread":   target if not monitor_only else "N/A",
                "percent_diff":    round(diff, 2) if diff is not None else "N/A",
                "mid_price":       round(mid_price, 8),
                "ask_layers":      ask_layers,
                "bid_layers":      bid_layers,
                "dws":             round(dws, 4),
                "dws_poor":        dws_poor,
                "depth_1.25x":     format_depth(depth_25),
                "depth_1.5x":      format_depth(depth_50),
                "imbalance_ratio": (round(imbalance_ratio, 2)
                                    if imbalance_ratio and imbalance_ratio != float("inf")
                                    else ("inf" if imbalance_ratio == float("inf") else "")),
                "heavier_side":    heavier_side or "",
                "_actionable":     actionable,
                "_price_move":     price_move_label,
                "_spikes":         spikes,
            }

        except Exception as e:
            print(f"[{symbol}] ✗ fetch/process error: {e}")
            return None


# ══════════════════════════════════════════════════════════════════════════════
# SINGLE CYCLE
# ══════════════════════════════════════════════════════════════════════════════

FAILED_PAIR_RATIO_FOR_OUTAGE_ALERT = 0.5   # ≥50% of pairs failing looks like an
                                            # outage, not isolated per-pair errors
OUTAGE_ALERT_COOLDOWN_MINUTES      = 30

_last_outage_alert: Optional[datetime] = None


async def run_cycle(shared_state: dict, session: aiohttp.ClientSession, cycle_num: int):
    semaphore   = asyncio.Semaphore(MAX_CONCURRENT_PAIRS)
    cycle_start = ngt_now()

    tasks = [
        process_pair(sym, tgt, shared_state, session, semaphore)
        for sym, tgt in PAIRS
    ]
    raw_results = await asyncio.gather(*tasks)

    results      = [r for r in raw_results if r is not None]
    warnings     = [r for r in results if r["status"] == "Warning"]
    alert_pairs  = [r for r in warnings if r["should_alert"]]
    spike_pairs  = [r for r in results if r.get("_spikes")]
    price_moves  = [(r["symbol"], r["_price_move"]) for r in results if r.get("_price_move")]

    elapsed = (ngt_now() - cycle_start).total_seconds()
    print(f"\n⏱  Cycle {cycle_num} complete in {elapsed:.1f}s — "
          f"{len(results)}/{len(PAIRS)} pairs | "
          f"{len(warnings)} warnings | {len(alert_pairs)} alerts firing")

    # ── Outage detection: a wave of failures means "API problem," not
    #    "44 unrelated coincidences" — worth its own alert with a cooldown
    #    so it doesn't fire every single cycle during a prolonged outage.
    global _last_outage_alert
    failed_count   = len(PAIRS) - len(results)
    failure_ratio  = failed_count / len(PAIRS) if PAIRS else 0
    if failure_ratio >= FAILED_PAIR_RATIO_FOR_OUTAGE_ALERT:
        cooldown_ok = (_last_outage_alert is None or
                       (ngt_now() - _last_outage_alert).total_seconds() / 60
                       >= OUTAGE_ALERT_COOLDOWN_MINUTES)
        if cooldown_ok:
            _last_outage_alert = ngt_now()
            await send_telegram(
                f"🔴 <b>Possible API outage</b>\n"
                f"{failed_count}/{len(PAIRS)} pairs failed to fetch this cycle "
                f"({ngt_now().strftime('%Y-%m-%d %H:%M:%S')} NGT).",
                session,
            )

    # ── Persist ────────────────────────────────────────────────────────────────
    clean_results = []
    for r in results:
        cr = {k: v for k, v in r.items() if not k.startswith("_")}
        clean_results.append(cr)

    update_daily_log(clean_results)

    if clean_results:
        pd.DataFrame(clean_results).to_csv(
            os.path.join(DATA_DIR, "latest.csv"), index=False
        )

    save_state(shared_state)

    # ── Telegram: anomaly alerts ───────────────────────────────────────────────
    if alert_pairs:
        msg  = f"⚠️ <b>Market Anomaly Alert</b>\n"
        msg += f"<i>{ngt_now().strftime('%Y-%m-%d %H:%M:%S')} (NGT)</i>\n"
        msg += f"{'─' * 30}\n"
        for r in alert_pairs:
            msg += f"\n<b>{r['symbol'].upper()}</b>\n"
            if not r["monitor_only"]:
                msg += (f"  Spread: {r['current_spread']}% "
                        f"(Target: {r['target_spread']}%, Diff: {r['percent_diff']:+}%)\n")
            else:
                msg += f"  Spread: {r['current_spread']}% (Monitor only)\n"
            msg += f"  Mid: {r['mid_price']:,.4f}\n"
            msg += f"  DWS: {r['dws']:.4f}{'  ⚠ poor' if r['dws_poor'] else ''}\n"
            msg += f"  Layers — Ask: {r['ask_layers']} | Bid: {r['bid_layers']}\n"
            msg += f"  Depth @ 1.25x: {r['depth_1.25x']} | 1.5x: {r['depth_1.5x']}\n"
            actionable = r.get("_actionable", [])
            if actionable:
                msg += "  <b>Issues:</b>\n"
                for alert_id, severity, label in actionable:
                    icon = "🚨" if severity == "CRITICAL" else "⚠️"
                    msg += f"    {icon} [{alert_id}] {label}\n"
        await send_telegram(msg, session)

    # ── Telegram: price move alerts ───────────────────────────────────────────
    if price_moves:
        msg  = "📊 <b>Price Movement Alerts</b>\n"
        msg += f"<i>{ngt_now().strftime('%Y-%m-%d %H:%M:%S')} (NGT)</i>\n"
        msg += f"{'─' * 30}\n"
        for sym, label in price_moves:
            msg += f"  <b>{sym.upper()}</b>: {label}\n"
        await send_telegram(msg, session)

    # ── Telegram: spike summary ────────────────────────────────────────────────
    if spike_pairs:
        msg  = "🚨 <b>Trade Spike Summary</b>\n"
        msg += f"<i>{ngt_now().strftime('%Y-%m-%d %H:%M:%S')} (NGT)</i>\n"
        msg += f"{'─' * 30}\n"
        for r in spike_pairs:
            msg += f"\n<b>{r['symbol'].upper()}</b>\n"
            for s in r["_spikes"]:
                msg += (f"  {s['window']} ({s['candle_count']} candles) — "
                        f"{s['currency']}{s['quote_volume']:,.2f}\n")
        await send_telegram(msg, session)


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

async def main(run_once: bool = False):
    os.makedirs(DATA_DIR, exist_ok=True)
    shared_state = load_state()
    cycle_num    = 0

    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_PAIRS + 5)
    async with aiohttp.ClientSession(connector=connector) as session:
        if run_once:
            await run_cycle(shared_state, session, cycle_num=1)
            return

        print(f"🚀 Starting continuous monitor — {len(PAIRS)} pairs, "
              f"{CYCLE_SLEEP_SECONDS}s cycle, "
              f"alert after {ANOMALY_ALERT_AFTER_MINUTES}min anomaly")

        while True:
            cycle_num += 1
            print(f"\n{'═' * 50}")
            print(f"  Cycle {cycle_num}  —  {ngt_now().strftime('%Y-%m-%d %H:%M:%S')} NGT")
            print(f"{'═' * 50}")
            try:
                await run_cycle(shared_state, session, cycle_num)
            except Exception as e:
                print(f"⚠️  Cycle {cycle_num} top-level error: {e}")

            print(f"💤 Sleeping {CYCLE_SLEEP_SECONDS}s until next cycle…")
            await asyncio.sleep(CYCLE_SLEEP_SECONDS)


if __name__ == "__main__":
    import sys
    run_once = "--once" in sys.argv
    asyncio.run(main(run_once=run_once))