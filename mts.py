"""
MTS — Monitor The Situation (v3)
=================================
Watches geopolitical news, macro markets, and crypto 24/7.
Uses Claude to score events. Fires Discord alerts only when
something is genuinely actionable. Mostly quiet. Loud when it matters.

v2 additions:
  - RSS feeds (Reuters, AP via Google News — replaces GDELT as primary)
  - Cross-scan memory (SQLite — Claude sees previous analyses)
  - Outcome tracking (logs signals, checks price at target/stop/expiry)
  - Weekly performance report (Sunday Discord summary)
  - Economic calendar (FOMC, CPI, NFP awareness)
  - Alert deduplication (similarity check against recent alerts)
  - Earnings calendar (macro-moving names)
  - Adaptive signal threshold (volatility-based)
  - FRED API (CPI, jobs, GDP, Treasury yields)
"""

import os
import re
import json
import sqlite3
import hashlib
import requests
import feedparser
import yfinance as yf
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from difflib import SequenceMatcher
import anthropic

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
FRED_API_KEY        = os.getenv("FRED_API_KEY", "")
SCAN_HOURS_BACK     = int(os.getenv("SCAN_HOURS_BACK", "4"))
SIGNAL_THRESHOLD    = int(os.getenv("SIGNAL_THRESHOLD", "8"))
DB_PATH             = os.getenv("MTS_DB_PATH", "/data/mts.db")
WEEKLY_REPORT_DAY   = int(os.getenv("WEEKLY_REPORT_DAY", "6"))  # 0=Mon, 6=Sun
DEDUP_SIMILARITY    = float(os.getenv("DEDUP_SIMILARITY", "0.50"))
CRYPTO_ENABLED      = os.getenv("CRYPTO_ENABLED", "true").lower() in ("true", "1", "yes")

WATCHLIST = {
    "Oil":           "USO",
    "Gold":          "GLD",
    "Defense":       "ITA",
    "S&P 500":       "SPY",
    "Bonds":         "TLT",
    "Dollar Index":  "UUP",
    "Volatility":    "^VIX",
    "Bitcoin":       "BTC-USD",
    "Natural Gas":   "UNG",
    "Emerging Mkts": "EEM",
}

CRYPTO_WATCHLIST = [
    "bitcoin", "ethereum", "solana", "binancecoin",
    "ripple", "avalanche-2", "chainlink", "the-open-network",
]

# Macro-moving earnings names to track
EARNINGS_WATCHLIST = [
    "NVDA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA",
    "JPM", "GS", "BAC", "WFC",
    "XOM", "CVX",
    "UNH", "JNJ",
    "CRM", "AVGO", "AMD",
]

# RSS feeds — free, no API key needed
RSS_FEEDS = [
    # Google News topic feeds
    "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx1YlY4U0FtVnVHZ0pWVXcoQAFQAQ?hl=en-US&gl=US&ceid=US:en",  # Business
    "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGRqTVhZU0FtVnVHZ0pWVXcoQAFQAQ?hl=en-US&gl=US&ceid=US:en",  # Technology
    "https://news.google.com/rss/topics/CAAqIggKIhxDQkFTRHdvSkwyMHZNR1ptTUhBU0FtVnVLQUFQAQ?hl=en-US&gl=US&ceid=US:en",  # World
    # Reuters via Google News
    "https://news.google.com/rss/search?q=site:reuters.com+when:1d&hl=en-US&gl=US&ceid=US:en",
    # AP via Google News
    "https://news.google.com/rss/search?q=site:apnews.com+when:1d&hl=en-US&gl=US&ceid=US:en",
    # Crypto-specific
    "https://news.google.com/rss/search?q=bitcoin+OR+ethereum+OR+crypto+when:1d&hl=en-US&gl=US&ceid=US:en",
]

# FRED series IDs for key economic indicators
FRED_SERIES = {
    "CPI":              "CPIAUCSL",
    "Core CPI":         "CPILFESL",
    "Unemployment":     "UNRATE",
    "Nonfarm Payrolls": "PAYEMS",
    "Fed Funds Rate":   "FEDFUNDS",
    "10Y Treasury":     "DGS10",
    "2Y Treasury":      "DGS2",
    "GDP Growth":       "A191RL1Q225SBEA",
    "Initial Claims":   "ICSA",
}

# ── Database ──────────────────────────────────────────────────────────────────

def init_db() -> sqlite3.Connection:
    """Initialize SQLite database for memory, signals, and outcome tracking."""
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS scan_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_type TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            event_summary TEXT,
            thesis TEXT,
            confidence_score INTEGER,
            signal_detected INTEGER,
            full_analysis TEXT
        );

        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_type TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            direction TEXT,
            instrument TEXT,
            entry_zone TEXT,
            target TEXT,
            stop_loss TEXT,
            max_hold_days INTEGER,
            confidence_score INTEGER,
            event_summary TEXT,
            full_analysis TEXT,
            outcome TEXT DEFAULT 'OPEN',
            outcome_price REAL,
            outcome_date TEXT,
            outcome_pnl_pct REAL
        );

        CREATE TABLE IF NOT EXISTS alert_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_type TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            event_summary TEXT,
            content_hash TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_scan_memory_type_ts ON scan_memory(scan_type, timestamp);
        CREATE INDEX IF NOT EXISTS idx_signals_outcome ON signals(outcome);
        CREATE INDEX IF NOT EXISTS idx_alert_log_hash ON alert_log(content_hash);
    """)
    conn.commit()
    return conn


# ── Cross-Scan Memory ─────────────────────────────────────────────────────────

def store_scan_memory(conn: sqlite3.Connection, scan_type: str, analysis: dict) -> None:
    conn.execute(
        """INSERT INTO scan_memory (scan_type, timestamp, event_summary, thesis,
           confidence_score, signal_detected, full_analysis)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            scan_type,
            datetime.now(timezone.utc).isoformat(),
            analysis.get("event_summary", ""),
            analysis.get("thesis", ""),
            analysis.get("confidence_score", 0),
            1 if analysis.get("signal_detected") else 0,
            json.dumps(analysis),
        ),
    )
    conn.commit()


def get_recent_memory(conn: sqlite3.Connection, scan_type: str, n: int = 5) -> list[dict]:
    rows = conn.execute(
        """SELECT timestamp, event_summary, thesis, confidence_score, signal_detected
           FROM scan_memory WHERE scan_type = ?
           ORDER BY id DESC LIMIT ?""",
        (scan_type, n),
    ).fetchall()
    return [dict(r) for r in rows]


def format_memory_context(memories: list[dict]) -> str:
    if not memories:
        return "No previous scans available — this is the first run."
    lines = []
    for m in reversed(memories):
        sig = "SIGNAL" if m["signal_detected"] else "quiet"
        lines.append(
            f"  [{m['timestamp'][:16]}] ({sig}, {m['confidence_score']}/10) "
            f"{m['event_summary']}"
        )
    return "\n".join(lines)


