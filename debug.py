"""
<<<<<<< HEAD
Quidax Market Monitor — API-based, OHM alert taxonomy v3
──────────────────────────────────────────────────────────────────────────────
Endpoints used:
  Quidax Depth  : GET /exchange-open-api/api/v1/markets/{symbol}/depth?limit=200
  Quidax K-Line : GET /exchange-open-api/api/v1/markets/{symbol}/k?period=1&limit=60
                  (1-minute candles, last 60 minutes — a rolling hourly window,
                   NOT 60-minute candles. See KLINE_CANDLE_MINUTES below.)
  MEXC          : GET /api/v3/ticker/price        (all symbols, one call — price only,
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
  D1 Volume Spike             — implemented (unchanged trigger logic; context is now a
                                 comparison against Quidax's own longer-term volume baseline,
                                 built from data already being fetched — no external 24h data)
  E1 Quidax API Failure       — implemented (unchanged: per-pair + outage-ratio detection)
  E2 Reference Feed Disconnect — implemented (MEXC/KuCoin batched call failure)
  F1 Cross-Pair Arbitrage Gap — implemented (triangulates via pairs already being fetched)

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
    A1, A3, A6 (CRITICAL/HIGH), B4-CRITICAL, D1, E1, E2
  Tier 2 — fire only after N consecutive cycles of the same issue, then 15-min cooldown:
    A2 (spread + shallow book), B1, B2, B3, B4-HIGH — N = TIER2_CONFIRM_CYCLES (3)
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
=======
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
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
"""

import asyncio
import json
<<<<<<< HEAD
import math
=======
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
import pandas as pd

<<<<<<< HEAD
from defaults import merge_config  # single source of truth for config

=======
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
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
<<<<<<< HEAD
=======
# Set QUIDAX_TG_BOT_TOKEN and QUIDAX_TG_CHAT_IDS (comma-separated) in a
# .env file (gitignored) or in your systemd unit's Environment= lines.
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
TELEGRAM_BOT_TOKEN = os.environ.get("QUIDAX_TG_BOT_TOKEN", "")
TELEGRAM_CHAT_IDS  = [c.strip() for c in os.environ.get("QUIDAX_TG_CHAT_IDS", "").split(",") if c.strip()]

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
    print("⚠️  QUIDAX_TG_BOT_TOKEN / QUIDAX_TG_CHAT_IDS not set — Telegram alerts are disabled.")

<<<<<<< HEAD
BASE_API_URL        = "https://openapi.quidax.io/exchange-open-api/api/v1"
MEXC_TICKER_URL      = "https://api.mexc.com/api/v3/ticker/price"
KUCOIN_TICKER_URL    = "https://api.kucoin.com/api/v1/market/allTickers"

# ── Persistence ───────────────────────────────────────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR    = "/app/data"
=======
BASE_API_URL = "https://openapi.quidax.io/exchange-open-api/api/v1"

# ── Persistence ───────────────────────────────────────────────────────────────
# Resolve DATA_DIR relative to this script's location so that debug.py and
# api.py always read/write the same files regardless of the working directory
# each process was launched from.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR    = os.path.join(_SCRIPT_DIR, "data")
# DATA_DIR = "/app/data"
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
STATE_FILE  = os.path.join(DATA_DIR, "health_state.json")
CONFIG_FILE = os.path.join(DATA_DIR, "monitor_config.json")

# ── Default configuration ─────────────────────────────────────────────────────
<<<<<<< HEAD
# Canonical defaults now live in defaults.py, shared verbatim with api.py so the
# two processes can never drift (this module imports merge_config from it). The
# dashboard writes changes to monitor_config.json; apply_config() re-reads it at
# the top of every cycle so adjustments take effect without restarting the process.


def _load_config_from_disk() -> dict:
    """Read monitor_config.json and deep-merge it over the shared defaults."""
