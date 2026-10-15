"""Collect daily concert ticket price snapshots from SeatGeek and Ticketmaster.

Each run pulls upcoming concerts for the cities/keywords in config.json and
stores one price snapshot per event per day in a local SQLite database.
Run it once a day (see README.md) to build up price history over time.

Usage:
    python collector.py                # collect from all enabled sources
    python collector.py --source seatgeek
    python collector.py --stats        # show what's in the database
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
ENV_PATH = ROOT / ".env"

SEATGEEK_URL = "https://api.seatgeek.com/2/events"
TICKETMASTER_URL = "https://app.ticketmaster.com/discovery/v2/events.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    source          TEXT NOT NULL,
    event_id        TEXT NOT NULL,
    name            TEXT,
    performers      TEXT,      -- JSON list of performer names
    venue           TEXT,
    city            TEXT,
    state           TEXT,
    venue_capacity  INTEGER,
    event_datetime  TEXT,      -- local time of the show
    url             TEXT,
    first_seen      TEXT,
    last_seen       TEXT,
    PRIMARY KEY (source, event_id)
);

CREATE TABLE IF NOT EXISTS price_snapshots (
    source          TEXT NOT NULL,
    event_id        TEXT NOT NULL,
    snapshot_date   TEXT NOT NULL,   -- one row per event per day
    captured_at     TEXT NOT NULL,
    days_until_event INTEGER,
    price_low       REAL,
    price_avg       REAL,
    price_median    REAL,
    price_high      REAL,
    listing_count   INTEGER,
    popularity      REAL,            -- SeatGeek event score
    performer_score REAL,            -- SeatGeek top performer score
    status          TEXT,            -- Ticketmaster sale status
    currency        TEXT,
    raw_json        TEXT,
    PRIMARY KEY (source, event_id, snapshot_date)
);

CREATE TABLE IF NOT EXISTS artists (
    source          TEXT NOT NULL,
    artist_id       TEXT NOT NULL,   -- SeatGeek performer id / Ticketmaster attraction id
    name            TEXT,
    first_seen      TEXT,
    last_seen       TEXT,
    PRIMARY KEY (source, artist_id)
);

-- Which artists are on which event's bill (many-to-many).
CREATE TABLE IF NOT EXISTS event_artists (
    source          TEXT NOT NULL,
    event_id        TEXT NOT NULL,
    artist_id       TEXT NOT NULL,
    PRIMARY KEY (source, event_id, artist_id)
);
"""


# --------------------------------------------------------------------------
# Setup helpers
# --------------------------------------------------------------------------