# ── Signal Logging & Outcome Tracking ────────────────────────────────────────

def log_signal(conn: sqlite3.Connection, scan_type: str, analysis: dict) -> int:
    trade = analysis.get("trade", {})
    instrument = trade.get("instrument") or trade.get("coin", "")
    cur = conn.execute(
        """INSERT INTO signals (scan_type, timestamp, direction, instrument,
           entry_zone, target, stop_loss, max_hold_days, confidence_score,
           event_summary, full_analysis)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            scan_type,
            datetime.now(timezone.utc).isoformat(),
            trade.get("direction", ""),
            instrument,
            trade.get("entry_zone", ""),
            trade.get("target", ""),
            trade.get("stop_loss", ""),
            _parse_int(trade.get("max_hold_days", "")),
            analysis.get("confidence_score", 0),
            analysis.get("event_summary", ""),
            json.dumps(analysis),
        ),
    )
    conn.commit()
    return cur.lastrowid


def _parse_price(s: str, label: str = "") -> float | None:
    """Extract price from a string. Uses midpoint for ranges like '$81.50 - $82.00'."""
    if not s:
        return None
    cleaned = str(s).replace(",", "")
    # Try to find a range first (e.g. "$81.50 - $82.00")
    range_match = re.findall(r"\$?([\d]+\.?\d*)", cleaned)
    if len(range_match) >= 2:
        try:
            low = float(range_match[0])
            high = float(range_match[1])
            # Sanity: if second number looks like a percentage (e.g. "+6%"), skip it
            if high > low * 3:
                result = low
            else:
                result = round((low + high) / 2, 2)
            return result
        except ValueError:
            pass
    if range_match:
        try:
            return float(range_match[0])
        except ValueError:
            pass
    if label:
        print(f"  [PARSE] Could not parse price from '{s}' ({label})")
    return None


def _parse_int(s, label: str = "") -> int | None:
    if isinstance(s, int):
        return s
    if isinstance(s, str):
        match = re.search(r"(\d+)", s)
        if match:
            return int(match.group(1))
    if label and s:
        print(f"  [PARSE] Could not parse int from '{s}' ({label})")
    return None


MAX_STALE_DAYS = 7  # Force-expire any signal older than this
MAX_REASONABLE_PNL = 30.0  # Reject P&L calculations above ±30%


def check_open_signals(conn: sqlite3.Connection) -> None:
    """Check all OPEN signals against current prices to resolve outcomes."""
    open_signals = conn.execute(
        "SELECT * FROM signals WHERE outcome = 'OPEN'"
    ).fetchall()

    if not open_signals:
        return

    print(f"\n[OUTCOMES] Checking {len(open_signals)} open signal(s)...")

    crypto_map = {
        "BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD",
        "BNB": "BNB-USD", "XRP": "XRP-USD", "AVAX": "AVAX-USD",
        "LINK": "LINK-USD", "TON": "TON-USD",
    }

    for sig in open_signals:
        instrument = sig["instrument"]
        if not instrument:
            continue

        signal_date = datetime.fromisoformat(sig["timestamp"])
        now = datetime.now(timezone.utc)
        days_open = (now - signal_date).days

        # Force-expire anything older than MAX_STALE_DAYS regardless
        if days_open > MAX_STALE_DAYS:
            conn.execute(
                """UPDATE signals SET outcome = ?, outcome_date = ? WHERE id = ?""",
                ("EXPIRED", now.isoformat(), sig["id"]),
            )
            conn.commit()
            print(f"  ⏰ {instrument} {sig['direction']} → EXPIRED (stale, {days_open}d open)")
            continue

        ticker = crypto_map.get(instrument.upper(), instrument)

        try:
            current = yf.Ticker(ticker).history(period="1d")
            if current.empty:
                continue
            price = current["Close"].iloc[-1]
        except Exception as e:
            print(f"  [OUTCOMES] Error fetching {ticker}: {e}")
            continue

        target_price = _parse_price(sig["target"], f"{instrument} target")
        stop_price = _parse_price(sig["stop_loss"], f"{instrument} stop")
        direction = sig["direction"]
        entry_price = _parse_price(sig["entry_zone"], f"{instrument} entry")
        max_days = sig["max_hold_days"]

        outcome = None
        outcome_pnl = None

        if max_days and days_open > max_days:
            outcome = "EXPIRED"
        elif direction == "LONG":
            if target_price and price >= target_price:
                outcome = "HIT_TARGET"
            elif stop_price and price <= stop_price:
                outcome = "HIT_STOP"
        elif direction == "SHORT":
            if target_price and price <= target_price:
                outcome = "HIT_TARGET"
            elif stop_price and price >= stop_price:
                outcome = "HIT_STOP"

        if outcome:
            if entry_price:
                if direction == "LONG":
                    outcome_pnl = round(((price - entry_price) / entry_price) * 100, 2)
                else:
                    outcome_pnl = round(((entry_price - price) / entry_price) * 100, 2)

                # Sanity check: reject obviously wrong P&L
                if outcome_pnl is not None and abs(outcome_pnl) > MAX_REASONABLE_PNL:
                    print(f"  ⚠️ {instrument} {direction} → {outcome} but P&L {outcome_pnl}% looks wrong (entry={sig['entry_zone']}, target={sig['target']}, price=${price:.2f}) — recording as UNKNOWN")
                    outcome_pnl = None

            conn.execute(
                """UPDATE signals SET outcome = ?, outcome_price = ?,
                   outcome_date = ?, outcome_pnl_pct = ? WHERE id = ?""",
                (outcome, round(price, 2), now.isoformat(), outcome_pnl, sig["id"]),
            )
            conn.commit()
            emoji = {"HIT_TARGET": "✅", "HIT_STOP": "❌", "EXPIRED": "⏰"}.get(outcome, "❓")
            pnl_str = f" ({'+' if outcome_pnl >= 0 else ''}{outcome_pnl}%)" if outcome_pnl is not None else ""
            print(f"  {emoji} {sig['instrument']} {direction} → {outcome}{pnl_str} @ ${price:.2f}")


# ── Alert Deduplication ──────────────────────────────────────────────────────

def is_duplicate_alert(conn: sqlite3.Connection, scan_type: str, event_summary: str,
                       instrument: str = "") -> bool:
    """
    Check if a similar alert was fired recently.
    Two layers:
      1. Instrument cooldown — same ticker/coin can't alert again within DEDUP window
      2. Summary similarity — catches rephrased versions of the same thesis
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()

    # Layer 1: instrument cooldown — if we alerted on this instrument recently, block it
    if instrument:
        instrument_match = conn.execute(
            """SELECT id FROM signals
               WHERE scan_type = ? AND instrument = ? AND timestamp > ?
               ORDER BY id DESC LIMIT 1""",
            (scan_type, instrument, cutoff),
        ).fetchone()
        if instrument_match:
            return True

    # Layer 2: summary similarity
    recent = conn.execute(
        """SELECT event_summary FROM alert_log
           WHERE scan_type = ? AND timestamp > ?
           ORDER BY id DESC LIMIT 10""",
        (scan_type, cutoff),
    ).fetchall()

    for row in recent:
        similarity = SequenceMatcher(None, event_summary.lower(), row["event_summary"].lower()).ratio()
        if similarity >= DEDUP_SIMILARITY:
            return True
    return False


def log_alert(conn: sqlite3.Connection, scan_type: str, event_summary: str) -> None:
    content_hash = hashlib.sha256(event_summary.encode()).hexdigest()[:16]
    conn.execute(
        "INSERT INTO alert_log (scan_type, timestamp, event_summary, content_hash) VALUES (?, ?, ?, ?)",
        (scan_type, datetime.now(timezone.utc).isoformat(), event_summary, content_hash),
    )
    conn.commit()


# ── RSS Feeds ────────────────────────────────────────────────────────────────

def fetch_rss_headlines(category: str = "all") -> list[dict]:
    """Fetch headlines from RSS feeds. Category: 'macro', 'crypto', or 'all'."""
    headlines = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=SCAN_HOURS_BACK * 2)

    feeds = RSS_FEEDS
    if category == "crypto":
        feeds = [f for f in RSS_FEEDS if "bitcoin" in f.lower() or "crypto" in f.lower()]
    elif category == "macro":
        feeds = [f for f in RSS_FEEDS if "bitcoin" not in f.lower() and "crypto" not in f.lower()]

    for feed_url in feeds:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:15]:
                published = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    published = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)

                if published and published < cutoff:
                    continue

                headlines.append({
                    "title":  entry.get("title", "").strip(),
                    "url":    entry.get("link", ""),
                    "date":   published.strftime("%Y-%m-%d %H:%M") if published else "",
                    "source": feed.feed.get("title", "RSS"),
                })
        except Exception as e:
            print(f"  [RSS] Error parsing feed: {e}")

    # Deduplicate by title similarity
    seen = []
    unique = []
    for h in headlines:
        title_lower = h["title"].lower()
        is_dup = False
        for s in seen:
            if SequenceMatcher(None, title_lower, s).ratio() > 0.80:
                is_dup = True
                break
        if not is_dup:
            seen.append(title_lower)
            unique.append(h)

    return unique


