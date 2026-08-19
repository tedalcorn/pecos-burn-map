#!/usr/bin/env python3
"""Scrape AllTrails routes + reviews for the Pecos map area into SQLite.

Anonymous (no AllTrails login). Cloudflare requires a real Chrome:
we launch Chrome unattached so the challenge solves itself, then attach
via CDP and make in-page fetch() calls against AllTrails' internal API.

Run: python3 alltrails/scrape_alltrails.py [--full]
  default: incremental (stop paging a trail once we hit known review ids)
  --full:  re-page everything

Writes alltrails/alltrails.db (gitignored — raw review text stays local).
"""

import json
import math
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
DB = HERE / "alltrails.db"
PROFILE = HERE / "chrome-profile"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 9271
KEY = "3p0t5s6b5g4g0e8k3c1j3w7y5c3m4t8i"   # AllTrails web client's public API key
PAUSE = 1.5          # seconds between API calls — be polite
PER_PAGE = 50
# Pecos Wilderness bbox (wilderness +0.06°, matches fetch_data.py AOI)
BBOX = {"xmin": -105.935, "ymin": 35.647, "xmax": -105.331, "ymax": 36.132}
# keep routes whose start point is within this many degrees of the wilderness polygon
WILD_DIST = 0.06
WILDERNESS_GEOJSON = HERE.parent / "docs" / "data" / "wilderness.geojson"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def init_db():
    con = sqlite3.connect(DB)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS trails(
      id INTEGER PRIMARY KEY, slug TEXT, name TEXT, length_m REAL,
      start_lat REAL, start_lng REAL, num_reviews INTEGER, avg_rating REAL,
      is_closed INTEGER, popularity REAL, raw_json TEXT,
      first_seen TEXT, last_seen TEXT);
    CREATE TABLE IF NOT EXISTS reviews(
      id INTEGER PRIMARY KEY, trail_id INTEGER, date TEXT, rating INTEGER,
      comment TEXT, obstacles TEXT, activity TEXT, lang TEXT,
      raw_json TEXT, fetched_at TEXT);
    CREATE INDEX IF NOT EXISTS idx_reviews_trail ON reviews(trail_id, date);
    CREATE TABLE IF NOT EXISTS runs(ts TEXT, trails_n INTEGER, new_reviews INTEGER, note TEXT);
    """)
    return con


LANDING = "https://www.alltrails.com/trail/us/new-mexico/lake-katherine-via-winsor-trail"
PROBE_TRAIL = 10030667          # Lake Katherine — used to confirm the API answers
WARMUPS = (25, 45, 75, 120)     # per-attempt Cloudflare settle time, seconds


def kill_chrome():
    subprocess.run(["pkill", "-f", f"remote-debugging-port={PORT}"], capture_output=True)
    time.sleep(2)


def launch_chrome(warmup):
    kill_chrome()
    subprocess.Popen([
        CHROME, f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}",
        "--no-first-run", "--window-size=1200,850", "--window-position=2000,100",
        LANDING,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Chrome must load the page UNATTACHED so the Cloudflare challenge solves
    # itself; attaching CDP too early trips its automation detection.
    time.sleep(warmup)


def connect(p):
    """Launch Chrome and attach, retrying with longer warmups until AllTrails'
    API actually answers. Unattended (launchd) runs are slower to clear the
    challenge than interactive ones — a single fixed wait fails intermittently,
    which is exactly how the 2026-08-02 run died."""
    last = ""
    for attempt, warmup in enumerate(WARMUPS, 1):
        log(f"Chrome attempt {attempt}/{len(WARMUPS)} (warmup {warmup}s)")
        launch_chrome(warmup)
        try:
            b = p.chromium.connect_over_cdp(f"http://localhost:{PORT}", timeout=60000)
            pg = b.contexts[0].pages[0]
            # Probe the API we actually depend on — a far better readiness test
            # than page length, which passes on Cloudflare's own error page.
            for extra in (0, 20, 30):
                if extra:
                    log(f"  not ready; waiting {extra}s more")
                    time.sleep(extra)
                    try:
                        pg.reload(timeout=60000)
                        time.sleep(6)
                    except Exception:
                        pass
                r = pg.evaluate(
                    """async (u) => {
                        try {
                            const r = await fetch(u, {headers: {'accept':'application/json'}});
                            return {status: r.status, len: (await r.text()).length};
                        } catch (e) { return {status: -1, len: 0, err: String(e)}; }
                    }""",
                    f"/api/alltrails/v2/trails/{PROBE_TRAIL}?key={KEY}")
                if r.get("status") == 200 and r.get("len", 0) > 500:
                    log(f"  API reachable (probe {r['len']} bytes) — proceeding")
                    return b, pg
                last = f"probe status={r.get('status')} len={r.get('len')}"
            b.close()
        except Exception as e:
            last = repr(e)[:160]
            log(f"  attach failed: {last}")
        kill_chrome()
    sys.exit(f"Cloudflare never cleared after {len(WARMUPS)} attempts — last: {last}")


class AT:
    """In-page fetch wrapper over the attached CDP session."""

    def __init__(self, pg):
        self.pg = pg

    def get(self, path):
        time.sleep(PAUSE)
        r = self.pg.evaluate(
            """async (u) => {
                const r = await fetch(u, {headers: {'accept':'application/json'}});
                return {status: r.status, text: await r.text()};
            }""", path)
        if r["status"] != 200:
            raise RuntimeError(f"GET {path.split('?')[0]} -> {r['status']}: {r['text'][:200]}")
        return json.loads(r["text"])

    def post(self, path, body):
        time.sleep(PAUSE)
        r = self.pg.evaluate(
            """async ([u, b]) => {
                const r = await fetch(u, {method:'POST',
                    headers: {'accept':'application/json','content-type':'application/json'},
                    body: JSON.stringify(b)});
                return {status: r.status, text: await r.text()};
            }""", [path, body])
        if r["status"] != 200:
            raise RuntimeError(f"POST {path} -> {r['status']}: {r['text'][:200]}")
        return json.loads(r["text"])


def discover_trails(at):
    """All AllTrails routes in the bbox, filtered to near-wilderness."""
    body = {
        "filters": {"hasTrails": True},
        "recordTypesToReturn": ["trail"],
        "recordAttributesToRetrieve": [
            "ID", "name", "slug", "length", "avg_rating", "num_reviews",
            "is_closed", "popularity", "_geoloc"],
        "location": {
            "mapRotation": 0,
            "topLeft": {"lat": BBOX["ymax"], "lng": BBOX["xmin"]},
            "topRight": {"lat": BBOX["ymax"], "lng": BBOX["xmax"]},
            "bottomLeft": {"lat": BBOX["ymin"], "lng": BBOX["xmin"]},
            "bottomRight": {"lat": BBOX["ymin"], "lng": BBOX["xmax"]},
        },
        "limit": 500,
    }
    d = at.post("/api/alltrails/explore/v1/search?nls=false", body)
    results = [r for r in d.get("searchResults", []) if r.get("type") == "trail"]
    log(f"explore search: {len(results)} routes in bbox")

    from shapely.geometry import shape, Point
    wild = shape(json.load(open(WILDERNESS_GEOJSON))["features"][0]["geometry"])
    keep = [r for r in results
            if wild.distance(Point(r["_geoloc"]["lng"], r["_geoloc"]["lat"])) < WILD_DIST]
    log(f"kept {len(keep)} routes within {WILD_DIST}° of the wilderness")
    return keep


def scrape_reviews(at, con, trail_id, known_ids, full=False):
    """Page through a trail's reviews, newest first; stop at known ids unless full."""
    new = 0
    page = 1
    while True:
        d = at.get(f"/api/alltrails/v2/trails/{trail_id}/reviews?per_page={PER_PAGE}&page={page}&key={KEY}")
        revs = d.get("trail_reviews", [])
        if not revs:
            break
        hit_known = False
        for rv in revs:
            if rv["id"] in known_ids:
                hit_known = True
                continue
            con.execute(
                "INSERT OR IGNORE INTO reviews VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rv["id"], trail_id, rv.get("date"), rv.get("rating"),
                 rv.get("comment"),
                 json.dumps(rv.get("obstacles") or rv.get("trailConditions") or []),
                 (rv.get("activity") or {}).get("uid") if isinstance(rv.get("activity"), dict) else rv.get("activity"),
                 rv.get("comment_lang"),
                 json.dumps({k: rv.get(k) for k in
                             ("obstacles", "trailConditions", "difficulty", "votes", "commentFeatures")}),
                 datetime.now(timezone.utc).isoformat(timespec="seconds")))
            new += 1
        con.commit()
        if (hit_known and not full) or len(revs) < PER_PAGE:
            break
        page += 1
    return new


