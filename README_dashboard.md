# Quidax Market Monitor — Dashboard API

## Files
```
api.py           ← FastAPI server
dashboard.html   ← Frontend (served at GET /)
data/
  latest.csv     ← Written by quidax_monitor.py each cycle
  health_state.json
  daily_log_YYYY-MM-DD.csv
```

## Setup

```bash
pip install fastapi uvicorn pandas
```

## Run

Make sure both files are in the **same directory** as your monitor, then:

```bash
uvicorn api:app --host 0.0.0.0 --port 8000 --reload
```

Open **http://localhost:8000** in your browser.

The dashboard auto-refreshes every 60 seconds.
To watch it live, just keep your monitor running alongside uvicorn.

## API Endpoints

| Endpoint | Returns |
|---|---|
| `GET /` | dashboard.html |
| `GET /api/status` | latest.csv as JSON + summary counts |
| `GET /api/history` | today's daily_log CSV as JSON |
| `GET /api/state` | health_state.json with anomaly age in minutes |
| `GET /api/pairs` | list of known pair symbols |
| `GET /health` | liveness check |

## Production (VPS)

Run both the monitor and the API as systemd services:

```ini
# /etc/systemd/system/quidax-monitor.service
[Unit]
Description=Quidax Market Monitor
After=network.target

[Service]
WorkingDirectory=/home/ubuntu/quidax
ExecStart=/usr/bin/python3 quidax_monitor.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/quidax-api.service
[Unit]
Description=Quidax Dashboard API
After=network.target

[Service]
WorkingDirectory=/home/ubuntu/quidax
ExecStart=/usr/bin/uvicorn api:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable quidax-monitor quidax-api
sudo systemctl start  quidax-monitor quidax-api
```

Add nginx + certbot if you want HTTPS on a domain.

Here is a detailed breakdown of each alert:

1. Order Book Anomalies (The "A" Series)

These issues are identified in the process_pair function and are subject to a
10-minute persistence rule (the issue must exist for 10 minutes before a
Telegram alert is fired).

  - A1: Crossed Order Book (CRITICAL)
      - Trigger: When the Best Bid price is greater than or equal to the Best
        Ask price.
  - A2: Spread Widening (HIGH)
      - Trigger: When the current spread deviates from the target_spread by more
        than +100% or less than -75%.
      - Gate: This only triggers an actionable alert if the DWS (Dynamic
        Weighted Spread) is also above 0.5, ensuring the spread isn't just a
        single "stray" order.
  - A2: Shallow Order Book (HIGH)
      - Trigger: When the number of order layers on either the Ask or Bid side
        falls below 10 layers.
  - A3: One-Sided Market (CRITICAL)
      - Trigger: When either the Ask side or the Bid side of the order book is
        completely empty.
  - A4: Thin Mid-Market (MEDIUM/Informational)
      - Trigger: When the total liquidity (USD equivalent) within the spread
        band is less than $5,000.
  - STALE: Stale Order Book (HIGH)
      - Trigger: When the top-of-book (best price and amount for both sides)
        remains identical for 3 consecutive cycles (3 minutes), suggesting the
        data feed or the market engine might be frozen.

2. Market Activity Alerts

These are triggered based on changes between the current cycle and the previous
cycle.

  - Price Movement Alert

      - Trigger: When the mid-price of a pair moves by 25% or more between
        one-minute cycles.
      - Action: Sends an immediate Telegram notification with 📈 or 📉 icons.

  - Trade Volume Spikes

      - Trigger: When the aggregate quote volume in the last 60 minutes exceeds
        a specific threshold.
      - Thresholds (Dynamic):
          - USDT_NGN: > 50,000,000 NGN
          - BTC/ETH/SOL/USDC pairs: > 50,000,000 NGN or $100,000 USDT
          - Other Altcoins: > 5,000,000 NGN or $5,000 USDT
          - GHS pairs: > 60,000 GHS

3. Logic & Delivery Rules

The script applies specific "filters" to prevent notification fatigue:

1.  Persistence Gate: Anomalies (like spread widening or shallow books) must
    persist for 10 minutes (ANOMALY_ALERT_AFTER_MINUTES) before the first alert
    is sent.
2.  Cooldown Period: Once an alert is sent for a pair, it will not alert again
    for that same pair/issue for 30 minutes (ALERT_COOLDOWN_MINUTES).
3.  Monitor-Only Mode: Pairs with a None target (like qdxusdt) are monitored for
    depth and price, but will never trigger a "Spread Widening" (A2) alert.
4.  Logging: Every check, regardless of whether it triggers an alert, is logged
    to a daily CSV file (data/daily_log_YYYY-MM-DD.csv).