# ── Data Fetchers — GDELT (fallback) ────────────────────────────────────────

def fetch_gdelt_news(query: str) -> list[dict]:
    try:
        url = (
            "https://api.gdeltproject.org/api/v2/doc/doc"
            f"?query={requests.utils.quote(query)}"
            f"&mode=artlist&maxrecords=25&timespan={SCAN_HOURS_BACK}h"
            "&sort=ToneDesc&format=json"
        )
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return [
            {"title": a.get("title", ""), "url": a.get("url", ""), "date": a.get("seendate", ""), "source": "GDELT"}
            for a in data.get("articles", [])
        ]
    except Exception as e:
        print(f"  [GDELT] Error: {e}")
        return []


def fetch_headlines(category: str) -> list[dict]:
    """Fetch headlines from RSS first, fall back to GDELT if RSS returns too few."""
    rss = fetch_rss_headlines(category)
    print(f"  RSS: {len(rss)} articles.")

    if len(rss) >= 5:
        return rss

    # GDELT fallback
    if category == "crypto":
        query = "bitcoin OR ethereum OR crypto OR cryptocurrency OR blockchain"
    else:
        query = "war OR conflict OR sanctions OR attack OR military OR crisis"

    gdelt = fetch_gdelt_news(query)
    print(f"  GDELT fallback: {len(gdelt)} articles.")
    return rss + gdelt


# ── Data Fetchers — Market ───────────────────────────────────────────────────

def fetch_market_snapshot() -> dict:
    snapshot = {}
    for name, ticker in WATCHLIST.items():
        try:
            hist = yf.Ticker(ticker).history(period="6d")
            if len(hist) >= 2:
                latest = hist["Close"].iloc[-1]
                five_d = hist["Close"].iloc[0]
                change = round(((latest - five_d) / five_d) * 100, 2)
                snapshot[name] = {
                    "ticker":        ticker,
                    "price":         round(latest, 2),
                    "change_5d_pct": change,
                }
        except Exception as e:
            print(f"  [Market] Error fetching {ticker}: {e}")
    return snapshot


# ── Data Fetchers — Crypto ───────────────────────────────────────────────────

def fetch_crypto_snapshot() -> dict:
    try:
        ids = ",".join(CRYPTO_WATCHLIST)
        url = (
            f"https://api.coingecko.com/api/v3/coins/markets"
            f"?vs_currency=usd&ids={ids}"
            f"&order=market_cap_desc&sparkline=false"
            f"&price_change_percentage=24h,7d"
        )
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        coins = resp.json()
        snapshot = {}
        for c in coins:
            snapshot[c["name"]] = {
                "symbol":       c["symbol"].upper(),
                "price":        c["current_price"],
                "change_24h":   round(c.get("price_change_percentage_24h") or 0, 2),
                "change_7d":    round(c.get("price_change_percentage_7d_in_currency") or 0, 2),
                "volume_24h_m": round((c.get("total_volume") or 0) / 1_000_000, 1),
                "market_cap_b": round((c.get("market_cap") or 0) / 1_000_000_000, 2),
            }
        return snapshot
    except Exception as e:
        print(f"  [CoinGecko] Error: {e}")
        return {}


def fetch_crypto_fear_greed() -> dict:
    try:
        resp = requests.get("https://api.alternative.me/fng/?limit=2", timeout=10)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if data:
            today     = data[0]
            yesterday = data[1] if len(data) > 1 else data[0]
            return {
                "value":          int(today["value"]),
                "classification": today["value_classification"],
                "yesterday":      int(yesterday["value"]),
            }
    except Exception as e:
        print(f"  [FearGreed] Error: {e}")
    return {}


# ── Economic Calendar ────────────────────────────────────────────────────────

