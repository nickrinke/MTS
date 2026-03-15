# MTS — Monitor The Situation

AI-powered geopolitical and crypto market scanner. Uses Claude to score events and fire Discord alerts when something is genuinely actionable. Runs as a Docker container on a 4-hour scan loop. Mostly quiet. Loud when it matters.

## What it does

- Scans macro headlines from RSS feeds (Reuters, AP, Google News) with GDELT as fallback
- Pulls market data from yfinance (10 macro assets)
- Feeds headlines, prices, economic indicators, and previous scan context to Claude for analysis
- Fires Discord alerts only when Claude scores a signal above an adaptive threshold
- Tracks signal outcomes (hit target, hit stop, expired) and posts weekly performance reports
- Deduplicates alerts so the same event doesn't spam Discord across scans
- Includes a self-hosted dashboard for monitoring signals, scan history, headlines, and performance

## Features

| Feature | Description |
|---------|-------------|
| **RSS Feeds** | Google News RSS (Business, World, Tech, Reuters, AP) as primary headline source. GDELT as fallback. |
| **Cross-Scan Memory** | SQLite stores last N analyses. Claude sees previous scan summaries to track developing situations. |
| **Outcome Tracking** | Every signal is logged with entry/target/stop. Each scan checks open signals against current prices. P&L capped at ±30% to flag bad parses. |
| **Weekly Report** | Sunday Discord post — signals fired, win/loss/expired, running hit rate, skeptical narrative recap by Claude. |
| **Economic Calendar** | FOMC, CPI, NFP dates injected into Claude's context for timing and risk assessment. |
| **Alert Dedup** | 48-hour similarity check (0.50 threshold) + instrument cooldown. Same ticker can't alert twice in 48h. |
| **Earnings Calendar** | Tracks upcoming earnings for macro-moving names (NVDA, AAPL, JPM, etc.) via yfinance. |
| **Adaptive Threshold** | Signal threshold adjusts based on VIX. Crisis (VIX ≥ 30) → 6. Calm (VIX ≤ 14) → 10. |
| **FRED API** | CPI, unemployment, Fed funds rate, Treasury yields, GDP, initial claims. |
| **Dashboard** | Self-hosted web UI — signals, outcomes, scan history, live headlines, alert feed, countdown timer. |
| **Crypto (toggleable)** | CoinGecko prices, Fear & Greed Index, crypto headlines. Enabled by default, disable via env var. |

## Requirements

