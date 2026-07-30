#!/usr/bin/env python3
"""Match AllTrails routes to USFS trails and derive per-trail condition signals.

Input:  alltrails/alltrails.db (from scrape_alltrails.py), docs/data/trails.geojson
Output: docs/data/conditions.json — derived signals only (counts, rates, flags);
        no review text is published.

Matching, in order of confidence:
  1. explicit trail numbers in the route name, e.g. "Borrego (150), Winsor (254)..."
  2. USFS trail names appearing as words in the route name
  3. route start point within ~250 m of a USFS trail (adds the trailhead trail)

Condition signal per calendar month per USFS trail:
  - review count, mean rating
  - mention-rate of each condition tag (structured `obstacles` field + text lexicon)
Flags compare the last 12 months to the same-calendar-month baseline from prior
years, so seasonal patterns (summer bugs, winter ice) don't fire alerts.
"""

import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from shapely.geometry import shape, Point
from shapely.ops import unary_union

HERE = Path(__file__).resolve().parent
DB = HERE / "alltrails.db"
TRAILS_GJ = HERE.parent / "docs" / "data" / "trails.geojson"
OUT = HERE.parent / "docs" / "data" / "conditions.json"

START_MATCH_DEG = 0.0025   # ~250 m
RECENT_MONTHS = 12
MIN_MENTIONS_TO_FLAG = 4
FLAG_RATIO = 2.0           # recent rate must exceed baseline × this
# Only structural conditions raise a map flag; transient/seasonal tags
# (mud, bugs, snow) stay in the stats but never alarm.
FLAGGABLE = {"deadfall", "washout", "overgrown", "hard_to_follow", "burn"}
RATING_DROP_FLAG = 0.3     # long-term rating drop threshold
MIN_REVIEWS_FOR_RATING = 20

# tag -> regex over comment text (case-insensitive). Structured `obstacles`
# values are mapped onto the same tags below.
LEXICON = {
    "deadfall":   r"deadfall|dead fall|blowdown|blow down|downed tree|down tree|fallen tree|trees? (?:down|across)|log ?jam",
    "washout":    r"wash(?:ed)? ?out|erod|landslide|slide area",
    "overgrown":  r"overgrown|over grown|bushwhack|brushy",
    "hard_to_follow": r"hard to follow|lost the trail|lose the trail|easy to lose|route.?finding|poorly marked|unmarked|faint trail",
    "snow_ice":   r"\bsnow|icy|\bice\b|posthol|microspikes|crampon",
    "mud":        r"\bmud|boggy|swampy",
    "bugs":       r"\bbugs?\b|mosquito|gnats|biting flies|deer ?fl(?:y|ies)",
    "burn":       r"\bburn(?:ed|t)? (?:area|zone|scar|forest)|fire damage|charred",
    "maintained": r"trail crew|freshly (?:cleared|cut)|recently (?:cleared|maintained|logged)|cut out|sawn|sawed out|log(?:ged)? out|well[- ]maintained",
}
OBSTACLE_MAP = {   # structured obstacle uids -> our tags (None = ignore)
    "blowdown": "deadfall", "fallen-trees": "deadfall",
    "washed-out": "washout", "erosion": "washout", "bridge-out": "washout",
    "flooded": "washout",
    "overgrown": "overgrown",
    "off-trail": "hard_to_follow",
    "snow": "snow_ice", "icy": "snow_ice", "ice": "snow_ice",
    "muddy": "mud", "mud": "mud",
    "bugs": "bugs", "insects": "bugs",
    "great": "maintained", "well-maintained": "maintained",
    "rocky": None, "slippery": None, "no-shade": None,
}