ECONOMIC_CALENDAR = [
    # FOMC 2025
    {"date": "2025-01-29", "event": "FOMC Rate Decision", "type": "FOMC"},
    {"date": "2025-03-19", "event": "FOMC Rate Decision", "type": "FOMC"},
    {"date": "2025-05-07", "event": "FOMC Rate Decision", "type": "FOMC"},
    {"date": "2025-06-18", "event": "FOMC Rate Decision", "type": "FOMC"},
    {"date": "2025-07-30", "event": "FOMC Rate Decision", "type": "FOMC"},
    {"date": "2025-09-17", "event": "FOMC Rate Decision", "type": "FOMC"},
    {"date": "2025-10-29", "event": "FOMC Rate Decision", "type": "FOMC"},
    {"date": "2025-12-17", "event": "FOMC Rate Decision", "type": "FOMC"},
    # FOMC 2026
    {"date": "2026-01-28", "event": "FOMC Rate Decision", "type": "FOMC"},
    {"date": "2026-03-18", "event": "FOMC Rate Decision", "type": "FOMC"},
    {"date": "2026-04-29", "event": "FOMC Rate Decision", "type": "FOMC"},
    {"date": "2026-06-17", "event": "FOMC Rate Decision", "type": "FOMC"},
    {"date": "2026-07-29", "event": "FOMC Rate Decision", "type": "FOMC"},
    {"date": "2026-09-16", "event": "FOMC Rate Decision", "type": "FOMC"},
    {"date": "2026-11-04", "event": "FOMC Rate Decision", "type": "FOMC"},
    {"date": "2026-12-16", "event": "FOMC Rate Decision", "type": "FOMC"},
    # CPI 2025 (approximate release dates)
    {"date": "2025-01-15", "event": "CPI Release (Dec)", "type": "CPI"},
    {"date": "2025-02-12", "event": "CPI Release (Jan)", "type": "CPI"},
    {"date": "2025-03-12", "event": "CPI Release (Feb)", "type": "CPI"},
    {"date": "2025-04-10", "event": "CPI Release (Mar)", "type": "CPI"},
    {"date": "2025-05-13", "event": "CPI Release (Apr)", "type": "CPI"},
    {"date": "2025-06-11", "event": "CPI Release (May)", "type": "CPI"},
    {"date": "2025-07-11", "event": "CPI Release (Jun)", "type": "CPI"},
    {"date": "2025-08-12", "event": "CPI Release (Jul)", "type": "CPI"},
    {"date": "2025-09-10", "event": "CPI Release (Aug)", "type": "CPI"},
    {"date": "2025-10-14", "event": "CPI Release (Sep)", "type": "CPI"},
    {"date": "2025-11-12", "event": "CPI Release (Oct)", "type": "CPI"},
    {"date": "2025-12-10", "event": "CPI Release (Nov)", "type": "CPI"},
    # CPI 2026
    {"date": "2026-01-14", "event": "CPI Release (Dec)", "type": "CPI"},
    {"date": "2026-02-11", "event": "CPI Release (Jan)", "type": "CPI"},
    {"date": "2026-03-11", "event": "CPI Release (Feb)", "type": "CPI"},
    {"date": "2026-04-14", "event": "CPI Release (Mar)", "type": "CPI"},
    {"date": "2026-05-12", "event": "CPI Release (Apr)", "type": "CPI"},
    {"date": "2026-06-10", "event": "CPI Release (May)", "type": "CPI"},
    {"date": "2026-07-14", "event": "CPI Release (Jun)", "type": "CPI"},
    {"date": "2026-08-12", "event": "CPI Release (Jul)", "type": "CPI"},
    {"date": "2026-09-15", "event": "CPI Release (Aug)", "type": "CPI"},
    {"date": "2026-10-13", "event": "CPI Release (Sep)", "type": "CPI"},
    {"date": "2026-11-10", "event": "CPI Release (Oct)", "type": "CPI"},
    {"date": "2026-12-10", "event": "CPI Release (Nov)", "type": "CPI"},
    # NFP 2025 (first Friday of each month)
    {"date": "2025-01-10", "event": "Nonfarm Payrolls (Dec)", "type": "NFP"},
    {"date": "2025-02-07", "event": "Nonfarm Payrolls (Jan)", "type": "NFP"},
    {"date": "2025-03-07", "event": "Nonfarm Payrolls (Feb)", "type": "NFP"},
    {"date": "2025-04-04", "event": "Nonfarm Payrolls (Mar)", "type": "NFP"},
    {"date": "2025-05-02", "event": "Nonfarm Payrolls (Apr)", "type": "NFP"},
    {"date": "2025-06-06", "event": "Nonfarm Payrolls (May)", "type": "NFP"},
    {"date": "2025-07-03", "event": "Nonfarm Payrolls (Jun)", "type": "NFP"},
    {"date": "2025-08-01", "event": "Nonfarm Payrolls (Jul)", "type": "NFP"},
    {"date": "2025-09-05", "event": "Nonfarm Payrolls (Aug)", "type": "NFP"},
    {"date": "2025-10-03", "event": "Nonfarm Payrolls (Sep)", "type": "NFP"},
    {"date": "2025-11-07", "event": "Nonfarm Payrolls (Oct)", "type": "NFP"},
    {"date": "2025-12-05", "event": "Nonfarm Payrolls (Nov)", "type": "NFP"},
    # NFP 2026
    {"date": "2026-01-09", "event": "Nonfarm Payrolls (Dec)", "type": "NFP"},
    {"date": "2026-02-06", "event": "Nonfarm Payrolls (Jan)", "type": "NFP"},
    {"date": "2026-03-06", "event": "Nonfarm Payrolls (Feb)", "type": "NFP"},
    {"date": "2026-04-03", "event": "Nonfarm Payrolls (Mar)", "type": "NFP"},
    {"date": "2026-05-08", "event": "Nonfarm Payrolls (Apr)", "type": "NFP"},
    {"date": "2026-06-05", "event": "Nonfarm Payrolls (May)", "type": "NFP"},
    {"date": "2026-07-02", "event": "Nonfarm Payrolls (Jun)", "type": "NFP"},
    {"date": "2026-08-07", "event": "Nonfarm Payrolls (Jul)", "type": "NFP"},
    {"date": "2026-09-04", "event": "Nonfarm Payrolls (Aug)", "type": "NFP"},
    {"date": "2026-10-02", "event": "Nonfarm Payrolls (Sep)", "type": "NFP"},
    {"date": "2026-11-06", "event": "Nonfarm Payrolls (Oct)", "type": "NFP"},
    {"date": "2026-12-04", "event": "Nonfarm Payrolls (Nov)", "type": "NFP"},
]


def get_upcoming_economic_events(days_ahead: int = 7) -> list[dict]:
    today = datetime.now(timezone.utc).date()
    window = today + timedelta(days=days_ahead)
    upcoming = []
    for event in ECONOMIC_CALENDAR:
        event_date = datetime.strptime(event["date"], "%Y-%m-%d").date()
        if today <= event_date <= window:
            days_until = (event_date - today).days
            upcoming.append({**event, "days_until": days_until})
    return upcoming


