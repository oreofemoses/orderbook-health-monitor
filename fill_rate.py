#!/usr/bin/env python3
"""
fill_rate.py — D2 fill-rate aggregation (shared, import-only).
──────────────────────────────────────────────────────────────────────────────
Turns observed fill EVENTS into an hourly per-market series and a self-baseline,
for the D2 "Fill Rate Deviation" check. Imported by debug.py (the 5s sampling
loop + the per-pair check) and api.py (the dashboard series).

Like defaults.py and kline_volume.py this module is import-only — no side
effects, no environment reads, no async, no fetching — so api.py can import it
without dragging in the monitor engine.

──────────────────────── Why this exists at all ───────────────────────────────
Quidax exposes NO public trades/fills endpoint. Verified 2026-09-14, all 404:
    /markets/{m}/trades          /trades?market={m}
    /markets/{m}/recent_trades   /markets/{m}/fills
    /markets/{m}/history         /markets/{m}/k_with_pending_trades
`/markets/{m}/order_book` looks like a fill feed — it returns individual orders
carrying state/executed_volume/trades_count — but it is a 20-deep top-of-book
sorted by PRICE, padded with FILLED orders from 2018, and it honours neither
paging nor sort (page/limit/order_by/state are all ignored; only ask_limit and
bid_limit do anything). It cannot be walked toward the present. Don't retry it.

So a fill is INFERRED, not read: the 24h rolling `vol` field on
/markets/tickers increases if and only if something traded. One batched call
covers every market on the exchange, which is what makes 5s sampling affordable.

──────────────────────── What "one event" actually means ──────────────────────
NOT one trade. Quidax recomputes its ticker roughly every 5 seconds (measured
2026-09-14 over 103 polls: the server-side `at` field advanced in steps of 5s
x14, 3s x4, 2s x5, 1s x3). Any number of fills landing inside one of those
windows collapses into a single observation. So an "event" is *a ~5s window in
which at least one fill occurred*, and the series is a floor on trade count, not
a count. That ceiling is the exchange's, not the sampler's — polling faster than
~5s buys nothing.

This is still ~3.5-4x finer than the best alternative (1-minute k-line candles
with volume > 0), measured across 36 active markets on the same window. It is
deliberately NOT called a trade count anywhere in this codebase, because the
moment someone reads it as one they will start comparing it against real trade
counts from elsewhere and quietly conclude the feed is broken.

Use `vol`, not `last`, as the trigger: `last` only moves when the PRICE changes,
so a run of fills at one price is invisible to it. Measured on the same sample,
vol-change fired 161 times to last-change's 42 — watching `last` undercounts by
~3.8x.

`vol` is a 24h ROLLING window (btcngn ticker 0.65215112 vs sum-of-24-hourly-
candles 0.651082), so it also DECREASES as old trades age out of the window.
Only increases count as fills; a decrease is the window sliding, not a trade.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# NGT — Lagos, UTC+1, no DST. Same convention as kline_volume.py and debug.py;
# the hourly buckets here must line up with the ones there or the dashboard
# renders two series an hour apart from each other.
NGT = timezone(timedelta(hours=1))

HOUR_MINUTES = 60

# The sampler's own resolution floor, in seconds — see the module docstring.
# Exposed so the config loader can refuse a poll interval below it rather than
# let someone "improve" the feed by hammering the exchange for no gain.
SERVER_TICKER_REFRESH_SECONDS = 5.0


def floor_to_hour(dt: datetime) -> datetime:
    """Round an NGT datetime DOWN to the start of its hour."""
    seconds = HOUR_MINUTES * 60
    epoch = int(dt.timestamp())
    return datetime.fromtimestamp((epoch // seconds) * seconds, tz=NGT)


def merge_points(archive: list, fresh: list) -> list:
    """
    Merge fresh hourly points into an archive, newest value winning per ts, and
    return it sorted ascending and unique by ts. Same contract as debug.py's
    merge_volume_points so both archives behave identically when api.py reads
    them alongside a live fetch.
    """
    by_ts = {p["ts"]: p for p in archive}
    for p in fresh:
        by_ts[p["ts"]] = p
    return [by_ts[k] for k in sorted(by_ts)]


def prune_points(points: list, retention_days: float) -> list:
    """Drop points older than retention_days, measured from the newest point."""
    if not points:
        return points
    newest = datetime.fromisoformat(points[-1]["ts"])
    cutoff = newest - timedelta(days=retention_days)
    return [p for p in points if datetime.fromisoformat(p["ts"]) >= cutoff]


def baseline_from_history(points: list, max_buckets: int) -> tuple[float | None, int]:
    """
    D2 self-baseline: mean events-per-hour over THIS market's own prior CLOSED
    hours. Same "a market is judged against itself, never a global threshold"
    pattern as A6's churn baseline and A4's depth baseline — and for the same
    reason. Fill rates differ by an order of magnitude across the pair list
    (measured: usdtngn ~92 events/hr, aaveusdt ~10), so any fixed cutoff is
    simultaneously deafening on the quiet markets and blind on the busy ones.

    Caller is responsible for passing only CLOSED hours — the in-progress hour
    is a partial count and would drag the baseline toward zero as it is compared
    against itself.
    """
    if not points:
        return None, 0
    window = points[-max_buckets:] if max_buckets > 0 else list(points)
    if not window:
        return None, 0
    return sum(p["events"] for p in window) / len(window), len(window)


def classify_deviation(current: int | None, baseline: float | None,
                       bucket_count: int, min_buckets: int,
                       min_baseline_events: float,
                       ratio_threshold: float) -> tuple[str, str] | None:
    """
    The D2 decision, kept pure so it is testable without a network or a clock.

    Returns (severity, reason) or None. `current` is the trailing-60-minute event
    count; `baseline` the mean over prior closed hours. Comparing a full trailing
    hour against a mean of full hours keeps the two sides the same shape — an
    in-progress partial hour would read as a collapse for 59 minutes out of 60.

    THE min_baseline_events GUARD IS THE LOAD-BEARING PART. Fill arrivals are
    Poisson-ish, so the noise on a count of n is about sqrt(n). On a market
    baselining at 40 events/hr that is +/-6 — a ratio test at 0.35 is nowhere
    near it. On a market baselining at 2/hr the same noise is +/-1.4, i.e. a
    perfectly healthy market routinely prints ratios under 0.35 by chance alone.
    Without this floor D2 would fire forever on exactly the thin markets an
    operator can do least about. Markets below the floor are still reported (the
    dashboard shows their rate) but never alerted on.
    """
    if current is None or baseline is None or bucket_count < min_buckets:
        return None

    if baseline <= 0:
        # No fills across the ENTIRE baseline window. Ratio-vs-baseline can never
        # catch this — the baseline converged to the outage, so the outage looks
        # normal. Identical failure mode to A6's zero-churn case, handled the
        # same explicit way rather than silently passing through.
        #
        # Callers MUST exclude delisted markets before reaching here or this
        # fires permanently on them: a delisted pair keeps a full, healthy-looking
        # book (verified 2026-09-14 — all seven delisted pairs still quoted 25
        # levels a side) while never trading again, so nothing else in the
        # taxonomy can tell it apart from a live market that has stopped filling.
        # That indistinguishability is the whole point of the check and also
        # exactly why the exclusion list cannot be inferred from the API.
        if current <= 0:
            return ("CRITICAL",
                    f"No fills observed across the entire {bucket_count}-hour "
                    f"baseline window — market may already have been dead when "
                    f"monitoring started; no active period available to compare "
                    f"against")
        return None  # baseline 0 but filling now — market just woke up, fine

    if baseline < min_baseline_events:
        return None  # too thin for a ratio to mean anything — see docstring

    ratio = current / baseline
    if ratio >= ratio_threshold:
        return None

    severity = "CRITICAL" if current == 0 else (
        "HIGH" if ratio < ratio_threshold / 2 else "MEDIUM")
    return (severity,
            f"Fill rate {current} events/h vs {baseline:.1f} typical for this "
            f"market (ratio {ratio:.2f}, fires below {ratio_threshold:.2f}, "
            f"{bucket_count}-hour baseline)")
