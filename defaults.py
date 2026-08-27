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

# How long the USDTNGN hourly base-volume archive is kept, in days. Deliberately
# NOT a config knob: the k-line API clamps every response to 300 candles anchored
# at the newest, so it can only ever serve ~12.5 days of hourly history (see
# kline_volume.py). Everything older exists ONLY in this archive, and letting an
# operator shrink the retention from a dashboard field would permanently destroy
# data nothing can re-fetch. 2 years at hourly resolution is ~17.5k points — a
# couple of MB of JSON, which the Fly volume comfortably holds.
VOLUME_ARCHIVE_RETENTION_DAYS = 730

# Issue ids an operator can acknowledge from the dashboard, i.e. tick to say
# "seen it, stop paging me until it clears". Lives here rather than in either
# process because BOTH need the same answer and for different reasons: debug.py
# sweeps this set each cycle to auto-clear acks whose issue has resolved, while
# api.py validates incoming ack requests against it. Two copies would drift into
# a state where an id can be acked but never auto-cleared (a permanent mute) or
# swept but never settable.
#
# E1/E2 are deliberately absent: both are global infrastructure alerts keyed
# "_global" rather than to a symbol, so there is no market row to tick.
#
# debug.py asserts at import that this covers every id in its tier tables, so
# adding a check there without adding it here fails loudly instead of shipping a
# silently un-acknowledgeable alert.
# A3 and B3 no longer fire (merged into A2 and B1 respectively) but stay in this
# set deliberately. An ack recorded against either before the merge is still
# sitting in alert_acks.json, and run_cycle's auto-clear sweep only retires acks
# whose id it iterates — drop them here and those acks become permanent, muting
# nothing but never going away either.
ACKABLE_ISSUE_IDS = frozenset({
    "A1", "A2", "A3", "A4", "A5", "A6",
    "B1", "B2", "B3", "B4",
    "D1", "F1", "G2",
})


# ── Telegram delivery tiers ───────────────────────────────────────────────────
# Which issues page immediately, which have to repeat, and which never leave the
# dashboard. Here rather than in debug.py for the same reason as
# ACKABLE_ISSUE_IDS: both processes need the same answer. The monitor uses it to
# gate delivery; api.py uses it to let the dashboard filter the log by tier. A
# second copy in api.py would drift the moment a check's tier is retuned, and it
# would drift silently — the dashboard would just quietly mislabel rows.
#
#   Tier 1 — fires on first detection.
#   Tier 2 — must repeat TIER2_CONFIRM_CYCLES consecutive cycles first.
#   Tier 3 — dashboard flag only, never Telegram.
#
# E1/E2 are Tier 1 but absent from these sets: both are keyed "_global" rather
# than to a market and never reach classify_tier, which is per-pair.
#
# The memberships have been retuned twice; the reasons matter more than the
# memberships themselves:
#   A1 -> Tier 3. A crossed book is unambiguous and still shows CRITICAL on the
#         dashboard, but it is the LM bot's own quoting to correct, not something
#         an operator acts on at 3am. Note that TIER3_IDS is tested FIRST in
#         classify_tier, so this silences A1 at every severity.
#   B1 -> Tier 1. A quoted price that has drifted from the trusted reference is
#         the incident an operator actually acts on, and it costs money for every
#         cycle it stands — holding it for three confirmations delayed the one
#         alert worth waking up for. The MEDIUM (peer-flat / merely quiet source)
#         variant is unaffected: the shared MEDIUM rule below is tested before
#         TIER1_IDS, so it still lands in Tier 3.
#   B2 -> Tier 3. Two reference exchanges disagreeing is a data-quality fact, not
#         a market incident: resolve_trusted_price already drops the outlier and
#         keeps pricing running, and if that leaves the comparison wrong, B1 says
#         so at Tier 1. Dashboard visibility is what B2 is for. TIER3_IDS is
#         tested first, so this silences B2 at every severity.
#   D1 -> Tier 3. A volume spike is context rather than an incident — nothing is
#         broken and there is no operator action it implies. It was the noisiest
#         id at Tier 1, and three-cycle confirmation only made it a slower kind of
#         noise, so it is now dashboard-only.
#
# A3 and B3 are absent because they no longer exist as live ids — the 2026-08
# review merged A3 into A2 (an empty side is the extreme of a shallow book) and
# B3 into B1 (an unusable reference is a fact about the price comparison, not a
# separate incident). Both survive in RETIRED_TIERS below so historical log rows
# still classify the way they did when they were written.
TIER1_IDS = frozenset({"A6", "B1"})
# A2 is the only live Tier-2 id left, and it is severity-split, so classify_tier
# answers for it before this set is ever consulted. Kept as the documented home
# of "needs confirming" ids rather than emptied: B4-HIGH and G2-HIGH also land in
# Tier 2 through their own severity splits, and the next id that needs
# confirmation belongs here.
TIER2_IDS = frozenset({"A2"})
TIER3_IDS = frozenset({"A1", "A4", "A5", "B2", "D1", "F1"})


