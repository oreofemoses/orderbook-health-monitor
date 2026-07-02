"""
Quidax Market Monitor — Dashboard API
--------------------------------------
Serves latest.csv + daily log + state as JSON for the dashboard.

Run:
    uvicorn api:app --host 0.0.0.0 --port 8000 --reload

Endpoints:
    GET /api/status          → latest.csv parsed as JSON (all pairs, current cycle)
<<<<<<< HEAD
    GET /api/history         → daily log CSV as JSON; optional ?date=YYYY-MM-DD (defaults to today)
=======
    GET /api/history         → today's daily log CSV as JSON (all checks so far today)
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
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
<<<<<<< HEAD
from typing import Any, Optional
=======
from typing import Any
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b

import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

<<<<<<< HEAD
from defaults import default_config, merge_config  # single source of truth for config

=======
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b

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
<<<<<<< HEAD
# DATA_DIR    = Path(__file__).parent / "data"
DATA_DIR = Path("/app/data")
=======
DATA_DIR    = Path(__file__).parent / "data"
# DATA_DIR = Path("/app/data")
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
LATEST_CSV  = DATA_DIR / "latest.csv"
STATE_FILE  = DATA_DIR / "health_state.json"
CONFIG_FILE = DATA_DIR / "monitor_config.json"
STATIC_DIR  = Path(".")          # dashboard.html lives next to api.py
NIGERIAN_TZ = timezone(timedelta(hours=1))

<<<<<<< HEAD
# ── Default config ────────────────────────────────────────────────────────────
# Canonical defaults + merge semantics now live in defaults.py, imported above
# and shared verbatim with debug.py so the two processes can never drift.


def load_config() -> dict:
    """Stored monitor_config.json merged over the shared defaults (defaults fill gaps)."""
=======
# ── Default config (mirrors debug.py defaults) ────────────────────────────────
DEFAULT_CONFIG: dict[str, Any] = {
    "timing": {
        "anomaly_alert_after_minutes": 10,
        "alert_cooldown_minutes":      30,
        "cycle_sleep_seconds":         60,
    },
    "orderbook": {
        "depth_limit":              200,
        "min_orderbook_layers":     10,
        "thin_depth_threshold":     5000,
        "depth_imbalance_ratio":    5.0,
        "stale_ob_cycles":          3,
        "mid_price_alert_threshold": 25,
        "dws_poor_threshold":       0.5,
        "min_abs_spread_diff_pct":  0.05,
    },
    "kline": {
        "candle_minutes":   1,
        "lookback_minutes": 60,
    },
    "pairs": [
        ["aaveusdt",     0.3   ],
        ["adausdt",      2.0   ],
        ["algousdt",     2.0   ],
        ["bchusdt",      1.20  ],
        ["bnbusdt",      0.3   ],
        ["bonkusdt",     2.0   ],
        ["btcusdt",      0.2   ],
        ["cakeusdt",     0.3   ],
        ["cfxusdt",      2.0   ],
        ["dashusdt",     2.0   ],
        ["dotusdt",      0.26  ],
        ["dogeusdt",     0.26  ],
        ["ethusdt",      0.25  ],
        ["fartcoinusdt", 2.0   ],
        ["flokiusdt",    0.5   ],
        ["hypeusdt",     2.0   ],
        ["linkusdt",     0.26  ],
        ["lskusdt",      1.5   ],
        ["ltcusdt",      0.3   ],
        ["pepeusdt",     0.5   ],
        ["polusdt",      0.5   ],
        ["rndrusdt",     2.0   ],
        ["shibusdt",     0.4   ],
        ["slpusdt",      2.0   ],
        ["solusdt",      0.25  ],
        ["suiusdt",      2.0   ],
        ["tonusdt",      0.3   ],
        ["trxusdt",      0.3   ],
        ["usdcusdt",     0.02  ],
        ["wifusdt",      2.0   ],
        ["xlmusdt",      0.3   ],
        ["xrpusdt",      0.3   ],
        ["xyousdt",      1.0   ],
        ["usdtcngn",     None  ],
        ["btcngn",       0.7   ],
        ["usdtngn",      0.95  ],
        ["ethngn",       0.75  ],
        ["trxngn",       0.75  ],
        ["xrpngn",       0.5   ],
        ["dashngn",      0.5   ],
        ["ltcngn",       0.5   ],
        ["solngn",       0.8   ],
        ["usdcngn",      1.2   ],
        ["cngnngn",      None  ],
        ["usdtghs",      1.3   ],
    ],
}


def load_config() -> dict:
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                stored = json.load(f)
<<<<<<< HEAD
            return merge_config(stored)
        except Exception:
            pass
    return default_config()
=======
            # Merge: stored values override defaults, missing keys fall back
            merged = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
            for section, values in stored.items():
                if section == "pairs":
                    merged["pairs"] = values
                elif isinstance(values, dict) and section in merged:
                    merged[section].update(values)
                else:
                    merged[section] = values
            return merged
        except Exception:
            pass
    return json.loads(json.dumps(DEFAULT_CONFIG))
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b


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
<<<<<<< HEAD
    for col in ("monitor_only", "should_alert", "telegram_fired", "dws_poor", "d1_spike"):
=======
    for col in ("monitor_only", "should_alert", "dws_poor"):
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
        if col in df.columns:
            df[col] = df[col].map(
                lambda v: str(v).strip().lower() in ("true", "1", "yes")
                if pd.notna(v) else False
            )

    # Numeric coercion (percent_diff / imbalance_ratio may be "N/A")
    for col in ("current_spread", "spread_abs", "percent_diff",
                "mid_price", "dws", "imbalance_ratio",
<<<<<<< HEAD
                "ask_layers", "bid_layers", "trusted_ref",
                "layer_churn_pct", "layer_churn_baseline_pct",
                "d1_window_volume", "d1_threshold"):
=======
                "ask_layers", "bid_layers"):
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    records = df.to_dict(orient="records")
    return _sanitize(records)


<<<<<<< HEAD
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
=======
def parse_daily_log() -> list[dict]:
    today = ngt_now().strftime("%Y-%m-%d")
    pattern = str(DATA_DIR / f"daily_log_{today}.csv")
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
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


def summary_stats(records: list[dict]) -> dict:
    total    = len(records)
    warnings = sum(1 for r in records if str(r.get("status", "")).lower() == "warning")
<<<<<<< HEAD
    alerted  = sum(1 for r in records if r.get("telegram_fired"))
=======
    alerted  = sum(1 for r in records if r.get("should_alert"))
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
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


<<<<<<< HEAD
@app.get("/favicon.ico")
def serve_favicon():
    path = STATIC_DIR / "favicon.ico"
    if not path.exists():
        raise HTTPException(status_code=404, detail="favicon.ico not found next to api.py")
    return FileResponse(path)


=======
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
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
<<<<<<< HEAD
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
=======
def get_history():
    """
    Today's full daily log — every check recorded for each pair, useful
    for the spread history chart (each STATUS/TIME column = one cycle).
    """
    rows = parse_daily_log()
    return JSONResponse({
        "date": ngt_now().strftime("%Y-%m-%d"),
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
        "rows": rows,
    })


@app.get("/api/state")
def get_state():
    """
