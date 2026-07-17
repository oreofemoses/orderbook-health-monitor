"""
Shared default configuration — single source of truth.
──────────────────────────────────────────────────────────────────────────────
Both the monitor engine (debug.py) and the dashboard API (api.py) import
DEFAULT_CONFIG and merge_config() from here, so the two processes can never
drift out of sync on what the defaults are or how a stored monitor_config.json
is layered on top of them.

Edit defaults in THIS file only. Neither debug.py nor api.py keeps its own copy
anymore.

NOTE: this module is import-only — it has no side effects, reads no environment,
and starts no async work, so api.py can import it without dragging in the
monitor engine's aiohttp/dotenv/apply_config machinery.
"""

import copy


# Fixed liquidity-uptime band half-width, in naira. This is deliberately NOT a
# config knob — it defines the persisted/graphed uptime series so that the
# stored history stays comparable over time regardless of dashboard tuning of
# the shared in-band weight.
UPTIME_FIXED_STEP_NGN = 1.0

# Fixed top-of-book spread ceiling, in naira, for the USDTNGN "spread ≤ ₦1
# compliance" metric (best_ask - best_bid <= this). Like UPTIME_FIXED_STEP_NGN
# above, this is deliberately NOT a config knob — it defines a persisted and
# graphed hourly series, so the stored history must stay comparable over time
# rather than shifting meaning whenever someone retunes a dashboard field.
SPREAD_GAP_FIXED_NGN = 1.0


