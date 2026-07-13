"""
Quidax Market Monitor — API-based, OHM alert taxonomy v3
──────────────────────────────────────────────────────────────────────────────
Endpoints used:
  Quidax Depth       : GET /exchange-open-api/api/v1/markets/{symbol}/depth?limit=200
  Quidax K-Line (B4) : GET /exchange-open-api/api/v1/markets/{symbol}/k?period=1&limit=60
                       (1-minute candles, last 60 minutes — a rolling hourly window.
                        Feeds check_circuit_breaker_proximity ONLY. See KLINE_CANDLE_MINUTES/
                        KLINE_LOOKBACK_MINUTES below. fetch_kline().)
  Quidax K-Line (D1) : GET /exchange-open-api/api/v1/markets/{symbol}/k?period=60&limit=4
                       (60-minute candles, last 240 minutes by default — a SEPARATE API call
                        from the B4 fetch above, on its own VOLUME_SPIKE_CANDLE_MINUTES/
                        VOLUME_SPIKE_LOOKBACK_MINUTES. fetch_kline_volume(). Decoupled from
                        B4's window deliberately: tuning D1's candle size/lookback (e.g. to
                        avoid sparse near-zero-volume 1-min candles on illiquid pairs) no
                        longer changes B4's circuit-breaker window, and vice versa.)
  MEXC               : GET /api/v3/ticker/price        (all symbols, one call — price only,
                                                     no 24h stats requested or needed)
  KuCoin        : GET /api/v1/market/allTickers    (all symbols, one call — this is the
                                                     *only* batched ticker endpoint KuCoin's
                                                     spot API exposes, so 24h fields ride
                                                     along whether we want them or not; we
                                                     just never read anything but `last`)

Alert scope (see OHM spec doc for full definitions):
  A1 Crossed Orderbook        — implemented
  A2 Bid-Ask Spread Widening  — implemented
  A3 One-Sided Market         — implemented (previously silently swallowed — see note below)
  A4 Thin Mid-Market          — implemented
  A5 Depth Imbalance          — implemented (was computed but never alerted in v1)
  A6 Layer Churn Stall        — implemented (near-touch layers not refreshing relative
                                 to THIS pair's own typical churn rate — a self-baseline,
                                 not a global threshold, so busy markets with lots of
                                 long-resting customer orders don't false-positive.
                                 Distinct from B3: B3 detects a dead upstream reference
                                 feed connection; A6 detects a live Quidax feed whose
                                 *content* has stopped moving, which can happen even
                                 while the API itself returns fresh data every cycle)
  B1 Price Discrepancy        — implemented, USDT-quoted pairs only (see note below)
  B2 Source Exchange Divergence — implemented, MEXC vs KuCoin
  B3 Stale Reference Feed     — implemented, per source (MEXC / KuCoin)
  B4 Circuit Breaker Proximity — implemented, reference-free (uses Quidax's own k-line window)
  D1 Volume Spike             — implemented (unchanged trigger logic; context is a comparison
                                 against Quidax's own longer-term volume baseline. Runs on its
                                 OWN k-line fetch (fetch_kline_volume, default 60min candles /
                                 240min lookback) — decoupled from B4's kline fetch since
                                 debug.py v3.1. No external 24h data used.)
  E1 Quidax API Failure       — implemented (unchanged: per-pair + outage-ratio detection)
  E2 Reference Feed Disconnect — implemented (MEXC/KuCoin batched call failure)
  F1 Cross-Pair Arbitrage Gap — implemented (triangulates via pairs already being fetched)
  G1 Depth-Walk Partial Fill  — implemented, USDTNGN only. Own 5s polling task, separate
                                 from the main cycle (see depth_walk_loop). Fires when a
                                 simulated 100k-USDT buy or sell can't be filled from the
                                 visible book — MEDIUM, Tier 3 (dashboard-only, no Telegram).
                                 Mid price for the slippage math is itself a small depth
                                 walk (default 1k USDT each side, averaged), not raw
                                 best_ask/best_bid — so a lone dust order at the touch
                                 doesn't distort the reference. Falls back to top-of-book
                                 mid if either side can't supply even the mid walk weight.
  G2 Candle Wick / Anomalous Print — implemented. Reuses B4's own kline_raw
                                 (no separate fetch) — scans every candle in
                                 the rolling window each cycle. low<=0 ->
                                 CRITICAL (Tier 1) regardless of magnitude;
                                 otherwise (high-low)/open*100 >= g2.swing_pct
                                 -> HIGH (Tier 2). Catches single-tick prints
                                 that revert between two depth polls, which
                                 neither B4 (reads open only) nor D1 (volume,
                                 not price) nor live depth polling (60s
                                 cadence) can see.

  C1/C2/C3 (LM bot health) and E3 (bot feed heartbeat) are explicitly OUT OF SCOPE —
  none of these are derivable from public Depth/K-line/reference-ticker data; they
  need direct telemetry from the bot's own process, which this monitor does not have.

NOTE on B1/B2/B3 scope: MEXC and KuCoin only quote assets against USDT (and similar
majors) — there is no MEXC/KuCoin "XNGN" or "XGHS" market to reference. So B1/B2/B3
only run for pairs whose QUOTE currency is "usdt" (e.g. btcusdt, ethusdt). NGN/GHS pairs
(btcngn, usdtngn, etc.) have no independent external price to check against — they're
covered instead by F1, which checks them against the implied cross-rate built from their
own USDT leg + the usdtngn/usdtghs bridge rate, all sourced from Quidax itself.

NOTE on 24h data: nothing in this monitor needs it. B1/B2/B3/B4 only ever use the
*current* price. MEXC's ticker/price endpoint returns price only (no volume at all),
so that's enforced at the fetch layer. KuCoin's allTickers payload still technically
contains 24h fields since there's no lighter batched alternative on their side — we
simply never read them. D1's context line used to borrow external 24h volume for
this; it's now built entirely from Quidax's own k-line history instead (see
update_volume_baseline below) — no external volume data is fetched or used anywhere.

NOTE on alert tiering & cooldowns:
  Tier 1 — fire on first occurrence, then 15-min cooldown per issue per pair:
    A1, A3, A6 (CRITICAL/HIGH), B4-CRITICAL, D1, E1, E2, G2-CRITICAL
  Tier 2 — fire only after N consecutive cycles of the same issue, then 15-min cooldown:
    A2 (spread + shallow book), B1, B2, B3, B4-HIGH, G2-HIGH — N = TIER2_CONFIRM_CYCLES (3)
  Tier 3 — dashboard flag only, never fire Telegram:
    A4, A5, F1, A6-MEDIUM (monitor-only zero-baseline case — see check_layer_churn_stall)

  NOTE: A6 was previously Tier 2 (gated by a now-removed per-A6 confirm-cycles
  knob). It now fires immediately on first occurrence like the other Tier-1 ids.
  The MEDIUM monitor-only variant still routes to Tier 3 (dashboard-only) via
  classify_tier, so promoting A6 did not turn frozen monitor-only books into
  Telegram noise.

  Consecutive counters and cooldown timestamps are persisted in health_state.json
  under each pair's "_alert" sub-key so they survive restarts. Counters reset to 0
  as soon as an issue clears for one cycle. Cooldowns survive regardless — a pair
  that resolves and re-triggers within the cooldown window does not re-fire.

  DELIVERY-GATED COOLDOWNS: a cooldown (and the post-fire counter reset) is committed
  ONLY after send_telegram confirms the message actually delivered (every chunk to
  every chat returned 2xx). should_fire_telegram no longer starts cooldowns as a side
  effect — it only decides + advances the Tier-2 counter. If a send is dropped (400
  bad chat_id, 429 rate limit, transport error), the cooldown is NOT burned and the
  alert retries on the next cycle instead of going dark for the full window.

  DEDUPED ISSUE IDS: two checks legitimately emit the same id twice in one cycle —
  A2 (spread widening + shallow book) and B3 (MEXC stale + KuCoin stale). Those tuples
  are collapsed by dedupe_actionable() at the point of detection so each id reaches
  should_fire_telegram exactly once per cycle (otherwise the Tier-2 counter would
  double-increment and confirm in 2 cycles instead of 3, and the cooldown set by the
  first instance would suppress the second from Telegram).

Run modes:
  python debug.py          # continuous loop (1-min cycle)
  python debug.py --once   # single pass then exit
"""

import asyncio
import json
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
import pandas as pd

from defaults import merge_config, default_config, UPTIME_FIXED_STEP_NGN, SPREAD_GAP_FIXED_NGN  # single source of truth for config

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
TELEGRAM_BOT_TOKEN = os.environ.get("QUIDAX_TG_BOT_TOKEN", "")
TELEGRAM_CHAT_IDS  = [c.strip() for c in os.environ.get("QUIDAX_TG_CHAT_IDS", "").split(",") if c.strip()]

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
    print("⚠️  QUIDAX_TG_BOT_TOKEN / QUIDAX_TG_CHAT_IDS not set — Telegram alerts are disabled.")

BASE_API_URL        = "https://openapi.quidax.io/exchange-open-api/api/v1"
MEXC_TICKER_URL      = "https://api.mexc.com/api/v3/ticker/price"
KUCOIN_TICKER_URL    = "https://api.kucoin.com/api/v1/market/allTickers"

# ── Persistence ───────────────────────────────────────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR    = "/app/data"
STATE_FILE  = os.path.join(DATA_DIR, "health_state.json")
CONFIG_FILE = os.path.join(DATA_DIR, "monitor_config.json")

# Per-pair alert suspensions — {symbol: ISO expiry (NGT)}. Written by api.py when
# an operator taps "Suspend" in the config drawer; read here at the fire gate.
# Deliberately a SEPARATE file from health_state.json: the API writes it out-of-band
# (mid-cycle), while this process rewrites health_state.json wholesale at the end of
# every cycle — sharing one file would let save_state() clobber a suspend the API set
# while a cycle was in flight. This mirrors the monitor_config.json direction
# (api writes / debug reads), so there's no cross-process write race.
SUSPENSIONS_FILE = os.path.join(DATA_DIR, "suspensions.json")

# G1 — USDTNGN depth-walk slippage tracker persistence. Separate files from
# STATE_FILE deliberately: this data updates every 5s (vs. the main 60s cycle)
# and has its own bucket/condense lifecycle — mixing it into health_state.json
# would mean rewriting the whole health state 12x more often than necessary.
DEPTH_WALK_SYMBOL        = "usdtngn"
DEPTH_WALK_RAW_FILE       = os.path.join(DATA_DIR, "usdtngn_slippage_raw.json")
DEPTH_WALK_CONDENSED_FILE = os.path.join(DATA_DIR, "usdtngn_slippage_hourly.json")

# ── Default configuration ─────────────────────────────────────────────────────
# Canonical defaults now live in defaults.py, shared verbatim with api.py so the
# two processes can never drift (this module imports merge_config from it). The
# dashboard writes changes to monitor_config.json; apply_config() re-reads it at
# the top of every cycle so adjustments take effect without restarting the process.


