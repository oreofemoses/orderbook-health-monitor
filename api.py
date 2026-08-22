"""
Quidax Market Monitor — Dashboard API
--------------------------------------
Serves latest.csv + daily log + state as JSON for the dashboard.

Run:
    uvicorn api:app --host 0.0.0.0 --port 8000 --reload

Endpoints:
    GET /api/status          → latest.csv parsed as JSON (all pairs, current cycle)
    GET /api/history         → daily log CSV as JSON; optional ?date=YYYY-MM-DD (defaults to today)
    GET /api/alert-analysis  → range analytics over the daily logs; ?start=&end=&gap_cycles=
    GET /api/alert-log       → daily-log detections for a range, filtered by tier/market/issue
    GET /api/diagnostics     → which writer last wrote what, and how long ago
    GET /api/state           → raw health_state.json (anomaly timers, cooldowns)
    GET /api/pairs           → configured pair symbols + targets from health_state
    GET /health              → simple liveness check
    GET /                    → serves dashboard.html from same directory
"""

import json
import math
import os
import glob
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response

from defaults import (default_config, merge_config, UPTIME_FIXED_STEP_NGN,
                      ACKABLE_ISSUE_IDS,
                      classify_tier)  # single source of truth for config
# Candle fetching + aggregation for the USDTNGN volume endpoints. Import-only
# module (no side effects, no async), shared with debug.py so the OCHLV field
# convention and NGT hour bucketing stay identical across both processes.
import kline_volume as klv


def _sanitize(obj):
    """Recursively replace float nan/inf with None so json.dumps never chokes."""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj

# ── Paths ─────────────────────────────────────────────────────────────────────
# Resolve DATA_DIR relative to this script so api.py and debug.py always
# share the same files regardless of which directory they were launched from.
# Production writes to the Fly volume at /app/data. OHM_DATA_DIR overrides it so
# the API can be pointed at ./data for a local run — debug.py reads the same
# variable with the same default, and the two MUST resolve to the same directory
# or the dashboard reads files nothing is writing.
DATA_DIR = Path(os.environ.get("OHM_DATA_DIR", "/app/data"))
LATEST_CSV  = DATA_DIR / "latest.csv"
STATE_FILE  = DATA_DIR / "health_state.json"
CONFIG_FILE = DATA_DIR / "monitor_config.json"
# Per-pair Telegram suspensions — {symbol: ISO expiry (NGT)}. This process owns
# writing it (the dashboard's Suspend/Resume buttons); the monitor process only
# reads it at its fire gate. Kept separate from health_state.json on purpose: the
# monitor rewrites health_state.json wholesale each cycle and would clobber a
# suspend written here mid-cycle. Same api-writes / monitor-reads direction as
# monitor_config.json, so there's no cross-process write race.
SUSPENSIONS_FILE = DATA_DIR / "suspensions.json"
# Per-(pair, issue) acknowledgements — {symbol: {issue_id: ISO acked_at (NGT)}}.
# This process owns writing it (the checkbox on each check row); the monitor only
# reads it, at its fire gate.
#
# An ack expires when the issue next reaches a good state, but this process never
# sees a cycle and so cannot observe that. The monitor records the clear instead,
# as `resolved_at` inside health_state.json (a file it already owns outright), and
# BOTH processes decide liveness by the same comparison — acked_at > resolved_at.
# That keeps expiry working without either process writing the other's file, so
# there is no cross-process write race in either direction. See debug.py's
# ALERT_ACKS_FILE comment for the monitor half.
ALERT_ACKS_FILE = DATA_DIR / "alert_acks.json"
# G1 depth-walk slippage tracker files (written by debug.py's depth_walk_loop)
DEPTH_WALK_RAW_FILE       = DATA_DIR / "usdtngn_slippage_raw.json"
DEPTH_WALK_CONDENSED_FILE = DATA_DIR / "usdtngn_slippage_hourly.json"
# USDTNGN hourly volume archive (written by debug.py's kline_volume_loop). Read
# here to cover hours older than a live fetch can reach (~300 hours) — recent
# hours come from the live call, so an absent file just means "no deep history".
VOLUME_SYMBOL       = "usdtngn"
VOLUME_ARCHIVE_FILE = DATA_DIR / "usdtngn_volume_hourly.json"
STATIC_DIR  = Path(".")          # dashboard.html lives next to api.py
NIGERIAN_TZ = timezone(timedelta(hours=1))

# ── Default config ────────────────────────────────────────────────────────────
# Canonical defaults + merge semantics now live in defaults.py, imported above
# and shared verbatim with debug.py so the two processes can never drift.