=======
# All tunable parameters live here. The dashboard writes changes to
# monitor_config.json; apply_config() re-reads it at the top of every cycle
# so adjustments take effect without restarting the process.
_DEFAULT_CONFIG: dict = {
    "timing": {
        "anomaly_alert_after_minutes": 10,
        "alert_cooldown_minutes":      30,
        "cycle_sleep_seconds":         60,
    },
    "orderbook": {
        "depth_limit":               200,
        "min_orderbook_layers":      10,
        "thin_depth_threshold":      5_000,
        "depth_imbalance_ratio":     5.0,
        "stale_ob_cycles":           3,
        "mid_price_alert_threshold": 25,
        "dws_poor_threshold":        0.5,
        "min_abs_spread_diff_pct":   0.05,
    },
    "kline": {
        "candle_minutes":   1,
        "lookback_minutes": 60,
    },
    "pairs": [
        ["aaveusdt",     0.3  ],
        ["adausdt",      2.0  ],
        ["algousdt",     2.0  ],
        ["bchusdt",      1.20 ],
        ["bnbusdt",      0.3  ],
        ["bonkusdt",     2.0  ],
        ["btcusdt",      0.2  ],
        ["cakeusdt",     0.3  ],
        ["cfxusdt",      2.0  ],
        ["dashusdt",     2.0  ],
        ["dotusdt",      0.26 ],
        ["dogeusdt",     0.26 ],
        ["ethusdt",      0.25 ],
        ["fartcoinusdt", 2.0  ],
        ["flokiusdt",    0.5  ],
        ["hypeusdt",     2.0  ],
        ["linkusdt",     0.26 ],
        ["lskusdt",      1.5  ],
        ["ltcusdt",      0.3  ],
        ["pepeusdt",     0.5  ],
        ["polusdt",      0.5  ],
        ["rndrusdt",     2.0  ],
        ["shibusdt",     0.4  ],
        ["slpusdt",      2.0  ],
        ["solusdt",      0.25 ],
        ["suiusdt",      2.0  ],
        ["tonusdt",      0.3  ],
        ["trxusdt",      0.3  ],
        ["usdcusdt",     0.02 ],
        ["wifusdt",      2.0  ],
        ["xlmusdt",      0.3  ],
        ["xrpusdt",      0.3  ],
        ["xyousdt",      1.0  ],
        ["usdtcngn",     None ],
        ["btcngn",       0.7  ],
        ["usdtngn",      0.95 ],
        ["ethngn",       0.75 ],
        ["trxngn",       0.75 ],
        ["xrpngn",       0.5  ],
        ["dashngn",      0.5  ],
        ["ltcngn",       0.5  ],
        ["solngn",       0.8  ],
        ["usdcngn",      1.2  ],
        ["cngnngn",      None ],
        ["usdtghs",      1.3  ],
    ],
}


def _load_config_from_disk() -> dict:
    """Read monitor_config.json and deep-merge with defaults."""
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                stored = json.load(f)
<<<<<<< HEAD
            return merge_config(stored)
        except Exception as exc:
            print(f"⚠️  Could not read {CONFIG_FILE}: {exc} — using defaults")
    return merge_config({})
=======
            merged = json.loads(json.dumps(_DEFAULT_CONFIG))
            for section, values in stored.items():
                if section == "pairs":
                    merged["pairs"] = values
                elif isinstance(values, dict) and section in merged:
                    merged[section].update(values)
                else:
                    merged[section] = values
            return merged
        except Exception as exc:
            print(f"⚠️  Could not read {CONFIG_FILE}: {exc} — using defaults")
    return json.loads(json.dumps(_DEFAULT_CONFIG))
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b


def apply_config():
    """
    Load config from disk and apply every value to module-level globals.
    Called once at startup and again at the top of each run_cycle so that
    dashboard edits take effect on the next cycle without a restart.
    """
<<<<<<< HEAD
    global PAIRS, PAIR_ALIASES, DEPTH_LIMIT, KLINE_CANDLE_MINUTES, KLINE_LOOKBACK_MINUTES, VOLUME_BASELINE_BUCKETS
    global CYCLE_SLEEP_SECONDS
    global MIN_ORDERBOOK_LAYERS, THIN_DEPTH_THRESHOLD, DEPTH_IMBALANCE_RATIO
    global DWS_POOR_THRESHOLD, MIN_ABS_SPREAD_DIFF_PCT
    global MAX_CONCURRENT_PAIRS, MONITOR_ONLY_SYMBOLS
    global PRICE_DISCREPANCY_PCT, SOURCE_DIVERGENCE_PCT, SOURCE_DIVERGENCE_OVERRIDES, STALE_REFERENCE_CYCLES
    global STALE_UNCHANGED_CYCLES, STALE_MOVEMENT_EPSILON_PCT
    global CIRCUIT_BREAKER_PCT, CIRCUIT_BREAKER_WARN_RATIO, ARB_GAP_PCT
    global LAYER_CHURN_TOP_PCT, LAYER_CHURN_BASELINE_BUCKETS
    global LAYER_CHURN_RATIO_THRESHOLD
    global VOLUME_SPIKE_MODE, VOLUME_SPIKE_RATIO, VOLUME_SPIKE_MIN_BUCKETS, VOLUME_SPIKE_WARMUP_FALLBACK

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

    # K-line
    DEPTH_LIMIT              = int(cfg["orderbook"]["depth_limit"])
    KLINE_CANDLE_MINUTES     = int(cfg["kline"]["candle_minutes"])
    KLINE_LOOKBACK_MINUTES   = int(cfg["kline"]["lookback_minutes"])
    VOLUME_BASELINE_BUCKETS  = int(cfg["kline"].get("volume_baseline_buckets", 24))

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
=======
    global PAIRS, DEPTH_LIMIT, KLINE_CANDLE_MINUTES, KLINE_LOOKBACK_MINUTES
    global ANOMALY_ALERT_AFTER_MINUTES, ALERT_COOLDOWN_MINUTES, CYCLE_SLEEP_SECONDS
    global MIN_ORDERBOOK_LAYERS, THIN_DEPTH_THRESHOLD, DEPTH_IMBALANCE_RATIO
    global STALE_OB_CYCLES, MID_PRICE_ALERT_THRESHOLD, DWS_POOR_THRESHOLD
    global MIN_ABS_SPREAD_DIFF_PCT, MAX_CONCURRENT_PAIRS, MONITOR_ONLY_SYMBOLS

    cfg = _load_config_from_disk()

    # Pairs
    PAIRS = [(str(sym).lower(), tgt) for sym, tgt in cfg["pairs"]]

    # K-line
    DEPTH_LIMIT            = int(cfg["orderbook"]["depth_limit"])
    KLINE_CANDLE_MINUTES   = int(cfg["kline"]["candle_minutes"])
    KLINE_LOOKBACK_MINUTES = int(cfg["kline"]["lookback_minutes"])

    # Timing
    ANOMALY_ALERT_AFTER_MINUTES = float(cfg["timing"]["anomaly_alert_after_minutes"])
    ALERT_COOLDOWN_MINUTES      = float(cfg["timing"]["alert_cooldown_minutes"])
    CYCLE_SLEEP_SECONDS         = float(cfg["timing"]["cycle_sleep_seconds"])

    # Orderbook thresholds
    MIN_ORDERBOOK_LAYERS        = int(cfg["orderbook"]["min_orderbook_layers"])
    THIN_DEPTH_THRESHOLD        = float(cfg["orderbook"]["thin_depth_threshold"])
    DEPTH_IMBALANCE_RATIO       = float(cfg["orderbook"]["depth_imbalance_ratio"])
    STALE_OB_CYCLES             = int(cfg["orderbook"]["stale_ob_cycles"])
    MID_PRICE_ALERT_THRESHOLD   = float(cfg["orderbook"]["mid_price_alert_threshold"])
    DWS_POOR_THRESHOLD          = float(cfg["orderbook"]["dws_poor_threshold"])
    MIN_ABS_SPREAD_DIFF_PCT     = float(cfg["orderbook"]["min_abs_spread_diff_pct"])
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b

    # Derived
    MAX_CONCURRENT_PAIRS = 10   # not user-facing yet; keep fixed
    MONITOR_ONLY_SYMBOLS = {sym for sym, tgt in PAIRS if tgt is None}