- Docker and Docker Compose
- Anthropic API key
- Discord webhook URL
- FRED API key (optional, free from [fred.stlouisfed.org](https://fred.stlouisfed.org/docs/api/api_key.html))

## Setup

### 1. Clone and configure

```bash
git clone https://github.com/nickrinke/mts.git
cd mts
cp .env.example .env
```

Edit `.env` and fill in your keys:

```env
ANTHROPIC_API_KEY=sk-ant-...
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
FRED_API_KEY=              # Optional
```

### 2. Build and run

```bash
docker compose up -d --build
```

### 3. Watch the first scan

```bash
docker logs -f mts
```

### 4. Open the dashboard

```
http://your-server-ip:8070
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | — | Required. Claude API key. |
| `DISCORD_WEBHOOK_URL` | — | Required. Discord webhook for alerts. |
| `FRED_API_KEY` | — | Optional. Enables economic indicator data. |
| `SCAN_HOURS_BACK` | `4` | How far back to look for headlines. |
| `SIGNAL_THRESHOLD` | `8` | Base threshold (adaptive adjusts from here). |
| `SCAN_INTERVAL` | `14400` | Seconds between scans (4 hours). |
| `MTS_DB_PATH` | `/data/mts.db` | SQLite database location. |
| `WEEKLY_REPORT_DAY` | `6` | Day for weekly report (0=Mon, 6=Sun). |
| `DEDUP_SIMILARITY` | `0.50` | Similarity threshold for alert dedup (0–1). |
| `CRYPTO_ENABLED` | `true` | Enable/disable crypto scanning. |

## Usage

MTS runs automatically on a loop. No interaction needed after setup.

To stop:

```bash
docker compose down
```

To restart:

```bash
docker compose up -d
```

To check open signals and past performance:

```bash
docker exec -it mts sqlite3 /data/mts.db

-- Open signals
SELECT * FROM signals WHERE outcome = 'OPEN';

-- Outcome breakdown
SELECT outcome, COUNT(*) FROM signals GROUP BY outcome;

-- Recent signals
SELECT instrument, direction, outcome, outcome_pnl_pct FROM signals ORDER BY timestamp DESC LIMIT 20;
```

## Architecture

```
Scan Loop (every 4h)
│
├── Check open signals → resolve outcomes (HIT_TARGET / HIT_STOP / EXPIRED)
│   ├── Force-expire anything older than 7 days
│   └── P&L sanity check (reject ±30%+ as bad parse)
├── Weekly report (Sundays only)
│
├── Fetch shared data
│   ├── Economic calendar (FOMC / CPI / NFP)
│   ├── Earnings calendar (yfinance, 14-day lookahead)
│   └── FRED indicators (if API key set)
│
├── MACRO
│   ├── Headlines (RSS → GDELT fallback)
│   ├── Market snapshot (yfinance, 10 assets)
│   ├── Adaptive threshold (VIX-based)
│   ├── Cross-scan memory (last 5 analyses)
│   ├── Claude analysis (7-day max hold enforced)
│   ├── Dedup check (48h window + instrument cooldown)
│   ├── Discord alert
│   └── Store to memory
│
├── CRYPTO (if enabled)
│   ├── Headlines (RSS → GDELT fallback)
│   ├── Prices (CoinGecko, 8 coins)
│   ├── Fear & Greed Index
│   ├── Cross-scan memory (last 5 analyses)
│   ├── Claude analysis
│   ├── Dedup check → Discord alert
│   └── Store to memory
│
├── Heartbeat (once daily if no signals fired)
│
Dashboard (port 8070)
├── FastAPI backend → reads SQLite
└── Frontend → stats, signals, headlines, alerts, scan history
```

## Project Structure

```
mts/
├── mts.py                    # Main scanner (v3)
├── Dockerfile                # Scanner container
├── docker-compose.yml        # Both services
├── entrypoint.sh             # Scan loop runner
├── requirements.txt          # Python dependencies
├── .env.example              # Environment variable template
├── README.md
└── dashboard/
    ├── api.py                # FastAPI backend
    ├── Dockerfile            # Dashboard container
    └── static/
        └── index.html        # Frontend
```

## Dashboard

The dashboard runs as a second Docker container on port 8070, reading from the same SQLite database.

- **Stats** — total signals, open count, hit rate, avg confidence
- **Charts** — outcome breakdown ring, per-instrument performance bars
- **Headlines** — live RSS feed + MTS analysis summaries with confidence scores
- **Signals** — filterable table (all/open/winners/losers/expired) with click-to-expand detail
- **Scan History** — every scan with confidence score and event summary
- **Alert Feed** — every Discord alert MTS has sent
- **Countdown** — live timer showing time until next scan

All sections are collapsible with "Show more" toggles. Auto-refreshes every 60 seconds.

## Tech Stack

- [Claude](https://docs.anthropic.com/) (Sonnet) — event scoring and analysis
- [FastAPI](https://fastapi.tiangolo.com/) — dashboard backend
- [yfinance](https://github.com/ranaroussi/yfinance) — market data
- [CoinGecko API](https://www.coingecko.com/en/api) — crypto prices
- [GDELT](https://www.gdeltproject.org/) — fallback headline source
- [FRED API](https://fred.stlouisfed.org/docs/api/) — economic indicators
- [feedparser](https://github.com/kurtmckee/feedparser) — RSS parsing
- [SQLite](https://sqlite.org/) — cross-scan memory and outcome tracking
- [Discord Webhooks](https://discord.com/developers/docs/resources/webhook) — alert delivery
- [Docker](https://www.docker.com/) — containerized deployment

## License

MIT
