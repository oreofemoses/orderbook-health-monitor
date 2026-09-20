#!/usr/bin/env python3
"""
baseline.py — robust, time-of-day-aware self-baselines (shared, import-only).
──────────────────────────────────────────────────────────────────────────────
Turns an hourly per-market series into "what is normal for THIS market at THIS
hour", for D1 (volume spike) and D2 (fill-rate deviation). Imported by debug.py
(the monitor's two checks) and api.py (the dashboard's /api/fill-rate), so the
number an operator reads is the number the verdict was made on.

Like defaults.py, fill_rate.py and kline_volume.py this module is import-only —
no side effects, no environment reads, no async, no fetching, and no clock of
its own (every entry point takes `now`) — so api.py can import it without
dragging in the monitor engine, and so every decision here is testable without
a network.

──────────────────────── Why this replaces a 24h mean ─────────────────────────
Both checks used to average their last ~24 hours of readings, ALL HOURS POOLED.
That is the documented cause of D1's noise: on a pair whose trading concentrates
in business hours, the peak window is structurally several times the daily mean,
so D1 fired every day on a perfectly healthy market and no amount of
confirmation could filter it — the signal really was present, for hours.

Comparing an hour against the same hour on prior days removes that entirely. A
16:00 reading is judged against this market's own prior 16:00s, so the daily
shape cancels instead of being mistaken for an anomaly.

──────────────────────── Why the MEDIAN, not the mean ─────────────────────────
The longer window and the robust statistic are ONE decision, not two. Under a
mean, a single past spike pollutes the baseline for the whole window — at 24
hours that is a day, at 30 days it would be a month, which is strictly WORSE
than what it replaces. The median is what makes a 30-day window safe: a spike is
one sample among ~30 and moves the middle one barely at all.

MAD (median absolute deviation) rides along, REPORTED BUT NEVER FIRED ON, so a
robust z-score can be watched against real data before anything depends on it.
Same staged approach D2 itself is under. `mad` is None when it computes to zero
— see mad() for why that is a contract and not an oversight.

──────────────────────── Hour-of-day only, for now ────────────────────────────
Deliberately NOT hour-of-week, and deliberately NOT split by weekday/weekend.

Hour-of-week is 168 slots; 30 days gives ~4.3 samples each, and a median of 4
points is barely more robust than a mean of 4 — it would spend its life on the
fallback rungs.

A weekday/weekend split is the better idea of the two and is still worth doing
LATER, once 30 days of archive genuinely exists. It cannot be done at launch:
the k-line endpoint backfills only ~12.5 days (see kline_volume.py), which is
~3.6 weekend samples per hour slot. Weekend slots would sit below any sensible
minimum for about four calendar weeks and fall back to a weekday-dominated
pooled median — i.e. weekend baselines would run HIGH and D1 would go quieter on
weekends for a month, which is the exact opposite of the point. Shipping 24
clean slots that clear their minimum on day one beats 48 slots that half work.

──────────────────────── The fallback ladder ──────────────────────────────────
Slots go thin (new market, sparse pair, cold start), so the baseline resolves
down a ladder and REPORTS WHICH RUNG IT LANDED ON. That reporting is
load-bearing, not decoration: nothing in this codebase renders a bare number
with no provenance, and "3.2x the typical 16:00, median of 21 days" earns trust
that "3.2x typical" does not.

    seasonal     median of this hour-of-day slot        >= min_slot_samples
    flat_median  median of the whole retained window    >= min_flat_samples
    sparse       the resolved median is exactly 0       (see below)
    none         not enough history — caller falls back to its own legacy mean

`sparse` exists because the median has a failure mode the mean does not. Under a
mean, one non-zero hour in the window makes the baseline positive. Under a
median, a market that trades in fewer than half of its hours has a median of
EXACTLY ZERO — and both callers gate on `baseline > 0`, so it would silently pin
itself to a fallback forever while the UI claimed the baseline was "still
building". It is not building; it is never going to be positive. Say so.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import NamedTuple

# NGT — Lagos, UTC+1, no DST. MUST match fill_rate.py, kline_volume.py and
# debug.py exactly. A time-of-day baseline computed on the wrong hour is not a
# degraded baseline, it is a broken one — and it would present as noise rather
# than as a bug, which is the hardest kind to find. Hence the hard rejection of
# naive datetimes below rather than a convenient assumption about them.
NGT = timezone(timedelta(hours=1))

# Ladder rungs, as the `method` field reports them. Exported as names so call
# sites compare against these rather than re-typing the strings.
SEASONAL    = "seasonal"
FLAT_MEDIAN = "flat_median"
SPARSE      = "sparse"
NONE        = "none"

# Values for the `baseline_model` config key. "flat_mean" is the rollback lever:
# it makes resolve() decline immediately so the caller uses its own pre-existing
# mean, with no deploy needed.
MODEL_SEASONAL  = "seasonal_median"
MODEL_FLAT_MEAN = "flat_mean"
VALID_MODELS    = (MODEL_SEASONAL, MODEL_FLAT_MEAN)


class BaselineResult(NamedTuple):
    """
    What the ladder resolved to, and how. `value` is None exactly when `method`
    is NONE. `samples` is how many readings the statistic was built from — in
    SLOT DAYS for `seasonal`, in readings for `flat_median` — which is why
    callers must not reuse their old hour-count gates against it. Six hours and
    six same-slot days are wildly different amounts of evidence.
    """
    value:   float | None
    method:  str
    samples: int
    mad:     float | None
    slot:    int | None      # hour-of-day the value describes; None off-slot


def _as_ngt(dt: datetime) -> datetime:
    """
    Localise to NGT, refusing naive datetimes.

    A naive datetime here would be silently read as whatever the host's local
    zone is — UTC on Fly, which is one hour off NGT. Every slot would shift by
    an hour and the baseline would quietly describe the wrong part of the day.
    There is no safe assumption available, so this raises instead of guessing.
    """
    if dt.tzinfo is None:
        raise ValueError("baseline.py requires timezone-aware datetimes (a naive "
                         "one would be read as host-local, which is UTC on Fly — "
                         "one hour off NGT, and every slot wrong)")
    return dt.astimezone(NGT)


def median(xs: list[float]) -> float | None:
    """Middle value, averaging the two middles on an even count. None if empty."""
    if not xs:
        return None
    ordered = sorted(float(x) for x in xs)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def mad(xs: list[float], centre: float) -> float | None:
    """
    Median absolute deviation about `centre` — the robust answer to "how much
    does this market normally vary", as the median is to "what is normal".

    RETURNS None WHEN IT COMPUTES TO ZERO, deliberately, rather than a real 0.0.
    A zero MAD is common here, not exotic: any slot in which more than half the
    samples are identical produces one, which on a quiet market is most slots.
    Every consumer of MAD divides by it — a robust z-score is
    |x - median| / (1.4826 * MAD) — so handing back 0.0 hands a division by zero
    to whoever wires that up later. None forces the question to be answered at
    the call site, once, instead of crashing there.
    """
    if not xs:
        return None
    result = median([abs(float(x) - centre) for x in xs])
    if result is None or result <= 0:
        return None
    return result


def slot_key(ts: datetime) -> int:
    """The NGT hour-of-day a reading belongs to, 0-23."""
    return _as_ngt(ts).hour


def last_closed_hour(now: datetime) -> datetime:
    """
    The most recent hour that has fully elapsed, in NGT.

    THIS IS THE ANCHOR FOR BOTH SIDES OF EVERY COMPARISON, and getting it wrong
    by one is the likeliest way to break this feature invisibly. D1's live
    measurement already works this way: fetch_kline_volume floors to the current
    candle boundary and steps back `lookback_minutes`, so at 14:37 NGT its window
    is hours 10..13 — the in-progress hour 14 has no candle and is not in it. A
    baseline keyed on the wall-clock hour would compare that window against a
    slot one hour later than the one it actually covers, every single time.
    """
    ngt = _as_ngt(now)
    return ngt.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)


def window_samples(points: list, value_key: str,
                   window_hours: int) -> list[tuple[datetime, float]]:
    """
    Collapse an hourly series into the quantity each check actually compares,
    as (end_hour, value) pairs.

    D2 compares a single trailing hour, so window_hours=1 and this is the series
    unchanged. D1 compares a 240-minute ROLLING WINDOW TOTAL, so window_hours=4
    and each sample is the sum of four consecutive hours, keyed by the last of
    them. Building D1's samples as window totals is what keeps both sides of its
    comparison the same shape. The alternative — summing four per-hour medians —
    is a different and wrong number, because the median of a sum is not the sum
    of the medians, and it would silently recalibrate spike_ratio and the
    absolute floor against a quantity neither was ever tuned for.

    A GAP BREAKS A WINDOW rather than shortening it. A sum over three of four
    hours is an undercount of unknown size, and undercounts in a baseline bias
    it downward — which for D1 means everything starts looking like a spike, and
    for D2 means a real collapse looks proportionally smaller. Dropping the
    sample costs one day from one slot; keeping it corrupts the statistic.
    """
    if window_hours < 1:
        raise ValueError(f"window_hours must be >= 1, got {window_hours}")

    parsed: list[tuple[datetime, float]] = []
    for p in points:
        try:
            ts = _as_ngt(datetime.fromisoformat(p["ts"]))
            parsed.append((ts, float(p[value_key])))
        except (KeyError, TypeError, ValueError):
            continue        # malformed point — skip, never crash a live cycle
    parsed.sort(key=lambda pair: pair[0])

    if window_hours == 1:
        return parsed

    by_hour = {ts: val for ts, val in parsed}
    step = timedelta(hours=1)
    out: list[tuple[datetime, float]] = []
    for end_ts, _ in parsed:
        total = 0.0
        for back in range(window_hours):
            hour = end_ts - step * back
            if hour not in by_hour:
                break                       # gap — drop this window entirely
            total += by_hour[hour]
        else:
            out.append((end_ts, total))
    return out


def resolve(points: list, value_key: str, now: datetime, *,
            window_hours: int = 1,
            baseline_days: float = 30.0,
            min_slot_samples: int = 8,
            min_flat_samples: int = 6,
            model: str = MODEL_SEASONAL) -> BaselineResult:
    """
    Walk the ladder and report where it landed. See the module docstring.

    `now` is passed in rather than read so a backtest can replay a real archive
    hour by hour without patching a clock.

    Returns method=NONE when there is not enough history to say anything. The
    caller then falls back to its own legacy mean (D1's update_volume_baseline,
    D2's baseline_from_history), both of which are left intact precisely so this
    can never be worse than what it replaces during warm-up or after a wipe.
    """
    if model == MODEL_FLAT_MEAN:
        # The rollback lever, flipped from the config drawer with no deploy.
        return BaselineResult(None, NONE, 0, None, None)

    samples = window_samples(points, value_key, window_hours)
    if not samples:
        return BaselineResult(None, NONE, 0, None, None)

    anchor = last_closed_hour(now)
    cutoff = anchor - timedelta(days=baseline_days)
    # Strictly BEFORE the anchor: the hour being judged must never contribute to
    # the baseline it is judged against. Same "exclude the current reading from
    # its own baseline" invariant as A4's depth baseline, A6's churn baseline and
    # both of the legacy means this sits in front of — a reading folded into its
    # own reference can never look anomalous enough.
    in_window = [(ts, val) for ts, val in samples if cutoff <= ts < anchor]
    if not in_window:
        return BaselineResult(None, NONE, 0, None, None)

    target = slot_key(anchor)
    slot_values = [val for ts, val in in_window if slot_key(ts) == target]

    if len(slot_values) >= min_slot_samples:
        centre = median(slot_values)
        method = SPARSE if (centre is not None and centre <= 0) else SEASONAL
        return BaselineResult(centre, method, len(slot_values),
                              mad(slot_values, centre), target)

    flat_values = [val for _, val in in_window]
    if len(flat_values) >= min_flat_samples:
        centre = median(flat_values)
        method = SPARSE if (centre is not None and centre <= 0) else FLAT_MEDIAN
        return BaselineResult(centre, method, len(flat_values),
                              mad(flat_values, centre), None)

    return BaselineResult(None, NONE, len(flat_values), None, None)


def describe(result: BaselineResult) -> str:
    """
    One short clause naming what the baseline actually is, for alert labels and
    dashboard rows. Written here rather than at each call site so Telegram and
    the dashboard cannot drift into describing the same number differently.

    The old wording said "N-hour baseline" everywhere. Under a slot median that
    is simply false — it is N DAYS AT THIS HOUR — and these strings go straight
    to Telegram, so leaving them would be an operator-facing lie rather than a
    cosmetic slip.
    """
    if result.method == SEASONAL:
        # `slot` is carried by resolve() but not by every caller that
        # reconstructs a result just to describe it, so name the hour only when
        # it is actually known rather than formatting a None into the string.
        at = f" at {result.slot:02d}:00" if result.slot is not None else " at this hour"
        return f"median of {result.samples} prior days{at}"
    if result.method == FLAT_MEDIAN:
        return f"median of {result.samples} recent readings"
    if result.method == SPARSE:
        return (f"trades too rarely for a median — {result.samples} readings, "
                f"over half of them zero")
    return "no baseline yet"