# Initialise with defaults (or saved config if it already exists)
<<<<<<< HEAD
PAIRS:                       list  = []
PAIR_ALIASES:                dict  = {}
DEPTH_LIMIT:                 int   = 200
KLINE_CANDLE_MINUTES:        int   = 1
KLINE_LOOKBACK_MINUTES:      int   = 60
VOLUME_BASELINE_BUCKETS:     int   = 24
=======
PAIRS:                       list = []
DEPTH_LIMIT:                 int   = 200
KLINE_CANDLE_MINUTES:        int   = 1
KLINE_LOOKBACK_MINUTES:      int   = 60
ANOMALY_ALERT_AFTER_MINUTES: float = 10
ALERT_COOLDOWN_MINUTES:      float = 30
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
CYCLE_SLEEP_SECONDS:         float = 60
MIN_ORDERBOOK_LAYERS:        int   = 10
THIN_DEPTH_THRESHOLD:        float = 5_000
DEPTH_IMBALANCE_RATIO:       float = 5.0
<<<<<<< HEAD
=======
STALE_OB_CYCLES:             int   = 3
MID_PRICE_ALERT_THRESHOLD:   float = 25
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
DWS_POOR_THRESHOLD:          float = 0.5
MIN_ABS_SPREAD_DIFF_PCT:     float = 0.05
MAX_CONCURRENT_PAIRS:        int   = 10
MONITOR_ONLY_SYMBOLS:        set   = set()
<<<<<<< HEAD
PRICE_DISCREPANCY_PCT:       float = 0.5
SOURCE_DIVERGENCE_PCT:       float = 0.3
SOURCE_DIVERGENCE_OVERRIDES: dict  = {}
STALE_REFERENCE_CYCLES:      int   = 3
STALE_UNCHANGED_CYCLES:      int   = 5
STALE_MOVEMENT_EPSILON_PCT:  float = 0.0
CIRCUIT_BREAKER_PCT:         float = 10.0
CIRCUIT_BREAKER_WARN_RATIO:  float = 0.8
ARB_GAP_PCT:                 float = 0.5
LAYER_CHURN_TOP_PCT:          float = 0.5
LAYER_CHURN_BASELINE_BUCKETS: int   = 20
LAYER_CHURN_RATIO_THRESHOLD:  float = 0.2
VOLUME_SPIKE_MODE:            str   = "baseline_relative"
VOLUME_SPIKE_RATIO:           float = 3.0
VOLUME_SPIKE_MIN_BUCKETS:     int   = 4
VOLUME_SPIKE_WARMUP_FALLBACK: str   = "absolute"
=======
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
apply_config()  # populate from disk immediately

# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS / HELPERS
# ══════════════════════════════════════════════════════════════════════════════