def _load_config_from_disk() -> dict:
    """Read monitor_config.json and deep-merge it over the shared defaults."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                stored = json.load(f)
            return merge_config(stored)
        except Exception as exc:
            print(f"⚠️  Could not read {CONFIG_FILE}: {exc} — using defaults")
    return merge_config({})


def apply_config():
    """
    Load config from disk and apply every value to module-level globals.
    Called once at startup and again at the top of each run_cycle so that
    dashboard edits take effect on the next cycle without a restart.
    """
    global PAIRS, PAIR_ALIASES, DEPTH_LIMIT, KLINE_CANDLE_MINUTES, KLINE_LOOKBACK_MINUTES
    global CYCLE_SLEEP_SECONDS
    global MIN_ORDERBOOK_LAYERS, THIN_DEPTH_THRESHOLD, DEPTH_IMBALANCE_RATIO
    global DWS_POOR_THRESHOLD, MIN_ABS_SPREAD_DIFF_PCT
    global MAX_CONCURRENT_PAIRS, MONITOR_ONLY_SYMBOLS
    global PRICE_DISCREPANCY_PCT, SOURCE_DIVERGENCE_PCT, SOURCE_DIVERGENCE_OVERRIDES, STALE_REFERENCE_CYCLES
    global STALE_UNCHANGED_CYCLES, STALE_MOVEMENT_EPSILON_PCT
    global CIRCUIT_BREAKER_PCT, CIRCUIT_BREAKER_WARN_RATIO, ARB_GAP_PCT
    global G2_SWING_PCT
    global LAYER_CHURN_TOP_PCT, LAYER_CHURN_BASELINE_BUCKETS
    global LAYER_CHURN_RATIO_THRESHOLD
    global VOLUME_SPIKE_MODE, VOLUME_SPIKE_RATIO, VOLUME_SPIKE_MIN_BUCKETS, VOLUME_SPIKE_WARMUP_FALLBACK
    global VOLUME_SPIKE_CANDLE_MINUTES, VOLUME_SPIKE_LOOKBACK_MINUTES, VOLUME_BASELINE_BUCKETS
    global DEPTH_WALK_WEIGHT_USDT, DEPTH_WALK_POLL_INTERVAL_SECONDS
    global DEPTH_WALK_RAW_RETENTION_SECONDS, DEPTH_WALK_CONDENSED_RETENTION_DAYS
    global DEPTH_WALK_MID_WEIGHT_USDT
    global UPTIME_REFERENCE_PRICE, UPTIME_WEIGHT_USDT, UPTIME_BAND_PCT

    cfg = _load_config_from_disk()

    # Pairs — optional 3rd element is a per-exchange alias dict, e.g.
    # {"mexc": "RENDER"}, for assets whose reference-exchange ticker diverges
    # from Quidax's. Stored separately from PAIRS so it doesn't disturb any
    # existing "for sym, tgt in PAIRS" consumer elsewhere in this file.
    PAIRS = []
    PAIR_ALIASES = {}
    for item in cfg["pairs"]:
        sym = str(item[0]).lower()
        tgt = item[1]
        aliases = item[2] if len(item) > 2 and item[2] else {}
        PAIRS.append((sym, tgt))
        PAIR_ALIASES[sym] = aliases

    # K-line (B4 circuit breaker only — D1 has its own fetch, loaded below)
    DEPTH_LIMIT              = int(cfg["orderbook"]["depth_limit"])
    KLINE_CANDLE_MINUTES     = int(cfg["kline"]["candle_minutes"])
    KLINE_LOOKBACK_MINUTES   = int(cfg["kline"]["lookback_minutes"])

    # Timing
    CYCLE_SLEEP_SECONDS = float(cfg["timing"]["cycle_sleep_seconds"])

    # Orderbook thresholds
    MIN_ORDERBOOK_LAYERS     = int(cfg["orderbook"]["min_orderbook_layers"])
    THIN_DEPTH_THRESHOLD     = float(cfg["orderbook"]["thin_depth_threshold"])
    DEPTH_IMBALANCE_RATIO    = float(cfg["orderbook"]["depth_imbalance_ratio"])
    DWS_POOR_THRESHOLD       = float(cfg["orderbook"]["dws_poor_threshold"])
    MIN_ABS_SPREAD_DIFF_PCT  = float(cfg["orderbook"]["min_abs_spread_diff_pct"])

    # Pricing / circuit breaker / arbitrage thresholds
    PRICE_DISCREPANCY_PCT      = float(cfg["pricing"]["price_discrepancy_pct"])
    SOURCE_DIVERGENCE_PCT      = float(cfg["pricing"]["source_divergence_pct"])
    # Per-symbol B2 override map: {symbol: pct}. Keys normalised to lowercase to
    # match PAIRS symbols; bad/negative/non-numeric entries are dropped defensively
    # so a malformed config can't crash the cycle (the global default still applies
    # to anything not in the map). .get() keeps configs written before this existed
    # loading cleanly.
    _raw_div_overrides = cfg["pricing"].get("source_divergence_overrides", {}) or {}
    SOURCE_DIVERGENCE_OVERRIDES = {}
    if isinstance(_raw_div_overrides, dict):
        for _sym, _val in _raw_div_overrides.items():
            try:
                _pct = float(_val)
            except (TypeError, ValueError):
                continue
            if _pct >= 0:
                SOURCE_DIVERGENCE_OVERRIDES[str(_sym).lower()] = _pct
    STALE_REFERENCE_CYCLES     = int(cfg["pricing"]["stale_reference_cycles"])
    STALE_UNCHANGED_CYCLES     = int(cfg["pricing"]["stale_unchanged_cycles"])
    STALE_MOVEMENT_EPSILON_PCT = float(cfg["pricing"]["stale_movement_epsilon_pct"])
    CIRCUIT_BREAKER_PCT        = float(cfg["pricing"]["circuit_breaker_pct"])
    CIRCUIT_BREAKER_WARN_RATIO = float(cfg["pricing"]["circuit_breaker_warn_ratio"])
    ARB_GAP_PCT                = float(cfg["pricing"]["arb_gap_pct"])

    # G2 — candle wick / anomalous print (reuses B4's kline_raw, no separate fetch)
    G2_SWING_PCT = float(cfg.get("g2", {}).get("swing_pct", 5.0))

    # Layer churn (A6) thresholds
    LAYER_CHURN_TOP_PCT         = float(cfg["layer_churn"]["top_pct"])
    LAYER_CHURN_BASELINE_BUCKETS = int(cfg["layer_churn"]["baseline_buckets"])
    LAYER_CHURN_RATIO_THRESHOLD = float(cfg["layer_churn"]["ratio_threshold"])

    # Volume spike (D1) trigger — .get fallbacks so configs written before this
    # block existed still load cleanly (defaults reproduce today's behaviour-plus-floor)
    vs = cfg.get("volume_spike", {})
    VOLUME_SPIKE_MODE            = str(vs.get("mode", "baseline_relative"))
    VOLUME_SPIKE_RATIO           = float(vs.get("spike_ratio", 3.0))
    VOLUME_SPIKE_MIN_BUCKETS     = int(vs.get("min_baseline_buckets", 4))
    VOLUME_SPIKE_WARMUP_FALLBACK = str(vs.get("warmup_fallback", "absolute"))
    # D1's own k-line window — independent of kline.* above (B4-only). .get
    # fallbacks reproduce the pre-split behaviour (1min candles/60min lookback)
    # for any config saved before this split existed.
    VOLUME_SPIKE_CANDLE_MINUTES  = int(vs.get("candle_minutes", 1))
    VOLUME_SPIKE_LOOKBACK_MINUTES = int(vs.get("lookback_minutes", 60))
    VOLUME_BASELINE_BUCKETS      = int(vs.get("baseline_buckets", 24))

    # G1 — depth-walk slippage tracker (USDTNGN only, independent 5s task)
    dw = cfg.get("depth_walk", {})
    DEPTH_WALK_WEIGHT_USDT             = float(dw.get("weight_usdt", 100_000))
    DEPTH_WALK_MID_WEIGHT_USDT         = float(dw.get("mid_weight_usdt", 1_000))
    DEPTH_WALK_POLL_INTERVAL_SECONDS   = float(dw.get("poll_interval_seconds", 5))
    DEPTH_WALK_RAW_RETENTION_SECONDS   = float(dw.get("raw_retention_seconds", 3600))
    DEPTH_WALK_CONDENSED_RETENTION_DAYS = float(dw.get("condensed_retention_days", 365))

    # Liquidity uptime (rides the same depth-walk sample). reference_price is
    # now the ACTIVE target price `s`: the band half-width is a percentage
    # p = n/s*100 (n = fixed 1₦ step) applied multiplicatively to live mid,
    # NOT a flat naira width. weight_usdt is the in-band size threshold,
    # independent of the slippage walk weight above.
    up = dw.get("uptime", {}) or {}
    _up_ref = float(up.get("reference_price", 1400))
    if _up_ref <= 0:
        # p = n/s is undefined for s <= 0. Reject by falling back to the config
        # default `s` rather than disabling the metric or dividing by zero.
        _up_ref = float(default_config()["depth_walk"]["uptime"]["reference_price"])
    UPTIME_REFERENCE_PRICE = _up_ref
    UPTIME_WEIGHT_USDT     = float(up.get("weight_usdt", 100_000))
    # p = n/s*100 — constant given config, recomputed on each apply.
    UPTIME_BAND_PCT        = UPTIME_FIXED_STEP_NGN / UPTIME_REFERENCE_PRICE * 100.0

    # Derived
    MAX_CONCURRENT_PAIRS = 10   # not user-facing yet; keep fixed
    MONITOR_ONLY_SYMBOLS = {sym for sym, tgt in PAIRS if tgt is None}


# Initialise with defaults (or saved config if it already exists)
PAIRS:                       list  = []
PAIR_ALIASES:                dict  = {}
DEPTH_LIMIT:                 int   = 200
KLINE_CANDLE_MINUTES:        int   = 1
KLINE_LOOKBACK_MINUTES:      int   = 60
CYCLE_SLEEP_SECONDS:         float = 60
MIN_ORDERBOOK_LAYERS:        int   = 10
THIN_DEPTH_THRESHOLD:        float = 5_000
DEPTH_IMBALANCE_RATIO:       float = 5.0
DWS_POOR_THRESHOLD:          float = 0.5
MIN_ABS_SPREAD_DIFF_PCT:     float = 0.05
MAX_CONCURRENT_PAIRS:        int   = 10
MONITOR_ONLY_SYMBOLS:        set   = set()
PRICE_DISCREPANCY_PCT:       float = 0.5
SOURCE_DIVERGENCE_PCT:       float = 0.3
SOURCE_DIVERGENCE_OVERRIDES: dict  = {}
STALE_REFERENCE_CYCLES:      int   = 3
STALE_UNCHANGED_CYCLES:      int   = 5
STALE_MOVEMENT_EPSILON_PCT:  float = 0.0
CIRCUIT_BREAKER_PCT:         float = 10.0
CIRCUIT_BREAKER_WARN_RATIO:  float = 0.8
ARB_GAP_PCT:                 float = 0.5
G2_SWING_PCT:                float = 5.0
LAYER_CHURN_TOP_PCT:          float = 0.5
LAYER_CHURN_BASELINE_BUCKETS: int   = 20
LAYER_CHURN_RATIO_THRESHOLD:  float = 0.2
VOLUME_SPIKE_MODE:            str   = "baseline_relative"
VOLUME_SPIKE_RATIO:           float = 3.0
VOLUME_SPIKE_MIN_BUCKETS:     int   = 4
VOLUME_SPIKE_WARMUP_FALLBACK: str   = "absolute"
VOLUME_SPIKE_CANDLE_MINUTES:  int   = 1
VOLUME_SPIKE_LOOKBACK_MINUTES: int  = 60
VOLUME_BASELINE_BUCKETS:      int   = 24
DEPTH_WALK_WEIGHT_USDT:              float = 100_000
DEPTH_WALK_MID_WEIGHT_USDT:          float = 1_000
DEPTH_WALK_POLL_INTERVAL_SECONDS:    float = 5
DEPTH_WALK_RAW_RETENTION_SECONDS:    float = 3600
DEPTH_WALK_CONDENSED_RETENTION_DAYS: float = 365
UPTIME_REFERENCE_PRICE:              float = 1400
UPTIME_WEIGHT_USDT:                  float = 100_000
UPTIME_BAND_PCT:                     float = UPTIME_FIXED_STEP_NGN / 1400 * 100.0
apply_config()  # populate from disk immediately

# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS / HELPERS
# ══════════════════════════════════════════════════════════════════════════════

NIGERIAN_TZ = timezone(timedelta(hours=1))

CURRENCY_SYMBOLS = {"USDT": "$", "NGN": "₦", "GHS": "₵"}
HIGH_VOL_TOKENS  = {"BTC", "ETH", "SOL", "USDC"}

REF_HISTORY_LEN = 8   # rolling readings kept per asset/exchange for B2 drift detection

LAYER_CHURN_MIN_HISTORY_BUCKETS = 5   # A6 cold-start gate — min prior churn readings
                                       # needed before the self-baseline is trusted at
                                       # all (not dashboard-configurable, same spirit as
                                       # D1's hardcoded bucket_count >= 2 gate)


def ngt_now() -> datetime:
    return datetime.now(NIGERIAN_TZ)


# Quote currencies actually present in PAIRS — checked longest-first so
# "usdt" (4 chars) isn't mistaken for a 3-char suffix.
KNOWN_QUOTE_CURRENCIES = ("usdt", "ngn", "ghs")


def split_symbol(sym: str) -> tuple[str, str]:
    """
    Split a concatenated symbol like 'btcusdt' into (base, quote).
    Quote length varies (ngn/ghs = 3 chars, usdt = 4 chars) — a fixed
    sym[:-3] slice silently mis-splits every usdt pair.

    Special case: usdtcngn's quote is "cngn", not "ngn" — but "cngn" can't be
    added as a generic suffix because pairs like btcngn/ethngn/usdcngn also
    happen to end in the literal characters "cngn" (base ends in "c" + "ngn"
    quote), which would mis-split all of those instead. Handling the one real
    *cngn pair explicitly avoids that collision.
    """
    lower = sym.lower()
    if lower == "usdtcngn":
        return "usdt", "cngn"
    for quote in sorted(KNOWN_QUOTE_CURRENCIES, key=len, reverse=True):
        if lower.endswith(quote):
            return lower[:-len(quote)], quote
    return lower[:-3], lower[-3:]


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


def format_depth(val) -> str:
    if val in (None, "", "N/A"):
        return "$0"
    val = float(val)
    if not val:               return "$0"
    if val >= 1_000_000:    return f"${val/1_000_000:.2f}M"
    if val >= 1_000:        return f"${val/1_000:.1f}K"
    return f"${val:.0f}"


# ══════════════════════════════════════════════════════════════════════════════
# API LAYER
# ══════════════════════════════════════════════════════════════════════════════

FETCH_MAX_RETRIES   = 2     # additional attempts after the first failure
FETCH_RETRY_BACKOFF = 1.5   # seconds, doubles each retry


async def _request_json(session: aiohttp.ClientSession, url: str, timeout: int = 10) -> dict | list:
    """GET a URL and return parsed JSON, retrying transient failures."""
    last_exc = None
    for attempt in range(FETCH_MAX_RETRIES + 1):
        try:
            async with session.get(url, headers={"accept": "application/json"},
                                   timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
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
    return payload["data"]


async def fetch_kline(session: aiohttp.ClientSession, symbol: str) -> list:
    """
    B4 (circuit breaker) feed. Returns 1-minute candles for the last 60 minutes
    by default (a rolling window, not calendar-day-scoped), per kline.candle_minutes
    / kline.lookback_minutes. Each candle: [timestamp_ms, open, CLOSE, high, low,
    volume] (strings) — the candle's OWN ts field is milliseconds. Independent of
    D1's fetch_kline_volume below — this window no longer changes when D1's candle
    size/lookback is tuned. B4 currently only reads index 1 (open) via
    check_circuit_breaker_proximity, so the OCHLV vs OHLCV distinction doesn't
    change its behaviour today — but flagged so future readers don't assume the
    other conventional ordering.

    `limit` is the number of candles, i.e. lookback_minutes / candle_minutes — with
    the default candle_minutes=1 that's numerically equal to lookback_minutes, which
    is why this historically read `limit=KLINE_LOOKBACK_MINUTES` directly.

    `timestamp` (the QUERY PARAM, distinct from the candle ts field above) is
    SECONDS since epoch per Quidax's docs (docs.quidax.io/reference/fetch-k-line-
    for-a-market) — NOT milliseconds. Sending milliseconds here resolves to a
    timestamp in the far future, so "only return data after that time" matches
    nothing and the API silently returns an empty candle list every cycle.

    Lookback is aligned to candle boundaries — see fetch_kline_volume for
    the full reasoning; at candle_minutes=1 the impact is tiny but the alignment
    keeps both k-line calls behaving identically.
    """
    candle_count = max(1, KLINE_LOOKBACK_MINUTES // KLINE_CANDLE_MINUTES)
    candle_seconds = KLINE_CANDLE_MINUTES * 60
    now_s = int(ngt_now().timestamp())
    current_boundary = (now_s // candle_seconds) * candle_seconds
    lookback_ts = current_boundary - KLINE_LOOKBACK_MINUTES * 60 - 1
    url = (f"{BASE_API_URL}/markets/{symbol}/k"
           f"?period={KLINE_CANDLE_MINUTES}&limit={candle_count}&timestamp={lookback_ts}")
    payload = await _request_json(session, url)
    return payload["data"]


async def fetch_kline_volume(session: aiohttp.ClientSession, symbol: str) -> list:
    """
    D1 (volume spike) feed — separate API call from fetch_kline above, on its own
    candle_minutes/lookback_minutes (volume_spike.candle_minutes / .lookback_minutes,
    default 60min candles / 240min lookback). Same payload shape as fetch_kline:
    [timestamp_ms, open, close, high, low, volume] (strings) per candle.

    Kept as a distinct function (rather than a parameterised fetch_kline) so B4's
    window and D1's window can never accidentally re-couple through a shared call site.

    `timestamp` (the query param) is SECONDS since epoch — see fetch_kline's
    docstring above for why this matters.

    LOOKBACK IS ALIGNED TO CANDLE BOUNDARIES. Quidax's candles are bucketed to
    fixed clock-hour boundaries (a 60-min candle starts at :00, not "60 min ago").
    A naive lookback of `now - 4h` typically lands mid-hour, so the oldest
    aligned hour's ts is BEFORE the cutoff and gets excluded by the "only k-line
    data after this time" filter. Verified empirically: `now - 240*60` with
    `limit=4` consistently returns 3 candles, not 4 — the window silently
    shrinks by one candle-period. Aligning the lookback to the last closed
    candle boundary AND subtracting 1 second (to make the boundary candle
    inclusive) fixes this so D1 gets its full configured N-candle window
    reliably. The currently-in-progress candle is still excluded (it hasn't
    closed yet, so the API has no candle for it) — that's inherent to using
    closed-candle data and can't be worked around here.
    """
    candle_count = max(1, VOLUME_SPIKE_LOOKBACK_MINUTES // VOLUME_SPIKE_CANDLE_MINUTES)
    candle_seconds = VOLUME_SPIKE_CANDLE_MINUTES * 60
    now_s = int(ngt_now().timestamp())
    # Round DOWN to the last closed candle boundary, step back N candles, then
    # subtract 1 second so the boundary candle at exactly that ts is inclusive.
    current_boundary = (now_s // candle_seconds) * candle_seconds
    lookback_ts = current_boundary - VOLUME_SPIKE_LOOKBACK_MINUTES * 60 - 1
    url = (f"{BASE_API_URL}/markets/{symbol}/k"
           f"?period={VOLUME_SPIKE_CANDLE_MINUTES}&limit={candle_count}&timestamp={lookback_ts}")
    payload = await _request_json(session, url)
    return payload["data"]


async def fetch_mexc_tickers(session: aiohttp.ClientSession) -> dict[str, dict]:
    """
    One batched call covering every MEXC symbol's *price only* — this endpoint doesn't
    even return volume, so there's nothing to accidentally over-fetch. Returns
    {ASSET: {"price":}} keyed by base asset, for every ...USDT pair MEXC lists.
    """
    data = await _request_json(session, MEXC_TICKER_URL, timeout=15)
    out = {}
    for row in data:
        sym = row.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4].upper()
        try:
            out[base] = {"price": float(row["price"])}
        except (KeyError, ValueError, TypeError):
            continue
    return out


async def fetch_kucoin_tickers(session: aiohttp.ClientSession) -> dict[str, dict]:
    payload = await _request_json(session, KUCOIN_TICKER_URL, timeout=15)
    out = {}
    for row in payload.get("data", {}).get("ticker", []):
        sym = row.get("symbol", "")
        if not sym.endswith("-USDT"):
            continue
        base = sym[:-5].upper()
        try:
            buy  = row.get("buy")
            sell = row.get("sell")
            if buy is None or sell is None:
                # fall back to last if bid/ask absent
                last = row.get("last")
                if last is None:
                    continue
                price = float(last)
            else:
                price = (float(buy) + float(sell)) / 2
        except (ValueError, TypeError):
            continue
        out[base] = {"price": price}
    return out


async def fetch_reference_data(session: aiohttp.ClientSession) -> tuple[dict, dict, list]:
    """
    Fetches both reference exchanges. Each is independent — if one fails, the other
    can still be used (B2/B3 logic below degrades gracefully to single-source).
    Returns (mexc_map, kucoin_map, e2_issues).
    """
    mexc_map, kucoin_map = {}, {}
    e2_issues = []
    try:
        mexc_map = await fetch_mexc_tickers(session)
    except Exception as e:
        print(f"⚠️  MEXC reference feed failed: {e}")
        e2_issues.append(("E2", "CRITICAL", f"MEXC reference feed unreachable: {e}"))
    try:
        kucoin_map = await fetch_kucoin_tickers(session)
    except Exception as e:
        print(f"⚠️  KuCoin reference feed failed: {e}")
        e2_issues.append(("E2", "CRITICAL", f"KuCoin reference feed unreachable: {e}"))
    return mexc_map, kucoin_map, e2_issues


# ══════════════════════════════════════════════════════════════════════════════
# ORDERBOOK ANALYTICS
# ══════════════════════════════════════════════════════════════════════════════

def build_orderbook_dfs(raw: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Convert raw depth payload to ask/bid DataFrames with columns [price, amount]."""
    def to_df(rows):
        empty = pd.DataFrame(columns=["price", "amount"])
        if not rows:
            return empty
        df = pd.DataFrame(rows, columns=["price", "amount"]).astype(float)
        # Drop phantom/sentinel levels. A level is real liquidity only if it has
        # a positive price AND positive size. Exchanges emit sentinel rows like
        # [0, 0] to signal an empty side; taken literally these count as a bogus
        # layer, which (a) hides a genuinely one-sided book from the A3 gate and
        # (b) poisons mid_price via best_bid/best_ask == 0. Filtering here, at the
        # single parser choke point, makes an all-sentinel side correctly .empty.
        df = df[(df["price"] > 0) & (df["amount"] > 0)].reset_index(drop=True)
        return df if not df.empty else empty

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


