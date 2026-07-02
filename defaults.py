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
        "candle_minutes":          1,
        "lookback_minutes":        60,
        "volume_baseline_buckets": 24,  # D1 — how many prior windows to average for the baseline
    },
    "volume_spike": {
        "mode":                 "baseline_relative",  # "baseline_relative" | "absolute"
        "spike_ratio":          3.0,    # D1 fires when window volume >= this * the pair's own baseline
        "min_baseline_buckets": 4,      # buckets required before the baseline is trusted enough to gate the trigger
        "warmup_fallback":      "absolute",  # before the baseline is ready: "absolute" | "suppress"
    },
    "layer_churn": {
        "top_pct":          0.5,  # A6 — fraction of each side's layers treated as "near-touch"
        "baseline_buckets": 20,   # how many prior cycles' churn scores to average for the self-baseline
        "ratio_threshold":  0.2,  # A6 fires when this cycle's churn drops below this fraction of baseline
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