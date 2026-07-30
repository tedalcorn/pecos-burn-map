#!/usr/bin/env python3
"""Fetch Pecos Wilderness burn/trail data and write GeoJSON for the map.

Sources (all public ArcGIS REST, no auth):
  - USFS EDW Wilderness boundaries      -> wilderness.geojson
  - USFS EDW TrailNFS_Publish           -> trails.geojson
  - NIFC Interagency Fire Perimeter History (all years) -> fires.geojson
  - NIFC WFIGS Year-To-Date perimeters  -> merged into fires.geojson

Run: python3 scripts/fetch_data.py
Writes into docs/data/ plus meta.json with the fetch timestamp.
"""

import io
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image
from shapely.geometry import shape, mapping

OUT = Path(__file__).resolve().parent.parent / "docs" / "data"

WILDERNESS_URL = "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_Wilderness_01/MapServer/0/query"
TRAILS_URL = "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_TrailNFSPublish_01/MapServer/0/query"
FIRE_HISTORY_URL = "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/InterAgencyFirePerimeterHistory_All_Years_View/FeatureServer/0/query"
# History layer lags (stops ~2021); this WFIGS layer covers 2021-present incl. year-to-date
FIRE_2021PLUS_URL = "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Interagency_Perimeters/FeatureServer/0/query"
# MTBS (fires >=1,000 ac, severity-mapped) catches gaps in NIFC — e.g. Medio 2020
MTBS_URL = "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_MTBS_01/MapServer/63/query"
TRAILHEADS_URL = "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_InfraRecreationSites_01/MapServer/0/query"

# Degrees of margin around the wilderness bbox so approach trails
# (Winsor, Panchuela, Jacks Creek, Iron Gate) are included.
BBOX_MARGIN = 0.06
SIMPLIFY_TOL = 0.0002  # ~20 m; keeps GeoJSON small without visible distortion


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def query_all(url, params):
    """Page through an ArcGIS query endpoint, returning GeoJSON features."""
    features = []
    offset = 0
    while True:
        p = dict(params, f="geojson", resultOffset=offset, resultRecordCount=1000)
        for attempt in range(4):
            r = requests.get(url, params=p, timeout=120)
            r.raise_for_status()
            data = r.json()
            if "error" not in data:
                break
            if data["error"].get("code") == 429 and attempt < 3:
                log(f"  rate-limited, waiting 70s (attempt {attempt + 1})")
                time.sleep(70)
                continue
            raise RuntimeError(f"{url}: {data['error']}")
        batch = data.get("features", [])
        features.extend(batch)
        if len(batch) < 1000:
            break
        offset += len(batch)
        log(f"  ...{len(features)} features so far")
    return features


def simplify_features(features, tol=SIMPLIFY_TOL):
    out = []
    for f in features:
        try:
            geom = shape(f["geometry"]).simplify(tol, preserve_topology=True)
            if geom.is_empty:
                continue
            f = dict(f, geometry=mapping(geom))
        except Exception:
            pass  # keep original geometry if simplification chokes
        out.append(f)
    return out


def geo_miles(coords):
    total = 0.0
    for a, b in zip(coords, coords[1:]):
        dx = (a[0] - b[0]) * 111320 * math.cos(math.radians((a[1] + b[1]) / 2))
        dy = (a[1] - b[1]) * 110540
        total += math.hypot(dx, dy)
    return total / 1609.34