# ══════════════════════════════════════════════════════════════════════════════
# A-SERIES CHECKS (pure orderbook, no external reference needed)
# ══════════════════════════════════════════════════════════════════════════════

def check_depth_imbalance(imbalance_ratio, heavier_side) -> list:
    """A5 — depth imbalance. (Computed in v1 but never wired into alerting.)"""
    if imbalance_ratio is None:
        return []
    if imbalance_ratio == float("inf"):
        return [("A5", "HIGH", f"Depth imbalance — all visible depth is on the {heavier_side} side")]
    if imbalance_ratio >= DEPTH_IMBALANCE_RATIO:
        return [("A5", "MEDIUM",
            f"Depth imbalance {imbalance_ratio:.1f}x — {heavier_side} side heavier "
            f"(threshold {DEPTH_IMBALANCE_RATIO}x)")]
    return []


def extract_top_levels(df: pd.DataFrame, top_pct: float) -> list:
    """
    A6 — returns the near-touch (price, amount) pairs for one side of the book:
    the nearest `top_pct` fraction of levels, ordered nearest-to-mid first (df is
    already sorted that way by build_orderbook_dfs). Always at least 1 level.

    Returned as plain lists (not tuples) deliberately — this gets persisted to
    health_state.json and reloaded as JSON on the next cycle, and JSON has no
    tuple type. Comparing a freshly-built tuple against a reloaded list would
    silently never match ([1,2] != (1,2) in Python) and make every cycle look
    like 100% churn. Keeping everything as lists end-to-end avoids that trap.
    """
    if df.empty:
        return []
    n = max(1, math.ceil(len(df) * top_pct))
    return [[round(float(r.price), 8), round(float(r.amount), 8)] for r in df.head(n).itertuples()]


def compute_layer_churn(prev_asks: Optional[list], prev_bids: Optional[list],
                         curr_asks: list, curr_bids: list) -> Optional[float]:
    """
    A6 — fraction of near-touch slots that changed since last cycle, both sides
    combined. Returns None on a pair's first-ever cycle (no previous snapshot to
    diff against). A level appearing/disappearing near the touch shifts every
    slot after it and counts each shifted slot as "changed" — that's genuine
    book activity, not noise, so it's deliberately not smoothed away.
    """
    if prev_asks is None or prev_bids is None:
        return None

    def side_diff(prev: list, curr: list) -> tuple[int, int]:
        total = max(len(prev), len(curr))
        if total == 0:
            return 0, 0
        changed = sum(
            1 for i in range(total)
            if (prev[i] if i < len(prev) else None) != (curr[i] if i < len(curr) else None)
        )
        return changed, total

    a_changed, a_total = side_diff(prev_asks, curr_asks)
    b_changed, b_total = side_diff(prev_bids, curr_bids)
    total = a_total + b_total
    if total == 0:
        return None
    return (a_changed + b_changed) / total


def update_layer_churn_baseline(symbol: str, churn_score: float, layer_hist_root: dict) -> tuple[Optional[float], int]:
    """
    A6 self-baseline: mean of THIS market's own prior churn scores — same
    "exclude the current reading from its own baseline" pattern as D1's volume
    baseline (update_volume_baseline). No time-bucketing needed here, unlike D1:
    churn is already a single-cycle diff, not an overlapping rolling window, so
    every cycle is a genuinely distinct reading.
    """
    hist    = layer_hist_root.setdefault(symbol, {})
    scores  = hist.setdefault("churn_scores", [])
    prior   = list(scores)
    baseline = (sum(prior) / len(prior)) if prior else None
    scores.append(churn_score)
    hist["churn_scores"] = scores[-LAYER_CHURN_BASELINE_BUCKETS:]
    return baseline, len(prior)


def check_layer_churn_stall(churn_score: Optional[float], baseline: Optional[float],
                             bucket_count: int, monitor_only: bool = False) -> list:
    """
    A6 — fires when near-touch layers have stopped refreshing relative to THIS
    market's own typical churn rate, not a global threshold. This is what makes
    it safe for busy markets that naturally carry a lot of long-resting customer
    orders near the touch (those markets just get a higher baseline to compare
    against, not a free pass via some fixed cutoff).

    Special case — baseline == 0: if a pair's book has been frozen since before
    the monitor started watching it, every churn reading is 0.0, which means the
    self-baseline ALSO converges to 0.0. A baseline built entirely from the stall
    just looks "normal" — ratio-vs-baseline can never catch it, because there's
    no non-stalled period in the window to compare against. For bot-managed pairs
    (monitor_only=False), zero churn across the *entire* baseline window is itself
    the strongest possible stall signal, not an absence of one — so it's handled
    explicitly here rather than silently passed through.
    """
    if churn_score is None or baseline is None or bucket_count < LAYER_CHURN_MIN_HISTORY_BUCKETS:
        return []
    if baseline <= 0:
        if churn_score <= 0:
            if monitor_only:
                return [("A6", "MEDIUM",
                    f"Near-touch layers show zero churn across the entire "
                    f"{bucket_count}-cycle baseline window — dashboard visibility "
                    f"only (monitor-only pair, no spread target configured)")]
            return [("A6", "CRITICAL",
                f"Near-touch layers show zero churn across the entire "
                f"{bucket_count}-cycle baseline window — book may already have "
                f"been stalled when monitoring started; no non-stalled period "
                f"available to compare against")]
        return []  # baseline 0 but current churn > 0 — book just started moving, fine
    ratio = churn_score / baseline
    if ratio < LAYER_CHURN_RATIO_THRESHOLD:
        severity = "CRITICAL" if ratio < LAYER_CHURN_RATIO_THRESHOLD / 2 else "HIGH"
        return [("A6", severity,
            f"Near-touch layers stalled — churn {churn_score:.0%} this cycle vs "
            f"{baseline:.0%} typical for this market (ratio {ratio:.2f}, "
            f"fires below {LAYER_CHURN_RATIO_THRESHOLD:.2f})")]
    return []


# ══════════════════════════════════════════════════════════════════════════════
# B-SERIES CHECKS (require MEXC/KuCoin reference — USDT-quoted pairs only)
# ══════════════════════════════════════════════════════════════════════════════

def _effectively_unchanged(prev, cur, eps_pct: float) -> bool:
    """
    True if `cur` is within eps_pct (relative %) of `prev` — i.e. the reading
    didn't meaningfully move. eps_pct == 0.0 reduces to exact equality, which is
    the historical B3 behaviour. Used for the per-source UNCHANGED counter.
    """
    if prev is None or cur is None:
        return False
    if prev == 0:
        return cur == 0
    return abs(cur - prev) / abs(prev) * 100.0 <= eps_pct


def _series_is_moving(series: list, eps_pct: float) -> bool:
    """
    True if a price series shows movement beyond eps_pct (relative range over its
    mean). This is the cross-source liveness signal: if THIS source is frozen but
    the PEER's recent window is still moving, the market is live and this source is
    genuinely stuck (real B3). If the peer is also flat, it's a quiet market.
    A series of <2 readings can't establish movement, so it reads as not-moving.
    """
    vals = [v for v in series if v is not None]
    if len(vals) < 2:
        return False
    lo, hi = min(vals), max(vals)
    base = sum(vals) / len(vals)
    if base == 0:
        return hi != lo
    return (hi - lo) / abs(base) * 100.0 > eps_pct


def resolve_trusted_price(asset: str, m_price, m_ok: bool, k_price, k_ok: bool,
                           ref_hist: dict,
                           divergence_threshold: Optional[float] = None) -> tuple[Optional[float], list]:
    """
    Per-asset reference resolution: detects B3 (stale feed) per source, then B2
    (source divergence) between the two surviving sources, and returns a single
    trusted price plus whatever issues fired along the way. `ref_hist` is the
    persisted per-asset state dict (rolling history + stale counters).

    `divergence_threshold` is the effective B2 % for THIS pair: the caller resolves
    it from the per-symbol override map (falling back to the global). When None
    (e.g. a direct/legacy call), it falls back to the global SOURCE_DIVERGENCE_PCT
    here, so old call sites keep their previous behaviour.

    NOTE: this can legitimately return TWO ("B3", ...) tuples in one cycle (one
    per source) — the caller folds them via dedupe_actionable before any tier /
    cooldown logic runs, so the duplicate id never double-counts.
    """
    issues = []
    ref_hist.setdefault("mexc", [])
    ref_hist.setdefault("kucoin", [])
    ref_hist.setdefault("m_unavail", 0)     # consecutive cycles MEXC failed to resolve
    ref_hist.setdefault("k_unavail", 0)
    ref_hist.setdefault("m_unchanged", 0)   # consecutive cycles MEXC resolved but didn't move
    ref_hist.setdefault("k_unchanged", 0)
    ref_hist.setdefault("m_ever_ok", False)
    ref_hist.setdefault("k_ever_ok", False)
    if m_ok:
        ref_hist["m_ever_ok"] = True
    if k_ok:
        ref_hist["k_ever_ok"] = True

    # ---- B3: per-source staleness, split into two independent conditions ----
    # The old single counter conflated two very different failures:
    #   (a) UNAVAILABLE — the source didn't resolve at all (API error / unlisted /
    #       wrong alias). A genuinely dead upstream feed; fires fast (Tier 2) once
    #       it has crossed STALE_REFERENCE_CYCLES.
    #   (b) UNCHANGED   — the source resolved but returned a price within
    #       STALE_MOVEMENT_EPSILON_PCT of last cycle. In a quiet / low-vol market
    #       this is normal, so on its own it is NOT evidence of a dead feed.
    #
    # The unchanged path is gated on CROSS-SOURCE LIVENESS: once it crosses
    # STALE_UNCHANGED_CYCLES it only escalates to Telegram (Tier 2) when the PEER
    # source is still moving — market live, this source stuck. If the peer is also
    # flat (calm market) or there is no usable peer (single-source asset), it is
    # emitted as MEDIUM, which classify_tier() routes to Tier 3 (dashboard-only,
    # no Telegram) — visible, but silent. Mirrors the A6 monitor-only convention.
    #
    # Both paths stay gated on *_ever_ok so a source that never once resolved
    # (a config gap, not a live feed dying) can't escalate.

    # --- per-source counters (unavailable takes precedence; resets the other) ---
    if not m_ok:
        ref_hist["m_unavail"] += 1
        ref_hist["m_unchanged"] = 0
    else:
        ref_hist["m_unavail"] = 0
        if ref_hist["mexc"] and _effectively_unchanged(
                ref_hist["mexc"][-1], m_price, STALE_MOVEMENT_EPSILON_PCT):
            ref_hist["m_unchanged"] += 1
        else:
            ref_hist["m_unchanged"] = 0

    if not k_ok:
        ref_hist["k_unavail"] += 1
        ref_hist["k_unchanged"] = 0
    else:
        ref_hist["k_unavail"] = 0
        if ref_hist["kucoin"] and _effectively_unchanged(
                ref_hist["kucoin"][-1], k_price, STALE_MOVEMENT_EPSILON_PCT):
            ref_hist["k_unchanged"] += 1
        else:
            ref_hist["k_unchanged"] = 0

    # append AFTER the unchanged comparison (which needs the prior value), so the
    # peer-liveness check below sees the freshest window including this cycle.
    if m_price is not None:
        ref_hist["mexc"] = (ref_hist["mexc"] + [m_price])[-REF_HISTORY_LEN:]
    if k_price is not None:
        ref_hist["kucoin"] = (ref_hist["kucoin"] + [k_price])[-REF_HISTORY_LEN:]

    # --- firing decisions, per source ---
    # *_pricing_stale: exclude this source from B1/B2 trusted-price math. Only a
    # confirmed dead feed or a confirmed real freeze (peer moving) does this; a
    # calm-flat source stays usable, since its price is still good.
    m_pricing_stale = False
    k_pricing_stale = False

    # MEXC
    if ref_hist["m_unavail"] >= STALE_REFERENCE_CYCLES and ref_hist["m_ever_ok"]:
        sev = "CRITICAL" if ref_hist["m_unavail"] >= STALE_REFERENCE_CYCLES * 2 else "HIGH"
        issues.append(("B3", sev,
            f"MEXC {asset}USDT feed unavailable — failed to resolve for {ref_hist['m_unavail']} cycles"))
        m_pricing_stale = True
    elif ref_hist["m_unchanged"] >= STALE_UNCHANGED_CYCLES and ref_hist["m_ever_ok"]:
        peer_moving = k_ok and _series_is_moving(
            ref_hist["kucoin"][-STALE_UNCHANGED_CYCLES:], STALE_MOVEMENT_EPSILON_PCT)
        if peer_moving:
            sev = "CRITICAL" if ref_hist["m_unchanged"] >= STALE_UNCHANGED_CYCLES * 2 else "HIGH"
            issues.append(("B3", sev,
                f"MEXC {asset}USDT feed frozen — unchanged for {ref_hist['m_unchanged']} cycles "
                f"while KuCoin still moving"))
            m_pricing_stale = True
        else:
            issues.append(("B3", "MEDIUM",
                f"MEXC {asset}USDT unchanged for {ref_hist['m_unchanged']} cycles "
                f"(peer flat/absent — likely quiet market, not a dead feed)"))

    # KuCoin
    if ref_hist["k_unavail"] >= STALE_REFERENCE_CYCLES and ref_hist["k_ever_ok"]:
        sev = "CRITICAL" if ref_hist["k_unavail"] >= STALE_REFERENCE_CYCLES * 2 else "HIGH"
        issues.append(("B3", sev,
            f"KuCoin {asset}-USDT feed unavailable — failed to resolve for {ref_hist['k_unavail']} cycles"))
        k_pricing_stale = True
    elif ref_hist["k_unchanged"] >= STALE_UNCHANGED_CYCLES and ref_hist["k_ever_ok"]:
        peer_moving = m_ok and _series_is_moving(
            ref_hist["mexc"][-STALE_UNCHANGED_CYCLES:], STALE_MOVEMENT_EPSILON_PCT)
        if peer_moving:
            sev = "CRITICAL" if ref_hist["k_unchanged"] >= STALE_UNCHANGED_CYCLES * 2 else "HIGH"
            issues.append(("B3", sev,
                f"KuCoin {asset}-USDT feed frozen — unchanged for {ref_hist['k_unchanged']} cycles "
                f"while MEXC still moving"))
            k_pricing_stale = True
        else:
            issues.append(("B3", "MEDIUM",
                f"KuCoin {asset}-USDT unchanged for {ref_hist['k_unchanged']} cycles "
                f"(peer flat/absent — likely quiet market, not a dead feed)"))

    usable_m = m_price if (m_ok and not m_pricing_stale) else None
    usable_k = k_price if (k_ok and not k_pricing_stale) else None

    # ---- B2: source divergence ----
    # Effective threshold is per-pair (override map), falling back to the global.
    div_thr = divergence_threshold if divergence_threshold is not None else SOURCE_DIVERGENCE_PCT
    trusted = None
    if usable_m is not None and usable_k is not None:
        avg = (usable_m + usable_k) / 2
        divergence_pct = abs(usable_m - usable_k) / avg * 100 if avg else 0
        if divergence_pct > div_thr:
            m_mean = sum(ref_hist["mexc"]) / len(ref_hist["mexc"]) if ref_hist["mexc"] else usable_m
            k_mean = sum(ref_hist["kucoin"]) / len(ref_hist["kucoin"]) if ref_hist["kucoin"] else usable_k
            outlier_is_mexc = abs(usable_m - m_mean) > abs(usable_k - k_mean)
            outlier = "MEXC" if outlier_is_mexc else "KuCoin"
            trusted = usable_k if outlier_is_mexc else usable_m
            issues.append(("B2", "HIGH",
                f"Source divergence {divergence_pct:.2f}% on {asset} "
                f"(fires past {div_thr:.2f}%) — "
                f"MEXC {usable_m:,.6g} vs KuCoin {usable_k:,.6g} — "
                f"{outlier} flagged as outlier and suspended from pricing this cycle"))
        else:
            trusted = avg
    elif usable_m is not None:
        trusted = usable_m
    elif usable_k is not None:
        trusted = usable_k
    # else: neither source usable -> trusted stays None (B1 will simply be skipped)

    return trusted, issues


