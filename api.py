"""
Quidax Market Monitor — Dashboard API
--------------------------------------
Serves latest.csv + daily log + state as JSON for the dashboard.

Run:
    uvicorn api:app --host 0.0.0.0 --port 8000 --reload

Endpoints:
    GET /api/status          → latest.csv parsed as JSON (all pairs, current cycle)
    GET /api/history         → daily log CSV as JSON; optional ?date=YYYY-MM-DD (defaults to today)
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

import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from defaults import default_config, merge_config, UPTIME_FIXED_STEP_NGN  # single source of truth for config


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
# DATA_DIR    = Path(__file__).parent / "data"
DATA_DIR = Path("/app/data")
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
# G1 depth-walk slippage tracker files (written by debug.py's depth_walk_loop)
DEPTH_WALK_RAW_FILE       = DATA_DIR / "usdtngn_slippage_raw.json"
DEPTH_WALK_CONDENSED_FILE = DATA_DIR / "usdtngn_slippage_hourly.json"
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
    for col in ("monitor_only", "should_alert", "telegram_fired", "dws_poor", "d1_spike"):
        if col in df.columns:
            df[col] = df[col].map(
                lambda v: str(v).strip().lower() in ("true", "1", "yes")
                if pd.notna(v) else False
            )

    # Numeric coercion (percent_diff / imbalance_ratio may be "N/A")
    for col in ("current_spread", "spread_abs", "percent_diff",
                "mid_price", "dws", "imbalance_ratio",
                "ask_layers", "bid_layers", "trusted_ref",
                "layer_churn_pct", "layer_churn_baseline_pct",
                "d1_window_volume", "d1_threshold"):
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


def summary_stats(records: list[dict]) -> dict:
    total    = len(records)
    warnings = sum(1 for r in records if str(r.get("status", "")).lower() == "warning")
    alerted  = sum(1 for r in records if r.get("telegram_fired"))
    healthy  = total - warnings
    ts       = records[0].get("timestamp") if records else None
    return {
        "total_pairs": total,
        "healthy":     healthy,
        "warnings":    warnings,
        "alerts_fired": alerted,
        "last_updated": ts,
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


# ── G1 USDTNGN depth-walk slippage ──────────────────────────────────────────
# Two endpoints serve the same conceptual dataset at different resolutions:
#   /raw     — the in-progress hourly bucket, 5s-resolution samples (last ≤1h)
#   /history — condensed hourly averages, one point per past hour, up to
#              condensed_retention_days back
# The dashboard stitches them together for a selectable time window; the
# stat card just averages whatever points fall inside the window (raw and
# hourly samples are treated as equal-weight per user spec).

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

    def _parse(bound: Optional[str]) -> Optional[datetime]:
        if not bound:
            return None
        # Query-string decoding turns "+" into " " — e.g. "2026-07-02T00:00:00+01:00"
        # arrives as "2026-07-02T00:00:00 01:00". Flip it back before ISO parsing so
        # clients that forget to percent-encode don't silently get an unfiltered result.
        if " " in bound and "T" in bound:
            head, sep, tail = bound.rpartition(" ")
            if ":" in tail and len(tail) <= 6:
                bound = head + "+" + tail
        try:
            dt = datetime.fromisoformat(bound)
        except ValueError:
            return None
        # Bare date/datetime strings arrive without tz — assume NGT for
        # consistency with how the monitor writes ts values.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=NIGERIAN_TZ)
        return dt

    start_dt = _parse(start)
    end_dt   = _parse(end)

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