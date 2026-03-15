"""
MTS Dashboard API
Reads from the MTS SQLite database and serves data to the frontend.
"""

import os
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

DB_PATH = os.getenv("MTS_DB_PATH", "/data/mts.db")

app = FastAPI(title="MTS Dashboard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# ── API Routes ───────────────────────────────────────────────────────────────

@app.get("/api/status")
def status():
    """Current system status — last scan time, open signals, etc."""
    conn = get_db()
    last_scan = conn.execute(
        "SELECT timestamp, scan_type, event_summary, confidence_score, signal_detected "
        "FROM scan_memory ORDER BY id DESC LIMIT 1"
    ).fetchone()

    open_count = conn.execute(
        "SELECT COUNT(*) as c FROM signals WHERE outcome = 'OPEN'"
    ).fetchone()["c"]

    total_signals = conn.execute("SELECT COUNT(*) as c FROM signals").fetchone()["c"]
    resolved = conn.execute("SELECT COUNT(*) as c FROM signals WHERE outcome != 'OPEN'").fetchone()["c"]
    hits = conn.execute("SELECT COUNT(*) as c FROM signals WHERE outcome = 'HIT_TARGET'").fetchone()["c"]

    conn.close()
    return {
        "last_scan": dict(last_scan) if last_scan else None,
        "open_signals": open_count,
        "total_signals": total_signals,
        "resolved_signals": resolved,
        "hit_rate": round(hits / resolved * 100, 1) if resolved > 0 else 0,
        "hits": hits,
    }


@app.get("/api/signals")
def signals(status: str = "all", limit: int = 50):
    """List signals with optional status filter."""
    conn = get_db()
    if status == "all":
        rows = conn.execute(
            "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM signals WHERE outcome = ? ORDER BY id DESC LIMIT ?",
            (status.upper(), limit),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/signals/{signal_id}")
def signal_detail(signal_id: int):
    """Get full signal detail including Claude's analysis."""
    conn = get_db()
    row = conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
    conn.close()
    if not row:
        return {"error": "Signal not found"}
    result = dict(row)
    if result.get("full_analysis"):
        try:
            result["full_analysis"] = json.loads(result["full_analysis"])
        except json.JSONDecodeError:
            pass
    return result


@app.get("/api/scans")
def scans(scan_type: str = "all", limit: int = 30):
    """Recent scan history."""
    conn = get_db()
    if scan_type == "all":
        rows = conn.execute(
            "SELECT * FROM scan_memory ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM scan_memory WHERE scan_type = ? ORDER BY id DESC LIMIT ?",
            (scan_type, limit),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/performance")
def performance():
    """Performance stats for the dashboard."""
    conn = get_db()

    # Outcome breakdown
    outcomes = conn.execute(
        "SELECT outcome, COUNT(*) as count FROM signals GROUP BY outcome"
    ).fetchall()
    outcome_map = {r["outcome"]: r["count"] for r in outcomes}

    # Weekly signal counts (last 8 weeks)
    weekly = conn.execute("""
        SELECT strftime('%Y-W%W', timestamp) as week,
               COUNT(*) as signals,
               SUM(CASE WHEN outcome = 'HIT_TARGET' THEN 1 ELSE 0 END) as hits,
               SUM(CASE WHEN outcome = 'HIT_STOP' THEN 1 ELSE 0 END) as stops,
               SUM(CASE WHEN outcome = 'EXPIRED' THEN 1 ELSE 0 END) as expired
        FROM signals
        GROUP BY week
        ORDER BY week DESC
        LIMIT 8
    """).fetchall()

    # Confidence distribution of winners vs losers
    winner_conf = conn.execute(
        "SELECT confidence_score FROM signals WHERE outcome = 'HIT_TARGET'"
    ).fetchall()
    loser_conf = conn.execute(
        "SELECT confidence_score FROM signals WHERE outcome = 'HIT_STOP'"
    ).fetchall()

    avg_winner_conf = (
        round(sum(r["confidence_score"] for r in winner_conf) / len(winner_conf), 1)
        if winner_conf else 0
    )
    avg_loser_conf = (
        round(sum(r["confidence_score"] for r in loser_conf) / len(loser_conf), 1)
        if loser_conf else 0
    )

    # P&L distribution (only valid ones)
    pnl_data = conn.execute(
        "SELECT instrument, direction, outcome_pnl_pct, outcome, confidence_score, timestamp "
        "FROM signals WHERE outcome_pnl_pct IS NOT NULL ORDER BY timestamp DESC"
    ).fetchall()

    # Instrument breakdown
    instrument_stats = conn.execute("""
        SELECT instrument,
               COUNT(*) as total,
               SUM(CASE WHEN outcome = 'HIT_TARGET' THEN 1 ELSE 0 END) as hits,
               SUM(CASE WHEN outcome = 'HIT_STOP' THEN 1 ELSE 0 END) as stops,
               AVG(CASE WHEN outcome_pnl_pct IS NOT NULL THEN outcome_pnl_pct END) as avg_pnl
        FROM signals
        WHERE instrument IS NOT NULL AND instrument != ''
        GROUP BY instrument
        ORDER BY total DESC
    """).fetchall()

    conn.close()
    return {
        "outcomes": outcome_map,
        "weekly": [dict(r) for r in reversed(list(weekly))],
        "avg_winner_confidence": avg_winner_conf,
        "avg_loser_confidence": avg_loser_conf,
        "pnl_data": [dict(r) for r in pnl_data],
        "instrument_stats": [dict(r) for r in instrument_stats],
    }


@app.get("/api/alerts")
def alerts(limit: int = 20):
    """Recent alert log."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM alert_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/headlines")
def headlines():
    """Live headlines from RSS feeds + recent MTS analysis summaries."""
    import feedparser
    from datetime import timezone as tz

    rss_feeds = [
        "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx1YlY4U0FtVnVHZ0pWVXcoQAFQAQ?hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/topics/CAAqIggKIhxDQkFTRHdvSkwyMHZNR1ptTUhBU0FtVnVLQUFQAQ?hl=en-US&gl=US&ceid=US:en",
    ]

    articles = []
    for feed_url in rss_feeds:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:10]:
                published = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    published = datetime(*entry.published_parsed[:6], tzinfo=tz.utc).isoformat()
                articles.append({
                    "title": entry.get("title", "").strip(),
                    "url": entry.get("link", ""),
                    "date": published or "",
                    "source": feed.feed.get("title", "News"),
                })
        except Exception:
            pass

    # Also get recent MTS analysis summaries
    conn = get_db()
    scans = conn.execute(
        """SELECT scan_type, timestamp, event_summary, confidence_score, signal_detected
           FROM scan_memory ORDER BY id DESC LIMIT 10"""
    ).fetchall()
    conn.close()

    return {
        "articles": articles[:20],
        "analysis": [dict(r) for r in scans],
    }


# ── Serve Frontend ───────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")
