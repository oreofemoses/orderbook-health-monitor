#!/usr/bin/env python3
"""
kline_volume.py — Quidax k-line volume aggregation (shared, import-only).
──────────────────────────────────────────────────────────────────────────────
Pulls Quidax candles for a market and sums their BASE volume over a window,
bucketed by NGT hour. Used by three callers:

  • api.py            — on-demand hourly series + trailing-60m total for the
                        USDTNGN dashboard tab (blocking urllib, run in FastAPI's
                        threadpool — every route here is a sync `def`).
  • debug.py          — the hourly archive loop, which imports only the PURE
                        helpers and does its own fetching over aiohttp.
  • the CLI at the
    bottom of this file — `python kline_volume.py ethngn 2026-07-10 2026-07-13`

NOTE: like defaults.py, this module is import-only — no side effects, no
environment reads, no async work — so api.py can import it without dragging in
the monitor engine (debug.py runs apply_config() and builds an asyncio.Lock at
import time; importing it from the API process would execute both).

Volume here is BASE volume (USDT for usdtngn) — the candle's raw index-5 field,
NOT multiplied by close. That is deliberately different from debug.py's
compute_window_volume, which reports D1's QUOTE volume (volume × close, in naira)
for spike thresholds. Two different numbers with two different jobs; don't
"reconcile" them.

────────────────────────── How the Quidax k-line API works ────────────────────
  GET /exchange-open-api/api/v1/markets/{market}/k?period=60&timestamp=<s>&limit=<n>

  • period    — candle size in MINUTES (60 = hourly). The docs list 1, 5, 15, 30,
                60, 120, 240, 360, 720, 1440, 4320 and 10080 as valid. ONLY THE
                FIRST FIVE ARE IMPLEMENTED. Verified 2026-08-13 on usdtngn and
                btcusdt: 1/5/15/30/60 return candles with the correct step, while
                120 and everything above return HTTP 200, status "success",
                message "Successful" — and an EMPTY data array.

                That failure mode is the dangerous part: asking for daily candles
                does not raise, it just silently yields no volume at all. So 60 is
                the coarsest candle available, and there is no way to trade
                resolution for reach (see the 300-row clamp below).
  • timestamp — SECONDS since epoch (NOT ms). An EXCLUSIVE lower bound: only
                candles strictly after this time come back, which is why every
                call site here subtracts 1s to make the boundary candle inclusive.
  • limit     — how many candles, newest-first. The docs say the max is 10000.
                THAT IS NOT TRUE — see the cap below.
  • Response  — rows ordered LATEST → EARLIEST, each:
                    [ts_ms, open, close, high, low, vol]   (OCHLV, ts in ms)

  Crucial behaviour #1 — the response is always ANCHORED AT THE MOST RECENT CLOSED
  candle and walks backward. `timestamp` only trims the old end; it does NOT move
  the anchor. Verified: asking for limit=100 with a timestamp 800 hours back
  returns the newest 100 candles, not the 100 following that timestamp. So the
  window you can see always ENDS at now — there is no way to page backwards, and
  no `end`/`to` parameter to do it with.

  Crucial behaviour #2 — the server silently CLAMPS limit to 300 regardless of
  what you ask for, at every period tested (1, 5 and 60 minutes all returned
  exactly 300 rows for limit=500). The documented 10000 is fiction. Combined with
  the anchor above, that means ONE CALL REACHES EXACTLY 300 PERIODS BACK AND NO
  FURTHER — about 12.5 days at hourly candles, ~5 hours at 1-minute candles.

  Consequence: anything older than 300 hours ago is permanently unfetchable from
  this endpoint. Callers get `reachable=False` and the oldest hour that WAS
  reachable, rather than a silently short series. debug.py's hourly archive
  exists precisely to accumulate history past this wall — for windows longer than
  ~12.5 days it is the ONLY source, so the archive is load-bearing, not a cache.

  The IN-PROGRESS candle is never returned, at any period — verified empirically:
  at 11:41 NGT the newest 60m candle is 10:00, and the newest 5m candle is 11:35.
  So an "hour so far" figure cannot come from the hourly feed; it has to be
  rebuilt from finer candles, which is what the trailing-60m helper does with
  1-minute candles.

  Zero-trade periods still return a candle (price carried forward, vol "0"), so a
  genuinely MISSING candle indicates a real data/API hole, not just a quiet hour —
  which is why the gap report is worth reading.
"""