def format_economic_calendar(events: list[dict]) -> str:
    if not events:
        return "No major scheduled economic events in the next 7 days."
    lines = []
    for e in sorted(events, key=lambda x: x["days_until"]):
        urgency = "⚠️ TODAY" if e["days_until"] == 0 else (
            "⚡ TOMORROW" if e["days_until"] == 1 else f"in {e['days_until']}d"
        )
        lines.append(f"  {urgency}: {e['event']} ({e['date']})")
    return "\n".join(lines)


# ── Earnings Calendar ────────────────────────────────────────────────────────

def fetch_upcoming_earnings(days_ahead: int = 14) -> list[dict]:
    upcoming = []
    for ticker in EARNINGS_WATCHLIST:
        try:
            stock = yf.Ticker(ticker)
            cal = stock.calendar
            if cal is not None and not (hasattr(cal, "empty") and cal.empty):
                if isinstance(cal, dict):
                    earn_date = cal.get("Earnings Date")
                    if isinstance(earn_date, list) and earn_date:
                        earn_date = earn_date[0]
                    if earn_date:
                        if hasattr(earn_date, "date"):
                            earn_date = earn_date.date()
                        elif isinstance(earn_date, str):
                            earn_date = datetime.strptime(earn_date[:10], "%Y-%m-%d").date()
                        today = datetime.now(timezone.utc).date()
                        days_until = (earn_date - today).days
                        if 0 <= days_until <= days_ahead:
                            upcoming.append({
                                "ticker": ticker,
                                "date": str(earn_date),
                                "days_until": days_until,
                            })
        except Exception:
            pass
    return upcoming


def format_earnings_calendar(earnings: list[dict]) -> str:
    if not earnings:
        return "No major earnings from watchlist in the next 14 days."
    lines = []
    for e in sorted(earnings, key=lambda x: x["days_until"]):
        urgency = "⚠️ TODAY" if e["days_until"] == 0 else (
            "⚡ TOMORROW" if e["days_until"] == 1 else f"in {e['days_until']}d"
        )
        lines.append(f"  {urgency}: {e['ticker']} earnings ({e['date']})")
    return "\n".join(lines)


# ── FRED API ─────────────────────────────────────────────────────────────────

def fetch_fred_data() -> dict:
    if not FRED_API_KEY:
        return {}

    data = {}
    for name, series_id in FRED_SERIES.items():
        try:
            url = (
                f"https://api.stlouisfed.org/fred/series/observations"
                f"?series_id={series_id}&api_key={FRED_API_KEY}"
                f"&file_type=json&sort_order=desc&limit=2"
            )
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            obs = resp.json().get("observations", [])
            if obs:
                latest = obs[0]
                prev = obs[1] if len(obs) > 1 else None
                val = latest.get("value", ".")
                if val != ".":
                    entry = {"value": float(val), "date": latest.get("date", "")}
                    if prev and prev.get("value", ".") != ".":
                        entry["previous"] = float(prev["value"])
                        entry["change"] = round(entry["value"] - entry["previous"], 3)
                    data[name] = entry
        except Exception as e:
            print(f"  [FRED] Error fetching {name}: {e}")

    return data


def format_fred_data(fred: dict) -> str:
    if not fred:
        return "FRED data unavailable (no API key configured)."
    lines = []
    for name, d in fred.items():
        change_str = ""
        if "change" in d:
            sign = "+" if d["change"] >= 0 else ""
            change_str = f" ({sign}{d['change']})"
        lines.append(f"  {name}: {d['value']}{change_str} (as of {d['date']})")
    return "\n".join(lines)


# ── Adaptive Threshold ───────────────────────────────────────────────────────

def compute_adaptive_threshold(market: dict, base_threshold: int = 8) -> int:
    """
    Adjust signal threshold based on VIX.
    High vol → lower threshold (more sensitive during crises).
    Low vol → higher threshold (filter noise during quiet periods).
    Base 8: crisis → 6, elevated → 7, normal → 8, calm → 9, dead → 10
    """
    vix_data = market.get("Volatility", {})
    vix_price = vix_data.get("price", 20)

    if vix_price >= 30:
        return max(base_threshold - 2, 6)
    elif vix_price >= 25:
        return max(base_threshold - 1, 7)
    elif vix_price <= 14:
        return min(base_threshold + 2, 10)
    elif vix_price <= 17:
        return min(base_threshold + 1, 9)
    else:
        return base_threshold


# ── Claude Analysis — Macro ──────────────────────────────────────────────────

MACRO_SYSTEM_PROMPT = """You are a macro event-driven trading analyst inside a system called MTS (Monitor The Situation).
Your job is to evaluate geopolitical and macroeconomic news and determine
whether any events create a high-conviction, asymmetric trade opportunity.

You think like a seasoned hedge fund analyst:
- You consider whether moves are already priced in
- You identify the specific instrument that captures the thesis cleanly
- You are conservative — most news does NOT warrant a trade
- You only flag a signal when the setup is genuinely compelling
- You wait for events that create obvious, directional macro moves
- You design trades for quick in-and-out execution — 1 to 7 days max, never longer
- You track developing situations across scans (previous scan context is provided)
- You factor in upcoming economic events (FOMC, CPI, NFP) when assessing risk and timing
- You note when a signal is the same as a previous scan (avoid re-alerting on stale events)

Respond ONLY with valid JSON. No markdown, no explanation outside the JSON."""