# Canonical defaults. Every tunable the dashboard can edit has an entry here;
# a stored monitor_config.json overrides these per-key via merge_config().
DEFAULT_CONFIG: dict = {
    "timing": {
        "cycle_sleep_seconds": 60,
    },
    "orderbook": {
        "depth_limit":             200,
        "min_orderbook_layers":    10,
        "thin_depth_threshold":    5_000,
        "depth_imbalance_ratio":   5.0,
        "dws_poor_threshold":      0.5,
        "min_abs_spread_diff_pct": 0.05,
    },
    "pricing": {
        "price_discrepancy_pct":      0.5,   # B1 — % diff Quidax vs trusted reference
        "source_divergence_pct":      0.3,   # B2 — global default % diff MEXC vs KuCoin
        # B2 — optional per-symbol override of the divergence threshold, keyed by
        # full USDT symbol (e.g. {"btcusdt": 0.05, "pepeusdt": 1.5}). Any pair not
        # listed falls back to source_divergence_pct above. USDT-quoted pairs only;
        # entries for NGN/GHS symbols are ignored (B2 doesn't run on them).
        "source_divergence_overrides": {},
        "stale_reference_cycles":     3,     # B3 — consecutive UNAVAILABLE reads before a dead feed fires
        "stale_unchanged_cycles":     5,     # B3 — consecutive UNCHANGED reads before the cross-source liveness check runs
        "stale_movement_epsilon_pct": 0.0,   # B3 — |move| <= this % counts as "unchanged"
        "circuit_breaker_pct":        10.0,  # B4 — total window move % treated as breaker risk
        "circuit_breaker_warn_ratio": 0.8,   # B4 — fire HIGH at this fraction of the pct above
        "arb_gap_pct":                0.5,   # F1 — % gap between actual and implied cross price
    },
    "kline": {
        # B4 (circuit breaker) ONLY as of the D1/B4 decoupling below. D1 has its
        # own independent candle_minutes/lookback_minutes under volume_spike —
        # changing these no longer touches D1's window, and vice versa.
        "candle_minutes":   1,
        "lookback_minutes": 60,
    },
    "g2": {
        # G2 — candle wick / anomalous print detector. Reuses B4's own k-line
        # feed (kline.candle_minutes / kline.lookback_minutes above) — no
        # separate API call. Scans every candle in the window each cycle:
        #   low <= 0                    -> CRITICAL, always, regardless of pct
        #   (high-low)/open*100 >= this -> HIGH
        "swing_pct": 5.0,
    },
    "volume_spike": {
        "mode":                 "baseline_relative",  # "baseline_relative" | "absolute"
        "spike_ratio":          3.0,    # D1 fires when window volume >= this * the pair's own baseline
        "min_baseline_buckets": 4,      # buckets required before the baseline is trusted enough to gate the trigger
        "warmup_fallback":      "absolute",  # before the baseline is ready: "absolute" | "suppress"
        # D1's OWN k-line fetch — independent of kline.* above (which feeds B4
        # only). A separate API call per pair per cycle, sized for markets where
        # 1-minute candles are too granular to reliably show volume.
        "candle_minutes":   60,   # D1 candle period, minutes
        "lookback_minutes": 240,  # D1 rolling window, minutes (4 candles at the default candle size)
        # How many prior D1 windows to average for the baseline. A new bucket is
        # recorded once per `lookback_minutes` of elapsed time (see
        # update_volume_baseline), so effective baseline span = buckets *
        # lookback_minutes. Default 6 * 240min = 24h, matching the old
        # 24 * 60min = 24h span from before candle/lookback were split out.
        "baseline_buckets": 6,
    },
    "layer_churn": {
        "top_pct":          0.5,  # A6 — fraction of each side's layers treated as "near-touch"
        "baseline_buckets": 20,   # how many prior cycles' churn scores to average for the self-baseline
        "ratio_threshold":  0.2,  # A6 fires when this cycle's churn drops below this fraction of baseline
    },
    "alerts": {
        # Global suspend duration (minutes). When an operator taps "Suspend" next
        # to a pair in the config drawer, that pair's Telegram alerts are muted for
        # this many minutes. Single global value — every pair's button uses it.
        # The pair keeps being monitored and stays visible on the dashboard; only
        # its Telegram delivery is gagged for the window. Runtime suspend state
        # (per-pair expiry timestamps) lives in suspensions.json, NOT here — this
        # is only the default length applied when a suspend is requested.
        "suspend_minutes": 30,
    },
    "depth_walk": {
        # G1 — USDTNGN depth-walk slippage tracker. Runs as its own 5s task,
        # independent of the main cycle_sleep_seconds loop. Shared weight for
        # both the buy-side (asks) and sell-side (bids) walk.
        "weight_usdt":              100_000,
        # Mid price is computed from a smaller depth-walk (default 1k USDT
        # each side, averaged) rather than raw best_ask/best_bid. This gives a
        # more realistic "fair value" reference for the slippage math — a lone
        # 5-USDT dust order at the touch won't distort mid the way it does with
        # top-of-book. Falls back to top-of-book if either side can't supply
        # even mid_weight_usdt (partial book), and flags the sample.
        "mid_weight_usdt":          1_000,
        "poll_interval_seconds":    5,
        "raw_retention_seconds":    3600,   # length of one in-progress hourly bucket before condensing
        "condensed_retention_days": 365,    # how long condensed hourly averages are kept
        # ── Liquidity uptime (rides the same 5s sample) ──────────────────────
        # Per poll, builds a ±p% band around the same depth-walk mid, where
        # p = n/s*100 — n is the fixed UPTIME_FIXED_STEP_NGN (1₦) step and s is
        # `reference_price` (the ACTIVE target price). The band is applied
        # MULTIPLICATIVELY: ask side counts asks priced <= mid*(1+p/100); bid
        # side counts bids priced >= mid*(1-p/100). So it equals ±1₦ only when
        # mid == s and scales with mid otherwise (lower s ⇒ wider band). Each
        # side scores 1/0 per sample (>= uptime.weight_usdt in band); the hourly
        # condense averages those into a 0..1 decimal per side, persisted and
        # graphed. Denominator is dynamic — only usable polls produce a sample,
        # so a present-but-too-thin book scores 0 (counts as down) while a
        # genuine no-book poll produces no sample and drops out entirely.
        #
        # n (UPTIME_FIXED_STEP_NGN) is a fixed code constant, not a knob. s is
        # configurable; s <= 0 is rejected at load time by falling back to this
        # default s (p = n/s is undefined otherwise).
        "uptime": {
            "reference_price": 1400,      # s — active band target price
            "weight_usdt":     100_000,   # in-band threshold, independent of depth_walk.weight_usdt
        },
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
        ["rndrusdt",     2.0,    {"mexc": "RENDER"} ],
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


def default_config() -> dict:
    """Return a fresh deep copy of DEFAULT_CONFIG (safe to mutate/return)."""
    return copy.deepcopy(DEFAULT_CONFIG)


def merge_config(stored: dict, base: dict | None = None) -> dict:
    """
    Deep-merge a stored/partial config dict over a fresh copy of `base`
    (DEFAULT_CONFIG when base is None).

    Rules (identical to the logic that used to be duplicated across both files):
      - "pairs" is replaced wholesale (it's a list, not a section to merge).
      - any other dict section is shallow-updated key-by-key onto the base, so a
        partial section (e.g. only one pricing knob) keeps the rest of that section.
      - any non-dict / unknown top-level key is replaced wholesale.

    `base` lets the same helper serve two cases with identical semantics:
      - loading from disk:   merge_config(stored)                 # over defaults
      - saving an edit (API): merge_config(body, base=current)    # over current
    """
    merged = copy.deepcopy(DEFAULT_CONFIG if base is None else base)
    for section, values in stored.items():
        if section == "pairs":
            merged["pairs"] = values
        elif isinstance(values, dict) and section in merged:
            merged[section].update(values)
        else:
            merged[section] = values
    return merged