def node_trail_network(features):
    """Split trail lines at every junction so each feature is one edge of the
    network (junction-to-junction). Raw USFS features are arbitrary chunks —
    Skyline arrives as a single 43-mile line — which makes segment-level
    interaction (the map's route builder) useless without this."""
    from shapely.geometry import LineString, Point
    from shapely.ops import unary_union, nearest_points
    lines = []
    for f in features:
        g = shape(f["geometry"])
        for part in getattr(g, "geoms", [g]):
            lines.append((part, f["properties"]))

    # Near-miss junctions: a trail's endpoint often sits a few meters off the
    # line it meets (digitization offset), so the union would never split
    # there. Snap endpoints onto any other line within ~25 m first.
    SNAP = 0.00022
    all_union = unary_union([ln for ln, _ in lines])
    snapped = []
    for ln, props in lines:
        coords = list(ln.coords)
        for idx in (0, -1):
            pt = Point(coords[idx][:2])
            others = [o for o, _ in lines if o is not ln and o.distance(pt) < SNAP]
            if others:
                target = nearest_points(pt, unary_union(others))[1]
                if 0 < pt.distance(target) <= SNAP:
                    coords[idx] = (target.x, target.y)
        snapped.append((LineString(coords), props))
    lines = snapped

    noded = unary_union([ln for ln, _ in lines])
    pieces = list(getattr(noded, "geoms", [noded]))
    out = []
    for piece in pieces:
        mid = piece.interpolate(0.5, normalized=True)
        parent = min(lines, key=lambda lp: lp[0].distance(mid))
        if parent[0].distance(mid) > 1e-6:
            continue
        miles = round(geo_miles(list(piece.coords)), 3)
        if miles < 0.03:
            continue  # junction slivers; the map's connect tolerance bridges them
        props = dict(parent[1])
        props["gis_miles"] = miles  # API returns lowercase keys
        out.append({"type": "Feature", "properties": props,
                    "geometry": mapping(piece)})
    log(f"  noded {len(features)} features -> {len(out)} junction-to-junction edges")
    return out


class Elevation:
    """Vertex elevations from AWS's public Terrarium terrain tiles (z13, ~19 m/px)."""

    Z = 13
    URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"

    def __init__(self):
        self.tiles = {}

    def _tile(self, tx, ty):
        if (tx, ty) not in self.tiles:
            r = requests.get(self.URL.format(z=self.Z, x=tx, y=ty), timeout=60)
            r.raise_for_status()
            self.tiles[(tx, ty)] = Image.open(io.BytesIO(r.content)).load()
        return self.tiles[(tx, ty)]

    def at(self, lng, lat):
        n = 2 ** self.Z
        fx = (lng + 180) / 360 * n
        fy = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
        tx, ty = int(fx), int(fy)
        px = min(255, int((fx - tx) * 256))
        py = min(255, int((fy - ty) * 256))
        r, g, b = self._tile(tx, ty)[px, py][:3]
        return round((r * 256 + g + b / 256) - 32768)

    def enrich(self, features):
        """Append elevation (m) as the z-coordinate of every vertex."""
        def add_z(coords):
            if coords and isinstance(coords[0], (int, float)):
                return [coords[0], coords[1], self.at(coords[0], coords[1])]
            return [add_z(c) for c in coords]
        for f in features:
            f["geometry"]["coordinates"] = add_z(f["geometry"]["coordinates"])
        log(f"  elevation: sampled via {len(self.tiles)} terrain tiles")


def round_coords(obj, ndigits=5):
    if isinstance(obj, float):
        return round(obj, ndigits)
    if isinstance(obj, list):
        return [round_coords(x, ndigits) for x in obj]
    return obj


def write_geojson(name, features):
    for f in features:
        f["geometry"]["coordinates"] = round_coords(f["geometry"]["coordinates"])
    fc = {"type": "FeatureCollection", "features": features}
    path = OUT / f"{name}.geojson"
    path.write_text(json.dumps(fc, separators=(",", ":")))
    log(f"  wrote {path.name}: {len(features)} features, {path.stat().st_size/1e6:.1f} MB")