import argparse
import json
import math
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE_API_URL = "https://openapi.quidax.io/exchange-open-api/api/v1"
# NOTE: urllib's default User-Agent ("Python-urllib/x.y") is blocked by the WAF in
# front of openapi.quidax.io and comes back 403 Forbidden. aiohttp (used by the
# main monitor) sends its own UA and isn't blocked, which is why debug.py works and
# a bare urllib call doesn't. A browser-like UA gets us past the filter.
API_HEADERS = {
    "accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}

HOUR_MINUTES = 60            # the dashboard series is hourly by design
# Documented ceiling for the `limit` param. We ask for what the docs allow; the
# server currently returns far less (see OBSERVED_MAX_CANDLES), but asking for
# more is not an error, and sizing requests from this rather than the observed
# clamp means the code automatically picks up the full window if Quidax ever
# honours the documented value.
MAX_LIMIT = 10000
# What the server ACTUALLY returns per response, measured 2026-08-13: exactly 300
# rows for limit=500/1000/2000, on usdtngn, btcusdt and ethngn alike, all with the
# same oldest timestamp — so it's a server clamp, not per-market data availability.
# Used only for estimates and operator-facing messages; real truncation is
# detected from the response itself, never assumed from this number.
OBSERVED_MAX_CANDLES = 300
FETCH_BUFFER_CANDLES = 2     # small cushion so boundary rounding never clips the start

NGT = timezone(timedelta(hours=1))   # Lagos, UTC+1, no DST
UTC = timezone.utc

# Quote currencies Quidax quotes against — used only to label the base asset in
# the output (e.g. ethngn -> "ETH"). Order doesn't matter; suffixes are distinct.
KNOWN_QUOTES = ("usdt", "ngn", "ghs")


def split_base_quote(market: str) -> tuple[str, str]:
    """ethngn -> ('ETH', 'NGN'). Falls back to (MARKET, '') if no known quote."""
    m = market.lower()
    for q in KNOWN_QUOTES:
        if m.endswith(q) and len(m) > len(q):
            return m[: -len(q)].upper(), q.upper()
    return m.upper(), ""


def floor_to_period(dt: datetime, period_minutes: int) -> datetime:
    """
    Round an NGT datetime DOWN to the start of its candle period.

    Quidax buckets candles to fixed clock boundaries (a 60-min candle starts at
    :00, not "60 minutes ago"), so every window bound has to be snapped the same
    way or the boundary candle falls outside the filter and silently vanishes.
    Same reasoning as debug.py's fetch_kline_volume alignment.
    """
    seconds = period_minutes * 60
    epoch = int(dt.timestamp())
    return datetime.fromtimestamp((epoch // seconds) * seconds, tz=NGT)


def size_limit(start_dt: datetime, period_minutes: int,
               now: datetime | None = None) -> int:
    """
    The `limit` to request for a window starting at `start_dt`. 0 means the window
    is entirely in the future and there is nothing to ask for.

    The response anchors at the newest candle regardless of `timestamp`, so limit
    must span now → start, not the length of the requested window.

    Whether the server actually honours that is a separate question, answered by
    inspecting the response — see hourly_series. This function only decides what
    to ask for.
    """
    now = now or datetime.now(tz=NGT)
    span = (now - start_dt).total_seconds() / (period_minutes * 60)
    periods = math.ceil(span)
    if periods < 0:
        return 0
    return min(periods + FETCH_BUFFER_CANDLES, MAX_LIMIT)


def fetch_klines(market: str, period_minutes: int, timestamp_s: int,
                 limit: int, timeout: int = 30) -> list:
    """
    Single blocking GET against the k-line endpoint. Returns the raw `data` list
    (latest-first) or raises RuntimeError with a readable message.

    Blocking on purpose: api.py's routes are all sync `def`, so FastAPI runs them
    in its threadpool and this never touches the event loop. debug.py must NOT
    call this — it has its own aiohttp path (see fetch_kline_volume_hourly).
    """
    url = (
        f"{BASE_API_URL}/markets/{market}/k"
        f"?period={period_minutes}&timestamp={timestamp_s}&limit={limit}"
    )
    req = urllib.request.Request(url, headers=API_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} from Quidax: {e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach Quidax: {e.reason}") from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Quidax returned non-JSON: {e}") from e

    if payload.get("status") != "success":
        raise RuntimeError(f"Quidax error: {payload.get('message', 'unknown')}")
    return payload.get("data") or []


def parse_candle(candle) -> tuple[datetime, float] | None:
    """
    One row -> (open time in NGT, BASE volume). None for a malformed row.

    Encodes the field-order convention in exactly one place: Quidax returns
    [ts_ms, open, CLOSE, high, low, volume] — OCHLV, not the more common OHLCV.
    Volume is index 5 either way, but keeping the unpack explicit means a future
    reader adding price logic here can't repeat the bug debug.py's
    compute_window_volume once had (quoting volume against `low` instead of
    `close`).
    """
    try:
        ts_ms, _open, _close, _high, _low, volume = candle[:6]
        return (datetime.fromtimestamp(int(ts_ms) / 1000, tz=UTC).astimezone(NGT),
                float(volume))
    except (IndexError, ValueError, TypeError):
        return None


def aggregate(rows: list, start_dt: datetime, end_excl: datetime,
              period_minutes: int = HOUR_MINUTES) -> dict:
    """
    Pure aggregation — no network. `start_dt`/`end_excl` are NGT-aware datetimes
    defining the half-open window [start_dt, end_excl). A candle belongs to the
    window if its OPEN time (in NGT) falls in that interval.

    Returns the total base volume, the per-period series (chronological, only
    periods actually returned), per-day sums/counts, and the exact set of
    expected-but-missing candle opens as NGT datetimes.
    """
    step = timedelta(minutes=period_minutes)

    # Every candle-open we expect in the window, snapped to period boundaries.
    expected_opens = set()
    t = floor_to_period(start_dt, period_minutes)
    if t < start_dt:
        t += step               # start wasn't on a boundary — first full period follows
    while t < end_excl:
        expected_opens.add(t)
        t += step

    total = 0.0
    by_open: dict[datetime, float] = {}
    per_day_vol: dict[str, float] = {}
    per_day_count: dict[str, int] = {}

    for candle in rows:
        parsed = parse_candle(candle)
        if parsed is None:
            continue            # malformed row — skip, don't crash the run
        open_ngt, vol = parsed
        if not (start_dt <= open_ngt < end_excl):
            continue            # outside the requested window (older tail or newer than end)
        if open_ngt in by_open:
            continue            # de-dupe any overlap defensively
        by_open[open_ngt] = vol

        day_key = open_ngt.strftime("%Y-%m-%d")
        total += vol
        per_day_vol[day_key] = per_day_vol.get(day_key, 0.0) + vol
        per_day_count[day_key] = per_day_count.get(day_key, 0) + 1

    series = [{"ts": ts.isoformat(), "volume": by_open[ts]} for ts in sorted(by_open)]
    return {
        "total": total,
        "series": series,
        "per_day_vol": per_day_vol,
        "per_day_count": per_day_count,
        "expected_count": len(expected_opens),
        "retrieved_count": len(by_open),
        "missing_opens": sorted(expected_opens - set(by_open)),
    }


def hourly_series(market: str, start_dt: datetime, end_excl: datetime,
                  now: datetime | None = None) -> dict:
    """
    Fetch + aggregate one hourly window in a single call. Returns the aggregate()
    dict plus `reachable` and `truncated_from` — the oldest hour the server was
    actually willing to return, so a caller can say which part of the window is
    missing rather than just that something is.

    Truncation is MEASURED, not assumed: `reachable` compares the oldest candle in
    the response against the requested start. That's deliberate — the server
    clamps responses well below the documented limit (currently 300 rows), and
    hard-coding that number would silently under-report the day it changes in
    either direction. If the clamp is lifted, this starts returning full windows
    with no code change; if it tightens, the flag notices immediately.
    """
    now = now or datetime.now(tz=NGT)
    limit = size_limit(start_dt, HOUR_MINUTES, now)
    if limit <= 0:
        empty = aggregate([], start_dt, end_excl, HOUR_MINUTES)
        empty.update({"reachable": True, "truncated_from": None})
        return empty

    # `timestamp` is an EXCLUSIVE lower bound — passing the start candle's exact
    # open drops that candle. Nudge back 1s so the boundary candle is included;
    # aggregate() still trims anything genuinely before start_dt, so the extra
    # second can never pull in an out-of-window candle.
    timestamp_s = int(start_dt.timestamp()) - 1
    rows = fetch_klines(market, HOUR_MINUTES, timestamp_s, limit)

    result = aggregate(rows, start_dt, end_excl, HOUR_MINUTES)

    # Oldest candle in the RAW response — not the aggregated window, which has
    # already been trimmed to [start, end) and so can never reveal that the
    # server refused to go back as far as we asked.
    opens = [p[0] for p in (parse_candle(c) for c in rows) if p is not None]
    oldest = min(opens) if opens else None

    if oldest is None or oldest <= start_dt:
        result["reachable"] = True
        result["truncated_from"] = None
    else:
        result["reachable"] = False
        result["truncated_from"] = oldest.isoformat()
    return result


def trailing_window_total(market: str, minutes: int = 60,
                          now: datetime | None = None) -> dict:
    """
    Base volume over the trailing `minutes`, rebuilt from 1-MINUTE candles.

    Why 1-minute candles: the hourly feed has no in-progress candle (the 11:00
    candle doesn't exist until 12:00), so a trailing hour is unreachable at
    period=60. Minute candles give the same total with at most ~1 minute of lag —
    the currently-open minute is excluded, which is why `end` is floored to the
    minute rather than being `now`.

    Returns {volume, start, end, expected_count, retrieved_count} — the counts let
    the dashboard distinguish "quiet hour" from "the feed dropped candles", since
    a zero-trade minute still returns a candle with vol 0.
    """
    now = now or datetime.now(tz=NGT)
    end_excl = floor_to_period(now, 1)
    start_dt = end_excl - timedelta(minutes=minutes)

    limit = size_limit(start_dt, 1, now)
    rows = fetch_klines(market, 1, int(start_dt.timestamp()) - 1, limit)
    result = aggregate(rows, start_dt, end_excl, 1)
    return {
        "volume": result["total"],
        "start": start_dt.isoformat(),
        "end": end_excl.isoformat(),
        "expected_count": result["expected_count"],
        "retrieved_count": result["retrieved_count"],
    }


def parse_day(s: str) -> datetime:
    """'YYYY-MM-DD' -> NGT-aware midnight datetime."""
    d = datetime.strptime(s, "%Y-%m-%d")
    return datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=NGT)


# ══════════════════════════════════════════════════════════════════════════════
# CLI — unchanged behaviour: aggregate a day range and print a gap report.
# Nothing above this line runs on import.
# ══════════════════════════════════════════════════════════════════════════════

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Aggregate Quidax hourly candle volume over a NGT day range.",
    )
    ap.add_argument("market", help="e.g. ethngn, btcusdt, usdtngn")
    ap.add_argument("start", help="start day, inclusive (YYYY-MM-DD, NGT)")
    ap.add_argument("end", help="end day, inclusive (YYYY-MM-DD, NGT)")
    args = ap.parse_args(argv)

    market = args.market.lower()
    try:
        start_dt = parse_day(args.start)
        end_day = parse_day(args.end)
    except ValueError:
        print("✗ Dates must be YYYY-MM-DD (e.g. 2026-07-10).", file=sys.stderr)
        return 2
    if end_day < start_dt:
        print("✗ End day is before start day.", file=sys.stderr)
        return 2

    end_excl = end_day + timedelta(days=1)   # inclusive end -> exclusive upper bound
    base, quote = split_base_quote(market)
    unit = base if base else "base units"

    now = datetime.now(tz=NGT)
    if start_dt > now:
        print("✗ Start day is in the future — nothing to fetch.", file=sys.stderr)
        return 2

    span_days = (end_excl - start_dt).days
    print(f"{market.upper()} — hourly volume aggregation")
    print(f"Timezone : Lagos (NGT, UTC+1)")
    print(f"Window   : {start_dt:%Y-%m-%d %H:%M} → {end_day:%Y-%m-%d} 23:00 NGT "
          f"(inclusive, {span_days} day{'s' if span_days != 1 else ''})")

    try:
        result = hourly_series(market, start_dt, end_excl, now=now)
    except RuntimeError as e:
        print(f"\n✗ {e}", file=sys.stderr)
        return 1

    if not result["reachable"]:
        earliest = datetime.fromisoformat(result["truncated_from"])
        print()
        print("⚠️  This range starts further back than the API will reach in one call.")
        print("    The endpoint is anchored at the newest candle and clamps the")
        print(f"    response (currently ~{OBSERVED_MAX_CANDLES} candles, whatever the "
              f"requested limit),")
        print(f"    so nothing before {earliest:%Y-%m-%d %H:%M} NGT came back. The "
              f"total below")
        print("    is missing the earlier part of the window.")

    if result["retrieved_count"] == 0:
        # Distinguish "this window is entirely past the wall" from "this market
        # doesn't exist" — both return nothing, but only one is a user error.
        if not result["reachable"]:
            print("\n✗ The whole requested window is older than the API will "
                  "serve — no candles fall inside it.", file=sys.stderr)
            print("  Nothing before "
                  f"{datetime.fromisoformat(result['truncated_from']):%Y-%m-%d %H:%M}"
                  " NGT is retrievable.", file=sys.stderr)
        else:
            print("\n✗ No candles returned. Check the market symbol is valid.",
                  file=sys.stderr)
        return 1

    # ── Report ──────────────────────────────────────────────────────────────────
    print()
    print(f"Expected candles : {result['expected_count']}")
    print(f"Retrieved        : {result['retrieved_count']}")

    missing = result["missing_opens"]
    if missing:
        # Split on the candle's END, not its open: the in-progress hour has an
        # open in the past but isn't a data hole — it simply hasn't closed yet,
        # and the API never returns an in-progress candle at any period.
        hour = timedelta(minutes=HOUR_MINUTES)
        future = [m for m in missing if m + hour > now]
        past = [m for m in missing if m + hour <= now]
        print(f"Missing          : {len(missing)}"
              + (f"  ({len(past)} in the past, {len(future)} not yet traded)"
                 if future else ""))
        if past:
            shown = ", ".join(m.strftime("%Y-%m-%d %H:%M") for m in past[:10])
            more = f" …and {len(past) - 10} more" if len(past) > 10 else ""
            print(f"  gaps (NGT)     : {shown}{more}")
    else:
        print(f"Missing          : 0")

    print()
    print(f"Per-day volume ({unit}):")
    # Walk every day in the requested span so zero/absent days still show.
    d = start_dt
    while d < end_excl:
        key = d.strftime("%Y-%m-%d")
        vol = result["per_day_vol"].get(key, 0.0)
        cnt = result["per_day_count"].get(key, 0)
        print(f"  {key}   {vol:>18.8f}   ({cnt}/24 candles)")
        d += timedelta(days=1)

    print()
    print(f"TOTAL: {result['total']:.8f} {unit}  "
          f"over {result['retrieved_count']} candles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