def check_price_discrepancy(quidax_mid: float, trusted_price: Optional[float],
                             target_spread: Optional[float] = None) -> list:
    """
    B1 — Quidax mid price vs. trusted external reference. USDT-quoted pairs only
    (caller-gated).

    The LM bot doesn't center its quotes symmetrically on the reference price —
    it applies the pair's target spread as a markup, so the resulting mid price
    normally sits ~target_spread/2 away from the reference even when everything
    is working exactly as designed (e.g. target spread 2% → ~1% expected offset).
    A flat global threshold couldn't tell that apart from real price drift, and
    fired false positives on any pair whose own expected offset already exceeded
    the threshold on its own.

    PRICE_DISCREPANCY_PCT is now the EXTRA tolerance allowed beyond that pair's
    own expected offset, not the whole budget — so the effective firing point is
    target_spread/2 + PRICE_DISCREPANCY_PCT, different per pair, not one global %.
    target_spread=None (shouldn't occur for B1-eligible pairs today, but handled
    defensively) falls back to the old flat-threshold behavior.
    """
    if trusted_price is None or not quidax_mid:
        return []
    diff_pct = (quidax_mid - trusted_price) / trusted_price * 100
    expected_offset_pct = (target_spread / 2.0) if target_spread else 0.0
    threshold_pct = expected_offset_pct + PRICE_DISCREPANCY_PCT
    if abs(diff_pct) >= threshold_pct:
        severity = "CRITICAL" if abs(diff_pct) >= expected_offset_pct + (PRICE_DISCREPANCY_PCT * 2) else "HIGH"
        return [("B1", severity,
            f"Quidax {quidax_mid:,.6g} vs reference {trusted_price:,.6g} "
            f"({diff_pct:+.2f}%) — expected offset ~{expected_offset_pct:.2f}% from "
            f"target spread ({target_spread if target_spread else 0}%), tolerance "
            f"±{PRICE_DISCREPANCY_PCT}% beyond that (fires past ±{threshold_pct:.2f}%)")]
    return []


def check_circuit_breaker_proximity(kline_raw: list, current_mid: float) -> list:
    """
    B4 — reference-free: compares current mid against the open of the oldest candle
    in the k-line lookback window. circuit_breaker_pct/warn_ratio are dashboard-configurable.
    """
    if not kline_raw or not current_mid:
        return []
    try:
        # Quidax returns candles latest-to-earliest, so the oldest candle in the
        # window — the true anchor for "moved X% over the last N minutes" — is
        # the LAST element, not the first. [ts, open, close, high, low, vol].
        window_open = float(kline_raw[-1][1])
    except (IndexError, ValueError, TypeError):
        return []
    if not window_open:
        return []
    move_pct  = (current_mid - window_open) / window_open * 100
    warn_level = CIRCUIT_BREAKER_PCT * CIRCUIT_BREAKER_WARN_RATIO
    if abs(move_pct) >= CIRCUIT_BREAKER_PCT:
        return [("B4", "CRITICAL",
            f"Price moved {move_pct:+.2f}% within the {KLINE_LOOKBACK_MINUTES}min window — "
            f"at/beyond configured breaker threshold ({CIRCUIT_BREAKER_PCT}%)")]
    if abs(move_pct) >= warn_level:
        return [("B4", "HIGH",
            f"Price moved {move_pct:+.2f}% within the {KLINE_LOOKBACK_MINUTES}min window — "
            f"approaching breaker threshold ({CIRCUIT_BREAKER_PCT}%, warn at {warn_level:.1f}%)")]
    return []


def check_candle_wicks(kline_raw: list) -> list:
    """
    G2 — candle wick / anomalous print. Reuses B4's own kline_raw (1-minute
    candles, KLINE_LOOKBACK_MINUTES window) — no separate API call.

    Scans EVERY candle currently in the window each cycle (not just the
    newest), because a wick can happen and fully revert between two 60s
    depth polls — this is the check that closes that blind spot. A given
    anomalous candle will naturally re-appear here on every cycle it's
    still inside the window, which is what drives Tier-2 confirmation and
    keeps it visible in the daily log for as long as it's relevant — no
    separate dedup/persistence needed on top of the existing per-(symbol,
    issue_id) cooldown machinery.

    Per candle [ts, open, close, high, low, vol]:
      low <= 0                         -> CRITICAL, always fires regardless
                                           of swing_pct (low is the minimum
                                           of the whole period, so this also
                                           catches a zero open/close without
                                           a separate check).
      (high - low) / open * 100 >= G2_SWING_PCT -> HIGH, total-range swing.

    Multiple candles firing in one cycle emit multiple ("G2", ...) tuples —
    dedupe_actionable() (same as A2/B3) folds them into one, keeping the
    highest severity and merging labels.
    """
    if not kline_raw:
        return []

    issues = []
    for candle in kline_raw:
        try:
            ts, open_, _close, high, low = (
                int(candle[0]), float(candle[1]), float(candle[2]),
                float(candle[3]), float(candle[4]),
            )
        except (IndexError, ValueError, TypeError):
            continue  # malformed candle — skip it, don't crash the cycle
        if open_ <= 0:
            continue  # can't compute a % swing off a zero/negative open

        candle_time = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%H:%M UTC")

        if low <= 0:
            issues.append(("G2", "CRITICAL",
                f"Candle at {candle_time} printed low={low:g} — price hit zero"))
            continue  # zero print already flagged; skip the swing check for this candle

        swing_pct = (high - low) / open_ * 100
        if swing_pct >= G2_SWING_PCT:
            issues.append(("G2", "HIGH",
                f"Candle at {candle_time} swung {swing_pct:.2f}% "
                f"(high {high:g} / low {low:g}) — at/beyond configured threshold ({G2_SWING_PCT}%)"))

    return issues


# ══════════════════════════════════════════════════════════════════════════════
# K-LINE SPIKE DETECTION (D1)
# ══════════════════════════════════════════════════════════════════════════════

def compute_window_volume(candles: list, sym: str) -> Optional[dict]:
    """
    Pure aggregation: sums the last VOLUME_SPIKE_LOOKBACK_MINUTES of
    VOLUME_SPIKE_CANDLE_MINUTES-minute candles (D1's own fetch_kline_volume feed,
    independent of B4's fetch_kline) into a single rolling quote-volume figure for
    this pair. Does NOT apply any threshold — that's D1's job (get_recent_spikes) —
    this just measures "how much traded." Pure rolling window — does NOT filter by
    calendar date.
    """
    if not candles:
        return None
    currency = get_currency_symbol(sym)
    total_quote_volume, candle_count = 0.0, 0
    window_start = window_end = None

    for candle in candles:
        try:
            # Quidax's actual field order per docs + empirical verification is
            # [ts, open, CLOSE, high, low, volume] — NOT the more common
            # [ts, open, high, low, close, volume]. Confirmed by inspecting
            # candles where high != low: e.g. [ts, 1393.95, 1393.93, 1393.97, 1390, ...]
            # can only be OCHLV since 1393.97 > 1393.93 rules out that being low.
            # Prior code unpacked as OHLCV and quoted-volume was silently multiplied
            # by the candle's LOW instead of CLOSE — off by a small amount per
            # candle, but wrong.
            ts, o, c, h, l, volume = candle[:6]
            candle_dt = datetime.fromtimestamp(int(ts) / 1000, tz=NIGERIAN_TZ)
            total_quote_volume += float(volume) * float(c)
            candle_count += 1
            if window_start is None or candle_dt < window_start:
                window_start = candle_dt
            if window_end is None or candle_dt > window_end:
                window_end = candle_dt
        except (ValueError, TypeError, IndexError):
            continue

    if candle_count == 0:
        return None

    window_label = (f"{window_start.strftime('%H:%M')}–{window_end.strftime('%H:%M')}"
                     if window_start and window_end else f"last {VOLUME_SPIKE_LOOKBACK_MINUTES} min")
    return {"window": window_label, "candle_count": candle_count,
            "quote_volume": total_quote_volume, "currency": currency}


def update_volume_baseline(symbol: str, current_volume: float, vol_hist_root: dict) -> tuple[Optional[float], int]:
    """
    Builds D1's "Quidax-only" baseline with no external data and no extra API calls
    beyond fetch_kline_volume — purely from the rolling window volume we already
    compute every cycle.

    A new "bucket" is only recorded once per VOLUME_SPIKE_LOOKBACK_MINUTES of real
    elapsed time, not every cycle. This matters: with the default 60s cycle and a
    240min lookback window, consecutive cycles' rolling windows overlap heavily —
    recording every cycle would just average near-duplicate overlapping readings
    against themselves and tell us almost nothing. Sampling once per window-length
    gives genuinely distinct historical readings to compare "right now" against.

    Returns (baseline_mean_of_PRIOR_buckets, how_many_prior_buckets_that_mean_is_built_from).
    The just-recorded current reading is deliberately excluded from its own baseline.
    """
    pair_hist = vol_hist_root.setdefault(symbol, {"buckets": [], "last_bucket_ts": None})
    now = ngt_now()
    last_ts = datetime.fromisoformat(pair_hist["last_bucket_ts"]) if pair_hist["last_bucket_ts"] else None

    prior_buckets = list(pair_hist["buckets"])
    baseline = (sum(prior_buckets) / len(prior_buckets)) if prior_buckets else None

    if last_ts is None or (now - last_ts).total_seconds() >= VOLUME_SPIKE_LOOKBACK_MINUTES * 60:
        pair_hist["buckets"].append(current_volume)
        pair_hist["buckets"] = pair_hist["buckets"][-VOLUME_BASELINE_BUCKETS:]
        pair_hist["last_bucket_ts"] = now.isoformat()

    return baseline, len(prior_buckets)


def get_recent_spikes(window_info: Optional[dict], sym: str,
                       baseline: Optional[float], bucket_count: int) -> list:
    """
    D1 — fires when this pair's rolling-window volume is unusually large *for this
    pair*, not just large in absolute terms.

    Returns issue tuples in the SAME shape every other check emits — a list of
    ("D1", "HIGH", label) — so the rest of the pipeline (dedupe_actionable,
    classify_tier, should_fire_telegram, the anomaly Telegram message loop,
    update_daily_log, and the dashboard's issues badge parsing) all treat D1
    identically to A1/A3/A6/B1/B2/B3/B4. Previously D1 lived in its own parallel
    `_spikes` list of dicts with a separate Telegram message, separate cooldown-
    detail branches, a separate daily-log status ("SPIKE") — all removed as of
    this consolidation. Severity is fixed at "HIGH" per user's chosen policy;
    unlike B4 there is no CRITICAL escalation for extreme spikes.

    Trigger (mode="baseline_relative", the default):
        window_volume >= VOLUME_SPIKE_RATIO * baseline   AND   window_volume >= floor
      where `floor` is the per-pair absolute threshold from get_threshold(). The
      floor keeps D1 from firing on a pair that does, say, 3x its normal but tiny
      volume (a baseline ratio with no economic significance), and stops a busy
      pair whose normal volume already clears the old flat threshold from firing
      every single cycle — exactly the per-pair-anchoring fix that B1 got.

    Baseline trust / warm-up:
      The baseline lives in health_state.json and rebuilds from zero after a wipe
      (e.g. an OOM reset). Until it has >= VOLUME_SPIKE_MIN_BUCKETS recorded windows
      (and is > 0, guarding the divide), the ratio isn't trusted. In that window:
        warmup_fallback="absolute" → trigger on the floor alone (today's behaviour,
                                     so no blind spot right after a restart)
        warmup_fallback="suppress" → no D1 until the baseline is ready
      mode="absolute" bypasses the baseline entirely and always uses the flat floor.
    """
    threshold = get_threshold(sym)
    if threshold is None or window_info is None:
        return []                       # monitor-only pair, or no k-line data

    vol = window_info["quote_volume"]
    cur = get_currency_symbol(sym)

    baseline_trusted = (
        VOLUME_SPIKE_MODE == "baseline_relative"
        and baseline is not None and baseline > 0
        and bucket_count >= VOLUME_SPIKE_MIN_BUCKETS
    )

    if baseline_trusted:
        fired = vol >= VOLUME_SPIKE_RATIO * baseline and vol >= threshold
        trigger = "baseline_relative"
    else:
        # absolute mode, or baseline-relative still warming up
        if VOLUME_SPIKE_MODE == "baseline_relative" and VOLUME_SPIKE_WARMUP_FALLBACK == "suppress":
            return []                   # deliberate warm-up blind spot
        fired = vol >= threshold
        trigger = "absolute"

    if not fired:
        return []

    # Label mirrors the anomaly-alert phrasing style: a short first-line
    # summary sufficient to identify the check + the pair-specific context
    # ("3x baseline over 6 windows, floor $5,000" etc). This is what appears
    # both in the consolidated Telegram message and in the daily log's Issues
    # column, so it needs to stand on its own.
    window_label = window_info.get("window", "")
    candle_count = window_info.get("candle_count", 0)
    if trigger == "baseline_relative":
        ratio = vol / baseline
        context = (f"≈{ratio:.1f}x the typical {VOLUME_SPIKE_LOOKBACK_MINUTES}min volume "
                   f"(≥{VOLUME_SPIKE_RATIO:g}x baseline over {bucket_count} windows, "
                   f"floor {cur}{threshold:,.0f})")
    elif baseline and bucket_count >= 2:
        ratio = vol / baseline
        context = (f"≈{ratio:.1f}x typical — fired on absolute floor "
                   f"{cur}{threshold:,.0f} (baseline warming, "
                   f"{bucket_count}/{VOLUME_SPIKE_MIN_BUCKETS} buckets)")
    else:
        context = (f"fired on absolute floor {cur}{threshold:,.0f} "
                   f"— baseline still building, no per-pair context yet")

    label = (f"Volume spike: {window_label} ({candle_count} candles) — "
             f"{cur}{vol:,.2f} ({context})")
    return [("D1", "HIGH", label)]