def usfs_trails():
    gj = json.load(open(TRAILS_GJ))
    merged = {}
    for f in gj["features"]:
        p = f["properties"]
        no = p.get("trail_no")
        if not no:
            continue
        name = (p.get("trail_name") or "").strip().upper()
        merged.setdefault(no, {"name": name, "geoms": [], "miles": 0})
        merged[no]["geoms"].append(shape(f["geometry"]))
        merged[no]["miles"] += p.get("gis_miles") or 0
        if len(name) > len(merged[no]["name"]):
            merged[no]["name"] = name
    for no, t in merged.items():
        t["geom"] = unary_union(t["geoms"])
        del t["geoms"]
    return merged


def match_routes(con, usfs):
    """AllTrails trail_id -> set of USFS trail_no, with method notes."""
    matches = {}
    name_index = {no: t["name"] for no, t in usfs.items() if len(t["name"]) >= 4}
    for tid, name, lat, lng in con.execute(
            "SELECT id, name, start_lat, start_lng FROM trails"):
        got = {}
        # 1. explicit numbers in parentheses
        for m in re.findall(r"\((\d{1,3}[A-Z]?)\)", name or ""):
            if m in usfs:
                got[m] = "number"
        # 2. USFS trail names as words in the route name
        up = (name or "").upper()
        for no, tname in name_index.items():
            if no in got:
                continue
            if re.search(rf"\b{re.escape(tname)}\b", up):
                got[no] = "name"
        # 3. start point near a USFS trail
        pt = Point(lng, lat)
        best, best_d = None, START_MATCH_DEG
        for no, t in usfs.items():
            d = t["geom"].distance(pt)
            if d < best_d:
                best, best_d = no, d
        if best and best not in got:
            got[best] = "startpoint"
        matches[tid] = got
    return matches


def tag_review(comment, obstacles_json):
    tags = set()
    try:
        for ob in json.loads(obstacles_json or "[]"):
            uid = (ob.get("uid") or ob.get("name") or ob if isinstance(ob, dict) else ob)
            uid = str(uid).lower().replace(" ", "-")
            t = OBSTACLE_MAP.get(uid)
            if t:
                tags.add(t)
    except (json.JSONDecodeError, AttributeError):
        pass
    c = (comment or "").lower()
    for tag, rx in LEXICON.items():
        if re.search(rx, c):
            tags.add(tag)
    return tags