def analyze_macro(headlines: list[dict], market: dict, memory: list[dict],
                  econ_events: list[dict], earnings: list[dict], fred: dict) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    headlines_text = "\n".join(
        f"- {h['title']} [{h.get('source', '')}] ({h['date'][:10] if h.get('date') else 'unknown'})"
        for h in headlines[:25]
    ) or "No major headlines detected."

    market_text = "\n".join(
        f"- {name}: ${data['price']} ({'+' if data['change_5d_pct'] >= 0 else ''}{data['change_5d_pct']}% 5d)"
        for name, data in market.items()
    )

    memory_text = format_memory_context(memory)
    econ_text = format_economic_calendar(econ_events)
    earnings_text = format_earnings_calendar(earnings)
    fred_text = format_fred_data(fred)

    user_prompt = f"""
Current UTC time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}

PREVIOUS SCAN CONTEXT (use this to track developing situations):
{memory_text}

RECENT HEADLINES (last {SCAN_HOURS_BACK} hours):
{headlines_text}

MARKET SNAPSHOT (5-day change):
{market_text}

UPCOMING ECONOMIC EVENTS (next 7 days):
{econ_text}

UPCOMING EARNINGS (next 14 days):
{earnings_text}

ECONOMIC INDICATORS (FRED):
{fred_text}

Analyze the above and return a JSON object with this exact structure:

{{
  "signal_detected": true or false,
  "confidence_score": 0-10,
  "event_summary": "1-2 sentence description of the key macro event",
  "thesis": "Why this creates a trade opportunity (or why it doesn't)",
  "already_priced_in": true or false,
  "is_developing": true or false,
  "is_repeat_of_previous": true or false,
  "trade": {{
    "direction": "LONG or SHORT",
    "instrument": "Ticker symbol (e.g. USO, GLD, ITA)",
    "instrument_name": "Human readable name",
    "entry_zone": "Price range to enter, e.g. $81.50 - $82.00",
    "target": "Price target to take profit, e.g. $87.00 (+6%)",
    "stop_loss": "Price to exit if wrong, e.g. $79.50 (-3%)",
    "max_hold_days": "Exit after this many days regardless. MAXIMUM 7 days — this system is for quick trades only.",
    "timeframe": "e.g. 1-3 days, 3-5 days, 5-7 days (never longer than 7 days)",
    "entry_note": "Any timing or entry guidance",
    "exit_note": "Conditions that should trigger early exit before target or stop",
    "invalidation": "What would make this thesis wrong"
  }},
  "calendar_risk": "Note any upcoming events that could impact this trade's timing",
  "historical_precedent": "Brief note on how similar events moved markets historically",
  "risk_level": "LOW / MEDIUM / HIGH"
}}

If no compelling signal exists, set signal_detected to false and confidence_score below 5.
The trade object should still be filled with your best assessment even if no signal.
Be specific with entry_zone, target, and stop_loss — use actual price levels based on current market data.
If this event is substantially the same as a previous scan, set is_repeat_of_previous to true.
max_hold_days must NEVER exceed 7. This system is designed for quick in-and-out trades only.
"""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1500,
        system=MACRO_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError as e:
        print(f"  [MACRO] JSON parse error: {e}")
        print(f"  [MACRO] Raw response: {raw[:500]}")
        return {"signal_detected": False, "confidence_score": 0,
                "event_summary": "Analysis failed — Claude returned invalid JSON",
                "thesis": "", "trade": {}, "risk_level": "LOW"}

CRYPTO_SYSTEM_PROMPT = """You are a crypto market analyst inside a system called MTS (Monitor The Situation).
Your job is to evaluate crypto-specific signals — price action, sentiment, news, fear/greed —
and determine whether any setup creates a high-conviction, asymmetric trade opportunity.

You think like an experienced crypto trader:
- You look for setups driven by real catalysts, not noise
- You consider fear/greed extremes and volume anomalies as signals
- You are conservative — most market conditions do NOT warrant a trade
- You only flag a signal when there is a clear, directional setup with asymmetric risk/reward
- Extreme fear can be a buy signal. Extreme greed can be a sell signal.
- You track developing situations across scans (previous scan context is provided)
- You note when a signal is the same as a previous scan (avoid re-alerting on stale events)

Respond ONLY with valid JSON. No markdown, no explanation outside the JSON."""


def analyze_crypto(headlines: list[dict], crypto: dict, fear_greed: dict,
                   memory: list[dict]) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    headlines_text = "\n".join(
        f"- {h['title']} [{h.get('source', '')}] ({h['date'][:10] if h.get('date') else 'unknown'})"
        for h in headlines[:25]
    ) or "No major crypto headlines detected."

    crypto_text = "\n".join(
        f"- {name} ({data['symbol']}): ${data['price']:,} | 24h: {'+' if data['change_24h'] >= 0 else ''}{data['change_24h']}% | 7d: {'+' if data['change_7d'] >= 0 else ''}{data['change_7d']}% | Vol: ${data['volume_24h_m']}M"
        for name, data in crypto.items()
    ) or "No crypto data available."

    fg_text = (
        f"Fear & Greed Index: {fear_greed.get('value', 'N/A')} ({fear_greed.get('classification', 'N/A')}) "
        f"vs yesterday: {fear_greed.get('yesterday', 'N/A')}"
        if fear_greed else "Fear & Greed Index: unavailable"
    )

    memory_text = format_memory_context(memory)

    user_prompt = f"""
Current UTC time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}

PREVIOUS SCAN CONTEXT (use this to track developing situations):
{memory_text}

CRYPTO HEADLINES (last {SCAN_HOURS_BACK} hours):
{headlines_text}

CRYPTO MARKET SNAPSHOT:
{crypto_text}

SENTIMENT:
{fg_text}

Analyze the above and return a JSON object with this exact structure:

{{
  "signal_detected": true or false,
  "confidence_score": 0-10,
  "event_summary": "1-2 sentence description of the key crypto setup or catalyst",
  "thesis": "Why this creates a trade opportunity (or why it doesn't)",
  "already_priced_in": true or false,
  "is_developing": true or false,
  "is_repeat_of_previous": true or false,
  "sentiment_note": "What the fear/greed reading suggests about market positioning",
  "trade": {{
    "direction": "LONG or SHORT",
    "coin": "e.g. BTC, ETH, SOL",
    "coin_name": "e.g. Bitcoin, Ethereum, Solana",
    "entry_zone": "Price range to enter, e.g. $82,000 - $83,500",
    "target": "Price target to take profit, e.g. $91,000 (+10%)",
    "stop_loss": "Price to exit if wrong, e.g. $78,000 (-5%)",
    "max_hold_days": "Exit after this many days regardless. MAXIMUM 7 days — this system is for quick trades only.",
    "entry_note": "Any timing or entry guidance",
    "exit_note": "Conditions that should trigger early exit before target or stop",
    "invalidation": "What would make this thesis wrong"
  }},
  "historical_precedent": "Brief note on how similar setups played out historically",
  "risk_level": "LOW / MEDIUM / HIGH"
}}

If no compelling signal exists, set signal_detected to false and confidence_score below 5.
Be specific with entry_zone, target, and stop_loss — use actual price levels from the data provided.
If this event is substantially the same as a previous scan, set is_repeat_of_previous to true.
max_hold_days must NEVER exceed 7. This system is designed for quick in-and-out trades only.
"""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1500,
        system=CRYPTO_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError as e:
        print(f"  [CRYPTO] JSON parse error: {e}")
        print(f"  [CRYPTO] Raw response: {raw[:500]}")
        return {"signal_detected": False, "confidence_score": 0,
                "event_summary": "Analysis failed — Claude returned invalid JSON",
                "thesis": "", "trade": {}, "risk_level": "LOW"}


# ── Discord Alerts ───────────────────────────────────────────────────────────