NIGERIAN_TZ = timezone(timedelta(hours=1))

CURRENCY_SYMBOLS = {"USDT": "$", "NGN": "₦", "GHS": "₵"}
HIGH_VOL_TOKENS  = {"BTC", "ETH", "SOL", "USDC"}

<<<<<<< HEAD
REF_HISTORY_LEN = 8   # rolling readings kept per asset/exchange for B2 drift detection

LAYER_CHURN_MIN_HISTORY_BUCKETS = 5   # A6 cold-start gate — min prior churn readings
                                       # needed before the self-baseline is trusted at
                                       # all (not dashboard-configurable, same spirit as
                                       # D1's hardcoded bucket_count >= 2 gate)

=======
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b

def ngt_now() -> datetime:
    return datetime.now(NIGERIAN_TZ)


# Quote currencies actually present in PAIRS — checked longest-first so
# "usdt" (4 chars) isn't mistaken for a 3-char suffix.
KNOWN_QUOTE_CURRENCIES = ("usdt", "ngn", "ghs")


def split_symbol(sym: str) -> tuple[str, str]:
    """
    Split a concatenated symbol like 'btcusdt' into (base, quote).
    Quote length varies (ngn/ghs = 3 chars, usdt = 4 chars) — a fixed
<<<<<<< HEAD
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
=======
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
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
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


<<<<<<< HEAD
def format_depth(val) -> str:
    if val in (None, "", "N/A"):
        return "$0"
    val = float(val)
    if not val:               return "$0"
=======
def format_depth(val: float) -> str:
    if not val:              return "$0"
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
    if val >= 1_000_000:    return f"${val/1_000_000:.2f}M"
    if val >= 1_000:        return f"${val/1_000:.1f}K"
    return f"${val:.0f}"


# ══════════════════════════════════════════════════════════════════════════════
# API LAYER
# ══════════════════════════════════════════════════════════════════════════════

FETCH_MAX_RETRIES   = 2     # additional attempts after the first failure
FETCH_RETRY_BACKOFF = 1.5   # seconds, doubles each retry


<<<<<<< HEAD
async def _request_json(session: aiohttp.ClientSession, url: str, timeout: int = 10) -> dict | list:
=======
async def _request_json(session: aiohttp.ClientSession, url: str) -> dict:
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
    """GET a URL and return parsed JSON, retrying transient failures."""
    last_exc = None
    for attempt in range(FETCH_MAX_RETRIES + 1):
        try:
            async with session.get(url, headers={"accept": "application/json"},
<<<<<<< HEAD
                                   timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
=======
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
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
<<<<<<< HEAD
=======
    # Response envelope: {"status": "success", "data": {"asks": [...], "bids": [...], ...}}
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
    return payload["data"]


async def fetch_kline(session: aiohttp.ClientSession, symbol: str) -> list:
    """
    Returns 1-minute candles for the last 60 minutes (a rolling window,
<<<<<<< HEAD
    not calendar-day-scoped). Each candle: [timestamp_ms, open, high, low, close, volume] (strings).
