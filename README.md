# Quidax Market Monitor — Dashboard API

## Files
```
api.py           ← FastAPI server
dashboard.html   ← Frontend (served at GET /)
data/
  latest.csv     ← Written by quidax_monitor.py each cycle
  health_state.json
  daily_log_YYYY-MM-DD.csv
  suspensions.json   ← api writes / monitor reads: per-pair 30m Telegram mutes
  alert_acks.json    ← api writes / monitor reads: per-(pair, issue) acks
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
| `GET /api/history` | one day's daily_log CSV as JSON (`?date=`); the dashboard now reads the log through `/api/alert-log` |
| `GET /api/state` | health_state.json with anomaly age in minutes |
| `GET /api/diagnostics` | which writer last wrote what, and how long ago — start here when the dashboard looks wrong |
| `GET /api/alert-analysis` | range analytics over the daily logs; `?start=&end=` (YYYY-MM-DD, NGT), `?gap_cycles=` |
| `GET /api/alert-log` | daily-log detections for a range; `?start=&end=&tier=&market=&issue=&page=&page_size=&format=csv` |
| `GET /api/pairs` | list of known pair symbols |
| `GET /api/alert-acks` | live per-(pair, issue) acknowledgements + the ackable id list |
| `POST /api/alert-acks` | set/clear one ack: `{symbol, issue_id, ack}` |
| `GET /api/usdtngn-volume` | USDTNGN hourly base volume (USDT); `?start=&end=` (ISO date or datetime, NGT) |
| `GET /api/usdtngn-volume/rolling` | USDTNGN trailing-window volume, `?minutes=` (default 60) |
| `GET /health` | liveness check |

### USDTNGN volume

Shown on the Depth-walk slippage tab: a trailing-60m stat card plus an hourly bar
chart. Volume is **base** volume (USDT traded) — not the naira quote volume the D1
spike alert thresholds on.

Two things about the k-line API shape this feature:

- **No in-progress candle.** An hourly candle only appears once the hour closes,
  so the chart's newest bar is the last *completed* hour. The trailing-60m card
  covers "now" instead, rebuilt from 1-minute candles (so it lags by up to a minute).
- **300 candles per response, anchored at the newest.** The documented limit of
  10000 is not honoured — the server clamps to 300 at every period, and there's no
  way to page backwards. A live fetch therefore reaches only ~12.5 days at hourly
  resolution. `debug.py`'s `kline_volume_loop` writes each closed hour to
  `data/usdtngn_volume_hourly.json`, and beyond that wall the archive is the **only**
  source — so 30-day views fill in over time rather than being complete immediately.

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

## The alerts, in plain English

Everything below lives in `debug.py`. Default thresholds come from `defaults.py`
and most are editable live from the dashboard's config drawer — `apply_config()`
re-reads `monitor_config.json` at the top of every cycle, so a change takes
effect on the next cycle without a restart. Where a number is quoted here, it's
the shipped default.

### How one cycle works

Every 60 seconds (`timing.cycle_sleep_seconds`) the monitor runs a full pass:

1. **Fetch the reference exchanges once.** Two batched calls — MEXC's
   `ticker/price` and KuCoin's `allTickers` — give a price for every asset both
   exchanges list against USDT. One call each, not one per pair. If either call
   throws, that's **E2**.
2. **Resolve one "trusted price" per asset.** For each USDT-quoted asset, the two
   reference prices are checked for staleness (**B1**) and disagreement (**B2**),
   and whatever survives is averaged into a single trusted number. This happens
   once per asset, not once per pair.
3. **Check every pair in parallel** (10 at a time). Each pair fetches its order
   book plus two separate k-line feeds, and runs every A-series check, B1, B4,
   G2 and D1 against them.
4. **Cross-pair arbitrage (F1)**, which can only run once every pair's mid price
   is known.
5. **Decide what actually gets sent to Telegram** — tiers, confirmation counters,
   cooldowns, episode caps, suspensions.
6. **Persist**: `latest.csv`, the daily log, and `health_state.json`.

A separate task polls USDTNGN every 5 seconds for the depth-walk metrics (**G1**),
completely independent of this 60s loop, and another tops up the hourly volume
archive.

### The numbers every check is built from

Computed once per pair per cycle, from the raw depth payload:

- **The book itself.** Asks and bids are parsed into two tables, sorted
  best-price-first. Any level with a non-positive price *or* a non-positive size
  is dropped as a phantom — exchanges emit `[0, 0]` sentinel rows to mean "this
  side is empty", and counting those as real layers would both hide a genuinely
  one-sided book and drag the mid price to zero.
- **Mid price** = (best ask + best bid) / 2. **Spread** = best ask − best bid.
  **Spread %** = spread / mid × 100.
- **DWS (depth-weighted spread).** A size-weighted answer to "how far from mid is
  the money actually sitting?" Take the top 10 levels each side, weight each
  level's distance from mid by its size, divide by the total size, and express
  that as a % of mid. A book with a wide top-of-book gap but real size just
  behind it scores *low* here; a book that's genuinely hollow scores high. This
  is what lets A2 tell a cosmetic spread apart from a real one.
- **Liquidity depth in a band.** The naira/dollar value of all resting orders
  priced within ±(spread % × 1.25) of mid — i.e. how much money is parked right
  around the touch. A 1.5× version is also computed for the dashboard.
- **Depth imbalance.** The same band, but bid-side value and ask-side value kept
  separate, then expressed as heavier ÷ lighter. One empty side gives infinity.

### A-series — order book structure

These need nothing but Quidax's own order book.

**A1 — Crossed order book (CRITICAL).** Fires when the best bid is greater than
or equal to the best ask. Someone is offering to buy at or above the price
someone else is selling at, which should be instantly arbitraged away; if it
persists, the matching engine or the market-maker's quoting is broken.

**A2 — Spread widening (HIGH).** Only for pairs with a configured target spread.
The check compares the live spread % against that target as a *relative* deviation:
`diff = (spread − target) / target × 100`. It flags when the spread is more than
**double** the target (`diff > +100%`) or less than **a quarter** of it
(`diff < −75%`) — both directions matter, since a collapsed spread usually means
the quoting bot has stopped widening properly. Two gates then have to agree
before it counts:

- The absolute miss must be at least `0.05` percentage points, so a pair with a
  tiny 0.02% target doesn't fire on rounding noise that happens to be "300% off".
- **DWS must also be poor** (> 0.5). This is the false-positive killer: a single
  stray order at the touch can blow the raw spread out while the book behind it
  is perfectly healthy. If the depth-weighted spread says the book is fine, no
  A2 fires. The raw deviation is still recorded and shown on the dashboard — it
  just isn't treated as an anomaly.

**A2 — Shallow order book (HIGH).** Same issue ID, independent check, *not*
gated on DWS: fires when either side has fewer than 10 price levels. When both
sub-checks fire in one cycle they're folded into a single A2 (see *Deduping*
below).

**A2:CRITICAL — One-sided market.** One entire side of the book is empty. The
pair short-circuits here — no mid price exists, so every other check is skipped
and the cycle reports this alone. Neither the A6 churn snapshot nor the A4 depth
reading is written on these cycles: a half-empty book has no meaningful
near-touch levels, and saving either would teach both self-baselines that the
outage is normal.

This was its own id (**A3**) until the 2026-08 review. An empty side and a
too-thin side are one failure at two magnitudes — one book, too little of it —
and splitting them meant an operator watching "shallow book" could miss the
moment shallow became empty. Urgency is carried by severity instead of by id:
A2:CRITICAL is Tier 1 and pages on sight, exactly where A3 sat, while A2's
spread-widening and layer-count cases stay Tier 2.

**A4 — Book depth deviation (MEDIUM, dashboard only).** The whole book — every
level the depth endpoint returns, both sides — is worth 50% less than this
market's own rolling average over the last 20 cycles. Drops only: a depth spike
is the outcome the bot exists to produce, not an alert. Silent until it has 5
prior readings, so a restart doesn't flag every pair at once.

This replaced a flat $5,000 floor applied to an in-band figure. That floor was
never currency-converted, so a naira book was judged against the same bare 5,000
as a dollar one — small markets flagged permanently while a large one could halve
without tripping it. A self-baseline has neither problem: every market is
measured against itself, in its own quote currency.

**A5 — Depth imbalance (MEDIUM/HIGH, dashboard only).** Fires when one side of
the book holds at least 5× the value of the other, or HIGH when the lighter side
is completely empty. Tells you the book is lopsided — usually one-way flow or
half the quoting stack having dropped out. Measures the same whole-book per-side
totals A4 sums, so it now reads inventory skew rather than skew at the touch: a
pair whose far-from-mid ask stack dwarfs its bids registers here even when the
top of book looks even.

**A6 — Layer churn stall (CRITICAL/HIGH).** This is the "is the book actually
alive?" check, and it's the subtlest one. The API can happily return fresh
responses while the *content* never changes.

Each cycle it snapshots the nearest 50% of levels on each side as (price, size)
pairs, and compares them slot-by-slot against last cycle's snapshot. The
**churn score** is the fraction of slots that changed. A level appearing or
disappearing near the touch shifts every slot behind it and counts each shifted
slot as changed — that's genuine activity, so it's deliberately not smoothed away.

The score isn't compared against a fixed threshold, because "normal" churn varies
enormously between a busy pair and a quiet one. Instead each market is compared
against **its own history**: the mean of its last 20 churn scores, always
excluding the current reading from its own baseline. A6 fires when this cycle's
churn drops below **20% of that pair's own typical churn**, and escalates to
CRITICAL below 10%. It stays silent until at least 5 prior readings exist, so a
fresh start doesn't fire on an empty baseline.

There's one case ratio-vs-baseline structurally cannot catch: a book that was
*already* frozen before monitoring began. Every reading is 0.0, so the baseline
converges to 0.0 too, and the stall looks perfectly normal. That's handled
explicitly — a baseline of zero *and* current churn of zero across the whole
window is treated as the strongest possible stall signal (CRITICAL) for
bot-managed pairs, and downgraded to a dashboard-only MEDIUM for monitor-only
pairs where nobody is expected to be quoting.

### B-series — pricing vs. the outside world

MEXC and KuCoin only quote assets against USDT, so **B1/B2 run on USDT-quoted
pairs only**. NGN and GHS pairs have no independent external price to check
against — they're covered by F1 instead.

**B1's stale-reference variant (MEDIUM→CRITICAL).** Runs per source, and splits
"stale" into two genuinely different failures:

- *Unavailable* — the source didn't resolve at all (API error, delisted, wrong
  ticker alias). After 3 consecutive cycles this fires HIGH; after 6, CRITICAL.
- *Unchanged* — the source resolved but returned a price within
  `stale_movement_epsilon_pct` (default 0.0%, i.e. exactly equal) of last cycle's.
  On its own this is **not** evidence of anything: quiet markets are quiet.

After 5 consecutive unchanged reads, the unchanged path applies a **cross-source
liveness test**: is the *peer* exchange's recent price window still moving? If
the peer is moving and this source is frozen, the market is live and this feed is
genuinely stuck. If the peer is flat too, it's just a calm market — emitted as
MEDIUM, which routes to dashboard-only. There's a second gate on top: even a
frozen-while-peer-moves source is suppressed as long as its stale price still
*agrees* with the peer within the B2 threshold. A frozen price that's still
correct isn't a problem yet; it only escalates once it has drifted far enough to
matter.

Both paths require the source to have resolved successfully at least once ever,
so a permanent config gap (a ticker that never existed) can't masquerade as a
feed that died. A source confirmed stale is excluded from the trusted-price math
for that cycle.

**B2 — Source divergence (HIGH).** With both sources usable, compare them:
`|mexc − kucoin| / average × 100`. Above the threshold (0.3% default, overridable
per symbol — thin alts legitimately need a looser one) it fires, and then does
**outlier attribution**: whichever source has drifted further from its own recent
8-reading mean is named as the outlier, suspended from pricing for that cycle,
and the *other* source becomes the trusted price. Below threshold, the trusted
price is simply the average of the two.

**B1 — Price discrepancy (HIGH/CRITICAL).** Quidax's mid vs. that trusted
reference. The subtlety here is that the market-maker doesn't quote symmetrically
around the reference — it applies the pair's target spread as a markup, so the
mid *normally* sits about `target_spread / 2` away from reference even when
everything is working exactly as designed. A flat global threshold couldn't tell
that apart from real drift and fired constantly on pairs whose expected offset
already exceeded it.

So `price_discrepancy_pct` (0.5%) is the **extra tolerance beyond that pair's own
expected offset**, not the whole budget. The effective firing point is
`target_spread / 2 + 0.5%`, different for every pair. A 2%-target pair fires past
±1.5%; a 0.2%-target pair fires past ±0.6%. Double the extra tolerance and it's
CRITICAL.

**B4 — Circuit breaker proximity (HIGH/CRITICAL).** Reference-free — it uses
Quidax's own k-line. Take the *open of the oldest candle* in the lookback
window — `kline.lookback_minutes`, 240 by default — and measure how far the
current mid has moved from it. (Quidax returns candles newest-first, so the
oldest is the last element.) At ±10% it's CRITICAL —
the pair is at the level where an exchange would typically halt trading. At 80%
of that (±8%) it fires HIGH as an early warning.

### D1 — Volume spike (HIGH)

Runs on its **own** k-line fetch — 60-minute candles over a 240-minute window —
deliberately separate from the `kline` feed B4 and G2 share, so tuning one never
silently changes the other. (Illiquid pairs produce lots of near-empty 1-minute
candles, which is why D1 wants the coarser period.)

Quote volume for the window is `Σ (candle volume × candle close)`. Worth knowing:
Quidax's candle field order is `[ts, open, close, high, low, volume]` — close
comes *third*, not fifth as in most APIs. Unpacking it the conventional way
multiplies volume by the candle's low instead of its close.

The trigger is **relative to the pair's own history**, not an absolute number.
The baseline is built with no extra API calls, from the window volume already
computed each cycle — but a new baseline "bucket" is only recorded once per
240 minutes of elapsed time, not every cycle. That matters: with a 60s cycle and
a 4-hour window, consecutive readings overlap almost entirely, and averaging
near-duplicates against each other would say nothing. Sampling once per window
length gives genuinely distinct historical readings. The baseline is the mean of
the last 6 such buckets (≈24 hours), always excluding the current reading.

D1 fires when **both** hold:

- window volume ≥ 3× that pair's own baseline, **and**
- window volume ≥ the pair's absolute floor.

The floor exists to stop a pair doing 3× its normally-tiny volume from firing on
a ratio with no economic meaning. The floors are:

| Pair | Floor |
|---|---|
| `usdtngn` | ₦50,000,000 |
| BTC / ETH / SOL / USDC vs NGN | ₦50,000,000 |
| Other NGN pairs | ₦5,000,000 |
| GHS pairs | ₵60,000 |
| BTC / ETH / SOL / USDC vs USDT | $100,000 |
| Other USDT pairs | $5,000 |

Until the baseline has 4 buckets it isn't trusted, and D1 falls back to firing on
the absolute floor alone (`warmup_fallback: "absolute"`) — so a restart that
wipes `health_state.json` doesn't create a blind spot. Setting it to `"suppress"`
trades that for silence during warm-up instead. Setting `mode: "absolute"`
bypasses the baseline permanently.

### E-series — infrastructure

**E1 — Quidax API outage (CRITICAL).** Not per-pair. If **50% or more** of the
configured pairs fail to fetch in a single cycle, that's an outage rather than
isolated errors, and one global alert fires. Individual pair failures are logged
but don't alert — each fetch already retries twice with backoff.

**E2 — Reference feed disconnect (CRITICAL).** MEXC's or KuCoin's batched call
threw. Fires per source, and the message notes that B1/B2 for the affected
source are suspended until it recovers.

Both are global (keyed on `_global`, not a symbol) and both are Tier 1.

### F1 — Cross-pair arbitrage gap (MEDIUM/HIGH, dashboard only)

The NGN pairs have no external reference, so F1 builds one out of Quidax's own
prices. It automatically discovers triangles from whatever pairs are configured —
no hardcoded list:

- **Base bridge**: any `XNGN` pair where `XUSDT` also exists. Implied price =
  `XUSDT × USDTNGN`. So BTCNGN is checked against BTCUSDT × USDTNGN.
- **Quote bridge**: the one CNGN special case — `CNGNNGN` implied by
  `USDTNGN ÷ USDTCNGN`.

The gap is `(actual − implied) / implied × 100`, and it fires past ±0.5%, or HIGH
past ±1%. It also does **root-cause attribution**: if one of the two legs already
fired B1 this cycle, that leg is named as the likely source of the gap; otherwise
the directly-quoted pair is flagged, since it's the one with no independent
reference to vindicate it.

F1 is dashboard-only and never sends Telegram. There's an opt-in research log
(`F1_GAP_LOG=<path>`) that records *every* triangle every cycle, fired or not —
threshold calibration can't be done from fires alone, since a fires-only log is
censored at exactly the boundary you're trying to tune.

### G-series — execution quality

*If the poll rate looks wrong.* Two things used to make the 5s poller run slow,
both fixed, and one thing that is simply configuration:

- **It slept the interval AFTER the work**, so the real period was 5s plus the
  fetch, the JSON write, and any time the event loop spent blocked in the main
  cycle's synchronous pandas. Measured against a simulated 55-pair cycle that
  was ~6.4s per poll — about 560 samples an hour instead of 720. The loop now
  ticks on a fixed monotonic schedule, and after a long block it skips to the
  next whole tick rather than firing catch-up polls back to back.
- **The HTTP connection pool was a third of the size the cycle needs.**
  `process_pair` gathers three requests per pair inside the
  `MAX_CONCURRENT_PAIRS` semaphore, so a cycle puts `MAX_CONCURRENT_PAIRS * 3`
  requests in flight; the pool was sized `MAX_CONCURRENT_PAIRS + 5`, from before
  that gather existed. At the defaults that was 30 requests through 15 slots,
  which throttled the cycle itself and put the depth-walk poll behind a queue
  every time one ran. It is now `MAX_CONCURRENT_PAIRS * FETCHES_PER_PAIR +
  BACKGROUND_CONNECTIONS`, the last being headroom reserved so the background
  loops never queue behind a cycle at all. The monitor prints the pool size at
  startup.
- **`depth_walk.poll_interval_seconds` is editable from the config drawer**, so
  check it before assuming a bug — the slippage tab reports the value the
  monitor is actually using in its stale-feed banner.

*If the tab looks dead.* The 5-second poller writes `data/usdtngn_slippage_raw.json`;
everything on the slippage tab derives from it. The two card groups read it
differently — buy/sell slippage is filtered to the selected window, while the
uptime and spread-gap cards read the raw samples unfiltered — so a stopped
poller used to show as blank slippage cards beside healthy-looking uptime
percentages computed from hours-old data. A banner now calls that out and blanks
the live cards once the newest sample is older than 24 poll intervals (min 2
minutes). The first thing to check is the newest `ts` in that file, not its
mtime: the loop rewrites the file every pass even when the fetch failed, so the
mtime stays fresh while the samples go stale.

**G1 — Depth-walk partial fill (MEDIUM, dashboard only).** USDTNGN only, on its
own 5-second task, because meaningful movement on a thin NGN book happens well
inside a 60-second window.

The core operation is a **depth walk**: consume the book level by level,
best-price-first, until 100,000 USDT of size has been taken, clipping the last
level to only the remainder actually needed. The result is the volume-weighted
average price a market order that size would really clear at.

The reference mid is itself a small depth walk — 1,000 USDT each side, averaged —
rather than raw best bid/ask, so a lone dust order at the touch can't distort it.
If either side can't supply even that, it falls back to top-of-book mid and flags
the sample.

From one poll it records:

- **Slippage**, per side: `(walk price / mid − 1) × 100`.
- **G1 itself**: fires when the entire visible book was consumed without reaching
  100k USDT. The sample is still recorded using whatever depth existed, and
  flagged, rather than dropped — a thin patch shouldn't leave a hole in the chart.
- **Liquidity uptime**, per side: a 1/0 score for whether the 100k walk price
  stayed within a fixed **₦1** of mid (ask side: walk price ≤ mid + ₦1; bid side:
  ≥ mid − ₦1).
- **Spread-gap compliance**: a single 1/0 for whether raw `best_ask − best_bid`
  was within **₦1**, using the same spread function A2 uses.

Samples accumulate into an hourly bucket; once the bucket is an hour old it's
condensed into one point — slippage and mid averaged, uptime and spread-gap
turned into pass-rate fractions, partial-fill flags OR'd — and kept for a year.
The ₦1 constants are fixed in code rather than exposed as config, precisely
because they define a persisted historical series: making them tunable would let
a dashboard edit silently change what years of stored history mean.

**G2 — Candle wick / anomalous print (HIGH/CRITICAL).** Reuses B4's k-line, no
extra fetch — which means `kline.candle_minutes` / `kline.lookback_minutes`
configure **both** checks. The dashboard's config drawer labels that section
"K-line (circuit breaker · B4 & candle wicks · G2)" for exactly this reason; it
said B4 only until 2026-08-20, which sent operators to the D1 volume fields when
they wanted to widen G2's window. D1's candle/lookback are a genuinely separate
pair and do *not* affect G2. It scans **every** candle in the lookback window every cycle, not
just the newest, because a bad print can happen and fully revert between two
60-second depth polls — that blind spot is the whole reason this check exists.

Per candle:

- `low <= 0` → CRITICAL, always, regardless of size. Low is the minimum of the
  whole period, so this also catches a zero open or close without a separate test.
- Otherwise `(high − low) / open × 100 >= 5%` → HIGH.

A bad candle naturally re-appears on every cycle it's still inside the window,
which is what drives Tier-2 confirmation and keeps it visible in the daily log —
no separate dedup state needed. It's also why G2 has a per-episode delivery cap
(below).

### What actually reaches Telegram

The dashboard shows every issue found. Telegram is gated much more tightly.

**Tiers.** Each issue is classified by ID and severity:

| Tier | Behaviour | Issues |
|---|---|---|
| 1 | Fires immediately on first detection | A2-CRITICAL, A6, B1, B4-CRITICAL, G2-CRITICAL, E1, E2 |
| 2 | Must repeat 3 consecutive cycles first | A2-HIGH, B4-HIGH, G2-HIGH |
| 3 | Dashboard flag only, never Telegram | A1, A4, A5, B2, D1, F1, A6-MEDIUM, B1-MEDIUM |

The MEDIUM variants of A6 and B1 exist *specifically* to land in Tier 3 — they're
the "visible but probably benign" cases (a monitor-only pair with a frozen book;
a reference source that is quiet rather than dead). They outrank the id's own
tier because `classify_tier` tests the MEDIUM rule before `TIER1_IDS`.

**Why B1 pages and B2 doesn't.** A quoted price that has drifted from the trusted
reference costs money every cycle it stands, so B1 fires on sight. Two reference
sources merely disagreeing does not: `resolve_trusted_price` already drops the
outlier and keeps pricing running, and if the survivor is still wrong, B1 says so
at Tier 1. D1 sits alongside it — a volume spike is context, not an incident, and
it was the noisiest id when it paged.

**A3 and B3 are retired ids.** A3 merged into A2 and B3 into B1 in the 2026-08
review. They still classify at their original tiers (`RETIRED_TIERS` in
`defaults.py`) so that 30 days of existing log rows keep the tier they were
written with, and they stay acknowledgeable so any ack recorded against them
before the merge can still auto-clear.

**Confirmation counters.** A Tier-2 issue increments a per-(pair, issue) counter
each cycle it's present and fires on the third. The counter resets to zero the
moment the issue is absent for a single cycle. It does *not* increment while a
cooldown is active — otherwise an issue that resolved and re-triggered inside the
window would fire the instant the cooldown lapsed.

**Cooldown: 15 minutes** per (pair, issue). Cooldowns survive resolution — a pair
that clears and re-triggers within the window stays silent.

**Delivery-gated cooldowns.** The cooldown and the post-fire counter reset are
committed **only after Telegram confirms delivery** — every chunk to every chat
returning 2xx. A dropped send (bad chat ID, rate limit, transport error) does not
burn the cooldown; the alert simply retries next cycle instead of going dark for
15 minutes.

**Per-episode caps (D1 and G2 only).** Both detect anomalies that *linger in a
rolling window* and therefore re-detect every single cycle for hours. Without a
cap they'd re-fire once per cooldown for the entire life of the window. So each
gets a cap of 2 confirmed deliveries per "episode" — an unbroken run of cycles in
which the issue keeps appearing. Fire on detection, one more after the cooldown,
then dashboard-visible but silent until the window finally clears and the episode
re-arms. A HIGH→CRITICAL escalation on the same lingering candle does *not* break
through the cap.

**Deduping.** Two checks legitimately emit the same ID in one cycle: A2 (spread
widening + shallow book) and B1 (MEXC stale + KuCoin stale, plus the price
discrepancy itself). These are folded
into one issue — highest severity wins, labels merged — before any tier logic
runs. Without this the Tier-2 counter would double-increment and confirm in 2
cycles instead of 3, and the cooldown set by the first copy would swallow the
second.

**Suspensions.** An operator can mute a pair from the dashboard for 30 minutes.
The pair keeps being monitored and stays fully visible; only its Telegram
delivery is gagged. On suspension its Tier-2 counters are reset, so a
still-present issue re-confirms from scratch when the window lifts rather than
blasting the instant it does. F1 alerts on *other* pairs that reference this one
are unaffected.

**Acknowledgements.** The narrower sibling of a suspension: a checkbox on each
check row in the dashboard's detail panel mutes **one issue on one market**,
rather than the whole pair. It has no clock. It lasts until that issue next
reaches a good state, at which point it retires itself and the box comes back
empty, ready to be ticked again the next time the alert appears. Like a
suspension it only gags Telegram — the issue stays fully visible on the
dashboard, the row is marked `muted`, and the daily log records `B1:acked`.
Tier-2 counters are reset on each acked cycle, so a still-present issue
re-confirms from scratch once the ack lapses instead of firing the instant it
does.

The two mute mechanisms are independent and compose: either alone is enough to
silence delivery, and an acked issue on a suspended pair stays acked after the
suspension expires.

The checkbox is only enabled while the check is actually in a bad state.
Acknowledging a passing check would be meaningless — the engine would observe it
clear on the very next cycle and retire the ack immediately.

*How the expiry works.* `api.py` owns `data/alert_acks.json` and writes an
`acked_at` timestamp; the monitor only reads it. But the monitor is the only
process that sees a cycle, so it's the only one that can notice an issue
clearing — it records that as `resolved_at` inside `health_state.json`, a file it
already owns outright. Both processes then decide liveness by the same
comparison, `acked_at > resolved_at`. Expiry therefore needs no deletion and no
second writer, so neither process ever writes the other's file and there is no
cross-process write race in either direction — the same discipline as
`monitor_config.json` and `suspensions.json`.

The ackable id set lives in `defaults.py` as `ACKABLE_ISSUE_IDS`, imported by
both processes; `debug.py` asserts at import that it covers every id in its tier
tables, so adding a check without making it acknowledgeable fails loudly rather
than shipping an alert that can be acked but never auto-cleared. E1/E2 are
excluded: both are global (keyed `_global`), so there is no market row to tick.

**Monitor-only pairs** (configured with a `None` target, e.g. `usdtcngn`,
`cngnngn`) are watched for structure, depth and price, but never fire A2 spread
widening or D1 — there's no target spread to deviate from and no volume floor
defined.

**Everything is logged regardless.** Every check on every pair, fired or not,
lands in `data/daily_log_YYYY-MM-DD.csv`, with a per-issue state string
(`B1:2/3cyc|A4:flag-only|D1:cooldown|G2:capped`) recording exactly why anything
suppressed was suppressed.

### Reading a market on the dashboard

Clicking a market opens the detail panel, which lists every check as its own row.
Each row carries three things, and the distinction matters:

- **The evidence** — what this market actually did this cycle, shown whether or
  not the check fired. A6 reads "41% of near-touch slots changed this cycle vs
  38% typical for this pair", not "pass". The engine records these numbers on
  every cycle regardless of outcome, so a passing row costs nothing extra to
  populate and is far more useful than a bare verdict when you're trying to work
  out whether something is drifting.
- **The rule** — the threshold the evidence was judged against. **Collapsed by
  default**: click a row (or focus it and hit Enter/Space) to disclose it, and the
  chevron turns down. Sixteen rows carrying three lines of prose each buried the
  evidence line, which is the part worth scanning. Which rows are open is
  remembered per check id, so it survives the re-render an acknowledgement
  triggers and carries across when you click to another market. Where a threshold
  is per-pair rather than global (B1's firing point, A6's self-baseline, D1's
  floor) the row shows *that pair's* value, since no single global constant would
  let you re-derive it.
- **The checkbox** — the acknowledgement described above.

The numbers come from a `metrics` dict each check writes into as it runs (see
`process_pair`), flattened onto the pair's row and served through `/api/status`.
Rows written on a one-sided-book short-circuit, and rows predating this feature, simply
don't carry those columns; every one is optional and renders as an em dash.

Two rows are context rather than checks and get no checkbox: **DWS**, which is
the gate deciding whether a wide raw spread counts as A2, and **Depth
Liquidity**, which feeds A4 and A5.

**Ordered by delivery tier, not by id.** Reading the panel top-down should answer
"what would have woken me?" before "what does the book look like?", and id order
buried a Tier-1 churn stall between two dashboard-only rows. Rows are grouped
under Tier 1 / Tier 2 / Tier 3 headings with the two context rows last, and
within a group the catalogue order survives, so A2's three rows still read
spread → shallow → empty.

The grouping is per *row*, not per id, which matters for the severity-split
checks: A2's spread and shallow-book rows sit in Tier 2 while its one-sided row
sits in Tier 1, even though all three carry the same `A2`. Rows that represent a
specific severity declare it as `tierSeverity`; the rest take the most urgent
tier their id can reach. Tiers come from the same derived `ISSUE_TIERS` map the
dropdowns use, so nothing here is hardcoded either.

### The alert log

Session-scoped: entries accumulate while the tab is open and are not reloaded
from the server, so a refresh starts it empty. Each entry is tagged with the
alert ids that actually **reached Telegram** on that cycle — read from
`telegram_detail`'s `:fired` verdicts, not from the raw issue list, so anything
suppressed by tier, cooldown, ack or episode cap stays out of a log of alerts
that were *sent*. Those ids show as coloured badges on the row and drive the
type filter.

The filter's dropdown is rebuilt from whatever ids the log currently holds, so it
only ever offers types that would match something. A selection is kept even once
its last matching entry ages off the 30-entry tail — a deliberately narrow filter
shouldn't silently widen back to "All" on its own.

### Is it actually running?

The monitor and the API are **separate processes**. A page that renders proves
only that the API is up — it serves whatever `latest.csv` last contained, however
old. The status light used to be driven by "did `/api/status` return 200", so a
monitor dead for hours still showed green.

It now reports the monitor: the sidebar reads **Monitor stale** and a banner
names the age whenever the newest cycle in `latest.csv` is older than three
cycles. `GET /api/diagnostics` is the one-shot version, and the fastest way to
tell the two failure modes apart:

| Symptom | Means |
|---|---|
| `monitor.running: false` | The monitor process is down. Check its startup log — an `ImportError` there means it never got past import. |
| `monitor.running: true`, `depth_walk.newest_sample_age_seconds` large | Monitor alive, the 5s depth-walk task is not. |
| Both fresh, slippage still blank | A window/filter problem, not a feed problem. |

Note `mtime_age_seconds` vs the content ages: `depth_walk_loop` rewrites its raw
file every pass even when the fetch failed, so a fresh mtime with stale contents
means the loop is running but getting nothing — a different fault from the loop
being dead. Never diagnose this one from `ls -l`.

### Alert analysis

A range view over the daily logs, on its own tab. Pick a preset (Today / 7d /
14d / 30d) or type two dates; one request to `/api/alert-analysis` returns every
figure on the page.

Two words carry the whole tab, and mixing them up makes the charts lie:

- A **detection** is one issue on one market in one cycle — literally one id in
  one row's `Issues` column.
- An **episode** is an unbroken run of the same detection, stitched across up to
  **2 missing cycles** so a single dropped fetch doesn't split one incident in
  half. A four-hour B1 is 240 detections and **one** episode.

The distinction matters wherever a count could mean either: the market ranking
switches between episode count and wall-clock time for exactly that reason, and
the stat strip reports both totals rather than picking one.

**Three things the daily log cannot tell you, which the tab says on its face:**

- **Detections, not deliveries.** `update_daily_log` doesn't persist
  `telegram_detail`, so nothing here distinguishes an alert that reached Telegram
  from one the tier, cooldown, ack or episode-cap gate suppressed. Every number
  is "the monitor saw this".
- **E1/E2 are absent.** Both are keyed `_global` and the log only holds per-pair
  rows, so API-outage and reference-feed analysis isn't derivable from this file.
- **No healthy denominator.** Only Warning rows are ever appended, so there's no
  per-pair "healthy vs not" ratio to compute. The only defensible denominator is
  elapsed span ÷ cycle period, which is what `expected_cycles` reports.

**What's on it**

| Panel | Answers |
|---|---|
| Stat strip | Episodes, detections, markets, critical count, median/mean/p95 duration, longest, escalations, still-open |
| Worst markets | Top 15 by episode count, or by **wall-clock time unhealthy** — a different ranking, see below |
| Daily trend | Episodes per day by severity, attributed to the day the episode started |
| Hour of day | Detections per hour per id, shaded **within each row** so a quiet alert's own peak stays visible next to a noisy one |
| Longest episodes | Top 50, with `esc` where severity rose mid-run and `open` where it never cleared |
| Noisiest pairs | episodes ÷ median duration — fires constantly, clears instantly. The threshold-tuning list, not an incident list |
| Still open at range end | Episodes whose last detection is the final cycle in the log |
| Daily log | Every detection in the range, filtered by tier → market → alert type |

**Two subtleties worth knowing.**

*Time in alert isn't the sum of episode durations.* A pair carrying A2, A5 and B1
simultaneously for an hour has three hours of episode time but was unhealthy for
one. The market ranking uses the **union** of its episode intervals; the naive
sum is reported alongside as `alert_minutes_sum`. Ranking by the sum would put
whichever pair trips the most checks at once on top regardless of how long it
was actually broken.

*The cycle period is measured, not read from config.*
`timing.cycle_sleep_seconds` is the sleep at the *end* of a cycle, not the
period — a real cycle is that sleep plus however long the pass took, so a
configured 60 lands as 65-80s on the wire. Episode stitching uses the median gap
between distinct log timestamps instead, which measures the real thing and can't
be thrown off by the large gaps quiet periods leave behind. The config value is
only a fallback for ranges too small to take a median from.

The response also carries `by_issue` (episodes, detections, markets and duration
percentiles per alert id). Nothing charts it directly any more — it survives
because the hour-of-day heatmap orders its rows by it.

**`GET /api/alert-analysis`** — `?start=&end=` (YYYY-MM-DD, NGT, inclusive;
defaults to the trailing 7 days) and `?gap_cycles=` (0-10, default 2). Max span
and max age are both 30 days, the same wall `parse_daily_log` enforces, applied
to both edges. Aggregation is server-side because a busy 30-day range is ~10^6
rows across 30 files: a couple of seconds of pandas and ~40 KB of JSON here,
versus 30 sequential fetches and tens of megabytes in the browser. There's no
polling — it's a historical view over closed logs, so it loads on first open and
then only when you change the range.

### The daily log

Lives on the Alert analysis tab and shares that tab's date range — it moved off
the Overview tab and lost its own single-day picker in the process. The old
per-day endpoint (`/api/history`) still exists and is unchanged; nothing in the
dashboard calls it any more.

**One row per detection, not per cycle.** The CSV stores one row per (cycle,
market) with a packed `B1:HIGH|A2:HIGH|A4:MEDIUM` column. The table explodes
that so each row carries exactly one id, one severity and one tier. This is not
cosmetic: that example row is simultaneously Tier 1, Tier 2 and Tier 3, so as a
single row a tier filter can neither show nor hide it correctly. Depth is
per-cycle and so repeats across the rows of one cycle.

**Three filters, applied in order, defaulting to Tier 1.**

1. **Tier** — 1, 2, 3 or all. Defaults to **Tier 1**, the set that actually pages
   someone. Tier is a function of id *and* severity, not id alone: B4 and G2 are
   Tier 1 at CRITICAL and Tier 2 otherwise, A2 is Tier 1 at CRITICAL (the
   one-sided book) and Tier 2 otherwise, and A6/B1 are Tier 1 with MEDIUM
   variants that are Tier 3. So the same id legitimately appears under two
   tiers.

   The alert-type dropdown is segmented into `<optgroup>`s by tier to make this
   readable. An id is filed under the most urgent tier it can reach and annotates
   the rest ("A2 · also T2") rather than appearing in two groups — the dropdown
   filters by id alone, so duplicate entries would filter identically and read as
   a bug. The id→tier map is **derived** from `classify_tier` in `api.py`
   (`ISSUE_TIERS`) and served on `/api/status` and the alert-log facets; the
   browser never hardcodes tiers, so retiering a check updates the grouping with
   no dashboard change.
2. **Market**
3. **Alert type**

Each dropdown is rebuilt from what the filters *above* it actually contain, so
no combination can land you on an empty table — pick Tier 1 and the markets on
offer are only those with Tier-1 rows; pick a market and the alert ids on offer
are only those it raised at that tier. Choosing a tier that makes a downstream
selection impossible clears it rather than leaving the table filtered by
something the dropdowns no longer say.

`classify_tier` and the tier tables live in **`defaults.py`**, imported by both
`debug.py` (to gate delivery) and `api.py` (to label and filter log rows). A
second copy in the API would drift the moment a check is retuned, and it would
drift silently — the dashboard would just quietly mislabel rows.

Filtering and paging are server-side; a 30-day range is ~10^6 detections and the
browser only ever holds one page. **Export CSV** downloads the whole filtered
set (capped at 250,000 rows), not just the page on screen.

### Out of scope

C1/C2/C3 (market-maker bot health) and E3 (bot feed heartbeat) are not
implemented and can't be — none of them are derivable from public depth, k-line
or reference-ticker data. They need telemetry from the bot's own process, which
this monitor has no access to.