def main():
    OUT.mkdir(parents=True, exist_ok=True)

    log("1/4 Pecos Wilderness boundary")
    wild = query_all(WILDERNESS_URL, {
        "where": "WILDERNESSNAME LIKE 'Pecos%'",
        "outFields": "WILDERNESSNAME,GIS_ACRES",
        "outSR": 4326,
    })
    if not wild:
        sys.exit("No wilderness polygon returned — aborting")
    write_geojson("wilderness", simplify_features(wild))

    # bbox (+margin) drives every other query
    geom = shape(wild[0]["geometry"])
    for f in wild[1:]:
        geom = geom.union(shape(f["geometry"]))
    xmin, ymin, xmax, ymax = geom.bounds
    bbox = f"{xmin-BBOX_MARGIN},{ymin-BBOX_MARGIN},{xmax+BBOX_MARGIN},{ymax+BBOX_MARGIN}"
    log(f"  AOI bbox: {bbox}")
    geo_params = {
        "geometry": bbox,
        "geometryType": "esriGeometryEnvelope",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outSR": 4326,
    }

    log("2/6 USFS trailheads")
    ths = query_all(TRAILHEADS_URL, dict(geo_params,
        where="SITE_SUBTYPE='TRAILHEAD'",
        outFields="public_site_name",
    ))
    # keep only trailheads near the wilderness itself, not the whole bbox
    ths = [f for f in ths if shape(f["geometry"]).distance(geom) < 0.04]
    for f in ths:
        f["properties"] = {"name": (f["properties"].get("public_site_name") or "Trailhead").strip()}
    write_geojson("trailheads", ths)

    log("3/6 USFS trails")
    trails = query_all(TRAILS_URL, dict(geo_params,
        where="1=1",
        outFields="TRAIL_NAME,TRAIL_NO,TRAIL_TYPE,GIS_MILES,TRAIL_SURFACE",
    ))
    trails = simplify_features(trails, tol=0.0001)
    trails = node_trail_network(trails)
    Elevation().enrich(trails)
    write_geojson("trails", trails)

    log("4/6 Fire perimeter history (all years)")
    hist = query_all(FIRE_HISTORY_URL, dict(geo_params,
        where="1=1",
        outFields="INCIDENT,FIRE_YEAR,GIS_ACRES,MAP_METHOD",
    ))
    for f in hist:
        p = f["properties"]
        yr = p.get("FIRE_YEAR")
        try:
            yr = int(str(yr)[:4])
        except (TypeError, ValueError):
            yr = None
        f["properties"] = {
            "name": (p.get("INCIDENT") or "Unknown").title(),
            "year": yr,
            "acres": round(p["GIS_ACRES"]) if p.get("GIS_ACRES") else None,
            "src": "history",
        }

    log("5/6 Fire perimeters 2021-present (WFIGS)")
    recent = query_all(FIRE_2021PLUS_URL, dict(geo_params,
        where="1=1",
        outFields="poly_IncidentName,poly_GISAcres,attr_FireDiscoveryDateTime",
    ))
    for f in recent:
        p = f["properties"]
        disc = p.get("attr_FireDiscoveryDateTime")  # epoch ms
        yr = datetime.fromtimestamp(disc / 1000, tz=timezone.utc).year if disc else None
        f["properties"] = {
            "name": (p.get("poly_IncidentName") or "Unknown").title(),
            "year": yr,
            "acres": round(p["poly_GISAcres"]) if p.get("poly_GISAcres") else None,
            "src": "wfigs",
        }
    # Both layers cover 2021; drop WFIGS perimeters already in history (same name+year).
    seen = {(f["properties"]["name"], f["properties"]["year"]) for f in hist}
    ytd = [f for f in recent if (f["properties"]["name"], f["properties"]["year"]) not in seen]

    log("6/6 MTBS burned areas (gap-fill)")
    mtbs = query_all(MTBS_URL, dict(geo_params,
        where="1=1",
        outFields="fire_name,year,acres",
    ))
    for f in mtbs:
        p = f["properties"]
        f["properties"] = {
            "name": (p.get("fire_name") or "Unknown").title(),
            "year": p.get("year"),
            "acres": round(p["acres"]) if p.get("acres") else None,
            "src": "mtbs",
        }
    # Keep an MTBS perimeter only if NIFC doesn't already have that fire:
    # no same-name+year match, and <50% of its area covered by same-year perimeters.
    existing = hist + ytd
    seen = {(f["properties"]["name"], f["properties"]["year"]) for f in existing}
    gap_fill = []
    for f in mtbs:
        key = (f["properties"]["name"], f["properties"]["year"])
        if key in seen:
            continue
        g = shape(f["geometry"])
        same_year = [shape(e["geometry"]) for e in existing
                     if e["properties"]["year"] == f["properties"]["year"]]
        covered = sum(g.intersection(sy).area for sy in same_year)
        if covered / g.area < 0.5:
            gap_fill.append(f)
            log(f"  MTBS gap-fill: {key[0]} ({key[1]})")

    fires = simplify_features(existing + gap_fill)
    fires.sort(key=lambda f: f["properties"]["year"] or 0)
    write_geojson("fires", fires)

    meta = {
        "fetched_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": {"trails": len(trails), "fires": len(fires)},
        "bbox": [xmin - BBOX_MARGIN, ymin - BBOX_MARGIN, xmax + BBOX_MARGIN, ymax + BBOX_MARGIN],
    }
    (OUT / "meta.json").write_text(json.dumps(meta, indent=2))
    log("done")


if __name__ == "__main__":
    main()