=======
    not calendar-day-scoped — see get_recent_spikes).
    Anchors via ?timestamp=<lookback_ms> so we never miss the current
    incomplete hour — one call, exactly KLINE_LOOKBACK_MINUTES candles,
    no looping needed.

    Each candle: [timestamp_ms, open, high, low, close, volume]  ← strings
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
    """
    lookback_ms = int((ngt_now().timestamp() - KLINE_LOOKBACK_MINUTES * 60) * 1000)
    url = (f"{BASE_API_URL}/markets/{symbol}/k"
           f"?period={KLINE_CANDLE_MINUTES}&limit={KLINE_LOOKBACK_MINUTES}&timestamp={lookback_ms}")
    payload = await _request_json(session, url)
<<<<<<< HEAD
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


=======
    # Response envelope: {"status": "success", "data": [[ts_ms, o, h, l, c, vol], ...]}
    return payload["data"]


>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
# ══════════════════════════════════════════════════════════════════════════════
# ORDERBOOK ANALYTICS
# ══════════════════════════════════════════════════════════════════════════════

def build_orderbook_dfs(raw: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
<<<<<<< HEAD
    """Convert raw depth payload to ask/bid DataFrames with columns [price, amount]."""
=======
    """
    Convert raw depth payload to ask/bid DataFrames with columns
    [price, amount].  Asks sorted ascending, bids descending.
    """
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
    def to_df(rows):
        if not rows:
            return pd.DataFrame(columns=["price", "amount"])
        df = pd.DataFrame(rows, columns=["price", "amount"])
<<<<<<< HEAD
        return df.astype(float)
=======
        df = df.astype(float)
        return df
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b

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


<<<<<<< HEAD
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
        window_open = float(kline_raw[0][1])  # [ts, open, high, low, close, vol] of oldest candle
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


# ══════════════════════════════════════════════════════════════════════════════
# K-LINE SPIKE DETECTION (D1)
# ══════════════════════════════════════════════════════════════════════════════

def compute_window_volume(candles: list, sym: str) -> Optional[dict]:
    """
    Pure aggregation: sums the last KLINE_LOOKBACK_MINUTES of 1-minute candles into a
    single rolling quote-volume figure for this pair. Does NOT apply any threshold —
    that's D1's job (get_recent_spikes) — this just measures "how much traded."
    Pure rolling window — does NOT filter by calendar date.
    """
    if not candles:
        return None
    currency = get_currency_symbol(sym)
    total_quote_volume, candle_count = 0.0, 0
    window_start = window_end = None
=======
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
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b

    for candle in candles:
        try:
            ts, o, h, l, c, volume = candle[:6]
            candle_dt = datetime.fromtimestamp(int(ts) / 1000, tz=NIGERIAN_TZ)
<<<<<<< HEAD
            total_quote_volume += float(volume) * float(c)
            candle_count += 1
=======

            quote_value = float(volume) * float(c)
            total_quote_volume += quote_value
            candle_count       += 1

>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
            if window_start is None or candle_dt < window_start:
                window_start = candle_dt
            if window_end is None or candle_dt > window_end:
                window_end = candle_dt
<<<<<<< HEAD
=======

>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
        except (ValueError, TypeError, IndexError):
            continue

    if candle_count == 0:
<<<<<<< HEAD
        return None

    window_label = (f"{window_start.strftime('%H:%M')}–{window_end.strftime('%H:%M')}"
                     if window_start and window_end else f"last {KLINE_LOOKBACK_MINUTES} min")
    return {"window": window_label, "candle_count": candle_count,
            "quote_volume": total_quote_volume, "currency": currency}


def update_volume_baseline(symbol: str, current_volume: float, vol_hist_root: dict) -> tuple[Optional[float], int]:
    """
    Builds D1's "Quidax-only" baseline with no external data and no extra API calls —
    purely from the rolling window volume we already compute every cycle.

    A new "bucket" is only recorded once per KLINE_LOOKBACK_MINUTES of real elapsed time,
    not every cycle. This matters: with the default 60s cycle and 60min lookback window,
    consecutive cycles' rolling windows overlap by ~59/60 — recording every cycle would
    just average near-duplicate overlapping readings against themselves and tell us
    almost nothing. Sampling once per window-length gives genuinely distinct historical
    readings to compare "right now" against.

    Returns (baseline_mean_of_PRIOR_buckets, how_many_prior_buckets_that_mean_is_built_from).
    The just-recorded current reading is deliberately excluded from its own baseline.
    """
    pair_hist = vol_hist_root.setdefault(symbol, {"buckets": [], "last_bucket_ts": None})
    now = ngt_now()
    last_ts = datetime.fromisoformat(pair_hist["last_bucket_ts"]) if pair_hist["last_bucket_ts"] else None

    prior_buckets = list(pair_hist["buckets"])
    baseline = (sum(prior_buckets) / len(prior_buckets)) if prior_buckets else None

    if last_ts is None or (now - last_ts).total_seconds() >= KLINE_LOOKBACK_MINUTES * 60:
        pair_hist["buckets"].append(current_volume)
        pair_hist["buckets"] = pair_hist["buckets"][-VOLUME_BASELINE_BUCKETS:]
        pair_hist["last_bucket_ts"] = now.isoformat()

    return baseline, len(prior_buckets)


def get_recent_spikes(window_info: Optional[dict], sym: str,
                       baseline: Optional[float], bucket_count: int) -> list:
    """
    D1 — fires when this pair's rolling-window volume is unusually large *for this
    pair*, not just large in absolute terms.

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
        fired   = vol >= VOLUME_SPIKE_RATIO * baseline and vol >= threshold
        trigger = "baseline_relative"
    else:
        # absolute mode, or baseline-relative still warming up
        if VOLUME_SPIKE_MODE == "baseline_relative" and VOLUME_SPIKE_WARMUP_FALLBACK == "suppress":
            return []                   # deliberate warm-up blind spot
        fired   = vol >= threshold
        trigger = "absolute"

    if not fired:
        return []

    spike = dict(window_info)
    spike["trigger"] = trigger
    if trigger == "baseline_relative":
        ratio = vol / baseline
        spike["ref_context"] = (f"≈{ratio:.1f}x the typical {KLINE_LOOKBACK_MINUTES}min volume "
                                 f"(≥{VOLUME_SPIKE_RATIO:g}x baseline over {bucket_count} windows, "
                                 f"floor {cur}{threshold:,.0f})")
    elif baseline and bucket_count >= 2:
        ratio = vol / baseline
        spike["ref_context"] = (f"≈{ratio:.1f}x typical — fired on absolute floor "
                                 f"{cur}{threshold:,.0f} (baseline warming, "
                                 f"{bucket_count}/{VOLUME_SPIKE_MIN_BUCKETS} buckets)")
    else:
        spike["ref_context"] = (f"fired on absolute floor {cur}{threshold:,.0f} "
                                 f"— baseline still building, no per-pair context yet")
    return [spike]


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
=======
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
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b