<<<<<<< HEAD
    Raw health_state.json. Per pair: an "_alert" sub-key (Tier-2 consecutive
    counters + per-issue cooldown expiries) and the last observed mid price with
    its timestamp (last_mid / last_mid_ts, NGT ISO — a stale timestamp means the
    pair's mid hasn't been observable since then). Plus the engine's rolling
    reference-feed history (_ref_hist), volume baselines (_vol_hist), layer-churn
    baselines (_layer_hist), and global cooldowns (_global).
    """
    return JSONResponse(load_state())
=======
    Raw health_state.json — anomaly timers, last alert timestamps,
    stale OB counters, last mid-price per pair.
    """
    state = load_state()
    # Augment each pair with a human-readable anomaly age
    now = ngt_now()
    for sym, data in state.items():
        if data.get("anomaly_since"):
            try:
                since = datetime.fromisoformat(data["anomaly_since"])
                age_s = (now - since).total_seconds()
                data["anomaly_age_minutes"] = round(age_s / 60, 1)
            except Exception:
                data["anomaly_age_minutes"] = None
        else:
            data["anomaly_age_minutes"] = None
    return JSONResponse(state)
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b


@app.get("/api/pairs")
def get_pairs():
    """
    List of known pairs derived from health_state.json keys
    (populated after the first monitor cycle runs).
    """
    state = load_state()
    pairs = list(state.keys())
    return JSONResponse({"pairs": pairs})

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
<<<<<<< HEAD
        fresh = default_config()
        save_config(fresh)
        return JSONResponse({"status": "reset", "config": fresh})
=======
        save_config(json.loads(json.dumps(DEFAULT_CONFIG)))
        return JSONResponse({"status": "reset", "config": DEFAULT_CONFIG})
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b

    if "pairs" in body:
        if not isinstance(body["pairs"], list):
            raise HTTPException(status_code=400, detail="pairs must be a list")
        for item in body["pairs"]:
<<<<<<< HEAD
            if not (isinstance(item, (list, tuple)) and len(item) in (2, 3)):
                raise HTTPException(status_code=400,
                    detail="Each pair must be [symbol, target_or_null] or [symbol, target_or_null, aliases]")
            sym, tgt = item[0], item[1]
            aliases = item[2] if len(item) == 3 else None
=======
            if not (isinstance(item, (list, tuple)) and len(item) == 2):
                raise HTTPException(status_code=400,
                    detail="Each pair must be [symbol, target_or_null]")
            sym, tgt = item
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
            if not isinstance(sym, str) or not sym.strip():
                raise HTTPException(status_code=400, detail=f"Invalid symbol: {sym!r}")
            if tgt is not None and not isinstance(tgt, (int, float)):
                raise HTTPException(status_code=400,
                    detail=f"Target for {sym} must be a number or null")
<<<<<<< HEAD
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

    for section in ("timing", "orderbook", "pricing", "kline", "layer_churn"):
        if section in body and isinstance(body[section], dict):
            for k, v in body[section].items():
                if section == "pricing" and k == "source_divergence_overrides":
                    continue  # nested map, validated explicitly above
=======

    for section in ("timing", "orderbook", "kline"):
        if section in body and isinstance(body[section], dict):
            for k, v in body[section].items():
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b
                if v is not None and not isinstance(v, (int, float)):
                    raise HTTPException(status_code=400,
                        detail=f"{section}.{k} must be a number")
                if isinstance(v, (int, float)) and v < 0:
                    raise HTTPException(status_code=400,
                        detail=f"{section}.{k} must be non-negative")

<<<<<<< HEAD
    # Layer the validated edit over the current config using the same merge
    # semantics as load — shared with debug.py via defaults.merge_config.
    current = merge_config(body, base=load_config())
=======
    current = load_config()
    for section, values in body.items():
        if section == "pairs":
            current["pairs"] = values
        elif isinstance(values, dict) and section in current:
            current[section].update(values)
        else:
            current[section] = values
>>>>>>> 2548ad4ca4a5f0786f75e1c0fe9662135c71e73b

    save_config(current)
    return JSONResponse({"status": "saved", "config": current})