# ══════════════════════════════════════════════════════════════════════════════
# F1 — CROSS-PAIR ARBITRAGE (triangulates using pairs already being fetched)
# ══════════════════════════════════════════════════════════════════════════════

def find_arb_triangles(pairs: list[tuple[str, Optional[float]]]) -> list[dict]:
    """
    Finds linked-pair groups derivable purely from symbols already in PAIRS:
      - base_bridge:  XNGN  vs  XUSDT * USDTNGN     (covers btcngn, ethngn, etc.)
      - quote_bridge: CNGNNGN vs USDTNGN / USDTCNGN  (the one CNGN special case)
    Runs automatically as pairs are added/removed via the dashboard — no hardcoding
    beyond the bridge currency names.
    """
    symbols = {sym for sym, _ in pairs}
    triangles = []

    if "usdtngn" in symbols:
        for sym in symbols:
            base, quote = split_symbol(sym)
            if quote == "ngn" and base not in ("usdt", "cngn"):
                usdt_leg = base + "usdt"
                if usdt_leg in symbols:
                    triangles.append({"direct": sym, "kind": "base_bridge",
                                       "legs": [usdt_leg, "usdtngn"]})

    if {"usdtcngn", "usdtngn", "cngnngn"} <= symbols:
        triangles.append({"direct": "cngnngn", "kind": "quote_bridge",
                           "legs": ["usdtngn", "usdtcngn"]})

    return triangles


def check_arb_gaps(triangles: list[dict], mids: dict[str, float], b1_fired: set[str]) -> list[dict]:
    """
    For each triangle, computes the implied cross price from its legs and compares
    to the directly-quoted price. Root-cause attribution: if one of the legs already
    fired B1 this cycle, that leg is named as the likely source; otherwise the direct
    pair itself is flagged (it's the one with no independent reference to confirm).
    """
    out = []
    for tri in triangles:
        direct_mid = mids.get(tri["direct"])
        leg_a, leg_b = tri["legs"]
        a, b = mids.get(leg_a), mids.get(leg_b)
        if direct_mid is None or not a or not b:
            continue
        implied = a * b if tri["kind"] == "base_bridge" else a / b
        if not implied:
            continue
        gap_pct = (direct_mid - implied) / implied * 100
        if abs(gap_pct) >= ARB_GAP_PCT:
            suspect = leg_a if leg_a in b1_fired else (leg_b if leg_b in b1_fired else tri["direct"])
            severity = "HIGH" if abs(gap_pct) >= ARB_GAP_PCT * 2 else "MEDIUM"
            out.append({
                "pair": tri["direct"], "gap_pct": gap_pct, "implied": implied,
                "actual": direct_mid, "legs": tri["legs"], "suspect": suspect,
                "severity": severity,
            })
    return out


# ══════════════════════════════════════════════════════════════════════════════
# G-SERIES — DEPTH-WALK SLIPPAGE TRACKER (USDTNGN only)
# ══════════════════════════════════════════════════════════════════════════════
# Answers: "what price would a 100k-USDT market buy/sell actually clear at,
# vs. the displayed mid?" This is distinct from A4 (thin mid-market, a static
# depth check) and A6 (layer churn, a staleness check) — it's a direct
# execution-cost simulation. Runs on its own 5s task (DEPTH_WALK_POLL_INTERVAL_SECONDS),
# independent of the main 60s cycle, because meaningful book movement on a
# thin NGN pair can happen well inside a 60s window.
#
# G1 — Depth-Walk Partial Fill (MEDIUM, Tier 3 dashboard-only): fires when the
# visible book can't supply the full DEPTH_WALK_WEIGHT_USDT on one side. The
# datapoint is still recorded (using whatever depth was available) and flagged
# rather than dropped, so a thin patch doesn't leave a gap in the chart.

def walk_depth_weighted(df: pd.DataFrame, weight_usdt: float) -> tuple[Optional[float], bool]:
    """
    Cumulatively consumes `df` (already sorted best-price-first by
    build_orderbook_dfs) until `weight_usdt` worth of base-asset quantity
    (the "amount" column) has been walked. The boundary layer is clipped to
    only the remainder needed — e.g. cumulative 97k + a 114k layer only
    weighs 3k of that layer, not the full 114k.

    Returns (weighted_avg_price, partial_fill). partial_fill is True when the
    entire book was consumed and still didn't reach weight_usdt (G1 case);
    weighted_avg_price in that case is computed over whatever was available.
    Returns (None, True) for an empty book.
    """
    if df.empty or weight_usdt <= 0:
        return None, True

    remaining = weight_usdt
    notional  = 0.0   # sum(price * consumed_amount)
    consumed  = 0.0   # sum(consumed_amount)

    for row in df.itertuples():
        price, amount = float(row.price), float(row.amount)
        if amount <= 0:
            continue
        take = min(amount, remaining)
        notional += price * take
        consumed += take
        remaining -= take
        if remaining <= 0:
            break

    if consumed <= 0:
        return None, True
    return notional / consumed, remaining > 0


def depth_within_band(df: pd.DataFrame, target_price: float, side: str,
                      weight_usdt: float) -> tuple[float, bool]:
    """
    Sum resting size (the "amount" column — USDT-denominated base quantity for
    USDTNGN, same unit walk_depth_weighted consumes) priced within a FLAT band
    on one side, and report whether it reaches weight_usdt.

      side="ask": count asks priced <= target_price   (target = mid*(1+p/100))
      side="bid": count bids priced >= target_price   (target = mid*(1-p/100))

    df is best-price-first (asks ascending, bids descending), so we stop as
    soon as a level falls outside the band. Returns (in_band_usdt, ok) where ok
    is (in_band_usdt >= weight_usdt). A present-but-too-thin band yields
    (small_number, False) — i.e. it still counts as a poll, just a failing one.
    """
    if df.empty or weight_usdt <= 0:
        return 0.0, False
    in_band = 0.0
    for row in df.itertuples():
        price, amount = float(row.price), float(row.amount)
        if amount <= 0:
            continue
        if side == "ask":
            if price > target_price:
                break
        else:  # bid
            if price < target_price:
                break
        in_band += amount
    return in_band, in_band >= weight_usdt


def compute_depth_walk_metrics(asks_df: pd.DataFrame, bids_df: pd.DataFrame,
                                weight_usdt: float,
                                mid_weight_usdt: float = 1_000.0,
                                uptime_band_pct: float = UPTIME_FIXED_STEP_NGN / 1400.0 * 100.0,
                                uptime_weight_usdt: float = 100_000.0) -> Optional[dict]:
    """
    Returns the G1 metric set for one snapshot, or None if either side of the
    book is empty (nothing meaningful to walk — mirrors the A1/A3 guard used
    elsewhere for empty books).

    Mid price definition: NOT best_ask/best_bid top-of-book — instead it's the
    average of two `mid_weight_usdt` walks (default 1k USDT each side), so a
    lone dust order at the touch can't distort the reference. Fallback to
    top-of-book mid when either side can't supply even mid_weight_usdt; when
    that happens `mid_from_fallback: True` is set on the sample so the chart
    can flag it. The main slippage walk (weight_usdt, default 100k) still
    runs independently and its own partial_fill flags still drive G1.

    Liquidity uptime is a boolean check on that SAME 100k walk: a side scores
    1 if its walk-fill price doesn't slip more than UPTIME_FIXED_STEP_NGN (1₦)
    away from mid (ask: weighted_avg_buy <= mid+1₦; bid: weighted_avg_sell >=
    mid-1₦). uptime_band_pct / uptime_weight_usdt are accepted for call-site
    compatibility but are no longer read by this calculation — the resting-
    depth-in-a-percent-band check they used to configure was replaced.

    Also computes spread-gap compliance: a single boolean (no ask/bid split)
    on the RAW top-of-book spread — best_ask - best_bid <= SPREAD_GAP_FIXED_NGN
    (₦1) — via compute_mid_and_spread, the same function A2 uses. Unlike
    uptime this is never condensed into hourly history; it only ever backs a
    live "this hour" stat card.
    """
    if asks_df.empty or bids_df.empty:
        return None

    # ── Mid price via small depth walk ──────────────────────────────────────
    mid_ask, mid_ask_partial = walk_depth_weighted(asks_df, mid_weight_usdt)
    mid_bid, mid_bid_partial = walk_depth_weighted(bids_df, mid_weight_usdt)
    mid_from_fallback = mid_ask_partial or mid_bid_partial or mid_ask is None or mid_bid is None

    if mid_from_fallback:
        # Book too thin for a walk-based mid — fall back to top-of-book, which
        # is the historical behaviour. compute_mid_and_spread already handles
        # empty-df guards; we ruled those out above.
        mid, _, _ = compute_mid_and_spread(asks_df, bids_df)
    else:
        mid = (mid_ask + mid_bid) / 2

    if not mid:
        return None

    # ── Main slippage walk (independent of mid computation) ─────────────────
    weighted_avg_buy,  partial_buy  = walk_depth_weighted(asks_df, weight_usdt)
    weighted_avg_sell, partial_sell = walk_depth_weighted(bids_df, weight_usdt)

    buy_slip_pct  = ((weighted_avg_buy  / mid) - 1) * 100 if weighted_avg_buy  is not None else None
    sell_slip_pct = ((weighted_avg_sell / mid) - 1) * 100 if weighted_avg_sell is not None else None

    # ── Liquidity uptime — boolean walk-price check ──────────────────────────
    # Reuses the SAME 100k-USDT walk already computed above for slippage
    # (weighted_avg_buy / weighted_avg_sell) instead of separately counting
    # resting depth in a band. A side scores 1 if that walk's fill price
    # doesn't slip more than UPTIME_FIXED_STEP_NGN (1₦) away from mid:
    #   Ask uptime: weighted_avg_buy  <= mid + 1₦
    #   Bid uptime: weighted_avg_sell >= mid - 1₦
    # A missing walk price (empty side / weight_usdt<=0 upstream) fails
    # rather than raising, consistent with the old "present but too thin"
    # behaviour of the band check this replaces.
    #
    # NOTE: uptime_band_pct / uptime_weight_usdt (and the dashboard config
    # fields backing them — reference_price, uptime.weight_usdt) are no
    # longer used by this calculation. Left in the signature/config schema
    # inert rather than removed, per design decision.
    uptime_ask_target = mid + UPTIME_FIXED_STEP_NGN
    uptime_bid_target = mid - UPTIME_FIXED_STEP_NGN
    uptime_ask_ok = weighted_avg_buy  is not None and weighted_avg_buy  <= uptime_ask_target
    uptime_bid_ok = weighted_avg_sell is not None and weighted_avg_sell >= uptime_bid_target

    # ── Spread-gap compliance — raw top-of-book check ────────────────────────
    # Independent of the walk-based mid/uptime above: reuses compute_mid_and_spread,
    # the SAME function the main 60s cycle uses for A2's current_spread/spread_abs,
    # so this is exactly "the number A2 already uses" rather than a re-derived one.
    # Single boolean per poll (spread has no ask/bid side split the way uptime
    # does) — passes when best_ask - best_bid <= SPREAD_GAP_FIXED_NGN (₦1, fixed
    # constant by design decision, not dashboard-configurable — see defaults.py).
    _, spread_gap_ngn, _ = compute_mid_and_spread(asks_df, bids_df)
    spread_gap_ok = spread_gap_ngn <= SPREAD_GAP_FIXED_NGN

    return {
        "mid":               mid,
        "mid_from_fallback": mid_from_fallback,
        "weighted_avg_buy":  weighted_avg_buy,
        "weighted_avg_sell": weighted_avg_sell,
        "buy_slip_pct":      buy_slip_pct,
        "sell_slip_pct":     sell_slip_pct,
        "partial_fill_buy":  partial_buy,
        "partial_fill_sell": partial_sell,
        "g1":                partial_buy or partial_sell,
        # Liquidity uptime (per-side, this sample)
        "uptime_ask_target": uptime_ask_target,
        "uptime_bid_target": uptime_bid_target,
        "uptime_ask_ok":     uptime_ask_ok,
        "uptime_bid_ok":     uptime_bid_ok,
        # Spread-gap compliance (this sample) — live stat card only, not
        # condensed/persisted (see condense_bucket — deliberately untouched).
        "spread_gap_ngn":    spread_gap_ngn,
        "spread_gap_ok":     spread_gap_ok,
    }


def load_depth_walk_raw() -> dict:
    """{"bucket_start": iso_str | None, "samples": [ {...}, ... ]}"""
    if os.path.exists(DEPTH_WALK_RAW_FILE):
        try:
            with open(DEPTH_WALK_RAW_FILE) as f:
                return json.load(f)
        except Exception as exc:
            print(f"⚠️  Could not read {DEPTH_WALK_RAW_FILE}: {exc} — starting fresh bucket")
    return {"bucket_start": None, "samples": []}


def save_depth_walk_raw(raw: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DEPTH_WALK_RAW_FILE, "w") as f:
        json.dump(raw, f, indent=2)


def load_depth_walk_condensed() -> list:
    if os.path.exists(DEPTH_WALK_CONDENSED_FILE):
        try:
            with open(DEPTH_WALK_CONDENSED_FILE) as f:
                return json.load(f)
        except Exception as exc:
            print(f"⚠️  Could not read {DEPTH_WALK_CONDENSED_FILE}: {exc} — starting fresh")
    return []


def save_depth_walk_condensed(condensed: list):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DEPTH_WALK_CONDENSED_FILE, "w") as f:
        json.dump(condensed, f, indent=2)