def load_env(path=ENV_PATH):
    """Load KEY=value lines from .env into os.environ (without overriding)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config(path=CONFIG_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def connect_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def get_json(url, params, retries=3):
    """GET a JSON endpoint, retrying on rate limits and transient errors."""
    full_url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    req = urllib.request.Request(full_url, headers={"User-Agent": "concert-ticket-prices/1.0"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                wait = 2 ** attempt * 5
                log(f"  HTTP {e.code}, retrying in {wait}s...")
                time.sleep(wait)
                continue
            body = e.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"HTTP {e.code} from {url}: {body}") from e
        except OSError as e:  # URLError, connection resets, timeouts
            if attempt < retries - 1:
                time.sleep(5)
                continue
            raise RuntimeError(f"Network error calling {url}: {getattr(e, 'reason', e)}") from e


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def days_until(event_dt_str, now):
    if not event_dt_str:
        return None
    try:
        event_date = datetime.fromisoformat(event_dt_str.replace("Z", "")).date()
    except ValueError:
        return None
    return (event_date - now.date()).days


# --------------------------------------------------------------------------
# SeatGeek
# --------------------------------------------------------------------------

def fetch_seatgeek(cfg, client_id):
    """Yield raw SeatGeek event dicts for each configured query."""
    now = datetime.now()
    base = {
        "client_id": client_id,
        "taxonomies.name": "concert",
        "datetime_local.gte": now.strftime("%Y-%m-%d"),
        "datetime_local.lte": (now + timedelta(days=cfg["days_ahead"])).strftime("%Y-%m-%d"),
        "per_page": 100,
        "sort": "datetime_local.asc",
    }
    # Price stats are only returned for apps SeatGeek has approved; approval
    # comes with a client secret.
    if os.environ.get("SEATGEEK_CLIENT_SECRET"):
        base["client_secret"] = os.environ["SEATGEEK_CLIENT_SECRET"]
    for query in build_queries(cfg):
        params = dict(base)
        if query.get("city"):
            params["venue.city"] = query["city"]
        if query.get("state"):
            params["venue.state"] = query["state"]
        if query.get("keyword"):
            params["q"] = query["keyword"]

        for page in range(1, cfg["max_pages"] + 1):
            params["page"] = page
            data = get_json(SEATGEEK_URL, params)
            events = data.get("events", [])
            yield from events
            total = data.get("meta", {}).get("total", 0)
            if page * params["per_page"] >= total or not events:
                break
            time.sleep(cfg["request_delay_seconds"])
        time.sleep(cfg["request_delay_seconds"])


def parse_seatgeek(ev, now):
    venue = ev.get("venue") or {}
    performers = ev.get("performers") or []
    stats = ev.get("stats") or {}
    event_dt = ev.get("datetime_local")
    event_row = {
        "source": "seatgeek",
        "event_id": str(ev["id"]),
        "name": ev.get("title"),
        "performers": json.dumps([p.get("name") for p in performers]),
        "venue": venue.get("name"),
        "city": venue.get("city"),
        "state": venue.get("state"),
        "venue_capacity": venue.get("capacity") or None,
        "event_datetime": event_dt,
        "url": ev.get("url"),
    }
    snap = {
        "source": "seatgeek",
        "event_id": str(ev["id"]),
        "days_until_event": days_until(event_dt, now),
        "price_low": stats.get("lowest_price"),
        "price_avg": stats.get("average_price"),
        "price_median": stats.get("median_price"),
        "price_high": stats.get("highest_price"),
        "listing_count": stats.get("listing_count"),
        "popularity": ev.get("score"),
        "performer_score": max((p.get("score") or 0 for p in performers), default=None),
        "status": None,
        "currency": "USD",
        "raw_json": json.dumps({"stats": stats, "score": ev.get("score")}),
    }
    artists = [(str(p["id"]), p.get("name")) for p in performers if p.get("id") is not None]
    return event_row, snap, artists


# --------------------------------------------------------------------------
# Ticketmaster
# --------------------------------------------------------------------------

def fetch_ticketmaster(cfg, api_key):
    """Yield raw Ticketmaster event dicts for each configured query."""
    now = datetime.now(timezone.utc)
    page_size = 200
    window_days = 30
    base = {
        "apikey": api_key,
        "classificationName": "music",
        "size": page_size,
        "sort": "date,asc",
        "countryCode": cfg.get("country_code", "US"),
    }
    # Ticketmaster refuses to page past 1000 results (size * page < 1000),
    # so busy cities are searched in date windows to stay under that cap.
    windows = []
    for offset in range(0, cfg["days_ahead"], window_days):
        start = now + timedelta(days=offset)
        end = now + timedelta(days=min(offset + window_days, cfg["days_ahead"]))
        windows.append((start, end))

    for query in build_queries(cfg):
        for start, end in windows:
            params = dict(base)
            params["startDateTime"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
            params["endDateTime"] = end.strftime("%Y-%m-%dT%H:%M:%SZ")
            if query.get("city"):
                params["city"] = query["city"]
            if query.get("state"):
                params["stateCode"] = query["state"]
            if query.get("keyword"):
                params["keyword"] = query["keyword"]

            max_pages = min(cfg["max_pages"], 1000 // page_size)
            for page in range(max_pages):
                params["page"] = page
                data = get_json(TICKETMASTER_URL, params)
                events = (data.get("_embedded") or {}).get("events", [])
                yield from events
                total_pages = (data.get("page") or {}).get("totalPages", 0)
                if page + 1 >= total_pages or not events:
                    break
                time.sleep(cfg["request_delay_seconds"])
            time.sleep(cfg["request_delay_seconds"])


def parse_ticketmaster(ev, now):
    embedded = ev.get("_embedded") or {}
    venue = (embedded.get("venues") or [{}])[0]
    attractions = embedded.get("attractions") or []
    start = (ev.get("dates") or {}).get("start") or {}
    status = ((ev.get("dates") or {}).get("status") or {}).get("code")
    ranges = ev.get("priceRanges") or []

    event_dt = start.get("localDate")
    if event_dt and start.get("localTime"):
        event_dt = f"{event_dt}T{start['localTime']}"

    event_row = {
        "source": "ticketmaster",
        "event_id": ev["id"],
        "name": ev.get("name"),
        "performers": json.dumps([a.get("name") for a in attractions]),
        "venue": venue.get("name"),
        "city": (venue.get("city") or {}).get("name"),
        "state": (venue.get("state") or {}).get("stateCode"),
        "venue_capacity": None,
        "event_datetime": event_dt,
        "url": ev.get("url"),
    }
    mins = [r["min"] for r in ranges if r.get("min") is not None]
    maxs = [r["max"] for r in ranges if r.get("max") is not None]
    snap = {
        "source": "ticketmaster",
        "event_id": ev["id"],
        "days_until_event": days_until(event_dt, now),
        "price_low": min(mins) if mins else None,
        "price_avg": None,
        "price_median": None,
        "price_high": max(maxs) if maxs else None,
        "listing_count": None,
        "popularity": None,
        "performer_score": None,
        "status": status,
        "currency": ranges[0].get("currency") if ranges else None,
        "raw_json": json.dumps({"priceRanges": ranges, "status": status}),
    }
    artists = [(a["id"], a.get("name")) for a in attractions if a.get("id")]
    return event_row, snap, artists


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def build_queries(cfg):
    """Combine cities and keywords into the list of searches to run."""
    cities = cfg.get("cities") or [{}]
    keywords = cfg.get("keywords") or [None]
    queries = []
    for city in cities:
        for kw in keywords:
            queries.append({"city": city.get("city"), "state": city.get("state"), "keyword": kw})
    return queries


def save(conn, event_row, snap, artists, now):
    now_iso = now.isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO events (source, event_id, name, performers, venue, city, state,
                            venue_capacity, event_datetime, url, first_seen, last_seen)
        VALUES (:source, :event_id, :name, :performers, :venue, :city, :state,
                :venue_capacity, :event_datetime, :url, :now, :now)
        ON CONFLICT (source, event_id) DO UPDATE SET
            name = excluded.name,
            performers = excluded.performers,
            venue = excluded.venue,
            city = excluded.city,
            state = excluded.state,
            venue_capacity = COALESCE(excluded.venue_capacity, events.venue_capacity),
            event_datetime = excluded.event_datetime,
            url = excluded.url,
            last_seen = excluded.last_seen
        """,
        {**event_row, "now": now_iso},
    )
    # If the collector runs more than once a day, the latest run wins.
    conn.execute(
        """
        INSERT OR REPLACE INTO price_snapshots
            (source, event_id, snapshot_date, captured_at, days_until_event,
             price_low, price_avg, price_median, price_high, listing_count,
             popularity, performer_score, status, currency, raw_json)
        VALUES
            (:source, :event_id, :snapshot_date, :captured_at, :days_until_event,
             :price_low, :price_avg, :price_median, :price_high, :listing_count,
             :popularity, :performer_score, :status, :currency, :raw_json)
        """,
        {**snap, "snapshot_date": now.date().isoformat(), "captured_at": now_iso},
    )
    for artist_id, name in artists:
        conn.execute(
            """
            INSERT INTO artists (source, artist_id, name, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (source, artist_id) DO UPDATE SET
                name = excluded.name,
                last_seen = excluded.last_seen
            """,
            (event_row["source"], artist_id, name, now_iso, now_iso),
        )
        conn.execute(
            "INSERT OR IGNORE INTO event_artists (source, event_id, artist_id) VALUES (?, ?, ?)",
            (event_row["source"], event_row["event_id"], artist_id),
        )