# ══════════════════════════════════════════════════════════════════════════════
# PERSISTENCE
# ══════════════════════════════════════════════════════════════════════════════

<<<<<<< HEAD
=======
_state_lock = asyncio.Lock()


>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
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
<<<<<<< HEAD
    Long-format daily log: one ROW per WARNING market per cycle. Only markets whose
    status is "Warning" this cycle are appended — healthy ("Checked") pairs and
    failed-fetch pairs are skipped, so the file stays small and every row is something
    worth reviewing. Rows keep PAIRS config order within a cycle.
    Columns: Timestamp, Market, Status, Issues, Depth.

    A cycle with no warnings appends nothing (and writes no header until the first
    warning of the day creates the file).
=======
    Long-format daily log: one ROW per (market, check) rather than 3 new
    COLUMNS per check. At a 60s cycle interval the old wide format could
    grow past 1,000+ columns in a single day, with every cycle paying the
    cost of reading and rewriting the whole (ever-growing) file. This
    version only appends — cost per cycle stays flat regardless of how
    many checks have already run today.

    Columns: Timestamp, Market, Status, Issues, Depth
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
    """
    now   = ngt_now()
    today = now.strftime("%Y-%m-%d")
    path  = os.path.join(DATA_DIR, f"daily_log_{today}.csv")
    ts    = now.strftime("%H:%M:%S")

<<<<<<< HEAD
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
=======
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
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b

    new_df      = pd.DataFrame(rows)
    file_exists = os.path.exists(path)
    new_df.to_csv(path, mode="a", header=not file_exists, index=False)
<<<<<<< HEAD
    print(f"✅ Daily log appended: {path} (+{len(rows)} warning row(s))")
=======
    print(f"✅ Daily log appended: {path} (+{len(rows)} rows)")
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

_telegram_lock = asyncio.Lock()
<<<<<<< HEAD
=======

>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
TELEGRAM_MAX_CHARS = 4000  # stay under Telegram's 4096 hard limit


def _chunk_telegram_message(msg: str, max_chars: int = TELEGRAM_MAX_CHARS) -> list:
<<<<<<< HEAD
    if len(msg) <= max_chars:
        return [msg]
    lines = msg.split("\n")
    chunks, current, current_len = [], [], 0
    for line in lines:
        extra = len(line) + (1 if current else 0)
=======
    """Split a message into chunks at line boundaries, each under max_chars."""
    if len(msg) <= max_chars:
        return [msg]
    lines = msg.split("\n")
    chunks, current = [], []
    current_len = 0
    for line in lines:
        extra = len(line) + (1 if current else 0)  # +1 for the joining "\n"
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
        if current and current_len + extra > max_chars:
            chunks.append("\n".join(current))
            current, current_len = [line], len(line)
        else:
            current.append(line)
            current_len += extra
    if current:
        chunks.append("\n".join(current))
    return chunks


<<<<<<< HEAD
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
=======
async def send_telegram(msg: str, session: aiohttp.ClientSession):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        return
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
    async with _telegram_lock:
        for chat_id in TELEGRAM_CHAT_IDS:
            chat_id = str(chat_id).strip()
            if not chat_id:
                continue
<<<<<<< HEAD
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
=======
            for chunk in _chunk_telegram_message(msg):
                try:
                    await session.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                        json={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"},
                        timeout=aiohttp.ClientTimeout(total=10),
                    )
                except Exception as e:
                    print(f"⚠️  Telegram send failed: {e}")
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b


# ══════════════════════════════════════════════════════════════════════════════
# PER-PAIR WORKER
# ══════════════════════════════════════════════════════════════════════════════

async def process_pair(
    symbol: str,
    target: Optional[float],
<<<<<<< HEAD
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    trusted_price: Optional[float],
    ref_issues: list,
    vol_hist_root: dict,
    layer_hist_root: dict,
) -> Optional[dict]:
    """
    Fetches depth + kline for one pair and runs every A/B/D check applicable to it.
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
=======
    shared_state: dict,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
) -> Optional[dict]:
    """
    Fetches depth + kline for one pair, runs all health checks,
    updates shared_state (time-based anomaly tracking), and returns a result dict.
    Alert firing decisions are left to the caller (main loop).
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
    """
    async with semaphore:
        monitor_only = target is None
        try:
<<<<<<< HEAD
=======
            # ── Fetch ──────────────────────────────────────────────────────
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
            depth_raw, kline_raw = await asyncio.gather(
                fetch_depth(session, symbol),
                fetch_kline(session, symbol),
            )
<<<<<<< HEAD
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
                    "_spikes": [],
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
=======

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
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
            if best_bid >= best_ask:
                issues.append(("A1", "CRITICAL",
                    f"Crossed orderbook — best bid {best_bid:,.6g} ≥ best ask {best_ask:,.6g}"))