def _mean(vals: list) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def condense_bucket(bucket_start: str, samples: list) -> Optional[dict]:
    """Collapses one hour's worth of raw 5s samples into a single averaged point."""
    if not samples:
        return None
    # Uptime = fraction of usable samples this hour that met the in-band
    # threshold, per side. Denominator is dynamic: samples carrying the flag
    # (every sample produced since the uptime feature shipped). Older samples
    # from before the feature lack the key and are excluded so we don't dilute
    # a fresh bucket with pre-feature blanks.
    ask_flags = [1.0 if s.get("uptime_ask_ok") else 0.0
                 for s in samples if s.get("uptime_ask_ok") is not None]
    bid_flags = [1.0 if s.get("uptime_bid_ok") else 0.0
                 for s in samples if s.get("uptime_bid_ok") is not None]
    return {
        "ts":                bucket_start,
        "buy_slip_pct":      _mean([s.get("buy_slip_pct")  for s in samples]),
        "sell_slip_pct":     _mean([s.get("sell_slip_pct") for s in samples]),
        "mid":               _mean([s.get("mid")           for s in samples]),
        "partial_fill_buy":  any(s.get("partial_fill_buy")  for s in samples),
        "partial_fill_sell": any(s.get("partial_fill_sell") for s in samples),
        "mid_from_fallback": any(s.get("mid_from_fallback") for s in samples),
        "g1":                any(s.get("g1")                for s in samples),
        "sample_count":      len(samples),
        # Per-side liquidity uptime, 0..1 decimals (None if no flagged samples)
        "uptime_ask":        (sum(ask_flags) / len(ask_flags)) if ask_flags else None,
        "uptime_bid":        (sum(bid_flags) / len(bid_flags)) if bid_flags else None,
        "uptime_ask_samples": len(ask_flags),
        "uptime_bid_samples": len(bid_flags),
    }


def prune_condensed(condensed: list, retention_days: float) -> list:
    if not condensed or retention_days <= 0:
        return condensed
    cutoff = ngt_now() - timedelta(days=retention_days)
    out = []
    for pt in condensed:
        try:
            ts = datetime.fromisoformat(pt["ts"])
        except (KeyError, ValueError):
            continue
        if ts >= cutoff:
            out.append(pt)
    return out


async def depth_walk_loop(session: aiohttp.ClientSession):
    """
    Standalone 5s task — independent of the main 60s A/B/D cycle. Fetches
    USDTNGN depth, computes the G1 slippage metrics, appends to the
    in-progress hourly raw bucket, and condenses+resets that bucket once it
    has been open for DEPTH_WALK_RAW_RETENTION_SECONDS. Any single-cycle
    failure (fetch error, empty book) is logged and skipped — it does not
    kill the loop or the main monitor.
    """
    raw = load_depth_walk_raw()
    if raw.get("bucket_start") is None:
        raw["bucket_start"] = ngt_now().isoformat()

    while True:
        try:
            payload = await fetch_depth(session, DEPTH_WALK_SYMBOL)
            asks_df, bids_df = build_orderbook_dfs(payload)
            # Uptime is now a boolean walk-price check (100k walk vs mid ± 1₦)
            # inside the metric fn — uptime_band_pct/uptime_weight_usdt below
            # are passed through but no longer used by that calculation.
            metrics = compute_depth_walk_metrics(
                asks_df, bids_df, DEPTH_WALK_WEIGHT_USDT,
                mid_weight_usdt=DEPTH_WALK_MID_WEIGHT_USDT,
                uptime_band_pct=UPTIME_BAND_PCT,
                uptime_weight_usdt=UPTIME_WEIGHT_USDT,
            )
            if metrics is not None:
                metrics["ts"] = ngt_now().isoformat()
                raw["samples"].append(metrics)
                if metrics["g1"]:
                    print(f"  [G1] {DEPTH_WALK_SYMBOL} depth-walk partial fill "
                          f"(buy={metrics['partial_fill_buy']}, sell={metrics['partial_fill_sell']})")
            else:
                print(f"⚠️  Depth-walk: {DEPTH_WALK_SYMBOL} book empty on one side — skipping sample")
        except Exception as e:
            print(f"⚠️  Depth-walk fetch/compute error: {e}")

        # Condense + reset once the current bucket has been open long enough
        bucket_start_dt = datetime.fromisoformat(raw["bucket_start"])
        age_seconds = (ngt_now() - bucket_start_dt).total_seconds()
        if age_seconds >= DEPTH_WALK_RAW_RETENTION_SECONDS:
            condensed_point = condense_bucket(raw["bucket_start"], raw["samples"])
            if condensed_point is not None:
                condensed = load_depth_walk_condensed()
                condensed.append(condensed_point)
                condensed = prune_condensed(condensed, DEPTH_WALK_CONDENSED_RETENTION_DAYS)
                save_depth_walk_condensed(condensed)
            raw = {"bucket_start": ngt_now().isoformat(), "samples": []}

        save_depth_walk_raw(raw)
        await asyncio.sleep(DEPTH_WALK_POLL_INTERVAL_SECONDS)