def main():
    full = "--full" in sys.argv
    con = init_db()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with sync_playwright() as p:
        b, pg = connect(p)
        at = AT(pg)

        trails = discover_trails(at)
        total_new = 0
        for i, t in enumerate(trails, 1):
            tid = t["ID"]
            con.execute(
                """INSERT INTO trails VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET num_reviews=excluded.num_reviews,
                     avg_rating=excluded.avg_rating, is_closed=excluded.is_closed,
                     popularity=excluded.popularity, last_seen=excluded.last_seen""",
                (tid, t.get("slug"), t.get("name"), t.get("length"),
                 t["_geoloc"]["lat"], t["_geoloc"]["lng"],
                 t.get("num_reviews"), t.get("avg_rating"),
                 int(bool(t.get("is_closed"))), t.get("popularity"),
                 json.dumps(t), now, now))
            known = {r[0] for r in con.execute(
                "SELECT id FROM reviews WHERE trail_id=?", (tid,))}
            db_n = len(known)
            site_n = t.get("num_reviews") or 0
            if not full and db_n >= site_n:
                log(f"[{i}/{len(trails)}] {t['name']}: up to date ({db_n})")
                continue
            new = scrape_reviews(at, con, tid, known, full=full)
            total_new += new
            log(f"[{i}/{len(trails)}] {t['name']}: +{new} reviews (site says {site_n})")

        con.execute("INSERT INTO runs VALUES (?,?,?,?)",
                    (now, len(trails), total_new, "full" if full else "incremental"))
        con.commit()
        log(f"done: {total_new} new reviews across {len(trails)} routes")

    subprocess.run(["pkill", "-f", f"remote-debugging-port={PORT}"], capture_output=True)


if __name__ == "__main__":
    main()