<<<<<<< HEAD
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
=======
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
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
            if 0 < depth_25 < THIN_DEPTH_THRESHOLD:
                issues.append(("A4", "MEDIUM",
                    f"Thin mid-market — depth within spread: {format_depth(depth_25)} "
                    f"(min {format_depth(THIN_DEPTH_THRESHOLD)})"))

<<<<<<< HEAD
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

            # ── Fold duplicate ids (A2 x2, B3 x2) BEFORE any tier/firing logic ──
            issues = dedupe_actionable(issues)

            # ── D1 — Volume spike, with Quidax's own longer-term baseline as context ──
            window_info = compute_window_volume(kline_raw, symbol)
            baseline, bucket_count = (None, 0)
            if window_info:
                baseline, bucket_count = update_volume_baseline(symbol, window_info["quote_volume"], vol_hist_root)
            spikes = get_recent_spikes(window_info, symbol, baseline, bucket_count)

            is_poor = bool(issues)
            # D1 spikes count as Tier 1 for display purposes
            has_spikes = bool(spikes)

            # ── D1 dashboard surfacing ──────────────────────────────────────
            # _spikes is stripped at the CSV boundary (the not-startswith("_")
            # filter in run_cycle), so the dashboard never sees it. Expose D1 via
            # plain fields that survive serialization. These are display-only and
            # deliberately kept OUT of `issues`/`_actionable` so D1 stays isolated
            # from status/tier/Tier-2-confirmation/B1-detection machinery.
            d1_threshold = get_threshold(symbol)
            if has_spikes:
                d1_context = spikes[0].get("ref_context", "")
            elif baseline and bucket_count >= 2 and window_info:
                ratio_txt = f"≈{window_info['quote_volume'] / baseline:.1f}x typical"
                if VOLUME_SPIKE_MODE == "baseline_relative" and bucket_count < VOLUME_SPIKE_MIN_BUCKETS:
                    d1_context = (f"{ratio_txt} (baseline warming "
                                  f"{bucket_count}/{VOLUME_SPIKE_MIN_BUCKETS} — absolute floor active)")
                else:
                    d1_context = f"{ratio_txt} ({bucket_count}-window baseline)"
            else:
                d1_context = "baseline building…"
            # D1 spike present → at least Tier 1 for display (the stated intent).
            # With other issues, keep the most urgent of (their tier, 1); with NO
            # other issues the spike itself is the Tier-1 signal — min(0, 1) would
            # otherwise yield 0 and hide the pair from the dashboard's default view.
            if has_spikes:
                tier = min(worst_tier(issues), 1) if issues else 1
            else:
                tier = worst_tier(issues)

            print(f"[{symbol}] {'⚠️ ' if is_poor else '✅'} spread={curr_spread:.4f}% mid={mid_price:,.4f} "
                  f"dws={dws:.4f} layers={ask_layers}/{bid_layers} issues={len(issues)}")
=======
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
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b

            return {
                "timestamp":       ngt_now().strftime("%Y-%m-%d %H:%M:%S"),
                "symbol":          symbol,
                "monitor_only":    monitor_only,
                "status":          "Warning" if is_poor else "Checked",
                "issues":          "|".join(f"{i[0]}:{i[1]}" for i in issues) if issues else "",
<<<<<<< HEAD
                "should_alert":    is_poor,
                "alert_tier":      tier,
=======
                "should_alert":    should_alert,
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
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
<<<<<<< HEAD
                "trusted_ref":     round(trusted_price, 8) if trusted_price else "N/A",
                "layer_churn_pct":          round(churn_score * 100, 1) if churn_score is not None else "N/A",
                "layer_churn_baseline_pct": round(churn_baseline * 100, 1) if churn_baseline is not None else "N/A",
                "telegram_fired":  False,   # set in run_cycle's firing loop once the
                "telegram_detail": "",      # tier/cooldown gate has been evaluated
                "d1_spike":         has_spikes,
                "d1_window_volume": round(window_info["quote_volume"], 2) if window_info else "N/A",
                "d1_threshold":     d1_threshold if d1_threshold is not None else "N/A",
                "d1_currency":      get_currency_symbol(symbol),
                "d1_context":       d1_context,
                "_actionable":     issues,
=======
                "_actionable":     actionable,
                "_price_move":     price_move_label,
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
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
<<<<<<< HEAD

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
=======
OUTAGE_ALERT_COOLDOWN_MINUTES      = 30

_last_outage_alert: Optional[datetime] = None
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b


async def run_cycle(shared_state: dict, session: aiohttp.ClientSession, cycle_num: int):
    apply_config()   # pick up any dashboard edits without restarting
<<<<<<< HEAD
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
    spike_pairs = [r for r in results if r.get("_spikes")]

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
        for tid in _TIER2_IDS | {"B4"}:
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
=======
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
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
                f"{failed_count}/{len(PAIRS)} pairs failed to fetch this cycle "
                f"({ngt_now().strftime('%Y-%m-%d %H:%M:%S')} NGT).",
                session,
            )