# Ids that no longer fire, mapped to how they used to classify. Existing daily
# logs hold 30 days of A3 and B3 rows, and the dashboard's tier filter classifies
# every row it renders through this function — without these, a historical
# A3:CRITICAL would fall through to the unknown-id default and quietly reclassify
# from Tier 1 to Tier 2, changing what an operator sees when reading back an
# incident. The live checks that replaced them are named in the comment above
# TIER1_IDS. B3's MEDIUM variant is handled by the shared MEDIUM rule below, so
# only its non-MEDIUM tier is recorded here.
RETIRED_TIERS = {"A3": 1, "B3": 2}


def classify_tier(issue_id: str, severity: str) -> int:
    """
    Return 1, 2, or 3 for a given (issue_id, severity) pair.

    Five ids are severity-dependent and cannot be classified from the id alone:
    B4 and G2 are Tier 1 at CRITICAL and Tier 2 otherwise; A2 is Tier 1 at
    CRITICAL (the empty-book case it absorbed from A3, which has to page as fast
    as A3 did) and Tier 2 otherwise; A6 and B1 have MEDIUM variants that exist
    *specifically* to land in Tier 3 (a monitor-only pair with a frozen book; a
    reference source that is merely quiet rather than dead) even though the id
    itself is Tier 1 — which is why the MEDIUM rule is tested BEFORE TIER1_IDS.
    Anything reading tiers off the id alone gets those five wrong.

    Also classifies the retired ids A3 and B3 as they classified when they were
    still live, so historical log rows keep their original tier — see
    RETIRED_TIERS.
    """
    if issue_id in TIER3_IDS:
        return 3
    if issue_id == "B4":
        return 1 if severity == "CRITICAL" else 2
    if issue_id == "G2":
        return 1 if severity == "CRITICAL" else 2
    if issue_id == "A2":
        # CRITICAL is the one-sided book absorbed from A3: an entire side of the
        # market is missing, which is unambiguous and pages on sight. Everything
        # else A2 emits (wide spread, thin layer count) still confirms first.
        return 1 if severity == "CRITICAL" else 2
    if severity == "MEDIUM" and issue_id in ("A6", "B1", "B3"):
        # A6: monitor-only zero-baseline case (see check_layer_churn_stall).
        # B1: an UNCHANGED reference source whose peer is flat or absent (quiet
        # market or single-source asset) — see resolve_trusted_price. Both emit
        # MEDIUM precisely to land here: dashboard visibility, no Telegram noise.
        # B3 is the retired id B1 absorbed that case from.
        return 3
    if issue_id in TIER1_IDS:
        return 1
    if issue_id in TIER2_IDS:
        return 2
    if issue_id in RETIRED_TIERS:
        return RETIRED_TIERS[issue_id]
    # Unknown ids default to Tier 2 (conservative)
    return 2