def send_macro_alert(analysis: dict) -> None:
    score     = analysis.get("confidence_score", 0)
    trade     = analysis.get("trade", {})
    direction = trade.get("direction", "")
    risk      = analysis.get("risk_level", "MEDIUM")

    direction_emoji = "🟢 LONG" if direction == "LONG" else "🔴 SHORT"
    invalidation    = trade.get("invalidation", "N/A")
    calendar_risk   = analysis.get("calendar_risk", "")

    content = (
        f"🚨 {analysis.get('event_summary', 'Macro signal detected.')}\n"
        f"\n"
        f"TRADE: {direction_emoji} {trade.get('instrument', '')} | Confidence: {score}/10 | Risk: {risk}\n"
        f"Entry:  {trade.get('entry_zone', 'N/A')}\n"
        f"Target: {trade.get('target', 'N/A')}\n"
        f"Stop:   {trade.get('stop_loss', 'N/A')} ⚠️ set manually after fill\n"
        f"Hold:   {trade.get('max_hold_days', 'N/A')} days max\n"
    )
    if calendar_risk:
        content += f"\n📅 Calendar risk: {calendar_risk}\n"
    content += (
        f"\nExit early if: {invalidation}\n"
        f"\n"
        f"-# MTS • {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} • NOT financial advice"
    )

    requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=10).raise_for_status()
    print("  [Discord] Macro alert sent.")


def send_crypto_alert(analysis: dict) -> None:
    score     = analysis.get("confidence_score", 0)
    trade     = analysis.get("trade", {})
    direction = trade.get("direction", "")
    risk      = analysis.get("risk_level", "MEDIUM")

    direction_emoji = "🟢 LONG" if direction == "LONG" else "🔴 SHORT"
    invalidation    = trade.get("invalidation", "N/A")

    content = (
        f"🚨 {analysis.get('event_summary', 'Crypto signal detected.')}\n"
        f"\n"
        f"TRADE: {direction_emoji} {trade.get('coin', '')} | Confidence: {score}/10 | Risk: {risk}\n"
        f"Entry:  {trade.get('entry_zone', 'N/A')}\n"
        f"Target: {trade.get('target', 'N/A')}\n"
        f"Stop:   {trade.get('stop_loss', 'N/A')} ⚠️ set manually after fill\n"
        f"Hold:   {trade.get('max_hold_days', 'N/A')} days max\n"
        f"\n"
        f"Exit early if: {invalidation}\n"
        f"\n"
        f"-# MTS • {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} • NOT financial advice"
    )

    requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=10).raise_for_status()
    print("  [Discord] Crypto alert sent.")