def load_config() -> dict:
    """Stored monitor_config.json merged over the shared defaults (defaults fill gaps)."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                stored = json.load(f)
            return merge_config(stored)
        except Exception:
            pass
    return default_config()


def save_config(cfg: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


app = FastAPI(title="Quidax Market Monitor API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # tighten in production
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def ngt_now() -> datetime:
    return datetime.now(NIGERIAN_TZ)


def parse_latest_csv() -> list[dict]:
    if not LATEST_CSV.exists():
        return []

    df = pd.read_csv(LATEST_CSV)

    # Normalise types — booleans arrive as strings from CSV
    for col in ("monitor_only", "should_alert", "telegram_fired", "dws_poor", "d1_spike",
                "ref_mexc_usable", "ref_kucoin_usable",
                # B1's two halves, reported separately so the dashboard can show
                # which one fired — the engine folds them into a single issue
                # tuple, so the issues string alone cannot tell them apart.
                "b1_price_diff_fired", "b1_stale_fired", "b1_reference_usable"):
        if col in df.columns:
            df[col] = df[col].map(
                lambda v: str(v).strip().lower() in ("true", "1", "yes")
                if pd.notna(v) else False
            )

    # Numeric coercion (percent_diff / imbalance_ratio may be "N/A")
    # Per-check evidence columns (see process_pair's `metrics`) land here too.
    # They are absent from rows written on an A3 short-circuit and from any row
    # predating the feature, so every one is optional — `if col in df.columns`
    # below already handles that, and pd.to_numeric(errors="coerce") turns the
    # per-row gaps into NaN, which _sanitize renders as null.
    for col in ("current_spread", "spread_abs", "percent_diff",
                "mid_price", "dws", "imbalance_ratio",
                "ask_layers", "bid_layers", "trusted_ref",
                "layer_churn_pct", "layer_churn_baseline_pct",
                "d1_window_volume", "d1_threshold",
                # A1 / A2
                "best_ask", "best_bid", "spread_diff_pp",
                "min_orderbook_layers", "dws_threshold", "min_abs_spread_diff_pct",
                # A4 / A5
                "depth_book_value", "depth_baseline_value", "depth_baseline_samples",
                "depth_deviation_pct", "depth_ratio", "depth_ratio_floor",
                "depth_min_history",
                "book_bid_value", "book_ask_value", "imbalance_threshold",
                # A6
                "layer_churn_ratio", "layer_churn_samples",
                "layer_churn_ratio_floor", "layer_churn_min_history",
                # B1
                "b1_diff_pct", "b1_expected_offset_pct", "b1_threshold_pct",
                # B2 / B3
                "ref_mexc", "ref_kucoin", "ref_divergence_pct",
                "ref_divergence_threshold_pct",
                "ref_mexc_unavail", "ref_kucoin_unavail",
                "ref_mexc_unchanged", "ref_kucoin_unchanged",
                # B4
                "b4_window_open", "b4_move_pct", "b4_warn_pct", "b4_breaker_pct",
                "kline_lookback_minutes",
                # G2
                "g2_worst_swing_pct", "g2_candles_scanned",
                "g2_zero_prints", "g2_swing_threshold_pct",
                # F1
                "f1_gap_pct", "f1_implied", "f1_actual", "f1_threshold_pct"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    records = df.to_dict(orient="records")
    return _sanitize(records)


def parse_daily_log(date_str: Optional[str] = None) -> list[dict]:
    if date_str:
        # Validate format and clamp to 30-day window
        try:
            requested = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=NIGERIAN_TZ)
        except ValueError:
            return []
        earliest = ngt_now() - timedelta(days=30)
        if requested < earliest.replace(hour=0, minute=0, second=0, microsecond=0):
            return []
        target_date = date_str
    else:
        target_date = ngt_now().strftime("%Y-%m-%d")
    pattern = str(DATA_DIR / f"daily_log_{target_date}.csv")
    files = glob.glob(pattern)
    if not files:
        return []
    df = pd.read_csv(files[0])
    return _sanitize(df.to_dict(orient="records"))


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    with open(STATE_FILE) as f:
        return json.load(f)


def load_suspensions() -> dict:
    """Read suspensions.json → {symbol: ISO expiry (NGT)}. Missing/corrupt → {}."""
    if not SUSPENSIONS_FILE.exists():
        return {}
    try:
        with open(SUSPENSIONS_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_suspensions(data: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(SUSPENSIONS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def prune_suspensions(data: dict) -> dict:
    """Drop entries whose expiry has already passed (or won't parse). Keeps the
    file from accumulating stale rows and means a GET only ever reports live mutes."""
    live = {}
    now = ngt_now()
    for sym, expiry in data.items():
        try:
            if now < datetime.fromisoformat(expiry):
                live[sym] = expiry
        except (ValueError, TypeError):
            continue
    return live


def load_alert_acks() -> dict:
    """Read alert_acks.json -> {symbol: {issue_id: ISO acked_at}}. Missing/corrupt -> {}."""
    if not ALERT_ACKS_FILE.exists():
        return {}
    try:
        with open(ALERT_ACKS_FILE) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return {str(s).lower(): v for s, v in data.items() if isinstance(v, dict)}
    except Exception:
        return {}


def save_alert_acks(data: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(ALERT_ACKS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _resolved_at(state: dict, symbol: str, issue_id: str) -> Optional[str]:
    """
    When the monitor last saw `issue_id` ABSENT for `symbol`, from health_state.json.
    None means it has never been seen absent (which includes "never observed").
    """
    return (((state.get(symbol) or {}).get("_alert") or {})
            .get("resolved_at") or {}).get(issue_id)


def prune_alert_acks(data: dict, state: dict) -> dict:
    """
    Drop acks the monitor has since seen clear — acked_at <= resolved_at — plus any
    unparseable row. Mirrors debug.is_acked exactly; the two must agree, or the
    dashboard would show a checkbox state the engine isn't acting on.

    An ack with no resolved_at is KEPT: the issue has not been observed clear since
    it was acknowledged, which is precisely when the mute should still apply.
    """
    live: dict = {}
    for sym, issues in data.items():
        if not isinstance(issues, dict):
            continue
        for issue_id, acked_at in issues.items():
            try:
                acked_ts = datetime.fromisoformat(acked_at)
            except (ValueError, TypeError):
                continue
            resolved_at = _resolved_at(state, sym, issue_id)
            if resolved_at:
                try:
                    if acked_ts <= datetime.fromisoformat(resolved_at):
                        continue        # cleared since the ack — retire it
                except (ValueError, TypeError):
                    pass                # corrupt resolved_at: keep the ack
            live.setdefault(sym, {})[issue_id] = acked_at
    return live


# A cycle is 60s by default; three missed cycles is a monitor that has stopped
# rather than one that ran slow. Derived from config so a retuned cycle length
# doesn't turn into a permanent false alarm.
def _monitor_stale_after_seconds() -> float:
    try:
        cycle = float(load_config().get("timing", {}).get("cycle_sleep_seconds", 60))
    except Exception:
        cycle = 60.0
    return max(180.0, cycle * 3 + 60.0)


def _age_seconds(ts_str: Optional[str]) -> Optional[float]:
    """Seconds since an NGT 'YYYY-MM-DD HH:MM:SS' stamp, or None if unusable."""
    if not ts_str:
        return None
    try:
        dt = datetime.strptime(str(ts_str), "%Y-%m-%d %H:%M:%S").replace(tzinfo=NIGERIAN_TZ)
    except ValueError:
        return None
    return (ngt_now() - dt).total_seconds()


def summary_stats(records: list[dict]) -> dict:
    total    = len(records)
    warnings = sum(1 for r in records if str(r.get("status", "")).lower() == "warning")
    alerted  = sum(1 for r in records if r.get("telegram_fired"))
    healthy  = total - warnings
    ts       = records[0].get("timestamp") if records else None

    # Whether the MONITOR is still running, which is a different question from
    # whether this API is reachable. The dashboard's status light used to answer
    # the second and label it "Live", so a monitor that had been dead for hours
    # still showed green as long as uvicorn answered — the API serves whatever
    # latest.csv last contained, however old that is.
    age = _age_seconds(ts)
    return {
        "total_pairs": total,
        "healthy":     healthy,
        "warnings":    warnings,
        "alerts_fired": alerted,
        "last_updated": ts,
        "last_updated_age_seconds": None if age is None else round(age, 1),
        "monitor_stale": True if age is None else age > _monitor_stale_after_seconds(),
        "server_time_ngt": ngt_now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "time_ngt": ngt_now().strftime("%Y-%m-%d %H:%M:%S")}


@app.get("/")
def serve_dashboard():
    path = STATIC_DIR / "dashboard.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="dashboard.html not found next to api.py")
    return FileResponse(path)


@app.get("/favicon.ico")
def serve_favicon():
    path = STATIC_DIR / "favicon.ico"
    if not path.exists():
        raise HTTPException(status_code=404, detail="favicon.ico not found next to api.py")
    return FileResponse(path)


# ── Alert tier metadata for the dashboard's grouped dropdowns ────────────────
# The two alert-type dropdowns group their options by delivery tier, which means
# the browser needs to know each id's tier. That answer is DERIVED here by asking
# classify_tier — never transcribed — because a hand-maintained copy in the page
# would drift silently the first time a check is retiered, and the symptom would
# be a dropdown quietly filing an alert under the wrong urgency.
#
# Severity is half the input (A2 is Tier 1 at CRITICAL and Tier 2 otherwise; A6
# and B1 have Tier 3 MEDIUM variants), so the map is keyed by BOTH. The page
# needs both projections of it: the alert-type dropdowns group an id by the most
# urgent tier it can reach (distinct values), while the per-market check panel
# files each ROW by its own case — A2's spread and shallow-book rows are Tier 2
# even though the id can reach Tier 1 through the one-sided case. Serving the
# full matrix lets the page answer both without a second endpoint or a local
# copy of the rules.
#
# E1/E2 are included even though they never reach classify_tier (both are keyed
# "_global", not to a market): they are Tier 1 by definition and an operator
# reading the dropdown should see them where they belong. The retired A3/B3 are
# included by virtue of being in ACKABLE_ISSUE_IDS, which is what lets historical
# log rows still group correctly.
_TIER_SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM")


def _issue_tier_map() -> dict[str, dict[str, int]]:
    """{issue_id: {severity: tier}} — computed, not transcribed."""
    out = {}
    for iid in sorted(ACKABLE_ISSUE_IDS):
        out[iid] = {sev: classify_tier(iid, sev) for sev in _TIER_SEVERITIES}
    # Global ids never reach classify_tier (both keyed "_global", not a market)
    # but are Tier 1 by definition.
    for iid in ("E1", "E2"):
        out[iid] = {sev: 1 for sev in _TIER_SEVERITIES}
    return out


ISSUE_TIERS = _issue_tier_map()


@app.get("/api/status")
def get_status():
    """
    Latest cycle results for all monitored pairs.
    Returns:
      - summary: aggregate counts
      - pairs:   one record per pair with all metrics
    """
    records = parse_latest_csv()
    return JSONResponse({
        "summary": summary_stats(records),
        "pairs":   records,
        # Static per-process, but served here rather than from its own endpoint so
        # the page has it after the very first poll — both alert dropdowns need it
        # to group their options, and one of them lives on a tab the user may open
        # before any other request has been made.
        "issue_tiers": ISSUE_TIERS,
    })


@app.get("/api/history")
def get_history(date: Optional[str] = None):
    """
    Daily log for a given date, returned as JSON rows.
    ?date=YYYY-MM-DD  — serve that day's file (max 30 days back; omit for today).
    Rows are in file order (oldest-first); the dashboard reverses for newest-first display.
    """
    resolved_date = date or ngt_now().strftime("%Y-%m-%d")
    rows = parse_daily_log(date)
    return JSONResponse({
        "date": resolved_date,
        "rows": rows,
    })


# ── Alert analysis ────────────────────────────────────────────────────────────
# Range analytics over the daily logs. Everything below reads
# data/daily_log_YYYY-MM-DD.csv, whose only columns are
# Timestamp,Market,Status,Issues,Depth — so four properties of that file shape
# every number this endpoint returns, and the dashboard labels them as such:
#
#   1. DETECTIONS, NOT DELIVERIES. The writer (debug.py update_daily_log) does
#      not persist telegram_detail, so nothing here can tell an alert that
#      reached Telegram apart from one suppressed by tier, cooldown, ack or
#      episode cap. Every count is "the monitor saw this", never "we sent this".
#   2. NO EPISODE RECORD EXISTS. health_state.json keeps only the LATEST
#      resolved_at per (market, issue), not a history, so runs have to be
#      stitched back together from consecutive cycles here.
#   3. E1/E2 ARE ABSENT. Both are global (keyed "_global") and the log only
#      takes per-pair rows, so API-outage and reference-feed analysis is not
#      derivable from this source at all.
#   4. WARNING ROWS ONLY. Healthy pairs are never appended, so there is no
#      honest per-pair healthy/unhealthy denominator in the file. The only
#      denominator we can defend is elapsed span ÷ cycle period, which is what
#      summary.expected_cycles reports.
#
# On the shape of the code: a busy 30-day range is ~10^6 log rows, and a
# pathologically noisy one can stitch to nearly as many episodes. Every stage
# below is therefore vectorised over numpy/pandas rather than looping in Python,
# and the few remaining loops run over already-aggregated frames whose row count
# is bounded by the number of issue ids (13) or markets (~55), never by cycles.

MAX_ANALYSIS_DAYS  = 30    # mirrors parse_daily_log's clamp
EPISODE_GAP_CYCLES = 2     # a run survives up to this many missing cycles
_SEVERITY_RANK     = {"MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
_RANK_TO_SEV       = {v: k for k, v in _SEVERITY_RANK.items()}
_MAX_LONGEST       = 50
_MAX_FLAPPING      = 25


def _sev_rank(sev: str) -> int:
    """Unknown severities sort below MEDIUM rather than raising — a future check
    with a new label degrades to 'least severe' instead of 500-ing the tab."""
    return _SEVERITY_RANK.get(str(sev).upper(), 0)


def _parse_analysis_range(start: Optional[str], end: Optional[str]):
    """
    Resolve ?start=/?end= (YYYY-MM-DD, NGT) into inclusive date bounds.

    Defaults to the trailing 7 days ending today. Rejects a malformed date, an
    inverted range, and a span or age beyond MAX_ANALYSIS_DAYS — the same 30-day
    wall parse_daily_log enforces, applied to both edges so a caller can't reach
    past retention from either side.
    """
    today = ngt_now().date()

    def _d(raw: str, field: str):
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400,
                                detail=f"{field} must be YYYY-MM-DD, got {raw!r}")

    end_d   = _d(end, "end") if end else today
    start_d = _d(start, "start") if start else end_d - timedelta(days=6)

    if start_d > end_d:
        raise HTTPException(status_code=400, detail="start must not be after end")
    span = (end_d - start_d).days + 1
    if span > MAX_ANALYSIS_DAYS:
        raise HTTPException(status_code=400,
                            detail=f"range spans {span} days; max is {MAX_ANALYSIS_DAYS}")
    if (today - start_d).days >= MAX_ANALYSIS_DAYS:
        raise HTTPException(status_code=400,
                            detail=f"start is beyond the {MAX_ANALYSIS_DAYS}-day retention window")
    return start_d, end_d


def _load_alert_frame(start_d, end_d, with_depth=False):
    """
    Concat every daily log in [start_d, end_d] into one frame carrying a real
    datetime, and report which dates were present.

    The log stores only HH:MM:SS, so the date has to come from the filename —
    that's the only reason days are read one file at a time rather than globbed.
    A missing or unreadable file is a missing DAY, not an error: the monitor
    appends nothing on a day with no warnings, so "file absent" and "file present
    but empty" both legitimately mean "no alerts that day" and are reported the
    same way.

    Only the three columns the analysis needs are parsed by default. Depth is
    the widest column in the file and the aggregates never read it, so it is
    opt-in: `with_depth` is for the alert-log endpoint, which shows it on the
    handful of rows in the page it returns.

    Both string-heavy steps run over UNIQUE values and are broadcast back
    through an inverse index, which is the single biggest win available here.
    A day holds at most ~1440 distinct clock times and ~55 distinct market names
    across tens of thousands of rows, so parsing the uniques turns two multi-
    second passes into two instant ones.

    pd.factorize rather than np.unique for the uniquing: np.unique on an object
    array sorts it, which means comparing Python strings pairwise, and that
    measured as the largest single cost in the whole endpoint. factorize hashes
    instead, and nothing downstream needs the categories in sorted order — every
    output list is sorted explicitly where it is built.
    """
    frames, found, missing = [], [], []
    d = start_d
    while d <= end_d:
        ds   = d.strftime("%Y-%m-%d")
        path = DATA_DIR / f"daily_log_{ds}.csv"
        df   = None
        if path.exists():
            try:
                cols = ["Timestamp", "Market", "Issues"]
                if with_depth:
                    cols.append("Depth")
                df = pd.read_csv(path, usecols=lambda c: c in cols, dtype=str)
            except Exception:
                df = None
        if df is not None and not df.empty:
            df["_date"] = ds
            codes, uniq = pd.factorize(df["Timestamp"].fillna(""))
            parsed = pd.to_datetime(pd.Series([f"{ds} {u}" for u in uniq]),
                                    format="%Y-%m-%d %H:%M:%S", errors="coerce")
            df["ts"] = parsed.to_numpy()[codes]
            frames.append(df)
            found.append(ds)
        else:
            missing.append(ds)
        d += timedelta(days=1)

    if not frames:
        return pd.DataFrame(), found, missing

    all_df = pd.concat(frames, ignore_index=True)
    all_df = all_df.dropna(subset=["ts"]).reset_index(drop=True)
    if all_df.empty:
        return pd.DataFrame(), found, missing

    # Market names are normalised on the uniques, then re-uniqued: two spellings
    # that differ only in case or padding must collapse to ONE category, which a
    # straight rename_categories would reject as a duplicate.
    inv, seen  = pd.factorize(all_df["Market"].fillna(""))
    normed     = pd.Index([str(x).strip().lower() for x in seen])
    inv2, cats = pd.factorize(normed)
    all_df["market"] = pd.Categorical.from_codes(inv2[inv], categories=cats)

    all_df["Issues"] = all_df["Issues"].fillna("").astype(str)
    all_df["_date"]  = all_df["_date"].astype("category")
    return all_df, found, missing


def _observed_cycle_seconds(all_df: pd.DataFrame) -> float:
    """
    The cycle period, measured from the data rather than read from config.

    timing.cycle_sleep_seconds is the SLEEP at the end of a cycle, not the
    period — a real cycle is that sleep plus however long the pass took, so a
    configured 60 routinely lands as 65-80s on the wire. Stitching episodes
    against the config number would split long runs on a slow day. The median
    gap between distinct cycle timestamps measures the real thing, and a median
    is immune to the large gaps quiet periods leave behind.

    Falls back to the configured sleep when there aren't enough distinct
    timestamps to take a median from.
    """
    stamps = pd.Series(sorted(all_df["ts"].unique()))
    if len(stamps) >= 10:
        gaps = stamps.diff().dt.total_seconds().dropna()
        gaps = gaps[gaps > 0]
        if not gaps.empty:
            med = float(gaps.median())
            if 5.0 <= med <= 900.0:
                return med
    try:
        configured = float(load_config().get("timing", {}).get("cycle_sleep_seconds", 60))
    except Exception:
        configured = 60.0
    return configured if configured > 0 else 60.0



def _explode_issues(all_df: pd.DataFrame):
    """
    One row per (cycle, market, issue) out of the packed "B1:HIGH|A4:MEDIUM"
    column.

    Nothing here touches a per-row string. Across a month there are only a few
    hundred distinct Issues strings and a few dozen distinct "ID:SEV" tokens,
    because a pair carrying the same issues for an hour writes the identical
    string every cycle. So the column is factorised once, every split and
    uppercase happens on the small category list, and the result is broadcast to
    10^6 rows by integer gather.

    The ragged explode is the standard offsets/repeat construction rather than
    DataFrame.explode: each combo contributes a known token count, so the output
    row for position k is combo_offset[k] + (k - start_of_run[k]) into a single
    flat token array. All integer arithmetic, one pass, no Python-level loop over
    rows.

    severity is carried as BOTH a label and a precomputed sev_rank. The rank is
    resolved here, on the ~40 distinct tokens, because the alternative — ranking
    at episode-build time — meant _sev_rank taking a Python call per detection,
    which measured as the single most expensive thing in the whole endpoint.
    """
    empty_cols = {"ts": [], "_date": [], "market": [], "issue": [],
                  "severity": [], "sev_rank": [], "_row": []}
    if all_df.empty:
        return pd.DataFrame(empty_cols)

    combo = all_df["Issues"].astype("category")
    cats  = list(combo.cat.categories)
    cc    = combo.cat.codes.to_numpy()

    tok_lists = [[t.strip() for t in str(c).split("|") if t.strip()] for c in cats]
    vocab = sorted({t for lst in tok_lists for t in lst})
    if not vocab:
        return pd.DataFrame(empty_cols)
    tok_index = {t: i for i, t in enumerate(vocab)}

    widths  = np.array([len(l) for l in tok_lists], dtype=np.int64)
    offsets = np.concatenate([[0], np.cumsum(widths)])
    flat    = np.fromiter((tok_index[t] for lst in tok_lists for t in lst),
                          dtype=np.int32, count=int(widths.sum()))

    rep   = widths[cc]
    total = int(rep.sum())
    if total == 0:
        return pd.DataFrame(empty_cols)
    row_idx   = np.repeat(np.arange(len(cc)), rep)
    run_start = np.repeat(np.cumsum(rep) - rep, rep)
    within    = np.arange(total) - run_start
    tok_codes = flat[np.repeat(offsets[cc], rep) + within]

    issue_names = np.array([t.split(":", 1)[0].strip() for t in vocab], dtype=object)
    sev_names   = np.array([t.split(":", 1)[1].strip().upper() if ":" in t else ""
                            for t in vocab], dtype=object)
    issue_cats, issue_of_tok = np.unique(issue_names, return_inverse=True)
    sev_cats,   sev_of_tok   = np.unique(sev_names,   return_inverse=True)
    rank_of_tok = np.array([_sev_rank(s) for s in sev_names], dtype=np.int16)

    mkt = all_df["market"]
    dat = all_df["_date"]
    return pd.DataFrame({
        "ts":       all_df["ts"].to_numpy()[row_idx],
        "_date":    pd.Categorical.from_codes(dat.cat.codes.to_numpy()[row_idx],
                                              categories=dat.cat.categories),
        "market":   pd.Categorical.from_codes(mkt.cat.codes.to_numpy()[row_idx],
                                              categories=mkt.cat.categories),
        "issue":    pd.Categorical.from_codes(issue_of_tok[tok_codes],
                                              categories=issue_cats),
        "severity": pd.Categorical.from_codes(sev_of_tok[tok_codes],
                                              categories=sev_cats),
        "sev_rank": rank_of_tok[tok_codes],
        # Position of this detection's source row in all_df. Lets a caller pull
        # a column it deliberately did not carry through the explode (Depth) for
        # just the rows it ends up returning.
        "_row":     row_idx,
    })


def _build_episodes(ex: pd.DataFrame, cycle_s: float, gap_cycles: int) -> pd.DataFrame:
    """
    Stitch per-(market, issue) detections into episodes.

    A run survives up to `gap_cycles` missing cycles, so a single dropped fetch
    or one-cycle flicker doesn't split a four-hour B1 into two episodes and
    inflate the flapping ranking with artefacts. The 1.25 slack absorbs cycle
    jitter on top of that — the period is a median, and individual cycles sit
    either side of it.

    Duration counts the cycle each detection represents, so a one-detection
    episode is one cycle long rather than zero: the issue WAS present for that
    cycle, and a zero would make single-cycle flaps vanish from every duration
    statistic and divide-by-zero the flapping score.

    Sorting is np.lexsort over the two category code arrays plus the timestamp,
    and the runs are then found with flatnonzero and folded with ufunc.reduceat.
    A groupby keyed on the run id would build one group object per episode —
    fine at a few thousand, several seconds at the ~10^6 a pathologically noisy
    month can produce. reduceat collapses the same contiguous slices in one pass.

    start_s/end_s are seconds from the frame's first timestamp. They exist so the
    interval merge downstream never has to touch epochs: the datetime64
    resolution pandas uses is version-dependent (ns before 3.0, µs after), so any
    hardcoded epoch divisor is a silent 1000x scaling bug waiting for an upgrade.
    """
    cols = ["market", "issue", "start", "end", "start_s", "end_s", "detections",
            "peak_rank", "first_rank", "severity", "escalated", "duration_min"]
    if ex.empty:
        return pd.DataFrame({c: [] for c in cols})

    tol      = (gap_cycles + 1) * cycle_s * 1.25
    m_codes  = ex["market"].cat.codes.to_numpy()
    i_codes  = ex["issue"].cat.codes.to_numpy()
    ts_all   = ex["ts"].to_numpy()
    order    = np.lexsort((ts_all, i_codes, m_codes))

    m_codes = m_codes[order]
    i_codes = i_codes[order]
    ts      = ts_all[order]
    rank    = ex["sev_rank"].to_numpy()[order]
    secs    = (ts - ts[0]) / np.timedelta64(1, "s")

    n = len(ts)
    brk = np.empty(n, dtype=bool)
    brk[0] = True
    brk[1:] = ((m_codes[1:] != m_codes[:-1]) | (i_codes[1:] != i_codes[:-1]) |
               ((secs[1:] - secs[:-1]) > tol))
    starts = np.flatnonzero(brk)
    ends   = np.append(starts[1:], n) - 1

    peak  = np.maximum.reduceat(rank, starts)
    first = rank[starts]
    sev_labels = np.array([_RANK_TO_SEV.get(int(r), "UNKNOWN")
                           for r in range(int(rank.max()) + 1)], dtype=object)

    eps = pd.DataFrame({
        "market":     pd.Categorical.from_codes(m_codes[starts],
                                                categories=ex["market"].cat.categories),
        "issue":      pd.Categorical.from_codes(i_codes[starts],
                                                categories=ex["issue"].cat.categories),
        "start":      ts[starts],
        "end":        ts[ends],
        "start_s":    secs[starts],
        "end_s":      secs[ends],
        "detections": (ends - starts + 1).astype("int64"),
        "peak_rank":  peak,
        "first_rank": first,
        "severity":   sev_labels[peak],
        "escalated":  peak > first,
    })
    eps["duration_min"] = (eps["end_s"] - eps["start_s"] + cycle_s) / 60.0
    return eps


def _sev_columns(frame: pd.DataFrame, key) -> pd.DataFrame:
    """severity value-counts pivoted to CRITICAL/HIGH/MEDIUM columns, always all
    three present so callers can index without a .get dance."""
    wide = (frame.groupby([key, "severity"], sort=False, observed=True).size()
                 .unstack(fill_value=0))
    for col in ("CRITICAL", "HIGH", "MEDIUM"):
        if col not in wide.columns:
            wide[col] = 0
    return wide


@app.get("/api/alert-analysis")
def get_alert_analysis(start: Optional[str] = None,
                       end: Optional[str] = None,
                       gap_cycles: int = EPISODE_GAP_CYCLES):
    """
    Aggregate alert analytics across a date range of daily logs.

    ?start=&end=   YYYY-MM-DD, NGT, inclusive. Defaults to the trailing 7 days.
                   Max span and max age are both MAX_ANALYSIS_DAYS.
    ?gap_cycles=   missing cycles an episode survives before it's split (0-10).

    Aggregation happens here rather than in the browser for a blunt reason: a
    busy 30-day range is on the order of 10^6 rows across 30 files, which is a
    few seconds of pandas and tens of KB of JSON, versus 30 sequential fetches
    and tens of megabytes the other way round.

    Everything returned counts DETECTIONS, not Telegram deliveries — see the
    section header above for why the log cannot distinguish them.
    """
    gap_cycles = max(0, min(int(gap_cycles), 10))
    start_d, end_d = _parse_analysis_range(start, end)

    all_df, found, missing = _load_alert_frame(start_d, end_d)
    coverage = {
        "start":              start_d.strftime("%Y-%m-%d"),
        "end":                end_d.strftime("%Y-%m-%d"),
        "days_in_range":      (end_d - start_d).days + 1,
        "days_with_data":     len(found),
        "dates_with_data":    found,
        "dates_without_data": missing,
        "gap_cycles":         gap_cycles,
    }
    blank = {
        "coverage": coverage, "summary": {}, "by_issue": [],
        "longest_episodes": [], "flapping": [],
        "hourly": {"hours": list(range(24)), "issues": [], "matrix": [],
                   "detections": [0] * 24, "episode_starts": [0] * 24},
        "still_open": [],
    }

    if all_df.empty:
        blank["summary"] = {"warning_rows": 0, "total_detections": 0, "total_episodes": 0,
                            "markets_affected": 0, "issue_types": 0}
        return JSONResponse(_sanitize(blank))

    cycle_s = _observed_cycle_seconds(all_df)
    ex = _explode_issues(all_df)
    if ex.empty:
        blank["summary"] = {"warning_rows": int(len(all_df)), "total_detections": 0,
                            "total_episodes": 0, "markets_affected": 0, "issue_types": 0}
        return JSONResponse(_sanitize(blank))

    eps        = _build_episodes(ex, cycle_s, gap_cycles)
    last_cycle = all_df["ts"].max()
    open_tol   = (gap_cycles + 1) * cycle_s * 1.25
    eps["open_at_end"] = (last_cycle - eps["end"]).dt.total_seconds() <= open_tol

    def iso(t):
        return pd.Timestamp(t).strftime("%Y-%m-%d %H:%M:%S")

    p95 = lambda s: float(s.quantile(0.95))

    # ── Per-issue ─────────────────────────────────────────────────────────────
    # No longer charted on its own. It stays because the hour-of-day heatmap
    # orders its rows by episode count and summary.top_issue reads the head of
    # it — deleting this would silently make the heatmap's row order arbitrary.
    det_by_issue = ex.groupby("issue", sort=False, observed=True).size()
    ei = eps.groupby("issue", sort=False, observed=True).agg(
        episodes    = ("duration_min", "size"),
        markets     = ("market",       "nunique"),
        median_min  = ("duration_min", "median"),
        p95_min     = ("duration_min", p95),
        max_min     = ("duration_min", "max"),
        total_min   = ("duration_min", "sum"),
        escalations = ("escalated",    "sum"),
        open_now    = ("open_at_end",  "sum"),
    )
    ei_sev = _sev_columns(eps, "issue")
    by_issue = [{
        "issue":       str(issue),
        "episodes":    int(r.episodes),
        "detections":  int(det_by_issue.get(issue, 0)),
        "markets":     int(r.markets),
        "median_min":  round(float(r.median_min), 2),
        "p95_min":     round(float(r.p95_min), 2),
        "max_min":     round(float(r.max_min), 2),
        "total_min":   round(float(r.total_min), 2),
        "escalations": int(r.escalations),
        "open_now":    int(r.open_now),
        "critical":    int(ei_sev.loc[issue, "CRITICAL"]),
        "high":        int(ei_sev.loc[issue, "HIGH"]),
        "medium":      int(ei_sev.loc[issue, "MEDIUM"]),
    } for issue, r in ei.iterrows()]
    by_issue.sort(key=lambda r: (-r["episodes"], -r["detections"]))

    # ── Worst market ──────────────────────────────────────────────────────────
    # A full per-market table used to ship in this payload for the analysis tab's
    # market chart. That chart is gone and the only survivor is the single name
    # behind the "Markets affected" card, so all that is computed now is the
    # ranking's head: most episodes, ties broken by summed episode minutes.
    em = eps.groupby("market", sort=False, observed=True).agg(
        episodes  = ("duration_min", "size"),
        total_min = ("duration_min", "sum"),
    ).sort_values(["episodes", "total_min"], ascending=False)
    top_market = str(em.index[0]) if len(em) else None

    # ── Longest episodes ──────────────────────────────────────────────────────
    longest = eps.nlargest(_MAX_LONGEST, "duration_min")
    longest_episodes = [{
        "market":       str(r.market),
        "issue":        str(r.issue),
        "severity":     str(r.severity),
        "start":        iso(r.start),
        "end":          iso(r.end),
        "duration_min": round(float(r.duration_min), 2),
        "detections":   int(r.detections),
        "escalated":    bool(r.escalated),
        "open_at_end":  bool(r.open_at_end),
    } for r in longest.itertuples()]

    # ── Flapping: many short episodes on one (market, issue) ──────────────────
    # The noise score is episodes per unit of median duration, floored at one
    # cycle so a run of single-cycle flaps can't divide by zero. A high score
    # means "fires constantly, resolves immediately" — a threshold worth
    # revisiting rather than an incident. Under 3 episodes there is nothing to
    # call a pattern, so those are dropped.
    cycle_min = cycle_s / 60.0
    fl = eps.groupby(["market", "issue"], sort=False, observed=True).agg(
        episodes   = ("duration_min", "size"),
        median_min = ("duration_min", "median"),
        total_min  = ("duration_min", "sum"),
        detections = ("detections",   "sum"),
    )
    fl = fl[fl["episodes"] >= 3]
    fl = fl.assign(score=fl["episodes"] / fl["median_min"].clip(lower=cycle_min))
    flapping = [{
        "market":     str(market),
        "issue":      str(issue),
        "episodes":   int(r.episodes),
        "median_min": round(float(r.median_min), 2),
        "total_min":  round(float(r.total_min), 2),
        "detections": int(r.detections),
        "score":      round(float(r.score), 2),
    } for (market, issue), r in fl.nlargest(_MAX_FLAPPING, "score").iterrows()]

    # ── Hour of day ───────────────────────────────────────────────────────────
    hour_ids = [r["issue"] for r in by_issue]
    hours    = ex["ts"].dt.hour
    grid     = (ex.assign(hour=hours).groupby(["issue", "hour"], sort=False, observed=True).size()
                  .unstack(fill_value=0)
                  .reindex(index=hour_ids, columns=range(24), fill_value=0))
    hourly = {
        "hours":          list(range(24)),
        "issues":         hour_ids,
        "matrix":         grid.astype("int64").values.tolist(),
        "detections":     [int(v) for v in hours.value_counts().reindex(range(24), fill_value=0)],
        "episode_starts": [int(v) for v in eps["start"].dt.hour.value_counts()
                                             .reindex(range(24), fill_value=0)],
    }

    # ── Still open at range end ───────────────────────────────────────────────
    still = eps[eps["open_at_end"]].nlargest(_MAX_LONGEST, "duration_min")
    still_open = [{
        "market":       str(r.market),
        "issue":        str(r.issue),
        "severity":     str(r.severity),
        "start":        iso(r.start),
        "duration_min": round(float(r.duration_min), 2),
        "detections":   int(r.detections),
    } for r in still.itertuples()]

    # ── Summary ───────────────────────────────────────────────────────────────
    span_s  = float((last_cycle - all_df["ts"].min()).total_seconds()) + cycle_s
    sev_all = eps["severity"].value_counts()

    summary = {
        "warning_rows":        int(len(all_df)),
        "total_detections":    int(len(ex)),
        "total_episodes":      int(len(eps)),
        "markets_affected":    int(ex["market"].nunique()),
        "issue_types":         int(ex["issue"].nunique()),
        "critical_episodes":   int(sev_all.get("CRITICAL", 0)),
        "high_episodes":       int(sev_all.get("HIGH", 0)),
        "medium_episodes":     int(sev_all.get("MEDIUM", 0)),
        "escalations":         int(eps["escalated"].sum()),
        "open_at_end":         int(eps["open_at_end"].sum()),
        "mttr_min":            round(float(eps["duration_min"].mean()), 2),
        "median_duration_min": round(float(eps["duration_min"].median()), 2),
        "p95_duration_min":    round(p95(eps["duration_min"]), 2),
        "longest_min":         round(float(eps["duration_min"].max()), 2),
        "longest_episode":     longest_episodes[0] if longest_episodes else None,
        "top_issue":           by_issue[0]["issue"]   if by_issue  else None,
        "top_market":          top_market,
        # Cycle accounting. observed_cycles counts distinct timestamps in the log
        # — i.e. cycles that produced AT LEAST ONE warning. expected_cycles is the
        # elapsed span ÷ measured period. Their ratio is the only defensible "how
        # much of the time was something wrong" figure this file supports, because
        # healthy pairs are never written and there is no per-pair denominator.
        "cycle_seconds":       round(cycle_s, 1),
        "observed_cycles":     int(all_df["ts"].nunique()),
        "expected_cycles":     int(round(span_s / cycle_s)) if cycle_s else 0,
        "first_cycle":         iso(all_df["ts"].min()),
        "last_cycle":          iso(last_cycle),
    }

    return JSONResponse(_sanitize({
        "coverage":         coverage,
        "summary":          summary,
        "by_issue":         by_issue,
        "longest_episodes": longest_episodes,
        "flapping":         flapping,
        "hourly":           hourly,
        "still_open":       still_open,
    }))


# ── Alert log ─────────────────────────────────────────────────────────────────
# The daily log, served over the analysis tab's date range instead of one day at
# a time, at DETECTION grain: one row per (cycle, market, issue) rather than the
# file's one row per (cycle, market) with a packed "B1:HIGH|A4:MEDIUM" column.
#
# That regrain is what makes the filters honest. A row carrying both a Tier-1 and
# a Tier-3 id cannot be shown or hidden correctly by a tier filter — it belongs
# in both buckets and neither. Exploding first means every row has exactly one
# id, one severity and one tier, so a filter selects rows rather than approximating.
#
# Paging and filtering are server-side because a 30-day range is ~10^6 detections;
# the browser receives one page at a time and never the range.

_LOG_PAGE_SIZES = (50, 100, 200, 500)
_LOG_CSV_MAX    = 250_000   # rows; a whole busy month is ~4x this


def _tier_lookup(ex: pd.DataFrame) -> np.ndarray:
    """
    Tier per exploded row, as a numpy int8 array.

    classify_tier is a Python function and there are ~10^6 rows, but only a few
    dozen distinct (issue, severity) pairs — both are categoricals. So the rule
    is evaluated once per pair into a small 2-D table indexed by category code,
    and the per-row answer is one fancy-index gather.
    """
    issues = list(ex["issue"].cat.categories)
    sevs   = list(ex["severity"].cat.categories)
    table  = np.empty((len(issues), len(sevs)), dtype="int8")
    for i, issue in enumerate(issues):
        for j, sev in enumerate(sevs):
            table[i, j] = classify_tier(str(issue), str(sev))
    return table[ex["issue"].cat.codes.to_numpy(), ex["severity"].cat.codes.to_numpy()]


@app.get("/api/alert-log")
def get_alert_log(start: Optional[str] = None,
                  end: Optional[str] = None,
                  tier: Optional[int] = 1,
                  market: Optional[str] = None,
                  issue: Optional[str] = None,
                  page: int = 0,
                  page_size: int = 50,
                  format: Optional[str] = None):
    """
    Detection rows for a date range, filtered and paged server-side.

    ?start=&end=   YYYY-MM-DD, NGT, inclusive — the analysis tab's global range.
    ?tier=         1 | 2 | 3, or 0/omitted-as-0 for all. **Defaults to 1**: Tier 1
                   is the set that actually pages someone, so it is the useful
                   thing to land on rather than the full firehose.
    ?market=       exact symbol, lowercased.
    ?issue=        exact issue id (B1, A2, …).
    ?page=&page_size=   page_size is clamped to _LOG_PAGE_SIZES.
    ?format=csv    stream the whole filtered set instead of one page.

    Facets cascade in the order the filters are meant to be used — tier, then
    market, then alert type — each computed with the filters ABOVE it applied and
    its own excluded. That is what makes every dropdown option non-empty: the
    markets offered are those that have rows at the chosen tier, and the ids
    offered are those that have rows at that tier for the chosen market.
    """
    start_d, end_d = _parse_analysis_range(start, end)
    tier      = int(tier or 0)
    if tier not in (0, 1, 2, 3):
        raise HTTPException(status_code=400, detail="tier must be 1, 2, 3, or 0 for all")
    market    = (market or "").strip().lower() or None
    issue     = (issue or "").strip().upper() or None
    page_size = page_size if page_size in _LOG_PAGE_SIZES else 50
    page      = max(0, int(page))

    all_df, found, missing = _load_alert_frame(start_d, end_d, with_depth=True)
    coverage = {
        "start":              start_d.strftime("%Y-%m-%d"),
        "end":                end_d.strftime("%Y-%m-%d"),
        "days_in_range":      (end_d - start_d).days + 1,
        "days_with_data":     len(found),
        "dates_without_data": missing,
    }
    empty = {
        "coverage": coverage,
        "filters":  {"tier": tier, "market": market, "issue": issue},
        "facets":   {"tiers": {"1": 0, "2": 0, "3": 0}, "markets": [], "issues": []},
        "total": 0, "page": 0, "page_size": page_size, "pages": 0, "rows": [],
    }

    ex = _explode_issues(all_df) if not all_df.empty else pd.DataFrame()
    if ex.empty:
        if format == "csv":
            return _log_csv_response([], start_d, end_d)
        return JSONResponse(_sanitize(empty))

    tiers   = _tier_lookup(ex)
    markets = ex["market"].to_numpy()
    issues  = ex["issue"].to_numpy()

    m_tier   = np.ones(len(ex), dtype=bool) if tier == 0 else (tiers == tier)
    m_market = np.ones(len(ex), dtype=bool) if market is None else (markets == market)
    m_issue  = np.ones(len(ex), dtype=bool) if issue is None else (issues == issue)

    # Facets, each excluding its own filter (see docstring).
    tier_counts = np.bincount(tiers[m_market & m_issue], minlength=4)
    facets = {
        "tiers": {str(t): int(tier_counts[t]) if t < len(tier_counts) else 0
                  for t in (1, 2, 3)},
        "markets": sorted(set(markets[m_tier & m_issue].tolist())),
        "issues":  sorted(set(issues[m_tier & m_market].tolist())),
        # Tier per id, for grouping the alert-type dropdown. Sent with the facets
        # rather than relied upon from /api/status so this tab is self-sufficient.
        "issue_tiers": ISSUE_TIERS,
    }

    keep = np.flatnonzero(m_tier & m_market & m_issue)
    # ex is chronological (files read oldest-first, rows appended per cycle), so
    # newest-first is a reversed slice rather than a sort of ~10^6 rows.
    keep = keep[::-1]
    total = int(len(keep))

    if format == "csv":
        return _log_csv_response(_log_rows(ex, all_df, keep[:_LOG_CSV_MAX], tiers),
                                 start_d, end_d)

    pages = max(1, -(-total // page_size))
    page  = min(page, pages - 1)
    rows  = _log_rows(ex, all_df, keep[page * page_size:(page + 1) * page_size], tiers)

    return JSONResponse(_sanitize({
        "coverage": coverage,
        "filters":  {"tier": tier, "market": market, "issue": issue},
        "facets":   facets,
        "total": total, "page": page, "page_size": page_size,
        "pages": pages if total else 0,
        "rows": rows,
    }))


def _log_rows(ex: pd.DataFrame, all_df: pd.DataFrame, idx, tiers) -> list[dict]:
    """
    Materialise the selected exploded rows.

    Depth is looked up here rather than carried through the explode: it's the
    widest column in the file and only the handful of rows actually being
    returned need it. `_row` is the position of each detection's source row in
    all_df, which _explode_issues keeps for exactly this.
    """
    if len(idx) == 0:
        return []
    sub   = ex.iloc[idx]
    src   = sub["_row"].to_numpy()
    depth = (all_df["Depth"].to_numpy()[src] if "Depth" in all_df.columns
             else np.full(len(src), ""))
    ts    = sub["ts"].dt.strftime("%Y-%m-%d %H:%M:%S").tolist()
    return [{
        "ts":       t,
        "market":   str(m),
        "issue":    str(i),
        "severity": str(sv),
        "tier":     int(tr),
        "depth":    "" if d is None or (isinstance(d, float) and math.isnan(d)) else str(d),
    } for t, m, i, sv, tr, d in zip(
        ts, sub["market"].astype(str), sub["issue"].astype(str),
        sub["severity"].astype(str), tiers[idx], depth)]


def _log_csv_response(rows: list[dict], start_d, end_d):
    """Filtered detections as a CSV download. Capped at _LOG_CSV_MAX rows —
    beyond that this stops being something anyone opens in a spreadsheet, and
    the daily_log_*.csv files are right there for a bulk pull."""
    header = "Timestamp,Market,Alert,Severity,Tier,Depth\n"
    body = "".join(
        f'{r["ts"]},{r["market"]},{r["issue"]},{r["severity"]},T{r["tier"]},'
        f'"{str(r["depth"]).replace(chr(34), chr(34) * 2)}"\n'
        for r in rows)
    name = f"alert_log_{start_d:%Y-%m-%d}_to_{end_d:%Y-%m-%d}.csv"
    return Response(content=header + body, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})



@app.get("/api/state")
def get_state():
    """
    Raw health_state.json. Per pair: an "_alert" sub-key (Tier-2 consecutive
    counters + per-issue cooldown expiries) and the last observed mid price with
    its timestamp (last_mid / last_mid_ts, NGT ISO — a stale timestamp means the
    pair's mid hasn't been observable since then). Plus the engine's rolling
    reference-feed history (_ref_hist), volume baselines (_vol_hist), layer-churn
    baselines (_layer_hist), and global cooldowns (_global).
    """
    return JSONResponse(load_state())


@app.get("/api/diagnostics")
def get_diagnostics():
    """
    Which writer last wrote what, and how long ago.

    Exists because "the dashboard looks wrong" has repeatedly turned out to mean
    "one of the two processes stopped", and nothing surfaced which. This API and
    the monitor are separate processes writing separate files; a page that
    renders proves only that THIS one is alive.

    Reports both the file's mtime and, where the file carries its own timestamp,
    the age of the newest record in it. Those differ in the case that matters:
    depth_walk_loop rewrites its raw file every pass even when the fetch failed,
    so a fresh mtime with stale contents means the loop is running but not
    getting data — a completely different fault from the loop being dead.
    """
    now = ngt_now()

    def file_info(path: Path, label: str, writer: str) -> dict:
        exists = path.exists()
        mtime_age = None
        if exists:
            mtime_age = (now - datetime.fromtimestamp(path.stat().st_mtime,
                                                      NIGERIAN_TZ)).total_seconds()
        return {
            "label": label, "writer": writer, "path": path.name,
            "exists": exists,
            "mtime_age_seconds": None if mtime_age is None else round(mtime_age, 1),
        }

    files = [
        file_info(LATEST_CSV,          "Latest cycle results",  "monitor"),
        file_info(STATE_FILE,          "Health state",          "monitor"),
        file_info(DEPTH_WALK_RAW_FILE, "Depth-walk raw bucket", "monitor (5s task)"),
        file_info(DEPTH_WALK_CONDENSED_FILE, "Depth-walk hourly", "monitor (5s task)"),
        file_info(DATA_DIR / f"daily_log_{now:%Y-%m-%d}.csv", "Today's daily log", "monitor"),
        file_info(CONFIG_FILE,         "Monitor config",        "api"),
    ]

    # Content ages: what the newest RECORD inside says, not when the file was touched.
    records   = parse_latest_csv()
    cycle_age = _age_seconds(records[0].get("timestamp") if records else None)

    raw = _load_json_file(DEPTH_WALK_RAW_FILE, {}) or {}
    samples = raw.get("samples") or []
    walk_age = None
    if samples:
        try:
            walk_age = (now - datetime.fromisoformat(samples[-1]["ts"])).total_seconds()
        except (KeyError, TypeError, ValueError):
            walk_age = None

    stale_after = _monitor_stale_after_seconds()
    return JSONResponse(_sanitize({
        "server_time_ngt": now.strftime("%Y-%m-%d %H:%M:%S"),
        "monitor": {
            "last_cycle":            records[0].get("timestamp") if records else None,
            "last_cycle_age_seconds": None if cycle_age is None else round(cycle_age, 1),
            "stale_after_seconds":   stale_after,
            "running":               cycle_age is not None and cycle_age <= stale_after,
            "pairs_in_last_cycle":   len(records),
        },
        "depth_walk": {
            "bucket_start":            raw.get("bucket_start"),
            "samples_in_bucket":       len(samples),
            "newest_sample":           samples[-1].get("ts") if samples else None,
            "newest_sample_age_seconds": None if walk_age is None else round(walk_age, 1),
        },
        "files": files,
    }))


@app.get("/api/pairs")
def get_pairs():
    """
    List of known pairs derived from health_state.json keys
    (populated after the first monitor cycle runs).
    """
    state = load_state()
    pairs = list(state.keys())
    return JSONResponse({"pairs": pairs})


# ── Per-pair Telegram suspensions ────────────────────────────────────────────
# A suspended pair keeps being monitored and stays on the dashboard; only its
# Telegram delivery is muted, and only for its OWN alerts (F1 on other legs is
# unaffected). Duration is the single global alerts.suspend_minutes from config.
# The monitor process reads suspensions.json at its fire gate — see debug.py.

@app.get("/api/suspensions")
def get_suspensions():
    """
    Current live suspensions: {symbol: ISO expiry (NGT)} with expired entries
    pruned, plus the configured default duration so the dashboard can label the
    button ("Suspend 30m") without a second round-trip. The pruned map is written
    back so the file self-cleans on read.
    """
    live = prune_suspensions(load_suspensions())
    # Persist the pruned view so stale rows don't linger (best-effort; a failed
    # write just means they get pruned again next read).
    try:
        save_suspensions(live)
    except Exception:
        pass
    minutes = (load_config().get("alerts", {}) or {}).get("suspend_minutes", 30)
    return JSONResponse({"suspensions": live, "suspend_minutes": minutes})


@app.post("/api/suspensions")
async def post_suspension(request: Request):
    """
    Set or clear a pair's Telegram suspension. Body:
        {"symbol": "btcusdt", "suspend": true}   → mute for alerts.suspend_minutes
        {"symbol": "btcusdt", "suspend": false}  → resume immediately
    Optional "minutes" overrides the configured default for this one call (kept
    for flexibility / future per-pair durations; the dashboard omits it and relies
    on the global config value). Applies immediately — independent of the config
    Save flow — so it can't be lost among unsaved config edits.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    symbol = body.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        raise HTTPException(status_code=400, detail="symbol is required")
    symbol = symbol.strip().lower()

    suspend = body.get("suspend", True)
    if not isinstance(suspend, bool):
        raise HTTPException(status_code=400, detail="suspend must be a boolean")

    data = prune_suspensions(load_suspensions())

    if not suspend:
        data.pop(symbol, None)
        save_suspensions(data)
        return JSONResponse({"status": "resumed", "symbol": symbol,
                             "suspended_until": None, "suspensions": data})

    # Duration: explicit override, else the global configured default.
    minutes = body.get("minutes")
    if minutes is None:
        minutes = (load_config().get("alerts", {}) or {}).get("suspend_minutes", 30)
    if not isinstance(minutes, (int, float)) or isinstance(minutes, bool) or minutes <= 0:
        raise HTTPException(status_code=400, detail="minutes must be a positive number")

    expiry = (ngt_now() + timedelta(minutes=float(minutes))).isoformat()
    data[symbol] = expiry
    save_suspensions(data)
    return JSONResponse({"status": "suspended", "symbol": symbol,
                         "suspended_until": expiry, "minutes": minutes,
                         "suspensions": data})


# ── Per-(pair, issue) alert acknowledgements ─────────────────────────────────
# The narrower sibling of a suspension. A suspension mutes a whole pair for a
# fixed 30 minutes; an ack mutes ONE issue on ONE pair, with no clock — it lasts
# until that issue reaches a good state, at which point it retires itself and the
# checkbox comes back empty, ready to be ticked again on the next occurrence.
#
# The two are independent and compose: either one alone is enough to mute, and an
# acked issue on a suspended pair stays acked when the suspension lapses.
#
# Expiry is a comparison, not a delete — see ALERT_ACKS_FILE above.

@app.get("/api/alert-acks")
def get_alert_acks():
    """
    Live acknowledgements: {symbol: {issue_id: ISO acked_at}}, with any whose issue
    has since cleared pruned out. The pruned view is written back so the file
    self-cleans on read (same contract as /api/suspensions).

    `ackable` is served alongside so the dashboard renders a checkbox on exactly
    the rows the engine can actually mute, without hardcoding the list twice.
    """
    live = prune_alert_acks(load_alert_acks(), load_state())
    try:
        save_alert_acks(live)
    except Exception:
        pass    # best-effort: it just gets pruned again on the next read
    return JSONResponse({"acks": live, "ackable": sorted(ACKABLE_ISSUE_IDS)})


@app.post("/api/alert-acks")
async def post_alert_ack(request: Request):
    """
    Set or clear one acknowledgement. Body:
        {"symbol": "btcusdt", "issue_id": "B1", "ack": true}   -> mute until it clears
        {"symbol": "btcusdt", "issue_id": "B1", "ack": false}  -> un-acknowledge now

    Applies immediately, independent of the config Save flow, so it can't be lost
    among unsaved config edits.

    Setting an ack stamps NOW. That timestamp is the whole mechanism: the monitor
    compares it against the last time it saw the issue absent, so re-acking an
    issue that already cleared correctly starts a fresh mute rather than reviving
    the old one.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    symbol = body.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        raise HTTPException(status_code=400, detail="symbol is required")
    symbol = symbol.strip().lower()

    issue_id = body.get("issue_id")
    if not isinstance(issue_id, str) or not issue_id.strip():
        raise HTTPException(status_code=400, detail="issue_id is required")
    issue_id = issue_id.strip().upper()
    if issue_id not in ACKABLE_ISSUE_IDS:
        raise HTTPException(
            status_code=400,
            detail=f"issue_id must be one of {sorted(ACKABLE_ISSUE_IDS)}")

    ack = body.get("ack", True)
    if not isinstance(ack, bool):
        raise HTTPException(status_code=400, detail="ack must be a boolean")

    state = load_state()
    data  = prune_alert_acks(load_alert_acks(), state)

    if not ack:
        if symbol in data:
            data[symbol].pop(issue_id, None)
            if not data[symbol]:
                del data[symbol]        # don't leave empty per-symbol shells behind
        save_alert_acks(data)
        return JSONResponse({"status": "cleared", "symbol": symbol,
                             "issue_id": issue_id, "acked_at": None, "acks": data})

    acked_at = ngt_now().isoformat()
    data.setdefault(symbol, {})[issue_id] = acked_at
    save_alert_acks(data)
    return JSONResponse({"status": "acked", "symbol": symbol, "issue_id": issue_id,
                         "acked_at": acked_at, "acks": data})


# ── G1 USDTNGN depth-walk slippage ──────────────────────────────────────────
# Two endpoints serve the same conceptual dataset at different resolutions:
#   /raw     — the in-progress hourly bucket, 5s-resolution samples (last ≤1h)
#   /history — condensed hourly averages, one point per past hour, up to
#              condensed_retention_days back
# The dashboard stitches them together for a selectable time window; the
# stat card just averages whatever points fall inside the window (raw and
# hourly samples are treated as equal-weight per user spec).

def _parse_iso_bound(bound: Optional[str]) -> Optional[datetime]:
    """
    Parse a ?start=/?end= query bound into an NGT-aware datetime, or None if it's
    absent/unparseable (callers treat None as "unbounded").

    Query-string decoding turns "+" into " " — e.g. "2026-07-02T00:00:00+01:00"
    arrives as "2026-07-02T00:00:00 01:00". Flip it back before ISO parsing so
    clients that forget to percent-encode don't silently get an unfiltered result.

    Bare date/datetime strings arrive without tz — assume NGT for consistency with
    how the monitor writes ts values.
    """
    if not bound:
        return None
    if " " in bound and "T" in bound:
        head, sep, tail = bound.rpartition(" ")
        if ":" in tail and len(tail) <= 6:
            bound = head + "+" + tail
    try:
        dt = datetime.fromisoformat(bound)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=NIGERIAN_TZ)
    return dt


def _load_json_file(path: Path, fallback):
    """Read a JSON file or return the fallback if missing/malformed. Isolated so
    a corrupt file on disk can't take down the API — the dashboard just sees an
    empty series until the next cycle rewrites the file."""
    if not path.exists():
        return fallback
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return fallback


@app.get("/api/usdtngn-slippage/raw")
def get_usdtngn_slippage_raw():
    """
    Current in-progress raw bucket: bucket_start (ISO NGT) + up to
    ~1h of 5s samples. Each sample carries ts, mid, weighted_avg_buy/sell,
    buy/sell_slip_pct, partial_fill flags, and the g1 boolean.

    Also returns the CURRENT config weights (weight_usdt, mid_weight_usdt) so
    the dashboard can accurately label its axes/legends without a separate
    /api/config round-trip on every render — and stay in sync when the
    operator changes those values.
    """
    raw = _load_json_file(DEPTH_WALK_RAW_FILE, {"bucket_start": None, "samples": []})
    cfg = load_config()
    dw  = cfg.get("depth_walk", {}) or {}
    up  = dw.get("uptime", {}) or {}
    # Band half-width is a PERCENT of live mid: p = n/s*100 (n = fixed 1₦ step,
    # s = target price). Mirror debug.py's s<=0 guard — fall back to the config
    # default s rather than dividing by zero — and expose the EFFECTIVE s so p
    # and s stay coherent on the tab labels.
    try:
        _up_ref = float(up.get("reference_price"))
    except (TypeError, ValueError):
        _up_ref = 0.0
    if _up_ref <= 0:
        _up_ref = float(default_config()["depth_walk"]["uptime"]["reference_price"])
    _band_pct = UPTIME_FIXED_STEP_NGN / _up_ref * 100.0
    raw["config"] = {
        "weight_usdt":     dw.get("weight_usdt"),
        "mid_weight_usdt": dw.get("mid_weight_usdt"),
        # How often the monitor is SUPPOSED to append a sample. The dashboard
        # derives its "is this feed still alive" threshold from this rather than
        # hardcoding one, so retuning the poll rate can't turn a healthy feed
        # into a permanent stale warning (or hide a dead one).
        "poll_interval_seconds": dw.get("poll_interval_seconds"),
        "uptime": {
            "reference_price": _up_ref,     # effective s (target price)
            "weight_usdt":     up.get("weight_usdt"),
            "band_pct":        _band_pct,   # p = n/s*100, the graphed/live band
        },
    }
    return JSONResponse(_sanitize(raw))


@app.get("/api/usdtngn-slippage/history")
def get_usdtngn_slippage_history(start: Optional[str] = None,
                                   end:   Optional[str] = None):
    """
    Condensed hourly averages. Optional ?start=&end= (ISO date or datetime,
    inclusive on both ends) narrows the returned window; omitted bounds return
    everything retained on disk. Points are already in chronological order as
    written by the monitor.
    """
    condensed = _load_json_file(DEPTH_WALK_CONDENSED_FILE, [])
    if not isinstance(condensed, list):
        condensed = []

    start_dt = _parse_iso_bound(start)
    end_dt   = _parse_iso_bound(end)

    out = []
    for pt in condensed:
        try:
            ts = datetime.fromisoformat(pt["ts"])
        except (KeyError, ValueError, TypeError):
            continue
        if start_dt and ts < start_dt:
            continue
        if end_dt and ts > end_dt:
            continue
        out.append(pt)

    return JSONResponse(_sanitize({
        "start":  start,
        "end":    end,
        "points": out,
    }))


# ── USDTNGN hourly base volume ───────────────────────────────────────────────
# Two endpoints, both USDT (BASE) volume — deliberately NOT the naira quote
# volume D1 thresholds on:
#   /api/usdtngn-volume          — hourly series for a window
#   /api/usdtngn-volume/rolling  — trailing-60m total, rebuilt from 1-min candles
#
# Unlike the slippage endpoints (pure file reads), these call Quidax at request
# time, which is what makes any window work without waiting for data to
# accumulate. But the reach is short: responses are clamped to 300 candles and
# anchored at the newest, so a live call covers only ~12.5 days of hours. The
# monitor's archive is merged underneath for everything older, and for windows
# past that wall it is the only source — see debug.py's kline_volume_loop.
#
# These routes are sync `def` like every other route here, so FastAPI runs them in
# its threadpool and kline_volume's blocking urllib fetch never touches the event
# loop. Don't convert them to `async def` without also switching to an async
# client.

# Request-time cache so the dashboard's 5s poll can't hammer Quidax. Keys are
# built from HOUR- or MINUTE-floored window bounds, so a moving window
# ("last 24h") produces a STABLE key for the whole hour/minute it's valid for —
# without that, every poll would be a cache miss on a slightly different window.
# Single-process uvicorn, so a plain dict is sufficient.
_VOLUME_CACHE: dict = {}
_VOLUME_CACHE_TTL_SECONDS = 90


def _volume_cache_get(key):
    hit = _VOLUME_CACHE.get(key)
    if not hit:
        return None
    expires_at, payload = hit
    if ngt_now() >= expires_at:
        _VOLUME_CACHE.pop(key, None)
        return None
    return payload


def _volume_cache_set(key, payload):
    _VOLUME_CACHE[key] = (ngt_now() + timedelta(seconds=_VOLUME_CACHE_TTL_SECONDS), payload)
    # Bound the cache — window bounds roll forward forever, so without this the
    # dict would grow one entry per distinct window for the process's lifetime.
    if len(_VOLUME_CACHE) > 64:
        for stale_key in [k for k, (exp, _) in _VOLUME_CACHE.items() if ngt_now() >= exp]:
            _VOLUME_CACHE.pop(stale_key, None)
    return payload


@app.get("/api/usdtngn-volume")
def get_usdtngn_volume(start: Optional[str] = None, end: Optional[str] = None):
    """
    Hourly USDTNGN base volume (USDT) for a window. Optional ?start=&end= (ISO
    date or datetime, NGT assumed); defaults to the last 24 hours.

    Both bounds snap to hour boundaries, and the upper bound is capped at the
    current hour — the in-progress hour is never included, because the k-line API
    has no candle for it at any period (verified: at 11:41 the newest 60m candle
    is 10:00). The trailing-60m endpoint below covers "right now" instead.

    Points are {ts, volume} in chronological order. `archive_only_points` counts
    how many came from the monitor's file because the live call couldn't reach
    them — expect this to be most of a 30-day window, since a live call only
    reaches ~300 hours back. `reachable` is false whenever the window starts
    beyond that wall, and `stale` is true when the live fetch failed outright and
    the response is archive-only. The dashboard surfaces both rather than showing
    a silently short series.
    """
    now = ngt_now()
    current_hour = klv.floor_to_period(now, klv.HOUR_MINUTES)

    start_dt = _parse_iso_bound(start) or (current_hour - timedelta(hours=24))
    end_dt   = _parse_iso_bound(end)   or now
    # A DATE-only bound means the whole day, matching kline_volume.py's CLI —
    # "?end=2026-08-11" should give all 24 hours of the 11th, not just its 00:00
    # candle. A full datetime is honoured as given, which is what the dashboard
    # sends (its archive pickers already expand to T23:59:59).
    if end and "T" not in end:
        end_dt = end_dt + timedelta(days=1) - timedelta(seconds=1)
    start_dt = klv.floor_to_period(start_dt, klv.HOUR_MINUTES)
    # end is inclusive-of-that-hour for the caller; internally we want a half-open
    # upper bound, hence +1h — then capped so we never ask for the open hour.
    end_excl = min(klv.floor_to_period(end_dt, klv.HOUR_MINUTES) + timedelta(hours=1),
                   current_hour)

    if end_excl <= start_dt:
        # Window is entirely inside the current (unfinished) hour, or inverted.
        return JSONResponse({"symbol": VOLUME_SYMBOL, "unit": "USDT",
                             "start": start_dt.isoformat(), "end": end_excl.isoformat(),
                             "points": [], "total": 0.0, "expected_count": 0,
                             "archive_only_points": 0, "reachable": True, "stale": False})

    cache_key = ("window", start_dt.isoformat(), end_excl.isoformat())
    cached = _volume_cache_get(cache_key)
    if cached is not None:
        return JSONResponse(cached)

    # Archive first — it's the only source for hours past the API's reach.
    archive = _load_json_file(VOLUME_ARCHIVE_FILE, [])
    if not isinstance(archive, list):
        archive = []
    by_ts = {}
    for pt in archive:
        try:
            ts = datetime.fromisoformat(pt["ts"])
        except (KeyError, ValueError, TypeError):
            continue
        if start_dt <= ts < end_excl:
            by_ts[pt["ts"]] = {"ts": pt["ts"], "volume": pt.get("volume")}

    # Live fetch layered on top: fresher, and covers the last ~300 hours even if
    # the monitor has never run. A failure here degrades to archive-only rather
    # than a 500 — same spirit as _load_json_file above.
    reachable, stale = True, False
    live_ts: set = set()
    try:
        live = klv.hourly_series(VOLUME_SYMBOL, start_dt, end_excl, now=now)
        reachable = live["reachable"]
        for pt in live["series"]:
            by_ts[pt["ts"]] = pt
            live_ts.add(pt["ts"])
    except RuntimeError:
        stale = True

    points = [by_ts[ts] for ts in sorted(by_ts)]
    payload = _sanitize({
        "symbol":              VOLUME_SYMBOL,
        "unit":                "USDT",
        "start":               start_dt.isoformat(),
        "end":                 end_excl.isoformat(),
        "points":              points,
        "total":               sum(p["volume"] for p in points if p.get("volume") is not None),
        "expected_count":      int((end_excl - start_dt).total_seconds() // 3600),
        # Points the live call couldn't supply — i.e. served purely from the
        # monitor's archive. Non-zero means the window reaches past the API wall
        # (or the live fetch failed), which is exactly when the archive earns
        # its keep.
        "archive_only_points": sum(1 for p in points if p["ts"] not in live_ts),
        "reachable":           reachable,
        "stale":               stale,
    })
    return JSONResponse(_volume_cache_set(cache_key, payload))


@app.get("/api/usdtngn-volume/rolling")
def get_usdtngn_volume_rolling(minutes: int = 60):
    """
    Trailing-window USDTNGN base volume (USDT), default the last 60 minutes,
    rebuilt from 1-MINUTE candles.

    Minute candles rather than the hourly feed because there is no in-progress
    hourly candle to read — see kline_volume.trailing_window_total. The window
    ends at the last CLOSED minute, so the figure lags real time by up to ~1
    minute; `start`/`end` say exactly what was covered so the dashboard can label
    it honestly instead of implying it's to-the-second.

    retrieved_count vs expected_count distinguishes a genuinely quiet hour from a
    feed with holes: a zero-trade minute still returns a candle with volume 0.
    """
    if minutes <= 0 or minutes > 1440:
        raise HTTPException(status_code=400, detail="minutes must be between 1 and 1440")

    now = ngt_now()
    # Minute-floored key: one upstream call per minute at most, regardless of how
    # many dashboards are polling every 5s.
    cache_key = ("rolling", minutes, klv.floor_to_period(now, 1).isoformat())
    cached = _volume_cache_get(cache_key)
    if cached is not None:
        return JSONResponse(cached)

    try:
        result = klv.trailing_window_total(VOLUME_SYMBOL, minutes=minutes, now=now)
    except RuntimeError as e:
        # Quidax unreachable — report it as data-unavailable rather than a 500 so
        # the stat card can show "—" while the rest of the tab keeps working.
        return JSONResponse({"symbol": VOLUME_SYMBOL, "unit": "USDT", "minutes": minutes,
                             "volume": None, "stale": True, "error": str(e)})

    result.update({"symbol": VOLUME_SYMBOL, "unit": "USDT",
                   "minutes": minutes, "stale": False})
    return JSONResponse(_sanitize(result))


@app.get("/api/config")
def get_config():
    """
    Current monitor configuration (thresholds, timing, pairs).
    Returns the merged result of DEFAULT_CONFIG + any saved overrides,
    plus _meta.config_file so you can verify both processes share the same path.
    """
    cfg = load_config()
    cfg["_meta"] = {
        "config_file": str(CONFIG_FILE.resolve()),
        "config_file_exists": CONFIG_FILE.exists(),
    }
    return JSONResponse(cfg)


@app.post("/api/config")
async def post_config(request: Request):
    """
    Save updated configuration. Accepts a full or partial config JSON body.
    The monitor process picks up the new values on its next cycle.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Special case: reset to defaults
    if body.get("_reset"):
        fresh = default_config()
        save_config(fresh)
        return JSONResponse({"status": "reset", "config": fresh})

    if "pairs" in body:
        if not isinstance(body["pairs"], list):
            raise HTTPException(status_code=400, detail="pairs must be a list")
        for item in body["pairs"]:
            if not (isinstance(item, (list, tuple)) and len(item) in (2, 3)):
                raise HTTPException(status_code=400,
                    detail="Each pair must be [symbol, target_or_null] or [symbol, target_or_null, aliases]")
            sym, tgt = item[0], item[1]
            aliases = item[2] if len(item) == 3 else None
            if not isinstance(sym, str) or not sym.strip():
                raise HTTPException(status_code=400, detail=f"Invalid symbol: {sym!r}")
            if tgt is not None and not isinstance(tgt, (int, float)):
                raise HTTPException(status_code=400,
                    detail=f"Target for {sym} must be a number or null")
            if aliases is not None:
                if not isinstance(aliases, dict):
                    raise HTTPException(status_code=400,
                        detail=f"Aliases for {sym} must be an object or null")
                for key, val in aliases.items():
                    if key not in ("mexc", "kucoin"):
                        raise HTTPException(status_code=400,
                            detail=f"Unknown alias key '{key}' for {sym} — only 'mexc'/'kucoin' allowed")
                    if val is not None and not (isinstance(val, str) and val.strip()):
                        raise HTTPException(status_code=400,
                            detail=f"Alias '{key}' for {sym} must be a non-empty string or null")

    # volume_spike has string enums (mode, warmup_fallback) so it can't go through
    # the numbers-only validator below — validate it explicitly.
    if "volume_spike" in body and isinstance(body["volume_spike"], dict):
        vs = body["volume_spike"]
        if "mode" in vs and vs["mode"] not in ("baseline_relative", "absolute"):
            raise HTTPException(status_code=400,
                detail="volume_spike.mode must be 'baseline_relative' or 'absolute'")
        if "warmup_fallback" in vs and vs["warmup_fallback"] not in ("absolute", "suppress"):
            raise HTTPException(status_code=400,
                detail="volume_spike.warmup_fallback must be 'absolute' or 'suppress'")
        for k in ("spike_ratio", "min_baseline_buckets"):
            if k in vs:
                v = vs[k]
                if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
                    raise HTTPException(status_code=400,
                        detail=f"volume_spike.{k} must be a non-negative number")
        if isinstance(vs.get("spike_ratio"), (int, float)) and vs["spike_ratio"] <= 0:
            raise HTTPException(status_code=400,
                detail="volume_spike.spike_ratio must be greater than 0")
        # D1's own k-line window (candle_minutes/lookback_minutes/baseline_buckets) —
        # independent of kline.* (B4-only). Must be strictly positive: a 0-minute
        # candle/lookback or a 0-bucket baseline is meaningless, unlike spike_ratio/
        # min_baseline_buckets above which tolerate 0.
        for k in ("candle_minutes", "lookback_minutes", "baseline_buckets"):
            if k in vs:
                v = vs[k]
                if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
                    raise HTTPException(status_code=400,
                        detail=f"volume_spike.{k} must be a positive number")

    # pricing.source_divergence_overrides is a per-symbol map {symbol: pct}, not a
    # scalar — it can't go through the numbers-only validator below, so validate it
    # explicitly (mirrors the volume_spike special-case) and skip it in that loop.
    if "pricing" in body and isinstance(body["pricing"], dict):
        ov = body["pricing"].get("source_divergence_overrides")
        if ov is not None:
            if not isinstance(ov, dict):
                raise HTTPException(status_code=400,
                    detail="pricing.source_divergence_overrides must be an object")
            for sym, val in ov.items():
                if not isinstance(sym, str) or not sym.strip():
                    raise HTTPException(status_code=400,
                        detail=f"Invalid override symbol: {sym!r}")
                if not isinstance(val, (int, float)) or isinstance(val, bool) or val < 0:
                    raise HTTPException(status_code=400,
                        detail=f"source_divergence_overrides[{sym}] must be a non-negative number")

    # alerts.suspend_minutes is a duration, not a threshold — must be strictly
    # positive (a 0-minute suspend is meaningless). Validated explicitly so it's
    # skipped by the non-negative numeric loop below.
    if "alerts" in body and isinstance(body["alerts"], dict):
        sm = body["alerts"].get("suspend_minutes")
        if sm is not None:
            if not isinstance(sm, (int, float)) or isinstance(sm, bool) or sm <= 0:
                raise HTTPException(status_code=400,
                    detail="alerts.suspend_minutes must be a positive number")

    # depth_walk.uptime is a nested {reference_price, weight_usdt} object, not a
    # scalar — validate it explicitly (mirrors volume_spike) and skip it in the
    # numbers-only loop below.
    if "depth_walk" in body and isinstance(body["depth_walk"], dict):
        up = body["depth_walk"].get("uptime")
        if up is not None:
            if not isinstance(up, dict):
                raise HTTPException(status_code=400,
                    detail="depth_walk.uptime must be an object")
            for k in ("reference_price", "weight_usdt"):
                if k in up and up[k] is not None:
                    v = up[k]
                    if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
                        raise HTTPException(status_code=400,
                            detail=f"depth_walk.uptime.{k} must be a non-negative number")

    for section in ("timing", "orderbook", "pricing", "kline", "layer_churn", "depth_walk"):
        if section in body and isinstance(body[section], dict):
            for k, v in body[section].items():
                if section == "pricing" and k == "source_divergence_overrides":
                    continue  # nested map, validated explicitly above
                if section == "depth_walk" and k == "uptime":
                    continue  # nested object, validated explicitly above
                if v is not None and not isinstance(v, (int, float)):
                    raise HTTPException(status_code=400,
                        detail=f"{section}.{k} must be a number")
                if isinstance(v, (int, float)) and v < 0:
                    raise HTTPException(status_code=400,
                        detail=f"{section}.{k} must be non-negative")

    # Layer the validated edit over the current config using the same merge
    # semantics as load — shared with debug.py via defaults.merge_config.
    current = merge_config(body, base=load_config())

    save_config(current)
    return JSONResponse({"status": "saved", "config": current})