# Canonical defaults. Every tunable the dashboard can edit has an entry here;
# a stored monitor_config.json overrides these per-key via merge_config().
DEFAULT_CONFIG: dict = {
    "timing": {
        "cycle_sleep_seconds": 60,
    },
    "orderbook": {
        "depth_limit":             200,
        "min_orderbook_layers":    10,
        # A4 — how far this cycle's whole-book depth has to fall below the
        # market's own rolling average before it fires, in percent. 50 means
        # "half the depth this market normally carries". Replaces the old
        # thin_depth_threshold, a flat 5,000 compared against an in-band figure
        # with no currency conversion — so a naira pair was judged against the
        # same bare number as a dollar one, and a market that is simply small
        # fired forever while a large one could halve unnoticed. A self-baseline
        # has neither problem: every market is judged against itself.
        # Clamped to 1..99 at load; 0 would fire on any dip below average and
        # 100+ could only ever fire on a completely empty book.
        "depth_deviation_pct":     50.0,
        # How many prior cycles' depth readings the A4 baseline averages. Matches
        # layer_churn.baseline_buckets (20) deliberately: both are per-cycle
        # self-baselines over the same book, and giving them different memories
        # would make two checks disagree about what "typical" means for the same
        # market at the same moment.
        "depth_baseline_buckets":  20,
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
        # Feeds BOTH B4 (circuit breaker) and G2 (candle wicks) — check_candle_wicks
        # reads the same kline_raw as check_circuit_breaker_proximity, so these two
        # values set the window for both. D1 is the one that's decoupled: it has its
        # own independent candle_minutes/lookback_minutes under volume_spike, and
        # changing these no longer touches D1's window or vice versa.
        #
        # 60/240 rather than the original 1/60: at a 60-minute window an anomalous
        # print aged out of G2's view about an hour after it happened, so an
        # operator looking shortly afterwards saw nothing. 240 minutes keeps it
        # visible for four hours.
        #
        # The cost of the 60-minute CANDLE is real and worth knowing: G2 measures
        # (high - low) / open per candle, so a brief wick is now weighed against a
        # whole hour's range instead of one minute's. Small anomalies that would
        # clear 5% of a 1-minute candle can be diluted below the threshold. If that
        # matters more than window length, keep lookback_minutes at 240 and put
        # candle_minutes back to 1 — 240 one-minute candles, same reach, full
        # resolution, at the cost of a larger API response per pair per cycle.
        "candle_minutes":   60,
        "lookback_minutes": 240,
    },
    "g2": {
        # G2 — candle wick / anomalous print detector. Reuses B4's own k-line
        # feed (kline.candle_minutes / kline.lookback_minutes above) — no
        # separate API call. Scans every candle in the window each cycle:
        #   low <= 0                    -> CRITICAL, always, regardless of pct
        #   (high-low)/open*100 >= this -> HIGH
        "swing_pct": 5.0,
        # Max Telegram deliveries per G2 "episode" for a pair. An anomalous candle
        # lingers in B4's k-line window (kline.lookback_minutes) for its whole life,
        # and G2 re-detects it every cycle — so without a cap it re-fires once per
        # cooldown for the full window. This caps deliveries at N per episode (fire
        # on detection, then one final fire after the cooldown), after which G2 stays
        # dashboard-visible but silent until the window clears of anomalies and the
        # episode re-arms. Counts confirmed deliveries, not attempts.
        "max_fires": 2,
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
        # Max Telegram deliveries per D1 "episode" for a pair. A volume spike stays
        # elevated in the rolling window (lookback_minutes — 4h at the defaults) and
        # D1 re-detects it every cycle, so without a cap it re-fires once per cooldown
        # for the window's whole life. Caps deliveries at N per episode (fire on
        # detection, then one final fire after the cooldown), after which D1 stays
        # dashboard-visible but silent until the window clears and the episode
        # re-arms. Counts confirmed deliveries, not attempts. Mirrors g2.max_fires.
        "max_fires": 2,
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