<<<<<<< HEAD
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

    tg_pairs = []   # [(result_dict, [telegram_issues], [telegram_spikes])]
    for r in results:
        if not r.get("_actionable") and not r.get("_spikes"):
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

        # D1 spikes — Tier 1, fire immediately with their own cooldown key
        spike_tg = []
        for spike in r.get("_spikes", []):
            if should_fire_telegram(shared_state, r["symbol"], "D1", "HIGH"):
                spike_tg.append(spike)
                detail_parts.append("D1:fired")
            else:
                detail_parts.append("D1:cooldown")   # D1 is Tier 1 — only cooldown suppresses it
                print(f"  [{r['symbol']}] [D1] tier=1 suppressed (cooldown)")

        # Surface the firing state to api.py / dashboard.html. telegram_fired is True
        # when at least one issue/spike passed the tier+cooldown gate this cycle — this
        # is the field summary_stats counts as "alerts_fired" and the per-market badge
        # reads. (should_alert alone is True for ANY pair with issues, which is exactly
        # why the dashboard's fired-count was stuck at 0 before this field was emitted.)
        # telegram_detail is the compact breakdown shown when issues did NOT fire,
        # e.g. "B1:2/3cyc|A4:flag-only".
        r["telegram_fired"]  = bool(tg_issues or spike_tg)
        r["telegram_detail"] = "|".join(detail_parts)

        if tg_issues or spike_tg:
            tg_pairs.append((r, tg_issues, spike_tg))

    if tg_pairs:
        # Split into anomaly pairs and spike-only pairs for cleaner messages
        anomaly_pairs  = [(r, issues) for r, issues, spikes in tg_pairs if issues]
        spike_pairs_tg = [(r, spikes) for r, issues, spikes in tg_pairs if spikes]

        if anomaly_pairs:
            msg = f"⚠️ <b>Market Anomaly Alert</b>\n<i>{ngt_now().strftime('%Y-%m-%d %H:%M:%S')} (NGT)</i>\n{'─'*3}"
            for r, issues in anomaly_pairs:
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
                for r, issues in anomaly_pairs:
                    for issue_id, _, _ in issues:
                        start_cooldown(shared_state, r["symbol"], issue_id)
                        reset_consecutive(shared_state, r["symbol"], issue_id)
            else:
                print("⚠️  Anomaly Telegram send failed — cooldowns NOT committed, will retry next cycle")

        if spike_pairs_tg:
            msg = f"🚨 <b>Trade Spike Summary</b>\n<i>{ngt_now().strftime('%Y-%m-%d %H:%M:%S')} (NGT)</i>\n{'─'*30}\n"
            for r, spikes in spike_pairs_tg:
                msg += f"\n<b>{r['symbol'].upper()}</b>\n"
                for s in spikes:
                    msg += f"  {s['window']} ({s['candle_count']} candles) — {s['currency']}{s['quote_volume']:,.2f}"
                    if s.get("ref_context"):
                        msg += f"  ({s['ref_context']})"
                    msg += "\n"
            # D1 cooldowns committed only on a confirmed delivery.
            if await send_telegram(msg, session):
                for r, _ in spike_pairs_tg:
                    start_cooldown(shared_state, r["symbol"], "D1")
            else:
                print("⚠️  Spike Telegram send failed — D1 cooldowns NOT committed, will retry next cycle")

    # ── Persist ──────────────────────────────────────────────────────────────────
    # State is saved AFTER all Telegram sends so that cooldown timestamps written
    # on confirmed delivery are captured. Saving before the sends (Bug G) meant a
    # crash mid-send would lose the cooldowns and re-fire on the next restart.
    clean_results = [{k: v for k, v in r.items() if not k.startswith("_")} for r in results]
    update_daily_log(clean_results)
    if clean_results:
        pd.DataFrame(clean_results).to_csv(os.path.join(DATA_DIR, "latest.csv"), index=False)
    save_state(shared_state)

=======

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

>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b

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

<<<<<<< HEAD
        print(f"🚀 Starting continuous monitor — {len(PAIRS)} pairs, {CYCLE_SLEEP_SECONDS}s cycle | "
              f"Tier 1: immediate fire, Tier 2: {TIER2_CONFIRM_CYCLES} cycles to confirm, "
              f"cooldown {ALERT_COOLDOWN_MINUTES}min")

        while True:
            cycle_num += 1
            print(f"\n{'═'*50}\n  Cycle {cycle_num}  —  {ngt_now().strftime('%Y-%m-%d %H:%M:%S')} NGT\n{'═'*50}")
=======
        print(f"🚀 Starting continuous monitor — {len(PAIRS)} pairs, "
              f"{CYCLE_SLEEP_SECONDS}s cycle, "
              f"alert after {ANOMALY_ALERT_AFTER_MINUTES}min anomaly")

        while True:
            cycle_num += 1
            print(f"\n{'═' * 50}")
            print(f"  Cycle {cycle_num}  —  {ngt_now().strftime('%Y-%m-%d %H:%M:%S')} NGT")
            print(f"{'═' * 50}")
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
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