# ══════════════════════════════════════════════════════════════════════════════
# PERSISTENCE
# ══════════════════════════════════════════════════════════════════════════════

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
    Long-format daily log: one ROW per WARNING market per cycle. Only markets whose
    status is "Warning" this cycle are appended — healthy ("Checked") pairs and
    failed-fetch pairs are skipped, so the file stays small and every row is something
    worth reviewing. Rows keep PAIRS config order within a cycle.
    Columns: Timestamp, Market, Status, Issues, Depth.

    D1 spikes appear here automatically as of the D1 consolidation — they emit a
    standard ("D1", "HIGH", label) issue tuple like every other check, which flips
    status to "Warning" and populates the Issues column with "D1:HIGH" alongside
    any A/B/F ids that also fired. The earlier SPIKE-specific status/branch has
    been removed; there's nothing special to do here for D1 anymore.

    A cycle with no warnings appends nothing (and writes no header until the first
    warning of the day creates the file).
    """
    now   = ngt_now()
    today = now.strftime("%Y-%m-%d")
    path  = os.path.join(DATA_DIR, f"daily_log_{today}.csv")
    ts    = now.strftime("%H:%M:%S")

    pair_order = {sym: i for i, (sym, _) in enumerate(PAIRS)}
    warning_results = [r for r in all_results
                       if str(r.get("status", "")).lower() == "warning"]
    warning_results.sort(key=lambda r: pair_order.get(r["symbol"], 1_000_000))

    if not warning_results:
        print("✅ Daily log: no warnings this cycle — nothing appended")
        return

    rows = [{
        "Timestamp": ts, "Market": r["symbol"], "Status": r["status"].upper(),
        "Issues": r.get("issues", ""),
        "Depth": f"{r.get('depth_1.25x', '')} / {r.get('depth_1.5x', '')}",
    } for r in warning_results]

    new_df      = pd.DataFrame(rows)
    file_exists = os.path.exists(path)
    new_df.to_csv(path, mode="a", header=not file_exists, index=False)
    print(f"✅ Daily log appended: {path} (+{len(rows)} warning row(s))")


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

_telegram_lock = asyncio.Lock()
TELEGRAM_MAX_CHARS = 4000  # stay under Telegram's 4096 hard limit


def _chunk_telegram_message(msg: str, max_chars: int = TELEGRAM_MAX_CHARS) -> list:
    if len(msg) <= max_chars:
        return [msg]
    lines = msg.split("\n")
    chunks, current, current_len = [], [], 0
    for line in lines:
        extra = len(line) + (1 if current else 0)
        if current and current_len + extra > max_chars:
            chunks.append("\n".join(current))
            current, current_len = [line], len(line)
        else:
            current.append(line)
            current_len += extra
    if current:
        chunks.append("\n".join(current))
    return chunks


async def send_telegram(msg: str, session: aiohttp.ClientSession) -> bool:
    """
    Send to every configured chat. Returns True if the message reached AT LEAST ONE
    chat (every chunk delivered 2xx to that chat); returns False only if NO chat
    received it at all.

    Why "at least one" and not "all": this return value gates cooldown commits in
    run_cycle. An earlier version required every chunk to every chat to be 2xx —
    which meant a single misconfigured or rate-limited chat_id made this return False
    on every cycle, the cooldown never committed, and the alert re-fired every cycle
    (back-to-back spam) even though the good chats DID receive it. "Delivered to
    someone" is the right signal for committing the cooldown; an alert that reached
    nobody (every chat failed / Telegram unreachable) still returns False and retries
    next cycle rather than going dark.

    Two original bugs this still guards against:
      1. `await session.post(...)` never inspected resp.status, so a 400/429 was
         silently swallowed and looked like a successful send.
      2. Not using `async with` left the response unclosed; the context manager here
         also lets us read resp.status / body for diagnostics.

    Per-chat / per-chunk failures are always logged so a bad chat_id or 429 stays
    visible. Unconfigured Telegram returns True (nothing to deliver — no retry needed).
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        return True
    delivered_any = False
    failed_any    = False
    async with _telegram_lock:
        for chat_id in TELEGRAM_CHAT_IDS:
            chat_id = str(chat_id).strip()
            if not chat_id:
                continue
            chat_ok = True
            for chunk in _chunk_telegram_message(msg):
                try:
                    async with session.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                        json={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status >= 400:
                            body = await resp.text()
                            print(f"⚠️  Telegram {resp.status} for chat {chat_id}: {body[:200]}")
                            chat_ok = False
                except Exception as e:
                    print(f"⚠️  Telegram send failed for chat {chat_id}: {e}")
                    chat_ok = False
            if chat_ok:
                delivered_any = True
            else:
                failed_any = True
    if delivered_any and failed_any:
        print("⚠️  Telegram: delivered to some chats but not all — committing cooldown "
              "anyway (≥1 recipient got it). Fix the failing chat_id flagged above.")
    return delivered_any


# ══════════════════════════════════════════════════════════════════════════════
# PER-PAIR WORKER
# ══════════════════════════════════════════════════════════════════════════════

async def process_pair(
    symbol: str,
    target: Optional[float],
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    trusted_price: Optional[float],
    ref_issues: list,
    vol_hist_root: dict,
    layer_hist_root: dict,
) -> Optional[dict]:
    """
    Fetches depth + kline (B4) + kline-volume (D1, its own independent call — see
    fetch_kline_volume) for one pair and runs every A/B/D check applicable to it.
    No timers, no cooldowns: every issue found this cycle is returned as actionable.
    `trusted_price`/`ref_issues` are pre-resolved once per asset in run_cycle (see
    resolve_trusted_price) and only populated for USDT-quoted pairs. `vol_hist_root`
    is the persisted per-pair volume-bucket history D1 uses for its own baseline.
    `layer_hist_root` is the persisted per-pair near-touch-level history A6 uses for
    its own churn self-baseline.

    The collected issue list is run through dedupe_actionable before it becomes
    `_actionable` (and before the dashboard `issues` string is built), so any id
    that two checks emit in the same cycle — A2 (spread + shallow) and B3 (MEXC +
    KuCoin) — is folded to a single tuple with the highest severity and merged
    labels. Everything downstream therefore sees each id exactly once.
    """
    async with semaphore:
        monitor_only = target is None
        try:
            depth_raw, kline_raw, kline_vol_raw = await asyncio.gather(
                fetch_depth(session, symbol),
                fetch_kline(session, symbol),
                fetch_kline_volume(session, symbol),
            )
            asks_df, bids_df = build_orderbook_dfs(depth_raw)

            # ── A3 — One-sided market ───────────────────────────────────────
            # NOTE: v1 of this monitor silently `return None`-ed here, which meant
            # a genuinely one-sided book (the most severe market-structure failure
            # in the spec) never actually fired an alert — it just vanished from
            # the cycle and looked like a fetch failure. Fixed: report it as A3.
            # A6 snapshot is intentionally NOT written here — a one-sided book
            # produces no meaningful near-touch levels to diff against next cycle,
            # and writing empty/partial snapshots would corrupt the churn baseline.
            if asks_df.empty or bids_df.empty:
                side = "ask" if asks_df.empty else "bid"
                print(f"[{symbol}] 🚨 A3 — one-sided market, no {side} orders")
                return {
                    "timestamp": ngt_now().strftime("%Y-%m-%d %H:%M:%S"),
                    "symbol": symbol, "monitor_only": monitor_only,
                    "status": "Warning", "issues": "A3:CRITICAL", "should_alert": True,
                    "alert_tier": 1,
                    "current_spread": "N/A", "spread_abs": "N/A",
                    "target_spread": target if not monitor_only else "N/A",
                    "percent_diff": "N/A", "mid_price": "N/A",
                    "ask_layers": len(asks_df), "bid_layers": len(bids_df),
                    "dws": "N/A", "dws_poor": False,
                    "depth_1.25x": "$0", "depth_1.5x": "$0",
                    "imbalance_ratio": "inf", "heavier_side": "bids" if asks_df.empty else "asks",
                    "trusted_ref": round(trusted_price, 8) if trusted_price else "N/A",
                    "layer_churn_pct": "N/A", "layer_churn_baseline_pct": "N/A",
                    "telegram_fired": False, "telegram_detail": "",
                    "_actionable": [("A3", "CRITICAL", f"One-sided market — no {side} orders")],
                }

            ask_layers, bid_layers = len(asks_df), len(bids_df)
            mid_price, spread_abs, curr_spread = compute_mid_and_spread(asks_df, bids_df)
            dws      = calculate_dws(asks_df, bids_df, mid_price)
            depth_25 = calculate_liquidity_depth(asks_df, bids_df, mid_price, curr_spread * 1.25)
            depth_50 = calculate_liquidity_depth(asks_df, bids_df, mid_price, curr_spread * 1.50)
            imbalance_ratio, heavier_side = calculate_depth_imbalance(asks_df, bids_df, mid_price, curr_spread * 1.25)

            issues = []

            # ── A1 — Crossed orderbook ──────────────────────────────────────
            best_ask, best_bid = asks_df["price"].iloc[0], bids_df["price"].iloc[0]
            if best_bid >= best_ask:
                issues.append(("A1", "CRITICAL",
                    f"Crossed orderbook — best bid {best_bid:,.6g} ≥ best ask {best_ask:,.6g}"))

            # ── A2 — Spread widening (vs target, DWS-confirmed) + shallow book ──
            # Both sub-checks emit id "A2"; dedupe_actionable folds them into one
            # tuple (merged label) below so the Tier-2 counter only advances once.
            #
            # DWS confirmation gate (applies to the spread-widening sub-check ONLY):
            # a raw spread that looks anomalous vs target — too wide (diff > +100%)
            # OR too tight (diff < -75%) — is only a real A2 if the DEPTH-WEIGHTED
            # spread is ALSO poor (dws > DWS_POOR_THRESHOLD). When DWS is within
            # tolerance the book is genuinely healthy despite the raw spread number,
            # so no A2 fires and the market reads as fully healthy — this is exactly
            # the false positive DWS was added to suppress. The spread diff % is still
            # computed and returned (percent_diff) for the dashboard regardless; it
            # just isn't flagged as A2 unless DWS confirms it. The shallow-orderbook
            # sub-check below is independent of DWS and unaffected by this gate.
            diff = None
            dws_poor = dws > DWS_POOR_THRESHOLD
            if not monitor_only and target:
                diff = ((curr_spread - target) / target) * 100
                abs_diff_pp = abs(curr_spread - target)
                spread_anomaly = (
                    (diff > 100 or diff < -75)
                    and abs_diff_pp >= MIN_ABS_SPREAD_DIFF_PCT
                    and dws_poor
                )
                if spread_anomaly:
                    issues.append(("A2", "HIGH",
                        f"Spread {curr_spread:.4f}% vs target {target}% (diff {diff:+.1f}%) "
                        f"| DWS {dws:.4f} > {DWS_POOR_THRESHOLD} (depth-weighted spread confirms)"))

            if ask_layers < MIN_ORDERBOOK_LAYERS or bid_layers < MIN_ORDERBOOK_LAYERS:
                issues.append(("A2", "HIGH",
                    f"Shallow orderbook — asks:{ask_layers} bids:{bid_layers} (min {MIN_ORDERBOOK_LAYERS})"))

            # ── A4 — Thin mid-market ────────────────────────────────────────
            if 0 < depth_25 < THIN_DEPTH_THRESHOLD:
                issues.append(("A4", "MEDIUM",
                    f"Thin mid-market — depth within spread: {format_depth(depth_25)} "
                    f"(min {format_depth(THIN_DEPTH_THRESHOLD)})"))

            # ── A5 — Depth imbalance ────────────────────────────────────────
            issues += check_depth_imbalance(imbalance_ratio, heavier_side)

            # ── A6 — Layer churn stall (near-touch levels not refreshing) ───
            # Self-baseline per market — see check_layer_churn_stall for why a
            # global "% unchanged" threshold doesn't work across busy vs. quiet pairs.
            curr_top_asks = extract_top_levels(asks_df, LAYER_CHURN_TOP_PCT)
            curr_top_bids = extract_top_levels(bids_df, LAYER_CHURN_TOP_PCT)
            layer_hist    = layer_hist_root.setdefault(symbol, {})
            prev_top_asks = layer_hist.get("last_top_asks")
            prev_top_bids = layer_hist.get("last_top_bids")
            churn_score   = compute_layer_churn(prev_top_asks, prev_top_bids, curr_top_asks, curr_top_bids)
            churn_baseline, churn_bucket_count = (None, 0)
            if churn_score is not None:
                churn_baseline, churn_bucket_count = update_layer_churn_baseline(
                    symbol, churn_score, layer_hist_root)
            issues += check_layer_churn_stall(churn_score, churn_baseline, churn_bucket_count, monitor_only)
            # Snapshot written only when both sides are present (A3 early-return skips this)
            layer_hist["last_top_asks"] = curr_top_asks
            layer_hist["last_top_bids"] = curr_top_bids

            # ── B1 — Price discrepancy (USDT-quoted pairs only) ─────────────
            _, quote = split_symbol(symbol)
            if quote == "usdt":
                issues += check_price_discrepancy(mid_price, trusted_price, target)

            # ── B2 / B3 — carried in from the per-asset reference pass ──────
            # ref_issues may carry two ("B3", ...) tuples (MEXC + KuCoin); the
            # dedupe below folds them so B3 counts once per cycle.
            issues += ref_issues

            # ── B4 — Circuit breaker proximity (reference-free) ─────────────
            issues += check_circuit_breaker_proximity(kline_raw, mid_price)

            # ── G2 — Candle wick / anomalous print (same kline_raw as B4) ───
            issues += check_candle_wicks(kline_raw)

            # ── Fold duplicate ids (A2 x2, B3 x2) BEFORE any tier/firing logic ──
            issues = dedupe_actionable(issues)

            # ── D1 — Volume spike, with Quidax's own longer-term baseline as context ──
            # kline_vol_raw is D1's own independent fetch (fetch_kline_volume), not
            # the B4 kline_raw above — this is the point of the D1/B4 decoupling.
            # As of the D1 consolidation, get_recent_spikes returns issue tuples
            # in the standard ("D1", "HIGH", label) shape, so D1 rides the exact
            # same pipeline as every other check from here on: dedupe, classify_tier,
            # should_fire_telegram, the anomaly Telegram loop, and update_daily_log
            # all treat it identically. No more parallel _spikes list, no more
            # bespoke tier calc, no more separate spike Telegram message.
            window_info = compute_window_volume(kline_vol_raw, symbol)
            baseline, bucket_count = (None, 0)
            if window_info:
                baseline, bucket_count = update_volume_baseline(symbol, window_info["quote_volume"], vol_hist_root)
            issues += get_recent_spikes(window_info, symbol, baseline, bucket_count)

            # Fold once more after D1 in case a dedupe is ever needed (D1 currently
            # only emits 0 or 1 tuple, but this keeps the invariant "issues is deduped
            # before status/tier/firing" holding regardless of future edits).
            issues = dedupe_actionable(issues)

            is_poor = bool(issues)
            has_d1  = any(i[0] == "D1" for i in issues)  # dashboard flag; d1_spike below
            tier    = worst_tier(issues)

            # ── D1 dashboard detail fields (informational, not divergent behaviour) ──
            # These populate the dashboard's dedicated D1 detail row (volume vs. floor,
            # ref context). They're NOT alert plumbing — they're per-check data fields
            # analogous to depth_1.25x or current_spread, kept alongside the rest of
            # the row so the dashboard doesn't have to re-derive them from `issues`.
            d1_threshold = get_threshold(symbol)
            if has_d1:
                # Same ref_context text that ends up in the issues label, extracted
                # for the standalone dashboard field. Cheap to rebuild here rather
                # than parse it back out of the label string.
                if VOLUME_SPIKE_MODE == "baseline_relative" and baseline and bucket_count >= VOLUME_SPIKE_MIN_BUCKETS:
                    ratio = window_info["quote_volume"] / baseline
                    d1_context = (f"≈{ratio:.1f}x the typical {VOLUME_SPIKE_LOOKBACK_MINUTES}min volume "
                                  f"(≥{VOLUME_SPIKE_RATIO:g}x baseline over {bucket_count} windows, "
                                  f"floor {get_currency_symbol(symbol)}{d1_threshold:,.0f})")
                elif baseline and bucket_count >= 2:
                    ratio = window_info["quote_volume"] / baseline
                    d1_context = (f"≈{ratio:.1f}x typical — fired on absolute floor "
                                  f"{get_currency_symbol(symbol)}{d1_threshold:,.0f} (baseline warming, "
                                  f"{bucket_count}/{VOLUME_SPIKE_MIN_BUCKETS} buckets)")
                else:
                    d1_context = (f"fired on absolute floor {get_currency_symbol(symbol)}{d1_threshold:,.0f} "
                                  f"— baseline still building, no per-pair context yet")
            elif baseline and bucket_count >= 2 and window_info:
                ratio_txt = f"≈{window_info['quote_volume'] / baseline:.1f}x typical"
                if VOLUME_SPIKE_MODE == "baseline_relative" and bucket_count < VOLUME_SPIKE_MIN_BUCKETS:
                    d1_context = (f"{ratio_txt} (baseline warming "
                                  f"{bucket_count}/{VOLUME_SPIKE_MIN_BUCKETS} — absolute floor active)")
                else:
                    d1_context = f"{ratio_txt} ({bucket_count}-window baseline)"
            else:
                d1_context = "baseline building…"

            print(f"[{symbol}] {'⚠️ ' if is_poor else '✅'} spread={curr_spread:.4f}% mid={mid_price:,.4f} "
                  f"dws={dws:.4f} layers={ask_layers}/{bid_layers} issues={len(issues)}")

            return {
                "timestamp":       ngt_now().strftime("%Y-%m-%d %H:%M:%S"),
                "symbol":          symbol,
                "monitor_only":    monitor_only,
                "status":          "Warning" if is_poor else "Checked",
                "issues":          "|".join(f"{i[0]}:{i[1]}" for i in issues) if issues else "",
                "should_alert":    is_poor,
                "alert_tier":      tier,
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
                "trusted_ref":     round(trusted_price, 8) if trusted_price else "N/A",
                "layer_churn_pct":          round(churn_score * 100, 1) if churn_score is not None else "N/A",
                "layer_churn_baseline_pct": round(churn_baseline * 100, 1) if churn_baseline is not None else "N/A",
                "telegram_fired":  False,   # set in run_cycle's firing loop once the
                "telegram_detail": "",      # tier/cooldown gate has been evaluated
                "d1_spike":         has_d1,
                "d1_window_volume": round(window_info["quote_volume"], 2) if window_info else "N/A",
                "d1_threshold":     d1_threshold if d1_threshold is not None else "N/A",
                "d1_currency":      get_currency_symbol(symbol),
                "d1_context":       d1_context,
                "_actionable":     issues,
            }

        except Exception as e:
            print(f"[{symbol}] ✗ fetch/process error: {e}")
            return None


# ══════════════════════════════════════════════════════════════════════════════
# SINGLE CYCLE
# ══════════════════════════════════════════════════════════════════════════════

FAILED_PAIR_RATIO_FOR_OUTAGE_ALERT = 0.5   # ≥50% of pairs failing looks like an
                                            # outage, not isolated per-pair errors

# ── Alert tier classification ─────────────────────────────────────────────────
# Maps issue_id → tier.  B4 has two tiers depending on severity (handled in
# classify_tier() below).  E1/E2 are handled separately in run_cycle.
_TIER1_IDS = {"A1", "A3", "A6", "D1"}    # fire immediately on first occurrence
_TIER2_IDS = {"A2", "B1", "B2", "B3"}    # require consecutive cycles
_TIER3_IDS = {"A4", "A5", "F1"}          # dashboard flag only — never Telegram

ALERT_COOLDOWN_MINUTES  = 15
TIER2_CONFIRM_CYCLES    = 3

# Severity ordering used when folding duplicate-id issues (highest wins).
_SEVERITY_RANK = {"CRITICAL": 3, "HIGH": 2, "MEDIUM": 1}


def dedupe_actionable(issues: list) -> list:
    """
    Collapse tuples that share an issue_id into one, keeping the highest severity
    and merging the distinct labels. Two checks legitimately emit the same id in a
    single cycle — A2 (spread widening + shallow book) and B3 (MEXC + KuCoin both
    stale) — and should_fire_telegram must see each id exactly once per cycle, or
    its Tier-2 counter double-increments (confirms in 2 cycles instead of 3) and
    the cooldown set by the first instance suppresses the second from Telegram.
    First-appearance order is preserved so the leading id stays first in the message.
    """
    merged: dict[str, list] = {}
    order:  list[str] = []
    for issue_id, severity, label in issues:
        if issue_id not in merged:
            merged[issue_id] = [severity, [label]]
            order.append(issue_id)
        else:
            cur = merged[issue_id]
            if _SEVERITY_RANK.get(severity, 0) > _SEVERITY_RANK.get(cur[0], 0):
                cur[0] = severity
            if label not in cur[1]:
                cur[1].append(label)
    return [(iid, merged[iid][0], " | ".join(merged[iid][1])) for iid in order]


def classify_tier(issue_id: str, severity: str) -> int:
    """Return 1, 2, or 3 for a given (issue_id, severity) pair."""
    if issue_id in _TIER3_IDS:
        return 3
    if issue_id == "B4":
        return 1 if severity == "CRITICAL" else 2
    if issue_id == "G2":
        return 1 if severity == "CRITICAL" else 2
    if severity == "MEDIUM" and issue_id in ("A6", "B3"):
        # A6: monitor-only zero-baseline case (see check_layer_churn_stall).
        # B3: an UNCHANGED source whose peer is flat or absent (quiet market or
        # single-source asset) — see resolve_trusted_price. Both emit MEDIUM
        # precisely to land here: dashboard visibility, no Telegram noise.
        return 3
    if issue_id in _TIER1_IDS:
        return 1
    if issue_id in _TIER2_IDS:
        return 2
    # Unknown ids default to Tier 2 (conservative)
    return 2


def worst_tier(issues: list) -> int:
    """
    Given a list of (issue_id, severity, label) tuples return the lowest
    tier number present (1 beats 2 beats 3). Returns 0 when no issues.
    """
    if not issues:
        return 0
    return min(classify_tier(iid, sev) for iid, sev, _ in issues)


def load_suspensions() -> dict:
    """
    Read suspensions.json → {symbol: ISO expiry (NGT)}. Missing/corrupt file yields
    an empty map (fail-open: a bad file must never mute or crash the alert path).
    Called once per cycle in run_cycle; the API writes this file when a suspend is
    set or cleared. Keys are lowercase symbols to match PAIRS.
    """
    if not os.path.exists(SUSPENSIONS_FILE):
        return {}
    try:
        with open(SUSPENSIONS_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"⚠️  Could not read {SUSPENSIONS_FILE}: {exc} — treating as no suspensions")
        return {}


def is_suspended(suspensions: dict, symbol: str) -> bool:
    """
    True if `symbol` has an active (non-expired) Telegram suspension right now.
    An expiry in the past (or an unparseable one) counts as not suspended, so a
    lapsed window self-heals even if the API never gets around to pruning it.
    """
    expiry_str = suspensions.get(symbol.lower())
    if not expiry_str:
        return False
    try:
        return ngt_now() < datetime.fromisoformat(expiry_str)
    except (ValueError, TypeError):
        return False


def _alert_state(shared_state: dict, symbol: str) -> dict:
    """Return (and lazily create) the _alert sub-dict for a pair."""
    pair = shared_state.setdefault(symbol, {})
    return pair.setdefault("_alert", {})


def is_in_cooldown(shared_state: dict, symbol: str, issue_id: str) -> bool:
    """True if a cooldown is active for this (symbol, issue_id) right now."""
    expiry_str = _alert_state(shared_state, symbol).get(f"cd_{issue_id}")
    if not expiry_str:
        return False
    try:
        expiry = datetime.fromisoformat(expiry_str)
        return ngt_now() < expiry
    except (ValueError, TypeError):
        return False


def start_cooldown(shared_state: dict, symbol: str, issue_id: str,
                   minutes: int = ALERT_COOLDOWN_MINUTES):
    """Record a cooldown expiry timestamp for (symbol, issue_id)."""
    expiry = ngt_now() + timedelta(minutes=minutes)
    _alert_state(shared_state, symbol)[f"cd_{issue_id}"] = expiry.isoformat()


def get_consecutive(shared_state: dict, symbol: str, issue_id: str) -> int:
    return _alert_state(shared_state, symbol).get(f"consec_{issue_id}", 0)


def increment_consecutive(shared_state: dict, symbol: str, issue_id: str) -> int:
    state = _alert_state(shared_state, symbol)
    key   = f"consec_{issue_id}"
    state[key] = state.get(key, 0) + 1
    return state[key]


def reset_consecutive(shared_state: dict, symbol: str, issue_id: str):
    _alert_state(shared_state, symbol).pop(f"consec_{issue_id}", None)


def should_fire_telegram(shared_state: dict, symbol: str,
                          issue_id: str, severity: str) -> bool:
    """
    Decide whether (symbol, issue_id) should fire Telegram this cycle.

    Side effect: increments the Tier-2 consecutive counter when the issue is
    confirming. It deliberately NO LONGER starts the cooldown or resets the
    counter — those are committed by the caller ONLY after send_telegram confirms
    delivery (see run_cycle). That way a dropped send (400/429/transport error)
    does not burn the cooldown, and the alert retries on the next cycle instead of
    going silent for the full window.

    Must be called exactly once per (symbol, issue_id) per cycle — the caller folds
    duplicate ids via dedupe_actionable first, since A2 and B3 can each emit two
    tuples in a single cycle.
    """
    tier = classify_tier(issue_id, severity)

    if tier == 3:
        return False

    if is_in_cooldown(shared_state, symbol, issue_id):
        # Still flagged on the dashboard by the caller; just don't Telegram.
        # Do NOT increment the consecutive counter while in cooldown — the issue
        # may have already resolved and re-triggered within the window, and
        # counting those cycles would make it fire again the instant the cooldown
        # expires.
        return False

    if tier == 1:
        # Tier 1 fires immediately; cooldown is committed by the caller on a
        # confirmed send.
        return True

    # Tier 2 — needs N consecutive confirmed hits before firing.
    confirm_needed = TIER2_CONFIRM_CYCLES
    count = increment_consecutive(shared_state, symbol, issue_id)
    return count >= confirm_needed


async def run_cycle(shared_state: dict, session: aiohttp.ClientSession, cycle_num: int):
    apply_config()   # pick up any dashboard edits without restarting
    suspensions = load_suspensions()   # per-pair Telegram mutes, set from the dashboard
    cycle_start = ngt_now()
    semaphore   = asyncio.Semaphore(MAX_CONCURRENT_PAIRS)

    # ── Step 1: reference exchange data, once per cycle (E2 gate) ──────────────
    mexc_map, kucoin_map, e2_issues = await fetch_reference_data(session)

    # ── Step 2: resolve a trusted price per asset (B2/B3), USDT-quoted pairs only
    ref_hist_root = shared_state.setdefault("_ref_hist", {})
    vol_hist_root = shared_state.setdefault("_vol_hist", {})
    layer_hist_root = shared_state.setdefault("_layer_hist", {})
    assets = {split_symbol(sym)[0].upper() for sym, _ in PAIRS if split_symbol(sym)[1] == "usdt"}

    # Per-asset reference-exchange aliases, pulled from each usdt pair's optional
    # 3rd config element (PAIR_ALIASES, dashboard-configurable). {"mexc": "RENDER"}
    # means "look this asset up as RENDER on MEXC" instead of its Quidax ticker —
    # needed when the two exchanges disagree on what to call a renamed asset
    # (e.g. MEXC lists RENDER, KuCoin still lists RNDR for the same token).
    asset_aliases: dict[str, dict] = {}
    # Per-asset effective B2 threshold: per-symbol override or the global default.
    # Built here (not inside resolve_trusted_price) so that function stays free of
    # symbol↔asset mapping — B-series is USDT-only, so asset↔usdt-symbol is 1:1.
    asset_divergence_pct: dict[str, float] = {}
    for sym, _ in PAIRS:
        base, quote = split_symbol(sym)
        if quote == "usdt":
            asset_aliases[base.upper()] = PAIR_ALIASES.get(sym, {})
            asset_divergence_pct[base.upper()] = SOURCE_DIVERGENCE_OVERRIDES.get(
                sym, SOURCE_DIVERGENCE_PCT)

    trusted_prices: dict[str, Optional[float]] = {}
    ref_issues_by_asset: dict[str, list] = {}

    for asset in assets:
        aliases = asset_aliases.get(asset, {})
        mexc_key = (aliases.get("mexc") or asset).upper()
        kucoin_key = (aliases.get("kucoin") or asset).upper()
        mx = mexc_map.get(mexc_key)
        k = kucoin_map.get(kucoin_key)
        m_price, m_ok = (mx["price"], True) if mx else (None, False)
        k_price, k_ok = (k["price"], True) if k else (None, False)

        asset_hist = ref_hist_root.setdefault(asset, {})
        trusted, issues = resolve_trusted_price(
            asset, m_price, m_ok, k_price, k_ok, asset_hist,
            divergence_threshold=asset_divergence_pct.get(asset, SOURCE_DIVERGENCE_PCT))
        trusted_prices[asset] = trusted
        ref_issues_by_asset[asset] = issues

    # ── Step 3: per-pair checks (A-series, B1, B4, D1) ──────────────────────────
    tasks = []
    for sym, tgt in PAIRS:
        base, quote = split_symbol(sym)
        asset = base.upper()
        is_usdt_pair = (quote == "usdt")
        tasks.append(process_pair(
            sym, tgt, session, semaphore,
            trusted_price=trusted_prices.get(asset) if is_usdt_pair else None,
            ref_issues=ref_issues_by_asset.get(asset, []) if is_usdt_pair else [],
            vol_hist_root=vol_hist_root,
            layer_hist_root=layer_hist_root,
        ))
    raw_results = await asyncio.gather(*tasks)
    results = [r for r in raw_results if r is not None]

    # ── Stamp last observed mid + timestamp per pair into shared_state ─────────
    # Exposed via /api/state so a frozen or stale pair's mid is visibly stale: a
    # mid with no "as of" timestamp looks current forever. Only pairs that
    # returned a numeric mid this cycle are stamped — a one-sided book (A3, mid
    # "N/A") or a failed fetch leaves the previous stamp untouched, so the gap
    # between last_mid_ts and now is exactly how long that pair's mid has been
    # unobservable.
    now_iso = ngt_now().isoformat()
    for r in results:
        if isinstance(r.get("mid_price"), (int, float)):
            pair_state = shared_state.setdefault(r["symbol"], {})
            pair_state["last_mid"]    = r["mid_price"]
            pair_state["last_mid_ts"] = now_iso

    # ── Step 4: F1 — cross-pair arbitrage, needs every pair's mid in hand ──────
    mids = {r["symbol"]: r["mid_price"] for r in results if isinstance(r["mid_price"], (int, float))}
    b1_fired = {r["symbol"] for r in results if "B1:" in (r.get("issues") or "")}
    triangles = find_arb_triangles(PAIRS)
    arb_gaps = check_arb_gaps(triangles, mids, b1_fired)

    by_symbol = {r["symbol"]: r for r in results}
    for gap in arb_gaps:
        r = by_symbol.get(gap["pair"])
        if not r:
            continue
        label = (f"Arb gap {gap['gap_pct']:+.2f}% vs implied {gap['implied']:,.6g} "
                 f"(legs: {'/'.join(gap['legs'])}) — suspect leg: {gap['suspect']}")
        issue = ("F1", gap["severity"], label)
        r["issues"] = (r["issues"] + "|" if r["issues"] else "") + f"{issue[0]}:{issue[1]}"
        r["_actionable"].append(issue)
        r["status"] = "Warning"
        r["should_alert"] = True
        # Recalculate tier — F1 is Tier 3, but don't raise it if pair already has a lower tier
        r["alert_tier"] = worst_tier(r["_actionable"])

    warnings    = [r for r in results if r["status"] == "Warning"]
    alert_pairs = [r for r in warnings if r["should_alert"]]

    elapsed = (ngt_now() - cycle_start).total_seconds()
    print(f"\n⏱  Cycle {cycle_num} complete in {elapsed:.1f}s — "
          f"{len(results)}/{len(PAIRS)} pairs | {len(warnings)} warnings | "
          f"{len(arb_gaps)} F1 gaps | {len(e2_issues)} E2 issues")

    # ── Reset consecutive counters for issues that cleared this cycle ───────────
    # For every pair we got results for, any Tier-2 issue that is NOT in the
    # current actionable list should have its counter reset to 0.
    active_issues_by_sym: dict[str, set] = {}
    for r in results:
        active_issues_by_sym[r["symbol"]] = {
            issue_id for issue_id, _, _ in r.get("_actionable", [])
        }
    for sym, _ in PAIRS:
        if sym not in active_issues_by_sym:
            continue   # pair failed to fetch — don't reset, leave counters as-is
        active = active_issues_by_sym[sym]
        for tid in _TIER2_IDS | {"B4", "G2"}:
            if tid not in active:
                reset_consecutive(shared_state, sym, tid)

    # ── E1: outage detection — Tier 1, uses its own "E1" cooldown key on "_global" ──
    # Cooldown is committed only on a confirmed send (delivery-gated) so a dropped
    # outage alert retries next cycle instead of going dark for the window.
    failed_count  = len(PAIRS) - len(results)
    failure_ratio = failed_count / len(PAIRS) if PAIRS else 0
    if failure_ratio >= FAILED_PAIR_RATIO_FOR_OUTAGE_ALERT:
        if not is_in_cooldown(shared_state, "_global", "E1"):
            sent = await send_telegram(
                f"🔴 <b>E1 — Possible Quidax API outage</b>\n"
                f"{failed_count}/{len(PAIRS)} pairs failed to fetch this cycle "
                f"({ngt_now().strftime('%Y-%m-%d %H:%M:%S')} NGT).",
                session,
            )
            if sent:
                start_cooldown(shared_state, "_global", "E1")
            else:
                print("[E1] Telegram send failed — cooldown not set, will retry next cycle")
        else:
            print(f"[E1] outage ratio {failure_ratio:.0%} — cooldown active, skipping Telegram")
    else:
        reset_consecutive(shared_state, "_global", "E1")

    # ── E2: reference feed disconnect — Tier 1, per feed source ────────────────
    if e2_issues:
        if not is_in_cooldown(shared_state, "_global", "E2"):
            msg = f"🔴 <b>E2 — Reference Feed Disconnect</b>\n<i>{ngt_now().strftime('%Y-%m-%d %H:%M:%S')} (NGT)</i>\n"
            for _, sev, label in e2_issues:
                msg += f"  🚨 {label}\n"
            msg += "\nAll B1/B2/B3 checks for affected source(s) are suspended until the feed recovers."
            if await send_telegram(msg, session):
                start_cooldown(shared_state, "_global", "E2")
            else:
                print("[E2] Telegram send failed — cooldown not set, will retry next cycle")
        else:
            print(f"[E2] reference feed down — cooldown active, skipping Telegram")
    else:
        reset_consecutive(shared_state, "_global", "E2")

    # ── Telegram: per-pair alerts with tier filtering ────────────────────────────
    # Build two buckets per pair:
    #   telegram_issues — passed tier/cooldown check, will appear in Telegram
    #   flagged_issues  — Tier 3 or still in cooldown/confirming, dashboard only
    # `should_alert` on the result dict is already True for any pair with issues,
    # so the dashboard always shows them. We only gate the Telegram send here.
    #
    # `_actionable` is already deduped at the source (process_pair) and F1 appends
    # at most one tuple per pair, so each issue_id appears exactly once here — the
    # "call should_fire_telegram once per (symbol, issue_id)" contract holds.
    #
    # COOLDOWN COMMIT: cooldowns (+ the post-fire Tier-2 counter reset) are applied
    # ONLY after send_telegram confirms delivery, so a failed send is retried next
    # cycle rather than suppressed for the full window.

    tg_pairs = []   # [(result_dict, [telegram_issues])]
    for r in results:
        if not r.get("_actionable"):
            continue

        # ── Suspended pairs: mute Telegram, keep dashboard visibility ──────────
        # An operator-set suspension gags THIS pair's own alerts only (F1 on other
        # legs is unaffected — check_arb_gaps already ran per-pair above). We skip
        # the fire gate entirely and, per the agreed "clean slate on resume" rule,
        # reset each Tier-2 consecutive counter so a still-present issue re-confirms
        # from scratch when the window lifts instead of blasting the instant it does
        # (mirrors how a cooldown holds counters). Cooldowns already in flight are
        # left untouched — they're time-based and resume naturally.
        if is_suspended(suspensions, r["symbol"]):
            for issue_id, _, _ in r.get("_actionable", []):
                reset_consecutive(shared_state, r["symbol"], issue_id)
            r["telegram_fired"]  = False
            r["suspended_until"] = suspensions.get(r["symbol"].lower())
            detail = [f"{iid}:suspended" for iid, _, _ in r.get("_actionable", [])]
            r["telegram_detail"] = "|".join(detail)
            print(f"  [{r['symbol']}] suspended — {len(detail)} issue(s) muted, "
                  f"until {r['suspended_until']}")
            continue

        tg_issues = []
        detail_parts = []   # per-issue state, rendered as the dashboard caption
        for issue_id, severity, label in r.get("_actionable", []):
            if should_fire_telegram(shared_state, r["symbol"], issue_id, severity):
                tg_issues.append((issue_id, severity, label))
                detail_parts.append(f"{issue_id}:fired")
            else:
                tier = classify_tier(issue_id, severity)
                if tier == 3:
                    state = "flag-only"
                elif is_in_cooldown(shared_state, r["symbol"], issue_id):
                    state = "cooldown"
                else:
                    consec = get_consecutive(shared_state, r["symbol"], issue_id)
                    need   = TIER2_CONFIRM_CYCLES
                    state  = f"{consec}/{need}cyc"   # dashboard renders this as "N/M cycles"
                detail_parts.append(f"{issue_id}:{state}")
                print(f"  [{r['symbol']}] [{issue_id}] tier={tier} suppressed ({state})")

        # Surface the firing state to api.py / dashboard.html. telegram_fired is True
        # when at least one issue passed the tier+cooldown gate this cycle — this is
        # the field summary_stats counts as "alerts_fired" and the per-market badge
        # reads. (should_alert alone is True for ANY pair with issues, which is exactly
        # why the dashboard's fired-count was stuck at 0 before this field was emitted.)
        # telegram_detail is the compact breakdown shown when issues did NOT fire,
        # e.g. "B1:2/3cyc|A4:flag-only|D1:cooldown".
        r["telegram_fired"]  = bool(tg_issues)
        r["telegram_detail"] = "|".join(detail_parts)

        if tg_issues:
            tg_pairs.append((r, tg_issues))

    if tg_pairs:
        # Single consolidated anomaly message — D1 rides this same path now
        # (previously a second "Trade Spike Summary" message was sent separately,
        # so a pair with both A4 and D1 firing produced two Telegrams instead of
        # one; that divergence is removed).
        msg = f"⚠️ <b>Market Anomaly Alert</b>\n<i>{ngt_now().strftime('%Y-%m-%d %H:%M:%S')} (NGT)</i>\n{'─'*3}"
        for r, issues in tg_pairs:
            ids = ", ".join(alert_id for alert_id, _, _ in issues)
            msg += f"\n<b>{r['symbol'].upper()}</b> — [{ids}]\n"
            for alert_id, severity, label in issues:
                icon = "🚨" if severity == "CRITICAL" else ("⚠️" if severity == "HIGH" else "ℹ️")
                msg += f"    {icon} [{alert_id}] {label}\n"
            if not r["monitor_only"] and r["target_spread"] != "N/A":
                msg += f"  Spread: {r['current_spread']}% (Target: {r['target_spread']}%, Diff: {r['percent_diff']}%)\n"
            else:
                msg += f"  Spread: {r['current_spread']}%\n"
            msg += f"  Mid: {r['mid_price']}"
            if r.get("trusted_ref") not in (None, "N/A"):
                msg += f"  |  Reference: {r['trusted_ref']}"
            msg += "\n"
            msg += f"  Layers — Ask: {r['ask_layers']} | Bid: {r['bid_layers']}\n"
        # Commit cooldown + counter reset ONLY on a confirmed delivery.
        if await send_telegram(msg, session):
            for r, issues in tg_pairs:
                for issue_id, _, _ in issues:
                    start_cooldown(shared_state, r["symbol"], issue_id)
                    reset_consecutive(shared_state, r["symbol"], issue_id)
        else:
            print("⚠️  Anomaly Telegram send failed — cooldowns NOT committed, will retry next cycle")

    # ── Persist ──────────────────────────────────────────────────────────────────
    # State is saved AFTER all Telegram sends so that cooldown timestamps written
    # on confirmed delivery are captured. Saving before the sends (Bug G) meant a
    # crash mid-send would lose the cooldowns and re-fire on the next restart.
    clean_results = [{k: v for k, v in r.items() if not k.startswith("_")} for r in results]
    update_daily_log(clean_results)
    if clean_results:
        pd.DataFrame(clean_results).to_csv(os.path.join(DATA_DIR, "latest.csv"), index=False)
    save_state(shared_state)


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

        print(f"🚀 Starting continuous monitor — {len(PAIRS)} pairs, {CYCLE_SLEEP_SECONDS}s cycle | "
              f"Tier 1: immediate fire, Tier 2: {TIER2_CONFIRM_CYCLES} cycles to confirm, "
              f"cooldown {ALERT_COOLDOWN_MINUTES}min")

        # G1 depth-walk tracker — independent 5s task, own loop, own persistence.
        # Fire-and-forget: it manages its own error handling per-cycle (see
        # depth_walk_loop) and never raises out to here, so it doesn't need
        # supervision beyond being kept alive alongside the main loop.
        depth_walk_task = asyncio.create_task(depth_walk_loop(session))
        print(f"🚀 Starting USDTNGN depth-walk tracker — {DEPTH_WALK_POLL_INTERVAL_SECONDS}s poll, "
              f"{DEPTH_WALK_WEIGHT_USDT:,.0f} USDT weight")

        while True:
            cycle_num += 1
            print(f"\n{'═'*50}\n  Cycle {cycle_num}  —  {ngt_now().strftime('%Y-%m-%d %H:%M:%S')} NGT\n{'═'*50}")
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