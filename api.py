"""
Quidax Market Monitor — Dashboard API
--------------------------------------
Serves latest.csv + daily log + state as JSON for the dashboard.

Run:
    uvicorn api:app --host 0.0.0.0 --port 8000 --reload

Endpoints:
    GET /api/status          → latest.csv parsed as JSON (all pairs, current cycle)
    GET /api/history         → today's daily log CSV as JSON (all checks so far today)
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

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse


def _sanitize(obj):
    """Recursively replace float nan/inf with None so json.dumps never chokes."""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj

# ── Config ────────────────────────────────────────────────────────────────────
DATA_DIR    = Path("data")
LATEST_CSV  = DATA_DIR / "latest.csv"
STATE_FILE  = DATA_DIR / "health_state.json"
STATIC_DIR  = Path(".")          # dashboard.html lives next to api.py
NIGERIAN_TZ = timezone(timedelta(hours=1))

app = FastAPI(title="Quidax Market Monitor API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # tighten in production
    allow_methods=["GET"],
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
    for col in ("monitor_only", "should_alert", "dws_poor"):
        if col in df.columns:
            df[col] = df[col].map(
                lambda v: str(v).strip().lower() in ("true", "1", "yes")
                if pd.notna(v) else False
            )

    # Numeric coercion (percent_diff / imbalance_ratio may be "N/A")
    for col in ("current_spread", "spread_abs", "percent_diff",
                "mid_price", "dws", "imbalance_ratio",
                "ask_layers", "bid_layers"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    records = df.to_dict(orient="records")
    return _sanitize(records)


def parse_daily_log() -> list[dict]:
    today = ngt_now().strftime("%Y-%m-%d")
    pattern = str(DATA_DIR / f"daily_log_{today}.csv")
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
    alerted  = sum(1 for r in records if r.get("should_alert"))
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
def get_history():
    """
    Today's full daily log — every check recorded for each pair, useful
    for the spread history chart (each STATUS/TIME column = one cycle).
    """
    rows = parse_daily_log()
    return JSONResponse({
        "date": ngt_now().strftime("%Y-%m-%d"),
        "rows": rows,
    })


@app.get("/api/state")
def get_state():
    """
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


@app.get("/api/pairs")
def get_pairs():
    """
    List of known pairs derived from health_state.json keys
    (populated after the first monitor cycle runs).
    """
    state = load_state()
    pairs = list(state.keys())
    return JSONResponse({"pairs": pairs})