def send_heartbeat(conn: sqlite3.Connection, macro_analysis: dict, crypto_analysis: dict, threshold: int) -> None:
    """Send a quiet heartbeat — max once per day."""
    today_str = datetime.now(timezone.utc).date().isoformat()
    existing = conn.execute(
        "SELECT id FROM alert_log WHERE scan_type = 'heartbeat' AND timestamp LIKE ?",
        (f"{today_str}%",),
    ).fetchone()
    if existing:
        return

    macro_score = macro_analysis.get("confidence_score", 0)
    crypto_score = crypto_analysis.get("confidence_score", 0)

    crypto_line = f" • Crypto: {crypto_score}/10" if CRYPTO_ENABLED else ""
    content = (
        f"💤 MTS scanned — no signals above threshold ({threshold}/10)\n"
        f"Macro: {macro_score}/10{crypto_line}\n"
        f"-# {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=10).raise_for_status()
        log_alert(conn, "heartbeat", f"Heartbeat {today_str}")
    except Exception:
        pass


# ── Weekly Performance Report ────────────────────────────────────────────────

def generate_weekly_report(conn: sqlite3.Connection) -> None:
    today = datetime.now(timezone.utc).date()
    if today.weekday() != WEEKLY_REPORT_DAY:
        return

    today_str = today.isoformat()
    existing = conn.execute(
        "SELECT id FROM alert_log WHERE scan_type = 'weekly_report' AND timestamp LIKE ?",
        (f"{today_str}%",),
    ).fetchone()
    if existing:
        return

    print("\n[WEEKLY REPORT] Generating...")

    week_ago = (today - timedelta(days=7)).isoformat()

    signals = conn.execute(
        "SELECT * FROM signals WHERE timestamp >= ? ORDER BY timestamp",
        (week_ago,),
    ).fetchall()

    all_resolved = conn.execute(
        "SELECT * FROM signals WHERE outcome != 'OPEN'"
    ).fetchall()

    total_signals = len(signals)
    resolved = [s for s in signals if s["outcome"] != "OPEN"]
    hits = [s for s in resolved if s["outcome"] == "HIT_TARGET"]
    stops = [s for s in resolved if s["outcome"] == "HIT_STOP"]
    expired = [s for s in resolved if s["outcome"] == "EXPIRED"]
    still_open = [s for s in signals if s["outcome"] == "OPEN"]

    all_hits = [s for s in all_resolved if s["outcome"] == "HIT_TARGET"]
    all_resolved_count = len(all_resolved)
    running_hit_rate = round(len(all_hits) / all_resolved_count * 100, 1) if all_resolved_count > 0 else 0

    avg_conf_winners = (
        round(sum(s["confidence_score"] for s in all_hits) / len(all_hits), 1) if all_hits else 0
    )
    all_stops = [s for s in all_resolved if s["outcome"] == "HIT_STOP"]
    avg_conf_losers = (
        round(sum(s["confidence_score"] for s in all_stops) / len(all_stops), 1) if all_stops else 0
    )

    pnl_signals = [s for s in resolved if s["outcome_pnl_pct"] is not None]
    best = max(pnl_signals, key=lambda s: s["outcome_pnl_pct"]) if pnl_signals else None
    worst = min(pnl_signals, key=lambda s: s["outcome_pnl_pct"]) if pnl_signals else None

    stats_text = f"""
Week: {week_ago} to {today_str}
Signals fired: {total_signals}
Resolved: {len(resolved)} (Target: {len(hits)}, Stop: {len(stops)}, Expired: {len(expired)})
Still open: {len(still_open)}
Running hit rate (all-time): {running_hit_rate}% ({len(all_hits)}/{all_resolved_count})
Avg confidence — winners: {avg_conf_winners}/10, losers: {avg_conf_losers}/10
"""
    if best:
        stats_text += f"Best call: {best['instrument']} {best['direction']} ({'+' if best['outcome_pnl_pct'] >= 0 else ''}{best['outcome_pnl_pct']}%)\n"
    if worst:
        stats_text += f"Worst call: {worst['instrument']} {worst['direction']} ({'+' if worst['outcome_pnl_pct'] >= 0 else ''}{worst['outcome_pnl_pct']}%)\n"

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            system="""You write brief, brutally honest weekly trading system recaps for Discord. Keep it under 800 characters. No markdown. Use plain text with line breaks.

Rules:
- Be skeptical of the data. If P&L numbers look unrealistic (e.g. +50% on an ETF in a week), call it out as likely a data/parsing error.
- If the sample size is small (fewer than 10 resolved signals), say the hit rate is not yet meaningful.
- If most signals are still open, note that the system may be generating signals faster than they resolve.
- Never hype performance. If you're not sure the numbers are reliable, say so.
- Focus on what needs to improve, not what looks good.""",
            messages=[{"role": "user", "content": f"Write a weekly MTS performance recap based on these stats:\n{stats_text}"}],
        )
        narrative = response.content[0].text.strip()
    except Exception as e:
        print(f"  [WEEKLY] Error generating narrative: {e}")
        narrative = "Weekly narrative unavailable."

    report = (
        f"📊 **MTS Weekly Report** — {week_ago} → {today_str}\n\n"
        f"{narrative}\n\n"
        f"Signals: {total_signals} | ✅ {len(hits)} | ❌ {len(stops)} | ⏰ {len(expired)} | 🔵 {len(still_open)}\n"
        f"Hit rate (all-time): {running_hit_rate}%\n"
        f"Avg confidence — W: {avg_conf_winners} / L: {avg_conf_losers}\n"
    )
    if best:
        report += f"Best: {best['instrument']} {best['direction']} (+{best['outcome_pnl_pct']}%)\n"
    if worst:
        report += f"Worst: {worst['instrument']} {worst['direction']} ({worst['outcome_pnl_pct']}%)\n"

    report += f"\n-# MTS Weekly • {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"

    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": report}, timeout=10).raise_for_status()
        print("  [Discord] Weekly report sent.")
        log_alert(conn, "weekly_report", f"Weekly report {today_str}")
    except Exception as e:
        print(f"  [Discord] Error sending weekly report: {e}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*60}")
    print(f"  MTS — Monitor The Situation (v2)")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}")

    conn = init_db()

    # ── Check open signals ──
    check_open_signals(conn)

    # ── Weekly report (Sundays only) ──
    generate_weekly_report(conn)

    # ── Shared data ──
    print("\n[DATA]")
    print("  Economic calendar...")
    econ_events = get_upcoming_economic_events()
    if econ_events:
        print(f"  {len(econ_events)} upcoming event(s):")
        for e in econ_events:
            print(f"    {e['event']} — {e['date']} ({e['days_until']}d)")

    print("  Earnings calendar...")
    earnings = fetch_upcoming_earnings()
    if earnings:
        print(f"  {len(earnings)} upcoming earnings:")
        for e in earnings:
            print(f"    {e['ticker']} — {e['date']} ({e['days_until']}d)")

    print("  FRED data...")
    fred = fetch_fred_data()
    print(f"  {len(fred)} indicators." if fred else "  Skipped (no API key).")

    # ── Macro ──
    print("\n[MACRO]")
    print("  Fetching headlines...")
    macro_headlines = fetch_headlines("macro")
    print(f"  Total: {len(macro_headlines)} articles.")

    print("  Fetching market snapshot...")
    market = fetch_market_snapshot()
    print(f"  {len(market)} assets.")

    threshold = compute_adaptive_threshold(market, SIGNAL_THRESHOLD)
    if threshold != SIGNAL_THRESHOLD:
        print(f"  Adaptive threshold: {SIGNAL_THRESHOLD} → {threshold} (VIX: {market.get('Volatility', {}).get('price', '?')})")
    else:
        print(f"  Threshold: {threshold}/10")

    macro_memory = get_recent_memory(conn, "macro", n=5)

    print("  Claude analysis...")
    macro_analysis = analyze_macro(macro_headlines, market, macro_memory, econ_events, earnings, fred)
    macro_score    = macro_analysis.get("confidence_score", 0)
    macro_detected = macro_analysis.get("signal_detected", False)
    is_repeat      = macro_analysis.get("is_repeat_of_previous", False)
    print(f"  Signal: {macro_detected} | Score: {macro_score}/10 | Repeat: {is_repeat}")
    print(f"  {macro_analysis.get('event_summary', 'N/A')}")

    store_scan_memory(conn, "macro", macro_analysis)

    macro_fired = False
    if macro_detected and macro_score >= threshold and not is_repeat:
        event_summary = macro_analysis.get("event_summary", "")
        macro_instrument = macro_analysis.get("trade", {}).get("instrument", "")
        if is_duplicate_alert(conn, "macro", event_summary, macro_instrument):
            print("  ⚡ Deduplicated — similar alert or same instrument recently.")
        else:
            print("  🚨 Alerting Discord...")
            send_macro_alert(macro_analysis)
            log_alert(conn, "macro", event_summary)
            log_signal(conn, "macro", macro_analysis)
            macro_fired = True
    else:
        print("  ✓ No macro signal.")

    # ── Crypto ──
    crypto_fired = False
    crypto_analysis = {"confidence_score": 0}
    if CRYPTO_ENABLED:
        print("\n[CRYPTO]")
        print("  Fetching crypto headlines...")
        crypto_headlines = fetch_headlines("crypto")
        print(f"  Total: {len(crypto_headlines)} articles.")

        print("  Fetching crypto prices...")
        crypto = fetch_crypto_snapshot()
        print(f"  {len(crypto)} coins.")

        print("  Fetching fear & greed...")
        fear_greed = fetch_crypto_fear_greed()
        if fear_greed:
            print(f"  Fear & Greed: {fear_greed['value']} ({fear_greed['classification']})")

        crypto_memory = get_recent_memory(conn, "crypto", n=5)

        print("  Claude analysis...")
        crypto_analysis = analyze_crypto(crypto_headlines, crypto, fear_greed, crypto_memory)
        crypto_score    = crypto_analysis.get("confidence_score", 0)
        crypto_detected = crypto_analysis.get("signal_detected", False)
        crypto_repeat   = crypto_analysis.get("is_repeat_of_previous", False)
        print(f"  Signal: {crypto_detected} | Score: {crypto_score}/10 | Repeat: {crypto_repeat}")
        print(f"  {crypto_analysis.get('event_summary', 'N/A')}")

        store_scan_memory(conn, "crypto", crypto_analysis)

        if crypto_detected and crypto_score >= threshold and not crypto_repeat:
            event_summary = crypto_analysis.get("event_summary", "")
            crypto_instrument = crypto_analysis.get("trade", {}).get("coin", "")
            if is_duplicate_alert(conn, "crypto", event_summary, crypto_instrument):
                print("  ⚡ Deduplicated — similar alert or same coin recently.")
            else:
                print("  🚨 Alerting Discord...")
                send_crypto_alert(crypto_analysis)
                log_alert(conn, "crypto", event_summary)
                log_signal(conn, "crypto", crypto_analysis)
                crypto_fired = True
        else:
            print("  ✓ No crypto signal.")
    else:
        print("\n[CRYPTO] Disabled.")

    # ── Heartbeat ──
    if not macro_fired and not crypto_fired:
        send_heartbeat(conn, macro_analysis, crypto_analysis, threshold)

    conn.close()
    print("\nDone.\n")


if __name__ == "__main__":
    main()