def collect(conn, source, fetcher, parser, cfg, key):
    now = datetime.now()
    seen = set()
    count = 0
    for ev in fetcher(cfg, key):
        event_row, snap, artists = parser(ev, now)
        if event_row["event_id"] in seen:  # same event can match several queries
            continue
        seen.add(event_row["event_id"])
        save(conn, event_row, snap, artists, now)
        count += 1
    conn.commit()
    log(f"{source}: saved {count} event snapshots")
    return count


def show_stats(conn):
    rows = conn.execute(
        """
        SELECT source, COUNT(DISTINCT event_id), COUNT(*), MIN(snapshot_date), MAX(snapshot_date)
        FROM price_snapshots GROUP BY source
        """
    ).fetchall()
    if not rows:
        print("Database is empty. Run `python collector.py` first.")
        return
    print(f"{'source':<14}{'events':>8}{'snapshots':>11}  date range")
    for source, events, snaps, first, last in rows:
        print(f"{source:<14}{events:>8}{snaps:>11}  {first} -> {last}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", choices=["seatgeek", "ticketmaster"],
                        help="only collect from this source")
    parser.add_argument("--stats", action="store_true", help="show database summary and exit")
    args = parser.parse_args()

    load_env()
    cfg = load_config()
    conn = connect_db(ROOT / cfg.get("database", "ticket_prices.db"))

    if args.stats:
        show_stats(conn)
        return 0

    sources = {
        "seatgeek": (fetch_seatgeek, parse_seatgeek, "SEATGEEK_CLIENT_ID"),
        "ticketmaster": (fetch_ticketmaster, parse_ticketmaster, "TICKETMASTER_API_KEY"),
    }
    wanted = [args.source] if args.source else cfg.get("sources", list(sources))

    failures = 0
    for name in wanted:
        fetcher, parse, env_var = sources[name]
        key = os.environ.get(env_var)
        if not key:
            log(f"{name}: skipped, {env_var} is not set (add it to .env)")
            failures += 1
            continue
        log(f"{name}: collecting...")
        try:
            collect(conn, name, fetcher, parse, cfg, key)
        except RuntimeError as e:
            log(f"{name}: FAILED - {e}")
            failures += 1

    conn.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