def main():
    con = sqlite3.connect(DB)
    usfs = usfs_trails()
    matches = match_routes(con, usfs)
    n_matched = sum(1 for v in matches.values() if v)
    print(f"matched {n_matched}/{len(matches)} AllTrails routes to USFS trails")

    # per-USFS monthly + annual aggregates
    monthly = defaultdict(lambda: {"n": 0, "rating_sum": 0, "rated_n": 0,
                                   "tags": defaultdict(int)})
    annual = defaultdict(lambda: {"n": 0, "rating_sum": 0, "rated_n": 0})
    all_ratings = [0, 0]
    for tid, date, rating, comment, obstacles in con.execute(
            "SELECT trail_id, date, rating, comment, obstacles FROM reviews WHERE date IS NOT NULL"):
        nos = matches.get(tid) or {}
        if not nos:
            continue
        ym = date[:7]
        tags = tag_review(comment, obstacles)
        if rating:
            all_ratings[0] += rating
            all_ratings[1] += 1
        for no in nos:
            b = monthly[(no, ym)]
            b["n"] += 1
            if rating:
                b["rating_sum"] += rating
                b["rated_n"] += 1
            for t in tags:
                b["tags"][t] += 1
            a = annual[(no, ym[:4])]
            a["n"] += 1
            if rating:
                a["rating_sum"] += rating
                a["rated_n"] += 1

    now = datetime.now(timezone.utc)
    cur_ym = now.strftime("%Y-%m")
    recent_yms = sorted({ym for (_, ym) in monthly if ym <= cur_ym})[-RECENT_MONTHS:]

    out = {"updated": now.isoformat(timespec="seconds"), "usfs": {}, "routes": {}}
    for no in sorted({no for (no, _) in monthly}):
        rows = {ym: b for (n2, ym), b in monthly.items() if n2 == no}
        recent = [b for ym, b in rows.items() if ym in recent_yms]
        prior = [(ym, b) for ym, b in rows.items() if ym < recent_yms[0]] if recent_yms else []
        r_n = sum(b["n"] for b in recent)
        p_n = sum(b["n"] for _, b in prior)
        r_rating = (sum(b["rating_sum"] for b in recent) /
                    max(1, sum(b["rated_n"] for b in recent)))
        p_rating = (sum(b["rating_sum"] for _, b in prior) /
                    max(1, sum(b["rated_n"] for _, b in prior)))
        flags = []
        tag_stats = {}
        recent_months_set = {ym[5:] for ym in recent_yms}
        for tag in LEXICON:
            r_m = sum(b["tags"].get(tag, 0) for b in recent)
            # seasonal baseline: prior-year reviews from the SAME calendar months
            p_same = [b for ym, b in prior if ym[5:] in recent_months_set]
            p_m = sum(b["tags"].get(tag, 0) for b in p_same)
            p_same_n = sum(b["n"] for b in p_same)
            r_rate = r_m / r_n if r_n else 0
            p_rate = p_m / p_same_n if p_same_n else 0
            if r_m or p_m:
                tag_stats[tag] = {"recent": r_m, "recent_rate": round(r_rate, 3),
                                  "baseline_rate": round(p_rate, 3)}
            if (tag in FLAGGABLE and r_m >= MIN_MENTIONS_TO_FLAG
                    and p_same_n >= 20 and r_rate > p_rate * FLAG_RATIO):
                flags.append(tag)
        if (r_n >= MIN_REVIEWS_FOR_RATING and p_n >= MIN_REVIEWS_FOR_RATING
                and p_rating - r_rating >= RATING_DROP_FLAG):
            flags.append("rating_drop")
        # annual series (2010+; earlier is trace volume) for the info card
        years = {}
        for (n2, yr), a in annual.items():
            if n2 == no and yr >= "2010":
                years[yr] = [a["n"], round(a["rating_sum"] / a["rated_n"], 2) if a["rated_n"] else None]
        # last-6-months theme summary
        yms6 = recent_yms[-6:]
        r6 = [b for ym, b in rows.items() if ym in yms6]
        tags6 = defaultdict(int)
        for b in r6:
            for t, c in b["tags"].items():
                tags6[t] += c
        n6 = sum(b["n"] for b in r6)
        rated6 = sum(b["rated_n"] for b in r6)
        out["usfs"][no] = {
            "name": usfs[no]["name"].title(),
            "reviews_recent": r_n, "reviews_total": r_n + p_n,
            "rating_recent": round(r_rating, 2) if r_n else None,
            "rating_prior": round(p_rating, 2) if p_n else None,
            "tags": tag_stats, "flags": flags,
            "annual": years,
            "recent6": {"n": n6,
                        "rating": round(sum(b["rating_sum"] for b in r6) / rated6, 2) if rated6 else None,
                        "tags": {t: c for t, c in sorted(tags6.items(), key=lambda x: -x[1]) if c > 0}},
            "n_routes": sum(1 for v in matches.values() if no in v),
        }

    out["wilderness_avg_rating"] = round(all_ratings[0] / all_ratings[1], 2) if all_ratings[1] else None

    # route-level matching table (for debugging / the README)
    for tid, name in con.execute("SELECT id, name FROM trails"):
        out["routes"][str(tid)] = {"name": name, "usfs": matches.get(tid) or {}}

    OUT.write_text(json.dumps(out, indent=1))
    flagged = {no: v["flags"] for no, v in out["usfs"].items() if v["flags"]}
    print(f"wrote {OUT.name}: {len(out['usfs'])} USFS trails, flags: {flagged or 'none'}")


if __name__ == "__main__":
    main()
